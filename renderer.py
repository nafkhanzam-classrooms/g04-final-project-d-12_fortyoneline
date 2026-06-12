# =============================================================================
# renderer.py — Game Kartu 41 client
# Person C: semua penggambaran Pygame (layar, sprite kartu, tombol, overlay).
#
# Renderer membaca ClientState dan TIDAK mengubahnya kecuali
# state.ui["hitboxes"]: dict nama → pygame.Rect yang dibangun ulang tiap frame
# supaya input_handler tahu apa yang bisa diklik.
# =============================================================================

import math
import os

import pygame

from shared.constants import SUIT_SYMBOLS

WIDTH, HEIGHT = 1280, 720

ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
CARD_DIR = os.path.join(ASSET_DIR, "cards")
FONT_PATH = os.path.join(ASSET_DIR, "fonts", "Kenney Pixel.ttf")

CARD_W, CARD_H = 64, 64  # ukuran asli pixel art Kenney

# Palet
FELT_DARK = (16, 64, 38)
FELT_LIGHT = (26, 94, 56)
PANEL = (20, 28, 24)
PANEL_LIGHT = (38, 52, 44)
WHITE = (240, 240, 235)
GREY = (150, 155, 150)
DIM_GREY = (95, 100, 96)
GOLD = (235, 190, 80)
RED = (210, 70, 60)
GREEN = (90, 200, 110)
BLUE = (90, 150, 220)

# Map rank → nama file ('2'→'02' ... '9'→'09'; 'A','10','J','Q','K' apa adanya)
_RANK_FILE = {str(n): f"0{n}" for n in range(2, 10)}


# =============================================================================
# Assets (lazy singleton — butuh pygame.init() lebih dulu)
# =============================================================================

