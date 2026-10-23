#!/usr/bin/env python3
"""Shared plumbing for the qqbot.bridge plugin.

Imported by notify.py (the status-change hook) and bot.py (the gateway daemon):
plugin paths, logging, config, the herdr CLI call, the QQ open-platform REST
client (token, messages, rich media), rate limiting, the msg_seq state machine,
and the message -> agent maps that let a quoted reply find its way back to a pane.

QQ open-platform notes baked in here (see README for the full boundary list):
  - passive replies carry the user's msg_id and an incrementing msg_seq;
    each msg_id accepts at most 4 replies in c2c / 5 in groups
  - group messages reject URLs outright (40054010)
  - markdown may need a permission the bot doesn't have; those errors fall back
    to plain text automatically
"""

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or PLUGIN_DIR
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or PLUGIN_DIR
LOG_PATH = os.path.join(STATE_DIR, "bridge.log")
LOG_MAX_BYTES = 2_000_000

STATUS_EMOJI = {"done": "✅", "blocked": "❓", "idle": "💤", "working": "⏳", "unknown": "❔"}

ROUTE_FILE = "msg_targets.json"   # sent-message id / ref_idx -> agent target
CURRENT_TARGET = "current_target.json"
BOUND_FILE = "bound_target.json"  # auto-bound c2c/group openid
SEQ_FILE = "seq_state.json"       # msg_id/event_id -> last used msg_seq
SENDS_FILE = "last_send.json"     # rate-limit timestamps
TOKEN_FILE = "token.json"
LOCK_NAME = "bot.lock"
PID_NAME = "bot.pid"      # separate from the lock: Windows byte-range locks are
                          # mandatory and would make the locked file unreadable
STOP_NAME = "bot.stop"

PASSIVE_CAP = {"c2c": 4, "group": 5}
TOKEN_TTL_MARGIN = 300

# markdown refused for a reason plain text can fix -> retry unformatted
MARKDOWN_FALLBACK_CODES = ("304036", "304004", "40034127", "40034124", "40034008",
                           "40034009", "40034010", "40034011", "40034128", "304061")
# passive reply no longer possible (window passed / cap hit / bad id) -> resend proactive
PASSIVE_GONE_CODES = ("304103", "40034005", "40034024", "40034128")
# content too long for one message
LENGTH_CODES = ("40054007", "40054018")


