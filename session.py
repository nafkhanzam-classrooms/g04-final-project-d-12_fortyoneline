"""
session.py — Game Kartu 41
Person B: Lobby, Room Manager, Turn Manager, Broadcast, Reconnect

REVISI v3 — disesuaikan dengan kebutuhan Person C (client):

Perubahan dari v2:
  1. handle_packet() : tambah routing untuk TAKE_DECK, TAKE_DISCARD, KNOCK, DISCARD
                       sebagai tipe packet langsung (bukan ACTION envelope)
  2. DISCARD         : Person C kirim objek kartu {suit, rank} bukan card_index.
                       Server cari index kartu di tangan engine via _find_card_index().
  3. _build_game_state_for_player() : assembler baru yang menghasilkan GAME_STATE
                       sesuai spec Person C — memetakan nama field engine ke nama
                       yang diharapkan renderer:
                         round          → round_number
                         current_player → current_turn
                         your_hand      → hand
                         top_discard    → discard_top
                         deck_remaining → deck_count
                       Menambahkan field baru:
                         room_code, phase, valid_actions, turn_deadline, players[]
  4. _broadcast_all_game_states() : gunakan _build_game_state_for_player()
                       (bukan raw encode_game_state dari engine)
  5. _turn_deadline   : simpan timestamp deadline timer agar bisa dimasukkan ke GAME_STATE
  6. valid_actions per pemain : simpan di _valid_actions_map agar bisa dikirim dalam GAME_STATE
"""

import socket
import threading
import json
import logging
import random
import string
import time
from typing import Optional

from protocol import (
    encode, decode,
    encode_game_state,
    encode_action_request,
    encode_action_response,
    encode_round_end,
    encode_game_over,
    encode_error,
    encode_player_joined,
    encode_game_start,
)
from game_engine import GameEngine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanta
# ---------------------------------------------------------------------------
MIN_PLAYERS          = 2
MAX_PLAYERS          = 4
TURN_TIMEOUT_SECONDS = 30
RECONNECT_GRACE_SECS = 60


# ---------------------------------------------------------------------------
# Util
# ---------------------------------------------------------------------------
def _new_room_code(existing: set) -> str:
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in existing:
            return code


def _new_player_id(existing: set) -> str:
    while True:
        pid = "P" + "".join(random.choices(string.digits, k=3))
        if pid not in existing:
            return pid


def _send_to(sock: socket.socket, data: bytes):
    """Kirim bytes ke socket. Abaikan error OS."""
    try:
        sock.sendall(data)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# PlayerInfo
# ---------------------------------------------------------------------------
class PlayerInfo:
    def __init__(self, player_id: str, username: str, sock: socket.socket):
        self.player_id        = player_id
        self.username         = username
        self.sock             = sock
        self.connected        = True
        self.disconnected_at: Optional[float] = None
        self.ready_next_round = False


