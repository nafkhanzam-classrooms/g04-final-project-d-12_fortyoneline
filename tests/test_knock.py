# =============================================================================
# tests/test_knock.py
# Unit test untuk alur KNOCK dan resolusi ronde di game_engine.py
# Jalankan: python -m pytest tests/test_knock.py -v
#        atau: python tests/test_knock.py
# =============================================================================

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import unittest
from game_engine import GameEngine, Card


# Helper: paksa kartu tertentu ke tangan pemain
def set_hand(engine: GameEngine, player_id: str, cards: list[Card]):
    engine.hands[player_id] = cards


# Helper: selesaikan setup awal (start_round + initial_discard) dengan cepat
def setup_round(engine: GameEngine, discard_index: int = 0):
    engine.start_round()
    engine.initial_discard(engine.player_ids[0], discard_index)


class TestKnockResolution(unittest.TestCase):

    def setUp(self):
        """Buat engine 4 pemain: Farrel (dealer), Ali, Uwais, Achmad."""
        self.players = ['farrel', 'ali', 'uwais', 'achmad']
        self.engine  = GameEngine(self.players)
        setup_round(self.engine)
        # Setelah setUp: giliran = ali (index 1)

    def _set_giliran(self, player_id: str):
        """Paksa giliran ke pemain tertentu."""
        self.engine.current_turn_index = self.engine.player_ids.index(player_id)

    # ── Skenario normal KNOCK ─────────────────────────────────────────────────

    def test_uwais_knock_menang_sendiri(self):
        """Uwais punya skor tertinggi, knock, yang lain kehilangan nyawa."""
        set_hand(self.engine, 'farrel', [Card('hearts',   '2'), Card('spades',  '3'), Card('diamonds', '4')])
        set_hand(self.engine, 'ali',    [Card('clubs',    '2'), Card('spades',  '4'), Card('diamonds', '3')])
        set_hand(self.engine, 'uwais',  [Card('hearts',   'A'), Card('hearts',  'K'), Card('hearts',   '9'), Card('hearts', '5')])
        set_hand(self.engine, 'achmad', [Card('spades',   '3'), Card('diamonds','4'), Card('clubs',    '5'), Card('hearts', '2')])

        self._set_giliran('uwais')
        result = self.engine.knock('uwais')

        self.assertTrue(result['success'])
        self.assertEqual(result['knocker'], 'uwais')
        self.assertIn('uwais',  result['winners'])
        self.assertIn('farrel', result['losers'])
        self.assertIn('ali',    result['losers'])
        self.assertIn('achmad', result['losers'])

        # Nyawa uwais tidak berubah, yang lain berkurang 1
        self.assertEqual(self.engine.lives['uwais'],  3)
        self.assertEqual(self.engine.lives['farrel'], 2)
        self.assertEqual(self.engine.lives['ali'],    2)
        self.assertEqual(self.engine.lives['achmad'], 2)

    def test_farrel_dan_ali_seri_achmad_kalah(self):
        """Farrel dan Ali skor sama (seri), Uwais dan Achmad skor lebih rendah → 2 pemenang."""
        # Buat farrel dan ali punya skor sama: hearts A+7 = 18
        set_hand(self.engine, 'farrel', [Card('hearts', 'A'), Card('hearts', '7')])
        set_hand(self.engine, 'ali',    [Card('hearts', 'A'), Card('hearts', '7')])
        set_hand(self.engine, 'uwais',  [Card('spades', '5'), Card('clubs',  '3'), Card('hearts', '2')])
        set_hand(self.engine, 'achmad', [Card('spades', '4'), Card('diamonds','4'), Card('clubs', '3')])

        self._set_giliran('ali')
        result = self.engine.knock('ali')

        self.assertIn('farrel', result['winners'])
        self.assertIn('ali',    result['winners'])
        self.assertIn('uwais',  result['losers'])
        self.assertIn('achmad', result['losers'])

        # Pemenang tidak kehilangan nyawa
        self.assertEqual(self.engine.lives['farrel'], 3)
        self.assertEqual(self.engine.lives['ali'],    3)
        self.assertEqual(self.engine.lives['uwais'],  2)
        self.assertEqual(self.engine.lives['achmad'], 2)

    def test_knock_bukan_giliran_ditolak(self):
        """Achmad mencoba knock tapi bukan gilirannya (giliran ali) → ditolak."""
        self._set_giliran('ali')
        result = self.engine.knock('achmad')
        self.assertFalse(result['success'])
        self.assertIn('error', result)

    def test_knock_ronde_tidak_aktif_ditolak(self):
        """Tidak bisa knock jika ronde belum aktif."""
        self.engine.round_active = False
        result = self.engine.knock('ali')
        self.assertFalse(result['success'])

    # ── Skenario eliminasi ────────────────────────────────────────────────────

    def test_farrel_dieliminasi_setelah_nyawa_habis(self):
        """Farrel tinggal 1 nyawa, kalah di ronde ini → dieliminasi."""
        self.engine.lives['farrel'] = 1
        set_hand(self.engine, 'farrel', [Card('spades', '2'), Card('clubs',  '3'), Card('diamonds', '4'), Card('hearts', '2')])
        set_hand(self.engine, 'ali',    [Card('hearts', 'A'), Card('hearts', 'K'), Card('hearts',   '9'), Card('hearts', '5')])
        set_hand(self.engine, 'uwais',  [Card('spades', '2'), Card('clubs',  '3'), Card('diamonds', '4'), Card('hearts', '3')])
        set_hand(self.engine, 'achmad', [Card('spades', '3'), Card('clubs',  '4'), Card('diamonds', '5'), Card('hearts', '2')])

        self._set_giliran('ali')
        result = self.engine.knock('ali')

        self.assertIn('farrel', result['eliminated'])
        self.assertEqual(self.engine.lives['farrel'], 0)

    def test_game_over_saat_satu_pemain_tersisa(self):
        """Setelah ronde, jika hanya 1 pemain yang masih hidup → game_over=True."""
        # Buat 3 pemain tinggal 1 nyawa
        self.engine.lives['ali']    = 1
        self.engine.lives['uwais']  = 1
        self.engine.lives['achmad'] = 1

        set_hand(self.engine, 'farrel', [Card('hearts', 'A'), Card('hearts', 'K'), Card('hearts', '9'), Card('hearts', '5')])
        set_hand(self.engine, 'ali',    [Card('spades', '2'), Card('clubs',  '3'), Card('diamonds', '2'), Card('hearts', '2')])
        set_hand(self.engine, 'uwais',  [Card('spades', '3'), Card('clubs',  '2'), Card('diamonds', '3'), Card('hearts', '2')])
        set_hand(self.engine, 'achmad', [Card('spades', '4'), Card('clubs',  '4'), Card('diamonds', '2'), Card('hearts', '2')])

        self._set_giliran('farrel')
        result = self.engine.knock('farrel')

        self.assertTrue(result['game_over'])
        self.assertEqual(result['champion'], 'farrel')

    def test_champion_adalah_none_jika_game_belum_selesai(self):
        """Jika game belum selesai, champion harus None."""
        self._set_giliran('ali')
        result = self.engine.knock('ali')
        # Semua pemain masih punya nyawa (3 pemain kalah dari 4)
        # → game belum tentu selesai
        if not result['game_over']:
            self.assertIsNone(result['champion'])

    # ── Force showdown (deck habis) ───────────────────────────────────────────

    def test_force_showdown_deck_kosong(self):
        """Jika deck habis, force_showdown bekerja dan knocker=None."""
        self.engine.deck.cards = []  # paksa deck kosong
        result = self.engine.force_showdown()

        self.assertTrue(result['success'])
        self.assertIsNone(result['knocker'])
        self.assertIn('scores', result)
        self.assertIn('winners', result)

    def test_force_showdown_tidak_aktif_ditolak(self):
        self.engine.round_active = False
        result = self.engine.force_showdown()
        self.assertFalse(result['success'])

    # ── Info di result ────────────────────────────────────────────────────────

    def test_result_mengandung_semua_key_penting(self):
        """Pastikan result knock punya semua key yang dibutuhkan Person B & C."""
        self._set_giliran('ali')
        result = self.engine.knock('ali')
        for key in ['success', 'knocker', 'scores', 'hands', 'winners', 'losers', 'lives', 'eliminated', 'game_over', 'champion']:
            with self.subTest(key=key):
                self.assertIn(key, result)

    def test_hands_dalam_result_berisi_semua_pemain(self):
        """Saat ronde berakhir, kartu semua pemain harus dibuka di result."""
        self._set_giliran('ali')
        result = self.engine.knock('ali')
        for pid in self.players:
            self.assertIn(pid, result['hands'])


