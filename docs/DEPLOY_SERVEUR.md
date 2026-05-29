# Déploiement serveur dédié (Ubuntu)

Dernière mise à jour : **28 mai 2026**.

Guide pour héberger BettingHUD sur un VPS Ubuntu — environnement **PROD** (production actuelle : **192.95.30.217**).

Le PC de développement est **PREPROD** : voir **`docs/ENVIRONNEMENTS.md`**.

> **Données** : `data/` et `models/` ne sont **pas** sur GitHub. Après un `git clone`, il faut copier la base SQLite et le bundle ML depuis la machine de dev.

---

## 1. Prérequis serveur

| Élément | Recommandation |
|---------|----------------|
| OS | Ubuntu **22.04** ou **24.04** LTS |
| RAM | **8 Go** minimum (Playwright + XGBoost) |
| Disque | **50 Go**+ (base + caches + Playwright) |
| Ports | **22** (SSH), **80** (HTTP), **443** (HTTPS optionnel) |

---

## 2. Accès SSH (clé recommandée)

### Sur Windows (une fois)

```powershell
ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\bettinghud_server -N '""' -C "bettinghud-deploy"
```

Ajouter la clé publique sur le serveur (`~/.ssh/authorized_keys`).

Fichier `~/.ssh/config` :

```
Host bettinghud
    HostName 192.95.30.217
    User ubuntu
    IdentityFile ~/.ssh/bettinghud_server
    IdentitiesOnly yes
```

Connexion : `ssh bettinghud`

---

## 3. Installation automatique

Depuis le dépôt cloné sur le serveur :

```bash
cd /opt/bettinghud
chmod +x deploy/install_ubuntu.sh
bash deploy/install_ubuntu.sh
```

Le script :

1. installe Python, git, nginx, ufw ;
2. clone GitHub dans `/opt/bettinghud` (si vide) ;
3. crée le venv et `pip install -r requirements.txt` ;
4. installe Playwright Chromium + dépendances système ;
5. active les services **systemd** et **nginx**.

Toujours réinstaller les deps après un `git pull` si `requirements.txt` a changé :

```bash
/opt/bettinghud/venv/bin/pip install -r requirements.txt
```

Packages critiques pour PROD (normalement dans `requirements.txt`) : `matplotlib`, `beautifulsoup4`, `lxml`, `streamlit-autorefresh`, `sqlalchemy`.

---

## 4. Copier données et modèle (obligatoire)

Depuis le PC de dev (PowerShell) :

```powershell
scp O:\Miouppy\Documents\BettingHUD\data\bettinghud.db bettinghud:/opt/bettinghud/data/bettinghud.db
scp O:\Miouppy\Documents\BettingHUD\models\xgb_model_tml_v47.pkl bettinghud:/opt/bettinghud/models/xgb_model_tml_v47.pkl
```

Optionnel (snapshot du jour déjà prêt) :

```powershell
scp O:\Miouppy\Documents\BettingHUD\data\cache\live_matches_snapshot.full.joblib bettinghud:/opt/bettinghud/data/cache/
```

Sinon, reconstruire sur le serveur :

```bash
ssh bettinghud
cd /opt/bettinghud
./venv/bin/python scripts/rebuild_live_projection.py
```

---

## 5. Services systemd

| Service | Rôle | Commande équivalente |
|---------|------|----------------------|
| `bettinghud-dashboard` | Streamlit sur `127.0.0.1:8501` | `streamlit run app/dashboard.py` |
| `bettinghud-daemon` | Sync résultats + top 15 probas/jour | `python scripts/portfolio_results_daemon.py` |
| `bettinghud-telegram-bot` | Commandes Telegram `/jour`, `/top5` | `python scripts/telegram_bot_daemon.py` |

Fichiers unit : `deploy/systemd/`.

Variables **dashboard** (voir `bettinghud-dashboard.service`) :

| Variable | Valeur PROD | Note |
|----------|-------------|------|
| `BETTINGHUD_ENV` | `prod` | Bandeau PROD dans l’UI |
| `BETTINGHUD_LIVE_DATA_DAEMON` | `1` | Sync live en arrière-plan |
| `BETTINGHUD_AUTO_SYNC_TOURS` | `1` | Sync tours automatique |
| `BETTINGHUD_HEADLESS` | **absent** | **Ne pas définir** sur le dashboard (désactive tous les onglets) |

Streamlit est lancé avec `--server.headless=true` (pas de navigateur sur le VPS) — c’est **distinct** de `BETTINGHUD_HEADLESS`.

```bash
sudo cp /opt/bettinghud/deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot
sudo systemctl start bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot
sudo systemctl status bettinghud-dashboard
```

Logs :

```bash
sudo journalctl -u bettinghud-dashboard -f
sudo journalctl -u bettinghud-daemon -f
```

---

## 6. Nginx (accès web par IP)

Config : `deploy/nginx/bettinghud.conf` — proxy vers Streamlit **avec support WebSocket** (`map $http_upgrade`, `proxy_buffering off`, timeouts longs).

- URL sans domaine : **http://192.95.30.217**
- Streamlit n’est **pas** exposé directement (écoute localhost uniquement).

