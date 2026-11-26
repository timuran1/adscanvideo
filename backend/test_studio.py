import io
import os
import time
import unittest
from unittest.mock import patch, Mock

import requests
from PIL import Image
import test_service as base
from test_service import module, HEADERS, TOKEN
import hashlib

OWNER = hashlib.sha256(TOKEN.encode()).hexdigest()
OTHER = {'Authorization': 'Bearer ' + 'b' * 64}


def reply(status, data):
    return Mock(status_code=status, json=lambda: data)


class StudioTests(unittest.TestCase):
    def setUp(self):
        base.ServiceTests.setUp(self)
        self.clean(); self.addCleanup(self.clean)
        patch.dict(os.environ, {'SUME_ENABLED': '1', 'SUME_API_KEY': 'sume_test'}).start()
        self.calls = []
        self.submit_reply = lambda: reply(202, {'data': {'job': {'id': 'job_12345678-aaaa'}}})
        self.state = 'processing'
        def fake(method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if url.endswith('/v1/files/upload'):
                return reply(201, {'data': {'public_url': f'https://media.sume.com/assets/asset_{len(self.calls)}/source'}})
            if url.endswith('/generate'):
                return self.submit_reply()
            if url.endswith('/status'):
                return reply(200, {'data': {'sume_status': self.state}})
            if url.endswith('/result'):
                return reply(200, {'data': {'result': {'artifacts': [{'url': 'https://media.sume.com/artifacts/artf_1/video.mp4'}]}}})
            raise AssertionError(url)
        patch('studio.requests.request', side_effect=fake).start()
        patch('studio.probe_seconds', side_effect=lambda path: self.clip_seconds).start()
        self.clip_seconds = 12.0

    def clean(self):
        # Other suites share this database and owner; leftover jobs would hold their credits.
        with module._jobs_conn() as db:
            db.execute('DELETE FROM studio_assets'); db.execute('DELETE FROM studio_jobs')

    def credit(self, count=1, owner=OWNER):
        with module._jobs_conn() as db:
            db.execute('INSERT INTO payments VALUES(?,?,?,?)', ('cs_' + os.urandom(6).hex(), owner, count, time.time()))

    def paid(self):
        return self.client.get('/api/usage', headers=HEADERS).json['paid_credits']

    def asset(self, kind='image', headers=HEADERS):
        if kind == 'image':
            data = io.BytesIO(); Image.new('RGB', (400, 600)).save(data, 'PNG'); name = 'My Face.png'
        else:
            data = io.BytesIO(b'media'); name = 'clip.mp4' if kind == 'video' else 'voice.wav'
        data.seek(0)
        r = self.client.post('/api/studio/assets', headers=headers, data={'kind': kind, 'file': (data, name)})
        self.assertEqual(r.status_code, 201, r.json)
        return r.json['asset_id']

    def generate(self, headers=HEADERS, **body):
        body = {'mode': 'recreate', 'prompt': 'A kite', 'duration': 10, 'aspect': '9:16', 'rights': True, **body}
        return self.client.post('/api/studio/generate', headers=headers, json=body)

    def submits(self):
        return [c for c in self.calls if c[1].endswith('/generate')]

    def age(self, jid, column='checked', seconds=60):
        with module._jobs_conn() as db:
            db.execute(f'UPDATE studio_jobs SET {column}={column}-? WHERE id=?', (seconds, jid))

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {'SUME_ENABLED': '0'}):
            self.assertFalse(self.client.get('/api/studio/config').json['enabled'])
            self.credit()
            self.assertEqual(self.generate().status_code, 503)
        self.assertEqual(self.calls, [])

    def test_generation_needs_paid_credits_and_never_uses_the_free_sample(self):
        r = self.generate()
        self.assertEqual((r.status_code, r.json['code'], r.json['credits_needed']), (402, 'payment_required', 1))
        self.assertEqual(self.submits(), [])
        self.assertEqual(self.client.get('/api/usage', headers=HEADERS).json['free_remaining'], 1)

    def test_recreate_spends_credits_names_references_and_returns_the_video(self):
        self.credit(2)
        face, voice = self.asset('image'), self.asset('audio')
        r = self.generate(prompt='@Nigora walks out and says the line in @voice', duration=25,
                          refs=[{'asset': face, 'label': 'Nigora'}, {'asset': voice, 'label': 'voice'}])
        self.assertEqual((r.status_code, r.json['status'], r.json['credits']), (202, 'running', 2))
        _, _, sent = self.submits()[0]
        self.assertEqual(sent['headers']['Idempotency-Key'], r.json['job_id'])
        self.assertEqual(sent['json']['prompt'], '@Image1 walks out and says the line in @Audio1')
        self.assertEqual((sent['json']['duration'], sent['json']['aspect_ratio'], sent['json']['model'], sent['json']['resolution']), (25, '9:16', 'magi-2', '1080p'))
        self.assertEqual(len(sent['json']['reference_image_urls']), 1); self.assertEqual(len(sent['json']['reference_audio_urls']), 1)
        self.assertEqual(self.paid(), 0)
        self.assertEqual(self.client.get('/api/studio/jobs/' + r.json['job_id'], headers=HEADERS).json['status'], 'running')
        self.state = 'completed'; self.age(r.json['job_id'])
        done = self.client.get('/api/studio/jobs/' + r.json['job_id'], headers=HEADERS).json
        self.assertEqual((done['status'], done['url']), ('done', 'https://media.sume.com/artifacts/artf_1/video.mp4'))
        self.assertEqual(self.paid(), 0)
        self.assertEqual(len(self.submits()), 1)
        # The file name the user chose is never sent on.
        self.assertTrue(all(c[2]['params']['filename'].startswith('reference.') for c in self.calls if c[1].endswith('/upload')))

    def test_failed_generation_restores_credits(self):
        self.credit()
        r = self.generate()
        self.assertEqual(self.paid(), 0)
        self.state = 'failed'; self.age(r.json['job_id'])
        self.assertEqual(self.client.get('/api/studio/jobs/' + r.json['job_id'], headers=HEADERS).json['status'], 'failed')
        self.assertEqual(self.paid(), 1)

    def test_refused_submit_restores_credits(self):
        self.credit()
        self.submit_reply = lambda: reply(429, {'error': {'code': 'queue_full'}})
        r = self.generate()
        self.assertEqual((r.json['status'], self.paid()), ('failed', 1))
        self.assertIn('busy', r.json['error'])

    def test_unknown_submit_outcome_is_retried_under_the_same_key_not_resubmitted(self):
        self.credit()
        def lost():
            raise requests.ConnectionError()
        self.submit_reply = lost
        r = self.generate(); jid = r.json['job_id']
        self.assertEqual((r.json['status'], self.paid()), ('submitting', 0))
        self.client.get('/api/studio/jobs/' + jid, headers=HEADERS)
        self.assertEqual(len(self.submits()), 1)
        self.submit_reply = lambda: reply(202, {'data': {'job': {'id': 'job_12345678-aaaa'}}})
        self.age(jid, 'updated')
        self.assertEqual(self.client.get('/api/studio/jobs/' + jid, headers=HEADERS).json['status'], 'running')
        self.assertEqual({c[2]['headers']['Idempotency-Key'] for c in self.submits()}, {jid})
        self.assertEqual(self.submits()[0][2]['json'], self.submits()[1][2]['json'])

    def test_one_generation_at_a_time(self):
        self.credit(2)
        self.assertEqual(self.generate().status_code, 202)
        self.assertEqual(self.generate().status_code, 409)

    def test_swap_edits_the_source_clip_and_bills_its_length(self):
        self.credit(2)
        clip, face, voice = self.asset('video'), self.asset('image'), self.asset('audio')
        self.assertEqual(self.generate(mode='swap').status_code, 400)
        self.assertEqual(self.generate(mode='swap', source=clip).status_code, 400)  # needs a photo
        self.assertEqual(self.generate(mode='swap', source=clip, refs=[{'asset': voice, 'label': 'v'}]).status_code, 400)
        self.clip_seconds = 11.4
        clip = self.asset('video')
        r = self.generate(mode='swap', source=clip, prompt='Replace the man with @hero', refs=[{'asset': face, 'label': 'hero'}])
        self.assertEqual((r.status_code, r.json['credits']), (202, 2))
        sent = self.submits()[0][2]['json']
        self.assertTrue(sent['video_url'].startswith('https://media.sume.com/'))
        self.assertEqual((sent['model'], sent['prompt'], sent['duration'], sent['resolution']), ('higgsfield-genjutsu-light-object-swap', 'Replace the man with @Image1', 12, '480p'))
        for key in ('aspect_ratio', 'generate_audio', 'reference_video_urls'):
            self.assertNotIn(key, sent)
        self.state = 'completed'; self.age(r.json['job_id'])
        self.credit(4)
        # Motion transfer uses the other Remix model, and the prompt is optional.
        r = self.generate(mode='swap', style='motion', source=clip, prompt='', refs=[{'asset': face, 'label': 'hero'}])
        sent = self.submits()[1][2]['json']
        self.assertEqual((r.status_code, sent['model']), (202, 'higgsfield-genjutsu-light')); self.assertNotIn('prompt', sent)
        self.state = 'completed'; self.age(r.json['job_id'])
        # Up to 15 seconds stays on Light; longer clips use full Remix at 4 s per credit.
        self.clip_seconds = 15.0
        self.assertEqual(self.generate(mode='swap', source=self.asset('video'), refs=[{'asset': face, 'label': 'hero'}]).json['credits'], 2)
        self.state = 'completed'; self.age(self.client.get('/api/studio/jobs', headers=HEADERS).json['jobs'][0]['job_id'])
        self.clip_seconds = 29.5; self.credit(8)
        r = self.generate(mode='swap', source=self.asset('video'), refs=[{'asset': face, 'label': 'hero'}])
        self.assertEqual((r.json['credits'], self.submits()[3][2]['json']['model'], self.submits()[3][2]['json']['duration']), (8, 'higgsfield-genjutsu-object-swap', 30))

    def test_reference_clip_billing_follows_the_model(self):
        self.credit(8)
        clip = self.asset('video')
        # MAGI-2 charges the output only, so a reference clip adds nothing.
        r = self.generate(source=clip, prompt='Same camera move as @video', duration=30)
        self.assertEqual(r.json['credits'], 2)
        self.assertEqual(self.submits()[0][2]['json']['prompt'], 'Same camera move as @Video1')
        self.state = 'completed'; self.age(r.json['job_id'])
        # Seedance bills the clip too, and never less than two thirds of the output: 30 + 20 seconds.
        self.clip_seconds = 4.0
        short = self.asset('video')
        with patch.dict(os.environ, {'SUME_VIDEO_MODEL': 'seedance-2.5-flash'}):
            self.assertEqual(self.generate(source=short, duration=30).json['credits'], 4)

    def test_lost_job_id_does_not_block_the_owner_or_strand_the_video(self):
        self.credit(2)
        lost = self.generate().json['job_id']
        self.state = 'completed'; self.age(lost)
        # The browser never polled the job, yet the next request moves it on.
        self.assertEqual(self.generate().status_code, 202)
        jobs = self.client.get('/api/studio/jobs', headers=HEADERS).json['jobs']
        self.assertEqual([j['status'] for j in jobs if j['job_id'] == lost], ['done'])

    def test_long_clips_and_bad_inputs_are_rejected_before_any_charge(self):
        self.credit()
        self.clip_seconds = 31.0
        data = io.BytesIO(b'media')
        r = self.client.post('/api/studio/assets', headers=HEADERS, data={'kind': 'video', 'file': (data, 'clip.mp4')})
        self.assertEqual(r.status_code, 400)
        for body in ({'rights': False}, {'duration': 31}, {'duration': 3}, {'aspect': '4:5'}, {'prompt': ''}, {'mode': 'other'}):
            self.assertEqual(self.generate(**body).status_code, 400, body)
        self.assertEqual((self.submits(), self.paid()), ([], 1))

    def test_jobs_and_references_are_private_to_their_owner(self):
        self.credit()
        face = self.asset('image')
        r = self.generate()
        self.assertEqual(self.client.get('/api/studio/jobs/' + r.json['job_id'], headers=OTHER).status_code, 404)
        self.credit(1, hashlib.sha256(('b' * 64).encode()).hexdigest())
        self.assertEqual(self.generate(headers=OTHER, refs=[{'asset': face, 'label': 'x'}]).status_code, 400)

    def test_generated_image_becomes_a_reference_and_costs_no_credit(self):
        self.assertEqual(self.client.post('/api/studio/image', headers=HEADERS, json={'prompt': 'A red bottle'}).status_code, 402)
        self.credit()
        r = self.client.post('/api/studio/image', headers=HEADERS, json={'prompt': 'A red bottle', 'aspect': '1:1'})
        self.assertEqual((r.status_code, r.json['credits']), (202, 0))
        self.assertTrue(self.calls[-1][1].endswith('/v1/image-router/generate'))
        self.state = 'completed'; self.age(r.json['job_id'])
        done = self.client.get('/api/studio/jobs/' + r.json['job_id'], headers=HEADERS).json
        self.assertEqual((done['status'], done['asset_id'], self.paid()), ('done', r.json['job_id'], 1))
        self.assertEqual(self.generate(refs=[{'asset': done['asset_id'], 'label': 'bottle'}]).status_code, 202)


if __name__ == '__main__':
    unittest.main()
