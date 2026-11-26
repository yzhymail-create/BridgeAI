#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桥接自测（不调登录接口、不生成真实二维码、不联网发消息）。

测试项：
① 二维码“编码→解码”往返核对（示例 liteapp 网址）+ 载荷判定规则
   （下载到 HTML 就编网址本身、绝不退回编 token；img_content 为空直接报错）
② 消息过滤（群消息/他人消息/自己消息被忽略、主人文本进 inbox、
   主人语音触发固定回复文案——发回复那步用 mock，不真发）
   注：图片/文件已接入，不再走固定回复，见 selftest_media.py ⑦。
③ redact 脱敏（日志含 token 时被抹掉）
④ 单实例锁（第二个实例抢不到锁）
⑤ 状态推进（received→executing→pending-send→sent，非法状态被拒绝）
⑥ send-once 只发一次（mock 发送：顺序调两次、并发调两次都只发一次；
   无 ret 但 HTTP 成功记 sent；失败留 pending-send 可重试；非文字无 ret 也记 sent）
⑦ note 附加消息只发一次（mock 发送：同一个 key 顺序/并发调两次都只发一次，
   不同 key 各发各的；不碰主状态；失败不记 key 可重试；非法 key 与空文本被拒绝）
⑧ 只重试 407（最多 3 次、各等 5 秒）；连接中断与服务端错误不重试
   （中断时消息可能已发出，重试会把同一条消息再发一遍）
⑨ 正文守卫（为空或以 -- 开头拒发：send_message/send-once/note/send
   与媒体说明文字都拦，合法正文放行）
⑩ 正在输入（getConfig 取 typing_ticket 缓存 9 分钟、sendTyping
   start/cancel 的 payload 与协议一致；失败静默降级不阻塞；
   ack 亮起、send-once 熄灭）
⑪ 连发合并支撑（pending 列同发送人 received、merged 状态与备注）

