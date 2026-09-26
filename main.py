# -*- coding: utf-8 -*-
"""
AstrBot Plugin: Baidu Netdisk Share Downloader
Download files from Baidu Netdisk share links using BaiduPCS-Go,
then send via AstrBot's platform adapters.
"""
import asyncio
import os
import queue
import re
import subprocess
import threading
import time
import urllib.parse
import uuid
import json as _json
import requests as _req

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Star, Context, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.star.filter.command import GreedyStr

try:
    from astrbot.api.event import MessageChain
except ImportError:  # 兼容旧版 AstrBot
    from astrbot.core.message.message_event_result import MessageChain

BPCS_PATH = os.path.join(os.path.dirname(__file__), "BaiduPCS-Go.exe")
# BaiduPCS-Go 官方 release 信息（下载 URL 和文件大小从 GitHub API 实时获取，不硬编码哈希值）
BPCS_VERSION = "v4.0.1"
BPCS_REPO = "qjfoidnh/BaiduPCS-Go"
BPCS_API_URL = f"https://api.github.com/repos/{BPCS_REPO}/releases/tags/{BPCS_VERSION}"
BPCS_ASSET_NAME = "BaiduPCS-Go-v4.0.1-windows-x64.zip"
# 国内镜像直链（与官方 GitHub release 完全一致，SHA256 已核对）
BPCS_CN_URL = "https://www.now61.cn/f/0pb4TV/BaiduPCS-Go.exe"
# exe 的 SHA256 和大小（来自 GitHub release 页面官方显示，可自行核对：
# https://github.com/qjfoidnh/BaiduPCS-Go/releases/tag/v4.0.1）
BPCS_EXE_SHA256 = "4719f6ebf7f7891284c9f53a6cc4e9474f872b444fddc05b60ad07147a96cd41"
BPCS_EXE_SIZE = 13314048
DOWNLOAD_DIR = None  # 初始化时根据配置设置
CLOUD_SAVE_DIR = None  # 网盘转存目录
FLASH_TASK_LIMIT_MB = 200
DOWNLOAD_TIMEOUT_SECONDS = -1  # 下载超时秒数，-1 = 不超时（可在配置中修改）

# 本插件下载过的本地文件清单，定时清理时只删这些，不动下载目录里的其他文件
_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "storage", "cleanup_manifest.json")

_bpcs_inited = False
_bpcs_lock = threading.Lock()
_auto_delete_cloud = True
_local_cleanup_hour = 3
# 模拟真实浏览器的请求头，降低被百度风控识别为自动化流量的概率
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_exe_ok_cache = {"sig": None}  # 已验证可用的 exe 的 (mtime, size)，避免每次请求都 spawn 进程
_cleanup_thread_started = False
_share_switch_lock = threading.Lock()  # 串行化“切换分享链接”（rm 旧转存 + 转存新链接 + 更新缓存）


def _ensure_bpcs_exe() -> bool:
    """检查 BaiduPCS-Go.exe 是否存在且可运行。不存在或损坏时返回 False。结果按文件签名缓存。"""
    try:
        st = os.stat(BPCS_PATH)
    except OSError:
        return False
    sig = (st.st_mtime_ns, st.st_size)
    if _exe_ok_cache["sig"] == sig:
        return True
    try:
        r = subprocess.run([BPCS_PATH, "--version"], capture_output=True, text=True, timeout=10)
        ok = r.returncode == 0
    except Exception:
        ok = False
    if ok:  # 只缓存成功结果，失败时下次重试
        _exe_ok_cache["sig"] = sig
    return ok


def _fetch_release_info() -> dict | None:
    """从 GitHub API 获取官方 release 信息（下载 URL 和文件大小来自官方，不硬编码）。"""
    headers = {"User-Agent": "astrbot_plugin_baidu_pan", "Accept": "application/vnd.github+json"}
    try:
        resp = _req.get(BPCS_API_URL, timeout=30, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except _req.exceptions.SSLError:
        import urllib3 as _u3
        _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)
        try:
            resp = _req.get(BPCS_API_URL, timeout=30, verify=False, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"[BaiduPan] 获取 GitHub release 信息失败: {e}")
            return None
    except Exception as e:
        logger.error(f"[BaiduPan] 获取 GitHub release 信息失败: {e}")
        return None


def _download_cn_exe() -> dict:
    """仅尝试从国内镜像直链下载 BaiduPCS-Go.exe。
    返回 {"ok": True} 或 {"error": "..."}。"""
    import hashlib as _hashlib

    def _sha256(data: bytes) -> str:
        h = _hashlib.sha256()
        h.update(data)
        return h.hexdigest()

    def _get(url: str, timeout: int = 300) -> bytes | None:
        headers = {"User-Agent": "astrbot_plugin_baidu_pan"}
        try:
            r = _req.get(url, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r.content
        except _req.exceptions.SSLError:
            import urllib3 as _u3
            _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)
            try:
                r = _req.get(url, timeout=timeout, verify=False, headers=headers)
                r.raise_for_status()
                return r.content
            except Exception:
                return None
        except Exception:
            return None

    logger.info(f"[BaiduPan] 尝试从国内镜像下载 BaiduPCS-Go.exe...")
    data = _get(BPCS_CN_URL, timeout=120)
    if data is None:
        return {"error": "国内镜像下载失败（网络不可达）"}
    if len(data) != BPCS_EXE_SIZE or _sha256(data) != BPCS_EXE_SHA256:
        return {"error": f"国内镜像文件校验失败: size={len(data)}/{BPCS_EXE_SIZE}"}
    try:
        with open(BPCS_PATH, "wb") as f:
            f.write(data)
        logger.info(f"[BaiduPan] 国内镜像下载成功（{len(data)} bytes，SHA256 校验通过）")
        return {"ok": True}
    except Exception as e:
        return {"error": f"写入 exe 失败: {e}"}


def _download_github_exe() -> dict:
    """仅尝试从 GitHub 官方 release 下载 BaiduPCS-Go.exe。
    返回 {"ok": True} 或 {"error": "..."}。"""
    import hashlib as _hashlib, zipfile as _zip, io as _io

    def _sha256(data: bytes) -> str:
        h = _hashlib.sha256()
        h.update(data)
        return h.hexdigest()

    def _get(url: str, timeout: int = 300) -> bytes | None:
        headers = {"User-Agent": "astrbot_plugin_baidu_pan"}
        try:
            r = _req.get(url, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r.content
        except _req.exceptions.SSLError:
            import urllib3 as _u3
            _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)
            try:
                r = _req.get(url, timeout=timeout, verify=False, headers=headers)
                r.raise_for_status()
                return r.content
            except Exception:
                return None
        except Exception:
            return None

    logger.info(f"[BaiduPan] 从 GitHub 官方 release 下载 BaiduPCS-Go {BPCS_VERSION}...")
    info = _fetch_release_info()
    if not info:
        return {"error": "GitHub 官方 release 获取失败，请检查网络后重试"}
    asset = None
    for a in info.get("assets", []):
        if a.get("name") == BPCS_ASSET_NAME:
            asset = a
            break
    if not asset:
        return {"error": f"未在 release {BPCS_VERSION} 中找到 {BPCS_ASSET_NAME}"}
    download_url = asset["browser_download_url"]
    expected_size = asset["size"]
    data = _get(download_url, timeout=300)
    if data is None:
        return {"error": "GitHub 下载失败，请检查网络后重试"}
    if len(data) != expected_size:
        return {"error": f"下载大小不匹配: 官方API={expected_size}, 实际={len(data)}"}
    try:
        with _zip.ZipFile(_io.BytesIO(data)) as zf:
            exe_name = None
            for n in zf.namelist():
                if os.path.basename(n).lower() == "baidupcs-go.exe":
                    exe_name = n
                    break
            if not exe_name:
                for n in zf.namelist():
                    if n.lower().endswith(".exe"):
                        exe_name = n
                        break
            if not exe_name:
                return {"error": "压缩包内未找到 BaiduPCS-Go.exe"}
            exe_data = zf.read(exe_name)
    except Exception as e:
        return {"error": f"解压失败: {e}"}
    if _sha256(exe_data) != BPCS_EXE_SHA256:
        return {"error": "exe SHA256 与官方不一致，文件可能被篡改"}
    try:
        with open(BPCS_PATH, "wb") as f:
            f.write(exe_data)
        logger.info(f"[BaiduPan] GitHub 下载成功（{len(exe_data)} bytes，SHA256 校验通过）")
        return {"ok": True}
    except Exception as e:
        return {"error": f"写入 exe 失败: {e}"}


def _download_bpcs_exe() -> dict:
    """下载 BaiduPCS-Go.exe。
    优先从国内镜像直链下载，失败回退 GitHub 官方 release。
    返回 {"ok": True, "source": "cn"/"github"} 或 {"error": "..."}。"""
    r = _download_cn_exe()
    if "ok" in r:
        r["source"] = "cn"
        return r
    logger.warning("[BaiduPan] 国内镜像下载失败，回退 GitHub")
    r = _download_github_exe()
    if "ok" in r:
        r["source"] = "github"
        return r
    return {"error": "国内镜像和 GitHub 均下载失败，请检查网络后使用 /pan download 重试"}


def _run_bpcs(args: list, timeout: int = 300) -> tuple:
    try:
        r = subprocess.run(
            [BPCS_PATH] + args, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace"
        )
        return r.stdout or "", r.stderr or "", r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1
    except Exception as e:
        return "", str(e), -2


def _init_bpcs() -> bool:
    """检查 BaiduPCS-Go 是否已有本地登录凭证（通过 who 命令）。"""
    global _bpcs_inited
    if _bpcs_inited:
        return True
    with _bpcs_lock:
        if _bpcs_inited:
            return True
        if not _ensure_bpcs_exe():
            logger.warning(f"[BaiduPan] BaiduPCS-Go.exe 不可用，请使用 /pan help 中提供的命令进行下载或查看readme")
            return False
        out, _, code = _run_bpcs(["who"], timeout=15)
        if code == 0 and out and "登录" not in (out or "") and "未登录" not in (out or ""):
            _bpcs_inited = True
            logger.info(f"[BaiduPan] BaiduPCS-Go already logged in: {out.strip()[:100]}")
            return True
        logger.info("[BaiduPan] BaiduPCS-Go not logged in, use /pan login")
        return False


