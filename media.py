#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信桥接媒体模块：图片与文件的收发（AES + CDN）。

只做媒体编解码与上传下载，不碰消息状态机与登录流程；
状态机、去重、防重发仍在 bridge.py。

协议依据（已核对腾讯 openclaw-weixin 官方源码，commit ffc7ae16）：
- src/messaging/send.ts：图片 item 为 {type:2, image_item:{media, mid_size}}，
  文件 item 为 {type:4, file_item:{media, file_name, len}}；说明文字与媒体
  拆成独立请求，各用独立 client_id，文字先发。
- src/cdn/upload.ts：filekey 与 aeskey 各为随机 16 字节；getUploadUrl 请求体
  带 filekey/media_type/to_user_id/rawsize/rawfilemd5/filesize（填充后大小）
  /no_need_thumb:true/aeskey（十六进制）；要求返回 upload_full_url 或
  upload_param，缺一不可。
- src/cdn/cdn-upload.ts：密文 POST 到 CDN，最多重试 3 次；4xx 立即失败；
  成功判定是 HTTP 200 且响应头 x-encrypted-param 非空（即下载参数）。
- src/cdn/cdn-url.ts：上传 URL 为
  {cdn}/upload?encrypted_query_param=<upload_param>&filekey=<filekey>，
  下载 URL 为 {cdn}/download?encrypted_query_param=<param>。
  注意：查询参数名是 encrypted_query_param（带 ed），媒体字段名是
  encrypt_query_param（不带 ed），两者不一样，别写混。
- src/media/media-download.ts：图片优先 image_item.aeskey（十六进制），
  其次 media.aes_key；无密钥的图片按明文处理；文件缺 media.aes_key 直接跳过；
  单个媒体上限 100MB。

密钥编码（两处容易混，写清楚）：
- aeskey 本身是 16 字节随机数；getUploadUrl 里传十六进制字符串；
  sendMessage 的 media.aes_key 传“十六进制字符串再 base64”（即对 32 个
  十六进制字符做 base64，不是直接对 16 字节做 base64）。
- 入站解密时 aes_key 可能是 base64(16 字节原文)，也可能是
  base64(32 位十六进制字符串)，两种都接受，见 decode_aes_key()。
