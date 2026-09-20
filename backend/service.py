"""Admission, durable allowances, private jobs, bounded workers and Stripe checkout."""
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

import requests
from flask import request, jsonify
from network_guard import validate_url

ROOT = Path(__file__).parent
TERMINAL = ('done', 'error')
MAX_FILE = 200 * 1024 * 1024


def start_worker(target, payload):
    threading.Thread(target=target, args=(payload,), daemon=True).start()


def install(app, jobs, db_path, runner):
    def connect():
        db = sqlite3.connect(db_path, timeout=30)
        db.execute('PRAGMA busy_timeout=30000')
        return db

    if not os.getenv('ADSCAN_QUOTA_SECRET'):
        raise RuntimeError('ADSCAN_QUOTA_SECRET must be configured before startup')

    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS access (job TEXT PRIMARY KEY, owner TEXT NOT NULL, ip TEXT NOT NULL, day TEXT NOT NULL, tier TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS access_quota ON access(day,ip,owner);
        CREATE TABLE IF NOT EXISTS payments (session TEXT PRIMARY KEY, owner TEXT NOT NULL, credits INTEGER NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS checkouts (session TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL);
        ''')

    def owner(required=False):
        value = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if re.fullmatch(r'[A-Za-z0-9_-]{32,128}', value):
            return hashlib.sha256(value.encode()).hexdigest(), value
        if required:
            return None, None
        value = secrets.token_urlsafe(32)
        return hashlib.sha256(value.encode()).hexdigest(), value

    def ip_key():
        # X-Real-IP is accepted only from the loopback nginx proxy; nginx overwrites it.
        ip = request.remote_addr or ''
        if ip in ('127.0.0.1', '::1'):
            ip = request.headers.get('X-Real-IP', ip)
        try:
            parsed = ipaddress.ip_address(ip)
            if parsed.version == 6:
                ip = str(ipaddress.ip_network(str(parsed) + '/64', strict=False))
            else:
                ip = str(parsed)
        except ValueError:
            ip = request.remote_addr or 'unknown'
        # Stable keyed hash avoids storing visitor addresses in the quota table.
        return hmac.new(os.environ['ADSCAN_QUOTA_SECRET'].encode(), ip.encode(), hashlib.sha256).hexdigest()

    def allowance(db, who, ip):
        day = time.strftime('%Y-%m-%d', time.gmtime())
        used = db.execute("SELECT COUNT(*) FROM access a JOIN jobs j ON j.id=a.job WHERE a.day=? AND (a.ip=? OR a.owner=?) AND a.tier='free' AND j.status!='error'", (day, ip, who)).fetchone()[0]
        paid = db.execute('SELECT COALESCE(SUM(credits),0) FROM payments WHERE owner=?', (who,)).fetchone()[0]
        spent = db.execute("SELECT COUNT(*) FROM access a JOIN jobs j ON j.id=a.job WHERE a.owner=? AND a.tier='paid' AND j.status!='error'", (who,)).fetchone()[0]
        return {'free_remaining': max(0, 1-used), 'paid_credits': max(0, paid-spent), 'reset_at': (int(time.time())//86400+1)*86400, 'billing_enabled': billing_enabled(), 'max_duration': 600}

    def billing_enabled():
        return bool(os.getenv('STRIPE_SECRET_KEY') and os.getenv('STRIPE_WEBHOOK_SECRET') and os.getenv('STRIPE_PRICE_VIDEO'))

    @app.before_request
    def access_control():
        if request.method == 'OPTIONS':
            return None
        if request.path in ('/api/recover', '/api/reap'):
            expected = os.getenv('ADSCAN_ADMIN_TOKEN', '')
            actual = request.headers.get('Authorization', '').removeprefix('Bearer ')
            if not expected or not hmac.compare_digest(actual, expected):
                return jsonify(error='Not authorized'), 403
        if request.endpoint in ('status', 'result'):
            who, _ = owner(True)
            jid = (request.view_args or {}).get('job_id')
            with connect() as db:
                row = db.execute('SELECT owner FROM access WHERE job=?', (jid,)).fetchone()
            # Older deployed clients cannot send Authorization. Only jobs explicitly
            # created without it use their 128-bit random ID as a bearer capability.
            # Authenticated-browser jobs never allow this compatibility path.
            if not who and isinstance(jid, str) and re.fullmatch(r'[a-f0-9]{32}', jid):
                who = hashlib.sha256(jid.encode()).hexdigest()
            if not who or not row or not hmac.compare_digest(row[0], who):
                return jsonify(error='Analysis not found.', code='not_found'), 404

    @app.after_request
    def private_response(response):
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/usage')
    def usage():
        who, token = owner()
        with connect() as db:
            data = allowance(db, who, ip_key())
        return jsonify(**data, owner_token=token)

    def reserve():
        who, token = owner()
        ip = ip_key()
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            data = allowance(db, who, ip)
            if not data['free_remaining'] and not data['paid_credits']:
                return None, (jsonify(error='Your free analysis for today is used. Buy a credit to analyze another video, or return after the daily reset.', code='payment_required', **data), 402)
            recent = db.execute('SELECT COUNT(*) FROM access WHERE (ip=? OR owner=?) AND created>?', (ip, who, time.time()-86400)).fetchone()[0]
            if recent >= 20:
                return None, (jsonify(error='Too many attempts. Please try again tomorrow.', code='rate_limited'), 429)
            active = db.execute("SELECT COUNT(*) FROM jobs WHERE status NOT IN ('done','error')").fetchone()[0]
            if active >= 1:
                return None, (jsonify(error='The analyzer is busy. Please try again in a minute. Your allowance has not been used.', code='busy'), 503, {'Retry-After': '60'})
            jid = secrets.token_hex(16)
            if owner(True)[0] is None:
                token = jid
                who = hashlib.sha256(jid.encode()).hexdigest()
            now = time.time()
            db.execute('INSERT INTO jobs(id,status,created,updated) VALUES(?,?,?,?)', (jid, 'starting', now, now))
            db.execute('INSERT INTO access VALUES(?,?,?,?,?,?)', (jid, who, ip, time.strftime('%Y-%m-%d', time.gmtime()), 'free' if data['free_remaining'] else 'paid', now))
        return (jid, token), None

    def supervise(payload):
        jid = payload['job_id']
        process = None
        try:
            env = dict(os.environ, ADSCAN_JOB_WORKER='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
            process = subprocess.Popen([sys.executable, str(ROOT/'job_worker.py')], stdin=subprocess.PIPE, start_new_session=True, env=env)
            deadline = time.monotonic() + int(os.getenv('ADSCAN_JOB_TIMEOUT', '480'))
            pending_input = json.dumps(payload).encode()
            while True:
                try:
                    process.communicate(pending_input, timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
                    if time.monotonic() >= deadline:
                        raise
                    try:
                        disk_bytes = sum(p.stat().st_size for p in Path(payload['work_dir']).rglob('*') if p.is_file())
                    except FileNotFoundError:
                        disk_bytes = 0
                    if disk_bytes > 250 * 1024 * 1024:
                        raise subprocess.TimeoutExpired(process.args, 0)
            if jobs.get(jid, {}).get('status') not in TERMINAL:
                jobs[jid].update(status='error', error='Analysis stopped unexpectedly. Please retry; your allowance was restored.')
        except subprocess.TimeoutExpired:
            jobs[jid].update(status='error', error='This video took too long. Try a shorter video or upload it directly; your allowance was restored.')
        except Exception:
            app.logger.exception('Analysis worker failed for %s', jid)
            jobs[jid].update(status='error', error='Unable to start analysis. Please try again; your allowance was restored.')
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            shutil.rmtree(payload['work_dir'], ignore_errors=True)

    def launch(reservation, source, question, mode, directory):
        jid, token = reservation
        try:
            start_worker(supervise, {'job_id': jid, 'url': source, 'question': question, 'mode': mode, 'work_dir': str(directory)})
        except Exception:
            jobs[jid].update(status='error', error='Could not start analysis. Please try again.')
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return jsonify(job_id=jid, owner_token=token), 202

    def inputs(data):
        question, mode = data.get('question', ''), data.get('mode', 'summary')
        if not isinstance(question, str) or len(question) > 4000 or mode not in ('summary', 'shots', 'ads', 'ad', 'cinema', 'podcast', 'marketing', 'tutorial'):
            raise ValueError('Choose a valid analysis mode and a question under 4,000 characters.')
        return question, mode

    @app.post('/api/analyze')
    def analyze():
        data = request.get_json(silent=True)
        try:
            if not isinstance(data, dict) or not isinstance(data.get('url'), str):
                raise ValueError('Provide a video URL.')
            question, mode = inputs(data)
            source = validate_url(data['url'].strip())
        except ValueError as e:
            return jsonify(error=str(e), code='invalid_input'), 400
        reservation, error = reserve()
        if error:
            return error
        directory = Path(tempfile.mkdtemp(prefix='adscan-job-'))
        return launch(reservation, source, question, mode, directory)

    @app.post('/api/analyze-upload')
    def upload():
        f = request.files.get('file')
        if not f or Path(f.filename or '').suffix.lower() not in ('.mp4', '.mov', '.webm', '.mkv'):
            return jsonify(error='Upload an MP4, MOV, WebM or MKV video.', code='invalid_input'), 400
        try:
            question, mode = inputs(request.form)
        except ValueError as e:
            return jsonify(error=str(e), code='invalid_input'), 400
        directory = Path(tempfile.mkdtemp(prefix='adscan-job-'))
        reservation = None
        try:
            # Use a fixed basename. User-supplied path components never reach disk.
            path = directory / ('upload' + Path(f.filename).suffix.lower())
            count = 0
            with path.open('wb') as out:
                while chunk := f.stream.read(1024*1024):
                    count += len(chunk)
                    if count > MAX_FILE:
                        return jsonify(error='Maximum video size is 200 MB.'), 413
                    out.write(chunk)
            if count == 0:
                return jsonify(error='The uploaded file is empty.'), 400
            reservation, error = reserve()
            if error:
                return error
            return launch(reservation, str(path), question, mode, directory)
        finally:
            if reservation is None:
                shutil.rmtree(directory, ignore_errors=True)

    @app.post('/api/billing/checkout')
    def checkout():
        if not billing_enabled():
            return jsonify(error='Payments are coming soon. Your daily free analysis remains available.', code='billing_unavailable'), 503
        who, _ = owner(True)
        if not who:
            return jsonify(error='Open the analyzer to initialize your browser session.'), 401
        # Only the server chooses price and credit quantity. No browser-supplied prices.
        try:
            response = requests.post('https://api.stripe.com/v1/checkout/sessions', auth=(os.environ['STRIPE_SECRET_KEY'], ''), data={
                'mode': 'payment', 'line_items[0][price]': os.environ['STRIPE_PRICE_VIDEO'], 'line_items[0][quantity]': '1',
                'client_reference_id': who, 'metadata[owner]': who,
                'success_url': 'https://adscanvideo.com/?payment=success#input-zone',
                'cancel_url': 'https://adscanvideo.com/?payment=cancelled#pricing',
            }, timeout=20)
            response.raise_for_status()
            data = response.json()
            with connect() as db:
                db.execute('INSERT INTO checkouts VALUES(?,?,?)', (data['id'], who, time.time()))
            return jsonify(url=data['url'])
        except (requests.RequestException, KeyError):
            return jsonify(error='Checkout is temporarily unavailable. Please try again.'), 502

    @app.post('/api/billing/webhook')
    def webhook():
        secret = os.getenv('STRIPE_WEBHOOK_SECRET')
        if not secret:
            return jsonify(error='Billing is not configured.'), 503
        raw = request.get_data()
        try:
            parts = request.headers.get('Stripe-Signature', '').split(',')
            timestamp = next(p[2:] for p in parts if p.startswith('t='))
            signatures = [p[3:] for p in parts if p.startswith('v1=')]
            expected = hmac.new(secret.encode(), timestamp.encode()+b'.'+raw, hashlib.sha256).hexdigest()
            if abs(time.time()-int(timestamp)) > 300 or not any(hmac.compare_digest(expected, sig) for sig in signatures):
                raise ValueError()
            event = json.loads(raw)
        except (ValueError, StopIteration):
            return jsonify(error='Invalid webhook signature.'), 400
        if event.get('type') in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
            session = event.get('data', {}).get('object', {})
            if session.get('payment_status') == 'paid' and session.get('mode') == 'payment':
                with connect() as db:
                    row = db.execute('SELECT owner FROM checkouts WHERE session=?', (session.get('id'),)).fetchone()
                    if not row:
                        # Retry if delivery races checkout persistence.
                        return jsonify(error='Unknown checkout session.'), 503
                    if session.get('client_reference_id') != row[0] or session.get('amount_total') != 50 or session.get('currency') != 'usd':
                        return jsonify(error='Checkout details do not match.'), 400
                    db.execute('INSERT OR IGNORE INTO payments VALUES(?,?,?,?)', (session['id'], row[0], 1, time.time()))
        return jsonify(received=True)

    # Hooks used by isolated regression tests; never exposed over HTTP.
    app.extensions['adscan'] = {'supervise': supervise, 'connect': connect}
