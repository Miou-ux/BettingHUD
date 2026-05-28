# Déploiement serveur dédié (Ubuntu)

Dernière mise à jour : **28 mai 2026**.

Guide pour héberger BettingHUD sur un VPS Ubuntu (production actuelle : **192.95.30.217**).

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

Dépendance UI supplémentaire (si absente du `requirements.txt`) :

```bash
/opt/bettinghud/venv/bin/pip install streamlit-autorefresh
```

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

Fichiers unit : `deploy/systemd/`.

```bash
sudo systemctl enable bettinghud-dashboard bettinghud-daemon
sudo systemctl start bettinghud-dashboard bettinghud-daemon
sudo systemctl status bettinghud-dashboard
```

Logs :

```bash
sudo journalctl -u bettinghud-dashboard -f
sudo journalctl -u bettinghud-daemon -f
```

---

## 6. Nginx (accès web par IP)

Config : `deploy/nginx/bettinghud.conf` — proxy vers Streamlit.

- URL sans domaine : **http://192.95.30.217**
- Streamlit n’est **pas** exposé directement (écoute localhost uniquement).

```bash
sudo nginx -t
sudo systemctl restart nginx
```

### HTTPS sans nom de domaine

Let’s Encrypt exige en général un nom de domaine. Alternatives :

- sous-domaine gratuit type `192-95-30-217.nip.io` pointant vers l’IP ;
- certificat auto-signé (avertissement navigateur) ;
- VPN / tunnel SSH si usage strictement privé.

---

## 7. Pipeline matin (cron)

Fichier : `deploy/cron/morning-pipeline` → `/etc/cron.d/bettinghud-morning`

- **05:00 UTC** chaque jour : `scripts/morning_live_pipeline.py`
- Logs : `data/logs/morning_pipeline_cron.log`

Sur Windows, équivalent : `scripts/register_morning_task.ps1` (tâche planifiée 07:00 locale).

---

## 8. Mise à jour après un push GitHub

```bash
ssh bettinghud
cd /opt/bettinghud
git pull
./venv/bin/pip install -r requirements.txt   # si requirements changé
sudo systemctl restart bettinghud-dashboard bettinghud-daemon
```

Si le modèle ou la base locale a changé, recopier `data/bettinghud.db` et/ou `models/*.pkl` puis rebuild snapshot si besoin.

---

## 9. Sécurité (à faire)

L’installation par défaut **n’ajoute pas d’authentification** sur l’URL publique.

Recommandations :

1. **Pare-feu** : limiter l’accès HTTP à ton IP (`sudo ufw` / règles provider).
2. **Basic auth Nginx** : `htpasswd` + directive `auth_basic` dans `bettinghud.conf`.
3. Ne pas committer `.env`, clés API, mots de passe.

---

## 10. Dépannage rapide

| Symptôme | Piste |
|----------|--------|
| Page blanche / 502 | `sudo systemctl status bettinghud-dashboard` ; `curl http://127.0.0.1:8501` sur le serveur |
| Daemon en échec | `journalctl -u bettinghud-daemon` ; vérifier `scripts/portfolio_results_daemon.py` présent après `git pull` |
| Pas de matchs live | Lancer `rebuild_live_projection.py` ; vérifier scrape prematch dans `data/scraped/` |
| WTA datée avril | `python scripts/sync_tours_daily.py` puis `ingest_sackmann_wta.py` (sqlalchemy requis) |
| Module manquant | `pip install` dans `/opt/bettinghud/venv` |

---

## 11. Arborescence production

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
