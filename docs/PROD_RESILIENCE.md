# Résilience PROD — redémarrage serveur et application

Dernière mise à jour : **28 mai 2026**.

Comment BettingHUD repart après une coupure électrique, un reboot manuel ou un crash du processus.

**Voir aussi** : [[DEPLOY_SERVEUR]], [[OPS_PROD_DEPANNAGE]].

---

## 1. Deux niveaux distincts

| Niveau | Question | Mécanisme principal |
|--------|----------|---------------------|
| **Machine (VPS)** | Le serveur Ubuntu redémarre-t-il après extinction ? | BIOS / panel hébergeur + boot Linux |
| **Application** | Streamlit / daemon repartent-ils si le process plante ? | **systemd** (`Restart=always`, `enabled`) |

---

## 2. Redémarrage de l’application (déjà en place)

Les services **`bettinghud-dashboard`**, **`bettinghud-daemon`** et **`bettinghud-telegram-bot`** sont gérés par systemd.

Fichiers : `deploy/systemd/bettinghud-dashboard.service`, `bettinghud-daemon.service`, `bettinghud-telegram-bot.service`.

| Directive | Valeur | Effet |
|-----------|--------|--------|
| `Restart=always` | toujours | Si Streamlit ou le daemon s’arrête (crash, OOM, `kill`), systemd le relance |
| `RestartSec=10` / `30` | délai | Pause avant nouvelle tentative (évite le spam) |
| `WantedBy=multi-user.target` | boot | Démarrage automatique quand Ubuntu atteint le mode multi-utilisateur |
| `systemctl enable …` | install | Lien symbolique pour le boot (fait par `install_ubuntu.sh`) |

**nginx** est aussi `enabled` : le proxy HTTP repart au boot.

### Vérifier sur le serveur

```bash
ssh bettinghud

# Doit afficher "enabled" pour les trois
systemctl is-enabled bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot nginx

# État actuel
systemctl status bettinghud-dashboard bettinghud-daemon nginx --no-pager

# Politique de redémarrage
systemctl show bettinghud-dashboard -p Restart,RestartUSec
```

### Après modification des fichiers `.service`

```bash
sudo cp /opt/bettinghud/deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bettinghud-dashboard bettinghud-daemon
sudo systemctl restart bettinghud-dashboard bettinghud-daemon
```

### Simuler un crash applicatif (test)

```bash
# Tuer Streamlit : systemd doit le relancer en ~10 s
sudo kill $(pgrep -f "streamlit run app/dashboard.py")
sleep 15
systemctl status bettinghud-dashboard --no-pager
curl -s http://127.0.0.1:8501/_stcore/health
```

### Logs en cas de crash répété

```bash
sudo journalctl -u bettinghud-dashboard -n 100 --no-pager
sudo journalctl -u bettinghud-daemon -n 100 --no-pager
```

Si le service entre en état **`failed`** (trop d’échecs rapides), voir les erreurs dans le journal puis :

```bash
sudo systemctl reset-failed bettinghud-dashboard
sudo systemctl start bettinghud-dashboard
```

---

## 3. Redémarrage du serveur (après extinction / reboot)

Quand la machine **redémarre** (coupure, `sudo reboot`, mise à jour noyau) :

1. Le firmware / l’hébergeur allume le VPS (voir § 4 si coupure longue).
2. Ubuntu démarre → `multi-user.target`.
3. systemd démarre les unités **`enabled`** : `nginx`, `bettinghud-dashboard`, `bettinghud-daemon`, `cron`.
4. Le **cron** 05:00 UTC relance le pipeline matin le jour suivant (pas besoin que l’app tourne à minuit).

**Ce qui ne repart pas tout seul** : un `rebuild_live_projection.py` interrompu au milieu — il faudra le relancer ou attendre le pipeline matin / le warmup dashboard.

### Test de bout en bout (maintenance planifiée)

```bash
ssh bettinghud
sudo reboot
# Attendre 2–3 min, puis depuis le PC :
curl -s -o /dev/null -w "%{http_code}\n" http://192.95.30.217/
ssh bettinghud "systemctl is-active bettinghud-dashboard bettinghud-daemon nginx"
```

---

## 4. Coupure électrique / serveur éteint longtemps

Le **logiciel** BettingHUD ne peut pas rallumer un VPS éteint : c’est le **fournisseur** (OVH, etc.) ou le **BIOS/IPMI**.

À configurer côté hébergeur (selon offre) :

| Option | Où (typique) |
|--------|----------------|
| **Redémarrage automatique** après retour alimentation | Manager OVH → serveur dédié → IPMI / politique d’alimentation |
| **Monitoring / alerte** | Ping ou sonde HTTP sur `http://192.95.30.217` |
| **Sauvegarde** | Snapshot disque ou backup `bettinghud.db` périodique |

Sans « auto power on », après une coupure il faut **allumer le serveur à la main** depuis le panel — ensuite systemd reprend comme au § 3.

---

## 5. Ce qui tourne au boot (checklist)

| Composant | Unité / fichier | `enabled` ? |
|-----------|-----------------|-------------|
| Dashboard Streamlit | `bettinghud-dashboard.service` | Oui |
| Daemon portefeuille | `bettinghud-daemon.service` | Oui |
| Reverse proxy | `nginx.service` | Oui |
| Pipeline matin | `/etc/cron.d/bettinghud-morning` | Cron système |

Vérifier le cron :

```bash
cat /etc/cron.d/bettinghud-morning
```

---

## 6. Surveillance optionnelle (recommandé)

systemd suffit pour **relancer** les processus ; il n’envoie pas d’e-mail si le site est down.

Options simples :

1. **Sonde externe** (UptimeRobot, Better Stack, etc.) sur `http://192.95.30.217/_stcore/health` ou la page d’accueil.
2. **Script cron local** (ex. toutes les 5 min) :

```bash
#!/bin/bash
# /opt/bettinghud/deploy/healthcheck.sh
curl -sf http://127.0.0.1:8501/_stcore/health >/dev/null || systemctl restart bettinghud-dashboard
```

3. **Alertes journal** : filtrer `Failed` / `OOM` dans `journalctl`.

---

## 7. Résumé

| Scénario | Comportement attendu |
|----------|----------------------|
| Crash Streamlit | Redémarrage auto ~10 s (`Restart=always`) |
| Crash daemon portefeuille | Redémarrage auto ~30 s |
| `sudo reboot` | Services `enabled` → repartent au boot |
| Coupure courant + auto power-on hébergeur | Boot Ubuntu → même chose |
| Serveur resté éteint | Intervention panel hébergeur, puis boot normal |

**Action unique à faire une fois** : confirmer `systemctl is-enabled` = **enabled** pour dashboard, daemon et nginx (déjà le cas si `install_ubuntu.sh` a été exécuté).
