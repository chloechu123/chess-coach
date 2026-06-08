"""
Interactive Slack coaching.

A `/coach <question>` slash command lets you ask your coach anything and get an
answer grounded in your actual analyzed games (the latest digest's features +
focus areas, pulled from Supabase). Claude answers as your coach.

Slack specifics handled here:
- Request signing verification (HMAC-SHA256 over `v0:timestamp:body`), with a
  5-minute replay window.
- The 3-second ack rule: the web route acks immediately and the heavy Claude
  work runs async, posting the final answer to the command's response_url.

Grounding: the bot only knows what's in your stored digest. It won't invent
engine evals or specific opening theory — for a concrete position it tells you
to run it through the board's analysis instead.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
import time
from typing import Optional

import anthropic
import requests

from .config import cfg

SLACK_SYSTEM = """You are the user's personal chess coach, chatting with them in Slack.

Below is their analyzed profile (Stockfish-derived stats + your prior coaching
focus areas from their recent games). Ground every answer in it.

Voice:
- Talk to them like a real coach who knows their game - warm, direct, a bit of
  personality. Contractions, natural rhythm. Answer like a person, not a manual.
- Be specific to THEM. Pull from their actual tendencies, openings, and patterns
  in the profile. Skip generic chess advice that would apply to anyone.
- Honest but encouraging. Short - this is Slack, not an essay. Get to the point,
  give them something concrete to act on.

Don't make things up: no invented engine evaluations, exact best moves, or
specific opening theory lines you're unsure of. Mostly talk in patterns and
tendencies, not individual moves. The ONE exception: if they ask about a specific
blunder/worst move, you may quote a move from the provided "worst_moments" data
(the move played, the engine's preferred move, and the swing) AS LONG AS you
include that entry's game_url so they can see the position themselves. Never
analyze or describe the position yourself, and never cite a move that isn't in the
provided data. If they ask about a position or line not in the data, tell them to
run it through the board's analysis / an engine and what to look for.

Use Slack mrkdwn: *bold*, _italic_, "• " bullets, and <url|label> links. No markdown headers."""


def verify_signature(signing_secret: str, timestamp: str, raw_body: str, signature: str) -> bool:
    if not (signing_secret and timestamp and signature):
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:   # replay protection
            return False
    except ValueError:
        return False
    base = f"v0:{timestamp}:{raw_body}"
    expected = "v0=" + hmac.new(signing_secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _trim_features(features: dict) -> dict:
    """Keep only what's useful for Q&A grounding; drop bulky move lists."""
    keep = ("n_games", "overall", "by_color", "accuracy_by_phase",
            "share_of_eval_lost_by_phase_pct", "error_counts", "blunder_timing",
            "blunder_motifs", "conversion", "worst_openings", "rating", "trend")
    out = {k: features.get(k) for k in keep if k in features}
    # a few worst moments help, trimmed
    wm = features.get("worst_moments", [])[:6]
    out["worst_moments"] = [{k: m.get(k) for k in
                             ("fullmove", "played", "engine_best", "win_drop_pct",
                              "phase", "motif", "game_url")}
                            for m in wm]
    return out


def load_context(username: str) -> Optional[dict]:
    if not cfg.use_supabase:
        return None
    from .store import SupabaseStore
    store = SupabaseStore(cfg.supabase_url, cfg.supabase_key)
    r = (store.sb.table("digests")
         .select("period_label,n_games,headline,focus_areas,features")
         .eq("username", username.lower())
         .order("created_at", desc=True).limit(1).execute())
    if not r.data:
        return None
    row = r.data[0]
    return {
        "period": row.get("period_label"),
        "headline": row.get("headline"),
        "focus_areas": row.get("focus_areas"),
        "stats": _trim_features(row.get("features") or {}),
    }


def answer_question(question: str, username: str) -> str:
    context = load_context(username)
    if context is None:
        return ("I don't have an analyzed profile yet for "
                f"*{username}*. Run a digest first, then ask me again.")
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    msg = (f"Player: {username}\n\n"
           f"Analyzed profile (JSON):\n{json.dumps(context, indent=2)}\n\n"
           f"Question: {question}")
    resp = client.messages.create(
        model=cfg.coach_model, max_tokens=1200, system=SLACK_SYSTEM,
        messages=[{"role": "user", "content": msg}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def strip_mention(text: str) -> str:
    """Remove the leading bot mention from an app_mention's text."""
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()


def post_message(channel: str, text: str, thread_ts: Optional[str] = None):
    """Post the coach's reply into the channel (in-thread if thread_ts given)."""
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {cfg.slack_bot_token}",
                 "Content-Type": "application/json; charset=utf-8"},
        json=payload, timeout=15,
    )


def log_qa(username: str, question: str, answer: str, slack_user_id: Optional[str] = None):
    """Persist a Q&A to qa_log. Never raise — logging must not break the reply."""
    try:
        from .store import get_store
        store = get_store(cfg)
        store.log_qa({
            "username": username.lower(),
            "slack_user_id": slack_user_id,
            "question": question,
            "answer": answer,
            "created_at": dt.datetime.utcnow().isoformat(),
        })
    except Exception:
        pass
