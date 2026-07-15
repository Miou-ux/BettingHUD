# Architecture actuelle, modèle et mises

Dernière mise à jour : 28 mai 2026.

Ce document décrit l'état courant du système après les changements récents sur le Live Tracker, le modèle v47, les ELO, le Report Opportunités et la logique de mise. Les pages historiques restent utiles, mais celle-ci sert de référence opérationnelle.

## 1. Vue d'ensemble

BettingHUD est une application locale Streamlit qui combine :

1. une base SQLite locale (`data/bettinghud.db`) ;
2. des historiques ATP/WTA synchronisés depuis TennisMyLife, Sackmann/Tennis Abstract et les tables internes ;
3. un scraper prematch Tennis Explorer pour les matchs/cotes du jour et du lendemain ;
4. un enrichissement joueur via profils Tennis Explorer et moteur de stats ;
5. un modèle ML XGBoost calibré, exporté en bundle `models/xgb_model_tml_v47.pkl` ;
6. un Live Tracker qui calcule probas, cotes justes, EV, score composite et mises ;
7. un portefeuille qui stocke les paris réels, les opportunités algo et les rapports historiques.

Le principe important : le Live Tracker ne recalcule pas tout à chaque rerun Streamlit. Il lit un snapshot disque enrichi quand il existe, puis reconstruit seulement lorsque la signature CSV/modèle/cache l'impose.

## 2. Fichiers clés

`app/dashboard.py`

- UI Streamlit (ordre onglets) : **Paris du jour** (Top 5 hybride prod — mêmes 5 picks que TG `/top5`), **Mon Portefeuille**, **Live Tracker**, **Top probas jour** (top 15 + chart — `docs/CHART_TOP_PROBAS_JOUR.md`), Backtest Kelly, Diagnostics, Tracking modèle, **Paramètres** (ex-sidebar). Masqués : Pari Live, Human Factors.
- Orchestration du build live, des filtres, des value bets, des mises et du Report Opportunités.
- Calcul du bankroll disponible, saisie de la cote réelle, enregistrement des paris.
- Affichage des infos joueur : rang/points, ELO, forme, fatigue, style, signaux avancés.

`scripts/ml_model.py`

- Prépare le dataset ATP/WTA.
- Construit les features ML, dont micro-Elo service/return, ELO match réel, fatigue, style, météo, points à défendre, signaux WTA.
- Entraîne XGBoost + calibration duale BO3/BO5.
- Sauvegarde le bundle modèle, les ELO maps et les Brier par segment.

`scripts/micro_elo_engine.py`

- Calcule les micro-Elo service/return à partir des points de service.
- Maintient maintenant des alias par nom en plus des IDs joueurs, pour réduire les fallbacks à `1500`.

`scripts/refresh_elo_maps_fast.py`

- Script utilitaire rapide pour rafraîchir les cartes ELO sans réentraîner le classifieur.
- Copie les ratings micro-Elo ID vers alias nom.
- Reconstruit l'ELO match winner/loser.
- Ne remplace pas un vrai retrain : il sert surtout de réparation rapide de couverture ELO.

`scripts/live_snapshot.py`

- Persiste les snapshots live dans `data/cache/live_matches_snapshot.joblib`.
- Archive le dernier snapshot full dans `data/cache/live_matches_snapshot.full.joblib`.
- Archive le J+1 dans `data/cache/live_matches_nextday.full.joblib`.
- Gère les métadonnées, signatures, lock de build et cache RAM.

`scripts/full_live_benchmark.py`

- Pipeline full headless : scrape prematch jour + demain, refresh profils TE, rebuild snapshot full.
- À utiliser pour préparer complètement la journée suivante.

`scripts/bets_db.py`

- Schémas et helpers SQLite pour `user_bets`, bankroll, résultats, CLV, opportunités algo.
- Contient la simulation de performance théorique du Report Opportunités.

## 3. Pipeline Live Tracker

### 3.1 Scrape prematch

`scripts/scraper_prematch.py` récupère les matchs Tennis Explorer du jour et du lendemain (`type=all`). Le CSV produit est stocké dans `data/scraped/prematch_odds_*.csv`.

Le CSV contient notamment :

