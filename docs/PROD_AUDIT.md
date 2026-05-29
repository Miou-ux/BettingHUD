# Audit PROD — résilience & sécurité

Audit du **28 mai 2026** sur le serveur **192.95.30.217** (`bettinghud`).  
Checklist d’amélioration — pas un état garanti dans le temps ; revoir après chaque changement infra.

**Voir aussi** : [[PROD_RESILIENCE]], [[OPS_PROD_DEPANNAGE]], [[DEPLOY_SERVEUR]].

---

## Synthèse

| Domaine | Niveau actuel | Priorité |
|---------|---------------|----------|
| Redémarrage app (systemd) | Bon | — |
| Redémarrage serveur (boot) | Bon si `enabled` | Vérifier auto power-on OVH |
| Sauvegardes `bettinghud.db` | Absent | **Haute** |
| Exposition web | HTTP public, sans auth | **Haute** |
| SSH | Clé OK, mots de passe encore possibles | **Moyenne** |
| Durcissement réseau | UFW actif, ports larges | **Moyenne** |
| Hygiène déploiement | Modifications Git locales sur `/opt` | **Moyenne** |
| Monitoring / alertes | Aucune sonde documentée | **Moyenne** |
| Co-hébergement | Freqtrade sur même machine | **Basse** (isolation) |

---

## Ce qui est déjà bien

- **systemd** : `bettinghud-dashboard`, `bettinghud-daemon`, `nginx` → `enabled` + `Restart=always`.
- **Streamlit** écoute uniquement **127.0.0.1:8501** (pas d’exposition directe).
- **UFW** actif, politique entrante deny par défaut.
- **unattended-upgrades** actif (patchs sécurité Ubuntu).
- **Ressources** : ~31 Go RAM, disque ~8 % — marge confortable.
- **Cron matin** : pipeline 05:00 UTC installé.
- **Snapshot live** : présent après rebuild (à maintenir quotidiennement).

---

## Résilience — améliorations recommandées

### Priorité haute

1. **Sauvegardes automatiques de `data/bettinghud.db`**
   - Aucun répertoire `backups/` détecté.
   - **Mise en place (PC local)** : `scripts\backup_prod_db_to_local.ps1` + tâche planifiée `scripts\register_prod_backup_task.ps1` (dossier `backups/prod/`, rétention 30 j).
   - Option complémentaire : cron sur le serveur + copie off-site (S3, autre VPS).

2. **Sonde de disponibilité externe**
   - systemd relance les processus mais n’alerte pas.
   - URL : `http://192.95.30.217/_stcore/health` ou page d’accueil.
   - Option : cron local + `systemctl restart` si health KO (filet secondaire).

3. **Documenter / activer auto power-on OVH**
   - Après coupure longue, le panel doit rallumer le serveur (hors scope app).

### Priorité moyenne

4. **Ne plus modifier le code sur `/opt/bettinghud`**
   - État observé : `git status` avec fichiers modifiés, branche behind `origin/main`.
   - Workflow : uniquement `git pull` après push PREPROD ; pas de `scp` de `app/` sauf urgence documentée.

5. **Redémarrer le daemon après `pip install`**
   - Log passé : `No module named 'matplotlib'` sur `portfolio_results_daemon` (top 15 probas ignoré).
   - Après mise à jour deps : `sudo systemctl restart bettinghud-daemon`.

6. **Healthcheck post-reboot**
   - Script ou checklist : health Streamlit + présence snapshot + dernier scrape CSV.

7. **Limite mémoire systemd (optionnel)**
   - Pic dashboard ~3,8 Go RAM observé pendant rebuild.
   - `MemoryMax=` / `MemoryHigh=` pour éviter OOM qui tuerait d’autres services (ex. Freqtrade).

### Priorité basse

8. **Serveur partagé avec Freqtrade**
   - Autre service actif (`freqtrade` sur `127.0.0.1:8080`).
   - Risque : charge CPU/RAM concurrente lors des scrapes Playwright.
   - Idéal à terme : VPS dédié BettingHUD ou cgroups / priorités CPU.

9. **Rotation des logs**
   - `data/logs` encore petit ; prévoir `logrotate` si croissance.

---

## Sécurité — améliorations recommandées

### Priorité haute

1. **Authentification devant le dashboard**
   - Site **public en HTTP** sans `auth_basic` nginx.
   - Toute personne connaissant l’IP voit paris, BR, stratégie.
   - Options :
     - **Basic auth nginx** (rapide, `htpasswd`).
     - **VPN** (WireGuard) + UFW : n’autoriser le port 80 que depuis ton IP ou le réseau VPN.
     - **HTTPS** + auth (domaine ou nip.io + Let’s Encrypt).

2. **HTTPS**
   - Port 443 ouvert dans UFW mais pas de TLS configuré pour BettingHUD.
   - Sans HTTPS : mots de passe basic auth et session Streamlit en clair sur le réseau.

### Priorité moyenne

3. **SSH : désactiver l’authentification par mot de passe**
   - État : `passwordauthentication yes` (clés aussi activées).
   - Recommandé : `PasswordAuthentication no` après validation de la clé `bettinghud_server`.

4. **Restreindre SSH (port 22) par IP**
   - UFW : `allow from TON_IP to any port 22` au lieu de `Anywhere`.
   - Réduit le brute-force (fail2ban non installé).

5. **fail2ban** (optionnel mais utile)
   - `sshd` + éventuellement `nginx` si logs d’échecs basic auth.

6. **Permissions `bettinghud.db`**
   - Fichier en `664` : lisible par tout utilisateur local du serveur.
   - `chmod 600` + propriétaire `ubuntu` si un seul service utilise la base.

### Priorité basse

7. **En-têtes nginx de durcissement**
   - `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy` (défense en profondeur).

8. **Séparer les secrets**
   - Pas de `.env` sur le serveur aujourd’hui (bien).
   - Si clés API ajoutées : fichier hors Git, permissions 600, pas dans les logs.

9. **Streamlit `enableXsrfProtection`**
   - Désactivé pour le reverse proxy — acceptable derrière nginx + auth ; à réévaluer si exposition directe.

---

## Plan d’action minimal (ordre suggéré)

| # | Action | Effort |
|---|--------|--------|
| 1 | Backup quotidien SQLite + test restauration | 30 min |
| 2 | Basic auth nginx **ou** UFW limiter 80/22 à ton IP | 30 min |
| 3 | Sonde UptimeRobot (ou équivalent) | 15 min |
| 4 | `PasswordAuthentication no` SSH | 15 min |
| 5 | `git pull` propre + `systemctl restart` daemon | 10 min |
| 6 | HTTPS (domaine / nip.io) | 1–2 h |

---

## Commandes de revérification

```bash
ssh bettinghud
systemctl is-enabled bettinghud-dashboard bettinghud-daemon nginx
systemctl is-active bettinghud-dashboard bettinghud-daemon nginx
curl -s http://127.0.0.1:8501/_stcore/health
ls -la /opt/bettinghud/data/cache/live_matches_snapshot*.joblib
sudo ufw status
sudo sshd -T | grep passwordauthentication
cd /opt/bettinghud && git status -sb
```
