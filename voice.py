# =============================================================================
# voice.py — Game Kartu 41 client
# Person C: voice chat push-to-talk lewat UDP relay server (port 5556).
#
# Datagram JSON, di-relay mentah ke semua pemain lain dalam room
# (server.py:250-310):
#   {"type": "VOICE_DATA", "room_code": "ABCDE", "player_id": "P001",
#    "audio_chunk": "<base64 int16 PCM>"}
#
# Relay mempelajari alamat UDP tiap pemain dari PAKET PERTAMANYA — maka
# start() langsung mengirim satu keepalive (audio_chunk kosong); tanpa itu
# client tidak akan pernah MENERIMA audio.
#
# Degradasi anggun: tanpa sounddevice/numpy atau tanpa perangkat audio,
# voice dimatikan (audio_ok=False) dan game jalan normal. Voice tidak pernah
# menyentuh jalur TCP.
# =============================================================================

import base64
import collections
import json
import socket
import threading
import time

try:
    import numpy as np
    import sounddevice as sd
    _IMPORT_OK = True
except Exception:  # ImportError atau error PortAudio saat load
    _IMPORT_OK = False

UDP_VOICE_PORT = 5556
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
BLOCK_FRAMES = 800          # 50 ms per blok
SPEAKING_HOLD_SEC = 0.3     # indikator "bicara" bertahan setelah paket terakhir
MAX_BUFFER_BLOCKS = 20      # ~1 s antrian playback per pemain


class VoiceChat:
    def __init__(self, host: str, player_id: str, room_code: str,
                 state=None, udp_port: int = UDP_VOICE_PORT):
        self.server_addr = (host, udp_port)
        self.player_id = player_id
        self.room_code = room_code
        self.state = state  # ClientState — dipakai untuk state.speaking
        self.talking = False
        self.audio_ok = False

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._closed = False
        self._lock = threading.Lock()
        # player_id → deque blok numpy int16 menunggu playback
        self._buffers: dict[str, collections.deque] = {}
        self._in_stream = None
        self._out_stream = None

    # ------------------------------------------------------------------
    def start(self):
        # Keepalive registrasi alamat — kirim walau audio mati, supaya
        # indikator bicara (recv thread) tetap berfungsi.
        self._send_chunk(b"")
        threading.Thread(target=self._recv_loop, daemon=True,
                         name="voice-recv").start()

        if not _IMPORT_OK:
            return
        try:
            self._in_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
                blocksize=BLOCK_FRAMES, callback=self._input_callback)
            self._out_stream = sd.OutputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
                blocksize=BLOCK_FRAMES, callback=self._output_callback)
            self._in_stream.start()
            self._out_stream.start()
            self.audio_ok = True
        except Exception:
            self.audio_ok = False
            self._stop_streams()

    def set_talking(self, talking: bool):
        self.talking = bool(talking)
        if talking and self.state is not None:
            # tampilkan indikator bicara sendiri juga
            self.state.speaking[self.player_id] = time.time() + SPEAKING_HOLD_SEC

    def close(self):
        self._closed = True
        self._stop_streams()
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------
    def _stop_streams(self):
        for stream in (self._in_stream, self._out_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
        self._in_stream = None
        self._out_stream = None

    def _send_chunk(self, raw: bytes):
        packet = json.dumps({
            "type": "VOICE_DATA",
            "room_code": self.room_code,
            "player_id": self.player_id,
            "audio_chunk": base64.b64encode(raw).decode("ascii"),
        }).encode("utf-8")
        try:
            self.sock.sendto(packet, self.server_addr)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Capture: callback dipanggil PortAudio tiap ~50 ms
    # ------------------------------------------------------------------
    def _input_callback(self, indata, frames, time_info, status):
        if self.talking and not self._closed:
            self._send_chunk(bytes(indata))
            if self.state is not None:
                self.state.speaking[self.player_id] = (
                    time.time() + SPEAKING_HOLD_SEC)

    # ------------------------------------------------------------------
    # Receive: datagram → buffer per pemain + flag speaking
    # ------------------------------------------------------------------
    def _recv_loop(self):
        while not self._closed:
            try:
                data, _ = self.sock.recvfrom(65535)
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if msg.get("type") != "VOICE_DATA":
                continue
            pid = msg.get("player_id", "")
            if not pid or pid == self.player_id:
                continue
            try:
                raw = base64.b64decode(msg.get("audio_chunk", ""))
            except (ValueError, TypeError):
                continue
            if not raw:
                continue  # keepalive
            if self.state is not None:
                self.state.speaking[pid] = time.time() + SPEAKING_HOLD_SEC
            if not _IMPORT_OK:
                continue
            block = np.frombuffer(raw, dtype=np.int16)
            with self._lock:
                buf = self._buffers.setdefault(
                    pid, collections.deque(maxlen=MAX_BUFFER_BLOCKS))
                buf.append(block)

    # ------------------------------------------------------------------
    # Playback: campur (mix) blok dari semua pemain ke satu OutputStream
    # ------------------------------------------------------------------
    def _output_callback(self, outdata, frames, time_info, status):
        mixed = np.zeros(frames, dtype=np.int32)
        with self._lock:
            for buf in self._buffers.values():
                if not buf:
                    continue
                block = buf.popleft()
                n = min(frames, len(block))
                mixed[:n] += block[:n].astype(np.int32)
        np.clip(mixed, -32768, 32767, out=mixed)
        outdata[:, 0] = mixed.astype(np.int16)
