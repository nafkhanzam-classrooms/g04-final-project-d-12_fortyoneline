"""
server.py — Game Kartu 41
Person B: Server utama (TCP bind/listen, accept loop, thread-per-client)

Tanggung jawab:
  - Bind dan listen socket TCP
  - Accept koneksi masuk, spawn thread per client
  - Routing packet masuk ke RoomManager / GameSession
  - Registry koneksi aktif
  - Reconnect & disconnect handling
  - UDP relay untuk voice chat

REVISI v3 — disesuaikan dengan kebutuhan Person C (client):
  - Terima tipe packet langsung dari Person C:
      RECONNECT, TAKE_DECK, TAKE_DISCARD, DISCARD, KNOCK
    (bukan ACTION envelope seperti format Person A)
  - Tipe RECONNECT diterima di _handshake() (Person C tidak kirim RECONNECT_REQ)
  - Whitelist tipe packet diperbarui sesuai aksi Person C
  - Validasi per tipe dipisah: ACTION (Person A internal) tetap divalidasi
    dengan validate_action_packet; tipe Person C divalidasi minimal
"""

import socket
import threading
import json
import logging
import time
import os

from protocol import encode, decode, encode_error, recv_message
from game_engine import validate_action_packet, validate_ping_packet, validate_reconnect_packet
from session import RoomManager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("logs/game.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TCP_HOST = "0.0.0.0"
TCP_PORT = 5555
UDP_PORT = 5556


# ---------------------------------------------------------------------------
# ClientHandler — satu thread per koneksi TCP
# ---------------------------------------------------------------------------
class ClientHandler(threading.Thread):
    """
    Thread yang hidup selama satu koneksi TCP aktif.

    Lifecycle:
      1. Terima packet LOGIN → registrasi ke room_manager
         Atau packet RECONNECT_REQ → validasi dan restore session
      2. Loop: baca packet → validasi → dispatch ke session
      3. Saat disconnect → beri tahu session
    """

    def __init__(
        self,
        conn: socket.socket,
        addr: tuple,
        room_manager: "RoomManager",
        server: "GameServer",
    ):
        super().__init__(daemon=True)
        self.conn = conn
        self.addr = addr
        self.room_manager = room_manager
        self.server = server

        self.player_id: str | None = None
        self.room_code: str | None = None
        self.running = True

    # ------------------------------------------------------------------
    def run(self):
        logger.info(f"CONNECT {self.addr}")
        try:
            self._handshake()
            if self.player_id and self.room_code:
                self._message_loop()
        except Exception as exc:
            logger.exception(f"ClientHandler error {self.addr}: {exc}")
        finally:
            self._on_disconnect()

    # ------------------------------------------------------------------
    # Handshake: terima LOGIN atau RECONNECT_REQ
    # ------------------------------------------------------------------
    def _handshake(self):
        msg = recv_message(self.conn)
        if msg is None:
            return

        msg_type = msg.get("type", "")

        # --- LOGIN baru ---
        if msg_type == "LOGIN":
            payload = msg.get("payload", {})
            if not isinstance(payload, dict):
                self.conn.sendall(encode_error("Payload LOGIN tidak valid"))
                return

            username  = payload.get("username", "").strip()
            room_code = payload.get("room_code", "").strip().upper() or None

            if not username:
                self.conn.sendall(encode_error("Username tidak boleh kosong"))
                return

            player_id, room_code, err = self.room_manager.register_player(
                username=username,
                room_code=room_code,
                sock=self.conn,
            )
            if err:
                self.conn.sendall(encode_error(err))
                return

            self.player_id = player_id
            self.room_code = room_code
            self.server.register_connection(player_id, self)

            # Kirim LOGIN_ACK
            self.conn.sendall(encode("LOGIN_ACK", {
                "player_id": player_id,
                "room_code": room_code,
                "session_id": self.room_manager.get_session_id(room_code),
                "message": "Berhasil bergabung ke room",
            }))
            logger.info(f"LOGIN player_id={player_id} username={username} room={room_code}")

        # --- RECONNECT (Person C kirim "RECONNECT", bukan "RECONNECT_REQ") ---
        # Diterima keduanya agar kompatibel jika Person A/C tidak sinkron
        elif msg_type in ("RECONNECT", "RECONNECT_REQ"):
            payload    = msg.get("payload", {})
            if not isinstance(payload, dict):
                self.conn.sendall(encode_error("Payload RECONNECT tidak valid"))
                return

            player_id  = payload.get("player_id", "").strip()
            # Person C mungkin kirim session_id atau room_code — terima keduanya
            session_id = payload.get("session_id", "").strip()

            if not player_id or not session_id:
                self.conn.sendall(encode_error(
                    "RECONNECT membutuhkan field 'player_id' dan 'session_id'"
                ))
                return

            ok, room_code, err = self.room_manager.reconnect_player(
                player_id=player_id,
                session_id=session_id,
                new_sock=self.conn,
            )
            if not ok:
                self.conn.sendall(encode_error(err))
                return

            self.player_id = player_id
            self.room_code = room_code
            self.server.register_connection(player_id, self)
            logger.info(f"RECONNECT player_id={player_id} session_id={session_id}")

        else:
            self.conn.sendall(encode_error("Harap kirim LOGIN atau RECONNECT terlebih dahulu"))

    # ------------------------------------------------------------------
    # Main loop: baca packet → validasi → dispatch
    # ------------------------------------------------------------------
    def _message_loop(self):
        session = self.room_manager.get_session(self.room_code)
        if session is None:
            return

        # Tipe packet yang dikirim langsung oleh Person C (tidak dalam ACTION envelope)
        # dan yang valid dari internal (ACTION envelope milik engine Person A)
        VALID_TYPES = {
            "ACTION",           # envelope Person A (divalidasi validate_action_packet)
            "PING",
            "READY",
            "TAKE_DECK",        # Person C: ambil dari deck
            "TAKE_DISCARD",     # Person C: ambil dari discard pile
            "DISCARD",          # Person C: buang kartu (payload = card object)
            "KNOCK",            # Person C: knock langsung
            "READY_NEXT_ROUND",
            "CHAT",
            "EMOJI_REACT",
        }

        while self.running:
            msg = recv_message(self.conn)
            if msg is None:
                break  # koneksi terputus

            if not isinstance(msg, dict) or "type" not in msg:
                self.conn.sendall(encode_error("Format packet tidak valid"))
                continue

            msg_type = msg.get("type", "")
            payload  = msg.get("payload", {})

            # Pastikan payload selalu dict
            if not isinstance(payload, dict):
                payload = {}
                msg["payload"] = payload

            if msg_type not in VALID_TYPES:
                self.conn.sendall(encode_error(f"Tipe packet tidak dikenal: {msg_type!r}"))
                logger.warning(f"UNKNOWN_TYPE player={self.player_id} type={msg_type}")
                continue

            # Validasi spesifik per tipe
            if msg_type == "ACTION":
                # Packet ACTION dipakai jalur internal / jika Person C berubah pikiran
                ok, reason = validate_action_packet(msg)
                if not ok:
                    self.conn.sendall(encode_error(reason))
                    logger.warning(f"INVALID_ACTION player={self.player_id} reason={reason}")
                    continue

            elif msg_type == "PING":
                ok, reason = validate_ping_packet(msg)
                if not ok:
                    self.conn.sendall(encode_error(reason))
                    continue

            # Tipe Person C yang lain (TAKE_DECK, TAKE_DISCARD, DISCARD, KNOCK, dsb)
            # tidak butuh validasi struktur ketat di level server —
            # validasi bisnis (giliran, state) dilakukan di dalam session.handle_packet()

            logger.info(f"RECV player={self.player_id} type={msg_type}")
            session.handle_packet(player_id=self.player_id, msg=msg)

    # ------------------------------------------------------------------
    def _on_disconnect(self):
        logger.info(f"DISCONNECT player={self.player_id} addr={self.addr}")
        try:
            self.conn.close()
        except OSError:
            pass
        if self.player_id and self.room_code:
            session = self.room_manager.get_session(self.room_code)
            if session:
                session.on_player_disconnect(self.player_id)
            self.server.unregister_connection(self.player_id)


# ---------------------------------------------------------------------------
# UDP Voice Relay
# ---------------------------------------------------------------------------
class UDPVoiceRelay(threading.Thread):
    """
    Thread terpisah untuk relay audio UDP antar pemain dalam satu room.
    Packet format: {"type":"VOICE_DATA","room_code":"XXX","player_id":"P001","audio_chunk":"<b64>"}
    Server relay ke semua pemain lain dalam room yang sama tanpa menyentuh TCP.
    """

    def __init__(self, room_manager: "RoomManager"):
        super().__init__(daemon=True)
        self.room_manager = room_manager
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((TCP_HOST, UDP_PORT))
        self._addr_map: dict[str, tuple] = {}   # player_id → UDP addr
        self._lock = threading.Lock()

    def run(self):
        logger.info(f"UDP Voice Relay listening :{UDP_PORT}")
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
                self._handle(data, addr)
            except Exception as exc:
                logger.warning(f"UDP error: {exc}")

    def _handle(self, data: bytes, addr: tuple):
        try:
            msg = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if msg.get("type") != "VOICE_DATA":
            return

        player_id = msg.get("player_id", "")
        room_code  = msg.get("room_code", "").upper()
        if not player_id or not room_code:
            return

        with self._lock:
            self._addr_map[player_id] = addr

        session = self.room_manager.get_session(room_code)
        if session is None:
            return

        with self._lock:
            for pid in session.get_connected_player_ids():
                if pid == player_id:
                    continue
                dest = self._addr_map.get(pid)
                if dest:
                    try:
                        self.sock.sendto(data, dest)
                    except OSError:
                        pass


# ---------------------------------------------------------------------------
# GameServer
# ---------------------------------------------------------------------------
class GameServer:
    """
    Entry point server.
    - TCP socket utama + accept loop
    - UDPVoiceRelay di thread terpisah
    - Registry koneksi aktif (player_id → ClientHandler)
    """

    def __init__(self, host=TCP_HOST, port=TCP_PORT):
        self.host = host
        self.port = port
        self.room_manager = RoomManager()
        self._connections: dict[str, ClientHandler] = {}
        self._conn_lock = threading.Lock()

    def register_connection(self, player_id: str, handler: "ClientHandler"):
        with self._conn_lock:
            self._connections[player_id] = handler

    def unregister_connection(self, player_id: str):
        with self._conn_lock:
            self._connections.pop(player_id, None)

    def start(self):
        os.makedirs("logs", exist_ok=True)

        UDPVoiceRelay(self.room_manager).start()

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((self.host, self.port))
        server_sock.listen(100)
        logger.info(f"TCP Server listening {self.host}:{self.port}")

        try:
            while True:
                conn, addr = server_sock.accept()
                ClientHandler(conn, addr, self.room_manager, self).start()
        except KeyboardInterrupt:
            logger.info("Server dihentikan")
        finally:
            server_sock.close()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    GameServer().start()
