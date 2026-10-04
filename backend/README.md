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
ffprobe and yt-dlp with impersonation support, Deno, and yt-dlp-ejs (install
the matching `yt-dlp[default]` dependency group). YouTube can require the EJS
solver to expose downloadable formats; the downloader also enables yt-dlp's
official `ejs:github` fallback if the package is missing. No dependency on the Hermes skill
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
- `GET /api/billing/checkout/<session_id>`: returns `pending` or `paid` for a
  Checkout Session owned by the caller, so the return page can confirm that the
  signed webhook granted the credit. Unknown sessions return 404.
- `POST /api/billing/webhook`: validates Stripe signatures and grants paid credits.
- `POST /api/billing/subscription/checkout`: creates a $39.99/month Checkout Session
  only when STRIPE_SUBSCRIPTION_ENABLED=1 and the matching recurring Price and
  customer portal login URL are configured. The browser must save its private
  access key before redirecting to Stripe.
- `GET /api/billing/subscription/checkout/<session_id>`: confirms fulfillment
  after a signed paid-invoice webhook, never merely from the success redirect.
- `GET /api/usage` also returns monthly_remaining and monthly_ends_at. Each paid
  invoice grants 20 analyses only during its billing period; unused analyses
  expire. A failed analysis restores that period's allowance.
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

1. Create a one-time **USD $3.99** Price in the AdScanVideo Stripe sandbox,
   with an eligible digital-product tax code and Managed Payments enabled.
   The current sandbox Price ID is `price_1UKfLtAuDBUbq28jU41dTSsU`.
   Stripe reports that this Price has inclusive tax behavior.
2. Set STRIPE_SECRET_KEY, STRIPE_PRICE_VIDEO and STRIPE_WEBHOOK_SECRET server-side.
   Keep `STRIPE_BILLING_ENABLED=0` until a complete checkout and webhook test
   passes. Set it to `1` only with matching live credentials at launch.
3. Register `https://api.adscanvideo.com/api/billing/webhook` for
   checkout.session.completed and checkout.session.async_payment_succeeded.
4. Restart and test an actual test-mode checkout, successful payment, delayed
   webhook, duplicate webhook, cancellation and a failed analysis.
5. Add account recovery and refund/dispute handling before broadly launching.
6. Set matching live credentials only after those checks. Update llms.txt when
   payments launch. Never use the success redirect as proof of payment.

The one-time checkout always selects its configured price on the server;
fulfillment requires a signed webhook for a known checkout, matching owner,
paid status and payment mode. Stripe may add tax or convert the displayed
currency, so fulfillment does not require the final total to be exactly USD
$3.99. The checkout session ID is unique in the credits ledger, so webhook
replay cannot grant duplicate credits. Monthly checkout remains behind a
separate disabled flag until its launch gate below is complete.

## Monthly subscription launch gate

The recurring implementation is staged but remains disabled in production until
all of these have been verified in the AdScanVideo Stripe account (not Connect Limo):

1. Create a recurring USD $39.99/month Price for a 20-analysis plan, check its
   product category/tax treatment, and set `STRIPE_PRICE_MONTHLY` server-side.
2. Enable Stripe's customer portal login link for the same account. Customers
   must be able to cancel and update payment methods using their email. Set its
   `https://billing.stripe.com/p/login/...` URL as `STRIPE_PORTAL_LOGIN_URL`.
3. Add `invoice.payment_succeeded`, `invoice.payment_failed`, and
   `customer.subscription.deleted` to the existing signed webhook destination,
   alongside the current Checkout events. Verify its signing secret still matches.
4. Test checkout, initial invoice, duplicate and out-of-order webhooks, renewal,
   failed payment, cancellation, access-key restore in another browser, and
   allowance expiry in Stripe test mode. The key must have Checkout Sessions
   write permission for recurring Checkout Sessions.
5. Back up the production SQLite databases, deploy backend code, then set
   `STRIPE_SUBSCRIPTION_ENABLED=1` only after the live recurring Price and portal
   are confirmed. Verify the live site advertises the plan and an actual paid
   invoice grants 20 analyses. Do not use an unpaid success redirect as proof.

The monthly allowance is attached to a private browser access key because the
product has no customer accounts. Customers must save it to restore access on
another browser; Stripe's portal manages billing, not analysis access.

## Deployment safety

Back up code, configuration, jobs.db and analytics.db first using SQLite backup.
Run tests against the staged release. Wait for active analyses to finish before
restarting. Verify the public frontend, not only GitHub Pages: the domain may be
bound to the separate Cloudflare Pages project. Deploy only static assets there,
not backend/, .git/, .env or deployment configuration. Retain the backup for
rollback; do not replace the live database during a code rollback.

## Find moments (beta)

`mode=moments` uses the existing question field for visual and optional dialogue
criteria. Candidates are grouped by continuous action, with separate visual
sample times and transcript intervals. Every sampled frame now includes a
millisecond timestamp. Transcript intervals preserve both start and end times.
Possible overlap is a candidate for manual verification, never a confirmed match.
This remains a sparse-sample workflow (at most 20 selected frames). Short events
can be missed; silence, missing transcription, or transcription errors can prevent
dialogue matching. Longer uploaded videos currently lack automatic transcription
by default; this mode does not change the audio budget.

The frontend's result timestamps seek within a local original-file player. The
player does not upload the reselected verification file. Report text remains
unchanged for Copy, Word and PDF. After a reload, select the same original file.

Before launch: deploy matching app.py, service.py and pipeline/transcribe.py to
the API, then deploy index.html. Do not expose the new mode before API validation
accepts it. Run `python -m unittest -q test_service` and
`node scripts/test-moment-review.cjs` from the repo root for the frontend test.
Finally test a non-private clip containing two similar actions with dialogue at
only one occurrence, plus a silent clip. Check original footage manually; mocked
API tests verify timestamps and request wiring, not model accuracy.
