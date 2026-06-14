import argparse
import json
import os
import queue
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from shared.protocol import encode
from shared.constants import (
    DEFAULT_PORT,
    PING_INTERVAL_SEC,
    RECONNECT_WINDOW_SEC,
)

TURN_TIMEOUT_SEC = 30
RECONNECT_RETRY_SEC = 3
TOAST_SECONDS = 4.0
SESSION_FILE = os.path.expanduser("~/.kartu41_session.json")
NET_DOWN = "__NET_DOWN__"
PHASE_CONNECT = "CONNECT"
PHASE_LOBBY = "LOBBY"
PHASE_PLAYING = "PLAYING"
PHASE_MUST_DISCARD = "MUST_DISCARD"
PHASE_ROUND_END = "ROUND_END"
PHASE_WAITING_READY = "WAITING_READY"
PHASE_GAME_OVER = "GAME_OVER"
PHASE_RECONNECTING = "RECONNECTING"

IN_GAME_PHASES = {
    PHASE_PLAYING, PHASE_MUST_DISCARD, PHASE_ROUND_END,
    PHASE_WAITING_READY, PHASE_LOBBY,
}

class LineFramer:
    def __init__(self):
        self._buf = b""

    def feed(self, data: bytes) -> list:
        self._buf += data
        messages = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict):
                messages.append(msg)
        return messages

class NetworkClient:
    def __init__(self, host: str, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.connected = False
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._gen = 0

    def connect(self, first_message: tuple | None = None):
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=10)
        sock.settimeout(None)
        with self._send_lock:
            self._sock = sock
            self._gen += 1
            gen = self._gen
        self.connected = True
        if first_message:
            self.send(first_message[0], first_message[1])
        t = threading.Thread(
            target=self._recv_loop, args=(sock, gen), daemon=True,
            name="net-recv",
        )
        t.start()

    def send(self, msg_type: str, payload: dict | None = None) -> bool:
        data = encode(msg_type, payload or {})
        with self._send_lock:
            sock = self._sock
        if sock is None:
            return False
        try:
            sock.sendall(data)
            return True
        except OSError:
            self.connected = False
            return False

    def close(self):
        with self._send_lock:
            self._gen += 1
            sock = self._sock
            self._sock = None
        self.connected = False
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _recv_loop(self, sock: socket.socket, gen: int):
        framer = LineFramer()
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                for msg in framer.feed(chunk):
                    self.inbox.put(msg)
        except OSError:
            pass

        with self._send_lock:
            still_current = (gen == self._gen)
        if still_current:
            self.connected = False
            self.inbox.put({"type": NET_DOWN, "payload": {}})

@dataclass
class ClientState:
    phase: str = PHASE_CONNECT
    username: str = ""
    player_id: str = ""
    room_code: str = ""
    host: str = "127.0.0.1"

    hand: list = field(default_factory=list)
    discard_top: dict | None = None
    deck_count: int = 0
    round_number: int = 0
    players: list = field(default_factory=list)
    current_turn: str = ""
    valid_actions: list = field(default_factory=list)
    turn_deadline: float | None = None

    round_result: dict | None = None
    game_over_info: dict | None = None

    chat_log: list = field(default_factory=list)
    toasts: list = field(default_factory=list)
    latency_ms: int | None = None
    last_ping_sent: float | None = None

    speaking: dict = field(default_factory=dict)
    reconnect_deadline: float | None = None
    ui: dict = field(default_factory=dict)

    def toast(self, text: str, now: float | None = None):
        now = time.time() if now is None else now
        self.toasts.append({"text": str(text), "until": now + TOAST_SECONDS})
        del self.toasts[:-6]

    def active_toasts(self, now: float | None = None) -> list:
        now = time.time() if now is None else now
        self.toasts = [t for t in self.toasts if t["until"] > now]
        return [t["text"] for t in self.toasts]

    def username_for(self, player_id: str) -> str:
        for p in self.players:
            if p.get("player_id") == player_id:
                return p.get("username") or player_id
        return player_id

    def is_my_turn(self) -> bool:
        return bool(self.player_id) and self.current_turn == self.player_id
    
    def clear_session_file(self):
        import os, re
        key = re.sub(r"[^A-Za-z0-9._-]+", "_", self.host).strip("_")
        if not key: key = "default"
        path = os.path.expanduser(f"~/.kartu41_session_{key}.json")
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

