#!/usr/bin/env python3
"""Offline smoke tests: the WebSocket client against a local ws:// server, plus
the pure logic (chunking, URL stripping, msg_seq cap, REFIDX routing, event
shapes). No QQ credentials or network involved.

Run: python tests/smoke_local.py  (exit 0 = all green)
"""

import os
import socket
import sys
import tempfile
import threading

_state = tempfile.mkdtemp(prefix="qqbridge-test-")
os.environ["HERDR_PLUGIN_STATE_DIR"] = _state
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qq   # noqa: E402  (import after the env override)
import qqws  # noqa: E402


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    return cond


# --- pure logic ---------------------------------------------------------------


def test_split_markdown():
    text = "para one\n```py\ncode1\ncode2\ncode3\n```\ntail line"
    chunks = qq.split_markdown(text, 12)
    ok = all(c.count("```") % 2 == 0 for c in chunks) and "code1" in "".join(chunks)
    ok = ok and qq.split_markdown("a" * 50, 10)[0].startswith("a")
    return check("split_markdown keeps fences balanced", ok, repr(chunks))


def test_strip_urls():
    out = qq.strip_urls("看 https://github.com/a/b?x=1 和 http://x.cn/y")
    ok = "github.com" in out and "http://" not in out and "a/b" not in out
    return check("strip_urls keeps only domains", ok, out)


def test_seq_and_passive_cap():
    for i in range(6):
        seq = qq.next_seq("m1")
    if not check("next_seq increments", seq == 6, str(seq)):
        return False
    body = qq._attach_passive({"msg_type": 0}, "c2c", "m1", None)
    ok = "msg_id" not in body  # 7th reply > cap 4 → proactive
    body = qq._attach_passive({"msg_type": 0}, "c2c", "m2", None)
    ok = ok and body.get("msg_seq") == 1
    body = qq._attach_passive({"msg_type": 0}, "group", "m3", None)
    ok = ok and body.get("msg_seq") == 1
    return check("passive cap falls back to proactive", ok, repr(body))


def test_ref_routing():
    qq.write_state(qq.ROUTE_FILE, {})
    qq.remember_message({"msg_map_limit": 100},
                        {"id": "ROBOT1.0_x", "ext_info": {"ref_idx": "REFIDX_abc"}},
                        {"pane": "w1:p1", "agent": "claude", "folder": "~"})
    hit = qq.target_for_event({"message_scene": {"ext": [
        {"key": "msg_idx", "value": "IDX_user1"},
        {"key": "ref_msg_idx", "value": "REFIDX_abc"},
    ]}})
    ok = check("REFIDX routes back to pane", hit.get("pane") == "w1:p1", repr(hit))
    miss = qq.target_for_event({"message_scene": {"ext": [{"value": "IDX_other"}]}})
    ok = ok and not miss
    return check("unrelated refs miss cleanly", ok, repr(miss))


def test_event_shapes():
    ok = qq.event_ids({"group_openid": "G1", "author": {"user_openid": "U1"}}) == \
        ("group", "G1", "U1")
    ok = ok and qq.event_ids({"author": {"user_openid": "U2"}}) == ("c2c", "U2", "U2")
    ok = ok and qq.interaction_ids({"group_openid": "G1", "user_openid": "U9"}) == \
        ("group", "G1", "U9")
    ok = ok and qq.strip_mentions("<@!ABC123> /read 20") == "/read 20"
    ok = ok and qq.strip_mentions("<@!ABC123><@!ABC123>hello") == "hello"
    return check("event id/mention extraction", ok)


def test_bind_and_push_target():
    qq.write_state(qq.BOUND_FILE, {})
    cfg = {"scene": "c2c"}
    qq.bind_target(cfg, "c2c", "OPENID_A")
    scene, openid = qq.push_target(cfg)
    ok = check("auto-bind becomes push target", (scene, openid) == ("c2c", "OPENID_A"),
               f"{scene} {openid}")
    cfg2 = {"scene": "c2c", "c2c_openid": "OPENID_B"}
    ok = ok and qq.push_target(cfg2)[1] == "OPENID_B"  # config wins over bind file
    return check("explicit config wins over bind", ok)


# --- websocket ----------------------------------------------------------------


def _frame(op: int, payload: bytes, fin: bool = True) -> bytes:
    b1 = (0x80 if fin else 0) | op
    n = len(payload)
    if n < 126:
        return bytes([b1, n]) + payload
    if n < 65536:
        return bytes([b1, 126]) + n.to_bytes(2, "big") + payload
    return bytes([b1, 127]) + n.to_bytes(8, "big") + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("eof")
        buf += chunk
    return buf


