import html
import os
import re
import sys

import requests

URL = "https://examen.autocontrole.be/Home/SelectExamType"
MARK = "Examen théorique Cat. B - AM - A1 - A2 - A"
STATE_FILE = "state_schaerbeek.txt"

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
TEST = os.environ.get("TEST") == "1"


def send(msg):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": msg},
        timeout=20,
    )


def get_status():
    r = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text))
    i = text.find(MARK)
    if i == -1:
        return "unknown"
    seg = text[i + len(MARK):]
    j = seg.find("Examen")
    if j != -1:
        seg = seg[:j]
    return "none" if "Temporairement" in seg else "available"


def main():
    status = get_status()
    last = open(STATE_FILE).read().strip() if os.path.exists(STATE_FILE) else ""

    if TEST:
        send(f"Test Schaerbeek : statut = {status}")
    elif status != last:
        if status == "available":
            send(f"🟢 Des places semblent disponibles à Schaerbeek (théorie B) !\n{URL}")
        elif status == "unknown":
            send("⚠️ Schaerbeek : la page a changé, le script ne trouve plus la carte Cat. B.")

    open(STATE_FILE, "w").write(status)
    print("status:", status)


if __name__ == "__main__":
    sys.exit(main())
