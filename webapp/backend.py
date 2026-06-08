"""
Web backend for the "send to a friend" UI.

A Modal-hosted FastAPI app with one real endpoint: POST /api/analyze {username}.
It pulls a bounded number of recent games, runs Stockfish, aggregates, and asks
Claude for coaching. Results are cached by (username, latest game) in Supabase
when configured, so re-runs are free until the player plays again.

Why bounded + synchronous here: a browser request shouldn't wait minutes. We cap
games and depth so a request returns in well under a minute. For full-history or
heavy analysis, use the async pipeline (chess_coach.main) instead — see README.

Deploy:
    modal deploy webapp/backend.py
The printed *.modal.run URL is your API base; put it in webapp/index.html.
"""

import json
import os

import modal

image = (
    modal.Image.debian_slim()
    .apt_install("stockfish")
    .pip_install("chess", "anthropic", "requests", "fastapi[standard]", "supabase")
    .env({"STOCKFISH_PATH": "/usr/games/stockfish"})
    .add_local_python_source("chess_coach")
)

# slim image for the endpoint + Q&A worker: no Stockfish/chess, so it cold-starts
# fast enough for Slack's URL-verification handshake.
web_image = (
    modal.Image.debian_slim()
    .pip_install("anthropic", "requests", "fastapi[standard]", "supabase")
    .add_local_python_source("chess_coach")
)

app = modal.App("chess-coach-web")
secret = modal.Secret.from_name("chess-coach")

# bounds for the synchronous web path
WEB_MAX_GAMES = int(os.environ.get("WEB_MAX_GAMES", "8"))
WEB_DEPTH = int(os.environ.get("WEB_DEPTH", "10"))


@app.function(image=image, secrets=[secret], timeout=300, max_containers=4)
@modal.concurrent(max_inputs=1)
def analyze_user(username: str, max_games: int = WEB_MAX_GAMES) -> dict:
    from chess_coach import chesscom, analysis, aggregate, coach
    from chess_coach.main import _record_from
    from chess_coach.config import cfg

    max_games = max(1, min(max_games, 15))  # hard cap to protect cost
    games = chesscom.get_recent_games(username, n=max_games, rated_only=True)
    if not games:
        return {"ok": False, "error": "No rated standard games found for that username."}

    # cache check (Supabase): keyed on newest game id
    newest_id = games[0]["game_id"]
    store = None
    if cfg.use_supabase:
        from chess_coach.store import SupabaseStore
        store = SupabaseStore(cfg.supabase_url, cfg.supabase_key)
        try:
            cached = (store.sb.table("web_cache").select("*")
                      .eq("username", username.lower()).eq("newest_id", newest_id)
                      .limit(1).execute())
            if cached.data:
                return {"ok": True, "cached": True, **cached.data[0]["payload"]}
        except Exception:
            pass

    engine = analysis.open_engine(cfg.stockfish_path, cfg.engine_threads, cfg.engine_hash_mb)
    records = []
    try:
        for g in games:
            ga = analysis.analyze_game(g["pgn"], g["user_color"], engine,
                                       depth=WEB_DEPTH, url=g["url"])
            if ga:
                rec = _record_from(g, ga, username)
                rec["username"] = username.lower()
                records.append(rec)
    finally:
        engine.quit()

    features = aggregate.build_features(records)
    coaching = coach.generate_coaching(
        features, f"Last {len(records)} games", cfg.anthropic_api_key, cfg.coach_model
    )

    payload = {
        "username": username,
        "n_games": features.get("n_games"),
        "snapshot": {
            "overall": features.get("overall"),
            "by_color": features.get("by_color"),
            "accuracy_by_phase": features.get("accuracy_by_phase"),
            "error_counts": features.get("error_counts"),
            "blunder_timing": features.get("blunder_timing"),
            "conversion": features.get("conversion"),
        },
        "worst_moments": features.get("worst_moments", [])[:6],
        "coaching": coaching,
    }

    if store:
        try:
            store.sb.table("web_cache").upsert(
                {"username": username.lower(), "newest_id": newest_id, "payload": payload},
                on_conflict="username",
            ).execute()
        except Exception:
            pass

    return {"ok": True, "cached": False, **payload}


@app.function(image=web_image, secrets=[secret], timeout=120)
def slack_coach_worker(question: str, username: str, channel: str,
                       thread_ts: str = None, slack_user_id: str = None):
    """Answer the question, log it, and post the reply back into the Slack channel."""
    from chess_coach import slackbot
    try:
        answer = slackbot.answer_question(question, username)
    except Exception as e:
        answer = f"Sorry — I hit an error answering that: {e}"
    slackbot.log_qa(username, question, answer, slack_user_id)
    slackbot.post_message(channel, answer, thread_ts)


@app.function(image=web_image, secrets=[secret])
@modal.asgi_app()
def fastapi_app():
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel

    from chess_coach import slackbot
    from chess_coach.config import cfg as _cfg

    web = FastAPI(title="Chess Coach API")
    web.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    class Req(BaseModel):
        username: str
        max_games: int = WEB_MAX_GAMES

    @web.get("/api/health")
    def health():
        return {"ok": True}

    @web.post("/api/analyze")
    def analyze(req: Req):
        u = req.username.strip().lstrip("@")
        if not u:
            return {"ok": False, "error": "Enter a chess.com username."}
        return analyze_user.remote(u, req.max_games)

    @web.post("/slack/events")
    async def slack_events(request: Request):
        raw = (await request.body()).decode()
        ts = request.headers.get("X-Slack-Request-Timestamp", "")
        sig = request.headers.get("X-Slack-Signature", "")
        if not slackbot.verify_signature(_cfg.slack_signing_secret, ts, raw, sig):
            return JSONResponse({"error": "bad signature"}, status_code=401)

        payload = json.loads(raw)

        # 1) one-time URL verification handshake when you set the Request URL
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}

        # 2) ignore Slack's automatic retries so cold starts don't double-answer
        if request.headers.get("x-slack-retry-num"):
            return JSONResponse({"ok": True})

        event = payload.get("event", {})
        # only react to @-mentions of the bot; never to bot messages (avoids loops)
        if event.get("type") == "app_mention" and not event.get("bot_id"):
            question = slackbot.strip_mention(event.get("text", ""))
            channel = event.get("channel")
            thread_ts = event.get("thread_ts") or event.get("ts")
            if question:
                slack_coach_worker.spawn(
                    question, _cfg.chesscom_username, channel, thread_ts, event.get("user")
                )

        # ack immediately; the answer is posted async via chat.postMessage
        return JSONResponse({"ok": True})

    return web