def _read_client_frame(sock: socket.socket):
    b1, b2 = _recv_exact(sock, 2)
    op, masked, n = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
    if n == 126:
        n = int.from_bytes(_recv_exact(sock, 2), "big")
    elif n == 127:
        n = int.from_bytes(_recv_exact(sock, 8), "big")
    if masked:
        key = _recv_exact(sock, 4)
        data = bytes(b ^ key[i % 4] for i, b in enumerate(_recv_exact(sock, n)))
    else:
        data = _recv_exact(sock, n)
    return op, masked, data


def ws_server_test(port_holder: list, results: list) -> None:
    import base64
    import hashlib

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port_holder.append(srv.getsockname()[1])
    srv.listen(1)
    conn, _ = srv.accept()
    try:
        # handshake
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(4096)
        key = ""
        for line in head.decode("latin1").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(
            hashlib.sha1((key + qqws._GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        # 1: plain text
        conn.sendall(_frame(qqws.TEXT, b"hello world"))
        # 2: fragmented message
        conn.sendall(_frame(qqws.TEXT, b"hel", fin=False))
        conn.sendall(_frame(qqws.CONT, b"lo"))
        # 3: ping, then a data message so the client's recv returns
        conn.sendall(_frame(qqws.PING, b"pi"))
        conn.sendall(_frame(qqws.TEXT, b"after-ping"))
        op, masked, data = _read_client_frame(conn)  # the pong (client frames must be masked)
        results.append(check("server ping -> client pong", op == qqws.PONG and data == b"pi"
                             and masked, f"op={op} data={data!r} masked={masked}"))
        # 4: large frame (200 KB)
        big = ("x" * 200_000).encode()
        conn.sendall(_frame(qqws.TEXT, big))
        # 5: client's masked text frame
        op, masked, data = _read_client_frame(conn)
        results.append(check("client frame arrives masked+intact",
                             op == qqws.TEXT and masked and data == b"client-hello",
                             f"op={op} masked={masked} data={data[:40]!r}"))
        # 6: close
        conn.sendall(_frame(qqws.CLOSE, (1000).to_bytes(2, "big")))
    except Exception as e:  # noqa: BLE001
        results.append(check("server thread survived", False, repr(e)))
    finally:
        srv.close()


def test_websocket():
    port_holder: list = []
    results: list = []
    t = threading.Thread(target=ws_server_test, args=(port_holder, results), daemon=True)
    t.start()
    for _ in range(100):
        if port_holder:
            break
        threading.Event().wait(0.05)
    ws = qqws.WS()
    last_err = None
    for _ in range(20):  # the listener is up, but allow the accept queue a moment
        try:
            ws.connect(f"ws://127.0.0.1:{port_holder[0]}", timeout=10)
            last_err = None
            break
        except (ConnectionRefusedError, OSError) as e:
            last_err = e
            threading.Event().wait(0.1)
    if last_err:
        raise last_err
    ok = True
    try:
        op, data = ws.recv_message(timeout=20)
        ok &= check("ws: single text frame", data == b"hello world", repr(data))
        op, data = ws.recv_message(timeout=20)
        ok &= check("ws: fragmented message reassembles", data == b"hello", repr(data))
        op, data = ws.recv_message(timeout=20)
        ok &= check("ws: message after ping arrives", data == b"after-ping", repr(data))
        op, data = ws.recv_message(timeout=30)
        ok &= check("ws: 200KB frame", len(data) == 200_000 and data.endswith(b"xxxx"))
        ws.send_text("client-hello")
        closed = False
        try:
            ws.recv_message(timeout=20)
        except qqws.Closed:
            closed = True
        ok &= check("ws: close frame raises Closed", closed)
    except Exception as e:  # noqa: BLE001 — a dead server shows up as a timeout here
        ok = check("ws: client loop crashed", False, repr(e))
    finally:
        ws.close()
    finally:
        ws.close()
    t.join(timeout=5)
    return ok and all(results)


def main() -> int:
    results = [
        test_split_markdown(),
        test_strip_urls(),
        test_seq_and_passive_cap(),
        test_ref_routing(),
        test_event_shapes(),
        test_bind_and_push_target(),
        test_websocket(),
    ]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} groups passed (state dir: {_state})")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