def _session_file_for(host: str, username: str | None = None) -> str:
    parts = [host or "localhost"]
    if username:
        parts.append(username)
    key = re.sub(r"[^A-Za-z0-9._-]+", "_", "_".join(parts)).strip("_")
    if not key:
        key = "default"
    return os.path.expanduser(f"~/.kartu41_session_{key}.json")

def save_session(state: ClientState):
    session_file = _session_file_for(state.host, state.username)
    try:
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump({
                "player_id": state.player_id,
                "room_code": state.room_code,
                "username": state.username,
                "host": state.host,
            }, f)
    except OSError:
        pass

def clear_session(host: str, username: str | None = None):
    session_file = _session_file_for(host, username)
    try:
        if os.path.exists(session_file):
            os.remove(session_file)
    except OSError:
        pass

def load_session(host: str, username: str = "") -> dict | None:
    candidates = [_session_file_for(host, username)]
    if not username:
        legacy = os.path.expanduser("~/.kartu41_session.json")
        if legacy not in candidates:
            candidates.append(legacy)
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return None

def _first(payload: dict, *keys, default=None):
    for k in keys:
        v = payload.get(k)
        if v is not None:
            return v
    return default

def _on_login_ack(state, p, now):
    state.player_id = p.get("player_id") or state.player_id
    state.room_code = p.get("room_code") or state.room_code
    state.phase = PHASE_LOBBY
    state.toast(p.get("message", "Bergabung ke room"), now)
    clear_session(state.host, state.username) 
    state.ui["focus"] = None

def _on_error(state, p, now):
    state.toast(p.get("message", "Error dari server"), now)

def _on_player_joined(state, p, now):
    players = p.get("players")
    if isinstance(players, list):
        state.players = players
    if p.get("room_code"):
        state.room_code = p["room_code"]
    if p.get("username") and p.get("player_id") != state.player_id:
        state.toast(f"{p['username']} bergabung", now)
    state.ui["ready_sent"] = False

def _on_game_start(state, p, now):
    state.phase = PHASE_PLAYING
    state.round_result = None
    state.ui["ready_sent"] = False
    state.ui["next_ready_sent"] = False
    state.toast(p.get("message", "Permainan dimulai!"), now)
    save_session(state)

def _on_your_hand(state, p, now):
    cards = p.get("cards")
    if isinstance(cards, list):
        state.hand = [c for c in cards if isinstance(c, dict)]

def _on_game_state(state, p, now):
    state.current_turn = _first(p, "current_turn", "current_player",
                                default=state.current_turn) or ""
    deck = _first(p, "deck_count", "deck_remaining")
    if isinstance(deck, int):
        state.deck_count = deck
    discard = _first(p, "discard_top", "top_discard")
    if isinstance(discard, dict):
        state.discard_top = discard
    rnd = p.get("round")
    if isinstance(rnd, int):
        state.round_number = rnd
    players = p.get("players")
    if isinstance(players, list) and players:
        state.players = players

    if state.current_turn != state.player_id:
        state.valid_actions = []
        if state.phase == PHASE_MUST_DISCARD:
            state.phase = PHASE_PLAYING

    if state.phase == PHASE_LOBBY:
        state.phase = PHASE_PLAYING

def _on_turn_indicator(state, p, now):
    state.current_turn = p.get("current_turn") or state.current_turn
    state.turn_deadline = now + TURN_TIMEOUT_SEC
    if not state.is_my_turn():
        state.valid_actions = []
    if state.phase in (PHASE_ROUND_END, PHASE_WAITING_READY):
        state.phase = PHASE_PLAYING
        state.round_result = None

        state.ui["next_ready_sent"] = False

def _on_valid_actions(state, p, now):
    actions = p.get("actions")
    state.valid_actions = actions if isinstance(actions, list) else []
    if state.valid_actions == ["DISCARD"]:
        state.phase = PHASE_MUST_DISCARD
    elif state.phase == PHASE_MUST_DISCARD:
        state.phase = PHASE_PLAYING

def _on_card_drawn(state, p, now):
    card = p.get("card")
    if isinstance(card, dict) and card.get("rank"):
        state.toast(f"Ambil kartu dari {p.get('source', '?')}", now)

def _on_knock_notification(state, p, now):
    state.toast(p.get("message") or f"{p.get('username', '?')} KNOCK!", now)

def _on_round_end(state, p, now):
    state.round_result = p
    state.phase = PHASE_ROUND_END
    state.valid_actions = []
    state.turn_deadline = None