# ---------------------------------------------------------------------------
# GameSession
# ---------------------------------------------------------------------------
class GameSession:
    """
    State machine satu room:
      LOBBY → MATCHMAKING → INITIAL_DISCARD → PLAYER_TURN
      → KNOCK_TRIGGERED → LAST_TURN_PHASE
      → ROUND_END → WAITING_READY → (NEXT_ROUND | GAME_OVER)

    Catatan INITIAL_DISCARD:
      Setelah start_round(), pemain pertama mendapat 5 kartu dan wajib
      membuang 1 (index 0–4) sebelum giliran normal dimulai.
      State INITIAL_DISCARD menunggu aksi INITIAL_DISCARD dari pemain pertama.
    """

    def __init__(self, room_code: str):
        self.room_code = room_code
        self.state     = "LOBBY"

        self._players: dict[str, PlayerInfo] = {}
        self._lock = threading.RLock()

        self._engine: Optional[GameEngine] = None

        self._turn_timer: Optional[threading.Timer]           = None
        self._last_turn_remaining: set                        = set()
        self._reconnect_timers: dict[str, threading.Timer]   = {}

        # Untuk dikirim dalam GAME_STATE ke renderer Person C
        self._turn_deadline: Optional[float] = None      # epoch timestamp deadline giliran
        self._valid_actions_map: dict[str, list] = {}    # player_id → valid_actions saat ini

    # ======================================================================
    # Akses pemain
    # ======================================================================
    def get_connected_player_ids(self) -> list[str]:
        with self._lock:
            return [pid for pid, p in self._players.items() if p.connected]

    def player_count(self) -> int:
        with self._lock:
            return len(self._players)

    # ======================================================================
    # Lobby
    # ======================================================================
    def add_player(self, player_id: str, username: str, sock: socket.socket) -> Optional[str]:
        """Return pesan error jika gagal, None jika berhasil."""
        with self._lock:
            if self.state != "LOBBY":
                return "Room sudah dalam permainan"
            if len(self._players) >= MAX_PLAYERS:
                return "Room penuh"
            self._players[player_id] = PlayerInfo(player_id, username, sock)

        logger.info(f"ROOM {self.room_code} | PLAYER_JOINED player_id={player_id} username={username}")

        total  = self.player_count()
        needed = MIN_PLAYERS - total if total < MIN_PLAYERS else 0

        # Broadcast notifikasi pemain baru (gunakan encode_player_joined dari protocol)
        self._broadcast(encode_player_joined(player_id, total, needed))

        # Juga broadcast daftar pemain lengkap agar client bisa render lobby
        self._broadcast(encode("LOBBY_STATE", {
            "players": self._player_list_snapshot(),
            "room_code": self.room_code,
        }))
        return None

    def _player_list_snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {"player_id": p.player_id, "username": p.username, "connected": p.connected}
                for p in self._players.values()
            ]

    # ======================================================================
    # Matchmaking
    # ======================================================================
    def try_start_game(self) -> bool:
        with self._lock:
            if self.state != "LOBBY":
                return False
            if len(self._players) < MIN_PLAYERS:
                return False
            self.state = "MATCHMAKING"

        logger.info(f"ROOM {self.room_code} | MATCHMAKING")
        self._start_round()
        return True

    # ======================================================================
    # Ronde
    # ======================================================================
    def _start_round(self):
        with self._lock:
            # Hanya pemain yang masih hidup (kecuali ronde pertama: semua)
            if self._engine is not None:
                player_ids = self._engine.active_players
            else:
                player_ids = list(self._players.keys())

            # Re-inisialisasi engine hanya di ronde pertama;
            # ronde berikutnya cukup panggil start_round() pada engine yang sama
            if self._engine is None:
                self._engine = GameEngine(
                    player_ids=player_ids,
                    session_id=self.room_code,
                )

            result = self._engine.start_round()
            first_player = result["first_player"]
            self.state = "INITIAL_DISCARD"
            self._last_turn_remaining = set()
            for p in self._players.values():
                p.ready_next_round = False

        logger.info(f"ROOM {self.room_code} | START_ROUND round={result['round']} first={first_player}")

        # Broadcast game_start
        with self._lock:
            all_ids = list(self._players.keys())
        self._broadcast(encode_game_start(all_ids, first_player))

        # Kirim tangan masing-masing secara private
        self._send_all_hands()

        # Minta pemain pertama untuk initial_discard
        self._prompt_initial_discard(first_player)

    def _send_all_hands(self):
        """Kirim GAME_STATE (versi Person C) ke setiap pemain yang terhubung."""
        with self._lock:
            if self._engine is None:
                return
            connected = [(pid, p.sock) for pid, p in self._players.items() if p.connected]

        for pid, sock in connected:
            state = self._build_game_state_for_player(pid)
            _send_to(sock, encode("GAME_STATE", state))

    def _prompt_initial_discard(self, first_player: str):
        """
        Set valid_actions untuk pemain pertama (INITIAL_DISCARD),
        lalu broadcast GAME_STATE ke semua — renderer Person C akan lihat
        valid_actions berisi ["INITIAL_DISCARD"] hanya untuk pemain pertama.
        """
        with self._lock:
            self._valid_actions_map = {first_player: ["INITIAL_DISCARD"]}
            self._turn_deadline = time.time() + TURN_TIMEOUT_SECONDS

        self._broadcast_all_game_states()

    # ======================================================================
    # Turn management
    # ======================================================================
    def _prompt_current_player(self):
        """Kirim VALID_ACTIONS ke pemain aktif, start timer."""
        with self._lock:
            if self._engine is None:
                return
            current = self._engine.current_player
            pinfo   = self._players.get(current)

        if pinfo is None or not pinfo.connected:
            self._auto_skip(current)
            return

        # Catat valid_actions dan turn_deadline untuk dimasukkan ke GAME_STATE
        actions = ["TAKE_DECK", "TAKE_DISCARD", "KNOCK"]
        deadline = time.time() + TURN_TIMEOUT_SECONDS
        with self._lock:
            self._valid_actions_map = {current: actions}
            self._turn_deadline = deadline

        self._broadcast_all_game_states()   # GAME_STATE sudah include valid_actions + turn_deadline

        self._cancel_turn_timer()
        self._turn_timer = threading.Timer(
            TURN_TIMEOUT_SECONDS, self._on_turn_timeout, args=[current]
        )
        self._turn_timer.start()

    def _cancel_turn_timer(self):
        if self._turn_timer:
            self._turn_timer.cancel()
            self._turn_timer = None

    def _on_turn_timeout(self, player_id: str):
        """Auto-take dari deck + discard kartu pertama jika timer habis."""
        logger.info(f"ROOM {self.room_code} | TURN_TIMEOUT player={player_id}")
        with self._lock:
            if self._engine is None:
                return
            if self._engine.current_player != player_id:
                return
            # take dari deck
            take_result = self._engine.take_card(player_id, "DECK")
            if not take_result.get("success"):
                return
            # discard index 0 (kartu pertama di tangan)
            discard_result = self._engine.discard_card(player_id, 0)

        self._broadcast_all_game_states()
        if discard_result.get("trigger") == "DECK_EMPTY":
            self._do_force_showdown()
        # discard_card sudah panggil _advance_turn internal
        else:
            self._prompt_current_player()

    def _auto_skip(self, player_id: str):
        """Skip giliran pemain disconnect: take deck + discard index 0."""
        logger.info(f"ROOM {self.room_code} | AUTO_SKIP player={player_id}")
        with self._lock:
            if self._engine is None:
                return
            take_result = self._engine.take_card(player_id, "DECK")
            if take_result.get("success"):
                discard_result = self._engine.discard_card(player_id, 0)
            else:
                discard_result = {}
        if discard_result.get("trigger") == "DECK_EMPTY":
            self._do_force_showdown()
        else:
            self._prompt_current_player()

    # ======================================================================
    # Packet handler (dipanggil dari ClientHandler)
    # ======================================================================
    def handle_packet(self, player_id: str, msg: dict):
        msg_type = msg.get("type", "")
        payload  = msg.get("payload", {}) or {}

        # --- Tipe langsung dari Person C ---
        if msg_type == "READY":
            self._handle_ready(player_id)

        elif msg_type == "TAKE_DECK":
            # Person C: {"type": "TAKE_DECK", "payload": {}}
            self._handle_take(player_id, {"source": "DECK"})

        elif msg_type == "TAKE_DISCARD":
            # Person C: {"type": "TAKE_DISCARD", "payload": {}}
            self._handle_take(player_id, {"source": "DISCARD"})

        elif msg_type == "KNOCK":
            # Person C: {"type": "KNOCK", "payload": {}}
            self._handle_knock(player_id)

        elif msg_type == "DISCARD":
            # Person C: {"type": "DISCARD", "payload": {"card": {"suit":..,"rank":..}}}
            # atau      {"type": "DISCARD", "payload": {"suit":..,"rank":..}}
            self._handle_discard_by_card(player_id, payload)

        elif msg_type == "READY_NEXT_ROUND":
            self._handle_ready_next_round(player_id)

        elif msg_type == "CHAT":
            self._handle_chat(player_id, payload)

        elif msg_type == "EMOJI_REACT":
            self._handle_emoji(player_id, payload)

        elif msg_type == "PING":
            pinfo = self._players.get(player_id)
            if pinfo:
                ts = payload.get("timestamp", time.time())
                _send_to(pinfo.sock, encode("PONG", {"timestamp": ts}))

        # --- ACTION envelope (jalur internal / kompatibilitas) ---
        elif msg_type == "ACTION":
            action = payload.get("action", "")
            if action == "TAKE":
                self._handle_take(player_id, payload)
            elif action == "DISCARD":
                self._handle_discard(player_id, payload)
            elif action == "KNOCK":
                self._handle_knock(player_id)
            elif action == "INITIAL_DISCARD":
                self._handle_initial_discard(player_id, payload)
            else:
                pinfo = self._players.get(player_id)
                if pinfo:
                    _send_to(pinfo.sock, encode_error(f"Aksi tidak dikenal: {action!r}"))

    # ------------------------------------------------------------------
    def _handle_ready(self, player_id: str):
        with self._lock:
            if self.state != "LOBBY":
                return
        self.try_start_game()

    # ------------------------------------------------------------------
    def _find_card_index(self, player_id: str, card_dict: dict) -> int:
        """
        Cari index kartu di tangan pemain berdasarkan suit+rank.
        Return -1 jika tidak ditemukan.
        Diperlukan karena Person C mengirim objek kartu, bukan card_index.
        """
        if self._engine is None:
            return -1
        hand = self._engine.hands.get(player_id, [])
        suit = card_dict.get("suit", "").lower()
        rank = card_dict.get("rank", "")
        for i, card in enumerate(hand):
            if card.suit.lower() == suit and card.rank == rank:
                return i
        return -1

    # ------------------------------------------------------------------
    def _handle_discard_by_card(self, player_id: str, payload: dict):
        """
        Handler DISCARD dari Person C — payload berisi objek kartu:
          {"type": "DISCARD", "payload": {"card": {"suit": "hearts", "rank": "A"}}}
        atau langsung:
          {"type": "DISCARD", "payload": {"suit": "hearts", "rank": "A"}}

        Server cari card_index di tangan engine, lalu panggil _handle_discard().
        """
        # Terima dua format: {card: {...}} atau langsung {suit:.., rank:..}
        card_dict = payload.get("card") or payload
        if not isinstance(card_dict, dict) or not card_dict.get("rank"):
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, encode_error(
                    "DISCARD membutuhkan payload kartu: {\"card\": {\"suit\":..., \"rank\":...}}"
                ))
            return

        with self._lock:
            card_index = self._find_card_index(player_id, card_dict)

        if card_index < 0:
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, encode_error(
                    f"Kartu {card_dict} tidak ditemukan di tangan Anda"
                ))
            return

        # Delegate ke handler yang sudah ada (pakai card_index)
        self._handle_discard(player_id, {"card_index": card_index})

    # ------------------------------------------------------------------
    def _handle_initial_discard(self, player_id: str, payload: dict):
        """
        Proses INITIAL_DISCARD dari pemain pertama.
        payload harus mengandung card_index (int).
        """
        with self._lock:
            if self.state != "INITIAL_DISCARD":
                pinfo = self._players.get(player_id)
                if pinfo:
                    _send_to(pinfo.sock, encode_error("Bukan fase initial discard"))
                return
            if self._engine is None:
                return

            card_index = payload.get("card_index")
            if not isinstance(card_index, int) or card_index < 0:
                pinfo = self._players.get(player_id)
                if pinfo:
                    _send_to(pinfo.sock, encode_error("card_index tidak valid"))
                return

            result = self._engine.initial_discard(player_id, card_index)

        if not result.get("success"):
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, encode_error(result.get("error", "Gagal initial discard")))
            return

        logger.info(f"ROOM {self.room_code} | INITIAL_DISCARD player={player_id} card_index={card_index}")

        with self._lock:
            self.state = "PLAYER_TURN"

        # Kirim konfirmasi ke pemain yang discard
        pinfo = self._players.get(player_id)
        if pinfo:
            _send_to(pinfo.sock, encode_action_response(True, {
                "discarded": result.get("discarded"),
                "next_player": result.get("next_player"),
            }))

        # Update hand pemain pertama (sekarang punya 4 kartu)
        with self._lock:
            state = self._engine.get_state_for_player(player_id)
        if pinfo and pinfo.connected:
            _send_to(pinfo.sock, encode_game_state(state))

        # Broadcast game state ke semua
        self._broadcast_all_game_states()
        self._prompt_current_player()

    # ------------------------------------------------------------------
    def _handle_take(self, player_id: str, payload: dict):
        """
        Proses ACTION TAKE.
        payload: {'action': 'TAKE', 'source': 'DECK'|'DISCARD'}
        source harus uppercase (sesuai engine Person A).
        """
        with self._lock:
            if self._engine is None:
                return
            state = self.state
            current = self._engine.current_player

        # Validasi giliran normal
        if state == "PLAYER_TURN" and current != player_id:
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, encode_error("Bukan giliran Anda"))
            return

        # Validasi giliran terakhir
        if state == "LAST_TURN_PHASE":
            with self._lock:
                if player_id not in self._last_turn_remaining:
                    pinfo = self._players.get(player_id)
                    if pinfo:
                        _send_to(pinfo.sock, encode_error("Bukan giliran terakhir Anda"))
                    return

        # source harus uppercase: 'DECK' atau 'DISCARD'
        source = payload.get("source", "").upper()
        if source not in ("DECK", "DISCARD"):
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, encode_error("source harus 'DECK' atau 'DISCARD'"))
            return

        with self._lock:
            result = self._engine.take_card(player_id, source)

        if not result.get("success"):
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, encode_error(result.get("error", "Gagal mengambil kartu")))
            return

        logger.info(f"ROOM {self.room_code} | TAKE player={player_id} source={source}")

        # Setelah take, pemain hanya boleh DISCARD — update valid_actions_map
        with self._lock:
            self._valid_actions_map = {player_id: ["DISCARD"]}
            self._turn_deadline = time.time() + TURN_TIMEOUT_SECONDS

        # Broadcast game state ke semua (sudah include valid_actions + turn_deadline)
        self._broadcast_all_game_states()

        # Kirim ACTION_RESP ke pemain yang take (opsional, sebagai konfirmasi)
        pinfo = self._players.get(player_id)
        if pinfo and pinfo.connected:
            _send_to(pinfo.sock, encode_action_response(True, {
                "card": result.get("card"),
                "deck_remaining": result.get("deck_remaining"),
            }))

    # ------------------------------------------------------------------
    def _handle_discard(self, player_id: str, payload: dict):
        """
        Proses ACTION DISCARD.
        payload: {'action': 'DISCARD', 'card_index': int}
        Menggunakan card_index, bukan card dict — sesuai engine Person A.
        """
        with self._lock:
            if self._engine is None:
                return
            state = self.state

        # Validasi state: hanya boleh discard saat PLAYER_TURN atau LAST_TURN_PHASE
        if state not in ("PLAYER_TURN", "LAST_TURN_PHASE"):
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, encode_error("Tidak bisa discard sekarang"))
            return

        card_index = payload.get("card_index")
        if not isinstance(card_index, int) or card_index < 0:
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, encode_error("card_index tidak valid"))
            return

        with self._lock:
            result = self._engine.discard_card(player_id, card_index)

        if not result.get("success"):
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, encode_error(result.get("error", "Gagal membuang kartu")))
            return

        logger.info(f"ROOM {self.room_code} | DISCARD player={player_id} index={card_index} card={result.get('discarded')}")

        self._cancel_turn_timer()

        # Reset valid_actions dan turn_deadline — akan diset ulang saat giliran berikutnya
        with self._lock:
            self._valid_actions_map = {}
            self._turn_deadline = None

        # Kirim konfirmasi ke yang discard
        pinfo = self._players.get(player_id)
        if pinfo and pinfo.connected:
            _send_to(pinfo.sock, encode_action_response(True, {
                "discarded": result.get("discarded"),
                "next_player": result.get("next_player"),
            }))

        self._broadcast_all_game_states()

        # Cek trigger DECK_EMPTY dari engine
        if result.get("trigger") == "DECK_EMPTY":
            self._do_force_showdown()
            return

        # Routing berdasarkan state saat ini
        with self._lock:
            cur_state = self.state

        if cur_state == "LAST_TURN_PHASE":
            with self._lock:
                self._last_turn_remaining.discard(player_id)
            self._handle_last_turn_advance()
        else:
            # discard_card di engine sudah panggil _advance_turn internal
            self._prompt_current_player()

    # ------------------------------------------------------------------
    def _handle_knock(self, player_id: str):
        """
        Proses ACTION KNOCK.
        engine.knock() langsung resolve round dan return dict hasil.
        Tidak ada last-turn phase jika knock — sesuai logika engine Person A.
        """
        with self._lock:
            if self._engine is None:
                return
            if self.state != "PLAYER_TURN":
                pinfo = self._players.get(player_id)
                if pinfo:
                    _send_to(pinfo.sock, encode_error("Tidak bisa knock sekarang"))
                return
            if self._engine.current_player != player_id:
                pinfo = self._players.get(player_id)
                if pinfo:
                    _send_to(pinfo.sock, encode_error("Bukan giliran Anda"))
                return

            # knock() langsung resolve round (sesuai engine Person A)
            result = self._engine.knock(player_id)

        if not result.get("success"):
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, encode_error(result.get("error", "Knock gagal")))
            return

        self._cancel_turn_timer()
        logger.info(f"ROOM {self.room_code} | KNOCK player={player_id}")

        knocker_username = self._players[player_id].username if player_id in self._players else player_id
        self._broadcast(encode("KNOCK_NOTIFICATION", {
            "player_id": player_id,
            "username": knocker_username,
            "message": f"{knocker_username} melakukan KNOCK!",
        }))

        # Proses hasil round
        self._process_round_result(result)

    # ------------------------------------------------------------------
    def _do_force_showdown(self):
        """Dipanggil saat deck habis (trigger DECK_EMPTY dari discard_card)."""
        with self._lock:
            if self._engine is None:
                return
            result = self._engine.force_showdown()
        logger.info(f"ROOM {self.room_code} | FORCE_SHOWDOWN (deck empty)")
        self._process_round_result(result)

    # ------------------------------------------------------------------
    def _handle_last_turn_advance(self):
        """
        Setelah seorang pemain selesai giliran terakhir.
        Jika semua sudah → force_showdown.
        """
        with self._lock:
            remaining = set(self._last_turn_remaining)

        if not remaining:
            self._do_force_showdown()
        else:
            next_pid = next(iter(remaining))
            pinfo = self._players.get(next_pid)
            if pinfo and pinfo.connected:
                _send_to(pinfo.sock, encode_action_request(next_pid, ["TAKE"]))
                self._cancel_turn_timer()
                self._turn_timer = threading.Timer(
                    TURN_TIMEOUT_SECONDS, self._on_last_turn_timeout, args=[next_pid]
                )
                self._turn_timer.start()
            else:
                with self._lock:
                    self._last_turn_remaining.discard(next_pid)
                self._handle_last_turn_advance()

    def _on_last_turn_timeout(self, player_id: str):
        logger.info(f"ROOM {self.room_code} | LAST_TURN_TIMEOUT player={player_id}")
        with self._lock:
            self._last_turn_remaining.discard(player_id)
        self._handle_last_turn_advance()

    # ------------------------------------------------------------------
    def _process_round_result(self, result: dict):
        """
        Setelah round selesai (dari knock atau force_showdown):
        broadcast hasil, cek game_over, atau masuk WAITING_READY.
        """
        with self._lock:
            self.state = "ROUND_END"

        # Broadcast hasil ronde (encode_round_end dari protocol.py)
        self._broadcast(encode_round_end(result))
        logger.info(f"ROOM {self.room_code} | ROUND_END scores={result.get('scores')}")

        if result.get("game_over"):
            champion = result.get("champion", "")
            self._broadcast(encode_game_over(champion))  # signature: champion str
            with self._lock:
                self.state = "GAME_OVER"
            logger.info(f"ROOM {self.room_code} | GAME_OVER champion={champion}")
            return

        # Minta konfirmasi semua pemain sebelum ronde berikutnya
        with self._lock:
            self.state = "WAITING_READY"
            for p in self._players.values():
                p.ready_next_round = False

        self._broadcast(encode("NEXT_ROUND_PROMPT", {
            "message": "Kirim READY_NEXT_ROUND untuk lanjut ke ronde berikutnya",
            "scores": result.get("scores"),
            "lives": result.get("lives"),
            "eliminated": result.get("eliminated", []),
        }))

    # ------------------------------------------------------------------
    def _handle_ready_next_round(self, player_id: str):
        """Ronde baru mulai setelah SEMUA pemain aktif yang terhubung kirim ini."""
        with self._lock:
            if self.state != "WAITING_READY":
                return
            if player_id not in self._players:
                return
            self._players[player_id].ready_next_round = True

            active_connected = [
                p for p in self._players.values()
                if p.connected and (self._engine is None or self._engine.lives.get(p.player_id, 0) > 0)
            ]
            all_ready = all(p.ready_next_round for p in active_connected)

        if all_ready:
            logger.info(f"ROOM {self.room_code} | Semua siap, mulai ronde baru")
            self._start_round()

    # ------------------------------------------------------------------
    def _handle_chat(self, player_id: str, payload: dict):
        message = payload.get("message", "").strip()
        if not message:
            return
        if len(message) > 200:
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, encode_error("Pesan terlalu panjang (maks 200 karakter)"))
            return

        with self._lock:
            username = self._players[player_id].username if player_id in self._players else player_id

        logger.info(f"ROOM {self.room_code} | CHAT player={player_id} msg={message[:50]}")
        self._broadcast(encode("CHAT_BROADCAST", {
            "player_id": player_id,
            "username": username,
            "message": message,
        }))

    def _handle_emoji(self, player_id: str, payload: dict):
        emoji = payload.get("emoji", "")
        if not emoji:
            return
        with self._lock:
            username = self._players[player_id].username if player_id in self._players else player_id
        self._broadcast(encode("EMOJI_REACT", {
            "player_id": player_id,
            "username": username,
            "emoji": emoji,
        }))

    # ======================================================================
    # Broadcast helpers
    # ======================================================================
    def _broadcast(self, data: bytes):
        """Kirim bytes ke semua pemain yang terhubung."""
        with self._lock:
            targets = [(p.player_id, p.sock) for p in self._players.values() if p.connected]
        for _, sock in targets:
            _send_to(sock, data)

    def _build_game_state_for_player(self, viewer_id: str) -> dict:
        """
        Assembler GAME_STATE lengkap sesuai spec renderer Person C.

        Memetakan field engine ke nama yang diharapkan Person C:
          round          → round_number
          current_player → current_turn
          your_hand      → hand
          top_discard    → discard_top
          deck_remaining → deck_count

        Menambahkan field yang tidak ada di engine:
          room_code, phase, valid_actions, turn_deadline, latency_ms, players[]
        """
        with self._lock:
            if self._engine is None:
                return {}

            # Raw state dari engine (kartu lawan sudah disembunyikan)
            raw = self._engine.get_state_for_player(viewer_id)
            phase = self.state
            room_code = self.room_code
            valid_actions = list(self._valid_actions_map.get(viewer_id, []))
            turn_deadline = self._turn_deadline

            # Susun players[] dari data engine + PlayerInfo
            players_out = []
            for pid, pinfo in self._players.items():
                if pid == viewer_id:
                    hand_count = raw.get("hand_count", 0)
                else:
                    opp = raw.get("opponents", {}).get(pid, {})
                    hand_count = opp.get("card_count", 0)

                players_out.append({
                    "player_id":  pid,
                    "username":   pinfo.username,
                    "lives":      raw.get("lives", {}).get(pid, 0),
                    "hand_count": hand_count,
                    "connected":  pinfo.connected,
                })

        return {
            # Field yang dibutuhkan Person C
            "room_code":    room_code,
            "round_number": raw.get("round"),          # engine: "round"
            "phase":        phase,
            "latency_ms":   None,                      # diukur client-side via PING/PONG
            "deck_count":   raw.get("deck_remaining"), # engine: "deck_remaining"
            "discard_top":  raw.get("top_discard"),    # engine: "top_discard"
            "hand":         raw.get("your_hand", []),  # engine: "your_hand"
            "current_turn": raw.get("current_player"), # engine: "current_player"
            "valid_actions": valid_actions,
            "turn_deadline": turn_deadline,
            "players":      players_out,
        }

    def _broadcast_all_game_states(self):
        """
        Kirim GAME_STATE (versi Person C) secara private ke masing-masing pemain.
        Kartu lawan TIDAK pernah terekspos.
        """
        with self._lock:
            if self._engine is None:
                return
            connected_players = [(pid, p.sock) for pid, p in self._players.items() if p.connected]

        for pid, sock in connected_players:
            state = self._build_game_state_for_player(pid)
            _send_to(sock, encode("GAME_STATE", state))

    # ======================================================================
    # Disconnect & Reconnect
    # ======================================================================
    def on_player_disconnect(self, player_id: str):
        with self._lock:
            pinfo = self._players.get(player_id)
            if pinfo is None:
                return
            pinfo.connected        = False
            pinfo.disconnected_at  = time.time()
            username               = pinfo.username

        logger.info(f"ROOM {self.room_code} | DISCONNECT player={player_id}")
        self._broadcast(encode("PLAYER_DISCONNECTED", {
            "player_id": player_id,
            "username": username,
        }))

        # Grace timer sebelum dianggap abandon
        timer = threading.Timer(
            RECONNECT_GRACE_SECS, self._on_reconnect_timeout, args=[player_id]
        )
        with self._lock:
            self._reconnect_timers[player_id] = timer
        timer.start()

        # Jika giliran dia di fase normal, auto-skip
        with self._lock:
            cur_state = self.state
            is_current = (
                self._engine is not None
                and self._engine.current_player == player_id
            )
        if cur_state == "PLAYER_TURN" and is_current:
            self._auto_skip(player_id)

    def reconnect_player(self, player_id: str, new_sock: socket.socket) -> tuple[bool, str]:
        """Panggil setelah validasi di RoomManager."""
        with self._lock:
            pinfo = self._players.get(player_id)
            if pinfo is None:
                return False, "Player tidak ditemukan di room ini"

            pinfo.sock            = new_sock
            pinfo.connected       = True
            pinfo.disconnected_at = None

            timer = self._reconnect_timers.pop(player_id, None)
            if timer:
                timer.cancel()

            if self._engine:
                snapshot = self._engine.get_reconnect_snapshot(player_id)
            else:
                snapshot = {}

        logger.info(f"ROOM {self.room_code} | RECONNECT player={player_id}")

        # Kirim GAME_STATE lengkap (versi Person C) — sudah termasuk hand, phase, dsb
        state = self._build_game_state_for_player(player_id)
        state["reconnected"] = True     # flag tambahan agar client tahu ini full-sync
        _send_to(new_sock, encode("RECONNECT_ACK", state))

        # Broadcast ke semua bahwa pemain kembali
        self._broadcast(encode("PLAYER_RECONNECTED", {
            "player_id": player_id,
            "username": pinfo.username,
        }))
        return True, ""

    def _on_reconnect_timeout(self, player_id: str):
        with self._lock:
            pinfo = self._players.get(player_id)
            if pinfo is None or pinfo.connected:
                return
            self._reconnect_timers.pop(player_id, None)
            username = pinfo.username

        logger.info(f"ROOM {self.room_code} | RECONNECT_TIMEOUT player={player_id}")
        self._broadcast(encode("PLAYER_ELIMINATED_DISCONNECT", {
            "player_id": player_id,
            "username": username,
        }))

    # ======================================================================
    # Helper untuk RoomManager
    # ======================================================================
    def get_session_id(self) -> Optional[str]:
        with self._lock:
            return self._engine.session_id if self._engine else None