def log(msg: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def load_config() -> dict | None:
    path = os.path.join(CONFIG_DIR, "config.toml")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        log(f"no config at {path}; skipping")
        return None
    except tomllib.TOMLDecodeError as e:
        log(f"bad config {path}: {e}")
        return None


def short_home(path: str) -> str:
    return path.replace(os.path.expanduser("~"), "~") if path else ""


def scene_cfg(cfg: dict) -> str:
    """Which scene notifications and replies use: "c2c" (default) or "group"."""
    return "group" if str(cfg.get("scene", "c2c")).lower() == "group" else "c2c"


# --- herdr CLI ---------------------------------------------------------------


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr_run(*args: str, timeout: int = 30) -> tuple[str, str]:
    """Run a herdr subcommand -> (stdout, error message).

    herdr reports failures as {"error": {...}} on stdout *with exit code 0*, so the
    error string — not the exit code — is what callers have to check.
    """
    try:
        out = subprocess.run([herdr_bin(), *args], capture_output=True, timeout=timeout, check=False)
    except Exception as e:
        return "", repr(e)
    text = out.stdout.decode(errors="replace").strip()
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return "", body["error"].get("message") or body["error"].get("code") or "herdr error"
    if out.returncode != 0:
        return "", (out.stderr.decode(errors="replace").strip() or f"exit {out.returncode}")[:300]
    return text, ""


def herdr_json(*args: str, timeout: int = 30) -> tuple[dict, str]:
    """herdr_run + unwrap the {"result": {...}} envelope."""
    text, err = herdr_run(*args, timeout=timeout)
    if err:
        return {}, err
    try:
        return json.loads(text).get("result", {}), ""
    except ValueError as e:
        return {}, repr(e)


# --- credentials & REST ------------------------------------------------------


def _opener(cfg: dict):
    proxy = str(cfg.get("proxy", "") or "")
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def api_base(cfg: dict) -> str:
    return str(cfg.get("api_base", "https://api.sgroup.qq.com")).rstrip("/")


def app_configured(cfg: dict) -> bool:
    appid = str(cfg.get("app_id", ""))
    secret = str(cfg.get("app_secret", ""))
    return bool(appid and secret and "REPLACE" not in appid and "REPLACE" not in secret)


def access_token(cfg: dict) -> str:
    """Access token for the REST API and gateway, cached in state until shortly
    before expiry (the platform hands out strings, hence the int() dances)."""
    if not app_configured(cfg):
        return ""
    cache = read_state(TOKEN_FILE, {})
    if cache.get("token") and cache.get("expires_at", 0) - TOKEN_TTL_MARGIN > time.time():
        return cache["token"]
    data = json.dumps({"appId": str(cfg.get("app_id")),
                       "clientSecret": str(cfg.get("app_secret"))}).encode()
    req = urllib.request.Request("https://bots.qq.com/app/getAppAccessToken", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _opener(cfg).open(req, timeout=15) as resp:
            out = json.loads(resp.read().decode())
    except Exception as e:
        log(f"token request failed: {e!r}")
        return cache.get("token", "")
    token = out.get("access_token") or ""
    if not token:
        log(f"token: bad response: {out}")
        return cache.get("token", "")
    try:
        ttl = int(out.get("expires_in") or 7200)
    except (TypeError, ValueError):
        ttl = 7200
    write_state(TOKEN_FILE, {"token": token, "expires_at": time.time() + ttl})
    return token


def api(cfg: dict, method: str, path: str, body: dict | None = None,
        timeout: int = 20, retries: int = 2) -> tuple[dict | None, str]:
    """QQ REST call -> (result, error). The error string starts with the numeric
    platform code so callers can branch on it (see the *_CODES tuples)."""
    tok = access_token(cfg)
    if not tok:
        return None, "no access token (app_id/app_secret set?)"
    url = f"{api_base(cfg)}{path}"
    data = json.dumps(body or {}).encode()
    last = ""
    for attempt in range(max(1, retries)):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"QQBot {tok}",
            "Content-Type": "application/json",
        })
        try:
            with _opener(cfg).open(req, timeout=timeout) as resp:
                text = resp.read().decode()
            parsed = json.loads(text) if text else {}
            # some endpoints answer HTTP 200 with an error object in the body
            if isinstance(parsed, dict) and parsed.get("code") not in (None, 0, "0"):
                message = parsed.get("message") or parsed.get("msg") or ""
                return None, f"{parsed['code']} {message}".strip()
            return parsed, ""
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                parsed = json.loads(detail)
                code = parsed.get("code", e.code)
                message = parsed.get("message") or parsed.get("msg") or detail[:200]
            except ValueError:
                code, message = e.code, detail[:200]
            err = f"{code} {message}"
            if e.code < 500:  # deterministic: retrying won't help
                return None, err
            last = err
        except Exception as e:
            last = repr(e)
            time.sleep(1 + attempt)
    return None, last


def gateway_url(cfg: dict) -> tuple[str, str]:
    result, err = api(cfg, "GET", "/gateway")
    if err:
        return "", err
    return result.get("url", ""), ("" if result.get("url") else "gateway returned no url")


def ack_interaction(cfg: dict, interaction_id: str) -> str:
    """PUT /interactions/{id} — the platform expects every button callback to be
    acknowledged this way; skipping it makes the QQ client show 第三方请求失败."""
    _, err = api(cfg, "PUT", f"/interactions/{interaction_id}", {"code": 0}, retries=1)
    return err


