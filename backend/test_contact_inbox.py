import base64,hashlib,hmac,json,os,time,unittest
from unittest.mock import Mock,patch
from test_service import ServiceTests,module
ADMIN={'Authorization':'Bearer inbox-test-admin'}
class ContactInboxTests(unittest.TestCase):
 def setUp(self):
  ServiceTests.setUp(self)
  self.env=patch.dict(os.environ,{'ADSCAN_ADMIN_TOKEN':'inbox-test-admin','RESEND_API_KEY':'test','RESEND_FROM':'AdScanVideo <reports@example.com>','RESEND_REPLY_TO':'founder@example.com','ADSCAN_INBOX_WEBHOOK_SECRET':'whsec_'+base64.b64encode(b'signing-test').decode(),'ADSCAN_INBOX_RECEIVING_KEY':'receiving-test','ADSCAN_INBOX_REPLY_DOMAIN':'inbox.example.com'});self.env.start();self.addCleanup(self.env.stop)
  with module._jobs_conn() as db:
   for t in ('support_contacts','support_messages','support_sync','support_webhook_events','email_accounts','report_emails','email_consents'):db.execute('DELETE FROM '+t)
   db.execute('INSERT INTO email_accounts VALUES(?,?,?,?)',('buyer@example.com','owner-test',time.time(),time.time()))
   db.execute('INSERT INTO checkouts(session,owner,created,credits) VALUES(?,?,?,?)',('cs_inbox','owner-test',time.time(),1))
  self.ident=self.client.get('/api/admin/contacts',headers=ADMIN).json['contacts'][0]['id']
 def send(self,**updates):
  data={'subject':'Checkout help','body':'Any trouble?','request_id':'inbox-unique-send-00001','confirmed':True,'purpose':'support'};data.update(updates)
  return self.client.post('/api/admin/contacts/'+self.ident+'/messages',headers=ADMIN,json=data)
 def webhook(self,event,stamp=None):
  body=json.dumps(event).encode();stamp=str(stamp or int(time.time()));event_id='evt-'+str(event.get('id','1'));sig=base64.b64encode(hmac.new(b'signing-test',event_id.encode()+b'.'+stamp.encode()+b'.'+body,hashlib.sha256).digest()).decode()
  return self.client.post('/api/email/inbound',data=body,content_type='application/json',headers={'svix-id':event_id,'svix-timestamp':stamp,'svix-signature':'v1,'+sig})
 def test_all_admin_routes_protected(self):
  for method,path in [('get','contacts'),('get','contacts/'+self.ident+'/messages'),('post','contacts/'+self.ident+'/messages'),('post','contacts/'+self.ident+'/mailbox-import')]:self.assertEqual(getattr(self.client,method)('/api/admin/'+path).status_code,403)
 def test_contacts_checkout_is_not_payment_or_consent(self):
  c=self.client.get('/api/admin/contacts',headers=ADMIN).json['contacts'][0];self.assertEqual(c['email'],'buyer@example.com');self.assertEqual(c['credit_checkouts'],1);self.assertEqual(c['confirmed_payments'],0);self.assertFalse(c['marketing_opt_in']);self.assertNotIn('owner',c)
  self.assertEqual(self.client.get('/api/admin/contacts?q=nomatch',headers=ADMIN).json['total'],0)
 @patch('contact_inbox.requests.post')
 def test_support_send_idempotent_and_no_marketing(self,sender):
  sender.return_value=Mock(json=lambda:{'id':'provider-support-1'})
  self.assertEqual(self.send().status_code,202);self.assertEqual(self.send().status_code,200);self.assertEqual(sender.call_count,1);self.assertEqual(sender.call_args.kwargs['json']['to'],['buyer@example.com'])
  reply=sender.call_args.kwargs['json']['reply_to'];self.assertEqual(reply,reply.lower());self.assertTrue(reply.endswith('@inbox.example.com'))
  self.assertEqual(self.send(body='Changed').status_code,409);self.assertEqual(self.send(purpose='marketing',request_id='other-unique-send-00001').status_code,400)
 @patch('contact_inbox.requests.post',side_effect=TimeoutError)
 def test_timeout_does_not_duplicate(self,sender):
  self.assertEqual(self.send().json['status'],'unknown');self.assertEqual(self.send().json['status'],'unknown');self.assertEqual(sender.call_count,1)
 def test_gmail_import_deduplicates_and_checks_contact(self):
  message={'provider_id':'1a15dc69dc654201','thread_id':'1a15dc69dc654201','direction':'outgoing','subject':'Help','body':'Plain message','created':time.time(),'sender':'founder@example.com','recipient':'buyer@example.com'};url='/api/admin/contacts/'+self.ident+'/mailbox-import'
  for _ in range(2):self.assertEqual(self.client.post(url,headers=ADMIN,json={'messages':[message]}).status_code,200)
  self.assertEqual(len(self.client.get('/api/admin/contacts/'+self.ident+'/messages',headers=ADMIN).json['messages']),1)
  message['recipient']='other@example.com';self.assertEqual(self.client.post(url,headers=ADMIN,json={'messages':[message]}).status_code,400)
 @patch('contact_inbox.requests.get')
 def test_signed_reply_fetched_and_deduplicated(self,fetch):
  with module._jobs_conn() as db:token=db.execute('SELECT reply_token FROM support_contacts WHERE id=?',(self.ident,)).fetchone()[0]
  pid='12345678-1234-1234-1234-123456789012';fetch.return_value=Mock(json=lambda:{'to':['reply+'+token+'@inbox.example.com'],'from':'buyer@example.com','subject':'Re: Help','html':'<script>alert(1)</script><p>Need help</p>'});event={'type':'email.received','data':{'email_id':pid}}
  self.assertEqual(self.webhook(event).status_code,200);self.assertEqual(self.webhook(event).status_code,200);self.assertEqual(fetch.call_count,1)
  messages=self.client.get('/api/admin/contacts/'+self.ident+'/messages',headers=ADMIN).json['messages'];self.assertEqual(len(messages),1);self.assertEqual(messages[0]['body'],'Need help')
  sync=self.client.get('/api/admin/contacts',headers=ADMIN).json['sync'];self.assertTrue(any(x['provider']=='resend' for x in sync))
  self.assertEqual(self.webhook(event,stamp=int(time.time()-1000)).status_code,403);self.assertEqual(self.client.post('/api/email/inbound',json=event).status_code,403)
 @patch('contact_inbox.requests.get')
 def test_unrelated_sender_not_imported(self,fetch):
  with module._jobs_conn() as db:token=db.execute('SELECT reply_token FROM support_contacts WHERE id=?',(self.ident,)).fetchone()[0]
  fetch.return_value=Mock(json=lambda:{'to':['reply+'+token+'@inbox.example.com'],'from':'other@example.com','text':'Not this contact'})
  self.assertEqual(self.webhook({'type':'email.received','data':{'email_id':'12345678-1234-1234-1234-123456789012'}}).status_code,200);self.assertEqual(self.client.get('/api/admin/contacts/'+self.ident+'/messages',headers=ADMIN).json['messages'],[])
