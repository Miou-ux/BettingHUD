# Environnements PREPROD / PROD

Dernière mise à jour : **28 mai 2026**.

Convention officielle du projet :

| Environnement | Machine | Rôle | URL typique |
|---------------|---------|------|-------------|
| **PREPROD** | PC local (Windows) | Développement, tests, expérimentations | `http://localhost:8501` |
| **PROD** | Serveur dédié Ubuntu | Usage réel, paris et données de référence | `http://192.95.30.217` |

> **Terminologie** : la PROD est un **serveur dédié** (machine physique ou dédiée chez l’hébergeur), pas un VPS mutualisé. Dans la doc et les échanges, préférer « serveur dédié PROD » plutôt que « VPS ».

Variable d’environnement : **`BETTINGHUD_ENV`** = `preprod` (défaut) ou `prod`.

---

## Règles d’or

1. **Tout changement de code** : développer et valider en **PREPROD**, puis déployer en **PROD** (`git push` → `git pull` sur le serveur).
2. **Paris réels** : enregistrés en priorité en **PROD** (portefeuille de référence).
3. **PREPROD** : tests, backtests, essais UI, retrain ML, rebuild snapshot sans impact sur les utilisateurs « live ».
4. **Ne jamais** lancer un nettoyage destructif de `user_bets` en PROD sans sauvegarde explicite.
5. **`data/` et `models/`** : bases distinctes (pas de sync automatique). Copie manuelle uniquement si besoin (voir ci-dessous).
6. **Ne pas confondre** `BETTINGHUD_HEADLESS` (scripts CLI) et `--server.headless` (Streamlit sans navigateur sur le serveur).

### Ce qui est synchronisé automatiquement

| Élément | PREPROD → PROD |
|---------|----------------|
| Code (`app/`, `scripts/`, `deploy/`, `requirements.txt`) | Oui, via **Git** |
| Paris en cours (`user_bets`) | **Non** |
| BR Live Tracker (`live_br_*` dans SQLite) | **Non** |
| Snapshots `data/cache/` | **Non** (rebuild par env) |
| Modèle `.pkl` | **Non** (promotion manuelle `scp`) |

Au **premier déploiement**, une copie `scp` de `bettinghud.db` peut avoir aligné PREPROD et PROD ; ensuite chaque environnement évolue séparément.

---

## Workflow de déploiement code

```text
PREPROD (PC)                    PROD (serveur)
     │                                │
     ├─ modifier code / tester        │
     ├─ git commit + push ───────────►├─ git pull
     │                                ├─ pip install -r requirements.txt (si besoin)
     │                                └─ systemctl restart bettinghud-dashboard bettinghud-daemon
```

Détail serveur : [[DEPLOY_SERVEUR]].

---

## Données et modèle ML

| Artefact | PREPROD | PROD |
|----------|---------|------|
| `data/bettinghud.db` | Copie de travail locale | Base **référence** production |
| `models/xgb_model_tml_v47.pkl` | Entraînement / essais | Bundle **actif** après validation PREPROD |
| `models/candidates/*.pkl` | Candidats WTA delta / v48 | **Non déployés** tant que `promote` explicite |
| `models/.ml_tour_routing_preprod.json` | Routage ATP/WTA niveau 1 | **Absent** — routage circuit **jamais** actif en PROD |
| `data/cache/` (snapshots) | Rebuild fréquent | Rebuild pipeline matin + manuel |

### Exploration routage ATP / WTA (PREPROD seulement)

Le split circuit (v47 ATP + candidat WTA) est une **expérimentation locale** — le serveur PROD ignore toujours `BETTINGHUD_ML_TOUR_ROUTING` grâce au garde-fou `BETTINGHUD_ENV=prod`.

Workflow typique :

1. Pipeline delta WTA : `py -3 scripts/run_wta_delta_preprod.py`
2. Activer routage : `ml_bundle_cli.py tour-routing on` + `$env:BETTINGHUD_ML_TOUR_ROUTING = "1"`
3. Smoke + replay : `preprod_tour_routing_smoke.py`, `shadow_wta_candidate_replay.py`, `preprod_tour_routing_replay.py`
4. Désactiver : `ml_bundle_cli.py tour-routing off`

Référence complète : [[ML_BUNDLE_ROLLBACK]] · [[CHANGELOG_RECENT]] § exploration split.

### Promouvoir un modèle PREPROD → PROD

Après retrain validé en local (Brier, audit snapshot) :

```powershell
scp O:\Miouppy\Documents\BettingHUD\models\xgb_model_tml_v47.pkl bettinghud:/opt/bettinghud/models/
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/rebuild_live_projection.py"
```

**Important** : si une API sœur (ex. `CourtAlpha` sous `/opt/courtalpha`) importe `scripts.ml_model.TennisMLModel`, le bundle actif doit être résolu **depuis le repo BettingHUD**, pas depuis le répertoire courant du service appelant. Le correctif de juillet 2026 dans `scripts/ml_model.py` couvre ce cas.

### Déploiement d’un script métier non tracké par défaut

Certaines routes CourtAlpha dépendent directement de scripts BettingHUD hors `app/` (ex. `scripts/backtest_prod_top5_2026.py` pour `/api/picks/top5-replay`). Si un script existe localement mais pas sur `/opt/bettinghud/scripts/`, une page PROD peut tomber en **500** malgré un dashboard BettingHUD sain.