# ---------------------------------------------------------------------------
# RoomManager
# ---------------------------------------------------------------------------
class RoomManager:
    """
    Registry semua room (GameSession).
    - register_player : buat room baru atau join room existing via room_code
    - reconnect_player: validasi dengan session_id (bukan room_code)
    - get_session     : ambil GameSession via room_code
    - get_session_id  : ambil session_id engine untuk dikirim ke client saat LOGIN_ACK
    """

    def __init__(self):
        self._sessions: dict[str, GameSession] = {}    # room_code → GameSession
        self._player_room: dict[str, str]      = {}    # player_id → room_code
        # Mapping session_id (dari engine) → room_code (untuk reconnect)
        self._session_id_map: dict[str, str]   = {}    # session_id → room_code
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def register_player(
        self,
        username: str,
        room_code: Optional[str],
        sock: socket.socket,
    ) -> tuple[str, str, Optional[str]]:
        """Return (player_id, room_code, error_or_None)."""
        with self._lock:
            player_id = _new_player_id(set(self._player_room.keys()))

            if room_code is None:
                # Buat room baru
                new_code = _new_room_code(set(self._sessions.keys()))
                session  = GameSession(new_code)
                self._sessions[new_code] = session
                room_code = new_code
            else:
                session = self._sessions.get(room_code)
                if session is None:
                    return "", "", f"Room '{room_code}' tidak ditemukan"

            self._player_room[player_id] = room_code

        err = session.add_player(player_id, username, sock)
        if err:
            with self._lock:
                self._player_room.pop(player_id, None)
            return "", "", err

        return player_id, room_code, None

    # ------------------------------------------------------------------
    def reconnect_player(
        self,
        player_id: str,
        session_id: str,
        new_sock: socket.socket,
    ) -> tuple[bool, str, str]:
        """
        Validasi identity dengan session_id (bukan room_code).
        Return (ok, room_code, error).
        """
        with self._lock:
            room_code = self._player_room.get(player_id)

        if room_code is None:
            return False, "", "Player ID tidak dikenal"

        session = self.get_session(room_code)
        if session is None:
            return False, "", "Session tidak ditemukan"

        # Verifikasi session_id cocok dengan engine di room tersebut
        engine_session_id = session.get_session_id()
        if engine_session_id != session_id:
            return False, "", "Session ID tidak cocok"

        ok, err = session.reconnect_player(player_id, new_sock)
        if not ok:
            return False, "", err
        return True, room_code, ""

    # ------------------------------------------------------------------
    def get_session(self, room_code: str) -> Optional[GameSession]:
        with self._lock:
            return self._sessions.get(room_code)

    def get_session_id(self, room_code: str) -> Optional[str]:
        """Ambil session_id engine — dikirim ke client saat LOGIN_ACK untuk kebutuhan reconnect."""
        session = self.get_session(room_code)
        if session is None:
            return None
        return session.get_session_id()

    def list_rooms(self) -> list[dict]:
        with self._lock:
            return [
                {"room_code": c, "player_count": s.player_count(), "state": s.state}
                for c, s in self._sessions.items()
            ]
