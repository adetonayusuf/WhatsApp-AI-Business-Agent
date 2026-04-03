import os
import json
import tempfile
import requests
import config
from datetime import datetime, date
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

# ════════════════════════════════════════════════════════════════
# INITIALISE SERVICES
# ════════════════════════════════════════════════════════════════

app           = Flask(__name__)
ai            = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
twilio_client = TwilioClient(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

LEADS_DB_ID    = os.getenv("NOTION_LEADS_DB_ID")
BOOKINGS_DB_ID = os.getenv("NOTION_BOOKINGS_DB_ID")
NOTION_HEADERS = {
    "Authorization": f"Bearer {os.getenv('NOTION_TOKEN')}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

conversations = {}  # In-memory conversation history per phone number


# ════════════════════════════════════════════════════════════════
# FEATURE 1 — VOICE NOTE TRANSCRIPTION
# ════════════════════════════════════════════════════════════════

def transcribe_voice(media_url: str) -> str:
    print("  Transcribing voice note...")
    audio_data = requests.get(
        media_url,
        auth=(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    ).content

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(audio_data)
        tmp_path = f.name

    with open(tmp_path, "rb") as audio_file:
        transcript = ai.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file
        )

    os.unlink(tmp_path)
    print(f"  Transcribed: {transcript.text}")
    return transcript.text


# ════════════════════════════════════════════════════════════════
# FEATURE 2 — IMAGE RECOGNITION
# ════════════════════════════════════════════════════════════════

def analyze_image(media_url: str) -> str:
    print("  Analysing image...")
    import base64

    auth      = (os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    img_data  = requests.get(media_url, auth=auth).content
    img_b64   = base64.b64encode(img_data).decode("utf-8")

    response = ai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"You are a helpful assistant for {config.BUSINESS_NAME}, "
                            f"a {config.BUSINESS_TYPE}. "
                            f"A customer sent this image. "
                            f"Identify what it shows and recommend the most relevant "
                            f"services from this list:\n{config.SERVICES_TEXT}\n"
                            f"Be warm, specific, and keep your reply to 2-3 sentences."
                        )
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
                    }
                ]
            }
        ],
        max_tokens=200
    )

    result = response.choices[0].message.content.strip()
    print(f"  Image analysis: {result}")
    return result


# ════════════════════════════════════════════════════════════════
# FEATURE 3 — PAYMENT LINK GENERATION (PAYSTACK)
# ════════════════════════════════════════════════════════════════

def generate_payment_link(amount: int, description: str) -> str | None:
    try:
        response = requests.post(
            "https://api.paystack.co/transaction/initialize",
            headers={
                "Authorization": f"Bearer {os.getenv('PAYSTACK_SECRET_KEY')}",
                "Content-Type": "application/json"
            },
            json={
                "email":       config.PAYMENT_EMAIL,
                "amount":      amount * 100,  # Paystack uses kobo
                "description": description,
            }
        ).json()

        if response.get("status"):
            link = response["data"]["authorization_url"]
            print(f"  Payment link generated: {link}")
            return link
        else:
            print(f"  Paystack error: {response.get('message')}")
            return None

    except Exception as e:
        print(f"  Payment error: {e}")
        return None


# ════════════════════════════════════════════════════════════════
# FEATURE 4 — APPOINTMENT BOOKING (NOTION)
# ════════════════════════════════════════════════════════════════

def save_booking(phone: str, service: str, date_str: str, time_str: str):
    try:
        requests.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json={
                "parent": {"database_id": BOOKINGS_DB_ID},
                "properties": {
                    "Customer name": {
                        "title": [{"text": {"content": phone}}]
                    },
                    "Phone": {
                        "rich_text": [{"text": {"content": phone}}]
                    },
                    "Service": {
                        "rich_text": [{"text": {"content": service}}]
                    },
                    "Date": {
                        "date": {"start": date_str}
                    },
                    "Time": {
                        "rich_text": [{"text": {"content": time_str}}]
                    },
                    "Status": {
                        "select": {"name": "Confirmed"}
                    }
                }
            }
        )
        print(f"  Booking saved: {service} on {date_str} at {time_str}")

    except Exception as e:
        print(f"  Booking save error: {e}")


