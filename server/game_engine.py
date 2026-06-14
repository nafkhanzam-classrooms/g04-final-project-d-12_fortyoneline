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

class Card:
    def __init__(self, suit: str, rank: str):
        self.suit = suit
        self.rank = rank

    @property
    def value(self) -> int:
        return CARD_VALUES[self.rank]

    def to_dict(self) -> dict:
        return {'suit': self.suit, 'rank': self.rank}

    @classmethod
    def from_dict(cls, d: dict) -> 'Card':
        return cls(d['suit'], d['rank'])

    def __repr__(self):
        from shared.constants import SUIT_SYMBOLS
        sym = SUIT_SYMBOLS.get(self.suit, self.suit[0].upper())
        return f"{self.rank}{sym}"

    def __eq__(self, other):
        return isinstance(other, Card) and self.suit == other.suit and self.rank == other.rank

class Deck:
    def __init__(self):
        self.cards: list[Card] = [
            Card(suit, rank) for suit in SUITS for rank in RANKS
        ]
        random.shuffle(self.cards)

    def draw(self) -> Card | None:
        return self.cards.pop() if self.cards else None

    def is_empty(self) -> bool:
        return len(self.cards) == 0

    def remaining(self) -> int:
        return len(self.cards)

def calculate_score(hand: list[Card]) -> int:
    if not hand:
        return 0

    suit_totals: dict[str, int] = {}
    for card in hand:
        suit_totals[card.suit] = suit_totals.get(card.suit, 0) + card.value

    majority_suit  = max(suit_totals, key=lambda s: suit_totals[s])
    majority_total = suit_totals[majority_suit]
    minority_total = sum(v for s, v in suit_totals.items() if s != majority_suit)

    return majority_total - minority_total

def validate_action_packet(msg: dict) -> tuple[bool, str]:
    if not isinstance(msg, dict):
        return False, 'Paket harus berupa JSON object'

    if 'type' not in msg:
        return False, "Field 'type' tidak ada"

    if 'payload' not in msg:
        return False, "Field 'payload' tidak ada"

    if not isinstance(msg['payload'], dict):
        return False, "Field 'payload' harus berupa JSON object"

    if msg['type'] != 'ACTION':
        return True, ''

    payload = msg['payload']

    if 'action' not in payload:
        return False, "Field 'action' tidak ada di payload"

    action = payload['action']
    valid_actions = {'TAKE', 'DISCARD', 'KNOCK', 'INITIAL_DISCARD'}
    if action not in valid_actions:
        return False, f"Aksi tidak dikenal: {action!r}. Pilihan valid: {sorted(valid_actions)}"

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

class ActivityLog:
    def __init__(self):
        self._entries: list[dict] = []

    def record(self, round_num: int, player: str, event: str, detail: dict = None):
        self._entries.append({
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',
            'round':     round_num,
            'player':    player,
            'event':     event,
            'detail':    detail or {},
        })

    def get_all(self) -> list[dict]:
        return list(self._entries)

    def get_round(self, round_num: int) -> list[dict]:
        return [e for e in self._entries if e['round'] == round_num]

    def get_player(self, player_id: str) -> list[dict]:
        return [e for e in self._entries if e['player'] == player_id]

    def clear(self):
        self._entries.clear()

class GameEngine:
    def __init__(self, player_ids: list, session_id: str = None):
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

        self.session_id: str = session_id or _generate_session_id()

        self.log: ActivityLog = ActivityLog()

        self._waiting_initial_discard: bool = False

    @property
    def current_player(self):
        return self.player_ids[self.current_turn_index]

    @property
    def active_players(self) -> list:
        return [pid for pid in self.player_ids if self.lives[pid] > 0]

    def start_round(self) -> dict:
        self.deck         = Deck()
        self.discard_pile = []
        self.hands        = {pid: [] for pid in self.player_ids}
        self.scores       = {}
        self.round_number += 1

        first = self.player_ids[0]
        for _ in range(FIRST_PLAYER_CARDS):
            card = self.deck.draw()
            if card:
                self.hands[first].append(card)

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

        self.log.record(self.round_number, player_id, LOG_EVENT_DISCARD, {
            'phase':    'initial',
            'card':     card.to_dict(),
        })

        return {
            'success':     True,
            'discarded':   card.to_dict(),
            'next_player': self.current_player,
        }

    def take_card(self, player_id, source: str) -> dict:
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
        if not self.round_active:
            return {'success': False, 'error': 'Ronde belum aktif'}
        if player_id != self.current_player:
            return {'success': False, 'error': f'Bukan giliran {player_id}, sekarang giliran {self.current_player}'}

        self.log.record(self.round_number, player_id, LOG_EVENT_KNOCK, {})

        return self._resolve_round(knocker=player_id)

    def force_showdown(self, knocker=None) -> dict:
        if not self.round_active:
            return {'success': False, 'error': 'Ronde belum aktif'}
        return self._resolve_round(knocker=knocker)

    def get_state_for_player(self, viewer_id) -> dict:
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
        if player_id not in self.player_ids:
            return {'error': f'Player {player_id!r} tidak ditemukan dalam sesi ini'}

        state = self.get_state_for_player(player_id)
        state['reconnected']   = True
        state['player_order']  = self.player_ids  # client perlu tahu urutan duduk
        return state

    def get_activity_log(self) -> list[dict]:
        return self.log.get_all()

    def get_round_log(self, round_num: int = None) -> list[dict]:
        return self.log.get_round(round_num or self.round_number)

    def _resolve_round(self, knocker) -> dict:
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
        n = len(self.player_ids)
        for _ in range(n):
            self.current_turn_index = (self.current_turn_index + 1) % n
            if self.lives[self.current_player] > 0:
                break

def _generate_session_id() -> str:
    import time
    ts   = int(time.time() * 1000) % 100000
    rand = random.randint(1000, 9999)
    return f"SES-{ts}-{rand}"