- date et heure ;
- tournoi, **category** (`ATP`, `WTA`, `Challenger`, `ITF`, …), surface inférée ;
- **`tournament_url`** (lien fiche TE) ;
- **`tourney_winner_points`** (points vainqueur — 125 = Challenger/WTA 125, 250+ = main draw) ;
- joueur 1 / joueur 2 ;
- cotes prematch ;
- URLs profils TE ;
- identifiants prematch quand disponibles.

Voir **`docs/CHALLENGERS_ET_TOURNOIS.md`** pour le filtrage main draw vs Challengers.

### 3.2 Snapshot deux niveaux

Le Live utilise deux modes :

- `preview` : affichage rapide, utilise au maximum les caches existants ;
- `full` : enrichissement complet, profils TE, stats, features avancées, prédictions ML.

Le snapshot full est préféré dès qu'il existe. Le snapshot J+1 permet de précharger demain et de promouvoir ces données le lendemain.

Signature snapshot :

- chemin CSV ;
- mtime CSV ;
- version du schéma profil ;
- mtime du bundle modèle ;
- version moteur `_ENGINES_CACHE_VERSION`.

Un changement de modèle ou de version moteur invalide donc le snapshot.

### 3.3 Filtres tournoi et qualité

**Tournois** (`scripts/tournament_tier.py`) :

- **Build live** : conserve ATP, WTA et **`Challenger`** (exclut ITF/UTR par nom) ;
- **Live Tracker (défaut)** : main draw uniquement (nom + points ≥ 250 si connus) ;
- **Toggle « Inclure les Challengers »** : challenger tier (ATP Challenger, WTA 125, `category=Challenger`) ;
- **Paris du jour / Top 5 / `/top5`** : main draw uniquement — sélection **hybride** (P≥77 %, rel≥75, gap≤30 pp, EV tier1/tier2, tri EV ↓) : voir [[HYBRID_PICK_SELECTION]].

**Qualité données** — le Live masque les matchs sans source rang/points exploitable pour les deux joueurs, ou dont la référence TML/WTA est **périmée** (> 12 mois par défaut).

Module partagé : `scripts/match_rank_quality.py` (Streamlit PROD, API PREPROD React via `filter_matches_for_daily_top_proba` / `live_tracker_picks`).

Les sources reconnues incluent notamment :

- `matches_recent` pour ATP ;
- `wta_matches` pour WTA ;
- `rankings_wta_current` en repli WTA ;
- `tennisexplorer_estimate` seulement quand autorisé par le flux (sinon exclus de l’UI).

**Fraîcheur** : chaque joueur doit avoir `stats_reference_date` ≤ `BETTINGHUD_STALE_RANK_STATS_MAX_DAYS` (défaut **365**) par rapport à la date du match. Sinon le match est masqué (caption dédiée dans le Live Tracker).

Le compteur `Profils TE complets` mesure autre chose : il indique si les profils Tennis Explorer ont été chargés pour les deux joueurs. Un match peut donc avoir rang/points valides même si un ancien snapshot avait encore des flags TE incomplets. Dans ce cas, il faut reconstruire un vrai snapshot full, pas seulement patcher les cotes.

## 4. Modèle ML courant

Bundle courant : `models/xgb_model_tml_v47.pkl`.

Le modèle est un XGBoost entraîné sur un dataset orienté P1/P2 :

- chaque match historique produit deux lignes ;
- la cible indique si P1 gagne ;
- les features sont strictement pré-match ;
- les calibrateurs BO3 et BO5 sont séparés.

### 4.1 Calibration BO3/BO5

Le modèle base XGBoost est calibré par isotonic regression avec deux branches :

- BO3 : majorité des matchs ATP/WTA ;
- BO5 : ATP Grand Chelem et contextes historiques détectés.

À l'inférence, `predict_proba_calibrated_routed()` choisit la branche selon `bo5_mask_from_features()`.

### 4.2 ELO actuels

Il existe désormais trois familles distinctes :

1. `Elo global` historique affiché : agrégat micro-Elo `(service + return) / 2`. Il reste proche de `1500` par construction et ne doit pas être lu comme un vrai Elo match.
2. `Elo service` / `Elo retour` : micro-Elo calculés sur les points de service/retour, avec piste globale et piste surface.
3. `Elo match réel` : nouveau Elo winner/loser classique, séparé du micro-Elo, avec version globale et version surface.

