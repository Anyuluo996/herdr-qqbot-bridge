#!/usr/bin/env python3
"""herdr plugin daemon: hold the QQ gateway WebSocket and turn QQ replies back
into agent input.

Command layer (designed for a multi-agent manager — the plugin takes over as
little as possible):

  # prefix   plugin commands: #agents #use #start #close #status #read #esc #key #help
  / prefix   passed through verbatim to the current agent (/model, /clear, ...)
  plain text submitted to the current agent as a prompt

A quote reply to one of the plugin's notifications routes to that notification's
agent; a plain message goes to the most recently active agent. Files are saved
to the plugin's inbox and the agent is handed their path.

  bot.py --daemon   run the gateway loop in the foreground
  bot.py --ensure   start the daemon if it isn't already running, then exit
  bot.py --stop     stop the running daemon
  bot.py --restart  stop it and start a fresh one (picks up edited code)
  bot.py --status   report whether the daemon is running
"""

import json
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime, timezone

import qq
import qqws

MAX_BUTTONS = 8
KEY_RE = re.compile(r"^[\w+-]{1,16}$")  # key names allowed for #key

HELP = (
    "**插件指令**（# 开头）\n"
    "· #agents — 列出 agent，#use 序号/名称 切换\n"
    "· #start 类型 — 启动 agent（别名 #启动）\n"
    "· #close 名称 — 关闭（别名 #停止）\n"
    "· #status — 全部 agent 状态总览\n"
    "· #sh 命令 — 本机执行 shell（需 allow_shell）\n"
    "· #read 行数 — 看当前 agent 的终端输出\n"
    "· #esc — 发 Esc 中断；#key 键名 — 发按键\n"
    "· #help — 本说明\n\n"
    "**透传**（插件不接管）\n"
    "· 直接发文字 = 当作 prompt 发给当前 agent（工作时 QQ 里实时流式显示思维链/工具调用）\n"
    "· /xxx = agent 自己的命令（/model /clear…）\n"
    "· 引用我的通知 = 精确路由到那条通知对应的 agent"
)

# #help quick-menu: 指令按钮 fill the input box (enter=true auto-sends in c2c)
HELP_BUTTONS = ["#agents", "#status", "#start", "#read", "#esc", "#help"]


def help_keyboard(scene: str) -> dict:
    rows = []
    for i in range(0, len(HELP_BUTTONS), 3):
        rows.append({"buttons": [{
            "id": f"h{i + j}",
            "render_data": {"label": btn[:10], "style": 1},
            "action": {"type": 2, "permission": {"type": 2}, "data": btn,
                       "enter": scene == "c2c"},
        } for j, btn in enumerate(HELP_BUTTONS[i:i + 3])]})
    return {"content": {"rows": rows}}

GONE = ("not found", "agent_not_found", "pane_not_found", "invalid pane")


def stop_requested(started_at: float) -> bool:
    """The stop flag's mtime must postdate this daemon, or every daemon would
    see the previous run's flag."""
    try:
        return os.path.getmtime(qq.stop_flag_path()) >= started_at
    except OSError:
        return False


def agent_status(pane: str) -> str:
    info, err = qq.herdr_json("agent", "get", pane, timeout=10)
    return "" if err else info.get("agent", {}).get("agent_status", "")


def target_from_agent(agent: dict) -> dict:
    return {
        "pane": agent.get("pane_id", ""),
        "agent": agent.get("display_agent") or agent.get("agent") or "agent",
        "folder": qq.short_home(agent.get("cwd") or agent.get("foreground_cwd") or ""),
    }


def list_agents() -> list:
    """herdr's agents, most recently active first — the one canonical order
    shared by #agents, #use and #status."""
    result, err = qq.herdr_json("agent", "list")
    if err:
        return []
    agents = [a for a in result.get("agents", []) if a.get("pane_id")]
    agents.sort(key=lambda a: a.get("state_change_seq", 0), reverse=True)
    return agents


def agent_label(agent: dict) -> str:
    name = agent.get("name") or agent.get("display_agent") or agent.get("agent") or "agent"
    kind = agent.get("agent") or ""
    where = qq.short_home(agent.get("cwd") or "")
    base = os.path.basename(where.rstrip("/\\")) if where else ""
    parts = [name]
    if kind and kind != name:
        parts.append(kind)
    if base:
        parts.append(base)
    return " · ".join(parts)


def submit(cfg: dict, pane: str, text: str) -> str:
    """Send text to an agent and make sure it actually goes in. Returns an error, or "".

    `herdr agent prompt` writes the text and the submit key in one burst. An agent that
    is already working queues that fine, but an idle Claude Code folds the trailing
    Enter into its paste-detection window — the text just sits in the input box. So when
    the agent wasn't working to begin with, wait for it to pick the prompt up, and press
    Enter once more if it never does.
    """
    was_working = agent_status(pane) == "working"
    _, err = qq.herdr_run("agent", "prompt", pane, text, timeout=60)
    if err:
        return err
    if was_working or not cfg.get("submit_fallback", True):
        return ""
    deadline = time.monotonic() + int(cfg.get("submit_wait_ms", 2500)) / 1000
    while time.monotonic() < deadline:
        time.sleep(0.25)
        status = agent_status(pane)
        if not status or status == "working":
            return ""  # picked it up (or we can't tell — better than a double submit)
    qq.log(f"bot: {pane} never started working; pressing enter to submit")
    _, err = qq.herdr_run("agent", "send-keys", pane, "enter")
    return err


