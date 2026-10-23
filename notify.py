#!/usr/bin/env python3
"""herdr plugin hook: push a QQ message on agent status changes.

Invoked by herdr with the event payload in HERDR_PLUGIN_EVENT_JSON.
Config lives in <plugin config dir>/config.toml (see config.example.toml).

Every notification is recorded against the agent it describes, so quoting it in
QQ routes back to that pane — see bot.py, the gateway daemon this hook keeps alive.
"""

import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import qq


def agent_info(pane_id: str) -> dict:
    """Enrich via `herdr agent get`: cwd, terminal title, session ref."""
    result, err = qq.herdr_json("agent", "get", pane_id, timeout=10)
    if err:
        qq.log(f"agent get failed for {pane_id}: {err}")
        return {}
    return result.get("agent", {})


def session_file(agent: str, session: dict) -> str:
    """Resolve the agent's transcript path from its herdr session ref."""
    kind, value = session.get("kind"), session.get("value", "")
    if kind == "path":
        if os.path.isfile(value):
            return value
        # stale ref (session rolled over): newest jsonl in the same dir
        d = os.path.dirname(value)
        hits = glob.glob(os.path.join(d, "*.jsonl")) if os.path.isdir(d) else []
        return max(hits, key=os.path.getmtime) if hits else ""
    if kind == "id" and value:
        home = os.path.expanduser("~")
        patterns = {
            "claude": f"{home}/.claude/projects/*/{value}.jsonl",
            "codex": f"{home}/.codex/sessions/*/*/*/rollout-*{value}.jsonl",
        }
        pat = patterns.get(agent)
        if pat:
            hits = glob.glob(pat)
            if hits:
                return max(hits, key=os.path.getmtime)
    return ""


def session_file_fallback(agent: str, cwd: str) -> str:
    """herdr reports no agent_session for some launches; find the newest
    transcript belonging to this cwd from the agent's own storage. The recency
    window keeps us honest when several agents share one directory."""
    if not cwd:
        return ""
    home = os.path.expanduser("~")
    now = time.time()
    fresh = 30 * 60
    if agent == "claude":
        slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)  # C:\Users\anyul → C--Users-anyul
        hits = glob.glob(os.path.join(home, ".claude", "projects", slug, "*.jsonl"))
    elif agent in ("pi", "omp"):  # omp is a pi-family CLI: same sessions layout
        root = ".omp" if agent == "omp" else ".pi"
        slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)  # sessions/--C--Users-anyul--/
        hits = [h for h in glob.glob(os.path.join(home, root, "agent", "sessions", "*", "*.jsonl"))
                if slug in os.path.basename(os.path.dirname(h))]
    elif agent == "agy":  # brain/<uuid>/.system_generated/logs/transcript.jsonl
        hits = glob.glob(os.path.join(home, ".gemini", "antigravity-cli", "brain", "*",
                                      ".system_generated", "logs", "transcript.jsonl"))
    else:
        return ""
    hits = [h for h in hits if now - os.path.getmtime(h) < fresh]
    return max(hits, key=os.path.getmtime) if hits else ""


TEXT_PARTS = ("text", "output_text", "input_text")  # codex writes user text as input_text


def _texts_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c["text"] for c in content
            if isinstance(c, dict) and c.get("type") in TEXT_PARTS and c.get("text")
        )
    return ""


def _head_lines(path: str, max_bytes: int = 600_000) -> list:
    try:
        with open(path, "rb") as f:
            return f.read(max_bytes).decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _tail_lines(path: str, max_bytes: int = 2_000_000) -> list:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - max_bytes))
            return f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def last_reply(path: str) -> str:
    """Last assistant text in a session jsonl (claude / codex / pi / generic shapes)."""
    for ln in reversed(_tail_lines(path)):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        # claude: type=assistant + message.role; pi: type=message + message.role;
        # codex: payload.role; generic: top-level role
        for holder in (o.get("message"), o.get("payload"), o):
            if isinstance(holder, dict) and holder.get("role") == "assistant":
                t = _texts_from_content(holder.get("content")).strip()
                if t:
                    return t
    return ""


GROK_SESSION = re.compile(r"(/[^\s]*/\.grok/sessions/[^/\s]+/[0-9a-fA-F][0-9a-fA-F-]{8,})/")


