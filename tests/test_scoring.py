import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import unittest
from server.game_engine import Card, calculate_score

class TestCalculateScore(unittest.TestCase):
    def test_contoh_prd_skor_rendah(self):
        hand = [
            Card('hearts',   '10'),
            Card('hearts',   '7'),
            Card('spades',   '5'),
            Card('clubs',    '3'),
        ]
        self.assertEqual(calculate_score(hand), 9)

    def test_contoh_prd_skor_tinggi(self):
        hand = [
            Card('hearts',   'A'),
            Card('hearts',   '10'),
            Card('hearts',   '7'),
            Card('spades',   '2'),
        ]
        self.assertEqual(calculate_score(hand), 26)

    def test_farrel_semua_satu_suit(self):
        hand = [
            Card('hearts', 'A'),
            Card('hearts', 'K'),
            Card('hearts', '9'),
            Card('hearts', '3'),
        ]
        self.assertEqual(calculate_score(hand), 33)

    def test_ali_mayoritas_tipis(self):
        hand = [
            Card('spades',  'A'),
            Card('spades',  '2'),
            Card('hearts',  'K'),
            Card('hearts',  '2'),
        ]
        self.assertEqual(calculate_score(hand), 1)

    def test_uwais_tiga_suit_berbeda(self):
        hand = [
            Card('diamonds', 'J'),
            Card('diamonds', '8'),
            Card('clubs',    '5'),
            Card('hearts',   '3'),
        ]
        self.assertEqual(calculate_score(hand), 10)

    def test_achmad_semua_berbeda_suit(self):
        hand = [
            Card('hearts',   'A'),
            Card('spades',   '5'),
            Card('diamonds', '3'),
            Card('clubs',    '2'),
        ]
        self.assertEqual(calculate_score(hand), 1)

    def test_skor_negatif_tidak_mungkin_dengan_satu_suit_dominan(self):
        hand = [
            Card('hearts',   'A'),
            Card('spades',   '9'),
            Card('diamonds', '9'),
            Card('clubs',    '9'),
        ]
        self.assertEqual(calculate_score(hand), -16)

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
        hand_a = [Card('hearts', 'A'), Card('hearts', '7'), Card('spades', '5'), Card('clubs', '3')]
        hand_b = [Card('clubs', '3'),  Card('spades', '5'), Card('hearts', '7'), Card('hearts', 'A')]
        self.assertEqual(calculate_score(hand_a), calculate_score(hand_b))

if __name__ == '__main__':
    unittest.main(verbosity=2)