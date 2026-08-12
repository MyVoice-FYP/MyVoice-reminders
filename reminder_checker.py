"""
reminder_checker.py
Runs on a schedule via GitHub Actions (NOT on Farhan's or any user's own
device) - checks every registered account's reminders in Firestore, and
for any reminder whose time has arrived and hasn't been notified yet:
  1. Sends an email to that account's address (Gmail SMTP)
  2. Sends a phone push notification via ntfy.sh (free, no account needed)
Then marks it "email_notified" in Firestore so it's never sent twice. This
is a SEPARATE field from "notified", which the desktop app uses for its
own local system-tray popup while it's open - keeping them separate means
the app's local notifications and this script's cloud notifications never
interfere with each other.

This script is intentionally self-contained (doesn't import the desktop
app's cloud_sync.py) so it has no dependency on the app's folder layout -
it only needs: requests, google-auth, and three secrets provided as
environment variables (see the GitHub Actions workflow file).

Environment variables required:
  FIREBASE_SERVICE_ACCOUNT_JSON  - the full contents of the Firebase
                                    service account key JSON file
  GMAIL_ADDRESS                  - the Gmail address to send FROM
  GMAIL_APP_PASSWORD             - a Gmail "App Password" (NOT your normal
                                    Gmail password - see setup notes)
"""

import os
import json
import smtplib
import re
from email.mime.text import MIMEText
from datetime import datetime, timezone

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

FIRESTORE_SCOPES = ["https://www.googleapis.com/auth/datastore"]
NTFY_BASE_URL = "https://ntfy.sh"

_creds = None
_project_id = None


# ---------------- FIRESTORE REST HELPERS (mirrors cloud_sync.py) ----------------
def _load_credentials():
    global _creds, _project_id
    if _creds is None:
        key_json = os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]
        key_data = json.loads(key_json)
        _project_id = key_data["project_id"]
        _creds = service_account.Credentials.from_service_account_info(
            key_data, scopes=FIRESTORE_SCOPES
        )
    if not _creds.valid:
        _creds.refresh(Request())
    return _creds, _project_id


def _auth_headers():
    creds, _ = _load_credentials()
    if not creds.valid:
        creds.refresh(Request())
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}


def _base_url():
    _, project_id = _load_credentials()
    return f"https://firestore.googleapis.com/v1/projects/{project_id}/databases/(default)/documents"


def _sanitize_email_for_doc_id(email):
    return email.replace(".", "_dot_").replace("@", "_at_")


def _encode_value(v):
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    raise ValueError(f"Unsupported type: {type(v)}")


def _decode_value(v):
    if "stringValue" in v:
        return v["stringValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "booleanValue" in v:
        return v["booleanValue"]
    raise ValueError(f"Unsupported Firestore value: {v}")


def _get_document(path):
    url = f"{_base_url()}/{path}"
    resp = requests.get(url, headers=_auth_headers())
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("fields", {})


def _set_document(path, fields_dict):
    url = f"{_base_url()}/{path}"
    body = {"fields": {k: _encode_value(v) for k, v in fields_dict.items()}}
    resp = requests.patch(url, headers=_auth_headers(), json=body)
    resp.raise_for_status()


def _list_documents(path):
    url = f"{_base_url()}/{path}"
    docs = []
    page_token = None
    while True:
        params = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, headers=_auth_headers(), params=params)
        resp.raise_for_status()
        data = resp.json()
        docs.extend(data.get("documents", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return docs


# ---------------- APP-LEVEL HELPERS ----------------
def get_all_user_emails():
    """Lists every registered account's email from the users/ collection."""
    emails = []
    for doc in _list_documents("users"):
        fields = doc.get("fields", {})
        if "email" in fields:
            emails.append(_decode_value(fields["email"]))
    return emails


def load_reminders(email):
    user_id = _sanitize_email_for_doc_id(email)
    fields = _get_document(f"users/{user_id}")
    if not fields or "reminders_json" not in fields:
        return []
    try:
        return json.loads(_decode_value(fields["reminders_json"]))
    except (TypeError, ValueError):
        return []


def save_reminders(email, reminders):
    """Merges with the existing account document (preserves
    password_hash/name/etc.) instead of overwriting it."""
    user_id = _sanitize_email_for_doc_id(email)
    existing = _get_document(f"users/{user_id}")
    fields = {k: _decode_value(v) for k, v in existing.items()} if existing else {}
    fields["email"] = email
    fields["reminders_json"] = json.dumps(reminders)
    _set_document(f"users/{user_id}", fields)


# ---------------- NOTIFICATION SENDERS ----------------
def send_email(to_email, subject, body):
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_email

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_address, gmail_app_password)
        server.send_message(msg)


def ntfy_topic_for(email):
    """Deterministic, unique-per-account ntfy.sh topic name. The user
    subscribes to this exact topic in the ntfy app to get push notifications."""
    safe = re.sub(r"[^a-zA-Z0-9]", "-", email.lower())
    return f"myvoice-{safe}"


def send_push(email, title, message):
    topic = ntfy_topic_for(email)
    try:
        requests.post(
            f"{NTFY_BASE_URL}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"  [ntfy] push failed for {email}: {e}")


# ---------------- MAIN ----------------
def main():
    now = datetime.now()  # naive local time - matches how the desktop app
                           # saves reminder datetimes (Qt.ISODate, no tz)

    emails = get_all_user_emails()
    print(f"Checking reminders for {len(emails)} account(s)...")

    for email in emails:
        reminders = load_reminders(email)
        if not reminders:
            continue

        due_now = []
        changed = False

        for r in reminders:
            if r.get("email_notified"):
                continue
            try:
                reminder_dt = datetime.fromisoformat(r["datetime"])
            except (KeyError, ValueError):
                continue
            if reminder_dt <= now:
                due_now.append(r)
                r["email_notified"] = True
                changed = True

        for r in due_now:
            print(f"  Notifying {email}: '{r['title']}'")
            try:
                send_email(
                    email,
                    f"MyVoice Reminder: {r['title']}",
                    f"Your reminder \"{r['title']}\" is due now.",
                )
            except Exception as e:
                print(f"  [email] failed for {email}: {e}")
            send_push(email, "MyVoice Reminder", r["title"])

        if changed:
            save_reminders(email, reminders)

    print("Done.")


if __name__ == "__main__":
    main()
