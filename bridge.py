#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Muse → 微信 ClawBot 桥接（Python 单文件实现）。

本程序只做微信消息收发，不执行任务：
serve 把主人发来的文字任务写进 inbox/，执行由外部 worker（主聊天里的 Muse）
完成，再调 send 把结果发回微信。

协议依据：腾讯 openclaw-weixin 协议文档
https://github.com/tencent/openclaw-weixin/blob/HEAD/docs/protocol_zh_CN.md
（iLink 官方通道，本人绑定流程）。

安全红线（任何情况下都不违反）：
- bot_token、qrcode 值、二维码载荷原文只存 state/session.json 与
  state/login_session.json（0600），绝不写进日志、聊天输出或其他文件；
- 所有日志输出前一律过 redact() 脱敏；
- login 只在用户说“手机在手边，马上扫”之后由主聊天触发；
  二维码过期不自动重生成，不自动循环登录流程。

子命令：login / wait-login / serve / send / send-once / send-media /
status / mark。图片与文件的编解码、上传下载在 media.py。
用法见 README.md。
"""

import argparse
import base64
import contextlib
import fcntl
import io
import json
import logging
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import media  # 图片与文件的编解码、上传下载（AES + CDN）


# ---------------------------------------------------------------------------
# 常量：协议参数
# ---------------------------------------------------------------------------

# iLink 官方域名（协议文档“协议范围和传输方式”一节）
DEFAULT_API_BASE = "https://ilinkai.weixin.qq.com"

# 取二维码时的 bot 类型（协议固定值）
BOT_TYPE = "3"

# 上报的插件版本：前三段必须是 openclaw-weixin 的版本格式，自家版本放后缀
# （协议“插件版本号”一节允许这种写法）
CHANNEL_VERSION = "3.0.1/muse-bridge-1.0.0"

# 观测标识，仅用于日志和监控归因，不参与鉴权或路由
BOT_AGENT = "MuseBridge"

# iLink-App-ClientVersion：插件版本按 0x00MMNNPP 编码后转十进制字符串。
# 3.0.1 -> 0x00030001 -> 196609
APP_CLIENT_VERSION = (3 << 16) | (0 << 8) | 1

# 二维码状态长轮询单次等待（协议规定 35 秒，源码 QR_LONG_POLL_TIMEOUT_MS）
QR_LONG_POLL_TIMEOUT = 35

# wait-login 总超时：60 秒。
# 注意：这是本项目自定的取值，不是协议规定（协议与源码里没有这个数，
# 60 秒只在实名 ticket 距过期不足 60 秒不再换码那条边距里出现过）。
LOGIN_TOTAL_TIMEOUT = 60

# getUpdates 默认长轮询超时（服务端会在响应里给 longpolling_timeout_ms，
# 有则按服务端的建议走，默认 35000ms）
GETUPDATES_TIMEOUT = 35

# 语音/视频的固定回复文案：语音和视频还没接，不假装读过内容。
# 图片与文件已接入（见 process_message），不再用这句回复。
NON_TEXT_REPLY = "语音和视频暂时还不能处理，请先用文字描述"

# 图片/文件下载失败时的固定回复文案
DOWNLOAD_FAILED_REPLY = "刚才的图片/文件没能下载下来，请再发一次"

# 消息状态机（state/status.json 按 message_id 记录）
STATUSES = ("received", "executing", "pending-send", "sent")

# 已处理消息 ID 最多保留条数（防 processed.json 无限长大）
PROCESSED_KEEP = 5000


class BridgeError(Exception):
    """桥接业务错误：文案直接展示给用户，须用中文、说清原因和下一步。"""


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

class Redactor:
    """脱敏器：把已知的敏感值替换成 ***。

    用法：拿到 token、qrcode 值、二维码载荷后立刻 redactor.add() 登记，
    之后所有日志与屏幕输出都过 scrub()。
    """

    def __init__(self):
        self._secrets = set()

    def add(self, value):
        """登记一个敏感值（太短的值不登记，避免误伤正常文本）。"""
        if value and isinstance(value, str) and len(value) >= 8:
            self._secrets.add(value)

    def scrub(self, text):
        """把文本里的敏感值全部替换成 ***。"""
        if not isinstance(text, str):
            text = str(text)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        # 兜底：Authorization 头里的 Bearer token 一律抹掉
        text = re.sub(r"(Bearer\s+)[^\s\"']+", r"\1***", text)
        return text


class _RedactingFormatter(logging.Formatter):
    """日志格式化器：格式化后再脱敏，双保险。"""

    def __init__(self, redactor, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._redactor = redactor

    def format(self, record):
        return self._redactor.scrub(super().format(record))


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _now_iso():
    """当前 UTC 时间，ISO 格式（带 Z 后缀）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(path):
    """读 JSON 文件；不存在或损坏返回 None。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_write_json(path, data):
    """原子写 JSON：先写临时文件再改名，避免中途被杀留下半截文件。"""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _write_json_0600(path, data):
    """写 JSON 并把权限收紧到 0600（仅所有者可读写），存凭证专用。"""
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _http_json(method, url, body=None, headers=None, timeout=35):
    """发一个 JSON 请求，返回解析后的响应字典。

    只用标准库 urllib。HTTP 错误与网络错误统一转成 BridgeError，
    错误里不带响应正文（正文里可能含凭证）。
    """
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise BridgeError(f"HTTP {exc.code}：{url}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise BridgeError(f"网络错误：{exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise BridgeError("服务端返回的不是 JSON") from exc


def _random_uin_b64():
    """X-WECHAT-UIN：随机 uint32 的十进制字符串再 base64 编码。"""
    n = random.randint(0, 2 ** 32 - 1)
    return base64.b64encode(str(n).encode("ascii")).decode("ascii")


def _app_headers():
    """应用请求头（所有 ilink 请求都带）。"""
    return {
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": str(APP_CLIENT_VERSION),
    }


def _auth_headers(token):
    """鉴权 Bot 请求的完整请求头（带 Authorization 与 X-WECHAT-UIN）。"""
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": _random_uin_b64(),
    }
    headers.update(_app_headers())
    return headers


def _base_info():
    """鉴权 POST 请求体里带的公共元数据。"""
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def _is_image_bytes(data):
    """按文件头魔数判断字节串是不是常见图片格式。"""
    return data.startswith(
        (
            b"\x89PNG\r\n\x1a\n",  # PNG
            b"\xff\xd8\xff",  # JPEG
            b"GIF87a",
            b"GIF89a",  # GIF
            b"RIFF",  # WEBP（RIFF....WEBP）
            b"BM",  # BMP
        )
    )


def _download_bytes(url, timeout=10, limit=2 * 1024 * 1024):
    """下载一个 URL，返回 (content_type, 字节串)。超限或失败抛异常。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content_type = resp.headers.get_content_type() or ""
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise BridgeError("下载内容超过 2MB 上限")
    return content_type, data


