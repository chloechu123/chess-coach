"""
Aggregate many analyzed games into a compact, deterministic feature set.

This is the most important design decision in the pipeline: we never hand raw
PGNs or hundreds of games to the language model. We compute the hard numbers
here (win/loss splits, accuracy by phase, blunder timing, opening scores,
conversion) and pass the model a small JSON summary plus a curated list of the
worst individual moments (FEN + played vs best + eval swing). That keeps the
context small, keeps cost down, and removes any opportunity for the model to
invent evaluations.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Optional

_SCORE = {"win": 1.0, "draw": 0.5, "loss": 0.0}


def _wdl(games: list[dict]) -> dict:
    w = sum(1 for g in games if g["user_result"] == "win")
    d = sum(1 for g in games if g["user_result"] == "draw")
    loss = sum(1 for g in games if g["user_result"] == "loss")
    n = len(games)
    accs = [g["accuracy"] for g in games if g.get("accuracy")]
    return {
        "games": n,
        "wins": w, "draws": d, "losses": loss,
        "score_pct": round(100 * sum(_SCORE[g["user_result"]] for g in games) / n, 1) if n else 0,
        "accuracy": round(mean(accs), 1) if accs else None,
    }


def _timing_bucket(fullmove: int) -> str:
    if fullmove <= 10:
        return "moves_1_10"
    if fullmove <= 20:
        return "moves_11_20"
    if fullmove <= 30:
        return "moves_21_30"
    if fullmove <= 40:
        return "moves_31_40"
    return "moves_41_plus"


def build_features(games: list[dict]) -> dict:
    """
    `games`: list of dicts each merging chess.com metadata with its analysis:
      required keys: user_color, user_result, time_class, accuracy,
                     user_moves (list of move dicts), eco, opening, url,
                     end_time, user_rating
    """
    if not games:
        return {"n_games": 0}

    games_sorted = sorted(games, key=lambda g: g.get("end_time", 0))
    n = len(games_sorted)

    # accuracy by phase + error counts + blunder timing + eval lost by phase
    phase_acc = defaultdict(list)
    error_counts = defaultdict(int)
    blunder_timing = defaultdict(int)
    cp_lost_by_phase = defaultdict(float)

    for g in games_sorted:
        for m in g["user_moves"]:
            phase_acc[m["phase"]].append(m["move_accuracy"])
            cls = m["classification"]
            if cls:
                error_counts[cls] += 1
            if cls == "blunder":
                blunder_timing[_timing_bucket(m["fullmove"])] += 1
            cp_lost = max(0, m["cp_before"] - m["cp_after"])
            cp_lost_by_phase[m["phase"]] += cp_lost

    total_cp_lost = sum(cp_lost_by_phase.values()) or 1.0

    # opening performance (group by ECO/name)
    by_opening = defaultdict(list)
    for g in games_sorted:
        key = (g.get("eco") or "?", (g.get("opening") or "Unknown")[:60])
        by_opening[key].append(g)
    openings = []
    for (eco, name), gs in by_opening.items():
        if len(gs) < 3:   # ignore tiny samples
            continue
        s = _wdl(gs)
        openings.append({"eco": eco, "name": name, **{k: s[k] for k in
                        ("games", "score_pct", "accuracy")}})
    openings.sort(key=lambda o: (o["score_pct"], o["games"]))
    worst_openings = openings[:5]
    best_openings = sorted(openings, key=lambda o: -o["score_pct"])[:5]

    # conversion: did the user win when they reached a winning position?
    def peak_adv(g):
        vals = [m["cp_after"] for m in g["user_moves"]]
        return max(vals) if vals else 0

    def trough(g):
        vals = [m["cp_after"] for m in g["user_moves"]]
        return min(vals) if vals else 0

    winning_games = [g for g in games_sorted if peak_adv(g) >= 200]
    losing_games = [g for g in games_sorted if trough(g) <= -200]
    conversion = {
        "reached_winning_position": len(winning_games),
        "won_from_winning_pct": round(
            100 * sum(1 for g in winning_games if g["user_result"] == "win") / len(winning_games), 1
        ) if winning_games else None,
        "reached_losing_position": len(losing_games),
        "saved_from_losing_pct": round(
            100 * sum(1 for g in losing_games if g["user_result"] != "loss") / len(losing_games), 1
        ) if losing_games else None,
    }

    # rating trend (first vs last available)
    rated = [g["user_rating"] for g in games_sorted if g.get("user_rating")]

    # worst moments across all games (top 12 by win_drop), for concrete coaching
    moments = []
    for g in games_sorted:
        for m in g["user_moves"]:
            if m["classification"] in ("blunder", "mistake"):
                moments.append({
                    "game_url": g.get("url"),
                    "time_class": g.get("time_class"),
                    "fullmove": m["fullmove"],
                    "color": m["color"],
                    "played": m["san"],
                    "engine_best": m["best_san"],
                    "win_drop_pct": m["win_drop"],
                    "phase": m["phase"],
                    "fen_before": m["fen_before"],
                })
    moments.sort(key=lambda x: -x["win_drop_pct"])
    worst_moments = moments[:12]

    by_tc = defaultdict(list)
    for g in games_sorted:
        by_tc[g.get("time_class") or "unknown"].append(g)

    return {
        "n_games": n,
        "date_range": {
            "from_epoch": games_sorted[0].get("end_time"),
            "to_epoch": games_sorted[-1].get("end_time"),
        },
        "rating": {
            "start": rated[0] if rated else None,
            "end": rated[-1] if rated else None,
            "delta": (rated[-1] - rated[0]) if len(rated) >= 2 else None,
        },
        "overall": _wdl(games_sorted),
        "by_color": {
            "white": _wdl([g for g in games_sorted if g["user_color"] == "white"]),
            "black": _wdl([g for g in games_sorted if g["user_color"] == "black"]),
        },
        "by_time_class": {tc: _wdl(gs) for tc, gs in by_tc.items()},
        "accuracy_by_phase": {ph: round(mean(v), 1) for ph, v in phase_acc.items()},
        "error_counts": dict(error_counts),
        "errors_per_game": {k: round(v / n, 2) for k, v in error_counts.items()},
        "blunder_timing": dict(blunder_timing),
        "share_of_eval_lost_by_phase_pct": {
            ph: round(100 * cp / total_cp_lost, 1) for ph, cp in cp_lost_by_phase.items()
        },
        "worst_openings": worst_openings,
        "best_openings": best_openings,
        "conversion": conversion,
        "worst_moments": worst_moments,
    }