用法：.venv/bin/python selftest.py
全部通过退出码 0，任一项失败退出码 1。
"""

import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge
from bridge import Bridge, BridgeError, Redactor

# 示例 liteapp 登录页网址（假的，只用来测编解码往返，不代表真实载荷）
SAMPLE_LITEAPP_URL = "liteapp://wxalite-demo/pages/login/entry?ticket=0123456789abcdef"


def _make_bridge(tmp_path):
    """在临时目录里建一个 Bridge 实例（不碰真实 state/）。"""
    return Bridge(tmp_path)


def test_qr_roundtrip():
    """① 编码→解码往返 + 载荷判定规则。"""
    # 往返：编示例网址，解出来必须一字不差
    png = bridge._encode_qr_png(SAMPLE_LITEAPP_URL)
    assert png.startswith(b"\x89PNG"), "编出的不是 PNG"
    decoded = bridge._decode_qr_png(png)
    assert decoded == SAMPLE_LITEAPP_URL, f"解码不一致：{decoded!r}"

    # 判定：下载到 HTML（不是图片）→ 编网址本身，绝不是 token
    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        real_download = bridge._download_bytes
        bridge._download_bytes = lambda url, **kw: ("text/html", b"<html>login</html>")
        try:
            out_png, expected = b.resolve_qr_payload(
                "https://example.com/login-page", "0123456789abcdef0123456789abcdef"
            )
        finally:
            bridge._download_bytes = real_download
        assert expected == "https://example.com/login-page", "应编网址本身"
        assert bridge._decode_qr_png(out_png) == expected, "编网址后解码不一致"

    # 判定：下载拿到图片字节 → 直接用图片
    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        real_download = bridge._download_bytes
        bridge._download_bytes = lambda url, **kw: ("image/png", png)
        try:
            out_png, expected = b.resolve_qr_payload(
                "https://example.com/qr.png", "0123456789abcdef0123456789abcdef"
            )
        finally:
            bridge._download_bytes = real_download
        assert out_png == png and expected is None, "图片字节应直接用"

    # 判定：img_content 为空 → 直接报错，绝不退回编 token
    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        try:
            b.resolve_qr_payload("", "0123456789abcdef0123456789abcdef")
        except BridgeError:
            pass
        else:
            raise AssertionError("img_content 为空时应报错")


def test_filtering():
    """② 消息过滤 + 非文字固定回复（mock 发送，不真发）。"""
    sent = []

    def mock_sender(to_user_id, context_token, text):
        sent.append((to_user_id, context_token, text))
        return True, True, ""

    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        session = {"ilink_user_id": "owner123"}
        processed = set()

        base = {"from_user_id": "owner123", "message_type": 1, "group_id": "",
                "context_token": "ctx1", "create_time_ms": 1700000000000}

        # 群消息：忽略
        r = b.process_message({**base, "message_id": 1, "group_id": "g1",
                               "item_list": [{"type": 1, "text_item": {"text": "hi"}}]},
                              session, processed, sender=mock_sender)
        assert r == "ignored:group", r
        # 他人消息：忽略
        r = b.process_message({**base, "message_id": 2, "from_user_id": "stranger",
                               "item_list": [{"type": 1, "text_item": {"text": "hi"}}]},
                              session, processed, sender=mock_sender)
        assert r == "ignored:other-user", r
        # 机器人自己的消息：忽略
        r = b.process_message({**base, "message_id": 3, "message_type": 2,
                               "item_list": [{"type": 1, "text_item": {"text": "hi"}}]},
                              session, processed, sender=mock_sender)
        assert r == "ignored:self", r
        # 主人文本：进 inbox，状态 received
        r = b.process_message({**base, "message_id": 4,
                               "item_list": [{"type": 1, "text_item": {"text": "你好"}}]},
                              session, processed, sender=mock_sender)
        assert r == "received", r
        inbox_file = Path(tmp) / "inbox" / "4.json"
        assert inbox_file.exists(), "主人文本应写进 inbox"
        assert json.loads(inbox_file.read_text(encoding="utf-8"))["text"] == "你好"
        # 重复消息：去重
        r = b.process_message({**base, "message_id": 4,
                               "item_list": [{"type": 1, "text_item": {"text": "你好"}}]},
                              session, processed, sender=mock_sender)
        assert r == "ignored:duplicate", r
        # 主人语音（未接入的类型）：固定回复文案，状态 sent，不真发
        r = b.process_message({**base, "message_id": 5,
                               "item_list": [{"type": 3, "voice_item": {}}]},
                              session, processed, sender=mock_sender)
        assert r == "replied:non-text", r
        assert len(sent) == 1, "非文字消息应恰好触发一次固定回复"
        assert sent[0][2] == bridge.NON_TEXT_REPLY, "回复文案必须是固定文案"

        status = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
        assert status["4"]["status"] == "received", "文本消息状态应为 received"
        assert status["5"]["status"] == "sent", "非文字消息状态应为 sent"
        assert "1" not in status and "2" not in status and "3" not in status, \
            "被忽略的消息不应记状态"


def test_redact():
    """③ redact 脱敏。"""
    redactor = Redactor()
    fake_token = "bot_token_0123456789abcdef"
    redactor.add(fake_token)
    redactor.add("qr_payload_secret_value")
    dirty = f"请求头 Authorization: Bearer {fake_token}，载荷 qr_payload_secret_value"
    clean = redactor.scrub(dirty)
    assert fake_token not in clean, "token 应被抹掉"
    assert "qr_payload_secret_value" not in clean, "载荷应被抹掉"
    assert "***" in clean, "脱敏后应有 *** 占位"
    # 太短的值不登记（避免误伤正常文本）
    redactor.add("short")
    assert redactor.scrub("short text") == "short text"


def test_single_instance_lock():
    """④ 单实例锁。"""
    with tempfile.TemporaryDirectory() as tmp:
        first = _make_bridge(tmp)
        second = _make_bridge(tmp)
        assert first.acquire_lock() is True, "第一个实例应抢到锁"
        assert second.acquire_lock() is False, "第二个实例应抢不到锁"
        first.release_lock()
        assert second.acquire_lock() is True, "第一个释放后第二个应抢到锁"
        second.release_lock()


def test_state_machine():
    """⑤ 状态推进。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        for state in ("received", "executing", "pending-send", "sent"):
            b.set_status(99, state)
            data = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
            assert data["99"]["status"] == state, f"状态应为 {state}"
        try:
            b.set_status(99, "done")
        except ValueError:
            pass
        else:
            raise AssertionError("非法状态应被拒绝")