def last_reply_grok(cwd: str) -> str:
    """grok stores per-cwd sessions: ~/.grok/sessions/<urlencoded-cwd>/<id>/chat_history.jsonl

    (The Telegram original also matched the pane's live process via lsof, which is
    POSIX-only; the mtime fallback below is what runs on Windows.)
    """
    if not cwd:
        return ""
    base = os.path.join(os.path.expanduser("~/.grok/sessions"), urllib.parse.quote(cwd, safe=""))
    dirs = [d for d in glob.glob(os.path.join(base, "*")) if os.path.isdir(d)]
    if not dirs:
        return ""
    session = max(dirs, key=os.path.getmtime)
    hist = os.path.join(session, "chat_history.jsonl")
    for ln in reversed(_tail_lines(hist)):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("type") == "assistant":
            t = o.get("content")
            t = (t if isinstance(t, str) else _texts_from_content(t)).strip()
            if t:
                return t
    return ""


def kimi_wire(session_id: str) -> str:
    """kimi-code: session_index.jsonl maps a session id to its directory."""
    idx = os.path.expanduser("~/.kimi-code/session_index.jsonl")
    sdir = ""
    for ln in _tail_lines(idx):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("sessionId") == session_id:
            sdir = o.get("sessionDir", "")
    if not sdir:
        return ""
    wires = sorted(glob.glob(os.path.join(sdir, "agents", "*", "wire.jsonl")))
    main_wire = os.path.join(sdir, "agents", "main", "wire.jsonl")
    return main_wire if main_wire in wires else (wires[0] if wires else "")


def last_reply_kimi(session_id: str) -> str:
    """kimi text lives in wire.jsonl content.part events."""
    wire = kimi_wire(session_id)
    if not wire:
        return ""
    for ln in reversed(_tail_lines(wire)):
        try:
            e = json.loads(ln)
        except Exception:
            continue
        ev = e.get("event", {})
        if e.get("type") == "context.append_loop_event" and ev.get("type") == "content.part":
            part = ev.get("part", {})
            if part.get("type") == "text" and part.get("text", "").strip():
                return part["text"].strip()
    return ""


def last_reply_hermes(session_id: str) -> str:
    """hermes keeps transcripts in sqlite: ~/.hermes/state.db messages table."""
    import sqlite3

    db_path = os.path.expanduser("~/.hermes/state.db")
    if not os.path.isfile(db_path):
        return ""
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        row = db.execute(
            "select content from messages where session_id=? and role='assistant'"
            " and content is not null and content != '' order by id desc limit 1",
            (session_id,),
        ).fetchone()
        db.close()
        return (row[0] if row else "").strip()
    except Exception as e:
        qq.log(f"hermes db read failed: {e!r}")
        return ""


def last_reply_opencode(session_id: str) -> str:
    """opencode: sqlite ~/.local/share/opencode/opencode.db, message + part tables."""
    import sqlite3

    db_path = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if not os.path.isfile(db_path):
        return ""
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        msgs = db.execute(
            "select id, data from message where session_id=? order by time_created desc limit 20",
            (session_id,),
        ).fetchall()
        for mid, data in msgs:
            try:
                if json.loads(data).get("role") != "assistant":
                    continue
            except Exception:
                continue
            parts = db.execute(
                "select data from part where message_id=? order by time_created", (mid,)
            ).fetchall()
            texts = []
            for (pd,) in parts:
                try:
                    pj = json.loads(pd)
                except Exception:
                    continue
                if pj.get("type") == "text" and pj.get("text", "").strip():
                    texts.append(pj["text"].strip())
            if texts:
                db.close()
                return "\n".join(texts)
        db.close()
    except Exception as e:
        qq.log(f"opencode db read failed: {e!r}")
    return ""


# Agents front-load synthetic context as "user" messages — an AGENTS.md dump, a
# <user_info> block — before anything the human said.
SYNTHETIC = ("<instructions>", "<user_info>", "<system-reminder>", "<environment", "agents.md")

OPENCODE_DB = "~/.local/share/opencode/opencode.db"