```bash
sudo cp /opt/bettinghud/deploy/nginx/bettinghud.conf /etc/nginx/sites-available/bettinghud
sudo ln -sf /etc/nginx/sites-available/bettinghud /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Copier aussi **`.streamlit/config.toml`** sur le serveur (thème + `enableCORS` / `enableXsrfProtection` adaptés au reverse proxy) :

```powershell
scp O:\Miouppy\Documents\BettingHUD\.streamlit\config.toml bettinghud:/opt/bettinghud/.streamlit/config.toml
```

### HTTPS sans nom de domaine

Let’s Encrypt exige en général un nom de domaine. Alternatives :

- sous-domaine gratuit type `192-95-30-217.nip.io` pointant vers l’IP ;
- certificat auto-signé (avertissement navigateur) ;
- VPN / tunnel SSH si usage strictement privé.

---

## 7. Pipeline matin (cron) + Telegram

Fichier : `deploy/cron/morning-pipeline` → `/etc/cron.d/bettinghud-morning`

- **05:00 UTC** chaque jour : `scripts/morning_live_pipeline.py`
- Logs : `data/logs/morning_pipeline_cron.log`
- Si `TELEGRAM_TOP5_AFTER_MORNING=1` dans `/opt/bettinghud/.env` → envoi **Top 5 proba** en fin de pipeline

Sur Windows, équivalent : `scripts/register_morning_task.ps1` (tâche planifiée 07:00 locale).

### Bot Telegram (commandes `/jour`, `/top5`)

Documentation complète : **`docs/TELEGRAM_TOP5.md`**.

1. Créer le bot (@BotFather), obtenir `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID`
2. Fichier **`/opt/bettinghud/.env`** (permissions `600`, jamais commité) :

```env
BETTINGHUD_ENV=prod
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TELEGRAM_TOP5_AFTER_MORNING=1
```

3. Activer le service :

```bash
sudo cp /opt/bettinghud/deploy/systemd/bettinghud-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bettinghud-telegram-bot
```

Logs : `data/logs/telegram_bot_daemon.log`

---

## 8. Mise à jour après un push GitHub

```bash
ssh bettinghud
cd /opt/bettinghud
git pull
./venv/bin/pip install -r requirements.txt   # si requirements changé
sudo systemctl restart bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot
```

Si le modèle ou la base locale a changé, recopier `data/bettinghud.db` et/ou `models/*.pkl` puis rebuild snapshot si besoin.

---

## 9. Redémarrage automatique (serveur et application)

Guide détaillé : **`docs/PROD_RESILIENCE.md`**.

| Besoin | Solution |
|--------|----------|
| L’app (Streamlit / daemon) plante | systemd `Restart=always` sur `bettinghud-dashboard` et `bettinghud-daemon` |
| Le serveur reboot | `systemctl enable` → services démarrent au boot |
| Coupure électrique longue | Réglage **auto power-on** chez l’hébergeur + même boot systemd |

Vérification rapide :

```bash
systemctl is-enabled bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot nginx
systemctl show bettinghud-dashboard -p Restart
```

Test crash : `sudo kill $(pgrep -f "streamlit run app/dashboard")` puis attendre 15 s et `curl http://127.0.0.1:8501/_stcore/health`.

---

## 10. Sécurité (à faire)

L’installation par défaut **n’ajoute pas d’authentification** sur l’URL publique.

Recommandations :

1. **Pare-feu** : limiter l’accès HTTP à ton IP (`sudo ufw` / règles provider).
2. **Basic auth Nginx** : `htpasswd` + directive `auth_basic` dans `bettinghud.conf`.
3. Ne pas committer `.env`, clés API, mots de passe.

---

## 11. Dépannage rapide

Guide détaillé (incidents mai 2026, variables, checklist) : **`docs/OPS_PROD_DEPANNAGE.md`**.

| Symptôme | Piste |
|----------|--------|
| Écran noir / vide | `journalctl -u bettinghud-dashboard` (ModuleNotFound) ; nginx WebSocket (§ 6) ; attendre chargement ML |
| Seulement « Prêt. » + ligne modèle | `BETTINGHUD_HEADLESS=1` sur le service dashboard → retirer et `systemctl restart` |
| Page blanche / 502 | `sudo systemctl status bettinghud-dashboard` ; `curl http://127.0.0.1:8501/_stcore/health` |
| Données PREPROD pas sur PROD | **Normal** — pas de sync auto ; paris réels à saisir en PROD |
| Daemon en échec | `journalctl -u bettinghud-daemon` ; `PYTHONPATH` + chemin script (pas `-m scripts`) |
| Pas de matchs live | `rebuild_live_projection.py` ; scrape prematch dans `data/scraped/` |
| WTA datée avril | `ingest_sackmann_wta.py` (`sqlalchemy` requis) |
| Module manquant | `./venv/bin/pip install -r requirements.txt` |

---

## 12. Arborescence production

```
/opt/bettinghud/
├── app/dashboard.py
├── scripts/
├── data/bettinghud.db          # copié manuellement
├── models/xgb_model_tml_v47.pkl
├── data/cache/                 # snapshots live
├── venv/
└── deploy/                     # scripts d’install
```