def _bpcs_config_path() -> str:
    """BaiduPCS-Go 的 pcs_config.json 路径（支持 BAIDUPCS_GO_CONFIG_DIR 环境变量）。"""
    env = os.environ.get("BAIDUPCS_GO_CONFIG_DIR")
    if env:
        return os.path.join(env, "pcs_config.json")
    return os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming", "BaiduPCS-Go", "pcs_config.json"
    )


def login_bduss_bpcs(bduss: str, stoken: str = "") -> dict:
    """用 BDUSS + STOKEN 登录 BaiduPCS-Go。"""
    global _bpcs_inited
    if not _ensure_bpcs_exe():
        return {"error": "BaiduPCS-Go.exe 缺失或损坏，请使用 /pan help 中提供的命令进行下载或查看readme"}
    args = ["login", f"-bduss={bduss}"]
    if stoken:
        args.append(f"-stoken={stoken}")
    out, err, code = _run_bpcs(args, timeout=30)
    text = (out or "") + chr(10) + (err or "")
    if code == 0 and "失败" not in text and "错误" not in text:
        _bpcs_inited = True
        logger.info("[BaiduPan] BaiduPCS-Go logged in via /pan bduss")
        return {"success": True}
    m = re.search(r"错误代码:\s*\d+[^\n]{0,60}|[\u4e00-\u9fa5]{2,20}[:：]?\s*[^\n]{0,60}", text)
    if m:
        return {"error": f"登录失败: {m.group(0).strip()}"}
    return {"error": f"登录失败（退出码 {code}）: {text.strip()[:200]}"}


def _patch_config_stoken(stoken: str, bduss: str = ""):
    """手动补写 pcs_config.json 中的 stoken 和 bduss 字段。
    login -cookies= 不会自动填充 stoken 字段，但 transfer 需要它。"""
    config_path = _bpcs_config_path()
    if not os.path.exists(config_path):
        logger.warning(f"[BaiduPan] pcs_config.json not found at {config_path}")
        return
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        user_list = cfg.get("baidu_user_list", [])
        if user_list:
            if stoken:
                user_list[0]["stoken"] = stoken
            if bduss:
                user_list[0]["bduss"] = bduss
            with open(config_path, "w", encoding="utf-8") as f:
                _json.dump(cfg, f, ensure_ascii=False, indent=4)
            logger.info(f"[BaiduPan] patched stoken into config")
    except Exception as e:
        logger.warning(f"[BaiduPan] failed to patch config: {e}")


def login_cookies_bpcs(cookie_str: str) -> dict:
    """用完整 Cookie 字符串登录 BaiduPCS-Go（v4.0.1 支持 -cookies 参数）。"""
    global _bpcs_inited
    if not _ensure_bpcs_exe():
        return {"error": "BaiduPCS-Go.exe 缺失或损坏，请使用 /pan help 中提供的命令进行下载或查看readme"}
    # 清洗：去掉可能干扰的空名项（如 =value）和 *_BFESS 字段
    cleaned = "; ".join(
        part.strip() for part in cookie_str.split(";")
        if part.strip() and "=" in part.strip()
        and "_BFESS" not in part
    )
    out, err, code = _run_bpcs(["login", f"-cookies={cleaned}"], timeout=30)
    text = (out or "") + chr(10) + (err or "")
    if code == 0 and "失败" not in text and "错误" not in text:
        # cookies 登录后 stoken 字段不会自动填充，手动补写
        stoken_m = re.search(r'STOKEN=([^\s;]+)', cleaned)
        bduss_m = re.search(r'BDUSS=([^\s;]+)', cleaned)
        if stoken_m:
            _patch_config_stoken(stoken_m.group(1), bduss_m.group(1) if bduss_m else "")
        _bpcs_inited = True
        logger.info("[BaiduPan] BaiduPCS-Go logged in via cookies")
        return {"success": True}
    m = re.search(r"错误代码:\s*\d+[^\n]{0,60}|[\u4e00-\u9fa5]{2,20}[:：]?\s*[^\n]{0,60}", text)
    if m:
        return {"error": f"登录失败: {m.group(0).strip()}"}
    return {"error": f"登录失败（退出码 {code}）: {text.strip()[:200]}"}