Checklist :

1. Vérifier la présence serveur : `ssh bettinghud "ls /opt/bettinghud/scripts/<script>.py"`
2. Si absent, copier explicitement : `scp ... bettinghud:/opt/bettinghud/scripts/`
3. Redémarrer le service consommateur (`courtalpha-api`, `bettinghud-dashboard`, etc.)

### Copier la base PROD → PREPROD (debug uniquement)

Utile pour reproduire un bug avec les vrais paris — **écrase** la base locale :

```powershell
scp bettinghud:/opt/bettinghud/data/bettinghud.db O:\Miouppy\Documents\BettingHUD\data\bettinghud.db
```

Ne pas faire l’inverse (PREPROD → PROD) sans contrôle : risque d’écraser l’historique réel.

### Backup quotidien PROD → PC local (recommandé)

Copie automatique de la base **production** sur ton PC (hors Git, dossier `backups/prod/`) :

```powershell
cd O:\Miouppy\Documents\BettingHUD
# Test manuel
powershell -ExecutionPolicy Bypass -File scripts\backup_prod_db_to_local.ps1

# Tâche planifiée Windows (05:30 chaque jour, 30 jours de rétention)
powershell -ExecutionPolicy Bypass -File scripts\register_prod_backup_task.ps1
```

Prérequis : alias SSH `bettinghud` (clé dans `~/.ssh/config`), venv Python sur le serveur (`/opt/bettinghud/venv`).

Restauration locale (écrase PREPROD) :

```powershell
Copy-Item backups\prod\bettinghud_prod_YYYYMMDD_HHmmss.db data\bettinghud.db -Force
```

---

## Configuration

### PREPROD (PC)

Par défaut, sans variable : `BETTINGHUD_ENV=preprod`.

```powershell
cd O:\Miouppy\Documents\BettingHUD
.\venv\Scripts\activate
$env:BETTINGHUD_ENV = "preprod"
streamlit run app/dashboard.py
```

### PROD (serveur)

Défini dans les unités systemd (`deploy/systemd/`) :

```ini
Environment=BETTINGHUD_ENV=prod
```

**Important :** ne pas mettre `BETTINGHUD_HEADLESS=1` sur le service **dashboard** — ce flag sert aux scripts (`rebuild_live_projection.py`, etc.) pour charger les moteurs **sans** dessiner l’UI. Streamlit serveur sans navigateur = déjà `--server.headless=true`.

L’onglet **Paramètres** du dashboard affiche un bandeau **PROD** ou **PREPROD**.

### Tableau des variables (résumé)

| Variable | PREPROD | PROD (systemd dashboard) |
|----------|---------|---------------------------|
| `BETTINGHUD_ENV` | `preprod` (défaut) | `prod` |
| `BETTINGHUD_HEADLESS` | Scripts CLI uniquement (`1`) | **Non défini** |
| `BETTINGHUD_LIVE_DATA_DAEMON` | Optionnel (`1` si activé) | `1` |
| `BETTINGHUD_AUTO_SYNC_TOURS` | Optionnel | `1` |

Planning complet (cron, daemon, ML, snapshot) : **`docs/SCHEDULE_MISES_A_JOUR.md`**.  
Détail ops et dépannage : **`docs/OPS_PROD_DEPANNAGE.md`**.

---

## Automatisations par environnement

> **Planning détaillé** (horaires, intervalles, commandes) : [[SCHEDULE_MISES_A_JOUR]] · **Crons hebdo** : [[CRONS_SEMAINE]].

| Tâche | PREPROD | PROD |
|-------|---------|------|
| Pipeline matin | Manuel ou tâche Windows (`register_morning_task.ps1`) | Cron **02:00** + **05:00** Paris |
| Sync tours ATP/WTA | Manuel | Cron **03:30** (WTA delta) |
| Retrain ML | Manuel | Cron **dim. 04:00** · rapport **lun. 08:00** TG |
| Telegram | **Non** (`--dry-run`) | Pipeline 05:00 + `bettinghud-telegram-bot.service` |
| Daemon portefeuille | `run_portfolio_daemon.bat` ou `--once` | `bettinghud-daemon.service` |

---

## Checklist avant mise en PROD

- [ ] Tests locaux OK (UI, Live Tracker, Paris du jour)
- [ ] `git push` sur `main`
- [ ] `ssh bettinghud` → `git pull`
- [ ] Redémarrage services si changement code
- [ ] Rebuild snapshot si nouveau modèle ou gros changement données
- [ ] Vérification URL PROD dans le navigateur

---

## Voir aussi

- [[DEPLOY_SERVEUR]] — installation et ops serveur
- [[SCHEDULE_MISES_A_JOUR]] — planning scrape, snapshot, ML, daemon, Telegram
- [[OPS_PROD_DEPANNAGE]] — incidents PROD, nginx, HEADLESS, checklist
- [[PROD_RESILIENCE]] — redémarrage automatique (systemd, boot serveur)
- [[ARCHITECTURE_ACTUELLE_ET_MISES]] — § 12 Déploiement
- [[CHANGELOG_RECENT]] — historique des changements
