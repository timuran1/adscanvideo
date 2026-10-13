"""Optional, scoped report links. Never grants account or paid export access."""
import hashlib
import os
import re
import secrets
import time
from html import escape
import requests
from flask import request, jsonify


def configured():
    return os.getenv('ADSCAN_REPORT_EMAIL_ENABLED') == '1' and bool(os.getenv('RESEND_API_KEY')) and bool(os.getenv('RESEND_FROM'))


def install(app, connect, jobs, owner, ip_key):
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS report_emails (id TEXT PRIMARY KEY, owner TEXT NOT NULL, ip TEXT NOT NULL, job TEXT NOT NULL, email TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, expires REAL NOT NULL, created REAL NOT NULL, status TEXT NOT NULL, provider_id TEXT NOT NULL DEFAULT '', marketing_requested INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS email_consents (email TEXT PRIMARY KEY, opted_in INTEGER NOT NULL DEFAULT 0, confirmed_at REAL, updated REAL NOT NULL);
        ''')

    @app.post('/api/reports/<jid>/email')
    def email_report(jid):
        who, _ = owner(True)
        if not who:
            return jsonify(error='Report not found.'), 404
        if not configured():
            return jsonify(error='Email delivery is not available yet. Your on-screen report is unchanged.'), 503
        data = request.get_json(silent=True) or {}
        address = str(data.get('email', '')).strip().lower()
        if len(address) > 254 or not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+', address):
            return jsonify(error='Enter a valid email address.'), 400
        if not isinstance(data.get('marketing_opt_in', False), bool):
            return jsonify(error='Invalid preference.'), 400
        now = time.time(); ident = secrets.token_hex(16); token = secrets.token_urlsafe(32)
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            access = db.execute('SELECT owner FROM access WHERE job=?', (jid,)).fetchone()
            job = jobs.get(jid)
            if not access or access[0] != who or not job:
                return jsonify(error='Report not found.'), 404
            if job.get('status') != 'done' or not job.get('result'):
                return jsonify(error='Wait until your report is complete.'), 409
            recent = db.execute('SELECT COUNT(*) FROM report_emails WHERE created>? AND (owner=? OR ip=? OR email=?)', (now-86400, who, ip_key(), address)).fetchone()[0]
            short = db.execute('SELECT 1 FROM report_emails WHERE created>? AND (owner=? OR email=?)', (now-300, who, address)).fetchone()
            if recent >= 5 or short:
                return jsonify(error='Please wait before requesting another email.'), 429
            db.execute('INSERT INTO report_emails(id,owner,ip,job,email,token_hash,expires,created,status,marketing_requested) VALUES(?,?,?,?,?,?,?,?,?,?)', (ident,who,ip_key(),jid,address,hashlib.sha256(token.encode()).hexdigest(),now+7*86400,now,'pending',int(data.get('marketing_opt_in',False))))
        link = 'https://adscanvideo.com/report/#' + token
        text = 'Your AdScanVideo report is ready.\n\nOpen your private report link: '+link+'\n\nThis link expires in seven days and lets its holder read this report only. Check important details against the original footage. It does not unlock paid exports or other reports.\n'
        if data.get('marketing_opt_in'):
            text += '\nOn the report page, confirm separately if you want occasional marketing tips.\n'
        payload = {'from':os.environ['RESEND_FROM'],'to':[address],'subject':'Your AdScanVideo report link','text':text,'html':'<p>Your AdScanVideo report is ready.</p><p><a href="'+escape(link,quote=True)+'">Open your private report</a></p><p>This link expires in seven days. Anyone with the link can read this report. It does not unlock paid exports or account access.</p>'}
        if os.getenv('RESEND_REPLY_TO'):
            payload['reply_to'] = os.environ['RESEND_REPLY_TO']
        try:
            response = requests.post('https://api.resend.com/emails',headers={'Authorization':'Bearer '+os.environ['RESEND_API_KEY'],'Idempotency-Key':'report-'+ident},json=payload,timeout=15)
            response.raise_for_status(); provider_id = response.json().get('id')
            if not provider_id:
                raise ValueError('Missing acceptance ID')
        except (requests.RequestException, ValueError):
            with connect() as db:
                db.execute("UPDATE report_emails SET status='failed' WHERE id=?",(ident,))
            return jsonify(error='Email delivery could not be confirmed. Your report stays available here. Please wait five minutes before retrying.'), 502
        with connect() as db:
            db.execute("UPDATE report_emails SET status='accepted',provider_id=? WHERE id=?",(provider_id,ident))
        return jsonify(message='Email accepted by Resend. Check your inbox and spam folder. Delivery is not yet confirmed.'), 202

    def link_record(db):
        token = request.headers.get('Authorization','').removeprefix('Bearer ')
        if not re.fullmatch(r'[A-Za-z0-9_-]{40,64}',token):
            return None
        return db.execute("SELECT job,email,marketing_requested FROM report_emails WHERE token_hash=? AND expires>? AND status='accepted'",(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()

    @app.get('/api/emailed-report')
    def emailed_report():
        with connect() as db:
            row = link_record(db)
            if not row:
                return jsonify(error='This report link is invalid or expired.'),404
            job = jobs.get(row[0])
            if not job or job.get('status') != 'done':
                return jsonify(error='This report is no longer available.'),404
            consent=db.execute('SELECT opted_in FROM email_consents WHERE email=?',(row[1],)).fetchone()
        return jsonify(title=job.get('title','Analysis report'),result=job.get('result',''),marketing_requested=bool(row[2]),marketing_opt_in=bool(consent and consent[0]))

    @app.post('/api/report-email-preference')
    def email_preference():
        value=(request.get_json(silent=True) or {}).get('marketing_opt_in')
        if not isinstance(value,bool):
            return jsonify(error='Invalid preference.'),400
        with connect() as db:
            row=link_record(db)
            if not row:
                return jsonify(error='This link is invalid or expired.'),404
            db.execute('INSERT INTO email_consents(email,opted_in,confirmed_at,updated) VALUES(?,?,?,?) ON CONFLICT(email) DO UPDATE SET opted_in=excluded.opted_in,confirmed_at=excluded.confirmed_at,updated=excluded.updated',(row[1],int(value),time.time(),time.time()))
        return jsonify(marketing_opt_in=value)
