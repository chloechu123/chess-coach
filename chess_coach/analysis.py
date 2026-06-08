"""
Per-game engine analysis.

Stockfish is the source of objective truth. For each position in a game we run
ONE engine evaluation (best move + score). A move's centipawn loss is then the
difference between the eval of the position before the move and the eval of the
position after it, both taken from the mover's point of view. This is the
standard one-eval-per-position trick (N+1 evals instead of 2N).

Win% and accuracy use the Lichess/Chess.com logistic model so the numbers line
up with what the user already sees on chess.com.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, asdict, field
from typing import Optional

import chess
import chess.engine
import chess.pgn


# --- tunables (overridable from config) -------------------------------------

MATE_CP = 10000          # mate scores are clamped to this magnitude in centipawns
CP_CLAMP = 1000          # cap eval magnitude for the win% model (1000cp ~= winning)
OPENING_LAST_FULLMOVE = 12
ENDGAME_NONPAWN_MATERIAL = 12   # both sides combined, non-pawn non-king material

# win%-drop thresholds for classifying the move the user actually played
INACCURACY_DROP = 10.0
MISTAKE_DROP = 20.0
BLUNDER_DROP = 30.0

_PIECE_VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
                chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}


def win_percent(cp: float) -> float:
    """Winning chances 0..100 for the side the cp score belongs to."""
    cp = max(-CP_CLAMP, min(CP_CLAMP, cp))
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-0.00368208 * cp)) - 1.0)


def move_accuracy(win_before: float, win_after: float) -> float:
    """Per-move accuracy 0..100 from the win% the mover gave up."""
    drop = max(0.0, win_before - win_after)
    acc = 103.1668 * math.exp(-0.04354 * drop) - 3.1669
    return max(0.0, min(100.0, acc))


def _nonpawn_material(board: chess.Board) -> int:
    total = 0
    for piece_type in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        total += _PIECE_VALUE[piece_type] * (
            len(board.pieces(piece_type, chess.WHITE))
            + len(board.pieces(piece_type, chess.BLACK))
        )
    return total


def _phase(board: chess.Board) -> str:
    if board.fullmove_number <= OPENING_LAST_FULLMOVE:
        return "opening"
    if _nonpawn_material(board) <= ENDGAME_NONPAWN_MATERIAL:
        return "endgame"
    return "middlegame"


def _classify(win_drop: float) -> Optional[str]:
    if win_drop >= BLUNDER_DROP:
        return "blunder"
    if win_drop >= MISTAKE_DROP:
        return "mistake"
    if win_drop >= INACCURACY_DROP:
        return "inaccuracy"
    return None


_NEAR_MATE = MATE_CP - 1000


def _material(board: chess.Board, color: bool) -> int:
    return sum(_PIECE_VALUE[pt] * len(board.pieces(pt, color))
               for pt in (chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN))


def _motif(board_before, played, opp_reply, cp_before, cp_after,
           classification, mover_white) -> Optional[str]:
    """Best-effort, deterministic motif tag for a mistake/blunder. Conservative on
    purpose: only a handful of high-confidence categories, plus a generic fallback."""
    if classification not in ("mistake", "blunder"):
        return None
    if cp_before >= _NEAR_MATE and cp_after < _NEAR_MATE:
        return "missed forced mate"
    if cp_after <= -_NEAR_MATE:
        return "walked into mate"
    color = chess.WHITE if mover_white else chess.BLACK
    try:
        b1 = board_before.copy()
        b1.push(played)
        m0 = _material(board_before, color)
        if opp_reply is not None and opp_reply in b1.legal_moves:
            b1.push(opp_reply)
        if m0 - _material(b1, color) >= 2:   # net-dropped a minor piece or more
            return "hung material"
    except Exception:
        pass
    if cp_before >= 150 and (cp_before - cp_after) >= 100:
        return "let a winning position slip"
    return "tactical oversight"


@dataclass
class MoveEval:
    ply: int
    fullmove: int
    color: str               # "white" | "black" (who moved)
    san: str
    fen_before: str
    cp_before: int           # mover POV, best play
    cp_after: int            # mover POV, after the move actually played
    win_drop: float
    best_san: str
    classification: Optional[str]
    phase: str
    move_accuracy: float
    motif: Optional[str] = None


@dataclass
class GameAnalysis:
    user_color: str          # "white" | "black"
    user_result: str         # "win" | "loss" | "draw"
    eco: Optional[str]
    opening: Optional[str]
    time_class: Optional[str]
    url: Optional[str]
    user_accuracy: float
    user_moves: list = field(default_factory=list)   # list[MoveEval] for the USER only
    blunders: list = field(default_factory=list)      # subset, classification == blunder

    def to_dict(self):
        d = asdict(self)
        return d


def _result_for(color: str, headers) -> str:
    res = headers.get("Result", "*")
    if res == "1/2-1/2":
        return "draw"
    if res == "1-0":
        return "win" if color == "white" else "loss"
    if res == "0-1":
        return "win" if color == "black" else "loss"
    return "unknown"


def analyze_game(
    pgn_text: str,
    user_color: str,
    engine: chess.engine.SimpleEngine,
    depth: int = 12,
    url: Optional[str] = None,
) -> Optional[GameAnalysis]:
    """Analyze a single game from the perspective of `user_color`."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None

    headers = game.headers
    board = game.board()
    limit = chess.engine.Limit(depth=depth)

    # Walk the game, evaluating every position exactly once. We keep the eval and
    # best move for each position from White's POV, then convert to mover POV.
    positions = [board.copy()]
    moves = list(game.mainline_moves())
    for mv in moves:
        board.push(mv)
        positions.append(board.copy())

    # eval[i] = (score_white_cp, best_move_uci) for positions[i]
    evals: list[tuple[int, Optional[chess.Move]]] = []
    for pos in positions:
        if pos.is_game_over():
            # terminal: score from side-to-move POV is decisive/draw
            if pos.is_checkmate():
                # side to move is mated -> very bad for them; express White POV
                white_cp = -MATE_CP if pos.turn == chess.WHITE else MATE_CP
            else:
                white_cp = 0
            evals.append((white_cp, None))
            continue
        info = engine.analyse(pos, limit)
        score = info["score"].white().score(mate_score=MATE_CP)
        pv = info.get("pv")
        best = pv[0] if pv else None
        evals.append((score, best))

    user_moves: list[MoveEval] = []
    replay = game.board()
    for i, mv in enumerate(moves):
        mover = "white" if replay.turn == chess.WHITE else "black"
        cp_before_white, best_move = evals[i]
        cp_after_white, _ = evals[i + 1]

        # convert to mover POV
        sign = 1 if mover == "white" else -1
        cp_before = sign * cp_before_white
        cp_after = sign * cp_after_white

        win_b = win_percent(cp_before)
        win_a = win_percent(cp_after)
        drop = max(0.0, win_b - win_a)

        san = replay.san(mv)
        best_san = replay.san(best_move) if best_move else san
        phase = _phase(replay)
        classification = _classify(drop)

        if mover == user_color:
            motif = _motif(replay, mv, evals[i + 1][1], int(cp_before), int(cp_after),
                           classification, mover == "white")
            user_moves.append(MoveEval(
                ply=i + 1,
                fullmove=replay.fullmove_number,
                color=mover,
                san=san,
                fen_before=replay.fen(),
                cp_before=int(cp_before),
                cp_after=int(cp_after),
                win_drop=round(drop, 1),
                best_san=best_san,
                classification=classification,
                phase=phase,
                move_accuracy=round(move_accuracy(win_b, win_a), 1),
                motif=motif,
            ))
        replay.push(mv)

    if user_moves:
        user_acc = round(sum(m.move_accuracy for m in user_moves) / len(user_moves), 1)
    else:
        user_acc = 0.0

    blunders = [m for m in user_moves if m.classification == "blunder"]

    return GameAnalysis(
        user_color=user_color,
        user_result=_result_for(user_color, headers),
        eco=headers.get("ECO"),
        opening=headers.get("ECOUrl", "").rsplit("/", 1)[-1].replace("-", " ") or None,
        time_class=headers.get("TimeControl"),
        url=url or headers.get("Link"),
        user_accuracy=user_acc,
        user_moves=[asdict(m) for m in user_moves],
        blunders=[asdict(m) for m in blunders],
    )


def open_engine(stockfish_path: str, threads: int = 1, hash_mb: int = 128):
    eng = chess.engine.SimpleEngine.popen_uci(stockfish_path)
    try:
        eng.configure({"Threads": threads, "Hash": hash_mb})
    except Exception:
        pass
    return eng