def extract_and_save_booking(phone: str, conversation: list) -> bool:
    """Ask AI to extract booking details from conversation and save to Notion."""
    try:
        extraction = ai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract booking details from this conversation. "
                        "Return ONLY valid JSON with these exact keys:\n"
                        "{\n"
                        '  "is_complete": true or false,\n'
                        '  "customer_name": "string or null",\n'
                        '  "service": "string or null",\n'
                        '  "date": "YYYY-MM-DD or null",\n'
                        '  "time": "string or null"\n'
                        "}\n"
                        "is_complete = true only if you have ALL four: "
                        "name, service, date AND time.\n"
                        "For dates, today is "
                        + datetime.now().strftime("%Y-%m-%d")
                        + ". If someone says 'Friday', calculate the actual date.\n"
                        "Return only JSON, no other text."
                    )
                },
                {
                    "role": "user",
                    "content": str(conversation[-6:])
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=150
        )

        details = json.loads(extraction.choices[0].message.content)
        print(f"  Booking extraction: {details}")

        if details.get("is_complete"):
            save_booking(
                phone=phone,
                service=details.get("service", "Unknown"),
                date_str=details.get("date", datetime.now().strftime("%Y-%m-%d")),
                time_str=details.get("time", "TBD")
            )
            return True

    except Exception as e:
        print(f"  Booking extraction error: {e}")

    return False


# ════════════════════════════════════════════════════════════════
# FEATURE 5 — HUMAN HANDOFF
# ════════════════════════════════════════════════════════════════

HANDOFF_TRIGGERS = [
    "speak to someone", "speak to a person", "real person",
    "human", "agent", "manager", "call me", "not helpful",
    "useless", "frustrated", "i want to talk", "escalate"
]

def needs_handoff(message: str) -> bool:
    msg = message.lower()
    return any(trigger in msg for trigger in HANDOFF_TRIGGERS)


def trigger_handoff(phone: str, message: str):
    print(f"  Handoff triggered for {phone}")

    try:
        twilio_client.messages.create(
            from_=os.getenv("TWILIO_WHATSAPP_NUMBER"),
            to=config.OWNER_WHATSAPP,
            body=(
                f"URGENT — Human handoff requested\n\n"
                f"Customer: {phone}\n"
                f"Message: {message}\n\n"
                f"Please reply to them directly."
            )
        )
        print("  Owner notified of handoff.")
    except Exception as e:
        print(f"  Handoff notify error: {e}")

    try:
        existing = requests.post(
            f"https://api.notion.com/v1/databases/{LEADS_DB_ID}/query",
            headers=NOTION_HEADERS,
            json={"filter": {"property": "Phone number",
                             "rich_text": {"equals": phone}}}
        ).json()

        if existing.get("results"):
            page_id = existing["results"][0]["id"]
            requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=NOTION_HEADERS,
                json={"properties": {
                    "Status": {"select": {"name": "Urgent"}}
                }}
            )
            print("  Lead marked Urgent in Notion.")
    except Exception as e:
        print(f"  Handoff Notion error: {e}")


# ════════════════════════════════════════════════════════════════
# FEATURE 6 — DAILY LEAD SUMMARY (sent to owner at 8am)
# ════════════════════════════════════════════════════════════════