Le retrain du 18 mai 2026 inclut les nouvelles features :

- `match_elo_diff` ;
- `surface_match_elo_diff`.

Ces deux features sont maintenant réellement utilisées par XGBoost, pas seulement affichées.

### 4.3 Résultats du retrain du 18 mai 2026

Commande utilisée :

```powershell
python scripts/update_model_tml.py --skip-sync --min-year 2020
```

Données :

- lignes ATP TennisMyLife : `17 353` ;
- lignes WTA Sackmann : `182 971` ;
- lignes nettoyées : `126 258` ;
- micro-Elo service/return utilisés : `60 808` matchs ;
- dataset supervisé : `66 850` exemples ;
- test temporel : `13 370` exemples.

Performance :

- accuracy test : `0.7243` ;
- Brier global test : `0.1797`.

Brier par segment :

| Segment | Brier | n |
|---|---:|---:|
| `global_isotonic` | `0.1797` | `13370` |
| `ATP_Clay` | `0.1902` | `2510` |
| `ATP_Clay_G` | `0.1924` | `254` |
| `ATP_Clay_M` | `0.2020` | `976` |
| `ATP_Grass` | `0.1854` | `594` |
| `ATP_Grass_G` | `0.1832` | `254` |
| `ATP_Hard` | `0.1867` | `5527` |
| `ATP_Hard_G` | `0.1508` | `756` |
| `ATP_Hard_M` | `0.1749` | `1604` |
| `WTA_Clay` | `0.1494` | `1250` |
| `WTA_Clay_M` | `0.1288` | `452` |
| `WTA_Hard` | `0.1700` | `3093` |
| `WTA_Hard_G` | `0.1349` | `422` |
| `surf_Clay` | `0.1767` | `3760` |
| `surf_Grass` | `0.1807` | `974` |
| `surf_Hard` | `0.1807` | `8620` |
| `tour_ATP` | `0.1876` | `8631` |
| `tour_WTA` | `0.1652` | `4739` |

Top features du retrain :

- `minutes_played_last7d_diff` ;
- `points_diff` ;
- `surface_match_elo_diff` ;
- `match_elo_diff` ;
- `rank_diff` ;
- `style_matchup_bias` ;
- `service_elo_diff`.

Conclusion : le nouvel Elo match réel est bien intégré au modèle. `surface_match_elo_diff` et `match_elo_diff` sont respectivement 3e et 4e features les plus importantes sur ce run.

## 5. Value bets et score composite

Pour chaque côté :

1. le modèle renvoie une cote juste `true_odd` ;
2. la probabilité modèle est `p_model = 1 / true_odd` ;
3. la proba implicite bookmaker est `p_implicit = 1 / odd_book` ;
4. l'EV est `p_model * odd_book - 1`.

`scripts/value_detector.py` applique un seuil EV minimal et tient compte de la confiance.

Le tri par défaut n'est pas l'EV brute. Le Live utilise le score composite :

```text
priority_score = (Sharpe_unitaire / Brier_segment) * (1 - Brier_segment / 0.25)
```

Implémentation : `scripts/priority_scoring.py`.

Intuition :

- une value très volatile mais mal calibrée est pénalisée ;
- une value moins spectaculaire mais mieux calibrée peut passer devant ;
- le score composite sert au tri Live et au Report Opportunités théorique.

### 5.1 Mode « Top 15 · EV favori » (toggle UI)

Toggle partagé entre **Live Tracker** et **Top probas jour** (`docs/CHART_TOP_PROBAS_JOUR.md`).

Quand activé sur le Live Tracker :

1. les matchs affichés passent le filtre **EV favori** entre **+15 %** et **+100 %** (`EV = p_fav × cote_fav − 1`, favori = côté `max(p1, 1−p1)`) ;
2. les value bets retenues sont celles du **côté favori modèle** uniquement ;
3. tri par **proba favori modèle** décroissante (aligné sur l’onglet Top probas) ;
4. plafond **15 tuiles** (`TOP_PROBAS_DISPLAY_LIMIT`).

Quand désactivé : tri Live habituel (Composite / Sharpe / EV brute).