"""

import base64
import binascii
import hashlib
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# 媒体类型（getUploadUrl 的 media_type 字段）
MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_FILE = 3
MEDIA_TYPE_VOICE = 4

# 消息项类型（item_list 里 item.type）
ITEM_TYPE_TEXT = 1
ITEM_TYPE_IMAGE = 2
ITEM_TYPE_VOICE = 3
ITEM_TYPE_FILE = 4
ITEM_TYPE_VIDEO = 5

# 默认 CDN 地址（协议文档“协议范围和传输方式”一节）
DEFAULT_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"

# 单个媒体上限 100MB（与官方客户端 WEIXIN_MEDIA_MAX_BYTES 一致）
MAX_MEDIA_BYTES = 100 * 1024 * 1024

# CDN 上传最多重试次数（官方 UPLOAD_MAX_RETRIES）
UPLOAD_MAX_RETRIES = 3

# 上传 POST 与下载 GET 的超时（秒）
UPLOAD_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 60


class MediaError(Exception):
    """媒体处理错误：文案直接展示给用户，须用中文、说清原因和下一步。"""


# ---------------------------------------------------------------------------
# AES-128-ECB + PKCS#7
# ---------------------------------------------------------------------------

def _aes_cipher(key):
    """建一个 AES-128-ECB 加密器（pycryptodome）。"""
    try:
        from Crypto.Cipher import AES
    except ImportError as exc:
        raise MediaError(
            "缺少依赖 pycryptodome：请先在 .venv 里运行 pip install pycryptodome"
        ) from exc
    if len(key) != 16:
        raise MediaError(f"AES 密钥必须是 16 字节，实际 {len(key)} 字节")
    return AES.new(key, AES.MODE_ECB)


def aes_padded_size(size):
    """PKCS#7 填充后的密文大小：总比明文多一整块（哪怕明文恰好是 16 的倍数）。"""
    return (size // 16 + 1) * 16


def aes_encrypt_ecb(plaintext, key):
    """AES-128-ECB 加密（PKCS#7 填充），返回密文。"""
    from Crypto.Util.Padding import pad
    return _aes_cipher(key).encrypt(pad(plaintext, 16))


def aes_decrypt_ecb(ciphertext, key):
    """AES-128-ECB 解密（去掉 PKCS#7 填充），返回明文。

    长度不是 16 的倍数或填充不对都报错（不静默返回错误内容）。
    """
    if len(ciphertext) == 0 or len(ciphertext) % 16 != 0:
        raise MediaError(f"密文长度 {len(ciphertext)} 不是 16 的倍数，无法解密")
    from Crypto.Util.Padding import unpad
    try:
        return unpad(_aes_cipher(key).decrypt(ciphertext), 16)
    except ValueError as exc:
        raise MediaError("解密失败：填充校验不对（密钥可能不对）") from exc


def decode_aes_key(value):
    """把各种形式的 AES 密钥值解成 16 字节原文。

    接受三种形式：
    1. 32 位十六进制字符串（如 image_item.aeskey）；
    2. base64(16 字节原文)；
    3. base64(32 位十六进制字符串)。
    其他形式直接报错，不猜。
    """
    if not value or not isinstance(value, str):
        raise MediaError("缺少 AES 密钥")
    text = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{32}", text):
        return bytes.fromhex(text)
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MediaError("AES 密钥既不是十六进制也不是合法 base64") from exc
    if len(raw) == 16:
        return raw
    if len(raw) == 32:
        try:
            return bytes.fromhex(raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            pass
    raise MediaError(f"AES 密钥解码后是 {len(raw)} 字节，不是 16 字节也不是 32 位十六进制")


# ---------------------------------------------------------------------------
# CDN 地址与媒体引用
# ---------------------------------------------------------------------------

def build_cdn_upload_url(cdn_base, upload_param, filekey):
    """拼 CDN 上传地址（无 upload_full_url 时的回退公式）。

    {cdn}/upload?encrypted_query_param=<upload_param>&filekey=<filekey>
    查询参数名带 ed：encrypted_query_param。
    """
    query = urllib.parse.urlencode(
        {"encrypted_query_param": upload_param, "filekey": filekey}
    )
    return cdn_base.rstrip("/") + "/upload?" + query


def build_cdn_download_url(cdn_base, encrypt_query_param):
    """拼 CDN 下载地址。

    {cdn}/download?encrypted_query_param=<param>
    注意参数名带 ed，与媒体字段名 encrypt_query_param（不带 ed）不同。
    """
    query = urllib.parse.urlencode({"encrypted_query_param": encrypt_query_param})
    return cdn_base.rstrip("/") + "/download?" + query


def build_media_ref(download_param, aeskey_hex):
    """拼 sendMessage 里媒体项的 media 引用。

    aes_key 是“十六进制字符串再 base64”，不是对 16 字节原文做 base64。
    """
    return {
        "encrypt_query_param": download_param,
        "aes_key": base64.b64encode(aeskey_hex.encode("ascii")).decode("ascii"),
        "encrypt_type": 1,
    }


def build_image_item(uploaded):
    """拼图片消息项（官方 sendImageMessageWeixin 的结构）。

    uploaded 是 upload_file() 返回的字典。
    mid_size 是密文大小（不是明文大小）。
    """
    return {
        "type": ITEM_TYPE_IMAGE,
        "image_item": {
            "media": build_media_ref(uploaded["download_param"], uploaded["aeskey_hex"]),
            "mid_size": uploaded["file_size_ciphertext"],
        },
    }


def build_file_item(uploaded, file_name):
    """拼文件消息项（官方 sendFileMessageWeixin 的结构）。

    len 是明文大小，转成十进制字符串。
    """
    return {
        "type": ITEM_TYPE_FILE,
        "file_item": {
            "media": build_media_ref(uploaded["download_param"], uploaded["aeskey_hex"]),
            "file_name": file_name,
            "len": str(uploaded["file_size"]),
        },
    }


# ---------------------------------------------------------------------------
# 文件名与类型判定
# ---------------------------------------------------------------------------

def safe_filename(name):
    """把文件名清成可安全落盘的形式：去掉路径分隔符与控制字符。

    空结果返回 "file"，防止目录穿越与空文件名。
    """
    name = (name or "").strip().replace("/", "_").replace("\\", "_").replace("\x00", "")
    name = "".join(ch for ch in name if ord(ch) >= 32)
    name = name.strip(".")
    return name or "file"


def guess_image_ext(data):
    """按文件头魔数猜图片扩展名，猜不出返回 None。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"BM"):
        return "bmp"
    return None


def is_image_bytes(data):
    """按文件头魔数判断字节串是不是常见图片。"""
    return guess_image_ext(data) is not None


def detect_kind(path):
    """按文件头判断一个本地文件是图片还是普通文件（发媒体时 --type 未给则用它）。"""
    try:
        with open(path, "rb") as fp:
            head = fp.read(16)
    except OSError as exc:
        raise MediaError(f"读文件失败：{exc}") from exc
    return "image" if is_image_bytes(head) else "file"


def guess_mime(filename):
    """按文件名猜 MIME 类型，猜不出返回通用二进制。"""
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


# ---------------------------------------------------------------------------
# 上传参数准备
# ---------------------------------------------------------------------------

def prepare_upload(plaintext, media_type, to_user_id):
    """准备上传参数：生成 filekey/aeskey，算 MD5 与填充后大小。

    返回字典，含 getUploadUrl 请求体（不含 base_info，调用方补）与
    filekey/aeskey（字节与十六进制两种形式）。
    """
    aeskey = os.urandom(16)
    filekey = uuid.uuid4().hex  # 32 位十六进制，与官方 randomBytes(16) 等价
    rawsize = len(plaintext)
    return {
        "filekey": filekey,
        "aeskey": aeskey,
        "aeskey_hex": aeskey.hex(),
        "file_size": rawsize,
        "file_size_ciphertext": aes_padded_size(rawsize),
        "body": {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": hashlib.md5(plaintext).hexdigest(),
            "filesize": aes_padded_size(rawsize),
            "no_need_thumb": True,
            "aeskey": aeskey.hex(),
        },
    }


# ---------------------------------------------------------------------------
# 媒体客户端：上传 / 下载（HTTP 方法可注入 mock，自测用）
# ---------------------------------------------------------------------------

class MediaClient:
    """媒体上传下载客户端。

    api_base/token 来自登录凭证；log 是 bridge 的 log 方法（可空）；
    redact 是脱敏登记回调（把 aeskey 与下载参数登记进脱敏器，可空）。
    _post_json/_post_bytes/_get_bytes 是独立方法，自测可直接替换成 mock。
    """

    def __init__(self, api_base, token, cdn_base=DEFAULT_CDN_BASE, log=None, redact=None):
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.cdn_base = cdn_base.rstrip("/")
        self._log = log or (lambda level, msg: None)
        self._redact = redact or (lambda value: None)

    # -- 底层 HTTP（自测可替换） --------------------------------------------

    def _post_json(self, path, body, timeout=15):
        """调一个鉴权 JSON 接口（这里只用来调 getUploadUrl）。"""
        # 延迟导入：bridge.py 在顶层 import media，顶层反向 import 会循环
        from bridge import BridgeError, _auth_headers, _base_info, _http_json
        full_body = dict(body)
        full_body["base_info"] = _base_info()
        try:
            return _http_json(
                "POST", self.api_base + path, full_body,
                _auth_headers(self.token), timeout=timeout,
            )
        except BridgeError as exc:
            raise MediaError(str(exc)) from exc

    def _post_bytes(self, url, data, timeout=UPLOAD_TIMEOUT):
        """POST 二进制到 CDN，返回 (http_status, x-encrypted-param 响应头值)。"""
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.headers.get("x-encrypted-param") or ""
        except urllib.error.HTTPError as exc:
            return exc.code, ""
        except (urllib.error.URLError, TimeoutError) as exc:
            raise MediaError(f"CDN 上传网络错误：{exc}") from exc

    def _get_bytes(self, url, timeout=DOWNLOAD_TIMEOUT):
        """GET 下载二进制，分块读，超过 100MB 上限直接报错。"""
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise MediaError(f"CDN 下载失败：HTTP {resp.status}")
                chunks = []
                total = 0
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_MEDIA_BYTES:
                        raise MediaError("媒体超过 100MB 上限，已停止下载")
                    chunks.append(chunk)
                return b"".join(chunks)
        except urllib.error.HTTPError as exc:
            raise MediaError(f"CDN 下载失败：HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise MediaError(f"CDN 下载网络错误：{exc}") from exc

    # -- 上传 ---------------------------------------------------------------

    def upload_file(self, path, to_user_id, media_type):
        """上传一个本地文件到微信 CDN，返回上传信息字典。

        流程：读文件 → 准备参数 → getUploadUrl → 加密 → POST 密文 → 取下载参数。
        返回：{filekey, download_param, aeskey_hex, file_size, file_size_ciphertext}。
        任一步失败都抛 MediaError（中文原因）。
        """
        path = Path(path)
        try:
            plaintext = path.read_bytes()
        except OSError as exc:
            raise MediaError(f"读文件失败：{exc}") from exc
        if len(plaintext) == 0:
            raise MediaError("文件为空，无法发送")
        if len(plaintext) > MAX_MEDIA_BYTES:
            raise MediaError("文件超过 100MB 上限，无法发送")

        prep = prepare_upload(plaintext, media_type, to_user_id)
        resp = self._post_json("/ilink/bot/getuploadurl", prep["body"])
        upload_full_url = (resp.get("upload_full_url") or "").strip()
        upload_param = resp.get("upload_param") or ""
        if not upload_full_url and not upload_param:
            raise MediaError("getUploadUrl 未返回上传地址（缺 upload_full_url 与 upload_param）")

        ciphertext = aes_encrypt_ecb(plaintext, prep["aeskey"])
        if len(ciphertext) != prep["file_size_ciphertext"]:
            raise MediaError("密文大小与上报大小不一致，已停止上传")

        if upload_full_url:
            cdn_url = upload_full_url
        else:
            cdn_url = build_cdn_upload_url(self.cdn_base, upload_param, prep["filekey"])

        # 密钥与下载参数登记进脱敏器：日志里出现时会被抹掉
        self._redact(prep["aeskey_hex"])
        self._redact(upload_param)

        last_error = ""
        for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
            status, download_param = self._post_bytes(cdn_url, ciphertext)
            if 400 <= status < 500:
                # 4xx 是客户端错误，重试没用，立即失败
                raise MediaError(f"CDN 上传被拒绝：HTTP {status}")
            if status == 200 and download_param:
                self._redact(download_param)
                self._log("info", f"媒体已上传 CDN（{len(plaintext)} 字节明文，第 {attempt} 次尝试）")
                return {
                    "filekey": prep["filekey"],
                    "download_param": download_param,
                    "aeskey_hex": prep["aeskey_hex"],
                    "file_size": prep["file_size"],
                    "file_size_ciphertext": prep["file_size_ciphertext"],
                }
            last_error = f"HTTP {status}" if status != 200 else "响应缺 x-encrypted-param"
            self._log("warning", f"CDN 上传第 {attempt} 次失败（{last_error}），准备重试")
            if attempt < UPLOAD_MAX_RETRIES:
                time.sleep(1)
        raise MediaError(f"CDN 上传失败（{UPLOAD_MAX_RETRIES} 次都失败：{last_error}）")

    # -- 下载 ---------------------------------------------------------------

    def download_item_media(self, item, kind, dest_dir, index=0):
        """下载一条入站消息里的一个媒体项，解密后落盘。

        kind 是 "image" 或 "file"；dest_dir 是保存目录（不存在会创建）；
        index 是同条消息里第几个同类媒体（用来给图片编序号文件名）。
        返回 {kind, file_name, path, size, mime}；path 是落盘后的绝对路径字符串。
        缺密钥（文件）或下载解密失败都抛 MediaError。
        """
        if kind == "image":
            sub = item.get("image_item") or {}
            aeskey_hex = sub.get("aeskey") or ""
        elif kind == "file":
            sub = item.get("file_item") or {}
            aeskey_hex = ""
        else:
            raise MediaError(f"不支持下载的媒体类型：{kind}")
        media = sub.get("media") or {}
        encrypt_param = media.get("encrypt_query_param") or ""
        full_url = media.get("full_url") or ""
        if not encrypt_param and not full_url:
            raise MediaError("媒体项没有下载地址（缺 encrypt_query_param 与 full_url）")

        # 密钥：图片优先 image_item.aeskey，其次 media.aes_key；
        # 文件必须有 media.aes_key，缺了按官方做法跳过（这里报清楚原因）。
        aes_key_value = aeskey_hex or media.get("aes_key") or ""
        key = None
        if aes_key_value:
            key = decode_aes_key(aes_key_value)
            self._redact(aes_key_value)
        elif kind == "file":
            raise MediaError("文件没有解密密钥（缺 media.aes_key），无法下载")
        # 图片无密钥时按官方做法当明文处理（不报错）

        url = full_url or build_cdn_download_url(self.cdn_base, encrypt_param)
        self._redact(encrypt_param)
        raw = self._get_bytes(url)
        plaintext = aes_decrypt_ecb(raw, key) if key is not None else raw

        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if kind == "file":
            file_name = safe_filename(sub.get("file_name") or "file")
        else:
            ext = guess_image_ext(plaintext) or "jpg"
            file_name = f"image_{index}.{ext}"
        dest_path = dest_dir / file_name
        # 同名文件加序号后缀，不覆盖已有文件
        if dest_path.exists():
            stem, suffix = dest_path.stem, dest_path.suffix
            n = 1
            while (dest_dir / f"{stem}_{n}{suffix}").exists():
                n += 1
            dest_path = dest_dir / f"{stem}_{n}{suffix}"
            file_name = dest_path.name
        dest_path.write_bytes(plaintext)
        return {
            "kind": kind,
            "file_name": file_name,
            "path": str(dest_path),
            "size": len(plaintext),
            "mime": guess_mime(file_name),
        }
