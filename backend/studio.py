"""AI studio: swap people or objects in a clip, or recreate an ad, through the Sume API.

Off unless SUME_ENABLED=1 and SUME_API_KEY are set. Generation spends paid
credits only, never the free sample or the monthly allowance. Sume charges per
job, so a job is submitted once under its own Idempotency-Key and afterwards
only polled; a retry with the same key returns the same Sume job.
"""
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time

import requests
from flask import request, jsonify
from PIL import Image

BASE = 'https://api.sume.com'
MEDIA_PREFIX = 'https://media.sume.com/'
ASPECTS = ('9:16', '16:9', '1:1', '3:4', '4:3')
ACTIVE = ('submitting', 'running')
KINDS = {
    'image': ({'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp'}, 10 * 1024 * 1024),
    'audio': ({'.mp3': 'audio/mpeg', '.wav': 'audio/wav'}, 20 * 1024 * 1024),
    'video': ({'.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm'}, 100 * 1024 * 1024),
}
MAX_IMAGES, MAX_AUDIO, MAX_SWAP_IMAGES = 9, 3, 8
# Sume Remix (Genjutsu): 'swap' replaces people, products or objects in the clip;
# 'motion' makes the reference character perform the clip's motion.
# Light Remix costs about a third of full Remix but only uses the first 15 seconds
# of a clip, so short clips go to Light and longer ones (up to 30 s) to full Remix,
# which spends credits faster.
SWAP_MODELS = {
    'swap': {'light': ('SUME_SWAP_LIGHT_MODEL', 'higgsfield-genjutsu-light-object-swap'), 'full': ('SUME_SWAP_MODEL', 'higgsfield-genjutsu-object-swap')},
    'motion': {'light': ('SUME_MOTION_LIGHT_MODEL', 'higgsfield-genjutsu-light'), 'full': ('SUME_MOTION_MODEL', 'higgsfield-genjutsu')},
}
SWAP_LIGHT_MAX = 15
# Billed seconds one paid credit buys. Set so the model cost stays at or under half
# of the lowest net credit price (about $2.85 in the 5 pack after card fees).
RATES = {'recreate': ('SUME_SECONDS_PER_CREDIT', 15), 'swap_light': ('SUME_SWAP_LIGHT_SECONDS_PER_CREDIT', 10), 'swap_full': ('SUME_SWAP_SECONDS_PER_CREDIT', 4)}
MIN_SECONDS, MAX_SECONDS = 4, 30
# Image models on offer and how many images of a pack each one uses. One paid
# credit opens a pack of IMAGE_PACK images; at these weights a full pack costs at
# most about $1.06 at Sume's list prices of 26 Nov 2026.
IMAGE_MODELS = {
    'nano-banana-2.1': {'name': 'Nano Banana 2.1', 'units': 1, 'extra': {}},
    'gpt-image-2.6': {'name': 'GPT Image 2.6', 'units': 1, 'extra': {'quality': 'medium'}},
    'gpt-image-2': {'name': 'ChatGPT Image 2', 'units': 1, 'extra': {'quality': 'medium'}},
    'nano-banana-pro': {'name': 'Nano Banana Pro', 'units': 3, 'extra': {}},
}
IMAGE_PACK = 20
MAX_IMAGE_REFS = 4
SUBMIT_ERRORS = {
    'invalid_request': 'The video model refused this request. Check the prompt and references, then try again. Your AI Credits were restored.',
    'insufficient_funds': 'Video generation is temporarily unavailable. Your AI Credits were restored.',
    'unauthorized': 'Video generation is temporarily unavailable. Your AI Credits were restored.',
    'queue_full': 'The video generator is busy. Please try again in a few minutes. Your AI Credits were restored.',
    'rate_limited': 'The video generator is busy. Please try again in a few minutes. Your AI Credits were restored.',
}
FAILED = 'The generation failed. Your AI Credits were restored.'
# Each upload holds a request thread while it streams to Sume; keep two free for everything else.
UPLOADS = threading.BoundedSemaphore(2)


