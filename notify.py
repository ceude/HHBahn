# notify.py — GitHub Action tarafindan calistirilir (repo kokune koy).
# data.js'ten en ucuz komboyu okur, Supabase'den aboneleri ceker,
# Gmail SMTP (uygulama sifresi) ile bcc mail atar.

import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_APP_PASSWORD"]
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

# 3) Mail
subject = f"Neue Wochenendtickets ab Hamburg — ab {cheapest_str} €"
html = f"""
<div style="font-family:Helvetica,Arial,sans-serif;max-width:520px;margin:0 auto">
  <div style="background:#EC0016;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
    <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;opacity:.85">Wochenende ab Hamburg</div>
    <div style="font-size:20px;font-weight:bold;margin-top:2px">Neue Tickets verf&uuml;gbar</div>
  </div>
  <div style="border:1px solid #D7DCE1;border-top:0;border-radius:0 0 8px 8px;padding:20px">
    <p style="color:#282D37;font-size:15px;line-height:1.5;margin:0 0 14px">
      Die Preisliste wurde aktualisiert. Wochenend-Tickets ab Hamburg Hbf (hin und zur&uuml;ck)
      gibt es <strong>ab {cheapest_str} &euro;</strong>.
    </p>
    <a href="{SITE_URL}" style="display:inline-block;background:#EC0016;color:#fff;text-decoration:none;font-weight:bold;font-size:14px;padding:10px 18px;border-radius:6px">Tickets ansehen</a>
    <p style="color:#878C96;font-size:11px;margin:18px 0 0">
      Preise sind Sparpreise zum Zeitpunkt des Scans und k&ouml;nnen sich jederzeit &auml;ndern.
    </p>
  </div>
</div>
"""

msg = MIMEMultipart("alternative")
msg["Subject"] = subject
msg["From"] = f"Wochenende ab Hamburg <{GMAIL_USER}>"
msg["To"] = GMAIL_USER  # alicilar bcc'de, birbirlerini gormezler
msg.attach(MIMEText(f"Neue Tickets ab {cheapest_str} EUR: {SITE_URL}", "plain", "utf-8"))
msg.attach(MIMEText(html, "html", "utf-8"))

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
    s.login(GMAIL_USER, GMAIL_PASS)
    s.sendmail(GMAIL_USER, [GMAIL_USER] + emails, msg.as_string())

print(f"OK — {len(emails)} aboneye gonderildi, en ucuz {cheapest_str} EUR")
