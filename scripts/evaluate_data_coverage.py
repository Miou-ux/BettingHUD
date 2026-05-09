"""Aperçu couverture données (SQLite) — à lancer après ingest / sync."""
import argparse
import os
import sqlite3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=os.path.join("data", "bettinghud.db"))
    args = ap.parse_args()
    if not os.path.isfile(args.db_path):
        print("Base absente:", args.db_path)
        return

    conn = sqlite3.connect(args.db_path)
    cur = conn.cursor()

    def cnt(table: str) -> str:
        try:
            n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            return str(n)
        except sqlite3.OperationalError:
            return "(absent)"

    print("matches_recent (TML / ATP) ", cnt("matches_recent"))
    print("wta_matches    (Sackmann)  ", cnt("wta_matches"))
    print("rankings_wta_current        ", cnt("rankings_wta_current"))
    conn.close()
    print(
        "\nRecommandations :\n"
        "  - ATP : python scripts/sync_tml_recent.py (ou update_model_tml.py)\n"
        "  - WTA : python scripts/pipeline_quality.py (ingest_sackmann_wta + rankings)\n"
    )


if __name__ == "__main__":
    main()
