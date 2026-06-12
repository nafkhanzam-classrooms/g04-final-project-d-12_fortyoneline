# =============================================================================
# tests/test_scoring.py
# Unit test untuk fungsi calculate_score di game_engine.py
# Jalankan: python -m pytest tests/test_scoring.py -v
#        atau: python tests/test_scoring.py
# =============================================================================

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import unittest
from game_engine import Card, calculate_score


class TestCalculateScore(unittest.TestCase):

    # ── Contoh langsung dari PRD ──────────────────────────────────────────────

    def test_contoh_prd_skor_rendah(self):
        """PRD halaman 1: ♥10 + ♥7 + ♠5 + ♣3 → mayoritas hearts=17, pengurang=8 → 9"""
        hand = [
            Card('hearts',   '10'),
            Card('hearts',   '7'),
            Card('spades',   '5'),
            Card('clubs',    '3'),
        ]
        self.assertEqual(calculate_score(hand), 9)

    def test_contoh_prd_skor_tinggi(self):
        """PRD halaman 2: ♥A + ♥10 + ♥7 + ♠2 → hearts=28, pengurang=2 → 26"""
        hand = [
            Card('hearts',   'A'),
            Card('hearts',   '10'),
            Card('hearts',   '7'),
            Card('spades',   '2'),
        ]
        self.assertEqual(calculate_score(hand), 26)

    # ── Skenario tangan pemain (pakai nama Farrel, Ali, Uwais, Achmad) ────────

    def test_farrel_semua_satu_suit(self):
        """Farrel punya 4 kartu hati semua → tidak ada pengurang → skor maksimal ronde ini"""
        hand = [
            Card('hearts', 'A'),   # 11
            Card('hearts', 'K'),   # 10
            Card('hearts', '9'),   # 9
            Card('hearts', '3'),   # 3
        ]
        # Total = 11+10+9+3 = 33, pengurang = 0
        self.assertEqual(calculate_score(hand), 33)

    def test_ali_mayoritas_tipis(self):
        """Ali: 2 kartu spades vs 2 kartu hearts, spades unggul tipis"""
        hand = [
            Card('spades',  'A'),  # 11
            Card('spades',  '2'),  # 2  → spades total = 13
            Card('hearts',  'K'),  # 10
            Card('hearts',  '2'),  # 2  → hearts total = 12
        ]
        # mayoritas spades=13, pengurang hearts=12 → 13-12 = 1
        self.assertEqual(calculate_score(hand), 1)

    def test_uwais_tiga_suit_berbeda(self):
        """Uwais: 3 suit berbeda, hanya 1 kartu per suit minor"""
        hand = [
            Card('diamonds', 'J'),  # 10
            Card('diamonds', '8'),  # 8  → diamonds = 18
            Card('clubs',    '5'),  # 5
            Card('hearts',   '3'),  # 3
        ]
        # mayoritas diamonds=18, pengurang=5+3=8 → 18-8 = 10
        self.assertEqual(calculate_score(hand), 10)

    def test_achmad_semua_berbeda_suit(self):
        """Achmad: tiap kartu beda suit → mayoritas = nilai tertinggi, sisa jadi pengurang"""
        hand = [
            Card('hearts',   'A'),  # 11
            Card('spades',   '5'),  # 5
            Card('diamonds', '3'),  # 3
            Card('clubs',    '2'),  # 2
        ]
        # mayoritas hearts=11, pengurang=5+3+2=10 → 11-10 = 1
        self.assertEqual(calculate_score(hand), 1)

    def test_skor_negatif_tidak_mungkin_dengan_satu_suit_dominan(self):
        """
        Skor bisa negatif jika kartu minoritas totalnya melebihi mayoritas.
        Contoh ekstrem: 1 as + 3 kartu kecil beda suit.
        """
        hand = [
            Card('hearts',   'A'),  # 11 → hearts = 11
            Card('spades',   '9'),  # 9
            Card('diamonds', '9'),  # 9
            Card('clubs',    '9'),  # 9  → pengurang = 27
        ]
        # 11 - 27 = -16 (valid sesuai aturan)
        self.assertEqual(calculate_score(hand), -16)

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_tangan_kosong(self):
        self.assertEqual(calculate_score([]), 0)

    def test_satu_kartu_saja(self):
        hand = [Card('spades', 'K')]
        self.assertEqual(calculate_score(hand), 10)

    def test_nilai_as_adalah_11(self):
        hand = [Card('clubs', 'A')]
        self.assertEqual(calculate_score(hand), 11)

    def test_nilai_j_q_k_10_adalah_10(self):
        for rank in ['J', 'Q', 'K', '10']:
            with self.subTest(rank=rank):
                hand = [Card('hearts', rank)]
                self.assertEqual(calculate_score(hand), 10)

    def test_nilai_angka_sesuai_rank(self):
        for rank in ['2', '3', '4', '5', '6', '7', '8', '9']:
            with self.subTest(rank=rank):
                hand = [Card('spades', rank)]
                self.assertEqual(calculate_score(hand), int(rank))

    def test_skor_tidak_terpengaruh_urutan_kartu(self):
        """Urutan kartu di tangan tidak boleh mempengaruhi skor."""
        hand_a = [Card('hearts', 'A'), Card('hearts', '7'), Card('spades', '5'), Card('clubs', '3')]
        hand_b = [Card('clubs', '3'),  Card('spades', '5'), Card('hearts', '7'), Card('hearts', 'A')]
        self.assertEqual(calculate_score(hand_a), calculate_score(hand_b))


if __name__ == '__main__':
    unittest.main(verbosity=2)