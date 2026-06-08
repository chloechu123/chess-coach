"""
Orchestrator.

  python -m chess_coach.main backfill        # analyze full history, one digest
  python -m chess_coach.main run             # incremental: only new games since last run

Pull -> analyze (skip already-analyzed) -> store -> aggregate -> coach -> deliver.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import Optional

from .config import cfg
from . import chesscom, analysis, aggregate, coach, deliver
from .store import get_store


def _record_from(game: dict, ga: analysis.GameAnalysis, username: str) -> dict:
    counts = {"blunder": 0, "mistake": 0, "inaccuracy": 0}
    for m in ga.user_moves:
        if m["classification"]:
            counts[m["classification"]] += 1
    return {
        "game_id": game["game_id"],
        "username": username.lower(),
        "end_time": game["end_time"],
        "time_class": game["time_class"],
        "user_color": game["user_color"],
        "user_result": game["user_result"],
        "user_rating": game["user_rating"],
        "accuracy": ga.user_accuracy,
        "eco": ga.eco,
        "opening": ga.opening,
        "url": game["url"],
        "n_blunders": counts["blunder"],
        "n_mistakes": counts["mistake"],
        "n_inaccuracies": counts["inaccuracy"],
        "user_moves": ga.user_moves,
        "blunders": ga.blunders,
    }


def _analyze_new_games(store, engine, since_epoch, username, time_classes, rated_only):
    new_count = 0
    for game in chesscom.iter_games(
        username, since_epoch=since_epoch,
        rated_only=rated_only, time_classes=time_classes,
    ):
        if not game.get("pgn") or store.is_analyzed(game["game_id"]):
            continue
        ga = analysis.analyze_game(
            game["pgn"], game["user_color"], engine,
            depth=cfg.analysis_depth, url=game["url"],
        )
        if ga is None:
            continue
        store.save_game(_record_from(game, ga, username))
        new_count += 1
        if new_count % 10 == 0:
            print(f"  analyzed {new_count} new games...", file=sys.stderr)
    return new_count


def _resolve_users(store, only_username=None) -> list[dict]:
    """The roster to process. Prefer a `users` table; fall back to the single
    configured user so an existing single-tenant setup keeps working unchanged.
    Per-user delivery targets default to the global config when not overridden,
    so everything lands in YOUR Notion/Slack unless a user says otherwise."""
    rows = []
    getter = getattr(store, "get_users", None)
    if getter:
        try:
            rows = getter() or []
        except Exception as e:
            print(f"[users] table lookup failed ({e}); using configured user", file=sys.stderr)

    if not rows:
        if not cfg.chesscom_username:
            return []
        rows = [{"username": cfg.chesscom_username, "display_name": cfg.chesscom_username,
                 "time_classes": list(cfg.time_classes) if cfg.time_classes else None,
                 "slack_webhook": None, "notion_db_id": None,
                 "backfill_months": cfg.backfill_months, "active": True}]

    users = []
    for r in rows:
        if not r.get("active", True):
            continue
        if only_username and r["username"].lower() != only_username.lower():
            continue
        tcs = r.get("time_classes")
        users.append({
            "username": r["username"],
            "display_name": r.get("display_name") or r["username"],
            "time_classes": set(tcs) if tcs else (cfg.time_classes or None),
            "rated_only": r.get("rated_only", cfg.rated_only),
            "slack_webhook": r.get("slack_webhook") or cfg.slack_webhook_url,
            "notion_db_id": r.get("notion_db_id") or cfg.notion_database_id,
            "backfill_months": r.get("backfill_months", cfg.backfill_months),
        })
    return users


def _run_user(user: dict, mode: str, store, engine):
    name = user["username"]
    if mode == "backfill":
        months = user["backfill_months"]
        since = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30 * months)).timestamp()) \
            if months and months > 0 else None
    else:
        since = store.last_end_time(name)

    print(f"[{mode}:{name}] pulling & analyzing new games...", file=sys.stderr)
    new = _analyze_new_games(store, engine, since, name, user["time_classes"], user["rated_only"])
    print(f"[{mode}:{name}] {new} new games analyzed", file=sys.stderr)

    # Rolling 30-day window for coaching + all-time baseline for the trend line.
    recent_since = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).timestamp())
    recent = aggregate.build_features(store.get_games(name, since_epoch=recent_since))
    baseline = aggregate.build_features(store.get_games(name))
    print(f"[{mode}:{name}] recent(30d)={recent.get('n_games')} baseline(all)={baseline.get('n_games')}",
          file=sys.stderr)

    if recent.get("n_games", 0) > 0:
        features, window_label = recent, "Rolling 30 days"
    else:
        features, window_label = baseline, "All-time (no games in the last 30 days)"
    trend = _build_trend(recent, baseline)
    features = {**features, "trend": trend}

    status = "ok"
    try:
        result = coach.generate_coaching(features, window_label, cfg.anthropic_api_key,
                                         cfg.coach_model, trend=trend)
    except Exception as e:
        status, result = f"coach_error: {e}", {
            "headline": f"Coaching failed: {e}", "digest_markdown": "", "focus_areas": []}

    title = f"Chess coaching — {user['display_name']} — {dt.date.today().isoformat()} ({features.get('n_games')} games)"
    store.save_digest({
        "created_at": dt.datetime.utcnow().isoformat(),
        "username": name.lower(),
        "period_label": window_label,
        "n_games": features.get("n_games"),
        "features": features,
        "headline": result.get("headline"),
        "focus_areas": result.get("focus_areas"),
        "markdown": result.get("digest_markdown"),
    })

    digest_url = None
    if cfg.notion_api_key and user["notion_db_id"]:
        try:
            digest_url = deliver.deliver_notion(
                cfg.notion_api_key, user["notion_db_id"], title,
                result, features, date_iso=dt.date.today().isoformat(),
                player=user["display_name"],
            )
            print(f"[deliver:{name}] notion: {digest_url}", file=sys.stderr)
        except Exception as e:
            print(f"[deliver:{name}] notion failed: {e}", file=sys.stderr)

    if user["slack_webhook"]:
        try:
            headline = f"{user['display_name']}: {result.get('headline','')}"
            deliver.deliver_slack(user["slack_webhook"], result.get("digest_markdown", ""),
                                  features=features, digest_url=digest_url, headline=headline)
            print(f"[deliver:{name}] slack sent", file=sys.stderr)
        except Exception as e:
            print(f"[deliver:{name}] slack failed: {e}", file=sys.stderr)

    try:
        store.log_run({
            "created_at": dt.datetime.utcnow().isoformat(),
            "username": name.lower(),
            "mode": mode,
            "new_games": new,
            "recent_games": recent.get("n_games"),
            "baseline_games": baseline.get("n_games"),
            "status": status,
        })
    except Exception as e:
        print(f"[heartbeat:{name}] log_run failed: {e}", file=sys.stderr)

    print(f"\n=== {user['display_name']} ===\n"
          + (result.get("digest_markdown") or result.get("headline", "")))


def run(mode: str, only_username: Optional[str] = None):
    if not cfg.anthropic_api_key:
        sys.exit("Set ANTHROPIC_API_KEY")

    store = get_store(cfg)
    users = _resolve_users(store, only_username)
    if not users:
        sys.exit("No users to process. Add rows to the `users` table or set CHESSCOM_USERNAME.")
    print(f"[{mode}] processing {len(users)} user(s): "
          f"{', '.join(u['username'] for u in users)}", file=sys.stderr)

    engine = analysis.open_engine(cfg.stockfish_path, cfg.engine_threads, cfg.engine_hash_mb)
    try:
        for user in users:
            try:
                _run_user(user, mode, store, engine)
            except Exception as e:
                print(f"[{mode}:{user['username']}] FAILED: {e}", file=sys.stderr)
                try:
                    store.log_run({"created_at": dt.datetime.utcnow().isoformat(),
                                   "username": user["username"].lower(), "mode": mode,
                                   "new_games": None, "recent_games": None,
                                   "baseline_games": None, "status": f"error: {e}"})
                except Exception:
                    pass
    finally:
        engine.quit()


def _build_trend(recent: dict, baseline: dict) -> dict:
    def acc(f): return (f.get("overall") or {}).get("accuracy")
    def score(f): return (f.get("overall") or {}).get("score_pct")
    def bpg(f): return (f.get("errors_per_game") or {}).get("blunder")
    r_rating = recent.get("rating") or {}
    return {
        "recent_games": recent.get("n_games"), "baseline_games": baseline.get("n_games"),
        "recent_accuracy": acc(recent), "baseline_accuracy": acc(baseline),
        "recent_score_pct": score(recent), "baseline_score_pct": score(baseline),
        "recent_blunders_per_game": bpg(recent), "baseline_blunders_per_game": bpg(baseline),
        "current_rating": r_rating.get("end"), "rating_change_30d": r_rating.get("delta"),
    }


def main():
    ap = argparse.ArgumentParser(description="chess.com -> Stockfish -> Claude coaching pipeline")
    ap.add_argument("mode", choices=["backfill", "run"], help="backfill = full history; run = incremental")
    ap.add_argument("--user", help="process only this chess.com handle (default: all active users)")
    args = ap.parse_args()
    run(args.mode, only_username=args.user)


if __name__ == "__main__":
    main()
