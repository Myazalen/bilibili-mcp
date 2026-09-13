#!/usr/bin/env python3
"""
Bilibili MCP Server
B站数据采集 MCP 服务，支持 OpenClaw / Claude Code / Cursor / Cline

功能：搜索视频、抓取评论、获取字幕、弹幕、回复评论
首次运行需扫码登录B站，凭证自动保存复用
"""

import asyncio
import json
import base64
import os
import random
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from mcp.server.fastmcp import FastMCP

from bilibili_api import video, search, comment, Credential, dynamic, opus
from bilibili_api import hot, rank, user, session, favorite_list
from bilibili_api.video_uploader import (
    VideoUploader, VideoUploaderPage, VideoMeta, Lines,
)
from bilibili_api.utils.picture import Picture
from bilibili_api.utils.utils import get_api
from bilibili_api.utils import network as bili_network
from bilibili_api.utils.network import (
    Api, get_client, get_buvid, get_bili_ticket, request_settings, HEADERS,
)
from bilibili_api.login_v2 import QrCodeLogin
from bilibili_api.exceptions import (
    ApiException, CredentialNoSessdataException, NetworkException,
)

# ========== 初始化 ==========

CRED_FILE = Path(__file__).parent / "bili_credential.json"
QR_FILE = Path(__file__).parent / "qrcode_login.png"
LOGIN_SESSION_FILE = Path(__file__).parent / "bili_login_session.json"
# B站二维码有效期（秒）
QR_TTL = 180

mcp = FastMCP("bilibili-mcp")

LOGIN_HINT = (
    "未登录B站或登录态已失效：请先调用 bili_login 生成二维码，"
    "让用户用B站App扫码，再轮询 bili_login_check 直到 status=done，然后重试本操作。"
)