> Distinction : la campagne backtest **top 10/jour** (`docs/BACKTEST_TOP10_PROBA_SIMULATIONS.md`) reste une simulation historique à 10 paris/jour ; l’UI dashboard affiche **15** lignes depuis le snapshot live.

## 6. Fonctionnement des mises Live

### 6.1 Bankroll utilisée

La bankroll Live Tracker part de :

- bankroll de départ Live ;
- profits/pertes des paris enregistrés ;
- ajustements manuels éventuels ;
- mises déjà engagées sur paris en cours.

Le dashboard calcule une bankroll disponible avant de proposer une mise.

### 6.2 Cote utilisée

La mise recommandée n'utilise pas obligatoirement la cote détectée par le scraper.

Dans le Live, l'utilisateur peut saisir `custom_odd`. Cette cote réelle saisie sert à :

- recalculer l'EV réelle du pari ;
- recalculer la fraction Kelly ;
- enregistrer le pari dans `user_bets.odds`.

C'est important : le portefeuille et le Report Opportunités doivent distinguer `odd_book` détectée et `real_odd` réellement prise.

### 6.3 Kelly utilisé

Pour une cote réelle `o` et une proba modèle `p` :

```text
b = o - 1
kelly_full = (b*p - (1-p)) / b
```

Puis :

```text
kelly_fraction = 0.5 * kelly_full
```

Cette fraction est ensuite ajustée par le Brier du segment :

```text
brier_factor = max(0, 1 - segment_brier / 0.25)
stake_frac = 0.5 * kelly_full * brier_factor
```

Enfin, la mise est plafonnée :

```text
stake_frac <= 15% de bankroll
stake_eur <= bankroll disponible
```

Le résultat est la mise recommandée en euros.

### 6.4 Pourquoi indexer par Brier

Le Brier mesure la qualité historique de calibration d'un segment.

Exemple :

- segment bien calibré `Brier = 0.13` → facteur `1 - 0.13/0.25 = 0.48` ;
- segment plus fragile `Brier = 0.20` → facteur `0.20` ;
- segment mauvais `Brier >= 0.25` → mise théorique tend vers `0`.

Le but est d'éviter de miser trop fort sur des segments où les probabilités sont historiquement moins fiables.

## 7. Enregistrement des paris réels

Quand l'utilisateur clique sur le bouton de pari :

1. le match, le joueur choisi, la cote réelle, la mise, la proba modèle et les métadonnées sont enregistrés ;
2. les caches portefeuille sont invalidés ;
3. la bankroll disponible est recalculée ;
4. le pari peut ensuite être résolu via résultats ou saisie manuelle.

Les champs clés côté base :

- `user_bets.odds` : cote réelle saisie ;
- `user_bets.stake` : mise réelle ;
- `user_bets.profit` : P/L réel une fois résolu ;
- `user_bets.status` : `En cours`, `Gagné`, `Perdu`, `Annulé`.

## 8. Report Opportunités

Le Report Opportunités stocke les value bets détectés, même si l'utilisateur ne les joue pas.

Table : `algo_opportunities`.

Objectifs :

- conserver l'historique des opportunités détectées ;
- comparer performance théorique et performance réelle ;
- consulter une journée passée ou une période.

### 8.1 Performance réelle

La performance réelle inclut uniquement les paris effectivement placés.

Elle utilise :

- `real_odd` = cote réellement saisie par l'utilisateur ;
- `real_stake` = mise réelle ;
- `real_profit` = profit réel résolu.

Elle ne repose donc pas sur la cote bookmaker détectée si l'utilisateur a pris une cote différente.

### 8.2 Performance théorique

La performance théorique simule ce que l'algorithme aurait fait en jouant toutes les opportunités détectées.

Elle utilise :

- le même Kelly 1/2 ;
- le même plafond 15% ;
- le même facteur Brier ;
- le tri par `priority_score`.

La simulation est ordonnée par jour :

1. on trie les opportunités de la journée par composite décroissant ;
2. on calcule chaque mise sur la bankroll théorique du matin ;
3. on consomme la liquidité disponible au fil des opportunités ;
4. on applique les résultats quand ils sont connus ;
5. la bankroll théorique de fin de journée devient la bankroll de départ du lendemain.

