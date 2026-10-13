import os
import re
import unittest
from unittest.mock import patch, Mock
from test_service import ServiceTests, module, HEADERS

class ReportEmailTests(unittest.TestCase):
    def setUp(self):
        ServiceTests.setUp(self)
        with module._jobs_conn() as db:
            db.execute('DELETE FROM report_emails'); db.execute('DELETE FROM email_consents')
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,{'ADSCAN_REPORT_EMAIL_ENABLED':'1','RESEND_API_KEY':'test-key','RESEND_FROM':'AdScanVideo <reports@mail.adscanvideo.com>'}).start()
        response=ServiceTests.post(self);self.jid=response.json['job_id'];module.jobs[self.jid].update(status='done',result='Private report <script>example</script>',title='Private title')
        self.sender=patch('report_email.requests.post',return_value=Mock(json=lambda:{'id':'provider-test'})).start()

    def send(self,headers=HEADERS,**extra):
        return self.client.post('/api/reports/'+self.jid+'/email',headers=headers,json={'email':'owner@example.com',**extra})

    def token(self):
        return re.search(r'/report/#([A-Za-z0-9_-]+)',self.sender.call_args.kwargs['json']['text'])[1]

    def test_foreign_owner_cannot_send(self):
        self.assertEqual(self.send(headers={'Authorization':'Bearer '+'b'*64}).status_code,404);self.sender.assert_not_called()

    def test_scoped_link_and_confirmed_consent(self):
        self.assertEqual(self.send(marketing_opt_in=True).status_code,202)
        token=self.token(); headers={'Authorization':'Bearer '+token}
        data=self.client.get('/api/emailed-report',headers=headers)
        self.assertEqual(data.status_code,200);self.assertEqual(data.json['result'],'Private report <script>example</script>');self.assertFalse(data.json['marketing_opt_in'])
        self.assertEqual(self.client.get('/api/usage',headers=headers).json['paid_credits'],0)
        self.assertEqual(self.client.get('/api/result/'+self.jid,headers=headers).status_code,404)
        self.assertEqual(self.client.post('/api/report-email-preference',headers=headers,json={'marketing_opt_in':True}).status_code,200)
        self.assertTrue(self.client.get('/api/emailed-report',headers=headers).json['marketing_opt_in'])
        self.client.post('/api/report-email-preference',headers=headers,json={'marketing_opt_in':False})
        self.assertFalse(self.client.get('/api/emailed-report',headers=headers).json['marketing_opt_in'])
        with module._jobs_conn() as db:db.execute('UPDATE report_emails SET expires=0')
        self.assertEqual(self.client.get('/api/emailed-report',headers=headers).status_code,404)

    def test_rate_limit_and_private_token_storage(self):
        self.assertEqual(self.send().status_code,202);token=self.token()
        self.assertEqual(self.send().status_code,429)
        with module._jobs_conn() as db:
            row=db.execute('SELECT token_hash FROM report_emails').fetchone()
        self.assertNotEqual(row[0],token)

    def test_failure_does_not_create_readable_link(self):
        self.sender.return_value.json=lambda:{}
        self.assertEqual(self.send().status_code,502)
        self.assertEqual(self.client.get('/api/emailed-report',headers={'Authorization':'Bearer '+self.token()}).status_code,404)

    def test_invalid_email_disabled_and_incomplete(self):
        self.assertEqual(self.send(email='bad\naddress').status_code,400)
        module.jobs[self.jid].update(status='analyzing');self.assertEqual(self.send().status_code,409)
        with patch.dict(os.environ,{'ADSCAN_REPORT_EMAIL_ENABLED':'0'}):self.assertEqual(self.send().status_code,503)
        self.sender.assert_not_called()
