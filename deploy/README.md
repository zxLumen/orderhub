# Server deployment

Target: a Linux host with Caddy already serving 80/443. This repo runs as a
systemd service; the Windows machine only runs the JD collector agent.

## 1. DNS

Create:

```
orderhub.zxlumen.cn  A  103.205.240.62
```

Wait for propagation; Caddy will obtain the certificate on the first HTTPS
request.

## 2. Install

```bash
sudo bash deploy/install.sh
```

This creates the `orderhub` user, `/opt/orderhub`, `/var/lib/orderhub`, a venv,
and `/etc/systemd/system/orderhub.service`.

## 3. Configure

```bash
sudo -u ubuntu python3 /opt/orderhub/scripts/make_password.py
sudo nano /etc/orderhub.env
```

Set at least:

- `ORDERHUB_TOKEN` (for the collector + Shortcuts)
- `ORDERHUB_COLLECTOR_TOKEN` (can be the same)
- `ORDERHUB_PASSWORD_HASH`
- `ORDERHUB_SESSION_SECRET` (`python -c "import secrets; print(secrets.token_urlsafe(48))"`)
- `ORDERHUB_COOKIE_SECURE=1`
- `ENABLE_JD_COLLECTOR=0`

Then:

```bash
sudo systemctl restart orderhub
sudo systemctl status orderhub
curl -s http://127.0.0.1:8000/api/health
```

## 4. Caddy

Copy `deploy/orderhub.caddy` into the existing Caddy sites directory:

```bash
cp deploy/orderhub.caddy /home/ubuntu/zxLumen-Blog/docker/caddy/sites/orderhub.caddy
docker exec docker-caddy-1 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker exec docker-caddy-1 caddy reload --config /etc/caddy/Caddyfile
```

The app binds `0.0.0.0:8000` so the Caddy container can reach it through
`host.docker.internal`. UFW only opens 22/80/443; port 8000 is not public.

## 5. Backup

```bash
sudo bash deploy/backup.sh
```

Add a cron entry, for example:

```
15 3 * * * /opt/orderhub/deploy/backup.sh >> /var/log/orderhub-backup.log 2>&1
```

## 6. Windows collector

On the Windows machine, `.env`:

```ini
ORDERHUB_SERVER_URL=https://orderhub.zxlumen.cn
ORDERHUB_COLLECTOR_TOKEN=...
ORDERHUB_DATA_DIR=C:\OrderHub\data
ORDERHUB_HOST=127.0.0.1
ENABLE_SCHEDULER=0
ENABLE_JD_COLLECTOR=0
JD_CDP_URL=http://127.0.0.1:9333
```

Then:

```bat
python -m app.collector_agent
```

`scripts\ensure_running.ps1` starts Chrome 9333 and the agent, and no longer
starts a local web server.
