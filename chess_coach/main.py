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


def _analyze_new_games(store, engine, since_epoch):
    new_count = 0
    for game in chesscom.iter_games(
        cfg.chesscom_username, since_epoch=since_epoch,
        rated_only=cfg.rated_only, time_classes=cfg.time_classes,
    ):
        if not game.get("pgn") or store.is_analyzed(game["game_id"]):
            continue
        ga = analysis.analyze_game(
            game["pgn"], game["user_color"], engine,
            depth=cfg.analysis_depth, url=game["url"],
        )
        if ga is None:
            continue
        store.save_game(_record_from(game, ga, cfg.chesscom_username))
        new_count += 1
        if new_count % 10 == 0:
            print(f"  analyzed {new_count} new games...", file=sys.stderr)
    return new_count


def run(mode: str):
    if not cfg.chesscom_username:
        sys.exit("Set CHESSCOM_USERNAME")
    if not cfg.anthropic_api_key:
        sys.exit("Set ANTHROPIC_API_KEY")

    store = get_store(cfg)
    engine = analysis.open_engine(cfg.stockfish_path, cfg.engine_threads, cfg.engine_hash_mb)

    try:
        if mode == "backfill":
            if cfg.backfill_months > 0:
                cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30 * cfg.backfill_months)
                since = int(cutoff.timestamp())
                window_since = since
                tc = f" ({','.join(sorted(cfg.time_classes))})" if cfg.time_classes else ""
                period_label = f"Last {cfg.backfill_months} month(s){tc}"
            else:
                since = None
                window_since = None
                period_label = "Full history"
        else:  # incremental
            since = store.last_end_time(cfg.chesscom_username)
            window_since = since
            period_label = f"Games since {dt.datetime.utcfromtimestamp(since).date()}" if since else "First run"

        print(f"[{mode}] pulling & analyzing new games...", file=sys.stderr)
        new = _analyze_new_games(store, engine, since)
        print(f"[{mode}] {new} new games analyzed", file=sys.stderr)
    finally:
        engine.quit()

    # The digest always coaches on a ROLLING 30-DAY window ("what to drill now"),
    # with an ALL-TIME baseline for the trend line. These answer different questions;
    # how much history we analyze (above) is separate from what the digest reports.
    recent_since = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).timestamp())
    recent = aggregate.build_features(store.get_games(cfg.chesscom_username, since_epoch=recent_since))
    baseline = aggregate.build_features(store.get_games(cfg.chesscom_username))
    print(f"[{mode}] recent(30d)={recent.get('n_games')} baseline(all)={baseline.get('n_games')}",
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

    title = f"Chess coaching — {dt.date.today().isoformat()} ({features.get('n_games')} games)"
    store.save_digest({
        "created_at": dt.datetime.utcnow().isoformat(),
        "username": cfg.chesscom_username.lower(),
        "period_label": window_label,
        "n_games": features.get("n_games"),
        "features": features,
        "headline": result.get("headline"),
        "focus_areas": result.get("focus_areas"),
        "markdown": result.get("digest_markdown"),
    })

    digest_url = None
    if cfg.notion_api_key and cfg.notion_database_id:
        try:
            digest_url = deliver.deliver_notion(
                cfg.notion_api_key, cfg.notion_database_id, title,
                result, features, date_iso=dt.date.today().isoformat(),
            )
            print(f"[deliver] notion: {digest_url}", file=sys.stderr)
        except Exception as e:
            print(f"[deliver] notion failed: {e}", file=sys.stderr)

    if cfg.slack_webhook_url:
        try:
            deliver.deliver_slack(cfg.slack_webhook_url, result.get("digest_markdown", ""),
                                  features=features, digest_url=digest_url,
                                  headline=result.get("headline"))
            print("[deliver] slack sent", file=sys.stderr)
        except Exception as e:
            print(f"[deliver] slack failed: {e}", file=sys.stderr)

    # Heartbeat: write a row EVERY run so a quiet week (0 new games) is distinguishable
    # from a dead scheduler. Query `runs` to confirm the cron is alive.
    try:
        store.log_run({
            "created_at": dt.datetime.utcnow().isoformat(),
            "mode": mode,
            "new_games": new,
            "recent_games": recent.get("n_games"),
            "baseline_games": baseline.get("n_games"),
            "status": status,
        })
    except Exception as e:
        print(f"[heartbeat] log_run failed: {e}", file=sys.stderr)

    # always print the digest to stdout too
    print("\n" + (result.get("digest_markdown") or result.get("headline", "")))


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
    args = ap.parse_args()
    run(args.mode)


if __name__ == "__main__":
    main()
