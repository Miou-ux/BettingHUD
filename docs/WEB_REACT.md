# Interface React — CourtAlpha



Projet **séparé** pour la future UI React, sans modifier Streamlit en prod.



## Emplacement



| Projet | Chemin | Rôle |

|--------|--------|------|

| **BettingHUD** | `O:\Miouppy\Documents\BettingHUD\` | Moteur, Streamlit, daemons, Telegram, PROD |

| **CourtAlpha** | `O:\Miouppy\Documents\CourtAlpha\` | React + FastAPI (PREPROD) |



## Documentation CourtAlpha



Toute la doc du front React vit dans le dépôt frère :



| Fichier | Contenu |

|---------|---------|

| `CourtAlpha/docs/README.md` | Index |

| `CourtAlpha/docs/GETTING_STARTED.md` | Lancement local |

| `CourtAlpha/docs/ARCHITECTURE.md` | Architecture, phases |

| `CourtAlpha/docs/UI_DESIGN.md` | Design CourtAlpha |

| `CourtAlpha/docs/PAGE_MAP.md` | Parité des 8 onglets Streamlit |

| `CourtAlpha/docs/API.md` | Endpoints REST |

| `CourtAlpha/docs/CHANGELOG.md` | Historique Web (rebrand, logo, EV, typo, tuiles, **1 Day 1 Pick**) |
| `CourtAlpha/docs/UI_DESIGN.md` | Charte couleurs, paliers EV, composants, interactions |

| `CourtAlpha/AGENTS.md` | Règle : documenter chaque changement |



## Lien technique



- Variable `BETTINGHUD_ROOT` → racine de **ce** dépôt (`BettingHUD/`)

- L'API Web importe `scripts/`, lit `data/cache/*.joblib` et `data/bettinghud.db`

- **Phase 1** : lecture seule — pas d'impact sur la prod



## Lancement rapide (PREPROD)



```powershell

# API

cd O:\Miouppy\Documents\CourtAlpha

O:\Miouppy\Documents\BettingHUD\venv\Scripts\python.exe -m uvicorn api.main:app --reload --port 8000



# React

cd O:\Miouppy\Documents\CourtAlpha\frontend

npm run dev

```



Streamlit reste disponible en parallèle : `streamlit run app/dashboard.py`.



## Pages CourtAlpha (PROD)

| Tier | Routes |
|------|--------|
| **Public** | `/1-day-1-pick`, `/pricing`, `/methodo`, `/login` |
| **Gratuit** (compte) | `/portfolio`, `/profile` |
| **Premium** | `/live`, `/paris`, `/top5`, `/top-probas` |

Admin (owner/admin) : `/backtest`, `/tracking`, `/frequentation`, `/settings`.

**Paris / Top 5 / Live** : `BetModal` — cote observée éditable, **EV + mise Kelly** recalculées à la saisie. Tuiles : badge **« Déjà X € »** si pari existant (`existing_stake_eur` API). Live : cote éditée sur `ValueBetCard`.

---

## Bascule prod

Activée sur **https://courtalpha.tech/** :

- nginx `/` → build React (`frontend/dist/`)
- nginx `/api` → FastAPI (`courtalpha-api.service`)
- Streamlit legacy : `https://admin.courtalpha.tech/`



## Sauvegardes



Avant tout déploiement ou gros changement sur le moteur partagé :



- `scripts/backup_prod_db_to_local.ps1`

- `backups/prod/` (DB + archive tar full)



Dernière sauvegarde complète documentée : **2026-06-05** (`bettinghud_prod_20260605_090505.db`, `bettinghud_prod_full_20260605_070605.tar.gz`).