def opencode_session(cwd: str, title: str) -> str:
    """Find an opencode session when herdr reports none — which it does intermittently,
    leaving the notification with no body and no title.

    opencode's own database records each session's directory and title, and it names the
    terminal "OC | <session title>", so the pane usually matches exactly. Falling back to
    the newest session in the directory is a guess: 68 of them share one directory here.
    """
    import sqlite3

    path = os.path.expanduser(OPENCODE_DB)
    if not cwd or not os.path.isfile(path):
        return ""
    wanted = title.split("|", 1)[1].strip() if "|" in title else title.strip()
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        if wanted:
            row = db.execute("select id from session where directory=? and title=?"
                             " order by time_updated desc limit 1", (cwd, wanted)).fetchone()
            if row:
                return row[0]
        row = db.execute("select id from session where directory=?"
                         " order by time_updated desc limit 1", (cwd,)).fetchone()
        if row:
            qq.log(f"opencode: no title match for {title!r}, using newest session in {cwd}")
            return row[0]
    except Exception as e:
        qq.log(f"opencode session lookup failed: {e!r}")
    return ""


def _usable_prompt(text: str) -> str:
    """Collapse a candidate first message, or "" if it is machine-generated context."""
    text = " ".join((text or "").split())
    if not text or text[0] in "<#" or any(s in text[:200].lower() for s in SYNTHETIC):
        return ""
    return text


def first_prompt_sql(agent: str, session_id: str) -> str:
    """First human message for the agents that keep transcripts in sqlite."""
    import sqlite3

    path = os.path.expanduser("~/.hermes/state.db" if agent == "hermes"
                              else "~/.local/share/opencode/opencode.db")
    if not session_id or not os.path.isfile(path):
        return ""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        if agent == "hermes":
            rows = db.execute(
                "select content from messages where session_id=? and role='user'"
                " and content is not null and content != '' order by id asc limit 10",
                (session_id,),
            ).fetchall()
            for (content,) in rows:
                found = _usable_prompt(content)
                if found:
                    return found
        else:
            msgs = db.execute(
                "select id, data from message where session_id=? order by time_created asc limit 20",
                (session_id,),
            ).fetchall()
            for mid, data in msgs:
                try:
                    if json.loads(data).get("role") != "user":
                        continue
                except Exception:
                    continue
                texts = []
                for (part,) in db.execute(
                        "select data from part where message_id=? order by time_created", (mid,)):
                    try:
                        parsed = json.loads(part)
                    except Exception:
                        continue
                    if parsed.get("type") == "text":
                        texts.append(parsed.get("text", ""))
                found = _usable_prompt(" ".join(texts))
                if found:
                    return found
    except Exception as e:
        qq.log(f"{agent} first-prompt read failed: {e!r}")
    return ""


def first_prompt(agent: str, session: dict, cwd: str, title: str = "") -> str:
    """The first thing the human actually typed in this session, or ""."""
    if agent in ("hermes", "opencode"):
        return first_prompt_sql(agent, resolve_session(agent, session, cwd, title))
    if agent == "grok":
        base = os.path.join(os.path.expanduser("~/.grok/sessions"), urllib.parse.quote(cwd, safe=""))
        dirs = [d for d in glob.glob(os.path.join(base, "*")) if os.path.isdir(d)]
        path = os.path.join(max(dirs, key=os.path.getmtime), "chat_history.jsonl") if dirs else ""
    elif agent == "kimi":
        path = kimi_wire(session.get("value", ""))
    else:
        path = session_file(agent, session) or session_file_fallback(agent, cwd)
    if not path:
        return ""
    for ln in _head_lines(path):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        text = ""
        if o.get("type") == "user" and "message" not in o:  # grok's shape
            text = _texts_from_content(o.get("content"))
        elif o.get("type") == "turn.prompt":                # kimi's shape
            text = _texts_from_content(o.get("input"))
        else:
            for holder in (o.get("message"), o.get("payload"), o):
                if isinstance(holder, dict) and holder.get("role") == "user":
                    text = _texts_from_content(holder.get("content"))
                    break
        found = _usable_prompt(text)
        if found:
            return found
    return ""


def uninformative(title: str, cwd: str, agent: str) -> bool:
    """Is herdr's terminal title just a banner? Some CLIs never set a real one: pi shows
    "π - <user>", a fresh Claude Code shows its own name, codex shows the directory."""
    t = title.strip().lower()
    if not t or t.startswith("π"):
        return True
    base = os.path.basename((cwd or "").rstrip("/")).lower()
    user = os.path.basename(os.path.expanduser("~")).lower()
    return t in (base, user, agent.lower(), "claude code", "codex", "pi", "grok")