def _on_next_round_prompt(state, p, now):
    state.phase = PHASE_WAITING_READY
    state.toast(p.get("message", "Siap untuk ronde berikutnya?"), now)

def _on_game_over(state, p, now):
    state.game_over_info = p
    state.phase = PHASE_GAME_OVER
    state.valid_actions = []
    state.turn_deadline = None
    clear_session(state.host, state.username)

def _set_player_connected(state, player_id, connected):
    for pl in state.players:
        if pl.get("player_id") == player_id:
            pl["connected"] = connected

def _on_player_disconnected(state, p, now):
    _set_player_connected(state, p.get("player_id"), False)
    state.toast(p.get("message") or f"{p.get('username', '?')} terputus", now)

def _on_player_reconnected(state, p, now):
    _set_player_connected(state, p.get("player_id"), True)
    state.toast(f"{p.get('username', '?')} tersambung kembali", now)

def _on_player_eliminated_disconnect(state, p, now):
    _set_player_connected(state, p.get("player_id"), False)
    state.toast(f"{p.get('username', '?')} dieliminasi (tidak reconnect)", now)

def _on_reconnect_ack(state, p, now):
    state.player_id = p.get("player_id") or state.player_id
    snap = p.get("state") or {}
    if not isinstance(snap, dict):
        snap = {}
    hand = snap.get("your_hand")
    if isinstance(hand, list):
        state.hand = [c for c in hand if isinstance(c, dict)]
    discard = snap.get("top_discard")
    if isinstance(discard, dict):
        state.discard_top = discard
    deck = snap.get("deck_remaining")
    if isinstance(deck, int):
        state.deck_count = deck
    state.current_turn = snap.get("current_player") or state.current_turn
    rnd = snap.get("round")
    if isinstance(rnd, int):
        state.round_number = rnd
    lives = snap.get("lives") or {}
    order = snap.get("player_order") or []
    players = snap.get("players")
    opponents = snap.get("opponents") or {}

    if isinstance(players, list) and players:
        state.players = players
    elif not state.players and isinstance(order, list):
        state.players = [
            {"player_id": pid, "username": pid, "lives": lives.get(pid),
             "hand_count": None, "connected": True}
            for pid in order
        ]
    for pl in state.players:
        pid = pl.get("player_id")
        if pid in lives:
            pl["lives"] = lives[pid]
        if pid in opponents:
            pl["hand_count"] = opponents[pid].get("card_count")

    actions = snap.get("valid_actions")
    if isinstance(actions, list):
        state.valid_actions = actions

    actions = snap.get("valid_actions")
    if isinstance(actions, list):
        state.valid_actions = actions

    session_state = snap.get("session_state")
    
    if session_state == "WAITING_READY":
        state.phase = PHASE_WAITING_READY
        state.round_result = snap.get("round_result")
    elif state.valid_actions == ["DISCARD"]:
        state.phase = PHASE_MUST_DISCARD
    else:
        state.phase = PHASE_PLAYING if snap else PHASE_LOBBY

    rem = snap.get("turn_remaining")
    if rem is not None:
        state.turn_deadline = now + rem
    else:
        state.turn_deadline = None
    state.reconnect_deadline = None
    state.toast(p.get("message", "Berhasil reconnect"), now)
    state.ui["focus"] = None

def _on_pong(state, p, now):
    if state.last_ping_sent is not None:
        state.latency_ms = max(0, int((now - state.last_ping_sent) * 1000))

def _on_chat_broadcast(state, p, now):
    state.chat_log.append({
        "username": p.get("username", "?"),
        "text": str(p.get("text", "")),
    })
    del state.chat_log[:-100]

_HANDLERS = {
    "LOGIN_ACK": _on_login_ack,
    "ERROR": _on_error,
    "PLAYER_JOINED": _on_player_joined,
    "GAME_START": _on_game_start,
    "YOUR_HAND": _on_your_hand,
    "GAME_STATE": _on_game_state,
    "TURN_INDICATOR": _on_turn_indicator,
    "VALID_ACTIONS": _on_valid_actions,
    "CARD_DRAWN": _on_card_drawn,
    "KNOCK_NOTIFICATION": _on_knock_notification,
    "ROUND_END": _on_round_end,
    "NEXT_ROUND_PROMPT": _on_next_round_prompt,
    "GAME_OVER": _on_game_over,
    "PLAYER_DISCONNECTED": _on_player_disconnected,
    "PLAYER_RECONNECTED": _on_player_reconnected,
    "PLAYER_ELIMINATED_DISCONNECT": _on_player_eliminated_disconnect,
    "RECONNECT_ACK": _on_reconnect_ack,
    "PONG": _on_pong,
    "CHAT_BROADCAST": _on_chat_broadcast,
}

