"""
Delivery — deliberately different per channel.

- Slack = the quick read: the coaching prose + a short worst-moments list + a
  link to Notion. Skimmable in the channel.
- Notion = the dashboard you keep: a callout headline, the coaching prose, then
  rich data tables (by time control, by phase, openings) and a full worst-moments
  table with a link to every game — plus number/date PROPERTIES on the page so the
  whole history is sortable and chartable over time. That's the stuff Slack can't
  do, and the reason to click through.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import requests

NOTION_HEADERS_VERSION = "2022-06-28"


# ---------------------------------------------------------------- shared md

def _md_to_mrkdwn(md: str) -> str:
    out = []
    for line in (md or "").splitlines():
        s = line.rstrip()
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            out.append(f"*{m.group(2).strip()}*")
            continue
        s = re.sub(r"^(\s*)[-*]\s+", r"\1• ", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"*\1*", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", s)
        out.append(s)
    return "\n".join(out)


# ---------------------------------------------------------------- Slack

def _worst_moments_slack(features: dict, limit: int = 4) -> str:
    wm = features.get("worst_moments", [])[:limit]
    if not wm:
        return ""
    lines = ["", "*Worst moments (Stockfish):*"]
    for m in wm:
        url = m.get("game_url") or ""
        link = f" <{url}|view game>" if url else ""
        lines.append(f"• Move {m.get('fullmove','?')} ({m.get('phase','')}): "
                     f"{m.get('played','?')} → engine liked {m.get('engine_best','?')} "
                     f"(−{m.get('win_drop_pct','')}%).{link}")
    return "\n".join(lines)


def _chunk(text: str, limit: int = 3500) -> list[str]:
    paras = text.split("\n\n")
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 2 > limit and cur:
            chunks.append(cur.strip()); cur = ""
        cur += p + "\n\n"
    if cur.strip():
        chunks.append(cur.strip())
    return chunks or [text[:limit]]


def deliver_slack(webhook_url: str, markdown: str, features: Optional[dict] = None,
                  digest_url: Optional[str] = None, headline: Optional[str] = None):
    """Quick read: prose + short worst-moments list + Notion link."""
    body = _md_to_mrkdwn(markdown or "")
    if headline:
        body = f"*♟️ Chess coaching update*\n\n{body}"
    if features:
        wm = _worst_moments_slack(features)
        if wm:
            body += "\n" + wm
    if digest_url:
        body += f"\n\n📊 Full dashboard + history in Notion: <{digest_url}|open>"

    chunks = _chunk(body)
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        text = chunk if total == 1 else f"{chunk}\n\n_(part {i}/{total})_"
        requests.post(webhook_url, json={"text": text}, timeout=20).raise_for_status()
    return True


# ---------------------------------------------------------------- Notion blocks

_INLINE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)|\*\*(.+?)\*\*")


def _rich_text(text: str):
    text = str(text)
    segs, pos = [], 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            segs.append({"type": "text", "text": {"content": text[pos:m.start()][:2000]}})
        if m.group(1) is not None:
            segs.append({"type": "text",
                         "text": {"content": m.group(1)[:2000], "link": {"url": m.group(2)}}})
        else:
            segs.append({"type": "text", "text": {"content": m.group(3)[:2000]},
                         "annotations": {"bold": True}})
        pos = m.end()
    if pos < len(text):
        segs.append({"type": "text", "text": {"content": text[pos:][:2000]}})
    return segs or [{"type": "text", "text": {"content": text[:2000]}}]


def _block(kind, **kw):
    return {"object": "block", "type": kind, kind: kw}


def _h(text, level=2):
    return _block(f"heading_{level}", rich_text=_rich_text(text))


def _bullet(text):
    return _block("bulleted_list_item", rich_text=_rich_text(text))


def _para(text):
    return _block("paragraph", rich_text=_rich_text(text))


def _callout(text, emoji="♟️"):
    return _block("callout", rich_text=_rich_text(text), icon={"type": "emoji", "emoji": emoji})


def _divider():
    return {"object": "block", "type": "divider", "divider": {}}


def _table(headers, rows):
    def trow(cells):
        return {"object": "block", "type": "table_row",
                "table_row": {"cells": [_rich_text(c) for c in cells]}}
    children = [trow(headers)] + [trow(r) for r in rows]
    return {"object": "block", "type": "table",
            "table": {"table_width": len(headers), "has_column_header": True,
                      "has_row_header": False, "children": children}}


def _prose_blocks(markdown: str, cap: int = 45):
    blocks = []
    for line in (markdown or "").splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            blocks.append(_h(m.group(2), min(3, len(m.group(1)))))
        elif s.startswith(("- ", "* ")):
            blocks.append(_bullet(s[2:]))
        else:
            blocks.append(_para(s))
    return blocks[:cap]


# ---------------------------------------------------------------- Notion delivery

def _schema(api_key: str, database_id: str) -> dict:
    try:
        r = requests.get(f"https://api.notion.com/v1/databases/{database_id}",
                         headers={"Authorization": f"Bearer {api_key}",
                                  "Notion-Version": NOTION_HEADERS_VERSION}, timeout=20)
        if r.status_code != 200:
            return {}
        return {n: p.get("type") for n, p in r.json().get("properties", {}).items()}
    except Exception:
        return {}


def _properties(schema: dict, title: str, metrics: dict, date_iso: Optional[str]):
    """Only set properties that actually exist in the DB, so it never errors."""
    title_name = next((n for n, t in schema.items() if t == "title"), "Name")
    props = {title_name: {"title": [{"text": {"content": title[:2000]}}]}}
    for name, val in metrics.items():
        t = schema.get(name)
        if t == "number" and isinstance(val, (int, float)):
            props[name] = {"number": round(float(val), 1)}
    if date_iso and schema.get("Date") == "date":
        props["Date"] = {"date": {"start": date_iso}}
    return props


def _data_tables(features: dict) -> list:
    blocks = []

    tc = features.get("by_time_class") or {}
    if tc:
        rows = [[k, str(v.get("games", "")), f"{v.get('score_pct','')}%",
                 str(v.get("accuracy", "") or "")] for k, v in tc.items()]
        blocks += [_h("By time control"),
                   _table(["Time control", "Games", "Score", "Accuracy"], rows)]

    acc = features.get("accuracy_by_phase") or {}
    lost = features.get("share_of_eval_lost_by_phase_pct") or {}
    if acc:
        rows = [[ph, str(acc.get(ph, "")), f"{lost.get(ph,'')}%"]
                for ph in ("opening", "middlegame", "endgame") if ph in acc]
        blocks += [_h("By phase"),
                   _table(["Phase", "Accuracy", "Share of eval lost"], rows)]

    conv = features.get("conversion") or {}
    if conv:
        rows = [["Reached a winning position", str(conv.get("reached_winning_position", "")),
                 f"{conv.get('won_from_winning_pct','')}% converted"],
                ["Reached a losing position", str(conv.get("reached_losing_position", "")),
                 f"{conv.get('saved_from_losing_pct','')}% saved"]]
        blocks += [_h("Conversion"), _table(["Situation", "Games", "Result"], rows)]

    ops = features.get("worst_openings") or []
    if ops:
        rows = [[o.get("name", "?")[:40], str(o.get("games", "")),
                 f"{o.get('score_pct','')}%", str(o.get("accuracy", "") or "")] for o in ops[:6]]
        blocks += [_h("Openings to watch"),
                   _table(["Opening", "Games", "Score", "Accuracy"], rows)]

    wm = features.get("worst_moments") or []
    if wm:
        rows = []
        for m in wm[:10]:
            url = m.get("game_url") or ""
            game = f"[view]({url})" if url else "—"
            rows.append([str(m.get("fullmove", "")), m.get("phase", ""),
                         m.get("played", "?"), m.get("engine_best", "?"),
                         f"−{m.get('win_drop_pct','')}%", game])
        blocks += [_h("Worst moments (flagged by Stockfish)"),
                   _para("The biggest single-move swings — open each game to study the position yourself."),
                   _table(["Move", "Phase", "Played", "Engine best", "Swing", "Game"], rows)]

    return blocks


def deliver_notion(api_key: str, database_id: str, title: str,
                   result: dict, features: dict, date_iso: Optional[str] = None):
    """Build a rich dashboard page: callout + prose + data tables + trend properties."""
    schema = _schema(api_key, database_id)

    overall = features.get("overall") or {}
    rating = features.get("rating") or {}
    metrics = {
        "Accuracy": overall.get("accuracy"),
        "Games": features.get("n_games"),
        "Rating": rating.get("end"),
        "Blunders": (features.get("error_counts") or {}).get("blunder"),
        "Score %": overall.get("score_pct"),
    }
    properties = _properties(schema, title, metrics, date_iso)

    children = []
    if result.get("headline"):
        children.append(_callout(result["headline"]))
    children.append(_h("Your coach's read"))
    children += _prose_blocks(result.get("digest_markdown", ""))
    children.append(_divider())
    children += _data_tables(features)
    children.append(_divider())
    children.append(_para("_Each digest is a row in this database — switch to a chart "
                          "view to track accuracy, rating and blunders over time._"))

    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
        "children": children[:100],
    }
    r = requests.post("https://api.notion.com/v1/pages",
                      headers={"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json",
                               "Notion-Version": NOTION_HEADERS_VERSION},
                      data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    return r.json().get("url")
