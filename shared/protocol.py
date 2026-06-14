import json
from .constants import (
    MSG_GAME_STATE, MSG_ACTION_REQ, MSG_ACTION_RESP,
    MSG_ROUND_END, MSG_GAME_OVER, MSG_ERROR,
    MSG_PLAYER_JOINED, MSG_GAME_START,
)

def encode(msg_type: str, payload: dict) -> bytes:
    message = json.dumps({'type': msg_type, 'payload': payload}, ensure_ascii=False)
    return (message + '\n').encode('utf-8')


def decode(raw: str) -> dict:
    return json.loads(raw.strip())

def encode_game_state(state: dict) -> bytes:
    return encode(MSG_GAME_STATE, state)


def encode_action_request(player_id: str, valid_actions: list) -> bytes:
    return encode(MSG_ACTION_REQ, {
        'player_id':     player_id,
        'valid_actions': valid_actions,
    })

def encode_action_response(success: bool, data: dict) -> bytes:
    return encode(MSG_ACTION_RESP, {'success': success, **data})

def encode_round_end(result: dict) -> bytes:
    return encode(MSG_ROUND_END, result)

def encode_game_over(champion: str) -> bytes:
    return encode(MSG_GAME_OVER, {'champion': champion})

def encode_error(message: str) -> bytes:
    return encode(MSG_ERROR, {'message': message})

def encode_player_joined(player_id: str, total_in_lobby: int, needed: int) -> bytes:
    return encode(MSG_PLAYER_JOINED, {
        'player_id':       player_id,
        'total_in_lobby':  total_in_lobby,
        'needed':          needed,
    })

def encode_game_start(player_ids: list, first_player: str) -> bytes:
    return encode(MSG_GAME_START, {
        'player_ids':   player_ids,
        'first_player': first_player,
    })

def encode_action(action: str, params: dict = None) -> bytes:
    payload = {'action': action}
    if params:
        payload.update(params)
    return encode('ACTION', payload)

def recv_message(sock) -> dict | None:
    buffer = b''
    try:
        while b'\n' not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buffer += chunk
        line, _ = buffer.split(b'\n', 1)
        return decode(line.decode('utf-8'))
    except (OSError, json.JSONDecodeError):
        return None