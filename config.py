# =====================================================================
#  PENGATURAN — file ini yang paling sering Anda edit.
#  Setelah mengedit, tutup jendela autopost lalu jalankan run.bat lagi.
# =====================================================================

# Akun X yang dipantau (tanpa @).
# PENTING: akun X cadangan Anda harus mem-FOLLOW semua akun ini.
ACCOUNTS = [
    "aespa_official",
    "aespasbbl",
    "karinaspics",
    "winterpic",
    "ujiminphoria",
    "snowdrop_010101",
    "sunshine_080507",
    "hourlykrn",
    "hourlykwinter",
]

# send: True = dikirim, False = di-skip
# topic: ID topic tujuan di grup Telegram
# keywords: nama / hashtag / emoji (huruf besar-kecil diabaikan;
#           kata latin dicocokkan per kata utuh, teks Korea/emoji apa adanya)
MEMBERS = {
    "karina": {"send": True, "topic": "18",
               "keywords": ["karina", "카리나", "유지민", "yu jimin", "jimin", "カリナ"]},
    "winter": {"send": True, "topic": "18",
               "keywords": ["winter", "윈터", "김민정", "민정", "minjeong", "pponyo", "뽀뇨", "ウィンター"]},
    "ningning": {"send": False,
                 "keywords": ["ningning", "닝닝", "宁艺卓", "ning yizhuo", "ニンニン"]},
    "giselle": {"send": False,
                "keywords": ["giselle", "지젤", "aeri", "에리", "ジゼル"]},
    # Idol lain: kalau disebut bersama karina/winter -> dianggap campuran -> skip
    "idol_lain": {"send": False, "keywords": [
        "ryujin", "류진", "yeji", "예지", "itzy",
        "wonyoung", "원영", "minji", "민지", "newjeans",
        "seulgi", "슬기", "irene", "아이린",
        "han sua", "한수아",
    ]},
}

# Penanda khusus per akun (username huruf kecil)
#   {"karina": ["💙"], "winter": ["⭐"]}  -> akun memakai emoji
#   {"all": "winter"}                      -> fansite satu member: tweet TANPA nama dianggap winter
#   {"unmatched_topic": "1"}               -> tweet tanpa nama member dikirim ke topic ini
ACCOUNT_RULES = {
    "aespasbbl":       {"karina": ["💙"], "winter": ["⭐"]},
    "karinaspics":     {"all": "karina"},
    "ujiminphoria":    {"all": "karina"},
    "winterpic":       {"all": "winter"},
    "snowdrop_010101": {"all": "winter"},
    "hourlykrn":       {"all": "karina"},
    "hourlykwinter":   {"all": "winter"},
    "mandubluu":      {"all": "karina"},
    "MyWinterWorld":   {"all": "winter"},
    "fc__mobile":       {"karina": ["Karina"], "winter": ["Winter"]},
    "notes0411":      {"all": "karina"},
    "karinaofmoment": {"all": "karina"}, 
    "joyminjeong":    {"all": "winter"},
    "winteraphy": {"all": "winter"},
    "mjkmwntr": {"all": "winter"},
}

SEND_UNMATCHED = False   # tweet tanpa nama member: False = skip
SKIP_IF_MIXED = True     # karina/winter + member/idol lain: True = skip
BOTH_TOPIC = "18"        # topic kalau tweet berisi karina DAN winter

# 'document' = foto dikirim sebagai file (kualitas asli 100%)
# 'photo'    = album biasa (tampilan rapi, dikompres Telegram)
SEND_MODE = "document"

INCLUDE_RETWEETS = True
MAX_POSTS_PER_RUN = 15         # maks tweet terkirim per pengecekan
DELAY_BETWEEN_POSTS = 3        # detik antar kiriman ke Telegram

# ---- Browser ----
CHECK_EVERY_MINUTES = 5        # jeda antar pengecekan (diacak ±20% supaya terlihat manusiawi)
SCROLLS_PER_CHECK = 3          # berapa kali scroll timeline tiap pengecekan
HEADLESS = False               # False = jendela Chrome terlihat (lebih aman dari deteksi)
RESTART_BROWSER_HOURS = 6      # buka ulang browser berkala supaya memori tidak menumpuk
