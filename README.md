# BettingHUD (Tennis)

Un système d'analyse de paris sportifs pour le tennis, utilisant le scraping pour récupérer les données live/cotes, et combinant modèles statistiques et Machine Learning pour détecter des écarts de cotes (Value Bets).

## Pré-requis

- Python 3.9+
- Les navigateurs pour Playwright (gérés automatiquement ci-dessous)

## Installation

1. Cloner le repository ou récupérer le code
2. Créer un environnement virtuel et installer les dépendances :
   ```bash
   python -m venv venv
   # Sur Windows:
   venv\Scripts\activate
   # Sur macOS/Linux:
   source venv/bin/activate
   
   pip install -r requirements.txt
   playwright install
   ```

## Initialisation des données

Avant de pouvoir utiliser les moteurs de ML ou de statistiques, vous devez ingérer les données historiques (depuis le repo open-source de Jeff Sackmann) et entraîner le modèle initial.

```bash
# 1. Télécharge et prépare la base de données (data/bettinghud.db)
python scripts/ingest_atp_data.py

# 2. Entraîne le modèle Random Forest initial (crée models/rf_model_v1.pkl)
python scripts/ml_model.py
```

## Utilisation

Plusieurs scripts sont disponibles pour tester les différentes briques du projet :

- **Dashboard Interactif (MVP) :**
  ```bash
  streamlit run app/dashboard.py
  ```
  *(Ouvre une interface web sur http://localhost:8501)*

- **Démonstration dans le terminal :**
  ```bash
  python main.py
  ```

- **Scrapers :**
  ```bash
  # Scraper les cotes prematch du jour sur Flashscore
  python scripts/scraper_prematch.py
  
  # Scraper le score en direct (Placeholder - nécessite un ID de match valide)
  python scripts/scraper_live.py
  ```

## Structure du projet

- `app/` : Interface utilisateur (Streamlit)
- `data/` : Données brutes, base SQLite et exports (ignoré par git)
- `models/` : Modèles Machine Learning entraînés
- `scripts/` :
  - `ingest_atp_data.py` : ETL depuis Github vers SQLite
  - `scraper_prematch.py` : Scraper Playwright pour les matchs à venir
  - `scraper_live.py` : Scraper Playwright pour le score en direct
  - `stats_engine.py` : Moteur de probabilités contextuelles (ex: "4-5 au service")
  - `ml_model.py` : Modèle de prédiction global (Random Forest)
  - `value_detector.py` : Comparateur Cotes vs Vraie Cote (EV%)
