"""
chess.com Published-Data API client.

Read-only, no auth for public profiles. Two endpoints matter:
  /pub/player/{user}/games/archives        -> list of monthly archive URLs
  /pub/player/{user}/games/{YYYY}/{MM}     -> JSON: {"games": [ ... ]}

Notes that bite you in production and are handled here:
- Cloudflare 403s requests without a descriptive User-Agent.
- Hit it SERIALLY. Parallel requests get throttled.
- Back off on 429.
- Filter to standard chess ("rules": "chess") only; skip variants.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Iterator, Optional

import requests

BASE = "https://api.chess.com/pub"

# chess.com asks for a contact in the UA. Put a real one here.
USER_AGENT = "chess-coach-pipeline/1.0 (contact: you@example.com)"

DRAW_RESULTS = {
    "stalemate", "agreed", "repetition", "insufficient",
    "50move", "timevsinsufficient",
}


def _get(url: str, accept_pgn: bool = False, max_retries: int = 5):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/x-chess-pgn" if accept_pgn else "application/json",
    }
    for attempt in range(max_retries):
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp
        if resp.status_code == 429:
            wait = 2 ** attempt
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    resp.raise_for_status()


def get_archive_urls(username: str) -> list[str]:
    url = f"{BASE}/player/{username.lower()}/games/archives"
    resp = _get(url)
    if resp is None:
        raise ValueError(f"chess.com user not found: {username}")
    return resp.json().get("archives", [])


def _color_and_result(game: dict, username: str) -> Optional[tuple[str, str]]:
    u = username.lower()
    white = game.get("white", {})
    black = game.get("black", {})
    if white.get("username", "").lower() == u:
        color, raw = "white", white.get("result")
    elif black.get("username", "").lower() == u:
        color, raw = "black", black.get("result")
    else:
        return None
    if raw == "win":
        result = "win"
    elif raw in DRAW_RESULTS:
        result = "draw"
    else:
        result = "loss"
    return color, result


def get_recent_games(username: str, n: int = 10, rated_only: bool = True) -> list[dict]:
    """Newest `n` standard games, walking archives newest-first. For sanity checks."""
    archives = get_archive_urls(username)
    out: list[dict] = []
    for archive_url in reversed(archives):
        resp = _get(archive_url)
        time.sleep(0.6)
        if resp is None:
            continue
        games = resp.json().get("games", [])
        for game in reversed(games):  # newest first within the month
            if standard_only_ok(game, rated_only):
                cr = _color_and_result(game, username)
                if cr is None or not game.get("pgn"):
                    continue
                color, result = cr
                out.append({
                    "game_id": game.get("uuid") or game.get("url", "").rsplit("/", 1)[-1],
                    "url": game.get("url"), "pgn": game.get("pgn"),
                    "end_time": game.get("end_time", 0),
                    "time_class": game.get("time_class"),
                    "time_control": game.get("time_control"),
                    "rated": game.get("rated", False),
                    "user_color": color, "user_result": result,
                    "white_rating": game.get("white", {}).get("rating"),
                    "black_rating": game.get("black", {}).get("rating"),
                    "user_rating": game.get(color, {}).get("rating"),
                })
                if len(out) >= n:
                    return out
    return out


def standard_only_ok(game: dict, rated_only: bool) -> bool:
    if game.get("rules") != "chess":
        return False
    if rated_only and not game.get("rated", False):
        return False
    return True


def _archive_month_before_cutoff(archive_url: str, since_epoch: int) -> bool:
    """True if the entire YYYY/MM archive ends before the cutoff (skip fetching)."""
    try:
        parts = archive_url.rstrip("/").split("/")
        year, month = int(parts[-2]), int(parts[-1])
    except (ValueError, IndexError):
        return False
    # first day of the *next* month, UTC; if that's still <= cutoff, skip
    nm_year, nm_month = (year + 1, 1) if month == 12 else (year, month + 1)
    next_month_start = dt.datetime(nm_year, nm_month, 1, tzinfo=dt.timezone.utc).timestamp()
    return next_month_start <= since_epoch


def iter_games(
    username: str,
    since_epoch: Optional[int] = None,
    rated_only: bool = True,
    standard_only: bool = True,
    time_classes: Optional[set] = None,
    request_delay: float = 0.6,
) -> Iterator[dict]:
    """
    Yield normalized game records (oldest first).

    Filters:
      since_epoch   only games ending after this (also skips whole old archives)
      time_classes  e.g. {"rapid"} or {"rapid","blitz"}; None = all

    Each record:
      {game_id, url, pgn, end_time, time_class, time_control, rated,
       user_color, user_result, white_rating, black_rating, user_rating}
    """
    archives = get_archive_urls(username)
    for archive_url in archives:
        if since_epoch and _archive_month_before_cutoff(archive_url, since_epoch):
            continue  # don't even fetch months entirely before the cutoff
        resp = _get(archive_url)
        time.sleep(request_delay)
        if resp is None:
            continue
        for game in resp.json().get("games", []):
            end_time = game.get("end_time", 0)
            if since_epoch and end_time <= since_epoch:
                continue
            if standard_only and game.get("rules") != "chess":
                continue
            if rated_only and not game.get("rated", False):
                continue
            if time_classes and game.get("time_class") not in time_classes:
                continue
            cr = _color_and_result(game, username)
            if cr is None:
                continue
            color, result = cr
            yield {
                "game_id": game.get("uuid") or game.get("url", "").rsplit("/", 1)[-1],
                "url": game.get("url"),
                "pgn": game.get("pgn"),
                "end_time": end_time,
                "time_class": game.get("time_class"),
                "time_control": game.get("time_control"),
                "rated": game.get("rated", False),
                "user_color": color,
                "user_result": result,
                "white_rating": game.get("white", {}).get("rating"),
                "black_rating": game.get("black", {}).get("rating"),
                "user_rating": game.get(color, {}).get("rating"),
            }
