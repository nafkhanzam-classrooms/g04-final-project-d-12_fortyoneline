"""
session.py — Game Kartu 41
Person B: Lobby, Room Manager, Turn Manager, Broadcast, Reconnect

Tanggung jawab:
  - RoomManager : membuat room, registry semua room, route koneksi masuk
  - GameSession  : state satu room (lobby → matchmaking → ronde → game over)
                   turn manager, broadcast, private send, knock resolution,
                   reconnect & disconnect handling, ready-next-round gate

Asumsi interface Person A:
  game_engine.py:
    - GameEngine(player_ids, session_id)
    - engine.start_round()          → dict  (state awal ronde)
    - engine.initial_discard()      → Card
    - engine.take_card(player_id, source)  → Card  (source: "deck"|"discard")
    - engine.discard_card(player_id, card_dict) → bool
    - engine.knock(player_id)       → bool
    - engine.force_showdown()       → dict  (hasil round)
    - engine.get_state_for_player(player_id) → dict
    - engine.get_full_state()       → dict
    - engine.get_reconnect_snapshot(player_id) → dict
    - engine.current_player        → str (player_id giliran aktif)
    - engine.active_players        → list[str]

  protocol.py (opsional — fallback ke _encode/_decode lokal jika belum ada):
    - encode_game_state(state)
    - encode_action_request(actions)
    - encode_round_end(result)
    - encode_game_over(winner)
    - encode_error(message)
    - encode_player_joined(players)
    - encode_game_start()
"""

import socket
import threading
import json
import logging
import random
import string
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Coba import dari game_engine dan protocol Person A.
# Jika belum ada, gunakan stub sederhana agar server tetap bisa diuji.
# ---------------------------------------------------------------------------
try:
    from game_engine import GameEngine
    _HAS_ENGINE = True
except ImportError:
    GameEngine = None  # type: ignore
    _HAS_ENGINE = False
    logger.warning("game_engine.py belum tersedia — stub mode aktif")

# ---------------------------------------------------------------------------
# Encode helper (fallback jika protocol.py Person A belum tersedia)
# ---------------------------------------------------------------------------
def _encode(msg: dict) -> bytes:
    return (json.dumps(msg) + "\n").encode("utf-8")


def _send_to(sock: socket.socket, msg: dict):
    """Kirim satu packet ke socket. Abaikan error OS (koneksi sudah tutup)."""
    try:
        sock.sendall(_encode(msg))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Konstanta
# ---------------------------------------------------------------------------
MIN_PLAYERS = 2
MAX_PLAYERS = 4
TURN_TIMEOUT_SECONDS = 30          # timer per giliran (fitur bonus)
RECONNECT_GRACE_SECONDS = 60       # waktu maksimal untuk reconnect
BETWEEN_ROUND_DELAY = 3            # detik jeda otomatis antar ronde


# ---------------------------------------------------------------------------
# Util: generate room code & player id
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


# ---------------------------------------------------------------------------
# PlayerInfo — data satu pemain dalam sebuah room
# ---------------------------------------------------------------------------
class PlayerInfo:
    def __init__(self, player_id: str, username: str, sock: socket.socket):
        self.player_id = player_id
        self.username = username
        self.sock = sock
        self.connected = True
        self.disconnected_at: Optional[float] = None
        self.ready_next_round = False  # flag untuk gate antar ronde