def stream_send(cfg: dict, openid: str, content: str, state: int, index: int,
                msg_id: str, stream_msg_id: str | None, msg_seq: int) -> tuple[str, str]:
    """One chunk of a c2c streaming message (POST /v2/users/{id}/stream_messages).
    replace mode: content_raw is the FULL text and must start with everything
    already sent (error 40007 otherwise). Returns (stream_msg_id, err)."""
    body = {"input_mode": "replace", "input_state": state, "index": index,
            "content_type": "markdown", "content_raw": content,
            "msg_id": msg_id, "msg_seq": msg_seq}
    if stream_msg_id:
        body["stream_msg_id"] = stream_msg_id
    result, err = api(cfg, "POST", f"/v2/users/{openid}/stream_messages", body)
    if err:
        return (stream_msg_id or ""), err
    return result.get("id") or (stream_msg_id or ""), ""


def _decode(data: bytes) -> str:
    """Windows cmd.exe emits the ANSI codepage (GBK on Chinese systems); agent
    CLIs emit UTF-8. Try UTF-8 first, fall back to GBK, never raise."""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_shell(cfg: dict, command: str, cwd: str = "",
              timeout: int = 30) -> tuple[str, str]:
    """Execute a shell command locally -> (combined output tail, err). Only ever
    called behind the allow_shell config gate."""
    workdir = cwd or str(cfg.get("shell_cwd", "") or os.path.expanduser("~"))
    try:
        out = subprocess.run(command, shell=True, capture_output=True, timeout=timeout,
                             cwd=workdir)
    except subprocess.TimeoutExpired:
        return "", f"超时（>{timeout}s，已终止）"
    except Exception as e:
        return "", repr(e)
    text = (_decode(out.stdout) + _decode(out.stderr)).strip()
    if out.returncode != 0:
        text += f"\n（exit {out.returncode}）"
    return text, ""


def intents(cfg: dict) -> int:
    """1<<25 covers c2c + group @ messages; 1<<26 covers keyboard callbacks."""
    default = (1 << 25) | (1 << 26)
    try:
        return int(cfg.get("intents", default))
    except (TypeError, ValueError):
        return default


# --- locks, rate limit, msg_seq ----------------------------------------------


