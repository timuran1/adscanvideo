# AdScanVideo deployment — verified September 20, 2026

## Frontend

Static HTML/CSS/JS, repository `timuran1/adscanvideo`, branch `main`. GitHub Pages
builds on push; CNAME is `adscanvideo.com`. There is no npm build.

**Do not assume a successful GitHub build means the public domain is updated.**
The public domain is attached to a separate manually deployed Cloudflare Pages
project named `adscanvideo`; pushes to GitHub do not update it. After each GitHub
release, deploy the public-only staging directory to this Pages project and verify
the custom domain. The September 20 release was deployed successfully as Pages
deployment `1dab126f`.

Verify both the GitHub origin and the public hostname:

```
gh api repos/timuran1/adscanvideo/pages/builds/latest --jq '.status, .commit'
curl --resolve adscanvideo.com:443:185.199.108.153 https://adscanvideo.com/llms.txt
curl https://adscanvideo.com/llms.txt
```

`_config.yml` excludes backend code from the GitHub Pages site. When deploying to
Cloudflare Pages, stage only public HTML, blog/, privacy/, what-is-adscanvideo/,
sitemap.xml, robots.txt, llms.txt and intentional public assets. Never upload
backend/, .git/, .env, .wrangler/ or server backups. `404.html` prevents unknown
paths from silently becoming the homepage. The current `www.adscanvideo.com`
hostname has no DNS record; production and canonical links use the apex hostname.

## Backend

The API is **Flask/Python**, not Node/Express.

- Host: `170.168.6.33`
- Directory: `/opt/adscanvideo-api`
- Service: `adscanvideo-api`
- Python environment: `/opt/adscanvideo-api/venv`
- Entry: `app.py`, with `service.py`, `network_guard.py`, `job_worker.py` and pipeline/
- Bind: `127.0.0.1:5001`, behind nginx and HTTPS
- Secrets: `/opt/adscanvideo-api/.env` (not in Git)
- State: jobs.db and analytics.db in the API directory
- Capacity: shared 2-core, 2 GB VPS; one analysis at a time
- YouTube extraction: system yt-dlp 2026.08.19, Deno, curl_cffi, and the
  matching yt-dlp-ejs solver package. Keep yt-dlp and yt-dlp-ejs compatible;
  `yt-dlp --list-impersonate-targets` should show available browser targets.

Gunicorn runs `-w 1 --threads 4 -b 127.0.0.1:5001 app:app --timeout 60`.
Do not increase worker count without replacing startup recovery. Each analysis
runs separately with a process-group deadline. See backend/README.md for the API
contract, test command, limits and Stripe activation checklist.

Nginx trusts CF-Connecting-IP only from official Cloudflare address ranges in
`/etc/nginx/snippets/adscan-cloudflare-real-ip.conf`, then overwrites X-Real-IP.
The upload request cap is 201 MB to allow multipart overhead; the application
caps actual video bytes at 200 MB. /admin and /admin/ are IP-restricted. Recovery
and reap endpoints require a server-side admin bearer token.

## Safe deployment

1. Stage source under releases/ and run backend/test_service.py with the venv.
2. Wait for zero active analyses. Back up code/config plus both DBs using SQLite
   backup; do not copy only a SQLite main file while its WAL is active.
3. Preserve stable quota/admin secrets. Disable local Whisper on this small VPS.
4. Install code, validate `nginx -t` and Python compilation, restart API, check
   `/health`, and reload nginx if its configuration changed.
5. Test an analysis to terminal `done`, then a second request (402), private
   result access (404 without owner token), and failures restoring allowance.
6. Deploy frontend and verify actual public content, not only build status.

Production backup for the September 20 rollout:
`/opt/adscanvideo-api/backups/20260920-121224/`.
Rollback should restore matching code/configuration, not overwrite newer user
records with an old database. No SSH credentials belong in this repository.

## Payments

The AdScanVideo Stripe account has live $3.99 one-time Checkout enabled. The
$39.99/month capped plan is implemented in source but gated off in production
pending a recurring Stripe Price, customer portal, signed invoice webhooks, and
backend deployment; the agency pilot is inquiry-only. The API creates Checkout
Sessions server-side and issues a credit only after a signed, paid webhook.
The live Checkout endpoint and hosted payment page were reachable on September
29, 2026; this did not charge a card or prove a real paid webhook delivery.
The frontend no longer offers a launch waitlist. Paid credits are tied to the
browser's owner token. The staged frontend adds an access-key copy and restore
flow; verify it before enabling monthly billing and remind customers to keep
the key private.

## October 4, 2026: candidate moment review

`moments` admission, fractional per-frame markers, transcript intervals and the
original-file verification player are live. Moment mode preserves temporal samples
before the 20-frame cap instead of applying global image deduplication. Production
service.py received only the mode whitelist change; subscription work remains
undeployed. Staged production tests: 38 passed. Local full source: 40 passed.
Live synthetic API test: completed; another owner received 404; second admission
received 402. Revised temporal-sample QA found both controlled movements and the
speech-linked occurrence. Exact action boundaries remain approximate.

Latest backend backup: `/opt/adscanvideo-api/backups/moments-20261004-102222/`.
Native Gemini tests use a separate private local project key. No provider switch
or duration-limit increase was made in production. The controlled 11-minute native
probe found real events plus false candidates; separate short-window verification
rejected the five false candidates. Real-world accuracy and latency require broader
validation. Demo QA records are not evidence of new customer activity.

## Native video release — October 4, 2026

Production uses `ADSCAN_VIDEO_PROVIDER=gemini` with `gemini-3.8-flash` through the native Files API, including video audio. `GEMINI_API_KEY` is private in the server .env. Existing OpenRouter settings remain available for an explicit configuration rollback; provider failures restore allowance through existing job handling, not automatic duplicate calls. Limits remain 10 minutes / 200 MB. Token preflight enforces a conservative $0.25 maximum reservation per generation request.

Customer result/status responses omit internal inference cost. Reports request plain text. Dashcam timeline provides observations and uncertainty without determining fault or legal responsibility. The existing dashboard retains estimated internal costs; a missing separate transcript is labelled as unmeasured for native jobs. Privacy describes native video/audio processing.

Native processing does not extract a separate transcript or count locally sampled frames. Those dashboard measures therefore do not measure native audio/video coverage. The underlying production service.py is patched only for the dashcam mode allowlist; staged monthly billing code from the working repository is not installed by this release.

## Upload-only 45-minute release — October 4, 2026

`ADSCAN_UPLOAD_ONLY=1` rejects URL admissions before quota reservation. `ADSCAN_MAX_DURATION=2700` is shared by validation and usage metadata. The public analyzer accepts files only, up to 45 minutes / 200 MB. `ADSCAN_NATIVE_REQUEST_CAP_USD=0.75` allows native long-video preflight; `ADSCAN_JOB_TIMEOUT=720` and `ADSCAN_STUCK_SECONDS=900` bound longer processing. Existing prices and daily allowances remain.

## Premium duration gate — October 4, 2026

The server derives each worker's duration limit from its reserved entitlement: free and single-credit jobs receive 600 seconds, monthly jobs receive 2700. User request bodies cannot override that limit. Production still has no active monthly checkout; 45-minute access is reserved for the planned $39.99 monthly plan. Usage metadata returns 600 for the currently available tiers. The repository's future monthly reservation logic uses monthly credits before daily free credits, preserving premium job limits.