# ---------------------------------------------------------------------------
# GameSession — satu room, satu game
# ---------------------------------------------------------------------------
class GameSession:
    """
    Mengelola state satu room dari Lobby hingga Game Over.

    State machine:
      LOBBY → MATCHMAKING → DEAL_CARDS → PLAYER_TURN → KNOCK_TRIGGERED
      → LAST_TURN_PHASE → REVEAL → SCORE_CALCULATION → LIFE_REDUCTION
      → PLAYER_ELIMINATION → (NEXT_ROUND | GAME_OVER)
    """

    def __init__(self, room_code: str):
        self.room_code = room_code
        self.state = "LOBBY"

        # {player_id: PlayerInfo}
        self._players: dict[str, PlayerInfo] = {}
        self._lock = threading.RLock()  # RLock agar method internal bisa saling panggil

        # Game engine (diinisialisasi saat game dimulai)
        self._engine: Optional[GameEngine] = None

        # Turn timer
        self._turn_timer: Optional[threading.Timer] = None

        # Tracking last-turn phase
        self._last_turn_remaining: set = set()  # player_id yang belum giliran terakhir

        # Reconnect grace timers: {player_id: Timer}
        self._reconnect_timers: dict[str, threading.Timer] = {}

    # ======================================================================
    # Akses data pemain
    # ======================================================================
    def get_connected_player_ids(self) -> list[str]:
        with self._lock:
            return [pid for pid, p in self._players.items() if p.connected]

    def get_all_player_ids(self) -> list[str]:
        with self._lock:
            return list(self._players.keys())

    def player_count(self) -> int:
        with self._lock:
            return len(self._players)

    def connected_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._players.values() if p.connected)

    # ======================================================================
    # Lobby: tambah pemain
    # ======================================================================
    def add_player(self, player_id: str, username: str, sock: socket.socket) -> Optional[str]:
        """
        Tambahkan pemain ke lobby.
        Return error string jika gagal, None jika berhasil.
        """
        with self._lock:
            if self.state != "LOBBY":
                return "Room sudah dalam permainan"
            if len(self._players) >= MAX_PLAYERS:
                return "Room penuh"

            self._players[player_id] = PlayerInfo(player_id, username, sock)
            logger.info(f"ROOM {self.room_code} | player_joined player_id={player_id} username={username}")

        # Broadcast ke semua: ada pemain baru
        self._broadcast({
            "type": "PLAYER_JOINED",
            "payload": {
                "player_id": player_id,
                "username": username,
                "players": self._player_list_snapshot(),
                "room_code": self.room_code,
            },
        })
        return None

    def _player_list_snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {"player_id": p.player_id, "username": p.username, "connected": p.connected}
                for p in self._players.values()
            ]

    # ======================================================================
    # Matchmaking: cek apakah bisa mulai
    # ======================================================================
    def try_start_game(self) -> bool:
        """
        Dipanggil setelah pemain mengirim READY, atau otomatis saat
        jumlah pemain sudah MIN_PLAYERS dan semua connect.
        Return True jika game berhasil dimulai.
        """
        with self._lock:
            if self.state != "LOBBY":
                return False
            if len(self._players) < MIN_PLAYERS:
                return False
            self.state = "MATCHMAKING"

        logger.info(f"ROOM {self.room_code} | MATCHMAKING dimulai")
        self._broadcast({"type": "GAME_START", "payload": {"message": "Permainan dimulai!"}})
        self._start_round()
        return True

    # ======================================================================
    # Ronde
    # ======================================================================
    def _start_round(self):
        with self._lock:
            active_ids = list(self._players.keys())
            if not _HAS_ENGINE:
                logger.warning("GameEngine tidak tersedia, ronde tidak dimulai")
                return

            self._engine = GameEngine(
                player_ids=active_ids,
                session_id=self.room_code,
            )
            self._engine.start_round()
            self.state = "DEAL_CARDS"
            self._last_turn_remaining = set()

            # Reset ready flag
            for p in self._players.values():
                p.ready_next_round = False

        logger.info(f"ROOM {self.room_code} | DEAL_CARDS")

        # Kirim kartu tangan masing-masing (private)
        self._send_all_hands()

        # Broadcast game state awal
        self._broadcast_game_state()

        with self._lock:
            self.state = "PLAYER_TURN"

        # Kirim giliran pertama
        self._prompt_current_player()

    def _send_all_hands(self):
        """Kirim YOUR_HAND secara private ke setiap pemain."""
        with self._lock:
            if self._engine is None:
                return
            for pid, pinfo in self._players.items():
                if not pinfo.connected:
                    continue
                state = self._engine.get_state_for_player(pid)
                hand = state.get("hand", [])
                _send_to(pinfo.sock, {
                    "type": "YOUR_HAND",
                    "payload": {"cards": hand},
                })

    # ======================================================================
    # Turn management
    # ======================================================================
    def _prompt_current_player(self):
        """Kirim VALID_ACTIONS ke pemain aktif, mulai timer."""
        with self._lock:
            if self._engine is None:
                return
            current = self._engine.current_player
            pinfo = self._players.get(current)

        if pinfo is None or not pinfo.connected:
            # Pemain disconnect, auto-skip
            self._auto_skip(current)
            return

        # Broadcast siapa yang giliran
        self._broadcast({
            "type": "TURN_INDICATOR",
            "payload": {
                "current_turn": current,
                "username": pinfo.username,
            },
        })

        # Kirim valid actions ke pemain aktif
        _send_to(pinfo.sock, {
            "type": "VALID_ACTIONS",
            "payload": {
                "actions": ["TAKE_DECK", "TAKE_DISCARD", "KNOCK"],
            },
        })

        # Start turn timer
        self._cancel_turn_timer()
        self._turn_timer = threading.Timer(
            TURN_TIMEOUT_SECONDS,
            self._on_turn_timeout,
            args=[current],
        )
        self._turn_timer.start()

    def _cancel_turn_timer(self):
        if self._turn_timer:
            self._turn_timer.cancel()
            self._turn_timer = None

    def _on_turn_timeout(self, player_id: str):
        """Auto-discard kartu pertama di tangan jika timer habis."""
        logger.info(f"ROOM {self.room_code} | TURN_TIMEOUT player={player_id}")
        with self._lock:
            if self._engine is None:
                return
            if self._engine.current_player != player_id:
                return  # giliran sudah berpindah
            # Ambil deck dulu agar ada kartu baru, lalu discard kartu pertama
            state = self._engine.get_state_for_player(player_id)
            hand = state.get("hand", [])
            if hand:
                first_card = hand[0]
                self._engine.take_card(player_id, "deck")
                self._engine.discard_card(player_id, first_card)

        self._broadcast_game_state()
        self._advance_turn()

    def _auto_skip(self, player_id: str):
        """Skip giliran pemain yang disconnect."""
        logger.info(f"ROOM {self.room_code} | AUTO_SKIP player={player_id}")
        self._advance_turn()

    def _advance_turn(self):
        """Minta engine maju ke pemain berikutnya."""
        with self._lock:
            if self._engine is None:
                return
            state = self.state

        if state == "LAST_TURN_PHASE":
            self._handle_last_turn_advance()
        elif state == "PLAYER_TURN":
            with self._lock:
                self._engine._advance_turn()
            self._broadcast_game_state()
            self._prompt_current_player()

    def _handle_last_turn_advance(self):
        """
        Setelah seorang pemain selesai giliran terakhir,
        cek apakah semua sudah selesai → showdown.
        """
        with self._lock:
            remaining = self._last_turn_remaining.copy()

        if not remaining:
            self._do_showdown()
        else:
            # Prompt pemain berikutnya dalam last turn
            with self._lock:
                if self._engine is None:
                    return
                next_pid = next(iter(remaining))

            pinfo = self._players.get(next_pid)
            if pinfo and pinfo.connected:
                _send_to(pinfo.sock, {
                    "type": "VALID_ACTIONS",
                    "payload": {"actions": ["TAKE_DECK", "TAKE_DISCARD"]},
                })
                self._cancel_turn_timer()
                self._turn_timer = threading.Timer(
                    TURN_TIMEOUT_SECONDS,
                    self._on_last_turn_timeout,
                    args=[next_pid],
                )
                self._turn_timer.start()
            else:
                # Pemain ini disconnect, skip
                with self._lock:
                    self._last_turn_remaining.discard(next_pid)
                self._handle_last_turn_advance()

    def _on_last_turn_timeout(self, player_id: str):
        logger.info(f"ROOM {self.room_code} | LAST_TURN_TIMEOUT player={player_id}")
        with self._lock:
            self._last_turn_remaining.discard(player_id)
        self._handle_last_turn_advance()

    # ======================================================================
    # Packet handler (dipanggil dari ClientHandler)
    # ======================================================================
    def handle_packet(self, player_id: str, msg: dict):
        msg_type = msg.get("type", "")

        if msg_type == "READY":
            self._handle_ready(player_id)
        elif msg_type == "PING":
            self._handle_ping(player_id)
        elif msg_type == "TAKE_DECK":
            self._handle_take(player_id, "deck")
        elif msg_type == "TAKE_DISCARD":
            self._handle_take(player_id, "discard")
        elif msg_type == "DISCARD":
            self._handle_discard(player_id, msg)
        elif msg_type == "KNOCK":
            self._handle_knock(player_id)
        elif msg_type == "CHAT":
            self._handle_chat(player_id, msg)
        elif msg_type == "EMOJI_REACT":
            self._handle_emoji(player_id, msg)
        elif msg_type == "READY_NEXT_ROUND":
            self._handle_ready_next_round(player_id)
        else:
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, {
                    "type": "ERROR",
                    "payload": {"message": f"Tipe packet tidak dikenal: {msg_type}"},
                })

    # ------------------------------------------------------------------
    def _handle_ready(self, player_id: str):
        """Pemain siap → coba mulai game."""
        with self._lock:
            if self.state != "LOBBY":
                return
        self.try_start_game()

    # ------------------------------------------------------------------
    def _handle_ping(self, player_id: str):
        pinfo = self._players.get(player_id)
        if pinfo:
            _send_to(pinfo.sock, {
                "type": "PONG",
                "payload": {"timestamp": time.time()},
            })

    # ------------------------------------------------------------------
    def _handle_take(self, player_id: str, source: str):
        """Proses TAKE_DECK atau TAKE_DISCARD."""
        with self._lock:
            if self._engine is None:
                return
            state = self.state
            current = self._engine.current_player

        # Validasi: hanya pemain aktif yang boleh aksi di giliran normal
        if state == "PLAYER_TURN" and current != player_id:
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, {
                    "type": "ERROR",
                    "payload": {"message": "Bukan giliran Anda"},
                })
            return

        # Last turn: hanya pemain yang masuk daftar last-turn
        if state == "LAST_TURN_PHASE":
            with self._lock:
                if player_id not in self._last_turn_remaining:
                    pinfo = self._players.get(player_id)
                    if pinfo:
                        _send_to(pinfo.sock, {"type": "ERROR", "payload": {"message": "Bukan giliran terakhir Anda"}})
                    return

        with self._lock:
            card = self._engine.take_card(player_id, source)

        if card is None:
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, {
                    "type": "ERROR",
                    "payload": {"message": f"Tidak bisa mengambil dari {source}"},
                })
            return

        # Kirim kartu baru ke pemain tersebut (private)
        pinfo = self._players.get(player_id)
        if pinfo:
            _send_to(pinfo.sock, {
                "type": "CARD_DRAWN",
                "payload": {
                    "source": source,
                    "card": card if isinstance(card, dict) else card.to_dict(),
                },
            })
            # Kirim hand terkini (private)
            with self._lock:
                current_state = self._engine.get_state_for_player(player_id)
            _send_to(pinfo.sock, {
                "type": "YOUR_HAND",
                "payload": {"cards": current_state.get("hand", [])},
            })

        self._broadcast_game_state()

        # Setelah take, pemain wajib discard — kirim valid actions DISCARD saja
        if pinfo and pinfo.connected:
            _send_to(pinfo.sock, {
                "type": "VALID_ACTIONS",
                "payload": {"actions": ["DISCARD"]},
            })

        logger.info(f"ROOM {self.room_code} | {source.upper()} player={player_id}")

    # ------------------------------------------------------------------
    def _handle_discard(self, player_id: str, msg: dict):
        with self._lock:
            if self._engine is None:
                return
            state = self.state
            current = self._engine.current_player

        # Validasi giliran
        if state == "PLAYER_TURN" and current != player_id:
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, {"type": "ERROR", "payload": {"message": "Bukan giliran Anda"}})
            return

        card_dict = msg.get("payload", {}).get("card", {})
        if not card_dict:
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, {"type": "ERROR", "payload": {"message": "Payload kartu tidak valid"}})
            return

        with self._lock:
            ok = self._engine.discard_card(player_id, card_dict)

        if not ok:
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, {"type": "ERROR", "payload": {"message": "Kartu tidak ditemukan di tangan"}})
            return

        logger.info(f"ROOM {self.room_code} | DISCARD player={player_id} card={card_dict}")
        self._cancel_turn_timer()
        self._broadcast_game_state()

        with self._lock:
            cur_state = self.state

        if cur_state == "LAST_TURN_PHASE":
            with self._lock:
                self._last_turn_remaining.discard(player_id)
            self._handle_last_turn_advance()
        else:
            self._advance_turn()

    # ------------------------------------------------------------------
    def _handle_knock(self, player_id: str):
        with self._lock:
            if self._engine is None:
                return
            if self.state != "PLAYER_TURN":
                return
            if self._engine.current_player != player_id:
                pinfo = self._players.get(player_id)
                if pinfo:
                    _send_to(pinfo.sock, {"type": "ERROR", "payload": {"message": "Bukan giliran Anda"}})
                return
            ok = self._engine.knock(player_id)
            if not ok:
                return

            # Tentukan pemain yang mendapat giliran terakhir (semua kecuali knocker)
            active = self._engine.active_players
            self._last_turn_remaining = set(p for p in active if p != player_id)
            self.state = "KNOCK_TRIGGERED"

        logger.info(f"ROOM {self.room_code} | KNOCK player={player_id}")
        self._cancel_turn_timer()

        knocker_username = self._players[player_id].username if player_id in self._players else player_id
        self._broadcast({
            "type": "KNOCK_NOTIFICATION",
            "payload": {
                "player_id": player_id,
                "username": knocker_username,
                "message": f"{knocker_username} melakukan KNOCK! Giliran terakhir untuk semua...",
            },
        })

        with self._lock:
            self.state = "LAST_TURN_PHASE"

        self._handle_last_turn_advance()

    # ------------------------------------------------------------------
    def _do_showdown(self):
        """Setelah semua last turn: reveal → score → life → eliminasi."""
        with self._lock:
            if self._engine is None:
                return
            self.state = "REVEAL"
            result = self._engine.force_showdown()

        logger.info(f"ROOM {self.room_code} | SHOWDOWN result={result}")

        # Kirim hasil ronde
        self._broadcast({
            "type": "ROUND_END",
            "payload": result,
        })

        # Cek game over
        with self._lock:
            active = self._engine.active_players if self._engine else []

        if len(active) <= 1:
            winner_id = active[0] if active else None
            winner_name = ""
            if winner_id and winner_id in self._players:
                winner_name = self._players[winner_id].username
            self._broadcast({
                "type": "GAME_OVER",
                "payload": {
                    "winner_id": winner_id,
                    "winner_username": winner_name,
                    "message": f"{winner_name} memenangkan permainan!",
                },
            })
            with self._lock:
                self.state = "GAME_OVER"
            logger.info(f"ROOM {self.room_code} | GAME_OVER winner={winner_id}")
            return

        # Minta konfirmasi semua pemain sebelum ronde berikutnya
        with self._lock:
            self.state = "WAITING_READY"
            for p in self._players.values():
                p.ready_next_round = False

        self._broadcast({
            "type": "NEXT_ROUND_PROMPT",
            "payload": {"message": "Kirim READY_NEXT_ROUND untuk melanjutkan ke ronde berikutnya"},
        })

    # ------------------------------------------------------------------
    def _handle_ready_next_round(self, player_id: str):
        """
        Terima konfirmasi siap dari pemain.
        Mulai ronde baru ketika SEMUA pemain aktif sudah siap.
        """
        with self._lock:
            if self.state != "WAITING_READY":
                return
            if player_id not in self._players:
                return
            self._players[player_id].ready_next_round = True

            # Cek apakah semua pemain aktif (connected) sudah ready
            active_connected = [
                p for p in self._players.values()
                if p.connected
            ]
            all_ready = all(p.ready_next_round for p in active_connected)

        if all_ready:
            logger.info(f"ROOM {self.room_code} | Semua pemain ready, mulai ronde baru")
            self._start_round()

    # ======================================================================
    # Broadcast & private send
    # ======================================================================
    def _broadcast(self, msg: dict):
        """Kirim packet ke semua pemain yang terhubung."""
        with self._lock:
            targets = [(p.player_id, p.sock) for p in self._players.values() if p.connected]
        for pid, sock in targets:
            try:
                sock.sendall(_encode(msg))
            except OSError:
                pass

    def _broadcast_game_state(self):
        """
        Kirim GAME_STATE (tanpa kartu lawan) ke semua pemain.
        Kartu tangan masing-masing dikirim via YOUR_HAND secara private.
        """
        with self._lock:
            if self._engine is None:
                return
            full = self._engine.get_full_state()
            players_snap = list(self._players.items())

        # Buat GAME_STATE publik (tanpa hand)
        public_state = {
            "current_turn": full.get("current_turn"),
            "deck_count": full.get("deck_count"),
            "discard_top": full.get("discard_top"),
            "round": full.get("round"),
            "players": [
                {
                    "player_id": pid,
                    "username": p.username,
                    "lives": full.get("lives", {}).get(pid),
                    "hand_count": full.get("hand_counts", {}).get(pid),
                    "connected": p.connected,
                }
                for pid, p in players_snap
            ],
        }

        self._broadcast({"type": "GAME_STATE", "payload": public_state})

    # ======================================================================
    # Disconnect & Reconnect
    # ======================================================================
    def on_player_disconnect(self, player_id: str):
        with self._lock:
            pinfo = self._players.get(player_id)
            if pinfo is None:
                return
            pinfo.connected = False
            pinfo.disconnected_at = time.time()

        logger.info(f"ROOM {self.room_code} | DISCONNECT player={player_id}")

        self._broadcast({
            "type": "PLAYER_DISCONNECTED",
            "payload": {
                "player_id": player_id,
                "username": pinfo.username,
                "message": f"{pinfo.username} terputus dari server",
            },
        })

        # Set grace timer: jika tidak reconnect dalam RECONNECT_GRACE_SECONDS,
        # pemain dihapus dari ronde
        timer = threading.Timer(
            RECONNECT_GRACE_SECONDS,
            self._on_reconnect_timeout,
            args=[player_id],
        )
        with self._lock:
            self._reconnect_timers[player_id] = timer
        timer.start()

        # Jika giliran pemain ini, auto-skip
        with self._lock:
            if self._engine and self._engine.current_player == player_id:
                state = self.state
        if state in ("PLAYER_TURN",):
            self._auto_skip(player_id)

    def reconnect_player(self, player_id: str, new_sock: socket.socket) -> tuple[bool, str]:
        """
        Panggil setelah validasi identitas di RoomManager.
        Kirim snapshot state terkini ke pemain yang reconnect.
        Return (True, "") jika sukses, (False, pesan_error) jika gagal.
        """
        with self._lock:
            pinfo = self._players.get(player_id)
            if pinfo is None:
                return False, "Player tidak ditemukan di room ini"

            pinfo.sock = new_sock
            pinfo.connected = True
            pinfo.disconnected_at = None

            # Batalkan grace timer
            timer = self._reconnect_timers.pop(player_id, None)
            if timer:
                timer.cancel()

        logger.info(f"ROOM {self.room_code} | RECONNECT player={player_id}")

        # Kirim snapshot ke pemain
        with self._lock:
            if self._engine:
                snapshot = self._engine.get_reconnect_snapshot(player_id)
            else:
                snapshot = {}

        _send_to(new_sock, {
            "type": "RECONNECT_ACK",
            "payload": {
                "player_id": player_id,
                "state": snapshot,
                "message": "Berhasil reconnect",
            },
        })

        # Kirim hand terkini (private)
        with self._lock:
            if self._engine:
                pstate = self._engine.get_state_for_player(player_id)
                _send_to(new_sock, {
                    "type": "YOUR_HAND",
                    "payload": {"cards": pstate.get("hand", [])},
                })

        # Broadcast ke semua bahwa pemain kembali
        self._broadcast({
            "type": "PLAYER_RECONNECTED",
            "payload": {
                "player_id": player_id,
                "username": pinfo.username,
            },
        })

        return True, ""

    def _on_reconnect_timeout(self, player_id: str):
        """Pemain tidak reconnect dalam batas waktu → hapus dari ronde aktif."""
        with self._lock:
            pinfo = self._players.get(player_id)
            if pinfo is None or pinfo.connected:
                return
            self._reconnect_timers.pop(player_id, None)

        logger.info(f"ROOM {self.room_code} | RECONNECT_TIMEOUT player={player_id}, dihapus dari ronde")

        self._broadcast({
            "type": "PLAYER_ELIMINATED_DISCONNECT",
            "payload": {
                "player_id": player_id,
                "username": pinfo.username if pinfo else player_id,
            },
        })

        # Hapus dari game engine jika memungkinkan
        # (bergantung interface Person A — skip jika tidak ada metode remove)