def test_send_once():
    """⑥ send-once 只发一次（mock 发送，不真发）+ 无 ret 记 sent。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        inbox_file = Path(tmp) / "inbox" / "777.json"
        inbox_file.write_text(json.dumps({
            "message_id": 777, "from_user_id": "owner123",
            "context_token": "ctx777", "text": "你好",
        }, ensure_ascii=False), encoding="utf-8")
        (Path(tmp) / "outbox" / "777.txt").write_text("你好，我是 Muse。", encoding="utf-8")
        b.set_status(777, "pending-send")

        # 无 ret 但 HTTP 成功：顺序调两次，只发一次，记 sent
        calls = []

        def mock_sender(to_user_id, context_token, text):
            calls.append((to_user_id, context_token, text))
            return True, False, "响应没有 ret 字段"

        real_send = Bridge.send_message
        Bridge.send_message = lambda self, to, ctx, text: mock_sender(to, ctx, text)
        try:
            args = argparse.Namespace(message_id="777")
            assert b.cmd_send_once(args) == 0, "第一次 send-once 应成功"
            b2 = _make_bridge(tmp)  # 另一个实例模拟另一个发送者
            assert b2.cmd_send_once(args) == 0, "第二次 send-once 应跳过"
        finally:
            Bridge.send_message = real_send
        assert len(calls) == 1, f"应恰好发送一次，实际 {len(calls)} 次"
        assert calls[0][0] == "owner123" and calls[0][2] == "你好，我是 Muse。"
        status = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
        assert status["777"]["status"] == "sent", "无 ret 但 HTTP 成功应记 sent"

        # 并发调两次（模拟 worker 与主聊天同时发），也只发一次
        b.set_status(777, "pending-send")
        slow_calls = []

        def slow_sender(to_user_id, context_token, text):
            time.sleep(0.3)
            slow_calls.append(text)
            return True, True, ""

        Bridge.send_message = lambda self, to, ctx, text: slow_sender(to, ctx, text)
        try:
            barrier = threading.Barrier(2)
            results = []

            def run_once(inst):
                barrier.wait(timeout=5)
                results.append(inst.cmd_send_once(argparse.Namespace(message_id="777")))

            t1 = threading.Thread(target=run_once, args=(b,))
            t2 = threading.Thread(target=run_once, args=(b2,))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
        finally:
            Bridge.send_message = real_send
        assert not t1.is_alive() and not t2.is_alive(), "并发调用不应卡死"
        assert sorted(results) == [0, 0], f"并发调用都应返回 0：{results}"
        assert len(slow_calls) == 1, f"并发应恰好发送一次，实际 {len(slow_calls)} 次"

        # 发送失败时留在 pending-send，允许重试
        fail_calls = []

        def fail_sender(to_user_id, context_token, text):
            fail_calls.append(text)
            return False, False, "网络错误"

        Bridge.send_message = lambda self, to, ctx, text: fail_sender(to, ctx, text)
        try:
            b.set_status(777, "pending-send")
            assert b.cmd_send_once(args) == 1, "发送失败应返回 1"
        finally:
            Bridge.send_message = real_send
        assert len(fail_calls) == 1
        status = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
        assert status["777"]["status"] == "pending-send", "失败应留在 pending-send"

        # 非文字消息无 ret 也记 sent（用语音项：图片/文件已接入，不再走固定回复）
        def mock_sender2(to_user_id, context_token, text):
            return True, False, "响应没有 ret 字段"

        r = b.process_message(
            {"message_id": 8, "from_user_id": "owner123", "message_type": 1,
             "group_id": "", "context_token": "ctx8", "create_time_ms": 1700000000000,
             "item_list": [{"type": 3, "voice_item": {}}]},
            {"ilink_user_id": "owner123"}, set(), sender=mock_sender2)
        assert r == "replied:non-text", r
        status = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
        assert status["8"]["status"] == "sent", "非文字无 ret 应记 sent"


def test_note():
    """⑦ note 附加消息只发一次（mock 发送，不真发）+ 不碰主状态。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        inbox_file = Path(tmp) / "inbox" / "888.json"
        inbox_file.write_text(json.dumps({
            "message_id": 888, "from_user_id": "owner123",
            "context_token": "ctx888", "text": "帮我推一下代码",
        }, ensure_ascii=False), encoding="utf-8")
        ack_file = Path(tmp) / "ack.txt"
        ack_file.write_text("收到：推代码，约半小时后反馈。", encoding="utf-8")
        b.set_status(888, "executing")

        # 同一个 key 顺序调两次只发一次，不同 key 各发各的，无 ret 也算发出
        calls = []

        def mock_sender(to_user_id, context_token, text):
            calls.append((to_user_id, context_token, text))
            return True, False, "响应没有 ret 字段"

        real_send = Bridge.send_message
        Bridge.send_message = lambda self, to, ctx, text: mock_sender(to, ctx, text)
        try:
            args = argparse.Namespace(message_id="888", key="ack",
                                      text_file=str(ack_file))
            assert b.cmd_note(args) == 0, "第一次 note 应成功"
            b2 = _make_bridge(tmp)  # 另一个实例模拟另一个发送者
            assert b2.cmd_note(args) == 0, "同一个 key 第二次应跳过"
            appr_file = Path(tmp) / "appr.txt"
            appr_file.write_text("需要你在 Muse 里点一下确认。", encoding="utf-8")
            args2 = argparse.Namespace(message_id="888", key="approval",
                                       text_file=str(appr_file))
            assert b.cmd_note(args2) == 0, "不同 key 应发出"
        finally:
            Bridge.send_message = real_send
        assert len(calls) == 2, f"应恰好发送两次，实际 {len(calls)} 次"
        assert calls[0][0] == "owner123"
        assert calls[0][2] == "收到：推代码，约半小时后反馈。"
        assert calls[1][2] == "需要你在 Muse 里点一下确认。"
        status = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
        assert status["888"]["status"] == "executing", "note 不应碰主状态"
        assert set(status["888"]["notes"]) == {"ack", "approval"}, "两个 key 都应记下"

        # 并发调同一个 key，也只发一次
        delay_file = Path(tmp) / "delay.txt"
        delay_file.write_text("要晚一点，约一小时后反馈。", encoding="utf-8")
        slow_calls = []

        def slow_sender(to_user_id, context_token, text):
            time.sleep(0.3)
            slow_calls.append(text)
            return True, True, ""

        Bridge.send_message = lambda self, to, ctx, text: slow_sender(to, ctx, text)
        try:
            barrier = threading.Barrier(2)
            results = []

            def run_note(inst):
                barrier.wait(timeout=5)
                results.append(inst.cmd_note(argparse.Namespace(
                    message_id="888", key="delay", text_file=str(delay_file))))

            t1 = threading.Thread(target=run_note, args=(b,))
            t2 = threading.Thread(target=run_note, args=(b2,))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
        finally:
            Bridge.send_message = real_send
        assert not t1.is_alive() and not t2.is_alive(), "并发调用不应卡死"
        assert sorted(results) == [0, 0], f"并发调用都应返回 0：{results}"
        assert len(slow_calls) == 1, f"并发应恰好发送一次，实际 {len(slow_calls)} 次"

        # 发送失败时不记 key，允许重试
        fail_calls = []

        def fail_sender(to_user_id, context_token, text):
            fail_calls.append(text)
            return False, False, "网络错误"

        Bridge.send_message = lambda self, to, ctx, text: fail_sender(to, ctx, text)
        try:
            retry_file = Path(tmp) / "retry.txt"
            retry_file.write_text("收到，正在处理。", encoding="utf-8")
            args3 = argparse.Namespace(message_id="888", key="retry",
                                       text_file=str(retry_file))
            assert b.cmd_note(args3) == 1, "发送失败应返回 1"
        finally:
            Bridge.send_message = real_send
        assert len(fail_calls) == 1
        status = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
        assert "retry" not in status["888"]["notes"], "失败不应记 key"
        Bridge.send_message = lambda self, to, ctx, text: mock_sender(to, ctx, text)
        try:
            assert b.cmd_note(args3) == 0, "失败后重试应成功"
        finally:
            Bridge.send_message = real_send

        # 非法 key 与空文本被拒绝
        try:
            b.cmd_note(argparse.Namespace(message_id="888", key="../x",
                                          text_file=str(ack_file)))
        except BridgeError:
            pass
        else:
            raise AssertionError("非法 key 应被拒绝")
        empty_file = Path(tmp) / "empty.txt"
        empty_file.write_text("  \n", encoding="utf-8")
        try:
            b.cmd_note(argparse.Namespace(message_id="888", key="empty",
                                          text_file=str(empty_file)))
        except BridgeError:
            pass
        else:
            raise AssertionError("空文本应被拒绝")


