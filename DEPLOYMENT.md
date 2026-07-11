# AdScanVideo deployment

## Frontend

The public website is a static HTML site hosted by GitHub Pages.

- Repository: `timuran1/adscanvideo`
- Production branch: `main`
- Custom domain: `adscanvideo.com`
- `CNAME` must continue to contain `adscanvideo.com`
- A push to `main` triggers the GitHub Pages deployment automatically

The site does not require an npm build. All CSS and JavaScript are contained in `index.html`; blog posts remain under `blog/`.

## Recommended API architecture

GitHub Pages cannot proxy `/api/*` to a Node.js server. Run the Express API on the VPS and expose it as:

`https://api.adscanvideo.com/api`

Then change this line in the `<head>` of `index.html`:

```html
<meta name="adscan-api-base" content="https://api.adscanvideo.com/api">
```

### Netlify DNS

Netlify is only managing DNS in the current architecture. Add:

| Host | Type | Value |
| --- | --- | --- |
| `api` | `A` | `YOUR_VPS_PUBLIC_IP` |

Keep the existing GitHub Pages apex and `www` records unchanged.

## VPS setup

These commands assume Ubuntu, a Node.js API project, and an Express entry file such as `server.js`.

```bash
sudo mkdir -p /opt/adscanvideo-api
sudo chown -R "$USER":"$USER" /opt/adscanvideo-api
cd /opt/adscanvideo-api
# Copy or clone the backend source here.
npm ci --omit=dev
```

Create `/etc/systemd/system/adscanvideo-api.service`:

```ini
[Unit]
Description=AdScanVideo API
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/adscanvideo-api
EnvironmentFile=/opt/adscanvideo-api/.env
ExecStart=/usr/bin/node server.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Start it:

```bash
sudo chown -R www-data:www-data /opt/adscanvideo-api
sudo systemctl daemon-reload
sudo systemctl enable --now adscanvideo-api
sudo systemctl status adscanvideo-api
```

The Express server should listen on `127.0.0.1:3000`, not directly on the public interface.

## Nginx reverse proxy

Create `/etc/nginx/sites-available/adscanvideo-api`:

```nginx
server {
    listen 80;
    server_name api.adscanvideo.com;

    client_max_body_size 200M;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;

    location /api/ {
        proxy_pass http://127.0.0.1:3000/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable Nginx and HTTPS:

```bash
sudo ln -s /etc/nginx/sites-available/adscanvideo-api /etc/nginx/sites-enabled/adscanvideo-api
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d api.adscanvideo.com
```

## Express requirements

Allow only the production and GitHub Pages frontend origins:

```js
import cors from 'cors';

app.use(cors({
  origin: [
    'https://adscanvideo.com',
    'https://www.adscanvideo.com',
    'https://timuran1.github.io',
  ],
  methods: ['GET', 'POST'],
}));
```

Keep API keys in `/opt/adscanvideo-api/.env`. Never commit that file to the public frontend repository.

## Deployment order

1. Deploy and test the backend locally on the VPS.
2. Add the `api` DNS record in Netlify DNS.
3. Configure Nginx and obtain the SSL certificate.
4. Test `https://api.adscanvideo.com/api/status/test`.
5. Update the `adscan-api-base` meta tag in `index.html`.
6. Push the frontend change to `main`.

## Google Analytics

The numeric GA4 property ID is not enough for the website tag. Add the GA4 Measurement ID in the form `G-XXXXXXXXXX` before enabling analytics in `index.html`.
