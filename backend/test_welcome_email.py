import json
import os
import re
import time
import unittest
from unittest.mock import Mock, patch

import requests
import welcome_email
from test_service import ServiceTests, module, HEADERS


class WelcomeEmailTests(unittest.TestCase):
    def setUp(self):
        ServiceTests.setUp(self)
        self.env = patch.dict(os.environ, {
            'ADSCAN_EMAIL_AUTH_ENABLED': '1', 'ADSCAN_WELCOME_ENABLED': '1',
            'RESEND_API_KEY': 'test', 'RESEND_FROM': 'AdScanVideo <reports@example.com>',
            'ADSCAN_INBOX_REPLY_DOMAIN': 'inbox.example.com',
            'ADSCAN_INBOX_RECEIVING_KEY': 'test', 'ADSCAN_INBOX_WEBHOOK_SECRET': 'test'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.clean(); self.addCleanup(self.clean)

    def clean(self):
        with module._jobs_conn() as db:
            for table in ('welcome_emails', 'support_messages', 'support_contacts',
                          'email_accounts', 'email_login_links', 'email_sessions',
                          'email_owner_aliases', 'email_consents'):
                db.execute('DELETE FROM ' + table)

    def verify(self, email='new@example.com', headers=HEADERS):
        with patch('email_auth.requests.post', return_value=Mock(json=lambda: {'id': 'login-test'})) as sender:
            result = self.client.post('/api/auth/email', headers=headers, json={'email': email})
            self.assertEqual(result.status_code, 202)
            token = re.search('/login/#([A-Za-z0-9_-]+)', sender.call_args.kwargs['json']['text'])[1]
        return self.client.post('/api/auth/verify', headers=headers, json={'token': token})

    def queued(self):
        with module._jobs_conn() as db:
            return db.execute('SELECT created,due,status,payload FROM welcome_emails').fetchone()

    def test_only_first_verification_schedules_once_and_does_not_opt_in(self):
        result = self.verify(); self.assertTrue(result.json['new_signup'])
        created, due, status, payload = self.queued()
        self.assertEqual(due - created, 120); self.assertEqual(status, 'scheduled')
        self.assertEqual(json.loads(payload)['to'], ['new@example.com'])
        with module._jobs_conn() as db:
            db.execute('DELETE FROM email_login_links')
            self.assertEqual(db.execute('SELECT count(*) FROM email_consents').fetchone()[0], 0)
        self.assertFalse(self.verify(headers={'Authorization': 'Bearer ' + result.json['owner_token']}).json['new_signup'])
        with module._jobs_conn() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM welcome_emails').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM support_messages').fetchone()[0], 1)

    @patch('welcome_email.requests.post')
    def test_delay_restart_persistence_and_reply_routing(self, send):
        self.verify(); created, due, _, _ = self.queued()
        send.return_value = Mock(json=lambda: {'id': 'welcome-receipt'})
        self.assertEqual(welcome_email.process_due(module._jobs_conn, due - 1), 0)
        self.assertEqual(send.call_count, 0)
        # A separate connection/process can pick up the durable queue after restart.
        self.assertEqual(welcome_email.process_due(module._jobs_conn, due), 1)
        self.assertEqual(welcome_email.process_due(module._jobs_conn, due + 60), 0)
        self.assertEqual(send.call_count, 1)
        payload = send.call_args.kwargs['json']
        self.assertRegex(payload['reply_to'], r'^reply\+[a-z0-9_-]{32}@inbox\.example\.com$')
        with module._jobs_conn() as db:
            msg = db.execute('SELECT status,provider_id FROM support_messages').fetchone()
            self.assertEqual(tuple(msg), ('accepted', 'welcome-receipt'))

    @patch('welcome_email.requests.post')
    def test_timeout_retries_same_payload_and_key_within_provider_window(self, send):
        self.verify(); _, due, _, _ = self.queued()
        send.side_effect = requests.Timeout()
        welcome_email.process_due(module._jobs_conn, due)
        first = send.call_args.kwargs
        self.assertEqual(self.queued()[2], 'retry')
        send.side_effect = None; send.return_value = Mock(json=lambda: {'id': 'recovered'})
        welcome_email.process_due(module._jobs_conn, due + 60)
        self.assertEqual(send.call_args.kwargs, first)
        self.assertEqual(self.queued()[2], 'accepted')

    @patch('welcome_email.requests.post')
    def test_uncertain_old_send_never_retries_after_idempotency_window(self, send):
        self.verify(); _, due, _, _ = self.queued()
        with module._jobs_conn() as db:
            db.execute("UPDATE welcome_emails SET status='sending',attempts=1,first_attempt=?,last_attempt=?", (due, due))
        welcome_email.process_due(module._jobs_conn, due + 23 * 3600)
        send.assert_not_called(); self.assertEqual(self.queued()[2], 'blocked')

    def test_existing_user_and_unverified_input_do_not_receive_welcome(self):
        with module._jobs_conn() as db:
            db.execute('INSERT INTO email_accounts VALUES(?,?,?,?)', ('new@example.com', 'existing-owner', time.time(), time.time()))
        self.assertFalse(self.verify().json['new_signup'])
        self.assertIsNone(self.queued())

    def test_disabled_feature_does_not_enqueue(self):
        with patch.dict(os.environ, {'ADSCAN_WELCOME_ENABLED': '0'}):
            self.verify()
        self.assertIsNone(self.queued())
