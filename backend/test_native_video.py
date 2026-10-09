import os,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import native_video as native
class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'video.mp4';self.path.write_bytes(b'test-video')
        self.session=Mock();self.session.headers={}
    def responses(self,count=100):
        def response(data,headers=None):
            r=Mock(ok=True,headers=headers or {});r.json.return_value=data;return r
        self.session.request.side_effect=[
            response({}, {'X-Goog-Upload-URL':'https://generativelanguage.googleapis.com/upload/test'}),
            response({'file':{'name':'files/test','uri':'https://generativelanguage.googleapis.com/v1beta/files/test','state':'ACTIVE'}}),
            response({'totalTokens':count}),
            response({'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'00:00 - 00:03\nVisible action.'}]}}],'usageMetadata':{'promptTokenCount':100,'candidatesTokenCount':20}})]
    @patch.dict(os.environ,{'GEMINI_API_KEY':'test-only'})
    def test_native_report_and_cleanup(self):
        self.responses()
        with patch('native_video.requests.Session',return_value=self.session):
            answer=native.analyze(self.path,'dashcam','Road timeline')
        self.assertIn('Visible action',answer['text']);self.session.delete.assert_called_once()
        self.assertFalse(self.session.trust_env)
        request=self.session.request.call_args_list[-1].kwargs['json']
        self.assertIn('fault',request['systemInstruction']['parts'][0]['text'])
        self.assertIn('No asterisks',request['systemInstruction']['parts'][0]['text'])
    @patch.dict(os.environ,{'GEMINI_API_KEY':'test-only','ADSCAN_NATIVE_REQUEST_CAP_USD':'0.25'})
    def test_expensive_input_stops_before_generation(self):
        self.responses(1000000)
        with patch('native_video.requests.Session',return_value=self.session),self.assertRaises(native.NativeUnavailable):
            native.analyze(self.path,'shots','')
        self.assertEqual(self.session.request.call_count,3);self.session.delete.assert_called_once()
    @patch.dict(os.environ,{'GEMINI_API_KEY':'test-only'})
    def test_provider_failure_does_not_leak_error(self):
        self.session.request.return_value=Mock(ok=False,status_code=401)
        with patch('native_video.requests.Session',return_value=self.session),self.assertRaisesRegex(native.NativeUnavailable,'temporarily unavailable'):
            native.analyze(self.path,'shots','')
    @patch.dict(os.environ,{'GEMINI_API_KEY':'test-only'})
    def test_generation_503_recovers_once(self):
        self.responses(); responses=list(self.session.request.side_effect)
        failed=Mock(ok=False,status_code=503,headers={})
        self.session.request.side_effect=responses[:-1]+[failed,responses[-1]]
        with patch('native_video.requests.Session',return_value=self.session),patch('native_video.time.sleep') as sleep:
            with self.assertLogs('native_video',level='WARNING') as logs:
                answer=native.analyze(self.path,'summary','')
        self.assertIn('Visible action',answer['text']);self.assertEqual(self.session.request.call_count,5)
        sleep.assert_called_once();self.assertIn('stage=generate http_status=503',logs.output[0])
        self.assertNotIn('test-only',' '.join(logs.output));self.session.delete.assert_called_once()

    @patch.dict(os.environ,{'GEMINI_API_KEY':'test-only'})
    def test_generation_retry_is_bounded(self):
        self.responses();responses=list(self.session.request.side_effect)
        self.session.request.side_effect=responses[:-1]+[Mock(ok=False,status_code=429,headers={})]*2
        with patch('native_video.requests.Session',return_value=self.session),patch('native_video.time.sleep'),self.assertRaises(native.NativeUnavailable):
            native.analyze(self.path,'summary','')
        self.assertEqual(self.session.request.call_count,5);self.session.delete.assert_called_once()

    @patch.dict(os.environ,{'GEMINI_API_KEY':'test-only','ADSCAN_NATIVE_REQUEST_CAP_USD':'0.04'})
    def test_generation_retry_cannot_exceed_request_cap(self):
        self.responses();responses=list(self.session.request.side_effect)
        self.session.request.side_effect=responses[:-1]+[Mock(ok=False,status_code=503,headers={})]
        with patch('native_video.requests.Session',return_value=self.session),patch('native_video.time.sleep') as sleep,self.assertRaises(native.NativeUnavailable):
            native.analyze(self.path,'summary','')
        self.assertEqual(self.session.request.call_count,4);sleep.assert_not_called()

    @patch.dict(os.environ,{'GEMINI_API_KEY':'test-only'})
    def test_ambiguous_generation_timeout_not_retried(self):
        self.responses();responses=list(self.session.request.side_effect)
        self.session.request.side_effect=responses[:-1]+[native.requests.Timeout('private URL and token')]
        with patch('native_video.requests.Session',return_value=self.session),self.assertRaisesRegex(native.NativeUnavailable,'connection failed'):
            native.analyze(self.path,'summary','')
        self.assertEqual(self.session.request.call_count,4);self.session.delete.assert_called_once()

if __name__=='__main__': unittest.main()
