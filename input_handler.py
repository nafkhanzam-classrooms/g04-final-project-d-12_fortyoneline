# =============================================================================
# input_handler.py — Game Kartu 41 client
# Person C: event Pygame → aksi protokol, di-gate oleh valid_actions + phase.
# Server tetap memvalidasi ulang — gating di sini hanya UX, bukan keamanan.
#
# Hitbox klik dibaca dari state.ui["hitboxes"] yang dibangun renderer pada
# frame sebelumnya (aman pada 60 FPS).
# =============================================================================

import pygame

TEXT_FIELDS = ("username_input", "room_input", "chat_input")
MAX_INPUT_LEN = {"username_input": 16, "room_input": 8, "chat_input": 120}


# ---------------------------------------------------------------------------
# Aksi
# ---------------------------------------------------------------------------

def _submit_login(state, net, reconnect=False):
    if reconnect:
        last = state.ui.get("last_session") or {}
        if not (last.get("player_id") and last.get("room_code")):
            return
        state.player_id = last["player_id"]
        state.room_code = last["room_code"]
        state.username = last.get("username", "")
        try:
            net.connect(first_message=("RECONNECT", {
                "player_id": state.player_id,
                "room_code": state.room_code,
            }))
        except OSError as e:
            state.toast(f"Gagal konek: {e}")
        return

    username = (state.ui.get("username_input") or "").strip()
    if not username:
        state.toast("Username tidak boleh kosong")
        return
    state.username = username
    room = (state.ui.get("room_input") or "").strip().upper()
    try:
        net.connect()
    except OSError as e:
        state.toast(f"Gagal konek ke {net.host}:{net.port} — {e}")
        return
    net.send("LOGIN", {"username": username, "room_code": room})


def _send_discard(state, net, index):
    if not (0 <= index < len(state.hand)):
        return
    card = state.hand[index]
    net.send("DISCARD", {"card": {"suit": card.get("suit"),
                                  "rank": card.get("rank")}})
    # Kosongkan aksi lokal supaya klik ganda tidak mengirim dua kali;
    # server akan mengirim VALID_ACTIONS berikutnya.
    state.valid_actions = []


def _send_chat(state, net):
    text = (state.ui.get("chat_input") or "").strip()
    if text:
        net.send("CHAT", {"text": text})
        state.ui["chat_input"] = ""


# ---------------------------------------------------------------------------
# Helper reset ke layar CONNECT
# ---------------------------------------------------------------------------

def _reset_to_connect(state, keep_session=False):
    """Bersihkan state permainan dan kembalikan ke layar login."""
    if keep_session and state.player_id and state.room_code:
        state.ui["last_session"] = {
            "player_id": state.player_id,
            "room_code": state.room_code,
            "username": state.username,
            "host": state.host
        }
    else:
        state.ui.pop("last_session", None)
        # FIX: Hapus file dari hardisk agar tidak muncul tombol saat restart game
        if hasattr(state, "clear_session_file"):
            state.clear_session_file()

    state.phase = "CONNECT"
    state.player_id = ""
    state.room_code = ""
    state.hand = []
    state.players = []
    state.valid_actions = []
    state.current_turn = ""
    state.round_result = None
    state.game_over_info = None
    state.turn_deadline = None
    state.ui["ready_sent"] = False
    state.ui["next_ready_sent"] = False
    state.ui["focus"] = "username_input"


# ---------------------------------------------------------------------------
# Klik mouse
# ---------------------------------------------------------------------------

