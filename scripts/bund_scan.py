#!/usr/bin/env python3
"""
BidOS — Bund-Radar-Scan (oeffentlichevergabe.de / Datenservice Öffentlicher Einkauf)
Läuft täglich per GitHub Action. Holt die letzten Tage als OCDS-Bulk-Export,
filtert auf offene Ausschreibungen (tag=tender) mit enthus-relevanten CPV-Codes
und schreibt sie als source='bund' ins Supabase radar_items (Upsert, idempotent).

ENV:
  SUPABASE_KEY  – Supabase service key (als GitHub-Secret hinterlegen)
"""
import os, re, json, io, zipfile, datetime, urllib.request, urllib.error

SB  = "https://zbpsumhtiatlqegitpte.supabase.co"
SBK = os.environ.get("SUPABASE_KEY", "")
DAYS_BACK   = int(os.environ.get("DAYS_BACK", "3"))   # bei jedem Lauf die letzten N Tage (idempotenter Upsert)
PRUNE_DAYS  = int(os.environ.get("PRUNE_DAYS", "45"))  # Bund-Treffer älter als X Tage entfernen
APP_HTML    = os.path.join(os.path.dirname(__file__), "..", "app.html")

if not SBK:
    raise SystemExit("❌ SUPABASE_KEY fehlt (GitHub-Secret setzen).")

# ── enthus-CPV-Codes + Ausschluss-Begriffe aus app.html (Single Source of Truth) ──
def load_enthus_cpv():
    s = open(APP_HTML, encoding="utf-8").read()
    m = re.search(r"const CPV_CATS=\[(.*?)\];", s, re.S)
    codes = set(re.findall(r"'(\d{8})-\d'", m.group(1)))
    classes = set(c[:5] for c in codes)
    return codes, classes

def load_exclude_keywords():
    s = open(APP_HTML, encoding="utf-8").read()
    m = re.search(r"const EXCLUDE_DEFAULT=\[(.*?)\];", s, re.S)
    if not m:
        return []
    return re.findall(r"'([^']+)'", m.group(1))

def load_it_positive():
    s = open(APP_HTML, encoding="utf-8").read()
    m = re.search(r"const IT_POSITIVE=\[(.*?)\];", s, re.S)
    if not m:
        return []
    return re.findall(r"'([^']+)'", m.group(1))

_WORD_CHAR = re.compile(r"[a-z0-9äöüß]")

def _kw_hit(txt, kw):
    """Wortgrenzen-Treffer statt reinem Substring-Check — verhindert Fehltreffer wie
    'itsm' in 'Arbeitsmarkt' oder 'san' in 'Sanierung'. Spiegelt kwHit() aus app.html."""
    kw = kw.lower().strip()
    if not kw:
        return False
    start = 0
    while True:
        i = txt.find(kw, start)
        if i == -1:
            return False
        before = txt[i - 1] if i > 0 else " "
        after = txt[i + len(kw)] if i + len(kw) < len(txt) else " "
        if not _WORD_CHAR.match(before) and not _WORD_CHAR.match(after):
            return True
        start = i + 1

def keyword_hit(title, description, keywords):
    """Fallback-Treffer über IT-Begriffe, falls die CPV-Klassifizierung fehlt/falsch ist
    (in der dt. Vergabepraxis häufig unpräzise gepflegt)."""
    txt = " " + (title + " " + (description or "")).lower() + " "
    for kw in keywords:
        if kw and _kw_hit(txt, kw):
            return kw
    return None

def is_relevant(title, description, exclude_keywords):
    """Serverseitiges Relevanz-Screening: verwirft Treffer mit Ausschluss-Begriff.
    Spiegelt tenderRelevance() aus app.html (nur die 'unfit'-Erkennung, konservativ)."""
    txt = " " + (title + " " + (description or "")).lower() + " "
    for kw in exclude_keywords:
        if kw and _kw_hit(txt, kw):
            return False, kw
    return True, None

# ── OCDS-Tagespaket laden ──
def fetch_day(day):
    url = f"https://oeffentlichevergabe.de/api/notice-exports?pubDay={day}&format=ocds.zip"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read()
    except Exception as e:
        print(f"  {day}: Download-Fehler ({e})")
        return None

def cpvs_of(t):
    out = []
    for it in (t.get("items") or []):
        c = it.get("classification") or {}
        if c.get("id"):
            out.append(str(c["id"]))
        for ac in (it.get("additionalClassifications") or []):
            if ac.get("id"):
                out.append(str(ac["id"]))
    return out