def display_title(info: dict, agent: str) -> str:
    """A title worth showing: herdr's, or the session's opening prompt when herdr's says
    nothing. A pane with no conversation yet legitimately has neither."""
    title = (info.get("terminal_title_stripped") or info.get("terminal_title") or "").strip()
    cwd = info.get("cwd") or info.get("foreground_cwd") or ""
    if uninformative(title, cwd, agent):
        return first_prompt(agent, info.get("agent_session") or {}, cwd, title) or title
    return title


def resolve_session(agent: str, session: dict, cwd: str, title: str) -> str:
    """The session id herdr reported, or one worked out from the agent's own storage."""
    value = (session or {}).get("value", "")
    if not value and agent == "opencode":
        value = opencode_session(cwd, title)
    return value


def last_reply_agy(path: str) -> str:
    """agy transcript: the reply is the last PLANNER_RESPONSE (or final) entry."""
    for ln in reversed(_tail_lines(path)):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("type") in ("PLANNER_RESPONSE", "FINAL_RESPONSE", "ASSISTANT_MESSAGE"):
            t = str(o.get("content", "") or "").strip()
            if t:
                return t
    return ""


def reply_snippet(agent: str, session: dict, cwd: str) -> str:
    if agent == "grok":
        return last_reply_grok(cwd)
    if agent == "kimi":
        return last_reply_kimi(session.get("value", ""))
    if agent == "hermes":
        return last_reply_hermes(session.get("value", ""))
    if agent == "opencode":
        return last_reply_opencode(resolve_session(agent, session, cwd, ""))
    if agent == "agy":
        sf = session_file(agent, session) or session_file_fallback(agent, cwd)
        return last_reply_agy(sf) if sf else ""
    sf = session_file(agent, session) or session_file_fallback(agent, cwd)
    if not sf:
        return ""
    found = last_reply(sf)
    if found:
        qq.log(f"snippet: {os.path.basename(sf)} ({len(found)} chars)")
    return found