@contextlib.contextmanager
def file_lock(path: str, timeout: float = 30.0):
    """Cross-platform advisory lock. On timeout, proceed unlocked — a rare
    duplicate send beats wedging the poller."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+b")
    deadline = time.monotonic() + timeout
    locked = False
    try:
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.2)
        yield locked
    finally:
        if locked:
            try:
                if fcntl is not None:
                    fcntl.flock(f, fcntl.LOCK_UN)
                else:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        f.close()


def throttle(cfg: dict, key: str) -> None:
    """Space sends to one relationship >= send_gap_seconds apart — QQ caps a
    single relationship at 20 messages/minute on top of the bot-wide limits."""
    gap = float(cfg.get("send_gap_seconds", 3.0))
    if gap <= 0:
        return
    with file_lock(state_path(SENDS_FILE + ".lock")):
        seen = read_state(SENDS_FILE, {})
        wait = float(seen.get(key, 0)) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        seen[key] = time.time()
        if len(seen) > 400:
            seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-200:])
        write_state(SENDS_FILE, seen)


def next_seq(key: str) -> int:
    """Next msg_seq for this msg_id/event_id. Same key+seq twice is rejected by
    the platform, so every send — even a retry — must claim a fresh number."""
    with file_lock(state_path(SEQ_FILE + ".lock")):
        seen = read_state(SEQ_FILE, {})
        seq = int(seen.get(key, 0)) + 1
        if len(seen) > 300:
            seen = {}
        seen[key] = seq
        write_state(SEQ_FILE, seen)
    return seq


# --- sending -----------------------------------------------------------------


def _messages_path(scene: str, openid: str) -> str:
    return (f"/v2/users/{openid}/messages" if scene == "c2c"
            else f"/v2/groups/{openid}/messages")


def _files_path(scene: str, openid: str) -> str:
    return (f"/v2/users/{openid}/files" if scene == "c2c"
            else f"/v2/groups/{openid}/files")


def _attach_passive(body: dict, scene: str, msg_id: str | None, event_id: str | None) -> dict:
    if event_id:
        body["event_id"] = event_id
        body["msg_seq"] = next_seq(event_id)
        return body
    if not msg_id:
        return body
    seq = next_seq(msg_id)
    cap = PASSIVE_CAP.get(scene, 4)
    if seq <= cap:
        body["msg_id"] = msg_id
        body["msg_seq"] = seq
    else:
        log(f"passive cap hit for {msg_id[:16]}… (seq {seq} > {cap}); sending proactive")
    return body


def send_text(cfg: dict, scene: str, openid: str, content: str,
              msg_id: str | None = None, event_id: str | None = None,
              markdown_content: str | None = None) -> tuple[dict | None, str]:
    """One message. markdown_content switches to msg_type=2 with an automatic
    plain-text retry when the bot lacks the permission. A passive reply whose
    window has passed is retried as a proactive message."""
    throttle(cfg, f"{scene}:{openid}")
    use_md = bool(markdown_content)
    body = ({"msg_type": 2, "markdown": {"content": markdown_content}} if use_md
            else {"msg_type": 0, "content": content})
    _attach_passive(body, scene, msg_id, event_id)
    result, err = api(cfg, "POST", _messages_path(scene, openid), body)
    if err and use_md and err.split()[0] in MARKDOWN_FALLBACK_CODES:
        log(f"markdown rejected ({err.split()[0]}); resending as plain text")
        body = {"msg_type": 0, "content": content}
        _attach_passive(body, scene, msg_id, event_id)
        result, err = api(cfg, "POST", _messages_path(scene, openid), body)
    if err and (msg_id or event_id) and err.split()[0] in PASSIVE_GONE_CODES:
        log(f"passive reply gone ({err.split()[0]}); resending proactive")
        body = {"msg_type": body["msg_type"],
                **({"markdown": {"content": markdown_content}} if use_md else {"content": content})}
        result, err = api(cfg, "POST", _messages_path(scene, openid), body)
    return result, err


def send_media(cfg: dict, scene: str, openid: str, file_info: str,
               msg_id: str | None = None, event_id: str | None = None) -> tuple[dict | None, str]:
    throttle(cfg, f"{scene}:{openid}")
    body = {"msg_type": 7, "media": {"file_info": file_info}}
    _attach_passive(body, scene, msg_id, event_id)
    return api(cfg, "POST", _messages_path(scene, openid), body)


def upload_file(cfg: dict, scene: str, openid: str, path: str,
                file_type: int = 4, timeout: int = 180) -> tuple[str, str]:
    """Direct multipart upload -> file_info. file_type: 1 image 2 video 3 voice 4
    file. The media and message endpoints are scene-bound and not interchangeable."""
    boundary = f"----herdrqq{int(time.time() * 1000):x}"
    parts = bytearray()
    for k, v in (("file_type", str(file_type)), ("srv_send_msg", "false")):
        parts += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    parts += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
              f"filename=\"{os.path.basename(path)}\"\r\n"
              "Content-Type: application/octet-stream\r\n\r\n").encode()
    with open(path, "rb") as f:
        parts += f.read()
    parts += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        api_base(cfg) + _files_path(scene, openid), data=bytes(parts), method="POST",
        headers={"Authorization": f"QQBot {access_token(cfg)}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with _opener(cfg).open(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return "", f"{e.code} {e.read().decode(errors='replace')[:200]}"
    except Exception as e:
        return "", repr(e)
    fi = out.get("file_info") or out.get("file_uuid") or ""
    return (fi, "") if fi else ("", f"no file_info in response: {out}")


def send_text_as_file(cfg: dict, scene: str, openid: str, text: str,
                      msg_id: str | None = None) -> tuple[dict | None, str]:
    """Last resort for output that won't fit one message: a .txt attachment."""
    fd, tmp = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        fi, err = upload_file(cfg, scene, openid, tmp, file_type=4)
        if err:
            return None, f"upload failed: {err}"
        return send_media(cfg, scene, openid, fi, msg_id=msg_id)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --- outbound content shaping ------------------------------------------------


