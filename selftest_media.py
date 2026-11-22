#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桥接媒体自测（图片与文件收发；不调真实接口、不往微信真发消息）。

测试项：
① AES-128-ECB 加密往返 + 填充边界 + 密钥三种解码形式 + 坏填充报错
② 上传参数构造（filekey/MD5/填充后大小/请求体字段）+ 文件名与类型判定
③ CDN 上传/下载 URL 构造公式（参数名 encrypted_query_param 带 ed）
④ CDN 上传 POST：4xx 立即失败、5xx 重试 3 次、缺下载参数重试后报错、
   成功返回下载参数；getUploadUrl 缺两个地址报错
⑤ 发送体结构：图片/文件 item 字段、说明文字先发、各自独立 client_id、
   单请求单 item；说明文字失败时不发媒体
⑥ 接收下载解密往返：full_url 优先、构造 URL、两种密钥形式、
   无密钥图片按明文、文件缺密钥报错、超 100MB 拒绝
⑦ process_message 集成：图片/文件进 inbox 记 received、图文混合、
   下载失败回“再发一次”、语音仍固定回复
⑧ send-once 媒体防重发：顺序/并发都只发一次、清单路径穿越被拒绝、
   失败留 pending-send、无清单时仍走文本

用法：.venv/bin/python selftest_media.py
全部通过退出码 0，任一项失败退出码 1。
"""

import argparse
import base64
import hashlib
import http.server
import json
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge
import media
from bridge import Bridge, BridgeError
from media import MediaClient, MediaError


def _make_bridge(tmp_path):
    """在临时目录里建一个 Bridge 实例（不碰真实 state/）。"""
    return Bridge(tmp_path)


def _make_client():
    """建一个 MediaClient（不连真实服务，HTTP 方法由各测试替换）。"""
    return MediaClient("https://ilinkai.weixin.qq.com", "fake-token")


def test_aes_roundtrip():
    """① AES 加密往返 + 填充边界 + 密钥解码 + 坏填充报错。"""
    key = bytes(range(16))
    for size in (0, 1, 15, 16, 17, 100, 1024):
        plain = bytes((i * 7) % 256 for i in range(size))
        cipher = media.aes_encrypt_ecb(plain, key)
        assert len(cipher) == media.aes_padded_size(size), f"密文大小不对：{size}"
        assert media.aes_decrypt_ecb(cipher, key) == plain, f"往返不一致：{size}"
    # 填充后大小：16 的倍数也要多一整块
    assert media.aes_padded_size(0) == 16
    assert media.aes_padded_size(16) == 32
    assert media.aes_padded_size(17) == 32

    # 密钥三种形式都解成同一把 16 字节密钥
    hex_key = key.hex()
    assert media.decode_aes_key(hex_key) == key, "十六进制形式"
    assert media.decode_aes_key(base64.b64encode(key).decode()) == key, "base64 原文"
    assert media.decode_aes_key(
        base64.b64encode(hex_key.encode()).decode()) == key, "base64 十六进制串"
    # 坏密钥：长度不对、不是 base64
    for bad in ("", "zzzz", "00112233", base64.b64encode(b"short").decode()):
        try:
            media.decode_aes_key(bad)
        except MediaError:
            pass
        else:
            raise AssertionError(f"坏密钥应报错：{bad!r}")
    # 坏密文：长度不是 16 的倍数、填充不对
    try:
        media.aes_decrypt_ecb(b"12345", key)
    except MediaError:
        pass
    else:
        raise AssertionError("长度不对的密文应报错")
    bad_cipher = media.aes_encrypt_ecb(b"hello", key)
    bad_cipher = bad_cipher[:-1] + bytes([bad_cipher[-1] ^ 0xFF])
    try:
        media.aes_decrypt_ecb(bad_cipher, key)
    except MediaError:
        pass
    else:
        raise AssertionError("填充不对的密文应报错")


def test_prepare_upload_and_helpers():
    """② 上传参数构造 + 文件名与类型判定。"""
    plain = b"hello wechat media"
    prep = media.prepare_upload(plain, media.MEDIA_TYPE_FILE, "owner123")
    body = prep["body"]
    assert len(prep["filekey"]) == 32 and all(
        c in "0123456789abcdef" for c in prep["filekey"]), "filekey 应为 32 位十六进制"
    assert prep["aeskey_hex"] == prep["aeskey"].hex()
    assert body["media_type"] == 3 and body["to_user_id"] == "owner123"
    assert body["rawsize"] == len(plain)
    assert body["rawfilemd5"] == hashlib.md5(plain).hexdigest()
    assert body["filesize"] == media.aes_padded_size(len(plain))
    assert body["no_need_thumb"] is True, "应固定 no_need_thumb: true"
    assert body["aeskey"] == prep["aeskey_hex"]
    assert prep["file_size"] == len(plain)
    assert prep["file_size_ciphertext"] == media.aes_padded_size(len(plain))
    # 两次准备的 filekey/aeskey 不一样（随机）
    prep2 = media.prepare_upload(plain, media.MEDIA_TYPE_FILE, "owner123")
    assert prep2["filekey"] != prep["filekey"] and prep2["aeskey"] != prep["aeskey"]

    # 文件名清理：目录穿越与控制字符被去掉（开头的点也被剥掉）
    assert media.safe_filename("../../etc/passwd") == "_.._etc_passwd"
    assert media.safe_filename("a/b\\c.txt") == "a_b_c.txt"
    assert media.safe_filename("") == "file"
    assert media.safe_filename("报告 2026.xlsx") == "报告 2026.xlsx"
    # 图片判定：魔数
    assert media.is_image_bytes(b"\x89PNG\r\n\x1a\nxxxx")
    assert media.is_image_bytes(b"\xff\xd8\xff\xe0xxxx")
    assert media.guess_image_ext(b"RIFF\x00\x00\x00\x00WEBP") == "webp"
    assert not media.is_image_bytes(b"hello, this is a text file")
    assert media.guess_image_ext(b"hello") is None
    # detect_kind：按文件头判定
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "a.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nxxxx")
        assert media.detect_kind(png) == "image"
        txt = Path(tmp) / "a.txt"
        txt.write_bytes(b"plain text")
        assert media.detect_kind(txt) == "file"


def test_cdn_url_builders():
    """③ CDN 上传/下载 URL 构造公式。"""
    base = "https://novac2c.cdn.weixin.qq.com/c2c"
    up = media.build_cdn_upload_url(base, "p&ram=1", "filekey123")
    assert up.startswith(base + "/upload?"), up
    query = urllib.parse.parse_qs(urllib.parse.urlparse(up).query)
    # 参数名带 ed：encrypted_query_param（不是 encrypt_query_param）
    assert query["encrypted_query_param"] == ["p&ram=1"], query
    assert query["filekey"] == ["filekey123"], query
    assert "encrypt_query_param" not in up.replace("encrypted_query_param", ""), \
        "上传 URL 不应出现不带 ed 的参数名"

    down = media.build_cdn_download_url(base, "abc/def+ghi")
    assert down.startswith(base + "/download?"), down
    query = urllib.parse.parse_qs(urllib.parse.urlparse(down).query)
    assert query["encrypted_query_param"] == ["abc/def+ghi"], query

    # 媒体引用：aes_key 是“十六进制字符串再 base64”
    ref = media.build_media_ref("dl-param", "00112233445566778899aabbccddeeff")
    assert ref["encrypt_query_param"] == "dl-param"
    assert ref["encrypt_type"] == 1
    assert ref["aes_key"] == base64.b64encode(
        b"00112233445566778899aabbccddeeff").decode()
    assert base64.b64decode(ref["aes_key"]).decode() == \
        "00112233445566778899aabbccddeeff"


def test_cdn_upload_retries():
    """④ CDN 上传 POST 重试行为（mock HTTP，不真发）。"""
    media.time.sleep = lambda s: None  # 重试间隔清零，自测快一点
    try:
        plain = b"fake file content for upload test"

        def run_case(post_json_resp, post_bytes_script):
            """跑一次 upload_file，返回（结果或异常，post_json 请求体，post_bytes 调用）。"""
            with tempfile.TemporaryDirectory() as tmp:
                fpath = Path(tmp) / "report.pdf"
                fpath.write_bytes(plain)
                client = _make_client()
                bodies = []
                calls = []

                def fake_post_json(path, body, timeout=15):
                    bodies.append((path, body))
                    return dict(post_json_resp)

                def fake_post_bytes(url, data, timeout=120):
                    calls.append((url, data))
                    return post_bytes_script.pop(0)

                client._post_json = fake_post_json
                client._post_bytes = fake_post_bytes
                try:
                    result = client.upload_file(fpath, "owner123", media.MEDIA_TYPE_FILE)
                except MediaError as exc:
                    return ("error", str(exc), bodies, calls)
                return ("ok", result, bodies, calls)

        # 成功：upload_full_url 优先；请求体字段齐全；POST 的是加密后内容
        status, result, bodies, calls = run_case(
            {"upload_full_url": "https://cdn.example/upload-target"},
            [(200, "download-param-xyz")])
        assert status == "ok", result
        assert result["download_param"] == "download-param-xyz"
        assert result["file_size"] == len(plain)
        assert result["file_size_ciphertext"] == media.aes_padded_size(len(plain))
        assert bodies[0][0] == "/ilink/bot/getuploadurl"
        assert bodies[0][1]["rawfilemd5"] == hashlib.md5(plain).hexdigest()
        assert bodies[0][1]["filesize"] == media.aes_padded_size(len(plain))
        assert len(calls) == 1 and calls[0][0] == "https://cdn.example/upload-target"
        key = media.decode_aes_key(result["aeskey_hex"])
        assert media.aes_decrypt_ecb(calls[0][1], key) == plain, "POST 的应是密文"

        # 回退：只有 upload_param 时按公式拼上传地址
        status, result, bodies, calls = run_case(
            {"upload_param": "up-param-1"}, [(200, "dl-2")])
        assert status == "ok", result
        url = calls[0][0]
        assert url.startswith("https://novac2c.cdn.weixin.qq.com/c2c/upload?"), url
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert query["encrypted_query_param"] == ["up-param-1"]
        assert query["filekey"] == [result["filekey"]]

        # getUploadUrl 两个地址都没有：直接报错，不 POST
        status, result, bodies, calls = run_case({}, [(200, "x")])
        assert status == "error" and len(calls) == 0, "无上传地址时不应 POST"

        # 4xx：立即失败，只调一次
        status, result, bodies, calls = run_case(
            {"upload_full_url": "https://cdn.example/u"},
            [(403, ""), (200, "x"), (200, "x")])
        assert status == "error" and "403" in result, result
        assert len(calls) == 1, "4xx 不应重试"

        # 5xx 两次后成功：共调 3 次
        status, result, bodies, calls = run_case(
            {"upload_full_url": "https://cdn.example/u"},
            [(500, ""), (502, ""), (200, "dl-3")])
        assert status == "ok" and len(calls) == 3, "5xx 应重试"

        # 200 但缺 x-encrypted-param：3 次都缺就报错
        status, result, bodies, calls = run_case(
            {"upload_full_url": "https://cdn.example/u"},
            [(200, ""), (200, ""), (200, "")])
        assert status == "error" and len(calls) == 3, "缺下载参数应重试 3 次后报错"
    finally:
        import time as _time
        media.time.sleep = _time.sleep


def test_send_body_structure():
    """⑤ 发送体结构：item 字段、说明文字先发、独立 client_id、单请求单 item。"""
    uploaded = {
        "filekey": "f" * 32,
        "download_param": "dl-param-1",
        "aeskey_hex": "00112233445566778899aabbccddeeff",
        "file_size": 1234,
        "file_size_ciphertext": 1248,
    }
    # 直接验 item 结构
    img = media.build_image_item(uploaded)
    assert img["type"] == 2
    assert img["image_item"]["media"]["encrypt_query_param"] == "dl-param-1"
    assert img["image_item"]["media"]["encrypt_type"] == 1
    assert img["image_item"]["media"]["aes_key"] == base64.b64encode(
        b"00112233445566778899aabbccddeeff").decode()
    assert img["image_item"]["mid_size"] == 1248, "mid_size 是密文大小"
    fitem = media.build_file_item(uploaded, "报告.xlsx")
    assert fitem["type"] == 4
    assert fitem["file_item"]["file_name"] == "报告.xlsx"
    assert fitem["file_item"]["len"] == "1234", "len 是明文大小的字符串"

    # 走 send_media 全流程（mock 上传 + 捕获 _post_send_message）
    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        posts = []

        def fake_post(msg, label):
            posts.append((msg, label))
            return True, True, ""

        real_post = Bridge._post_send_message
        Bridge._post_send_message = lambda self, msg, label: fake_post(msg, label)
        try:
            img_path = Path(tmp) / "pic.png"
            img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
            mock_upload = lambda path, to, kind: dict(uploaded)
            ok, confirmed, errmsg = b.send_media(
                "owner123", "ctx1", img_path, "image",
                caption="这是图片", uploader=mock_upload)
            assert ok and confirmed, errmsg
            assert len(posts) == 2, "带说明文字应发两个请求"
            first, second = posts[0][0], posts[1][0]
            assert first["item_list"][0]["type"] == 1, "第一个请求应是文本"
            assert first["item_list"][0]["text_item"]["text"] == "这是图片"
            assert second["item_list"][0]["type"] == 2, "第二个请求应是图片"
            assert len(first["item_list"]) == 1 and len(second["item_list"]) == 1, \
                "每个请求只带一个 item"
            assert first["client_id"] != second["client_id"], "client_id 必须各自独立"
            assert first["context_token"] == "ctx1" == second["context_token"]
            assert first["message_type"] == 2 and first["message_state"] == 2

            # 无说明文字：只发一个请求
            posts.clear()
            doc_path = Path(tmp) / "doc.pdf"
            doc_path.write_bytes(b"%PDF-fake")
            ok, _, _ = b.send_media("owner123", "ctx1", doc_path, "file",
                                    uploader=mock_upload)
            assert ok and len(posts) == 1
            assert posts[0][0]["item_list"][0]["type"] == 4
            assert posts[0][0]["item_list"][0]["file_item"]["file_name"] == "doc.pdf"

            # 说明文字发失败：不发媒体，uploader 不应被调
            upload_calls = []

            def counting_upload(path, to, kind):
                upload_calls.append(path)
                return dict(uploaded)

            real_send_message = Bridge.send_message
            Bridge.send_message = lambda self, to, ctx, text: (False, False, "网络错误")
            try:
                ok, _, errmsg = b.send_media(
                    "owner123", "ctx1", doc_path, "file",
                    caption="说明", uploader=counting_upload)
            finally:
                Bridge.send_message = real_send_message
            assert not ok and "说明文字" in errmsg
            assert len(upload_calls) == 0, "说明文字失败时不应上传媒体"

            # 文件不存在 / 为空 / 超大：直接失败，不调上传
            ok, _, errmsg = b.send_media("owner123", "ctx1", Path(tmp) / "nope.png",
                                         "image", uploader=mock_upload)
            assert not ok and "不存在" in errmsg
            empty = Path(tmp) / "empty.bin"
            empty.write_bytes(b"")
            ok, _, errmsg = b.send_media("owner123", "ctx1", empty, "file",
                                         uploader=mock_upload)
            assert not ok and "为空" in errmsg
            big = Path(tmp) / "big.bin"
            big.write_bytes(b"x" * 20)
            real_max = media.MAX_MEDIA_BYTES
            media.MAX_MEDIA_BYTES = 10
            try:
                ok, _, errmsg = b.send_media("owner123", "ctx1", big, "file",
                                             uploader=mock_upload)
            finally:
                media.MAX_MEDIA_BYTES = real_max
            assert not ok and "100MB" in errmsg
        finally:
            Bridge._post_send_message = real_post


def test_download_decrypt_roundtrip():
    """⑥ 接收下载解密往返（mock _get_bytes，不真联网）。"""
    key = bytes(range(16))
    plain_img = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"
    cipher_img = media.aes_encrypt_ecb(plain_img, key)

    with tempfile.TemporaryDirectory() as tmp:
        client = _make_client()
        urls = []

        def fake_get(url, timeout=60):
            urls.append(url)
            return cipher_img

        client._get_bytes = fake_get
        dest = Path(tmp) / "media"

        # 图片：hex aeskey + 构造 URL（无 full_url）
        item = {"image_item": {
            "aeskey": key.hex(),
            "media": {"encrypt_query_param": "enc-param-1"}}}
        info = client.download_item_media(item, "image", dest, 0)
        assert info["kind"] == "image" and info["file_name"] == "image_0.png"
        assert Path(info["path"]).read_bytes() == plain_img, "解密后应与明文一致"
        assert urls[0] == ("https://novac2c.cdn.weixin.qq.com/c2c/download"
                           "?encrypted_query_param=enc-param-1"), urls[0]

        # full_url 优先于构造 URL
        urls.clear()
        item2 = {"image_item": {
            "aeskey": key.hex(),
            "media": {"encrypt_query_param": "enc-param-2",
                      "full_url": "https://cdn.example/direct/file"}}}
        info2 = client.download_item_media(item2, "image", dest, 1)
        assert urls[0] == "https://cdn.example/direct/file"
        assert info2["file_name"] == "image_1.png"

        # media.aes_key 两种 base64 形式（无 image_item.aeskey 时）
        for aes_value in (base64.b64encode(key).decode(),
                          base64.b64encode(key.hex().encode()).decode()):
            item3 = {"image_item": {
                "media": {"encrypt_query_param": "enc-param-3",
                          "aes_key": aes_value}}}
            info3 = client.download_item_media(item3, "image", dest, 2)
            assert Path(info3["path"]).read_bytes() == plain_img, f"aes_key 形式：{aes_value}"

        # 图片无密钥：按明文处理
        client._get_bytes = lambda url, timeout=60: plain_img
        item4 = {"image_item": {"media": {"encrypt_query_param": "enc-param-4"}}}
        info4 = client.download_item_media(item4, "image", dest, 3)
        assert Path(info4["path"]).read_bytes() == plain_img

        # 文件：带文件名，缺密钥报错
        client._get_bytes = fake_get
        plain_doc = b"file content here"
        cipher_doc = media.aes_encrypt_ecb(plain_doc, key)
        client._get_bytes = lambda url, timeout=60: cipher_doc
        file_item = {"file_item": {
            "file_name": "季度报告.xlsx",
            "media": {"encrypt_query_param": "enc-param-5",
                      "aes_key": base64.b64encode(key).decode()}}}
        finfo = client.download_item_media(file_item, "file", dest, 0)
        assert finfo["file_name"] == "季度报告.xlsx"
        assert Path(finfo["path"]).read_bytes() == plain_doc
        assert finfo["mime"] in ("application/vnd.openxmlformats-officedocument"
                                 ".spreadsheetml.sheet",
                                 "application/octet-stream")
        # 文件缺密钥：报错
        no_key = {"file_item": {"file_name": "a.txt",
                                "media": {"encrypt_query_param": "enc-param-6"}}}
        try:
            client.download_item_media(no_key, "file", dest, 0)
        except MediaError as exc:
            assert "密钥" in str(exc)
        else:
            raise AssertionError("文件缺密钥应报错")
        # 媒体项无下载地址：报错
        no_url = {"image_item": {"aeskey": key.hex(), "media": {}}}
        try:
            client.download_item_media(no_url, "image", dest, 0)
        except MediaError as exc:
            assert "下载地址" in str(exc)
        else:
            raise AssertionError("无下载地址应报错")

    # 超 100MB 拒绝：本地 HTTP 服务 + 临时调小上限（不真下 100MB）
    served = b"y" * 64

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(served)))
            self.end_headers()
            self.wfile.write(served)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/file"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        client = _make_client()
        assert client._get_bytes(url) == served, "正常大小应能下载"
        real_max = media.MAX_MEDIA_BYTES
        media.MAX_MEDIA_BYTES = 10
        try:
            client._get_bytes(url)
        except MediaError as exc:
            assert "100MB" in str(exc)
        else:
            raise AssertionError("超上限应拒绝下载")
        finally:
            media.MAX_MEDIA_BYTES = real_max
    finally:
        server.shutdown()
        server.server_close()


def test_process_message_media():
    """⑦ process_message 集成：图片/文件进 inbox、混合、下载失败、语音固定回复。"""
    sent = []

    def mock_sender(to_user_id, context_token, text):
        sent.append((to_user_id, context_token, text))
        return True, True, ""

    def mock_downloader(item, kind, dest_dir, index):
        name = "image_0.png" if kind == "image" else "报表.xlsx"
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / name).write_bytes(b"fake")
        return {"kind": kind, "file_name": name, "path": str(dest / name),
                "size": 4, "mime": "image/png" if kind == "image" else
                "application/octet-stream"}

    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        session = {"ilink_user_id": "owner123", "baseurl": "https://x", "bot_token": "t"}
        processed = set()
        base = {"from_user_id": "owner123", "message_type": 1, "group_id": "",
                "context_token": "ctx1", "create_time_ms": 1700000000000}

        # 单张图片：进 inbox，type image，状态 received，不发固定回复
        r = b.process_message(
            {**base, "message_id": 11, "item_list": [{"type": 2, "image_item": {}}]},
            session, processed, sender=mock_sender, media_downloader=mock_downloader)
        assert r == "received", r
        rec = json.loads((Path(tmp) / "inbox" / "11.json").read_text(encoding="utf-8"))
        assert rec["type"] == "image" and len(rec["media"]) == 1
        assert rec["media"][0]["file_name"] == "image_0.png"
        assert rec["media"][0]["path"] == "media/11/image_0.png"
        assert len(sent) == 0, "图片不应触发固定回复"

        # 单个文件：type file
        r = b.process_message(
            {**base, "message_id": 12,
             "item_list": [{"type": 4, "file_item": {"file_name": "报表.xlsx"}}]},
            session, processed, sender=mock_sender, media_downloader=mock_downloader)
        assert r == "received", r
        rec = json.loads((Path(tmp) / "inbox" / "12.json").read_text(encoding="utf-8"))
        assert rec["type"] == "file" and rec["media"][0]["file_name"] == "报表.xlsx"

        # 图文混合：type mixed，文本与媒体都在
        r = b.process_message(
            {**base, "message_id": 13, "item_list": [
                {"type": 1, "text_item": {"text": "看这张图"}},
                {"type": 2, "image_item": {}}]},
            session, processed, sender=mock_sender, media_downloader=mock_downloader)
        assert r == "received", r
        rec = json.loads((Path(tmp) / "inbox" / "13.json").read_text(encoding="utf-8"))
        assert rec["type"] == "mixed" and rec["text"] == "看这张图"
        assert len(rec["media"]) == 1

        # 媒体下载失败、无文本：回“请再发一次”，记 sent
        def failing_downloader(item, kind, dest_dir, index):
            raise MediaError("CDN 下载失败：HTTP 500")

        r = b.process_message(
            {**base, "message_id": 14, "item_list": [{"type": 2, "image_item": {}}]},
            session, processed, sender=mock_sender, media_downloader=failing_downloader)
        assert r == "replied:media-failed", r
        assert len(sent) == 1 and sent[0][2] == bridge.DOWNLOAD_FAILED_REPLY

        # 语音：仍走固定回复
        r = b.process_message(
            {**base, "message_id": 15, "item_list": [{"type": 3, "voice_item": {}}]},
            session, processed, sender=mock_sender, media_downloader=mock_downloader)
        assert r == "replied:non-text", r
        assert sent[-1][2] == bridge.NON_TEXT_REPLY

        # 视频：仍走固定回复
        r = b.process_message(
            {**base, "message_id": 16, "item_list": [{"type": 5, "video_item": {}}]},
            session, processed, sender=mock_sender, media_downloader=mock_downloader)
        assert r == "replied:non-text", r

        status = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
        assert status["11"]["status"] == "received"
        assert status["12"]["status"] == "received"
        assert status["13"]["status"] == "received"
        assert status["14"]["status"] == "sent"
        assert status["15"]["status"] == "sent"
        assert status["16"]["status"] == "sent"


def test_send_once_media():
    """⑧ send-once 媒体防重发：顺序/并发只发一次、清单穿越拒绝、失败留 pending-send。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _make_bridge(tmp)
        inbox_file = Path(tmp) / "inbox" / "888.json"
        inbox_file.write_text(json.dumps({
            "message_id": 888, "from_user_id": "owner123",
            "context_token": "ctx888", "type": "text", "text": "发我一份文件",
        }, ensure_ascii=False), encoding="utf-8")
        (Path(tmp) / "outbox" / "report.pdf").write_bytes(b"%PDF-fake-content")
        (Path(tmp) / "outbox" / "888.media.json").write_text(json.dumps({
            "type": "file", "file": "report.pdf", "caption": "你要的文件",
        }, ensure_ascii=False), encoding="utf-8")
        b.set_status(888, "pending-send")

        calls = []

        def mock_send_media(self, to, ctx, path, kind, caption="", uploader=None):
            calls.append((to, ctx, str(path), kind, caption))
            return True, False, "响应没有 ret 字段"

        real_send_media = Bridge.send_media
        Bridge.send_media = mock_send_media
        try:
            args = argparse.Namespace(message_id="888")
            assert b.cmd_send_once(args) == 0, "第一次应成功"
            b2 = _make_bridge(tmp)
            assert b2.cmd_send_once(args) == 0, "第二次应跳过"
        finally:
            Bridge.send_media = real_send_media
        assert len(calls) == 1, f"应恰好发送一次，实际 {len(calls)} 次"
        assert calls[0][0] == "owner123" and calls[0][3] == "file"
        assert calls[0][2].endswith("report.pdf") and calls[0][4] == "你要的文件"
        status = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
        assert status["888"]["status"] == "sent", "无 ret 但 HTTP 成功应记 sent"

        # 并发调两次，也只发一次
        b.set_status(888, "pending-send")
        slow_calls = []

        def slow_send_media(self, to, ctx, path, kind, caption="", uploader=None):
            import time as _time
            _time.sleep(0.3)
            slow_calls.append(path)
            return True, True, ""

        Bridge.send_media = slow_send_media
        try:
            barrier = threading.Barrier(2)
            results = []

            def run_once(inst):
                barrier.wait(timeout=5)
                results.append(inst.cmd_send_once(argparse.Namespace(message_id="888")))

            t1 = threading.Thread(target=run_once, args=(b,))
            t2 = threading.Thread(target=run_once, args=(b2,))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
        finally:
            Bridge.send_media = real_send_media
        assert not t1.is_alive() and not t2.is_alive(), "并发调用不应卡死"
        assert sorted(results) == [0, 0]
        assert len(slow_calls) == 1, f"并发应恰好发送一次，实际 {len(slow_calls)} 次"

        # 清单路径穿越：拒绝，不发送
        (Path(tmp) / "outbox" / "888.media.json").write_text(json.dumps({
            "type": "file", "file": "../state/session.json",
        }, ensure_ascii=False), encoding="utf-8")
        b.set_status(888, "pending-send")
        Bridge.send_media = mock_send_media
        try:
            try:
                b.cmd_send_once(argparse.Namespace(message_id="888"))
            except BridgeError as exc:
                assert "纯文件名" in str(exc)
            else:
                raise AssertionError("路径穿越清单应被拒绝")
        finally:
            Bridge.send_media = real_send_media

        # 发送失败：留在 pending-send，可重试
        (Path(tmp) / "outbox" / "888.media.json").write_text(json.dumps({
            "type": "file", "file": "report.pdf",
        }, ensure_ascii=False), encoding="utf-8")

        def fail_send_media(self, to, ctx, path, kind, caption="", uploader=None):
            return False, False, "网络错误"

        Bridge.send_media = fail_send_media
        try:
            b.set_status(888, "pending-send")
            assert b.cmd_send_once(argparse.Namespace(message_id="888")) == 1
        finally:
            Bridge.send_media = real_send_media
        status = json.loads((Path(tmp) / "state" / "status.json").read_text(encoding="utf-8"))
        assert status["888"]["status"] == "pending-send", "失败应留在 pending-send"

        # 无清单时仍走文本（回归：删掉清单 + 写 txt）
        (Path(tmp) / "outbox" / "888.media.json").unlink()
        (Path(tmp) / "outbox" / "888.txt").write_text("纯文本回复", encoding="utf-8")
        text_calls = []

        def mock_send_message(self, to, ctx, text):
            text_calls.append(text)
            return True, True, ""

        real_send_message = Bridge.send_message
        Bridge.send_message = mock_send_message
        try:
            b.set_status(888, "pending-send")
            assert b.cmd_send_once(argparse.Namespace(message_id="888")) == 0
        finally:
            Bridge.send_message = real_send_message
        assert text_calls == ["纯文本回复"], "无清单时应走文本发送"


TESTS = [
    ("① AES 加密往返与密钥解码", test_aes_roundtrip),
    ("② 上传参数构造与文件名判定", test_prepare_upload_and_helpers),
    ("③ CDN 上传/下载 URL 构造", test_cdn_url_builders),
    ("④ CDN 上传重试行为", test_cdn_upload_retries),
    ("⑤ 发送体结构与说明文字顺序", test_send_body_structure),
    ("⑥ 接收下载解密往返", test_download_decrypt_roundtrip),
    ("⑦ 收消息集成：图片/文件进 inbox", test_process_message_media),
    ("⑧ send-once 媒体防重发", test_send_once_media),
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
