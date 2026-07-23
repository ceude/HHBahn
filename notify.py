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
SEND_MAIL = os.environ.get("SEND_MAIL", "true").strip().lower() != "false"

# 1) data.js -> en ucuz kombo
raw = open("data.js", encoding="utf-8").read()
data = json.loads(re.sub(r"^\s*window\.BAHN_DATA\s*=\s*", "", raw).rstrip().rstrip(";"))
deals = data.get("deals") or []
if not deals:
    print("Kombo yok, mail atilmiyor.")
    sys.exit(0)
cheapest = min(d["total"] for d in deals)
cheapest_str = f"{cheapest:.2f}".replace(".", ",")

def eur(v):
    return f"{v:.2f}".replace(".", ",")

MONTHS_DE = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
             "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
DAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

def de_date(iso_str):
    from datetime import datetime
    dt = datetime.fromisoformat(iso_str)
    return f"{DAYS_DE[dt.weekday()]} {dt.day}. {MONTHS_DE[dt.month - 1]}"

# Kalkis -> sehir -> en ucuz TEK YON bacak
ow_best = {}
for d in deals:
    o = d.get("origin", "Hamburg")
    for leg_dir in ("out", "ret"):
        leg = d.get(leg_dir) or {}
        p = leg.get("price")
        if p is None:
            continue
        cur = ow_best.setdefault(o, {}).get(d["city"])
        if cur is None or p < cur["price"]:
            ow_best[o][d["city"]] = {
                "price": p, "dep": leg.get("dep"), "low": bool(leg.get("low")),
            }

order = [o for o in ["Hamburg", "München"] if o in ow_best] + \
        [o for o in ow_best if o not in ("Hamburg", "München")]

cheapest_ow = min(
    (i["price"] for c in ow_best.values() for i in c.values()), default=cheapest
)
cheapest_ow_str = eur(cheapest_ow)

def top5(o):
    items = [(city, i) for city, i in ow_best.get(o, {}).items()]
    items.sort(key=lambda x: x[1]["price"])
    return items[:5]

def origin_block_html(o):
    rows = ""
    for city, i in top5(o):
        badge = ('<span style="background:#E7F7ED;color:#0A8A3A;font-size:10px;font-weight:bold;'
                 'padding:2px 6px;border-radius:99px;margin-left:6px">Bestpreis</span>') if i["low"] else ""
        rows += (
            '<tr>'
            f'<td style="padding:7px 0;border-bottom:1px solid #EDEFF2;color:#282D37;font-size:14px">'
            f'<strong>{city}</strong>{badge}<br>'
            f'<span style="color:#878C96;font-size:12px">{de_date(i["dep"])}</span></td>'
            f'<td style="padding:7px 0;border-bottom:1px solid #EDEFF2;text-align:right;'
            f'color:#EC0016;font-size:15px;font-weight:bold;white-space:nowrap">{eur(i["price"])} &euro;</td>'
            '</tr>'
        )
    return (
        f'<div style="margin:0 0 18px">'
        f'<div style="font-size:12px;letter-spacing:1.5px;text-transform:uppercase;'
        f'color:#878C96;font-weight:bold;margin-bottom:6px">ab {o}</div>'
        f'<table style="width:100%;border-collapse:collapse">{rows}</table></div>'
    )

total_routes = sum(len(v) for v in ow_best.values())
more_routes = max(0, total_routes - sum(len(top5(o)) for o in order))

origin_lines_html = "".join(origin_block_html(o) for o in order)
origin_lines_txt = " | ".join(
    f"ab {o}: " + ", ".join(f"{c} {eur(i['price'])} EUR" for c, i in top5(o))
    for o in order
)

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
new_js = "window.BAHN_DATA = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n"
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
    <div style="font-size:20px;font-weight:bold;margin-top:2px">Neue Wochenendtickets</div>
  </div>
  <div style="border:1px solid #D7DCE1;border-top:0;border-radius:0 0 8px 8px;padding:20px">
    <p style="color:#282D37;font-size:15px;line-height:1.5;margin:0 0 14px">
      Die Preisliste wurde aktualisiert. Die g&uuml;nstigsten Direktverbindungen
      f&uuml;r eine <strong>einfache Fahrt</strong> am Wochenende:
    </p>
    {origin_lines_html}
    <p style="color:#282D37;font-size:14px;line-height:1.5;margin:0 0 12px">
      {"Und noch <strong>" + str(more_routes) + " weitere Verbindungen</strong> — Hin- und R&uuml;ckfahrt, alle Wochenenden und Tagesausfl&uuml;ge:" if more_routes else "Alle Verbindungen, Hin- und R&uuml;ckfahrt und Tagesausfl&uuml;ge:"}
    </p>
    <a href="{SITE_URL}" style="display:inline-block;background:#EC0016;color:#fff;text-decoration:none;font-weight:bold;font-size:14px;padding:10px 18px;border-radius:6px">Alle St&auml;dte &amp; Preise ansehen &rarr;</a>
    <p style="color:#878C96;font-size:11px;margin:18px 0 0">
      Preise sind Sparpreise zum Zeitpunkt des Scans und k&ouml;nnen sich jederzeit &auml;ndern.<br>
      <a href="{unsub_link(email)}" style="color:#878C96;text-decoration:underline">Abmelden</a> &middot;
      <a href="{unsub_link(email)}" style="color:#878C96;text-decoration:underline">Unsubscribe</a> &middot;
      <a href="{unsub_link(email)}" style="color:#878C96;text-decoration:underline">Abonelikten &ccedil;&#305;k</a>
    </p>
  </div>
</div>
"""


if not SEND_MAIL:
    print("SEND_MAIL=false — mail atlandi, dip bayraklari islendi.")
    raise SystemExit(0)

subject = f"Wochenend-Bahntrips — einfache Fahrt ab {cheapest_ow_str} €"
sent = 0
with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
    s.login(GMAIL_USER, GMAIL_PASS)
    for email in emails:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Wochenendkurztrip <{GMAIL_USER}>"
        msg["To"] = email
        msg.attach(MIMEText(
            f"Einfache Fahrt ab {cheapest_ow_str} EUR — {origin_lines_txt}\n{SITE_URL}\nAbmelden / Unsubscribe / Abonelikten cik: {unsub_link(email)}",
            "plain", "utf-8"))
        msg.attach(MIMEText(build_html(email), "html", "utf-8"))
        try:
            s.sendmail(GMAIL_USER, [email], msg.as_string())
            sent += 1
        except smtplib.SMTPException as e:
            print(f"gonderilemedi ({email}): {e}")

print(f"OK — {sent}/{len(emails)} aboneye gonderildi, en ucuz {cheapest_str} EUR")
