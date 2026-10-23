"""Repro harness: run bot.stream_progress against a live pane with QQ sends
stubbed, so stalls/crashes in the stream thread reproduce locally and any
traceback is visible on stderr."""
import os
import sys
import tempfile
import threading
import time

os.environ["HERDR_PLUGIN_STATE_DIR"] = tempfile.mkdtemp(prefix="qqbridge-harness-")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import qq  # noqa: E402
import bot  # noqa: E402

sent: list = []
_orig_stream = qq.stream_send


def fake_stream(cfg, openid, content, state, index, msg_id, sid, seq, mode="append"):
    sent.append((round(time.time() - T0, 1), state, index, content[:120].replace("\n", "\\n")))
    return {"id": f"fake-{index}"}, ""


qq.stream_send = fake_stream
qq.remember_message = lambda *a, **k: None


def on_thread_exc(args):
    print(f"!!! THREAD EXCEPTION: {args.exc_type.__name__}: {args.exc_value}")
    import traceback
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)


threading.excepthook = on_thread_exc

T0 = time.time()
pane = sys.argv[1] if len(sys.argv) > 1 else "w4:pA"
cfg = {"stream_interval_seconds": 3, "stream_max_seconds": 60,
       "stream_compact": True, "stream_total_chars": 3000}

t = threading.Thread(target=bot.stream_progress,
                     args=(cfg, "FAKEOPENID", pane, "fake-msg-id", "harness · ~"),
                     daemon=True)
t.start()
t.join(timeout=90)
time.sleep(0.3)
print(f"=== thread alive after 90s: {t.is_alive()}")
print(f"=== chunks sent: {len(sent)}")
for s in sent:
    print(f"  t+{s[0]}s state={s[1]} idx={s[2]}: {s[3]}")