# ---------------------------------------------------------------------------
# RoomManager — registry semua room, entry point untuk server.py
# ---------------------------------------------------------------------------
class RoomManager:
    """
    Mengelola semua room (GameSession) yang aktif.

    - Buat room baru saat pemain pertama login tanpa room_code
    - Route koneksi masuk ke room yang tepat via room_code
    - Registry player_id → room_code untuk validasi reconnect
    """

    def __init__(self):
        self._sessions: dict[str, GameSession] = {}   # room_code → GameSession
        self._player_room: dict[str, str] = {}         # player_id → room_code
        self._player_names: dict[str, str] = {}        # player_id → username
        self._lock = threading.Lock()

    # ======================================================================
    def register_player(
        self,
        username: str,
        room_code: Optional[str],
        sock: socket.socket,
    ) -> tuple[str, str, Optional[str]]:
        """
        Daftarkan pemain ke room.
        - room_code=None → buat room baru
        - room_code=X    → gabung room X

        Return: (player_id, room_code, error_or_None)
        """
        with self._lock:
            # Generate player_id unik
            player_id = _new_player_id(set(self._player_room.keys()))

            if room_code is None:
                # Buat room baru
                new_code = _new_room_code(set(self._sessions.keys()))
                session = GameSession(new_code)
                self._sessions[new_code] = session
                room_code = new_code

            else:
                session = self._sessions.get(room_code)
                if session is None:
                    return "", "", f"Room '{room_code}' tidak ditemukan"

            self._player_room[player_id] = room_code
            self._player_names[player_id] = username

        # Tambahkan ke session (di luar lock agar tidak deadlock)
        err = session.add_player(player_id, username, sock)
        if err:
            with self._lock:
                self._player_room.pop(player_id, None)
                self._player_names.pop(player_id, None)
            return "", "", err

        return player_id, room_code, None

    # ======================================================================
    def reconnect_player(
        self,
        player_id: str,
        room_code: str,
        new_sock: socket.socket,
    ) -> tuple[bool, str]:
        """
        Validasi identitas dan delegasi ke GameSession.reconnect_player.
        """
        with self._lock:
            stored_room = self._player_room.get(player_id)

        if stored_room is None:
            return False, "Player ID tidak dikenal"
        if stored_room != room_code:
            return False, "Room code tidak cocok"

        session = self.get_session(room_code)
        if session is None:
            return False, "Session tidak ditemukan"

        return session.reconnect_player(player_id, new_sock)

    # ======================================================================
    def get_session(self, room_code: str) -> Optional[GameSession]:
        with self._lock:
            return self._sessions.get(room_code)

    def list_rooms(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "room_code": code,
                    "player_count": s.player_count(),
                    "state": s.state,
                }
                for code, s in self._sessions.items()
            ]
