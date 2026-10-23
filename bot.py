#!/usr/bin/env python3
"""herdr plugin daemon: hold the QQ gateway WebSocket and turn QQ replies back
into agent input.

Receives C2C / group @ messages and keyboard callbacks over the gateway. A quote
reply to one of the plugin's notifications is submitted to that notification's
agent via `herdr agent prompt`; a plain message goes to the most recently active
agent. Files are saved to the plugin's inbox and the agent is handed their path.
Also serves /agents, /read and /esc.

  bot.py --daemon   run the gateway loop in the foreground
  bot.py --ensure   start the daemon if it isn't already running, then exit
  bot.py --stop     stop the running daemon
  bot.py --restart  stop it and start a fresh one (picks up edited code)
  bot.py --status   report whether the daemon is running
"""

import json
import os
import socket
import sys
import time
from datetime import datetime, timezone

import qq
import qqws

MAX_BUTTONS = 8

HELP = (
    "引用（回复）我的任何通知，文字会发给那个 agent；\n"
    "直接发消息（不引用）会发给最近活跃的 agent。\n\n"
    "/agents — 列出 agent 并选择\n"
    "/read [行数] — 查看 agent 最近的终端输出\n"
    "/esc — 向 agent 发送 Esc 中断\n"
    "/help — 本说明\n\n"
    "发文件/图片给我，会保存后把路径交给 agent。"
)

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
        reply(cfg, scene, openid, f"🚪 {qq.label(target)} 已关闭（pane 不在了）。\n用 /agents 另选。", msg_id)
    else:
        reply(cfg, scene, openid, f"⚠️ {qq.label(target)}: {err}", msg_id)


# --- commands ----------------------------------------------------------------


def button_label(agent: dict) -> str:
    """QQ button labels cap at 10 characters, so this is name-only; the status
    emoji rides along when it fits."""
    name = agent.get("display_agent") or agent.get("agent") or "agent"
    emoji = qq.STATUS_EMOJI.get(agent.get("agent_status", ""), "")
    base = f"{emoji}{name}"[:10] if emoji else name[:10]
    return base or "agent"


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


def cmd_agents(cfg: dict, scene: str, openid: str, msg_id: str) -> None:
    result, err = qq.herdr_json("agent", "list")
    if err:
        reply(cfg, scene, openid, f"⚠️ {err}", msg_id)
        return
    agents = [a for a in result.get("agents", []) if a.get("pane_id")]
    if not agents:
        reply(cfg, scene, openid, "🫙 没有在跑的 agent", msg_id)
        return
    agents.sort(key=lambda a: a.get("state_change_seq", 0), reverse=True)
    shown, extra = agents[:MAX_BUTTONS], len(agents) - MAX_BUTTONS
    text = "🤖 选择要对话的 agent" + (f"（还有 {extra} 个未列出）" if extra > 0 else "")
    _, send_err = qq.send_text(cfg, scene, openid, text, msg_id=msg_id)
    if send_err:
        qq.log(f"bot: /agents header failed: {send_err}")
        return
    # the keyboard rides on its own message: a markdown body plus a keyboard is
    # the one combination the platform documents, and plain text + keyboard is
    # not — so this stays msg_type=0 always
    body = {"msg_type": 0, "content": text, "keyboard": agent_keyboard(shown)}
    qq._attach_passive(body, scene, msg_id, None)
    _, err2 = qq.api(cfg, "POST", qq._messages_path(scene, openid), body)
    if err2:
        qq.log(f"bot: /agents keyboard failed: {err2}")


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


def handle_command(cfg: dict, scene: str, openid: str, msg_id: str, text: str, target: dict) -> None:
    parts = text.split()
    cmd = parts[0].lower()

    if cmd in ("/start", "/help"):
        reply(cfg, scene, openid, HELP, msg_id)
        return

    if cmd in ("/agents", "/a"):
        cmd_agents(cfg, scene, openid, msg_id)
        return

    if not target.get("pane"):
        reply(cfg, scene, openid, "🤷 还没有选择 agent — 先用 /agents", msg_id)
        return

    if cmd == "/read":
        cmd_read(cfg, scene, openid, msg_id, target, parts)
        return

    if cmd == "/esc":
        _, err = qq.herdr_run("agent", "send-keys", target["pane"], "esc")
        if err:
            report_failure(cfg, scene, openid, msg_id, target, err)
            return
        reply(cfg, scene, openid, f"⎋ 已向 {qq.label(target)} 发送 Esc", msg_id, target=target)
        return

    reply(cfg, scene, openid, f"❓ 未知的命令 {cmd} — 试试 /help", msg_id)


# --- routing -----------------------------------------------------------------


def resolve(cfg: dict, scene: str, openid: str, msg_id: str, d: dict) -> dict:
    """Which agent does this message address? The quoted notification, else the
    most recently active agent (unless reply_to_latest is turned off)."""
    target = qq.target_for_event(d)
    if target:
        return target
    if qq._event_refs(d):
        reply(cfg, scene, openid, "🕰 那条通知太老，路由不到了 — 引用新一点的通知，或用 /agents 选择", msg_id)
        return {}
    if not cfg.get("reply_to_latest", True):
        reply(cfg, scene, openid, "↩️ 请引用我的某条通知来选择 agent", msg_id)
        return {}
    target = qq.current_target()
    if not target.get("pane"):
        reply(cfg, scene, openid, "🤷 还没有选择 agent — 先用 /agents", msg_id)
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
    """Reason this message may NOT drive agents, or "" when it may."""
    allowed = [str(u) for u in cfg.get("allow_users", [])]
    if allowed and sender not in allowed:
        return "sender not allowlisted"
    bound_scene, bound = qq.push_target(cfg)
    if not bound:
        return "" if cfg.get("auto_bind", True) else "no bound target"
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
    reason = authorized(cfg, scene, reply_to, sender)
    msg_id = d.get("id") or ""
    if reason == "no bound target":
        if scene == qq.scene_cfg(cfg) and reply_to:
            qq.bind_target(cfg, scene, reply_to)
            reply(cfg, scene, reply_to, "✅ 已绑定当前会话，开始使用吧。\n\n" + HELP, msg_id)
        else:
            qq.log("bot: message ignored (no bound target, auto_bind off)")
        return
    if reason:
        qq.log(f"bot: ignoring message ({reason}) from {sender[:12]}…")
        return
    if stale(d, cfg):
        qq.log(f"bot: dropping stale message {msg_id[:16]}…")
        return

    text = qq.strip_mentions(d.get("content") or "")
    if (d.get("attachments") or []):
        target = resolve(cfg, scene, reply_to, msg_id, d)
        if target.get("pane"):
            handle_attachment(cfg, scene, reply_to, msg_id, target, text, d)
        return
    if not text:
        return

    if text.startswith("/"):
        handle_command(cfg, scene, reply_to, msg_id, text,
                       qq.target_for_event(d) or qq.current_target())
        return

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
    reply(cfg, scene, reply_to, f"➡️ 已发给 {qq.label(target)}", msg_id, target=target)


def handle_interaction(cfg: dict, d: dict, event_id: str) -> None:
    """A /agents button press: make that agent the target for plain messages."""
    data = d.get("data") or {}
    pane = (data.get("resolved") or {}).get("button_data") or data.get("button_data") or ""
    scene, reply_to, sender = qq.interaction_ids(d)
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
