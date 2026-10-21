import hashlib
import json
import os
import re
import sys

import requests
from playwright.sync_api import sync_playwright

ROOT = "https://examen.autocontrole.be/"
B_RE = re.compile(r"Examen\s+théorique\s+Cat\.\s*B\s*-\s*AM", re.I)
STATE_FILE = "state_schaerbeek.txt"

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
ID_DATE = os.environ["ID_DATE"]  # date de validité de la carte, ex. JJ/MM/AAAA
TEST = os.environ.get("TEST") == "1"
CARD = (os.environ.get("CARD") or "B").strip()  # "B" = vraie surveillance, "first" = test sur la 1re carte

# Lit le calendrier : pour chaque date, les heures cliquables (boutons) et toutes les heures vues.
JS_CALENDAR = """
() => {
  const out = {};
  let cur = null;
  for (const el of document.querySelectorAll("body *")) {
    if (el.children.length) continue;
    const t = (el.textContent || "").trim();
    if (/^\\d{2}-\\d{2}-\\d{2}$/.test(t)) { cur = t; out[cur] = out[cur] || {btn: [], all: []}; continue; }
    if (cur && /^\\d{2}:\\d{2}$/.test(t)) {
      out[cur].all.push(t);
      if (el.closest("button, a, [role=button], .btn")) out[cur].btn.push(t);
    }
  }
  return out;
}
"""


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


def format_slots(slots):
    lines = [f"{d} : {', '.join(t)}" for d, t in sorted(slots.items(), key=lambda kv: tuple(reversed(kv[0].split("-")))) if t]
    return "\n".join(lines[:30]) or "(aucune heure lue)"


def read_calendar(page):
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(1500)
    data = page.evaluate(JS_CALENDAR)
    slots = {d: v["btn"] for d, v in data.items() if v["btn"]}
    debug = ", ".join(f"{d}:{len(v['btn'])}/{len(v['all'])}" for d, v in list(data.items())[:8])
    return slots, debug


def body_text(page):
    return re.sub(r"\s+", " ", page.inner_text("body").replace("\xa0", " "))


def run():
    err = ""
    slots, debug = {}, ""
    status = "unknown"
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

        try:
            page.get_by_text(re.compile(r"Réserver un rendez-vous", re.I)).last.click(timeout=10000)
            field = page.get_by_placeholder(re.compile(r"par exemple", re.I))
            field.wait_for(state="visible", timeout=15000)
            field.click()
            field.press_sequentially(ID_DATE, delay=60)
            try:
                page.get_by_role("checkbox").first.check(timeout=5000)
            except Exception:
                page.get_by_text(re.compile(r"Je déclare avoir", re.I)).click()
            page.get_by_role("button", name=re.compile(r"Confirmer", re.I)).click()
            page.wait_for_load_state("networkidle", timeout=60000)
        except Exception as e:
            err = "etape carte: " + repr(e)[:200]

        select_btn = page.get_by_text(re.compile(r"^Sélectionner$"))
        try:
            if CARD == "first":
                select_btn.first.click(timeout=10000)
                page.wait_for_load_state("networkidle", timeout=60000)
                page.wait_for_timeout(2000)
                slots, debug = read_calendar(page)
                status = "available" if slots else "none"
            else:
                text = body_text(page)
                m = B_RE.search(text)
                if m:
                    seg = text[m.end():]
                    j = seg.find("Examen")
                    if j != -1:
                        seg = seg[:j]
                    if "Temporairement" in seg:
                        status = "none"
                    else:
                        status = "available"
                        card = (
                            page.locator("div")
                            .filter(has_text=B_RE)
                            .filter(has=select_btn)
                            .last
                        )
                        card.get_by_text(re.compile(r"^Sélectionner$")).click(timeout=10000)
                        page.wait_for_load_state("networkidle", timeout=60000)
                        page.wait_for_timeout(2000)
                        slots, debug = read_calendar(page)
        except Exception as e:
            err += " | selection: " + repr(e)[:200]

        page.screenshot(path="page.png", full_page=False)
        final_url = page.url
        browser.close()

    return status, slots, debug, err, final_url


def main():
    status, slots, debug, err, final_url = run()

    if status == "available":
        sig = hashlib.sha1(json.dumps(slots, sort_keys=True).encode()).hexdigest()[:10]
        state = "available:" + sig
    else:
        state = status
    last = open(STATE_FILE).read().strip() if os.path.exists(STATE_FILE) else ""

    if TEST:
        send(
            f"Test Schaerbeek (carte={CARD}) : statut = {status}\n"
            f"Erreur: {err or 'aucune'}\nURL: {final_url}\n"
            f"Jours (boutons/toutes heures): {debug or '-'}\n\n{format_slots(slots)}"
        )
        send_photo("page.png", "Ce que voit le robot")
    elif state != last:
        if status == "available":
            send(f"🟢 Schaerbeek : places disponibles (théorie B) !\n\n{format_slots(slots)}\n\n{ROOT}")
            send_photo("page.png", "Page au moment de l'alerte")
        elif status == "unknown":
            send(f"⚠️ Schaerbeek : le script ne trouve plus la carte Cat. B. {err}")

    open(STATE_FILE, "w").write(state)
    print("state:", state)


if __name__ == "__main__":
    sys.exit(main())
