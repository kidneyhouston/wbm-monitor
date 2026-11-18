#!/usr/bin/env python3
"""WBM-Monitor: Prüft Webseiten auf neue Treffer zu "Rathenower Straße"
und benachrichtigt per Push (ntfy) und/oder E-Mail.

Läuft ohne zusätzliche Pakete (nur Python-Standardbibliothek).
Gesteuert über Umgebungsvariablen (in GitHub: "Secrets"):
  NTFY_TOPIC    (Pflicht)    geheimer Themenname für die Push-Nachrichten
  NOTIFY_EMAIL  (optional)   E-Mail-Adresse für Benachrichtigungen
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS (optional)
                             eigener Mailversand (z. B. Gmail mit App-Passwort).
                             Ohne diese Angaben leitet ntfy die Nachricht per Mail weiter.
  SEND_TEST     "true" = Statusbericht senden (wird bei manuellem Start gesetzt)
"""
import hashlib
import html
import json
import os
import re
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

# ---------------------------------------------------------------- Einstellungen

# Was gilt als Treffer? (Regulärer Ausdruck, Groß-/Kleinschreibung egal)
KEYWORD = re.compile(r"Rathenower\s*(?:Stra(?:ß|ss)e|Str\.)", re.I)

# baseline=True : Beim ersten erfolgreichen Abruf werden bereits vorhandene Treffer
#                 still gemerkt (z. B. alte Pressemitteilungen zum Richtfest).
# baseline=False: Jeder Treffer löst sofort eine Benachrichtigung aus.
SOURCES = [
    {"name": "WBM Wohnungsangebote",
     "url": "https://www.wbm.de/wohnungen-berlin/angebote/", "baseline": False},
    {"name": "WBM Pressemitteilungen",
     "url": "https://www.wbm.de/presse/pressemitteilung/", "baseline": True},
    {"name": "WBM Aktuelles",
     "url": "https://www.wbm.de/presse/aktuelles/", "baseline": True},
    {"name": "inberlinwohnen.de",
     "url": "https://www.inberlinwohnen.de/wohnungsfinder/", "baseline": False},
]

# Nach so vielen Fehlversuchen in Folge (bei 10-Minuten-Takt ~ 1 Stunde) kommt eine Warnung.
FAIL_ALERT_AFTER = 6

STATE_FILE = "state.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# ---------------------------------------------------------------- Hilfsfunktionen


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "de-DE,de;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.status, r.read().decode(charset, "replace")


def to_text(raw):
    raw = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw)
    txt = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(txt)).strip()


def find_hits(text):
    hits = {}
    for m in KEYWORD.finditer(text):
        snippet = text[max(0, m.start() - 80): m.end() + 120].strip()
        key = hashlib.sha1(snippet.encode("utf-8")).hexdigest()[:16]
        hits[key] = snippet
    return hits


def send_mail(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["NOTIFY_EMAIL"]
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT") or "465")
    with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, timeout=30) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def notify(title, message, click=None, urgent=True, tag="house"):
    """Schickt Push (ntfy) und ggf. Mail. Gibt True zurück, wenn mind. ein Kanal klappte."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    mail = os.environ.get("NOTIFY_EMAIL", "").strip()
    own_smtp = bool(os.environ.get("SMTP_HOST", "").strip())
    ok = False

    if not topic:
        print("WARNUNG: Secret NTFY_TOPIC ist leer oder nicht gesetzt")
    else:
        payload = {"topic": topic, "title": title, "message": message,
                   "priority": 5 if urgent else 3, "tags": [tag]}
        if click:
            payload["click"] = click
        versuche = []
        if mail and not own_smtp:
            versuche.append({**payload, "email": mail})  # ntfy leitet zusätzlich per Mail weiter
        versuche.append(payload)  # Notlösung: nur Push, ohne Mail
        for p in versuche:
            try:
                req = urllib.request.Request(
                    "https://ntfy.sh", data=json.dumps(p).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=30).read()
                ok = True
                break
            except urllib.error.HTTPError as e:
                antwort = e.read().decode("utf-8", "replace")[:300]
                print(f"ntfy-Fehler: HTTP {e.code} - {antwort}")
            except Exception as e:  # noqa: BLE001
                print(f"ntfy-Fehler: {e}")

    if mail and own_smtp:
        try:
            send_mail(title, message + (f"\n\n{click}" if click else ""))
            ok = True
        except Exception as e:  # noqa: BLE001
            print(f"Mail-Fehler: {e}")

    return ok


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- Hauptprogramm


def main():
    state = load_state()
    first_run = not state
    state.setdefault("seen", {})
    state.setdefault("fails", {})
    state.setdefault("init", [])
    state["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")  # hält Repo "aktiv"

    report = []
    for src in SOURCES:
        name, url = src["name"], src["url"]

        # 1) Seite abrufen
        try:
            status, raw = fetch(url)
            text = to_text(raw)
            if status != 200 or len(text) < 300:
                raise RuntimeError(f"HTTP {status}, nur {len(text)} Zeichen Text")
        except Exception as e:  # noqa: BLE001
            n = state["fails"].get(name, 0) + 1
            state["fails"][name] = n
            report.append(f"✗ {name}: Abruf fehlgeschlagen ({e})")
            if n == FAIL_ALERT_AFTER:
                notify(f"WBM-Monitor: {name} nicht erreichbar",
                       f"{n} Fehlversuche in Folge. Letzter Fehler: {e}\n"
                       "Das Skript muss evtl. angepasst werden.",
                       click=url, urgent=False, tag="warning")
            continue

        if state["fails"].get(name, 0) >= FAIL_ALERT_AFTER:
            notify(f"WBM-Monitor: {name} wieder erreichbar", "Die Überwachung läuft wieder.",
                   click=url, urgent=False, tag="white_check_mark")
        state["fails"][name] = 0

        # 2) Treffer suchen und mit bereits Bekanntem vergleichen
        hits = find_hits(text)
        seen = state["seen"].setdefault(name, [])
        new = {k: v for k, v in hits.items() if k not in seen}

        if src["baseline"] and name not in state["init"]:
            state["init"].append(name)
            seen.extend(new.keys())
            report.append(f"✓ {name}: erreichbar, {len(hits)} bestehende Treffer still gemerkt")
            continue

        for key, snippet in new.items():
            if notify(f"WBM: Neuer Treffer auf {name}", f"…{snippet}…", click=url):
                seen.append(key)
        report.append(f"✓ {name}: erreichbar ({len(text)} Zeichen), "
                      f"{len(hits)} Treffer, davon {len(new)} neu")

    save_state(state)

    print("\n".join(report))
    if first_run or os.environ.get("SEND_TEST", "").lower() == "true":
        notify("WBM-Monitor: Statusbericht", "\n".join(report), urgent=False, tag="mag")


if __name__ == "__main__":
    main()
