# Hybride prod — HYB P75+P80-all

Sélection **Top 5**, **Telegram**, **dashboard**, **1D1P** (juillet 2026).

## Règle

1. **Base P75-TIER** (max **6** picks/jour)  
   - p ≥ **73 %**, rel ≥ **80**, EV **6–55 %**, gap ≤ **35 pp**, Kelly ≥ **2 %**  
   - tier fill (EV **15–35 %** d’abord) · tri score · BGF off  

2. **Compléments P80** (sans plafond)  
   - p ≥ **80 %**, rel ≥ **80**, **sans filtre EV**  
   - matchs **non déjà** pris par P75-TIER  

3. **Union** dédoublonnée, tri **proba modèle ↓**, rang 1…N  

## 1 Day 1 Pick

Meilleur pick = **proba fav max** dans l’union (`best_1d1p_pick_from_hyb`), pas le premier rang de la liste.

## Code

| Fichier | Rôle |
|---------|------|
| `scripts/hyb_p75_p80_selection.py` | Logique P75+P80 |
| `scripts/hybrid_pick_selection.py` | `select_hybrid_picks()` → prod |
| `scripts/hybrid_pick_selection.py` | `select_hybrid_picks_legacy()` → ancien P77 |
| `scripts/daily_top_proba_store.py` | `collect_hybrid_proba_picks()` |
| `scripts/discord_1d1p_core.py` | `load_1d1p_today_pick()` |

## Legacy (backtests comparatifs)

Ancienne règle P77 · rel≥85 (repli 80) · EV tier1 15–35 % + tier2 30–55 % · max 6 :

```python
from scripts.hybrid_pick_selection import select_hybrid_picks_legacy
```