def enabled():
    return os.getenv('SUME_ENABLED') == '1' and bool(os.getenv('SUME_API_KEY'))


def number(name, default):
    try:
        return max(1, int(os.getenv(name) or default))
    except ValueError:
        return default


def seconds_per_credit(rate='recreate'):
    return number(*RATES[rate])


def install_schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS studio_assets (id TEXT PRIMARY KEY, owner TEXT NOT NULL, ip TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL, seconds REAL NOT NULL DEFAULT 0, created REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS studio_assets_owner ON studio_assets(owner,created);
    CREATE TABLE IF NOT EXISTS studio_jobs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, credits INTEGER NOT NULL, seconds REAL NOT NULL DEFAULT 0, sume_job TEXT NOT NULL DEFAULT '', request TEXT NOT NULL, result_url TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL, checked REAL NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS studio_jobs_owner ON studio_jobs(owner,created);
    ''')
    if 'units' not in {row[1] for row in db.execute('PRAGMA table_info(studio_jobs)')}:
        db.execute('ALTER TABLE studio_jobs ADD COLUMN units INTEGER NOT NULL DEFAULT 0')


def images_left(db, who):
    """Images still available from the packs this owner opened. Failed images give theirs back."""
    packs = db.execute("SELECT COUNT(*) FROM studio_jobs WHERE owner=? AND kind='image_pack'", (who,)).fetchone()[0]
    used = db.execute("SELECT COALESCE(SUM(units),0) FROM studio_jobs WHERE owner=? AND kind='image' AND status!='failed'", (who,)).fetchone()[0]
    return max(0, packs * IMAGE_PACK - used)


def spent(db, who):
    """Paid credits held or used by this owner's generations. Failed jobs give theirs back."""
    return db.execute("SELECT COALESCE(SUM(credits),0) FROM studio_jobs WHERE owner=? AND status!='failed'", (who,)).fetchone()[0]


def probe_seconds(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-show_entries', 'format=duration',
                             '-of', 'default=nw=1:nk=1', str(path)], capture_output=True, text=True, timeout=20)
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise ValueError('This file could not be read. Try exporting it again.')


def check_image(path):
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            kind = image.format
    except Exception:
        raise ValueError('This image could not be read. Use a JPEG, PNG or WebP file.')
    if kind not in ('JPEG', 'PNG', 'WEBP'):
        raise ValueError('Use a JPEG, PNG or WebP image.')
    if not (300 <= width <= 6000 and 300 <= height <= 6000) or not (0.4 <= width / height <= 2.5):
        raise ValueError('Images must be 300 to 6000 pixels on each side and no narrower than 2:5 or wider than 5:2.')


def translate(prompt, tokens):
    """Turn the user's own reference names (@Nigora) into the model's positional ones (@Image1)."""
    lookup = {name.lower(): token for name, token in tokens.items()}
    return re.sub(r'@([A-Za-z0-9_]{1,24})', lambda m: lookup.get(m.group(1).lower(), m.group(0)), prompt)


def install(app, connect, owner, ip_key, allowance):
    def sume(method, path, **kwargs):
        headers = {'Authorization': 'Bearer ' + os.environ['SUME_API_KEY'], **kwargs.pop('headers', {})}
        return requests.request(method, BASE + path, headers=headers, timeout=kwargs.pop('timeout', 30), **kwargs)

    def body_of(response):
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def gate():
        if not enabled():
            return None, (jsonify(error='AI video generation is not available yet.', code='studio_unavailable'), 503)
        who, _ = owner(True)
        if not who:
            return None, (jsonify(error='Reload the analyzer before using the AI studio.'), 401)
        return who, None

    def public(row):
        jid, kind, status, credits, seconds, result_url, error = row
        data = {'job_id': jid, 'kind': kind, 'status': status, 'credits': credits, 'seconds': seconds}
        if status == 'done':
            data['url'] = result_url
            if kind == 'image':
                data['asset_id'] = jid
        if status == 'failed':
            data['error'] = error or FAILED
        return data

    def fail(jid, message):
        with connect() as db:
            db.execute("UPDATE studio_jobs SET status='failed',error=?,updated=? WHERE id=? AND status IN ('submitting','running')", (message, time.time(), jid))

    def submit(jid):
        """Send a job to Sume. Safe to repeat: the job id is the Idempotency-Key."""
        with connect() as db:
            row = db.execute("SELECT kind,request FROM studio_jobs WHERE id=? AND status='submitting'", (jid,)).fetchone()
        if not row:
            return
        route = '/v1/image-router/generate' if row[0] == 'image' else '/v1/video-router/generate'
        try:
            response = sume('POST', route, headers={'Idempotency-Key': jid}, json=json.loads(row[1]), timeout=15)
        except requests.RequestException:
            return  # Unknown outcome. A later poll repeats this call under the same key.
        data = body_of(response)
        if response.status_code >= 500:
            return
        if response.status_code >= 400:
            code = (data.get('error') or {}).get('code', '')
            app.logger.warning('Sume refused studio job %s: %s %s', jid, response.status_code, code)
            return fail(jid, SUBMIT_ERRORS.get(code, FAILED))
        sume_job = ((data.get('data') or {}).get('job') or {}).get('id', '')
        if not isinstance(sume_job, str) or not re.fullmatch(r'job_[A-Za-z0-9-]{8,80}', sume_job):
            app.logger.warning('Sume returned no job id for studio job %s', jid)
            return
        with connect() as db:
            db.execute("UPDATE studio_jobs SET status='running',sume_job=?,updated=? WHERE id=? AND status='submitting'", (sume_job, time.time(), jid))

    def refresh(jid):
        with connect() as db:
            row = db.execute('SELECT kind,status,sume_job,created,updated,checked,owner FROM studio_jobs WHERE id=?', (jid,)).fetchone()
        if not row:
            return
        kind, status, sume_job, created, updated, checked, who = row
        now = time.time()
        if status in ACTIVE and now - created > 3600:
            return fail(jid, 'The generation took too long. Your AI Credits were restored.')
        if status == 'submitting' and now - updated > 15:
            with connect() as db:
                db.execute('UPDATE studio_jobs SET updated=? WHERE id=?', (now, jid))
            return submit(jid)
        if status != 'running' or now - checked < 3:
            return
        with connect() as db:
            db.execute('UPDATE studio_jobs SET checked=? WHERE id=?', (now, jid))
        try:
            response = sume('GET', '/v1/jobs/' + sume_job + '/status', timeout=15)
            state = (body_of(response).get('data') or {}).get('sume_status')
            if response.status_code != 200 or state in ('queued', 'processing', None):
                return
            if state != 'completed':
                return fail(jid, FAILED)
            result = sume('GET', '/v1/jobs/' + sume_job + '/result', timeout=15)
            artifacts = ((body_of(result).get('data') or {}).get('result') or {}).get('artifacts') or []
        except requests.RequestException:
            return
        if result.status_code != 200:
            return
        url = artifacts[0].get('url', '') if artifacts and isinstance(artifacts[0], dict) else ''
        if not isinstance(url, str) or not url.startswith(MEDIA_PREFIX):
            return fail(jid, FAILED)
        with connect() as db:
            done = db.execute("UPDATE studio_jobs SET status='done',result_url=?,updated=? WHERE id=? AND status='running'", (url, time.time(), jid)).rowcount
            if done and kind == 'image':
                # A generated image becomes a reference the same owner can use.
                db.execute('INSERT OR IGNORE INTO studio_assets(id,owner,ip,kind,url,created) VALUES(?,?,?,?,?,?)', (jid, who, '', 'image', url, time.time()))

    def refresh_active(who):
        # A browser can lose a job id (closed tab, timed-out request). Any later
        # visit by the owner still moves the job on, so it cannot block them for good.
        with connect() as db:
            active = [row[0] for row in db.execute("SELECT id FROM studio_jobs WHERE owner=? AND status IN ('submitting','running')", (who,))]
        for jid in active:
            refresh(jid)

    def reserve(who, kind, credits, seconds, body, units=0):
        """Hold credits and record the job in one transaction, before anything is sent."""
        refresh_active(who)
        jid = secrets.token_hex(16)
        now = time.time()
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM studio_jobs WHERE owner=? AND status IN ('submitting','running')", (who,)).fetchone():
                return None, (jsonify(error='Your previous generation is still running. Wait for it to finish.', code='busy'), 409)
            paid = allowance(db, who, ip_key())['paid_credits']
            # An image comes out of the owner's pack; an empty pack costs one credit to refill.
            open_pack = kind == 'image' and images_left(db, who) < units
            need = 1 if open_pack else credits
            if paid < need:
                what = f'A pack of {IMAGE_PACK} images costs 1 AI Credit.' if kind == 'image' else f'This needs {need} AI Credit{"" if need == 1 else "s"}.'
                return None, (jsonify(error=f'{what} You have {paid}.', code='payment_required', credits_needed=need, paid_credits=paid), 402)
            limit = number('SUME_IMAGES_PER_DAY', 100) if kind == 'image' else number('SUME_VIDEOS_PER_DAY', 20)
            group = "kind='image'" if kind == 'image' else "kind!='image'"
            if db.execute('SELECT COUNT(*) FROM studio_jobs WHERE owner=? AND created>? AND ' + group, (who, now - 86400)).fetchone()[0] >= limit:
                return None, (jsonify(error='Daily generation limit reached. Please try again tomorrow.', code='rate_limited'), 429)
            if open_pack:
                db.execute("INSERT INTO studio_jobs(id,owner,kind,status,credits,request,created,updated) VALUES(?,?,'image_pack','done',1,'{}',?,?)", (secrets.token_hex(16), who, now, now))
            db.execute('INSERT INTO studio_jobs(id,owner,kind,status,credits,seconds,request,created,updated,units) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (jid, who, kind, 'submitting', credits, seconds, json.dumps(body), now, now, units))
        return jid, None

    def started(jid):
        submit(jid)
        with connect() as db:
            row = db.execute('SELECT id,kind,status,credits,seconds,result_url,error FROM studio_jobs WHERE id=?', (jid,)).fetchone()
        return jsonify(public(row)), 202

    @app.get('/api/studio/config')
    def studio_config():
        return jsonify(enabled=enabled(), seconds_per_credit=seconds_per_credit(), swap_light_seconds_per_credit=seconds_per_credit('swap_light'), swap_seconds_per_credit=seconds_per_credit('swap_full'),
                       swap_light_max=SWAP_LIGHT_MAX, reference_clip_billed=os.getenv('SUME_VIDEO_MODEL', 'magi-2').startswith('seedance'), max_swap_images=MAX_SWAP_IMAGES, image_pack=IMAGE_PACK, max_image_refs=MAX_IMAGE_REFS,
                       image_models=[{'id': key, 'name': value['name'], 'units': value['units']} for key, value in IMAGE_MODELS.items()], min_seconds=MIN_SECONDS, max_seconds=MAX_SECONDS,
                       aspects=list(ASPECTS), resolution=os.getenv('SUME_VIDEO_RESOLUTION', '1080p'), max_images=MAX_IMAGES, max_audio=MAX_AUDIO)

    @app.post('/api/studio/assets')
    def studio_asset():
        who, error = gate()
        if error:
            return error
        kind = request.form.get('kind', '')
        f = request.files.get('file')
        suffix = Path(f.filename or '').suffix.lower() if f else ''
        if kind not in KINDS or suffix not in KINDS[kind][0]:
            return jsonify(error='Upload a JPEG, PNG or WebP image, an MP3 or WAV audio file, or an MP4, MOV or WebM video.', code='invalid_input'), 400
        types, limit = KINDS[kind]
        ip = ip_key()
        with connect() as db:
            if db.execute('SELECT COUNT(*) FROM studio_assets WHERE (owner=? OR ip=?) AND created>?', (who, ip, time.time() - 86400)).fetchone()[0] >= number('SUME_UPLOADS_PER_DAY', 40):
                return jsonify(error='Daily upload limit reached. Please try again tomorrow.', code='rate_limited'), 429
        if not UPLOADS.acquire(blocking=False):
            return jsonify(error='Uploads are busy. Please try again in a minute.', code='busy'), 503, {'Retry-After': '30'}
        directory = Path(tempfile.mkdtemp(prefix='adscan-studio-'))
        try:
            # Fixed basename: the user's file name never reaches disk or Sume.
            path = directory / ('reference' + suffix)
            count = 0
            with path.open('wb') as out:
                while chunk := f.stream.read(1024 * 1024):
                    count += len(chunk)
                    if count > limit:
                        return jsonify(error=f'Maximum {kind} size is {limit // (1024 * 1024)} MB.', code='too_large'), 413
                    out.write(chunk)
            if count == 0:
                return jsonify(error='The uploaded file is empty.', code='invalid_input'), 400
            seconds = 0.0
            try:
                if kind == 'image':
                    check_image(path)
                else:
                    seconds = probe_seconds(path)
                    low, high = (2.0, 30.0) if kind == 'audio' else (MIN_SECONDS, MAX_SECONDS + 0.2)
                    if not low <= seconds <= high:
                        raise ValueError('Audio must be 2 to 30 seconds long.' if kind == 'audio' else 'Videos must be 4 to 30 seconds long. Trim the clip and upload it again.')
            except subprocess.TimeoutExpired:
                return jsonify(error='This file could not be read. Try exporting it again.', code='invalid_input'), 400
            except ValueError as e:
                return jsonify(error=str(e), code='invalid_input'), 400
            try:
                with path.open('rb') as source:
                    response = sume('POST', '/v1/files/upload', params={'filename': path.name}, headers={'Content-Type': types[suffix]}, data=source, timeout=(10, 240))
            except requests.RequestException:
                return jsonify(error='The upload did not go through. Please try again.', code='upload_failed'), 502
            url = (body_of(response).get('data') or {}).get('public_url', '')
            if response.status_code not in (200, 201) or not isinstance(url, str) or not url.startswith(MEDIA_PREFIX):
                app.logger.warning('Sume refused a studio upload: %s', response.status_code)
                return jsonify(error='The upload did not go through. Please try again.', code='upload_failed'), 502
            aid = secrets.token_hex(16)
            with connect() as db:
                db.execute('INSERT INTO studio_assets(id,owner,ip,kind,url,seconds,created) VALUES(?,?,?,?,?,?,?)', (aid, who, ip, kind, url, seconds, time.time()))
            return jsonify(asset_id=aid, kind=kind, seconds=seconds), 201
        finally:
            UPLOADS.release()
            shutil.rmtree(directory, ignore_errors=True)

    @app.post('/api/studio/generate')
    def studio_generate():
        who, error = gate()
        if error:
            return error
        data = request.get_json(silent=True)
        try:
            if not isinstance(data, dict):
                raise ValueError('Send a prompt and a mode.')
            mode, prompt, refs, source_id = data.get('mode'), data.get('prompt'), data.get('refs', []), data.get('source')
            if mode not in ('swap', 'recreate'):
                raise ValueError('Choose swap or recreate.')
            if not isinstance(prompt, str) or len(prompt.strip()) > 2000 or (mode == 'recreate' and not prompt.strip()):
                raise ValueError('Write a prompt of up to 2,000 characters.')
            if data.get('rights') is not True:
                raise ValueError('Confirm that you have the rights to the people, voices and footage you uploaded.')
            if not isinstance(refs, list) or len(refs) > MAX_IMAGES + MAX_AUDIO or not all(
                    isinstance(r, dict) and isinstance(r.get('asset'), str) and isinstance(r.get('label'), str)
                    and re.fullmatch(r'[A-Za-z0-9_]{1,24}', r['label']) for r in refs):
                raise ValueError('Each reference needs a short name made of letters, numbers or underscores.')
            labels = [r['label'].lower() for r in refs]
            if len(set(labels)) != len(labels) or 'video' in labels:
                raise ValueError('Give each reference its own name. The name "video" is reserved for your source clip.')
            if source_id is not None and not isinstance(source_id, str):
                raise ValueError('Upload the source video again.')
            wanted = [r['asset'] for r in refs] + ([source_id] if source_id else [])
            with connect() as db:
                assets = {row[0]: row[1:] for row in db.execute(
                    'SELECT id,kind,url,seconds FROM studio_assets WHERE owner=? AND id IN (' + ','.join('?' * len(wanted)) + ')', (who, *wanted))} if wanted else {}
            if len(assets) != len(set(wanted)):
                raise ValueError('A reference is missing. Upload it again.')
            source = assets[source_id] if source_id else None
            if source and source[0] != 'video':
                raise ValueError('The source must be a video.')
            images, audio, tokens = [], [], {}
            for r in refs:
                kind, url, _ = assets[r['asset']]
                if kind == 'video':
                    raise ValueError('Add a video as the source clip, not as a reference.')
                bucket, name = (images, 'Image') if kind == 'image' else (audio, 'Audio')
                bucket.append(url)
                tokens[r['label']] = f'@{name}{len(bucket)}'
            if len(images) > MAX_IMAGES or len(audio) > MAX_AUDIO:
                raise ValueError(f'Use up to {MAX_IMAGES} images and {MAX_AUDIO} audio files.')
            body = {'model': os.getenv('SUME_VIDEO_MODEL', 'magi-2'), 'resolution': os.getenv('SUME_VIDEO_RESOLUTION', '1080p')}
            rate = 'recreate'
            if mode == 'swap':
                # Remix keeps the clip's own length and framing and takes one clip plus 1 to 8 images.
                style = data.get('style', 'swap')
                if style not in SWAP_MODELS:
                    raise ValueError('Choose swap or motion transfer.')
                if not source:
                    raise ValueError('Upload the video you want to change.')
                if audio:
                    raise ValueError('Swap keeps the sound of your video and cannot take audio references. Use Recreate for a new voice.')
                if not 1 <= len(images) <= MAX_SWAP_IMAGES:
                    raise ValueError(f'Add 1 to {MAX_SWAP_IMAGES} photos of the person, product or object to put into the video.')
                # The model wants the clip's own length, rounded up to a whole second.
                seconds = billable = min(MAX_SECONDS, math.ceil(source[2]))
                tier = 'light' if seconds <= SWAP_LIGHT_MAX else 'full'
                rate = 'swap_' + tier
                body.update(model=os.getenv(*SWAP_MODELS[style][tier]), resolution=os.getenv('SUME_SWAP_RESOLUTION', '480p'), video_url=source[1], duration=seconds)
            else:
                seconds = data.get('duration')
                aspect = data.get('aspect', '9:16')
                if isinstance(seconds, bool) or not isinstance(seconds, int) or not MIN_SECONDS <= seconds <= MAX_SECONDS or aspect not in ASPECTS:
                    raise ValueError(f'Choose a length from {MIN_SECONDS} to {MAX_SECONDS} seconds and a listed aspect ratio.')
                if audio and not images and not source:
                    raise ValueError('An audio reference needs at least one image or a source video with it.')
                if sum(assets[r['asset']][2] for r in refs if assets[r['asset']][0] == 'audio') > 30.2:
                    raise ValueError('Audio references can total 30 seconds at most.')
                # Seedance bills a reference clip with the output: its own seconds, and never
                # less than two thirds of the output's. MAGI-2 charges the output only.
                billable = seconds + (max(source[2], seconds * 2 / 3) if source and body['model'].startswith('seedance') else 0)
                body.update(duration=seconds, aspect_ratio=aspect, generate_audio=True)
                if source:
                    body.update(reference_video_urls=[source[1]])
                    tokens['video'] = '@Video1'
                if audio:
                    body.update(reference_audio_urls=audio)
            if images:
                body.update(reference_image_urls=images)
            if prompt.strip():
                body['prompt'] = translate(prompt.strip(), tokens)
        except ValueError as e:
            return jsonify(error=str(e), code='invalid_input'), 400
        credits = math.ceil(billable / seconds_per_credit(rate))
        jid, error = reserve(who, mode, credits, seconds, body)
        return error or started(jid)

    @app.post('/api/studio/image')
    def studio_image():
        who, error = gate()
        if error:
            return error
        data = request.get_json(silent=True)
        data = data if isinstance(data, dict) else {}
        prompt, aspect, refs = data.get('prompt'), data.get('aspect', '9:16'), data.get('refs', [])
        model = data.get('model') or os.getenv('SUME_IMAGE_MODEL', 'nano-banana-2.1')
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000 or aspect not in ASPECTS or model not in IMAGE_MODELS:
            return jsonify(error='Describe the image in up to 2,000 characters and choose a listed model and aspect ratio.', code='invalid_input'), 400
        if not isinstance(refs, list) or len(refs) > MAX_IMAGE_REFS or not all(isinstance(r, str) for r in refs):
            return jsonify(error=f'Use up to {MAX_IMAGE_REFS} reference photos.', code='invalid_input'), 400
        body = {'model': model, 'prompt': prompt.strip(), 'aspect_ratio': aspect, **IMAGE_MODELS[model]['extra']}
        if refs:
            if data.get('rights') is not True:
                return jsonify(error='Confirm that you have the rights to the photos you uploaded.', code='invalid_input'), 400
            with connect() as db:
                urls = {row[0]: row[1] for row in db.execute("SELECT id,url FROM studio_assets WHERE owner=? AND kind='image' AND id IN (" + ','.join('?' * len(refs)) + ')', (who, *refs))}
            if len(urls) != len(set(refs)):
                return jsonify(error='A reference photo is missing. Upload it again.', code='invalid_input'), 400
            body['image_urls'] = [urls[r] for r in refs]
        jid, error = reserve(who, 'image', 0, 0, body, IMAGE_MODELS[model]['units'])
        return error or started(jid)

    @app.get('/api/studio/jobs/<jid>')
    def studio_job(jid):
        who, error = gate()
        if error:
            return error
        def load():
            with connect() as db:
                return db.execute('SELECT id,kind,status,credits,seconds,result_url,error FROM studio_jobs WHERE id=? AND owner=?', (jid, who)).fetchone()
        row = load()
        if not row:
            return jsonify(error='Generation not found.', code='not_found'), 404
        if row[2] in ACTIVE:
            refresh(jid)
            row = load()
        return jsonify(public(row))

    @app.get('/api/studio/jobs')
    def studio_jobs():
        who, error = gate()
        if error:
            return error
        refresh_active(who)
        with connect() as db:
            rows = db.execute("SELECT id,kind,status,credits,seconds,result_url,error FROM studio_jobs WHERE owner=? AND kind!='image_pack' ORDER BY created DESC LIMIT 20", (who,)).fetchall()
            left = images_left(db, who)
        return jsonify(jobs=[public(row) for row in rows], images_left=left)
