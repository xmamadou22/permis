import html as htmllib
import json
import os
import re
import sys
import time
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

START = "https://www.securiteautomobile.be/Rdv_pc"
STATE_FILE = "state_anderlecht.txt"
CENTER = "Anderlecht"

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
TEST = os.environ.get("TEST") == "1"

PRENOM = os.environ.get("AND_PRENOM", "").strip()
NOM = os.environ.get("AND_NOM", "").strip()
NAISSANCE = os.environ.get("AND_NAISSANCE", "").strip()  # JJ/MM/AAAA
RRN = re.sub(r"\D", "", os.environ.get("AND_RRN", ""))  # 11 chiffres

NO_PLACE_RE = re.compile(r"plus\s+de\s+places?", re.I)

# Lit le calendrier de façon générique : dates (formats courants) + heures dans des boutons.
JS_CALENDAR = r"""
() => {
  const own = (el) => el.tagName === "INPUT"
    ? (el.value || "").trim()
    : Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join("").trim();
  const months = "janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre";
  const dateNum = /^\d{1,2}[\/.-]\d{1,2}[\/.-]\d{2,4}$/;
  const dateTxt = new RegExp("^(?:\\p{L}+\\.?\\s+)?\\d{1,2}\\s+(?:" + months + ")\\.?(?:\\s+\\d{4})?$", "iu");
  const timeRe = /^\d{1,2}[:h]\d{2}$/;
  const out = {};
  let cur = "Date inconnue";
  for (const el of document.querySelectorAll("body *")) {
    const t = own(el);
    if (!t || t.length > 40) continue;
    if (dateNum.test(t) || dateTxt.test(t)) { cur = t; continue; }
    if (timeRe.test(t)) {
      const b = el.closest("button, a, [role=button], .btn, input[type=button], input[type=submit], input[type=radio], label");
      if (b && !b.disabled && !b.classList.contains("disabled")) {
        (out[cur] = out[cur] || []).push(t.replace("h", ":"));
      }
    }
  }
  return out;
}
"""

JOURS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
DATE_FORMATS = ["%d-%m-%y", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d.%m.%Y"]


def parse_date(d):
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(d, f)
        except ValueError:
            pass
    return None


def date_key(d):
    return (parse_date(d) or datetime.max, d)


def day_label(d):
    dt = parse_date(d)
    return f"{JOURS[dt.weekday()]} {dt.strftime('%d/%m')}" if dt else d


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


def day_lines(slots, cap=25):
    days = [(d, ts) for d, ts in sorted(slots.items(), key=lambda kv: date_key(kv[0])) if ts]
    lines = [
        f"📅 <b>{htmllib.escape(day_label(d))}</b> — " + "  ·  ".join(htmllib.escape(t) for t in sorted(ts))
        for d, ts in days[:cap]
    ]
    if len(days) > cap:
        lines.append(f"… et {len(days) - cap} jour(s) de plus")
    return "\n".join(lines) or "(aucune heure lue)"


def pairs_of(slots):
    return {(d, t) for d, ts in slots.items() for t in ts}


def build_alert(slots, new_slots, prefix=""):
    n_slots = sum(len(v) for v in slots.values())
    n_days = sum(1 for v in slots.values() if v)
    head = (
        f"{prefix}🟢 <b>Places disponibles à {CENTER}</b>\n"
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
    foot = f'\n\n👉 <a href="{START}">Réserver maintenant</a>\n<i>Les créneaux partent vite.</i>'
    return head + body + foot


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"status": "", "slots": {}}


def body_text(page):
    return re.sub(r"\s+", " ", page.inner_text("body").replace("\xa0", " "))


def try_click(page, pattern, timeout=3000):
    """Clique si l'élément est là (popup, cookies...), sinon continue sans erreur."""
    try:
        page.get_by_text(pattern).first.click(timeout=timeout)
        return True
    except Exception:
        return False


def type_into(locator, value):
    locator.click()
    locator.press_sequentially(value, delay=70)


def first_visible(page, pattern, timeout=15):
    """Premier élément visible dont le texte correspond (ignore les doublons cachés)."""
    end = time.time() + timeout
    while time.time() < end:
        loc = page.get_by_text(pattern)
        for i in range(loc.count()):
            if loc.nth(i).is_visible():
                return loc.nth(i)
        time.sleep(0.5)
    raise RuntimeError(f"élément introuvable ou caché : {pattern.pattern}")


def wait_for_booking_page(ctx, timeout=30):
    """Attend que la page de réservation (booking.exam.s-a.be) soit ouverte, même dans un nouvel onglet."""
    end = time.time() + timeout
    while time.time() < end:
        for pg in ctx.pages:
            if "exam.s-a.be" in pg.url:
                pg.wait_for_load_state("networkidle", timeout=60000)
                return pg
        time.sleep(0.5)
    urls = " | ".join(pg.url[:80] for pg in ctx.pages)
    raise RuntimeError(f"la page de réservation ne s'est pas ouverte (onglets : {urls})")


def run():
    status, slots, err, excerpt = "unknown", {}, "", ""
    step = "démarrage"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="fr-BE", viewport={"width": 430, "height": 900})
        page = ctx.new_page()
        try:
            step = "ouverture du site"
            page.goto(START, wait_until="networkidle", timeout=60000)

            step = "popup « J'ai compris »"
            try:
                page.get_by_text(re.compile(r"J.ai compris", re.I)).first.click(timeout=8000)
                page.wait_for_timeout(1200)  # fin de l'animation de fermeture
            except Exception:
                pass

            step = "bouton « Prendre un RDV »"
            target = first_visible(page, re.compile(r"Prendre un RDV", re.I))
            href = target.evaluate("el => { const a = el.closest('a'); return a ? a.href : ''; }")
            if href.startswith("http") and not href.rstrip("/").endswith("#"):
                page.goto(href, wait_until="networkidle", timeout=60000)
            else:
                target.click(timeout=10000)
            page = wait_for_booking_page(ctx, 30)

            step = "cookies"
            try_click(page, re.compile(r"Accepter et fermer", re.I), timeout=5000)

            step = "identité (prénom, nom)"
            prenom = page.get_by_label(re.compile(r"Pr[ée]nom", re.I)).first
            nom = page.get_by_label(re.compile(r"^\s*Nom", re.I)).first
            try:
                prenom.wait_for(state="visible", timeout=15000)
            except Exception:
                inputs = page.locator("input:visible")
                prenom, nom = inputs.nth(0), inputs.nth(1)
            type_into(prenom, PRENOM)
            type_into(nom, NOM)

            step = "date de naissance"
            jj, mm, aaaa = re.split(r"\D+", NAISSANCE)[:3]
            type_into(page.get_by_placeholder("JJ"), jj)
            type_into(page.get_by_placeholder("MM"), mm)
            type_into(page.get_by_placeholder("AAAA"), aaaa)

            step = "numéro de registre national"
            rrn = page.get_by_label(re.compile(r"registre national", re.I)).first
            try:
                rrn.wait_for(state="visible", timeout=3000)
            except Exception:
                rrn = page.locator("input:visible").nth(5)
            type_into(rrn, RRN)

            step = "bouton Continuer"
            page.get_by_text(re.compile(r"^\s*Continuer", re.I)).first.click(timeout=10000)
            page.wait_for_load_state("networkidle", timeout=60000)
            page.wait_for_timeout(1500)
            if re.search(r"est invalide|invalide\.", body_text(page), re.I) and page.get_by_placeholder("JJ").count():
                raise RuntimeError("identité refusée par le site (vérifie les secrets AND_*)")

            step = "choix de la catégorie B"
            page.get_by_text(re.compile(r"Catégorie B", re.I)).first.click(timeout=15000)
            page.wait_for_timeout(1000)

            step = "choix « Théorie B »"
            page.get_by_text(re.compile(r"^\s*Théorie B\s*$", re.I)).first.click(timeout=15000)
            page.wait_for_load_state("networkidle", timeout=60000)
            page.wait_for_timeout(2500)

            step = "lecture du calendrier"
            text = body_text(page)
            if NO_PLACE_RE.search(text):
                status = "none"
            else:
                status = "available"
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)
                slots = page.evaluate(JS_CALENDAR)
        except Exception as e:
            err = f"étape « {step} » : {repr(e)[:220]}"
            try:
                excerpt = body_text(page)[:250]  # texte visible uniquement (pas les champs saisis)
            except Exception:
                pass
        browser.close()
    return status, slots, err, excerpt