def dispatch(state: ClientState, msg: dict, now: float | None = None):
    now = time.time() if now is None else now
    if not isinstance(msg, dict):
        return
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    handler = _HANDLERS.get(msg.get("type", ""))
    if handler is not None:
        handler(state, payload, now)

def start_ping_loop(net: NetworkClient, state: ClientState,
                    stop: threading.Event):
    def loop():
        while not stop.is_set():
            if net.connected and state.player_id:
                state.last_ping_sent = time.time()
                net.send("PING", {"timestamp": state.last_ping_sent})
            stop.wait(PING_INTERVAL_SEC)
    threading.Thread(target=loop, daemon=True, name="ping").start()

class ReconnectController:
    def __init__(self, net: NetworkClient, state: ClientState):
        self.net = net
        self.state = state
        self._attempting = False
        self._next_try = 0.0

    def on_net_down(self, now: float):
        st = self.state
        if not (st.player_id and st.room_code) or st.phase in (
                PHASE_CONNECT, PHASE_GAME_OVER):
            st.toast("Koneksi ke server terputus", now)
            if st.phase != PHASE_GAME_OVER:
                st.phase = PHASE_CONNECT
            return
        if st.phase != PHASE_RECONNECTING:
            st.phase = PHASE_RECONNECTING
            st.reconnect_deadline = now + RECONNECT_WINDOW_SEC
            st.toast("Koneksi putus — mencoba reconnect...", now)
        self._next_try = now

    def tick(self, now: float):
        st = self.state
        if st.phase != PHASE_RECONNECTING:
            return
        if st.reconnect_deadline and now > st.reconnect_deadline:
            st.phase = PHASE_CONNECT
            st.reconnect_deadline = None
            st.toast("Reconnect gagal — waktu habis", now)
            return
        if self._attempting or now < self._next_try:
            return
        self._attempting = True
        threading.Thread(target=self._try_once, daemon=True,
                         name="reconnect").start()

    def _try_once(self):
        st = self.state
        try:
            self.net.connect(first_message=(
                "RECONNECT",
                {"player_id": st.player_id, "room_code": st.room_code},
            ))
        except OSError:
            pass
        finally:
            self._next_try = time.time() + RECONNECT_RETRY_SEC
            self._attempting = False

_HEADLESS_HELP = """\
Perintah: ready | take d | take p | discard <i> | knock | next | chat <teks> | hand | state | help | quit"""

def _print_state_summary(state: ClientState):
    me = " (GILIRANKU)" if state.is_my_turn() else ""
    print(f"  fase={state.phase} ronde={state.round_number} "
          f"deck={state.deck_count} discard={state.discard_top} "
          f"giliran={state.current_turn}{me}")
    print(f"  aksi_valid={state.valid_actions}")
    for i, c in enumerate(state.hand):
        print(f"    [{i}] {c.get('rank')} {c.get('suit')}")
    for p in state.players:
        flag = "" if p.get("connected", True) else " [PUTUS]"
        print(f"    - {p.get('username')} lives={p.get('lives')} "
              f"kartu={p.get('hand_count')}{flag}")

def _handle_headless_command(line: str, state: ClientState, net: NetworkClient) -> bool:
    parts = line.strip().split()
    if not parts:
        return True
    cmd = parts[0].lower()

    if cmd == "quit":
        return False
    elif cmd == "help":
        print(_HEADLESS_HELP)
    elif cmd == "ready":
        net.send("READY", {})
    elif cmd == "take" and len(parts) > 1:
        if parts[1].startswith("d"):
            net.send("TAKE_DECK", {})
        else:
            net.send("TAKE_DISCARD", {})
    elif cmd == "discard" and len(parts) > 1:
        try:
            card = state.hand[int(parts[1])]
        except (ValueError, IndexError):
            print(f"!! indeks kartu tidak valid (hand: {len(state.hand)} kartu)")
            return True
        net.send("DISCARD", {"card": {"suit": card.get("suit"), "rank": card.get("rank")}})
    elif cmd == "knock":
        net.send("KNOCK", {})
    elif cmd == "next":
        net.send("READY_NEXT_ROUND", {})
    elif cmd == "chat":
        net.send("CHAT", {"text": line.strip()[5:]})
    elif cmd == "hand":
        for i, c in enumerate(state.hand):
            print(f"    [{i}] {c.get('rank')} {c.get('suit')}")
    elif cmd == "state":
        _print_state_summary(state)
    else:
        print(f"!! perintah tidak dikenal: {cmd}  (ketik 'help')")
    return True