_URL_RE = re.compile(r"https?://[^\s<>\"）)】]+", re.I)


def strip_urls(text: str) -> str:
    """QQ refuses URLs in group messages (40054010); keeping just the domain
    still tells the reader where it pointed."""
    return _URL_RE.sub(lambda m: m.group(0).split("/")[2] + "/…", text)


def split_markdown(text: str, limit: int) -> list[str]:
    """Chunk text so each piece fits one message, never cutting a fenced code
    block in half — an open fence is closed and reopened across the seam."""
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    fence = info = ""
    fence_re = re.compile(r"^\s*(```|~~~)(.*)$")

    def flush(reopen: bool) -> None:
        nonlocal cur, size
        if fence:
            cur.append(fence)
        chunks.append("\n".join(cur))
        cur = [f"{fence}{info}"] if (reopen and fence) else []
        size = sum(len(x) + 1 for x in cur)

    for line in text.split("\n"):
        while len(line) > limit:  # a single monstrous line
            if cur:
                flush(True)
            cut = limit - size
            cur.append(line[:cut])
            size += cut + 1
            flush(True)
            line = line[cut:]
        if cur and size + len(line) + 1 > limit:
            flush(True)
        m = fence_re.match(line)
        if m:
            if fence and line.strip().startswith(fence):
                fence = info = ""
            elif not fence:
                fence, info = m.group(1), m.group(2).strip()
        cur.append(line)
        size += len(line) + 1
    if cur:
        flush(False)
    return [c for c in chunks if c.strip()]


def push_target(cfg: dict) -> tuple[str, str]:
    """(scene, openid) notifications go to. Explicit config wins over auto-bind."""
    scene = scene_cfg(cfg)
    key = "group_openid" if scene == "group" else "c2c_openid"
    conf = str(cfg.get(key, "") or "")
    if conf and "REPLACE" not in conf:
        return scene, conf
    bound = read_state(BOUND_FILE, {})
    return scene, str(bound.get(scene, "") or "")


def bind_target(cfg: dict, scene: str, openid: str) -> None:
    bound = read_state(BOUND_FILE, {})
    bound[scene] = openid
    write_state(BOUND_FILE, bound)
    log(f"bound {scene} target {openid[:12]}…")


def send_update(cfg: dict, header: str, reply: str, target: dict) -> str:
    """Push one notification to the bound target (proactive — no msg_id).
    Every chunk's ref is recorded so quoting any of them routes back. Returns
    the last error seen, or ""."""
    scene, openid = push_target(cfg)
    if not openid:
        return "no bound target yet (send the bot a message first)"
    limit = int(cfg.get("snippet_chars") or 0)
    if limit > 0 and len(reply) > limit:
        reply = reply[:limit].rstrip() + "…"
    if scene == "group" and cfg.get("strip_group_urls", True):
        reply = strip_urls(reply)
        header = strip_urls(header)
    use_md = bool(cfg.get("markdown", True))
    chunks = split_markdown(reply, int(cfg.get("chunk_chars", 1000))) if reply else []
    cap = max(1, int(cfg.get("max_messages", 6)))
    trimmed = max(0, len(chunks) - cap)
    chunks = chunks[:cap]
    err_final = ""
    pieces = [f"{header}\n{chunks[0]}"] if chunks else [header]
    pieces += [chunk for chunk in chunks[1:]]
    for i, piece in enumerate(pieces):
        if trimmed and chunks and i == len(pieces) - 1:
            piece += f"\n(+{trimmed} parts trimmed)"
        result, err = send_text(cfg, scene, openid, piece,
                                markdown_content=piece if use_md else None)
        if err:
            err_final = err
            log(f"push {i + 1}/{len(pieces)} failed: {err}: {piece[:80]!r}")
            continue
        remember_message(cfg, result or {}, target)
    return err_final