def main():
    missing = [n for n, v in [("AND_PRENOM", PRENOM), ("AND_NOM", NOM), ("AND_NAISSANCE", NAISSANCE), ("AND_RRN", RRN)] if not v]
    if missing:
        print("Secrets manquants :", ", ".join(missing))
        return 0
    if len(RRN) != 11 or len(re.split(r"\D+", NAISSANCE)) < 3:
        print("AND_RRN doit avoir 11 chiffres et AND_NAISSANCE le format JJ/MM/AAAA")
        return 0

    status, slots, err, excerpt = run()
    last = load_state()
    key = status if (status != "available" or slots) else "available_noread"
    new = pairs_of(slots) - pairs_of(last.get("slots", {}))
    new_slots = {}
    for d, t in sorted(new):
        new_slots.setdefault(d, []).append(t)

    if TEST:
        if key == "available":
            send(build_alert(slots, {}, prefix="🧪 <i>Test Anderlecht</i>\n"))
        elif key == "none":
            send("🧪 Test Anderlecht : statut = <b>none</b> ✅\nToutes les étapes sont passées, pas de place pour l'instant.")
        else:
            msg = f"🧪 Test Anderlecht : statut = <b>{key}</b>"
            if err:
                msg += f"\n❌ {htmllib.escape(err)}"
            if excerpt:
                msg += f"\n\nTexte de la page : <i>{htmllib.escape(excerpt)}</i>"
            send(msg)
    elif key == "available" and new:
        send(build_alert(slots, new_slots))
    elif key == "available_noread" and last.get("status") != key:
        send(
            f"🟡 <b>{CENTER}</b> : des places semblent disponibles, mais je n'ai pas pu lire les heures.\n"
            f'👉 <a href="{START}">Ouvrir le site</a>'
        )
    elif key == "unknown" and last.get("status") != "unknown":
        send(f"⚠️ <b>{CENTER}</b> : le script est bloqué.\n{htmllib.escape(err)}")

    if not TEST:  # un test ne doit pas marquer des créneaux comme « déjà vus »
        json.dump({"status": key, "slots": slots}, open(STATE_FILE, "w"))
    print("state:", key, "nouveaux:", len(new))
    return 0


if __name__ == "__main__":
    sys.exit(main())
