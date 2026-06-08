"""Configuration via environment variables. Import `cfg`."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    # required
    chesscom_username: str = os.environ.get("CHESSCOM_USERNAME", "")

    # engine
    stockfish_path: str = os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish")
    analysis_depth: int = int(os.environ.get("ANALYSIS_DEPTH", "12"))
    engine_threads: int = int(os.environ.get("ENGINE_THREADS", "2"))
    engine_hash_mb: int = int(os.environ.get("ENGINE_HASH_MB", "128"))

    # coaching model
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    coach_model: str = os.environ.get("COACH_MODEL", "claude-sonnet-4-6")

    # storage (optional; falls back to local JSON if unset)
    supabase_url: str = os.environ.get("SUPABASE_URL", "")
    supabase_key: str = os.environ.get("SUPABASE_KEY", "")
    local_state_dir: str = os.environ.get("LOCAL_STATE_DIR", "./state")

    # delivery (all optional)
    notion_api_key: str = os.environ.get("NOTION_API_KEY", "")
    notion_database_id: str = os.environ.get("NOTION_DATABASE_ID", "")
    slack_webhook_url: str = os.environ.get("SLACK_WEBHOOK_URL", "")
    slack_signing_secret: str = os.environ.get("SLACK_SIGNING_SECRET", "")
    slack_bot_token: str = os.environ.get("SLACK_BOT_TOKEN", "")

    # run behavior
    rated_only: bool = os.environ.get("RATED_ONLY", "true").lower() == "true"
    # backfill window: number of months back to analyze (0 / unset = full history)
    backfill_months: int = int(os.environ.get("BACKFILL_MONTHS", "0"))
    # restrict to time classes, comma-separated e.g. "rapid" or "rapid,blitz" (unset = all)
    _time_classes_raw: str = os.environ.get("TIME_CLASSES", "")

    @property
    def time_classes(self):
        vals = {t.strip().lower() for t in self._time_classes_raw.split(",") if t.strip()}
        return vals or None

    @property
    def use_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)


cfg = Config()
