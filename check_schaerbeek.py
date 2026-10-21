import html as htmllib
import json
import os
import random
import re
import sys
import time
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

ROOT = "https://examen.autocontrole.be/"
B_RE = re.compile(r"Examen\s+théorique\s+Cat\.\s*B\s*-\s*AM", re.I)
STATE_FILE = "state_schaerbeek.txt"

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
ID_DATE = os.environ["ID_DATE"]  # date de validité de la carte, ex. JJ/MM/AAAA
TEST = os.environ.get("TEST") == "1"
CARD = (os.environ.get("CARD") or "B").strip()  # "B" = vraie surveillance, "first" = 1re carte, ou ex. "C1" pour tester
TARGET_RE = (
    B_RE
    if CARD.upper() in ("B", "FIRST")
    else re.compile(r"Examen\s+théorique\s+Cat\.\s*" + re.escape(CARD), re.I)
)

TYPES_URL = ROOT + "Home/SelectExamType"
WATCH = os.environ.get("WATCH") == "1"  # mode surveillance continue (une seule session qu'on recharge)
WATCH_SECONDS = int(os.environ.get("WATCH_SECONDS") or 20700)
WATCH_INTERVAL = float(os.environ.get("WATCH_INTERVAL") or 15)  # secondes entre deux rechargements
SESSION_MAX = 1800  # on repart d'une session neuve au moins toutes les 30 min
FAILS_BEFORE_ALERT = 3  # on n'envoie « bloqué » qu'après 3 échecs d'affilée

# Calendrier : pour chaque date, heures cliquables (boutons) et toutes les heures vues.
# On lit le texte propre de chaque élément (robuste si le bouton contient une icône).
JS_CALENDAR = r"""
() => {
  const own = (el) => el.tagName === "INPUT"
    ? (el.value || "").trim()
    : Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join("").trim();
  const out = {};
  let cur = null;
  for (const el of document.querySelectorAll("body *")) {
    const t = own(el);
    if (/^\d{2}-\d{2}-\d{2}$/.test(t)) { cur = t; out[cur] = out[cur] || {btn: [], all: []}; continue; }
    if (cur && /^\d{2}:\d{2}$/.test(t)) {
      out[cur].all.push(t);
      const b = el.closest("button, a, [role=button], .btn, input[type=button], input[type=submit], input[type=radio]");
      if (b && !b.disabled && !b.classList.contains("disabled")) out[cur].btn.push(t);
    }
  }
  return out;
}
"""

def send(msg):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": msg[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=20,
    )


JOURS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]


def date_key(d):
    try:
        return datetime.strptime(d, "%d-%m-%y")
    except ValueError:
        return datetime.max


def day_label(d):
    try:
        dt = datetime.strptime(d, "%d-%m-%y")
        return f"{JOURS[dt.weekday()]} {dt.strftime('%d/%m')}"
    except ValueError:
        return d


def day_lines(slots, cap=25):
    days = [(d, ts) for d, ts in sorted(slots.items(), key=lambda kv: date_key(kv[0])) if ts]
    lines = [
        f"📅 <b>{htmllib.escape(day_label(d))}</b> — " + "  ·  ".join(htmllib.escape(t) for t in sorted(ts))
        for d, ts in days[:cap]
    ]
    if len(days) > cap:
        lines.append(f"… et {len(days) - cap} jour(s) de plus")
    return "\n".join(lines) or "(aucune heure lue)"


def build_alert(slots, new_slots, prefix=""):
    n_slots = sum(len(v) for v in slots.values())
    n_days = sum(1 for v in slots.values() if v)
    head = (
        f"{prefix}🟢 <b>Places disponibles à Schaerbeek</b>\n"
        f"🚗 Examen théorique · permis B\n"
        f"🕒 {n_slots} créneau(x) sur {n_days} jour(s)\n\n"
    )
    if new_slots and pairs_of(new_slots) != pairs_of(slots):
        body = (
            "🆕 <b>Nouveaux créneaux</b>\n" + day_lines(new_slots, 12)
            + "\n\n📋 <b>Tous les créneaux</b>\n" + day_lines(slots, 12)
        )
    else:
        body = "📋 <b>Créneaux disponibles</b>\n" + day_lines(slots)
    foot = f'\n\n👉 <a href="{ROOT}">Réserver maintenant</a>\n<i>Les créneaux partent vite.</i>'
    return head + body + foot


def read_calendar(page):
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(1500)
    data = page.evaluate(JS_CALENDAR)
    slots = {d: v["btn"] for d, v in data.items() if v["btn"]}
    debug = ", ".join(f"{d}:{len(v['btn'])}/{len(v['all'])}" for d, v in data.items() if v["all"] or v["btn"])
    debug = debug or f"{len(data)} jours lus, aucune heure"
    html = ""
    return slots, debug, html


def body_text(page):
    return re.sub(r"\s+", " ", page.inner_text("body").replace("\xa0", " "))


def new_context(browser):
    return browser.new_context(locale="fr-BE", viewport={"width": 430, "height": 900})


def login(page):
    """Accueil -> Réserver -> date de validité de la carte -> Confirmer -> page des types d'examen."""
    page.goto(ROOT, wait_until="networkidle", timeout=60000)
    try:
        page.get_by_text("FR", exact=True).first.click(timeout=5000)
        page.wait_for_load_state("networkidle")
    except Exception:
        pass

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


def on_types_page(page):
    return "SelectExamType" in page.url


