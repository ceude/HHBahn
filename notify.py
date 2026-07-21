# notify.py — GitHub Action tarafindan calistirilir (repo kokune koy).
# data.js'ten en ucuz komboyu okur, Supabase'den aboneleri ceker,
# her aboneye KENDI abonelikten-cikis linkiyle ayri mail atar (Gmail SMTP).
#
# Gerekli GitHub secret'lari:
#   SUPABASE_URL, SUPABASE_SERVICE_KEY, GMAIL_USER, GMAIL_APP_PASSWORD,
#   UNSUB_SECRET  (unsub_setup.sql icindeki secret ile AYNI deger)

import hashlib
import hmac
import json
import os
import re
import smtplib
import sys
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_APP_PASSWORD"]
UNSUB_SECRET = os.environ["UNSUB_SECRET"]
SITE_URL = os.environ.get("SITE_URL", "https://ceude.github.io/HHBahn")

# 1) data.js -> en ucuz kombo
raw = open("data.js", encoding="utf-8").read()
data = json.loads(re.sub(r"^\s*window\.BAHN_DATA\s*=\s*", "", raw).rstrip().rstrip(";"))
deals = data.get("deals") or []
if not deals:
    print("Kombo yok, mail atilmiyor.")
    sys.exit(0)
cheapest = min(d["total"] for d in deals)
cheapest_str = f"{cheapest:.2f}".replace(".", ",")

# Kalkis bazli en dusuk (mail metni icin)
by_origin = {}
for d in deals:
    o = d.get("origin", "Hamburg")
    by_origin[o] = min(by_origin.get(o, 1e9), d["total"])
def eur(v):
    return f"{v:.2f}".replace(".", ",")
# Sabit sira: Hamburg, Munchen, sonra kalanlar
order = [o for o in ["Hamburg", "München"] if o in by_origin] + \
        [o for o in by_origin if o not in ("Hamburg", "München")]
origin_lines_html = "".join(
    f'<li style="margin:2px 0">ab <strong>{o}</strong> — ab {eur(by_origin[o])} &euro;</li>'
    for o in order
)
origin_lines_txt = " | ".join(f"ab {o}: {eur(by_origin[o])} EUR" for o in order)

# ---- DIP FIYAT: gecmisle karsilastir, bayrakla, data.js'i guncelle ----
def route_key(origin, city, variant, direction):
    return f"{origin}|{city}|{variant}|{direction}"

# Bu taramadaki her rota-yon icin gorulen en dusuk fiyati topla
seen_low = {}  # route_key -> price
for d in deals:
    o = d.get("origin", "Hamburg")
    seen_low_rt = route_key(o, d["city"], d["variant"], "rt")
    seen_low[seen_low_rt] = min(seen_low.get(seen_low_rt, 1e9), d["total"])
    for leg_dir in ("out", "ret"):
        leg = d.get(leg_dir) or {}
        p = leg.get("price")
        if p is None:
            continue
        k = route_key(o, d["city"], "ow", leg_dir)
        seen_low[k] = min(seen_low.get(k, 1e9), p)

# Supabase'deki kayitli en dusukleri cek
hist = {}
try:
    hr = requests.get(
        f"{SUPABASE_URL}/rest/v1/bahn_price_low?select=route_key,low_price",
        headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"},
        timeout=20,
    )
    if hr.ok:
        hist = {row["route_key"]: float(row["low_price"]) for row in hr.json()}
except requests.RequestException as e:
    print(f"dip gecmisi okunamadi (atlandi): {e}")

# Bu fiyat kayitli en dusuge esit/altindaysa dip kabul et
def is_low(key, price):
    prev = hist.get(key)
    return prev is None or price <= prev + 0.001

for d in deals:
    o = d.get("origin", "Hamburg")
    d["lowRt"] = is_low(route_key(o, d["city"], d["variant"], "rt"), d["total"])
    for leg_dir in ("out", "ret"):
        leg = d.get(leg_dir) or {}
        p = leg.get("price")
        if p is not None:
            leg["low"] = is_low(route_key(o, d["city"], "ow", leg_dir), p)

