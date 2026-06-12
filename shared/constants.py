# =============================================================================
# shared/constants.py
# Semua konstanta permainan 41. Diimport oleh game_engine, protocol, server,
# dan client — jangan taruh logika di sini, hanya nilai tetap.
# =============================================================================

# ── Kartu ─────────────────────────────────────────────────────────────────────

SUITS = ['spades', 'hearts', 'diamonds', 'clubs']
RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']

# Nilai poin tiap rank
CARD_VALUES = {
    'A': 11,
    'J': 10, 'Q': 10, 'K': 10, '10': 10,
    '2': 2, '3': 3, '4': 4, '5': 5,
    '6': 6, '7': 7, '8': 8, '9': 9,
}

# Simbol tampilan (opsional, untuk renderer Person C)
SUIT_SYMBOLS = {
    'spades':   '♠',
    'hearts':   '♥',
    'diamonds': '♦',
    'clubs':    '♣',
}

# ── Aturan permainan ──────────────────────────────────────────────────────────

MIN_PLAYERS        = 2
MAX_PLAYERS        = 4
INITIAL_LIVES      = 3
FIRST_PLAYER_CARDS = 5   # pemain pertama dapat 5 kartu, wajib buang 1
OTHER_PLAYER_CARDS = 4   # pemain lain dapat 4 kartu

# ── Nama aksi (dikirim dalam pesan JSON) ─────────────────────────────────────

ACTION_TAKE             = 'TAKE'             # ambil kartu
ACTION_DISCARD          = 'DISCARD'          # buang kartu
ACTION_KNOCK            = 'KNOCK'            # tutup ronde
ACTION_INITIAL_DISCARD  = 'INITIAL_DISCARD'  # buang wajib pemain pertama

# Sumber saat aksi TAKE
SOURCE_DECK    = 'DECK'
SOURCE_DISCARD = 'DISCARD'

# ── Tipe pesan TCP (dipakai di protocol.py) ───────────────────────────────────

MSG_GAME_STATE      = 'GAME_STATE'       # server kirim state ke satu client
MSG_ACTION_REQ      = 'ACTION_REQ'       # server minta aksi dari pemain giliran
MSG_ACTION_RESP     = 'ACTION_RESP'      # server konfirmasi hasil aksi
MSG_ROUND_END       = 'ROUND_END'        # server umumkan hasil ronde
MSG_GAME_OVER       = 'GAME_OVER'        # server umumkan pemenang akhir
MSG_ERROR           = 'ERROR'            # server kirim pesan error
MSG_PLAYER_JOINED   = 'PLAYER_JOINED'    # notifikasi pemain baru masuk lobby
MSG_GAME_START      = 'GAME_START'       # server beritahu game dimulai

# Fitur wajib — real-time
MSG_PING            = 'PING'             # client → server: ukur latensi
MSG_PONG            = 'PONG'             # server → client: balas ping

# Fitur wajib — reconnect
MSG_RECONNECT_REQ   = 'RECONNECT_REQ'    # client → server: minta rejoin
MSG_RECONNECT_OK    = 'RECONNECT_OK'     # server → client: kirim full state
MSG_RECONNECT_FAIL  = 'RECONNECT_FAIL'   # server → client: sesi tidak ditemukan

# Fitur wajib — sinkronisasi & validasi
MSG_STATE_SYNC      = 'STATE_SYNC'       # server paksa sync state ke semua client
MSG_INVALID_PACKET  = 'INVALID_PACKET'   # server tolak paket tidak valid

# ── Konfigurasi jaringan ──────────────────────────────────────────────────────
# Dipakai Person B di server.py dan Person C di client.py

DEFAULT_HOST        = '0.0.0.0'
DEFAULT_PORT        = 5555
BUFFER_SIZE         = 4096
PING_INTERVAL_SEC   = 5      # kirim PING tiap N detik
PING_TIMEOUT_SEC    = 15     # disconnect jika tidak ada respons N detik
RECONNECT_WINDOW_SEC = 60    # waktu maksimal client boleh reconnect setelah putus

# ── Konfigurasi logging ───────────────────────────────────────────────────────

LOG_FILE            = 'game_41.log'
LOG_LEVEL           = 'INFO'   # DEBUG | INFO | WARNING | ERROR

# ── Tipe event untuk activity log & match replay ─────────────────────────────

LOG_EVENT_TAKE      = 'TAKE'
LOG_EVENT_DISCARD   = 'DISCARD'
LOG_EVENT_KNOCK     = 'KNOCK'
LOG_EVENT_ROUND_END = 'ROUND_END'
LOG_EVENT_GAME_OVER = 'GAME_OVER'
LOG_EVENT_CONNECT   = 'CONNECT'
LOG_EVENT_DISCONNECT= 'DISCONNECT'
LOG_EVENT_RECONNECT = 'RECONNECT'