def _read_json(path: Path) -> dict:
    """读取 JSON 文件，文件不存在或损坏时返回空 dict"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _read_cred_file() -> dict:
    """读取凭证文件原始内容"""
    return _read_json(CRED_FILE)


def _cred_file_hint() -> str:
    """未登录时给出精确原因，方便 AI 判断要不要重新扫码"""
    if not CRED_FILE.exists():
        return "凭证文件不存在，从未登录过"
    data = _read_cred_file()
    if not data:
        return "凭证文件损坏（内容不是合法 JSON）"
    if not (data.get("sessdata") or "").strip():
        return "凭证文件存在但 sessdata 为空（登录没有真正完成），此状态一律按未登录处理"
    return "凭证里 sessdata 非空，但接口仍判定未登录，通常是登录态已过期"


def load_credential(require_login: bool = True) -> Credential | None:
    """
    从文件加载凭证。

    require_login=True（默认）时只有 sessdata 非空才算真的登录过，否则返回 None。
    这一点很关键：空 sessdata 的凭证会让字幕等需登录接口抛
    CredentialNoSessdataException，也会让 bili_login 误判"已登录"而不再出二维码。

    require_login=False 用于搜索、视频信息这类匿名也能用的接口，登录后自动带上登录态。
    """
    data = _read_cred_file()
    if not data:
        return None
    sessdata = (data.get("sessdata") or "").strip()
    if require_login and not sessdata:
        return None
    return Credential(
        sessdata=sessdata,
        bili_jct=(data.get("bili_jct") or "").strip(),
        buvid3=(data.get("buvid3") or "").strip(),
        buvid4=(data.get("buvid4") or "").strip(),
        dedeuserid=(data.get("dedeuserid") or "").strip(),
        ac_time_value=(data.get("ac_time_value") or "").strip(),
    )


def save_credential(cred: Credential) -> None:
    """
    保存凭证到文件。

    ac_time_value 也一并保存，bilibili-api 用它来刷新 cookie，不存的话登录态只能等过期。
    """
    CRED_FILE.write_text(
        json.dumps({
            "sessdata": cred.sessdata or "",
            "bili_jct": cred.bili_jct or "",
            "buvid3": cred.buvid3 or "",
            "buvid4": cred.buvid4 or "",
            "dedeuserid": cred.dedeuserid or "",
            "ac_time_value": cred.ac_time_value or "",
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def _cleanup_qrcode() -> None:
    """清理扫码登录生成的临时二维码图片"""
    if QR_FILE.exists():
        QR_FILE.unlink()


# B站返回的时间戳统一按北京时间格式化，避免服务器时区不是 UTC+8 时日期对不上
_CN_TZ = timezone(timedelta(hours=8))


def _format_ts(ts, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """把 Unix 时间戳格式化成北京时间字符串，无效值返回空串"""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts, _CN_TZ).strftime(fmt)
    except (OverflowError, OSError, ValueError):
        return ""


# ========== 登录会话（进程级缓存） ==========

_login_session: QrCodeLogin | None = None


def get_cred() -> Credential:
    """获取已登录凭证；未登录时抛出带操作指引的异常（需要登录态的接口用）"""
    cred = load_credential()
    if not cred:
        raise Exception(f"{LOGIN_HINT}（当前状态：{_cred_file_hint()}）")
    return cred


def get_cred_any() -> Credential:
    """获取凭证，未登录时返回匿名凭证（公开数据接口用）"""
    return load_credential(require_login=False) or Credential()


async def _credential_status(cred: Credential) -> tuple[bool, str, str]:
    """
    校验凭证是否真的可用。

    Returns:
        (是否有效, 用户名, 失败原因)
    """
    if not cred or not cred.has_sessdata():
        return False, "", "凭证中没有 sessdata"
    try:
        if not await cred.check_valid():
            return False, "", "接口返回未登录，登录态可能已过期"
    except Exception as e:
        return False, "", f"校验请求失败: {e}"

    username = ""
    try:
        info = await user.get_self_info(credential=cred)
        username = info.get("name", "") or info.get("uname", "")
    except Exception:
        pass
    return True, username, ""


# ========== 登录凭证兜底解析 ==========

_LOGIN_POLL_API = get_api("login")["qrcode"]["web"]["get_events"]


def _parse_login_cookies(url: str) -> dict:
    """
    从登录接口返回的 url 里解析 cookies。

    比 bilibili-api 自带的解析宽容：忽略大小写，兼容 &amp; 转义和 ; 分隔，
    值保持原样不做解码（SESSDATA 本身就是带着 %2C 的编码串）。
    """
    query = url.split("?", 1)[1] if "?" in url else url
    result = {}
    for chunk in re.split(r"[&;]", query.replace("&amp;", "&")):
        key, sep, value = chunk.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        if key in ("sessdata", "bili_jct", "dedeuserid", "buvid3", "buvid4"):
            result[key] = value.strip()
    return result


def _save_login_session(qr_key: str) -> None:
    """把扫码会话的 qrcode_key 落盘，进程重启后还能继续轮询"""
    if not qr_key:
        return
    LOGIN_SESSION_FILE.write_text(
        json.dumps({"qrcode_key": qr_key, "created_at": int(time.time())}),
        encoding="utf-8",
    )


def _load_login_session() -> str:
    """读取仍在有效期内的扫码会话，没有则返回空串"""
    data = _read_json(LOGIN_SESSION_FILE)
    qr_key = (data.get("qrcode_key") or "").strip()
    created_at = int(data.get("created_at") or 0)
    if not qr_key or time.time() - created_at > QR_TTL:
        return ""
    return qr_key


def _clear_login_session() -> None:
    if LOGIN_SESSION_FILE.exists():
        LOGIN_SESSION_FILE.unlink()


def _credential_from_login_payload(data: dict, cookies: dict) -> Credential | None:
    """从登录接口返回的 url 或 Set-Cookie 里取出凭证，取不到返回 None"""
    parsed = _parse_login_cookies(data.get("url") or "")

    def from_cookie(name: str) -> str:
        for k, v in (cookies or {}).items():
            if k.lower() == name.lower():
                return str(v)
        return ""

    sessdata = parsed.get("sessdata") or from_cookie("SESSDATA")
    if not sessdata:
        return None
    return Credential(
        sessdata=sessdata,
        bili_jct=parsed.get("bili_jct") or from_cookie("bili_jct"),
        buvid3=parsed.get("buvid3") or from_cookie("buvid3"),
        buvid4=parsed.get("buvid4") or from_cookie("buvid4"),
        dedeuserid=parsed.get("dedeuserid") or from_cookie("DedeUserID"),
        ac_time_value=data.get("refresh_token") or from_cookie("ac_time_value"),
    )


async def _poll_login_state(qr_key: str) -> tuple[str, Credential | None]:
    """
    直接轮询扫码登录接口，返回 (状态, 凭证)。

    状态取值：scanning / confirming / timeout / done / error。

    不用 QrCodeLogin.check_state 有两个原因：
      1. 它把会话存在进程内存里，n8n 这类 stdio 客户端每次工具调用都可能新起进程，
         扫码会话会丢，导致"扫了码但永远登录不上"；这里改成用落盘的 qrcode_key 轮询。
      2. 它解析登录返回串的方式很脆（按大写键名硬切），解析失败会存出一个空凭证。
         这里同时从 url 和 Set-Cookie 取 cookie，解析更宽容。
    """
    if not qr_key:
        return "error", None
    client = get_client()
    buvid3, _ = await get_buvid()
    resp = await client.request(
        method=_LOGIN_POLL_API["method"],
        url=_LOGIN_POLL_API["url"],
        params={"qrcode_key": qr_key},
        headers=HEADERS.copy(),
        cookies={"buvid3": buvid3, "opus-goback": "1"},
    )
    data = resp.json().get("data") or {}
    code = data.get("code")
    if code == 86101:
        return "scanning", None
    if code == 86090:
        return "confirming", None
    if code == 86038:
        return "timeout", None
    return "done", _credential_from_login_payload(data, resp.cookies or {})


async def _ensure_buvid(cred: Credential) -> Credential:
    """
    补齐 buvid3 / buvid4。

    这两个是B站的设备标识，写进凭证文件后每次请求都带上，比每次进程重启都自动申请
    一个新的 buvid 更像正常浏览器，被风控拦截（HTTP 412）的概率更低。
    """
    if cred.buvid3 and cred.buvid4:
        return cred
    try:
        buvid3, buvid4 = await get_buvid()
    except Exception:
        return cred
    cred.buvid3 = cred.buvid3 or buvid3
    cred.buvid4 = cred.buvid4 or buvid4
    return cred


# ========== 反风控：请求节流 + 412 退避重试 ==========
#
# B站对同一 IP 的高频请求会直接返回 412（一页 HTML 风控页）。匿名请求、短时间连续
# 调用空间类接口（如 bili_user_videos）尤其容易触发，实测连续第 3 次就中招。
# 这里在 bilibili-api 的统一请求出口上做两件事：
#   1. 串行化请求，并保证两次请求之间至少间隔 BILI_MIN_REQUEST_INTERVAL 秒
#   2. 遇到 412 退避重试，仍失败则抛出简短可读的错误（不再把整页 HTML 抛给客户端）

# 两次请求之间的最小间隔（秒），匿名请求调大它最有效
MIN_REQUEST_INTERVAL = float(os.environ.get("BILI_MIN_REQUEST_INTERVAL", "2.0"))
# 命中 412 后最多额外重试几次，以及每轮等待多久（实测风控窗口约十几秒）
MAX_412_RETRY = int(os.environ.get("BILI_412_RETRY", "2"))
RETRY_DELAYS = (5.0, 15.0, 30.0)

_request_lock = asyncio.Lock()
_last_request_at = 0.0
_orig_api_request = Api._request


async def _throttled_api_request(self, raw: bool = False, byte: bool = False):
    """给 bilibili-api 的请求加节流与 412 重试（替换 Api._request）"""
    global _last_request_at
    attempt = 0
    while True:
        async with _request_lock:
            gap = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                result = await _orig_api_request(self, raw=raw, byte=byte)
            except NetworkException as e:
                _last_request_at = time.monotonic()
                if getattr(e, "status", None) != 412 or attempt >= MAX_412_RETRY:
                    if getattr(e, "status", None) == 412:
                        raise ApiException(
                            "触发B站风控（HTTP 412），请求被安全策略拒绝。"
                            f"已退避重试 {attempt} 次仍失败：请确认已登录"
                            "（bili_check_credential 返回 logged_in=true）、降低调用频率、"
                            "稍等几分钟再试；仍不行可换出口 IP 或配置代理。"
                        ) from e
                    raise
            else:
                _last_request_at = time.monotonic()
                return result
        attempt += 1
        # 风控拦截通常是短时的，退避后再试（sleep 放在锁外，避免阻塞其它请求）
        delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
        await asyncio.sleep(delay + random.uniform(0, 1.5))


Api._request = _throttled_api_request


async def _safe_get_bili_ticket(credential=None):
    """
    bili_ticket 取不到时不能拖垮正常请求。

    库里是直接在拿票据失败的情况下抛异常的，一旦票据接口被风控，所有请求都会跟着失败。
    这里失败就当作没有票据，并静默 10 分钟不再重试，避免每次请求都多打一次接口。
    """
    global _ticket_retry_after
    if time.monotonic() < _ticket_retry_after:
        return "", "0"
    try:
        return await get_bili_ticket(credential)
    except Exception:
        _ticket_retry_after = time.monotonic() + 600
        return "", "0"


_ticket_retry_after = 0.0
bili_network.get_bili_ticket = _safe_get_bili_ticket

# bili_ticket 是B站网页端使用的反爬票据，带上它能让请求更像正常浏览器
request_settings.set_enable_bili_ticket(True)


# ========== Tool: 扫码登录（生成二维码） ==========

@mcp.tool()
async def bili_login() -> str:
    """
    生成B站登录二维码，返回二维码图片（base64）供用户扫码。
    用户扫码后，调用 bili_login_check 检查登录状态。

    Returns:
        包含二维码base64图片数据的JSON，前端可直接渲染为图片展示给用户
    """
    global _login_session

    # 已有凭证时先确认真的有效：过期或空的凭证不能把二维码入口堵死
    note = ""
    cred = load_credential()
    if cred:
        valid, username, err = await _credential_status(cred)
        if valid:
            return json.dumps({
                "status": "already_logged_in",
                "message": f"已登录B站（{username}），无需重复登录" if username else "已登录B站，无需重复登录",
                "username": username,
            }, ensure_ascii=False)
        note = f"检测到已保存的凭证不可用（{err or '校验未通过'}），请重新扫码登录。"

    # 生成新的登录会话
    _login_session = QrCodeLogin()
    await _login_session.generate_qrcode()
    # 会话落盘：n8n 等 stdio 客户端每次调用可能新起进程，靠它才能续上轮询
    _save_login_session(getattr(_login_session, "_QrCodeLogin__qr_key", ""))

    # 获取二维码图片的 base64（无损PNG）
    pic = _login_session.get_qrcode_picture()
    img_base64 = base64.b64encode(pic.content).decode("utf-8")

    # 保存二维码图片到项目根目录
    with open(QR_FILE, "wb") as f:
        f.write(pic.content)

    # 获取终端文本版二维码（纯文本客户端备用）
    terminal_qr = _login_session.get_qrcode_terminal()

    # 获取原始扫码URL（用户也可以手动在B站App打开）
    qr_url = getattr(_login_session, "_QrCodeLogin__qr_link", "")

    return json.dumps({
        "status": "qrcode_ready",
        "message": note or "请用B站App扫描二维码登录（180秒内有效）",
        "qrcode_image": f"data:image/png;base64,{img_base64}",
        "qrcode_file": str(QR_FILE),
        "qrcode_terminal": terminal_qr,
        "qrcode_url": qr_url,
        "next_step": "用户扫码后，请调用 bili_login_check 检查登录状态",
    }, ensure_ascii=False)


# ========== Tool: 检查登录状态 ==========

@mcp.tool()
async def bili_login_check() -> str:
    """
    检查B站扫码登录状态。在用户扫码后调用此工具。
    如果返回 "scanning" 或 "confirming"，请等待几秒后再次调用。
    如果返回 "done"，登录成功，可以使用其他工具了。
    如果返回 "timeout"，需要重新调用 bili_login 生成新二维码。

    Returns:
        当前登录状态
    """
    global _login_session

    # 优先用进程内的会话；进程被重启过（n8n 常见）则用落盘的 qrcode_key 续上
    qr_key = getattr(_login_session, "_QrCodeLogin__qr_key", "") if _login_session else ""
    if not qr_key:
        qr_key = _load_login_session()

    if not qr_key:
        cred = load_credential()
        if cred:
            return json.dumps({
                "status": "already_logged_in",
                "message": "已登录B站",
            }, ensure_ascii=False)
        return json.dumps({
            "status": "no_session",
            "message": "没有进行中的登录，请先调用 bili_login 生成二维码",
        }, ensure_ascii=False)

    state, cred = await _poll_login_state(qr_key)

    if state == "scanning":
        return json.dumps({
            "status": "scanning",
            "message": "已扫码，等待用户在手机上确认...",
            "next_step": "请等待3秒后再次调用 bili_login_check",
        }, ensure_ascii=False)

    elif state == "confirming":
        return json.dumps({
            "status": "confirming",
            "message": "用户已确认，正在处理...",
            "next_step": "请等待2秒后再次调用 bili_login_check",
        }, ensure_ascii=False)

    elif state == "timeout":
        _login_session = None
        _clear_login_session()
        _cleanup_qrcode()
        return json.dumps({
            "status": "timeout",
            "message": "二维码已过期，请重新调用 bili_login 生成新二维码",
        }, ensure_ascii=False)

    elif state == "error":
        return json.dumps({
            "status": "no_session",
            "message": "登录会话无效（没有 qrcode_key），请重新调用 bili_login 生成二维码",
        }, ensure_ascii=False)

    else:  # done
        _login_session = None
        _clear_login_session()
        _cleanup_qrcode()

        # 没有 sessdata 的"假登录"一旦写进文件，字幕等接口会一直报
        # CredentialNoSessdataException，所以必须先验证再保存。
        if not cred or not (cred.sessdata or "").strip():
            return json.dumps({
                "status": "login_failed",
                "message": "扫码已确认，但登录接口没返回可用的 SESSDATA（url 和 Set-Cookie 里都没有），凭证未保存。",
                "next_step": "请重新调用 bili_login 再扫一次；若反复失败，改用手动方式：浏览器登录B站后 F12 → Application → Cookies，把 SESSDATA / bili_jct / DedeUserID 通过 bili_login_with_cookies 写入",
            }, ensure_ascii=False)

        await _ensure_buvid(cred)
        save_credential(cred)

        valid, username, err = await _credential_status(cred)
        if not valid:
            return json.dumps({
                "status": "done",
                "logged_in": False,
                "message": f"凭证已保存，但校验没通过（{err}），需要登录态的接口可能仍不可用",
                "next_step": "可调用 bili_check_credential 复查，必要时重新调用 bili_login 扫码",
            }, ensure_ascii=False)

        return json.dumps({
            "status": "done",
            "logged_in": True,
            "username": username,
            "message": f"登录成功（{username}）！凭证已保存，现在可以使用所有B站功能了" if username else "登录成功！凭证已保存，现在可以使用所有B站功能了",
        }, ensure_ascii=False)


# ========== Tool: 手动写入 Cookies 登录（扫码解析失败时的备用方案） ==========

@mcp.tool()
async def bili_login_with_cookies(
    sessdata: str,
    bili_jct: str = "",
    dedeuserid: str = "",
    buvid3: str = "",
    ac_time_value: str = "",
) -> str:
    """
    手动填入浏览器 Cookies 完成登录，主要用于扫码登录解析不出 SESSDATA 的兜底方案。

    使用方式：浏览器登录B站后按 F12 → Application → Cookies → https://www.bilibili.com，
    复制 SESSDATA（必需）、bili_jct、DedeUserID 传给本工具即可。
    写操作（发评论/发动态/上传）还需要 bili_jct。

    Args:
        sessdata: Cookie 里的 SESSDATA，必需
        bili_jct: Cookie 里的 bili_jct，写操作需要
        dedeuserid: Cookie 里的 DedeUserID
        buvid3: Cookie 里的 buvid3，可留空（库会自动获取）
        ac_time_value: Cookie 里的 ac_time_value，可留空，填了可以自动刷新登录态

    Returns:
        保存与校验结果
    """
    sessdata = (sessdata or "").strip()
    if not sessdata:
        return json.dumps({
            "success": False,
            "message": "sessdata 不能为空。请在浏览器 Cookies 里复制 SESSDATA 完整值（通常以数字开头、含 %2C）",
        }, ensure_ascii=False)

    cred = Credential(
        sessdata=sessdata,
        bili_jct=(bili_jct or "").strip(),
        buvid3=(buvid3 or "").strip(),
        dedeuserid=(dedeuserid or "").strip(),
        ac_time_value=(ac_time_value or "").strip(),
    )
    await _ensure_buvid(cred)
    save_credential(cred)

    valid, username, err = await _credential_status(cred)
    return json.dumps({
        "success": valid,
        "logged_in": valid,
        "uid": cred.dedeuserid or "",
        "username": username,
        "message": f"登录成功（{username}）！凭证已保存" if valid else f"凭证已保存，但校验没通过：{err}。请确认复制的是当前登录的完整 SESSDATA。",
    }, ensure_ascii=False)


# ========== Tool: 登录状态查询 ==========

@mcp.tool()
async def bili_check_credential() -> str:
    """
    检查当前B站登录凭证是否有效

    Returns:
        登录状态信息
    """
    cred = load_credential()
    if not cred:
        return json.dumps({
            "logged_in": False,
            "message": f"未登录：{_cred_file_hint()}",
            "next_step": "调用 bili_login 扫码登录；若扫码后依旧拿不到有效凭证，可按 README 手动写入 bili_credential.json",
        }, ensure_ascii=False)

    valid, username, err = await _credential_status(cred)
    return json.dumps({
        "logged_in": valid,
        "uid": cred.dedeuserid or "",
        "username": username,
        "message": "凭证有效" if valid else f"凭证无效：{err}",
        "next_step": "" if valid else "调用 bili_login 重新扫码登录",
    }, ensure_ascii=False)


# ========== Tool 1: 搜索视频 ==========

@mcp.tool()
async def bili_search(keyword: str, num: int = 10, order: str = "totalrank") -> str:
    """
    搜索B站视频

    Args:
        keyword: 搜索关键词，如"AI Agent"、"大模型教程"
        num: 返回视频数量，默认10，最大50
        order: 排序方式 totalrank=综合 click=播放量 pubdate=最新 dm=弹幕
    
    Returns:
        JSON格式的视频列表，包含标题、BV号、播放量、评论数、UP主等
    """
    order_map = {
        "totalrank": search.OrderVideo.TOTALRANK,
        "click": search.OrderVideo.CLICK,
        "pubdate": search.OrderVideo.PUBDATE,
        "dm": search.OrderVideo.DM,
    }
    order_enum = order_map.get(order, search.OrderVideo.TOTALRANK)

    result = await search.search_by_type(
        keyword=keyword,
        search_type=search.SearchObjectType.VIDEO,
        page=1,
        order_type=order_enum,
    )

    videos = []
    for item in result.get("result", [])[:num]:
        title = item.get("title", "").replace('<em class="keyword">', "").replace("</em>", "")
        videos.append({
            "bvid": item.get("bvid", ""),
            "aid": item.get("aid", 0),
            "title": title,
            "author": item.get("author", ""),
            "play": item.get("play", 0),
            "review": item.get("review", 0),
            "danmaku": item.get("video_review", 0),
            "duration": item.get("duration", ""),
            "description": item.get("description", "")[:200],
        })

    return json.dumps({"keyword": keyword, "count": len(videos), "videos": videos}, ensure_ascii=False)


# ========== Tool 2: 获取评论 ==========

@mcp.tool()
async def bili_comments(bvid: str, num: int = 30) -> str:
    """
    获取B站视频的热门评论

    Args:
        bvid: 视频BV号，如"BV1uNk1YxEJQ"
        num: 获取评论数量，默认30
    
    Returns:
        JSON格式的评论列表，包含用户名、评论内容、点赞数、回复数
    """
    # 评论区第一页匿名可读，登录后自动带上登录态（翻页需要登录）
    cred = get_cred_any()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    aid = info["aid"]

    comments = []
    page = 1
    while len(comments) < num:
        try:
            resp = await comment.get_comments(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                page_index=page,
                order=comment.OrderType.LIKE,
                credential=cred,
            )
            replies = resp.get("replies") or []
            if not replies:
                break

            for r in replies:
                member = r.get("member", {})
                content = r.get("content", {})
                c = {
                    "rpid": r.get("rpid", 0),
                    "user": member.get("uname", ""),
                    "content": content.get("message", ""),
                    "like": r.get("like", 0),
                    "reply_count": r.get("rcount", 0),
                    "time": r.get("ctime", 0),
                    "time_at": _format_ts(r.get("ctime", 0)),
                }
                # 子评论
                sub_replies = []
                for sub in (r.get("replies") or [])[:2]:
                    sub_replies.append({
                        "user": sub.get("member", {}).get("uname", ""),
                        "content": sub.get("content", {}).get("message", ""),
                        "like": sub.get("like", 0),
                    })
                if sub_replies:
                    c["top_replies"] = sub_replies
                comments.append(c)

            page += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            break

    return json.dumps({"bvid": bvid, "count": len(comments[:num]), "comments": comments[:num]}, ensure_ascii=False)


# ========== Tool 3: 获取字幕 ==========

@mcp.tool()
async def bili_subtitle(bvid: str) -> str:
    """
    获取B站视频的AI字幕（语音转文字）

    Args:
        bvid: 视频BV号，如"BV1uNk1YxEJQ"
    
    Returns:
        视频的完整字幕文本
    """
    # 字幕列表走 /x/player/wbi/v2，该接口必须要登录态。
    # 未登录时返回可读的 JSON 提示，而不是把库的原始异常抛给客户端。
    cred = load_credential()
    if not cred:
        return json.dumps({
            "error": "not_logged_in",
            "message": f"获取字幕需要登录态：{_cred_file_hint()}。请调用 bili_login 扫码登录后重试。",
        }, ensure_ascii=False)

    v = video.Video(bvid=bvid, credential=cred)

    info = await v.get_info()
    cid = info.get("cid", 0)
    if not cid and info.get("pages"):
        cid = info["pages"][0].get("cid", 0)

    if not cid:
        return json.dumps({"error": "无法获取cid"}, ensure_ascii=False)

    try:
        subtitle_list = await v.get_subtitle(cid=cid)
    except CredentialNoSessdataException:
        return json.dumps({
            "error": "not_logged_in",
            "message": f"获取字幕需要登录态：{LOGIN_HINT}",
        }, ensure_ascii=False)
    except Exception as e:
        # -101 = 账号未登录，说明登录态已经过期
        if getattr(e, "code", None) == -101:
            return json.dumps({
                "error": "login_expired",
                "message": f"登录态已过期（-101 账号未登录）：{LOGIN_HINT}",
            }, ensure_ascii=False)
        return json.dumps({
            "error": "subtitle_query_failed",
            "message": f"查询字幕列表失败: {e}",
        }, ensure_ascii=False)

    subtitles = (subtitle_list or {}).get("subtitles", [])

    if not subtitles:
        return json.dumps({"message": "该视频没有字幕"}, ensure_ascii=False)

    # 优先AI中文字幕
    target = None
    for s in subtitles:
        if s.get("lan") in ["ai-zh", "zh-CN", "zh"]:
            target = s
            break
    if not target:
        target = subtitles[0]

    # 下载字幕
    import aiohttp
    sub_url = target.get("subtitle_url", "")
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url

    if not sub_url:
        return json.dumps({"error": "字幕地址为空"}, ensure_ascii=False)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                sub_url, headers={"Referer": "https://www.bilibili.com/"}
            ) as resp:
                sub_data = await resp.json()
    except Exception as e:
        return json.dumps({
            "error": "subtitle_download_failed",
            "message": f"字幕文件下载失败: {e}",
        }, ensure_ascii=False)

    texts = [item.get("content", "") for item in sub_data.get("body", [])]
    full_text = "\n".join(texts)

    return json.dumps({
        "bvid": bvid,
        "title": info.get("title", ""),
        "language": target.get("lan_doc", ""),
        "segments": len(texts),
        "text": full_text,
    }, ensure_ascii=False)


# ========== Tool 4: 获取弹幕 ==========

@mcp.tool()
async def bili_danmaku(bvid: str, num: int = 100) -> str:
    """
    获取B站视频的弹幕

    Args:
        bvid: 视频BV号
        num: 获取弹幕数量，默认100
    
    Returns:
        弹幕列表，包含弹幕文本和出现时间
    """
    cred = get_cred_any()
    v = video.Video(bvid=bvid, credential=cred)

    danmakus = await v.get_danmakus(page_index=0)
    result = []
    for d in danmakus[:num]:
        result.append({
            "text": d.text,
            "time": d.dm_time,
        })

    return json.dumps({"bvid": bvid, "count": len(result), "danmakus": result}, ensure_ascii=False)


# ========== Tool 5: 视频详情 ==========

@mcp.tool()
async def bili_video_info(bvid: str) -> str:
    """
    获取B站视频的详细信息

    Args:
        bvid: 视频BV号
    
    Returns:
        视频标题、描述、UP主、播放量、评论数、收藏数等详细数据
    """
    cred = get_cred_any()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    stat = info.get("stat", {})

    return json.dumps({
        "bvid": info.get("bvid"),
        "aid": info.get("aid"),
        "title": info.get("title"),
        "description": info.get("desc"),
        "author": info.get("owner", {}).get("name"),
        "duration": info.get("duration"),
        "pages": len(info.get("pages", [])),
        "tags": [t.get("tag_name") for t in info.get("tag", []) if t.get("tag_name")],
        "stat": {
            "view": stat.get("view", 0),
            "danmaku": stat.get("danmaku", 0),
            "reply": stat.get("reply", 0),
            "favorite": stat.get("favorite", 0),
            "coin": stat.get("coin", 0),
            "like": stat.get("like", 0),
            "share": stat.get("share", 0),
        },
    }, ensure_ascii=False)


# ========== Tool 6: 回复评论 ==========

@mcp.tool()
async def bili_reply(bvid: str, text: str, rpid: int = 0, root: int = 0) -> str:
    """
    在B站视频下发表评论或回复某条评论

    Args:
        bvid: 视频BV号
        text: 评论/回复的文本内容
        rpid: 要回复的目标评论ID（0表示发表新评论）
        root: 楼层根评论ID（回复一级评论时不用填，回复楼中楼时填根评论ID）
    
    Returns:
        发表结果
    """
    cred = get_cred()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    aid = info["aid"]

    try:
        if rpid == 0:
            # 发表新评论
            result = await comment.send_comment(
                text=text,
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                credential=cred,
            )
        else:
            # 回复评论
            # root=0 表示回复一级评论，root=rpid
            # root!=0 表示回复楼中楼，root=根评论，parent=目标评论
            actual_root = root if root != 0 else rpid
            result = await comment.send_comment(
                text=text,
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                root=actual_root,
                parent=rpid,
                credential=cred,
            )

        return json.dumps({"success": True, "message": "评论发送成功"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ========== Tool 7: 批量采集 ==========

@mcp.tool()
async def bili_crawl(keyword: str, max_videos: int = 5, comments_per_video: int = 20, get_subtitles: bool = True) -> str:
    """
    批量采集：搜索B站视频并抓取每个视频的评论和字幕

    Args:
        keyword: 搜索关键词
        max_videos: 最多采集视频数，默认5
        comments_per_video: 每个视频采集评论数，默认20
        get_subtitles: 是否获取字幕，默认True
    
    Returns:
        包含视频信息、评论和字幕的完整采集数据
    """
    # 搜索
    search_result = await search.search_by_type(
        keyword=keyword,
        search_type=search.SearchObjectType.VIDEO,
        page=1,
        order_type=search.OrderVideo.TOTALRANK,
    )

    results = []
    for item in search_result.get("result", [])[:max_videos]:
        bvid = item.get("bvid", "")
        if not bvid:
            continue

        title = item.get("title", "").replace('<em class="keyword">', "").replace("</em>", "")
        video_data = {
            "bvid": bvid,
            "title": title,
            "author": item.get("author", ""),
            "play": item.get("play", 0),
            "review": item.get("review", 0),
        }

        # 评论
        comments_json = await bili_comments(bvid=bvid, num=comments_per_video)
        comments_data = json.loads(comments_json)

        # 字幕（需要登录态，失败时把原因带回去，不静默吞掉）
        subtitle_text = ""
        subtitle_error = ""
        if get_subtitles:
            try:
                subtitle_data = json.loads(await bili_subtitle(bvid=bvid))
                subtitle_text = subtitle_data.get("text", "")
                if subtitle_data.get("error"):
                    subtitle_error = subtitle_data.get("message") or subtitle_data["error"]
            except Exception as e:
                subtitle_error = str(e)

        results.append({
            "video": video_data,
            "comments": comments_data.get("comments", []),
            "subtitle_text": subtitle_text,
            "subtitle_error": subtitle_error,
        })

        await asyncio.sleep(1)

    return json.dumps({
        "keyword": keyword,
        "video_count": len(results),
        "total_comments": sum(len(r["comments"]) for r in results),
        "results": results,
    }, ensure_ascii=False)


# ========== 辅助：封面处理 ==========

import subprocess


def _extract_cover_from_video(video_path: str) -> Picture:
    """从视频第3秒截取一帧作为封面，返回 Picture 对象"""
    tmp_cover = os.path.join(tempfile.gettempdir(), f"bili_cover_{os.getpid()}.png")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "3", "-i", video_path,
             "-vframes", "1", "-q:v", "2", tmp_cover],
            capture_output=True, timeout=30,
        )
        if os.path.isfile(tmp_cover) and os.path.getsize(tmp_cover) > 0:
            return Picture.from_file(tmp_cover)
    finally:
        if os.path.isfile(tmp_cover):
            os.remove(tmp_cover)
    # fallback: 第0秒
    tmp_cover2 = os.path.join(tempfile.gettempdir(), f"bili_cover2_{os.getpid()}.png")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "0", "-i", video_path,
             "-vframes", "1", "-q:v", "2", tmp_cover2],
            capture_output=True, timeout=30,
        )
        if os.path.isfile(tmp_cover2) and os.path.getsize(tmp_cover2) > 0:
            return Picture.from_file(tmp_cover2)
    finally:
        if os.path.isfile(tmp_cover2):
            os.remove(tmp_cover2)
    raise RuntimeError(f"无法从视频截取封面: {video_path}")




@mcp.tool()
async def bili_send_dynamic(
    text: str,
    images: list[str] | None = None,
    topic_id: int = 0,
    schedule_time: int = 0,
) -> str:
    """
    发布B站图文动态

    Args:
        text: 动态文本内容，支持@和表情（如 [doge]）
        images: 图片列表，每项可以是本地文件路径或图片URL，最多9张。为空则发布纯文字动态
        topic_id: 话题ID（可选，0表示不关联话题）
        schedule_time: 定时发布的Unix时间戳（可选，0表示立即发布）

    Returns:
        发布结果，包含动态ID
    """
    if not text or not text.strip():
        return json.dumps({"success": False, "error": "text 不能为空"}, ensure_ascii=False)

    cred = get_cred()

    dyn = dynamic.BuildDynamic.empty()
    dyn.add_plain_text(text.strip())
    # 上传图片
    if images:
        for img_path in images[:9]:
            try:
                if not img_path or not img_path.strip():
                    continue
                img_path = img_path.strip()
                if img_path.startswith(("http://", "https://")):
                    pic = await Picture.async_from_url(img_path)
                else:
                    if not os.path.isfile(img_path):
                        return json.dumps({"success": False, "error": f"图片文件不存在: {img_path}"}, ensure_ascii=False)
                    pic = Picture.from_file(img_path)
                dyn.add_image(pic)
            except Exception as e:
                return json.dumps({"success": False, "error": f"图片处理失败: {img_path} - {str(e)}"}, ensure_ascii=False)

    if topic_id:
        dyn.set_topic(topic_id)

    if schedule_time > 0:
        dyn.set_send_time(schedule_time)

    try:
        result = await dynamic.send_dynamic(info=dyn, credential=cred)
        return json.dumps({
            "success": True,
            "message": "动态发布成功",
            "data": result if isinstance(result, dict) else str(result),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ========== Tool 9: 上传视频 ==========

@mcp.tool()
async def bili_upload_video(
    video_path: str,
    title: str,
    desc: str = "",
    tid: int = 124,
    tags: str = "AI",
    cover_path: str = "",
    source: str = "",
    dynamic_text: str = "",
    no_reprint: bool = True,
) -> str:
    """
    上传视频到B站

    Args:
        video_path: 视频文件的本地路径
        title: 视频标题（最多80字）
        desc: 视频简介描述
        tid: 分区ID，默认124（趣味科普人文-社科·法律·心理）。
             常用分区：17=单机游戏 21=日常 95=数码 122=野生技术协会
             124=社科 160=生活记录 171=电子竞技 183=影视杂谈
             188=科技资讯 201=科学 207=财经商业 208=科技 209=手工
             230=其他(生活) 231=美食 234=健身 32=完结动画
        tags: 标签，逗号分隔，如"AI,教程,编程"（至少1个标签）
        cover_path: 封面图片路径（可选，不填则B站自动截取）
        source: 转载来源URL（非原创时必填）
        dynamic_text: 粉丝动态文本（可选，投稿时同步发布的动态内容）
        no_reprint: 是否启用未经作者授权禁止转载，默认True

    Returns:
        上传结果，包含BV号
    """
    # ---- 参数校验 ----
    if not video_path or not video_path.strip():
        return json.dumps({"success": False, "error": "video_path 不能为空，请提供视频文件的本地路径"}, ensure_ascii=False)

    video_path = video_path.strip()
    if not os.path.isfile(video_path):
        return json.dumps({"success": False, "error": f"视频文件不存在: {video_path}"}, ensure_ascii=False)

    if not title or not title.strip():
        return json.dumps({"success": False, "error": "title 不能为空"}, ensure_ascii=False)

    cred = get_cred()

    try:
        # 准备分P
        page = VideoUploaderPage(
            path=video_path,
            title=title.strip()[:80],
            description=(desc or "")[:250],
        )

        # 标签处理
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        if not tag_list:
            tag_list = ["视频"]

        # 判断原创/转载
        is_original = not bool(source and source.strip())

        # 封面：用户提供 > 自动从视频截取
        if cover_path and cover_path.strip() and os.path.isfile(cover_path.strip()):
            cover_pic = Picture.from_file(cover_path.strip())
        else:
            cover_pic = _extract_cover_from_video(video_path)

        meta = VideoMeta(
            tid=tid,
            title=title.strip()[:80],
            desc=desc or "",
            cover=cover_pic,
            tags=tag_list,
            original=is_original,
            source=source.strip() if source and source.strip() else None,
            no_reprint=no_reprint if is_original else False,
            dynamic=dynamic_text.strip() if dynamic_text and dynamic_text.strip() else None,
        )

        uploader = VideoUploader(
            pages=[page],
            meta=meta,
            credential=cred,
        )

        # 上传事件回调（记录进度）
        progress_info = {"phase": "初始化"}

        @uploader.on("__ALL__")
        async def on_event(data: dict):
            progress_info["phase"] = str(data)

        result = await uploader.start()
        return json.dumps({
            "success": True,
            "message": "视频上传成功",
            "data": result if isinstance(result, dict) else str(result),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
            "last_phase": progress_info.get("phase", "unknown") if "progress_info" in dir() else "init",
        }, ensure_ascii=False)


# ========== Tool 10: 上传多P视频 ==========

@mcp.tool()
async def bili_upload_video_multi(
    video_paths: list[str],
    page_titles: list[str],
    title: str,
    desc: str = "",
    tid: int = 124,
    tags: str = "AI",
    cover_path: str = "",
    source: str = "",
) -> str:
    """
    上传多P视频到B站（多个分P合并为一个投稿）

    Args:
        video_paths: 视频文件路径列表，如["/path/p1.mp4", "/path/p2.mp4"]
        page_titles: 各分P标题列表，与video_paths一一对应
        title: 视频总标题
        desc: 视频简介
        tid: 分区ID，默认124
        tags: 标签，逗号分隔
        cover_path: 封面图片路径（可选）
        source: 转载来源URL（非原创时必填）

    Returns:
        上传结果
    """
    cred = get_cred()

    if not video_paths:
        return json.dumps({"success": False, "error": "video_paths 不能为空"}, ensure_ascii=False)
    if not page_titles:
        return json.dumps({"success": False, "error": "page_titles 不能为空"}, ensure_ascii=False)
    if len(video_paths) != len(page_titles):
        return json.dumps({"success": False, "error": "video_paths 和 page_titles 数量必须一致"}, ensure_ascii=False)

    try:
        pages = []
        for i, vp in enumerate(video_paths):
            vp = (vp or "").strip()
            if not vp or not os.path.isfile(vp):
                return json.dumps({"success": False, "error": f"文件不存在: {vp}"}, ensure_ascii=False)
            pages.append(VideoUploaderPage(
                path=vp,
                title=(page_titles[i] or "").strip()[:80] or f"P{i+1}",
                description="",
            ))

        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()] or ["视频"]
        is_original = not bool(source and source.strip())

        # 封面：用户提供 > 从第一个视频自动截取
        if cover_path and cover_path.strip() and os.path.isfile(cover_path.strip()):
            cover_pic = Picture.from_file(cover_path.strip())
        else:
            cover_pic = _extract_cover_from_video(pages[0].path)

        meta = VideoMeta(
            tid=tid,
            title=(title or "").strip()[:80],
            desc=desc or "",
            cover=cover_pic,
            tags=tag_list,
            original=is_original,
            source=source.strip() if source and source.strip() else None,
        )

        uploader = VideoUploader(pages=pages, meta=meta, credential=cred)

        result = await uploader.start()
        return json.dumps({
            "success": True,
            "message": f"多P视频上传成功（共{len(pages)}P）",
            "data": result if isinstance(result, dict) else str(result),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ========== Tool 11: 发布专栏文章 (Opus) ==========

@mcp.tool()
async def bili_send_opus(
    title: str,
    content: str,
    images: list[str] | None = None,
    category_id: int = 0,
) -> str:
    """
    发布B站图文（Opus，新版专栏）

    Args:
        title: 文章标题
        content: 文章正文内容（纯文本，段落用换行分隔）
        images: 文章中插入的图片路径或URL列表（可选）
        category_id: 分类ID（可选，0表示不指定）

    Returns:
        发布结果
    """
    cred = get_cred()

    dyn = dynamic.BuildDynamic.empty()

    # 专栏动态以纯文本+图片方式构建
    # 标题作为第一行加粗
    full_text = f"【{title}】\n\n{content}"
    dyn.add_plain_text(full_text)

    if images:
        for img_path in images:
            try:
                if not img_path or not img_path.strip():
                    continue
                img_path = img_path.strip()
                if img_path.startswith(("http://", "https://")):
                    pic = await Picture.async_from_url(img_path)
                else:
                    pic = Picture.from_file(img_path)
                dyn.add_image(pic)
            except Exception as e:
                pass  # 图片失败不阻断发布

    try:
        result = await dynamic.send_dynamic(info=dyn, credential=cred)
        return json.dumps({
            "success": True,
            "message": "图文发布成功",
            "data": result if isinstance(result, dict) else str(result),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ========== Tool 12: 查询分区列表 ==========

@mcp.tool()
async def bili_video_zones() -> str:
    """
    获取B站视频常用分区ID列表，供上传视频时选择tid参数

    Returns:
        常用分区ID及名称
    """
    zones = {
        "科技": {
            188: "科技资讯", 122: "野生技术协会", 95: "数码",
            208: "科技", 209: "手工",
        },
        "知识": {
            201: "科学", 124: "社科·法律·心理", 207: "财经商业",
            228: "人文历史", 36: "科技(知识)",
        },
        "生活": {
            21: "日常", 160: "生活记录", 230: "其他",
            231: "美食", 234: "健身", 161: "搞笑",
        },
        "游戏": {
            17: "单机游戏", 171: "电子竞技", 172: "手机游戏",
            65: "网络游戏",
        },
        "影视": {
            183: "影视杂谈", 138: "搞笑", 182: "影视剪辑",
        },
        "动画": {
            32: "完结动画", 33: "连载动画", 51: "MAD·AMV",
        },
        "音乐": {
            28: "原创音乐", 31: "翻唱", 59: "演奏",
        },
    }
    return json.dumps(zones, ensure_ascii=False)


# ========================================================================
#                        V1.2 — 数据分析 & 互动运营
# ========================================================================


# ========== Tool 13: 热门视频 ==========

@mcp.tool()
async def bili_hot_videos(pn: int = 1, ps: int = 20) -> str:
    """
    获取B站当前热门视频列表

    Args:
        pn: 页码，默认1
        ps: 每页数量，默认20，最大50

    Returns:
        热门视频列表，包含标题、播放量、UP主等
    """
    result = await hot.get_hot_videos(pn=pn, ps=min(ps, 50))
    videos = []
    for item in result.get("list", []):
        stat = item.get("stat", {})
        videos.append({
            "bvid": item.get("bvid", ""),
            "title": item.get("title", ""),
            "author": item.get("owner", {}).get("name", ""),
            "play": stat.get("view", 0),
            "like": stat.get("like", 0),
            "danmaku": stat.get("danmaku", 0),
            "reply": stat.get("reply", 0),
            "desc": (item.get("desc", "") or "")[:100],
            "duration": item.get("duration", 0),
            "tname": item.get("tname", ""),
        })
    return json.dumps({"page": pn, "count": len(videos), "videos": videos}, ensure_ascii=False)


# ========== Tool 14: 热搜关键词 ==========

@mcp.tool()
async def bili_hot_buzzwords(page_num: int = 1, page_size: int = 20) -> str:
    """
    获取B站热搜词/热门关键词

    Args:
        page_num: 页码，默认1
        page_size: 每页数量，默认20

    Returns:
        热搜词列表
    """
    result = await hot.get_hot_buzzwords(page_num=page_num, page_size=page_size)
    return json.dumps(result, ensure_ascii=False)


# ========== Tool 15: 每周必看 ==========

@mcp.tool()
async def bili_weekly_hot(week: int = 0) -> str:
    """
    获取B站每周必看视频推荐

    Args:
        week: 期数（0表示获取期数列表，>0表示获取该期的视频）

    Returns:
        每周必看期数列表或指定期的视频列表
    """
    if week <= 0:
        result = await hot.get_weekly_hot_videos_list()
        return json.dumps(result, ensure_ascii=False)
    else:
        result = await hot.get_weekly_hot_videos(week=week)
        return json.dumps(result, ensure_ascii=False)


# ========== Tool 16: 排行榜 ==========

@mcp.tool()
async def bili_rank(category: str = "all", day: int = 3) -> str:
    """
    获取B站各分区排行榜

    Args:
        category: 分区名，可选值：
            all=全站 original=原创 rookie=新人
            douga=动画 music=音乐 dance=舞蹈 game=游戏
            knowledge=知识 technology=科技 sports=运动 car=汽车
            life=生活 food=美食 animal=动物 fashion=时尚
            ent=娱乐 cinephile=影视
        day: 时间维度，3=三日 7=七日

    Returns:
        排行榜视频列表
    """
    type_map = {
        "all": rank.RankType.All, "original": rank.RankType.Original,
        "rookie": rank.RankType.Rookie, "douga": rank.RankType.Douga,
        "music": rank.RankType.Music, "dance": rank.RankType.Dance,
        "game": rank.RankType.Game, "knowledge": rank.RankType.Knowledge,
        "technology": rank.RankType.Technology, "sports": rank.RankType.Sports,
        "car": rank.RankType.Car, "life": rank.RankType.Life,
        "food": rank.RankType.Food, "animal": rank.RankType.Animal,
        "fashion": rank.RankType.Fashion, "ent": rank.RankType.Ent,
        "cinephile": rank.RankType.Cinephile,
    }
    day_map = {3: rank.RankDayType.THREE_DAY, 7: rank.RankDayType.WEEK}

    rank_type = type_map.get(category.lower(), rank.RankType.All)
    rank_day = day_map.get(day, rank.RankDayType.THREE_DAY)

    result = await rank.get_rank(type_=rank_type, day=rank_day)
    videos = []
    for item in result.get("list", []):
        stat = item.get("stat", {})
        videos.append({
            "bvid": item.get("bvid", ""),
            "title": item.get("title", ""),
            "author": item.get("owner", {}).get("name", ""),
            "play": stat.get("view", 0),
            "like": stat.get("like", 0),
            "coin": stat.get("coin", 0),
            "score": item.get("score", 0),
            "tname": item.get("tname", ""),
        })
    return json.dumps({"category": category, "day": day, "count": len(videos), "videos": videos}, ensure_ascii=False)


# ========== Tool 17: 用户信息 ==========

@mcp.tool()
async def bili_user_info(uid: int) -> str:
    """
    获取B站用户的详细信息

    Args:
        uid: 用户UID

    Returns:
        用户昵称、粉丝数、关注数、签名、等级、视频数等
    """
    cred = get_cred_any()
    u = user.User(uid=uid, credential=cred)
    info = await u.get_user_info()

    # 尝试获取UP主数据
    up_stat = {}
    try:
        up_stat = await u.get_up_stat()
    except:
        pass

    relation = {}
    try:
        relation = await u.get_relation_info()
    except:
        pass

    return json.dumps({
        "uid": uid,
        "name": info.get("name", ""),
        "sign": info.get("sign", ""),
        "level": info.get("level", 0),
        "face": info.get("face", ""),
        "fans": relation.get("follower", info.get("follower", 0)),
        "following": relation.get("following", info.get("following", 0)),
        "likes": up_stat.get("likes", 0),
        "archive_view": up_stat.get("archive", {}).get("view", 0),
        "article_view": up_stat.get("article", {}).get("view", 0),
        "is_senior_member": info.get("is_senior_member", 0),
        "top_photo": info.get("top_photo", ""),
    }, ensure_ascii=False)


# ========== Tool 18: 用户视频列表 ==========

@mcp.tool()
async def bili_user_videos(uid: int, pn: int = 1, ps: int = 30, order: str = "pubdate", keyword: str = "") -> str:
    """
    获取B站用户的投稿视频列表

    Args:
        uid: 用户UID
        pn: 页码，默认1
        ps: 每页数量，默认30
        order: 排序方式 pubdate=最新 click=播放量 stow=收藏
        keyword: 搜索关键词（在该用户视频中搜索）

    Returns:
        视频列表，每条含 created（Unix 时间戳）、created_at（北京时间 yyyy-mm-dd HH:MM:SS）
        和 created_date（北京时间日期）
    """
    cred = get_cred_any()
    u = user.User(uid=uid, credential=cred)

    order_map = {
        "pubdate": user.VideoOrder.PUBDATE,
        "click": user.VideoOrder.VIEW,
        "stow": user.VideoOrder.FAVORITE,
    }
    order_enum = order_map.get(order, user.VideoOrder.PUBDATE)

    result = await u.get_videos(pn=pn, ps=ps, order=order_enum, keyword=keyword)
    videos = []
    for item in result.get("list", {}).get("vlist", []):
        created = item.get("created", 0)
        videos.append({
            "bvid": item.get("bvid", ""),
            "title": item.get("title", ""),
            "play": item.get("play", 0),
            "comment": item.get("comment", 0),
            "created": created,
            "created_at": _format_ts(created),
            "created_date": _format_ts(created, "%Y-%m-%d"),
            "length": item.get("length", ""),
            "description": (item.get("description", "") or "")[:100],
        })
    return json.dumps({
        "uid": uid,
        "page": pn,
        "total": result.get("page", {}).get("count", 0),
        "count": len(videos),
        "videos": videos,
    }, ensure_ascii=False)


# ========== Tool 19: 收藏夹列表 ==========

@mcp.tool()
async def bili_favorite_lists(uid: int = 0) -> str:
    """
    获取用户的收藏夹列表

    Args:
        uid: 用户UID（0表示获取自己的收藏夹）

    Returns:
        收藏夹列表，包含ID、名称、视频数量
    """
    cred = get_cred_any()
    if uid == 0:
        # 获取自己的收藏夹（uid=0）必须先登录，并且需要 dedeuserid
        me = load_credential()
        if not me or not (me.dedeuserid or "").strip().isdigit():
            return json.dumps({
                "error": "not_logged_in",
                "message": f"uid=0（获取自己的收藏夹）需要先登录：{_cred_file_hint()}。请调用 bili_login 扫码登录后重试。",
            }, ensure_ascii=False)
        uid = int(me.dedeuserid)

    result = await favorite_list.get_video_favorite_list(uid=uid, credential=cred)
    fav_lists = []
    for item in (result.get("list", []) or []):
        fav_lists.append({
            "id": item.get("id", 0),
            "title": item.get("title", ""),
            "media_count": item.get("media_count", 0),
            "fav_state": item.get("fav_state", 0),
        })
    return json.dumps({"uid": uid, "count": len(fav_lists), "lists": fav_lists}, ensure_ascii=False)


# ========== Tool 20: 收藏夹内容 ==========

@mcp.tool()
async def bili_favorite_content(media_id: int, page: int = 1, keyword: str = "") -> str:
    """
    获取收藏夹内的视频列表

    Args:
        media_id: 收藏夹ID（从 bili_favorite_lists 获取）
        page: 页码，默认1
        keyword: 搜索关键词（在收藏夹内搜索）

    Returns:
        收藏夹内的视频列表
    """
    cred = get_cred_any()
    result = await favorite_list.get_video_favorite_list_content(
        media_id=media_id,
        page=page,
        keyword=keyword if keyword else None,
        credential=cred,
    )
    medias = []
    for item in (result.get("medias", []) or []):
        medias.append({
            "bvid": item.get("bvid", ""),
            "title": item.get("title", ""),
            "play": item.get("cnt_info", {}).get("play", 0),
            "collect": item.get("cnt_info", {}).get("collect", 0),
            "author": item.get("upper", {}).get("name", ""),
            "duration": item.get("duration", 0),
            "fav_time": item.get("fav_time", 0),
        })
    return json.dumps({
        "media_id": media_id,
        "page": page,
        "has_more": result.get("has_more", False),
        "count": len(medias),
        "medias": medias,
    }, ensure_ascii=False)


# ========== Tool 21: 发私信 ==========

@mcp.tool()
async def bili_send_message(receiver_uid: int, text: str) -> str:
    """
    给B站用户发送私信

    Args:
        receiver_uid: 接收者的UID
        text: 私信文本内容

    Returns:
        发送结果
    """
    cred = get_cred()

    if not text or not text.strip():
        return json.dumps({"success": False, "error": "text 不能为空"}, ensure_ascii=False)

    try:
        result = await session.send_msg(
            credential=cred,
            receiver_id=receiver_uid,
            msg_type=session.EventType.TEXT,
            content=text.strip(),
        )
        return json.dumps({
            "success": True,
            "message": f"私信已发送给UID:{receiver_uid}",
            "data": result if isinstance(result, dict) else str(result),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ========== Tool 22: 未读消息 ==========

@mcp.tool()
async def bili_unread_messages() -> str:
    """
    获取B站未读消息数（私信、@、回复、点赞等）

    Returns:
        各类未读消息数量
    """
    cred = get_cred()
    try:
        result = await session.get_unread_messages(credential=cred)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ========== Tool 23: 最近收到的回复 ==========

@mcp.tool()
async def bili_received_replies() -> str:
    """
    获取最近收到的评论回复通知

    Returns:
        回复列表
    """
    cred = get_cred()
    try:
        result = await session.get_replies(credential=cred)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ========== Tool 24: 最近收到的@和点赞 ==========

@mcp.tool()
async def bili_received_at_and_likes() -> str:
    """
    获取最近收到的@提及和点赞通知

    Returns:
        包含 at 和 likes 两部分的通知数据
    """
    cred = get_cred()
    result = {}
    try:
        result["at"] = await session.get_at(credential=cred)
    except Exception as e:
        result["at_error"] = str(e)
    try:
        result["likes"] = await session.get_likes(credential=cred)
    except Exception as e:
        result["likes_error"] = str(e)
    return json.dumps(result, ensure_ascii=False)


# ========== 启动 ==========

if __name__ == "__main__":
    mcp.run(transport="stdio")
