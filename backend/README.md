# AdScanVideo API

Production Flask code and its vendored video pipeline. Keep Gunicorn at **one
worker**: startup recovery assumes that a previous process is gone. Use four
request threads so uploads and polling do not block one another. Admission allows
one active analysis on the shared 2 GB VPS; a busy response does not consume quota.

## Configuration

Copy names from `.env.example` to `/opt/adscanvideo-api/.env` on the server. Generate
`ADSCAN_QUOTA_SECRET` and `ADSCAN_ADMIN_TOKEN` with `secrets.token_urlsafe(48)`.
Keep these stable and private. `ADSCAN_ENABLE_WHISPER=0` is intentional: subtitles
are used when available, and otherwise videos up to
`ADSCAN_WHISPER_AUTO_MAX_SECONDS` (120 seconds by default) use the isolated
`base` Whisper model automatically. Longer videos stay visual-only unless
`ADSCAN_ENABLE_WHISPER=1`. Whisper always runs in a disposable process with a
deadline.

Dependencies: Flask, flask-cors, requests, Pillow, gunicorn; system ffmpeg,
ffprobe and yt-dlp with impersonation support. No dependency on the Hermes skill
folder remains. Run:

```
venv/bin/gunicorn -w 1 --threads 4 -b 127.0.0.1:5001 app:app --timeout 60
venv/bin/python -m unittest -v test_service
```

Tests use isolated temporary databases and mocked admission workers; they do not
consume production quota or call the paid model. A worker-deadline test launches
a sleeping subprocess and verifies its termination and cleanup.

## API contract

Generate a random 32-byte browser token. Send it as `Authorization: Bearer TOKEN`
on usage, analyze, upload, status, result and checkout requests. It is a bearer
credential: do not publish it. The server stores only its SHA-256 digest.
Legacy clients submitting without Authorization receive a 128-bit random job ID
that itself acts as the bearer capability for that one job. New browser clients
require their owner token; old 12-character IDs are not accepted publicly.
Clearing browser storage loses access to that browser's results and paid credits;
account recovery is a follow-up before broadly promoting paid usage.

- `GET /api/usage`: free_remaining, paid_credits, reset_at (UTC), billing_enabled.
- `POST /api/analyze`: JSON url, mode, optional question.
- `POST /api/analyze-upload`: multipart file, mode, optional question.
- `GET /api/status/ID` and `/api/result/ID`: require the owning token.
- `POST /api/billing/checkout`: returns a Stripe-hosted checkout URL when configured.
- `POST /api/billing/webhook`: validates Stripe signatures and grants paid credits.
- `/api/reap` and `/api/recover`: require ADSCAN_ADMIN_TOKEN as a bearer token.

Admission returns 202 with job_id, 402 when payment is required, 503 with
Retry-After when the single analysis slot is occupied, and 429 after 20 daily
attempts. Reservation and quota checks are one SQLite transaction. Failed jobs
restore their allowance. Free quota resets at midnight UTC and applies to both
owner and network (IPv6 /64); changing the browser token cannot reset an IP's
allowance. Shared networks share the anonymous free allowance. This is abuse
mitigation, not a verified one-person identity system.

Nginx must overwrite X-Real-IP and trust CF-Connecting-IP **only** from official
Cloudflare CIDRs. Gunicorn must remain bound to loopback. Do not trust arbitrary
X-Forwarded-For values.

## Reliability

Each job owns a process group and a temporary directory. The supervisor kills the
whole group after 480 seconds or excessive scratch-file growth. Download, probe,
frame extraction and optional transcription also have individual deadlines.
Source duration is validated before model calls. Video probing/extraction cannot
open network protocols. Remote downloads use a local forward proxy that rejects
non-public DNS answers and connects directly to the validated address. URLs are
HTTP(S)-only; proxy redirects must pass the same checks.

Maximum upload: 200 MB; maximum duration: 600 seconds for every tier at launch.
Up to 60 raw frames are sampled across the full duration and at most 20 retained.
Transient errors are user-readable and don't expose filesystem paths. Completion
writes result and terminal status together. Restart recovery marks unfinished
jobs failed so credits are restored; importing under ADSCAN_TESTING=1 or
ADSCAN_JOB_WORKER=1 does not perform production recovery.

## Stripe activation (template, not yet live-tested)

1. Create a one-time **USD $0.50** Price in Stripe test mode.
2. Set STRIPE_SECRET_KEY, STRIPE_PRICE_VIDEO and STRIPE_WEBHOOK_SECRET server-side.
3. Register `https://api.adscanvideo.com/api/billing/webhook` for
   checkout.session.completed and checkout.session.async_payment_succeeded.
4. Restart and test an actual test-mode checkout, successful payment, delayed
   webhook, duplicate webhook, cancellation and a failed analysis.
5. Add account recovery and refund/dispute handling before broadly launching.
6. Set matching live credentials only after those checks. Update llms.txt when
   payments launch. Never use the success redirect as proof of payment.

The $19/month plan is explicitly marked as planned. No subscription or unlimited
entitlement is implemented. Checkout always selects the configured price on the
server; fulfillment requires a known checkout, matching owner, paid status,
payment mode, USD currency and exactly 50 cents. The checkout session ID is unique
in the credits ledger, so webhook replay cannot grant duplicate credits.

## Deployment safety

Back up code, configuration, jobs.db and analytics.db first using SQLite backup.
Run tests against the staged release. Wait for active analyses to finish before
restarting. Verify the public frontend, not only GitHub Pages: the domain may be
bound to the separate Cloudflare Pages project. Deploy only static assets there,
not backend/, .git/, .env or deployment configuration. Retain the backup for
rollback; do not replace the live database during a code rollback.
