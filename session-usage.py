#!/usr/bin/env python3
"""Report coding-agent session token usage to Herdr as pane metadata tokens.

OpenCode sessions come from its SQLite database; pi, Claude Code, and
GitHub Copilot sessions from their JSONL session files. Agents without
a known session format only get state tokens.

Feeds the sidebar agent rows configured in ~/.config/herdr/config.toml
([ui.sidebar.agents]):

  icon   — agent status glyph, reported for every agent kind:
           ✳ working, ⋯ background, ! blocked, ✓ done, ○ idle, ? unknown
           (colors come from sidebar rules in config.toml)
  state  — status text: Herdr's own state word, except that a stale
           "idle" is upgraded while OpenCode keeps work running in the
           background: "working" when the main session is actually
           mid-turn, "background" when subagents or background shells
           are still alive after the turn has ended (Herdr cannot see
           either of those on its own)
  model  — session model slug (e.g. claude-sonnet-5-5)
  ctx    — context used: tokens of the latest assistant message
           (input + cache.read + cache.write + output + reasoning for
           OpenCode, usage.totalTokens for pi, input + output + cache
           for Claude Code and Copilot, each as its own footer counts)
  cost   — cumulative session cost (e.g. $6.26; for Copilot the
           billed AI credits, e.g. 0.35cr)
  ch     — cache hit rate of the latest turn: cache.read / (input +
           cache.read + cache.write) (e.g. CH:98.6%)

Runs as a launchd agent (com.<user>.herdr-session-usage). Herdr expires
tokens via --ttl-ms when this reporter stops, so stale values vanish.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

HERDR = shutil.which("herdr") or "/opt/homebrew/bin/herdr"
OPENCODE = shutil.which("opencode") or str(
    Path.home() / ".opencode" / "bin" / "opencode"
)
DB = Path.home() / ".local/share/opencode/opencode.db"
PI_SESSIONS = Path.home() / ".pi/agent/sessions"
CLAUDE_HOME = Path(
    os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
)
COPILOT_HOME = Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")
SOURCE = "session-usage"
INTERVAL = 5.0
TTL_MS = int(3 * INTERVAL * 1000)  # expire if three cycles are missed


def herdr_json(*args):
    proc = subprocess.run(
        [HERDR, *args], capture_output=True, text=True, timeout=10
    )
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    if not text.startswith("{"):
        return None
    return json.loads(text)


def opencode_api(path):
    """GET an OpenCode HTTP API path; the CLI handles service and auth."""
    try:
        proc = subprocess.run(
            [OPENCODE, "api", "GET", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = proc.stdout.strip()
    if proc.returncode != 0 or not text.startswith("{"):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def active_sessions():
    """Ids of OpenCode sessions running right now (turns and subagents)."""
    data = opencode_api("/api/session/active")
    if not isinstance(data, dict):
        return set()
    return set((data.get("data") or {}).keys())


def shell_sessions(directory):
    """Ids of sessions owning a running background shell at directory."""
    if not directory:
        return set()
    data = opencode_api(
        "/api/shell?location[directory]=" + quote(directory, safe="/")
    )
    if not isinstance(data, dict):
        return set()
    sessions = set()
    for entry in data.get("data") or []:
        if entry.get("status") != "running":
            continue
        sid = (entry.get("metadata") or {}).get("sessionID")
        if sid:
            sessions.add(sid)
    return sessions


def humanize(n):
    if n >= 1_000_000:
        return "{:.1f}M".format(n / 1_000_000)
    if n >= 1_000:
        return "{:.1f}K".format(n / 1_000)
    return str(n)


def opencode_usage(db, sid):
    """Return (model, ctx, cost, ch) display strings or None."""
    row = db.execute(
        "SELECT model, cost FROM session_v2 WHERE id = ?", (sid,)
    ).fetchone()
    if row is None:
        return None
    try:
        model = json.loads(row[0])["id"]
    except Exception:
        model = "?"

    ctx = ch = None
    # The newest message may still be streaming and lack token data;
    # use the latest completed one (same as OpenCode's own footer).
    msg = db.execute(
        "SELECT data FROM session_message WHERE session_id = ? "
        "AND type = 'assistant' "
        "AND json_extract(data, '$.tokens') IS NOT NULL "
        "ORDER BY seq DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if msg is not None:
        try:
            tokens = json.loads(msg[0]).get("tokens")
        except Exception:
            tokens = None
        if tokens:
            cache = tokens.get("cache") or {}
            read = cache.get("read") or 0
            write = cache.get("write") or 0
            prompt = (tokens.get("input") or 0) + read + write
            total = (
                prompt
                + (tokens.get("output") or 0)
                + (tokens.get("reasoning") or 0)
            )
            ctx = humanize(total)
            if prompt > 0:
                ch = "CH:{:.1f}%".format(100.0 * read / prompt)

    return model, ctx, "${:.2f}".format(row[1]), ch


def pi_session_file(session):
    """Path of the pi session JSONL behind a Herdr agent_session ref."""
    value = session.get("value")
    if not value:
        return None
    if session.get("kind") == "path" or value.endswith(".jsonl"):
        path = Path(value)
    else:
        # agent_session_id: <cwd slug>/<timestamp>_<id>.jsonl under PI_SESSIONS.
        matches = sorted(PI_SESSIONS.glob("*/*_%s.jsonl" % value))
        if not matches:
            return None
        path = matches[-1]
    return path if path.is_file() else None


def pi_usage(path):
    """Return (model, ctx, cost, ch) display strings or None.

    Pi appends one JSONL entry per event. Usage sits on assistant and
    toolResult messages and on compaction and branch-summary entries.
    ctx is usage.totalTokens of the latest completed assistant message,
    ch the cache hit rate of its prompt, and cost the session total.
    """
    if path is None:
        return None
    model = None
    cost = 0.0
    ctx = ch = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a torn line while pi appends
                kind = entry.get("type")
                if kind == "model_change":
                    model = entry.get("modelId") or model
                    continue
                msg = entry.get("message") or {}
                usage = msg.get("usage") or entry.get("usage")
                if not usage:
                    continue
                cost += (usage.get("cost") or {}).get("total") or 0
                if kind != "message" or msg.get("role") != "assistant":
                    continue
                model = msg.get("model") or model
                if msg.get("stopReason") in ("aborted", "error"):
                    continue
                total = usage.get("totalTokens") or (
                    (usage.get("input") or 0)
                    + (usage.get("output") or 0)
                    + (usage.get("cacheRead") or 0)
                    + (usage.get("cacheWrite") or 0)
                )
                if total:
                    ctx = humanize(total)
                prompt = (
                    (usage.get("input") or 0)
                    + (usage.get("cacheRead") or 0)
                    + (usage.get("cacheWrite") or 0)
                )
                if prompt:
                    ch = "CH:{:.1f}%".format(
                        100.0 * (usage.get("cacheRead") or 0) / prompt
                    )
    except OSError:
        return None
    return model or "?", ctx, "${:.2f}".format(cost), ch


def claude_session_file(session):
    """Path of the Claude Code transcript behind a Herdr agent_session ref."""
    value = session.get("value")
    if not value:
        return None
    if session.get("kind") == "path" or value.endswith(".jsonl"):
        path = Path(value)
    else:
        # agent_session_id: <cwd slug>/<id>.jsonl under ~/.claude/projects.
        matches = sorted((CLAUDE_HOME / "projects").glob("*/*%s.jsonl" % value))
        if not matches:
            return None
        path = matches[-1]
    return path if path.is_file() else None


def claude_usage(path):
    """Return (model, ctx, cost, ch) display strings or None.

    Claude Code appends one JSONL entry per event. Usage sits on
    assistant messages; newer cost-state entries carry the cumulative
    session cost. ctx and ch follow the main thread, so sidechain entries
    from subagents are skipped for those but still counted in the
    per-message cost fallback.
    """
    if path is None:
        return None
    model = None
    ctx = ch = None
    cost = None
    message_cost = 0.0
    saw_message_cost = False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a torn line while Claude Code appends
                kind = entry.get("type")
                if kind == "cost-state":
                    total = entry.get("totalCostUSD")
                    if isinstance(total, (int, float)):
                        cost = total  # cumulative; the last state wins
                    for name in entry.get("modelUsage") or {}:
                        model = model or name
                    continue
                if kind != "assistant":
                    continue
                msg = entry.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                per = entry.get("costUSD")
                if isinstance(per, (int, float)):
                    message_cost += per
                    saw_message_cost = True
                if entry.get("isSidechain"):
                    continue
                model = msg.get("model") or model
                read = usage.get("cache_read_input_tokens") or 0
                write = usage.get("cache_creation_input_tokens") or 0
                prompt = (usage.get("input_tokens") or 0) + read + write
                total = prompt + (usage.get("output_tokens") or 0)
                if total:
                    ctx = humanize(total)
                if prompt:
                    ch = "CH:{:.1f}%".format(100.0 * read / prompt)
    except OSError:
        return None
    if cost is None and saw_message_cost:
        cost = message_cost
    if cost is not None:
        cost = "${:.2f}".format(cost)
    return model or "?", ctx, cost, ch


def copilot_session_file(session):
    """Path of the Copilot events.jsonl behind a Herdr agent_session ref."""
    value = session.get("value")
    if not value:
        return None
    if session.get("kind") == "path" or value.endswith(".jsonl"):
        path = Path(value)
    else:
        # agent_session_id: ~/.copilot/session-state/<id>/events.jsonl.
        path = COPILOT_HOME / "session-state" / value / "events.jsonl"
    return path if path.is_file() else None


def copilot_usage(path):
    """Return (model, ctx, cost, ch) display strings or None.

    Copilot persists session.usage_record receipts and periodic
    session.usage_checkpoint snapshots to events.jsonl; live
    assistant.usage events are ephemeral. A checkpoint subsumes the
    receipts before it, so totals restart after each checkpoint. cost is
    the accumulated nano-AI units shown as credits (totalNanoAiu / 1e9),
    the unit the Copilot footer bills in.
    """
    if path is None:
        return None
    model = None
    ctx = ch = None
    nano_aiu = 0.0
    records = []
    premium = 0.0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a torn line while Copilot appends
                kind = entry.get("type")
                data = entry.get("data") or {}
                if kind == "session.model_change":
                    model = (
                        data.get("newModel")
                        or data.get("model")
                        or data.get("modelId")
                        or model
                    )
                elif kind == "session.usage_checkpoint":
                    records = []  # the checkpoint subsumes earlier receipts
                    nano_aiu = data.get("totalNanoAiu") or 0
                    premium = data.get("totalPremiumRequests") or 0
                    for metric in data.get("providerModelMetrics") or []:
                        model = metric.get("modelId") or model
                elif kind in ("session.usage_record", "assistant.usage"):
                    usage = data.get("usage") or data
                    if usage:
                        records.append(usage)
    except OSError:
        return None

    for usage in records:
        model = usage.get("model") or model
        copilot = usage.get("copilotUsage") or {}
        nano_aiu += usage.get("totalNanoAiu") or copilot.get("totalNanoAiu") or 0

    if records:
        usage = records[-1]
        read = usage.get("cacheReadTokens") or 0
        write = usage.get("cacheWriteTokens") or 0
        prompt = (usage.get("inputTokens") or 0) + read + write
        total = prompt + (usage.get("outputTokens") or 0)
        if total:
            ctx = humanize(total)
        if prompt:
            ch = "CH:{:.1f}%".format(100.0 * read / prompt)

    cost = None
    credits = nano_aiu / 1_000_000_000
    if credits > 0:
        cost = "{:.2f}cr".format(credits)
    elif premium > 0:
        cost = "{:.0f}req".format(premium)
    return model or "?", ctx, cost, ch


def agent_usage(session, db):
    """Return (model, ctx, cost, ch) for a Herdr agent_session, or None."""
    agent = session.get("agent")
    if agent == "opencode":
        sid = session.get("value")
        return opencode_usage(db, sid) if sid else None
    if agent == "pi":
        return pi_usage(pi_session_file(session))
    if agent == "claude":
        return claude_usage(claude_session_file(session))
    if agent == "copilot":
        return copilot_usage(copilot_session_file(session))
    return None


ICONS = {
    "working": "\u2733",  # ✳
    "background": "\u22ef",  # ⋯
    "blocked": "!",
    "done": "\u2713",  # ✓
    "idle": "\u25cb",  # ○
    "unknown": "?",
}


def report_once(db, log):
    data = herdr_json("agent", "list")
    if not data:
        return 0
    agents = (data.get("result") or {}).get("agents") or []

    active = None  # resolved once per cycle, only if actually needed
    shell_dirs = {}

    def active_now():
        nonlocal active
        if active is None:
            active = active_sessions()
        return active

    def shells_in(directory):
        if directory not in shell_dirs:
            shell_dirs[directory] = shell_sessions(directory)
        return shell_dirs[directory]

    reported = 0
    for agent in agents:
        pane = agent.get("pane_id")
        if not pane:
            continue
        status = agent.get("agent_status") or "unknown"
        session = agent.get("agent_session") or {}
        sid = session.get("value") if session.get("agent") == "opencode" else None

        # OpenCode keeps subagents and background shells alive after a
        # turn ends, while Herdr's own state falls back to "idle". Upgrade
        # that stale state to what is really going on.
        if sid and status in ("idle", "unknown"):
            if sid in active_now():
                status = "working"  # main session is actually mid-turn
            else:
                # Subagent sessions and their shells outlive the turn and
                # can nest. Match them purely against OpenCode's live data:
                # the session itself, any descendant, and any running shell
                # they own (with the pane's directory as the shell scope).
                # Note: time_suspended/time_archived are set even on
                # sessions that are still running, so those flags cannot
                # be used to filter dead sessions out.
                family = {sid}
                family.update(
                    row[0]
                    for row in db.execute(
                        "WITH RECURSIVE tree(id) AS ("
                        " SELECT id FROM session_v2 WHERE parent_id = ?"
                        " UNION ALL"
                        " SELECT s.id FROM session_v2 s JOIN tree t"
                        " ON s.parent_id = t.id"
                        ") SELECT id FROM tree",
                        (sid,),
                    )
                )
                if not family.isdisjoint(active_now()):
                    status = "background"  # a subagent is still running
                elif not family.isdisjoint(
                    shells_in(agent.get("cwd") or agent.get("foreground_cwd"))
                ):
                    status = "background"  # a background shell is still running

        tokens = []
        icon = ICONS.get(status)
        if icon:
            tokens.append("icon=" + icon)
        tokens.append("state=" + status)
        usage = agent_usage(session, db)
        if usage:
            model, ctx, cost, ch = usage
            if model:
                tokens.append("model=" + model)
            if cost:
                tokens.append("cost=" + cost)
            if ctx:
                tokens.append("ctx=" + ctx)
            if ch:
                tokens.append("ch=" + ch)
        args = [HERDR, "pane", "report-metadata", pane, "--source", SOURCE]
        for token in tokens:
            args += ["--token", token]
        args += ["--ttl-ms", str(TTL_MS)]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            reported += 1
            if log:
                print("%s <- %s" % (pane, " ".join(tokens)), flush=True)
    return reported


def main():
    once = "--once" in sys.argv

    def connect():
        return sqlite3.connect(
            "file:{}?mode=ro".format(DB), uri=True, timeout=3
        )

    db = connect()
    while True:
        try:
            report_once(db, once)
        except sqlite3.Error as exc:
            print("session-usage: db error: %s" % exc, file=sys.stderr, flush=True)
            try:
                db.close()
            except Exception:
                pass
            time.sleep(1)
            db = connect()
        except Exception as exc:
            print("session-usage: %s" % exc, file=sys.stderr, flush=True)
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
