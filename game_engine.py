# =============================================================================
# game_engine.py
# Logika permainan kartu 41 — TIDAK ada kode jaringan di sini.
#
# CARA PAKAI (untuk Person B — server):
#   from game_engine import GameEngine
#   engine = GameEngine(['farrel', 'ali', 'uwais', 'achmad'])
#   engine.start_round()
#   engine.initial_discard('farrel', card_index=2)
#   result = engine.take_card('ali', source='DECK')
#   result = engine.discard_card('ali', card_index=0)
#   result = engine.knock('uwais')
#   state  = engine.get_state_for_player('ali')   # kartu lawan tersembunyi
#   log    = engine.get_activity_log()            # untuk match replay / logging
#   snap   = engine.get_reconnect_snapshot('ali') # kirim ke client yang reconnect
# =============================================================================

import random
import datetime
from shared.constants import (
    CARD_VALUES, SUITS, RANKS,
    INITIAL_LIVES, FIRST_PLAYER_CARDS, OTHER_PLAYER_CARDS,
    MIN_PLAYERS, MAX_PLAYERS,
    SOURCE_DECK, SOURCE_DISCARD,
    LOG_EVENT_TAKE, LOG_EVENT_DISCARD, LOG_EVENT_KNOCK,
    LOG_EVENT_ROUND_END, LOG_EVENT_GAME_OVER,
)


# =============================================================================
# Card — representasi satu kartu
# =============================================================================

class Card:
    def __init__(self, suit: str, rank: str):
        """
        suit : 'spades' | 'hearts' | 'diamonds' | 'clubs'
        rank : 'A' | '2' .. '10' | 'J' | 'Q' | 'K'
        """
        self.suit = suit
        self.rank = rank

    @property
    def value(self) -> int:
        """Nilai poin kartu ini."""
        return CARD_VALUES[self.rank]

    def to_dict(self) -> dict:
        """Konversi ke dict — dipakai protocol.py saat encode JSON."""
        return {'suit': self.suit, 'rank': self.rank}

    @classmethod
    def from_dict(cls, d: dict) -> 'Card':
        """Buat Card dari dict — dipakai saat decode JSON dari client/server."""
        return cls(d['suit'], d['rank'])

    def __repr__(self):
        from shared.constants import SUIT_SYMBOLS
        sym = SUIT_SYMBOLS.get(self.suit, self.suit[0].upper())
        return f"{self.rank}{sym}"

    def __eq__(self, other):
        return isinstance(other, Card) and self.suit == other.suit and self.rank == other.rank


# =============================================================================
# Deck — tumpukan kartu tertutup
# =============================================================================

class Deck:
    def __init__(self):
        self.cards: list[Card] = [
            Card(suit, rank) for suit in SUITS for rank in RANKS
        ]
        random.shuffle(self.cards)

    def draw(self) -> Card | None:
        """Ambil 1 kartu dari atas deck. Return None jika deck kosong."""
        return self.cards.pop() if self.cards else None

    def is_empty(self) -> bool:
        return len(self.cards) == 0

    def remaining(self) -> int:
        return len(self.cards)


# =============================================================================
# Fungsi hitung skor — berdiri sendiri, mudah ditest
# =============================================================================

def calculate_score(hand: list[Card]) -> int:
    """
    Hitung skor tangan berdasarkan suit terbanyak.

    Cara hitung:
    - Kelompokkan nilai kartu per suit
    - Suit dengan total nilai tertinggi = mayoritas (dijumlahkan)
    - Suit lain = pengurang (dijumlahkan lalu dikurangi dari mayoritas)

    Contoh: ♥10 + ♥7 + ♠5 + ♣3
      → hearts=17, spades=5, clubs=3
      → mayoritas=17, pengurang=8 → skor = 17 - 8 = 9

    Contoh skor tinggi: ♥A + ♥10 + ♥7 + ♠2
      → hearts=28, spades=2
      → mayoritas=28, pengurang=2 → skor = 28 - 2 = 26
    """
    if not hand:
        return 0

    suit_totals: dict[str, int] = {}
    for card in hand:
        suit_totals[card.suit] = suit_totals.get(card.suit, 0) + card.value

    majority_suit  = max(suit_totals, key=lambda s: suit_totals[s])
    majority_total = suit_totals[majority_suit]
    minority_total = sum(v for s, v in suit_totals.items() if s != majority_suit)

    return majority_total - minority_total


