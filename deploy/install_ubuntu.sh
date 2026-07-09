#!/usr/bin/env bash
# Installation BettingHUD sur Ubuntu 24.04 (serveur dédié).
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
APP_DIR="${APP_DIR:-/opt/bettinghud}"
REPO_URL="${REPO_URL:-https://github.com/Miou-ux/BettingHUD.git}"

echo "[1/6] Paquets système…"
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3 python3-venv python3-pip git nginx ufw \
  build-essential libffi-dev libssl-dev curl

sudo ufw allow OpenSSH >/dev/null 2>&1 || true
sudo ufw allow 80/tcp >/dev/null 2>&1 || true
sudo ufw allow 443/tcp >/dev/null 2>&1 || true
sudo ufw --force enable >/dev/null 2>&1 || true

echo "[2/6] Dossier application ${APP_DIR}…"
sudo mkdir -p "${APP_DIR}"
sudo chown -R ubuntu:ubuntu "${APP_DIR}"

if [[ ! -f "${APP_DIR}/app/dashboard.py" ]]; then
  echo "  Clone du dépôt…"
  rm -rf "${APP_DIR:?}"/*
  git clone --depth 1 "${REPO_URL}" "${APP_DIR}"
fi

cd "${APP_DIR}"

echo "[3/6] Environnement Python…"
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "[4/6] Playwright (Chromium)…"
playwright install chromium
sudo "${APP_DIR}/venv/bin/playwright" install-deps chromium 2>/dev/null \
  || sudo playwright install-deps chromium 2>/dev/null \
  || true

mkdir -p data/cache data/logs models

echo "[5/6] Services systemd…"
if [[ -d "${APP_DIR}/deploy/systemd" ]]; then
  for unit in bettinghud-dashboard.service bettinghud-daemon.service bettinghud-telegram-bot.service; do
    sudo cp "${APP_DIR}/deploy/systemd/${unit}" "/etc/systemd/system/${unit}"
  done
  sudo systemctl daemon-reload
  sudo systemctl enable bettinghud-dashboard.service bettinghud-daemon.service bettinghud-telegram-bot.service
fi

echo "[6/6] Nginx…"
if [[ -f "${APP_DIR}/deploy/nginx/bettinghud.conf" ]]; then
  sudo cp "${APP_DIR}/deploy/nginx/bettinghud.conf" /etc/nginx/sites-available/bettinghud
  sudo ln -sf /etc/nginx/sites-available/bettinghud /etc/nginx/sites-enabled/bettinghud
  sudo rm -f /etc/nginx/sites-enabled/default
  sudo nginx -t
  sudo systemctl enable nginx
  sudo systemctl restart nginx
fi

echo "[7/7] Crons PROD (deploy/cron/*)…"
if [[ -d "${APP_DIR}/deploy/cron" ]]; then
  for f in "${APP_DIR}"/deploy/cron/*; do
    [[ -f "${f}" ]] || continue
  base="$(basename "${f}")"
  sudo cp "${f}" "/etc/cron.d/bettinghud-${base}"
  sudo sed -i 's/\r$//' "/etc/cron.d/bettinghud-${base}"
  sudo chmod 644 "/etc/cron.d/bettinghud-${base}"
  done
fi

echo "=== Installation terminée ==="
echo "Prochaine étape : copier data/ et models/ depuis ton PC, puis :"
echo "  sudo systemctl start bettinghud-dashboard bettinghud-daemon"
