"""Supprime les tables Sackmann ATP héritées qui ne sont plus utilisées.

Tables ciblées (DROP IF EXISTS) :
  - matches              (Sackmann ATP, remplacée par matches_recent / TML)
  - players              (Sackmann ATP, plus de chemin déterministe ATP via cette table)
  - rankings_atp_current (Sackmann ATP, plus consulté côté stats_engine)

À lancer une seule fois après vérification que le dashboard fonctionne sans elles :

  python scripts/purge_sackmann_atp.py --db-path data/bettinghud.db
"""
import argparse
import sqlite3


TABLES = ("matches", "players", "rankings_atp_current")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default="data/bettinghud.db")
    p.add_argument("--dry-run", action="store_true", help="N'effectue aucun DROP, affiche seulement.")
    args = p.parse_args()

    conn = sqlite3.connect(args.db_path, timeout=120.0)
    try:
        cur = conn.cursor()
        existing = {
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for t in TABLES:
            if t not in existing:
                print(f"  - {t} : absente")
                continue
            count = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if args.dry_run:
                print(f"  - {t} : présente ({count} lignes) — DRY RUN, pas de DROP")
                continue
            cur.execute(f"DROP TABLE IF EXISTS {t}")
            print(f"  - {t} : DROP ({count} lignes supprimées)")
        if not args.dry_run:
            conn.commit()
            cur.execute("VACUUM")
            print("VACUUM effectué.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
