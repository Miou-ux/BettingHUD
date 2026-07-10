# Archive WTA Sackmann — sauvegarde & restauration

Dernière mise à jour : **10 juillet 2026**.

L’archive sous `data/raw/tennis_wta/` est la **dernière copie connue** du dépôt [JeffSackmann/tennis_wta](https://github.com/JeffSackmann/tennis_wta) (repo **404** depuis juin 2026). Elle alimente `wta_matches` en SQLite, le ML WTA et `stats_engine`. **Ne pas modifier sans backup.**

**Voir aussi :** [[OPS_PROD_DEPANNAGE]] § 6.4 · [[CHANGELOG_RECENT]] · `scripts/_wta_delta_acceptance.md` · `scripts/backup_wta_sackmann_archive.py`.

---

## Contenu de l’archive (prod, 10/07/2026)

| Élément | Détail |
|---------|--------|
| **Chemin live** | `/opt/bettinghud/data/raw/tennis_wta/` (~78 Mo) |
| **Alimentation** | Pipeline **delta WTA** (voir ci-dessous) — cron **03:30** `bettinghud-data-sync` |
| **Schéma** | 49 colonnes Sackmann (`wta_matches_*.csv`) |
| **Fichiers** | 35 CSV : main 2010–2026, qual/ITF 2010–2026, `wta_players.csv`, `wta_rankings_current.csv` |

### Fraîcheur des matchs (prod, 10/07/2026)

| Fichier / table | `max(tourney_date)` | Source |
|-----------------|---------------------|--------|
| `wta_matches_2026.csv` (main) | **2026-07-10** | tennis-data + pont **Flashscore** |
| `wta_matches_qual_itf_2026.csv` | **2026-07-10** | pont **Flashscore** (prematch TE ITF) — tennis-data **sans ITF** dans le xlsx 2026 |
| `wta_rankings_current.csv` | ranking_date **2026-07-10** | `refresh_wta_rankings_current.py` (matchs + cache TE) |
| SQLite `wta_matches` | **406 666** lignes · max **2026-07-10** | `ingest_sackmann_wta.py` après pipeline |

### Pipeline delta (post-Sackmann)

| Étape | Script | Rôle |
|-------|--------|------|
| 1 | `sync_wta_delta.py` | Matchs tennis-data.co.uk → CSV main / qual_itf |
| 2 | `enrich_wta_delta_metadata.py` | IDs, âges, bio, dédup |
| 3 | `refresh_wta_rankings_current.py --ingest` | `wta_rankings_current.csv` + SQLite `rankings_wta_current` |
| 4 | `enrich_wta_delta_te_stats.py` | Stats service **Flashscore** |
| 5 | `sync_wta_flashscore_results.py` | Pont lag ~1 sem. → **main + qual/ITF** |
| 6 | `backfill_wta_delta_ranks.py` | Rangs match-time (historique + classement + cache TE) |
| 7 | `pipeline_quality.py` | Ingest SQLite + index |

**Important :** le backfill rangs tourne **après** Flashscore (évite les trous sur les lignes FS sans `WRank`).

### Source tennis-data — probe ITF

```bash
./venv/bin/python scripts/_probe_tdcuk_wta_tiers.py --year 2026 --refresh
```

Verdict prod (10/07) : **`source_no_itf_in_xlsx`** — le xlsx 2026w ne contient que le main tour (WTA250+). Le qual/ITF ne peut pas avancer via tennis-data seul.

### Modèle ML (prod, retrain 10/07/2026)

| Bundle | `global_test_brier` | `tour_WTA` | `tour_ATP` | Notes |
|--------|---------------------|------------|------------|-------|
| `xgb_model_tml_v47.pkl` (**actif**) | **0.1830** | **0.1692** | 0.1906 | Retrain manuel post-enrichissement WTA (`--min-year 2020`) |
| `xgb_model_tml_v47.pkl.elo_backup` | 0.1749 | 0.1718 | 0.1775 | Avant retrain 10/07 |
| `xgb_model_tml_v47_pre_wta_delta.pkl` | 0.1816 | 0.1664 | 0.1900 | Rollback juin (sans delta récent) |

**Modèle unique ATP+WTA** : un retrain WTA peut déplacer le Brier ATP (split temporel 80/20 commun, Elo/styles partagés). Voir [[CHANGELOG_RECENT]] § WTA 10/07.

```bash
./venv/bin/python scripts/_probe_ml_bundle_brier.py
```

### Classements WTA courants (juillet 2026)

Le fichier `wta_rankings_current.csv` **Sackmann d’origine** s’arrête au **2026-06-08**. Depuis juillet 2026, il est **régénéré** par `scripts/refresh_wta_rankings_current.py` :

1. Dernier rang/points par joueuse dans les CSV `wta_matches*` (delta tennis-data).
2. Complément cache profil **Tennis Explorer** si plus récent.

Appelé dans `sync_tours_daily` (03:30) et `morning_live_pipeline` (02:00) avant `ingest_rankings_current.py`.

---

## Backup immédiat — 17/06/2026 13:09 UTC

| Emplacement | Fichier | Taille |
|-------------|---------|--------|
| **Prod** | `/opt/bettinghud/data/backups/wta_sackmann/wta_sackmann_20260617_130955.tar.gz` | ~13,2 Mo |
| **Prod** | `…/wta_sackmann_20260617_130955.manifest.json` | manifest SHA256 par fichier |
| **Hors-site (PC local)** | `data/backups/wta_sackmann_offsite/wta_sackmann_20260617_130955.tar.gz` | copie `scp` du 17/06/2026 |
| **Hors-site (PC local)** | `…/wta_sackmann_20260617_130955.manifest.json` | idem |

**SHA256 tarball (prod) :** `299d3689e7773506d69931e08e4af91cf714848f9de3ec07c2b7c38ed0b3fbd8`

**Commande utilisée :**

```powershell
cd O:\Miouppy\Documents\BettingHUD
py scripts/backup_wta_sackmann_archive.py --remote bettinghud --retain 12 -v
```

**Copie hors-site :**

```powershell
New-Item -ItemType Directory -Force -Path data\backups\wta_sackmann_offsite
scp bettinghud:/opt/bettinghud/data/backups/wta_sackmann/wta_sackmann_20260617_130955.tar.gz `
    bettinghud:/opt/bettinghud/data/backups/wta_sackmann/wta_sackmann_20260617_130955.manifest.json `
    data\backups\wta_sackmann_offsite\
```

> Les dossiers `data/backups/` et `backups/` sont dans `.gitignore` — **ne pas committer** les tarballs.

---

## Procédures

### Backup manuel (avant tout append delta WTA)

```powershell
# Depuis Windows → backup sur le serveur
py scripts/backup_wta_sackmann_archive.py --remote bettinghud --retain 12

# Sur le serveur directement
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/backup_wta_sackmann_archive.py --retain 12"

# Local (si copie raw présente en PREPROD)
py scripts/backup_wta_sackmann_archive.py --raw-dir data/raw/tennis_wta --retain 4
```

Chaque run produit :

- `wta_sackmann_YYYYMMDD_HHMMSS.tar.gz`
- `wta_sackmann_YYYYMMDD_HHMMSS.manifest.json` (SHA256, lignes, `max_tourney_date` par CSV)

### Backup automatique prod

Fichier cron : `deploy/cron/wta-sackmann-backup`

```cron
# Dimanche 02:15 Europe/Paris
15 2 * * 0 ubuntu cd /opt/bettinghud && /opt/bettinghud/venv/bin/python scripts/backup_wta_sackmann_archive.py --retain 12
```

Installation sur le serveur (une fois) :

```bash
sudo cp /opt/bettinghud/deploy/cron/wta-sackmann-backup /etc/cron.d/bettinghud-wta-backup
sudo chmod 644 /etc/cron.d/bettinghud-wta-backup
```

Logs : `/opt/bettinghud/data/logs/wta_backup_cron.log`

### Vérifier une archive

```bash
# Lister le contenu
tar -tzf wta_sackmann_20260617_130955.tar.gz | head

# Comparer SHA256 d'un CSV extrait vs manifest
tar -xOzf wta_sackmann_20260617_130955.tar.gz wta_matches_2026.csv | sha256sum
# doit correspondre à la entrée "wta_matches_2026.csv" dans le .manifest.json
```

### Restauration complète (rollback)

```bash
# Sur prod — remplacer le dossier raw (après backup de l'état courant !)
cd /opt/bettinghud
./venv/bin/python scripts/backup_wta_sackmann_archive.py --retain 12   # snapshot état actuel
rm -rf data/raw/tennis_wta
mkdir -p data/raw/tennis_wta
tar -xzf data/backups/wta_sackmann/wta_sackmann_20260617_130955.tar.gz -C data/raw/tennis_wta
./venv/bin/python scripts/ingest_sackmann_wta.py
```

Depuis la copie hors-site Windows :

```powershell
scp data\backups\wta_sackmann_offsite\wta_sackmann_20260617_130955.tar.gz bettinghud:/tmp/
ssh bettinghud "cd /opt/bettinghud && rm -rf data/raw/tennis_wta && mkdir -p data/raw/tennis_wta && tar -xzf /tmp/wta_sackmann_20260617_130955.tar.gz -C data/raw/tennis_wta && ./venv/bin/python scripts/ingest_sackmann_wta.py"
```

---

## Registre des backups

| Date UTC | Archive | Emplacements | Notes |
|----------|---------|--------------|-------|
| 2026-06-17 13:09 | `wta_sackmann_20260617_130955.tar.gz` | prod + `data/backups/wta_sackmann_offsite/` | **Backup de référence** pré-merge delta |
| 2026-06-18 07:03 | `wta_sackmann_20260618_070337.tar.gz` | prod | **Backup pré-promotion** delta en prod |

---

## Rappels

1. **Toujours** backup avant append delta — gate J0 dans `_wta_delta_brier_plan.md` (cron auto **dim. 02:15**).
2. Conserver **au moins une copie hors serveur** (PC, disque externe, cloud perso).
3. Ne pas remplacer l’archive par des mirrors Kaggle/buildoak sans vérifier fraîcheur (prod 2026 > buildoak fév. 2026).
4. Cron quotidien **03:30** : `sync_tours_daily.py` → pipeline delta WTA sur `data/raw/tennis_wta/`.
5. Rapport santé ML **lundi 08:00** : `ml_weekly_telegram_notify.py` (Brier WTA + alertes jobs).

---

## Preprod delta (expériences locales)

Pipeline isolé pour tests avant promotion prod. **Prod utilise le même code** sur `data/raw/tennis_wta/` depuis le **18/06/2026**.

| Chemin | Rôle |
|--------|------|
| `data/archives/wta_sackmann_socle/` | **IMMUTABLE** — extrait du tarball de référence |
| `data/preprod/wta_work/tennis_wta/` | Copie de travail + delta append |
| `data/preprod/bettinghud_wta_delta.db` | SQLite preprod (ingest WTA work) |
| `models/preprod/xgb_wta_delta_candidate.pkl` | Bundle candidat |

```powershell
# Pipeline complet preprod (gate J6 à la fin)
py scripts/run_wta_delta_preprod.py --force-refresh-work

# Étapes partielles
py scripts/wta_socle_manager.py verify
py scripts/wta_socle_manager.py init
py scripts/wta_socle_manager.py refresh-work --force
py scripts/sync_wta_delta.py --work-dir data/preprod/wta_work/tennis_wta
py scripts/enrich_wta_delta_te_stats.py --work-dir data/preprod/wta_work/tennis_wta
py scripts/check_wta_delta_acceptance.py --raw-dir data/preprod/wta_work/tennis_wta --brier-gate
```

**Prod n'est pas modifié** par les scripts preprod pointant vers `data/preprod/` — la prod live est sous `data/raw/tennis_wta/`.
