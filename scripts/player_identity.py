import re
import sqlite3
from typing import Optional, Dict

import pandas as pd


def canonical_name(name: str) -> str:
    n = str(name or "").lower().strip()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n)
    parts = [p for p in n.split(" ") if p]
    if not parts:
        return ""
    if len(parts) == 2 and len(parts[1]) == 1:
        return f"{parts[0]} {parts[1]}"
    last = parts[-2] if len(parts) >= 2 and len(parts[-1]) == 1 else parts[-1]
    ini = parts[-1][0] if parts[-1] else ""
    if len(parts) >= 2 and len(parts[-1]) == 1:
        ini = parts[-1]
    return f"{last} {ini}".strip()


class PlayerIdentityResolver:
    def __init__(self, db_path: str = "data/bettinghud.db"):
        self.db_path = db_path
        self._ensure_alias_table()

    def _ensure_alias_table(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS player_aliases (
                alias TEXT PRIMARY KEY,
                canonical_name TEXT,
                player_id TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def upsert_alias(self, alias: str, canonical: str, player_id: Optional[str]):
        if not alias:
            return
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO player_aliases(alias, canonical_name, player_id)
            VALUES(?,?,?)
            ON CONFLICT(alias) DO UPDATE SET
              canonical_name=excluded.canonical_name,
              player_id=excluded.player_id
            """,
            (alias, canonical, None if player_id is None else str(player_id)),
        )
        conn.commit()
        conn.close()

    def resolve_player_id(self, raw_name: str, players_df: pd.DataFrame) -> Optional[str]:
        alias = canonical_name(raw_name)
        if not alias:
            return None
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT player_id FROM player_aliases WHERE alias=?", (alias,)).fetchone()
        conn.close()
        if row and row[0]:
            return str(row[0])

        # deterministic fallback against players table
        parts = str(raw_name or "").strip().split()
        if len(parts) >= 2:
            first = parts[0]
            last = " ".join(parts[1:])
            exact = players_df[(players_df["name_first"] == first) & (players_df["name_last"] == last)]
            if not exact.empty:
                pid = str(exact.iloc[0]["player_id"])
                self.upsert_alias(alias, alias, pid)
                return pid
            if last.endswith("."):
                first_letter = last[0]
                last_part = first
                partial = players_df[
                    players_df["name_last"].str.contains(last_part, case=False, na=False)
                    & players_df["name_first"].str.startswith(first_letter, na=False)
                ]
                if len(partial) == 1:
                    pid = str(partial.iloc[0]["player_id"])
                    self.upsert_alias(alias, alias, pid)
                    return pid

        self.upsert_alias(alias, alias, None)
        return None

    def audit_collisions(self) -> Dict[str, pd.DataFrame]:
        conn = sqlite3.connect(self.db_path)
        aliases = pd.read_sql("SELECT * FROM player_aliases", conn)
        conn.close()
        if aliases.empty:
            return {"collisions": pd.DataFrame(columns=["alias", "n_ids"])}
        g = aliases.dropna(subset=["player_id"]).groupby("alias", as_index=False)["player_id"].nunique()
        collisions = g[g["player_id"] > 1].rename(columns={"player_id": "n_ids"})
        return {"collisions": collisions}

