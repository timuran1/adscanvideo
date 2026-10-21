"""Verified email login, single-use links and private aggregate signup metrics."""
import hashlib
import os
import re
import secrets
import time
from html import escape
import requests
import welcome_email
from flask import request, jsonify


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def configured():
    return os.getenv('ADSCAN_EMAIL_AUTH_ENABLED') == '1' and bool(os.getenv('RESEND_API_KEY') and os.getenv('RESEND_FROM'))


def install(app, connect, owner, ip_key):
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS email_accounts (email TEXT PRIMARY KEY, owner TEXT UNIQUE NOT NULL, created REAL NOT NULL, verified REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS email_login_links (hash TEXT PRIMARY KEY, email TEXT NOT NULL, source_owner TEXT NOT NULL, ip TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, consumed REAL, status TEXT NOT NULL, consent INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS email_sessions (hash TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS email_owner_aliases (source TEXT PRIMARY KEY, owner TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS email_preferences (hash TEXT PRIMARY KEY, email TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS email_consents (email TEXT PRIMARY KEY, opted_in INTEGER NOT NULL DEFAULT 0, confirmed_at REAL, updated REAL NOT NULL);
        ''')
        welcome_email.install_schema(db)

    def resolve_reference(db, who):
        row = db.execute('SELECT owner FROM email_owner_aliases WHERE source=?', (who,)).fetchone()
        return row[0] if row else who

    def resolve_token(token):
        hashed = digest(token)
        with connect() as db:
            if token.startswith('as_'):
                row = db.execute('SELECT owner FROM email_sessions WHERE hash=? AND expires>?', (hashed,time.time())).fetchone()
                return row[0] if row else None
            # Retired anonymous keys must not gain access to a verified email account.
            if db.execute('SELECT 1 FROM email_owner_aliases WHERE source=?', (hashed,)).fetchone():
                return None
        return hashed

    def account_email(who):
        with connect() as db:
            row = db.execute('SELECT email FROM email_accounts WHERE owner=?',(who,)).fetchone()
        return row[0] if row else None

    @app.post('/api/auth/email')
    def request_email_login():
        if not configured():
            return jsonify(error='Email sign in is temporarily unavailable.'),503
        body = request.get_json(silent=True) or {}
        if not isinstance(body,dict):
            return jsonify(error='Enter a valid email address.'),400
        email = body.get('email','')
        consent = body.get('marketing_opt_in',False)
        if not isinstance(email,str) or len(email)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email.strip()) or not isinstance(consent,bool):
            return jsonify(error='Enter a valid email address and preference.'),400
        email = email.strip().lower()
        source,_ = owner(True)
        if not source:
            return jsonify(error='Reload the analyzer before signing in.'),401
        raw=secrets.token_urlsafe(32); now=time.time(); hashed=digest(raw)
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            recent = db.execute('SELECT COUNT(*) FROM email_login_links WHERE created>? AND (email=? OR ip=?)',(now-3600,email,ip_key())).fetchone()[0]
            short = db.execute('SELECT 1 FROM email_login_links WHERE created>? AND (email=? OR ip=?)',(now-60,email,ip_key())).fetchone()
            if recent>=5 or short:
                return jsonify(error='Please wait before requesting another sign in email.'),429
            db.execute('INSERT INTO email_login_links VALUES(?,?,?,?,?,?,NULL,?,?)',(hashed,email,source,ip_key(),now,now+900,'pending',int(consent)))
        link='https://adscanvideo.com/login/#'+raw
        text='Sign in to AdScanVideo\n\n'+link+'\n\nThis link expires in 15 minutes and works once. Open it in the browser where you analyzed your video to keep that report in your account. If you did not request it, ignore this email.'
        if consent:
            text+='\n\nYou requested occasional AdScanVideo tips. Signing in confirms that preference. You can unsubscribe anytime from your email preferences.'
        payload={'from':os.environ['RESEND_FROM'],'to':[email],'subject':'Your AdScanVideo sign in link','text':text,'html':'<p>Sign in to AdScanVideo</p><p><a href="'+escape(link,quote=True)+'">Sign in securely</a></p><p>'+escape(text.split('\n\n',2)[-1]).replace('\n','<br>')+'</p>'}
        try:
            sent=requests.post('https://api.resend.com/emails',headers={'Authorization':'Bearer '+os.environ['RESEND_API_KEY'],'Idempotency-Key':'login-'+hashed},json=payload,timeout=15)
            sent.raise_for_status()
            if not sent.json().get('id'):
                raise ValueError('No acceptance ID')
        except (requests.RequestException,ValueError):
            with connect() as db:db.execute("UPDATE email_login_links SET status='failed' WHERE hash=?",(hashed,))
            return jsonify(error='Could not send the sign in email. Please wait a minute and try again.'),502
        with connect() as db:db.execute("UPDATE email_login_links SET status='accepted' WHERE hash=?",(hashed,))
        return jsonify(message='Check your inbox for a sign in link. It expires in 15 minutes.'),202

    @app.post('/api/auth/verify')
    def verify_email_login():
        body=request.get_json(silent=True) or {}
        raw=body.get('token','') if isinstance(body,dict) else ''
        if not isinstance(raw,str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,64}',raw):
            return jsonify(error='This sign in link is invalid or expired.'),400
        source,_=owner(True);now=time.time(); session='as_'+secrets.token_urlsafe(32)
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT email,source_owner,consent FROM email_login_links WHERE hash=? AND expires>? AND consumed IS NULL AND status='accepted'",(digest(raw),now)).fetchone()
            if not row:
                return jsonify(error='This sign in link is invalid or expired. Request a new link.'),400
            email,requested_source,consent=row
            account=db.execute('SELECT owner FROM email_accounts WHERE email=?',(email,)).fetchone()
            canonical=account[0] if account else digest(secrets.token_urlsafe(32))
            if not account:
                db.execute('INSERT INTO email_accounts VALUES(?,?,?,?)',(email,canonical,now,now))
                welcome_email.enqueue(db,email,now)
            # Only attach this browser's proven anonymous holdings. Never attach a
            # different signed-in account or retain an old anonymous key as login.
            migrated=False
            if source and source==requested_source and source!=canonical and not db.execute('SELECT 1 FROM email_accounts WHERE owner=?',(source,)).fetchone():
                for table in ('access','payments','checkouts','subscription_checkouts','subscriptions','subscription_periods','export_unlocks','report_emails'):
                    db.execute('UPDATE '+table+' SET owner=? WHERE owner=?',(canonical,source))
                db.execute('INSERT OR REPLACE INTO email_owner_aliases VALUES(?,?)',(source,canonical));migrated=True
            db.execute('UPDATE email_login_links SET consumed=? WHERE hash=?',(now,digest(raw)))
            db.execute('INSERT INTO email_sessions VALUES(?,?,?,?)',(digest(session),canonical,now,now+30*86400))
            if consent:
                db.execute('INSERT INTO email_consents VALUES(?,1,?,?) ON CONFLICT(email) DO UPDATE SET opted_in=1,confirmed_at=excluded.confirmed_at,updated=excluded.updated',(email,now,now))
        return jsonify(owner_token=session,email=email,new_signup=not bool(account),reports_attached=migrated)

    @app.get('/api/auth/session')
    def session_status():
        who,_=owner(True)
        email=account_email(who) if who else None
        return jsonify(authenticated=bool(email),email=email)

    @app.post('/api/auth/logout')
    def logout_email():
        raw=request.headers.get('Authorization','').removeprefix('Bearer ')
        with connect() as db:db.execute('DELETE FROM email_sessions WHERE hash=?',(digest(raw),))
        return jsonify(logged_out=True)

    @app.post('/api/auth/preferences-link')
    def preferences_link():
        who,_=owner(True);email=account_email(who) if who else None
        if not email:
            return jsonify(error='Sign in to manage email preferences.'),401
        raw=secrets.token_urlsafe(32)
        with connect() as db:
            db.execute('INSERT INTO email_preferences VALUES(?,?,?)',(digest(raw),email,time.time()))
        return jsonify(url='https://adscanvideo.com/email-preferences/#'+raw)

    @app.post('/api/email/unsubscribe')
    def unsubscribe():
        body=request.get_json(silent=True) or {};raw=body.get('token','') if isinstance(body,dict) else ''
        if not isinstance(raw,str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,64}',raw):
            return jsonify(error='Invalid email preferences link.'),400
        with connect() as db:
            row=db.execute('SELECT email FROM email_preferences WHERE hash=?',(digest(raw),)).fetchone()
            if not row:
                return jsonify(error='Invalid email preferences link.'),400
            db.execute('INSERT INTO email_consents VALUES(?,0,NULL,?) ON CONFLICT(email) DO UPDATE SET opted_in=0,updated=excluded.updated',(row[0],time.time()))
        return jsonify(message='You are unsubscribed from marketing emails. Requested report and sign in emails can still be delivered.')

    return {'resolve_token':resolve_token,'resolve_reference':resolve_reference,'account_email':account_email}
