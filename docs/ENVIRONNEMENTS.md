# Environnements PREPROD / PROD

Dernière mise à jour : **28 mai 2026**.

Convention officielle du projet :

| Environnement | Machine | Rôle | URL typique |
|---------------|---------|------|-------------|
| **PREPROD** | PC local (Windows) | Développement, tests, expérimentations | `http://localhost:8501` |
| **PROD** | Serveur dédié Ubuntu | Usage réel, paris et données de référence | `http://192.95.30.217` |

Variable d’environnement : **`BETTINGHUD_ENV`** = `preprod` (défaut) ou `prod`.

---

## Règles d’or

1. **Tout changement de code** : développer et valider en **PREPROD**, puis déployer en **PROD** (`git push` → `git pull` sur le serveur).
2. **Paris réels** : enregistrés en priorité en **PROD** (portefeuille de référence).
3. **PREPROD** : tests, backtests, essais UI, retrain ML, rebuild snapshot sans impact sur les utilisateurs « live ».
4. **Ne jamais** lancer un nettoyage destructif de `user_bets` en PROD sans sauvegarde explicite.
5. **`data/` et `models/`** : bases distinctes (pas de sync automatique). Copie manuelle uniquement si besoin (voir ci-dessous).

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
| `data/cache/` (snapshots) | Rebuild fréquent | Rebuild pipeline matin + manuel |

### Promouvoir un modèle PREPROD → PROD

Après retrain validé en local (Brier, audit snapshot) :

```powershell
scp O:\Miouppy\Documents\BettingHUD\models\xgb_model_tml_v47.pkl bettinghud:/opt/bettinghud/models/
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/rebuild_live_projection.py"
```

### Copier la base PROD → PREPROD (debug uniquement)

Utile pour reproduire un bug avec les vrais paris — **écrase** la base locale :

```powershell
scp bettinghud:/opt/bettinghud/data/bettinghud.db O:\Miouppy\Documents\BettingHUD\data\bettinghud.db
```

Ne pas faire l’inverse (PREPROD → PROD) sans contrôle : risque d’écraser l’historique réel.

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

L’onglet **Paramètres** du dashboard affiche un bandeau **PROD** ou **PREPROD**.

---

## Automatisations par environnement

| Tâche | PREPROD | PROD |
|-------|---------|------|
| Pipeline matin | Manuel ou tâche Windows (`register_morning_task.ps1`) | Cron 05:00 UTC |
| Daemon portefeuille | `run_portfolio_daemon.bat` ou `--once` | `bettinghud-daemon.service` |
| Sync tours / ML auto | Threads dashboard local | Idem (variables `BETTINGHUD_*`) |

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
- [[ARCHITECTURE_ACTUELLE_ET_MISES]] — § 12 Déploiement
- [[CHANGELOG_RECENT]] — historique des changements
