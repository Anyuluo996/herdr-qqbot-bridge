# herdr-qqbot-bridge

用 **QQ 官方机器人**（QQ 开放平台，非 NapCat/OneBot）驱动 [herdr](https://herdr.dev) 里的编码 agent：agent 完成或阻塞时推送结果到 QQ，引用回复即可把文字（或文件）直接发回那个 agent 的终端。

Fork 自 [cokekitten/herdr-telegram-bridge](https://github.com/cokekitten/herdr-telegram-bridge)（MIT），把 Telegram 传输层整体替换为 QQ 开放平台 API v2，并移植到 Windows。herdr 侧逻辑（事件钩子、`herdr agent prompt` 注入、各 agent 会话嗅探、inbox）与上游保持一致。

```
agent done/blocked ──> notify.py（事件钩子）──> QQ 推送
QQ 引用回复 ──> bot.py（网关守护）──> herdr agent prompt ──> agent 终端
```

## 功能

| 功能 | 说明 |
|---|---|
| 状态推送 | agent done/blocked 时推送最后一条回复（支持 claude/codex/pi/grok/kimi/hermes/opencode 的会话嗅探），长文自动分片 |
| 引用路由 | 引用某条通知 = 精确路由到那个 agent；不引用 = 最近活跃的 agent |
| /agents | 按钮面板选择 agent（QQ 内嵌键盘回调按钮） |
| /read | 查看 agent 最近终端输出，超长自动改发 .txt 文件 |
| /esc | 向 agent 发送 Esc 中断 |
| 收文件 | 发给机器人的图片/文件保存到 inbox，路径交给 agent |
| 防打扰 | 抖动确认（settle_ms）、去抖（quiet_seconds）、断线重放消息按时间丢弃 |
| 平台适配 | msg_seq 状态机、被动回复超限自动转主动、Markdown 无权限自动降级纯文本、群聊 URL 自动脱敏、20 条/分钟限速队列 |

## 安装

```bash
herdr plugin install Anyuluo996/herdr-qqbot-bridge
# 或本地开发
herdr plugin link /path/to/herdr-qqbot-bridge
```

### 配置

1. 在 [QQ 开放平台](https://q.qq.com) 手机 QQ 扫码注册，**创建机器人**，拿到 `AppID` 和 `AppSecret`。
2. 写配置：

```bash
herdr plugin config-dir qqbot.bridge   # 打印配置目录
cp config.example.toml <配置目录>/config.toml
# 编辑 app_id / app_secret
```

3. **绑定会话**：给机器人发任意一条消息（单聊直接私聊它；群聊先在开放平台把机器人加进群）。默认 `auto_bind = true`，第一个发消息的会话自动绑定，机器人会回复确认。openid 会记录在插件 state 目录的 `bound_target.json` 里。

4. 重启 daemon（或重启 herdr）：

```bash
herdr plugin action qqbot.bridge bot-restart
herdr plugin action qqbot.bridge test   # 收到测试消息即通
```

## 使用

- agent 完成时收到推送，**长按/引用回复**那条通知，文字直接进入对应 agent 的终端
- 直接发消息（不引用）= 发给最近活跃的 agent
- `/agents` 选人，`/read [行数]` 看输出，`/esc` 中断，`/help` 帮助
- 发图片/文件给机器人 → 存到 inbox → agent 收到绝对路径

## 与 Telegram 版的差异（为什么不能照搬）

| 能力 | Telegram | QQ 官方 |
|---|---|---|
| 回复路由 | reply_to message_id | 引用 + REFIDX 映射（发送时记录 `ext_info.ref_idx`，回复事件反查） |
| 事件获取 | getUpdates 长轮询 | WebSocket 网关（含心跳/RESUME，`qqws.py` 手写零依赖客户端） |
| 被动回复 | 无限制 | 单聊 60 分钟/4 次，群聊 5 分钟/5 次，超限自动转主动消息 |
| 主动消息 | 无限制 | 用户可关闭「允许主动发送」；频控 20 条/分钟/关系、1000 条/天（2026 政策） |
| 富文本 | HTML 子集 + 可折叠引用 | msg_type=2 Markdown（可能需权限，自动降级）；无可折叠块，超长输出改发 .txt 文件 |
| 按钮 | callback_data 64 字节 | 回调按钮可用但 **label ≤ 10 字符** |
| 群聊 URL | 无限制 | **直接拒绝（40054010）**，自动替换为域名 |
| 内容审核 | 无 | 出站内容过风控（40034006），代码内容偶发被拦 |
| 网络 | 常需代理 | 国内直连 |

其他平台硬性限制（配置无法绕过）：C2C 需好友/添加关系（40054004）；未认证机器人只能加自己为群主/管理员的群；个人身份证认证（免费）可放宽部分频控。

## Windows 说明

原版仅支持 macOS/Linux（`fcntl`、`lsof`）。本 fork：

- 锁与守护进程派生改为跨平台（`msvcrt`/`DETACHED_PROCESS`）
- 移除 grok 的 `lsof` 特判（mtime 回退）
- manifest 按 `platforms` 拆分启动/事件命令；POSIX 上若 `python` 不存在，编辑 manifest 里 `[[actions]]` 的 `python` 为 `python3`
- 需要 Python ≥ 3.11（`tomllib`）

## 沙箱实测清单（上线前建议跑一遍）

- [ ] 单聊被动窗口实际值（官方总览写 60 分钟，API 页写 5 分钟，以实测为准）
- [ ] 文本长度实际上限（默认 chunk_chars=1000 保守值）
- [ ] 你的 bot 的 Markdown 是否免申请
- [ ] 按钮回调事件 `data.resolved.button_data` 字段结构
- [ ] 引用回复事件里 `message_scene.ext` 的 REFIDX 字段名

## 文件结构

```
herdr-plugin.toml   manifest（startup/events/actions，按平台拆分）
config.example.toml 配置模板（中文注释）
qq.py               共享层：herdr CLI、token/REST、限速、msg_seq、路由、跨平台进程管理
qqws.py             零依赖 RFC6455 WebSocket 客户端
bot.py              网关守护进程：消息/命令/按钮 → agent
notify.py           pane.agent_status_changed 钩子 → QQ 推送
tests/smoke_local.py 离线自测：WS 握手/帧、分片、路由、脱敏
```

## License

MIT（承袭上游；详见 LICENSE）
