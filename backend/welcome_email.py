"""Durable, one-time account welcome. Run the worker from the systemd timer."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

import requests

SUBJECT = 'Welcome to AdScanVideo'
BODY = ('Thanks for trying AdScanVideo! Your email is verified and your account is ready.\n\n'
        'If you need help with your video analysis or run into trouble with checkout, '
        'just reply to this email. Your reply goes directly to our support inbox.\n\n'
        'You can return to your analysis at https://adscanvideo.com/\n\n'
        'Timur\nAdScanVideo\n\n'
        'This is a one-time account welcome, not a marketing subscription.')


def install_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS welcome_emails (
      email TEXT PRIMARY KEY, message_id TEXT UNIQUE NOT NULL, created REAL NOT NULL,
      due REAL NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
      last_attempt REAL, first_attempt REAL, payload TEXT NOT NULL)''')


def enqueue(db, email, now):
    if os.getenv('ADSCAN_WELCOME_ENABLED') != '1':
        return
    domain = os.getenv('ADSCAN_INBOX_REPLY_DOMAIN', '')
    if not (os.getenv('RESEND_API_KEY') and os.getenv('RESEND_FROM')
            and os.getenv('ADSCAN_INBOX_RECEIVING_KEY') and os.getenv('ADSCAN_INBOX_WEBHOOK_SECRET')
            and re.fullmatch(r'[a-z0-9.-]+', domain)):
        return
    ident = hashlib.sha256(email.encode()).hexdigest()[:32]
    db.execute('INSERT OR IGNORE INTO support_contacts VALUES(?,?,?,?)',
               (ident, email, now, secrets.token_urlsafe(24)))
    token = db.execute('SELECT reply_token FROM support_contacts WHERE id=?', (ident,)).fetchone()[0]
    key = 'welcome-' + ident
    payload = {'from': os.environ['RESEND_FROM'], 'to': [email],
               'reply_to': 'reply+' + token.lower() + '@' + domain,
               'subject': SUBJECT, 'text': BODY}
    inserted = db.execute('''INSERT OR IGNORE INTO welcome_emails
      (email,message_id,created,due,status,payload) VALUES(?,?,?,?,'scheduled',?)''',
      (email, key, now, now + 120, json.dumps(payload))).rowcount
    if inserted:
        db.execute('''INSERT INTO support_messages
          (id,contact_id,direction,provider,provider_id,subject,body,created,status,sender,recipient)
          VALUES(?,?,'outgoing','resend',?,?,?,?,'scheduled',?,?)''',
          (key, ident, 'pending:' + key, SUBJECT, BODY, now, payload['from'], email))


def process_due(connect, now=None):
    """Claim transactionally; reuse immutable payload/key for bounded safe retries."""
    now = time.time() if now is None else now
    if os.getenv('ADSCAN_WELCOME_ENABLED') != '1' or not os.getenv('RESEND_API_KEY'):
        return 0
    processed = 0
    for _ in range(10):
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT email,message_id,payload,attempts,first_attempt
              FROM welcome_emails WHERE
              (status IN ('scheduled','retry') AND due<=?) OR
              (status='sending' AND last_attempt<=?) ORDER BY due LIMIT 1''',
              (now, now - 90)).fetchone()
            if not row:
                break
            email, key, raw_payload, attempts, first = row
            # Resend retains idempotency keys for 24 hours. Never retry beyond 22h.
            if attempts >= 8 or (first is not None and now - first >= 22 * 3600):
                db.execute("UPDATE welcome_emails SET status='blocked' WHERE email=?", (email,))
                db.execute("UPDATE support_messages SET status='unknown',error=? WHERE id=?",
                           ('Welcome send needs review; automatic retries stopped.', key))
                continue
            db.execute("UPDATE welcome_emails SET status='sending',attempts=attempts+1,last_attempt=?,first_attempt=coalesce(first_attempt,?) WHERE email=?", (now, now, email))
            db.execute("UPDATE support_messages SET status='sending' WHERE id=?", (key,))
        status = 'retry'; provider_id = None
        try:
            response = requests.post('https://api.resend.com/emails',
                headers={'Authorization': 'Bearer ' + os.environ['RESEND_API_KEY'],
                         'Idempotency-Key': key}, json=json.loads(raw_payload), timeout=15)
            response.raise_for_status()
            provider_id = response.json().get('id')
            if not isinstance(provider_id, str) or not provider_id:
                raise ValueError('Missing provider receipt')
            status = 'accepted'
        except requests.HTTPError as exc:
            if exc.response is not None and 400 <= exc.response.status_code < 500 and exc.response.status_code not in (429,):
                status = 'blocked'
        except (requests.RequestException, ValueError):
            pass
        with connect() as db:
            db.execute('UPDATE welcome_emails SET status=?,due=? WHERE email=?',
                       (status, now + min(3600, 60 * 2 ** attempts), email))
            if status == 'accepted':
                db.execute("UPDATE support_messages SET status='accepted',provider_id=?,created=?,error='' WHERE id=?", (provider_id, time.time(), key))
            else:
                db.execute('UPDATE support_messages SET status=?,error=? WHERE id=?',
                           ('unknown' if status == 'blocked' else 'retry',
                            'Welcome email awaiting provider confirmation.' if status == 'retry' else 'Welcome send needs review; automatic retries stopped.', key))
        processed += 1
    return processed


if __name__ == '__main__':
    path = Path(os.getenv('ADSCAN_DATA_DIR', str(Path(__file__).parent))) / 'jobs.db'
    def connect():
        return sqlite3.connect(path, timeout=30)
    process_due(connect)