def run_headless(args):
    if not args.name:
        print("--headless butuh --name", file=sys.stderr)
        sys.exit(1)

    state = ClientState(username=args.name, host=args.host)
    net = NetworkClient(args.host, args.port)
    recon = ReconnectController(net, state)

    print(f"[NET] menghubungi {args.host}:{args.port} ...")
    try:
        net.connect()
    except OSError as e:
        print(f"[NET] gagal konek: {e}", file=sys.stderr)
        sys.exit(1)
    net.send("LOGIN", {"username": args.name, "room_code": (args.room or "").upper()})
    stop = threading.Event()
    start_ping_loop(net, state, stop)

    cmd_q: "queue.Queue[str]" = queue.Queue()

    def stdin_loop():
        for line in sys.stdin:
            cmd_q.put(line)
        cmd_q.put("quit")

    threading.Thread(target=stdin_loop, daemon=True, name="stdin").start()
    print(_HEADLESS_HELP)

    prev_phase = state.phase
    running = True
    try:
        while running:
            now = time.time()
            try:
                while True:
                    msg = net.inbox.get_nowait()
                    mtype = msg.get("type", "")
                    if mtype == NET_DOWN:
                        recon.on_net_down(now)
                    else:
                        if mtype != "PONG":
                            print(f"[RECV] {mtype} {msg.get('payload')}")
                        dispatch(state, msg, now)
            except queue.Empty:
                pass
            if state.phase != prev_phase:
                print(f"[STATE] {prev_phase} -> {state.phase}")
                _print_state_summary(state)
                prev_phase = state.phase

            recon.tick(now)
            try:
                line = cmd_q.get(timeout=0.1)
                running = _handle_headless_command(line, state, net)
            except queue.Empty:
                pass
    finally:
        stop.set()
        net.close()
    print("[NET] keluar.")

def run_gui(args):
    import pygame
    import client.input_handler as input_handler
    import client.renderer as renderer
    try:
        from client.voice import VoiceChat
    except Exception:
        VoiceChat = None
    saved = load_session(args.host, args.name or "") or {}
    state = ClientState(host=args.host)
    state.ui["username_input"] = args.name or saved.get("username", "")
    state.ui["room_input"] = args.room or ""
    state.ui["focus"] = "username_input"
    state.ui["last_session"] = saved

    net = NetworkClient(args.host, args.port)
    recon = ReconnectController(net, state)

    stop = threading.Event()
    start_ping_loop(net, state, stop)

    pygame.init()
    screen = pygame.display.set_mode((renderer.WIDTH, renderer.HEIGHT))
    pygame.display.set_caption("Kartu 41")
    clock = pygame.time.Clock()

    voice = None
    running = True
    try:
        while running:
            now = time.time()
            try:
                while True:
                    msg = net.inbox.get_nowait()
                    if msg.get("type") == NET_DOWN:
                        recon.on_net_down(now)
                    else:
                        dispatch(state, msg, now)
            except queue.Empty:
                pass
            if (voice is None and VoiceChat is not None
                    and state.player_id and state.room_code):
                voice = VoiceChat(args.host, state.player_id,
                                  state.room_code, state)
                voice.start()
                if not voice.audio_ok:
                    state.toast("Voice chat nonaktif (audio tidak tersedia)", now)

            running = input_handler.handle(pygame.event.get(), state, net, voice)

            if state.phase == "CONNECT" and voice is not None:
                voice.close()
                voice = None
            recon.tick(now)
            renderer.draw(screen, state, now)
            pygame.display.flip()
            clock.tick(60)
    finally:
        stop.set()
        if voice is not None:
            voice.close()
        net.close()
        pygame.quit()

def main():
    ap = argparse.ArgumentParser(description="Client Game Kartu 41")
    ap.add_argument("--host", default="127.0.0.1", help="alamat server")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--name", default="", help="username")
    ap.add_argument("--room", default="", help="kode room (kosong = buat baru)")
    ap.add_argument("--headless", action="store_true", help="mode teks tanpa GUI (harness uji integrasi)")
    args = ap.parse_args()
    if args.headless:
        run_headless(args)
    else:
        run_gui(args)

if __name__ == "__main__":
    main()