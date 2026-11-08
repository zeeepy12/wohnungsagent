import os, re, json, hashlib, imaplib, email, smtplib, datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
import anthropic

MODEL = "claude-haiku-5-5"
STATE_FILE = Path("state/seen.json")
CFG = json.loads(Path("config.json").read_text(encoding="utf-8"))
client = anthropic.Anthropic()

SYSTEM = f"""Du extrahierst Mietwohnungs-Inserate aus dem gegebenen Text und bewertest sie.
Kriterien: {json.dumps(CFG['kriterien'], ensure_ascii=False)}
Heutiges Datum: {datetime.date.today().isoformat()}.
Harte Kriterien: Stadt, max. Kaltmiete, Zimmer, Fläche, Stadtteil, Muss-Merkmale und der Bezugstermin:
Die Wohnung muss frühestens 2 Monate nach heute beziehbar sein. "Ab sofort", "sofort frei" oder ein Termin
in weniger als 2 Monaten (z. B. in 2 Wochen) bedeutet urteil=nein. Fehlt jede Angabe zum Bezug, ist das "unklar".
Weiche Kriterien: hell, ruhig (aus Beschreibung/Lage ableiten).
Antworte NUR mit einem JSON-Array, ohne Text drumherum. Pro Inserat ein Objekt:
{{"id": "URL oder Titel", "titel": "", "url": "", "kaltmiete": zahl|null, "zimmer": zahl|null,
"flaeche": zahl|null, "stadtteil": "", "balkon": "ja|nein|unklar", "hell": "ja|nein|unklar",
"ruhig": "ja|nein|unklar", "bezug": "Datum/Text aus dem Inserat oder unklar", "urteil": "passt|vielleicht|nein", "grund": "ein kurzer Satz"}}
urteil=passt: alle harten Kriterien nachweislich erfüllt und kein weiches Kriterium negativ.
urteil=vielleicht: kein hartes Kriterium verletzt, aber etwas ist unklar (z. B. nur Warmmiete bekannt).
urteil=nein: ein hartes Kriterium verletzt oder weiches klar negativ. Erfinde nichts."""


def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    for a in soup.find_all("a", href=True):
        a.append(f" <{a['href']}>")
    return soup.get_text("\n", strip=True)


def evaluate(text, source):
    r = client.messages.create(
        model=MODEL, max_tokens=4000, system=SYSTEM,
        messages=[{"role": "user", "content": f"Quelle: {source}\n\n{text[:30000]}"}],
    )
    raw = re.sub(r"^```(?:json)?|```$", "", r.content[0].text.strip(), flags=re.M).strip()
    try:
        return json.loads(raw)
    except Exception:
        print("JSON nicht lesbar für", source)
        return []


DETAIL_SYSTEM = f"""Heutiges Datum: {datetime.date.today().isoformat()}.
Du liest die Detailseite eines Wohnungsinserats. Suche den frühesten Bezugstermin ("Verfügbar ab", "frei ab",
"Bezugsfrei ab", "ab sofort") und prüfe Balkon/Terrasse, Helligkeit und Ruhe der Lage.
Antworte NUR mit einem JSON-Objekt:
{{"bezug_text": "Wortlaut aus dem Inserat oder unklar", "bezug_datum": "YYYY-MM-DD oder null",
"sofort": true|false, "balkon": "ja|nein|unklar", "hell": "ja|nein|unklar", "ruhig": "ja|nein|unklar"}}
Bei Angaben wie "01.03." ergänze das nächste passende Jahr. Erfinde nichts."""


def needs_date(item):
    b = str(item.get("bezug") or "unklar").lower()
    return b.startswith("unklar") and str(item.get("url", "")).startswith("http")


