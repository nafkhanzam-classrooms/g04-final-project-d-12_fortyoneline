# =============================================================================
# tests/test_client_protocol.py
# Unit test untuk framing (LineFramer) dan dispatcher client — murni,
# tanpa server dan tanpa pygame.
# Jalankan: python -m pytest tests/test_client_protocol.py -v
#        atau: python tests/test_client_protocol.py
# =============================================================================

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
import tempfile
import unittest

import client
from client import (
    ClientState, LineFramer, dispatch,
    PHASE_CONNECT, PHASE_LOBBY, PHASE_PLAYING, PHASE_MUST_DISCARD,
    PHASE_ROUND_END, PHASE_WAITING_READY, PHASE_GAME_OVER,
)

NOW = 1_000_000.0


def msg(mtype, payload=None):
    return {"type": mtype, "payload": payload if payload is not None else {}}


class TestLineFramer(unittest.TestCase):

    def test_satu_pesan_utuh(self):
        f = LineFramer()
        out = f.feed(b'{"type": "PONG", "payload": {}}\n')
        self.assertEqual(out, [{"type": "PONG", "payload": {}}])

    def test_partial_recv_disambung(self):
        """Baris terpotong di tengah chunk harus menunggu sisa byte-nya."""
        f = LineFramer()
        self.assertEqual(f.feed(b'{"type": "GAME_'), [])
        self.assertEqual(f.feed(b'START", "payload": {}}'), [])
        out = f.feed(b'\n')
        self.assertEqual(out, [{"type": "GAME_START", "payload": {}}])

    def test_beberapa_pesan_dalam_satu_chunk(self):
        f = LineFramer()
        raw = (b'{"type": "A", "payload": {}}\n'
               b'{"type": "B", "payload": {}}\n'
               b'{"type": "C", "payload": {}}\n')
        out = f.feed(raw)
        self.assertEqual([m["type"] for m in out], ["A", "B", "C"])

    def test_payload_unicode(self):
        f = LineFramer()
        raw = json.dumps({"type": "CHAT_BROADCAST",
                          "payload": {"text": "héllo ♠♥ дружба"}},
                         ensure_ascii=False).encode("utf-8") + b"\n"
        # potong di tengah byte multibyte
        out = f.feed(raw[:15])
        self.assertEqual(out, [])
        out = f.feed(raw[15:])
        self.assertEqual(out[0]["payload"]["text"], "héllo ♠♥ дружба")

    def test_baris_rusak_diskip_tanpa_crash(self):
        f = LineFramer()
        out = f.feed(b'ini bukan json\n{"type": "PONG", "payload": {}}\n')
        self.assertEqual(out, [{"type": "PONG", "payload": {}}])

    def test_baris_kosong_diabaikan(self):
        f = LineFramer()
        out = f.feed(b'\n\n{"type": "PONG", "payload": {}}\n\n')
        self.assertEqual(len(out), 1)

    def test_json_bukan_dict_diskip(self):
        f = LineFramer()
        out = f.feed(b'[1, 2, 3]\n"string"\n')
        self.assertEqual(out, [])


class DispatcherTestBase(unittest.TestCase):
    def setUp(self):
        self.state = ClientState()
        # save_session (dipanggil LOGIN_ACK) jangan menulis ke $HOME asli
        self._tmp = tempfile.NamedTemporaryFile(delete=False)
        self._orig_session_file = client.SESSION_FILE
        client.SESSION_FILE = self._tmp.name

    def tearDown(self):
        client.SESSION_FILE = self._orig_session_file
        os.unlink(self._tmp.name)


class TestDispatcherLobby(DispatcherTestBase):

    def test_login_ack(self):
        dispatch(self.state, msg("LOGIN_ACK", {
            "player_id": "P001", "room_code": "ABCDE", "message": "ok"}), NOW)
        self.assertEqual(self.state.player_id, "P001")
        self.assertEqual(self.state.room_code, "ABCDE")
        self.assertEqual(self.state.phase, PHASE_LOBBY)
        # sesi tersimpan untuk reconnect
        with open(client.SESSION_FILE) as f:
            saved = json.load(f)
        self.assertEqual(saved["player_id"], "P001")

    def test_player_joined_memperbarui_daftar(self):
        players = [
            {"player_id": "P001", "username": "ali", "connected": True},
            {"player_id": "P002", "username": "budi", "connected": True},
        ]
        dispatch(self.state, msg("PLAYER_JOINED", {
            "player_id": "P002", "username": "budi",
            "players": players, "room_code": "ABCDE"}), NOW)
        self.assertEqual(self.state.players, players)
        self.assertEqual(self.state.room_code, "ABCDE")

    def test_game_start(self):
        self.state.phase = PHASE_LOBBY
        dispatch(self.state, msg("GAME_START", {"message": "mulai!"}), NOW)
        self.assertEqual(self.state.phase, PHASE_PLAYING)

    def test_error_jadi_toast(self):
        dispatch(self.state, msg("ERROR", {"message": "Room penuh"}), NOW)
        self.assertIn("Room penuh", self.state.active_toasts(NOW))


