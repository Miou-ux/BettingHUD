#!/usr/bin/env bash
# Déploie les scripts/crons P0 ops sur PROD (SSH alias bettinghud).
set -euo pipefail

HOST="${1:-bettinghud}"
APP="/opt/bettinghud"

echo "=== Deploy P0 ops -> ${HOST} ==="

FILES=(
  scripts/ops_telegram_alert.py
  scripts/cron_run_with_alert.py
  scripts/post_ml_train_hook.py
  scripts/backup_prod_db_server.py
  scripts/prod_health_watchdog.py
  scripts/update_model_tml.py
)

for f in "${FILES[@]}"; do
  echo "scp ${f}"
  scp "${f}" "${HOST}:${APP}/${f}"
done

echo "scp deploy/cron/*"
ssh "${HOST}" "mkdir -p /tmp/bettinghud-cron-p0"
scp deploy/cron/morning-pipeline deploy/cron/data-sync deploy/cron/ops-p0 \
  "${HOST}:/tmp/bettinghud-cron-p0/"

scp deploy/sudoers/bettinghud-ops "${HOST}:/tmp/bettinghud-ops.sudoers"

ssh "${HOST}" bash -s <<'REMOTE'
set -euo pipefail
APP=/opt/bettinghud
  sudo cp /tmp/bettinghud-cron-p0/morning-pipeline /etc/cron.d/bettinghud-morning-pipeline
  sudo cp /tmp/bettinghud-cron-p0/data-sync /etc/cron.d/bettinghud-data-sync
  sudo cp /tmp/bettinghud-cron-p0/ops-p0 /etc/cron.d/bettinghud-ops-p0
  sudo rm -f /etc/cron.d/bettinghud-morning
for f in /etc/cron.d/bettinghud-morning-pipeline /etc/cron.d/bettinghud-data-sync /etc/cron.d/bettinghud-ops-p0; do
  sudo sed -i 's/\r$//' "$f"
  sudo chmod 644 "$f"
done
sudo cp /tmp/bettinghud-ops.sudoers /etc/sudoers.d/bettinghud-ops
sudo chmod 440 /etc/sudoers.d/bettinghud-ops
sudo visudo -c
mkdir -p "$APP/backups/prod"
cd "$APP"
echo "--- Test ops alert (dry-run) ---"
venv/bin/python scripts/ops_telegram_alert.py "P0 deploy test" "cron+watchdog+backup installés" --dry-run
echo "--- Test health watchdog ---"
venv/bin/python scripts/prod_health_watchdog.py || true
echo "--- Test backup serveur ---"
venv/bin/python scripts/backup_prod_db_server.py
echo "--- Services ---"
systemctl is-active bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot
REMOTE

echo "=== Deploy P0 terminé ==="