# --- outgoing ----------------------------------------------------------------


def reply(cfg: dict, scene: str, openid: str, content: str, msg_id: str | None = None,
          event_id: str | None = None, target: dict | None = None) -> None:
    """Send one message, remembering which agent it is about so quoting it routes back."""
    use_md = bool(cfg.get("markdown", True))
    if scene == "group" and cfg.get("strip_group_urls", True):
        content = qq.strip_urls(content)
    result, err = qq.send_text(cfg, scene, openid, content, msg_id=msg_id, event_id=event_id,
                               markdown_content=content if use_md else None)
    if err:
        qq.log(f"bot: reply failed: {err}: {content[:80]!r}")
        return
    if target:
        qq.remember_message(cfg, result or {}, target)


def report_failure(cfg: dict, scene: str, openid: str, msg_id: str, target: dict, err: str) -> None:
    """A closed pane is the common case and deserves plain language."""
    if any(m in err.lower() for m in GONE):
        reply(cfg, scene, openid, f"🚪 {qq.label(target)} 已关闭（pane 不在了）。\n#agents 另选，#start 重开。", msg_id)
    else:
        reply(cfg, scene, openid, f"⚠️ {qq.label(target)}: {err}", msg_id)


# --- plugin commands ---------------------------------------------------------


def button_label(agent: dict) -> str:
    """QQ button labels cap at 10 characters."""
    name = agent.get("name") or agent.get("display_agent") or agent.get("agent") or "agent"
    emoji = qq.STATUS_EMOJI.get(agent.get("agent_status", ""), "")
    return (f"{emoji}{name}")[:10] or "agent"


def agent_keyboard(agents: list) -> dict:
    """One callback button per agent; tapping it sends an INTERACTION_CREATE we
    turn into the active target."""
    rows = []
    for i, a in enumerate(agents[:MAX_BUTTONS]):
        rows.append({"buttons": [{
            "id": f"ag{i}",
            "render_data": {"label": button_label(a), "style": 1},
            "action": {"type": 1, "permission": {"type": 2}, "data": a.get("pane_id", "")},
        }]})
    return {"content": {"rows": rows}}


def agents_text(agents: list) -> str:
    lines = []
    for i, a in enumerate(agents, 1):
        emoji = qq.STATUS_EMOJI.get(a.get("agent_status", ""), "❔")
        lines.append(f"{i}. {emoji} {agent_label(a)}")
    return "\n".join(lines)


def send_with_keyboard(cfg: dict, scene: str, openid: str, text: str, keyboard: dict,
                       msg_id: str | None = None, target: dict | None = None) -> None:
    """#agents panel: the list as text, plus a keyboard in whatever wrap the bot
    actually renders (config keyboard_mode). QQ inline keyboards are behind a
    platform review — bots without it get the text list only, so the API answer
    is not trusted beyond "accepted" and a text menu always rides along."""
    mode = str(cfg.get("keyboard_mode", "markdown")).lower()
    if mode in ("", "off", "none"):
        reply(cfg, scene, openid, text, msg_id, target=target)
        return
    if mode == "text":
        body = {"msg_type": 0, "content": text, "keyboard": keyboard}
    else:  # markdown
        body = {"msg_type": 2, "markdown": {"content": text}, "keyboard": keyboard}
    qq._attach_passive(body, scene, msg_id, None)
    result, err = qq.api(cfg, "POST", qq._messages_path(scene, openid), body)
    if err:
        qq.log(f"bot: keyboard send failed ({err}); falling back to text")
        if err.split()[0] in qq.MARKDOWN_FALLBACK_CODES and mode == "markdown":
            body = {"msg_type": 0, "content": text, "keyboard": keyboard}
            qq._attach_passive(body, scene, msg_id, None)
            result, err = qq.api(cfg, "POST", qq._messages_path(scene, openid), body)
        if err:
            reply(cfg, scene, openid, text, msg_id, target=target)
            return
    if target:
        qq.remember_message(cfg, result or {}, target)


def cmd_agents(cfg: dict, scene: str, openid: str, msg_id: str) -> None:
    agents = list_agents()
    if not agents:
        reply(cfg, scene, openid, "🫙 没有在跑的 agent — 用 #start <类型> 启动，如 #start pi", msg_id)
        return
    text = "🤖 agent 列表（#use 序号或名称 选择，点按钮同样有效）：\n" + agents_text(agents)
    send_with_keyboard(cfg, scene, openid, text, agent_keyboard(agents), msg_id)


