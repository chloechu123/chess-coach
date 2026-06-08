-- Supabase / Postgres schema for the chess coaching pipeline.
-- Run in the Supabase SQL editor.

create table if not exists games (
    game_id          text primary key,
    username         text not null,
    end_time         bigint not null,
    time_class       text,
    user_color       text,
    user_result      text,             -- win | loss | draw
    user_rating      int,
    accuracy         real,
    eco              text,
    opening          text,
    url              text,
    n_blunders       int default 0,
    n_mistakes       int default 0,
    n_inaccuracies   int default 0,
    user_moves       jsonb,            -- per-move evals for the user
    blunders         jsonb,
    created_at       timestamptz default now()
);

create index if not exists games_user_endtime_idx
    on games (username, end_time desc);

create table if not exists digests (
    id            bigint generated always as identity primary key,
    created_at    timestamptz default now(),
    username      text not null,
    period_label  text,
    n_games       int,
    headline      text,
    focus_areas   jsonb,
    features      jsonb,              -- full deterministic snapshot, for trends
    markdown      text
);

create index if not exists digests_user_idx
    on digests (username, created_at desc);

-- cache for the synchronous web UI (keyed on the player's newest game)
create table if not exists web_cache (
    username    text primary key,
    newest_id   text,
    payload     jsonb,
    updated_at  timestamptz default now()
);

-- log of /coach Q&A so the digest can reference recurring questions over time
create table if not exists qa_log (
    id             bigint generated always as identity primary key,
    created_at     timestamptz default now(),
    username       text not null,      -- chess.com handle the advice was about
    slack_user_id  text,               -- who asked
    question       text not null,
    answer         text
);
create index if not exists qa_log_user_idx on qa_log (username, created_at desc);

-- heartbeat: one row per pipeline run, so a quiet week (0 new games) is
-- distinguishable from a dead scheduler. Query: select * from runs order by created_at desc limit 10;
create table if not exists runs (
    id              bigint generated always as identity primary key,
    created_at      timestamptz default now(),
    mode            text,               -- 'backfill' | 'run'
    new_games       integer,            -- games analyzed this run
    recent_games    integer,            -- games in the rolling 30-day window
    baseline_games  integer,            -- all-time games at run time
    status          text                -- 'ok' or an error string
);
create index if not exists runs_created_idx on runs (created_at desc);
