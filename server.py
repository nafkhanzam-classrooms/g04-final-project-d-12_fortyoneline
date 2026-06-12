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
"""

import socket
import threading
import json
import logging
import time
import random
import string

from session import RoomManager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
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
BUFFER_SIZE = 4096
RECV_TIMEOUT = 60  # detik sebelum koneksi dianggap mati


# ---------------------------------------------------------------------------
# Helper: encode / decode sederhana (fallback jika protocol.py belum ada)
# Saat Person A sudah selesai, import dari protocol:
#   from protocol import encode, decode, recv_message
# ---------------------------------------------------------------------------
def _encode(msg: dict) -> bytes:
    return (json.dumps(msg) + "\n").encode("utf-8")


def _decode(raw: str) -> dict:
    return json.loads(raw.strip())


def _recv_message(sock: socket.socket) -> dict | None:
    """
    Baca satu packet (diakhiri '\\n') dari socket.
    Return None jika koneksi terputus atau timeout.
    """
    buf = b""
    try:
        while True:
            chunk = sock.recv(BUFFER_SIZE)
            if not chunk:
                return None
            buf += chunk
            if b"\n" in buf:
                line, _ = buf.split(b"\n", 1)
                return _decode(line.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# ClientHandler — satu thread per koneksi TCP
# ---------------------------------------------------------------------------
class ClientHandler(threading.Thread):
    """
    Thread yang hidup selama satu koneksi TCP aktif.

    Lifecycle:
      1. Terima packet LOGIN → registrasi ke room_manager
      2. Loop: baca packet → dispatch ke session
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
    # Handshake: terima LOGIN atau RECONNECT
    # ------------------------------------------------------------------
    def _handshake(self):
        msg = _recv_message(self.conn)
        if msg is None:
            return

        msg_type = msg.get("type", "")

        # --- LOGIN baru ---
        if msg_type == "LOGIN":
            payload = msg.get("payload", {})
            username = payload.get("username", "").strip()
            room_code = payload.get("room_code", "").strip().upper()

            if not username:
                self._send({"type": "ERROR", "payload": {"message": "Username kosong"}})
                return

            player_id, room_code, err = self.room_manager.register_player(
                username=username,
                room_code=room_code or None,
                sock=self.conn,
            )
            if err:
                self._send({"type": "ERROR", "payload": {"message": err}})
                return

            self.player_id = player_id
            self.room_code = room_code
            self.server.register_connection(player_id, self)

            self._send({
                "type": "LOGIN_ACK",
                "payload": {
                    "player_id": player_id,
                    "room_code": room_code,
                    "message": "Berhasil bergabung ke room",
                },
            })
            logger.info(f"LOGIN player_id={player_id} username={username} room={room_code}")

        # --- RECONNECT ---
        elif msg_type == "RECONNECT":
            payload = msg.get("payload", {})
            player_id = payload.get("player_id", "")
            room_code = payload.get("room_code", "").upper()

            ok, err = self.room_manager.reconnect_player(
                player_id=player_id,
                room_code=room_code,
                new_sock=self.conn,
            )
            if not ok:
                self._send({"type": "ERROR", "payload": {"message": err}})
                return

            self.player_id = player_id
            self.room_code = room_code
            self.server.register_connection(player_id, self)
            logger.info(f"RECONNECT player_id={player_id} room={room_code}")

        else:
            self._send({"type": "ERROR", "payload": {"message": "Harap kirim LOGIN terlebih dahulu"}})

    # ------------------------------------------------------------------
    # Main loop: baca packet → dispatch
    # ------------------------------------------------------------------
    def _message_loop(self):
        session = self.room_manager.get_session(self.room_code)
        if session is None:
            return

        while self.running:
            msg = _recv_message(self.conn)
            if msg is None:
                break  # koneksi terputus

            msg_type = msg.get("type", "")

            # Validasi basic
            if not self._is_valid_packet(msg):
                self._send({"type": "ERROR", "payload": {"message": "Packet tidak valid"}})
                logger.warning(f"INVALID_PACKET player={self.player_id} msg={msg}")
                continue

            logger.info(f"RECV player={self.player_id} type={msg_type}")

            # Dispatch
            session.handle_packet(player_id=self.player_id, msg=msg)

    # ------------------------------------------------------------------
    def _on_disconnect(self):
        logger.info(f"DISCONNECT player={self.player_id} addr={self.addr}")
        self.conn.close()
        if self.player_id and self.room_code:
            session = self.room_manager.get_session(self.room_code)
            if session:
                session.on_player_disconnect(self.player_id)
            self.server.unregister_connection(self.player_id)

    # ------------------------------------------------------------------
    def _send(self, msg: dict):
        try:
            self.conn.sendall(_encode(msg))
        except OSError:
            pass

    # ------------------------------------------------------------------
    @staticmethod
    def _is_valid_packet(msg: dict) -> bool:
        """Validasi struktur packet minimal."""
        if not isinstance(msg, dict):
            return False
        if "type" not in msg:
            return False
        allowed_types = {
            "LOGIN", "RECONNECT", "READY",
            "TAKE_DECK", "TAKE_DISCARD", "DISCARD", "KNOCK",
            "CHAT", "EMOJI_REACT", "PING", "READY_NEXT_ROUND",
        }
        return msg["type"] in allowed_types


