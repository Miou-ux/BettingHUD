from datetime import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.sync_tml_recent import sync_years
from scripts.ml_model import TennisMLModel


def update_model(min_year=2010):
    current_year = datetime.utcnow().year
    print(f"[TML] Sync {min_year}-{current_year} ...")
    sync_years(min_year=min_year, max_year=current_year)

    print("[ML] Training model from TennisMyLife dataset ...")
    ml = TennisMLModel()
    ml.train()
    print("[DONE] Model refreshed.")


if __name__ == "__main__":
    update_model(min_year=2010)
