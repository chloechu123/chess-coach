# Chess Coach Pipeline

A scheduled pipeline that pulls your chess.com games, evaluates them with
Stockfish, and uses Claude to produce **cross-game, personalized coaching** —
the thing chess.com's own per-game Game Review doesn't do. Output lands in
Notion (a longitudinal coaching log) and/or Slack.

```
chess.com Published-Data API ──► Stockfish (objective eval) ──► aggregate
                                                                    │
                                          deterministic features +  │
                                          curated worst moments      ▼
Notion / Slack ◄── deliver ◄── coaching digest ◄── Claude (the coach)
                                                         ▲
                              Supabase (games + digests, dedupe + trends)
```

## The two design decisions that matter

1. **Stockfish is the source of truth; Claude is the coach.** Claude is a
   language model, not a chess engine — asked to evaluate positions it will
   hallucinate. So every objective number (centipawn loss, accuracy, blunder
   detection, best moves) comes from Stockfish. Claude only ever sees those
   numbers and is explicitly told not to re-evaluate anything. It does what it's
   good at: spotting recurring patterns across hundreds of games and turning
   them into a prioritized study plan.

2. **Never send raw games to the model.** `aggregate.py` computes the hard stats
   deterministically and hands Claude a small JSON summary plus the ~12 worst
   individual moments (FEN + played vs. best + win-probability swing). Small
   context, low cost, no room to invent.

## Why Modal, not a Supabase Edge Function

The Stockfish pass is a CPU-bound native binary. Deno Edge Functions can't run
a native engine and hit CPU/time limits analyzing hundreds of games. Modal runs
the real binary, has cron built in, and scales the heavy backfill. Supabase
stays as the datastore. (You can also just run it locally — see below.)

## Setup

```bash
pip install -r requirements.txt          # python-chess + anthropic (+ optional backends)
apt-get install stockfish                # or point STOCKFISH_PATH at a binary
cp .env.example .env                      # fill in CHESSCOM_USERNAME + ANTHROPIC_API_KEY
```

Set the contact email in `chess_coach/chesscom.py` `USER_AGENT` — chess.com's
Cloudflare 403s requests without a descriptive User-Agent.

If using Supabase, run `schema.sql` in the SQL editor and set `SUPABASE_URL` /
`SUPABASE_KEY`. Otherwise it writes JSON to `./state` automatically.

For Notion delivery, create a database, share it with your integration, and set
`NOTION_API_KEY` / `NOTION_DATABASE_ID`. Optional `Accuracy` (number) and
`Games` (number) columns get populated for free.

## Run

```bash
# full history, one digest (run once)
python -m chess_coach.main backfill

# incremental: only games since the last run (schedule this)
python -m chess_coach.main run
```

Local runs print the digest to stdout regardless of delivery config, so you can
try it with zero external services beyond chess.com + Anthropic.

## Deploy on Modal

```bash
modal secret create chess-coach CHESSCOM_USERNAME=... ANTHROPIC_API_KEY=... \
    SUPABASE_URL=... SUPABASE_KEY=... NOTION_API_KEY=... NOTION_DATABASE_ID=...
modal deploy modal_app.py        # weekly incremental digest, Mondays 13:00 UTC
modal run modal_app.py::backfill # one-time full-history analysis
```

## Tuning

- `ANALYSIS_DEPTH` (default 12): higher = slower but sharper blunder detection.
  Depth 12 is plenty for finding tactical errors across many games.
- Thresholds for inaccuracy/mistake/blunder live in `analysis.py`
  (`*_DROP`, expressed as win-percentage loss).
- `COACH_MODEL`: `claude-sonnet-4-6` is the cost/quality sweet spot for this
  synthesis; switch to a larger model for the full-history backfill if you want
  deeper write-ups.

## Module map

| file | role |
|---|---|
| `chesscom.py` | pull archives + games (serial, rate-limit aware) |
| `analysis.py` | Stockfish per-move eval, win%/accuracy, blunder classification |
| `aggregate.py` | deterministic cross-game features + worst-moment samples |
| `coach.py` | Claude pass → structured coaching JSON + markdown |
| `store.py` | Supabase or local-JSON persistence (dedupe + cursor) |
| `deliver.py` | Notion + Slack delivery |
| `main.py` | orchestration: `backfill` / `run` |
| `modal_app.py` | image + cron + backfill entrypoint |

## What's verified vs. what needs your credentials

The Stockfish analysis engine and the full offline path (analyze → record →
aggregate) are tested and working. The live chess.com pull and the Claude call
are written against the documented APIs but need your network + keys to exercise
end-to-end — start with a local `backfill` to confirm.

## Honest scope note

Lit-square coaching and engine review help most in the ~600–1400 range. Above
~1800 the value shifts from "stop hanging pieces" to deep opening/endgame prep,
where you'd extend this with an opening-tree report and tablebase endgame checks
rather than generic blunder digests.
```

## Interactive Slack coaching (@mention)

Ask your coach questions by @-mentioning the bot in any channel it's in, and get
answers grounded in your analyzed games. Uses the Events API (no 3-second slash
deadline, no always-warm container).

Setup:
1. Create a Slack app (https://api.slack.com/apps) -> From scratch -> pick your workspace.
2. OAuth & Permissions -> Bot Token Scopes: add `app_mentions:read` and `chat:write`.
   Install to workspace, then copy the Bot User OAuth Token (`xoxb-...`).
3. Basic Information -> copy the Signing Secret.
4. Add both to the Modal secret, then deploy (the endpoint must be live before step 5):
   ```
   modal secret update chess-coach SLACK_SIGNING_SECRET=... SLACK_BOT_TOKEN=xoxb-...
   modal deploy webapp/backend.py
   ```
5. Event Subscriptions -> Enable. Request URL: `https://<your-modal-app>.modal.run/slack/events`
   (Slack sends a challenge; the endpoint answers it automatically). Under
   "Subscribe to bot events" add `app_mention`. Save.
6. Reinstall the app if Slack prompts (scope change). Invite the bot to a channel:
   `/invite @YourBot`.

Usage: `@YourBot what should I drill this week?` The bot replies in-thread. It runs
Claude async and posts via chat.postMessage, so cold starts just delay the reply a
few seconds rather than failing. Slack retries are de-duplicated to avoid double answers.

Every answer is logged to the `qa_log` table (question, answer, who asked, timestamp).

Note: it coaches based on `CHESSCOM_USERNAME`'s latest stored digest. For a multi-user
bot, map the Slack user ID to a chess.com handle and pass that into `slack_coach_worker`.
