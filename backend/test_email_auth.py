import os,re,time,unittest,hashlib
from unittest.mock import patch,Mock
from test_service import ServiceTests,module,HEADERS,TOKEN

class EmailAuthTests(unittest.TestCase):
    def setUp(self):
        ServiceTests.setUp(self)
        self.tables=('email_accounts','email_login_links','email_sessions','email_owner_aliases','email_preferences','email_consents','report_emails')
        self.clean();self.addCleanup(self.clean)
        patch.dict(os.environ,{'ADSCAN_EMAIL_AUTH_ENABLED':'1','RESEND_API_KEY':'test','RESEND_FROM':'AdScanVideo <reports@mail.adscanvideo.com>'}).start()
        self.sender=patch('email_auth.requests.post',return_value=Mock(json=lambda:{'id':'email-accepted'})).start()
    def clean(self):
        with module._jobs_conn() as db:
            for table in self.tables:db.execute('DELETE FROM '+table)
    def issue(self,consent=False):
        r=self.client.post('/api/auth/email',headers=HEADERS,json={'email':'buyer@example.com','marketing_opt_in':consent});self.assertEqual(r.status_code,202)
        return re.search('/login/#([A-Za-z0-9_-]+)',self.sender.call_args.kwargs['json']['text'])[1]
    def verify(self,token,headers=HEADERS):
        return self.client.post('/api/auth/verify',headers=headers,json={'token':token})
    def test_checkout_requires_verification_not_email_input(self):
        with patch.dict(os.environ,{'STRIPE_BILLING_ENABLED':'1','STRIPE_SECRET_KEY':'test','STRIPE_WEBHOOK_SECRET':'test','STRIPE_PRICE_VIDEO':'price_test','STRIPE_SUBSCRIPTION_ENABLED':'1','STRIPE_PRICE_MONTHLY':'price_month','STRIPE_PORTAL_LOGIN_URL':'https://billing.stripe.com/p/login/test'}):
            self.assertEqual(self.client.post('/api/billing/checkout',headers=HEADERS,json={'offer':'single'}).status_code,401)
            self.assertEqual(self.client.post('/api/billing/subscription/checkout',headers=HEADERS).status_code,401)
            self.issue();self.assertEqual(self.client.post('/api/billing/checkout',headers=HEADERS,json={'offer':'single'}).status_code,401)
    def test_single_use_hashed_token_and_expiry(self):
        t=self.issue();self.assertEqual(self.verify(t).status_code,200);self.assertEqual(self.verify(t).status_code,400)
        with module._jobs_conn() as db:
            self.assertNotEqual(db.execute('SELECT hash FROM email_login_links').fetchone()[0],t);db.execute('UPDATE email_login_links SET created=0,expires=0,consumed=NULL')
        self.assertEqual(self.verify(t).status_code,400)
    def test_same_browser_report_credit_migration_and_retired_key(self):
        r=ServiceTests.post(self);jid=r.json['job_id'];module.jobs[jid].update(status='done',result='stored report');old=hashlib.sha256(TOKEN.encode()).hexdigest()
        with module._jobs_conn() as db:db.execute('INSERT INTO payments VALUES(?,?,?,?)',('cs_test_existing',old,3,time.time()))
        v=self.verify(self.issue());h={'Authorization':'Bearer '+v.json['owner_token']}
        self.assertTrue(v.json['reports_attached']);self.assertEqual(self.client.get('/api/result/'+jid,headers=h).status_code,200);self.assertEqual(self.client.get('/api/result/'+jid,headers=HEADERS).status_code,404)
        self.assertEqual(self.client.get('/api/usage',headers=h).json['paid_credits'],3);self.assertEqual(self.client.get('/api/usage',headers=h).json['free_remaining'],0)
    def test_other_browser_cannot_attach_source_holdings(self):
        r=ServiceTests.post(self);jid=r.json['job_id'];module.jobs[jid].update(status='done',result='private report');v=self.verify(self.issue(),{'Authorization':'Bearer '+'b'*64});h={'Authorization':'Bearer '+v.json['owner_token']}
        self.assertFalse(v.json['reports_attached']);self.assertEqual(self.client.get('/api/result/'+jid,headers=h).status_code,404);self.assertEqual(self.client.get('/api/result/'+jid,headers=HEADERS).status_code,200)
    def test_consent_confirmed_then_durable_unsubscribe(self):
        token=self.issue(consent=True)
        with module._jobs_conn() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM email_consents').fetchone()[0],0)
        v=self.verify(token);h={'Authorization':'Bearer '+v.json['owner_token']}
        with module._jobs_conn() as db:self.assertEqual(db.execute('SELECT opted_in FROM email_consents').fetchone()[0],1)
        t=self.client.post('/api/auth/preferences-link',headers=h).json['url'].split('#')[1];self.client.post('/api/auth/preferences-link',headers=h)
        self.assertEqual(self.client.post('/api/email/unsubscribe',json={'token':t}).status_code,200)
        with module._jobs_conn() as db:self.assertEqual(db.execute('SELECT opted_in FROM email_consents').fetchone()[0],0)
    def test_input_rate_limit_no_premature_signup(self):
        self.assertEqual(self.client.post('/api/auth/email',headers=HEADERS,json={'email':'bad\nemail'}).status_code,400)
        self.issue();self.assertEqual(self.client.post('/api/auth/email',headers=HEADERS,json={'email':'buyer@example.com'}).status_code,429)
        with module._jobs_conn() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM email_accounts').fetchone()[0],0)
    def test_free_verified_watermark_paid_removal(self):
        with patch.dict(os.environ,{'ADSCAN_EXPORT_PAYWALL':'1','ADSCAN_EXPORT_PAYWALL_FROM':'0'}):
            r=ServiceTests.post(self);jid=r.json['job_id'];module.jobs[jid].update(status='done',result='report');self.assertFalse(self.client.get('/api/billing/export/'+jid,headers=HEADERS).json['can_export'])
            v=self.verify(self.issue());h={'Authorization':'Bearer '+v.json['owner_token']};data=self.client.get('/api/billing/export/'+jid,headers=h).json;self.assertTrue(data['can_export']);self.assertTrue(data['watermarked'])
            with module._jobs_conn() as db:db.execute("UPDATE access SET tier='paid' WHERE job=?",(jid,))
            self.assertFalse(self.client.get('/api/billing/export/'+jid,headers=h).json['watermarked'])
    def test_logout_revokes_session_unchecked_consent_off(self):
        v=self.verify(self.issue());h={'Authorization':'Bearer '+v.json['owner_token']};self.assertTrue(self.client.get('/api/auth/session',headers=h).json['authenticated']);self.client.post('/api/auth/logout',headers=h);self.assertFalse(self.client.get('/api/auth/session',headers=h).json['authenticated'])
        with module._jobs_conn() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM email_consents').fetchone()[0],0)
    def test_verified_checkout_and_old_pending_webhook_after_migration(self):
        old=hashlib.sha256(TOKEN.encode()).hexdigest()
        with module._jobs_conn() as db:db.execute('INSERT INTO checkouts(session,owner,created,credits) VALUES(?,?,?,?)',('cs_test_pending',old,time.time(),3))
        verified=self.verify(self.issue());h={'Authorization':'Bearer '+verified.json['owner_token']}
        event={'type':'checkout.session.completed','data':{'object':{'id':'cs_test_pending','client_reference_id':old,'payment_status':'paid','mode':'payment'}}}
        self.assertEqual(ServiceTests.signed_event(self,event).status_code,200)
        self.assertEqual(self.client.get('/api/usage',headers=h).json['paid_credits'],3)
        with patch.dict(os.environ,{'STRIPE_BILLING_ENABLED':'1','STRIPE_SECRET_KEY':'test','STRIPE_WEBHOOK_SECRET':'test','STRIPE_PRICE_VIDEO':'price_test'}):
            self.sender.return_value.json=lambda:{'id':'cs_test_new','url':'https://checkout.stripe.com/test'}
            self.assertEqual(self.client.post('/api/billing/checkout',headers=h,json={'offer':'single'}).status_code,200)
            self.assertEqual(self.sender.call_args.kwargs['data']['customer_email'],'buyer@example.com')
    def test_failed_delivery_and_expired_session(self):
        self.sender.return_value.json=lambda:{}
        self.assertEqual(self.client.post('/api/auth/email',headers=HEADERS,json={'email':'buyer@example.com'}).status_code,502)
        with module._jobs_conn() as db:db.execute('DELETE FROM email_login_links')
        self.sender.return_value.json=lambda:{'id':'email-accepted'}
        v=self.verify(self.issue());h={'Authorization':'Bearer '+v.json['owner_token']}
        with module._jobs_conn() as db:db.execute('UPDATE email_sessions SET expires=0')
        self.assertFalse(self.client.get('/api/auth/session',headers=h).json['authenticated'])
    def test_repeat_login_recovers_account_without_duplicate_signup(self):
        first=self.verify(self.issue());h={'Authorization':'Bearer '+first.json['owner_token']};who=self.client.get('/api/usage',headers=h).json
        with module._jobs_conn() as db:
            account=db.execute('SELECT owner FROM email_accounts').fetchone()[0];db.execute('INSERT INTO payments VALUES(?,?,?,?)',('cs_test_recover',account,2,time.time()));db.execute('UPDATE email_login_links SET created=0')
        other={'Authorization':'Bearer '+'b'*64}
        sent=self.client.post('/api/auth/email',headers=other,json={'email':'buyer@example.com'});self.assertEqual(sent.status_code,202)
        t=re.search('/login/#([A-Za-z0-9_-]+)',self.sender.call_args.kwargs['json']['text'])[1];v=self.verify(t,other)
        self.assertFalse(v.json['new_signup']);self.assertEqual(self.client.get('/api/usage',headers={'Authorization':'Bearer '+v.json['owner_token']}).json['paid_credits'],2)
    def test_switching_email_does_not_move_an_existing_account(self):
        first=self.verify(self.issue());h={'Authorization':'Bearer '+first.json['owner_token']}
        with module._jobs_conn() as db:
            who=db.execute('SELECT owner FROM email_accounts').fetchone()[0];db.execute('INSERT INTO payments VALUES(?,?,?,?)',('cs_test_stay',who,2,time.time()));db.execute('UPDATE email_login_links SET created=0')
        self.assertEqual(self.client.post('/api/auth/email',headers=h,json={'email':'other@example.com'}).status_code,202)
        t=re.search('/login/#([A-Za-z0-9_-]+)',self.sender.call_args.kwargs['json']['text'])[1];v=self.verify(t,h)
        self.assertFalse(v.json['reports_attached']);self.assertEqual(self.client.get('/api/usage',headers={'Authorization':'Bearer '+v.json['owner_token']}).json['paid_credits'],0);self.assertEqual(self.client.get('/api/usage',headers=h).json['paid_credits'],2)