def transcript_step_lines(agent: str, path: str, offset: int,
                          compact: bool = True) -> tuple[str, int]:
    """New transcript entries since byte `offset`, formatted as progress lines:
    💭 thinking, 🔧 tool calls, · interim text. Returns ("", new_offset). The
    transcript is where an agent's intermediate steps actually live — the
    terminal TUI redraws in place and polls miss them. compact=True keeps each
    line short and drops interim text (the answer streams/closes separately)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return "", offset
    if not data:
        return "", offset
    lines = qq._decode(data).split("\n")
    remainder = lines.pop()  # partial trailing line unless the read ended on \n
    new_offset = offset + len(data) - len(remainder.encode("utf-8"))
    think_cap, tool_cap, text_cap = (60, 50, 120) if compact else (120, 100, 120)
    out = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if agent == "agy":
            # planner turns carry thinking + tool_calls as sibling fields; the
            # final answer never lands in this transcript (pane/server only)
            t = str(o.get("type", ""))
            content = str(o.get("content", "") or "").strip()
            if t == "PLANNER_RESPONSE":
                th = " ".join(str(o.get("thinking", "") or "").split())
                if th:
                    out.append("💭 " + th[:think_cap])
                for tc in (o.get("tool_calls") or []):
                    if isinstance(tc, dict):
                        name = str(tc.get("name", "?"))
                        args = tc.get("args") or {}
                        brief = (args.get("CommandLine") if isinstance(args, dict) else None) \
                            or (args.get("Arguments") if isinstance(args, dict) else None) \
                            or json.dumps(args, ensure_ascii=False)
                        out.append(f"🔧 {name}(" + " ".join(str(brief).split())[:tool_cap] + ")")
                continue
            if not content or t in ("USER_INPUT", "EPHEMERAL_MESSAGE", "SYSTEM_MESSAGE"):
                continue
            if "TOOL" in t:
                out.append("🔧 " + " ".join(content.split())[:tool_cap])
            elif "THINK" in t or "REASON" in t:
                out.append("💭 " + " ".join(content.split())[:think_cap])
            elif t in ("PLANNER_RESPONSE", "ASSISTANT_MESSAGE", "FINAL_RESPONSE", "GENERIC"):
                out.append("· " + " ".join(content.split())[:text_cap])
            continue
        msg = o.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "thinking":
                t = " ".join(str(c.get("thinking", "")).split())
                if t:
                    out.append("💭 " + t[:think_cap])
            elif ct in ("toolCall", "tool_use"):
                name = str(c.get("name", "?"))
                args = c.get("arguments") or c.get("input") or {}
                brief = (args.get("command") if isinstance(args, dict) else None) \
                    or (args.get("file_path") if isinstance(args, dict) else None) \
                    or json.dumps(args, ensure_ascii=False)
                out.append(f"🔧 {name}(" + " ".join(str(brief).split())[:tool_cap] + ")")
            elif ct == "text" and not compact:
                t = " ".join(str(c.get("text", "")).split())
                if t:
                    out.append("· " + t[:text_cap])
    return ("\n".join(out) + "\n") if out else "", new_offset


def reply_snippet_of_pane(pane_id: str) -> str:
    """Reply text for a pane without an event payload (streaming finale)."""
    info = agent_info(pane_id)
    cwd = info.get("cwd") or info.get("foreground_cwd") or ""
    agent = info.get("display_agent") or info.get("agent") or ""
    return tidy(reply_snippet(agent, info.get("agent_session") or {}, cwd))


def tidy(text: str) -> str:
    """Trim trailing spaces and collapse blank-line runs, keeping the markdown shape."""
    out: list[str] = []
    for line in text.strip().split("\n"):
        line = line.rstrip()
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out)


def ensure_bot(cfg: dict) -> None:
    """Keep the gateway daemon alive. The manifest's [[startup]] hook starts it with
    herdr; this is the safety net for a daemon that died or a plugin enabled mid-session."""
    if cfg.get("replies", True) and qq.app_configured(cfg):
        qq.spawn_bot()


def settled(pane_id: str, status: str, cfg: dict) -> bool:
    """Has the pane really reached `status`, or is this a flap?

    herdr infers agent state by reading the terminal, and some CLIs blink through a
    state mid-turn: grok flashes `blocked` for about a second while it starts up, which
    used to fire a notification before the agent had even been asked anything. Waiting a
    moment and re-reading the pane costs a few seconds of latency and removes the whole
    class of spurious pushes.
    """
    wait = int(cfg.get("settle_ms", 4000)) / 1000
    if wait <= 0:
        return True
    time.sleep(wait)
    now = agent_info(pane_id).get("agent_status", "")
    if not now or now == status:
        return True  # unchanged, or we can't tell — don't drop a real notification
    # `done` is terminal; herdr reports the pane as idle once it has been observed
    if status == "done" and now in ("idle", "done"):
        return True
    qq.log(f"unsettled: {pane_id} was {status}, now {now} — skipping")
    return False


def swap_last_status(pane_id: str, status: str) -> str:
    """Record this pane's status and return the previous one ("" if unseen)."""
    seen = qq.read_state("last_status.json", {})
    prev = seen.get(pane_id, "")
    if len(seen) > 500:
        seen = {}
    seen[pane_id] = status
    qq.write_state("last_status.json", seen)
    return prev


def recently_sent(key: str, quiet_seconds: int) -> bool:
    """Debounce: skip if the same pane+status fired within quiet_seconds."""
    if quiet_seconds <= 0:
        return False
    now = time.time()
    seen = qq.read_state("last_sent.json", {})
    last = seen.get(key, 0)
    seen = {k: v for k, v in seen.items() if now - v < 3600}
    seen[key] = now
    qq.write_state("last_sent.json", seen)
    return now - last < quiet_seconds


