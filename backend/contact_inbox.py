"""Private contact history and support mail. All admin routes require the owner token."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from email.utils import parseaddr
from html.parser import HTMLParser

import requests
from flask import jsonify, request


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.skip += 1
        if tag in ('p', 'br', 'div'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.skip = max(0, self.skip - 1)
    def handle_data(self, text):
        if not self.skip: self.parts.append(text)


def plain_body(data):
    if data.get('text'): return str(data['text'])[:30000]
    parser = PlainText(); parser.feed(str(data.get('html') or '')[:100000])
    return ''.join(parser.parts).strip()[:30000]


def valid_signature(body, headers, secret):
    try:
        stamp = headers['svix-timestamp']; event = headers['svix-id']
        if abs(time.time() - int(stamp)) > 300: return False
        key = base64.b64decode(secret.removeprefix('whsec_'), validate=True)
        signed = event.encode() + b'.' + stamp.encode() + b'.' + body
        expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
        return any(hmac.compare_digest(expected, item[3:]) for item in headers.get('svix-signature', '').split() if item.startswith('v1,'))
    except (KeyError, ValueError, TypeError): return False


def install(app, connect):
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS support_contacts(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,created REAL NOT NULL,reply_token TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS support_messages(id TEXT PRIMARY KEY,contact_id TEXT NOT NULL,direction TEXT NOT NULL,provider TEXT NOT NULL,provider_id TEXT NOT NULL,thread_id TEXT NOT NULL DEFAULT '',subject TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,status TEXT NOT NULL,sender TEXT NOT NULL,recipient TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',UNIQUE(provider,provider_id));
        CREATE INDEX IF NOT EXISTS support_messages_contact ON support_messages(contact_id,created);
        CREATE TABLE IF NOT EXISTS support_sync(provider TEXT PRIMARY KEY,synced REAL NOT NULL,detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS support_webhook_events(id TEXT PRIMARY KEY,created REAL NOT NULL);
        ''')

    def refresh_contacts(db):
        rows = db.execute('''SELECT lower(email),min(created) FROM (
          SELECT email,created FROM email_accounts UNION ALL SELECT email,created FROM report_emails
          UNION ALL SELECT email,updated AS created FROM email_consents) GROUP BY lower(email)''').fetchall()
        for email, created in rows:
            ident = hashlib.sha256(email.encode()).hexdigest()[:32]
            db.execute('INSERT OR IGNORE INTO support_contacts VALUES(?,?,?,?)', (ident,email,created,secrets.token_urlsafe(24)))

    def contact(db, ident):
        db.row_factory = __import__('sqlite3').Row
        return db.execute('SELECT * FROM support_contacts WHERE id=?', (ident,)).fetchone()

    def state(db):
        sync = [dict(zip(('provider','synced','detail'), row)) for row in db.execute('SELECT provider,synced,detail FROM support_sync')]
        return {'sending_enabled': bool(os.getenv('RESEND_API_KEY') and os.getenv('RESEND_FROM') and os.getenv('RESEND_REPLY_TO')),
                'reply_to': os.getenv('RESEND_REPLY_TO',''), 'receiving_domain': os.getenv('ADSCAN_INBOX_REPLY_DOMAIN',''), 'automatic_receiving': bool(os.getenv('ADSCAN_INBOX_WEBHOOK_SECRET') and os.getenv('ADSCAN_INBOX_RECEIVING_KEY') and os.getenv('ADSCAN_INBOX_REPLY_DOMAIN')),
                'welcome_enabled': os.getenv('ADSCAN_WELCOME_ENABLED') == '1',
                'welcome_delay_seconds': 120, 'sync': sync}

    @app.get('/api/admin/contacts')
    def admin_contacts():
        query = str(request.args.get('q','')).strip().lower()[:254]
        try: offset = max(0,int(request.args.get('offset',0)))
        except ValueError: return jsonify(error='Invalid page.'),400
        with connect() as db:
            refresh_contacts(db); db.row_factory = __import__('sqlite3').Row
            # Literal search avoids LIKE wildcard surprises.
            where = 'WHERE instr(c.email,?)>0'; total = db.execute('SELECT count(*) FROM support_contacts c '+where,(query,)).fetchone()[0]
            rows = db.execute('''SELECT c.id,c.email,c.created,a.verified,
              EXISTS(SELECT 1 FROM email_consents ec WHERE lower(ec.email)=c.email AND opted_in=1 AND confirmed_at IS NOT NULL) marketing_opt_in,
              (SELECT count(*) FROM checkouts WHERE owner=a.owner) credit_checkouts,
              (SELECT count(*) FROM subscription_checkouts WHERE owner=a.owner) subscription_checkouts,
              (SELECT max(created) FROM (SELECT created FROM checkouts WHERE owner=a.owner UNION ALL SELECT created FROM subscription_checkouts WHERE owner=a.owner)) last_checkout,
              (SELECT count(*) FROM payments WHERE owner=a.owner)+(SELECT count(*) FROM subscription_periods WHERE owner=a.owner) confirmed_payments,
              (SELECT count(*) FROM support_messages WHERE contact_id=c.id AND direction='incoming') replies,
              (SELECT count(*) FROM support_messages WHERE contact_id=c.id AND direction='outgoing') messages_sent
              FROM support_contacts c LEFT JOIN email_accounts a ON lower(a.email)=c.email '''+where+''' ORDER BY coalesce(a.verified,c.created) DESC,c.id LIMIT 100 OFFSET ?''',(query,offset)).fetchall()
            return jsonify(contacts=[dict(row) for row in rows],total=total,offset=offset,**state(db))

    @app.get('/api/admin/contacts/<ident>/messages')
    def admin_messages(ident):
        with connect() as db:
            refresh_contacts(db); c = contact(db,ident)
            if not c: return jsonify(error='Contact not found.'),404
            messages = [dict(row) for row in db.execute('SELECT id,direction,provider,provider_id,thread_id,subject,body,created,status,sender,recipient,error FROM support_messages WHERE contact_id=? ORDER BY created,id',(ident,))]
            # Existing report delivery records are shown without private bearer links.
            for row in db.execute('SELECT id,created,status FROM report_emails WHERE lower(email)=?',(c['email'],)):
                messages.append({'id':row['id'],'direction':'outgoing','provider':'report email','subject':'Your AdScanVideo report link','body':'Report-link email. Private access link is not retained in this inbox.','created':row['created'],'status':row['status'],'sender':os.getenv('RESEND_FROM','AdScanVideo'),'recipient':c['email']})
            messages.sort(key=lambda x:x['created'])
            return jsonify(email=c['email'],messages=messages,**state(db))

    @app.post('/api/admin/contacts/<ident>/messages')
    def admin_send(ident):
        data = request.get_json(silent=True) or {}
        subject = str(data.get('subject','')).strip(); body = str(data.get('body','')).strip(); key = str(data.get('request_id',''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,80}',key) or not 1 <= len(subject) <= 200 or '\n' in subject or '\r' in subject or not 1 <= len(body) <= 10000:
            return jsonify(error='Add a subject and message, then retry.'),400
        if data.get('purpose') != 'support' or data.get('confirmed') is not True:
            return jsonify(error='Review and confirm this individual support message before sending.'),400
        if not (os.getenv('RESEND_API_KEY') and os.getenv('RESEND_FROM') and os.getenv('RESEND_REPLY_TO')):
            return jsonify(error='Support email sending is not configured.'),503
        with connect() as db:
            db.execute('BEGIN IMMEDIATE'); refresh_contacts(db); c = contact(db,ident)
            if not c: return jsonify(error='Contact not found.'),404
            old = db.execute('SELECT contact_id,subject,body,status FROM support_messages WHERE id=?',(key,)).fetchone()
            if old:
                if (old['contact_id'],old['subject'],old['body']) != (ident,subject,body): return jsonify(error='This send identifier belongs to another message.'),409
                return jsonify(id=key,status=old['status'],duplicate=True),200
            recent = db.execute("SELECT count(*) FROM support_messages WHERE direction='outgoing' AND provider='resend' AND created>?",(time.time()-3600,)).fetchone()[0]
            if recent >= 30: return jsonify(error='Hourly support email limit reached.'),429
            db.execute("INSERT INTO support_messages(id,contact_id,direction,provider,provider_id,subject,body,created,status,sender,recipient) VALUES(?,?,'outgoing','resend',?,?,?,?,'sending',?,?)",(key,ident,'pending:'+key,subject,body,time.time(),os.environ['RESEND_FROM'],c['email']))
        reply_to = os.environ['RESEND_REPLY_TO']
        domain = os.getenv('ADSCAN_INBOX_REPLY_DOMAIN','')
        if domain and re.fullmatch(r'[a-z0-9.-]+',domain): reply_to = 'reply+'+c['reply_token'].lower()+'@'+domain
        try:
            response = requests.post('https://api.resend.com/emails',headers={'Authorization':'Bearer '+os.environ['RESEND_API_KEY'],'Idempotency-Key':'support-'+key},json={'from':os.environ['RESEND_FROM'],'to':[c['email']],'reply_to':reply_to,'subject':subject,'text':body},timeout=15)
            response.raise_for_status(); provider_id = response.json().get('id')
            if not isinstance(provider_id,str) or not provider_id: raise ValueError('Missing provider receipt')
        except Exception:
            # A timeout can occur after provider acceptance. Never auto-resend an uncertain send.
            with connect() as db: db.execute("UPDATE support_messages SET status='unknown',error='Send could not be confirmed. Check the mail provider before trying another message.' WHERE id=?",(key,))
            return jsonify(id=key,status='unknown',error='Send could not be confirmed. Check the mail provider before sending again.'),502
        with connect() as db: db.execute("UPDATE support_messages SET status='accepted',provider_id=? WHERE id=?",(provider_id,key))
        return jsonify(id=key,status='accepted'),202

    @app.post('/api/admin/contacts/<ident>/mailbox-import')
    def admin_mailbox_import(ident):
        """Owner-only bridge for verified Gmail connector reads. Does not send mail."""
        data = request.get_json(silent=True) or {}; messages = data.get('messages',[])
        if not isinstance(messages,list) or len(messages)>100: return jsonify(error='Invalid message list.'),400
        parsed=[]
        for m in messages:
            if not isinstance(m,dict): return jsonify(error='Invalid message.'),400
            pid=str(m.get('provider_id','')); direction=m.get('direction'); subject=str(m.get('subject','')); body=str(m.get('body',''))
            if not re.fullmatch('[a-f0-9]{10,40}',pid) or direction not in ('incoming','outgoing') or len(subject)>500 or len(body)>30000: return jsonify(error='Invalid mailbox message.'),400
            try: created=float(m['created']); assert 0 < created <= time.time()+300
            except (KeyError,ValueError,TypeError,AssertionError): return jsonify(error='Invalid message date.'),400
            parsed.append((m,pid,direction,subject,body,created))
        with connect() as db:
            refresh_contacts(db); c=contact(db,ident)
            if not c: return jsonify(error='Contact not found.'),404
            for m,pid,direction,subject,body,created in parsed:
                sender=parseaddr(str(m.get('sender','')))[1].lower(); recipient=parseaddr(str(m.get('recipient','')))[1].lower()
                if (sender if direction=='incoming' else recipient) != c['email']: return jsonify(error='Message does not belong to this contact.'),400
            for m,pid,direction,subject,body,created in parsed:
                db.execute('INSERT OR IGNORE INTO support_messages(id,contact_id,direction,provider,provider_id,thread_id,subject,body,created,status,sender,recipient) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('gmail-'+pid,ident,direction,'gmail',pid,str(m.get('thread_id',''))[:100],subject,body,created,'received' if direction=='incoming' else 'sent',str(m.get('sender',''))[:320],str(m.get('recipient',''))[:320]))
            db.execute('INSERT OR REPLACE INTO support_sync VALUES(?,?,?)',('gmail',time.time(),'Gmail checked via the connected mailbox. Replies are imported when mailbox sync is run; dashboard refresh does not check Gmail.'))
        return jsonify(imported=len(parsed))

    @app.post('/api/email/inbound')
    def inbox_webhook():
        secret=os.getenv('ADSCAN_INBOX_WEBHOOK_SECRET',''); raw=request.get_data()
        if not secret or len(raw)>1024*1024 or not valid_signature(raw,request.headers,secret): return jsonify(error='Not authorized'),403
        event=request.get_json(silent=True) or {}; event_id=request.headers['svix-id']; payload=event.get('data') or {}; kind=event.get('type')
        with connect() as db:
            if db.execute('SELECT 1 FROM support_webhook_events WHERE id=?',(event_id,)).fetchone(): return jsonify(ok=True)
        if kind=='email.received':
            pid=str(payload.get('email_id',''))
            if not re.fullmatch(r'[a-f0-9-]{36}',pid): return jsonify(error='Invalid receipt'),400
            try:
                r=requests.get('https://api.resend.com/emails/receiving/'+pid,headers={'Authorization':'Bearer '+os.environ['ADSCAN_INBOX_RECEIVING_KEY']},timeout=15);r.raise_for_status();mail=r.json()
            except Exception: return jsonify(error='Mailbox retrieval unavailable'),503
            tokens=[]
            for address in mail.get('to',[]):
                address=parseaddr(address)[1].lower(); match=re.fullmatch(r'reply\+([a-zA-Z0-9_-]{32})@'+re.escape(os.getenv('ADSCAN_INBOX_REPLY_DOMAIN','')),address)
                if match: tokens.append(match[1])
            sender=parseaddr(str(mail.get('from','')))[1].lower()
            with connect() as db:
                for token in tokens:
                    c=db.execute('SELECT id,email FROM support_contacts WHERE lower(reply_token)=?',(token,)).fetchone()
                    if c and sender==c[1]:
                        db.execute('INSERT OR IGNORE INTO support_messages(id,contact_id,direction,provider,provider_id,subject,body,created,status,sender,recipient) VALUES(?,?,\'incoming\',\'resend\',?,?,?,?,\'received\',?,?)',('resend-'+pid,c[0],pid,str(mail.get('subject',''))[:500],plain_body(mail),time.time(),sender,c[1]))
                db.execute('INSERT OR REPLACE INTO support_sync VALUES(?,?,?)',('resend',time.time(),'Incoming email received through the signed Resend webhook.'))
                db.execute('INSERT OR IGNORE INTO support_webhook_events VALUES(?,?)',(event_id,time.time()))
        elif kind in ('email.delivered','email.bounced','email.failed','email.delivery_delayed'):
            status=kind.split('.')[1]
            with connect() as db:
                db.execute("UPDATE support_messages SET status=? WHERE provider='resend' AND provider_id=? AND direction='outgoing' AND status NOT IN ('bounced','failed') AND (status!='delivered' OR ? IN ('bounced','failed'))",(status,payload.get('email_id',''),status))
                db.execute('INSERT OR IGNORE INTO support_webhook_events VALUES(?,?)',(event_id,time.time()))
        return jsonify(ok=True)