def send_daily_summary():
    print(f"\nSending daily summary — {datetime.now()}")
    try:
        today = date.today().isoformat()

        result = requests.post(
            f"https://api.notion.com/v1/databases/{LEADS_DB_ID}/query",
            headers=NOTION_HEADERS,
            json={
                "filter": {
                    "property": "First contact",
                    "date":     {"equals": today}
                }
            }
        ).json()

        leads    = result.get("results", [])
        total    = len(leads)
        bookings = sum(1 for l in leads if
            l["properties"].get("Intent", {})
             .get("select", {}).get("name") == "Booking")
        pricing  = sum(1 for l in leads if
            l["properties"].get("Intent", {})
             .get("select", {}).get("name") == "Pricing")
        urgent   = sum(1 for l in leads if
            l["properties"].get("Status", {})
             .get("select", {}).get("name") == "Urgent")

        summary = (
            f"Daily Summary — {today}\n"
            f"Business: {config.BUSINESS_NAME}\n\n"
            f"Total conversations: {total}\n"
            f"Booking enquiries: {bookings}\n"
            f"Pricing enquiries: {pricing}\n"
            f"Needs attention: {urgent}\n\n"
            f"Open Notion to see full details."
        )

        twilio_client.messages.create(
            from_=os.getenv("TWILIO_WHATSAPP_NUMBER"),
            to=config.OWNER_WHATSAPP,
            body=summary
        )
        print("  Daily summary sent to owner.")

    except Exception as e:
        print(f"  Summary error: {e}")


# ════════════════════════════════════════════════════════════════
# CORE — NOTION LEADS
# ════════════════════════════════════════════════════════════════

def save_to_notion(phone: str, message: str, reply: str, intent: str):
    try:
        summary = f"Customer: {message[:120]}\nBot: {reply[:120]}"

        existing = requests.post(
            f"https://api.notion.com/v1/databases/{LEADS_DB_ID}/query",
            headers=NOTION_HEADERS,
            json={"filter": {"property": "Phone number",
                             "rich_text": {"equals": phone}}}
        ).json()

        if existing.get("results"):
            page_id = existing["results"][0]["id"]
            requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=NOTION_HEADERS,
                json={"properties": {
                    "Last message": {
                        "rich_text": [{"text": {"content": message[:200]}}]
                    },
                    "Intent": {
                        "select": {"name": intent}
                    },
                    "Summary": {
                        "rich_text": [{"text": {"content": summary}}]
                    },
                }}
            )
            print(f"  Notion lead updated: {phone}")
        else:
            requests.post(
                "https://api.notion.com/v1/pages",
                headers=NOTION_HEADERS,
                json={
                    "parent": {"database_id": LEADS_DB_ID},
                    "properties": {
                        "Customer name": {
                            "title": [{"text": {"content": phone}}]
                        },
                        "Phone number": {
                            "rich_text": [{"text": {"content": phone}}]
                        },
                        "Last message": {
                            "rich_text": [{"text": {"content": message[:200]}}]
                        },
                        "Intent": {
                            "select": {"name": intent}
                        },
                        "Status": {
                            "select": {"name": "New"}
                        },
                        "First contact": {
                            "date": {"start": datetime.now().strftime("%Y-%m-%d")}
                        },
                        "Summary": {
                            "rich_text": [{"text": {"content": summary}}]
                        },
                    }
                }
            )
            print(f"  New lead saved: {phone}")

    except Exception as e:
        print(f"  Notion error: {e}")


# ════════════════════════════════════════════════════════════════
# CORE — AI REPLY ENGINE
# ════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = f"""
You are a helpful WhatsApp assistant for {config.BUSINESS_NAME},
a {config.BUSINESS_TYPE}.

SERVICES:
{config.SERVICES_TEXT}

HOURS: {config.HOURS}
LOCATION: {config.LOCATION}

AVAILABLE BOOKING SLOTS: {', '.join(config.AVAILABLE_SLOTS)}

BOOKING FLOW:
When a customer wants to book, collect in this order:
1. Their full name
2. Which service they want
3. Preferred date
4. Preferred time from the available slots above
Then confirm all details back to them clearly.

RULES:
- Always be warm, friendly and professional
- Keep replies to 3 sentences maximum
- Match the customer's language — English or Pidgin both fine
- Never invent services or prices not listed above
- When all booking details are confirmed, end your reply with exactly:
  "We look forward to seeing you! I will send your payment link now."
