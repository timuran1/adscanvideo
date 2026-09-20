# AdScanVideo deployment — verified September 20, 2026

## Frontend

Static HTML/CSS/JS, repository `timuran1/adscanvideo`, branch `main`. GitHub Pages
builds on push; CNAME is `adscanvideo.com`. There is no npm build.

**Do not assume a successful GitHub build means the public domain is updated.**
The public Cloudflare-served site was still returning an older page while the
GitHub origin had the latest files. The saved Wrangler configuration identifies
a separate Cloudflare Pages project named `adscanvideo`. Inspect that project's
custom domains and deployment after `wrangler login` before changing routing.
The exact active Cloudflare binding remains to be verified after authentication.

Verify both the GitHub origin and the public hostname:

```
gh api repos/timuran1/adscanvideo/pages/builds/latest --jq '.status, .commit'
curl --resolve adscanvideo.com:443:185.199.108.153 https://adscanvideo.com/llms.txt
curl https://adscanvideo.com/llms.txt
```

`_config.yml` excludes backend code from the GitHub Pages site. When deploying to
Cloudflare Pages, stage only public HTML, blog/, privacy/, what-is-adscanvideo/,
sitemap.xml, robots.txt, llms.txt and intentional public assets. Never upload
backend/, .git/, .env, .wrangler/ or server backups. 404.html prevents unknown
paths from silently becoming the homepage.

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

Stripe is disabled until server-only test credentials and a USD $0.50 one-time
Price are configured. The $19/month plan is marked planned, not purchasable.
Signature-verified, idempotent webhook fulfillment is implemented and tested
with synthetic signed events; real Stripe Checkout still needs test-mode
verification when credentials arrive. Add account recovery and refund/dispute
handling before broadly promoting paid usage.
