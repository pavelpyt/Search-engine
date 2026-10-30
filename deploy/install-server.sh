#!/usr/bin/env bash
# Runs on the VM as root, from deploy/deploy.sh. Secrets stay in /etc/research-agents.env.
set -euo pipefail
ARCHIVE=$1
APP=/opt/research-agents
VENV=/opt/research-agents-venv  # outside $APP: a venv breaks when moved, its scripts hardcode the path
HOST=demo.pavelpytlik.cz

[ -f /etc/research-agents.env ] || { echo "/etc/research-agents.env is missing" >&2; exit 1; }
python3 -c 'import ensurepip' 2>/dev/null || apt-get install -y -qq python3-venv
id research >/dev/null 2>&1 || useradd --system --home-dir /var/lib/research-agents --shell /usr/sbin/nologin research

rm -rf "$APP.new" && mkdir -p "$APP.new"
tar -xzf "$ARCHIVE" -C "$APP.new" --no-same-owner --warning=no-unknown-keyword
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --disable-pip-version-check -r "$APP.new/requirements.txt"
rm -rf "$APP.old"
[ -d "$APP" ] && mv "$APP" "$APP.old"
mv "$APP.new" "$APP"

install -m 644 "$APP/deploy/research-agents.service" /etc/systemd/system/research-agents.service
systemctl daemon-reload
systemctl enable --quiet research-agents
systemctl restart research-agents

if [ ! -d "/etc/letsencrypt/live/$HOST" ]; then
  sed '/^# --- https ---/,$d' "$APP/deploy/nginx-research-agents.conf" > /etc/nginx/sites-available/research-agents
  ln -sf /etc/nginx/sites-available/research-agents /etc/nginx/sites-enabled/research-agents
  nginx -t -q && systemctl reload nginx
  certbot certonly --webroot -w /var/www/letsencrypt -d "$HOST" --non-interactive --agree-tos \
    --register-unsafely-without-email --deploy-hook "systemctl reload nginx"
fi
install -m 644 "$APP/deploy/nginx-research-agents.conf" /etc/nginx/sites-available/research-agents
ln -sf /etc/nginx/sites-available/research-agents /etc/nginx/sites-enabled/research-agents
nginx -t -q && systemctl reload nginx
