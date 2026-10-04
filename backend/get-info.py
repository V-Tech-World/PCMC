"""
get-info.py -- OPERATIONS SCRIPT, NOT PART OF THE APP.

Opens (or re-opens) the WhatsApp thread that VoiceCare sends HIGH-risk
alerts into, and prints the conversation id to paste into backend/.env.

Why it is a separate script: a WhatsApp conversation only stays open for 24
hours. The backend just posts into an OPEN conversation -- it deliberately
never opens one itself. When the window closes (or when the care team's number
changes), run this once and copy the new id into `.env`.

    python get-info.py                       # open for the configured number
    python get-info.py 94771234567           # open for a different number

It sends ONE message: the template below, which is what starts the thread
(WhatsApp only allows a business-initiated conversation via an approved
template). Fix/approve that template's text in the Zernio dashboard.
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ZERNIO_API_KEY", "")
# Zernio inbox account (dashboard > Inbox) -- same value as
# ZERNIO_INBOX_ACCOUNT_ID in .env.
ACCOUNT_ID = os.getenv("ZERNIO_INBOX_ACCOUNT_ID", "6a180a034c7f364ffded3c9c")
# Care-team number, digits only with country code (no '+').
PARTICIPANT_ID = (
    os.argv[1] if len(sys.argv) > 1 else os.getenv("ALERT_WHATSAPP_TO", "94782586272")
)
TEMPLATE_NAME = os.getenv("ZERNIO_ALERT_TEMPLATE", "sandbox_start")

SUGGESTED_TEMPLATE_TEXT = (
    "VoiceCare care-team line. High-risk post-discharge alerts will be "
    "sent here. Reply STOP to unsubscribe, HELP for support."
)

if not API_KEY:
    raise SystemExit("ZERNIO_API_KEY is missing -- copy .env.example to .env first.")

response = requests.post(
    "https://zernio.com/api/v1/inbox/conversations",
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    },
    json={
        "accountId": ACCOUNT_ID,
        "participantId": PARTICIPANT_ID,
        "templateName": TEMPLATE_NAME,
        "templateLanguage": "en",
    },
    timeout=20,
)

print("Open conversation status:", response.status_code)
try:
    body = response.json()
except ValueError:
    raise SystemExit(f"Non-JSON response: {response.text[:300]}")

print("Response:", body)
if response.status_code >= 400:
    raise SystemExit("\nCould not open the conversation -- see the response above.")

data = body.get("data") or {}
conversation_id = data.get("conversationId")
message_id = data.get("messageId")

print(f"\nconversationId: {conversation_id}")
print(f"messageId:      {message_id}")

if not conversation_id:
    raise SystemExit("\nNo conversationId in the response -- nothing to copy.")

print("\nAdd/update these in backend/.env:")
print(f"  ZERNIO_INBOX_ACCOUNT_ID={ACCOUNT_ID}")
print(f"  ZERNIO_ALERT_CONVERSATION_ID={conversation_id}")
print("  ALERT_DELIVERY=whatsapp")

print(f"\nInit message (template '{TEMPLATE_NAME}') -- set this text in the")
print("Zernio dashboard so the care team knows what this thread is for:")
print(f"  \"{SUGGESTED_TEMPLATE_TEXT}\"")