class TestTurnFlow(unittest.TestCase):
    """Test alur giliran dan validasi aksi per giliran."""

    def setUp(self):
        self.engine = GameEngine(['farrel', 'ali', 'uwais', 'achmad'])
        setup_round(self.engine)
        # Giliran = ali (index 1)

    def test_giliran_awal_adalah_ali(self):
        """Setelah initial_discard, giliran pertama adalah pemain kedua (ali)."""
        self.assertEqual(self.engine.current_player, 'ali')

    def test_take_dari_deck_berhasil(self):
        result = self.engine.take_card('ali', 'DECK')
        self.assertTrue(result['success'])
        self.assertIn('card', result)

    def test_take_dari_discard_berhasil(self):
        """Discard pile sudah ada 1 kartu (initial discard farrel)."""
        result = self.engine.take_card('ali', 'DISCARD')
        self.assertTrue(result['success'])

    def test_take_sumber_tidak_valid(self):
        result = self.engine.take_card('ali', 'LEMARI')
        self.assertFalse(result['success'])

    def test_take_bukan_giliran_ditolak(self):
        result = self.engine.take_card('uwais', 'DECK')  # seharusnya giliran ali
        self.assertFalse(result['success'])

    def test_discard_setelah_take_berhasil(self):
        self.engine.take_card('ali', 'DECK')
        hand_size_before = len(self.engine.hands['ali'])
        result = self.engine.discard_card('ali', 0)
        self.assertTrue(result['success'])
        self.assertEqual(len(self.engine.hands['ali']), hand_size_before - 1)

    def test_giliran_pindah_setelah_discard(self):
        self.engine.take_card('ali', 'DECK')
        self.engine.discard_card('ali', 0)
        self.assertEqual(self.engine.current_player, 'uwais')

    def test_giliran_skip_pemain_eliminasi(self):
        """Jika uwais dieliminasi, giliran dari ali langsung ke achmad."""
        self.engine.lives['uwais'] = 0
        self.engine.take_card('ali', 'DECK')
        self.engine.discard_card('ali', 0)
        self.assertEqual(self.engine.current_player, 'achmad')


class TestGetState(unittest.TestCase):
    """Test bahwa get_state_for_player menyembunyikan kartu lawan."""

    def setUp(self):
        self.engine = GameEngine(['farrel', 'ali', 'uwais', 'achmad'])
        setup_round(self.engine)

    def test_kartu_sendiri_terlihat(self):
        state = self.engine.get_state_for_player('ali')
        self.assertIn('your_hand', state)
        self.assertIsInstance(state['your_hand'], list)

    def test_kartu_lawan_tidak_terlihat(self):
        state = self.engine.get_state_for_player('ali')
        # Lawan hanya tampil sebagai card_count, bukan list kartu
        for pid, info in state['opponents'].items():
            self.assertIn('card_count', info)
            self.assertNotIn('hand', info)

    def test_semua_key_ada(self):
        state = self.engine.get_state_for_player('farrel')
        for key in ['round', 'current_player', 'your_hand', 'top_discard', 'deck_remaining', 'lives', 'opponents']:
            self.assertIn(key, state)

    def test_player_tidak_dikenal(self):
        state = self.engine.get_state_for_player('budi')
        self.assertIn('error', state)


if __name__ == '__main__':
    unittest.main(verbosity=2)