def enrich(item):
    """Öffnet den Inseratslink und sucht Bezugstermin (und ggf. Balkon/hell/ruhig)."""
    try:
        resp = requests.get(item["url"], timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = html_to_text(resp.text)
        r = client.messages.create(
            model=MODEL, max_tokens=500, system=DETAIL_SYSTEM,
            messages=[{"role": "user", "content": text[:25000]}],
        )
        raw = re.sub(r"^```(?:json)?|```$", "", r.content[0].text.strip(), flags=re.M).strip()
        info = json.loads(raw)
    except Exception as e:
        print("Detailseite nicht auswertbar:", item.get("url"), e)
        return item

    limit = datetime.date.today() + datetime.timedelta(days=61)  # ca. 2 Monate
    datum_ok = False
    if info.get("sofort"):
        item["bezug"], item["urteil"] = "ab sofort", "nein"
    else:
        item["bezug"] = info.get("bezug_text") or "unklar"
        try:
            d = datetime.date.fromisoformat(info.get("bezug_datum") or "")
            if d < limit:
                item["urteil"] = "nein"
            else:
                datum_ok = True
        except ValueError:
            pass

    for k in ("balkon", "hell", "ruhig"):
        if item.get(k, "unklar") == "unklar" and info.get(k) in ("ja", "nein"):
            item[k] = info[k]
    felder = [item.get(k, "unklar") for k in ("balkon", "hell", "ruhig")]
    if "nein" in felder:
        item["urteil"] = "nein"
    elif datum_ok and item.get("urteil") == "vielleicht" and all(f == "ja" for f in felder):
        item["urteil"] = "passt"
    return item


def page_items(state):
    for s in CFG["seiten"]:
        if s["url"].startswith("HIER"):
            continue
        try:
            resp = requests.get(s["url"], timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        except Exception as e:
            print("Fehler", s["name"], e)
            continue
        text = html_to_text(resp.text)
        h = hashlib.sha1(text.encode()).hexdigest()
        if state["pages"].get(s["url"]) == h:
            continue
        state["pages"][s["url"]] = h
        yield s["name"], text


def mail_items(state):
    user, pw = os.getenv("IMAP_USER"), os.getenv("IMAP_PASSWORD")
    if not (user and pw):
        return
    box = imaplib.IMAP4_SSL(os.getenv("IMAP_HOST", "imap.gmail.com"))
    box.login(user, pw)
    box.select("INBOX")
    since = (datetime.date.today() - datetime.timedelta(days=3)).strftime("%d-%b-%Y")
    for sender in CFG["mail_senders"]:
        _, data = box.search(None, f'(FROM "{sender}" SINCE {since})')
        for num in data[0].split():
            _, msg_data = box.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            mid = "mail:" + str(msg.get("Message-ID", num))
            if mid in state["seen"]:
                continue
            state["seen"].append(mid)
            body = ""
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/html", "text/plain"):
                    payload = part.get_payload(decode=True) or b""
                    txt = payload.decode(part.get_content_charset() or "utf-8", "ignore")
                    body += html_to_text(txt) if ctype == "text/html" else txt
                    if ctype == "text/html":
                        break
            yield f"Mail {sender}", body
    box.logout()


def schreibe_bewerbung(item):
    profil = os.getenv("BEWERBER_PROFIL", "").strip()
    if not profil:
        return ""
    system = (
        "Du schreibst eine kurze, freundliche Bewerbungsnachricht (max. 150 Wörter, Deutsch, per Sie) "
        "auf ein Wohnungsinserat. Nutze NUR Angaben aus dem Profil und dem Inserat, erfinde nichts "
        "(kein Gehalt, keine Arbeitgeber, keine Zusagen, die nicht im Profil stehen). Beziehe dich konkret "
        "auf die Wohnung, nenne den Wunsch nach einem Besichtigungstermin und signiere mit dem Namen aus dem Profil. "
        "Gib nur den Nachrichtentext aus, ohne Betreff und ohne Erklärungen."
    )
    try:
        r = client.messages.create(
            model=MODEL, max_tokens=600, system=system,
            messages=[{"role": "user", "content":
                       f"Profil:\n{profil}\n\nInserat:\n{json.dumps(item, ensure_ascii=False)}"}],
        )
        return r.content[0].text.strip()
    except Exception as e:
        print("Bewerbungstext fehlgeschlagen:", e)
        return ""


def send_mail(hits):
    lines = []
    for h in hits:
        lines.append(
            f"[{h['urteil'].upper()}] {h.get('titel','')}\n"
            f"{h.get('kaltmiete')} € kalt | {h.get('zimmer')} Zi. | {h.get('flaeche')} m² | {h.get('stadtteil')}\n"
            f"Bezug: {h.get('bezug')}\n"
            f"Balkon: {h.get('balkon')} | hell: {h.get('hell')} | ruhig: {h.get('ruhig')}\n"
            f"{h.get('grund','')}\n{h.get('url','')}\n"
            + (f"\n--- Bewerbungsentwurf ---\n{h['bewerbung']}\n" if h.get("bewerbung") else "")
        )
    msg = EmailMessage()
    msg["Subject"] = f"{len(hits)} neue Wohnung(en) in Bremen"
    msg["From"], msg["To"] = os.environ["SMTP_USER"], os.environ["MAIL_TO"]
    msg.set_content("\n".join(lines))
    with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


def main():
    state = json.loads(STATE_FILE.read_text())
    hits = []
    for source, text in list(page_items(state)) + list(mail_items(state)):
        for item in evaluate(text, source):
            key = item.get("id") or item.get("url") or item.get("titel")
            if not key or key in state["seen"] or item.get("urteil") not in ("passt", "vielleicht"):
                continue
            if needs_date(item):
                item = enrich(item)
            state["seen"].append(key)
            if item.get("urteil") in ("passt", "vielleicht"):
                hits.append(item)
    for h in hits:
        if h.get("urteil") == "passt":
            h["bewerbung"] = schreibe_bewerbung(h)
    if hits:
        send_mail(hits)
    print(f"{len(hits)} neue Treffer")
    state["seen"] = state["seen"][-2000:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
