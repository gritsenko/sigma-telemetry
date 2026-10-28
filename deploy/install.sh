#!/usr/bin/env bash
# Native install for a Debian/Ubuntu LXC container (no Docker): systemd service + system Caddy.
# Run as root from the cloned repo, which must live at /opt/sigma-telemetry:
#   git clone https://github.com/gritsenko/sigma-telemetry.git /opt/sigma-telemetry
#   /opt/sigma-telemetry/deploy/install.sh
# Safe to rerun: also the update path after `git pull`.
set -euo pipefail

APP=/opt/sigma-telemetry
ENV_FILE=/etc/sigma-telemetry.env
DOMAIN=${DOMAIN:-sigmastat.gritsenko.biz}

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[ "$(cd "$(dirname "$0")/.." && pwd)" = "$APP" ] || { echo "clone the repo to $APP" >&2; exit 1; }

apt-get update -q
apt-get install -y -q python3 python3-venv caddy sqlite3

id sigmastat >/dev/null 2>&1 || useradd --system --home /var/lib/sigma-telemetry --shell /usr/sbin/nologin sigmastat
install -d -o sigmastat -g sigmastat -m 750 /var/lib/sigma-telemetry

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q -r "$APP/server/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
  cat >"$ENV_FILE" <<EOF
ALLOWED_ORIGINS=https://sigma.gritsenko.biz,http://localhost:5380
STATS_TOKEN=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
RETENTION_DAYS=365
EOF
  chmod 640 "$ENV_FILE"
  chgrp sigmastat "$ENV_FILE"
  echo "created $ENV_FILE (STATS_TOKEN inside)"
fi

install -m 644 "$APP/deploy/sigma-telemetry.service" /etc/systemd/system/sigma-telemetry.service
systemctl daemon-reload
systemctl enable sigma-telemetry
systemctl restart sigma-telemetry

if ! grep -q "^$DOMAIN" /etc/caddy/Caddyfile 2>/dev/null; then
  # The Debian package ships a :80 demo site; replace it with ours on first install.
  grep -q "^:80" /etc/caddy/Caddyfile 2>/dev/null && : >/etc/caddy/Caddyfile
  cat >>/etc/caddy/Caddyfile <<EOF

$DOMAIN {
	encode gzip
	request_body {
		max_size 32KB
	}
	reverse_proxy 127.0.0.1:8000
}
EOF
fi
systemctl enable caddy
systemctl reload-or-restart caddy

sleep 1
curl -fsS http://127.0.0.1:8000/health && echo " — app up; check https://$DOMAIN/health"
