"""
Uji coba: apakah twitterviewer.net bisa menampilkan tweet di browser headless GitHub Actions?
Browser biasa (tanpa trik penyamaran). Hasil + screenshot dikirim ke Telegram.
"""
import os
import time

import requests
from playwright.sync_api import sync_playwright

USER = os.environ.get("TEST_USER", "").strip() or "aespa_official"
BOT = os.environ.get("BOT_TOKEN", "").strip()
CHAT = os.environ.get("CHAT_ID", "").strip()
TOPIC = os.environ.get("TOPIC_ID", "").strip()
SHOT = "viewer.png"


def tg(method, data, files=None):
    if TOPIC and TOPIC != "1":
        data["message_thread_id"] = TOPIC
    r = requests.post(f"https://api.telegram.org/bot{BOT}/{method}", data=data, files=files, timeout=60)
    print(method, r.status_code, r.text[:200])


def main():
    api_calls = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1800}, locale="en-US")

        def on_response(resp):
            if "GetUserTimeline" in resp.url:
                h = resp.headers
                api_calls.append(f"HTTP {resp.status} | anti-bot-reason: {h.get('x-anti-bot-reason', '-')} "
                                 f"| uses-left: {h.get('x-anti-bot-uses-left', '-')}")

        page.on("response", on_response)
        t0 = time.time()
        error = ""
        try:
            page.goto(f"https://twitterviewer.net/{USER}", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector('a[href*="/status/"], img[src*="pbs.twimg.com/media"]', timeout=45000)
        except Exception as e:
            error = f"{type(e).__name__}"
        page.wait_for_timeout(4000)

        links = page.eval_on_selector_all(
            'a[href*="/status/"]', "els => [...new Set(els.map(e => e.href))]")
        imgs = page.eval_on_selector_all(
            "img", "els => [...new Set(els.map(e => e.currentSrc || e.src))].filter(s => /pbs\\.twimg\\.com\\/media/.test(s))")
        videos = page.eval_on_selector_all(
            "video, video source", "els => els.map(e => e.currentSrc || e.src).filter(Boolean)")
        turnstile = page.locator('iframe[src*="challenges.cloudflare.com"]').count()
        title = page.title()
        page.screenshot(path=SHOT)
        browser.close()

    lines = [
        "🧪 Uji twitterviewer.net (GitHub Actions, headless)",
        f"akun: {USER} | judul: {title} | {time.time() - t0:.0f} detik",
        f"tunggu tweet: {'OK' if not error else 'gagal (' + error + ')'}",
        f"API GetUserTimeline: {' ; '.join(api_calls) or 'tidak terpanggil'}",
        f"link tweet: {len(links)} | foto pbs.twimg: {len(imgs)} | video: {len(videos)} | iframe Turnstile: {turnstile}",
    ]
    if links:
        lines.append("contoh link: " + links[0])
    if imgs:
        lines.append("contoh foto: " + imgs[0])
    report = "\n".join(lines)
    print(report)

    if BOT and CHAT:
        tg("sendMessage", {"chat_id": CHAT, "text": report})
        with open(SHOT, "rb") as f:
            tg("sendPhoto", {"chat_id": CHAT, "caption": "Screenshot browser GitHub"}, files={"photo": f})


if __name__ == "__main__":
    main()