class TestDispatcherInGame(DispatcherTestBase):

    def setUp(self):
        super().setUp()
        self.state.player_id = "P001"
        self.state.phase = PHASE_PLAYING
        self.state.players = [
            {"player_id": "P001", "username": "ali", "lives": 3,
             "hand_count": 4, "connected": True},
            {"player_id": "P002", "username": "budi", "lives": 3,
             "hand_count": 4, "connected": True},
        ]

    def test_your_hand(self):
        cards = [{"suit": "hearts", "rank": "A"},
                 {"suit": "spades", "rank": "10"}]
        dispatch(self.state, msg("YOUR_HAND", {"cards": cards}), NOW)
        self.assertEqual(self.state.hand, cards)

    def test_game_state_ejaan_normal(self):
        dispatch(self.state, msg("GAME_STATE", {
            "current_turn": "P002", "deck_count": 30,
            "discard_top": {"suit": "clubs", "rank": "7"}, "round": 2,
            "players": self.state.players}), NOW)
        self.assertEqual(self.state.current_turn, "P002")
        self.assertEqual(self.state.deck_count, 30)
        self.assertEqual(self.state.discard_top["rank"], "7")
        self.assertEqual(self.state.round_number, 2)

    def test_game_state_ejaan_engine(self):
        """Blockers §7: server saat ini meneruskan key engine — client harus
        membaca current_player / deck_remaining / top_discard juga."""
        dispatch(self.state, msg("GAME_STATE", {
            "current_player": "P002", "deck_remaining": 25,
            "top_discard": {"suit": "hearts", "rank": "K"}}), NOW)
        self.assertEqual(self.state.current_turn, "P002")
        self.assertEqual(self.state.deck_count, 25)
        self.assertEqual(self.state.discard_top["rank"], "K")

    def test_game_state_none_tidak_menimpa(self):
        self.state.current_turn = "P001"
        self.state.deck_count = 10
        self.state.discard_top = {"suit": "clubs", "rank": "3"}
        dispatch(self.state, msg("GAME_STATE", {
            "current_turn": None, "deck_count": None,
            "discard_top": None, "round": None, "players": []}), NOW)
        self.assertEqual(self.state.current_turn, "P001")
        self.assertEqual(self.state.deck_count, 10)
        self.assertEqual(self.state.discard_top["rank"], "3")

    def test_turn_indicator_set_deadline(self):
        dispatch(self.state, msg("TURN_INDICATOR", {
            "current_turn": "P001", "username": "ali"}), NOW)
        self.assertEqual(self.state.current_turn, "P001")
        self.assertAlmostEqual(self.state.turn_deadline, NOW + 30)

    def test_turn_indicator_lawan_menghapus_aksi(self):
        self.state.valid_actions = ["TAKE_DECK"]
        dispatch(self.state, msg("TURN_INDICATOR", {
            "current_turn": "P002", "username": "budi"}), NOW)
        self.assertEqual(self.state.valid_actions, [])

    def test_valid_actions_take(self):
        dispatch(self.state, msg("VALID_ACTIONS", {
            "actions": ["TAKE_DECK", "TAKE_DISCARD", "KNOCK"]}), NOW)
        self.assertEqual(self.state.valid_actions,
                         ["TAKE_DECK", "TAKE_DISCARD", "KNOCK"])
        self.assertEqual(self.state.phase, PHASE_PLAYING)
        self.assertAlmostEqual(self.state.turn_deadline, NOW + 30)

    def test_valid_actions_discard_pindah_fase(self):
        dispatch(self.state, msg("VALID_ACTIONS", {"actions": ["DISCARD"]}), NOW)
        self.assertEqual(self.state.phase, PHASE_MUST_DISCARD)
        dispatch(self.state, msg("VALID_ACTIONS",
                                 {"actions": ["TAKE_DECK"]}), NOW)
        self.assertEqual(self.state.phase, PHASE_PLAYING)

    def test_card_drawn_tidak_mengubah_hand(self):
        """Server mengirim YOUR_HAND setelah CARD_DRAWN — hand jangan
        dimutasi dua kali."""
        self.state.hand = [{"suit": "clubs", "rank": "2"}]
        dispatch(self.state, msg("CARD_DRAWN", {
            "source": "deck", "card": {"suit": "hearts", "rank": "A"}}), NOW)
        self.assertEqual(len(self.state.hand), 1)

    def test_round_end(self):
        result = {"knocker": "P002", "scores": {"P001": 20, "P002": 35},
                  "winners": ["P002"], "losers": ["P001"],
                  "lives": {"P001": 2, "P002": 3}, "eliminated": [],
                  "hands": {}, "game_over": False, "champion": None}
        dispatch(self.state, msg("ROUND_END", result), NOW)
        self.assertEqual(self.state.phase, PHASE_ROUND_END)
        self.assertEqual(self.state.round_result, result)
        self.assertIsNone(self.state.turn_deadline)

    def test_next_round_prompt(self):
        self.state.phase = PHASE_ROUND_END
        dispatch(self.state, msg("NEXT_ROUND_PROMPT", {"message": "siap?"}), NOW)
        self.assertEqual(self.state.phase, PHASE_WAITING_READY)

    def test_game_over(self):
        dispatch(self.state, msg("GAME_OVER", {
            "winner_id": "P002", "winner_username": "budi",
            "message": "budi menang!"}), NOW)
        self.assertEqual(self.state.phase, PHASE_GAME_OVER)
        self.assertEqual(self.state.game_over_info["winner_username"], "budi")

    def test_player_disconnected_dan_reconnected(self):
        dispatch(self.state, msg("PLAYER_DISCONNECTED", {
            "player_id": "P002", "username": "budi",
            "message": "budi terputus"}), NOW)
        self.assertFalse(self.state.players[1]["connected"])
        dispatch(self.state, msg("PLAYER_RECONNECTED", {
            "player_id": "P002", "username": "budi"}), NOW)
        self.assertTrue(self.state.players[1]["connected"])

    def test_pong_menghitung_latensi_dari_waktu_kirim(self):
        # session.py:448 membalas timestamp milik server, bukan echo —
        # latensi dihitung dari last_ping_sent client sendiri.
        self.state.last_ping_sent = NOW - 0.05
        dispatch(self.state, msg("PONG", {"timestamp": 12345.0}), NOW)
        self.assertEqual(self.state.latency_ms, 50)

    def test_chat_broadcast(self):
        dispatch(self.state, msg("CHAT_BROADCAST", {
            "player_id": "P002", "username": "budi", "text": "halo!"}), NOW)
        self.assertEqual(self.state.chat_log[-1],
                         {"username": "budi", "text": "halo!"})


