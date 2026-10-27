"""
aespa autopost (Windows) — x.com Following timeline -> Telegram topic.

Cara pakai:
  python autopost.py login   # sekali: buka Chrome, login akun X cadangan
  python autopost.py test    # kirim pesan uji ke Telegram
  python autopost.py         # jalan terus (cek tiap ~5 menit)
  python autopost.py once    # satu kali pengecekan lalu berhenti (untuk uji)
  python autopost.py reset   # lupakan riwayat, mulai lagi dari tweet terbaru
  python autopost.py backfill 3   # kirim ulang tweet 3 jam terakhir yang terlewat
"""
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


def save_state(s):
    s["seen"] = s["seen"][-SEEN_LIMIT:]
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
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet") or {}
    legacy = result.get("legacy")
    if not legacy:
        return None
    t = dict(legacy)
    t["id_str"] = legacy.get("id_str") or result.get("rest_id")
    note = (((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {})
    t["full_text"] = note.get("text") or legacy.get("full_text") or ""
    t["user"] = {"screen_name": _user_screen_name(result)}
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
    tweets = []

    def take(item_content):
        if not item_content or "promotedMetadata" in item_content:   # lewati iklan
            return
        t = normalize((item_content.get("tweet_results") or {}).get("result"))
        if t:
            tweets.append(t)

    for ins in _find_instructions(data) or []:
        entries = ins.get("entries") or ([ins["entry"]] if ins.get("entry") else [])
        for e in entries:
            content = e.get("content") or {}
            take(content.get("itemContent"))
            for it in content.get("items") or []:       # thread / modul percakapan
                take((it.get("item") or {}).get("itemContent"))
    return tweets


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


def extract_media(src):
    lst = (src.get("extended_entities") or src.get("entities") or {}).get("media", [])
    out = []
    for m in lst:
        if m.get("type") == "photo":
            out.append({"kind": "photo", "url": orig_photo_url(m["media_url_https"])})
        else:  # video / animated_gif
            vs = [v for v in (m.get("video_info") or {}).get("variants", [])
                  if v.get("content_type") == "video/mp4"]
            vs.sort(key=lambda v: v.get("bitrate", 0), reverse=True)
            if vs:
                out.append({"kind": "video", "url": vs[0]["url"]})
    return out


def download(item, base, idx):
    try:
        r = requests.get(item["url"], timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException:
        return None
    if r.status_code != 200 or len(r.content) > MAX_UPLOAD_BYTES:
        return None
    if item["kind"] == "video":
        ext = "mp4"
    else:
        m = re.search(r"format=(\w+)", item["url"])
        ext = m.group(1) if m else "jpg"
    return (f"{base}_{idx + 1}.{ext}", r.content)


# ============================ TELEGRAM ============================

def tg(method, data, files=None, topic=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = dict(data)
    t = str(topic or "").strip()
    if t and t != "1" and t.lower() != "general":
        data["message_thread_id"] = t
    for attempt in range(3):
        try:
            r = requests.post(url, data=data, files=files, timeout=180)
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            log(f"Telegram {method} error: {e}")
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
        return tg(method, data, files={typ: it["file"]}, topic=topic)

    for start in range(0, len(items), 10):
        chunk = items[start:start + 10]
        files, media = {}, []
        for i, it in enumerate(chunk):
            key = f"f{i}"
            files[key] = it["file"]
            obj = {"type": tg_type(it), "media": f"attach://{key}"}
            if start == 0 and i == 0 and caption:
                obj["caption"] = caption
            if obj["type"] == "video":
                obj["supports_streaming"] = True
            media.append(obj)
        if not tg("sendMediaGroup", {"chat_id": CHAT_ID, "media": json.dumps(media)}, files=files, topic=topic):
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
        caption += "\n⚠️ Sebagian media >50MB / gagal diunduh, lihat di link."
    if not photos and not videos:
        return tg("sendMessage", {"chat_id": CHAT_ID, "text": caption}, topic=topic)

    groups = [g for g in (photos, videos) if g] if C.SEND_MODE == "document" else [photos + videos]
    for gi, g in enumerate(groups):
        if not send_group(g, caption if gi == 0 else "", topic):
            return False
    return True


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

    sent = skipped = filtered = 0
    for t in new:
        if sent >= C.MAX_POSTS_PER_RUN:
            break
        user = t["user"]["screen_name"]
        is_rt = "retweeted_status" in t
        src = t["retweeted_status"] if is_rt else t
        media = [] if (is_rt and not C.INCLUDE_RETWEETS) else extract_media(src)

        if src["id_str"] in seen:            # tweet asli sudah pernah dikirim (repost dari akun lain)
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
                sent += 1
                time.sleep(C.DELAY_BETWEEN_POSTS)
            else:
                log(f"[{user}] gagal kirim {t['id_str']}, dicoba lagi pengecekan berikutnya")
                break
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


def open_browser(pw, headless=None):
    headless = C.HEADLESS if headless is None else headless
    kwargs = dict(user_data_dir=PROFILE_DIR, headless=headless, viewport={"width": 1280, "height": 900},
                  args=["--disable-blink-features=AutomationControlled"])
    try:
        return pw.chromium.launch_persistent_context(channel="chrome", **kwargs)  # Chrome yang terpasang
    except Exception:
        return pw.chromium.launch_persistent_context(**kwargs)                    # Chromium bawaan Playwright


def collect(page):
    captured = []

    def on_response(resp):
        url = resp.url
        if "/graphql/" in url and ("HomeLatestTimeline" in url or "HomeTimeline" in url):
            try:
                captured.append(resp.json())
            except Exception:
                pass

    page.on("response", on_response)
    try:
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        if "/login" in page.url or "/i/flow" in page.url or "logout" in page.url:
            raise NotLoggedIn()
        try:  # pastikan tab Following (kronologis) yang aktif
            page.get_by_role("tab", name=re.compile(r"^(Following|Mengikuti)$", re.I)).first.click(timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(random.randint(4000, 6000))
        for _ in range(C.SCROLLS_PER_CHECK):
            page.mouse.wheel(0, random.randint(2500, 3500))
            page.wait_for_timeout(random.randint(2000, 3500))
        page.mouse.wheel(0, -20000)  # kembali ke atas
    finally:
        page.remove_listener("response", on_response)

    tweets = []
    for data in captured:
        tweets.extend(parse_timeline_json(data))
    if not captured:
        log("Peringatan: data timeline tidak tertangkap (halaman lambat / tampilan X berubah?).")
    return tweets


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
                process(collect(page), state)
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
    if mode == "backfill":
        hours = float(sys.argv[2]) if len(sys.argv) > 2 else 3
        state = load_state()
        state["since_ms"] = int((time.time() - hours * 3600) * 1000)
        state["seen"] = [i for i in state["seen"] if tweet_time_ms(i) < state["since_ms"]]
        save_state(state)
        return print(f"OK. Tweet dari {hours:g} jam terakhir yang belum terkirim akan dikirim saat run.bat jalan.")

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