# Rollback bundle ML (v47 → v48+)

Garantie avant **Phase 1** (données WTA) et **Phase 2** (calibration segment) : le bundle **v47** reste récupérable en **une commande**, sans revert git.

## Principe

| Fichier | Rôle |
|---------|------|
| `models/xgb_model_tml_v47.pkl` | Bundle prod habituel (défaut code) |
| `models/baselines/xgb_model_tml_v47_prod_baseline.pkl` | **Copie figée** — ne jamais écraser sauf `freeze` explicite |
| `models/.ml_bundle_active` | Pointeur vers le bundle chargé au runtime |
| `models/candidates/*.pkl` | Entraînements v48 / WTA delta — **n'écrasent jamais v47** |
| `models/baselines/ml_bundle_manifest.json` | SHA256 + dates freeze / activation |

Priorité au chargement (`TennisMLModel`, dashboard, daemon) :

1. Variable d'environnement `BETTINGHUD_ML_BUNDLE`
2. Fichier `models/.ml_bundle_active`
3. `models/xgb_model_tml_v47.pkl`

## Avant Phase 1 — figer v47 (PREPROD + PROD)

```bash
# Local
py -3 scripts/ml_bundle_cli.py freeze

# Prod (une fois)
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/ml_bundle_cli.py freeze"
```

Vérifier :

```bash
py -3 scripts/ml_bundle_cli.py status
```

## Workflow v48 candidat (sans risque)

```text
1. run_wta_delta_preprod.py  → models/candidates/xgb_wta_delta_candidate.pkl
2. check_wta_brier_j6.py     → gate Brier vs baseline figée
3. Backtest / shadow proba   → comparer v47 vs candidat
4. Si OK : ml_bundle_cli.py promote <candidat>
5. Si KO : ml_bundle_cli.py rollback
```

Le candidat **n'écrit jamais** dans `models/xgb_model_tml_v47.pkl` ni dans `baselines/` tant que vous n'appelez pas `promote`.

## Rollback d'urgence (prod)

```bash
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/ml_bundle_cli.py rollback && \
  sudo systemctl restart courtalpha-api bettinghud-daemon bettinghud-dashboard bettinghud-telegram-bot"
```

Effet :

- Restaure `models/xgb_model_tml_v47.pkl` depuis la baseline figée
- Met à jour `models/.ml_bundle_active`
- **Aucun** changement de code ni de données WTA nécessaire

Alternative sans toucher au fichier par défaut (test rapide) :

```bash
export BETTINGHUD_ML_BUNDLE=models/baselines/xgb_model_tml_v47_prod_baseline.pkl
```

## Routage ATP / WTA — niveau 1 (PREPROD uniquement)

> **Statut juillet 2026 : exploration PREPROD.** Le serveur PROD charge toujours **un seul bundle** (v47). Le routage circuit est un banc d’essai local pour valider le candidat WTA delta avant toute `promote`.

### Pourquoi explorer le split ?

Le modèle prod actuel est un **bundle unifié** (ATP + WTA, une seule passe `prepare_data` + un XGBoost). Le pipeline **delta WTA** produit un candidat (`models/candidates/xgb_wta_delta_candidate.pkl`) qui peut :

- améliorer `tour_WTA` sur le hold-out test ;
- dégrader `tour_ATP` ou des segments (`ATP_Clay`, etc.) si on `promote` le bundle entier.

Le **routage niveau 1** permet de mesurer l’impact WTA **sans toucher aux probas ATP** : v47 figé sur ATP, candidat sur WTA uniquement.

Trois chemins possibles après évaluation :

| Chemin | Description | Rollback |
|--------|-------------|----------|
| **A — Bundle unifié** | `promote` du candidat → un seul `.pkl` prod | `ml_bundle_cli.py rollback` |
| **B — Routage niveau 1** | Deux bundles, choix à l’inférence par `tour` | `tour-routing off` (+ éventuellement `rollback`) |
| **C — Abandon** | Garder v47 baseline | — |

**Aucun de ces chemins n’est actif en PROD** à ce stade.

### Garde-fou PROD

Ignoré si `BETTINGHUD_ENV=prod` (systemd serveur) — vérifié par `is_tour_routing_allowed()` et `tests/test_ml_tour_router.py`.

| Fichier / variable | Rôle |
|--------------------|------|
| `models/.ml_tour_routing_preprod.json` | Config ATP + WTA bundles |
| `BETTINGHUD_ML_TOUR_ROUTING=1` | Active le routage (PREPROD) |
| `BETTINGHUD_ML_WTA_BUNDLE` | Override chemin WTA (optionnel) |
| `BETTINGHUD_ML_ATP_BUNDLE` | Override chemin ATP (optionnel) |

À l’inférence, `TennisMLModel.model_for_inference(tour)` délègue au bon backend ; `tour_routing_status()` expose l’état du routeur.

### Activer (PREPROD)

