# Données ATP & WTA — fraîcheur, qualité, dérive

Dernière mise à jour : **16 août 2026**.

Référence opérationnelle pour la **fiabilité des données historiques** (TML / Sackmann / delta WTA) alimentant le ML, le feature store et l’UI « dernier match ».

**Voir aussi :** [[WTA_SACKMANN_ARCHIVE]] · [[CRONS_SEMAINE]] · `scripts/_wta_delta_acceptance.md` · `scripts/qc_post_sync.py` · `scripts/wta_delta_qc_gates.py`

---

## 1. Sources et rôle

| Tour | Source primaire | Table SQLite | Usage |
|------|-----------------|--------------|-------|
| **ATP** | TennisMyLife (`sync_tml_recent.py`) | `matches_recent` | Elo, features historiques, dernier match ATP |
| **WTA main** | Delta tennis-data + Flashscore sur archive Sackmann | `wta_matches` | Idem côté WTA |
| **WTA ITF/qual** | Flashscore (tennis-data xlsx 2026 **sans ITF**) | `wta_matches` (qual_itf CSV) | Volume ITF ; rangs partiels |
| **Live du jour** | Scrape Tennis Explorer + snapshot | `live_matches_snapshot.*` | Picks / proba du jour (indépendant du lag Sackmann) |

Le modèle prod (**v47**, bundle unique ATP+WTA) et le feature store consomment `matches_recent` + `wta_matches`. Le snapshot live est rafraîchi à **02:00** ; l’ingest historique à **03:30**.

---

## 2. Chaîne nocturne (03:30)

Ordre dans `scripts/sync_tours_daily.py` :

1. Delta WTA : `sync_wta_delta.py` → `enrich_wta_delta_metadata.py` → rangs / stats TE
2. `sync_wta_flashscore_results.py` + backfill rangs
3. Dédup post-Flashscore : `enrich_wta_delta_metadata.py --dedup`
4. ATP : `sync_tml_recent.py`
5. `sync_atp_flashscore_results.py`
6. `pipeline_quality.py` → ingest WTA + index SQLite
7. `build_feature_store.py` + `refresh_elo_maps_fast.py`
8. QC : `qc_post_sync.py` + gates WTA (`wta_delta_qc_gates`)

**Meta horodatée** (table `bets_meta`) :

| Clé | Quand |
|-----|-------|
| `last_tml_sync_ts` | Fin `sync_tml_recent` OK |
| `last_sackmann_sync_ts` | Fin `pipeline_quality` OK |
| `last_tours_sync_ts` | Idem (depuis 16/08/2026 — plus bloqué par QC seul) |

Logs : `data/logs/tours_auto_sync.log`, `data/logs/tours_cron.log`.

---

## 3. Contrôles qualité (QC)

### Post-sync global — `qc_post_sync.py`

- ATP / WTA : `MAX(tourney_date)` ≥ J-5 (seuil `BETTINGHUD_MORNING_TOURS_MAX_LAG_DAYS`, déf. 5)
- Âge `player_feature_store.joblib` ≤ 36 h
- Merge des gates WTA delta

### Gates WTA — `wta_delta_qc_gates.py`

| Gate | Seuil | Description |
|------|-------|-------------|
| **C1** | 0 doublon | Clé `(tourney_date, tourney_name, winner_name, loser_name)` sur CSV raw |
| **D1** | ≥ 90 % WARN / ≥ 80 % FAIL | Rangs main tour post-cutoff |
| SQLite rangs | idem | Cohérence post-ingest |

Cutoff delta : `BETTINGHUD_WTA_SACKMANN_CUTOFF` (déf. `20260526`).

### Audit manuel

```bash
# Prod
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/_audit_atp_wta_data.py"
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/qc_post_sync.py"
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/wta_delta_qc_gates.py"
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/_probe_wta_date_display.py"
```

---

## 4. Incident & correctif — 16 août 2026

### Symptômes