# ---------------------------------------------------------------------------
# UDP Voice Relay
# ---------------------------------------------------------------------------
class UDPVoiceRelay(threading.Thread):
    """
    Thread terpisah untuk relay audio UDP antar pemain dalam satu room.
    Packet format: {"type":"VOICE_DATA","room_code":"XXX","player_id":"P001","audio_chunk":"<b64>"}
    Server relay ke semua pemain lain dalam room yang sama.
    """

    def __init__(self, room_manager: "RoomManager"):
        super().__init__(daemon=True)
        self.room_manager = room_manager
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((TCP_HOST, UDP_PORT))
        # Map player_id → UDP address (diisi saat client mengirim packet pertama)
        self._addr_map: dict[str, tuple] = {}
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
        room_code = msg.get("room_code", "").upper()

        if not player_id or not room_code:
            return

        # Simpan / perbarui alamat UDP pengirim
        with self._lock:
            self._addr_map[player_id] = addr

        # Kirim ke semua pemain lain dalam room
        session = self.room_manager.get_session(room_code)
        if session is None:
            return

        relay_data = data  # kirim ulang packet mentah

        with self._lock:
            for pid in session.get_connected_player_ids():
                if pid == player_id:
                    continue
                dest = self._addr_map.get(pid)
                if dest:
                    try:
                        self.sock.sendto(relay_data, dest)
                    except OSError:
                        pass


# ---------------------------------------------------------------------------
# GameServer
# ---------------------------------------------------------------------------
class GameServer:
    """
    Entry point server.

    - Membuat TCP socket utama
    - Menjalankan UDPVoiceRelay di thread terpisah
    - Accept loop: spawn ClientHandler per koneksi
    - Registry koneksi aktif (player_id → ClientHandler)
    """

    def __init__(self, host=TCP_HOST, port=TCP_PORT):
        self.host = host
        self.port = port
        self.room_manager = RoomManager()

        # Registry koneksi: player_id → ClientHandler
        self._connections: dict[str, ClientHandler] = {}
        self._conn_lock = threading.Lock()

    # ------------------------------------------------------------------
    def register_connection(self, player_id: str, handler: "ClientHandler"):
        with self._conn_lock:
            self._connections[player_id] = handler

    def unregister_connection(self, player_id: str):
        with self._conn_lock:
            self._connections.pop(player_id, None)

    def get_handler(self, player_id: str) -> "ClientHandler | None":
        with self._conn_lock:
            return self._connections.get(player_id)

    # ------------------------------------------------------------------
    def start(self):
        import os
        os.makedirs("logs", exist_ok=True)

        # UDP Voice Relay
        udp_relay = UDPVoiceRelay(self.room_manager)
        udp_relay.start()

        # TCP Server
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((self.host, self.port))
        server_sock.listen(100)
        logger.info(f"TCP Server listening {self.host}:{self.port}")

        try:
            while True:
                conn, addr = server_sock.accept()
                handler = ClientHandler(
                    conn=conn,
                    addr=addr,
                    room_manager=self.room_manager,
                    server=self,
                )
                handler.start()
        except KeyboardInterrupt:
            logger.info("Server dihentikan")
        finally:
            server_sock.close()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    GameServer().start()