# Yeni dip degerlerini Supabase'e yaz (upsert)
upserts = []
for k, p in seen_low.items():
    prev = hist.get(k)
    if prev is None or p < prev:
        upserts.append({"route_key": k, "low_price": round(p, 2)})
if upserts:
    try:
        ur = requests.post(
            f"{SUPABASE_URL}/rest/v1/bahn_price_low",
            headers={
                "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            json=upserts, timeout=30,
        )
        if not ur.ok:
            print(f"dip yazilamadi: {ur.status_code} {ur.text[:200]}")
    except requests.RequestException as e:
        print(f"dip yazilamadi (atlandi): {e}")

# data.js'i low bayraklariyla geri yaz (site bunu okuyup rozet gosterir)
new_js = "window.BAHN_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n"
open("data.js", "w", encoding="utf-8").write(new_js)
print(f"Dip guncellendi: {len(upserts)} yeni dip.")

# 2) Aboneler
r = requests.get(
    f"{SUPABASE_URL}/rest/v1/bahn_subscribers?select=email",
    headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"},
    timeout=20,
)
r.raise_for_status()
emails = [row["email"] for row in r.json()]
if not emails:
    print("Abone yok, mail atilmiyor.")
    sys.exit(0)


def unsub_link(email: str) -> str:
    token = hmac.new(UNSUB_SECRET.encode(), email.strip().lower().encode(), hashlib.sha256).hexdigest()
    return f"{SITE_URL}/?unsub={urllib.parse.quote(email)}&t={token}"


def build_html(email: str) -> str:
    return f"""
<div style="font-family:Helvetica,Arial,sans-serif;max-width:520px;margin:0 auto">
  <div style="background:#EC0016;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
    <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;opacity:.85">Wochenendkurztrip</div>
    <div style="font-size:20px;font-weight:bold;margin-top:2px">Neue Tickets verf&uuml;gbar</div>
  </div>
  <div style="border:1px solid #D7DCE1;border-top:0;border-radius:0 0 8px 8px;padding:20px">
    <p style="color:#282D37;font-size:15px;line-height:1.5;margin:0 0 14px">
      Die Preisliste wurde aktualisiert. Wochenend-Tickets (hin und zur&uuml;ck) gibt es:
    </p>
    <ul style="color:#282D37;font-size:15px;line-height:1.6;margin:0 0 14px;padding-left:20px">
      {origin_lines_html}
    </ul>
    <a href="{SITE_URL}" style="display:inline-block;background:#EC0016;color:#fff;text-decoration:none;font-weight:bold;font-size:14px;padding:10px 18px;border-radius:6px">Tickets ansehen</a>
    <p style="color:#878C96;font-size:11px;margin:18px 0 0">
      Preise sind Sparpreise zum Zeitpunkt des Scans und k&ouml;nnen sich jederzeit &auml;ndern.<br>
      <a href="{unsub_link(email)}" style="color:#878C96;text-decoration:underline">Abmelden / Abonelikten &ccedil;&#305;k</a>
    </p>
  </div>
</div>
"""


subject = f"Neue Wochenend-Bahntrips — ab {cheapest_str} €"
sent = 0
with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
    s.login(GMAIL_USER, GMAIL_PASS)
    for email in emails:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Wochenendkurztrip <{GMAIL_USER}>"
        msg["To"] = email
        msg.attach(MIMEText(
            f"Neue Wochenend-Bahntrips ({origin_lines_txt}): {SITE_URL}\nAbmelden: {unsub_link(email)}",
            "plain", "utf-8"))
        msg.attach(MIMEText(build_html(email), "html", "utf-8"))
        try:
            s.sendmail(GMAIL_USER, [email], msg.as_string())
            sent += 1
        except smtplib.SMTPException as e:
            print(f"gonderilemedi ({email}): {e}")

print(f"OK — {sent}/{len(emails)} aboneye gonderildi, en ucuz {cheapest_str} EUR")
