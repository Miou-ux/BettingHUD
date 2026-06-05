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

| `CourtAlpha/docs/CHANGELOG.md` | Historique Web (rebrand, logo, EV, typo, tuiles) |
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



## Bascule prod (futur)



Non activée. Quand prête :



- nginx `/` → build React

- nginx `/api` → FastAPI

- nginx `/legacy` → Streamlit (temporaire)



Voir `CourtAlpha/docs/ARCHITECTURE.md` phase 5.



## Sauvegardes



Avant tout déploiement ou gros changement sur le moteur partagé :



- `scripts/backup_prod_db_to_local.ps1`

- `backups/prod/` (DB + archive tar full)



Dernière sauvegarde complète documentée : **2026-06-05** (`bettinghud_prod_20260605_090505.db`, `bettinghud_prod_full_20260605_070605.tar.gz`).

