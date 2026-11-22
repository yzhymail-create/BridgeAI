# Muse → 微信 ClawBot 桥接

手机微信私聊发文字任务，Muse 实际用工具执行，结果回微信，全程中文。
本程序只做微信消息收发，不执行任务；执行由主聊天里的 Muse 完成。

协议：腾讯 iLink 官方通道（本人绑定）。详细规则见 `docs/需求说明.md`。

## 环境

- Python 3.12，虚拟环境 `.venv/`（依赖见 `requirements.txt`：
  `qrcode[pil]` + `opencv-python-headless` + `pycryptodome`；
  `qrcode` 的图片支持需要 Pillow，所以装的是 `qrcode[pil]`）
- HTTP 只用标准库 `urllib`，不依赖 openclaw，没有跑过 npx 安装命令
- 所有命令都在 `~/workspace/wechat-bridge/` 下用虚拟环境的 Python 运行：

```bash
cd ~/workspace/wechat-bridge
.venv/bin/python bridge.py status
```

## 命令用法

### 1. 登录（两步，等用户说“手机在手边，马上扫”才做）

```bash
.venv/bin/python bridge.py login        # 取二维码，打印图片路径（只打印路径，不打印 token）
.venv/bin/python bridge.py wait-login   # 轮询扫码状态，总超时 60 秒
```

- 二维码有效期很短，生成后请尽快用微信扫一扫扫描。
- 扫码时手机上如果显示一串数字，把数字告诉主聊天，写入 `state/pin.txt`
  （0600），程序会在下次轮询时提交，用后立即删除该文件。
- 二维码过期不会自动重生成：重新运行 `login` 即可。
- 登录成功后凭证存 `state/session.json`（0600），不要发给任何人。

### 2. 收消息服务

```bash
.venv/bin/python bridge.py serve
```

- 单实例运行；第二个实例会直接报错退出。
- 前台运行，Ctrl+C 停止；也可以用 `nohup` 放后台（见“后台运行”）。
- 只收不执行：主人文字、图片、文件任务写进 `inbox/`，状态 `received`；
  图片/文件下载解密后落盘到 `inbox/media/<message_id>/`，
  inbox 记录里记相对路径 `media/<id>/<文件名>`；
  语音/视频等未接入类型自动回固定文案，不假装读过内容。
  无文本且媒体全下载失败时回“刚才的图片/文件没能下载下来，请再发一次”。

后台运行（示例）：

```bash
cd ~/workspace/wechat-bridge
nohup .venv/bin/python bridge.py serve >> logs/serve.out 2>&1 &
```

### 3. 发消息

正式发送只用 `send-once`（按消息 ID 只发一次，防重发）：

```bash
.venv/bin/python bridge.py send-once <message_id>
```

- 它自己读 `inbox/<message_id>.json`（收件人、令牌）与
  `outbox/<message_id>.txt`（回复文本），不用在命令行里传令牌。
- 媒体回复：worker 先把文件放进 `outbox/`，再写清单
  `outbox/<message_id>.media.json`，例如
  `{"type": "file", "file": "报告.xlsx", "caption": "你要的文件"}`；
  `send-once` 有清单时按清单发媒体（说明文字先发），无清单时走文本。
  清单里的 `file` 必须是 outbox 里的纯文件名，不许带路径。
- 查状态→发消息→记 `sent` 全程加状态锁，两个发送者同时调也只发一次；
  已是 `sent` 的直接跳过。
- HTTP 成功即记 `sent`，不等 `ret`（微信经常不回 `ret`，实测没 `ret` 也能收到）。
  只有命令报错（网络不通、非零 `ret`）才留在 `pending-send`。
- 每次发送都在 `logs/bridge.log` 记一条（只记字数与结果，不记内容）。

底层命令 `send --to <用户ID> --context-token <令牌> --text-file <文件>`
只做单次发送、不查状态，日常不要直接用（令牌从 inbox 文件里取，不要贴进聊天记录）。

发图片/文件（底层单次发送，日常请用 `send-once` + 媒体清单）：

```bash
.venv/bin/python bridge.py send-media --to <用户ID> --file <本地路径> \
    [--type image|file] [--caption "说明文字"] [--context-token <令牌>]
```

- `--type` 不给时按文件头自动判定（图片魔数 → image，否则 file）。
- 有说明文字时先发一条文本再发媒体（两个独立请求）。
- 单个文件上限 100MB；媒体先加密上传到微信 CDN 再发送。

### 4. 查看状态

```bash
.venv/bin/python bridge.py status
```

显示登录状态、serve 是否运行、inbox 各状态计数、最近活动时间。
不输出任何凭证。

### 5. 推进消息状态（供外部 worker 调用）

```bash
.venv/bin/python bridge.py mark <message_id> received|executing|pending-send|sent
```

## 停止 serve

- 前台运行：Ctrl+C。
- 后台运行：先查 PID，再杀掉：

```bash
.venv/bin/python bridge.py status   # 显示 serve 的 PID
kill <PID>                          # 优雅停止（SIGTERM）
```

确认进程已死后才能重新启动；锁文件在 `state/serve.lock`。
如果进程已死但锁文件残留，删掉 `state/serve.lock` 再启动。

## 重新登录

凭证过期（日志出现 `-14`）或换微信号时：

```bash
.venv/bin/python bridge.py login
.venv/bin/python bridge.py wait-login
```

登录成功后重启 serve 即可（游标与已处理记录保留，不会重复收旧消息）。

## 唤醒 hook

`~/hooks/scripts/wechat-inbox-watch.sh`：每 15 秒查一次收件箱，
有 `received` 状态的新消息就唤醒 worker，无则静默。
登录成功后才注册启用；准备阶段只写了文件，没有注册、没有启用。

## 后台服务跨虚拟机替换

后台 serve 不能保证在虚拟机替换后存活：这是一条已知限制，不是 bug。
恢复方法：替换后重新启动 serve（凭证在 `state/session.json`，
随 home 目录持久化，不用重新登录）；hook 由系统恢复后继续检查收件箱。
恢复后用 `status` 命令核对：登录状态、serve 运行状态、inbox 计数。

## 自测

```bash
.venv/bin/python selftest.py          # 文字收发与防重发，6 项
.venv/bin/python selftest_media.py    # 图片/文件收发，8 项
```

不调登录接口、不生成真实二维码、不联网发消息。
自测 6 项：二维码编解码往返与载荷判定、消息过滤与非文字固定回复、
redact 脱敏、单实例锁、状态推进、send-once 只发一次与无 ret 记 sent。
媒体自测 8 项：AES 加密往返与密钥解码、上传参数构造与文件名判定、
CDN 上传/下载 URL 构造、CDN 上传重试行为、发送体结构与说明文字顺序、
接收下载解密往返、收消息集成（图片/文件进 inbox）、send-once 媒体防重发。

## 安全提醒

- `state/`、`inbox/`、`outbox/`、`logs/` 已在 `.gitignore` 里排除，不进 git。
- 凭证只在 `state/session.json`（0600）；不要把凭证贴进聊天、日志或任何文件。
- 日志已脱敏（token、二维码值、AES 密钥、CDN 上传/下载参数都不进日志），
  但仍不要把 `logs/` 发给外人。