# =============================================================================
# Validasi paket — anti-invalid packet sederhana
# =============================================================================

def validate_action_packet(msg: dict) -> tuple[bool, str]:
    """
    Validasi struktur dan isi paket aksi yang datang dari client.
    Dipanggil di server.py SEBELUM meneruskan ke game_engine.

    Args:
        msg : dict hasil decode dari protocol.decode()

    Return:
        (True, '')           — paket valid
        (False, 'alasan')    — paket tidak valid, sertakan alasan ke client

    Contoh pakai di server.py:
        msg = protocol.recv_message(conn)
        ok, reason = validate_action_packet(msg)
        if not ok:
            conn.sendall(protocol.encode_error(reason))
            continue
        # proses aksi ...
    """
    # ── Cek struktur envelope ─────────────────────────────────────────────────
    if not isinstance(msg, dict):
        return False, 'Paket harus berupa JSON object'

    if 'type' not in msg:
        return False, "Field 'type' tidak ada"

    if 'payload' not in msg:
        return False, "Field 'payload' tidak ada"

    if not isinstance(msg['payload'], dict):
        return False, "Field 'payload' harus berupa JSON object"

    if msg['type'] != 'ACTION':
        # Tipe lain (PING, RECONNECT_REQ) divalidasi terpisah di server
        return True, ''

    payload = msg['payload']

    # ── Cek field 'action' ────────────────────────────────────────────────────
    if 'action' not in payload:
        return False, "Field 'action' tidak ada di payload"

    action = payload['action']
    valid_actions = {'TAKE', 'DISCARD', 'KNOCK', 'INITIAL_DISCARD'}
    if action not in valid_actions:
        return False, f"Aksi tidak dikenal: {action!r}. Pilihan valid: {sorted(valid_actions)}"

    # ── Cek field per aksi ────────────────────────────────────────────────────
    if action in ('TAKE', 'INITIAL_DISCARD'):
        if action == 'TAKE':
            if 'source' not in payload:
                return False, "TAKE membutuhkan field 'source'"
            if payload['source'] not in ('DECK', 'DISCARD'):
                return False, f"'source' tidak valid: {payload['source']!r}. Gunakan 'DECK' atau 'DISCARD'"

    if action in ('DISCARD', 'INITIAL_DISCARD'):
        if 'card_index' not in payload:
            return False, f"{action} membutuhkan field 'card_index'"
        if not isinstance(payload['card_index'], int):
            return False, f"'card_index' harus integer, bukan {type(payload['card_index']).__name__}"
        if payload['card_index'] < 0:
            return False, f"'card_index' tidak boleh negatif"

    return True, ''


def validate_ping_packet(msg: dict) -> tuple[bool, str]:
    """
    Validasi paket PING dari client.

    Paket PING harus punya struktur:
        {'type': 'PING', 'payload': {'timestamp': float}}
    """
    if not isinstance(msg, dict):
        return False, 'Paket harus berupa JSON object'
    if msg.get('type') != 'PING':
        return False, "Tipe bukan PING"
    payload = msg.get('payload', {})
    if 'timestamp' not in payload:
        return False, "PING membutuhkan field 'timestamp'"
    if not isinstance(payload['timestamp'], (int, float)):
        return False, "'timestamp' harus berupa angka"
    return True, ''


def validate_reconnect_packet(msg: dict) -> tuple[bool, str]:
    """
    Validasi paket RECONNECT_REQ dari client.

    Paket harus punya struktur:
        {'type': 'RECONNECT_REQ', 'payload': {'player_id': str, 'session_id': str}}
    """
    if not isinstance(msg, dict):
        return False, 'Paket harus berupa JSON object'
    if msg.get('type') != 'RECONNECT_REQ':
        return False, "Tipe bukan RECONNECT_REQ"
    payload = msg.get('payload', {})
    if 'player_id' not in payload:
        return False, "RECONNECT_REQ membutuhkan field 'player_id'"
    if 'session_id' not in payload:
        return False, "RECONNECT_REQ membutuhkan field 'session_id'"
    if not isinstance(payload['player_id'], str) or not payload['player_id'].strip():
        return False, "'player_id' harus string tidak kosong"
    if not isinstance(payload['session_id'], str) or not payload['session_id'].strip():
        return False, "'session_id' harus string tidak kosong"
    return True, ''


# =============================================================================
# ActivityLog — logging aksi pemain untuk replay & audit
# =============================================================================