- If you cannot answer, say: "Let me connect you with our team."
"""


def get_ai_reply(phone: str, message: str) -> str:
    if phone not in conversations:
        conversations[phone] = []

    conversations[phone].append({"role": "user", "content": message})
    conversations[phone] = conversations[phone][-10:]

    try:
        response = ai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *conversations[phone]
            ],
            max_tokens=200,
            temperature=0.7
        )
        reply = response.choices[0].message.content.strip()
        conversations[phone].append({"role": "assistant", "content": reply})
        return reply

    except Exception as e:
        print(f"  AI error: {e}")
        return "Sorry, I'm having trouble right now. Please try again in a moment."


def detect_intent(message: str) -> str:
    msg = message.lower()
    if any(w in msg for w in ["price", "cost", "how much", "charge", "fee"]):
        return "Pricing"
    if any(w in msg for w in ["book", "appointment", "schedule", "date", "slot"]):
        return "Booking"
    if any(w in msg for w in ["complain", "refund", "problem", "issue"]):
        return "Complaint"
    return "General"


# ════════════════════════════════════════════════════════════════
# MAIN WEBHOOK
# ════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.values.get("Body", "").strip()
    sender_phone = request.values.get("From", "").strip()
    media_url    = request.values.get("MediaUrl0", "")
    media_type   = request.values.get("MediaContentType0", "")

    print(f"\n{'='*45}")
    print(f"From: {sender_phone}")

    # ── Voice note ────────────────────────────────────────────
    if media_url and "audio" in media_type:
        try:
            incoming_msg = transcribe_voice(media_url)
        except Exception as e:
            print(f"  Voice error: {e}")
            incoming_msg = "I sent a voice note"

    # ── Image ─────────────────────────────────────────────────
    elif media_url and "image" in media_type:
        try:
            image_reply = analyze_image(media_url)
            save_to_notion(sender_phone, "[Image received]", image_reply, "General")
            response = MessagingResponse()
            response.message(image_reply)
            return str(response)
        except Exception as e:
            print(f"  Image error: {e}")
            incoming_msg = "I sent an image"

    if not incoming_msg:
        return str(MessagingResponse())

    print(f"Message: {incoming_msg}")

    # ── Human handoff check ───────────────────────────────────
    if needs_handoff(incoming_msg):
        trigger_handoff(sender_phone, incoming_msg)
        reply = (
            "I'm connecting you with a member of our team right now. "
            "Someone will be with you shortly — thank you for your patience!"
        )
        save_to_notion(sender_phone, incoming_msg, reply, "Handoff")
        response = MessagingResponse()
        response.message(reply)
        return str(response)

    # ── Generate AI reply ─────────────────────────────────────
    reply  = get_ai_reply(sender_phone, incoming_msg)
    intent = detect_intent(incoming_msg)
    print(f"Reply: {reply}")
    print(f"Intent: {intent}")

    # ── Extract and save booking when details are complete ────
    if sender_phone in conversations:
        booking_saved = extract_and_save_booking(
            sender_phone,
            conversations[sender_phone]
        )
        if booking_saved:
            print(f"  Booking confirmed and saved for {sender_phone}")

    # ── Generate payment link on booking confirmation ─────────
    if "payment link" in reply.lower() or (
        intent == "Booking" and "look forward" in reply.lower()
    ):
        link = generate_payment_link(
            amount=config.DEPOSIT_AMOUNT,
            description=f"Booking deposit — {config.BUSINESS_NAME}"
        )
        if link:
            reply += f"\n\nPay your deposit here to secure your slot:\n{link}"

    # ── Save lead to Notion ───────────────────────────────────
    save_to_notion(sender_phone, incoming_msg, reply, intent)

    response = MessagingResponse()
    response.message(reply)
    return str(response)


# ════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def health():
    return f"WhatsApp AI Agent v2 — {config.BUSINESS_NAME} — running.", 200


# ════════════════════════════════════════════════════════════════
# SCHEDULER — Daily summary at 8am
# ════════════════════════════════════════════════════════════════

scheduler = BackgroundScheduler()
scheduler.add_job(send_daily_summary, "cron", hour=8, minute=0)
scheduler.start()


# ════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 50)
    print(f"  WHATSAPP AI AGENT v2")
    print(f"  Client: {config.BUSINESS_NAME}")
    print(f"  Features: Voice | Image | Payment | Booking | Handoff | Summary")
    print("=" * 50)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
