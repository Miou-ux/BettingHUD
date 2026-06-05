#!/usr/bin/env bash
# Active admin.courtalpha.tech (BettingHUD Streamlit) — à lancer sur PROD ou via ssh.
#
# Prérequis chez le registrar DNS :
#   admin.courtalpha.tech  A  192.95.30.217
#
# Usage :
#   bash deploy/nginx/setup_admin_subdomain.sh
#   ssh bettinghud 'bash /opt/bettinghud/deploy/nginx/setup_admin_subdomain.sh'
set -euo pipefail

ADMIN_HOST="${ADMIN_HOST:-admin.courtalpha.tech}"
SERVER_IP="${SERVER_IP:-192.95.30.217}"
NGINX_SRC="${NGINX_SRC:-/opt/bettinghud/deploy/nginx/bettinghud.prod.conf}"
NGINX_DST="${NGINX_DST:-/etc/nginx/sites-available/bettinghud}"

echo "==> Vérification DNS ${ADMIN_HOST} -> ${SERVER_IP}"
RESOLVED="$(getent ahostsv4 "${ADMIN_HOST}" 2>/dev/null | awk '{print $1; exit}' || true)"
if [[ -z "${RESOLVED}" ]]; then
  RESOLVED="$(dig +short A "${ADMIN_HOST}" 2>/dev/null | head -1 || true)"
fi
if [[ "${RESOLVED}" != "${SERVER_IP}" ]]; then
  echo "ERREUR: enregistrement DNS manquant ou incorrect (résolu: ${RESOLVED:-aucun})."
  echo "Ajoutez chez votre registrar :  ${ADMIN_HOST}  A  ${SERVER_IP}"
  exit 1
fi
echo "DNS OK (${RESOLVED})"

echo "==> Certificat Let's Encrypt (extension SAN admin)"
if ! sudo certbot certonly --nginx \
  -d courtalpha.tech -d www.courtalpha.tech -d "${ADMIN_HOST}" \
  --expand --non-interactive --agree-tos --keep-until-expiring; then
  echo "certbot --expand a échoué, tentative certificat dédié..."
  sudo certbot certonly --nginx -d "${ADMIN_HOST}" --non-interactive --agree-tos
fi

echo "==> Nginx"
sudo cp "${NGINX_SRC}" "${NGINX_DST}"
sudo ln -sf "${NGINX_DST}" /etc/nginx/sites-enabled/bettinghud
sudo nginx -t
sudo systemctl reload nginx

echo "==> Vérifications"
curl -sf -o /dev/null -w "admin_https=%{http_code}\n" "https://${ADMIN_HOST}/"
curl -sf -o /dev/null -w "legacy_8502=%{http_code}\n" "https://courtalpha.tech:8502/" || true

echo "OK — BettingHUD : https://${ADMIN_HOST}/"
echo "Pensez à mettre à jour BETTINGHUD_WEB_BASE_URL=https://${ADMIN_HOST} dans /opt/bettinghud/.env"
