#!/usr/bin/env python3
"""Local preview of the AI studio. Binds to 127.0.0.1 and uses a throwaway database.

  python scripts/studio_dev.py          fake Sume: spends nothing, results never play
  python scripts/studio_dev.py --live   real Sume key from backend/.env: every
                                        generation spends the Sume balance

Open http://127.0.0.1:8766/site/?demo to see the composer under a sample report.
The page has a "Add 3 test credits" button that works only against this server.
"""
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parent.parent
LIVE = '--live' in sys.argv
os.environ.update(ADSCAN_TESTING='1', ADSCAN_DATA_DIR=tempfile.mkdtemp(prefix='adscan-studio-dev-'), ADSCAN_QUOTA_SECRET='local-dev',
                  ADSCAN_ADMIN_TOKEN='local-dev', ADSCAN_FREE_POLICY='sample', SUME_ENABLED='1')
if LIVE:
    # Read the key file into the environment without echoing any of it.
    for line in (REPO / 'backend' / '.env').read_text().splitlines():
        name, found, value = line.partition('=')
        if found and name.strip().startswith('SUME_') and value.strip():
            os.environ[name.strip()] = value.strip()
    if not os.environ.get('SUME_API_KEY'):
        sys.exit('Paste your key after SUME_API_KEY= in backend/.env first.')
else:
    os.environ['SUME_API_KEY'] = 'fake'

sys.path.insert(0, str(REPO / 'backend'))
import app as module  # noqa: E402
from flask import Response, request, send_from_directory  # noqa: E402

if not LIVE:
    started = {}

    def reply(status, data):
        return Mock(status_code=status, json=lambda: data)

    def fake(method, url, **kwargs):
        if url.endswith('/files/upload'):
            return reply(201, {'data': {'public_url': 'https://media.sume.com/assets/asset_%d/source' % time.time_ns()}})
        if url.endswith('/generate'):
            job = 'job_' + kwargs['headers']['Idempotency-Key']
            started.setdefault(job, time.time())
            print('Would send to Sume:', kwargs['json'], flush=True)
            return reply(202, {'data': {'job': {'id': job}}})
        job = url.split('/')[-2]
        if url.endswith('/status'):
            return reply(200, {'data': {'sume_status': 'completed' if time.time() - started.get(job, 0) > 6 else 'processing'}})
        return reply(200, {'data': {'result': {'artifacts': [{'url': 'https://media.sume.com/artifacts/demo/video.mp4'}]}}})

    patch('studio.requests.request', side_effect=fake).start()

app = module.app
DEMO = '''<script>addEventListener('load', () => { if (!location.search.includes('demo')) return;
  renderReport('Shot 1, 00:00 to 00:04: wide shot, a woman walks into a teahouse.\\nShot 2, 00:04 to 00:08: close-up, she puts a ring on the table.\\nShot 3, 00:08 to 00:12: medium shot, a man asks for one more chance.');
  document.getElementById('result').style.display = 'block';
  const b = document.createElement('button'); b.textContent = 'Add 3 test credits (local only)'; b.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:99;padding:10px';
  b.onclick = async () => { await fetch('/dev/credit', {method: 'POST', headers: {Authorization: 'Bearer ' + ownerToken}}); await refreshUsage(); document.getElementById('studio-duration').dispatchEvent(new Event('change')); };
  document.body.append(b); });</script>'''


@app.post('/dev/credit')
def dev_credit():
    who = hashlib.sha256(request.headers['Authorization'].removeprefix('Bearer ').encode()).hexdigest()
    with module._jobs_conn() as db:
        db.execute('INSERT INTO payments VALUES(?,?,?,?)', ('cs_dev_%d' % time.time_ns(), who, 3, time.time()))
    return {'ok': True}


@app.get('/site/')
def dev_home():
    html = (REPO / 'index.html').read_text().replace('https://api.adscanvideo.com/api', '/api')
    head, _, tail = html.rpartition('</body>')
    return Response(head + DEMO + '</body>' + tail, mimetype='text/html')


@app.get('/samples/<path:name>')
def dev_samples(name):
    return send_from_directory(REPO / 'samples', name)


@app.get('/studio.js')
def dev_script():
    return send_from_directory(REPO, 'studio.js', max_age=0)


print('AI studio preview:', 'LIVE Sume key, generations cost money' if LIVE else 'fake Sume, nothing is spent', flush=True)
app.run('127.0.0.1', 8766, threaded=True)