def test_http_json_proxy_retry():
    """⑧ 只重试 407；连接中断与服务端错误不重试（中断时消息可能已发出）。"""
    import urllib.error

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ret": 0}'

    calls = {"n": 0}
    sleeps = []

    def flaky_open(req, timeout):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise urllib.error.HTTPError(
                req.full_url, 407, "Proxy Auth Required", {}, None)
        return FakeResp()

    real_open = bridge._open_url
    real_sleep = bridge.time.sleep
    bridge._open_url = flaky_open
    bridge.time.sleep = lambda s: sleeps.append(s)
    try:
        out = bridge._http_json("GET", "https://example.invalid/x")
        assert out == {"ret": 0}, "两次 407 后应成功"
        assert calls["n"] == 3, f"应调 3 次，实际 {calls['n']} 次"
        assert sleeps == [5, 5], f"两次重试各等 5 秒，实际 {sleeps}"
        # 服务端 500 不重试
        calls["n"] = 0
        sleeps.clear()

        def err500(req, timeout):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 500, "Server Error", {}, None)

        bridge._open_url = err500
        try:
            bridge._http_json("GET", "https://example.invalid/x")
            raise AssertionError("500 应抛错")
        except BridgeError as exc:
            assert "500" in str(exc), "500 应原样抛错"
        assert calls["n"] == 1, "500 不应重试"
        assert sleeps == [], "500 不应等"
        # 连接中断不重试：消息可能已发出，重试会重发
        calls["n"] = 0
        sleeps.clear()

        def closed(req, timeout):
            calls["n"] += 1
            raise urllib.error.URLError(
                "Remote end closed connection without response")

        bridge._open_url = closed
        try:
            bridge._http_json("GET", "https://example.invalid/x")
            raise AssertionError("连接中断应抛错")
        except BridgeError as exc:
            assert "Remote end closed" in str(exc), "中断应原样抛错"
        assert calls["n"] == 1, "连接中断不应重试"
        assert sleeps == [], "连接中断不应等"
    finally:
        bridge._open_url = real_open
        bridge.time.sleep = real_sleep