def pick_agent(agents: list, token: str) -> dict | None:
    """Match '#use' argument by 1-based index (the #agents order) or name part."""
    token = token.strip()
    if token.isdigit():
        i = int(token) - 1
        return agents[i] if 0 <= i < len(agents) else None
    for a in agents:
        for candidate in (a.get("name"), a.get("display_agent"), a.get("agent")):
            if candidate and token.lower() in str(candidate).lower():
                return a
    return None


def cmd_use(cfg: dict, scene: str, openid: str, msg_id: str, args: list) -> None:
    agents = list_agents()
    if not agents:
        reply(cfg, scene, openid, "🫙 没有在跑的 agent — #start 先启动一个", msg_id)
        return
    if not args:
        reply(cfg, scene, openid, "用法：#use <序号|名称>\n" + agents_text(agents), msg_id)
        return
    agent = pick_agent(agents, args[0])
    if not agent:
        reply(cfg, scene, openid, f"🤷 没找到 {args[0]!r} — #agents 看列表", msg_id)
        return
    target = target_from_agent(agent)
    qq.set_current_target(target)
    reply(cfg, scene, openid, f"🎯 {qq.label(target)}\n现在直接发消息即可", msg_id, target=target)


def cmd_status(cfg: dict, scene: str, openid: str, msg_id: str) -> None:
    agents = list_agents()
    if not agents:
        reply(cfg, scene, openid, "🫙 没有在跑的 agent — #start <类型> 启动", msg_id)
        return
    current = qq.current_target().get("pane", "")
    lines = []
    for i, a in enumerate(agents, 1):
        emoji = qq.STATUS_EMOJI.get(a.get("agent_status", ""), "❔")
        mark = " 👈当前" if a.get("pane_id") == current else ""
        lines.append(f"{i}. {emoji} {agent_label(a)}{mark}")
    state = {"done": "完成", "blocked": "等你输入", "working": "干活中",
             "idle": "空闲", "unknown": "未知"}
    cur = next((a for a in agents if a.get("pane_id") == current), None)
    tail = f"\n当前目标状态：{state.get(cur.get('agent_status'), '未知')}" if cur else ""
    reply(cfg, scene, openid, "📊 " + "；".join(
        state.get(a.get("agent_status"), "未知") for a in agents) + "\n" + "\n".join(lines) + tail,
        msg_id)


# --- #start ------------------------------------------------------------------


def free_pane(cfg: dict) -> tuple[str, str]:
    """A pane id an agent can start in, or ("", err). Reuses an idle shell pane
    before splitting a new one off the focused pane."""
    agents = list_agents()
    used = {a.get("pane_id") for a in agents}
    result, err = qq.herdr_json("pane", "list")
    if err:
        return "", f"pane list: {err}"
    panes = result.get("panes", [])
    free = [p for p in panes
            if p.get("pane_id") not in used and p.get("agent_status", "unknown") in ("", "unknown")]
    if free:
        # the focused pane is whatever the user is typing in — take it only as a
        # last resort
        pick = next((p for p in free if not p.get("focused")), free[0])
        return pick["pane_id"], ""
    focused = next((p for p in panes if p.get("focused")), panes[0] if panes else None)
    if not focused:
        return "", "没有可用 pane，也没有可拆分的焦点 pane"
    result, err = qq.herdr_json("pane", "split", focused["pane_id"], "--direction", "right")
    if err:
        return "", f"pane split: {err}"
    return (result.get("pane") or {}).get("pane_id", ""), ""


def configured_agent(cfg: dict, name: str) -> dict | None:
    for entry in cfg.get("agent", []):
        if entry.get("name") == name:
            return entry
    return None


