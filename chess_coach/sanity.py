"""
Quick sanity check — no Anthropic key, no database required.

Pulls your most recent games from chess.com, runs the real Stockfish analysis,
and prints the deterministic feature set + worst moments. This is exactly what
the pipeline would hand to Claude; eyeballing it confirms the data extraction
is correct before you wire up coaching/delivery.

Usage:
    python -m chess_coach.sanity KiingConsBBQ           # last 10 games
    python -m chess_coach.sanity KiingConsBBQ 20 10     # 20 games, depth 10
"""

from __future__ import annotations

import json
import sys

from .config import cfg
from . import chesscom, analysis, aggregate
from .main import _record_from


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python -m chess_coach.sanity <username> [n_games] [depth]")
    username = sys.argv[1]
    n_games = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    depth = int(sys.argv[3]) if len(sys.argv) > 3 else cfg.analysis_depth

    print(f"Pulling last {n_games} games for {username}...", file=sys.stderr)
    games = chesscom.get_recent_games(username, n=n_games, rated_only=cfg.rated_only)
    if not games:
        sys.exit("No games found (check the username, or set RATED_ONLY=false).")
    print(f"Got {len(games)} games. Analyzing with Stockfish (depth {depth})...", file=sys.stderr)

    engine = analysis.open_engine(cfg.stockfish_path, cfg.engine_threads, cfg.engine_hash_mb)
    records = []
    try:
        for i, g in enumerate(games, 1):
            ga = analysis.analyze_game(g["pgn"], g["user_color"], engine, depth=depth, url=g["url"])
            if ga is None:
                continue
            rec = _record_from(g, ga, username)
            rec["username"] = username.lower()
            records.append(rec)
            print(f"  [{i}/{len(games)}] {g['user_color']} {ga.user_result} "
                  f"acc={ga.user_accuracy} blunders={len(ga.blunders)}", file=sys.stderr)
    finally:
        engine.quit()

    features = aggregate.build_features(records)

    print("\n================ FEATURE SNAPSHOT ================")
    snap = {k: features[k] for k in (
        "n_games", "overall", "by_color", "accuracy_by_phase", "error_counts",
        "errors_per_game", "blunder_timing", "share_of_eval_lost_by_phase_pct",
        "conversion", "worst_openings",
    ) if k in features}
    print(json.dumps(snap, indent=2))

    print("\n================ WORST MOMENTS ================")
    for m in features.get("worst_moments", [])[:8]:
        print(f"  move {m['fullmove']:>3} {m['played']:<7} (engine: {m['engine_best']:<7}) "
              f"win-drop {m['win_drop_pct']:>5}%  [{m['phase']}]  {m['game_url']}")
    print("\nLooks right? Then add ANTHROPIC_API_KEY and run: python -m chess_coach.main backfill")


if __name__ == "__main__":
    main()