class ActivityLog:
    """
    Mencatat semua aksi selama satu sesi game.
    Dipakai engine secara internal; Person B bisa ambil via engine.get_activity_log().

    Setiap entry adalah dict:
    {
        'timestamp' : str ISO 8601,
        'round'     : int,
        'player'    : str,
        'event'     : str,   # LOG_EVENT_* dari constants
        'detail'    : dict,  # data spesifik per event
    }
    """

    def __init__(self):
        self._entries: list[dict] = []

    def record(self, round_num: int, player: str, event: str, detail: dict = None):
        """Catat satu event. Dipanggil otomatis oleh GameEngine."""
        self._entries.append({
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',
            'round':     round_num,
            'player':    player,
            'event':     event,
            'detail':    detail or {},
        })

    def get_all(self) -> list[dict]:
        """Return semua log entry — untuk disimpan server atau dikirim sebagai replay."""
        return list(self._entries)

    def get_round(self, round_num: int) -> list[dict]:
        """Return log entry untuk ronde tertentu saja."""
        return [e for e in self._entries if e['round'] == round_num]

    def get_player(self, player_id: str) -> list[dict]:
        """Return semua aksi satu pemain sepanjang game."""
        return [e for e in self._entries if e['player'] == player_id]

    def clear(self):
        self._entries.clear()


# =============================================================================
# GameEngine — state permainan + semua aksi yang valid
# =============================================================================

