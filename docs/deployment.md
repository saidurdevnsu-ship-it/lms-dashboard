# Deployment

Production target: a Linux server (Debian/Ubuntu) behind nginx, running under gunicorn, with MySQL 8.4.

## Environment variables (required in production)

| Variable | Example / notes |
|---|---|
| `SECRET_KEY` | Long random string — no `django-insecure-` prefix |
| `DJANGO_DEBUG` | `false` |
| `SITE_URL` | `https://pulse.sts.edu.bd` — must start with `https://` for secure-cookie and HSTS checks |
| `ALLOWED_HOSTS` | `pulse.sts.edu.bd` |
| `DB_NAME` | MySQL database name (triggers MySQL backend; unset = SQLite) |
| `DB_USER` | MySQL user |
| `DB_PASSWORD` | MySQL password |
| `DB_HOST` | `127.0.0.1` or socket path |
| `DB_PORT` | `3306` |
| `AI_PROVIDER` | `anthropic` or `gemini` |
| `AI_MODEL` | e.g. `claude-sonnet-4-5` |
| `AI_FINAL_MODEL` | e.g. `claude-opus-4` (used for final paper generation) |
| `ANTHROPIC_API_KEY` | Anthropic key (if provider = anthropic) |
| `GEMINI_API_KEY` | Gemini key (if provider = gemini) |
| `GEMINI_PAID_TIER` | `true` if on a paid Gemini plan |
| `EMAIL_HOST` | SMTP server |
| `EMAIL_PORT` | `587` |
| `EMAIL_HOST_USER` | SMTP user |
| `EMAIL_HOST_PASSWORD` | SMTP password |
| `EMAIL_USE_TLS` | `true` |
| `DEFAULT_FROM_EMAIL` | `STS Pulse <noreply@sts.edu.bd>` |
| `MAKE_WEBHOOK_URL` | Make.com scenario webhook URL (leave blank to disable) |
| `MAKE_MAILHOOK_ADDRESS` | Make.com inbound email address (leave blank to disable) |

## Pre-flight check

```bash
python manage.py check --deploy --fail-level WARNING
```

This must exit 0. Known warnings that must all be absent in production: `W004` (HSTS), `W008` (SSL redirect), `W012`/`W016` (secure cookies), `W018` (DEBUG). See SR-01 in `docs/security-review-2026-11-02.md`.

## First deploy / fresh server

```bash
# 1. Install system packages
apt install python3 python3-venv default-libmysqlclient-dev build-essential pkg-config nginx

# 2. Clone repo, create venv
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 3. Set env vars in /etc/environment or a .env loaded by gunicorn's service unit

# 4. Load MySQL timezone tables (needed for date-based admin filters)
#    Download timezone_posix.sql from https://dev.mysql.com/downloads/timezones.html
mysql -u root mysql < timezone_posix.sql
#    Verify: SELECT CONVERT_TZ(NOW(), 'UTC', 'Asia/Dhaka') IS NOT NULL;

# 5. Collect static
python manage.py collectstatic --noinput

# 6. Migrate (back up first if upgrading)
python manage.py migrate

# 7. Create superuser
python manage.py createsuperuser
```

## Upgrading (each release)

```bash
# Back up the database before every migrate (see database-design.md § Backups)
mysqldump --single-transaction lms > backups/lms-before-<tag>-$(date +%F).sql

git pull
venv/bin/pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate
# Restart gunicorn
systemctl restart gunicorn
```

## nginx config (outline)

```nginx
server {
    listen 443 ssl http2;
    server_name pulse.sts.edu.bd;

    location /static/ { alias /srv/pulse/staticfiles/; }
    location /media/  {
        # Django view sets X-Accel-Redirect; nginx serves the file.
        # The /protected/ path must NOT be accessible directly.
        internal;
        alias /srv/pulse/media/;
    }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 180s;  # allow for AI generation polling
    }
}
```

`SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` in settings requires that nginx always sets `X-Forwarded-Proto` and that clients cannot reach gunicorn directly.

## gunicorn (systemd unit outline)

```ini
[Service]
WorkingDirectory=/srv/pulse
ExecStart=/srv/pulse/venv/bin/gunicorn config.wsgi:application \
    --workers 2 --timeout 120 --bind 127.0.0.1:8000
EnvironmentFile=/etc/pulse.env
Restart=on-failure
```

Two workers is safe with `threading.Thread` for AI generation (see ADR-002). Add more workers only after moving generation to a task queue.

## Backups

See `docs/database-design.md` § Backups for the full procedure. Run before every `migrate`.