class TestDispatcherReconnect(DispatcherTestBase):

    def test_reconnect_ack_membangun_ulang_state(self):
        self.state.phase = "RECONNECTING"
        self.state.reconnect_deadline = NOW + 30
        snapshot = {
            "your_hand": [{"suit": "spades", "rank": "Q"}],
            "top_discard": {"suit": "hearts", "rank": "4"},
            "deck_remaining": 17,
            "lives": {"P001": 2, "P002": 1},
            "current_player": "P002",
            "player_order": ["P001", "P002"],
            "reconnected": True,
        }
        dispatch(self.state, msg("RECONNECT_ACK", {
            "player_id": "P001", "state": snapshot, "message": "ok"}), NOW)
        self.assertEqual(self.state.phase, PHASE_PLAYING)
        self.assertEqual(self.state.hand, snapshot["your_hand"])
        self.assertEqual(self.state.discard_top["rank"], "4")
        self.assertEqual(self.state.deck_count, 17)
        self.assertEqual(self.state.current_turn, "P002")
        self.assertIsNone(self.state.reconnect_deadline)
        # daftar pemain dibangun dari player_order + lives
        self.assertEqual([p["player_id"] for p in self.state.players],
                         ["P001", "P002"])
        self.assertEqual(self.state.players[1]["lives"], 1)

    def test_reconnect_ack_snapshot_kosong_kembali_ke_lobby(self):
        """Game belum mulai (engine None) → snapshot {} → kembali ke lobby."""
        dispatch(self.state, msg("RECONNECT_ACK", {
            "player_id": "P001", "state": {}, "message": "ok"}), NOW)
        self.assertEqual(self.state.phase, PHASE_LOBBY)


class TestDispatcherDefensive(DispatcherTestBase):

    def test_tipe_tak_dikenal_diabaikan(self):
        dispatch(self.state, msg("HALUSINASI_XYZ", {"a": 1}), NOW)
        self.assertEqual(self.state.phase, PHASE_CONNECT)

    def test_payload_hilang(self):
        dispatch(self.state, {"type": "GAME_START"}, NOW)
        self.assertEqual(self.state.phase, PHASE_PLAYING)

    def test_payload_bukan_dict(self):
        dispatch(self.state, {"type": "YOUR_HAND", "payload": "rusak"}, NOW)
        self.assertEqual(self.state.hand, [])

    def test_pesan_bukan_dict(self):
        dispatch(self.state, "bukan dict", NOW)  # tidak crash

    def test_your_hand_cards_none(self):
        self.state.hand = [{"suit": "clubs", "rank": "2"}]
        dispatch(self.state, msg("YOUR_HAND", {"cards": None}), NOW)
        self.assertEqual(len(self.state.hand), 1)  # tidak tertimpa None


if __name__ == "__main__":
    unittest.main(verbosity=2)
