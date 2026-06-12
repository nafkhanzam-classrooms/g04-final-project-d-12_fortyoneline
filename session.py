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
        # Ready awal perlu dipisah dari ready antar ronde supaya game tidak
        # mulai hanya karena satu pemain menekan READY.
        self.ready = False  # flag untuk ready di lobby (awal game)
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
        # [FIX #4 — Person C] siapa yang knock di ronde ini (untuk hasil showdown)
        self._knocker: Optional[str] = None

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
            
            # Saat ada pemain baru masuk lobby, semua ready lama dibatalkan
            # agar start game tetap menunggu seluruh pemain yang aktif.
            for p in self._players.values():
                p.ready = False
            
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

            # [FIX #12 — Person C] Jangan buat GameEngine baru tiap ronde:
            # lives ikut ter-reset sehingga game tidak pernah berakhir.
            # Engine dibuat sekali; ronde berikutnya cukup start_round() lagi.
            # (TODO Person A: start_round masih membagikan kartu ke pemain
            # yang sudah tereliminasi — giliran mereka memang di-skip, tapi
            # skor mereka ikut dihitung saat showdown.)
            if self._engine is None:
                self._engine = GameEngine(
                    player_ids=active_ids,
                    session_id=self.room_code,
                )
            self._engine.start_round()
            self.state = "DEAL_CARDS"
            self._last_turn_remaining = set()
            self._knocker = None  # [FIX #4 — Person C]

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
                # [FIX #1 — Person C] engine memakai key 'your_hand', bukan
                # 'hand' — sebelumnya YOUR_HAND selalu terkirim kosong.
                hand = state.get("your_hand", [])
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

        # [FIX #5 — Person C] Awal ronde: pemain pertama (5 kartu) wajib
        # INITIAL DISCARD dulu — tanpa ini engine menolak semua aksi dengan
        # "Ronde belum aktif". Client cukup mengirim DISCARD biasa;
        # _handle_discard yang merutekan ke engine.initial_discard().
        with self._lock:
            waiting_initial = (self._engine is not None
                               and self._engine._waiting_initial_discard)
        actions = (["DISCARD"] if waiting_initial
                   else ["TAKE_DECK", "TAKE_DISCARD", "KNOCK"])

        # Kirim valid actions ke pemain aktif
        _send_to(pinfo.sock, {
            "type": "VALID_ACTIONS",
            "payload": {
                "actions": actions,
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
        # [FIX #2/#5/#6/#8 — Person C] ditulis ulang:
        #   - source engine harus 'DECK' uppercase (#2)
        #   - discard_card menerima card_index int, bukan card dict (#3)
        #   - saat menunggu initial discard → pakai initial_discard() (#5)
        #   - discard_card sudah memajukan giliran → jangan _advance_turn lagi (#6)
        #   - cek trigger DECK_EMPTY → showdown (#8)
        result = {}
        with self._lock:
            if self._engine is None:
                return
            if self._engine.current_player != player_id:
                return  # giliran sudah berpindah
            if self._engine._waiting_initial_discard:
                self._engine.initial_discard(player_id, 0)
            else:
                take = self._engine.take_card(player_id, "DECK")
                if not take.get("success"):
                    # deck kosong → tidak ada yang bisa diambil, tutup ronde
                    result = {"trigger": "DECK_EMPTY"}
                else:
                    result = self._engine.discard_card(player_id, 0)

        # Sinkronkan tangan pemain yang kena auto-discard
        pinfo = self._players.get(player_id)
        if pinfo and pinfo.connected:
            with self._lock:
                pstate = self._engine.get_state_for_player(player_id)
            _send_to(pinfo.sock, {
                "type": "YOUR_HAND",
                "payload": {"cards": pstate.get("your_hand", [])},
            })

        self._broadcast_game_state()
        if result.get("trigger") == "DECK_EMPTY":
            self._do_showdown()
            return
        # giliran sudah maju di dalam engine — cukup prompt pemain berikutnya
        self._prompt_current_player()

    def _auto_skip(self, player_id: str):
        """Skip giliran pemain yang disconnect."""
        logger.info(f"ROOM {self.room_code} | AUTO_SKIP player={player_id}")
        self._advance_turn()

    def _advance_turn(self):
        """Minta engine maju ke pemain berikutnya.

        [FIX #6 — Person C] Sekarang HANYA dipakai jalur auto-skip
        (pemain disconnect, belum melakukan aksi apa pun). Jalur discard
        tidak memanggil ini lagi karena engine.discard_card() sudah
        memajukan giliran sendiri — dulu giliran maju dua kali.
        """
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
            # [FIX #9 — Person C] Giliran terakhir mengikuti urutan duduk
            # engine (current_player), bukan next(iter(set)) yang acak.
            # Engine juga memvalidasi take/discard terhadap current_player,
            # jadi urutannya memang HARUS sinkron dengan engine.
            with self._lock:
                if self._engine is None:
                    return
                next_pid = self._engine.current_player
                for _ in range(len(self._engine.player_ids)):
                    if next_pid in remaining:
                        break
                    self._engine._advance_turn()
                    next_pid = self._engine.current_player

            pinfo = self._players.get(next_pid)
            if pinfo and pinfo.connected:
                # [Person C] TURN_INDICATOR juga di last turn supaya
                # countdown & highlight giliran di client tetap jalan.
                self._broadcast({
                    "type": "TURN_INDICATOR",
                    "payload": {
                        "current_turn": next_pid,
                        "username": pinfo.username,
                    },
                })
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
                    # [FIX #9 — Person C] engine ikut maju saat skip
                    if self._engine is not None:
                        self._engine._advance_turn()
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
        """Pemain siap → tandai ready dan cek apakah semua pemain sudah ready."""
        with self._lock:
            if self.state != "LOBBY":
                return
            pinfo = self._players.get(player_id)
            if not pinfo:
                return
            # READY lobby harus jadi per-player gate; jangan langsung start
            # sebelum semua pemain yang connected sudah ready.
            pinfo.ready = True
            
            # Cek apakah semua pemain yang connected sudah ready
            connected_players = [p for p in self._players.values() if p.connected]
            if len(connected_players) < MIN_PLAYERS:
                return
            all_ready = all(p.ready for p in connected_players)
            if not all_ready:
                return
        
        # Semua pemain ready, mulai game
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

        # [FIX #2 — Person C] engine butuh source 'DECK'/'DISCARD' uppercase,
        # dan mengembalikan dict {'success', 'card', ...} — bukan Card/None.
        # Dulu: lowercase selalu gagal, dan karena return dict != None,
        # kegagalan lolos sebagai sukses dengan card = seluruh dict hasil.
        with self._lock:
            result = self._engine.take_card(player_id, source.upper())

        if not (isinstance(result, dict) and result.get("success")):
            err = result.get("error") if isinstance(result, dict) else None
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, {
                    "type": "ERROR",
                    "payload": {"message": err or f"Tidak bisa mengambil dari {source}"},
                })
            return
        card = result["card"]  # sudah berupa dict {'suit', 'rank'}

        # Kirim kartu baru ke pemain tersebut (private)
        pinfo = self._players.get(player_id)
        if pinfo:
            _send_to(pinfo.sock, {
                "type": "CARD_DRAWN",
                "payload": {
                    "source": source,
                    "card": card,
                },
            })
            # Kirim hand terkini (private)
            with self._lock:
                current_state = self._engine.get_state_for_player(player_id)
            _send_to(pinfo.sock, {
                "type": "YOUR_HAND",
                # [FIX #1 — Person C] key engine = 'your_hand'
                "payload": {"cards": current_state.get("your_hand", [])},
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

        # [FIX #3 — Person C] engine.discard_card menerima card_index (int),
        # bukan card dict — terjemahkan dict {suit, rank} ke index di tangan.
        # [FIX #5 — Person C] discard pertama ronde dirutekan ke
        # engine.initial_discard() (buang wajib pemain pertama yang dapat
        # 5 kartu) — dulu tidak pernah dipanggil siapa pun sehingga ronde
        # tidak pernah aktif.
        with self._lock:
            hand = self._engine.hands.get(player_id, [])
            card_index = next(
                (i for i, c in enumerate(hand)
                 if c.suit == card_dict.get("suit")
                 and c.rank == card_dict.get("rank")),
                None,
            )
            if card_index is None:
                result = {"success": False,
                          "error": "Kartu tidak ditemukan di tangan"}
                was_initial = False
            elif self._engine._waiting_initial_discard:
                result = self._engine.initial_discard(player_id, card_index)
                was_initial = True
            else:
                result = self._engine.discard_card(player_id, card_index)
                was_initial = False

        if not result.get("success"):
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, {"type": "ERROR", "payload": {
                    "message": result.get("error", "Discard gagal")}})
            return

        logger.info(f"ROOM {self.room_code} | DISCARD player={player_id} card={card_dict}")
        self._cancel_turn_timer()

        # [Person C] Sinkronkan tangan pemain setelah buang — tanpa ini
        # client masih menampilkan kartu yang sudah dibuang.
        pinfo = self._players.get(player_id)
        if pinfo and pinfo.connected:
            with self._lock:
                pstate = self._engine.get_state_for_player(player_id)
            _send_to(pinfo.sock, {
                "type": "YOUR_HAND",
                "payload": {"cards": pstate.get("your_hand", [])},
            })

        self._broadcast_game_state()

        # [FIX #8 — Person C] deck habis setelah buang → langsung showdown.
        # Dulu trigger DECK_EMPTY dari engine tidak pernah dicek.
        if result.get("trigger") == "DECK_EMPTY":
            self._do_showdown()
            return

        with self._lock:
            cur_state = self.state

        if cur_state == "LAST_TURN_PHASE":
            with self._lock:
                self._last_turn_remaining.discard(player_id)
            self._handle_last_turn_advance()
        else:
            # [FIX #6 — Person C] engine.discard_card()/initial_discard()
            # sudah memajukan giliran — dulu _advance_turn() di sini membuat
            # giliran maju DUA kali dan pemain terlewati.
            self._prompt_current_player()

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
            # [FIX #4 — Person C] JANGAN panggil engine.knock() — method itu
            # langsung menutup ronde & menghitung skor, bertabrakan dengan
            # desain LAST_TURN_PHASE session ini. Keputusan semantik:
            # session yang memegang alur last-turn (sesuai aturan 41: pemain
            # lain dapat satu giliran terakhir), knocker cukup dicatat dan
            # showdown dilakukan via engine.force_showdown(knocker=...).
            if not self._engine.round_active:
                return
            self._knocker = player_id
            self._engine.log.record(self._engine.round_number, player_id,
                                    "KNOCK", {})

            # Tentukan pemain yang mendapat giliran terakhir (semua kecuali knocker)
            active = self._engine.active_players
            self._last_turn_remaining = set(p for p in active if p != player_id)
            # [FIX #4 — Person C] majukan giliran engine melewati knocker
            # supaya last turn berjalan sesuai urutan duduk (lihat FIX #9).
            self._engine._advance_turn()
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
            # [FIX #4 — Person C] sertakan knocker agar muncul di ROUND_END
            result = self._engine.force_showdown(knocker=self._knocker)
            self._knocker = None

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

    # ------------------------------------------------------------------
    # [FIX #10 — Person C] handle_packet men-dispatch CHAT/EMOJI_REACT ke
    # dua method ini, tapi keduanya tidak pernah didefinisikan →
    # AttributeError mematikan thread handler & memutus koneksi pemain.
    def _handle_chat(self, player_id: str, msg: dict):
        pinfo = self._players.get(player_id)
        if pinfo is None:
            return
        text = str(msg.get("payload", {}).get("text", "")).strip()[:200]
        if not text:
            return
        self._broadcast({
            "type": "CHAT_BROADCAST",
            "payload": {
                "player_id": player_id,
                "username": pinfo.username,
                "text": text,
            },
        })

    def _handle_emoji(self, player_id: str, msg: dict):
        pinfo = self._players.get(player_id)
        if pinfo is None:
            return
        emoji = str(msg.get("payload", {}).get("emoji", "")).strip()[:8]
        if not emoji:
            return
        self._broadcast({
            "type": "EMOJI_BROADCAST",
            "payload": {
                "player_id": player_id,
                "username": pinfo.username,
                "emoji": emoji,
            },
        })

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
        # [FIX #7 — Person C] key dari engine.get_full_state() adalah
        # current_player / deck_remaining / top_discard / hands — bukan
        # current_turn / deck_count / discard_top / hand_counts. Dulu semua
        # field ini selalu None di client.
        hands = full.get("hands", {})
        public_state = {
            "current_turn": full.get("current_player"),
            "deck_count": full.get("deck_remaining"),
            "discard_top": full.get("top_discard"),
            "round": full.get("round"),
            "players": [
                {
                    "player_id": pid,
                    "username": p.username,
                    "lives": full.get("lives", {}).get(pid),
                    "hand_count": len(hands.get(pid, [])),
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

        # Jika giliran pemain ini, auto-skip kecuali pada initial discard.
        # Saat ronde baru dimulai, hanya pemain pertama yang punya 5 kartu
        # dan boleh membuang; kalau dia disconnect, giliran harus tetap
        # menunggu reconnect agar tidak pindah ke pemain kedua yang hanya
        # punya 4 kartu.
        # [FIX #11 — Person C] dulu `state` hanya ter-assign di dalam if,
        # sehingga disconnect saat bukan giliran pemain (atau saat masih di
        # lobby) crash dengan UnboundLocalError.
        with self._lock:
            is_current = (self._engine is not None
                          and self._engine.current_player == player_id)
            state = self.state
            waiting_initial = bool(self._engine and self._engine._waiting_initial_discard)
        if is_current and state == "PLAYER_TURN" and not waiting_initial:
            self._auto_skip(player_id)
        elif is_current and state == "LAST_TURN_PHASE":
            # [Person C] tanpa ini sesi macet menunggu last turn pemain
            # yang sudah putus.
            with self._lock:
                self._last_turn_remaining.discard(player_id)
                if self._engine is not None:
                    self._engine._advance_turn()
            self._handle_last_turn_advance()

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
                current = self._engine.current_player
                waiting_initial = self._engine._waiting_initial_discard
            else:
                snapshot = {}
                current = None
                waiting_initial = False

            # Snapshot reconnect harus membawa roster lengkap supaya client
            # tidak menampilkan Pxxx sebagai nama pemain setelah reconnect.
            snapshot["players"] = self._player_list_snapshot()

            # Kirim status giliran yang benar agar client tidak menyimpan
            # aksi discard/take yang sudah kedaluwarsa saat reconnect.
            if current == player_id and self.state in ("PLAYER_TURN", "LAST_TURN_PHASE"):
                snapshot["valid_actions"] = (["DISCARD"] if waiting_initial
                                             else ["TAKE_DECK", "TAKE_DISCARD", "KNOCK"])
                snapshot["current_turn_username"] = self._players[player_id].username
            else:
                snapshot["valid_actions"] = []
                snapshot["current_turn_username"] = (
                    self._players[current].username if current in self._players else None
                )

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
                    # [FIX #1 — Person C] key engine = 'your_hand'
                    "payload": {"cards": pstate.get("your_hand", [])},
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