# --- message -> agent routing ------------------------------------------------
#
# A target is {"pane": <pane id>, "agent": <display name>, "folder": <~-relative cwd>}.


def label(target: dict) -> str:
    name = target.get("agent") or "agent"
    where = target.get("folder") or target.get("pane") or ""
    return f"{name} · {where}" if where else name


def remember_message(cfg: dict, result: dict, target: dict) -> None:
    """Map the sent message's id and ref index back to the agent, so a quoted
    reply from the user routes to the right pane."""
    if not target.get("pane"):
        return
    entry = {**target, "ts": int(time.time())}
    refs = read_state(ROUTE_FILE, {})
    keys = [str(result["id"])] if result.get("id") else []
    ref_idx = (result.get("ext_info") or {}).get("ref_idx") or ""
    if ref_idx:
        keys.append(str(ref_idx))
    for key in keys:
        refs[key] = entry
    limit = int(cfg.get("msg_map_limit", 1000))
    if len(refs) > limit:
        refs = dict(sorted(refs.items(), key=lambda kv: kv[1].get("ts", 0))[-limit:])
    write_state(ROUTE_FILE, refs)
    write_state(CURRENT_TARGET, entry)


def _event_refs(d: dict) -> list[str]:
    """REFIDX-style indices in an incoming event marking the quoted message."""
    out: list[str] = []

    def walk(v):
        if isinstance(v, str):
            if v.startswith("REFIDX_"):
                out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(d.get("message_scene"))
    walk(d.get("message_reference"))
    return out


def target_for_event(d: dict) -> dict:
    refs = read_state(ROUTE_FILE, {})
    for ref in _event_refs(d):
        hit = refs.get(ref)
        if hit:
            return hit
    return {}


def current_target() -> dict:
    return read_state(CURRENT_TARGET, {})


def set_current_target(target: dict) -> None:
    if target.get("pane"):
        write_state(CURRENT_TARGET, {**target, "ts": int(time.time())})


# --- incoming event shapes ---------------------------------------------------
#
# The platform's payloads vary by client version and doc page; read them
# tolerantly and prefer the first field that is actually there.


def event_ids(d: dict) -> tuple[str, str, str]:
    """(scene, reply-to openid, sender openid) for a message event."""
    gid = d.get("group_openid") or ""
    if gid:
        sender = ((d.get("author") or {}).get("user_openid")
                  or d.get("user_openid") or d.get("group_member_openid") or "")
        return "group", gid, sender
    sender = ((d.get("author") or {}).get("user_openid") or d.get("user_openid")
              or (d.get("author") or {}).get("id") or "")
    return "c2c", sender, sender


def interaction_ids(d: dict) -> tuple[str, str, str]:
    """(scene, reply-to openid, sender openid) for a keyboard callback."""
    gid = d.get("group_openid") or ""
    sender = (d.get("user_openid") or (d.get("user") or {}).get("user_openid")
              or d.get("group_member_openid") or "")
    if gid:
        return "group", gid, sender
    sender = sender or d.get("c2c_openid") or ""
    return "c2c", sender, sender


_MENTION = re.compile(r"^(?:<@!\w+>|<@\w+>|@\S+)\s*")


def strip_mentions(text: str) -> str:
    """Group @ messages arrive with an @-token prefix; peel all of them off."""
    text = text.strip()
    while True:
        m = _MENTION.match(text)
        if not m:
            return text
        text = text[m.end():].strip()


# --- inbox -------------------------------------------------------------------


def inbox_dir() -> str:
    path = os.path.join(STATE_DIR, "inbox")
    os.makedirs(path, exist_ok=True)
    return path