- UI settings : date WTA **20/07/2029** (Iasi Open)
- QC bloquant : **307 doublons C1** → sync `rc=1` chaque nuit depuis ~fin juillet
- `enrich_wta_delta_metadata.py` en échec : `TypeError: Invalid value 'R' for dtype 'float64'`
- Meta `last_tours_sync_iso` figée au **20/07** alors que Sackmann tournait

### Causes

1. **Ligne corrompue** `tourney_date=20290720` dans un CSV delta (sans validation amont)
2. **Doublons** multi-sources (tennis-data + Flashscore) jamais dédupliqués car enrich plantait avant `--dedup`
3. **Bug pandas** : colonnes `round` / `entry` lues en float64 quand vides → écriture `"R"` impossible
4. **Meta sync** : `last_tours_sync_ts` uniquement si `rc=0` final (QC bloquant)

### Correctifs code

| Fichier | Changement |
|---------|------------|
| `wta_sackmann_common.py` | `drop_aberrant_wta_tourney_dates`, `ensure_wta_frame_writable`, `max_sane_wta_year` |
| `enrich_wta_delta_metadata.py` | Filtre dates + dtype object avant assignation |
| `ingest_sackmann_wta.py` | Rejet dates > année courante + 1 à l’ingest |
| `sync_tours_daily.py` | Stamp `last_tours_sync_ts` dès ingest Sackmann OK |
| `bets_db.py` | Filtre SQL dates WTA aberrantes pour « dernier match » |
| `date_display_eu.py` | Affichage DD/MM/YYYY CET |

### Nettoyage prod (16/08/2026)

Script : `scripts/fix_wta_data_cleanup.py`

```bash
cd /opt/bettinghud
./venv/bin/python scripts/fix_wta_data_cleanup.py
./venv/bin/python scripts/ingest_sackmann_wta.py
./venv/bin/python scripts/build_feature_store.py
```

Résultat :

- **307** doublons main tour supprimés
- **1** ligne 2029 supprimée
- **409 028** lignes WTA en SQLite
- QC : **0 blocking, 0 warnings**
- Dernier match WTA : **15/08/2026 Cincinnati**

---

## 5. État post-correctif (16/08/2026 ~09:00 CET)

| Indicateur | ATP | WTA |
|------------|-----|-----|
| Dernier match | 14/08 Canada Masters | 15/08 Cincinnati WTA |
| Max DB | 2026-08-16 | 2026-08-15 |
| Lag > 5 j | Non | Non |
| QC post-sync | OK | OK |
| Rangs main post-cutoff | — | **98,6 %** |
| Rangs all tiers | — | **83,2 %** |
| Doublons C1 | — | **0** |
| Dates futures aberrantes | — | **0** |
| `last_tours_sync_iso` | 16/08 08:52 | (idem) |

---

## 6. Risque de dérive long terme

### Ce qui est bien couvert aujourd’hui

- **Fraîcheur** : QC J-5 + crons quotidiens + preflight matin
- **Doublons WTA** : dédup nocturne + gate C1 (alerte si régression)
- **Dates impossibles** : filtre ingest + enrich + affichage SQL
- **Archive WTA** : backup hebdo tarball (`wta-sackmann-backup`)
- **Brier / ML** : retrain hebdo dim. + rapport lundi admin TG

### Exposition résiduelle (dérive possible)