def read_types(page):
    """Lit la page des types d'examen ; si la carte cible est disponible, ouvre le calendrier et lit les heures."""
    err = ""
    slots, debug = {}, ""
    status = "unknown"
    select_btn = page.get_by_text(re.compile(r"^Sélectionner$"))
    try:
        if CARD == "first":
            select_btn.first.click(timeout=10000)
            page.wait_for_load_state("networkidle", timeout=60000)
            page.wait_for_timeout(2000)
            slots, debug, _ = read_calendar(page)
            status = "available" if slots else "none"
        else:
            text = body_text(page)
            m = TARGET_RE.search(text)
            if m:
                seg = text[m.end():]
                j = seg.find("Examen")
                if j != -1:
                    seg = seg[:j]
                if "Temporairement" in seg:
                    status = "none"
                else:
                    status = "available"
                    card = page.locator("div").filter(has_text=TARGET_RE).filter(has=select_btn).last
                    card.get_by_text(re.compile(r"^Sélectionner$")).click(timeout=10000)
                    page.wait_for_load_state("networkidle", timeout=60000)
                    page.wait_for_timeout(2000)
                    slots, debug, _ = read_calendar(page)
    except Exception as e:
        err = "selection: " + repr(e)[:200]
    return status, slots, debug, err


def run():
    """Un seul passage complet (utilisé pour les tests)."""
    err = ""
    status, slots, debug = "unknown", {}, ""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = new_context(browser)
        page = ctx.new_page()
        try:
            login(page)
        except Exception as e:
            err = "etape carte: " + repr(e)[:200]
        status, slots, debug, err2 = read_types(page)
        err = (err + " | " + err2).strip(" |") if err2 else err
        browser.close()
    return status, slots, debug, "", err, ""


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"status": "", "slots": {}}


def pairs_of(slots):
    return {(d, t) for d, ts in slots.items() for t in ts}


def handle(status, slots, err):
    """Alerte (si besoin) et mémorisation de l'état. Jamais appelée pour un simple test."""
    last = load_state()
    new = pairs_of(slots) - pairs_of(last.get("slots", {}))
    key = status if (status != "available" or slots) else "available_noread"

    new_slots = {}
    for d, t in sorted(new):
        new_slots.setdefault(d, []).append(t)

    if key == "available" and new:
        send(build_alert(slots, new_slots))
    elif key == "available_noread" and last.get("status") != key:
        send(
            "🟡 <b>Schaerbeek</b> : des places semblent disponibles, mais je n'ai pas pu lire les heures.\n"
            f'👉 <a href="{ROOT}">Ouvrir le site</a>'
        )
    elif key == "unknown" and last.get("status") != "unknown":
        send(f"⚠️ <b>Schaerbeek</b> : le script ne trouve plus la carte Cat. B. {htmllib.escape(err)}")

    json.dump({"status": key, "slots": slots}, open(STATE_FILE, "w"))
    return key, len(new)


def main():
    status, slots, debug, html, err, final_url = run()
    if TEST:
        key = status if (status != "available" or slots) else "available_noread"
        if key == "available":
            send(build_alert(slots, {}, prefix=f"🧪 <i>Test (carte {htmllib.escape(CARD)})</i>\n"))
        else:
            extra = f"\nErreur : {htmllib.escape(err)}" if err else ""
            send(f"🧪 Test Schaerbeek (carte {htmllib.escape(CARD)}) : statut = <b>{status}</b>{extra}")
        return 0
    key, n_new = handle(status, slots, err)
    print("state:", key, "nouveaux:", n_new)
    return 0


def watch():
    """Reste sur la même session : on se reconnecte seulement si la session est perdue."""
    end = time.time() + WATCH_SECONDS
    logins = 0
    fails = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = page = None
        started = 0.0

        def fresh():
            nonlocal ctx, page, started, logins
            if ctx:
                try:
                    ctx.close()
                except Exception:
                    pass
            ctx = new_context(browser)
            page = ctx.new_page()
            login(page)
            started = time.time()
            logins += 1

        while time.time() < end:
            t0 = time.time()
            try:
                relogged = False
                if page is None or time.time() - started > SESSION_MAX:
                    fresh()
                    relogged = True
                else:
                    page.goto(TYPES_URL, wait_until="networkidle", timeout=60000)  # « rafraîchir »
                    if not on_types_page(page):
                        fresh()
                        relogged = True

                status, slots, debug, err = read_types(page)
                if (status == "unknown" or (status == "available" and not slots)) and not relogged:
                    fresh()  # deuxième avis avec une session neuve avant d'alerter
                    status, slots, debug, err = read_types(page)

                if status == "unknown":
                    fails += 1
                    if fails >= FAILS_BEFORE_ALERT:
                        handle(status, slots, err)
                    print(time.strftime("%H:%M:%S"), "lecture impossible", fails, "/", FAILS_BEFORE_ALERT, flush=True)
                    page = None  # on repart d'une session neuve
                else:
                    fails = 0
                    key, n_new = handle(status, slots, err)
                    print(time.strftime("%H:%M:%S"), "etat:", key, "| nouveaux:", n_new, "| connexions:", logins, flush=True)
            except Exception as e:
                page = None
                fails += 1
                if fails >= FAILS_BEFORE_ALERT:
                    handle("unknown", {}, "session : " + repr(e)[:200])
                print(time.strftime("%H:%M:%S"), "erreur de session", fails, "/", FAILS_BEFORE_ALERT, ": nouvelle connexion dans 45 s", flush=True)
                time.sleep(45)
                continue
            time.sleep(max(2.0, WATCH_INTERVAL * random.uniform(0.8, 1.2) - (time.time() - t0)))
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(watch() if WATCH and not TEST else main())