def cmd_start(cfg: dict, scene: str, openid: str, msg_id: str, args: list) -> None:
    if not args:
        names = [e.get("name", "?") for e in cfg.get("agent", [])]
        reply(cfg, scene, openid,
              "用法：#start <类型|配置名> [agent参数…]\n"
              "类型：pi / claude / codex / gemini / opencode / kimi / grok / qwen 等\n"
              "已配置：" + ("、".join(names) if names else "无（config.toml 里可加 [[agent]]）"),
              msg_id)
        return
    name = args[0]
    entry = configured_agent(cfg, name)
    if entry:
        kind = entry.get("kind") or name
        agent_name = entry.get("name") or kind
        extra = [str(a) for a in entry.get("args", [])] + args[1:]
    else:
        kind = name
        agent_name = name
        extra = args[1:]

    taken = {a.get("name") for a in list_agents()}
    base = agent_name
    n = 2
    while agent_name in taken:
        agent_name = f"{base}-{n}"
        n += 1

    pane, err = free_pane(cfg)
    if err:
        reply(cfg, scene, openid, f"⚠️ {err}", msg_id)
        return

    # [[agent]].env: the pane's shell gets the vars before the agent starts —
    # how agy gets its proxy (the pane shell persists them for the session)
    env = {str(k): str(v) for k, v in ((entry or {}).get("env") or {}).items()}
    if env:
        pinfo, perr = qq.herdr_json("pane", "get", pane, timeout=10)
        title = str((pinfo.get("pane") or pinfo or {}).get("terminal_title", "")).lower() \
            if not perr else ""
        if "pwsh" in title or "powershell" in title or "cmd" in title:
            line = "; ".join(f"$env:{k}='{v}'" for k, v in env.items())
        else:
            line = " ".join(f"export {k}='{v}'" for k, v in env.items())
        _, env_err = qq.herdr_run("pane", "run", pane, line, timeout=15)
        if env_err:
            reply(cfg, scene, openid, f"⚠️ 环境变量注入失败：{env_err}", msg_id)
            return
        time.sleep(1)

    reply(cfg, scene, openid, f"🚀 正在启动 {kind} → pane {pane}（名称 {agent_name}）", msg_id)
    cmd = ["agent", "start", agent_name, "--kind", kind, "--pane", pane]
    if extra:
        cmd += ["--"] + extra
    _, err = qq.herdr_run(*cmd, timeout=60)
    if err and "not ready" not in err.lower() and "blocked" not in err.lower():
        reply(cfg, scene, openid, f"⚠️ 启动失败：{err}", msg_id)
        return

    # startup prompts (folder trust, theme pickers) leave the agent blocked —
    # surface the pane tail so the user can drive it with #key from QQ
    deadline = time.monotonic() + 15
    status = ""
    while time.monotonic() < deadline:
        time.sleep(2)
        status = agent_status(pane)
        if status in ("idle", "working", "blocked"):
            break
    if status in ("idle", "working"):
        target = {"pane": pane, "agent": agent_name, "folder": ""}
        qq.set_current_target(target)
        reply(cfg, scene, openid,
              f"✅ {agent_name}（{kind}）已就绪，已设为当前目标，直接发消息即可", msg_id, target=target)
        return
    out, _ = qq.herdr_run("agent", "read", pane, "--lines", "8", "--format", "text")
    body = (out.strip() or "（无输出）")[-400:]
    reply(cfg, scene, openid,
          f"⏳ {agent_name} 卡在启动提示上（状态 {status or '启动中'}）：\n{body}\n"
          f"用 #key down / #key enter 这类按键驱动它，#status 看进度", msg_id)


def cmd_close(cfg: dict, scene: str, openid: str, msg_id: str, args: list) -> None:
    agents = list_agents()
    if not args:
        reply(cfg, scene, openid, "用法：#close <名称|序号>\n" + (agents_text(agents) or "（无）"), msg_id)
        return
    agent = pick_agent(agents, args[0])
    if not agent:
        reply(cfg, scene, openid, f"🤷 没找到 {args[0]!r}", msg_id)
        return
    pane = agent.get("pane_id", "")
    _, err = qq.herdr_run("pane", "close", pane, timeout=15)
    if err:
        reply(cfg, scene, openid, f"⚠️ 关闭失败：{err}", msg_id)
        return
    if qq.current_target().get("pane") == pane:
        qq.write_state(qq.CURRENT_TARGET, {})
    reply(cfg, scene, openid, f"🚪 已关闭 {agent_label(agent)}", msg_id)


def cmd_sh(cfg: dict, scene: str, openid: str, msg_id: str, command: str, target: dict) -> None:
    """#! runs on THIS machine — the whole point of a personal bridge, and also
    the most dangerous button in the house, hence the config gate."""
    if not cfg.get("allow_shell", False):
        reply(cfg, scene, openid, "本地命令执行已关闭 — config.toml 里 allow_shell = true 开启", msg_id)
        return
    if not command:
        reply(cfg, scene, openid, "用法：#sh <命令> — 在本机执行（工作目录跟随当前 agent）", msg_id)
        return
    cwd = ""
    if target.get("pane"):
        info, err = qq.herdr_json("agent", "get", target["pane"], timeout=10)
        if not err:
            cwd = info.get("agent", {}).get("cwd") or ""
    out, err = qq.run_shell(cfg, command, cwd=cwd, timeout=int(cfg.get("shell_timeout", 30)))
    text = f"$ {command}\n" + (err if err else (out or "（无输出）"))
    limit = int(cfg.get("chunk_chars", 1000)) * 2
    if len(text) > limit:
        text = "…\n" + text[-limit:]
    reply(cfg, scene, openid, text, msg_id, target=target)


def cmd_read(cfg: dict, scene: str, openid: str, msg_id: str, target: dict, args: list) -> None:
    lines = args[1] if len(args) > 1 and args[1].isdigit() else str(cfg.get("read_lines", 40))
    out, err = qq.herdr_run("agent", "read", target["pane"], "--lines", lines, "--format", "text")
    if err:
        report_failure(cfg, scene, openid, msg_id, target, err)
        return
    body = out.strip() or "（无输出）"
    head = f"📄 {qq.label(target)}"
    content = f"{head}\n{body}"
    if len(content) > int(cfg.get("chunk_chars", 1000)) * 2:
        # one message can't hold it and a passive reply may only span a few
        # messages anyway — the whole output goes over as a file
        result, ferr = qq.send_text_as_file(cfg, scene, openid, content, msg_id=msg_id)
        if ferr:
            qq.log(f"bot: /read file fallback failed: {ferr}")
            trimmed = "…\n" + content[-int(cfg.get("chunk_chars", 1000)):]
            reply(cfg, scene, openid, trimmed, msg_id, target=target)
        elif target:
            qq.remember_message(cfg, result or {}, target)
        return
    reply(cfg, scene, openid, content, msg_id, target=target)


