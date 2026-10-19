"""
X (Twitter) -> Telegram group/topic auto-poster, dijalankan oleh GitHub Actions.
Hanya tweet bermedia (foto/video) tentang karina/winter yang dikirim.

Mode:
  python x_to_telegram.py            # run normal (dipanggil terjadwal)
  python x_to_telegram.py test       # kirim pesan uji ke Telegram
  python x_to_telegram.py diagnose   # cek kondisi endpoint X
  python x_to_telegram.py reset      # hapus state (mulai ulang dari tweet terbaru)
"""
import json
import os
import re
import sys
import time

import requests

import config as C

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
MAX_UPLOAD_BYTES = 49 * 1024 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
DEFAULT_TOPIC = os.environ.get("TOPIC_ID", "").strip()

# Daftar akun: variable ACCOUNTS (GitHub) kalau diisi, kalau tidak pakai config.py
_env_accounts = [a.strip().lstrip("@") for a in os.environ.get("ACCOUNTS", "").split(",") if a.strip()]
ACCOUNTS = _env_accounts or C.ACCOUNTS


# ============================ STATE ============================

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("last_ids", {})
    s.setdefault("cooldown_until", 0)
    return s


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, sort_keys=True)
        f.write("\n")


def cmp_id(a, b):
    """Bandingkan ID tweet (string angka besar)."""
    return (int(a) > int(b)) - (int(a) < int(b))


# ============================ X / SYNDICATION ============================

def syndication_urls(user):
    return [
        f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{user}",
        f"https://cdn.syndication.twimg.com/timeline/profile?screen_name={user}",
    ]


def parse_timeline(text):
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', text, re.S)
    try:
        data = json.loads(m.group(1) if m else text)
    except (json.JSONDecodeError, AttributeError):
        return []
    entries = (data.get("props", {}).get("pageProps", {}).get("timeline", {}).get("entries")
               or data.get("entries") or [])
    out = []
    for e in entries:
        t = (e or {}).get("content", {}).get("tweet")
        if t and t.get("id_str"):
            out.append(t)
    return out


def fetch_timeline(user):
    """Return (tweets, rate_limited)."""
    got429 = False
    for url in syndication_urls(user):
        try:
            r = requests.get(url, timeout=30, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/json",
                "Accept-Language": "en-US,en;q=0.9",
            })
        except requests.RequestException as e:
            print(f"[{user}] error koneksi ke {url.split('/')[2]}: {e}")
            continue
        if r.status_code != 200:
            print(f"[{user}] HTTP {r.status_code} dari {url.split('/')[2]}")
            got429 |= r.status_code == 429
            continue
        tweets = parse_timeline(r.text)
        if tweets:
            return tweets, False
        print(f"[{user}] respons kosong dari {url.split('/')[2]}")
    return [], got429


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
    hashtags = " ".join("#" + h.get("text", "") for h in src.get("entities", {}).get("hashtags", []))
    hay = _norm((src.get("full_text") or src.get("text") or "") + " " + hashtags)
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
            vs = [v for v in m.get("video_info", {}).get("variants", []) if v.get("content_type") == "video/mp4"]
            vs.sort(key=lambda v: v.get("bitrate", 0), reverse=True)
            if vs:
                out.append({"kind": "video", "url": vs[0]["url"]})
    return out


def download(item, base, idx):
    try:
        r = requests.get(item["url"], timeout=120, headers={"User-Agent": UA})
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

def _with_topic(data, topic):
    t = str(topic or DEFAULT_TOPIC or "").strip()
    if t and t != "1" and t.lower() != "general":  # General tidak boleh diberi thread id
        data["message_thread_id"] = t
    return data


def tg(method, data, files=None, topic=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = _with_topic(dict(data), topic)
    for attempt in range(3):
        try:
            r = requests.post(url, data=data, files=files, timeout=180)
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"Telegram {method} error: {e}")
            return False
        if body.get("ok"):
            return True
        retry = body.get("parameters", {}).get("retry_after")
        if retry and attempt < 2:  # Telegram minta tunggu
            time.sleep(int(retry) + 1)
            continue
        print(f"Telegram {method} gagal: {r.text[:300]}")
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

    ok = True
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
        ok = tg("sendMediaGroup", {"chat_id": CHAT_ID, "media": json.dumps(media)}, files=files, topic=topic)
        if not ok:
            break
    return ok


