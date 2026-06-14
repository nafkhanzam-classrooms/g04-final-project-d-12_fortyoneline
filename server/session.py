import socket
import threading
import json
import logging
import random
import string
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from server.game_engine import GameEngine
    _HAS_ENGINE = True
except ImportError:
    GameEngine = None
    _HAS_ENGINE = False
    logger.warning("game_engine.py belum tersedia, stub mode aktif")

def _encode(msg: dict) -> bytes:
    return (json.dumps(msg) + "\n").encode("utf-8")

def _send_to(sock: socket.socket, msg: dict):
    try:
        sock.sendall(_encode(msg))
    except OSError:
        pass

MIN_PLAYERS = 2
MAX_PLAYERS = 4
TURN_TIMEOUT_SECONDS = 30
RECONNECT_GRACE_SECONDS = 60
BETWEEN_ROUND_DELAY = 3

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

class PlayerInfo:
    def __init__(self, player_id: str, username: str, sock: socket.socket):
        self.player_id = player_id
        self.username = username
        self.sock = sock
        self.connected = True
        self.disconnected_at: Optional[float] = None
        self.ready = False
        self.ready_next_round = False

class GameSession:
    def __init__(self, room_code: str):
        self.room_code = room_code
        self.state = "LOBBY"

        self._players: dict[str, PlayerInfo] = {}
        self._lock = threading.RLock()

        self._engine: Optional[GameEngine] = None

        self._turn_timer: Optional[threading.Timer] = None
        self._turn_deadline: Optional[float] = None
        
        self._last_turn_remaining: set = set()
        self._knocker: Optional[str] = None
        self._last_turn_remaining: set = set()
        self._knocker: Optional[str] = None
        self._last_round_result: Optional[dict] = None

        self._reconnect_timers: dict[str, threading.Timer] = {}

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

    def add_player(self, player_id: str, username: str, sock: socket.socket) -> Optional[str]:
        with self._lock:
            if self.state != "LOBBY":
                return "Room sudah dalam permainan"
            if len(self._players) >= MAX_PLAYERS:
                return "Room penuh"

            self._players[player_id] = PlayerInfo(player_id, username, sock)

            for p in self._players.values():
                p.ready = False
            
            logger.info(f"ROOM {self.room_code} | player_joined player_id={player_id} username={username}")

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

    def try_start_game(self) -> bool:
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

    def _start_round(self):
        with self._lock:
            active_ids = list(self._players.keys())
            if not _HAS_ENGINE:
                logger.warning("GameEngine tidak tersedia, ronde tidak dimulai")
                return

            if self._engine is None:
                self._engine = GameEngine(
                    player_ids=active_ids,
                    session_id=self.room_code,
                )
            self._engine.start_round()
            self.state = "DEAL_CARDS"
            self._last_turn_remaining = set()
            self._knocker = None

            for p in self._players.values():
                p.ready_next_round = False

        logger.info(f"ROOM {self.room_code} | DEAL_CARDS")
        self._send_all_hands()
        self._broadcast_game_state()
        with self._lock:
            self.state = "PLAYER_TURN"
        self._prompt_current_player()

    def _send_all_hands(self):
        with self._lock:
            if self._engine is None:
                return
            for pid, pinfo in self._players.items():
                if not pinfo.connected:
                    continue
                state = self._engine.get_state_for_player(pid)

                hand = state.get("your_hand", [])
                _send_to(pinfo.sock, {
                    "type": "YOUR_HAND",
                    "payload": {"cards": hand},
                })

    def _prompt_current_player(self):
        with self._lock:
            if self._engine is None:
                return
            current = self._engine.current_player
            pinfo = self._players.get(current)

        if pinfo is None or not pinfo.connected:
            self._auto_skip(current)
            return

        self._broadcast({
            "type": "TURN_INDICATOR",
            "payload": {
                "current_turn": current,
                "username": pinfo.username,
            },
        })
        with self._lock:
            waiting_initial = (self._engine is not None
                               and self._engine._waiting_initial_discard)
        actions = (["DISCARD"] if waiting_initial
                   else ["TAKE_DECK", "TAKE_DISCARD", "KNOCK"])
        _send_to(pinfo.sock, {
            "type": "VALID_ACTIONS",
            "payload": {
                "actions": actions,
            },
        })
        self._cancel_turn_timer()
        self._turn_deadline = time.time() + TURN_TIMEOUT_SECONDS
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
            self._turn_deadline = None

    def _on_turn_timeout(self, player_id: str):
        logger.info(f"ROOM {self.room_code} | TURN_TIMEOUT player={player_id}")
        result = {}
        with self._lock:
            if self._engine is None:
                return
            if self._engine.current_player != player_id:
                return
            if self._engine._waiting_initial_discard:
                self._engine.initial_discard(player_id, 0)
            else:
                hand = self._engine.hands.get(player_id, [])
                if len(hand) > 4:
                    result = self._engine.discard_card(player_id, 0)
                else:
                    take = self._engine.take_card(player_id, "DECK")
                    if not take.get("success"):
                        result = {"trigger": "DECK_EMPTY"}
                    else:
                        result = self._engine.discard_card(player_id, 0)

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
        self._prompt_current_player()

    def _auto_skip(self, player_id: str):
        logger.info(f"ROOM {self.room_code} | AUTO_SKIP player={player_id}")
        self._advance_turn()

    def _advance_turn(self):
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
        with self._lock:
            remaining = self._last_turn_remaining.copy()
        if not remaining:
            self._do_showdown()
        else:
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
                self._turn_deadline = time.time() + TURN_TIMEOUT_SECONDS
                self._turn_timer = threading.Timer(
                    TURN_TIMEOUT_SECONDS,
                    self._on_last_turn_timeout,
                    args=[next_pid],
                )
                self._turn_timer.start()
            else:
                with self._lock:
                    self._last_turn_remaining.discard(next_pid)
                    if self._engine is not None:
                        self._engine._advance_turn()
                self._handle_last_turn_advance()

    def _on_last_turn_timeout(self, player_id: str):
        logger.info(f"ROOM {self.room_code} | LAST_TURN_TIMEOUT player={player_id}")
        with self._lock:
            if self._engine:
                hand = self._engine.hands.get(player_id, [])
                if len(hand) > 4:
                    self._engine.discard_card(player_id, 0)
                    
            self._last_turn_remaining.discard(player_id)
        self._handle_last_turn_advance()

    def handle_packet(self, player_id: str, msg: dict):
        msg_type = msg.get("type", "")
        if msg_type == "READY":
            self._handle_ready(player_id)
        elif msg_type == "UNREADY":
            self._handle_unready(player_id)
        elif msg_type == "UNREADY_NEXT_ROUND":
            self._handle_unready_next_round(player_id)
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
        elif msg_type == "LEAVE":
            self._handle_leave(player_id)
        else:
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, {
                    "type": "ERROR",
                    "payload": {"message": f"Tipe packet tidak dikenal: {msg_type}"},
                })

    def _handle_ready(self, player_id: str):
        with self._lock:
            if self.state != "LOBBY":
                return
            pinfo = self._players.get(player_id)
            if not pinfo:
                return
            pinfo.ready = True
            connected_players = [p for p in self._players.values() if p.connected]
            if len(connected_players) < MIN_PLAYERS:
                return
            all_ready = all(p.ready for p in connected_players)
            if not all_ready:
                return
        self.try_start_game()

    def _handle_unready(self, player_id: str):
        with self._lock:
            if self.state != "LOBBY":
                return
            pinfo = self._players.get(player_id)
            if pinfo:
                pinfo.ready = False
        logger.info(f"ROOM {self.room_code} | UNREADY player={player_id}")

    def _handle_unready_next_round(self, player_id: str):
        with self._lock:
            if self.state != "WAITING_READY":
                return
            pinfo = self._players.get(player_id)
            if pinfo:
                pinfo.ready_next_round = False
        logger.info(f"ROOM {self.room_code} | UNREADY_NEXT_ROUND player={player_id}")

    def _handle_ping(self, player_id: str):
        pinfo = self._players.get(player_id)
        if pinfo:
            _send_to(pinfo.sock, {
                "type": "PONG",
                "payload": {"timestamp": time.time()},
            })

    def _handle_take(self, player_id: str, source: str):
        with self._lock:
            if self._engine is None:
                return
            state = self.state
            current = self._engine.current_player

        if state == "PLAYER_TURN" and current != player_id:
            pinfo = self._players.get(player_id)
            if pinfo:
                _send_to(pinfo.sock, {
                    "type": "ERROR",
                    "payload": {"message": "Bukan giliran Anda"},
                })
            return

        if state == "LAST_TURN_PHASE":
            with self._lock:
                if player_id not in self._last_turn_remaining:
                    pinfo = self._players.get(player_id)
                    if pinfo:
                        _send_to(pinfo.sock, {"type": "ERROR", "payload": {"message": "Bukan giliran terakhir Anda"}})
                    return
                
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
        card = result["card"]
        pinfo = self._players.get(player_id)
        if pinfo:
            _send_to(pinfo.sock, {
                "type": "CARD_DRAWN",
                "payload": {
                    "source": source,
                    "card": card,
                },
            })
            with self._lock:
                current_state = self._engine.get_state_for_player(player_id)
            _send_to(pinfo.sock, {
                "type": "YOUR_HAND",

                "payload": {"cards": current_state.get("your_hand", [])},
            })
        self._broadcast_game_state()
        if pinfo and pinfo.connected:
            _send_to(pinfo.sock, {
                "type": "VALID_ACTIONS",
                "payload": {"actions": ["DISCARD"]},
            })
        logger.info(f"ROOM {self.room_code} | {source.upper()} player={player_id}")

    def _handle_discard(self, player_id: str, msg: dict):
        with self._lock:
            if self._engine is None:
                return
            state = self.state
            current = self._engine.current_player
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
        
        with self._lock:
            cur_state = self.state

        if cur_state == "LAST_TURN_PHASE":
            with self._lock:
                self._last_turn_remaining.discard(player_id)
            self._handle_last_turn_advance()
        else:
            self._prompt_current_player()

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
            if not self._engine.round_active:
                return
            self._knocker = player_id
            self._engine.log.record(self._engine.round_number, player_id, "KNOCK", {})
            active = self._engine.active_players
            self._last_turn_remaining = set(p for p in active if p != player_id)
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

    def _do_showdown(self):
        with self._lock:
            if self._engine is None:
                return
            self.state = "REVEAL"
            result = self._engine.force_showdown(knocker=self._knocker)
            self._knocker = None
            self._last_round_result = result

        logger.info(f"ROOM {self.room_code} | SHOWDOWN result={result}")
        self._broadcast({
            "type": "ROUND_END",
            "payload": result,
        })
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

        with self._lock:
            self.state = "WAITING_READY"
            for p in self._players.values():
                p.ready_next_round = False

        self._broadcast({
            "type": "NEXT_ROUND_PROMPT",
            "payload": {},
        })

    def _handle_ready_next_round(self, player_id: str):
        with self._lock:
            if self.state != "WAITING_READY":
                return
            if player_id not in self._players:
                return
            self._players[player_id].ready_next_round = True


            active_connected = [
                p for p in self._players.values()
                if p.connected
            ]
            all_ready = all(p.ready_next_round for p in active_connected)

        if all_ready:
            logger.info(f"ROOM {self.room_code} | Semua pemain ready, mulai ronde baru")
            self._start_round()

    def _handle_leave(self, player_id: str):
            with self._lock:
                pinfo = self._players.get(player_id)
                if pinfo:
                    pinfo.left_intentionally = True

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

    def _broadcast(self, msg: dict):
        with self._lock:
            targets = [(p.player_id, p.sock) for p in self._players.values() if p.connected]
        for pid, sock in targets:
            try:
                sock.sendall(_encode(msg))
            except OSError:
                pass

    def _broadcast_game_state(self):
        with self._lock:
            if self._engine is None:
                return
            full = self._engine.get_full_state()
            players_snap = list(self._players.items())
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

    def on_player_disconnect(self, player_id: str):
        with self._lock:
            pinfo = self._players.get(player_id)
            if pinfo is None:
                return
            pinfo.connected = False
            pinfo.disconnected_at = time.time()
            intentionally = getattr(pinfo, 'left_intentionally', False)
            state = self.state

        logger.info(f"ROOM {self.room_code} | DISCONNECT player={player_id}")

        if self.state == "LOBBY":
            with self._lock:
                self._players.pop(player_id, None)
                self._reconnect_timers.pop(player_id, None)
            
            self._broadcast({
                "type": "PLAYER_DISCONNECTED",
                "payload": {
                    "player_id": player_id,
                    "username": pinfo.username,
                    "message": f"{pinfo.username} keluar dari room"
                }
            })      
            self._broadcast({
                "type": "PLAYER_JOINED",
                "payload": {
                    "player_id": "",
                    "username": "",
                    "players": self._player_list_snapshot(),
                    "room_code": self.room_code
                }
            })
            return

        if intentionally:
            with self._lock:
                if self._engine is not None and player_id in self._engine.lives:
                    self._engine.lives[player_id] = 0
                self._players.pop(player_id, None)
                self._reconnect_timers.pop(player_id, None)

            self._broadcast({
                "type": "PLAYER_ELIMINATED_DISCONNECT",
                "payload": {
                    "player_id": player_id,
                    "username": pinfo.username,
                },
            })
            with self._lock:
                active = self._engine.active_players if self._engine else []
            if self._engine is not None and len(active) <= 1:
                winner_id = active[0] if active else None
                winner_name = self._players[winner_id].username if winner_id in self._players else (winner_id or "System")
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
                return
        else:
            timer = threading.Timer(
                RECONNECT_GRACE_SECONDS,
                self._on_reconnect_timeout,
                args=[player_id],
            )
            with self._lock:
                self._reconnect_timers[player_id] = timer
            timer.start()

        with self._lock:
            is_current = (self._engine is not None and self._engine.current_player == player_id)
            waiting_initial = bool(self._engine and self._engine._waiting_initial_discard)

        if is_current and state == "PLAYER_TURN" and not waiting_initial:
            self._auto_skip(player_id)
        elif is_current and state == "LAST_TURN_PHASE":
            with self._lock:
                self._last_turn_remaining.discard(player_id)
                if self._engine is not None:
                    self._engine._advance_turn()
            self._handle_last_turn_advance() 

        elif state == "WAITING_READY":
            with self._lock:
                active_connected = [p for p in self._players.values() if p.connected]
                all_ready = all(p.ready_next_round for p in active_connected) if active_connected else False
            if all_ready:
                self._start_round()    

        if self._engine is not None:
            self._broadcast_game_state()

    def reconnect_player(self, player_id: str, new_sock: socket.socket) -> tuple[bool, str]:
        with self._lock:
            pinfo = self._players.get(player_id)
            if pinfo is None:
                return False, "Player tidak ditemukan di room ini"

            pinfo.sock = new_sock
            pinfo.connected = True
            pinfo.disconnected_at = None

            timer = self._reconnect_timers.pop(player_id, None)
            if timer:
                timer.cancel()

        logger.info(f"ROOM {self.room_code} | RECONNECT player={player_id}")

        with self._lock:
            if self._engine:
                snapshot = self._engine.get_reconnect_snapshot(player_id)
                current = self._engine.current_player
                waiting_initial = self._engine._waiting_initial_discard
            else:
                snapshot = {}
                current = None
                waiting_initial = False

            snapshot["players"] = self._player_list_snapshot()

            if getattr(self, "_turn_deadline", None):
                snapshot["turn_remaining"] = max(0, self._turn_deadline - time.time())

            snapshot["session_state"] = self.state
            if self.state == "WAITING_READY":
                snapshot["round_result"] = getattr(self, "_last_round_result", None)

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

        with self._lock:
            if self._engine:
                pstate = self._engine.get_state_for_player(player_id)
                _send_to(new_sock, {
                    "type": "YOUR_HAND",

                    "payload": {"cards": pstate.get("your_hand", [])},
                })

        self._broadcast({
            "type": "PLAYER_RECONNECTED",
            "payload": {
                "player_id": player_id,
                "username": pinfo.username,
            },
        })
        return True, ""

    def _on_reconnect_timeout(self, player_id: str):
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

class RoomManager:
    def __init__(self):
        self._sessions: dict[str, GameSession] = {}
        self._player_room: dict[str, str] = {}
        self._player_names: dict[str, str] = {}
        self._lock = threading.Lock()


    def register_player(
        self,
        username: str,
        room_code: Optional[str],
        sock: socket.socket,
    ) -> tuple[str, str, Optional[str]]:
        with self._lock:

            player_id = _new_player_id(set(self._player_room.keys()))

            if room_code is None:

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


        err = session.add_player(player_id, username, sock)
        if err:
            with self._lock:
                self._player_room.pop(player_id, None)
                self._player_names.pop(player_id, None)
            return "", "", err

        return player_id, room_code, None


    def reconnect_player(
        self,
        player_id: str,
        room_code: str,
        new_sock: socket.socket,
    ) -> tuple[bool, str]:
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