def _encode_qr_png(payload):
    """把一串文本编成二维码 PNG 字节串（qrcode 库）。"""
    try:
        import qrcode
    except ImportError as exc:
        raise BridgeError("缺少依赖 qrcode：请先在 .venv 里运行 pip install qrcode") from exc
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _decode_qr_png(png_bytes):
    """用 OpenCV QRCodeDetector 解一个 PNG 里的二维码，返回解出的文本。

    解不出返回空字符串（调用方按失败处理）。
    """
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise BridgeError(
            "缺少依赖 opencv-python-headless：请先在 .venv 里运行 "
            "pip install opencv-python-headless"
        ) from exc
    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return ""
    detector = cv2.QRCodeDetector()
    data, _points, _straight = detector.detectAndDecode(img)
    return data or ""


# ---------------------------------------------------------------------------
# 桥接主体
# ---------------------------------------------------------------------------

class Bridge:
    """桥接主体：所有命令都挂在这个类上，base_dir 可注入（方便自测用临时目录）。"""

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        # 目录结构：state（凭证/游标/状态）inbox（收到的任务）outbox（待发结果）
        # logs（日志）state/qr（二维码图片）
        self.state_dir = self.base_dir / "state"
        self.inbox_dir = self.base_dir / "inbox"
        self.outbox_dir = self.base_dir / "outbox"
        self.logs_dir = self.base_dir / "logs"
        self.qr_dir = self.state_dir / "qr"
        # 收到的图片/文件解密后存在 inbox/media/<message_id>/ 下
        self.media_dir = self.inbox_dir / "media"
        for d in (self.state_dir, self.inbox_dir, self.outbox_dir,
                  self.logs_dir, self.qr_dir, self.media_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.redactor = Redactor()
        self.logger = self._setup_logging()
        self._lock_fp = None  # serve 单实例锁持有的文件句柄
        self._stop = False  # serve 收到停止信号时置 True
        self._status_lock_depth = 0  # 状态锁重入计数
        self._status_lock_fp = None  # 状态锁持有的文件句柄

    # -- 日志 ---------------------------------------------------------------

    def _setup_logging(self):
        """日志同时写文件（logs/bridge.log）与屏幕，全部过脱敏。"""
        logger = logging.getLogger("wechat-bridge")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        fmt = _RedactingFormatter(
            self.redactor, "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"
        )
        file_handler = logging.FileHandler(self.logs_dir / "bridge.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(fmt)
        logger.addHandler(console_handler)
        logger.propagate = False
        return logger

    def log(self, level, msg):
        """写一条日志（Formatter 会自动脱敏，这里再脱一次，双保险）。"""
        getattr(self.logger, level)(self.redactor.scrub(msg))

    # -- 凭证 ---------------------------------------------------------------

    @property
    def session_path(self):
        return self.state_dir / "session.json"

    @property
    def login_session_path(self):
        return self.state_dir / "login_session.json"

    @property
    def pin_path(self):
        return self.state_dir / "pin.txt"

    def load_session(self):
        """读登录凭证；没登录就报错。读出后把 token 登记进脱敏器。"""
        data = _load_json(self.session_path)
        if not data:
            raise BridgeError("尚未登录：请先运行 login 生成二维码，再运行 wait-login 完成绑定")
        for key in ("bot_token", "ilink_bot_id", "baseurl", "ilink_user_id"):
            if not data.get(key):
                raise BridgeError(f"凭证文件缺字段 {key}，请重新登录")
        self.redactor.add(data["bot_token"])
        return data

    def save_session(self, data):
        """保存凭证原文到 state/session.json，权限 0600。"""
        _write_json_0600(self.session_path, data)

    # -- 单实例锁 -----------------------------------------------------------

    def acquire_lock(self):
        """抢 serve 单实例锁：抢到返回 True，抢不到返回 False（已有实例在跑）。"""
        lock_path = self.state_dir / "serve.lock"
        fp = open(lock_path, "w", encoding="utf-8")
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fp.close()
            return False
        fp.write(str(os.getpid()))
        fp.flush()
        self._lock_fp = fp
        return True

    def release_lock(self):
        """释放单实例锁。"""
        if self._lock_fp is not None:
            try:
                fcntl.flock(self._lock_fp, fcntl.LOCK_UN)
                self._lock_fp.close()
            except OSError:
                pass
            self._lock_fp = None

    def serve_running(self):
        """检查 serve 是否在运行。返回 (是否运行, PID 或 None)。"""
        lock_path = self.state_dir / "serve.lock"
        if not lock_path.exists():
            return False, None
        pid = None
        try:
            pid = int(lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        # 试抢锁：抢得到说明没人在跑，抢不到说明有人在跑
        try:
            fp = open(lock_path, "w", encoding="utf-8")
        except OSError:
            return False, pid
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fp.close()
            return True, pid
        fcntl.flock(fp, fcntl.LOCK_UN)
        fp.close()
        return False, None

    # -- 状态机 -------------------------------------------------------------

    def _load_status(self):
        """读 state/status.json（message_id -> {status, updated_at, ...}）。"""
        data = _load_json(self.state_dir / "status.json")
        return data if isinstance(data, dict) else {}

    @contextlib.contextmanager
    def _status_lock(self):
        """状态文件锁（可重入）：查状态、发消息、记 sent 在同一把锁里完成，
        两个发送者同时调也不会发两遍。同一进程内嵌套调用只计深度，不死锁。
        """
        if self._status_lock_depth > 0:
            self._status_lock_depth += 1
            try:
                yield
            finally:
                self._status_lock_depth -= 1
            return
        lock_path = self.state_dir / "status.lock"
        fp = open(lock_path, "w", encoding="utf-8")
        deadline = time.monotonic() + 30  # 发消息最慢约 15 秒超时，留一倍余量
        try:
            while True:
                try:
                    fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise BridgeError("状态锁等待超时（30 秒）")
                    time.sleep(0.1)
            self._status_lock_fp = fp
            self._status_lock_depth = 1
            try:
                yield
            finally:
                self._status_lock_depth = 0
                self._status_lock_fp = None
                fcntl.flock(fp, fcntl.LOCK_UN)
        finally:
            fp.close()

    def set_status(self, message_id, state, note=""):
        """推进一条消息的状态，并同步 inbox 文件里的状态字段。"""
        if state not in STATUSES:
            raise ValueError(f"非法状态 {state}，只允许：{'/'.join(STATUSES)}")
        with self._status_lock():
            self._set_status_locked(message_id, state, note)

    def _set_status_locked(self, message_id, state, note=""):
        """set_status 的实际写操作（调用方须已持有状态锁）。"""
        data = self._load_status()
        entry = data.get(str(message_id), {})
        entry["status"] = state
        entry["updated_at"] = _now_iso()
        if note:
            entry["note"] = note
        data[str(message_id)] = entry
        _atomic_write_json(self.state_dir / "status.json", data)
        inbox_file = self.inbox_dir / f"{message_id}.json"
        if inbox_file.exists():
            try:
                record = json.loads(inbox_file.read_text(encoding="utf-8"))
                record["status"] = state
                _atomic_write_json(inbox_file, record)
            except (OSError, ValueError):
                pass

    # -- 二维码 -------------------------------------------------------------

    def resolve_qr_payload(self, img_content, qrcode):
        """决定二维码图片的来源与预期载荷。

        返回 (png_bytes, expected_payload)：
        - img_content 是 http(s) 网址时先尝试下载：拿到图片字节就直接用，
          expected_payload 为 None（解码核对只要求能解出非空内容）；
        - 否则直接把 img_content 这个网址本身编成二维码，
          expected_payload 就是这个网址（解码必须一字不差）；
        - 绝不能退回只编 32 位 qrcode token；img_content 为空时直接报错。
        """
        if not img_content:
            raise BridgeError(
                "服务端没有返回 qrcode_img_content，无法生成二维码"
                "（拒绝退回只编 token，请检查服务端响应）"
            )
        if img_content.startswith(("http://", "https://")):
            try:
                content_type, data = _download_bytes(img_content)
            except Exception as exc:  # 下载失败就改走“编网址”那条路
                self.log("warning", f"qrcode_img_content 下载失败，改走编网址：{exc}")
            else:
                if content_type.startswith("image/") or _is_image_bytes(data):
                    return data, None
                self.log("info", "qrcode_img_content 下载到的不是图片，改走编网址")
        # 直接编网址本身（liteapp 登录页网址也是网址，照编）
        return _encode_qr_png(img_content), img_content

    # -- 命令：login --------------------------------------------------------

    def cmd_login(self):
        """取二维码：调 get_bot_qrcode，生成 PNG 并解码核对，保存登录会话。

        只生成二维码，不等扫码结果；等扫码用 wait-login。
        """
        # 本人绑定：请求体只有 local_token_list。
        # 首次登录传空数组：带非空 token 列表时服务端可能返回一张“已绑定”
        # 死码，扫了没反应、状态恒为 wait（第三方实测坑）。
        body = {"local_token_list": []}
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _random_uin_b64(),
        }
        headers.update(_app_headers())
        url = f"{DEFAULT_API_BASE}/ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}"
        resp = _http_json("POST", url, body, headers, timeout=15)
        qrcode = resp.get("qrcode")
        img_content = resp.get("qrcode_img_content")
        if not qrcode:
            raise BridgeError("服务端没有返回 qrcode，请稍后重试")
        self.redactor.add(qrcode)
        self.redactor.add(img_content)

        # 生成 PNG 并用 OpenCV 解码核对：解不出或与预期不一致就拒绝展示
        png_bytes, expected = self.resolve_qr_payload(img_content, qrcode)
        decoded = _decode_qr_png(png_bytes)
        if expected is not None:
            if decoded != expected:
                raise BridgeError("二维码解码核对失败：解出内容与预期载荷不一致，已拒绝展示")
        elif not decoded:
            raise BridgeError("服务端返回的二维码图片无法解码，已拒绝展示")

        # 唯一文件名：时间戳 + 随机后缀，不叫 qr.png，避免新旧混淆
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"qr_{stamp}_{uuid.uuid4().hex[:6]}.png"
        qr_path = self.qr_dir / name
        qr_path.write_bytes(png_bytes)

        # 保存当次 qrcode 值（轮询用）到登录会话文件，0600
        _write_json_0600(self.login_session_path, {
            "qrcode": qrcode,
            "qr_file": name,
            "created_at": _now_iso(),
            "api_base": DEFAULT_API_BASE,
        })

        # 只打印图片路径，不打印 token 与二维码载荷原文
        print(f"二维码已生成：{qr_path}")
        print("请用微信扫一扫扫描（有效期很短，请尽快扫描）。")
        print("扫码后运行 wait-login 等待确认结果。")
        return 0


    # -- 命令：wait-login ----------------------------------------------------

    def cmd_wait_login(self):
        """轮询二维码状态，等用户扫码确认。

        单次长轮询 35 秒，总超时 60 秒（本项目取值，见 LOGIN_TOTAL_TIMEOUT 注释）。
        confirmed 保存凭证；expired / verify_code_blocked 报错停止，不自动重生成；
        need_verifycode 时等 state/pin.txt 里的数字（用后立即删除）。
        """
        login_session = _load_json(self.login_session_path)
        if not login_session or not login_session.get("qrcode"):
            raise BridgeError("没有进行中的登录：请先运行 login 生成二维码")
        qrcode = login_session["qrcode"]
        base = login_session.get("api_base") or DEFAULT_API_BASE
        self.redactor.add(qrcode)

        deadline = time.monotonic() + LOGIN_TOTAL_TIMEOUT
        pending_code = None  # 暂存的配对数字，下次轮询时带上
        pin_announced = False  # 是否已提示过“手机显示了数字”
        scanned_announced = False  # 是否已提示过“已扫码”
        try:
            while time.monotonic() < deadline:
                # 每轮检查 PIN 文件：有就读出、用后立即删除
                if self.pin_path.exists():
                    try:
                        pin = self.pin_path.read_text(encoding="utf-8").strip()
                    except OSError:
                        pin = ""
                    try:
                        self.pin_path.unlink()
                    except OSError:
                        pass
                    if pin:
                        pending_code = pin
                        self.log("info", "已读取配对数字，下次轮询将提交")

                params = {"qrcode": qrcode}
                if pending_code:
                    params["verify_code"] = pending_code
                url = base + "/ilink/bot/get_qrcode_status?" + urllib.parse.urlencode(params)
                try:
                    resp = _http_json("GET", url, None, _app_headers(),
                                      timeout=QR_LONG_POLL_TIMEOUT)
                except BridgeError as exc:
                    # 超时、取消或网络错误都按 wait 继续等（协议规定）
                    self.log("warning", f"轮询出错，按 wait 继续：{exc}")
                    time.sleep(1)
                    continue

                status = resp.get("status", "")

                # 需要配对数字：这次请求本身不带 verify_code，下次轮询再带
                if "need_verifycode" in status:
                    if pending_code is not None:
                        # 上次数字不对，清空，等新数字
                        pending_code = None
                        print("上次输入的数字不匹配，请重新输入。")
                    if not pin_announced:
                        print("手机微信显示了数字：请把数字告诉主聊天，"
                              "写入 state/pin.txt 后继续等待。")
                        pin_announced = True
                    time.sleep(2)
                    continue

                if status == "scaned":
                    if pending_code is not None:
                        pending_code = None  # 数字已被接受，丢掉暂存
                    if not scanned_announced:
                        print("已扫码，等待手机上确认…")
                        scanned_announced = True
                elif status == "confirmed":
                    bot_token = resp.get("bot_token")
                    bot_id = resp.get("ilink_bot_id")
                    if not bot_token or not bot_id:
                        raise BridgeError("登录失败：服务器未返回 bot_token/ilink_bot_id")
                    self.save_session({
                        "bot_token": bot_token,
                        "ilink_bot_id": bot_id,
                        "baseurl": resp.get("baseurl") or DEFAULT_API_BASE,
                        "ilink_user_id": resp.get("ilink_user_id") or "",
                        "saved_at": _now_iso(),
                    })
                    # 登录成功，清理一次性文件
                    try:
                        self.login_session_path.unlink()
                    except OSError:
                        pass
                    try:
                        self.pin_path.unlink()
                    except OSError:
                        pass
                    print(f"登录成功，已绑定用户 {resp.get('ilink_user_id') or ''}")
                    return 0
                elif status == "expired":
                    raise BridgeError("二维码已过期，请重新运行 login 生成新的二维码（不会自动重生成）")
                elif status == "verify_code_blocked":
                    raise BridgeError("数字错误次数过多，登录已停止；请重新运行 login")
                elif status == "scaned_but_redirect":
                    host = resp.get("redirect_host")
                    if host:
                        base = f"https://{host}"
                        self.log("info", "轮询地址已按 redirect_host 切换")
                elif status == "binded_redirect":
                    # 这个 Bot 已经绑定过本实例，不再签发新凭证：用已有凭证
                    if self.session_path.exists():
                        try:
                            self.login_session_path.unlink()
                        except OSError:
                            pass
                        print("该微信已绑定过本桥接，无需重复登录。")
                        return 0
                    raise BridgeError("该微信已绑定过其他实例，但本地没有凭证；请重新运行 login")
                # wait 或其他未知状态：继续等
                time.sleep(1)
        finally:
            # 退出时清掉残留的 PIN（数字一次一用，不留给下次）
            try:
                self.pin_path.unlink()
            except OSError:
                pass
        raise BridgeError("登录超时（60 秒），请重新运行 login 后再试")

    # -- 命令：serve --------------------------------------------------------

    def _get_updates(self, session, cursor, timeout):
        """调 getUpdates 拉新消息。客户端超时 = 服务端建议 + 5 秒余量。"""
        url = session["baseurl"].rstrip("/") + "/ilink/bot/getupdates"
        body = {"get_updates_buf": cursor, "base_info": _base_info()}
        return _http_json("POST", url, body, _auth_headers(session["bot_token"]),
                          timeout=timeout + 5)

    def _load_cursor(self):
        data = _load_json(self.state_dir / "cursor.json")
        if isinstance(data, dict):
            return data.get("get_updates_buf") or ""
        return ""

    def _save_cursor(self, cursor):
        _atomic_write_json(self.state_dir / "cursor.json",
                           {"get_updates_buf": cursor, "updated_at": _now_iso()})

    def _load_processed(self):
        data = _load_json(self.state_dir / "processed.json")
        if isinstance(data, list):
            return set(data)
        return set()

    def _save_processed(self, processed):
        # 只保留最近 PROCESSED_KEEP 条，避免文件无限长大
        items = sorted(processed, reverse=True)[:PROCESSED_KEEP]
        _atomic_write_json(self.state_dir / "processed.json", items)

    def _write_inbox(self, record):
        """把一条消息写进 inbox/<message_id>.json。"""
        _atomic_write_json(self.inbox_dir / f"{record['message_id']}.json", record)

    def process_message(self, msg, session, processed, sender=None,
                        media_downloader=None):
        """处理一条入站消息，返回处理结果（供日志与自测）。

        过滤规则：只处理扫码绑定用户的私聊；群消息、其他用户消息、
        机器人自己的消息直接忽略，不记录任务。
        内容分三类：文本（type 1）与图片/文件（type 2/4）下载后写 inbox
        记 received，等 worker 处理；语音/视频（type 3/5）等未接入的类型
        立即固定回复，不假装读过内容。
        sender 可注入（自测用 mock 代替真发）；默认用 self.send_message。
        media_downloader 可注入（自测用 mock 代替真下载）；
        默认用 MediaClient.download_item_media，签名与之一致：
        (item, kind, dest_dir, index) -> {kind, file_name, path, size, mime}。
        """
        mid = msg.get("message_id")
        if mid is None:
            return "ignored:no-id"
        if mid in processed:
            return "ignored:duplicate"
        processed.add(mid)

        owner = session["ilink_user_id"]
        if msg.get("message_type") == 2:
            return "ignored:self"  # 机器人自己的消息
        if msg.get("group_id"):
            return "ignored:group"  # 群消息
        if msg.get("from_user_id") != owner:
            return "ignored:other-user"  # 其他用户消息

        # 按类型分类：文本 / 已接入的媒体（图片、文件）/ 未接入的（语音、视频等）
        texts = []
        media_items = []  # (kind, item) 列表，kind 是 "image" 或 "file"
        unsupported_types = []
        for item in msg.get("item_list") or []:
            item_type = item.get("type")
            if item_type == media.ITEM_TYPE_TEXT:
                text = (item.get("text_item") or {}).get("text", "")
                if text:
                    texts.append(text)
            elif item_type == media.ITEM_TYPE_IMAGE:
                media_items.append(("image", item))
            elif item_type == media.ITEM_TYPE_FILE:
                media_items.append(("file", item))
            else:
                unsupported_types.append(item_type)

        record = {
            "message_id": mid,
            "from_user_id": msg.get("from_user_id"),
            "context_token": msg.get("context_token") or "",
            "create_time_ms": msg.get("create_time_ms"),
            "received_at": _now_iso(),
        }

        if texts or media_items:
            # 下载图片/文件（解密后落盘），与文本一起记成一条任务
            downloaded = []
            failed = []
            downloader = media_downloader
            if downloader is None and media_items:
                # 只有真有图片/文件要下载时才建客户端（纯文本消息不建）
                client = media.MediaClient(
                    session["baseurl"], session["bot_token"],
                    log=self.log, redact=self.redactor.add)
                downloader = client.download_item_media
            counts = {"image": 0, "file": 0}
            for kind, item in media_items:
                index = counts[kind]
                counts[kind] += 1
                try:
                    info = downloader(item, kind, self.media_dir / str(mid), index)
                except media.MediaError as exc:
                    failed.append({"kind": kind, "error": str(exc)})
                    self.log("warning", f"消息 {mid} 的{kind}下载失败：{exc}")
                    continue
                # 只记相对路径，不把绝对路径写进 inbox（worker 在同一目录下能找到）
                info["path"] = f"media/{mid}/{info['file_name']}"
                downloaded.append(info)
            if downloaded:
                record["media"] = downloaded
            if failed:
                record["media_failed"] = failed

            # 文本与媒体全灭（无文本、媒体全下载失败）：回“请再发一次”
            if not texts and not downloaded:
                record["type"] = "media-failed"
                record["item_types"] = [t for _, t in
                                        [(k, it.get("type")) for k, it in media_items]]
                self._write_inbox(record)
                return self._reply_fixed(
                    msg, mid, DOWNLOAD_FAILED_REPLY, sender, "media-failed")

            kinds = sorted({info["kind"] for info in downloaded})
            if texts and not downloaded:
                record["type"] = "text"
            elif not texts and kinds == ["image"]:
                record["type"] = "image"
            elif not texts and kinds == ["file"]:
                record["type"] = "file"
            else:
                record["type"] = "mixed"
            if texts:
                record["text"] = "\n".join(texts)
            if unsupported_types:
                # 同条消息里还混着语音/视频：记下来，worker 可决定是否提醒用户
                record["unsupported_types"] = unsupported_types
            self._write_inbox(record)
            self.set_status(mid, "received")
            detail = f"{len(record.get('text', ''))} 字" if texts else ""
            if downloaded:
                detail += f"{'、' if detail else ''}{len(downloaded)} 个媒体"
            self.log("info", f"收到{record['type']}消息 {mid}（{detail}），已记 received")
            return "received"

        # 只有语音/视频等未接入类型：立即固定回复，不假装读过内容
        record["type"] = "non-text"
        record["item_types"] = [item.get("type") for item in msg.get("item_list") or []]
        self._write_inbox(record)
        return self._reply_fixed(msg, mid, NON_TEXT_REPLY, sender, "non-text")

    def _reply_fixed(self, msg, mid, reply_text, sender, label):
        """给一条消息回固定文案。HTTP 成功即记 sent，不等 ret；只有发送报错
        才记 pending-send，等外部 worker 处理。返回处理结果字符串。"""
        send = sender or self.send_message
        ok, confirmed, errmsg = send(
            msg.get("from_user_id"), msg.get("context_token") or "", reply_text
        )
        if ok:
            # HTTP 成功即记 sent，不等 ret（微信经常不回 ret，但消息实际能到）
            self.set_status(mid, "sent")
            self.log("info", f"{label}消息 {mid} 已固定回复，记 sent")
            return f"replied:{label}"
        self.set_status(mid, "pending-send", note=errmsg)
        self.log("warning", f"{label}消息 {mid} 固定回复发送失败，记 pending-send：{errmsg}")
        return f"replied-unconfirmed:{label}"

    def cmd_serve(self):
        """收消息服务：长轮询 getUpdates，只收不执行。

        单实例运行；游标持久化；按 message_id 去重；Ctrl+C 或 SIGTERM 优雅停止。
        """
        session = self.load_session()
        if not self.acquire_lock():
            raise BridgeError("serve 已在运行（锁被占用），请先停掉旧的再启动")

        def _on_signal(signum, _frame):
            self._stop = True

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
        try:
            cursor = self._load_cursor()
            processed = self._load_processed()
            poll_timeout = GETUPDATES_TIMEOUT
            print("serve 已启动，正在收消息…（Ctrl+C 停止）")
            self.log("info", "serve 已启动")
            while not self._stop:
                try:
                    resp = self._get_updates(session, cursor, poll_timeout)
                except BridgeError as exc:
                    self.log("warning", f"getUpdates 出错，3 秒后重试：{exc}")
                    time.sleep(3)
                    continue
                # 服务端建议的下次超时时间
                suggested = resp.get("longpolling_timeout_ms")
                if isinstance(suggested, (int, float)) and suggested > 0:
                    poll_timeout = max(5, int(suggested) // 1000)
                ret = resp.get("ret")
                errcode = resp.get("errcode")
                if ret not in (None, 0):
                    if ret == -14 or errcode == -14:
                        raise BridgeError("会话已过期（-14），serve 已停止；请重新登录")
                    self.log("warning", f"getUpdates 返回 ret={ret}，5 秒后重试")
                    time.sleep(5)
                    continue
                # 只在返回非空时更新游标（协议规定）
                new_cursor = resp.get("get_updates_buf")
                if new_cursor:
                    cursor = new_cursor
                    self._save_cursor(cursor)
                for msg in resp.get("msgs") or []:
                    try:
                        result = self.process_message(msg, session, processed)
                    except Exception as exc:  # 单条消息出错不拖垮整个服务
                        self.log("error", f"处理消息时出错，已跳过：{exc}")
                        continue
                    if result.startswith("ignored"):
                        self.log("info", f"消息已忽略（{result}）")
                self._save_processed(processed)
            print("serve 已停止")
            self.log("info", "serve 已停止")
            return 0
        finally:
            self.release_lock()


    # -- 发消息 -------------------------------------------------------------

    def _build_outbound_msg(self, to_user_id, context_token, item_list):
        """拼 sendMessage 的 msg 信封（文本与媒体共用）。

        每个请求只带一个 item（官方做法：文本与媒体拆成独立请求，各用
        独立 client_id）；client_id 每次随机生成，不复用。
        """
        msg = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": uuid.uuid4().hex,
            "message_type": 2,
            "message_state": 2,
            "item_list": item_list,
        }
        if context_token:
            msg["context_token"] = context_token
        else:
            # 协议：缺少令牌时客户端只记警告并继续发，不证明服务端一定接受
            self.log("warning", "sendMessage 缺少 context_token，仍尝试发送")
        return msg

    def _post_send_message(self, msg, log_label):
        """POST 一个 sendMessage 请求，返回 (ok, confirmed, errmsg)。

        - ok=True, confirmed=True：ret==0，服务端已接受；
        - ok=True, confirmed=False：响应缺 ret，无法断言送达；
        - ok=False：HTTP 失败或 ret 非零，errmsg 说明原因。
        log_label 只写类型与大小，不写内容。
        """
        session = self.load_session()
        body = {"msg": msg, "base_info": _base_info()}
        url = session["baseurl"].rstrip("/") + "/ilink/bot/sendmessage"
        try:
            resp = _http_json("POST", url, body, _auth_headers(session["bot_token"]),
                              timeout=15)
        except BridgeError as exc:
            self.log("warning", f"sendMessage 失败：{exc}")
            return False, False, str(exc)
        if "ret" not in resp:
            # 微信经常不回 ret，但消息实际能到（2026-11-22 实测）；只记日志
            self.log("info", f"sendMessage 已发出（无 ret，{log_label}）")
            return True, False, "响应没有 ret 字段"
        if resp.get("ret") != 0:
            errmsg = resp.get("errmsg") or f"ret={resp.get('ret')}"
            self.log("warning", f"sendMessage 失败：{errmsg}")
            return False, False, errmsg
        self.log("info", f"sendMessage 已发出（ret=0，{log_label}）")
        return True, True, ""

    def send_message(self, to_user_id, context_token, text):
        """发一条文本消息。返回 (ok, confirmed, errmsg)，含义见 _post_send_message。"""
        msg = self._build_outbound_msg(
            to_user_id, context_token,
            [{"type": 1, "text_item": {"text": text}}])
        return self._post_send_message(msg, f"{len(text)} 字")

    def send_message_item(self, to_user_id, context_token, item, log_label):
        """发一条单 item 消息（图片/文件等媒体项）。返回同 send_message。"""
        msg = self._build_outbound_msg(to_user_id, context_token, [item])
        return self._post_send_message(msg, log_label)

    def _upload_media_file(self, path, to_user_id, kind):
        """把一个本地文件上传到微信 CDN，返回上传信息字典（见 media.py）。"""
        session = self.load_session()
        client = media.MediaClient(
            session["baseurl"], session["bot_token"],
            log=self.log, redact=self.redactor.add)
        media_type = (media.MEDIA_TYPE_IMAGE if kind == "image"
                      else media.MEDIA_TYPE_FILE)
        return client.upload_file(path, to_user_id, media_type)

    def send_media(self, to_user_id, context_token, path, kind, caption="",
                   uploader=None):
        """上传并发送一个图片/文件。返回 (ok, confirmed, errmsg)，含义同文本。

        kind 是 "image" 或 "file"；caption 是可选的说明文字，有则先发一条
        文本再发媒体（官方做法：两个独立请求）。说明文字发失败时不发媒体。
        uploader 可注入（自测用 mock 代替真上传）；默认用 self._upload_media_file。
        """
        path = Path(path)
        if kind not in ("image", "file"):
            return False, False, f"不支持的媒体类型 {kind}（只支持 image/file）"
        if not path.exists():
            return False, False, f"文件不存在：{path}"
        size = path.stat().st_size
        if size == 0:
            return False, False, "文件为空，无法发送"
        if size > media.MAX_MEDIA_BYTES:
            return False, False, "文件超过 100MB 上限，无法发送"
        if caption:
            ok, _confirmed, errmsg = self.send_message(
                to_user_id, context_token, caption)
            if not ok:
                return False, False, f"说明文字发送失败：{errmsg}"
        upload = uploader or self._upload_media_file
        try:
            uploaded = upload(path, to_user_id, kind)
        except media.MediaError as exc:
            self.log("warning", f"媒体上传失败：{exc}")
            return False, False, str(exc)
        if kind == "image":
            item = media.build_image_item(uploaded)
            label = f"图片 {size} 字节"
        else:
            item = media.build_file_item(uploaded, path.name)
            label = f"文件 {path.name} {size} 字节"
        return self.send_message_item(to_user_id, context_token, item, label)

    def cmd_send(self, args):
        """发文本给指定用户。文本从文件或 stdin 读，避免进 shell 历史。"""
        if args.text_file:
            text = Path(args.text_file).read_text(encoding="utf-8")
        else:
            text = sys.stdin.read()
        text = text.rstrip("\n")
        if not text.strip():
            raise BridgeError("要发送的文本为空")
        ok, confirmed, errmsg = self.send_message(
            args.to, args.context_token or "", text
        )
        if ok and confirmed:
            print("服务端已接受（ret=0）。")
            return 0
        if ok and not confirmed:
            # 缺 ret：微信经常不回 ret，但消息实际能到；HTTP 成功即算发出
            print("已发出（HTTP 成功，微信未回 ret）。")
            return 0
        print(f"发送失败：{errmsg}", file=sys.stderr)
        return 1

    def cmd_send_media(self, args):
        """发图片/文件给指定用户（先上传 CDN 再发送）。

        --type 不给时按文件头自动判定（图片魔数 → image，否则 file）。
        底层单次发送、不查状态，日常请用 send-once + 媒体清单（见 README）。
        """
        path = Path(args.file)
        kind = args.type
        if not kind:
            try:
                kind = media.detect_kind(path)
            except media.MediaError as exc:
                raise BridgeError(str(exc)) from exc
        ok, confirmed, errmsg = self.send_media(
            args.to, args.context_token or "", path, kind,
            caption=args.caption or "")
        if ok and confirmed:
            print("服务端已接受（ret=0）。")
            return 0
        if ok and not confirmed:
            print("已发出（HTTP 成功，微信未回 ret）。")
            return 0
        print(f"发送失败：{errmsg}", file=sys.stderr)
        return 1

    def cmd_send_once(self, args):
        """按 message_id 只发一次：查状态→发→记 sent，全程状态锁保护。

        两个发送者（hook worker 与主聊天）同时调也不会发两遍：
        查状态、发消息、记 sent 在同一把锁里完成；已是 sent 的直接跳过。
        HTTP 成功即记 sent，不等 ret。

        文本回复：读 outbox/<message_id>.txt。
        媒体回复：worker 先写 outbox/<message_id>.media.json 清单
        {"type": "file"|"image", "file": "<outbox 里的文件名>",
         "caption": "可选说明文字"}，并把文件放进 outbox/；
        本命令读清单发媒体（说明文字先发）。清单里的 file 必须是纯文件名，
        带路径分隔符直接拒绝（防目录穿越）。
        """
        message_id = str(args.message_id)
        with self._status_lock():
            data = self._load_status()
            entry = data.get(message_id) or {}
            if entry.get("status") == "sent":
                print(f"{message_id} 已发过，跳过。")
                return 0
            inbox_file = self.inbox_dir / f"{message_id}.json"
            if not inbox_file.exists():
                raise BridgeError(f"inbox 里没有 {message_id}，无法发送")
            record = json.loads(inbox_file.read_text(encoding="utf-8"))
            if not record.get("from_user_id"):
                raise BridgeError(f"inbox 里 {message_id} 缺少 from_user_id")
            manifest_path = self.outbox_dir / f"{message_id}.media.json"
            if manifest_path.exists():
                # 媒体回复：按清单发图片/文件
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except ValueError as exc:
                    raise BridgeError(f"媒体清单 {message_id}.media.json 不是合法 JSON") from exc
                kind = manifest.get("type")
                fname = manifest.get("file") or ""
                if kind not in ("image", "file"):
                    raise BridgeError("媒体清单 type 必须是 image 或 file")
                if not fname or Path(fname).name != fname:
                    raise BridgeError("媒体清单 file 必须是 outbox 里的纯文件名，不许带路径")
                media_path = self.outbox_dir / fname
                if not media_path.exists():
                    raise BridgeError(f"outbox 里没有媒体文件 {fname}")
                ok, confirmed, errmsg = self.send_media(
                    record.get("from_user_id"), record.get("context_token") or "",
                    media_path, kind, caption=manifest.get("caption") or "")
                if ok:
                    self.set_status(message_id, "sent",
                                    note="" if confirmed else "无 ret，HTTP 成功即记 sent")
                    self.log("info", f"{message_id} 媒体回复已发出，记 sent")
                    print(f"{message_id} 已发出，记 sent。")
                    return 0
                self.set_status(message_id, "pending-send", note=errmsg)
                print(f"发送失败：{errmsg}", file=sys.stderr)
                return 1
            outbox_file = self.outbox_dir / f"{message_id}.txt"
            if not outbox_file.exists():
                raise BridgeError(f"outbox 里没有 {message_id}.txt，先写好回复再发")
            text = outbox_file.read_text(encoding="utf-8").rstrip("\n")
            if not text.strip():
                raise BridgeError("要发送的文本为空")
            ok, confirmed, errmsg = self.send_message(
                record.get("from_user_id"), record.get("context_token") or "", text
            )
            if ok:
                self.set_status(message_id, "sent",
                                note="" if confirmed else "无 ret，HTTP 成功即记 sent")
                self.log("info", f"{message_id} 回复已发出，记 sent")
                print(f"{message_id} 已发出，记 sent。")
                return 0
            self.set_status(message_id, "pending-send", note=errmsg)
            print(f"发送失败：{errmsg}", file=sys.stderr)
            return 1

    # -- 命令：status ---------------------------------------------------------

    def cmd_status(self):
        """显示登录状态、serve 运行状态、inbox 各状态计数、最近活动时间。

        一律不输出 token 等凭证。
        """
        # 登录状态
        data = _load_json(self.session_path)
        if data and data.get("bot_token"):
            self.redactor.add(data["bot_token"])
            print("登录：已登录")
            print(f"绑定用户：{data.get('ilink_user_id') or ''}")
            print(f"接口地址：{data.get('baseurl') or ''}")
        else:
            print("登录：未登录")

        # serve 运行状态
        running, pid = self.serve_running()
        if running:
            print(f"serve：运行中（PID {pid}）" if pid else "serve：运行中")
        else:
            print("serve：未运行")

        # inbox 各状态计数
        status_data = self._load_status()
        counts = {s: 0 for s in STATUSES}
        for entry in status_data.values():
            state = entry.get("status") if isinstance(entry, dict) else None
            if state in counts:
                counts[state] += 1
        inbox_files = list(self.inbox_dir.glob("*.json"))
        print(f"inbox 文件：{len(inbox_files)} 条")
        print("状态计数：" + "、".join(f"{s} {counts[s]}" for s in STATUSES))

        # 最近活动时间（取 inbox 文件、游标、状态文件里最新的修改时间；
        # 不取日志文件，因为 status 命令本身就会碰日志文件）
        latest = 0.0
        for path in list(self.inbox_dir.glob("*.json")) + [
            self.state_dir / "cursor.json",
            self.state_dir / "status.json",
        ]:
            try:
                latest = max(latest, path.stat().st_mtime)
            except OSError:
                pass
        if latest:
            when = datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M:%S")
            print(f"最近活动：{when}")
        else:
            print("最近活动：无")
        return 0

    # -- 命令：mark -----------------------------------------------------------

    def cmd_mark(self, args):
        """推进一条消息的状态（供外部 worker 调用）。"""
        try:
            self.set_status(args.message_id, args.state)
        except ValueError as exc:
            raise BridgeError(str(exc)) from exc
        print(f"{args.message_id} -> {args.state}")
        return 0


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def build_parser():
    """构建 argparse 解析器（子命令 + 中文帮助）。"""
    parser = argparse.ArgumentParser(
        prog="bridge.py",
        description="Muse → 微信 ClawBot 桥接：只做消息收发，不执行任务。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="取二维码（生成 PNG 并解码核对，不等扫码结果）")
    sub.add_parser("wait-login", help="轮询二维码状态，等用户扫码确认（总超时 60 秒）")
    sub.add_parser("serve", help="收消息服务：长轮询 getUpdates，只收不执行")

    send_parser = sub.add_parser("send", help="发文本给指定用户")
    send_parser.add_argument("--to", required=True, help="目标用户 ID（to_user_id）")
    send_parser.add_argument("--context-token", default="",
                             help="会话上下文令牌（入站消息里的 context_token）")
    send_parser.add_argument("--text-file", default="",
                             help="文本文件路径；不给则从 stdin 读")

    send_once_parser = sub.add_parser(
        "send-once", help="按消息 ID 只发一次（查状态→发→记 sent，全程加锁防重发）")
    send_once_parser.add_argument("message_id", help="消息 ID（inbox/outbox 里的文件名）")

    send_media_parser = sub.add_parser(
        "send-media", help="发图片/文件给指定用户（先上传 CDN 再发送）")
    send_media_parser.add_argument("--to", required=True, help="目标用户 ID（to_user_id）")
    send_media_parser.add_argument("--file", required=True, help="要发送的本地文件路径")
    send_media_parser.add_argument("--type", choices=["image", "file"], default="",
                                   help="媒体类型；不给则按文件头自动判定")
    send_media_parser.add_argument("--caption", default="",
                                   help="可选说明文字（先发一条文本再发媒体）")
    send_media_parser.add_argument("--context-token", default="",
                                   help="会话上下文令牌（入站消息里的 context_token）")

    sub.add_parser("status", help="显示登录状态、serve 状态、inbox 计数、最近活动")

    mark_parser = sub.add_parser("mark", help="推进一条消息的状态（供外部 worker 调用）")
    mark_parser.add_argument("message_id", help="消息 ID")
    mark_parser.add_argument("state", choices=list(STATUSES), help="新状态")
    return parser


def main(argv=None):
    """命令行入口：按子命令分发；业务错误打印中文原因后退出码 1。"""
    args = build_parser().parse_args(argv)
    bridge = Bridge(Path(__file__).resolve().parent)
    try:
        if args.command == "login":
            return bridge.cmd_login()
        if args.command == "wait-login":
            return bridge.cmd_wait_login()
        if args.command == "serve":
            return bridge.cmd_serve()
        if args.command == "send":
            return bridge.cmd_send(args)
        if args.command == "send-once":
            return bridge.cmd_send_once(args)
        if args.command == "send-media":
            return bridge.cmd_send_media(args)
        if args.command == "status":
            return bridge.cmd_status()
        if args.command == "mark":
            return bridge.cmd_mark(args)
    except BridgeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