class Assets:
    _instance = None

    @classmethod
    def get(cls) -> "Assets":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        # Font: Kenney Pixel kalau ada, kalau tidak font sistem.
        # Font simbol terpisah untuk ♠♥♦♣ (font pixel sering tidak punya glyph).
        def _font(size):
            if os.path.exists(FONT_PATH):
                return pygame.font.Font(FONT_PATH, size)
            return pygame.font.SysFont("dejavusansmono,couriernew", size, bold=True)

        self.font_big = _font(56)
        self.font_h1 = _font(36)
        self.font_body = _font(22)
        self.font_small = _font(16)
        self.font_symbol = pygame.font.SysFont("dejavusans,arial", 22)
        self.font_symbol_big = pygame.font.SysFont("dejavusans,arial", 30)

        self._cards: dict[tuple, pygame.Surface] = {}
        self.card_back = self._load("card_back.png")
        self.card_empty = self._load("card_empty.png")

    # ------------------------------------------------------------------
    def _load(self, filename: str) -> pygame.Surface | None:
        path = os.path.join(CARD_DIR, filename)
        try:
            return pygame.image.load(path).convert_alpha()
        except (pygame.error, FileNotFoundError):
            return None

    def card(self, card: dict | None) -> pygame.Surface:
        """Surface 64x64 untuk satu kartu; fallback digambar manual supaya
        client tidak pernah crash karena asset hilang."""
        if not isinstance(card, dict) or not card.get("rank"):
            return self._placeholder(None, None)
        key = (card.get("suit"), card.get("rank"))
        if key not in self._cards:
            rank_file = _RANK_FILE.get(str(key[1]), str(key[1]))
            surf = self._load(f"card_{key[0]}_{rank_file}.png")
            self._cards[key] = surf or self._placeholder(*key)
        return self._cards[key]

    def back(self) -> pygame.Surface:
        if self.card_back is None:
            self.card_back = self._placeholder(None, None, back=True)
        return self.card_back

    def empty(self) -> pygame.Surface:
        if self.card_empty is None:
            self.card_empty = self._placeholder(None, None, empty=True)
        return self.card_empty

    def _placeholder(self, suit, rank, back=False, empty=False) -> pygame.Surface:
        surf = pygame.Surface((CARD_W, CARD_H), pygame.SRCALPHA)
        if empty:
            pygame.draw.rect(surf, (255, 255, 255, 60),
                             surf.get_rect(), 2, border_radius=6)
            return surf
        color = (60, 70, 140) if back else (250, 250, 245)
        pygame.draw.rect(surf, color, surf.get_rect(), border_radius=6)
        pygame.draw.rect(surf, (30, 30, 30), surf.get_rect(), 2, border_radius=6)
        if not back and rank:
            ink = RED if suit in ("hearts", "diamonds") else (25, 25, 30)
            txt = self.font_small.render(str(rank), True, ink)
            surf.blit(txt, (5, 4))
            sym = self.font_symbol.render(SUIT_SYMBOLS.get(suit, "?"), True, ink)
            surf.blit(sym, sym.get_rect(center=(CARD_W // 2, CARD_H // 2 + 6)))
        return surf


def _scaled(surf: pygame.Surface, factor: int) -> pygame.Surface:
    # Faktor integer menjaga pixel art tetap tajam — jangan smoothscale.
    return pygame.transform.scale_by(surf, factor) if factor != 1 else surf


# =============================================================================
# Helper umum
# =============================================================================

_background: pygame.Surface | None = None


def _felt_background() -> pygame.Surface:
    global _background
    if _background is None:
        _background = pygame.Surface((WIDTH, HEIGHT))
        for y in range(HEIGHT):
            t = abs(y - HEIGHT / 2) / (HEIGHT / 2)
            color = [int(l + (d - l) * t) for l, d in zip(FELT_LIGHT, FELT_DARK)]
            pygame.draw.line(_background, color, (0, y), (WIDTH, y))
    return _background


def _hit(state, key, rect: pygame.Rect):
    state.ui["hitboxes"][key] = rect


def _text(screen, font, text, color, center=None, topleft=None):
    surf = font.render(str(text), True, color)
    rect = surf.get_rect()
    if center:
        rect.center = center
    elif topleft:
        rect.topleft = topleft
    screen.blit(surf, rect)
    return rect


def _button(screen, state, key, rect, label, enabled=True, accent=GOLD):
    a = Assets.get()
    mouse = pygame.mouse.get_pos()
    hover = enabled and rect.collidepoint(mouse)
    bg = accent if hover else (PANEL_LIGHT if enabled else PANEL)
    fg = (20, 20, 20) if hover else (WHITE if enabled else DIM_GREY)
    pygame.draw.rect(screen, bg, rect, border_radius=8)
    pygame.draw.rect(screen, accent if enabled else DIM_GREY, rect, 2,
                     border_radius=8)
    _text(screen, a.font_body, label, fg, center=rect.center)
    if enabled:
        _hit(state, key, rect)


def _input_box(screen, state, key, rect, label, placeholder=""):
    a = Assets.get()
    focused = state.ui.get("focus") == key
    value = state.ui.get(key, "")
    pygame.draw.rect(screen, PANEL_LIGHT, rect, border_radius=6)
    pygame.draw.rect(screen, GOLD if focused else GREY, rect, 2, border_radius=6)
    _text(screen, a.font_small, label, GREY, topleft=(rect.x, rect.y - 22))
    shown = value if value else placeholder
    color = WHITE if value else DIM_GREY
    if focused and value is not None and (pygame.time.get_ticks() // 500) % 2:
        shown = value + "|"
        color = WHITE
    _text(screen, a.font_body, shown, color,
          topleft=(rect.x + 10, rect.y + (rect.h - 22) // 2))
    _hit(state, key, rect)


def _draw_toasts(screen, state, now):
    a = Assets.get()
    y = HEIGHT - 60
    for text in reversed(state.active_toasts(now)):
        surf = a.font_small.render(text, True, WHITE)
        box = surf.get_rect(topleft=(20, y)).inflate(20, 12)
        shade = pygame.Surface(box.size, pygame.SRCALPHA)
        shade.fill((0, 0, 0, 170))
        screen.blit(shade, box.topleft)
        screen.blit(surf, (box.x + 10, box.y + 6))
        y -= box.h + 6


# =============================================================================
# Layar CONNECT
# =============================================================================

def _draw_connect(screen, state, now):
    a = Assets.get()
    _text(screen, a.font_big, "KARTU 41", GOLD, center=(WIDTH // 2, 150))
    _text(screen, a.font_small, "Game jaringan — Progjar D-12", GREY,
          center=(WIDTH // 2, 200))

    _input_box(screen, state, "username_input",
               pygame.Rect(WIDTH // 2 - 180, 290, 360, 44),
               "Username", "ketik nama...")
    _input_box(screen, state, "room_input",
               pygame.Rect(WIDTH // 2 - 180, 380, 360, 44),
               "Kode room (kosongkan untuk buat room baru)", "ABCDE")
    _button(screen, state, "connect_btn",
            pygame.Rect(WIDTH // 2 - 110, 470, 220, 52), "CONNECT")

    last = state.ui.get("last_session") or {}
    if last.get("player_id") and last.get("room_code"):
        _button(screen, state, "resume_btn",
                pygame.Rect(WIDTH // 2 - 170, 545, 340, 40),
                f"RECONNECT ke {last['room_code']} ({last.get('username', '')})",
                accent=BLUE)
    _text(screen, a.font_small,
          f"server: {state.host}   [Tab] pindah kolom  [Enter] connect",
          DIM_GREY, center=(WIDTH // 2, HEIGHT - 30))


# =============================================================================
# Layar LOBBY
# =============================================================================

def _draw_lobby(screen, state, now):
    a = Assets.get()
    _text(screen, a.font_h1, "LOBBY", WHITE, center=(WIDTH // 2, 80))
    _text(screen, a.font_small, "Bagikan kode room ini ke pemain lain:",
          GREY, center=(WIDTH // 2, 140))
    _text(screen, a.font_big, state.room_code or "...", GOLD,
          center=(WIDTH // 2, 200))

    y = 290
    players = state.players or [{"username": state.username, "connected": True}]
    for p in players:
        name = p.get("username", "?")
        me = "  (kamu)" if p.get("player_id") == state.player_id else ""
        ok = p.get("connected", True)
        _text(screen, a.font_body, f"• {name}{me}", WHITE if ok else DIM_GREY,
              center=(WIDTH // 2, y))
        y += 36

    ready_sent = state.ui.get("ready_sent")
    _button(screen, state, "ready_btn",
            pygame.Rect(WIDTH // 2 - 110, 560, 220, 52),
            "MENUNGGU..." if ready_sent else "READY",
            enabled=not ready_sent)
    _text(screen, a.font_small, "Game mulai saat semua pemain (min 2) READY",
          DIM_GREY, center=(WIDTH // 2, 640))


# =============================================================================
# Layar TABLE (PLAYING / MUST_DISCARD + dasar untuk overlay)
# =============================================================================

CHAT_W = 280
TABLE_CX = (WIDTH - CHAT_W) // 2  # pusat area meja (kanan dipakai chat)


def _draw_opponent_panel(screen, state, p, rect, now):
    a = Assets.get()
    connected = p.get("connected", True)
    is_turn = p.get("player_id") == state.current_turn

    pygame.draw.rect(screen, PANEL, rect, border_radius=10)
    if is_turn:
        pygame.draw.rect(screen, GOLD, rect, 3, border_radius=10)

    name_color = WHITE if connected else DIM_GREY
    _text(screen, a.font_body, p.get("username", "?"), name_color,
          topleft=(rect.x + 12, rect.y + 8))

    # Nyawa sebagai deretan ♥
    lives = p.get("lives")
    hearts = "♥" * lives if isinstance(lives, int) else "?"
    _text(screen, a.font_symbol, hearts, RED if connected else DIM_GREY,
          topleft=(rect.x + 12, rect.y + 34))

    # Kipas kartu tertutup sesuai hand_count
    count = p.get("hand_count")
    count = count if isinstance(count, int) else 0
    back = Assets.get().back()
    for i in range(min(count, 6)):
        img = back.copy()
        if not connected:
            img.fill((110, 110, 110, 255), special_flags=pygame.BLEND_RGBA_MULT)
        screen.blit(img, (rect.x + 110 + i * 18, rect.y + 14))

    if not connected:
        _text(screen, a.font_small, "TERPUTUS", RED,
              topleft=(rect.x + 12, rect.y + rect.h - 24))

    # Indikator bicara (voice)
    if state.speaking.get(p.get("player_id"), 0) > now:
        pygame.draw.circle(screen, GREEN, (rect.right - 16, rect.y + 16), 7)


def _draw_countdown(screen, state, now):
    if state.turn_deadline is None:
        return
    remaining = state.turn_deadline - now
    if remaining <= 0:
        return
    frac = max(0.0, min(1.0, remaining / 30.0))
    bar = pygame.Rect(TABLE_CX - 150, 96, 300, 10)
    pygame.draw.rect(screen, PANEL, bar, border_radius=5)
    color = GREEN if frac > 0.5 else (GOLD if frac > 0.2 else RED)
    fill = pygame.Rect(bar.x, bar.y, int(bar.w * frac), bar.h)
    pygame.draw.rect(screen, color, fill, border_radius=5)
    Assets.get()
    _text(screen, Assets.get().font_small, f"{int(remaining)}s", WHITE,
          center=(bar.centerx, bar.y + 24))


def _draw_chat(screen, state, now):
    a = Assets.get()
    panel = pygame.Rect(WIDTH - CHAT_W, 0, CHAT_W, HEIGHT)
    shade = pygame.Surface(panel.size, pygame.SRCALPHA)
    shade.fill((0, 0, 0, 120))
    screen.blit(shade, panel.topleft)
    _text(screen, a.font_body, "CHAT", GREY, topleft=(panel.x + 14, 12))

    input_rect = pygame.Rect(panel.x + 10, HEIGHT - 46, CHAT_W - 20, 34)
    y = input_rect.y - 26
    for entry in reversed(state.chat_log[-14:]):
        line = f"{entry['username']}: {entry['text']}"
        surf = a.font_small.render(line[:40], True, WHITE)
        screen.blit(surf, (panel.x + 14, y))
        y -= 22
        if y < 44:
            break

    focused = state.ui.get("focus") == "chat_input"
    pygame.draw.rect(screen, PANEL_LIGHT, input_rect, border_radius=6)
    pygame.draw.rect(screen, GOLD if focused else GREY, input_rect, 2,
                     border_radius=6)
    value = state.ui.get("chat_input", "")
    shown = value or "[Enter] untuk chat"
    if focused and (pygame.time.get_ticks() // 500) % 2:
        shown = value + "|"
    _text(screen, a.font_small, shown[-34:], WHITE if value else DIM_GREY,
          topleft=(input_rect.x + 8, input_rect.y + 8))
    _hit(state, "chat_input", input_rect)


def _draw_table(screen, state, now):
    a = Assets.get()

    # HUD kiri-atas
    ping = f"{state.latency_ms} ms" if state.latency_ms is not None else "--"
    _text(screen, a.font_small,
          f"Room {state.room_code}   Ronde {state.round_number}   Ping {ping}",
          GREY, topleft=(16, 12))

    # Nama pemain yang sedang giliran
    if state.current_turn:
        who = ("GILIRANMU!" if state.is_my_turn()
               else f"Giliran: {state.username_for(state.current_turn)}")
        _text(screen, a.font_h1, who, GOLD if state.is_my_turn() else WHITE,
              center=(TABLE_CX, 70))
    _draw_countdown(screen, state, now)

    # Panel lawan (atas / sisi)
    others = [p for p in state.players if p.get("player_id") != state.player_id]
    panel_w, panel_h = 230, 92
    spots = [
        pygame.Rect(TABLE_CX - panel_w // 2, 120, panel_w, panel_h),
        pygame.Rect(40, 240, panel_w, panel_h),
        pygame.Rect(WIDTH - CHAT_W - panel_w - 40, 240, panel_w, panel_h),
    ]
    for p, rect in zip(others, spots):
        _draw_opponent_panel(screen, state, p, rect, now)

    # Tengah: deck + discard
    cy = 330
    deck_img = _scaled(a.back(), 2)
    deck_rect = deck_img.get_rect(center=(TABLE_CX - 90, cy + 40))
    for off in (6, 3, 0):  # efek tumpukan
        screen.blit(deck_img, (deck_rect.x - off, deck_rect.y - off))
    _hit(state, "deck", deck_rect.inflate(12, 12))
    _text(screen, a.font_small, f"Deck: {state.deck_count}", WHITE,
          center=(deck_rect.centerx, deck_rect.bottom + 18))

    disc_img = _scaled(a.card(state.discard_top) if state.discard_top
                       else a.empty(), 2)
    disc_rect = disc_img.get_rect(center=(TABLE_CX + 90, cy + 40))
    screen.blit(disc_img, disc_rect)
    _hit(state, "discard", disc_rect.inflate(12, 12))
    _text(screen, a.font_small, "Buangan", WHITE,
          center=(disc_rect.centerx, disc_rect.bottom + 18))

    # Tombol aksi — aktif hanya jika ada di valid_actions
    actions = state.valid_actions or []
    bx, by, bw, bh, gap = TABLE_CX - 280, 470, 170, 44, 20
    _button(screen, state, "btn_take_deck",
            pygame.Rect(bx, by, bw, bh), "TAKE DECK",
            enabled="TAKE_DECK" in actions)
    _button(screen, state, "btn_take_discard",
            pygame.Rect(bx + bw + gap, by, bw, bh), "TAKE PILE",
            enabled="TAKE_DISCARD" in actions)
    _button(screen, state, "btn_knock",
            pygame.Rect(bx + 2 * (bw + gap), by, bw, bh), "KNOCK",
            enabled="KNOCK" in actions, accent=RED)

    if state.phase == "MUST_DISCARD":
        _text(screen, a.font_body, "Pilih kartu untuk dibuang!", GOLD,
              center=(TABLE_CX, 545))

    # Tangan sendiri, bawah-tengah, klikabel
    hand = state.hand
    if hand:
        scale = 2
        cw = CARD_W * scale
        spacing = min(cw + 12, max(60, (TABLE_CX * 2 - 120) // max(1, len(hand))))
        total = spacing * (len(hand) - 1) + cw
        x0 = TABLE_CX - total // 2
        mouse = pygame.mouse.get_pos()
        base_y = HEIGHT - cw - 20
        for i, card in enumerate(hand):
            rect = pygame.Rect(x0 + i * spacing, base_y, cw, cw)
            hovered = rect.collidepoint(mouse)
            if hovered:
                rect = rect.move(0, -16)  # hover lift
            img = _scaled(a.card(card), scale)
            if state.phase == "MUST_DISCARD" and hovered:
                pygame.draw.rect(screen, GOLD, rect.inflate(8, 8), 3,
                                 border_radius=8)
            screen.blit(img, rect)
            _hit(state, ("hand", i), rect)

    # Info nyawa sendiri
    mine = next((p for p in state.players
                 if p.get("player_id") == state.player_id), None)
    if mine is not None and isinstance(mine.get("lives"), int):
        _text(screen, a.font_symbol_big, "♥" * mine["lives"], RED,
              topleft=(16, HEIGHT - 40))

    _draw_chat(screen, state, now)


# =============================================================================
# Overlay ROUND_END / WAITING_READY
# =============================================================================

def _draw_card_row(screen, cards, x, y):
    a = Assets.get()
    for i, c in enumerate(cards or []):
        if isinstance(c, dict):
            screen.blit(a.card(c), (x + i * (CARD_W + 6), y))


def _draw_round_end(screen, state, now):
    a = Assets.get()
    result = state.round_result or {}

    shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 175))
    screen.blit(shade, (0, 0))

    panel = pygame.Rect(WIDTH // 2 - 460, 50, 920, 600)
    pygame.draw.rect(screen, PANEL, panel, border_radius=14)
    pygame.draw.rect(screen, GOLD, panel, 2, border_radius=14)

    knocker = result.get("knocker")
    title = (f"{state.username_for(knocker)} KNOCK — hasil ronde"
             if knocker else "Hasil ronde")
    _text(screen, a.font_h1, title, GOLD, center=(panel.centerx, panel.y + 36))

    scores = result.get("scores") or {}
    hands = result.get("hands") or {}
    winners = set(result.get("winners") or [])
    losers = set(result.get("losers") or [])
    lives = result.get("lives") or {}
    eliminated = set(result.get("eliminated") or [])

    pids = list(scores.keys()) or [p.get("player_id") for p in state.players]
    y = panel.y + 80
    for pid in pids:
        if pid is None:
            continue
        name = state.username_for(pid)
        if pid in winners:
            tag, color = "MENANG", GREEN
        elif pid in eliminated:
            tag, color = "TERSINGKIR", RED
        elif pid in losers:
            tag, color = "-1 NYAWA", RED
        else:
            tag, color = "", WHITE
        _text(screen, a.font_body, name, color, topleft=(panel.x + 30, y + 20))
        _text(screen, a.font_small, tag, color, topleft=(panel.x + 30, y + 48))
        _draw_card_row(screen, hands.get(pid), panel.x + 230, y)
        score = scores.get(pid)
        _text(screen, a.font_h1, "?" if score is None else score, color,
              center=(panel.right - 130, y + 32))
        lv = lives.get(pid)
        if isinstance(lv, int):
            _text(screen, a.font_symbol, "♥" * lv if lv > 0 else "✖", RED,
                  center=(panel.right - 60, y + 32))
        y += CARD_H + 26

    if state.phase == "WAITING_READY":
        if state.ui.get("next_ready_sent"):
            _text(screen, a.font_body, "Menunggu pemain lain...", GREY,
                  center=(panel.centerx, panel.bottom - 45))
        else:
            _button(screen, state, "next_round_btn",
                    pygame.Rect(panel.centerx - 150, panel.bottom - 70, 300, 48),
                    "SIAP RONDE BERIKUTNYA")
    else:
        _text(screen, a.font_small, "Menunggu server...", GREY,
              center=(panel.centerx, panel.bottom - 40))


# =============================================================================
# Layar GAME_OVER
# =============================================================================

def _draw_game_over(screen, state, now):
    a = Assets.get()
    info = state.game_over_info or {}
    winner = (info.get("winner_username")
              or state.username_for(info.get("winner_id", ""))
              or "???")
    _text(screen, a.font_big, "GAME OVER", RED, center=(WIDTH // 2, 160))
    _text(screen, a.font_h1, f"Juara: {winner}", GOLD,
          center=(WIDTH // 2, 260))
    if info.get("message"):
        _text(screen, a.font_body, info["message"], WHITE,
              center=(WIDTH // 2, 320))

    # Tabel nyawa terakhir dari hasil ronde final
    lives = (state.round_result or {}).get("lives") or {}
    y = 390
    for pid, lv in lives.items():
        hearts = "♥" * lv if isinstance(lv, int) and lv > 0 else "tersingkir"
        _text(screen, a.font_body,
              f"{state.username_for(pid)}  —  {hearts}", WHITE,
              center=(WIDTH // 2, y))
        y += 34

    _button(screen, state, "exit_btn",
            pygame.Rect(WIDTH // 2 - 100, HEIGHT - 110, 200, 48), "KELUAR")


# =============================================================================
# Overlay RECONNECTING
# =============================================================================

def _draw_reconnecting(screen, state, now):
    a = Assets.get()
    shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 190))
    screen.blit(shade, (0, 0))

    cx, cy = WIDTH // 2, HEIGHT // 2
    # Spinner: busur berputar
    angle = (now * 4) % (2 * math.pi)
    pygame.draw.arc(screen, GOLD, pygame.Rect(cx - 36, cy - 96, 72, 72),
                    angle, angle + 4.2, 5)
    _text(screen, a.font_h1, "Menghubungkan ulang...", WHITE, center=(cx, cy + 20))
    if state.reconnect_deadline:
        remaining = max(0, int(state.reconnect_deadline - now))
        _text(screen, a.font_body, f"sisa waktu: {remaining}s", GREY,
              center=(cx, cy + 64))


# =============================================================================
# Entry point
# =============================================================================

def draw(screen, state, now):
    state.ui["hitboxes"] = {}
    screen.blit(_felt_background(), (0, 0))

    phase = state.phase
    if phase == "CONNECT":
        _draw_connect(screen, state, now)
    elif phase == "LOBBY":
        _draw_lobby(screen, state, now)
    elif phase == "GAME_OVER":
        _draw_game_over(screen, state, now)
    else:
        _draw_table(screen, state, now)
        if phase in ("ROUND_END", "WAITING_READY"):
            _draw_round_end(screen, state, now)
        elif phase == "RECONNECTING":
            _draw_reconnecting(screen, state, now)

    _draw_toasts(screen, state, now)
