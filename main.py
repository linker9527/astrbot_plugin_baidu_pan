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

_bpcs_inited = False
_bpcs_lock = threading.Lock()
_auto_delete_cloud = True
_local_cleanup_hour = 3


def _ensure_bpcs_exe() -> bool:
    """检查 BaiduPCS-Go.exe 是否存在且可运行。不存在或损坏时返回 False。"""
    if not os.path.exists(BPCS_PATH):
        return False
    try:
        r = subprocess.run([BPCS_PATH, "--version"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


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


def _download_bpcs_exe() -> dict:
    """下载 BaiduPCS-Go.exe。
    优先从国内镜像直链下载（已与官方核对 SHA256 一致），失败回退 GitHub 官方 release。
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

    # ── 方案 1：国内镜像直链（直接下载 exe，不用解压）──
    logger.info(f"[BaiduPan] 尝试从国内镜像下载 BaiduPCS-Go.exe...")
    data = _get(BPCS_CN_URL, timeout=120)
    if data is not None:
        if len(data) == BPCS_EXE_SIZE and _sha256(data) == BPCS_EXE_SHA256:
            try:
                with open(BPCS_PATH, "wb") as f:
                    f.write(data)
                logger.info(f"[BaiduPan] 国内镜像下载成功（{len(data)} bytes，SHA256 校验通过）")
                return {"ok": True}
            except Exception as e:
                return {"error": f"写入 exe 失败: {e}"}
        logger.warning(f"[BaiduPan] 国内镜像文件校验失败: size={len(data)}/{BPCS_EXE_SIZE}, sha256={'ok' if _sha256(data)==BPCS_EXE_SHA256 else 'mismatch'}")
    else:
        logger.warning("[BaiduPan] 国内镜像下载失败，回退 GitHub")

    # ── 方案 2：GitHub 官方 release（下载 zip，校验 size，解压）──
    logger.info(f"[BaiduPan] 从 GitHub 官方 release 下载 BaiduPCS-Go {BPCS_VERSION}...")
    info = _fetch_release_info()
    if not info:
        return {"error": "国内镜像和 GitHub 均下载失败，请检查网络后使用 /pan download 重试"}
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
    # GitHub 下载的 exe 也校验 SHA256
    if _sha256(exe_data) != BPCS_EXE_SHA256:
        return {"error": "exe SHA256 与官方不一致，文件可能被篡改"}
    try:
        with open(BPCS_PATH, "wb") as f:
            f.write(exe_data)
        logger.info(f"[BaiduPan] GitHub 下载成功（{len(exe_data)} bytes，SHA256 校验通过）")
        return {"ok": True}
    except Exception as e:
        return {"error": f"写入 exe 失败: {e}"}


def _run_bpcs(args: list, timeout: int = 300) -> tuple:
    try:
        r = subprocess.run(
            [BPCS_PATH] + args, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8"
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1


def _init_bpcs() -> bool:
    """检查 BaiduPCS-Go 是否已有本地登录凭证（通过 who 命令）。"""
    global _bpcs_inited
    if _bpcs_inited:
        return True
    with _bpcs_lock:
        if _bpcs_inited:
            return True
        if not _ensure_bpcs_exe():
            logger.warning(f"[BaiduPan] BaiduPCS-Go.exe 不可用，请使用 /pan download 下载")
            return False
        out, _, code = _run_bpcs(["who"], timeout=15)
        if code == 0 and out and "登录" not in (out or "") and "未登录" not in (out or ""):
            _bpcs_inited = True
            logger.info(f"[BaiduPan] BaiduPCS-Go already logged in: {out.strip()[:100]}")
            return True
        logger.info("[BaiduPan] BaiduPCS-Go not logged in, use /pan login")
        return False


def login_bduss_bpcs(bduss: str, stoken: str = "") -> dict:
    """用 BDUSS + STOKEN 登录 BaiduPCS-Go。"""
    global _bpcs_inited
    if not _ensure_bpcs_exe():
        return {"error": "BaiduPCS-Go.exe 缺失或损坏，请使用 /pan download 下载"}
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
    import json as _json
    config_path = os.path.join(
        os.environ.get("BAIDUPCS_GO_CONFIG_DIR", ""),
        "pcs_config.json"
    ) if os.environ.get("BAIDUPCS_GO_CONFIG_DIR") else os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming", "BaiduPCS-Go", "pcs_config.json"
    )
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
        return {"error": "BaiduPCS-Go.exe 缺失或损坏，请使用 /pan download 下载"}
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
    """用百度账号密码登录 BaiduPCS-Go，登录成功后凭证保存在本地，全局生效。"""
    global _bpcs_inited
    if not _ensure_bpcs_exe():
        return {"error": "BaiduPCS-Go.exe 缺失或损坏，请使用 /pan download 下载"}
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
        return {"error": "BaiduPCS-Go.exe 缺失或损坏，请使用 /pan download 下载"}
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
    cfg_path = os.path.join(
        os.environ.get("BAIDUPCS_GO_CONFIG_DIR", ""),
        "pcs_config.json"
    ) if os.environ.get("BAIDUPCS_GO_CONFIG_DIR") else os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming", "BaiduPCS-Go", "pcs_config.json"
    )
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
    sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
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
        r = sess.get(share_link, timeout=10,
                     headers={"Referer": "https://pan.baidu.com/disk/home"})
        m = re.search(r'window\.yunData\s*=\s*(\{.+?\});', r.text)
        if not m:
            return {"error": "访问分享页失败，可能需要重新登录"}
        yd = re.sub(r"'", '"', m.group(1))
        yd = re.sub(r'(\w+):', r'"\1":', yd)
        yd = _json.loads(yd)
        if yd.get("loginstate", "0") == "0":
            return {"error": "网盘未登录，请使用 /pan qrlogin 重新登录"}
        bdstoken = yd.get("bdstoken", "")
        share_uk = yd.get("share_uk", "")
        shareid = yd.get("shareid", "")
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
            }, headers=headers, timeout=10)
            resp2 = r2.json()
            if resp2.get("errno") != 0:
                if resp2.get("errno") == -9:
                    return {"error": "提取码错误"}
                return {"error": f"密码验证失败: {resp2.get('errno')}"}

        # Step 3: 重新访问分享页（带 init referer），获取新 bdstoken
        r3 = sess.get(share_link, timeout=10,
                      headers={"Referer": f"https://pan.baidu.com/share/init?surl={surl}"})
        m = re.search(r'window\.yunData\s*=\s*(\{.+?\});', r3.text)
        if m:
            yd2 = re.sub(r"'", '"', m.group(1))
            yd2 = re.sub(r'(\w+):', r'"\1":', yd2)
            yd2 = _json.loads(yd2)
            bdstoken = yd2.get("bdstoken", bdstoken)

        # Step 4: 获取文件列表
        list_url = (
            f"https://pan.baidu.com/share/list"
            f"?bdstoken={bdstoken}&root=1&web=5&app_id=250528"
            f"&shorturl={surl[1:]}&channel=chunlei&clienttype=0"
        )
        r4 = sess.get(list_url, timeout=10, headers={"Referer": share_link})
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
                       timeout=15)
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
        import traceback
        logger.exception(f"[BaiduPan] transfer API error: {e}")
        return {"error": f"转存API调用异常: {e}"}


def _cleanup_local_dir():
    """清理本地下载目录中的所有文件。"""
    if not DOWNLOAD_DIR or not os.path.exists(DOWNLOAD_DIR):
        return
    count = 0
    try:
        for fname in os.listdir(DOWNLOAD_DIR):
            fpath = os.path.join(DOWNLOAD_DIR, fname)
            if os.path.isfile(fpath):
                os.remove(fpath)
                count += 1
        logger.info(f"[BaiduPan] local cleanup: removed {count} files from {DOWNLOAD_DIR}")
    except Exception as e:
        logger.warning(f"[BaiduPan] local cleanup error: {e}")


def _schedule_local_cleanup(hour: int):
    """安排每天定时清理本地下载文件。"""
    if hour < 0 or hour > 23:
        logger.info("[BaiduPan] local auto-cleanup disabled")
        return
    try:
        from astrbot.core import star
        # 使用 AstrBot 的定时任务机制
        import asyncio
        async def _cleanup_loop():
            while True:
                now = time.localtime()
                # 计算到目标小时的秒数
                target = int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                          hour, 0, 0, now.tm_wday, now.tm_yday, now.tm_isdst)))
                if target <= time.time():
                    target += 86400  # 明天
                wait_secs = target - time.time()
                await asyncio.sleep(wait_secs)
                _cleanup_local_dir()
                await asyncio.sleep(86400)  # 等24小时再跑下一次
        # 启动后台协程
        import threading
        def _start_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_cleanup_loop())
        t = threading.Thread(target=_start_loop, daemon=True)
        t.start()
        logger.info(f"[BaiduPan] scheduled local cleanup at {hour}:00 daily")
    except Exception as e:
        logger.warning(f"[BaiduPan] schedule cleanup failed: {e}")


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


def download_share(surl: str, pwd: str = "", max_mb: int = 200) -> dict:
    share_url = f"https://pan.baidu.com/s/{surl}"
    max_bytes = max_mb * 1024 * 1024

    # Step 1: transfer share to own cloud (via Python API, bypassing BPCS v4.0.1 cookie bug)
    cloud_dir = CLOUD_SAVE_DIR or "/我的资源/AutoTransfer"
    import subprocess as _subprocess
    transfer_path = cloud_dir
    result = _transfer_via_api(surl, pwd, transfer_path)
    if "error" in result:
            return {"error": result["error"]}
    logger.info(f"[BaiduPan] transfer API success: {result.get('filenames', [])}")

    # Step 2: find file in cloud storage
    own_share = result.get("_own_share", False)
    target = None

    if own_share:
        for fname in result.get("filenames", []):
            search_out, _, _ = _run_bpcs(["search", fname], timeout=60)
            for line in search_out.strip().split("\n"):
                line = line.strip()
                parts = line.split()
                if len(parts) >= 10 and parts[3] != "-" and parts[-1] == fname:
                    raw_size = parts[3]
                    size_bytes = 0
                    try:
                        if raw_size.upper().endswith("GB"):
                            size_bytes = int(float(raw_size[:-2]) * 1024 * 1024 * 1024)
                        elif raw_size.upper().endswith("MB"):
                            size_bytes = int(float(raw_size[:-2]) * 1024 * 1024)
                        elif raw_size.upper().endswith("KB"):
                            size_bytes = int(float(raw_size[:-2]) * 1024)
                        elif raw_size.upper().endswith("TB"):
                            size_bytes = int(float(raw_size[:-2]) * 1024 * 1024 * 1024 * 1024)
                        elif raw_size.upper().endswith("B"):
                            size_bytes = int(float(raw_size[:-1]))
                        else:
                            size_bytes = int(float(raw_size))
                    except (ValueError, IndexError):
                        continue
                    target = {"size": size_bytes, "name": fname, "path": fname}
                    logger.info(f"[BaiduPan] found own file via search: {fname} ({size_bytes} bytes)")
                    break
            if target:
                break
        if not target:
            return {"error": f"自己的分享链接，但云端搜索不到文件 '{result.get('filenames', ['?'])[0]}'，可能已被删除，请重新上传后分享"}
    else:
        out, err, code = _run_bpcs(["ls", "-l", transfer_path], timeout=60)
        if code != 0:
            return {"error": "获取文件列表失败"}

        for line in out.strip().split("\n"):
            line = line.strip()
            if (not line or line.startswith("#") or line.startswith("当前")
                    or line.startswith("总") or "Total:" in line
                    or "----" in line or "获取" in line):
                continue
            parts = line.split()
            if len(parts) >= 10 and parts[3] != "-":
                raw_size = parts[3]
                size_bytes = 0
                try:
                    if raw_size.upper().endswith("GB"):
                        size_bytes = int(float(raw_size[:-2]) * 1024 * 1024 * 1024)
                    elif raw_size.upper().endswith("MB"):
                        size_bytes = int(float(raw_size[:-2]) * 1024 * 1024)
                    elif raw_size.upper().endswith("KB"):
                        size_bytes = int(float(raw_size[:-2]) * 1024)
                    elif raw_size.upper().endswith("TB"):
                        size_bytes = int(float(raw_size[:-2]) * 1024 * 1024 * 1024 * 1024)
                    elif raw_size.upper().endswith("B"):
                        size_bytes = int(float(raw_size[:-1]))
                    else:
                        size_bytes = int(float(raw_size))
                except (ValueError, IndexError):
                    continue
                name = parts[-1]
                target = {"size": size_bytes, "name": name, "path": f"{transfer_path}/{name}"}
                break

        if not target:
            return {"error": "转存后未找到可下载文件，请检查网盘转存目录"}

    if max_mb > 0 and target["size"] > max_bytes:
        return {"error": f"文件过大({target['size']//1024//1024}MB)超过{max_mb}MB限制，请自行下载: {share_url}"}

    # Step 3: download (超时按文件大小动态计算，配置了 timeout_seconds 则优先使用；-1 不超时)
    # 重新设置 savedir 确保下载到正确目录
    _run_bpcs(["config", "set", "-savedir", DOWNLOAD_DIR], timeout=15)
    dl_timeout = DOWNLOAD_TIMEOUT_SECONDS if DOWNLOAD_TIMEOUT_SECONDS > 0 else max(600, int(target["size"] / (1024 * 1024) * 15))
    out, err, code = _run_bpcs(
        ["download", target["path"], "--ow"], timeout=dl_timeout
    )
    if code != 0 or "失败" in (out or "") or "错误" in (out or ""):
        logger.error(f"[BaiduPan] download failed: code={code}, out={out[:200]}, err={err[:200]}")
        return {"error": "下载失败"}

    # Find downloaded file: BaiduPCS-Go 保存的文件名一般是原名
    if not DOWNLOAD_DIR:
        return {"error": "下载目录未设置"}
    logger.info(f"[BaiduPan] download done, looking in {DOWNLOAD_DIR} for {target['name']}")
    candidates = [
        os.path.join(DOWNLOAD_DIR, target["name"]),
        os.path.join(DOWNLOAD_DIR, f"{surl}_{target['name']}"),
    ]
    local_path = next((p for p in candidates if os.path.exists(p)), None)
    if not local_path:
        for f in os.listdir(DOWNLOAD_DIR):
            if f == target["name"] or f.startswith(surl):
                local_path = os.path.join(DOWNLOAD_DIR, f)
                break

    if not local_path or not os.path.exists(local_path):
        logger.warning(f"[BaiduPan] file not found in {DOWNLOAD_DIR}, files: {os.listdir(DOWNLOAD_DIR)[:20]}")
        return {"error": "下载完成但未找到文件"}

    # Step 4: delete from cloud after successful download
    # Step 4: delete from cloud after successful download (if enabled)
    if _auto_delete_cloud:
        _run_bpcs(["rm", target["path"]], timeout=30)
    return {"path": local_path, "size": os.path.getsize(local_path), "name": target["name"]}



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
    def _list_dir(dir_path: str, indent: int = 0, collect_items: bool = True):
        nonlocal all_items
        out, _, code = _run_bpcs(["ls", "-l", dir_path], timeout=60)
        if code != 0:
            return
        prefix = "  " * indent
        for line in out.strip().split("\n"):
            line = line.strip()
            if (not line or line.lstrip().startswith("#") or line.startswith("当前")
                    or line.startswith("总") or "Total:" in line
                    or "----" in line or "获取" in line):
                continue
            parts = line.split()
            if len(parts) >= 9:
                name = parts[-1]
                raw_size = parts[3]
                is_dir = (raw_size == "-")
                size_str = raw_size if is_dir else _format_size(raw_size)
                if is_dir:
                    lines.append(f"{prefix}📁 {name}/")
                    if collect_items:
                        all_items.append({"path": f"{dir_path}/{name}", "name": name, "size": "-", "is_dir": True})
                    _list_dir(f"{dir_path}/{name}", indent + 1, collect_items)
                else:
                    lines.append(f"{prefix}📄 {name}  ({size_str})")
                    if collect_items:
                        all_items.append({"path": f"{dir_path}/{name}", "name": name, "size": raw_size, "is_dir": False})
    
    # 获取顶层目录内容，不展示顶层目录名，子目录各自成树，空行分隔
    out, _, code = _run_bpcs(["ls", "-l", cloud_dir], timeout=60)
    if code != 0:
        return {"error": "获取目录列表失败"}
    
    top_items = []
    for line in out.strip().split("\n"):
        line = line.strip()
        if (not line or line.lstrip().startswith("#") or line.startswith("当前")
                or line.startswith("总") or "Total:" in line
                or "----" in line or "获取" in line):
            continue
        parts = line.split()
        if len(parts) >= 9:
            name = parts[-1]
            raw_size = parts[3]
            is_dir = (raw_size == "-")
            top_items.append({"name": name, "is_dir": is_dir, "raw_size": raw_size})
    
    tree_count = 0
    for item in top_items:
        if tree_count > 0:
            lines.append("")  # 空行分隔树
        if item["is_dir"]:
            lines.append(f"📁 {item['name']}/")
            all_items.append({"path": f"{cloud_dir}/{item['name']}", "name": item["name"], "size": "-", "is_dir": True})
            _list_dir(f"{cloud_dir}/{item['name']}", 1)
        else:
            lines.append(f"📄 {item['name']}  ({_format_size(item['raw_size'])})")
            all_items.append({"path": f"{cloud_dir}/{item['name']}", "name": item["name"], "size": item["raw_size"], "is_dir": False})
        tree_count += 1
    
    if not lines:
        return {"error": "目录为空"}
    
    return {"text": "\n".join(lines), "items": all_items, "own_share": False}
def download_share_path(surl: str, pwd: str, cloud_path: str, max_mb: int = 0) -> dict:
    """转存后下载指定路径的文件或文件夹（含所有内容）。"""
    cloud_dir = CLOUD_SAVE_DIR or "/我的资源/AutoTransfer"
    result = _transfer_via_api(surl, pwd, cloud_dir)
    if "error" in result:
        return {"error": result["error"]}
    
    full_path = f"{cloud_dir}/{cloud_path.lstrip('/')}"
    
    # 检查路径是否存在
    out, _, code = _run_bpcs(["ls", "-l", full_path], timeout=60)
    if code != 0:
        return {"error": f"云端路径不存在: {cloud_path}"}
    
    # 判断是文件还是目录
    is_dir = False
    file_size = 0
    for line in out.strip().split("\n"):
        line = line.strip()
        if (not line or line.lstrip().startswith("#") or line.startswith("当前")
                or line.startswith("总") or "Total:" in line or "----" in line):
            continue
        parts = line.split()
        if len(parts) >= 9 and parts[-1] == cloud_path.rstrip("/").split("/")[-1]:
            if parts[3] == "-":
                is_dir = True
            else:
                try:
                    raw = parts[3].upper()
                    if raw.endswith("GB"):
                        file_size = int(float(raw[:-2]) * 1024 * 1024 * 1024)
                    elif raw.endswith("MB"):
                        file_size = int(float(raw[:-2]) * 1024 * 1024)
                    elif raw.endswith("KB"):
                        file_size = int(float(raw[:-2]) * 1024)
                    elif raw.endswith("B"):
                        file_size = int(float(raw[:-1]))
                    else:
                        file_size = int(float(raw))
                except (ValueError, IndexError):
                    pass
            break
    
    # 检查大小限制（仅对单个文件检查）
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
    import subprocess as _subprocess
    # 把文件信息塞进队列，给 handler 监控文件大小用
    # 如果 file_size 为0但文件非目录，把 0 也塞进去，让 handler 监控时用实际文件大小
    if progress_queue is not None and not is_dir:
        progress_queue.put(("_info", file_name, file_size))
    try:
        proc = _subprocess.Popen(
            [BPCS_PATH, "download", full_path, "--ow"],
            stdout=_subprocess.PIPE, stderr=_subprocess.STDOUT,
            stdin=_subprocess.PIPE
        )
        proc.stdin.write(b"y\n")
        proc.stdin.close()
        if dl_timeout > 0:
            out = proc.communicate(timeout=dl_timeout)[0]
        else:
            out = proc.communicate()[0]
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        code = proc.returncode
    except _subprocess.TimeoutExpired:
        logger.error(f"[BaiduPan] download timeout ({dl_timeout}s)")
        if progress_queue is not None:
            progress_queue.put(("_error", "下载超时"))
        return {"error": "下载超时"}
    except Exception as e:
        logger.error(f"[BaiduPan] download subprocess error: {e}")
        return {"error": f"下载进程异常: {e}"}
    if code != 0 or "失败" in (out or "") or "错误" in (out or ""):
        logger.error(f"[BaiduPan] download failed: code={code}, out={(out or '')[:200]}")
        return {"error": "下载失败"}
    
    # 查找下载的文件
    if not DOWNLOAD_DIR:
        return {"error": "下载目录未设置"}
    
    if is_dir:
        downloaded = []
        for root, dirs, files in os.walk(DOWNLOAD_DIR):
            for f in files:
                fp = os.path.join(root, f)
                downloaded.append({"path": fp, "name": f, "size": os.path.getsize(fp)})
        if not downloaded:
            return {"error": "文件夹下载完成但未找到文件"}
        return {"path": DOWNLOAD_DIR, "name": cloud_path.rstrip("/").split("/")[-1], "size": 0, "is_dir": True, "files": downloaded}
    else:
        name = cloud_path.rstrip("/").split("/")[-1]
        candidates = [
            os.path.join(DOWNLOAD_DIR, name),
            os.path.join(DOWNLOAD_DIR, f"{surl}_{name}"),
        ]
        local_path = next((p for p in candidates if os.path.exists(p)), None)
        if not local_path:
            for f in os.listdir(DOWNLOAD_DIR):
                if f == name or f.startswith(surl):
                    local_path = os.path.join(DOWNLOAD_DIR, f)
                    break
        if not local_path or not os.path.exists(local_path):
            return {"error": "下载完成但未找到文件"}
        return {"path": local_path, "size": os.path.getsize(local_path), "name": name}



def download_from_cloud(cloud_path: str, max_mb: int = 0, progress_queue: "queue.Queue" = None) -> dict:
    """从网盘下载指定路径的文件或文件夹（不转存，直接下载已有文件）。
    
    progress_queue: 可选，传入 queue.Queue 后，下载过程中会向队列放入进度字符串。
    """
    import subprocess as _subprocess
    cloud_dir = CLOUD_SAVE_DIR or "/我的资源/AutoTransfer"
    if cloud_path:
        full_path = f"{cloud_dir}/{cloud_path.lstrip('/')}"
        file_name = cloud_path.rstrip("/").split("/")[-1]
    else:
        full_path = cloud_dir
        file_name = "全部文件"

    logger.info(f"[BaiduPan] download_from_cloud: cloud_path={cloud_path}, full_path={full_path}")
    # 检查路径是否存在。bpcs ls -l 查文件本身不显示文件信息，需要查父目录
    ls_path = full_path
    ls_file_name = file_name
    # 如果包含文件名（无"/"结尾），尝试查父目录
    if not cloud_path.endswith("/"):
        parent = full_path.rstrip("/").rsplit("/", 1)
        if len(parent) >= 2:
            ls_path = parent[0]
            ls_file_name = parent[1]
    out, _, code = _run_bpcs(["ls", "-l", ls_path], timeout=60)
    logger.info(f"[BaiduPan] download_from_cloud: ls_path={ls_path}, ls_file_name={ls_file_name}, code={code}, out_len={len(out or '')}")
    if code != 0:
        return {"error": f"路径不存在: {cloud_path}"}
    if "错误" in (out or "") or "不存在" in (out or ""):
        return {"error": f"路径不存在或访问失败: {cloud_path}"}

    # 判断是文件还是目录
    is_dir = False
    file_size = 0
    for line in out.strip().split("\n"):
        line = line.strip()
        if (not line or line.lstrip().startswith("#") or line.startswith("当前")
                or line.startswith("总") or "Total:" in line or "----" in line):
            continue
        parts = line.split()
        if len(parts) >= 9 and parts[-1] == ls_file_name:
            if parts[3] == "-":
                is_dir = True
            else:
                try:
                    raw = parts[3].upper()
                    if raw.endswith("GB"):
                        file_size = int(float(raw[:-2]) * 1024 * 1024 * 1024)
                    elif raw.endswith("MB"):
                        file_size = int(float(raw[:-2]) * 1024 * 1024)
                    elif raw.endswith("KB"):
                        file_size = int(float(raw[:-2]) * 1024)
                    elif raw.endswith("B"):
                        file_size = int(float(raw[:-1]))
                    else:
                        file_size = int(float(raw))
                except (ValueError, IndexError):
                    logger.warning(f"[BaiduPan] download_from_cloud: failed to parse size from parts[3]={parts[3]!r}, raw={raw!r}")
                    pass
            break
    logger.info(f"[BaiduPan] download_from_cloud: is_dir={is_dir}, file_size={file_size} ({file_size/1024/1024:.1f}MB)")

    # 如果还是没解析到大小，且是文件，再试一次：ls 查目录
    if not is_dir and file_size == 0 and cloud_path.endswith("/") == False:
        parent_dir = full_path.rstrip("/").rsplit("/", 1)[0] if "/" in full_path.rstrip("/") else full_path
        search_name = full_path.rstrip("/").split("/")[-1]
        logger.info(f"[BaiduPan] download_from_cloud: retry ls on parent dir={parent_dir}, search={search_name}")
        out2, _, _ = _run_bpcs(["ls", "-l", parent_dir], timeout=60)
        for line in out2.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 9 and parts[-1] == search_name and parts[3] != "-":
                try:
                    raw = parts[3].upper()
                    if raw.endswith("GB"):
                        file_size = int(float(raw[:-2]) * 1024 * 1024 * 1024)
                    elif raw.endswith("MB"):
                        file_size = int(float(raw[:-2]) * 1024 * 1024)
                    elif raw.endswith("KB"):
                        file_size = int(float(raw[:-2]) * 1024)
                    elif raw.endswith("B"):
                        file_size = int(float(raw[:-1]))
                    else:
                        file_size = int(float(raw))
                except (ValueError, IndexError):
                    pass
                break
        logger.info(f"[BaiduPan] download_from_cloud: retry file_size={file_size} ({file_size/1024/1024:.1f}MB)")

    # 大小限制（仅单文件）
    max_bytes = max_mb * 1024 * 1024
    if not is_dir and max_mb > 0 and file_size > max_bytes:
        return {"error": f"文件过大 ({file_size//1024//1024}MB) 超过 {max_mb}MB 限制"}

    # 下载
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
    try:
        proc = _subprocess.Popen(
            [BPCS_PATH, "download", full_path, "--ow"],
            stdout=_subprocess.PIPE, stderr=_subprocess.STDOUT,
            stdin=_subprocess.PIPE
        )
        proc.stdin.write(b"y\n")
        proc.stdin.close()
        if dl_timeout > 0:
            out = proc.communicate(timeout=dl_timeout)[0]
        else:
            out = proc.communicate()[0]
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        code = proc.returncode
    except _subprocess.TimeoutExpired:
        logger.error(f"[BaiduPan] download timeout ({dl_timeout}s)")
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
        for root, dirs, files in os.walk(DOWNLOAD_DIR):
            for f in files:
                fp = os.path.join(root, f)
                downloaded.append({"path": fp, "name": f, "size": os.path.getsize(fp)})
        if not downloaded:
            return {"error": "文件夹下载完成但未找到文件"}
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
        if _auto_delete_cloud:
            _run_bpcs(["rm", full_path], timeout=30)
        return {"path": local_path, "size": os.path.getsize(local_path), "name": file_name}

def _get_account_folder() -> str:
    """从 BPCS 配置读取当前登录账号的 uid 和 name，返回账号文件夹名（如 620186943_hitomi999）。"""
    cfg_path = os.path.join(
        os.environ.get("BAIDUPCS_GO_CONFIG_DIR", ""),
        "pcs_config.json"
    ) if os.environ.get("BAIDUPCS_GO_CONFIG_DIR") else os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming", "BaiduPCS-Go", "pcs_config.json"
    )
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


def _format_size(raw_size: str) -> str:
    """解析 BPCS ls 输出的文件大小字符串并格式化。"""
    try:
        raw = raw_size.upper()
        if raw.endswith("GB"):
            return f"{float(raw[:-2]):.2f} GB"
        elif raw.endswith("MB"):
            return f"{float(raw[:-2]):.2f} MB"
        elif raw.endswith("KB"):
            return f"{float(raw[:-2]):.2f} KB"
        elif raw.endswith("B"):
            return f"{float(raw[:-1]):.0f} B"
        elif raw.endswith("TB"):
            return f"{float(raw[:-2]):.2f} TB"
        else:
            return f"{float(raw):.0f} B"
    except (ValueError, IndexError):
        return raw_size


async def run_pipeline(surl: str, pwd: str, max_mb: int) -> dict:
    """Download file. Returns {"path": str, "size": int, "name": str} or {"error": str}"""
    try:
        dl = await asyncio.to_thread(download_share, surl, pwd, max_mb)
        return dl if isinstance(dl, dict) else {"error": "下载失败"}
    except Exception as e:
        logger.exception(f"[BaiduPan] pipeline error: {e}")
        return {"error": f"处理失败: {e}"}


@register(
    "astrbot_plugin_baidu_pan",
    "linker9527",
    "百度网盘分享文件自动下载发送",
    "1.1.0",
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

        # 设置 BaiduPCS-Go 下载目录（exe 可用时才设置）
        if _ensure_bpcs_exe():
            _run_bpcs(["config", "set", "-savedir", DOWNLOAD_DIR], timeout=15)
        else:
            logger.warning("[BaiduPan] BaiduPCS-Go.exe 不可用，请使用 /pan download 下载，配置将在下载后生效")

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
        if cleanup_hour >= 0 and cleanup_hour <= 23:
            _schedule_local_cleanup(cleanup_hour)
        else:
            logger.info("[BaiduPan] local auto-cleanup disabled")

        threading.Thread(target=_init_bpcs, daemon=True).start()

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

    async def _send_file(self, event: AstrMessageEvent, dl: dict, share_url: str = ""):
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
        elif send_mode == "onebot":
            ret = await self._send_onebot(event, file_path, name, size_mb, share_url)
            if ret:
                return ret
            return
        elif send_mode == "other":
            return await self._send_other(event, file_path, name, size_mb, share_url)
        else:
            ret = await self._send_onebot(event, file_path, name, size_mb, share_url)
            if ret:
                return ret
            return

    async def _send_onebot(self, event: AstrMessageEvent, file_path: str, name: str, size_mb: float, share_url: str = ""):
        """OneBot / napcat 路径：直接发送文件，失败则返回链接"""
        logger.info(f"[BaiduPan] _send_onebot: name={name}, size={size_mb:.1f}MB")
        try:
            event.chain_result([File(name=name, file=file_path)])
            logger.info(f"[BaiduPan] _send_onebot: direct send success")
            return event
        except Exception as e2:
            logger.warning(f"[BaiduPan] _send_onebot: direct send failed: {e2}")
            # 兜底：返回链接
            msg = f"⚠️ 文件 {size_mb:.1f}MB 发送失败"
            if share_url:
                msg += f"。请自行下载: {share_url}"
            event.chain_result(msg)
            return event

    async def _send_other(self, event: AstrMessageEvent, file_path: str, name: str, size_mb: float, share_url: str = ""):
        """其他平台路径：直接发送文件，失败则返回链接"""
        try:
            event.chain_result([File(name=name, file=file_path)])
            return event
        except Exception as e:
            logger.warning(f"[BaiduPan] send file failed: {e}")
            msg = "⚠️ 文件发送失败"
            if share_url:
                msg += f"，请自行下载: {share_url}"
            event.chain_result(msg)
            return event

    async def _send_official(self, event: AstrMessageEvent, file_path: str, name: str, size_mb: float, share_url: str = ""):
        """QQ官方机器人路径：<=200MB 走官方API上传，>200MB 只能返回链接"""
        logger.info(f"[BaiduPan] _send_official: name={name}, size={size_mb:.1f}MB, limit={FLASH_TASK_LIMIT_MB}MB")
        if size_mb <= FLASH_TASK_LIMIT_MB:
            logger.info(f"[BaiduPan] _send_official: file <= limit, sending directly")
            event.chain_result([File(name=name, file=file_path)])
            return event

        logger.info(f"[BaiduPan] _send_official: file > limit, returning link")
        msg = "⚠️ 文件超过200MB，QQ官方API暂不支持大文件上传"
        if share_url:
            msg += f"，请自行下载: {share_url}"
        event.chain_result(msg)
        return event

    @filter.llm_tool(name="pan_list")
    async def pan_list(self, event: AstrMessageEvent, link: str, pwd: str = ""):
        """查看百度网盘分享链接的目录结构。用户说"查一下这个百度网盘链接"、"看看里面有什么文件"时调用。

        Args:
            link(string): 百度网盘分享链接，如 https://pan.baidu.com/s/1abc123
            pwd(string): 提取码（可选）
        """
        if self._is_blacklisted(event):
            return
        surl = ""
        if link.startswith("http") or link.startswith("pan.baidu.com") or link.startswith("yun.baidu.com"):
            if not link.startswith("http"):
                link = "https://" + link
            surl, p2 = parse_share_link(link)
            pwd = p2 or pwd
        else:
            surl = link

        if not surl:
            return "无法解析链接"

        # 新链接时清理旧转存目录
        cloud_dir = CLOUD_SAVE_DIR or "/我的资源/AutoTransfer"
        if self._cached_surl and self._cached_surl != surl:
            _run_bpcs(["rm", cloud_dir], timeout=30)

        result = await asyncio.to_thread(list_share_content, surl, pwd)
        if "error" in result:
            return result["error"]

        # 缓存
        self._cached_surl = surl
        self._cached_pwd = pwd
        self._cached_tree = result["text"]
        self._cached_items = result.get("items", [])

        return result["text"]

    @filter.llm_tool(name="pan_download_dir")
    async def pan_download_dir(self, event: AstrMessageEvent, folder_path: str, link: str = "", pwd: str = ""):
        """下载百度网盘分享中的文件夹（含所有内容）。用户说"下载xxx文件夹"、"下载xxx里面的xxx文件夹"时调用。如果之前已查看过目录树，LLM应从树中找到完整路径。下载完成后，文件保存在本地目录：{DOWNLOAD_DIR}/<账号uid_用户名>/<文件夹名>/...（如 E:\\downloads\\620186943_hitomi999\\大气层包\\...），回复用户时把实际保存路径一起告诉用户。

        self._active_downloads = getattr(self, "_active_downloads", set())
        dl_key = f"tool:pan_download_dir:{event.get_sender_id()}"
        if dl_key in self._active_downloads:
                logger.warning(f"[BaiduPan] pan_download_dir: duplicate blocked: {dl_key}")
                return "⏳ 该文件正在下载中，请稍候..."
        self._active_downloads.add(dl_key)

        Args:
            folder_path(string): 文件夹路径，如 "大气层包" 或 "大气层包/子文件夹"，不含顶层目录名
            link(string): 百度网盘分享链接（可选，如未查看过目录则需提供）
            pwd(string): 提取码（可选）
        """
        if self._is_blacklisted(event):
            return

        surl = ""
        if link:
            if link.startswith("http") or link.startswith("pan.baidu.com") or link.startswith("yun.baidu.com"):
                if not link.startswith("http"):
                    link = "https://" + link
                surl, p2 = parse_share_link(link)
                pwd = p2 or pwd
            else:
                surl = link

        if surl:
            # 新链接，先转存
            cloud_dir = CLOUD_SAVE_DIR or "/我的资源/AutoTransfer"
            if self._cached_surl and self._cached_surl != surl:
                _run_bpcs(["rm", cloud_dir], timeout=30)
            result = await asyncio.to_thread(list_share_content, surl, pwd)
            if "error" in result:
                return result["error"]
            self._cached_surl = surl
            self._cached_pwd = pwd
            self._cached_tree = result["text"]
            self._cached_items = result.get("items", [])
        elif not self._cached_surl:
            return "请先查看目录: /pan <链接> [密码]"

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
                        event.plain_result(f"❌ {msg[1]}")
                    else:
                        event.plain_result(msg)
                except queue.Empty:
                    pass
                if file_info and not dl_future.done():
                    fname, total = file_info
                    current = _get_local_file_size(fname)
                    if current > 0 and total > 0:
                        pct = min(current / total * 100, 99.9)
                        event.plain_result(f"⏬ 下载中: {pct:.1f}%")
                await asyncio.sleep(self._progress_interval)
        dl = await dl_future
        if "error" in dl:
            return dl["error"]

        if dl.get("is_dir"):
            file_count = len(dl.get("files", []))
            event.plain_result(f"✅ 文件夹 '{folder_path}' 下载完成，共 {file_count} 个文件")
            event.plain_result(f"📁 保存路径: {dl['path']}")
            return f"文件夹 '{folder_path}' 下载完成，共 {file_count} 个文件，保存在 {dl['path']}"
        else:
            share_url = f"https://pan.baidu.com/s/{self._cached_surl}"
            await self._send_file(event, dl, share_url)
            return f"文件 '{folder_path}' 下载并发送完成"

    @filter.llm_tool(name="pan_download_all")
    async def pan_download_all(self, event: AstrMessageEvent, link: str = "", pwd: str = ""):
        """下载百度网盘分享中的所有文件。用户说"全部下载"、"下载所有文件"、"都下载"时调用。下载完成后，文件保存在本地目录：{DOWNLOAD_DIR}/<账号uid_用户名>/<文件名>（如 E:\\downloads\\620186943_hitomi999\\xxx.zip），回复用户时把实际保存路径一起告诉用户。

        self._active_downloads = getattr(self, "_active_downloads", set())
        dl_key = f"tool:pan_download_all:{event.get_sender_id()}"
        if dl_key in self._active_downloads:
                logger.warning(f"[BaiduPan] pan_download_all: duplicate blocked: {dl_key}")
                return "⏳ 该文件正在下载中，请稍候..."
        self._active_downloads.add(dl_key)

        Args:
            link(string): 百度网盘分享链接（可选，如未查看过目录则需提供）
            pwd(string): 提取码（可选）
        """
        if self._is_blacklisted(event):
            return

        surl = ""
        if link:
            if link.startswith("http") or link.startswith("pan.baidu.com") or link.startswith("yun.baidu.com"):
                if not link.startswith("http"):
                    link = "https://" + link
                surl, p2 = parse_share_link(link)
                pwd = p2 or pwd
            else:
                surl = link

        if surl:
            cloud_dir = CLOUD_SAVE_DIR or "/我的资源/AutoTransfer"
            if self._cached_surl and self._cached_surl != surl:
                _run_bpcs(["rm", cloud_dir], timeout=30)
            result = await asyncio.to_thread(list_share_content, surl, pwd)
            if "error" in result:
                return result["error"]
            self._cached_surl = surl
            self._cached_pwd = pwd
            self._cached_tree = result["text"]
            self._cached_items = result.get("items", [])
        elif not self._cached_surl:
            return "请先查看目录: /pan <链接> [密码]"

        # 下载整个转存目录
        prog_q = queue.Queue() if self._progress_enabled else None
        dl_future = asyncio.create_task(asyncio.to_thread(download_from_cloud, "", self._get_max_mb(), prog_q))
        if prog_q:
            while not dl_future.done():
                try:
                    msg = prog_q.get_nowait()
                    if isinstance(msg, tuple) and msg[0] == "_error":
                        event.plain_result(f"❌ {msg[1]}")
                    else:
                        event.plain_result(msg)
                except queue.Empty:
                    pass
                await asyncio.sleep(self._progress_interval)
        dl = await dl_future
        if "error" in dl:
            return dl["error"]

        file_count = len(dl.get("files", []))
        event.plain_result(f"✅ 全部文件下载完成，共 {file_count} 个文件")
        event.plain_result(f"📁 保存路径: {dl['path']}")
        return f"全部文件下载完成，共 {file_count} 个文件，保存在 {dl['path']}"

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
        # 下载锁：只对 dir/file 下载操作生效，非下载操作不锁
        self._active_downloads = getattr(self, '_active_downloads', set())
        dl_key = f"{event.get_sender_id()}:{str(args).strip()}"
        # 延迟到 dir/file 分支才加锁，避免 help/look 等操作被锁
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
            return

        if parts[0] == "download":
            yield event.plain_result("⏳ 正在从 GitHub 官方 release 下载 BaiduPCS-Go...")
            result = await asyncio.to_thread(_download_bpcs_exe)
            if "ok" in result:
                yield event.plain_result(f"✅ BaiduPCS-Go.exe 下载成功！现在可以使用 /pan login 等命令了。")
            else:
                yield event.plain_result(f"❌ 下载失败: {result.get('error', '未知错误')}")
            return

        # 以下命令都需要 BaiduPCS-Go.exe
        if not _ensure_bpcs_exe():
            yield event.plain_result("❌ BaiduPCS-Go.exe 缺失或损坏，请使用 /pan download 下载")
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
            yield event.plain_result("⏳ 正在注入 Cookies...")
            result = await asyncio.to_thread(login_cookies_bpcs, parts[1])
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
            import requests as req
            import json as _json
            sess = req.Session()
            sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
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
                        # 从 session 的 cookie jar 提取全部 cookie（已自动解析）
                        full_cookie = "; ".join(f"{k}={v}" for k, v in sess.cookies.items())
                        # 访问 pan.baidu.com 获取 pan 专用 cookie（csrfToken / PANPSC）
                        try:
                            await asyncio.to_thread(
                                lambda: sess.get("https://pan.baidu.com/disk/main", timeout=10)
                            )
                        except Exception:
                            pass
                        # 重新提取完整 cookie（含 pan 专用 cookie）
                        full_cookie = "; ".join(f"{k}={v}" for k, v in sess.cookies.items())
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
            dir_path = parts[1]
            # 下载锁检查
            if dl_key in self._active_downloads:
                logger.warning(f"[BaiduPan] on_pan: duplicate download blocked: {dl_key}")
                yield event.plain_result("⏳ 该文件正在下载中，请稍候...")
                return
            self._active_downloads.add(dl_key)
            _lock_acquired = True
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
                        else:
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
                    await asyncio.sleep(self._progress_interval)
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
            if _lock_acquired: self._active_downloads.discard(dl_key)
            return

        if parts[0] in ("file", "f"):
            # 下载文件（使用缓存的链接）
            if not self._cached_surl:
                yield event.plain_result("请先使用 /pan <链接> [密码] 查看目录")
                return
            if len(parts) < 2:
                yield event.plain_result("用法: /pan file <文件路径>")
                return
            f_path = parts[1]
            # 下载操作才加锁
            if dl_key in self._active_downloads:
                logger.warning(f"[BaiduPan] on_pan: duplicate download blocked: {dl_key}")
                yield event.plain_result("⏳ 该文件正在下载中，请稍候...")
                return
            self._active_downloads.add(dl_key)
            _lock_acquired = True
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
                        else:
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
                    await asyncio.sleep(self._progress_interval)
            dl = await dl_future
            logger.info(f"[BaiduPan] on_pan file: download complete, dl={dl}")
            if "error" in dl:
                logger.error(f"[BaiduPan] on_pan file: download error: {dl['error']}")
                yield event.plain_result(f"❌ {dl['error']}")
            else:
                logger.info(f"[BaiduPan] on_pan file: download complete, path={dl.get('path')}")
                yield event.chain_result([File(name=dl.get("name", "file"), file=dl["path"])])
            if _lock_acquired: self._active_downloads.discard(dl_key)
            return

        # 默认: /pan <链接> [密码] → 转存并展示目录树
        link = parts[0]
        pwd = parts[1] if len(parts) > 1 else ""
        surl = ""
        if link.startswith("http") or link.startswith("pan.baidu.com") or link.startswith("yun.baidu.com"):
            if not link.startswith("http"):
                link = "https://" + link
            surl, p2 = parse_share_link(link)
            pwd = p2 or pwd
        else:
            surl = link

        if not surl:
            yield event.plain_result("❌ 无法解析链接")
            return

        # 新链接时清理旧转存目录
        cloud_dir = CLOUD_SAVE_DIR or "/我的资源/AutoTransfer"
        if self._cached_surl and self._cached_surl != surl:
            _run_bpcs(["rm", cloud_dir], timeout=30)

        yield event.plain_result("⏳ 正在转存并获取目录结构...")
        result = await asyncio.to_thread(list_share_content, surl, pwd)
        if "error" in result:
            yield event.plain_result(f"❌ {result['error']}")
        else:
            self._cached_surl = surl
            self._cached_pwd = pwd
            self._cached_tree = result["text"]
            self._cached_items = result.get("items", [])
            yield event.plain_result(result["text"])