def test_body_guard():
    """⑨ 正文守卫：为空或以 -- 开头拒发，各发送路径都拦。"""
    # 判定函数本身
    assert bridge._body_guard_error("") is not None
    assert bridge._body_guard_error("  \n ") is not None
    assert bridge._body_guard_error("--key ack") is not None
    assert bridge._body_guard_error("  --foo") is not None
    assert bridge._body_guard_error(None) is not None
    assert bridge._body_guard_error("你好") is None
    assert bridge._body_guard_error("结果是 a--b") is None
    assert bridge._body_guard_error("- 单横线开头不算") is None

    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        # send_message 守卫在联网之前：-- 开头直接拒发，不需要登录
        ok, confirmed, errmsg = b.send_message("owner123", "ctx", "--key ack")
        assert not ok and "正文守卫" in errmsg, errmsg
        ok, confirmed, errmsg = b.send_message("owner123", "ctx", "   ")
        assert not ok and "正文守卫" in errmsg, errmsg

        # note：-- 开头被拒，mock 发送不应被调，key 不记
        inbox_file = Path(tmp) / "inbox" / "999.json"
        inbox_file.write_text(json.dumps({
            "message_id": 999, "from_user_id": "owner123",
            "context_token": "ctx999", "text": "任务",
        }, ensure_ascii=False), encoding="utf-8")
        b.set_status(999, "executing")
        bad_file = Path(tmp) / "bad.txt"
        bad_file.write_text("--key ack", encoding="utf-8")
        calls = []
        real_send = Bridge.send_message
        Bridge.send_message = lambda self, to, ctx, text: calls.append(text) or (True, True, "")
        try:
            try:
                b.cmd_note(argparse.Namespace(message_id="999", key="ack",
                                              text_file=str(bad_file)))
            except BridgeError as exc:
                assert "正文守卫" in str(exc)
            else:
                raise AssertionError("note 正文以 -- 开头应被拒绝")
        finally:
            Bridge.send_message = real_send
        assert calls == [], "守卫拒发时不应调发送"
        status = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
        assert "notes" not in status["999"], "守卫拒发不应记 key"

        # send-once：outbox 文本以 -- 开头被拒，状态不变
        (Path(tmp) / "outbox" / "999.txt").write_text("--key", encoding="utf-8")
        b.set_status(999, "pending-send")
        try:
            b.cmd_send_once(argparse.Namespace(message_id="999"))
        except BridgeError as exc:
            assert "正文守卫" in str(exc)
        else:
            raise AssertionError("send-once 正文以 -- 开头应被拒绝")
        status = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
        assert status["999"]["status"] == "pending-send", "守卫拒发不应记 sent"

        # send 命令：-- 开头被拒
        try:
            b.cmd_send(argparse.Namespace(to="owner123", context_token="ctx",
                                          text_file=str(bad_file)))
        except BridgeError as exc:
            assert "正文守卫" in str(exc)
        else:
            raise AssertionError("send 正文以 -- 开头应被拒绝")

        # 媒体说明文字：-- 开头时说明文字先被守卫拦下，不上传、不发媒体
        doc = Path(tmp) / "doc.pdf"
        doc.write_bytes(b"%PDF-fake")
        upload_calls = []
        ok, _, errmsg = b.send_media(
            "owner123", "ctx", doc, "file", caption="--caption",
            uploader=lambda path, to, kind: upload_calls.append(path) or {})
        assert not ok and "说明文字" in errmsg, errmsg
        assert upload_calls == [], "说明文字被守卫拒发时不应上传媒体"


