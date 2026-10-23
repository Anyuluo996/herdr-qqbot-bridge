#!/usr/bin/env python3
"""One-off probe: which msg_type actually renders inline keyboard buttons for
this bot? Sends two labelled messages with identical keyboards; whichever shows
a button in QQ answers the permission/shape question. Run from the plugin dir."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qq  # noqa: E402


def keyboard(label: str) -> dict:
    return {"content": {"rows": [{"buttons": [{
        "id": f"probe-{label}",
        "render_data": {"label": f"按钮{label}", "style": 1},
        "action": {"type": 2, "permission": {"type": 2}, "data": "#status"},
    }]}]}}


def main() -> None:
    cfg = qq.load_config()
    scene = qq.scene_cfg(cfg)
    openid = qq.push_target(cfg)[1]
    if not openid:
        print("no bound target")
        return
    for label, body in (
        ("A", {"msg_type": 0, "content": "键盘测试A：纯文本 + 键盘"}),
        ("B", {"msg_type": 2, "markdown": {"content": "**键盘测试B**：Markdown + 键盘"}}),
    ):
        body["keyboard"] = keyboard(label)
        qq.throttle(cfg, f"{scene}:{openid}")
        _, err = qq.api(cfg, "POST", qq._messages_path(scene, openid), body)
        print(label, "ok" if not err else f"ERR {err}")
        time.sleep(3)


if __name__ == "__main__":
    main()