```powershell
$env:BETTINGHUD_ENV = "preprod"
$env:BETTINGHUD_ML_TOUR_ROUTING = "1"
$env:BETTINGHUD_DB_PATH = "data/preprod/bettinghud_wta_delta.db"   # optionnel, DB delta WTA
py -3 scripts/ml_bundle_cli.py tour-routing on `
  --atp models/baselines/xgb_model_tml_v47_prod_baseline.pkl `
  --wta models/candidates/xgb_wta_delta_candidate.pkl
py -3 scripts/preprod_tour_routing_smoke.py
```

### Évaluer (PREPROD)

```powershell
# Hold-out Brier v47 vs candidat (segments tour_ATP / tour_WTA)
py -3 scripts/shadow_wta_candidate_replay.py

# Replay sélection hybride Top 5 — v47 unifié vs routé (mode rapide, ~minutes)
py -3 scripts/preprod_tour_routing_replay.py

# Replay complet feature-store 2026 (lent, ~15–30 min, cache joblib)
py -3 scripts/preprod_tour_routing_replay.py --full-feature-store
```

| Script | Compare | Métriques typiques |
|--------|---------|-------------------|
| `shadow_wta_candidate_replay.py` | Bundles sur hold-out + picks prod figés | Brier segment `tour_WTA` / `tour_ATP`, hit, flat |
| `preprod_tour_routing_replay.py` | Resélection Top 5 hybride avec probas recalculées | hit %, PnL flat, diff picks v47 vs routé |

Le mode rapide du replay recalcule les probas WTA via `predict_match` ; le mode `--full-feature-store` reconstruit l’index batch complet (validation |Δ| vs export CSV).

### Rollback immédiat

```powershell
py -3 scripts/ml_bundle_cli.py tour-routing off
$env:BETTINGHUD_ML_TOUR_ROUTING = "0"
```

Comportement : `predict_match` et Brier segment utilisent **v47 pour ATP**, **candidat WTA pour WTA**. Aucun changement sur le serveur PROD tant que `BETTINGHUD_ENV=prod`.

**Ne pas confondre** avec le routage **BO3 / BO5** (calibration isotonique dans un même bundle) — voir `PREDICTION_ET_MISE.md` § 1bis.

### Routage niveau 3 — split « propre » ATP-only / WTA-only (PREPROD, juillet 2026)

Entraînements **séparés** sur snapshot DB prod (`data/preprod/bettinghud_prod_snapshot.db`) :

| Bundle | Filtre | BO3/BO5 |
|--------|--------|---------|
| `models/candidates/xgb_atp_only_l3.pkl` | `prepare_data(tour_filter="ATP")` | **Oui** — dual calibrateur GC / BO3 |
| `models/candidates/xgb_wta_only_l3.pkl` | `prepare_data(tour_filter="WTA")` | BO3 seulement (WTA Majeurs = BO3 live) |

Split temporel **80/20 par tour** (pas de mélange ATP/WTA à l'entraînement).

```powershell
$env:BETTINGHUD_ENV = "preprod"
py -3 scripts/preprod_tour_split_l3.py --fetch-prod
# ou snapshot local : --snapshot-from data/bettinghud.db

$env:BETTINGHUD_ML_TOUR_ROUTING = "1"
py -3 scripts/preprod_tour_split_l3.py --eval-only --enable-routing
py -3 scripts/preprod_tour_routing_replay.py
```

Rapport JSON : `data/preprod/tour_split_l3_report.json` (Brier global mixé, par tour, vs joint sur mêmes lignes test).

CLI entraînement unitaire : `update_model_tml.py --tour-filter ATP|WTA --skip-sync --db-path ... --output-pkl ...`

## Phase 2 (calibration segment)

Deux options sûres :

| Option | Rollback |
|--------|----------|
| **A** — Calibration dans un **nouveau bundle** v48 | `rollback` → v47 baseline |
| **B** — Couche post-calibration segment (code séparé, flag env) | `BETTINGHUD_SEGMENT_CALIB=0` ou rollback code |

Recommandation : **option A** dans le candidat `.pkl`, validée par gate J6, puis `promote` ou `rollback`.

## Critères go / no-go avant promote

| Métrique | Seuil |
|----------|--------|
| Brier global test | ≤ v47 + 0.005 |
| `tour_WTA` | ≤ v47 |
| `ATP_Clay` | < v47 (objectif Phase 1+2) |
| Brier Top5 replay 2026 | ≤ v47 |

## Liens

- Exploration (chronologie) : `docs/CHANGELOG_RECENT.md` § « Exploration split ATP / WTA »
- Pipeline WTA preprod : `scripts/run_wta_delta_preprod.py`
- Gate J6 : `scripts/check_wta_brier_j6.py`
- Plan WTA : `scripts/_wta_delta_brier_plan.md`
- Archive & Brier référence : `docs/WTA_SACKMANN_ARCHIVE.md`
- Tests routage : `tests/test_ml_tour_router.py`
- Ancien rollback WTA delta : `models/xgb_model_tml_v47_pre_wta_delta.pkl` (historique)