def test_typing():
    """⑩ 正在输入：payload 与协议一致、ticket 缓存 9 分钟、失败静默降级。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        (Path(tmp) / "state" / "session.json").write_text(json.dumps({
            "bot_token": "fake-token-0123456789", "ilink_bot_id": "bot1",
            "baseurl": "https://ilinkai.weixin.qq.com",
            "ilink_user_id": "owner123",
        }), encoding="utf-8")
        http_calls = []

        def fake_http(method, url, body=None, headers=None, timeout=35):
            http_calls.append((method, url, body))
            if url.endswith("/ilink/bot/getconfig"):
                return {"ret": 0, "typing_ticket": "ticket-abc-123456789"}
            if url.endswith("/ilink/bot/sendtyping"):
                return {}
            raise AssertionError(f"意外 URL：{url}")

        real_http = bridge._http_json
        bridge._http_json = fake_http
        try:
            # 亮起：先 getConfig 再 sendTyping(status=1)
            assert b.send_typing("owner123", "ctx1", True) is True
            assert len(http_calls) == 2, http_calls
            cfg = http_calls[0]
            assert cfg[0] == "POST" and cfg[1].endswith("/ilink/bot/getconfig")
            assert cfg[2]["ilink_user_id"] == "owner123"
            assert cfg[2]["context_token"] == "ctx1"
            assert "base_info" in cfg[2]
            typ = http_calls[1]
            assert typ[1].endswith("/ilink/bot/sendtyping")
            assert typ[2]["ilink_user_id"] == "owner123"
            assert typ[2]["typing_ticket"] == "ticket-abc-123456789"
            assert typ[2]["status"] == 1, "status=1 表示正在输入"
            assert "base_info" in typ[2]
            # 熄灭：缓存命中，不再调 getConfig，status=2
            http_calls.clear()
            assert b.send_typing("owner123", "ctx1", False) is True
            assert len(http_calls) == 1, "缓存期内不应重取 ticket"
            assert http_calls[0][2]["status"] == 2, "status=2 表示取消输入"
            # 缓存过期（fetched_at 超过 9 分钟）：重新取
            cache_path = Path(tmp) / "state" / "typing_ticket.json"
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            cache["fetched_at"] = time.time() - bridge.TYPING_TICKET_TTL - 1
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            http_calls.clear()
            assert b.send_typing("owner123", "ctx1", True) is True
            assert len(http_calls) == 2, "过期后应重新取 ticket"
            # 换发送人：缓存不复用，重新取
            http_calls.clear()
            assert b.send_typing("other999", "ctx2", True) is True
            assert len(http_calls) == 2, "不同用户不应复用缓存"
            # getConfig ret 非 0：静默降级返回 False，不抛错
            bridge._http_json = lambda *a, **kw: {"ret": -1, "typing_ticket": "x"}
            cache_path.unlink()
            assert b.send_typing("owner123", "ctx1", True) is False
            # 网络错误：静默降级返回 False，不抛错
            def boom(*a, **kw):
                raise BridgeError("网络错误：Remote end closed")
            bridge._http_json = boom
            assert b.send_typing("owner123", "ctx1", True) is False
        finally:
            bridge._http_json = real_http

    # 挂接点：ack 发出后亮起、send-once 发出后熄灭；typing 失败不影响发送结果
    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        inbox_file = Path(tmp) / "inbox" / "555.json"
        inbox_file.write_text(json.dumps({
            "message_id": 555, "from_user_id": "owner123",
            "context_token": "ctx555", "text": "任务",
        }, ensure_ascii=False), encoding="utf-8")
        (Path(tmp) / "outbox" / "555.txt").write_text("做完了。", encoding="utf-8")
        ack_file = Path(tmp) / "ack.txt"
        ack_file.write_text("收到：任务，约几分钟后反馈。", encoding="utf-8")
        b.set_status(555, "executing")
        typing_calls = []
        real_send = Bridge.send_message
        real_typing = Bridge.send_typing
        Bridge.send_message = lambda self, to, ctx, text: (True, True, "")
        Bridge.send_typing = lambda self, to, ctx, t: typing_calls.append((to, ctx, t)) or False
        try:
            assert b.cmd_note(argparse.Namespace(
                message_id="555", key="ack", text_file=str(ack_file))) == 0
            assert typing_calls == [("owner123", "ctx555", True)], typing_calls
            # 非 ack 的 note 不亮 typing
            typing_calls.clear()
            assert b.cmd_note(argparse.Namespace(
                message_id="555", key="delay", text_file=str(ack_file))) == 0
            assert typing_calls == [], "delay 不应亮 typing"
            # send-once 成功后熄灭；typing 返回 False 也不影响退出码
            b.set_status(555, "pending-send")
            assert b.cmd_send_once(argparse.Namespace(message_id="555")) == 0
            assert typing_calls == [("owner123", "ctx555", False)], typing_calls
        finally:
            Bridge.send_message = real_send
            Bridge.send_typing = real_typing


def test_burst_merge_support():
    """⑪ 连发合并支撑：pending 列同发送人 received、merged 状态与备注。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        for mid, sender, ts in ((101, "owner123", 1000), (102, "owner123", 2000),
                                (103, "owner123", 3000), (104, "other999", 2500)):
            (Path(tmp) / "inbox" / f"{mid}.json").write_text(json.dumps({
                "message_id": mid, "from_user_id": sender,
                "context_token": f"ctx{mid}", "create_time_ms": ts,
                "type": "text", "text": f"消息{mid}",
            }, ensure_ascii=False), encoding="utf-8")
            b.set_status(mid, "received")
        # pending：按时间升序；按发送人过滤
        pending = b.list_pending()
        assert [p["message_id"] for p in pending] == ["101", "102", "104", "103"], pending
        owner_pending = b.list_pending("owner123")
        assert [p["message_id"] for p in owner_pending] == ["101", "102", "103"]
        assert owner_pending[0]["type"] == "text"
        # merged 状态 + 备注（mark --note）：被合并的消息不再出现在 pending
        assert b.cmd_mark(argparse.Namespace(
            message_id="101", state="merged", note="已合并到 103")) == 0
        assert b.cmd_mark(argparse.Namespace(
            message_id="102", state="merged", note="已合并到 103")) == 0
        status = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
        assert status["101"]["status"] == "merged"
        assert status["101"]["note"] == "已合并到 103"
        assert [p["message_id"] for p in b.list_pending("owner123")] == ["103"], \
            "merged 后只剩最新一条"
        # 非法状态仍被拒绝
        try:
            b.set_status(103, "done")
        except ValueError:
            pass
        else:
            raise AssertionError("非法状态应被拒绝")


TESTS = [
    ("① 二维码编解码往返与载荷判定", test_qr_roundtrip),
    ("② 消息过滤与非文字固定回复", test_filtering),
    ("③ redact 脱敏", test_redact),
    ("④ 单实例锁", test_single_instance_lock),
    ("⑤ 状态推进", test_state_machine),
    ("⑥ send-once 只发一次与无 ret 记 sent", test_send_once),
    ("⑦ note 附加消息只发一次与不碰主状态", test_note),
    ("⑧ 只重试 407，连接中断与服务端错误不重试", test_http_json_proxy_retry),
    ("⑨ 正文守卫拒发空正文与 -- 开头", test_body_guard),
    ("⑩ 正在输入 payload/缓存/静默降级与挂接点", test_typing),
    ("⑪ 连发合并支撑 pending 与 merged 状态", test_burst_merge_support),
]


def main():
    failed = 0
    for name, fn in TESTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - 自测要报出任何失败
            failed += 1
            print(f"FAIL {name}：{exc}")
        else:
            print(f"PASS {name}")
    print(f"共 {len(TESTS)} 项，通过 {len(TESTS) - failed} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