def login_bpcs(username: str, password: str) -> dict:
    """用百度账号密码登录 BaiduPCS-Go，登录成功后凭证保存在本地，全局生效。
    优先走交互式 stdin 输入，避免密码出现在进程命令行（同机其他进程可见）。"""
    global _bpcs_inited
    if not _ensure_bpcs_exe():
        return {"error": "BaiduPCS-Go.exe 缺失或损坏，请使用 /pan help 中提供的命令进行下载或查看readme"}

    # 方式一：交互式输入（密码不进命令行）
    try:
        proc = subprocess.Popen(
            [BPCS_PATH, "login"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        out_bytes, _ = proc.communicate(
            input=f"{username}\n{password}\n".encode("utf-8"), timeout=60
        )
        text = (out_bytes or b"").decode("utf-8", errors="replace")
        if proc.returncode == 0 and "失败" not in text and "错误" not in text:
            _bpcs_inited = True
            logger.info("[BaiduPan] BaiduPCS-Go logged in via /pan login (stdin)")
            return {"success": True, "output": text.strip()[:300]}
        logger.info(f"[BaiduPan] stdin login 未成功，回退参数方式: rc={proc.returncode}")
    except Exception as e:
        logger.warning(f"[BaiduPan] stdin login 异常，回退参数方式: {e}")

    # 方式二：参数式（兜底）
    out, err, code = _run_bpcs(
        ["login", f"-username={username}", f"-password={password}"], timeout=60
    )
    text = (out or "") + chr(10) + (err or "")
    if code == 0 and "失败" not in text and "错误" not in text:
        _bpcs_inited = True
        logger.info("[BaiduPan] BaiduPCS-Go logged in via /pan login")
        return {"success": True, "output": text.strip()[:300]}
    m = re.search(r"[\u4e00-\u9fa5]{2,20}[:：]?\s*[0-9a-zA-Z\u4e00-\u9fa5 ,.()]+", text)
    if m:
        return {"error": f"登录失败: {m.group(0).strip()}"}
    return {"error": f"登录失败（退出码 {code}）: {text.strip()[:200]}"}


def logout_bpcs() -> dict:
    """退出 BaiduPCS-Go 当前登录的百度帐号，清除本地凭证。"""
    global _bpcs_inited
    if not _ensure_bpcs_exe():
        return {"error": "BaiduPCS-Go.exe 缺失或损坏，请使用 /pan help 中提供的命令进行下载或查看readme"}
    out, err, code = _run_bpcs(["logout", "-y"], timeout=30)
    text = (out or "") + chr(10) + (err or "")
    if code == 0:
        _bpcs_inited = False
        logger.info("[BaiduPan] BaiduPCS-Go logged out via /pan unlogin")
        return {"success": True}
    return {"error": f"退出失败（退出码 {code}）: {text.strip()[:200]}"}


def _build_pan_session() -> object:
    """从 pcs_config.json 读取 cookies，构建已登录的 requests.Session。
    自动访问 pan.baidu.com/disk/main 获取 csrfToken / PANPSC 等 pan 专用 cookie。"""
    cfg_path = _bpcs_config_path()
    if not os.path.exists(cfg_path):
        logger.warning("[BaiduPan] pcs_config.json not found, cannot build pan session")
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        user = cfg.get("baidu_user_list", [{}])[0]
        cookies_str = user.get("cookies", "") or user.get("bduss", "")
        if not cookies_str:
            logger.warning("[BaiduPan] no cookies/bduss in config")
            return None
    except Exception as e:
        logger.warning(f"[BaiduPan] failed to read config: {e}")
        return None

    sess = _req.Session()
    sess.headers.update({
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    # 如果只有 BDUSS 没有完整 cookie，用 BDUSS 做一个简单 session
    if "BDUSS=" not in cookies_str and "=" not in cookies_str:
        sess.cookies.set("BDUSS", cookies_str, domain=".baidu.com")
        # 尝试从 stoken 字段补充
        stoken = user.get("stoken", "")
        if stoken:
            sess.cookies.set("STOKEN", stoken, domain=".baidu.com")
    else:
        for part in cookies_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    sess.cookies.set(k, v, domain=".baidu.com")
    # 访问 pan.baidu.com 获取 pan 专用 cookie（csrfToken / PANPSC）
    try:
        sess.get("https://pan.baidu.com/disk/main", timeout=10)
    except Exception:
        pass
    return sess


def _grab_share_field(html: str, key: str) -> str:
    """从分享页 HTML 中提取单个字段值（兼容引号/数值两种形式）。
    不做整页 JSON 解析，避免正则改写 URL/时间等内容导致解析失败。"""
    m = re.search(rf'["\']{re.escape(key)}["\']\s*:\s*["\']?([A-Za-z0-9_\-.]*)', html or "")
    return m.group(1) if m else ""


def _transfer_via_api(surl: str, pwd: str, target_path: str) -> dict:
    """用 Python requests 直接调用百度网盘 API 转存分享链接到指定目录。
    替代 BaiduPCS-Go 的 transfer 命令（v4.0.1 有 cookie 传递 bug）。

    返回: {"success": True, "filenames": [...], "fs_ids": [...]}
          或 {"error": "错误信息"}
    """
    sess = _build_pan_session()
    if sess is None:
        return {"error": "无法获取百度网盘登录凭证，请先使用 /pan qrlogin 登录"}

    share_link = f"https://pan.baidu.com/s/{surl}"

    try:
        # Step 1: 访问分享页，获取 bdstoken / share_uk / shareid
        # 分享页较重且百度风控会拖响应，超时放宽到 30s 并重试一次
        r = None
        for _attempt in (1, 2):
            try:
                r = sess.get(share_link, timeout=30,
                             headers={"Referer": "https://pan.baidu.com/disk/home"})
                break
            except _req.exceptions.RequestException as e:
                if _attempt == 2:
                    if isinstance(e, _req.exceptions.ReadTimeout):
                        # 正常分享页 1s 左右响应；两次都读超时基本是分享已在
                        # 服务端失效（失效分享百度会挂 ~50s 才返回 500）
                        return {"error": "分享页无响应（重试仍超时），该分享链接很可能已失效，请让分享者重新分享"}
                    raise
                logger.warning(f"[BaiduPan] 分享页请求失败，重试: {e}")
                time.sleep(1)
        if _grab_share_field(r.text, "loginstate") == "0":
            return {"error": "网盘未登录，请使用 /pan qrlogin 重新登录"}
        bdstoken = _grab_share_field(r.text, "bdstoken")
        share_uk = _grab_share_field(r.text, "share_uk") or _grab_share_field(r.text, "uk")
        shareid = _grab_share_field(r.text, "shareid")
        if not bdstoken or not shareid:
            return {"error": "无法获取分享信息，链接可能已失效"}
        logger.info(f"[BaiduPan] transfer: shareid={shareid}, bdstoken={bdstoken[:16]}...")

        # Step 2: 验证密码（如果有）
        if pwd:
            verify_url = (
                f"https://pan.baidu.com/share/verify"
                f"?shareid={shareid}&time={int(time.time()*1000)}"
                f"&clienttype=1&uk={share_uk}"
            )
            headers = {
                "Referer": share_link,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }
            r2 = sess.post(verify_url, data={
                "pwd": pwd, "vcode": "null", "vcode_str": "null", "bdstoken": bdstoken
            }, headers=headers, timeout=20)
            resp2 = r2.json()
            if resp2.get("errno") != 0:
                if resp2.get("errno") == -9:
                    return {"error": "提取码错误"}
                return {"error": f"密码验证失败: {resp2.get('errno')}"}

        # Step 3: 重新访问分享页（带 init referer），获取新 bdstoken
        r3 = sess.get(share_link, timeout=30,
                      headers={"Referer": f"https://pan.baidu.com/share/init?surl={surl}"})
        bdstoken = _grab_share_field(r3.text, "bdstoken") or bdstoken

        # Step 4: 获取文件列表（短链一般以 1 开头，list 接口要求去掉）
        short = surl[1:] if surl.startswith("1") else surl
        list_url = (
            f"https://pan.baidu.com/share/list"
            f"?bdstoken={bdstoken}&root=1&web=5&app_id=250528"
            f"&shorturl={short}&channel=chunlei&clienttype=0"
        )
        r4 = sess.get(list_url, timeout=30, headers={"Referer": share_link})
        resp4 = r4.json()
        if resp4.get("errno") != 0:
            return {"error": f"获取文件列表失败: {resp4.get('errno')}"}
        files = resp4.get("list", [])
        if not files:
            return {"error": "分享链接中没有文件"}
        fs_ids = [str(f.get("fs_id")) for f in files]
        filenames = [f.get("server_filename") for f in files]
        logger.info(f"[BaiduPan] transfer files: {filenames}")

        # Step 5: 执行转存
        transfer_url = (
            f"https://pan.baidu.com/share/transfer"
            f"?app_id=250528&channel=chunlei&clienttype=0&web=1"
            f"&bdstoken={bdstoken}&shareid={shareid}&from={share_uk}"
        )
        transfer_data = {
            "fsidlist": "[" + ",".join(fs_ids) + "]",
            "path": target_path,
        }
        r5 = sess.post(transfer_url, data=transfer_data,
                       headers={"Referer": share_link,
                                "Content-Type": "application/x-www-form-urlencoded"},
                       timeout=30)
        resp5 = r5.json()
        errno = resp5.get("errno", -1)
        if errno == 0:
            logger.info(f"[BaiduPan] transfer success: {filenames}")
            return {"success": True, "filenames": filenames, "fs_ids": fs_ids}
        elif errno in (2, 4):
            # errno=2: 自己的分享链接无法重复转存
            # errno=4: 文件已转存过，目录里已有这些文件
            # 两种情况文件都在云端，可以正常列出和下载
            own = (errno == 2)
            logger.info(f"[BaiduPan] transfer errno={errno} (already exists), filenames={filenames}")
            return {"success": True, "filenames": filenames, "fs_ids": fs_ids, "_own_share": own}
        elif errno == 12:
            # 文件冲突，检查具体错误
            info = resp5.get("info", [])
            conflict_msg = "文件冲突"
            if info:
                for item in info:
                    e = item.get("errno", 0)
                    if e == -30:
                        conflict_msg = "目标目录下已有同名文件"
            return {"error": f"转存失败: {conflict_msg}"}
        else:
            show_msg = resp5.get("show_msg", "") or resp5.get("err_msg", "")
            return {"error": f"转存失败(errno={errno}): {show_msg}"}

    except Exception as e:
        logger.exception(f"[BaiduPan] transfer API error: {e}")
        return {"error": f"转存API调用异常: {e}"}


def _manifest_load() -> set:
    """读取本插件下载过的文件清单。"""
    try:
        with open(_MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return set(data.get("files", [])) if isinstance(data, dict) else set(data)
    except Exception:
        return set()


def _manifest_save(paths: set):
    try:
        os.makedirs(os.path.dirname(_MANIFEST_PATH), exist_ok=True)
        with open(_MANIFEST_PATH, "w", encoding="utf-8") as f:
            _json.dump({"files": sorted(paths)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[BaiduPan] save cleanup manifest failed: {e}")


def _manifest_add(paths: list):
    """登记插件下载到本地的文件，供定时清理使用。"""
    new = {p for p in paths if p}
    if not new:
        return
    _manifest_save(_manifest_load() | new)


def _cleanup_local_dir():
    """清理本插件下载并登记过的本地文件（不会动下载目录里的其他文件）。"""
    paths = _manifest_load()
    if not paths:
        return
    removed = 0
    remain = set()
    for p in paths:
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
            elif os.path.exists(p):
                remain.add(p)  # 目录等非普通文件，保留不动
        except Exception as e:
            logger.warning(f"[BaiduPan] cleanup skip {p}: {e}")
            remain.add(p)
    _manifest_save(remain)
    if removed:
        logger.info(f"[BaiduPan] local cleanup: removed {removed} plugin-downloaded files")


def _schedule_local_cleanup(hour: int):
    """安排每天定时清理本地下载文件（仅清理插件自己下载的文件）。"""
    global _cleanup_thread_started
    if hour < 0 or hour > 23:
        logger.info("[BaiduPan] local auto-cleanup disabled")
        return
    if _cleanup_thread_started:
        logger.info("[BaiduPan] local cleanup thread already running, skip")
        return
    _cleanup_thread_started = True

    def _worker():
        while True:
            now = time.localtime()
            target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                  hour, 0, 0, 0, 0, -1))
            if target <= time.time():
                target += 86400  # 明天
            time.sleep(max(target - time.time(), 1))
            _cleanup_local_dir()
            time.sleep(60)

    threading.Thread(target=_worker, daemon=True, name="baidupan-cleanup").start()
    logger.info(f"[BaiduPan] scheduled local cleanup at {hour}:00 daily")


def parse_share_link(link: str) -> tuple:
    link = link.strip().replace("\\", "")
    if "baidu.com/link" in link or "url=" in link:
        link = urllib.parse.unquote(link)
    surl = ""
    pwd = ""
    m = re.search(r'pan\.baidu\.com/s/([A-Za-z0-9_-]+)', link)
    if m:
        surl = m.group(1)
    m = re.search(r'baidu\.com/s/([A-Za-z0-9_-]+)', link)
    if m and not surl:
        surl = m.group(1)
    m = re.search(r'(?:pwd|password|提取码)[:\s=]*([A-Za-z0-9]{4,6})', link, re.IGNORECASE)
    if m:
        pwd = m.group(1).strip()
    return surl, pwd


def _parse_size_bytes(raw_size: str) -> int:
    """解析 BPCS ls 输出的大小字符串（如 1.5GB / 300KB / -）为字节数，失败返回 0。"""
    raw = (raw_size or "").strip().upper()
    if not raw or raw == "-":
        return 0
    try:
        for unit, mult in (("TB", 1024 ** 4), ("GB", 1024 ** 3),
                           ("MB", 1024 ** 2), ("KB", 1024)):
            if raw.endswith(unit):
                return int(float(raw[:-len(unit)]) * mult)
        if raw.endswith("B"):
            return int(float(raw[:-1]))
        return int(float(raw))
    except ValueError:
        return 0


def _format_size(raw_size: str) -> str:
    """解析 BPCS ls 输出的文件大小字符串并格式化。"""
    b = _parse_size_bytes(raw_size)
    if b <= 0:
        return raw_size
    for unit, threshold in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if b >= threshold:
            return f"{b / threshold:.2f} {unit}"
    return f"{b:.0f} B"


# BPCS ls -l 行结构: 序号 FSID APPID 大小 创建日期 时间 修改日期 时间 [MD5] 文件名(可含空格)
_BPCS_LS_RE = re.compile(
    r'^\s*\d+\s+\d+\s+\d+\s+(\S+)\s+'
    r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+'
    r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+'
    r'(?:[0-9a-fA-F]{32}\s+)?(.+?)\s*$'
)


def _parse_ls_line(line: str):
    """解析 BPCS ls -l 的一行文件记录。
    返回 (name, size_raw, is_dir)，非文件行返回 None。
    按日期列定位文件名起点，支持文件名含空格。"""
    m = _BPCS_LS_RE.match(line)
    if not m:
        return None
    size_raw, name = m.group(1), m.group(2)
    is_dir = size_raw == "-" or name.endswith("/")
    name = name.rstrip("/")
    if not name:
        return None
    return name, size_raw, is_dir


def list_share_content(surl: str, pwd: str = "") -> dict:
    """转存分享链接并列出目录结构，返回格式化后的文本和文件列表。"""
    cloud_dir = CLOUD_SAVE_DIR or "/我的资源/AutoTransfer"
    result = _transfer_via_api(surl, pwd, cloud_dir)
    if "error" in result:
        return {"error": result["error"]}

    own_share = result.get("_own_share", False)
    filenames = result.get("filenames", [])

    lines = []
    all_items = []  # [(path, name, size, is_dir)]

    if own_share:
        lines.append("⚠️ 这是你自己的分享链接，API 无法重复转存")
        lines.append(f"   分享中的文件: {', '.join(filenames)}")
        lines.append("   如需下载，请直接从网盘原目录操作")
        return {"text": "\n".join(lines), "items": [], "own_share": True}

    # 递归列出目录内容
    def _list_dir(dir_path: str, indent: int = 0):
        if indent > 10:  # 防御过深的目录嵌套
            return
        out, _, code = _run_bpcs(["ls", "-l", dir_path], timeout=60)
        if code != 0:
            return
        prefix = "  " * indent
        for line in out.split("\n"):
            parsed = _parse_ls_line(line)
            if not parsed:
                continue
            name, size_raw, is_dir = parsed
            if is_dir:
                lines.append(f"{prefix}📁 {name}/")
                all_items.append({"path": f"{dir_path}/{name}", "name": name, "size": "-", "is_dir": True})
                _list_dir(f"{dir_path}/{name}", indent + 1)
            else:
                lines.append(f"{prefix}📄 {name}  ({_format_size(size_raw)})")
                all_items.append({"path": f"{dir_path}/{name}", "name": name, "size": size_raw, "is_dir": False})

    # 获取顶层目录内容，不展示顶层目录名，子目录各自成树，空行分隔
    out, _, code = _run_bpcs(["ls", "-l", cloud_dir], timeout=60)
    if code != 0:
        return {"error": "获取目录列表失败"}

    top_items = []
    for line in out.split("\n"):
        parsed = _parse_ls_line(line)
        if parsed:
            top_items.append(parsed)

    tree_count = 0
    for name, size_raw, is_dir in top_items:
        if tree_count > 0:
            lines.append("")  # 空行分隔树
        if is_dir:
            lines.append(f"📁 {name}/")
            all_items.append({"path": f"{cloud_dir}/{name}", "name": name, "size": "-", "is_dir": True})
            _list_dir(f"{cloud_dir}/{name}", 1)
        else:
            lines.append(f"📄 {name}  ({_format_size(size_raw)})")
            all_items.append({"path": f"{cloud_dir}/{name}", "name": name, "size": size_raw, "is_dir": False})
        tree_count += 1

    if not lines:
        return {"error": "目录为空"}

    return {"text": "\n".join(lines), "items": all_items, "own_share": False}


def download_from_cloud(cloud_path: str, max_mb: int = 0, progress_queue: "queue.Queue" = None) -> dict:
    """从网盘下载指定路径的文件或文件夹（不转存，直接下载已有文件）。

    progress_queue: 可选，传入 queue.Queue 后，下载过程中会向队列放入进度信息。
    """
    cloud_dir = CLOUD_SAVE_DIR or "/我的资源/AutoTransfer"
    if cloud_path:
        full_path = f"{cloud_dir}/{cloud_path.strip('/')}"
        file_name = full_path.rstrip("/").split("/")[-1]
    else:
        full_path = cloud_dir
        file_name = "全部文件"

    logger.info(f"[BaiduPan] download_from_cloud: cloud_path={cloud_path}, full_path={full_path}")

    is_dir = not cloud_path
    file_size = 0
    if cloud_path:
        # ls 查父目录判断文件/文件夹（bpcs ls -l 查文件本身不显示信息）
        norm = full_path.rstrip("/")
        parent = norm.rsplit("/", 1)[0] if "/" in norm else "/"
        out, _, code = _run_bpcs(["ls", "-l", parent], timeout=60)
        if code != 0 or "错误" in (out or "") or "不存在" in (out or ""):
            return {"error": f"路径不存在: {cloud_path}"}
        found = None
        for line in out.split("\n"):
            parsed = _parse_ls_line(line)
            if parsed and parsed[0] == file_name:
                found = parsed
                break
        if not found:
            return {"error": f"云端路径不存在: {cloud_path}"}
        is_dir = found[2]
        file_size = 0 if is_dir else _parse_size_bytes(found[1])
    logger.info(f"[BaiduPan] download_from_cloud: is_dir={is_dir}, file_size={file_size} ({file_size/1024/1024:.1f}MB)")

    # 大小限制（仅单文件）
    max_bytes = max_mb * 1024 * 1024
    if not is_dir and max_mb > 0 and file_size > max_bytes:
        return {"error": f"文件过大 ({file_size//1024//1024}MB) 超过 {max_mb}MB 限制"}

    # 下载前先检查本地是否已有该文件（避免 BPCS 卡在重复下载）
    if not is_dir:
        account_folder = _get_account_folder()
        check_paths = []
        if account_folder:
            check_paths.append(os.path.join(DOWNLOAD_DIR, account_folder, file_name))
        check_paths.append(os.path.join(DOWNLOAD_DIR, file_name))
        for p in check_paths:
            if os.path.exists(p):
                logger.info(f"[BaiduPan] file already exists locally: {p}")
                return {"path": p, "name": file_name, "size": os.path.getsize(p), "is_dir": False}

    # 下载（使用 subprocess.Popen 实时读输出，避免 BPCS 卡住）
    _run_bpcs(["config", "set", "-savedir", DOWNLOAD_DIR], timeout=15)
    if DOWNLOAD_TIMEOUT_SECONDS > 0:
        dl_timeout = DOWNLOAD_TIMEOUT_SECONDS
    else:
        dl_timeout = max(600, int(file_size / (1024 * 1024) * 15)) if not is_dir else 3600
    logger.info(f"[BaiduPan] download_from_cloud: starting download, dl_timeout={dl_timeout}s, progress_queue={progress_queue is not None}")
    # 把文件信息塞进队列，给 handler 监控文件大小用
    # 如果 file_size 为0但文件非目录，把 0 也塞进去，让 handler 监控时用实际文件大小
    if progress_queue is not None and not is_dir:
        progress_queue.put(("_info", file_name, file_size))
    started_at = time.time()
    try:
        proc = subprocess.Popen(
            [BPCS_PATH, "download", full_path, "--ow"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE
        )
        try:
            proc.stdin.write(b"y\n")
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # 进程可能已退出，communicate 会给出结果
        if dl_timeout > 0:
            out = proc.communicate(timeout=dl_timeout)[0]
        else:
            out = proc.communicate()[0]
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        code = proc.returncode
    except subprocess.TimeoutExpired:
        logger.error(f"[BaiduPan] download timeout ({dl_timeout}s), killing process")
        # 超时后必须 kill，否则 BPCS 会继续在后台下载
        proc.kill()
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        if progress_queue is not None:
            progress_queue.put(("_error", "下载超时"))
        return {"error": "下载超时"}
    except Exception as e:
        logger.error(f"[BaiduPan] download subprocess error: {e}")
        return {"error": f"下载进程异常: {e}"}
    if code != 0 or "失败" in (out or "") or "错误" in (out or ""):
        logger.error(f"[BaiduPan] download failed: code={code}, out={(out or '')[:200]}")
        if progress_queue is not None:
            progress_queue.put(("_error", f"下载失败 (code={code})"))
        return {"error": "下载失败"}
    logger.info(f"[BaiduPan] download_from_cloud: download completed, code={code}")

    if not DOWNLOAD_DIR:
        return {"error": "下载目录未设置"}

    logger.info(f"[BaiduPan] download_from_cloud: searching for downloaded file, DOWNLOAD_DIR={DOWNLOAD_DIR}")
    if is_dir:
        downloaded = []

        def _collect(only_recent: bool):
            found_files = []
            for root, _, files in os.walk(DOWNLOAD_DIR):
                for f in files:
                    fp = os.path.join(root, f)
                    if only_recent:
                        try:
                            if os.path.getctime(fp) < started_at - 120:
                                continue  # 只收本次下载窗口内新建的文件
                        except OSError:
                            continue
                    found_files.append({"path": fp, "name": f, "size": os.path.getsize(fp)})
            return found_files

        downloaded = _collect(only_recent=True)
        if not downloaded:
            # 兜底：时间过滤没匹配到时退回收集全部
            downloaded = _collect(only_recent=False)
        if not downloaded:
            return {"error": "文件夹下载完成但未找到文件"}
        _manifest_add([d["path"] for d in downloaded])
        if _auto_delete_cloud:
            _run_bpcs(["rm", full_path], timeout=30)
        return {"path": DOWNLOAD_DIR, "name": file_name, "size": 0, "is_dir": True, "files": downloaded}
    else:
        # 先找账号专属文件夹（BPCS 会按 uid_用户名 创建子目录）
        account_folder = _get_account_folder()
        search_dirs = []
        if account_folder:
            search_dirs.append(os.path.join(DOWNLOAD_DIR, account_folder, file_name))
        search_dirs.append(os.path.join(DOWNLOAD_DIR, file_name))
        local_path = None
        for p in search_dirs:
            if os.path.exists(p):
                local_path = p
                break
        if not local_path:
            for root, _, files in os.walk(DOWNLOAD_DIR):
                if file_name in files:
                    local_path = os.path.join(root, file_name)
                    break
        if not local_path or not os.path.exists(local_path):
            return {"error": "下载完成但未找到文件"}
        _manifest_add([local_path])
        if _auto_delete_cloud:
            _run_bpcs(["rm", full_path], timeout=30)
        return {"path": local_path, "size": os.path.getsize(local_path), "name": file_name}


def _get_account_folder() -> str:
    """从 BPCS 配置读取当前登录账号的 uid 和 name，返回账号文件夹名（如 620186943_hitomi999）。"""
    cfg_path = _bpcs_config_path()
    if not os.path.exists(cfg_path):
        return ""
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        user = cfg.get("baidu_user_list", [{}])[0]
        uid = user.get("uid", "")
        name = user.get("name", "")
        if uid and name:
            return f"{uid}_{name}"
    except Exception:
        pass
    return ""


def _get_local_file_size(file_name: str) -> int:
    """在 DOWNLOAD_DIR 下查找文件并返回大小，找不到返回 0。"""
    account_folder = _get_account_folder()
    check_paths = []
    if account_folder:
        check_paths.append(os.path.join(DOWNLOAD_DIR, account_folder, file_name))
    check_paths.append(os.path.join(DOWNLOAD_DIR, file_name))
    for p in check_paths:
        if os.path.exists(p):
            try:
                return os.path.getsize(p)
            except OSError:
                return 0
    return 0


@register(
    "astrbot_plugin_baidu_pan",
    "linker9527",
    "百度网盘分享文件自动下载发送",
    "1.7.0",
    "https://github.com/linker9527/astrbot_plugin_baidu_pan",
)
class BaiduPanPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        global DOWNLOAD_DIR
        custom_dir = str(self.config.get("download_dir", "")).strip()
        if custom_dir:
            DOWNLOAD_DIR = os.path.abspath(custom_dir)
        else:
            DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "storage", "downloads")
        try:
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        except Exception as e:
            logger.warning(f"[BaiduPan] 配置的 download_dir ({DOWNLOAD_DIR}) 不可用 ({e})，回退到默认路径")
            DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "storage", "downloads")
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        logger.info(f"[BaiduPan] download dir: {DOWNLOAD_DIR}")

        global CLOUD_SAVE_DIR
        cloud_dir = str(self.config.get("cloud_save_dir", "")).strip()
        if cloud_dir:
            CLOUD_SAVE_DIR = cloud_dir.rstrip("/")
        else:
            CLOUD_SAVE_DIR = "/我的资源/AutoTransfer"
        logger.info(f"[BaiduPan] cloud save dir: {CLOUD_SAVE_DIR}")

        # 缓存：链接、密码、目录树、文件列表
        self._cached_surl = ""
        self._cached_pwd = ""
        self._cached_tree = ""
        self._cached_items = []
        # 下载去重锁（同一用户同一命令/工具参数只允许一个下载任务）
        self._active_downloads = set()

        # 设置 BaiduPCS-Go 下载目录（exe 可用时才设置）
        if _ensure_bpcs_exe():
            _run_bpcs(["config", "set", "-savedir", DOWNLOAD_DIR], timeout=15)
        else:
            logger.warning("[BaiduPan] BaiduPCS-Go.exe 不可用，请使用 /pan help 中提供的命令进行下载或查看readme，配置将在下载后生效")

        # 解析黑名单
        bl = str(self.config.get("blacklist", "")).strip()
        self._blacklist = set()
        if bl:
            for uid in re.split(r"[,，\s]+", bl):
                uid = uid.strip()
                if uid:
                    self._blacklist.add(uid)
        if self._blacklist:
            logger.info(f"[BaiduPan] blacklist: {self._blacklist}")

        # 读取下载后自动删除网盘文件开关
        global _auto_delete_cloud
        _auto_delete_cloud = bool(self.config.get("auto_delete_cloud", True))
        logger.info(f"[BaiduPan] auto_delete_cloud: {_auto_delete_cloud}")

        # 进度显示配置
        self._progress_enabled = bool(self.config.get("progress_enabled", False))
        self._progress_interval = int(self.config.get("progress_interval", 30))
        if self._progress_enabled:
            logger.info(f"[BaiduPan] progress reporting enabled, interval={self._progress_interval}s")

        # 下载超时（秒），-1 = 不超时
        global DOWNLOAD_TIMEOUT_SECONDS
        DOWNLOAD_TIMEOUT_SECONDS = int(self.config.get("timeout_seconds", -1))
        logger.info(f"[BaiduPan] download timeout: {DOWNLOAD_TIMEOUT_SECONDS}s")

        # 读取每天定时清理本地文件的小时数
        global _local_cleanup_hour
        cleanup_hour = int(self.config.get("local_cleanup_hour", 3))
        _local_cleanup_hour = cleanup_hour
        _schedule_local_cleanup(cleanup_hour)

        threading.Thread(target=_init_bpcs, daemon=True).start()

        # 可选：配置了 BDUSS 则启动时自动登录
        bduss_val = str(self.config.get("bduuss", "")).strip()
        if bduss_val:
            threading.Thread(target=self._auto_login_bduss, args=(bduss_val,), daemon=True).start()

    def _auto_login_bduss(self, bduss: str):
        time.sleep(2)  # 等待 _init_bpcs 先跑完
        result = login_bduss_bpcs(bduss, "")
        if "error" in result:
            logger.warning(f"[BaiduPan] bduuss 配置自动登录失败: {result['error']}")
        else:
            logger.info("[BaiduPan] bduuss 配置自动登录成功")

    def _transfer_and_list(self, surl: str, pwd: str) -> dict:
        """线程安全的"切换分享链接"：清理旧转存 + 转存并列目录 + 更新缓存。
        锁在同步函数内，配合 asyncio.to_thread 调用不会阻塞事件循环。"""
        cloud_dir = CLOUD_SAVE_DIR or "/我的资源/AutoTransfer"
        with _share_switch_lock:
            if self._cached_surl and self._cached_surl != surl:
                _run_bpcs(["rm", cloud_dir], timeout=30)
            result = list_share_content(surl, pwd)
            if "error" not in result:
                self._cached_surl = surl
                self._cached_pwd = pwd
                self._cached_tree = result.get("text", "")
                self._cached_items = result.get("items", [])
        return result

    def _check_switch_allowed(self, surl: str) -> str:
        """切换到新链接前检查是否有下载正在进行（避免删掉正在下载的转存文件）。"""
        if surl and self._cached_surl != surl and self._active_downloads:
            return "⏳ 当前有下载任务进行中，请稍后再查看其他链接"
        return ""

    @staticmethod
    def _normalize_link(link: str, pwd: str) -> tuple:
        """把用户输入的链接/surl 规范化为 (surl, pwd)。支持：
        完整链接(含?pwd=)、裸surl、裸surl + 提取码(空格分隔或"提取码:"前缀)。"""
        link = (link or "").strip()
        if link.startswith(("http", "pan.baidu.com", "yun.baidu.com")):
            if not link.startswith("http"):
                link = "https://" + link
            surl, p2 = parse_share_link(link)
            return surl, (p2 or pwd)
        # 裸 surl：取第一段，剩余部分尝试提取密码
        seg = link.split(None, 1)
        surl = seg[0]
        rest = seg[1].strip() if len(seg) > 1 else ""
        if not pwd and rest:
            m = re.search(r'(?:pwd|password|提取码)[:\s=]*([A-Za-z0-9]{4,6})', rest, re.IGNORECASE)
            if m:
                pwd = m.group(1)
            else:
                first = rest.split()[0]
                if re.fullmatch(r'[A-Za-z0-9]{4,6}', first):
                    pwd = first
        return surl, pwd

    @staticmethod
    async def _interruptible_sleep(fut: asyncio.Future, seconds: int):
        """分段休眠，长间隔下也能及时感知下载完成。"""
        for _ in range(max(1, int(seconds))):
            if fut.done():
                return
            await asyncio.sleep(1)

    def _is_blacklisted(self, event: AstrMessageEvent) -> bool:
        if self._get_send_mode() != "onebot":
            return False  # 黑名单仅 OneBot 模式生效
        sid = event.get_sender_id()
        return sid and sid in self._blacklist

    def _get_max_mb(self) -> int:
        # 0 = 不限制；official 模式下硬限 200MB（发了也发不了）
        cap = int(self.config.get("max_file_size", 200))
        if self._get_send_mode() == "official":
            cap = min(cap, FLASH_TASK_LIMIT_MB) if cap > 0 else FLASH_TASK_LIMIT_MB
        return cap

    def _get_send_mode(self) -> str:
        return str(self.config.get("send_mode", "onebot")).strip().lower()

    async def _send_file(self, event: AstrMessageEvent, dl: dict, share_url: str = "") -> bool:
        """根据平台类型和文件大小选择发送方式。"""
        file_path = dl["path"]
        file_size = dl["size"]
        size_mb = file_size / (1024 * 1024)
        name = dl["name"]
        logger.info(f"[BaiduPan] _send_file: name={name}, size={size_mb:.1f}MB, share_url={share_url[:50] if share_url else 'None'}")

        # 自动检测平台，优先于配置
        platform = getattr(event.platform_meta, "name", "") if event.platform_meta else ""
        platform_id = getattr(event, "platform_identifier", "") or ""
        logger.info(f"[BaiduPan] _send_file: platform={platform}, platform_id={platform_id}")
        if platform == "qq_official" or "qq_official" in platform_id or "qq_official" in platform:
            logger.info(f"[BaiduPan] _send_file: detected qq_official, using _send_official")
            return await self._send_official(event, file_path, name, size_mb, share_url)

        send_mode = self._get_send_mode()
        logger.info(f"[BaiduPan] _send_file: send_mode={send_mode}")
        if send_mode == "official":
            return await self._send_official(event, file_path, name, size_mb, share_url)
        elif send_mode == "other":
            return await self._send_other(event, file_path, name, size_mb, share_url)
        else:  # onebot 及默认
            return await self._send_onebot(event, file_path, name, size_mb, share_url)

    async def _send_onebot(self, event: AstrMessageEvent, file_path: str, name: str, size_mb: float, share_url: str = "") -> bool:
        """OneBot / napcat 路径：直接发送文件，失败则发送链接"""
        logger.info(f"[BaiduPan] _send_onebot: name={name}, size={size_mb:.1f}MB")
        try:
            await event.send(MessageChain(chain=[File(name=name, file=file_path)]))
            logger.info(f"[BaiduPan] _send_onebot: direct send success")
            return True
        except Exception as e2:
            logger.warning(f"[BaiduPan] _send_onebot: direct send failed: {e2}")
            # 兜底：发送链接
            msg = f"⚠️ 文件 {size_mb:.1f}MB 发送失败"
            if share_url:
                msg += f"。请自行下载: {share_url}"
            try:
                await event.send(MessageChain(chain=[Plain(msg)]))
            except Exception:
                pass
            return False

    async def _send_other(self, event: AstrMessageEvent, file_path: str, name: str, size_mb: float, share_url: str = "") -> bool:
        """其他平台路径：直接发送文件，失败则发送链接"""
        try:
            await event.send(MessageChain(chain=[File(name=name, file=file_path)]))
            return True
        except Exception as e:
            logger.warning(f"[BaiduPan] send file failed: {e}")
            msg = "⚠️ 文件发送失败"
            if share_url:
                msg += f"，请自行下载: {share_url}"
            try:
                await event.send(MessageChain(chain=[Plain(msg)]))
            except Exception:
                pass
            return False

    async def _send_official(self, event: AstrMessageEvent, file_path: str, name: str, size_mb: float, share_url: str = "") -> bool:
        """QQ官方机器人路径：<=200MB 走官方API上传，>200MB 只能返回链接"""
        logger.info(f"[BaiduPan] _send_official: name={name}, size={size_mb:.1f}MB, limit={FLASH_TASK_LIMIT_MB}MB")
        if size_mb <= FLASH_TASK_LIMIT_MB:
            logger.info(f"[BaiduPan] _send_official: file <= limit, sending directly")
            try:
                await event.send(MessageChain(chain=[File(name=name, file=file_path)]))
                return True
            except Exception as e:
                logger.warning(f"[BaiduPan] _send_official: send failed: {e}")
                return False

        logger.info(f"[BaiduPan] _send_official: file > limit, returning link")
        msg = "⚠️ 文件超过200MB，QQ官方API暂不支持大文件上传"
        if share_url:
            msg += f"，请自行下载: {share_url}"
        try:
            await event.send(MessageChain(chain=[Plain(msg)]))
        except Exception:
            pass
        return False

    @filter.llm_tool(name="pan_list")
    async def pan_list(self, event: AstrMessageEvent, link: str, pwd: str = ""):
        """查看百度网盘分享链接的目录结构。用户说"查一下这个百度网盘链接"、"看看里面有什么文件"时调用。

        Args:
            link(string): 百度网盘分享链接，如 https://pan.baidu.com/s/1abc123
            pwd(string): 提取码（可选）
        """
        if self._is_blacklisted(event):
            return "你没有权限使用该功能。"
        surl, pwd = self._normalize_link(link, pwd)

        if not surl:
            return "无法解析链接"

        blocked = self._check_switch_allowed(surl)
        if blocked:
            return blocked

        result = await asyncio.to_thread(self._transfer_and_list, surl, pwd)
        if "error" in result:
            return result["error"]
        return result["text"]

    @filter.llm_tool(name="pan_download_dir")
    async def pan_download_dir(self, event: AstrMessageEvent, folder_path: str, link: str = "", pwd: str = ""):
        """下载百度网盘分享中的文件夹（含所有内容）。用户说"下载xxx文件夹"、"下载xxx里面的xxx文件夹"时调用。如果之前已查看过目录树，LLM应从树中找到完整路径。下载完成后，文件保存在本地目录：{DOWNLOAD_DIR}/<账号uid_用户名>/<文件夹名>/...，回复用户时把实际保存路径一起告诉用户。

        Args:
            folder_path(string): 文件夹路径，如 "大气层包" 或 "大气层包/子文件夹"，不含顶层目录名
            link(string): 百度网盘分享链接（可选，如未查看过目录则需提供）
            pwd(string): 提取码（可选）
        """
        if self._is_blacklisted(event):
            return "你没有权限使用该功能。"

        surl = ""
        if link:
            surl, pwd = self._normalize_link(link, pwd)

        if surl:
            blocked = self._check_switch_allowed(surl)
            if blocked:
                return blocked
            result = await asyncio.to_thread(self._transfer_and_list, surl, pwd)
            if "error" in result:
                return result["error"]
        elif not self._cached_surl:
            return "请先查看目录: /pan <链接> [密码]"

        dl_key = f"tool:pan_download_dir:{event.get_sender_id()}:{folder_path}"
        if dl_key in self._active_downloads:
            logger.warning(f"[BaiduPan] pan_download_dir: duplicate blocked: {dl_key}")
            return "⏳ 该文件正在下载中，请稍候..."
        self._active_downloads.add(dl_key)
        try:
            prog_q = queue.Queue() if self._progress_enabled else None
            dl_future = asyncio.create_task(asyncio.to_thread(download_from_cloud, folder_path, self._get_max_mb(), prog_q))
            if prog_q:
                file_info = None
                _last_prog_size = None
                _last_prog_time = None
                while not dl_future.done():
                    try:
                        msg = prog_q.get_nowait()
                        if isinstance(msg, tuple) and msg[0] == "_info":
                            file_info = (msg[1], msg[2])
                        elif isinstance(msg, tuple) and msg[0] == "_error":
                            await event.send(event.plain_result(f"❌ {msg[1]}"))
                        elif isinstance(msg, str):
                            await event.send(event.plain_result(msg))
                    except queue.Empty:
                        pass
                    if file_info and not dl_future.done():
                        fname, total = file_info
                        current = _get_local_file_size(fname)
                        if current > 0 and total > 0:
                            pct = min(current / total * 100, 99.9)
                            await event.send(event.plain_result(f"⏬ 下载中: {pct:.1f}%"))
                    await self._interruptible_sleep(dl_future, self._progress_interval)
            dl = await dl_future
            if "error" in dl:
                return dl["error"]

            if dl.get("is_dir"):
                file_count = len(dl.get("files", []))
                await event.send(event.plain_result(f"✅ 文件夹 '{folder_path}' 下载完成，共 {file_count} 个文件"))
                await event.send(event.plain_result(f"📁 保存路径: {dl['path']}"))
                return f"文件夹 '{folder_path}' 下载完成，共 {file_count} 个文件，保存在 {dl['path']}"
            else:
                share_url = f"https://pan.baidu.com/s/{self._cached_surl}"
                sent = await self._send_file(event, dl, share_url)
                if sent:
                    return f"文件 '{folder_path}' 下载并发送完成"
                return f"文件 '{folder_path}' 已下载到 {dl['path']}，但发送失败"
        finally:
            self._active_downloads.discard(dl_key)

    @filter.llm_tool(name="pan_download_all")
    async def pan_download_all(self, event: AstrMessageEvent, link: str = "", pwd: str = ""):
        """下载百度网盘分享中的所有文件。用户说"全部下载"、"下载所有文件"、"都下载"时调用。下载完成后，文件保存在本地目录：{DOWNLOAD_DIR}/<账号uid_用户名>/<文件名>（如 E:\\downloads\\620186943_hitomi999\\xxx.zip），回复用户时把实际保存路径一起告诉用户。

        Args:
            link(string): 百度网盘分享链接（可选，如未查看过目录则需提供）
            pwd(string): 提取码（可选）
        """
        if self._is_blacklisted(event):
            return "你没有权限使用该功能。"

        surl = ""
        if link:
            surl, pwd = self._normalize_link(link, pwd)

        if surl:
            blocked = self._check_switch_allowed(surl)
            if blocked:
                return blocked
            result = await asyncio.to_thread(self._transfer_and_list, surl, pwd)
            if "error" in result:
                return result["error"]
        elif not self._cached_surl:
            return "请先查看目录: /pan <链接> [密码]"

        dl_key = f"tool:pan_download_all:{event.get_sender_id()}"
        if dl_key in self._active_downloads:
            logger.warning(f"[BaiduPan] pan_download_all: duplicate blocked: {dl_key}")
            return "⏳ 该文件正在下载中，请稍候..."
        self._active_downloads.add(dl_key)
        try:
            # 下载整个转存目录
            prog_q = queue.Queue() if self._progress_enabled else None
            dl_future = asyncio.create_task(asyncio.to_thread(download_from_cloud, "", self._get_max_mb(), prog_q))
            if prog_q:
                while not dl_future.done():
                    try:
                        msg = prog_q.get_nowait()
                        if isinstance(msg, tuple) and msg[0] == "_error":
                            await event.send(event.plain_result(f"❌ {msg[1]}"))
                        elif isinstance(msg, str):
                            await event.send(event.plain_result(msg))
                    except queue.Empty:
                        pass
                    await self._interruptible_sleep(dl_future, self._progress_interval)
            dl = await dl_future
            if "error" in dl:
                return dl["error"]

            file_count = len(dl.get("files", []))
            await event.send(event.plain_result(f"✅ 全部文件下载完成，共 {file_count} 个文件"))
            await event.send(event.plain_result(f"📁 保存路径: {dl['path']}"))
            return f"全部文件下载完成，共 {file_count} 个文件，保存在 {dl['path']}"
        finally:
            self._active_downloads.discard(dl_key)

    async def _login_flow(self, event: AstrMessageEvent, username: str, password: str):
        """/pan login 子命令：账密登录，全局生效"""
        if not event.is_private_chat():
            yield event.plain_result("⚠️ 密码会出现在聊天记录中，建议私聊执行以防泄露")
        yield event.plain_result("⏳ 正在登录百度网盘...")
        result = await asyncio.to_thread(login_bpcs, username, password)
        if "error" in result:
            yield event.plain_result(f"❌ {result['error']}")
            return
        yield event.plain_result("✅ 登录成功！BaiduPCS-Go 已保存登录凭证，全局生效。")
        yield event.plain_result("🔐 密码已出现在聊天记录中，建议撤回/删除该消息，防止泄露")

    @filter.command("pan", "下载百度网盘分享文件")
    async def on_pan(self, event: AstrMessageEvent, args: GreedyStr):
        """用法: /pan <链接或surl> [提取码]  |  /pan login <账号> <密码>"""

        if self._is_blacklisted(event):
            return
        dl_key = f"{event.get_sender_id()}:{str(args).strip()}"
        _lock_acquired = False
        parts = [p for p in str(args).split() if p]
        if not parts or parts[0] == "help":
            yield event.plain_result("用法:")
            yield event.plain_result("/pan <链接> [密码]  转存并查看目录树")
            yield event.plain_result("/pan look  重新查看目录树")
            yield event.plain_result("/pan dir <文件夹路径>  下载文件夹")
            yield event.plain_result("/pan file <文件路径>  下载指定文件")
            yield event.plain_result("/pan qrlogin  扫码登录(推荐)")
            yield event.plain_result("/pan bduss <BDUSS> [STOKEN]")
            yield event.plain_result("/pan login <账号> <密码>")
            yield event.plain_result("/pan unlogin  退出登录")
            yield event.plain_result("/pan download  下载/更新 BaiduPCS-Go 工具")
            yield event.plain_result("/pan download git  强制从 GitHub 下载（出问题时用）")
            sponsor_path = os.path.join(os.path.dirname(__file__), "sponsor.png")
            if os.path.exists(sponsor_path):
                yield event.chain_result([
                    Plain("支持作者↑↑↑"),
                    Image.fromFileSystem(sponsor_path)
                ])
            return

        if parts[0] == "download":
            if len(parts) > 1 and parts[1] == "git":
                # 强制使用 GitHub 下载
                yield event.plain_result("⏳ 正在从 GitHub 官方 release 下载 BaiduPCS-Go...")
                result = await asyncio.to_thread(_download_github_exe)
                if "ok" in result:
                    yield event.plain_result("✅ BaiduPCS-Go.exe 下载成功！现在可以使用 /pan login 等命令了。")
                else:
                    yield event.plain_result(f"❌ 下载失败: {result.get('error', '未知错误')}")
                return
            # 第一步：国内加速下载
            yield event.plain_result("⏳ 正在使用国内加速下载 BaiduPCS-Go...")
            result = await asyncio.to_thread(_download_cn_exe)
            if "ok" in result:
                yield event.plain_result("✅ BaiduPCS-Go.exe 下载成功！现在可以使用 /pan login 等命令了。")
                return
            # 第二步：国内失败，回退 GitHub
            yield event.plain_result("⏳ 国内下载失败，正在从 GitHub 官方 release 下载...")
            result = await asyncio.to_thread(_download_github_exe)
            if "ok" in result:
                yield event.plain_result("✅ BaiduPCS-Go.exe 下载成功！现在可以使用 /pan login 等命令了。")
            else:
                yield event.plain_result(f"❌ 下载失败: {result.get('error', '未知错误')}")
            return

        # 以下命令都需要 BaiduPCS-Go.exe
        if not _ensure_bpcs_exe():
            yield event.plain_result("❌ BaiduPCS-Go.exe 缺失或损坏，请使用 /pan help 中提供的命令进行下载或查看readme")
            return

        if parts[0] == "look":
            if self._cached_tree:
                yield event.plain_result(self._cached_tree)
            else:
                yield event.plain_result("请先使用 /pan <链接> [密码] 查看目录")
            return

        if parts[0] == "login":
            if len(parts) < 3:
                yield event.plain_result("用法: /pan login <百度账号> <密码>")
                return
            async for r in self._login_flow(event, parts[1], parts[2]):
                yield r
            return

        if parts[0] == "unlogin":
            yield event.plain_result("⏳ 正在退出登录...")
            result = await asyncio.to_thread(logout_bpcs)
            if "error" in result:
                yield event.plain_result(f"❌ {result['error']}")
            else:
                yield event.plain_result("✅ 已退出百度网盘登录，本地凭证已清除。如需重新登录请私聊发送 /pan login 账号 密码")
            return

        if parts[0] == "cookies":
            if len(parts) < 2 or len(parts[1]) < 50:
                yield event.plain_result("用法: /pan cookies <完整Cookies字符串>（浏览器登录 pan.baidu.com 后 F12 → Network → 任意请求 → 复制 Cookie 请求头）")
                return
            # Cookies 字符串本身不含空格分隔的多段，用原始参数拼接，避免被空格截断
            cookie_str = " ".join(parts[1:])
            yield event.plain_result("⏳ 正在注入 Cookies...")
            result = await asyncio.to_thread(login_cookies_bpcs, cookie_str)
            if "error" in result:
                yield event.plain_result(f"❌ {result['error']}")
            else:
                yield event.plain_result(f"✅ 登录成功！{result.get('output', '')}")
            return

        if parts[0] == "bduss":
            if len(parts) < 2 or len(parts[1]) < 10:
                yield event.plain_result("用法: /pan bduss <BDUSS值> [STOKEN值]\n获取: 浏览器登录 pan.baidu.com → F12 → Application → Cookies → 复制 BDUSS 和 STOKEN 的值")
                return
            bduss = parts[1]
            stoken = parts[2] if len(parts) >= 3 else ""
            yield event.plain_result("⏳ 正在登录...")
            result = await asyncio.to_thread(login_bduss_bpcs, bduss, stoken)
            if "error" in result:
                yield event.plain_result(f"❌ {result['error']}")
            else:
                extra = "（含STOKEN，转存可用）" if stoken else "（无STOKEN，转存可能不可用，仅下载）"
                yield event.plain_result(f"✅ 登录成功！{extra}")
            return

        if parts[0] == "qrlogin":
            sess = _req.Session()
            sess.headers.update({"User-Agent": _BROWSER_UA, "Accept-Language": "zh-CN,zh;q=0.9"})
            # Step 1: 获取二维码（session 会自动保存 BAIDUID 等初始 cookie）
            try:
                r = await asyncio.to_thread(
                    lambda: sess.get(
                        "https://passport.baidu.com/v2/api/getqrcode?lp=pc&qrloginfrom=pc",
                        timeout=10,
                    )
                )
                qdata = r.json()
            except Exception as e:
                yield event.plain_result(f"❌ 获取二维码失败: {e}")
                return
            if qdata.get("errno") != 0:
                yield event.plain_result("❌ 获取二维码失败")
                return
            qsign = qdata["sign"]
            qimgurl = "https://" + qdata["imgurl"]
            # 下载二维码图片
            try:
                img_r = await asyncio.to_thread(lambda: sess.get(qimgurl, timeout=10))
            except Exception as e:
                yield event.plain_result(f"❌ 下载二维码失败: {e}")
                return
            qr_path = os.path.join(os.path.dirname(__file__), "storage", "qr_login.png")
            os.makedirs(os.path.dirname(qr_path), exist_ok=True)
            with open(qr_path, "wb") as f:
                f.write(img_r.content)
            # 发二维码图片
            yield event.chain_result([
                Image.fromFileSystem(qr_path),
                Plain("请用百度网盘 APP 扫码登录（3分钟内有效）")
            ])
            # Step 2: 轮询扫码状态
            gid = str(uuid.uuid4()).upper()
            confirmed = False
            for _ in range(60):
                await asyncio.sleep(3)
                tt = str(int(time.time() * 1000))
                poll_url = (
                    f"https://passport.baidu.com/channel/unicast"
                    f"?channel_id={qsign}&gid={gid}&tpl=mm"
                    f"&_sdkFrom=1&apiver=v3&tt={tt}&_={tt}&callback="
                )
                try:
                    pr = await asyncio.to_thread(lambda: sess.get(poll_url, timeout=35))
                    ptext = (pr.text or "").strip()
                except Exception:
                    continue
                if not ptext:
                    continue
                # 去除可能的 JSONP 包裹
                if ptext.startswith("(") and ptext.endswith(")"):
                    ptext = ptext[1:-1]
                if ptext.startswith("tangram") or ptext.startswith("callback"):
                    ptext = ptext[ptext.index("(")+1:ptext.rindex(")")]
                # 解析 JSON
                try:
                    pdata = _json.loads(ptext)
                except Exception:
                    continue
                if pdata.get("errno") != 0:
                    continue  # 未扫码
                cv_str = pdata.get("channel_v", "")
                if not cv_str:
                    continue
                try:
                    cv = _json.loads(cv_str) if isinstance(cv_str, str) else cv_str
                except Exception:
                    continue
                if cv.get("status") == 1 and not confirmed:
                    confirmed = True
                    yield event.plain_result("✅ 已扫码，请在手机上确认登录")
                    continue
                v = cv.get("v", "")
                if v:
                    # Step 3: 用 v 换取登录凭证（session 自动带上之前累积的 cookie）
                    login_url = (
                        f"https://passport.baidu.com/v3/login/main/qrbdusslogin"
                        f"?bduss={v}&u=&loginVersion=v4&qrcode=1&tpl=mm&apiver=v3"
                    )
                    try:
                        lr = await asyncio.to_thread(
                            lambda: sess.get(login_url, timeout=10, allow_redirects=False)
                        )
                    except Exception as e:
                        yield event.plain_result(f"❌ 获取凭证失败: {e}")
                        return
                    # 访问 pan.baidu.com 获取 pan 专用 cookie（csrfToken / PANPSC）
                    try:
                        await asyncio.to_thread(
                            lambda: sess.get("https://pan.baidu.com/disk/main", timeout=10)
                        )
                    except Exception:
                        pass
                    # 从 session 的 cookie jar 提取全部 cookie（已含 pan 专用 cookie）
                    full_cookie = "; ".join(f"{k}={cv2}" for k, cv2 in sess.cookies.items())
                    logger.info(f"[BaiduPan] qrlogin cookie keys: {list(sess.cookies.keys())}")
                    logger.info(f"[BaiduPan] qrlogin full_cookie len: {len(full_cookie)}")
                    bduss_m = re.search(r'BDUSS=([^\s;]+)', full_cookie)
                    if not bduss_m:
                        yield event.plain_result("❌ 未能提取 BDUSS，登录失败")
                        return
                    # Step 4: 用完整 cookie 登录 BaiduPCS-Go
                    result = await asyncio.to_thread(login_cookies_bpcs, full_cookie)
                    if "error" in result:
                        # 回退到仅 BDUSS+STOKEN 方式
                        stoken_m = re.search(r'STOKEN=([^\s;]+)', full_cookie)
                        bduss_val = bduss_m.group(1)
                        stoken_val = stoken_m.group(1) if stoken_m else ""
                        result2 = await asyncio.to_thread(login_bduss_bpcs, bduss_val, stoken_val)
                        if "error" in result2:
                            yield event.plain_result(f"❌ {result2['error']}")
                        else:
                            extra = "（含STOKEN，转存可能受限）" if stoken_val else "（无STOKEN）"
                            yield event.plain_result(f"✅ 扫码登录成功！{extra}")
                    else:
                        yield event.plain_result("✅ 扫码登录成功！（完整cookie，转存可用）")
                    return
            yield event.plain_result("❌ 二维码已超时，请重新发送 /pan qrlogin")
            return

        if parts[0] in ("dir", "folder", "fl"):
            # 下载文件夹（使用缓存的链接）
            if not self._cached_surl:
                yield event.plain_result("请先使用 /pan <链接> [密码] 查看目录")
                return
            if len(parts) < 2:
                yield event.plain_result("用法: /pan dir <文件夹路径>")
                return
            # 路径可能含空格，拼回完整参数
            dir_path = " ".join(parts[1:])
            # 下载锁检查
            if dl_key in self._active_downloads:
                logger.warning(f"[BaiduPan] on_pan: duplicate download blocked: {dl_key}")
                yield event.plain_result("⏳ 该文件正在下载中，请稍候...")
                return
            self._active_downloads.add(dl_key)
            _lock_acquired = True
            try:
                logger.info(f"[BaiduPan] on_pan dir: dir_path={dir_path}, progress_enabled={self._progress_enabled}")
                yield event.plain_result(f"⏳ 正在下载文件夹: {dir_path} ...")
                prog_q = queue.Queue() if self._progress_enabled else None
                dl_future = asyncio.create_task(asyncio.to_thread(download_from_cloud, dir_path, self._get_max_mb(), prog_q))
                if prog_q:
                    file_info = None
                    _last_prog_size = None
                    _last_prog_time = None
                    while not dl_future.done():
                        try:
                            msg = prog_q.get_nowait()
                            if isinstance(msg, tuple) and msg[0] == "_info":
                                file_info = (msg[1], msg[2])  # (name, size)
                            elif isinstance(msg, tuple) and msg[0] == "_error":
                                yield event.plain_result(f"❌ {msg[1]}")
                            elif isinstance(msg, str):
                                yield event.plain_result(msg)
                        except queue.Empty:
                            pass
                        # 监控本地文件大小
                        if file_info and not dl_future.done():
                            fname, total = file_info
                            current = _get_local_file_size(fname)
                            # 如果 total==0 但本地文件有大小，用本地文件大小作为 total
                            if total <= 0 and current > 0:
                                total = current
                                file_info = (fname, total)
                            if current > 0 and total > 0:
                                now = time.time()
                                if _last_prog_time is None or now - _last_prog_time >= 1.0:
                                    if _last_prog_size is not None and _last_prog_time is not None:
                                        dt = now - _last_prog_time
                                        if dt > 0:
                                            speed_bps = (current - _last_prog_size) / dt
                                            speed_str = f"{speed_bps/1024/1024:.2f} MB/s" if speed_bps >= 1024*1024 else f"{speed_bps/1024:.1f} KB/s"
                                            remain = (total - current) / speed_bps if speed_bps > 0 else 0
                                            if remain >= 3600:
                                                eta = f"{remain/3600:.1f}h"
                                            elif remain >= 60:
                                                eta = f"{remain/60:.1f}m"
                                            else:
                                                eta = f"{remain:.0f}s"
                                            _last_prog_size = current
                                            _last_prog_time = now
                                            pct = min(current / total * 100, 100.0)
                                            yield event.plain_result(f"⏬ {pct:.1f}%  {speed_str}  ETA {eta}")
                                            if current >= total:
                                                yield event.plain_result("✅ 下载完成，正在发送...")
                                                break
                                    else:
                                        _last_prog_size = current
                                        _last_prog_time = now
                        await self._interruptible_sleep(dl_future, self._progress_interval)
                dl = await dl_future
                logger.info(f"[BaiduPan] on_pan dir: download complete, dl={dl}")
                if "error" in dl:
                    logger.error(f"[BaiduPan] on_pan dir: download error: {dl['error']}")
                    yield event.plain_result(f"❌ {dl['error']}")
                else:
                    if dl.get("is_dir"):
                        yield event.plain_result(f"✅ 文件夹 '{dir_path}' 下载完成，共 {len(dl.get('files', []))} 个文件")
                        yield event.plain_result(f"📁 保存路径: {dl['path']}")
                    else:
                        yield event.chain_result([File(name=dl.get("name", "file"), file=dl["path"])])
            finally:
                if _lock_acquired:
                    self._active_downloads.discard(dl_key)
            return

        if parts[0] in ("file", "f"):
            # 下载文件（使用缓存的链接）
            if not self._cached_surl:
                yield event.plain_result("请先使用 /pan <链接> [密码] 查看目录")
                return
            if len(parts) < 2:
                yield event.plain_result("用法: /pan file <文件路径>")
                return
            # 路径可能含空格，拼回完整参数
            f_path = " ".join(parts[1:])
            # 下载操作才加锁
            if dl_key in self._active_downloads:
                logger.warning(f"[BaiduPan] on_pan: duplicate download blocked: {dl_key}")
                yield event.plain_result("⏳ 该文件正在下载中，请稍候...")
                return
            self._active_downloads.add(dl_key)
            _lock_acquired = True
            try:
                logger.info(f"[BaiduPan] on_pan file: f_path={f_path}, progress_enabled={self._progress_enabled}")
                yield event.plain_result(f"⏳ 正在下载文件: {f_path} ...")
                prog_q = queue.Queue() if self._progress_enabled else None
                dl_future = asyncio.create_task(asyncio.to_thread(download_from_cloud, f_path, self._get_max_mb(), prog_q))
                if prog_q:
                    file_info = None
                    _last_prog_size = None
                    _last_prog_time = None
                    while not dl_future.done():
                        try:
                            msg = prog_q.get_nowait()
                            if isinstance(msg, tuple) and msg[0] == "_info":
                                file_info = (msg[1], msg[2])
                            elif isinstance(msg, tuple) and msg[0] == "_error":
                                yield event.plain_result(f"❌ {msg[1]}")
                            elif isinstance(msg, str):
                                yield event.plain_result(msg)
                        except queue.Empty:
                            pass
                        if file_info and not dl_future.done():
                            fname, total = file_info
                            current = _get_local_file_size(fname)
                            # 如果 total==0 但本地文件有大小，用本地文件大小作为 total
                            if total <= 0 and current > 0:
                                total = current
                                file_info = (fname, total)
                            if current > 0 and total > 0:
                                now = time.time()
                                if _last_prog_time is None or now - _last_prog_time >= 1.0:
                                    if _last_prog_size is not None and _last_prog_time is not None:
                                        dt = now - _last_prog_time
                                        if dt > 0:
                                            speed_bps = (current - _last_prog_size) / dt
                                            speed_str = f"{speed_bps/1024/1024:.2f} MB/s" if speed_bps >= 1024*1024 else f"{speed_bps/1024:.1f} KB/s"
                                            remain = (total - current) / speed_bps if speed_bps > 0 else 0
                                            if remain >= 3600:
                                                eta = f"{remain/3600:.1f}h"
                                            elif remain >= 60:
                                                eta = f"{remain/60:.1f}m"
                                            else:
                                                eta = f"{remain:.0f}s"
                                            _last_prog_size = current
                                            _last_prog_time = now
                                            pct = min(current / total * 100, 100.0)
                                            yield event.plain_result(f"⏬ {pct:.1f}%  {speed_str}  ETA {eta}")
                                            if current >= total:
                                                yield event.plain_result("✅ 下载完成，正在发送...")
                                                break
                                    else:
                                        _last_prog_size = current
                                        _last_prog_time = now
                        await self._interruptible_sleep(dl_future, self._progress_interval)
                dl = await dl_future
                logger.info(f"[BaiduPan] on_pan file: download complete, dl={dl}")
                if "error" in dl:
                    logger.error(f"[BaiduPan] on_pan file: download error: {dl['error']}")
                    yield event.plain_result(f"❌ {dl['error']}")
                else:
                    logger.info(f"[BaiduPan] on_pan file: download complete, path={dl.get('path')}")
                    yield event.chain_result([File(name=dl.get("name", "file"), file=dl["path"])])
            finally:
                if _lock_acquired:
                    self._active_downloads.discard(dl_key)
            return

        # 默认: /pan <链接> [密码] → 转存并展示目录树（整串交给 _normalize_link 统一解析）
        surl, pwd = self._normalize_link(" ".join(parts), "")

        if not surl:
            yield event.plain_result("❌ 无法解析链接")
            return

        blocked = self._check_switch_allowed(surl)
        if blocked:
            yield event.plain_result(f"❌ {blocked}")
            return

        yield event.plain_result("⏳ 正在转存并获取目录结构...")
        result = await asyncio.to_thread(self._transfer_and_list, surl, pwd)
        if "error" in result:
            yield event.plain_result(f"❌ {result['error']}")
        else:
            yield event.plain_result(result["text"])