def safe_name(name: str, limit: int = 80) -> str:
    """A sender-supplied filename is untrusted: keep a flat, boring basename so
    nothing can climb out of the inbox."""
    name = re.sub(r"[^\w.\-]+", "_", os.path.basename(name or "")).strip("._")
    if not name:
        return "file"
    if len(name) > limit:
        stem, dot, ext = name.rpartition(".")
        ext = f".{ext}" if dot and 0 < len(ext) <= 10 else ""
        name = (stem or name)[:limit - len(ext)] + ext
    return name


def download(cfg: dict, url: str, dest: str, timeout: int = 120) -> str:
    """Fetch an attachment (QQ CDN) to dest. Returns an error message, or ""."""
    if url.startswith("//"):
        url = "https:" + url
    req = urllib.request.Request(url, headers={"User-Agent": "herdr-qqbot-bridge"})
    try:
        with _opener(cfg).open(req, timeout=timeout) as resp, open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:
        return repr(e)
    try:
        return "" if os.path.getsize(dest) else "downloaded 0 bytes"
    except OSError as e:
        return repr(e)


def sweep_inbox(days: float) -> int:
    """Delete inbox files older than `days`. Returns how many went."""
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    gone = 0
    for name in os.listdir(inbox_dir()):
        path = os.path.join(inbox_dir(), name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                gone += 1
        except OSError:
            pass
    return gone


# --- state files -------------------------------------------------------------


def state_path(name: str) -> str:
    return os.path.join(STATE_DIR, name)


def read_state(name: str, default):
    try:
        with open(state_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_state(name: str, obj) -> None:
    """Atomic write: the hook and the poller are separate processes sharing these files."""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = state_path(f"{name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, state_path(name))


# --- poller lifecycle --------------------------------------------------------


def stop_flag_path() -> str:
    return state_path(STOP_NAME)


def bot_lock():
    """Take the daemon singleton lock. Returns the held file (keep the reference
    alive!) or None when another process holds it."""
    os.makedirs(STATE_DIR, exist_ok=True)
    f = open(state_path(LOCK_NAME), "a+b")
    try:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


def bot_running() -> bool:
    f = bot_lock()
    if f is None:
        return True
    f.close()  # closing drops the lock we just took
    return False


def bot_pid() -> int:
    try:
        with open(state_path(PID_NAME), encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def spawn_bot() -> bool:
    """Start the gateway daemon detached, so it outlives the hook that spawned it.

    The resolved plugin dirs are passed down explicitly: a daemon started from the
    manifest's [[startup]] hook must agree with the event hook on where state lives.
    """
    if bot_running():
        return False
    env = {
        **os.environ,
        "HERDR_PLUGIN_STATE_DIR": STATE_DIR,
        "HERDR_PLUGIN_CONFIG_DIR": CONFIG_DIR,
        "HERDR_BIN_PATH": shutil.which(herdr_bin()) or herdr_bin(),
    }
    devnull = os.open(os.devnull, os.O_RDWR)
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x08)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(PLUGIN_DIR, "bot.py"), "--daemon"],
            cwd=PLUGIN_DIR, env=env, stdin=devnull, stdout=devnull, stderr=devnull,
            **kwargs,
        )
    except Exception as e:
        os.close(devnull)
        log(f"bot: spawn failed: {e!r}")
        return False
    os.close(devnull)
    log("bot: spawned gateway daemon")
    return True


def stop_poller() -> int:
    """Ask the daemon to stop via the stop flag (graceful — it sits in a ws read),
    then SIGTERM as a fallback. Returns its pid, or 0. The flag comes first and
    works even when the pid file is missing."""
    running = bot_running()
    pid = bot_pid()
    if not running:
        return 0
    try:
        with open(stop_flag_path(), "w") as f:
            f.write(str(time.time()))
    except OSError as e:
        log(f"bot: stop flag write failed: {e!r}")
    for _ in range(15):
        if not bot_running():
            return pid
        time.sleep(1)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)  # Windows: hard-terminates, which is fine here
        except OSError as e:
            log(f"bot: stop failed: {e!r}")
            return 0
    for _ in range(10):
        if not bot_running():
            break
        time.sleep(1)
    return pid