def sb_request(method, path, body=None, prefer=None):
    headers = {"apikey": SBK, "Authorization": "Bearer " + SBK, "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SB + path, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)

def main():
    ecodes, eclasses = load_enthus_cpv()
    exclude_kw = load_exclude_keywords()
    it_kw = load_it_positive()
    print(f"enthus-CPV: {len(ecodes)} Codes / {len(eclasses)} Klassen | Ausschluss-Begriffe: {len(exclude_kw)} | IT-Begriffe: {len(it_kw)}")
    today = datetime.date.today()
    rows = {}
    skipped_unfit = 0
    keyword_only_rids = set()
    for i in range(1, DAYS_BACK + 1):
        day = (today - datetime.timedelta(days=i)).isoformat()
        raw = fetch_day(day)
        if not raw:
            continue
        z = zipfile.ZipFile(io.BytesIO(raw))
        cnt = 0
        for n in z.namelist():
            try:
                d = json.loads(z.read(n))
            except Exception:
                continue
            r = (d.get("releases") or [{}])[0]
            t = r.get("tender", {})
            if (r.get("tag") or [None])[0] != "tender":   # nur offene Ausschreibungen
                continue
            cpvs = cpvs_of(t)
            exact = [c for c in cpvs if c in ecodes]
            cls = [c for c in cpvs if c[:5] in eclasses]
            title = t.get("title") or ""
            description = t.get("description") or ""
            kw_hit = None
            if not cls:
                # CPV-Klassifizierung fehlt/ist falsch gepflegt (in der Praxis häufig) —
                # Fallback über IT-Positiv-Begriffe, statt den Treffer ganz zu verwerfen.
                kw_hit = keyword_hit(title, description, it_kw)
                if not kw_hit:
                    continue
            relevant, hit = is_relevant(title, description, exclude_kw)
            if not relevant:
                skipped_unfit += 1
                continue
            score = 85 if exact else (70 if cls else 55)
            nid = (r.get("ocid") or n).replace("/", "-")
            buyer = r.get("buyer") or {}
            addr = buyer.get("address") or {}
            docs = t.get("documents") or []
            url = next((dd.get("url") for dd in docs if dd.get("url")), None) \
                  or (buyer.get("contactPoint") or {}).get("url") or "https://oeffentlichevergabe.de"
            val = (t.get("value") or {}).get("amount")
            rid = "bund-" + nid[:60]
            if kw_hit:
                keyword_only_rids.add(rid)
            rows[rid] = {
                "id": rid, "title": (t.get("title") or "(ohne Titel)")[:300],
                "contracting_authority": (buyer.get("name") or "")[:200],
                "region": addr.get("region") or "DEU",
                "submission_deadline": None,
                "estimated_value": int(val) if val else None,
                "cpv_codes": (list(dict.fromkeys(cls + exact)) or cpvs)[:10],
                "source": "bund", "source_url": url[:500],
                "description": (t.get("description") or "")[:600],
                "full_text": ((t.get("title") or "") + " " + (t.get("description") or ""))[:1500],
                "match_score": score, "status": "new",
                "discovered_at": datetime.datetime.utcnow().isoformat() + "Z",
            }
            cnt += 1
        print(f"  {day}: {cnt} enthus-relevante offene Treffer")
    rows = list(rows.values())
    print(f"Gesamt: {len(rows)} Treffer zum Upsert ({len(keyword_only_rids)} davon nur über IT-Begriff, ohne CPV-Match) | {skipped_unfit} als unpassend vorgefiltert (nicht gespeichert)")

    ok = 0
    for i in range(0, len(rows), 50):
        batch = rows[i:i + 50]
        try:
            sb_request("POST", "/rest/v1/radar_items?on_conflict=id", batch,
                       prefer="resolution=merge-duplicates,return=minimal")
            ok += len(batch)
        except urllib.error.HTTPError as e:
            print("  Upsert-Fehler:", e.code, e.read().decode()[:200]); break
    print(f"✅ Upsert: {ok} Zeilen")

    # ── Alte Bund-Treffer entfernen (Frische halten) ──
    cutoff = (today - datetime.timedelta(days=PRUNE_DAYS)).isoformat()
    try:
        sb_request("DELETE", f"/rest/v1/radar_items?source=eq.bund&discovered_at=lt.{cutoff}",
                   prefer="return=minimal")
        print(f"🧹 Bund-Treffer älter als {cutoff} entfernt")
    except urllib.error.HTTPError as e:
        print("  Prune-Hinweis:", e.code)

if __name__ == "__main__":
    main()
