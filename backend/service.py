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


SINGLE_CREDIT_CENTS = 399
MONTHLY_ANALYSES = 20

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
        CREATE TABLE IF NOT EXISTS subscription_checkouts (session TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL, subscription TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS subscriptions (id TEXT PRIMARY KEY, owner TEXT NOT NULL, customer TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS subscription_periods (invoice TEXT PRIMARY KEY, subscription TEXT NOT NULL, owner TEXT NOT NULL, starts REAL NOT NULL, ends REAL NOT NULL, credits INTEGER NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS subscription_periods_owner ON subscription_periods(owner,starts,ends);
        ''')
        access_columns = {row[1] for row in db.execute('PRAGMA table_info(access)')}
        if 'country' not in access_columns:
            db.execute("ALTER TABLE access ADD COLUMN country TEXT NOT NULL DEFAULT ''")
        if 'input_method' not in access_columns:
            db.execute("ALTER TABLE access ADD COLUMN input_method TEXT NOT NULL DEFAULT ''")
        if 'subscription_invoice' not in access_columns:
            db.execute("ALTER TABLE access ADD COLUMN subscription_invoice TEXT NOT NULL DEFAULT ''")
        subscription_checkout_columns = {row[1] for row in db.execute('PRAGMA table_info(subscription_checkouts)')}
        if 'subscription' not in subscription_checkout_columns:
            db.execute("ALTER TABLE subscription_checkouts ADD COLUMN subscription TEXT NOT NULL DEFAULT ''")
        if 'url' not in subscription_checkout_columns:
            db.execute("ALTER TABLE subscription_checkouts ADD COLUMN url TEXT NOT NULL DEFAULT ''")

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
        periods = active_periods(db, who)
        has_subscription = db.execute("SELECT 1 FROM subscriptions WHERE owner=? AND status!='canceled'", (who,)).fetchone()
        return {'free_remaining': max(0, 1-used), 'paid_credits': max(0, paid-spent),
                'monthly_remaining': sum(row[2] for row in periods),
                'monthly_ends_at': min((row[1] for row in periods), default=None),
                'subscription_enabled': subscription_enabled(),
                'manage_billing_url': os.getenv('STRIPE_PORTAL_LOGIN_URL', '') if has_subscription else '',
                'reset_at': (int(time.time())//86400+1)*86400,
                'billing_enabled': billing_enabled(), 'max_duration': 600}

    def active_periods(db, who):
        now = time.time()
        rows = db.execute('''SELECT p.invoice,p.ends,p.credits,
            (SELECT COUNT(*) FROM access a JOIN jobs j ON j.id=a.job
             WHERE a.subscription_invoice=p.invoice AND a.tier='monthly' AND j.status!='error')
            FROM subscription_periods p WHERE p.owner=? AND p.starts<=? AND p.ends>?
            ORDER BY p.ends,p.invoice''', (who, now, now)).fetchall()
        return [(invoice, ends, max(0, credits-spent)) for invoice, ends, credits, spent in rows]

    def billing_enabled():
        return os.getenv('STRIPE_BILLING_ENABLED') == '1' and bool(
            os.getenv('STRIPE_SECRET_KEY') and os.getenv('STRIPE_WEBHOOK_SECRET') and os.getenv('STRIPE_PRICE_VIDEO')
        )

    def subscription_enabled():
        portal = os.getenv('STRIPE_PORTAL_LOGIN_URL', '')
        return (billing_enabled() and os.getenv('STRIPE_SUBSCRIPTION_ENABLED') == '1'
                and bool(re.fullmatch(r'price_[A-Za-z0-9]+', os.getenv('STRIPE_PRICE_MONTHLY', '')))
                and portal.startswith('https://billing.stripe.com/p/login/'))

    @app.before_request
    def access_control():
        if request.method == 'OPTIONS':
            return None
        if request.path in ('/api/recover', '/api/reap', '/admin'):
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

    @app.get('/api/admin/dashboard')
    def admin_dashboard():
        """Privacy-safe operational metrics for the private owner dashboard."""
        now = time.time()
        day_ago = now - 86400
        week_ago = now - 7 * 86400
        chart_start = now - 13 * 86400
        with connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT id,status,created,updated,mode,cost,frames_kept,"
                "transcript_chars,duration,error FROM jobs ORDER BY created DESC"
            ).fetchall()
            access_rows = db.execute(
                "SELECT a.job,a.tier,a.country,a.input_method FROM access a"
            ).fetchall()
            payment = db.execute(
                "SELECT COALESCE(SUM(credits),0) AS credits, COUNT(*) AS payments FROM payments"
            ).fetchone()
            checkout_count = db.execute("SELECT COUNT(*) AS count FROM checkouts").fetchone()["count"]

        access_by_job = {row["job"]: row for row in access_rows}
        tier_by_job = {job: row["tier"] for job, row in access_by_job.items()}
        terminal = {"done", "error"}
        total = len(rows)
        completed = sum(row["status"] == "done" for row in rows)
        failed = sum(row["status"] == "error" for row in rows)
        active_rows = [row for row in rows if row["status"] not in terminal]
        last_24h = [row for row in rows if (row["created"] or 0) >= day_ago]
        last_7d = [row for row in rows if (row["created"] or 0) >= week_ago]
        last_7d_terminal = [row for row in last_7d if row["status"] in terminal]
        last_7d_done = [row for row in last_7d if row["status"] == "done"]

        def rate(numerator, denominator):
            return round(numerator / denominator, 4) if denominator else None

        def iso_day(timestamp):
            return time.strftime('%Y-%m-%d', time.gmtime(timestamp))

        days = []
        for offset in range(13, -1, -1):
            timestamp = now - offset * 86400
            key = iso_day(timestamp)
            bucket = [row for row in rows if iso_day(row["created"] or 0) == key]
            days.append({
                "date": key,
                "total": len(bucket),
                "completed": sum(row["status"] == "done" for row in bucket),
                "failed": sum(row["status"] == "error" for row in bucket),
            })

        mode_counts = {}
        for row in last_7d:
            mode = row["mode"] or "unknown"
            entry = mode_counts.setdefault(mode, {"mode": mode, "total": 0, "completed": 0, "failed": 0})
            entry["total"] += 1
            entry["completed"] += row["status"] == "done"
            entry["failed"] += row["status"] == "error"

        input_counts = {}
        country_counts = {}
        for row in last_7d:
            access = access_by_job.get(row["id"])
            method = (access["input_method"] if access else "") or "unknown"
            country = (access["country"] if access else "") or "unknown"
            entry = input_counts.setdefault(method, {"input_method": method, "total": 0, "completed": 0, "failed": 0})
            entry["total"] += 1
            entry["completed"] += row["status"] == "done"
            entry["failed"] += row["status"] == "error"
            country_counts[country] = country_counts.get(country, 0) + 1

        def failure_category(row):
            if row["status"] != "error":
                return ""
            error = (row["error"] or "").lower()
            if any(word in error for word in ("download", "private", "unavailable", "platform", "youtube", "link")):
                return "Video link unavailable"
            if any(word in error for word in ("too long", "10 minutes", "duration")):
                return "Video too long"
            if any(word in error for word in ("timeout", "took too long", "deadline")):
                return "Processing timeout"
            if any(word in error for word in ("readable video", "format", "decode", "corrupt")):
                return "Unreadable video"
            return "Processing error"

        recent = []
        for row in rows[:30]:
            created = row["created"] or 0
            recent.append({
                "created": int(created),
                "status": row["status"] or "unknown",
                "mode": row["mode"] or "unknown",
                "duration": row["duration"] or "",
                "frames": row["frames_kept"] or 0,
                "has_transcript": bool(row["transcript_chars"]),
                "cost": round(row["cost"] or 0, 6),
                "tier": tier_by_job.get(row["id"], "unknown"),
                "country": (access_by_job[row["id"]]["country"] or "unknown") if row["id"] in access_by_job else "unknown",
                "input_method": (access_by_job[row["id"]]["input_method"] or "unknown") if row["id"] in access_by_job else "unknown",
                "failure_category": failure_category(row),
            })

        paid_jobs = sum(tier_by_job.get(row["id"]) in ("paid", "monthly") for row in rows)
        free_jobs = sum(tier_by_job.get(row["id"]) == "free" for row in rows)
        longest_active = max((now - (row["updated"] or row["created"] or now) for row in active_rows), default=0)
        traffic = None
        snapshot_path = ROOT / 'analytics-output' / 'adscanvideo-weekly.json'
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
            traffic = {
                'generated_at': int(snapshot_path.stat().st_mtime),
                'period': snapshot.get('period', ''),
                'overview': snapshot.get('overview', {}),
                'previous': snapshot.get('previousPeriod', {}),
                'countries': snapshot.get('countries', [])[:10],
                'sources': snapshot.get('sources', [])[:10],
                'funnel': snapshot.get('funnelTotals', {}),
            }
        except (OSError, ValueError, TypeError):
            pass
        return jsonify({
            "generated_at": int(now),
            "overview": {
                "total": total,
                "last_24h": len(last_24h),
                "last_7d": len(last_7d),
                "completed": completed,
                "failed": failed,
                "active": len(active_rows),
                "completion_rate_7d": rate(len(last_7d_done), len(last_7d_terminal)),
                "failure_rate_7d": rate(sum(row["status"] == "error" for row in last_7d), len(last_7d_terminal)),
                "cost_7d": round(sum(row["cost"] or 0 for row in last_7d_done), 6),
                "avg_cost_7d": round(sum(row["cost"] or 0 for row in last_7d_done) / len(last_7d_done), 6) if last_7d_done else None,
                "frames_7d": sum(row["frames_kept"] or 0 for row in last_7d_done),
            },
            "operations": {
                "billing_enabled": billing_enabled(),
                "price_cents": SINGLE_CREDIT_CENTS,
                "longest_active_seconds": int(max(0, longest_active)),
                "active_statuses": [row["status"] for row in active_rows],
            },
            "revenue": {
                "checkouts_started": checkout_count,
                "payment_records": payment["payments"],
                "credits_issued": payment["credits"],
                "paid_jobs": paid_jobs,
                "free_jobs": free_jobs,
            },
            "daily": days,
            "modes": sorted(mode_counts.values(), key=lambda item: (-item["total"], item["mode"])),
            "inputs": sorted(input_counts.values(), key=lambda item: (-item["total"], item["input_method"])),
            "job_countries": sorted(({"country": key, "jobs": value} for key, value in country_counts.items()), key=lambda item: (-item["jobs"], item["country"])),
            "traffic": traffic,
            "recent": recent,
        })

    def reserve(input_method):
        who, token = owner()
        ip = ip_key()
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            data = allowance(db, who, ip)
            if not data['free_remaining'] and not data['monthly_remaining'] and not data['paid_credits']:
                return None, (jsonify(error='Your free analysis for today is used. Buy a credit to analyze another video, or return after the daily reset.', code='payment_required', **data), 402)
            recent = db.execute('SELECT COUNT(*) FROM access WHERE (ip=? OR owner=?) AND created>?', (ip, who, time.time()-86400)).fetchone()[0]
            if recent >= (40 if data['monthly_remaining'] or data['paid_credits'] else 20):
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
            country = request.headers.get('CF-IPCountry', '').strip().upper()
            if not re.fullmatch(r'[A-Z]{2}', country) or country in ('XX', 'T1', 'A1'):
                country = ''
            monthly_invoice = next((invoice for invoice, _, remaining in active_periods(db, who) if remaining), '') if not data['free_remaining'] else ''
            tier = 'free' if data['free_remaining'] else 'monthly' if monthly_invoice else 'paid'
            db.execute('INSERT INTO access(job,owner,ip,day,tier,created,country,input_method,subscription_invoice) VALUES(?,?,?,?,?,?,?,?,?)',
                       (jid, who, ip, time.strftime('%Y-%m-%d', time.gmtime()), tier, now, country, input_method, monthly_invoice))
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
        if not isinstance(question, str) or len(question) > 4000 or mode not in ('summary', 'shots', 'ads', 'ad', 'cinema', 'podcast', 'marketing', 'tutorial', 'moments'):
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
        reservation, error = reserve('url')
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
            reservation, error = reserve('upload')
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
            response = requests.post('https://api.stripe.com/v1/checkout/sessions', auth=(os.environ['STRIPE_SECRET_KEY'], ''), headers={'Stripe-Version': '2025-03-31.basil'}, data={
                'mode': 'payment', 'line_items[0][price]': os.environ['STRIPE_PRICE_VIDEO'], 'line_items[0][quantity]': '1',
                'managed_payments[enabled]': 'true',
                'client_reference_id': who, 'metadata[owner]': who,
                'success_url': 'https://adscanvideo.com/?payment=success&session_id={CHECKOUT_SESSION_ID}#input-zone',
                'cancel_url': 'https://adscanvideo.com/?payment=cancelled#pricing',
            }, timeout=20)
            response.raise_for_status()
            data = response.json()
            with connect() as db:
                db.execute('INSERT INTO checkouts VALUES(?,?,?)', (data['id'], who, time.time()))
            return jsonify(url=data['url'])
        except (requests.RequestException, KeyError):
            return jsonify(error='Checkout is temporarily unavailable. Please try again.'), 502

    @app.get('/api/billing/checkout/<session_id>')
    def checkout_status(session_id):
        who, _ = owner(True)
        if not who or not re.fullmatch(r'cs_(?:test|live)_[A-Za-z0-9]{1,200}', session_id):
            return jsonify(error='Checkout not found.'), 404
        with connect() as db:
            row = db.execute('SELECT owner FROM checkouts WHERE session=?', (session_id,)).fetchone()
            if not row or not hmac.compare_digest(row[0], who):
                return jsonify(error='Checkout not found.'), 404
            paid = db.execute('SELECT 1 FROM payments WHERE session=? AND owner=?', (session_id, who)).fetchone()
        return jsonify(status='paid' if paid else 'pending')

    @app.post('/api/billing/subscription/checkout')
    def subscription_checkout():
        if not subscription_enabled():
            return jsonify(error='Monthly checkout is not available yet.', code='subscription_unavailable'), 503
        who, _ = owner(True)
        if not who:
            return jsonify(error='Open the analyzer to initialize your browser session.'), 401
        with connect() as db:
            existing = db.execute("SELECT 1 FROM subscriptions WHERE owner=? AND status!='canceled'", (who,)).fetchone()
            pending = db.execute("SELECT url FROM subscription_checkouts WHERE owner=? AND subscription='' AND created>? ORDER BY created DESC LIMIT 1",
                                 (who, time.time()-23*3600)).fetchone()
        if existing:
            return jsonify(error='A subscription is already linked to this browser. Use Manage billing to update it.', code='subscription_exists'), 409
        if pending and pending[0].startswith('https://checkout.stripe.com/'):
            return jsonify(url=pending[0])
        try:
            response = requests.post('https://api.stripe.com/v1/checkout/sessions', auth=(os.environ['STRIPE_SECRET_KEY'], ''),
                                     headers={'Stripe-Version': '2025-03-31.basil'}, data={
                'mode': 'subscription', 'line_items[0][price]': os.environ['STRIPE_PRICE_MONTHLY'],
                'line_items[0][quantity]': '1', 'managed_payments[enabled]': 'true',
                'client_reference_id': who, 'metadata[owner]': who,
                'success_url': 'https://adscanvideo.com/?payment=monthly_success&session_id={CHECKOUT_SESSION_ID}#input-zone',
                'cancel_url': 'https://adscanvideo.com/?payment=cancelled#pricing',
            }, timeout=20)
            response.raise_for_status()
            data = response.json()
            with connect() as db:
                db.execute('INSERT INTO subscription_checkouts(session,owner,created,url) VALUES(?,?,?,?)', (data['id'], who, time.time(), data['url']))
            return jsonify(url=data['url'])
        except (requests.RequestException, KeyError, sqlite3.IntegrityError):
            return jsonify(error='Monthly checkout is temporarily unavailable. Please try again.'), 502

    @app.get('/api/billing/subscription/checkout/<session_id>')
    def subscription_checkout_status(session_id):
        who, _ = owner(True)
        if not who or not re.fullmatch(r'cs_(?:test|live)_[A-Za-z0-9]{1,200}', session_id):
            return jsonify(error='Checkout not found.'), 404
        with connect() as db:
            row = db.execute('SELECT owner,subscription FROM subscription_checkouts WHERE session=?', (session_id,)).fetchone()
            if not row or not hmac.compare_digest(row[0], who):
                return jsonify(error='Checkout not found.'), 404
            paid = db.execute('SELECT 1 FROM subscription_periods WHERE subscription=? AND owner=?', (row[1], who)).fetchone() if row[1] else None
        return jsonify(status='paid' if paid else 'pending')

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
                    # Stripe Managed Payments may add tax or convert the displayed currency.
                    # This session was created server-side for exactly one configured Price;
                    # the signed, paid session and stored owner are the fulfillment proof.
                    if session.get('client_reference_id') != row[0]:
                        return jsonify(error='Checkout details do not match.'), 400
                    db.execute('INSERT OR IGNORE INTO payments VALUES(?,?,?,?)', (session['id'], row[0], 1, time.time()))
            elif session.get('mode') == 'subscription' and session.get('subscription') and session.get('customer'):
                with connect() as db:
                    row = db.execute('SELECT owner,subscription FROM subscription_checkouts WHERE session=?', (session.get('id'),)).fetchone()
                    if not row:
                        return jsonify(error='Unknown subscription checkout.'), 503
                    if session.get('client_reference_id') != row[0]:
                        return jsonify(error='Checkout details do not match.'), 400
                    if row[1] and row[1] != session['subscription']:
                        return jsonify(error='Subscription details do not match.'), 400
                    existing = db.execute('SELECT owner,customer FROM subscriptions WHERE id=?', (session['subscription'],)).fetchone()
                    if existing and existing != (row[0], session['customer']):
                        return jsonify(error='Subscription details do not match.'), 400
                    db.execute('UPDATE subscription_checkouts SET subscription=? WHERE session=?', (session['subscription'], session['id']))
                    db.execute('INSERT OR IGNORE INTO subscriptions VALUES(?,?,?,?)',
                               (session['subscription'], row[0], session['customer'], 'active'))
        elif event.get('type') == 'invoice.payment_succeeded':
            invoice = event.get('data', {}).get('object', {})
            subscription = invoice.get('subscription') or (invoice.get('parent') or {}).get('subscription_details', {}).get('subscription')
            if subscription and invoice.get('status') == 'paid' and invoice.get('amount_paid', 0) > 0:
                with connect() as db:
                    row = db.execute('SELECT owner,customer FROM subscriptions WHERE id=?', (subscription,)).fetchone()
                    if not row:
                        # Checkout and invoice events are not guaranteed to arrive in order.
                        return jsonify(error='Subscription checkout has not arrived yet.'), 503
                    if invoice.get('customer') != row[1]:
                        return jsonify(error='Invoice customer does not match.'), 400
                    lines = (invoice.get('lines') or {}).get('data') or []
                    price = os.getenv('STRIPE_PRICE_MONTHLY', '')
                    matches = [line for line in lines if
                               ((line.get('price') or {}).get('id') if isinstance(line.get('price'), dict) else line.get('price')) == price
                               or ((line.get('pricing') or {}).get('price_details') or {}).get('price') == price]
                    if not matches:
                        return jsonify(received=True)
                    period = matches[0].get('period') or {}
                    starts, ends = period.get('start'), period.get('end')
                    if not (isinstance(starts, int) and isinstance(ends, int) and 0 < ends-starts <= 35*86400):
                        return jsonify(error='Invalid billing period.'), 503
                    db.execute('INSERT OR IGNORE INTO subscription_periods VALUES(?,?,?,?,?,?,?)',
                               (invoice['id'], subscription, row[0], starts, ends, MONTHLY_ANALYSES, time.time()))
                    db.execute("UPDATE subscriptions SET status='active' WHERE id=?", (subscription,))
        elif event.get('type') == 'invoice.payment_failed':
            invoice = event.get('data', {}).get('object', {})
            subscription = invoice.get('subscription') or (invoice.get('parent') or {}).get('subscription_details', {}).get('subscription')
            if subscription:
                with connect() as db:
                    db.execute("UPDATE subscriptions SET status='past_due' WHERE id=? AND status!='canceled'", (subscription,))
        elif event.get('type') == 'customer.subscription.deleted':
            subscription = event.get('data', {}).get('object', {}).get('id')
            if subscription:
                with connect() as db:
                    db.execute("UPDATE subscriptions SET status='canceled' WHERE id=?", (subscription,))
        return jsonify(received=True)

    # Hooks used by isolated regression tests; never exposed over HTTP.
    app.extensions['adscan'] = {'supervise': supervise, 'connect': connect}
