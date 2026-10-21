import html
import os
import re
import sys

import requests

URL = "https://examen.autocontrole.be/Home/SelectExamType"
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


def get_status():
    r = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    info = (
        f"HTTP {r.status_code}\n"
        f"URL finale: {r.url}\n"
        f"Longueur HTML: {len(r.text)}\n"
        f"'Cat. B' present: {'Cat. B' in text}\n"
        f"'Temporairement' present: {'Temporairement' in text}\n"
        f"Debut du texte: {text[:400]}"
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
    elif status != last:
        if status == "available":
            send(f"🟢 Des places semblent disponibles à Schaerbeek (théorie B) !\n{URL}")
        elif status == "unknown":
            send("⚠️ Schaerbeek : la page a changé, le script ne trouve plus la carte Cat. B.")

    open(STATE_FILE, "w").write(status)
    print("status:", status)


if __name__ == "__main__":
    sys.exit(main())
