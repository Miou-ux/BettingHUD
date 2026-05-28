"""
Crée une archive ZIP « quasi totale » du projet BettingHUD + guide RESTAURATION.md.

Exclusions (régénérables ou bruit) : venv, __pycache__, *.pyc, caches outils.
Le dépôt .git et data/ (SQLite, cache, raw) sont inclus pour restauration fidèle.

Usage (depuis la racine du repo) :
  py -3 scripts/create_full_project_backup.py
  py -3 scripts/create_full_project_backup.py --out backups/custom.zip
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUPS = ROOT / "backups"

SKIP_DIR_NAMES = frozenset(
    {
        "venv",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
    }
)
SKIP_SUFFIXES = (".pyc", ".pyo")


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.endswith("__pycache__")


def _should_skip_file(path: Path) -> bool:
    if path.suffix.lower() in SKIP_SUFFIXES:
        return True
    return False


def build_restoration_markdown(archive_name: str) -> str:
    return f"""# BettingHUD — Restauration complète depuis la sauvegarde

**Archive :** `{archive_name}`  
**Généré le (UTC) :** {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")} UTC

Ce paquet vise à recréer l’application **BettingHUD** sur une autre machine (ou après perte de fichiers) avec le même code, les mêmes données locales, modèles et historique Git si présents dans l’archive.

---

## 1. Contenu typique de l’archive

| Zone | Rôle |
|------|------|
| `app/` | Dashboard Streamlit (`dashboard.py`) |
| `scripts/` | ML, sync, scrapers, backtests, utilitaires |
| `data/` | SQLite (`bettinghud.db`), CSV bruts, cache joueurs, logs, fichiers scrapés |
| `models/` | Bundles XGBoost (`xgb_model_tml_v*.pkl`) et PNG d’importance |
| `docs/` | Architecture, prédiction / mise |
| `requirements.txt` | Versions figées des paquets Python |
| `.git/` | Historique Git (si inclus dans la sauvegarde) |
| `README.md` | Vue d’ensemble du projet |

Le dossier **`venv/` n’est pas inclus** : il doit être recréé (voir §3).

---

## 2. Prérequis système

- **Windows 10/11**, **macOS** ou **Linux**.
- **Python 3.10+** (recommandé : **3.11** aligné avec les builds du projet).
- Espace disque suffisant (données brutes WTA/ATP + SQLite peuvent représenter **plusieurs Go**).
- Connexion Internet pour `pip install` et `playwright install` (première installation).

---

## 3. Restauration pas à pas

### 3.1 Décompresser

1. Créez un dossier cible, par ex. `C:\\Users\\Vous\\Documents\\BettingHUD` (Windows) ou `~/BettingHUD`.
2. Extrayez **tout** le contenu de l’archive dans ce dossier de façon à obtenir une racine contenant `app/`, `scripts/`, `data/`, `requirements.txt`, etc.

### 3.2 Environnement virtuel

Ouvrez un terminal **dans la racine du projet** (là où se trouve `requirements.txt`).

**Windows (PowerShell) :**

```powershell
py -3 -m venv venv
.\\venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**macOS / Linux :**

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3.3 Playwright (scrapers)

Les scrapers utilisent Playwright :

```bash
playwright install
```

### 3.4 Lecture des fichiers Excel (cotes tennis-data, backtest)

Si `pandas.read_excel` échoue, installez :

```bash
pip install openpyxl
```

### 3.5 Chemins et base SQLite

- La base par défaut est **`data/bettinghud.db`** (voir `TennisMLModel` dans `scripts/ml_model.py`).
- Après extraction, vérifiez que `data/bettinghud.db` existe ; sinon il faudra relancer les scripts d’ingestion (`sync_tml_recent.py`, `ingest_sackmann_wta.py`, etc. — voir `README.md` et `docs/ARCHITECTURE.md`).

### 3.6 Modèle ML

- Les bundles sont sous **`models/`** (ex. `xgb_model_tml_v47.pkl`).
- Le code référence le chemin par défaut dans `scripts/ml_model.py` (`self.model_path`).
- Si vous restaurez une version plus ancienne du code avec un nom de fichier `.pkl` différent, alignez le chemin ou ré-entraînez : `python scripts/update_model_tml.py --min-year 2010`.

### 3.7 Lancer l’application

```bash
streamlit run app/dashboard.py
```

Ou sous Windows si `python` n’est pas dans le PATH :

```powershell
py -3 -m streamlit run app/dashboard.py
```

URL locale habituelle : **http://localhost:8501**

### 3.8 Variables d’environnement (optionnel)

Le dashboard lit des variables `BETTINGHUD_*` (intervalles auto-sync, auto-ML, cache profils, etc.). Aucune n’est **obligatoire** pour un premier démarrage : les défauts sont dans `app/dashboard.py` (`os.getenv(...)`). Pour personnaliser, créez un fichier `.env` à la racine (si vous utilisez `python-dotenv` ailleurs) ou définissez les variables dans le système / le lanceur Streamlit.

Exemples de noms : `BETTINGHUD_AUTO_SYNC_INTERVAL_SEC`, `BETTINGHUD_AUTO_ML_TRAIN_INTERVAL_SEC`, `BETTINGHUD_IDENTITY_WORKERS`, etc.

---

## 4. Vérifications rapides après restauration

1. `python -c "from scripts.ml_model import TennisMLModel; m=TennisMLModel(); print(m.model_path, os.path.exists(m.model_path))"` — le fichier modèle doit exister.
2. `streamlit run app/dashboard.py` — pas d’erreur d’import.
3. Optionnel : `python scripts/backtest_2026.py --help` si vous utilisez le backtest.

---

## 5. Regénérer données / modèle depuis zéro (si archive incomplète)

Si `data/` ou `models/` manquent partiellement :

1. Lisez **`README.md`** (pipeline `update_model_tml.py`).
2. Lisez **`docs/ARCHITECTURE.md`** pour l’ordre des syncs et l’automation.

---

## 6. Restaurer l’historique Git

Si le dossier **`.git/`** est présent dans l’archive, après extraction vous pouvez continuer avec `git status`, `git log`, branches, etc. Sinon, initialisez un nouveau dépôt ou clonez depuis un remote si vous en avez un.

---

## 7. Support de cette sauvegarde

- Script utilisé pour générer l’archive : `scripts/create_full_project_backup.py` (s’il est présent dans l’archive).
- Pour refaire une sauvegarde identique sur la machine restaurée : `py -3 scripts/create_full_project_backup.py`

Bon retour en ligne.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out",
        type=str,
        default=None,
        help="Chemin du fichier .zip de sortie (défaut: backups/BettingHUD_Full_<timestamp>.zip)",
    )
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_name = f"BettingHUD_Full_{ts}.zip"
    out_path = Path(args.out) if args.out else (BACKUPS / default_name)
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    archive_basename = out_path.name
    readme = build_restoration_markdown(archive_basename)

    n_files = 0
    skipped = 0

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("RESTAURATION.md", readme.encode("utf-8"))
        n_files += 1

        for dirpath, dirnames, filenames in os.walk(ROOT):
            dp = Path(dirpath)
            # Ne pas descendre dans les dossiers exclus
            dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

            for fn in filenames:
                fp = dp / fn
                try:
                    rel = fp.relative_to(ROOT)
                except ValueError:
                    continue
                # Évite d’embarquer d’anciennes sauvegardes ZIP (taille + récursion).
                if len(rel.parts) >= 2 and rel.parts[0] == "backups" and fp.suffix.lower() == ".zip":
                    skipped += 1
                    continue
                try:
                    if fp.resolve() == out_path.resolve():
                        continue
                except OSError:
                    continue
                if _should_skip_file(fp):
                    skipped += 1
                    continue
                arc = fp.relative_to(ROOT).as_posix()
                try:
                    zf.write(fp, arcname=arc)
                    n_files += 1
                except OSError as e:
                    print(f"[WARN] skip {fp}: {e}", file=sys.stderr)
                    skipped += 1

    readme_sidecar = out_path.with_name(out_path.stem + "_RESTAURATION.md")
    readme_sidecar.write_text(readme, encoding="utf-8")

    print(f"Archive créée : {out_path}")
    print(f"Entrées ZIP   : {n_files} (fichiers + RESTAURATION.md)")
    print(f"Ignorés       : {skipped} (fichiers .pyc / erreurs)")
    print(f"Guide copié   : {readme_sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