def _handle_click(pos, state, net):
    hitboxes = state.ui.get("hitboxes") or {}
    key = None
    
    # FIX: Membalik urutan agar elemen teratas (Overlay) dicegat kliknya duluan
    for k, rect in reversed(list(hitboxes.items())):
        if rect.collidepoint(pos):
            key = k
            break

    if key is None:
        if state.ui.get("focus") == "chat_input":
            state.ui["focus"] = None
        return

    # FIX: Prioritaskan tombol Help agar tombol di belakangnya tidak bisa diklik
    if state.ui.get("show_help"):
        if key == "close_help_btn":
            state.ui["show_help"] = False
        return

    actions = state.valid_actions or []

    # Fokus field teks
    if key in TEXT_FIELDS:
        state.ui["focus"] = key
        return
    if state.ui.get("focus") == "chat_input":
        state.ui["focus"] = None

    # --- LOGIKA GAMEPLAY & TOMBOL BAWAAN ---
    if key == "connect_btn":
        _submit_login(state, net)
    elif key == "resume_btn":
        _submit_login(state, net, reconnect=True)
    elif key == "ready_btn":
        if net.send("READY", {}):
            state.ui["ready_sent"] = True
    elif key == "unready_btn":
        # Kirim UNREADY ke server dan batalkan flag lokal
        net.send("UNREADY", {})
        state.ui["ready_sent"] = False
    elif key in ("deck", "btn_take_deck"):
        if "TAKE_DECK" in actions:
            net.send("TAKE_DECK", {})
            state.valid_actions = []
    elif key in ("discard", "btn_take_discard"):
        if "TAKE_DISCARD" in actions:
            net.send("TAKE_DISCARD", {})
            state.valid_actions = []
    elif key == "btn_knock":
        if "KNOCK" in actions:
            net.send("KNOCK", {})
            state.valid_actions = []
    elif isinstance(key, tuple) and key[0] == "hand":
        if state.phase == "MUST_DISCARD" and "DISCARD" in actions:
            _send_discard(state, net, key[1])
    elif key == "next_round_btn":
        if net.send("READY_NEXT_ROUND", {}):
            state.ui["next_ready_sent"] = True
    elif key == "next_unready_btn":
        # Batalkan siap ronde berikutnya
        net.send("UNREADY_NEXT_ROUND", {})
        state.ui["next_ready_sent"] = False

    # --- UPDATE LOGIKA MENU, HELP & QUIT ---
    elif key == "lobby_menu_btn":
        # Jika di Lobby, keluar berarti benar-benar meninggalkan permainan
        net.send("LEAVE", {})
        net.close()
        _reset_to_connect(state, keep_session=False)
    elif key in ("round_end_menu_btn", "ingame_menu_btn"):
        # Jika In-Game, JANGAN kirim LEAVE. Hanya putuskan soket.
        # Ini akan menjadikan pemain abu-abu di meja sehingga dia bisa Reconnect.
        net.close()
        _reset_to_connect(state, keep_session=True)
    elif key == "game_over_menu_btn":
        net.close()
        _reset_to_connect(state, keep_session=False) # Game usai, jangan simpan sesi
    elif key in ("ingame_quit_btn", "game_over_quit_btn", "menu_quit_btn", "exit_btn"):
        if key == "ingame_quit_btn":
            net.send("LEAVE", {})

        # FIX: Hapus file saat menutup aplikasi
        if hasattr(state, "clear_session_file"):
            state.clear_session_file()

        return "quit"
    elif key == "menu_help_btn":
        state.ui["show_help"] = True

# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

def _handle_keydown(event, state, net, voice):
    focus = state.ui.get("focus")

    # --- mengetik di field teks ---
    if focus in TEXT_FIELDS:
        if event.key == pygame.K_BACKSPACE:
            state.ui[focus] = state.ui.get(focus, "")[:-1]
        elif event.key == pygame.K_ESCAPE:
            state.ui["focus"] = None
        elif event.key == pygame.K_TAB and state.phase == "CONNECT":
            state.ui["focus"] = ("room_input" if focus == "username_input"
                                 else "username_input")
        elif event.key == pygame.K_RETURN:
            if focus == "chat_input":
                _send_chat(state, net)
                state.ui["focus"] = None
            elif state.phase == "CONNECT":
                _submit_login(state, net)
        elif event.unicode and event.unicode.isprintable():
            value = state.ui.get(focus, "")
            if len(value) < MAX_INPUT_LEN.get(focus, 32):
                state.ui[focus] = value + event.unicode
        return None

    # --- tanpa fokus teks ---
    if event.key == pygame.K_RETURN:
        if state.phase == "CONNECT":
            _submit_login(state, net)
        elif state.phase not in ("LOBBY",):
            state.ui["focus"] = "chat_input"
        elif state.phase == "LOBBY" and not state.ui.get("ready_sent"):
            if net.send("READY", {}):
                state.ui["ready_sent"] = True
    elif event.key == pygame.K_v:
        if voice is not None:
            voice.set_talking(True)
    elif event.key == pygame.K_ESCAPE:
        if state.phase == "GAME_OVER":
            return "quit"
        state.ui["focus"] = None
    return None


# ---------------------------------------------------------------------------
# Entry point — dipanggil tiap frame dari client.run_gui
# ---------------------------------------------------------------------------

def handle(events, state, net, voice=None) -> bool:
    """Proses semua event satu frame. Return False jika harus keluar."""
    for event in events:
        if event.type == pygame.QUIT:
            return False
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if _handle_click(event.pos, state, net) == "quit":
                return False
        elif event.type == pygame.KEYDOWN:
            if _handle_keydown(event, state, net, voice) == "quit":
                return False
        elif event.type == pygame.KEYUP:
            if event.key == pygame.K_v and voice is not None:
                voice.set_talking(False)
    return True