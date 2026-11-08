"""
aespa autopost (Windows) — x.com Following timeline -> Telegram topic.

Cara pakai:
  python autopost.py login   # sekali: buka Chrome, login akun X cadangan
  python autopost.py test    # kirim pesan uji ke Telegram
  python autopost.py         # jalan terus (cek tiap ~5 menit)
  python autopost.py once    # satu kali pengecekan lalu berhenti (untuk uji)
  python autopost.py reset   # lupakan riwayat, mulai lagi dari tweet terbaru
  python autopost.py backfill 3   # proses ulang tweet 3 jam terakhir (yang sudah terkirim tidak dobel)
  python autopost.py catchup 24   # kirim tweet 24 jam terakhir yang BELUM terkirim saja
"""
import html
import json
import logging
import os
import random
import re
import sys
import time

import requests

import config as C

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "state.json")
SECRETS_FILE = os.path.join(BASE, "secrets.json")
PROFILE_DIR = os.path.join(BASE, "chrome-profile")   # sesi login X disimpan di sini
LOG_FILE = os.path.join(BASE, "autopost.log")

MAX_UPLOAD_BYTES = 49 * 1024 * 1024
TWITTER_EPOCH_MS = 1288834974657
SEEN_LIMIT = 5000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.info


# ============================ SECRETS & STATE ============================

