"""
Persistence. Two interchangeable backends behind one interface:

- SupabaseStore: production. Dedupes games, stores per-game analysis summaries
  and the coaching digests so trends can be tracked over time.
- LocalStore: zero-dependency JSON files. Lets you run a full backfill and dev
  loop without provisioning anything.

State we care about:
- which games we've already analyzed (dedupe by game_id)
- the latest end_time per user (incremental cursor)
- the analyzed-game summaries (for re-aggregation)
- the digests (longitudinal coaching log)
"""

from __future__ import annotations

import json
import os
from typing import Optional


class LocalStore:
    def __init__(self, state_dir: str = "./state"):
        self.dir = state_dir
        os.makedirs(self.dir, exist_ok=True)
        self.games_path = os.path.join(self.dir, "games.json")
        self.digests_path = os.path.join(self.dir, "digests.json")

    def _load(self, path):
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return {}

    def _save(self, path, data):
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def last_end_time(self, username: str) -> Optional[int]:
        games = self._load(self.games_path)
        times = [g["end_time"] for g in games.values()
                 if g.get("username") == username.lower() and g.get("end_time")]
        return max(times) if times else None

    def is_analyzed(self, game_id: str) -> bool:
        return game_id in self._load(self.games_path)

    def save_game(self, record: dict):
        games = self._load(self.games_path)
        games[record["game_id"]] = record
        self._save(self.games_path, games)

    def get_games(self, username: str, since_epoch: Optional[int] = None) -> list[dict]:
        games = self._load(self.games_path)
        out = [g for g in games.values() if g.get("username") == username.lower()]
        if since_epoch:
            out = [g for g in out if g.get("end_time", 0) > since_epoch]
        return out

    def save_digest(self, record: dict):
        digests = self._load(self.digests_path)
        digests.setdefault("items", []).append(record)
        self._save(self.digests_path, digests)

    def log_qa(self, record: dict):
        path = os.path.join(self.dir, "qa_log.json")
        data = self._load(path)
        data.setdefault("items", []).append(record)
        self._save(path, data)


class SupabaseStore:
    def __init__(self, url: str, key: str):
        from supabase import create_client
        self.sb = create_client(url, key)

    def last_end_time(self, username: str) -> Optional[int]:
        r = (self.sb.table("games")
             .select("end_time")
             .eq("username", username.lower())
             .order("end_time", desc=True).limit(1).execute())
        return r.data[0]["end_time"] if r.data else None

    def is_analyzed(self, game_id: str) -> bool:
        r = self.sb.table("games").select("game_id").eq("game_id", game_id).limit(1).execute()
        return bool(r.data)

    def save_game(self, record: dict):
        self.sb.table("games").upsert(record, on_conflict="game_id").execute()

    def get_games(self, username: str, since_epoch: Optional[int] = None) -> list[dict]:
        q = self.sb.table("games").select("*").eq("username", username.lower())
        if since_epoch:
            q = q.gt("end_time", since_epoch)
        return q.execute().data

    def save_digest(self, record: dict):
        self.sb.table("digests").insert(record).execute()

    def log_qa(self, record: dict):
        self.sb.table("qa_log").insert(record).execute()


def get_store(cfg) -> object:
    if cfg.use_supabase:
        return SupabaseStore(cfg.supabase_url, cfg.supabase_key)
    return LocalStore(cfg.local_state_dir)