class GameEngine:
    """
    Mengelola satu sesi permainan 41 dari lobby hingga game over.

    ── Atribut publik (boleh dibaca Person B) ───────────────────────────────
        player_ids      : list urutan pemain
        lives           : dict {player_id: sisa_nyawa}
        hands           : dict {player_id: list[Card]}  ← JANGAN kirim ke client!
        discard_pile    : list[Card]
        current_player  : player_id yang sedang giliran
        round_active    : bool
        round_number    : int
        session_id      : str — ID unik sesi, dipakai untuk reconnect

    ── Semua method aksi mengembalikan dict dengan key 'success' (bool) ─────
        Jika success=False → ada key 'error' berisi penjelasan.
    """

    def __init__(self, player_ids: list, session_id: str = None):
        """
        player_ids : list ID pemain, urutan = urutan duduk.
                     Elemen pertama = dealer (dapat 5 kartu di awal ronde).
        session_id : ID unik sesi untuk keperluan reconnect.
                     Jika None, di-generate otomatis dari timestamp.

        Contoh: GameEngine(['farrel', 'ali', 'uwais', 'achmad'])
        """
        if not MIN_PLAYERS <= len(player_ids) <= MAX_PLAYERS:
            raise ValueError(f"Jumlah pemain harus {MIN_PLAYERS}–{MAX_PLAYERS}")

        self.player_ids: list         = list(player_ids)
        self.lives: dict              = {pid: INITIAL_LIVES for pid in player_ids}
        self.hands: dict              = {pid: [] for pid in player_ids}
        self.scores: dict             = {}

        self.deck: Deck               = Deck()
        self.discard_pile: list[Card] = []
        self.current_turn_index: int  = 0
        self.round_active: bool       = False
        self.round_number: int        = 0

        # Session ID untuk reconnect handling (Person B simpan ini di session.py)
        self.session_id: str = session_id or _generate_session_id()

        # Activity log — otomatis diisi engine, dipakai untuk replay & logging
        self.log: ActivityLog = ActivityLog()

        # Fase khusus awal ronde
        self._waiting_initial_discard: bool = False

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def current_player(self):
        """ID pemain yang sedang giliran."""
        return self.player_ids[self.current_turn_index]

    @property
    def active_players(self) -> list:
        """Daftar pemain yang masih punya nyawa."""
        return [pid for pid in self.player_ids if self.lives[pid] > 0]

    # ── Setup ronde ───────────────────────────────────────────────────────────

    def start_round(self) -> dict:
        """
        Kocok ulang deck, bagikan kartu, siapkan state ronde baru.
        Setelah dipanggil, server harus minta initial_discard dari pemain pertama.

        Return:
            {'success': True, 'round': int, 'first_player': str, 'hands_dealt': dict}
        """
        self.deck         = Deck()
        self.discard_pile = []
        self.hands        = {pid: [] for pid in self.player_ids}
        self.scores       = {}
        self.round_number += 1

        # Pemain pertama dapat 5 kartu
        first = self.player_ids[0]
        for _ in range(FIRST_PLAYER_CARDS):
            card = self.deck.draw()
            if card:
                self.hands[first].append(card)

        # Pemain lain dapat 4 kartu
        for pid in self.player_ids[1:]:
            for _ in range(OTHER_PLAYER_CARDS):
                card = self.deck.draw()
                if card:
                    self.hands[pid].append(card)

        self.current_turn_index       = 0
        self._waiting_initial_discard = True
        self.round_active             = False

        return {
            'success':      True,
            'round':        self.round_number,
            'first_player': first,
            'hands_dealt':  {pid: len(self.hands[pid]) for pid in self.player_ids},
        }

    def initial_discard(self, player_id, card_index: int) -> dict:
        """
        Pemain pertama wajib buang 1 dari 5 kartunya → jadi kartu teratas discard pile.
        Harus dipanggil tepat sekali setelah start_round, sebelum aksi lain.

        Args:
            player_id  : harus == player_ids[0]
            card_index : index kartu di tangan (0–4)

        Return sukses:
            {'success': True, 'discarded': dict_kartu, 'next_player': str}
        """
        if not self._waiting_initial_discard:
            return {'success': False, 'error': 'Initial discard sudah selesai atau start_round belum dipanggil'}
        if player_id != self.player_ids[0]:
            return {'success': False, 'error': f'Hanya {self.player_ids[0]} (pemain pertama) yang melakukan initial discard'}
        if self.discard_pile:
            return {'success': False, 'error': 'Initial discard sudah dilakukan sebelumnya'}

        hand = self.hands[player_id]
        if not (0 <= card_index < len(hand)):
            return {'success': False, 'error': f'Index kartu tidak valid (0–{len(hand)-1})'}

        card = hand.pop(card_index)
        self.discard_pile.append(card)

        self._waiting_initial_discard = False
        self.round_active             = True
        self.current_turn_index       = 1

        # ── Log ───────────────────────────────────────────────────────────────
        self.log.record(self.round_number, player_id, LOG_EVENT_DISCARD, {
            'phase':    'initial',
            'card':     card.to_dict(),
        })

        return {
            'success':     True,
            'discarded':   card.to_dict(),
            'next_player': self.current_player,
        }

    # ── Aksi per giliran ──────────────────────────────────────────────────────

    def take_card(self, player_id, source: str) -> dict:
        """
        Pemain ambil 1 kartu dari deck tertutup atau dari atas discard pile.

        Args:
            player_id : ID pemain yang mengambil
            source    : 'DECK' atau 'DISCARD'

        Return sukses:
            {'success': True, 'card': dict_kartu, 'deck_remaining': int}

        Setelah take_card sukses, pemain WAJIB discard_card sebelum giliran berakhir.
        """
        if not self.round_active:
            return {'success': False, 'error': 'Ronde belum aktif'}
        if player_id != self.current_player:
            return {'success': False, 'error': f'Bukan giliran {player_id}, sekarang giliran {self.current_player}'}

        if source == SOURCE_DECK:
            if self.deck.is_empty():
                return {'success': False, 'error': 'Deck sudah kosong'}
            card = self.deck.draw()

        elif source == SOURCE_DISCARD:
            if not self.discard_pile:
                return {'success': False, 'error': 'Discard pile kosong'}
            card = self.discard_pile.pop()

        else:
            return {'success': False, 'error': f'Source tidak valid: {source!r}. Gunakan "DECK" atau "DISCARD"'}

        self.hands[player_id].append(card)

        # ── Log ───────────────────────────────────────────────────────────────
        self.log.record(self.round_number, player_id, LOG_EVENT_TAKE, {
            'source':         source,
            'card':           card.to_dict(),
            'deck_remaining': self.deck.remaining(),
        })

        return {
            'success':        True,
            'card':           card.to_dict(),
            'deck_remaining': self.deck.remaining(),
        }

    def discard_card(self, player_id, card_index: int) -> dict:
        """
        Pemain buang 1 kartu dari tangan ke discard pile.
        Dipanggil SETELAH take_card. Mengakhiri giliran pemain ini.

        Args:
            player_id  : ID pemain yang membuang
            card_index : index kartu di tangan yang ingin dibuang

        Return sukses:
            {'success': True, 'discarded': dict_kartu, 'next_player': str}
        Jika deck habis setelah buang:
            {..., 'trigger': 'DECK_EMPTY'}  ← server harus panggil force_showdown()
        """
        if not self.round_active:
            return {'success': False, 'error': 'Ronde belum aktif'}
        if player_id != self.current_player:
            return {'success': False, 'error': f'Bukan giliran {player_id}, sekarang giliran {self.current_player}'}

        hand = self.hands[player_id]
        if not (0 <= card_index < len(hand)):
            return {'success': False, 'error': f'Index kartu tidak valid (0–{len(hand)-1})'}

        card = hand.pop(card_index)
        self.discard_pile.append(card)
        self._advance_turn()

        # ── Log ───────────────────────────────────────────────────────────────
        self.log.record(self.round_number, player_id, LOG_EVENT_DISCARD, {
            'card':           card.to_dict(),
            'deck_remaining': self.deck.remaining(),
        })

        result = {
            'success':     True,
            'discarded':   card.to_dict(),
            'next_player': self.current_player,
        }

        if self.deck.is_empty():
            result['trigger'] = 'DECK_EMPTY'

        return result

    def knock(self, player_id) -> dict:
        """
        Pemain memilih KNOCK — dilakukan SEBELUM mengambil kartu di giliran ini.
        Langsung menutup ronde dan menghitung skor semua pemain.

        Return: hasil dari _resolve_round().
        """
        if not self.round_active:
            return {'success': False, 'error': 'Ronde belum aktif'}
        if player_id != self.current_player:
            return {'success': False, 'error': f'Bukan giliran {player_id}, sekarang giliran {self.current_player}'}

        # ── Log ───────────────────────────────────────────────────────────────
        self.log.record(self.round_number, player_id, LOG_EVENT_KNOCK, {})

        return self._resolve_round(knocker=player_id)

    def force_showdown(self, knocker=None) -> dict:
        """
        Dipanggil server saat deck habis (discard_card mengembalikan trigger='DECK_EMPTY')
        ATAU setelah fase last-turn selesai pasca KNOCK.
        Semua kartu dibuka dan skor dihitung.

        [FIX #4 — Person C] Tambah parameter opsional `knocker`: session.py
        menjalankan alur LAST_TURN_PHASE sendiri (knock TIDAK menutup ronde
        seketika), lalu memanggil force_showdown(knocker=...) supaya hasil
        ROUND_END tetap mencantumkan siapa yang knock.

        Return: sama dengan _resolve_round().
        """
        if not self.round_active:
            return {'success': False, 'error': 'Ronde belum aktif'}
        return self._resolve_round(knocker=knocker)

    # ── Query state ───────────────────────────────────────────────────────────

    def get_state_for_player(self, viewer_id) -> dict:
        """
        Kembalikan state yang HANYA boleh dilihat oleh viewer_id.
        Kartu tangan lawan DISEMBUNYIKAN — hanya jumlah kartunya yang terlihat.

        Dipakai server setiap kali perlu kirim update ke satu client.
        Termasuk field 'session_id' untuk keperluan reconnect di sisi client.

        Return:
        {
            'session_id'     : str,
            'round'          : int,
            'round_active'   : bool,
            'current_player' : str,
            'your_hand'      : [{'suit':..,'rank':..}, ...],
            'hand_count'     : int,
            'top_discard'    : {'suit':..,'rank':..} | None,
            'deck_remaining' : int,
            'lives'          : {player_id: int, ...},
            'opponents'      : {player_id: {'card_count': int}, ...},
        }
        """
        if viewer_id not in self.player_ids:
            return {'error': f'Player {viewer_id!r} tidak dikenal'}

        return {
            'session_id':     self.session_id,
            'round':          self.round_number,
            'round_active':   self.round_active,
            'current_player': self.current_player,
            'your_hand':      [c.to_dict() for c in self.hands[viewer_id]],
            'hand_count':     len(self.hands[viewer_id]),
            'top_discard':    self.discard_pile[-1].to_dict() if self.discard_pile else None,
            'deck_remaining': self.deck.remaining(),
            'lives':          dict(self.lives),
            'opponents':      {
                pid: {'card_count': len(self.hands[pid])}
                for pid in self.player_ids if pid != viewer_id
            },
        }

    def get_full_state(self) -> dict:
        """
        State lengkap SEMUA kartu semua pemain.
        Hanya untuk logging server-side atau spectator mode. JANGAN kirim ke client biasa.
        """
        return {
            'session_id':     self.session_id,
            'round':          self.round_number,
            'round_active':   self.round_active,
            'current_player': self.current_player,
            'deck_remaining': self.deck.remaining(),
            'top_discard':    self.discard_pile[-1].to_dict() if self.discard_pile else None,
            'lives':          dict(self.lives),
            'hands':          {
                pid: [c.to_dict() for c in cards]
                for pid, cards in self.hands.items()
            },
        }

    def get_reconnect_snapshot(self, player_id: str) -> dict:
        """
        Kirim ke client yang baru reconnect supaya state-nya langsung sinkron.
        Sama seperti get_state_for_player, tapi ditambah flag 'reconnected': True
        supaya client tahu ini adalah full-sync, bukan update biasa.

        Contoh pakai di server.py (session.py):
            snap = engine.get_reconnect_snapshot('ali')
            conn.sendall(protocol.encode_reconnect_ok(snap))
        """
        if player_id not in self.player_ids:
            return {'error': f'Player {player_id!r} tidak ditemukan dalam sesi ini'}

        state = self.get_state_for_player(player_id)
        state['reconnected']   = True
        state['player_order']  = self.player_ids  # client perlu tahu urutan duduk
        return state

    def get_activity_log(self) -> list[dict]:
        """
        Return semua log entry untuk sesi ini.
        Dipakai Person B untuk: menyimpan ke file, mengirim sebagai match replay,
        atau menampilkan riwayat aksi di spectator mode.

        Setiap entry: {'timestamp', 'round', 'player', 'event', 'detail'}
        """
        return self.log.get_all()

    def get_round_log(self, round_num: int = None) -> list[dict]:
        """
        Return log untuk ronde tertentu. Default: ronde sekarang.
        """
        return self.log.get_round(round_num or self.round_number)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _resolve_round(self, knocker) -> dict:
        """
        Hitung skor semua pemain, tentukan pemenang, kurangi nyawa yang kalah.

        Return:
        {
            'success'   : True,
            'knocker'   : str | None,
            'scores'    : {player_id: int, ...},
            'hands'     : {player_id: [dict_kartu, ...], ...},
            'winners'   : [player_id, ...],
            'losers'    : [player_id, ...],
            'lives'     : {player_id: int, ...},
            'eliminated': [player_id, ...],
            'game_over' : bool,
            'champion'  : str | None,
        }
        """
        self.round_active = False
        self.scores = {
            pid: calculate_score(self.hands[pid]) for pid in self.player_ids
        }

        max_score = max(self.scores.values())
        winners   = [pid for pid, sc in self.scores.items() if sc == max_score]
        losers    = [pid for pid in self.player_ids if pid not in winners]

        for pid in losers:
            self.lives[pid] -= 1

        eliminated  = [pid for pid in self.player_ids if self.lives[pid] <= 0]
        still_alive = [pid for pid in self.player_ids if self.lives[pid] > 0]
        game_over   = len(still_alive) <= 1

        # ── Log ───────────────────────────────────────────────────────────────
        self.log.record(self.round_number, knocker or 'system', LOG_EVENT_ROUND_END, {
            'knocker':    knocker,
            'scores':     dict(self.scores),
            'winners':    winners,
            'losers':     losers,
            'eliminated': eliminated,
        })
        if game_over:
            champion = still_alive[0] if still_alive else None
            self.log.record(self.round_number, champion or 'system', LOG_EVENT_GAME_OVER, {
                'champion': champion,
            })

        result = {
            'success':    True,
            'knocker':    knocker,
            'scores':     dict(self.scores),
            'hands':      {
                pid: [c.to_dict() for c in self.hands[pid]]
                for pid in self.player_ids
            },
            'winners':    winners,
            'losers':     losers,
            'lives':      dict(self.lives),
            'eliminated': eliminated,
            'game_over':  game_over,
            'champion':   still_alive[0] if game_over and still_alive else None,
        }
        return result

    def _advance_turn(self):
        """Pindahkan giliran ke pemain berikutnya yang masih hidup."""
        n = len(self.player_ids)
        for _ in range(n):
            self.current_turn_index = (self.current_turn_index + 1) % n
            if self.lives[self.current_player] > 0:
                break


# =============================================================================
# Helpers
# =============================================================================

def _generate_session_id() -> str:
    """Generate session ID unik berbasis timestamp + random."""
    import time
    ts   = int(time.time() * 1000) % 100000
    rand = random.randint(1000, 9999)
    return f"SES-{ts}-{rand}"