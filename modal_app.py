"""
Modal deployment.

Why Modal and not a Supabase Edge Function: the Stockfish pass is a CPU-bound
native binary chewing through hundreds of positions per game. Deno Edge
Functions can't run a native engine and will hit CPU/wall-clock limits. Modal
runs the real binary, scales the analysis, and gives you cron for free.
Supabase stays in the picture as the datastore (set SUPABASE_* secrets).

Deploy:
    modal deploy modal_app.py
Run a one-time full backfill:
    modal run modal_app.py::backfill
The weekly incremental digest runs automatically on the schedule below.

Secrets (create once):
    modal secret create chess-coach \
        CHESSCOM_USERNAME=... ANTHROPIC_API_KEY=... \
        SUPABASE_URL=... SUPABASE_KEY=... \
        NOTION_API_KEY=... NOTION_DATABASE_ID=... SLACK_WEBHOOK_URL=...
"""

import modal

image = (
    modal.Image.debian_slim()
    .apt_install("stockfish")
    .pip_install("chess", "anthropic", "requests", "supabase", "notion-client")
    .env({"STOCKFISH_PATH": "/usr/games/stockfish"})
    .add_local_python_source("chess_coach")
)

app = modal.App("chess-coach")
secret = modal.Secret.from_name("chess-coach")


@app.function(image=image, secrets=[secret], timeout=3600)
def backfill():
    from chess_coach.main import run
    run("backfill")


@app.function(image=image, secrets=[secret], timeout=3600,
              schedule=modal.Cron("0 13 * * 1"))  # Mondays 13:00 UTC
def weekly():
    from chess_coach.main import run
    run("run")


@app.local_entrypoint()
def main():
    # `modal run modal_app.py` -> incremental run
    weekly.remote()