| Risque | Gravité | Mécanisme | Mitigation recommandée |
|--------|---------|-----------|------------------------|
| **Sources externes fragiles** | Haute | Repo Sackmann mort ; tennis-data sans ITF 2026 ; Flashscore layout / rate-limit | Surveiller `tours_auto_sync.log` ; backup WTA ; sonde `_probe_tdcuk_wta` |
| **Doublons récurrents** | Moyenne | Nouvelles sources ou alias noms → re-doublons C1 | Gate C1 quotidien ; `--dedup` post-FS ; revue alias `wta_name_aliases.py` |
| **Trous rangs / stats service WTA** | Moyenne | ITF sans rang WTA ; TE stats partielles (~90 % main, ~83 % all) | Gate D1 ; `enrich_wta_delta_te_stats` retry ; accepter ITF hors picks prod |
| **IDs joueuses synthétiques** | Moyenne | Delta crée IDs 920000+ si joueuse inconnue | `enrich_wta_delta_metadata` remap ; ~40 synthétiques restants post-cutoff (surveiller) |
| **Dérive calibration ML** | Moyenne | Bundle unique ATP+WTA : enrichissement WTA déplace Brier ATP | Retrain contrôlé ; comparer `global_test_brier` + split tour ; rollback bundle `.elo_backup` |
| **Meta / UI trompeuse** | Faible | Timestamps partiels si étape amont échoue | Stamp `last_tours_sync_ts` sur ingest OK (fix 16/08) |
| **Erreurs silencieuses delta** | Moyenne | `enrich_wta_delta_metadata` échouait mais pipeline continuait partiellement | Corrigé dtype ; faire échouer plus tôt si enrich initial FAIL (option future) |
| **Lag acceptable vs réel** | Faible | Historique J-1/J-2 normal (FS lag ~1 sem. sur ITF) | Distinction snapshot live (jour J) vs Sackmann (J-1) |

### Verdict dérive

- **Court terme (picks du jour)** : **faible dérive** — snapshot TE + cotes live dominent ; historique Sackmann surtout pour Elo/features.
- **Moyen terme (calibration / backtest WTA)** : **dérive modérée** — pipeline delta multi-sources sans éditeur unique ; QC C1/D1 + backup limitent l’ampleur.
- **Long terme (2027+)** : **dépendance structurelle** à Flashscore + tennis-data ; plan de repli = archive tarball + scripts delta documentés ; revue semestrielle des gates et du Brier WTA vs ATP.

### Actions de surveillance (routine)

1. Lundi **08:00** — rapport ML admin (Brier WTA)
2. Quotidien — `grep 'rc=0' data/logs/tours_auto_sync.log | tail -1`
3. Hebdo — `./venv/bin/python scripts/_audit_atp_wta_data.py`
4. Mensuel — comparer hit-rate hybrid WTA vs ATP (`prod_top5_segment_perf.py`)

---

## 7. Fichiers utiles

| Fichier | Rôle |
|---------|------|
| `scripts/sync_tours_daily.py` | Orchestration sync |
| `scripts/fix_wta_data_cleanup.py` | Nettoyage manuel (dedup + ingest + QC) |
| `scripts/_audit_atp_wta_data.py` | Audit fraîcheur / couverture |
| `scripts/_audit_wta_future_dates.py` | Lignes dates futures en DB |
| `scripts/_probe_wta_date_display.py` | Dernier match + meta sync |
| `scripts/check_wta_delta_acceptance.py` | Checklist acceptance delta |
| `scripts/wta_delta_qc_gates.py` | Gates C1/D1 post-sync |
| `scripts/ops_alert_human.py` | Messages TG admin lisibles (échecs cron / QC) |
| `scripts/cron_run_with_alert.py` | Wrapper cron + alerte TG |

---

## 8. Notifications Telegram admin (nuit)

Les jobs nocturnes alertent via `cron_run_with_alert.py` → `ops_alert_human.py`.

| Heure | Job | Message typique |
|-------|-----|-----------------|
| **02:00** | Préparation snapshot | Problème TE / snapshot + impact publication 05:00 |
| **03:30** | Sync ATP+WTA | Doublons WTA, script en échec, état ATP/WTA actuel |
| **04:40** | Preflight | Liste des contrôles FAIL avant publish |
| **05:00** | Publication picks | Échec chaîne publish |
| **06:30** | Digest admin | Calibration en langage clair + bloc « Données tennis » |

Sections standard : **Ce qui bloque** · **État des données** · **Impact** · **Que faire**.

```bash
python scripts/daily_admin_notify.py --dry-run
python -m pytest tests/test_ops_alert_human.py -q
```