def cmd_key(cfg: dict, scene: str, openid: str, msg_id: str, target: dict, args: list) -> None:
    if not args or not KEY_RE.match(args[0]):
        reply(cfg, scene, openid, "用法：#key <键名>（down/up/left/right/enter/esc/tab/space/1…）", msg_id)
        return
    _, err = qq.herdr_run("agent", "send-keys", target["pane"], args[0])
    if err:
        report_failure(cfg, scene, openid, msg_id, target, err)
        return
    reply(cfg, scene, openid, f"⌨️ {args[0]} → {qq.label(target)}", msg_id, target=target)


def handle_command(cfg: dict, scene: str, openid: str, msg_id: str, text: str) -> None:
    parts = text[1:].split()
    if not parts:
        return
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("help", "帮助", "?"):
        send_with_keyboard(cfg, scene, openid, HELP, help_keyboard(scene), msg_id)
        return
    if cmd in ("agents", "a", "列表"):
        cmd_agents(cfg, scene, openid, msg_id)
        return
    if cmd in ("use", "切换", "选中"):
        cmd_use(cfg, scene, openid, msg_id, args)
        return
    if cmd in ("status", "状态"):
        cmd_status(cfg, scene, openid, msg_id)
        return
    if cmd in ("start", "启动"):
        cmd_start(cfg, scene, openid, msg_id, args)
        return
    if cmd in ("close", "停止", "关闭"):
        cmd_close(cfg, scene, openid, msg_id, args)
        return

    body = text[1:]
    if cmd in ("sh", "!", "shell", "命令"):
        raw = body.split(None, 1)[1].strip() if len(body.split(None, 1)) > 1 else ""
        cmd_sh(cfg, scene, openid, msg_id, raw, qq.current_target())
        return

    target = qq.current_target()
    if not target.get("pane"):
        reply(cfg, scene, openid, "🤷 还没有选中 agent — #agents 选择，或 #start 启动", msg_id)
        return

    if cmd in ("read", "读"):
        cmd_read(cfg, scene, openid, msg_id, target, [cmd] + args)
        return
    if cmd == "esc":
        _, err = qq.herdr_run("agent", "send-keys", target["pane"], "esc")
        if err:
            report_failure(cfg, scene, openid, msg_id, target, err)
            return
        reply(cfg, scene, openid, f"⎋ Esc → {qq.label(target)}", msg_id, target=target)
        return
    if cmd in ("key", "键"):
        cmd_key(cfg, scene, openid, msg_id, target, args)
        return

    reply(cfg, scene, openid, f"❓ 未知指令 #{cmd} — #help 看清单", msg_id)


# --- live progress streaming -------------------------------------------------


def stream_progress(cfg: dict, openid: str, pane: str, msg_id: str, label: str) -> None:
    """Live progress in one QQ streaming message. Steps and thinking come from
    the agent's own transcript (appended per entry — thinking/toolCall/text),
    which is where intermediate work actually shows up; the terminal pane only
    backfills when no transcript is findable. Append mode: each chunk carries
    just the new lines."""
    interval = max(2, int(cfg.get("stream_interval_seconds", 4)))
    deadline = time.monotonic() + int(cfg.get("stream_max_seconds", 600))
    import notify  # lazy: transcript parsing is only needed while streaming

    agent = kind = cwd = ""
    tpath = ""
    info, err = qq.herdr_json("agent", "get", pane, timeout=10)
    if not err:
        a = info.get("agent", {})
        agent = a.get("display_agent") or a.get("agent") or ""
        kind = a.get("agent") or ""
        cwd = a.get("cwd") or a.get("foreground_cwd") or ""
        tpath = (notify.session_file(kind, a.get("agent_session") or {})
                 or notify.session_file_fallback(kind, cwd))
    offset = 0
    if tpath and os.path.isfile(tpath):
        try:
            offset = os.path.getsize(tpath)
        except OSError:
            tpath = ""
    else:
        tpath = ""
    qq.log(f"stream: source={'transcript' if tpath else 'terminal'} ({agent}, {kind})")

    seen: set[str] = set()
    index = 0
    stream_id = None
    seq = qq.next_seq(msg_id)
    compact = bool(cfg.get("stream_compact", True))

    def touch(done: bool = False) -> None:
        # heartbeat for notify's push suppression: a fresh ts means this stream
        # is (or just was) covering the pane's answer
        qq.write_state("stream_state.json", {pane: {"ts": time.time(), "done": done}})

    def chunk(state: int, text: str) -> bool:
        nonlocal stream_id, index
        if not text:
            return True
        result, err = qq.stream_send(cfg, openid, text, state, index, msg_id, stream_id, seq)
        if err:
            qq.log(f"stream: chunk {index} ({len(text)} chars) failed: {err}")
            return False
        qq.log(f"stream: chunk {index} sent ({len(text)} chars, state {state})")
        stream_id = result.get("id") or stream_id
        index += 1
        touch()
        return True

    touch()
    if not chunk(1, f"⏳ {label}\n"):
        return
    while time.monotonic() < deadline:
        status = agent_status(pane)
        fresh = ""
        if tpath:
            fresh, offset = notify.transcript_step_lines(kind, tpath, offset, compact=compact)
        else:
            out, err = qq.herdr_run("agent", "read", pane, "--lines", "10", "--format", "text")
            if not err:
                new_lines = []
                for ln in out.splitlines():
                    ln = ln.rstrip()
                    if not ln or ln in seen or SPINNER_RE.search(ln):
                        continue
                    seen.add(ln)
                    new_lines.append(ln)
                if new_lines:
                    fresh = "\n".join(new_lines) + "\n"
        if status not in ("working", "unknown", ""):
            final = notify.reply_snippet_of_pane(pane)
            tail = (fresh + "✅ " + final) if final else (fresh + "（已完成）")
            # the stream message is the answer now — the completion push is
            # suppressed for this pane, so this chunk carries the routing anchor
            result, ferr = qq.stream_send(cfg, openid, tail[:1200], 10, index, msg_id,
                                          stream_id, seq)
            if ferr:
                qq.log(f"stream: final chunk failed: {ferr}")
            else:
                qq.log(f"stream: final chunk sent ({len(tail)} chars)")
                qq.remember_message(cfg, result or {},
                                    {"pane": pane, "agent": agent, "folder": ""})
            touch(done=True)
            return
        if not chunk(1, fresh):
            return
        time.sleep(interval)