def main() -> None:
    cfg = qq.load_config()

    if "--test" in sys.argv:
        if cfg is None:
            print(f"config.toml not found in {qq.CONFIG_DIR}", file=sys.stderr)
            sys.exit(1)
        err = qq.send_update(cfg, "🔔 qqbot.bridge 测试消息", "",
                             {"pane": "", "agent": "test", "folder": ""})
        if err:
            print(f"send failed: {err}", file=sys.stderr)
            sys.exit(1)
        ensure_bot(cfg)
        return

    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON", "")
    if not raw:
        qq.log("no HERDR_PLUGIN_EVENT_JSON; exiting")
        return
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as e:
        qq.log(f"bad event json: {e}")
        return
    # payload shape: {"event": "pane_agent_status_changed", "data": {...fields...}}
    if isinstance(event.get("data"), dict):
        event = event["data"]
    if os.environ.get("QQ_NOTIFY_DEBUG") or "agent_status" not in event:
        qq.log(f"raw event: {raw[:500]}")

    status = event.get("agent_status", "unknown")
    pane_id = event.get("pane_id", "?")
    agent = event.get("display_agent") or event.get("agent") or "agent"
    title = event.get("title") or ""
    qq.log(f"event: pane={pane_id} agent={agent} status={status}")

    if cfg is None:
        return
    ensure_bot(cfg)
    # herdr only reports "done" for unfocused panes (attention semantics); a
    # focused pane goes working->idle directly. Treat that as done too.
    prev = swap_last_status(pane_id, status)
    if status == "idle" and prev == "working":
        status = "done"
        qq.log(f"working->idle on {pane_id}: treating as done (focused completion)")
    if status not in cfg.get("statuses", ["done"]):
        return
    agents = cfg.get("agents", [])
    if agents and (event.get("agent") or "") not in agents:
        return
    # confirm before the debounce is stamped, so a flap doesn't suppress the real one
    if not settled(pane_id, status, cfg):
        return
    if recently_sent(f"{pane_id}:{status}", int(cfg.get("quiet_seconds", 5))):
        qq.log(f"debounced pane={pane_id} status={status}")
        return

    info = agent_info(pane_id)
    cwd = info.get("cwd") or info.get("foreground_cwd") or ""
    folder = qq.short_home(cwd)
    title = display_title(info, event.get("agent") or "") or title
    reply = tidy(reply_snippet(event.get("agent") or "", info.get("agent_session") or {}, cwd))

    # a done push with no sniffed reply (agy's answer never hits disk) falls
    # back to the pane's visible text
    if status == "done" and not reply:
        out, err = qq.herdr_run("agent", "read", pane_id, "--lines", "25",
                                "--format", "text", timeout=10)
        if not err and out.strip():
            lines = [l.rstrip() for l in out.splitlines()
                     if l.strip() and not re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]", l)]
            reply = "\n".join(lines[-30:])

    # a blocked push without context is useless — show what the agent is asking
    if status == "blocked":
        out, err = qq.herdr_run("agent", "read", pane_id, "--lines", "6",
                                "--format", "text", timeout=10)
        if not err and out.strip():
            tail = " ".join(out.strip().split())[-350:]
            reply = (reply + "\n\n❓ " + tail) if reply else "❓ " + tail
        # numbered options in the pane become tappable buttons
        opts, _cur = qq.pane_options(pane_id)
        if opts and cfg.get("keyboard_mode", "markdown") != "off":
            scene0 = qq.scene_cfg(cfg)
            openid0 = qq.push_target(cfg)[1]
            if openid0:
                kb = qq.options_keyboard(pane_id, opts)
                sent = False
                for use_md in ((True, False) if cfg.get("markdown", True) else (False,)):
                    body = ({"msg_type": 2, "markdown": {"content": reply}} if use_md
                            else {"msg_type": 0, "content": reply})
                    body["keyboard"] = kb
                    result, kerr = qq.api(cfg, "POST",
                                          qq._messages_path(scene0, openid0), body)
                    if not kerr:
                        qq.remember_message(cfg, result or {},
                                            {"pane": pane_id, "agent": agent, "folder": folder})
                        sent = True
                        break
                    qq.log(f"blocked push keyboard attempt failed: {kerr.split()[0]}")
                if sent:
                    return  # keyboard push delivered; nothing else to send

    # a live progress stream that just covered this pane already showed the
    # answer — a second full-size push would dump it all over again
    stream = qq.read_state("stream_state.json", {}).get(pane_id) or {}
    if status == "done" and stream and time.time() - stream.get("ts", 0) < 90:
        qq.log(f"push suppressed for {pane_id}: progress stream just finished")
        return

    # one line, so a phone's notification preview spends its few lines on the reply
    # rather than on metadata. ✅/❓ already says done/blocked; the word is redundant.
    emoji = qq.STATUS_EMOJI.get(status, "")
    head = " · ".join(p for p in (f"{emoji} {agent}".strip(), folder or pane_id,
                                  title[:int(cfg.get("title_chars", 48))]) if p)
    err = qq.send_update(cfg, head, reply,
                         {"pane": pane_id, "agent": agent, "folder": folder})
    if err:
        qq.log(f"notification failed: {err}")


if __name__ == "__main__":
    main()