def load_secrets():
    try:
        with open(SECRETS_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except FileNotFoundError:
        sys.exit("File secrets.json belum ada. Salin secrets.example.json menjadi secrets.json lalu isi.")
    return str(s.get("BOT_TOKEN", "")).strip(), str(s.get("CHAT_ID", "")).strip()


BOT_TOKEN, CHAT_ID = "", ""


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("since_ms", 0)
    s.setdefault("seen", [])
    return s


SENT_LIMIT = 50000   # ID tweet & media yang SUDAH TERKIRIM (disimpan jauh lebih lama dari "seen")


def save_state(s):
    s["seen"] = s["seen"][-SEEN_LIMIT:]
    for k in ("sent_ids", "sent_media"):
        if k in s:
            s[k] = s[k][-SENT_LIMIT:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


def tweet_time_ms(tweet_id):
    """Waktu posting dari ID tweet (snowflake)."""
    return (int(tweet_id) >> 22) + TWITTER_EPOCH_MS


# ============================ PARSE DATA x.com ============================

def _user_screen_name(result):
    user = (((result.get("core") or {}).get("user_results") or {}).get("result") or {})
    return ((user.get("core") or {}).get("screen_name")
            or (user.get("legacy") or {}).get("screen_name") or "")


def normalize(result):
    """Ubah objek tweet GraphQL x.com menjadi format sederhana."""
    if not isinstance(result, dict):
        return None
    if result.get("__typename") == "TweetWithVisibilityResults" or ("tweet" in result and "legacy" not in result):
        result = result.get("tweet") or {}
    legacy = result.get("legacy")
    if not legacy:
        return None
    t = dict(legacy)
    t["id_str"] = legacy.get("id_str") or result.get("rest_id")
    note = (((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {})
    t["full_text"] = html.unescape(note.get("text") or legacy.get("full_text") or "")  # &amp; -> &
    t["user"] = {"screen_name": _user_screen_name(result)}
    t["_raw"] = result   # objek asli dari X (dipakai kalau lokasi media berubah)
    rt = ((legacy.get("retweeted_status_result") or {}).get("result"))
    t["retweeted_status"] = normalize(rt) if rt else None
    if not t["retweeted_status"]:
        t.pop("retweeted_status")
    return t if t["id_str"] else None


def _find_instructions(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("instructions"), list):
            return obj["instructions"]
        for v in obj.values():
            r = _find_instructions(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_instructions(v)
            if r is not None:
                return r
    return None


def parse_timeline_json(data):
    """Ambil semua tweet dari respons timeline. Mencari 'tweet_results' di mana pun dalam JSON,
    supaya tetap jalan walau X memindahkan posisi data di dalam respons."""
    tweets, seen_ids = [], set()

    def walk(o, promoted=False):
        if isinstance(o, dict):
            promoted = promoted or "promotedMetadata" in o          # lewati iklan
            tr = o.get("tweet_results")
            if isinstance(tr, dict):
                t = None if promoted else normalize(tr.get("result"))
                if t and t["id_str"] not in seen_ids:
                    seen_ids.add(t["id_str"])
                    tweets.append(t)
            for k, v in o.items():
                if k in ("tweet_results", "retweeted_status_result", "quoted_status_result"):
                    continue    # sudah diproses / bagian dari tweet lain
                walk(v, promoted)
        elif isinstance(o, list):
            for v in o:
                walk(v, promoted)

    walk(data)
    return tweets


def dump_debug(captured_meta):
    """Simpan respons timeline mentah ke debug_timeline.json untuk diperiksa."""
    path = os.path.join(BASE, "debug_timeline.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(captured_meta, f, ensure_ascii=False)
        size = os.path.getsize(path)
        if size > 3_000_000:   # potong kalau terlalu besar
            with open(path, "w", encoding="utf-8") as f:
                json.dump(captured_meta[:1], f, ensure_ascii=False)
        log(f"Respons timeline mentah disimpan ke debug_timeline.json ({os.path.getsize(path) // 1024} KB).")
    except Exception as e:
        log(f"Gagal menyimpan debug_timeline.json: {e}")


# ============================ FILTER MEMBER ============================

def _norm(s):
    return str(s).lower().replace("️", "")


def has_keyword(hay, k):
    if not k:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", k):
        return re.search(r"(^|[^a-z0-9])" + re.escape(k) + r"([^a-z0-9]|$)", hay) is not None
    return k in hay


def classify(src, user):
    hashtags = " ".join("#" + h.get("text", "") for h in (src.get("entities") or {}).get("hashtags", []))
    hay = _norm((src.get("full_text") or "") + " " + hashtags)
    rules = C.ACCOUNT_RULES.get(user.lower(), {})

    matched = [name for name, m in C.MEMBERS.items()
               if any(has_keyword(hay, _norm(k)) for k in m.get("keywords", []) + rules.get(name, []))]
    if not matched and rules.get("all"):
        matched = [rules["all"]]
    if not matched and rules.get("unmatched_topic"):
        return True, [], str(rules["unmatched_topic"])

    inc = [n for n in matched if C.MEMBERS.get(n, {}).get("send")]
    exc = [n for n in matched if n in C.MEMBERS and not C.MEMBERS[n].get("send")]
    if not matched:
        send = C.SEND_UNMATCHED
    elif inc and exc:
        send = not C.SKIP_IF_MIXED
    else:
        send = bool(inc)

    topic = ""
    if len(inc) > 1 and C.BOTH_TOPIC:
        topic = C.BOTH_TOPIC
    elif inc:
        topic = C.MEMBERS[inc[0]].get("topic", "")
    return send, matched, topic


# ============================ MEDIA ============================

def orig_photo_url(u):
    m = re.match(r"^(.*)\.(jpe?g|png|webp)$", u, re.I)
    if not m:
        return u + ("&" if "?" in u else "?") + "name=orig"
    return f"{m.group(1)}?format={m.group(2).lower()}&name=orig"


def photo_urls(u):
    """URL foto berurutan dari kualitas tertinggi: orig -> 4096x4096 -> large -> asli."""
    base = re.sub(r"\?.*$", "", u)
    m = re.match(r"^(.*)\.(jpe?g|png|webp)$", base, re.I)
    if m:
        stem, fmt = m.group(1), m.group(2).lower()
        cands = [f"{stem}?format={fmt}&name={n}" for n in ("orig", "4096x4096", "large")]
    else:
        cands = [f"{base}?name={n}" for n in ("orig", "4096x4096", "large")]
    return cands + [u]


def orig_photo_url(u):
    return photo_urls(u)[0]


SKIP_MEDIA_KEYS = ("retweeted_status_result", "quoted_status_result", "core", "user_results",
                   "card", "author_community_relationship", "birdwatch_pivot", "community_results")


def _media_items(src):
    """Daftar objek media mentah. Lokasi standar dulu, lalu cari di mana pun dalam tweet."""
    lst = (src.get("extended_entities") or src.get("entities") or {}).get("media") or []
    if lst:
        return lst
    found, keys = [], set()

    def walk(o):
        if isinstance(o, dict):
            url = o.get("media_url_https") or o.get("original_img_url")
            typ = o.get("type") or o.get("__typename", "")
            if url and isinstance(url, str) and ("pbs.twimg.com" in url or "video.twimg.com" in url):
                k = o.get("media_key") or o.get("id_str") or url
                if k not in keys:
                    keys.add(k)
                    found.append(o)
                return
            for k, v in o.items():
                if k not in SKIP_MEDIA_KEYS:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(src.get("_raw") or {})
    return found


MEDIA_KEY = re.compile(r"/(media|ext_tw_video|amplify_video|tweet_video)/([A-Za-z0-9_-]+)")


def media_key(url):
    """Identitas media yang sama walau dipakai di tweet berbeda (pbs.twimg.com/media/XXX, video id)."""
    m = MEDIA_KEY.search(url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def extract_media(src):
    out = []
    for m in _media_items(src):
        url = m.get("media_url_https") or m.get("original_img_url") or ""
        typ = str(m.get("type") or m.get("__typename") or "").lower()
        variants = (m.get("video_info") or {}).get("variants") or m.get("variants") or []
        if "video" in typ or "gif" in typ or variants:
            vs = [v for v in variants if "mp4" in str(v.get("content_type", "")) or str(v.get("url", "")).split("?")[0].endswith(".mp4")]
            vs.sort(key=lambda v: v.get("bitrate", 0) or 0, reverse=True)
            if vs:
                out.append({"kind": "video", "url": vs[0]["url"], "urls": [v["url"] for v in vs]})
                continue
        if url and "pbs.twimg.com" in url:
            urls = photo_urls(url)
            out.append({"kind": "photo", "url": urls[0], "urls": urls})
    return out


def download(item, base, idx, max_bytes=None):
    """Unduh media; coba ulang & turun ke kualitas berikutnya kalau gagal / terlalu besar."""
    max_bytes = max_bytes or MAX_UPLOAD_BYTES
    reasons = []
    for url in item.get("urls") or [item["url"]]:
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0",
                                                            "Referer": "https://x.com/"})
            except requests.RequestException as e:
                reasons.append(type(e).__name__)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200 and r.content:
                if len(r.content) > max_bytes:
                    reasons.append(f">{max_bytes // (1024 * 1024)}MB")
                    break  # coba varian berikutnya (kualitas lebih rendah)
                if item["kind"] == "video":
                    ext = "mp4"
                else:
                    m = re.search(r"format=(\w+)", url)
                    ext = m.group(1) if m else "jpg"
                return (f"{base}_{idx + 1}.{ext}", r.content)
            reasons.append(f"HTTP {r.status_code}")
            if r.status_code in (404, 410):
                break  # URL ini memang tidak ada, coba URL berikutnya
            time.sleep(2 * (attempt + 1))
    log(f"Gagal unduh media {idx + 1} ({item['kind']}): {', '.join(reasons[-4:])}")
    return None


# ============================ TELEGRAM ============================

def tg(method, data, files=None, topic=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = dict(data)
    t = str(topic or "").strip()
    if t and t != "1" and t.lower() != "general":
        data["message_thread_id"] = t
    size = 0
    for v in (files or {}).values():
        if isinstance(v, tuple) and len(v) > 1 and isinstance(v[1], (bytes, bytearray)):
            size += len(v[1])
    # upload besar butuh waktu: minimal 2 menit, +1 menit per 4 MB, maks 8 menit
    write_timeout = min(max(120, size / (4 * 1024 * 1024) * 60), 480)
    if size > 5 * 1024 * 1024:
        log(f"Mengupload {method} {size / 1024 / 1024:.1f} MB ke Telegram (batas {write_timeout / 60:.0f} menit)...")
    for attempt in range(3):
        try:
            r = requests.post(url, data=data, files=files, timeout=write_timeout)  # satu angka: berlaku utk kirim & terima
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            log(f"Telegram {method} error: {(str(e).replace(BOT_TOKEN, '<TOKEN>') if BOT_TOKEN else str(e))[:200]}")
            if size > 5 * 1024 * 1024:
                return False   # upload besar gagal: jangan ulang file yang sama, pakai fallback video kecil
            time.sleep(5)
            continue
        if body.get("ok"):
            return True
        retry = (body.get("parameters") or {}).get("retry_after")
        if retry and attempt < 2:
            time.sleep(int(retry) + 1)
            continue
        log(f"Telegram {method} gagal: {r.text[:300]}")
        return False
    return False


def send_group(items, caption, topic):
    def tg_type(it):
        if it["kind"] == "video":
            return "video"
        return "document" if C.SEND_MODE == "document" else "photo"

    if len(items) == 1:
        it = items[0]
        typ = tg_type(it)
        method = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}[typ]
        data = {"chat_id": CHAT_ID, "caption": caption}
        if typ == "video":
            data["supports_streaming"] = "true"
        if it.get("url_only"):
            data[typ] = it["url_only"]
            return tg(method, data, topic=topic)
        return tg(method, data, files={typ: it["file"]}, topic=topic)

    for start in range(0, len(items), 10):
        chunk = items[start:start + 10]
        files, media = {}, []
        for i, it in enumerate(chunk):
            if it.get("url_only"):
                obj = {"type": tg_type(it), "media": it["url_only"]}
            else:
                key = f"f{i}"
                files[key] = it["file"]
                obj = {"type": tg_type(it), "media": f"attach://{key}"}
            if start == 0 and i == 0 and caption:
                obj["caption"] = caption
            if obj["type"] == "video":
                obj["supports_streaming"] = True
            media.append(obj)
        if not tg("sendMediaGroup", {"chat_id": CHAT_ID, "media": json.dumps(media)}, files=files or None, topic=topic):
            return False
    return True


def send_tweet(t, src, media, user, topic):
    is_rt = "retweeted_status" in t
    src_user = (src.get("user") or {}).get("screen_name") or user
    text = re.sub(r"\s*https://t\.co/\w+\s*$", "", src.get("full_text") or "").strip()
    link = f"https://x.com/{src_user}/status/{src['id_str']}"
    header = f"🔁 @{user} repost @{src_user}\n\n" if is_rt else ""
    body = header + text
    if len(body) > 850:
        body = body[:849] + "…"
    caption = f"{body}\n\n🔗 {link}"

    photos, videos, failed = [], [], 0
    for i, item in enumerate(media):
        f = download(item, f"{src_user}_{src['id_str']}", i)
        if not f:
            failed += 1
            continue
        (photos if item["kind"] == "photo" else videos).append({"kind": item["kind"], "file": f})
    if failed:
        caption += f"\n⚠️ {failed} dari {len(media)} media gagal diunduh, lihat di link."
    if not photos and not videos:
        return tg("sendMessage", {"chat_id": CHAT_ID, "text": caption}, topic=topic)

    photos_done = [False]   # foto cukup dikirim sekali walau video dicoba berkali-kali

    def send_all(ph, vi, cap):
        if C.SEND_MODE == "document" or photos_done[0]:
            if ph and not photos_done[0]:
                if not send_group(ph, cap, topic):
                    return False
                photos_done[0] = True
                cap = ""                      # caption sudah ikut di foto
            elif photos_done[0]:
                cap = ""
            return send_group(vi, cap, topic) if vi else True
        return send_group(ph + vi, cap, topic)

    vids = [m for m in media if m["kind"] == "video"]

    def try_url():
        """Minta Telegram mengambil video langsung dari server X (tanpa upload dari PC)."""
        levels = max(len(m.get("urls") or [m["url"]]) for m in vids)
        for level in range(levels):
            by_url = [{"kind": "video",
                       "url_only": (m.get("urls") or [m["url"]])[min(level, len(m.get("urls") or [m["url"]]) - 1)]}
                      for m in vids]
            if send_all(photos, by_url, caption):
                log(f"Video terkirim lewat URL{' (kualitas tertinggi)' if level == 0 else f' (varian ke-{level + 1})'}.")
                return True
        return False

    url_first = getattr(C, "VIDEO_VIA_URL_FIRST", True)
    if vids and url_first and try_url():
        return True
    if send_all(photos, videos, caption):
        return True
    if vids and not url_first and try_url():
        return True
    if videos:   # upload video gagal (biasanya terlalu besar / koneksi lambat): coba versi lebih kecil
        small_mb = getattr(C, "SMALL_VIDEO_MB", 15)
        log(f"Upload gagal, mencoba video versi lebih kecil (≤{small_mb} MB)...")
        small = []
        for i, item in enumerate(m for m in media if m["kind"] == "video"):
            f = download(item, f"{src_user}_{src['id_str']}", i, max_bytes=small_mb * 1024 * 1024)
            if f:
                small.append({"kind": "video", "file": f})
        if small and send_all(photos, small, caption + "\n(video versi lebih kecil, kualitas penuh di link)"):
            return True
    return False


# ============================ PROSES ============================

def process(tweets, state):
    """Kirim tweet baru dari akun yang dipantau. Return jumlah terkirim."""
    accounts = {a.strip().lstrip("@").lower() for a in C.ACCOUNTS if a.strip()}
    seen = set(state["seen"])

    first_run = not state["since_ms"]
    if first_run:
        state["since_ms"] = int(time.time() * 1000)

    uniq = {}
    for t in tweets:
        uniq.setdefault(t["id_str"], t)
    mine = [t for t in uniq.values() if (t.get("user") or {}).get("screen_name", "").lower() in accounts]

    if first_run:
        state["seen"].extend(t["id_str"] for t in mine)
        save_state(state)  # simpan ke file supaya tidak inisialisasi ulang setelah restart
        log(f"Inisialisasi: {len(mine)} tweet yang sudah ada ditandai lama. Tweet baru terkirim mulai berikutnya.")
        return 0

    new = [t for t in mine if t["id_str"] not in seen and tweet_time_ms(t["id_str"]) >= state["since_ms"]]
    new.sort(key=lambda t: int(t["id_str"]))

    def _src(t):
        return t["retweeted_status"] if "retweeted_status" in t else t

    if len(new) >= 5 and not any(extract_media(_src(t)) for t in new):
        path = os.path.join(BASE, "debug_tweets.json")
        try:
            sample = [{k: v for k, v in _src(t).items() if k != "retweeted_status"} for t in new[:5]]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(sample, f, ensure_ascii=False, default=str)
        except Exception:
            pass
        log(f"Semua {len(new)} tweet baru terbaca TANPA media -> kemungkinan format media X berubah. "
            "Tweet TIDAK ditandai diproses. Contoh data disimpan ke debug_tweets.json.")
        alert("semua tweet baru terbaca tanpa media (format data X kemungkinan berubah). "
              "Tweet tidak ditandai, kirim debug_tweets.json ke pengembang.", state)
        return 0

    sent = skipped = filtered = consecutive_fail = 0
    state["backlog_oldest_ms"] = 0
    for t in new:
        if sent >= C.MAX_POSTS_PER_RUN:
            # antrean belum habis: ingat tweet tertua yang belum diproses supaya
            # pengecekan berikutnya scroll mundur sampai ke situ
            left = [x for x in new if x["id_str"] not in seen]
            if left:
                state["backlog_oldest_ms"] = min(tweet_time_ms(x["id_str"]) for x in left)
                log(f"Antrean: {len(left)} tweet menunggu, dilanjutkan pengecekan berikutnya.")
            break
        user = t["user"]["screen_name"]
        is_rt = "retweeted_status" in t
        src = t["retweeted_status"] if is_rt else t
        media = [] if (is_rt and not C.INCLUDE_RETWEETS) else extract_media(src)

        sent_ids = state.setdefault("sent_ids", [])
        sent_media = state.setdefault("sent_media", [])
        keys = [k for k in (media_key(m["url"]) for m in media) if k]
        if src["id_str"] in seen or src["id_str"] in sent_ids:   # tweet asli sudah pernah diproses / dikirim
            skipped += 1
        elif keys and all(k in sent_media for k in keys):      # foto/video yang sama sudah pernah dikirim
            log(f"[{user}] skip {src['id_str']} (media sama sudah pernah terkirim)")
            skipped += 1
        elif not media:
            skipped += 1
        else:
            ok_send, matched, topic = classify(src, user)
            if not ok_send:
                log(f"[{user}] skip {src['id_str']} (member: {','.join(matched) or '-'})")
                filtered += 1
            elif send_tweet(t, src, media, user, topic):
                log(f"[{user}] terkirim {src['id_str']} -> topic {topic or 'default'}")
                sent_ids.extend(x for x in {src['id_str'], t['id_str']} if x not in sent_ids)
                sent_media.extend(k for k in keys if k not in sent_media)
                state.get("fail_counts", {}).pop(src["id_str"], None)
                consecutive_fail = 0
                sent += 1
                time.sleep(C.DELAY_BETWEEN_POSTS)
            else:
                fails = state.setdefault("fail_counts", {})
                n = fails.get(src["id_str"], 0) + 1
                fails[src["id_str"]] = n
                consecutive_fail += 1
                max_try = getattr(C, "MAX_SEND_ATTEMPTS", 3)
                if n < max_try:
                    log(f"[{user}] gagal kirim {src['id_str']} (percobaan {n}/{max_try}), lanjut ke tweet lain")
                    save_state(state)
                    if consecutive_fail >= 3:   # banyak gagal beruntun -> kemungkinan koneksi/Telegram bermasalah
                        log("3 kiriman gagal beruntun, koneksi ke Telegram mungkin bermasalah. Berhenti dulu.")
                        break
                    continue   # JANGAN tandai seen; dicoba lagi pengecekan berikutnya
                # sudah gagal berkali-kali: kirim teks + link saja supaya antrean tidak tertahan
                text = re.sub(r"\s*https://t\.co/\w+\s*$", "", src.get("full_text") or "").strip()[:800]
                link = f"https://x.com/{(src.get('user') or {}).get('screen_name') or user}/status/{src['id_str']}"
                if tg("sendMessage", {"chat_id": CHAT_ID,
                                      "text": f"{text}\n\n🔗 {link}\n⚠️ Media gagal diupload setelah {n}x percobaan, lihat di link."},
                      topic=topic):
                    log(f"[{user}] {src['id_str']} gagal {n}x -> dikirim sebagai teks + link")
                    sent_ids.extend(x for x in {src['id_str'], t['id_str']} if x not in sent_ids)
                    sent_media.extend(k for k in keys if k not in sent_media)
                    fails.pop(src["id_str"], None)
                    sent += 1
                else:
                    save_state(state)
                    continue
        state["seen"].append(t["id_str"])
        seen.add(t["id_str"])
        if src["id_str"] != t["id_str"]:
            state["seen"].append(src["id_str"])
            seen.add(src["id_str"])
        save_state(state)

    log(f"Timeline: {len(uniq)} tweet, dari akun dipantau: {len(mine)}, baru: {len(new)} | "
        f"terkirim: {sent}, skip tanpa media: {skipped}, skip bukan karina/winter: {filtered}")
    return sent


# ============================ BROWSER ============================

class NotLoggedIn(Exception):
    pass


class XErrorPage(Exception):
    """Halaman 'Something went wrong' dari X (biasanya rate limit sementara / gangguan X)."""
    pass


X_ERROR_TEXT = re.compile(r"something went wrong|terjadi kesalahan|ada yang salah", re.I)


def x_error_page(page):
    try:
        return page.get_by_text(X_ERROR_TEXT).first.is_visible(timeout=1500)
    except Exception:
        return False


def check_x_error(page):
    """Kalau muncul halaman error X: klik 'Try again' sekali; kalau masih error -> XErrorPage."""
    if not x_error_page(page):
        return
    log("Halaman error X terdeteksi ('Something went wrong'). Mencoba 'Try again' sekali...")
    try:
        page.get_by_role("button", name=re.compile(r"^(try again|coba lagi|retry)$", re.I)).first.click(timeout=5000)
        page.wait_for_timeout(8000)
    except Exception:
        pass
    if x_error_page(page):
        raise XErrorPage()


def open_browser(pw, headless=None):
    headless = C.HEADLESS if headless is None else headless
    kwargs = dict(user_data_dir=PROFILE_DIR, headless=headless, viewport={"width": 1280, "height": 900},
                  args=["--disable-blink-features=AutomationControlled",
                        # tetap aktif walau layar terkunci / jendela tertutup / di-minimize
                        "--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--disable-background-timer-throttling"])
    try:
        return pw.chromium.launch_persistent_context(channel="chrome", **kwargs)  # Chrome yang terpasang
    except Exception:
        return pw.chromium.launch_persistent_context(**kwargs)                    # Chromium bawaan Playwright


DIALOG_SEL = '[role="dialog"], [data-testid="sheetDialog"], [data-testid="confirmationSheetDialog"]'
# Tombol "aman" untuk menutup popup. Tombol konfirmasi akun (Yes / Update email / Verify) TIDAK diklik otomatis.
SAFE_BUTTON = re.compile(r"^(close|not now|skip|dismiss|got it|maybe later|no thanks|"
                         r"tutup|nanti saja|lewati|lain kali|tidak sekarang|oke|ok)$", re.I)


def blocking_dialog_text(page):
    """Teks popup yang sedang menutupi halaman ('' kalau tidak ada)."""
    try:
        dlg = page.locator(DIALOG_SEL).first
        if dlg.count() and dlg.is_visible():
            return re.sub(r"\s+", " ", dlg.inner_text(timeout=3000)).strip()[:200]
    except Exception:
        pass
    return ""


def dismiss_dialogs(page):
    """Coba tutup popup dengan Escape / tombol Close / Not now. Return teks popup yang masih tersisa."""
    for _ in range(3):
        text = blocking_dialog_text(page)
        if not text:
            return ""
        log(f"Popup X terdeteksi: {text[:120]}")
        page.keyboard.press("Escape")
        page.wait_for_timeout(1500)
        if not blocking_dialog_text(page):
            log("Popup tertutup dengan Escape.")
            return ""
        try:
            dlg = page.locator(DIALOG_SEL).first
            closed = False
            close_btn = dlg.locator('[aria-label="Close"], [aria-label="Tutup"], [data-testid="app-bar-close"]').first
            if close_btn.count():
                close_btn.click(timeout=3000)
                closed = True
            else:
                for b in dlg.get_by_role("button").all():
                    label = (b.inner_text(timeout=1000) or "").strip()
                    if SAFE_BUTTON.match(label):
                        b.click(timeout=3000)
                        log(f"Klik tombol popup: {label}")
                        closed = True
                        break
            if not closed:
                break
        except Exception:
            break
        page.wait_for_timeout(1500)
    return blocking_dialog_text(page)


def alert_with_screenshot(page, text, state, key):
    """Peringatan + screenshot layar bot ke Telegram (maks sekali per 3 jam per jenis)."""
    now = time.time()
    if now - state.get(key, 0) < 3 * 3600:
        return
    try:
        png = page.screenshot()
        tg("sendPhoto", {"chat_id": CHAT_ID, "caption": ("⚠️ autopost: " + text)[:1000]},
           files={"photo": ("bot.png", png)}, topic=C.BOTH_TOPIC)
    except Exception:
        tg("sendMessage", {"chat_id": CHAT_ID, "text": "⚠️ autopost: " + text}, topic=C.BOTH_TOPIC)
    state[key] = now
    save_state(state)


LAST_CAPTURE_OK = False   # True kalau X mengirim respons timeline (walau isinya 0 tweet baru)


def collect(page, state=None, cover_since_ms=0):
    global LAST_CAPTURE_OK
    LAST_CAPTURE_OK = False
    captured = []
    captured_meta = []

    ops_seen = []

    def on_response(resp):
        url = resp.url
        if "/graphql/" not in url:
            return
        op = url.split("/graphql/", 1)[1].split("?")[0].split("/")[-1]
        ops_seen.append(op)
        if "HomeLatestTimeline" in url or "HomeTimeline" in url:
            try:
                data = resp.json()
                captured.append(data)
                captured_meta.append({"op": op, "status": resp.status, "data": data})
            except Exception:
                pass

    page.on("response", on_response)
    try:
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        if "/login" in page.url or "/i/flow" in page.url or "logout" in page.url:
            raise NotLoggedIn()
        check_x_error(page)
        left = dismiss_dialogs(page)
        if left and state is not None:
            log("Popup tidak bisa ditutup otomatis. Butuh tindakan manual di jendela Chrome bot.")
            alert_with_screenshot(page, "ada popup X yang menutupi timeline dan tidak bisa ditutup otomatis: "
                                  f"\"{left[:150]}\". Buka PC, selesaikan popup di jendela Chrome bot.",
                                  state, "last_dialog_alert")
        try:  # pastikan tab Following (kronologis) yang aktif
            page.get_by_role("tab", name=re.compile(r"^(Following|Mengikuti)$", re.I)).first.click(timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(random.randint(4000, 6000))
        try:  # tombol "Show N posts" -> muat tweet terbaru
            pill = page.get_by_text(re.compile(r"^(Show \d+ posts?|Tampilkan \d+ (posting|postingan))$", re.I)).first
            if pill.count():
                pill.click(timeout=3000)
                page.wait_for_timeout(3000)
        except Exception:
            pass
        for _ in range(C.SCROLLS_PER_CHECK):
            page.mouse.wheel(0, random.randint(2500, 3500))
            page.wait_for_timeout(random.randint(2000, 3500))

        # Mengejar ketinggalan: scroll terus sampai tweet tertua yang terbaca lebih tua dari
        # pengecekan sukses terakhir, supaya tweet selama PC mati / restart tidak terlewat.
        if cover_since_ms and not captured:
            check_x_error(page)   # timeline kosong: jangan mengejar ketinggalan di halaman error
        has_tweets = any(parse_timeline_json(d) for d in captured)
        if captured and not has_tweets:
            log(f"Respons timeline diterima ({len(captured)}x) tapi 0 tweet terbaca. "
                f"Operasi graphql terlihat: {sorted(set(ops_seen))[:15]}")
            dump_debug(captured_meta)
        if cover_since_ms and captured and has_tweets:
            max_extra = getattr(C, "MAX_CATCHUP_SCROLLS", 25)
            extra = stuck = 0
            prev_count = -1
            covered = False
            while extra < max_extra:
                ts = [t for d in captured for t in parse_timeline_json(d)]
                oldest = min((tweet_time_ms(t["id_str"]) for t in ts), default=None)
                if oldest is not None and oldest <= cover_since_ms:
                    covered = True
                    break
                stuck = stuck + 1 if len(ts) == prev_count else 0
                if stuck >= 4:      # 4x scroll tanpa tweet baru termuat -> X tidak memuat lebih jauh
                    break
                prev_count = len(ts)
                page.keyboard.press("End")          # scroll paling bawah (pemicu muat lanjutan)
                page.mouse.wheel(0, random.randint(2500, 3500))
                page.wait_for_timeout(random.randint(2500, 4000))
                extra += 1
            if extra:
                ts = [t for d in captured for t in parse_timeline_json(d)]
                oldest = min((tweet_time_ms(t["id_str"]) for t in ts), default=None)
                covered = covered or (oldest is not None and oldest <= cover_since_ms)
                reach_h = (time.time() * 1000 - oldest) / 3600000 if oldest else 0
                gap_h = (time.time() * 1000 - cover_since_ms) / 3600000
                if covered:
                    log(f"Mengejar ketinggalan: scroll tambahan {extra}x, {len(ts)} tweet, lengkap.")
                else:
                    why = "X berhenti memuat tweet lama" if stuck >= 4 else "batas MAX_CATCHUP_SCROLLS"
                    log(f"Mengejar ketinggalan TIDAK lengkap ({why}): scroll {extra}x, {len(ts)} tweet, "
                        f"terbaca {reach_h:.1f} jam ke belakang dari jeda {gap_h:.1f} jam.")
                    if state is not None:
                        alert(f"mengejar ketinggalan tidak lengkap ({why}): terbaca {reach_h:.1f} jam "
                              f"dari jeda {gap_h:.1f} jam. Tweet sebelum itu mungkin terlewat.", state)
        page.mouse.wheel(0, -50000)  # kembali ke atas
        if not captured:
            check_x_error(page)
    finally:
        page.remove_listener("response", on_response)

    tweets = []
    for data in captured:
        tweets.extend(parse_timeline_json(data))
    LAST_CAPTURE_OK = bool(captured)
    if not captured:
        log("Peringatan: data timeline tidak tertangkap (halaman lambat / tampilan X berubah?).")
    return tweets


class StaleTimeline(Exception):
    pass


def check_fresh(page, tweets, state):
    """Deteksi timeline 'macet': tweet terbaru yang tertangkap tidak berubah terlalu lama."""
    stale_hours = getattr(C, "STALE_HOURS", 3)
    now = time.time()
    newest = max((int(t["id_str"]) for t in tweets), default=0)
    if newest > state.get("newest_id", 0):
        state["newest_id"] = newest
        state["newest_changed_at"] = now
        save_state(state)
        return
    state.setdefault("newest_changed_at", now)
    idle_h = (now - state["newest_changed_at"]) / 3600
    if idle_h >= stale_hours:
        log(f"Timeline tidak bergerak {idle_h:.1f} jam. Browser akan dibuka ulang.")
        alert_with_screenshot(page, f"timeline X tidak bergerak {idle_h:.0f} jam (popup / logout / X bermasalah?). "
                              "Bot membuka ulang browser. Kalau peringatan ini berulang, cek layar PC.",
                              state, "last_stale_alert")
        state["newest_changed_at"] = now  # beri waktu sebelum peringatan berikutnya
        save_state(state)
        raise StaleTimeline()


def alert(text, state):
    """Kirim peringatan ke Telegram (maks sekali per jam)."""
    now = time.time()
    if now - state.get("last_alert", 0) > 3600:
        tg("sendMessage", {"chat_id": CHAT_ID, "text": "⚠️ autopost: " + text}, topic=C.BOTH_TOPIC)
        state["last_alert"] = now
        save_state(state)


def run(loop=True):
    from playwright.sync_api import sync_playwright

    state = load_state()
    with sync_playwright() as pw:
        ctx, started = None, 0
        while True:
            try:
                if ctx is None or time.time() - started > C.RESTART_BROWSER_HOURS * 3600:
                    if ctx:
                        ctx.close()
                    ctx = open_browser(pw)
                    started = time.time()
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                last = state.get("last_check", 0)
                if last and time.time() - last > C.CHECK_EVERY_MINUTES * 60 * 4:
                    log(f"Catatan: jeda sejak cek terakhir {(time.time() - last) / 3600:.1f} jam "
                        "(PC sempat sleep / hang?).")
                state["last_check"] = time.time()
                last_ok = state.get("last_ok", 0)
                cover = int((last_ok - 600) * 1000) if last_ok else 0   # mundur 10 menit untuk aman
                tweets = collect(page, state, cover_since_ms=cover)
                process(tweets, state)
                if tweets:  # sukses hanya kalau tweet benar-benar terbaca
                    backlog = state.get("backlog_oldest_ms", 0)
                    # masih ada antrean -> titik "terakhir sukses" = tweet tertua yang belum terkirim
                    state["last_ok"] = backlog / 1000 if backlog else time.time()
                    state["x_error_count"] = 0
                    state["empty_count"] = 0
                    save_state(state)
                elif LAST_CAPTURE_OK:
                    state["empty_count"] = state.get("empty_count", 0) + 1
                    save_state(state)
                    if state["empty_count"] >= 6:   # ~30 menit berturut-turut
                        alert_with_screenshot(page, "X mengirim data timeline tapi bot tidak bisa membaca tweet "
                                              f"di dalamnya ({state['empty_count']}x berturut-turut). Format data X "
                                              "kemungkinan berubah. Kirim file debug_timeline.json ke pengembang.",
                                              state, "last_parse_alert")
                check_fresh(page, tweets, state)
            except XErrorPage:
                n = state.get("x_error_count", 0) + 1
                state["x_error_count"] = n
                pause_min = min(15 * 2 ** (n - 1), 120)          # 15, 30, 60, 120, 120 ... menit
                state["pause_until"] = time.time() + pause_min * 60
                save_state(state)
                log(f"X menampilkan 'Something went wrong' (ke-{n} berturut-turut). "
                    f"Kemungkinan rate limit sementara. Bot istirahat {pause_min} menit.")
                if n >= 3:
                    alert_with_screenshot(page, f"X terus menampilkan 'Something went wrong' ({n}x berturut-turut). "
                                          f"Bot istirahat {pause_min} menit lalu mencoba lagi otomatis. "
                                          "Kalau berlanjut berjam-jam, coba buka x.com manual di Chrome bot.",
                                          state, "last_xerror_alert")
            except StaleTimeline:
                try:
                    ctx.close()
                except Exception:
                    pass
                ctx = None
            except NotLoggedIn:
                log("Akun X tidak dalam keadaan login. Jalankan: python autopost.py login")
                alert("akun X cadangan logout. Jalankan login.bat di PC.", state)
            except Exception as e:
                log(f"Error: {type(e).__name__}: {e}")
                try:
                    if ctx:
                        ctx.close()
                except Exception:
                    pass
                ctx = None
            if not loop:
                break
            wait = C.CHECK_EVERY_MINUTES * 60 * random.uniform(0.8, 1.2)
            wait = max(wait, state.get("pause_until", 0) - time.time())
            log(f"Cek berikutnya dalam {wait / 60:.1f} menit.")
            time.sleep(wait)
        if ctx:
            ctx.close()


def login():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = open_browser(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://x.com/login")
        input("\nLogin akun X CADANGAN di jendela Chrome yang terbuka.\n"
              "Setelah timeline muncul, kembali ke sini dan tekan Enter... ")
        ctx.close()
    print("Sesi login tersimpan di folder chrome-profile. Selanjutnya jalankan run.bat")


def main():
    global BOT_TOKEN, CHAT_ID
    mode = (sys.argv[1] if len(sys.argv) > 1 else "run").lower()
    if mode == "login":
        return login()
    if mode == "reset":
        save_state({"since_ms": 0, "seen": []})
        return print("Riwayat direset. Pengecekan berikutnya dimulai dari tweet terbaru.")
    if mode == "catchup":
        hours = float(sys.argv[2]) if len(sys.argv) > 2 else 24
        state = load_state()
        point_ms = int((time.time() - hours * 3600) * 1000)
        state["last_ok"] = point_ms / 1000                       # scroll mundur sampai titik ini
        state["since_ms"] = min(state.get("since_ms") or point_ms, point_ms)
        state["backlog_oldest_ms"] = point_ms
        save_state(state)                                       # daftar "sudah terkirim" TIDAK dihapus
        return print(f"OK. Saat run.bat jalan, bot scroll mundur {hours:g} jam dan hanya mengirim tweet "
                     "yang BELUM pernah terkirim (tidak ada kiriman ganda).")
    if mode == "backfill":
        hours = float(sys.argv[2]) if len(sys.argv) > 2 else 3
        state = load_state()
        state["since_ms"] = int((time.time() - hours * 3600) * 1000)
        state["seen"] = [i for i in state["seen"] if tweet_time_ms(i) < state["since_ms"]]
        state["last_ok"] = state["since_ms"] / 1000   # mengejar ketinggalan scroll mundur sampai titik ini
        state["backlog_oldest_ms"] = state["since_ms"]
        save_state(state)   # catatan "sudah terkirim" (sent_ids / sent_media) TIDAK dihapus -> tidak ada kiriman ganda
        return print(f"OK. Tweet dari {hours:g} jam terakhir diproses ulang (termasuk yang dulu di-skip), "
                     "tapi yang sudah pernah terkirim tidak dikirim lagi.")

    BOT_TOKEN, CHAT_ID = load_secrets()
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("BOT_TOKEN / CHAT_ID di secrets.json belum diisi.")
    if mode == "test":
        ok = tg("sendMessage", {"chat_id": CHAT_ID, "text": "✅ Bot aktif (Windows)."}, topic=C.BOTH_TOPIC)
        return print("Berhasil, cek Telegram." if ok else "Gagal, cek secrets.json / bot admin grup.")
    log(f"autopost mulai. Memantau {len(C.ACCOUNTS)} akun lewat tab Following.")
    run(loop=(mode != "once"))


if __name__ == "__main__":
    main()