# --- routing -----------------------------------------------------------------


def resolve(cfg: dict, scene: str, openid: str, msg_id: str, d: dict) -> dict:
    """Which agent does this message address? The quoted notification, else the
    most recently active agent (unless reply_to_latest is turned off)."""
    target = qq.target_for_event(d)
    if target:
        return target
    if qq._event_refs(d):
        reply(cfg, scene, openid, "🕰 那条通知太老，路由不到了 — 引用新一点的通知，或 #use 选择", msg_id)
        return {}
    if not cfg.get("reply_to_latest", True):
        reply(cfg, scene, openid, "↩️ 请引用我的某条通知来选择 agent，或 #use 选择", msg_id)
        return {}
    target = qq.current_target()
    if not target.get("pane"):
        reply(cfg, scene, openid, "🤷 还没有选中 agent — #agents 选择，#start <类型> 启动", msg_id)
    return target


def stale(d: dict, cfg: dict) -> bool:
    """Gateway RESUME replays messages missed while offline; a stale one must
    not land in an agent as a prompt."""
    minutes = float(cfg.get("stale_drop_minutes", 15))
    ts = d.get("timestamp") or ""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - when).total_seconds()
        return age > minutes * 60
    except (ValueError, TypeError):
        return False


def authorized(cfg: dict, scene: str, reply_to: str, sender: str) -> str:
    """Reason this message may NOT drive agents, or "" when it may. "no bound
    target" is special: the caller may auto-bind the sender (see handle_message)."""
    allowed = [str(u) for u in cfg.get("allow_users", [])]
    if allowed and sender not in allowed:
        return "sender not allowlisted"
    bound_scene, bound = qq.push_target(cfg)
    if not bound:
        return "no bound target"
    if scene != bound_scene:
        return "scene mismatch"
    if reply_to != bound:
        return "not the bound target"
    return ""


# --- handlers ----------------------------------------------------------------


