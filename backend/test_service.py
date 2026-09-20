import concurrent.futures
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
import shutil
import time
import unittest
from unittest.mock import patch, Mock

TEMP = tempfile.TemporaryDirectory()
os.environ.update(ADSCAN_TESTING='1', ADSCAN_DATA_DIR=TEMP.name, ADSCAN_QUOTA_SECRET='test-only-secret')
import app as module
from network_guard import validate_url, public_addresses

TOKEN = 'a'*64
HEADERS = {'Authorization': 'Bearer '+TOKEN}

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.client = module.app.test_client()
        with module._jobs_conn() as db:
            for table in ('jobs','access','payments','checkouts'):
                db.execute('DELETE FROM '+table)
        self.thread = patch('service.start_worker').start()
        self.validation = patch('service.validate_url', side_effect=lambda url: url).start()
        self.addCleanup(patch.stopall)
        created = []
        original_mkdtemp = tempfile.mkdtemp
        def tracked_mkdtemp(*args, **kwargs):
            directory = original_mkdtemp(*args, **kwargs)
            created.append(directory)
            return directory
        patch('service.tempfile.mkdtemp', side_effect=tracked_mkdtemp).start()
        self.addCleanup(lambda: [shutil.rmtree(p, ignore_errors=True) for p in created])

    def post(self, headers=HEADERS, **kwargs):
        return self.client.post('/api/analyze', json={'url':'https://example.com/video.mp4','mode':'summary'}, headers=headers, **kwargs)

    def finish(self, response, status='done'):
        module.jobs[response.json['job_id']].update(status=status)

    def test_usage_starts_at_one(self):
        self.assertEqual(self.client.get('/api/usage',headers=HEADERS).json['free_remaining'],1)

    def test_second_request_requires_payment(self):
        first=self.post(); self.assertEqual(first.status_code,202); self.finish(first)
        self.assertEqual(self.post().status_code,402)

    def test_new_browser_same_ip_cannot_bypass(self):
        first=self.post(); self.finish(first)
        self.assertEqual(self.post({'Authorization':'Bearer '+'b'*64}).status_code,402)

    def test_same_owner_new_ip_cannot_bypass(self):
        first=self.post(); self.finish(first)
        self.assertEqual(self.post(environ_overrides={'REMOTE_ADDR':'8.8.8.8'}).status_code,402)

    def test_forwarded_header_does_not_bypass(self):
        first=self.post(environ_overrides={'REMOTE_ADDR':'8.8.8.8'}); self.finish(first)
        self.assertEqual(self.post({**HEADERS,'X-Real-IP':'1.1.1.1'},environ_overrides={'REMOTE_ADDR':'8.8.8.8'}).status_code,402)

    def test_failed_job_restores_quota(self):
        first=self.post(); self.finish(first,'error')
        self.assertEqual(self.post().status_code,202)

    def test_daily_reset(self):
        first=self.post(); self.finish(first)
        with module._jobs_conn() as db: db.execute("UPDATE access SET day='2000-01-01'")
        self.assertEqual(self.post().status_code,202)

    def test_private_results(self):
        first=self.post(); jid=first.json['job_id']; self.finish(first)
        self.assertEqual(len(jid),32)
        for endpoint in ('status','result'):
            self.assertEqual(self.client.get(f'/api/{endpoint}/{jid}',headers=HEADERS).status_code,200)
            self.assertEqual(self.client.get(f'/api/{endpoint}/{jid}').status_code,404)
            self.assertEqual(self.client.get(f'/api/{endpoint}/{jid}',headers={'Authorization':'Bearer '+'b'*64}).status_code,404)

    def test_old_frontend_can_poll_its_new_job(self):
        response=self.post({}); self.assertEqual(response.status_code,202)
        jid=response.json['job_id']
        self.assertEqual(len(jid),32)
        self.assertEqual(self.client.get('/api/status/'+jid).status_code,200)
        self.finish(response)
        self.assertEqual(self.post({}).status_code,402)

    def test_admin_is_protected(self):
        self.assertEqual(self.client.post('/api/recover',json={'recover':'1'}).status_code,403)
        self.assertEqual(self.client.get('/api/reap').status_code,403)

    def test_global_capacity(self):
        self.post()
        response=self.post({'Authorization':'Bearer '+'b'*64},environ_overrides={'REMOTE_ADDR':'8.8.4.4'})
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json['code'],'busy')
        with module._jobs_conn() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM access').fetchone()[0],1)

    def test_concurrent_admission_atomic(self):
        def submit(_):
            with module.app.test_client() as client:
                return client.post('/api/analyze',json={'url':'https://example.com/v.mp4'},headers=HEADERS).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            codes=list(pool.map(submit,range(5)))
        self.assertEqual(codes.count(202),1)
        self.assertEqual(codes.count(402),4)

    def test_invalid_requests(self):
        for body in ([],None,{'url':5},{'url':'x','mode':[]},{'url':'x','question':5}):
            self.assertEqual(self.client.post('/api/analyze',json=body).status_code,400)

    def test_upload_filename_is_not_a_path(self):
        with patch('service.tempfile.mkdtemp',return_value=TEMP.name), patch('service.start_worker') as thread:
            response=self.client.post('/api/analyze-upload',data={'file':(io.BytesIO(b'video'),'../../escape.mp4')},headers=HEADERS)
            self.assertEqual(response.status_code,202)
            payload=thread.call_args.args[1]
            self.assertEqual(payload['url'],str(Path(TEMP.name)/'upload.mp4'))
            self.assertTrue(Path(payload['url']).exists())

    def test_empty_and_invalid_upload(self):
        for filename,content in [('bad.exe',b'x'),('video.mp4',b'')]:
            self.assertEqual(self.client.post('/api/analyze-upload',data={'file':(io.BytesIO(content),filename)},headers=HEADERS).status_code,400)

    def test_billing_disabled_without_keys(self):
        with patch.dict(os.environ,{'STRIPE_SECRET_KEY':'','STRIPE_WEBHOOK_SECRET':''}):
            self.assertEqual(self.client.post('/api/billing/checkout',headers=HEADERS).status_code,503)

    def test_webhook_signature_and_idempotency(self):
        who=hashlib.sha256(TOKEN.encode()).hexdigest()
        with module._jobs_conn() as db: db.execute('INSERT INTO checkouts VALUES(?,?,?)',('cs_test',who,time.time()))
        event={'type':'checkout.session.completed','data':{'object':{'id':'cs_test','payment_status':'paid','mode':'payment','client_reference_id':who,'amount_total':399,'currency':'usd'}}}
        raw=json.dumps(event).encode(); stamp=str(int(time.time())); secret='whsec_test'
        sig=hmac.new(secret.encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        with patch.dict(os.environ,{'STRIPE_WEBHOOK_SECRET':secret}):
            self.assertEqual(self.client.post('/api/billing/webhook',data=raw).status_code,400)
            for _ in range(2):
                r=self.client.post('/api/billing/webhook',data=raw,headers={'Stripe-Signature':f't={stamp},v1={sig}'})
                self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.get('/api/usage',headers=HEADERS).json['paid_credits'],1)
        first=self.post(); self.finish(first)
        second=self.post(); self.finish(second)
        self.assertEqual(self.post().status_code,402)

    def test_unpaid_webhook_does_not_grant_credit(self):
        event={'type':'checkout.session.completed','data':{'object':{'id':'x','payment_status':'unpaid','mode':'payment'}}}
        raw=json.dumps(event).encode(); stamp=str(int(time.time()))
        sig=hmac.new(b'test',stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        with patch.dict(os.environ,{'STRIPE_WEBHOOK_SECRET':'test'}):
            self.assertEqual(self.client.post('/api/billing/webhook',data=raw,headers={'Stripe-Signature':f't={stamp},v1={sig}'}).status_code,200)
        self.assertEqual(self.client.get('/api/usage',headers=HEADERS).json['paid_credits'],0)

    def test_pipeline_systemexit_is_terminal_and_cleans(self):
        jid='c'*32; module.jobs[jid]={'status':'starting'}
        directory=tempfile.mkdtemp(prefix='adscan-test-')
        with patch.object(module,'download',side_effect=SystemExit('private stack detail')):
            module.run_analysis(jid,'bad','',work_dir=directory)
        self.assertEqual(module.jobs[jid]['status'],'error')
        self.assertNotIn('private stack',module.jobs[jid]['error'])
        self.assertFalse(Path(directory).exists())

    def test_duration_limit_before_ai(self):
        jid='c'*32; module.jobs[jid]={'status':'starting'}
        with patch.object(module,'download',return_value={'video_path':'test'}), patch.object(module,'get_metadata',return_value={'duration_seconds':601,'width':100}), patch.object(module.requests,'post') as paid:
            module.run_analysis(jid,'test','')
        self.assertIn('10 minutes',module.jobs[jid]['error']); paid.assert_not_called()

    def test_short_video_audio_transcribes_without_global_whisper(self):
        meta={'has_audio':True}
        with patch.dict(os.environ,{'ADSCAN_ENABLE_WHISPER':'0'}):
            self.assertTrue(module.should_transcribe_audio(meta,10,''))
            self.assertFalse(module.should_transcribe_audio(meta,121,''))
            self.assertFalse(module.should_transcribe_audio(meta,10,'captions'))
            self.assertFalse(module.should_transcribe_audio({'has_audio':False},10,''))

    def test_global_whisper_still_allows_long_video_audio(self):
        with patch.dict(os.environ,{'ADSCAN_ENABLE_WHISPER':'1'}):
            self.assertTrue(module.should_transcribe_audio({'has_audio':True},600,''))

    def test_reaper_does_not_touch_finished_jobs(self):
        for jid,status in [('old','extracting'),('done','done')]:
            module.jobs[jid]={'status':status,'created':1}
        with module._jobs_conn() as db: db.execute('UPDATE jobs SET updated=1')
        self.assertEqual(module.reap_stuck_jobs(),1)
        self.assertEqual(module.jobs['done']['status'],'done')

    def test_worker_deadline_kills_and_cleans(self):
        import subprocess, sys
        jid='d'*32; module.jobs[jid]={'status':'starting'}
        directory=tempfile.mkdtemp(prefix='adscan-deadline-')
        actual_popen=subprocess.Popen
        processes=[]
        def sleeping_worker(*args, **kwargs):
            process=actual_popen([sys.executable,'-c','import time; time.sleep(30)'],stdin=subprocess.PIPE,start_new_session=True)
            processes.append(process)
            return process
        with patch.dict(os.environ,{'ADSCAN_JOB_TIMEOUT':'1'}), patch('service.subprocess.Popen',side_effect=sleeping_worker):
            module.app.extensions['adscan']['supervise']({'job_id':jid,'work_dir':directory})
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(module.jobs[jid]['status'],'error')
        self.assertFalse(Path(directory).exists())

    def test_upload_size_enforced(self):
        with patch('service.MAX_FILE',2):
            response=self.client.post('/api/analyze-upload',data={'file':(io.BytesIO(b'oversized'),'video.mp4')},headers=HEADERS)
        self.assertEqual(response.status_code,413)
        self.assertEqual(self.client.get('/api/usage',headers=HEADERS).json['free_remaining'],1)

    def test_playlist_disguised_as_upload_is_rejected(self):
        path=Path(TEMP.name)/'fake.mp4'
        path.write_text('#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1/private\n#EXT-X-ENDLIST\n')
        with self.assertRaises(SystemExit): module.get_metadata(str(path))

    def test_cors(self):
        self.assertIsNone(self.client.get('/api/usage',headers={'Origin':'https://evil.example'}).headers.get('Access-Control-Allow-Origin'))
        self.assertEqual(self.client.get('/api/usage',headers={'Origin':'https://adscanvideo.com'}).headers.get('Access-Control-Allow-Origin'),'https://adscanvideo.com')

    def test_local_and_private_urls_blocked(self):
        for url in ('file:///etc/passwd','http://127.0.0.1/x','http://169.254.169.254/','http://[::1]/','http://10.0.0.1/','https://user:password@example.com/'):
            with self.assertRaises(ValueError): validate_url(url)
        with patch('network_guard.socket.getaddrinfo',return_value=[(2,1,6,'',('10.0.0.1',443))]):
            with self.assertRaises(ValueError): public_addresses('rebinding.example',443)

if __name__=='__main__': unittest.main()