def send_tweet(t, src, media, user, topic):
    is_rt = bool(t.get("retweeted_status"))
    src_user = (src.get("user") or {}).get("screen_name") or user
    text = re.sub(r"\s*https://t\.co/\w+\s*$", "", src.get("full_text") or src.get("text") or "").strip()
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


# ============================ CORE ============================

def process_account(user, state):
    """Return True kalau kena rate limit."""
    key = user.lower()
    last_id = state["last_ids"].get(key)

    tweets, limited = fetch_timeline(user)
    if not tweets:
        print(f"[{user}] tidak ada data")
        return limited

    tweets.sort(key=lambda t: int(t["id_str"]))
    newest = tweets[-1]["id_str"]
    if not last_id:
        state["last_ids"][key] = newest
        print(f"[{user}] inisialisasi, last_id = {newest}")
        return False

    fresh = [t for t in tweets if cmp_id(t["id_str"], last_id) > 0]
    sent = skipped = filtered = 0
    for t in fresh:
        if sent >= C.MAX_POSTS_PER_RUN:
            break
        is_rt = bool(t.get("retweeted_status"))
        src = t["retweeted_status"] if is_rt else t
        media = [] if (is_rt and not C.INCLUDE_RETWEETS) else extract_media(src)
        if not media:
            state["last_ids"][key] = t["id_str"]
            skipped += 1
            continue

        ok_send, matched, topic = classify(src, user)
        if not ok_send:
            print(f"[{user}] skip {src['id_str']} (member: {','.join(matched) or '-'})")
            state["last_ids"][key] = t["id_str"]
            filtered += 1
            continue

        if not send_tweet(t, src, media, user, topic):
            print(f"[{user}] gagal kirim {t['id_str']}, dicoba lagi run berikutnya")
            break
        state["last_ids"][key] = t["id_str"]
        sent += 1
        time.sleep(C.DELAY_BETWEEN_POSTS)

    print(f"[{user}] baru: {len(fresh)}, terkirim: {sent}, skip tanpa media: {skipped}, "
          f"skip bukan karina/winter: {filtered}")
    return False


def run():
    state = load_state()
    now = time.time()
    if state["cooldown_until"] > now:
        print(f"Masih jeda rate limit X, sisa {int((state['cooldown_until'] - now) / 60)} menit. Lewati.")
        return
    for acc in ACCOUNTS:
        acc = acc.strip().lstrip("@")
        if not acc:
            continue
        if process_account(acc, state):
            state["cooldown_until"] = now + C.COOLDOWN_MINUTES * 60
            print(f"Kena rate limit X (429). Jeda {C.COOLDOWN_MINUTES} menit.")
            break
        save_state(state)  # simpan per akun supaya progres tidak hilang
        time.sleep(1.5)
    save_state(state)


def diagnose():
    user = ACCOUNTS[0]
    for url in syndication_urls(user):
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": UA})
            print(f"{url.split('/')[2]} -> HTTP {r.status_code} | {len(r.text)} byte | "
                  f"tweet: {len(parse_timeline(r.text))} | "
                  f"rate-limit-reset: {r.headers.get('x-rate-limit-reset', '-')} | "
                  f"retry-after: {r.headers.get('retry-after', '-')}")
        except requests.RequestException as e:
            print(f"{url.split('/')[2]} -> error: {e}")


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "run").lower()
    if mode in ("run", "test") and (not BOT_TOKEN or not CHAT_ID):
        sys.exit("BOT_TOKEN / CHAT_ID belum diisi di GitHub Secrets.")
    if mode == "test":
        print("Berhasil." if tg("sendMessage", {"chat_id": CHAT_ID, "text": "✅ Bot aktif (GitHub Actions)."},
                                topic=C.BOTH_TOPIC) else "Gagal, cek secrets / admin bot.")
    elif mode == "diagnose":
        diagnose()
    elif mode == "reset":
        save_state({"last_ids": {}, "cooldown_until": 0})
        print("State direset.")
    else:
        run()


if __name__ == "__main__":
    main()
