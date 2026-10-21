import os
import re
import sys

import requests
from playwright.sync_api import sync_playwright

ROOT = "https://examen.autocontrole.be/"
URL = ROOT + "Home/SelectExamType"
PATTERN = re.compile(r"Examen\s+théorique\s+Cat\.\s*B\s*-\s*AM", re.I)
STATE_FILE = "state_schaerbeek.txt"

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
TEST = os.environ.get("TEST") == "1"


def send(msg):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": msg[:3900]},
        timeout=20,
    )


def send_photo(path, caption=""):
    with open(path, "rb") as f:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
            data={"chat_id": CHAT_ID, "caption": caption[:900]},
            files={"photo": f},
            timeout=60,
        )


def get_status():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="fr-BE", viewport={"width": 430, "height": 900})
        page = ctx.new_page()

        page.goto(ROOT, wait_until="networkidle", timeout=60000)
        try:
            page.get_by_text("FR", exact=True).first.click(timeout=5000)
            page.wait_for_load_state("networkidle")
        except Exception:
            pass

        page.goto(URL, wait_until="networkidle", timeout=60000)
        if "SelectExamType" not in page.url:
            try:
                page.get_by_text(re.compile(r"rendez-vous|afspraak", re.I)).first.click(timeout=5000)
                page.wait_for_load_state("networkidle")
            except Exception:
                pass

        page.screenshot(path="page.png", full_page=True)
        text = re.sub(r"\s+", " ", page.inner_text("body").replace("\xa0", " "))
        final_url = page.url
        browser.close()

    info = (
        f"URL finale: {final_url}\n"
        f"'Cat. B' present: {'Cat. B' in text}\n"
        f"'Temporairement' present: {'Temporairement' in text}\n"
        f"Debut: {text[:300]}"
    )

    m = PATTERN.search(text)
    if not m:
        return "unknown", info
    seg = text[m.end():]
    j = seg.find("Examen")
    if j != -1:
        seg = seg[:j]
    return ("none" if "Temporairement" in seg else "available"), info


def main():
    status, info = get_status()
    last = open(STATE_FILE).read().strip() if os.path.exists(STATE_FILE) else ""

    if TEST:
        send(f"Test Schaerbeek : statut = {status}\n\n{info}")
        send_photo("page.png", "Ce que voit le robot")
    elif status != last:
        if status == "available":
            send(f"🟢 Des places semblent disponibles à Schaerbeek (théorie B) !\n{URL}")
            send_photo("page.png", "Page au moment de l'alerte")
        elif status == "unknown":
            send("⚠️ Schaerbeek : le script ne trouve plus la carte Cat. B (page changée ?).")

    open(STATE_FILE, "w").write(status)
    print("status:", status)


if __name__ == "__main__":
    sys.exit(main())