def handle_attachment(cfg: dict, scene: str, openid: str, msg_id: str, target: dict,
                      caption: str, d: dict) -> None:
    """Save incoming files to the inbox and hand the agent their path.

    herdr's injection channel is text only, so the file itself can't be piped into
    the terminal — the agent gets absolute paths (plus the caption) and reads them.
    """
    if not cfg.get("accept_files", True):
        reply(cfg, scene, openid, "📎 附件接收已关闭（config: accept_files）", msg_id)
        return
    cap = float(cfg.get("max_file_mb", 20))
    paths = []
    for att in (d.get("attachments") or [])[:5]:
        if not isinstance(att, dict):
            continue
        url = att.get("url") or ""
        name = att.get("filename") or att.get("file_name") or "file"
        try:
            size = int(att.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if not url:
            reply(cfg, scene, openid, f"⚠️ {name}: 事件里没有下载地址，忽略", msg_id)
            continue
        if size and size > cap * 1024 * 1024:
            reply(cfg, scene, openid, f"📎 {size / 1048576:.1f} MB 超过 {cap:g} MB 限制", msg_id)
            continue
        dest = os.path.join(qq.inbox_dir(),
                            f"{time.strftime('%Y%m%d-%H%M%S')}-{msg_id[:8]}-{qq.safe_name(name)}")
        err = qq.download(cfg, url, dest)
        if err:
            qq.log(f"bot: download failed: {err}")
            reply(cfg, scene, openid, f"⚠️ 下载失败: {err}", msg_id)
            continue
        qq.log(f"bot: saved {dest} ({os.path.getsize(dest)} bytes)")
        paths.append(dest)

    if not paths:
        return
    prompt = "\n".join(([caption] if caption else []) + paths)
    err = submit(cfg, target["pane"], prompt)
    if err:
        report_failure(cfg, scene, openid, msg_id, target, err)
        return
    qq.set_current_target(target)
    names = "、".join(os.path.basename(p) for p in paths)
    reply(cfg, scene, openid, f"📎 {names} → {qq.label(target)}", msg_id, target=target)


def handle_message(cfg: dict, d: dict) -> None:
    scene, reply_to, sender = qq.event_ids(d)
    if scene != qq.scene_cfg(cfg):
        qq.log(f"bot: dropping {scene} message (scene is {qq.scene_cfg(cfg)})")
        return
    msg_id = d.get("id") or ""
    text = qq.strip_mentions(d.get("content") or "")
    qq.log(f"bot: message from {sender[:12]}… scene={scene} text={text[:60]!r}")
    reason = authorized(cfg, scene, reply_to, sender)
    if reason == "no bound target":
        if not cfg.get("auto_bind", True):
            qq.log("bot: message ignored (no bound target, auto_bind off)")
            return
        qq.bind_target(cfg, scene, reply_to)
        reply(cfg, scene, reply_to, "✅ 已绑定当前会话，开始使用吧。\n\n" + HELP, msg_id)
        return
    if reason:
        qq.log(f"bot: ignoring message ({reason}) from {sender[:12]}…")
        return
    if stale(d, cfg):
        qq.log(f"bot: dropping stale message {msg_id[:16]}…")
        return

    if (d.get("attachments") or []):
        target = resolve(cfg, scene, reply_to, msg_id, d)
        if target.get("pane"):
            handle_attachment(cfg, scene, reply_to, msg_id, target, text, d)
        return
    if not text:
        return

    if text.startswith("#"):
        handle_command(cfg, scene, reply_to, msg_id, text)
        return

    # everything else passes through to the agent: plain text as a prompt,
    # /slash as the agent's own command — the plugin takes over neither, and
    # stays quiet on success (the completion push is the feedback)
    target = resolve(cfg, scene, reply_to, msg_id, d)
    if not target.get("pane"):
        return
    err = submit(cfg, target["pane"], text)
    if err:
        qq.log(f"bot: prompt {target['pane']} failed: {err}")
        report_failure(cfg, scene, reply_to, msg_id, target, err)
        return
    qq.set_current_target(target)
    qq.log(f"bot: prompted {target['pane']} ({len(text)} chars)")
    if scene == "c2c" and cfg.get("stream_progress", True) and not text.startswith("/"):
        # live progress: one QQ streaming message grows with the terminal while
        # the agent works (c2c only — group messages don't support streaming)
        threading.Thread(target=stream_progress, daemon=True,
                         args=(cfg, reply_to, target["pane"], msg_id, qq.label(target))).start()


def handle_interaction(cfg: dict, d: dict, event_id: str) -> None:
    """A #agents button press: make that agent the target for plain messages."""
    data = d.get("data") or {}
    pane = (data.get("resolved") or {}).get("button_data") or data.get("button_data") or ""
    scene, reply_to, sender = qq.interaction_ids(d)
    qq.log(f"bot: interaction from {sender[:12]}… scene={scene} data={pane[:24]!r}")
    # the platform requires an explicit ack for every callback — without it the
    # client shows 第三方请求失败. The ack id lives inside the payload (d.id),
    # NOT in the gateway frame's top-level id (that one is for event_id replies)
    # — per tencent-connect/qqbot-nodejs, which casts d itself to InteractionEvent.
    ack_id = d.get("id") or event_id
    if ack_id:
        err = qq.ack_interaction(cfg, ack_id)
        if err:
            qq.log(f"bot: interaction ack failed: {err}")
    if scene != qq.scene_cfg(cfg):
        return
    reason = authorized(cfg, scene, reply_to, sender)
    if reason and reason != "no bound target":
        qq.log(f"bot: ignoring interaction ({reason})")
        return
    if not pane:
        return
    result, err = qq.herdr_json("agent", "get", pane)
    agent = result.get("agent", {})
    if err or not agent:
        if reply_to:
            qq.send_text(cfg, scene, reply_to, f"⚠️ agent 不在了: {err or 'not found'}", event_id=event_id)
        return
    target = target_from_agent(agent)
    qq.set_current_target(target)
    if reply_to:
        reply(cfg, scene, reply_to, f"🎯 {qq.label(target)}\n现在直接发消息即可（不引用 = 发给它）",
              event_id=event_id, target=target)


def handle_dispatch(cfg: dict, t: str, d: dict, event_id: str) -> None:
    if t in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
        handle_message(cfg, d)
    elif t == "INTERACTION_CREATE":
        handle_interaction(cfg, d, event_id)
    else:
        qq.log(f"gateway: event {t}")


# --- gateway session ---------------------------------------------------------


def serve(cfg: dict, url: str, session: dict, started_at: float) -> None:
    """One gateway connection, with heartbeats. Returns when the connection drops
    (poll_loop reconnects, resuming the session when the gateway still holds it)."""
    ws = qqws.WS()
    ws.connect(url)
    try:
        op, payload = ws.recv_message(timeout=30)
        hello = json.loads(payload)
        if hello.get("op") != 10:
            raise ProtocolError(f"expected HELLO, got op={hello.get('op')}")
        interval = int((hello.get("d") or {}).get("heartbeat_interval") or 30000) / 1000
        token = f"QQBot {qq.access_token(cfg)}"
        if session["id"] and session["seq"]:
            ws.send_text(json.dumps({"op": 6, "d": {"token": token,
                                                    "session_id": session["id"],
                                                    "seq": session["seq"]}}))
            qq.log("gateway: resuming session")
        else:
            ws.send_text(json.dumps({"op": 2, "d": {"token": token,
                                                    "intents": qq.intents(cfg),
                                                    "shard": [0, 1]}}))
        qq.log("gateway: connected")
        next_beat = time.monotonic() + interval
        last_ack = time.monotonic()
        while True:
            if stop_requested(started_at):
                raise StopBot()
            wait = min(next_beat - time.monotonic(), 5.0)
            try:
                _, payload = ws.recv_message(timeout=max(0.1, wait))
            except (socket.timeout, TimeoutError):
                if time.monotonic() >= next_beat:
                    ws.send_text(json.dumps({"op": 1, "d": session["seq"]}))
                    next_beat = time.monotonic() + interval
                    if time.monotonic() - last_ack > interval * 2.5:
                        raise ProtocolError("heartbeat timed out")
                continue
            msg = json.loads(payload)
            op = msg.get("op")
            if op == 11:
                last_ack = time.monotonic()
            elif op == 1:  # server wants a heartbeat right now
                ws.send_text(json.dumps({"op": 1, "d": session["seq"]}))
                next_beat = time.monotonic() + interval
            elif op == 7:
                qq.log("gateway: reconnect requested")
                return
            elif op == 9:
                if not msg.get("d"):
                    session["id"], session["seq"] = "", 0  # dead session: identify fresh
                qq.log("gateway: invalid session")
                return
            elif op == 0:
                session["seq"] = msg.get("s", session["seq"])
                t, d = msg.get("t", ""), msg.get("d") or {}
                if t == "READY":
                    session["id"] = d.get("session_id", "")
                    qq.log("gateway: online")
                elif t == "RESUMED":
                    qq.log("gateway: resumed")
                else:
                    try:
                        handle_dispatch(cfg, t, d, msg.get("id", ""))
                    except Exception as e:
                        qq.log(f"dispatch {t} failed: {e!r}")
    finally:
        ws.close()


class StopBot(Exception):
    pass


class ProtocolError(Exception):
    pass


def run_daemon(started_at: float) -> None:
    session: dict = {"id": "", "seq": 0}
    backoff = 5.0
    while True:
        if stop_requested(started_at):
            break
        cfg = qq.load_config()
        if cfg is None or not qq.app_configured(cfg):
            time.sleep(30)
            continue
        try:
            url, err = qq.gateway_url(cfg)
            if err or not url:
                raise ProtocolError(f"gateway url: {err or 'empty'}")
            serve(cfg, url, session, started_at)
            backoff = 5.0
        except StopBot:
            break
        except Exception as e:
            qq.log(f"gateway session ended: {e!r}")
        time.sleep(backoff)
        backoff = min(60.0, backoff * 2)
    try:
        os.remove(qq.stop_flag_path())
    except OSError:
        pass
    qq.log("bot: daemon stopped")


def run_foreground() -> None:
    lock = qq.bot_lock()
    if lock is None:
        qq.log("bot: another daemon holds the lock; exiting")
        return
    # the pid goes in its own file: the lock file is byte-locked, and Windows
    # mandatory locks would deny other processes even a read
    with open(qq.state_path(qq.PID_NAME), "w") as f:
        f.write(str(os.getpid()))
    started_at = time.time()
    try:
        os.remove(qq.stop_flag_path())
    except OSError:
        pass
    qq.log(f"bot: daemon started pid={os.getpid()}")
    try:
        run_daemon(started_at)
    finally:
        qq.log("bot: poller exited")


def main() -> None:
    if "--daemon" in sys.argv:
        run_foreground()
    elif "--stop" in sys.argv:
        pid = qq.stop_poller()
        print(f"stopped daemon pid={pid}" if pid else "daemon not running")
    elif "--restart" in sys.argv:
        qq.stop_poller()
        print("daemon started" if qq.spawn_bot() else "daemon still running — try again")
    elif "--status" in sys.argv:
        print(f"daemon running pid={qq.bot_pid()}" if qq.bot_running() else "daemon not running")
    else:  # --ensure, and the default for the [[startup]] hook
        print("daemon started" if qq.spawn_bot()
              else f"daemon already running pid={qq.bot_pid()}")


if __name__ == "__main__":
    main()