C'est donc une trajectoire de capital, pas une simple addition de mises unitaires.

### 8.3 Capital de départ théorique

La simulation démarre sur la bankroll Live Tracker réelle courante.

Ensuite, pour les jours suivants :

```text
BR matin J+1 = BR soir J
```

Ce comportement évite de redémarrer artificiellement chaque jour avec `1U`.

## 9. Préparation complète de demain

Commande recommandée :

```powershell
python scripts/full_live_benchmark.py
```

Ce pipeline :

1. supprime les anciens locks/progressions ;
2. purge les caches joueurs live ;
3. scrape Tennis Explorer pour aujourd'hui et demain ;
4. force/relit les profils TE ;
5. reconstruit le snapshot full ;
6. archive automatiquement le J+1 dans `live_matches_nextday.full.joblib`.

Après un retrain modèle, il faut reconstruire le snapshot car les cotes justes et features dépendent du bundle.

## 10. Procédure après modification ML

Après tout changement de `self.features` :

1. lancer un retrain complet :

```powershell
python scripts/update_model_tml.py --skip-sync --min-year 2020
```

2. vérifier :

- Brier global ;
- Brier par segment ;
- importances features ;
- présence du nouveau champ dans `bundle["features"]`.

3. reconstruire le Live :

```powershell
python scripts/full_live_benchmark.py
```

4. redémarrer Streamlit :

```powershell
python -m streamlit run app/dashboard.py --server.port 8502 --server.address 127.0.0.1
```

5. vérifier :

- nombre de matchs full ;
- nombre de matchs J+1 ;
- couverture profils TE ;
- couverture ELO ;
- opportunités persistées dans le Report Opportunités.

## 11. Points d'attention

- Beaucoup de joueurs de qualifications/ITF peuvent rester à ELO `1500` s'ils sont absents des historiques ATP/WTA.
- Les alias par nom améliorent la couverture quand le joueur existe historiquement mais que l'ID Live manque.
- Un snapshot incrémental peut conserver d'anciens flags. Pour une vraie remise à plat, forcer un build `full`.
- Le Brier segment doit être lu comme qualité de calibration historique, pas comme certitude sur un match isolé.
- Le score composite doit rester prioritaire sur l'EV brute pour ordonner les mises théoriques.

## 12. Environnements PREPROD / PROD

| | PREPROD | PROD |
|---|---------|------|
| **Hôte** | PC local Windows | Serveur Ubuntu dédié |
| **Usage** | Dev, tests, retrain, backtests | Paris réels, snapshot du jour, daemon 24/7 |
| **Code** | Working copy locale | `/opt/bettinghud` + `git pull` |
| **Données** | `data/bettinghud.db` locale | Base référence production |

Référence : **`docs/ENVIRONNEMENTS.md`**.

Les paris réels, la bankroll Live Tracker et les caches **ne sont pas synchronisés** automatiquement entre PREPROD et PROD — uniquement le **code** via Git.

## 13. Déploiement serveur (production)

Référence complète : **`docs/DEPLOY_SERVEUR.md`**. Dépannage et incidents : **`docs/OPS_PROD_DEPANNAGE.md`**.

| Composant | Emplacement / commande |
|-----------|-------------------------|
| Code | `/opt/bettinghud` (clone `https://github.com/Miou-ux/BettingHUD`) |
| Dashboard | `systemctl` → `bettinghud-dashboard` (Streamlit `:8501` localhost) |
| Daemon | `systemctl` → `bettinghud-daemon` (`portfolio_results_daemon`) |
| Bot Telegram | `systemctl` → `bettinghud-telegram-bot` — `/jour`, `/top5` — voir **`docs/TELEGRAM_TOP5.md`** |
| Web public | nginx port **80** → proxy vers Streamlit |
| Pipeline matin | cron **02:00 Europe/Paris** → `morning_live_pipeline.py` (+ Top 5 si `TELEGRAM_TOP5_AFTER_MORNING=1`) |
| Mise à jour code | `git pull` + `systemctl restart bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot` |

Les données runtime (`data/bettinghud.db`, `models/*.pkl`, caches) restent sur le serveur et ne sont pas versionnées dans Git.
