# =============================================================================
# protocol.py
# Encode / decode pesan JSON antar server dan client via TCP.
#
# CARA PAKAI (Person B — server):
#   from protocol import encode_game_state, encode_action_request, decode
#   data = encode_game_state(engine.get_state_for_player('ali'))
#   conn.sendall(data)
#   msg  = decode(raw_line)   # raw_line = satu baris dari socket (diakhiri \n)
#
# CARA PAKAI (Person C — client):
#   from protocol import decode, encode_action
#   msg = decode(raw_line)
#   if msg['type'] == MSG_ACTION_REQ:
#       ...tampilkan pilihan aksi ke pemain...
#   data = encode_action('TAKE', {'source': 'DECK'})
#   sock.sendall(data)
# =============================================================================

import json
from shared.constants import (
    MSG_GAME_STATE, MSG_ACTION_REQ, MSG_ACTION_RESP,
    MSG_ROUND_END, MSG_GAME_OVER, MSG_ERROR,
    MSG_PLAYER_JOINED, MSG_GAME_START,
)


# =============================================================================
# Primitif encode / decode
# =============================================================================

def encode(msg_type: str, payload: dict) -> bytes:
    """
    Bungkus payload ke dalam envelope {'type': ..., 'payload': ...},
    serialize ke JSON, tambahkan newline, encode ke bytes.

    Server dan client membaca socket line-by-line (split '\n'),
    lalu decode setiap baris dengan fungsi decode() di bawah.
    """
    message = json.dumps({'type': msg_type, 'payload': payload}, ensure_ascii=False)
    return (message + '\n').encode('utf-8')


def decode(raw: str) -> dict:
    """
    Terima satu baris string dari socket, parse JSON.

    Return: {'type': str, 'payload': dict}
    Raise : json.JSONDecodeError jika format salah.
    """
    return json.loads(raw.strip())


# =============================================================================
# Encode dari SERVER → CLIENT
# (dipanggil di server.py / session.py)
# =============================================================================

def encode_game_state(state: dict) -> bytes:
    """
    Kirim snapshot state permainan ke satu client.
    Gunakan engine.get_state_for_player(player_id) sebagai argumen state,
    BUKAN get_full_state() — kartu lawan harus tersembunyi.

    Contoh:
        data = encode_game_state(engine.get_state_for_player('ali'))
        conn.sendall(data)
    """
    return encode(MSG_GAME_STATE, state)


def encode_action_request(player_id: str, valid_actions: list) -> bytes:
    """
    Minta pemain melakukan aksi (dikirim ke semua client, bukan hanya yang giliran,
    supaya client lain tahu sedang menunggu siapa).

    Args:
        player_id     : siapa yang harus bertindak
        valid_actions : aksi yang boleh dipilih, misal ['TAKE', 'KNOCK']
                        atau ['DISCARD'] (setelah take_card)

    Contoh:
        data = encode_action_request('farrel', ['TAKE', 'KNOCK'])
        broadcast(data)
    """
    return encode(MSG_ACTION_REQ, {
        'player_id':     player_id,
        'valid_actions': valid_actions,
    })


def encode_action_response(success: bool, data: dict) -> bytes:
    """
    Konfirmasi hasil aksi ke client yang melakukan aksi.
    data bisa berisi kartu yang diambil, kartu yang dibuang, dll.

    Contoh sukses  : encode_action_response(True,  {'card': {...}})
    Contoh gagal   : encode_action_response(False, {'error': 'Bukan giliran kamu'})
    """
    return encode(MSG_ACTION_RESP, {'success': success, **data})


def encode_round_end(result: dict) -> bytes:
    """
    Umumkan hasil ronde ke semua client.
    result adalah return value dari engine.knock() atau engine.force_showdown().

    Contoh:
        result = engine.knock('uwais')
        broadcast(encode_round_end(result))
    """
    return encode(MSG_ROUND_END, result)


def encode_game_over(champion: str) -> bytes:
    """
    Umumkan pemenang akhir game ke semua client.

    Contoh:
        broadcast(encode_game_over('farrel'))
    """
    return encode(MSG_GAME_OVER, {'champion': champion})


def encode_error(message: str) -> bytes:
    """
    Kirim pesan error ke client.

    Contoh:
        conn.sendall(encode_error('Aksi tidak valid'))
    """
    return encode(MSG_ERROR, {'message': message})


def encode_player_joined(player_id: str, total_in_lobby: int, needed: int) -> bytes:
    """
    Notifikasi ke semua client bahwa ada pemain baru masuk lobby.

    Contoh:
        broadcast(encode_player_joined('achmad', total_in_lobby=3, needed=4))
    """
    return encode(MSG_PLAYER_JOINED, {
        'player_id':       player_id,
        'total_in_lobby':  total_in_lobby,
        'needed':          needed,
    })


def encode_game_start(player_ids: list, first_player: str) -> bytes:
    """
    Beritahu semua client bahwa game akan segera dimulai.

    Contoh:
        broadcast(encode_game_start(['farrel','ali','uwais','achmad'], 'farrel'))
    """
    return encode(MSG_GAME_START, {
        'player_ids':   player_ids,
        'first_player': first_player,
    })


# =============================================================================
# Encode dari CLIENT → SERVER
# (dipanggil di client.py / input_handler.py)
# =============================================================================

def encode_action(action: str, params: dict = None) -> bytes:
    """
    Kirim aksi pemain ke server.

    Args:
        action : 'TAKE' | 'DISCARD' | 'KNOCK'
        params : dict parameter tambahan tergantung aksi

    Contoh TAKE dari deck  : encode_action('TAKE',    {'source': 'DECK'})
    Contoh TAKE dari pile  : encode_action('TAKE',    {'source': 'DISCARD'})
    Contoh DISCARD         : encode_action('DISCARD', {'card_index': 2})
    Contoh KNOCK           : encode_action('KNOCK')
    """
    payload = {'action': action}
    if params:
        payload.update(params)
    return encode('ACTION', payload)


# =============================================================================
# Helper baca socket (untuk server.py dan client.py)
# =============================================================================

def recv_message(sock) -> dict | None:
    """
    Baca satu pesan lengkap dari socket (sampai menemukan newline).
    Mengembalikan dict hasil decode, atau None jika koneksi terputus.

    Contoh pakai di server.py:
        msg = recv_message(conn)
        if msg is None:
            # client disconnect
            break
        action = msg['payload']['action']

    Contoh pakai di client.py:
        msg = recv_message(sock)
        if msg['type'] == MSG_ACTION_REQ:
            ...
    """
    buffer = b''
    try:
        while b'\n' not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                return None  # koneksi ditutup
            buffer += chunk
        line, _ = buffer.split(b'\n', 1)
        return decode(line.decode('utf-8'))
    except (OSError, json.JSONDecodeError):
        return None