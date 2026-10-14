#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日自动签到（跨平台版 v2）
=====================================

在 WorkBuddy 客户端**已启动**的前提下，自动完成当日签到并领取赠送积分。

支持的平台
----------
  Windows : 完整支持（tasklist + ReadProcessMemory + DPAPI + state.vscdb）
  Linux   : 完整支持（ps + /proc/<pid>/maps|mem，同用户免 root）
  macOS   : 受限支持（ps 探测 + state.vscdb；**无法读取其他进程内存**，
            因为 macOS 需要 root 才能 task_for_pid，做不到"开箱即用"）

取令牌策略（惰性、逐个"验活"，命中即止）
----------------------------------------
  1. 本地加密缓存（仅当本机存在原生加密能力时启用，如 Windows DPAPI；
     Linux/macOS 出于不落明文令牌的考虑**默认不缓存**）
  2. WorkBuddy 运行内存中的活跃令牌（**主要来源**；磁盘副本通常已被服务端撤销）
  3. 磁盘持久化令牌（state.vscdb，Electron safeStorage 加密；回退路径）

接口（与客户端同源）
--------------------
  POST {endpoint}/v2/billing/meter/checkin-activity-status   查询签到活动状态
  POST {endpoint}/v2/billing/meter/daily-checkin             领取当日签到积分

请求头：Authorization: Bearer <token> / X-User-Id / X-Domain
        **不可携带 X-Device-Token**（会被网关以 invalid_token 拒绝）

安全设计
--------
  * 令牌绝不写入 stdout / 日志
  * endpoint 必须命中**硬编码**官方主机白名单（https、无端口、无 query/fragment），
    防止令牌被诱导发往非官方主机
  * 失败区分类型：probe / platform / network / auth / business / config / timeout
  * 不 fail-open：状态字段缺失时不会静默跳过，而是继续尝试签到（服务端幂等保护）
  * "客户端未运行"与"探测失败"严格区分，后者按错误处理

退出码
------
  0  成功 / 今天已签到 / WorkBuddy 未运行（按约定跳过）
  1  失败（探测失败 / 平台不支持 / 未登录 / 令牌不可得 / 网络或接口异常）

可选参数
--------
  --status-only  只查询状态，不执行领取（也不写缓存）
  --verbose      把候选来源摘要写入日志（不含令牌内容）
  --doctor       打印环境自检报告后退出（不访问网络、不写缓存）
"""

import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 平台判定
# ---------------------------------------------------------------------------
if sys.platform.startswith("win"):
    PLATFORM = "windows"
elif sys.platform == "darwin":
    PLATFORM = "macos"
else:
    PLATFORM = "linux"

_PLATFORM_NAMES = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}
PLATFORM_LABEL = _PLATFORM_NAMES[PLATFORM]

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
HOME = Path(os.path.expanduser("~"))
CACHE_DIR = HOME / ".workbuddy" / "workbuddy-daily-checkin"
TOKEN_CACHE = CACHE_DIR / ".token_cache"
LOG_FILE = CACHE_DIR / "checkin.log"
LOG_MAX_BYTES = 512 * 1024

FALLBACK_ENDPOINT = "https://www.workbuddy.cn"

# 官方域名白名单：作为**硬编码的信任锚**，绝不从可写文件(如 product.json)读取。
OFFICIAL_HOSTS = {
    "www.workbuddy.cn",
    "staging.workbuddy.cn",
    "wb.tencentbuddy.com",
    "copilot.tencent.com",
    "staging-copilot.tencent.com",
    "www.codebuddy.ai",
    "staging-codebuddy.tencent.com",
}

STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"

_UA_OS = {
    "windows": "Windows NT 10.0; Win64; x64",
    "macos": "Macintosh; Intel Mac OS X 10_15_7",
    "linux": "X11; Linux x86_64",
}


def ua_for(platform: str) -> str:
    """按平台生成 User-Agent（纯函数，便于各平台分别断言）。"""
    os_part = _UA_OS.get(platform, _UA_OS["linux"])
    return (f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/138.0.0.0 Safari/537.36 WorkBuddy/5.8.2")


UA = ua_for(PLATFORM)

HTTP_TIMEOUT = 8            # 单次请求超时(秒)
TOKEN_TTL_SECONDS = 1800    # 缓存令牌有效期(秒)
MAX_MEMORY_CANDIDATES = 20  # 参与验活的内存候选上限(按长度优先)
TIME_BUDGET = 100           # 整个取令牌阶段的时间预算(秒)

VERBOSE = "--verbose" in sys.argv
STATUS_ONLY = "--status-only" in sys.argv
DOCTOR = "--doctor" in sys.argv


# ---------------------------------------------------------------------------
# 日志（带简易轮转；绝不写入令牌）
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 路径发现（跨平台：多候选，取存在的第一个）
# ---------------------------------------------------------------------------
def user_data_dirs() -> list:
    """WorkBuddy(Electron) userData 目录候选，按平台给出。"""
    cands = []
    if PLATFORM == "windows":
        ad = os.environ.get("APPDATA")
        if ad:
            cands.append(Path(ad) / "WorkBuddy")
        cands.append(HOME / "AppData" / "Roaming" / "WorkBuddy")
    elif PLATFORM == "macos":
        cands.append(HOME / "Library" / "Application Support" / "WorkBuddy")
        cands.append(HOME / "Library" / "Application Support" / "workbuddy")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else HOME / ".config"
        cands.append(base / "WorkBuddy")
        cands.append(base / "workbuddy")
        cands.append(base / "WorkBuddy (Beta)")
    out, seen = [], set()
    for c in cands:
        if str(c) not in seen:
            seen.add(str(c))
            out.append(c)
    return out


def product_json_candidates() -> list:
    """客户端 product.json 候选路径（用于读取官方 endpoint）。"""
    # 显式覆盖：一旦设置则**唯一**生效（便于定制路径与自动化测试）
    env = os.environ.get("WORKBUDDY_PRODUCT_JSON")
    if env:
        return [Path(env)]
    cands = []
    tail = Path("resources") / "app.asar.unpacked" / "cli" / "product.json"
    if PLATFORM == "windows":
        la = os.environ.get("LOCALAPPDATA")
        if la:
            cands.append(Path(la) / "Programs" / "WorkBuddy" / tail)
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            pf = os.environ.get(var)
            if pf:
                cands.append(Path(pf) / "WorkBuddy" / tail)
        cands.append(HOME / "AppData" / "Local" / "Programs" / "WorkBuddy" / tail)
    elif PLATFORM == "macos":
        for app in ("WorkBuddy", "workbuddy"):
            cands.append(Path("/Applications") / f"{app}.app" / "Contents" / tail)
            cands.append(HOME / "Applications" / f"{app}.app" / "Contents" / tail)
    else:
        for base in ("/usr/share", "/usr/lib", "/opt", "/usr/local/lib", "/usr/local/share"):
            cands.append(Path(base) / "workbuddy" / tail)
            cands.append(Path(base) / "WorkBuddy" / tail)
        cands.append(HOME / ".local" / "share" / "workbuddy" / tail)
    out, seen = [], set()
    for c in cands:
        if str(c) not in seen:
            seen.add(str(c))
            out.append(c)
    return out


def find_user_data() -> Path:
    for d in user_data_dirs():
        try:
            if d.is_dir():
                return d
        except Exception:
            continue
    return user_data_dirs()[0]


def find_product_json():
    # 显式覆盖优先且具权威性（即使文件不存在也按"不可读"处理，不回落到其它候选）
    env = os.environ.get("WORKBUDDY_PRODUCT_JSON")
    if env:
        return Path(env)
    for p in product_json_candidates():
        try:
            if p.is_file():
                return p
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# 原生加密能力（用于本地令牌缓存）
#   仅当存在**真正的**原生加密时才启用缓存；否则一律不落盘（避免明文令牌）。
# ---------------------------------------------------------------------------
if PLATFORM == "windows":
    import ctypes
    import ctypes.wintypes as wt

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def secret_protect(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)

    def secret_unprotect(blob: bytes):
        buf = ctypes.create_string_buffer(blob, len(blob))
        blob_in = _DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
else:
    # 非 Windows：不提供保护 → 缓存被禁用（见 save_cached_token）
    def secret_protect(data: bytes):
        return None

    def secret_unprotect(blob: bytes):
        return None


def cache_enabled(platform: str = "") -> bool:
    """是否允许把令牌落盘缓存。

    仅当平台具备**原生加密**能力时启用（当前仅 Windows DPAPI）。否则一律不落盘，
    从根本上避免在 Linux/macOS 上产生明文令牌文件。
    """
    return (platform or PLATFORM) == "windows"


CACHE_ENABLED = cache_enabled()


# ---------------------------------------------------------------------------
# 客户端进程探测（严格区分 未运行 / 探测失败）
# ---------------------------------------------------------------------------
def _pids_from_tasklist() -> tuple:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=25)
    except FileNotFoundError:
        return None, "tasklist 不可用"
    except subprocess.TimeoutExpired:
        return None, "tasklist 超时"
    except Exception as e:
        return None, f"tasklist 调用异常: {type(e).__name__}"
    if out.returncode != 0:
        return None, f"tasklist 返回码 {out.returncode}"
    pids = []
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "workbuddy.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids, ""


# 精确匹配 WorkBuddy 可执行名：`workbuddy` / `WorkBuddy` / `workbuddy Helper`。
# 不匹配 `notworkbuddy-helper`、`myworkbuddy-cli` 等含该子串的无关进程。
_WB_COMM_RE = re.compile(r"^workbuddy([-\s].*)?$", re.IGNORECASE)


def parse_ps_output(text: str, self_pid: int = 0) -> list:
    """解析 `ps -A -o pid=,comm=` 输出，返回匹配 WorkBuddy 的 pid 列表（纯函数，便于测试）。

    仅精确匹配可执行名（允许 `workbuddy`、`WorkBuddy`、`workbuddy Helper` 等带空格/短横线
    的形态），杜绝 `notworkbuddy-helper` 这类**子串**误匹配——避免去读无关进程的内存。
    """
    pids = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_s, comm = parts
        base = os.path.basename(comm)
        if not _WB_COMM_RE.match(base):
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid != self_pid:
            pids.append(pid)
    return pids


def _pids_from_ps() -> tuple:
    """POSIX：用 ps 列出进程，按进程名匹配 WorkBuddy。"""
    if not shutil.which("ps"):
        return None, "ps 不可用"
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,comm="],
                             capture_output=True, timeout=25)
    except subprocess.TimeoutExpired:
        return None, "ps 超时"
    except Exception as e:
        return None, f"ps 调用异常: {type(e).__name__}"
    if out.returncode != 0:
        return None, f"ps 返回码 {out.returncode}"
    return parse_ps_output(out.stdout.decode("utf-8", "replace"), os.getpid()), ""


def probe_client() -> tuple:
    """返回 (state, pids, detail)；state ∈ {'running','not-running','probe-failed'}"""
    if PLATFORM == "windows":
        pids, detail = _pids_from_tasklist()
    else:
        pids, detail = _pids_from_ps()
    if pids is None:
        return "probe-failed", [], detail
    return ("running" if pids else "not-running"), pids, ""


# ---------------------------------------------------------------------------
# 进程内存读取（跨平台抽象）
#   Windows: VirtualQueryEx + ReadProcessMemory
#   Linux  : /proc/<pid>/maps + /proc/<pid>/mem（同用户，免 root）
#   macOS  : 不可用（task_for_pid 需要 root），显式标记不支持
# ---------------------------------------------------------------------------
JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")
MAX_REGION = 64 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024


class MemoryReader:
    """只读进程内存读取器基类。不修改目标进程。"""
    available = True
    reason = ""

    def __init__(self, pid: int):
        self.pid = pid
        self._handle = None
        self._mbi = None
        self._ctypes = None

    def open(self) -> bool:
        return False

    def iter_regions(self):
        return iter(())

    def read(self, base: int, size: int):
        return None

    def close(self) -> None:
        pass


if PLATFORM == "windows":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PROCESS_QUERY_INFORMATION, _PROCESS_VM_READ = 0x0400, 0x0010
    _MEM_COMMIT, _PAGE_GUARD, _PAGE_NOACCESS = 0x1000, 0x100, 0x01
    _PAGE_READABLE = (0x02, 0x04, 0x20, 0x40)

    class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                    ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]

    class WindowsReader(MemoryReader):
        available = True
        reason = ""

        def open(self) -> bool:
            import ctypes as _ct
            self._ctypes = _ct
            self._mbi = _MEMORY_BASIC_INFORMATION()
            self._handle = _kernel32.OpenProcess(
                _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, self.pid)
            return bool(self._handle)

        def iter_regions(self):
            ct = self._ctypes
            addr = 0
            while True:
                if not _kernel32.VirtualQueryEx(self._handle, ct.c_void_p(addr),
                                                ct.byref(self._mbi), ct.sizeof(self._mbi)):
                    break
                base = self._mbi.BaseAddress or 0
                size = self._mbi.RegionSize
                if (self._mbi.State == _MEM_COMMIT and self._mbi.Protect in _PAGE_READABLE
                        and not (self._mbi.Protect & _PAGE_GUARD)
                        and self._mbi.Protect != _PAGE_NOACCESS
                        and 0 < size <= MAX_REGION):
                    yield base, size
                nxt = base + size
                if nxt <= addr:
                    break
                addr = nxt

        def read(self, base: int, size: int):
            ct = self._ctypes
            buf = ct.create_string_buffer(size)
            read = ct.c_size_t(0)
            ok = _kernel32.ReadProcessMemory(self._handle, ct.c_void_p(base), buf, size,
                                             ct.byref(read))
            if ok and read.value:
                return buf.raw[:read.value]
            return None

        def close(self) -> None:
            if self._handle:
                try:
                    _kernel32.CloseHandle(self._handle)
                except Exception:
                    pass
                self._handle = None


def parse_proc_maps(text: str) -> list:
    """解析 /proc/<pid>/maps 文本，返回可读区段 [(base, size)]（纯函数，便于测试）。

    仅保留权限位以 'r' 开头、长度合法的区段；忽略非可读页与异常行。
    """
    regions = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if not parts[1].startswith("r"):
            continue
        try:
            a, b = parts[0].split("-")
            base, end = int(a, 16), int(b, 16)
        except Exception:
            continue
        size = end - base
        if 0 < size <= MAX_REGION:
            regions.append((base, size))
    return regions


def _pread(fd: int, size: int, offset: int) -> bytes:
    """按偏移读取文件描述符。优先 os.pread（Unix）；缺失时退化为 lseek+read。"""
    if hasattr(os, "pread"):
        return os.pread(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, size)


class LinuxReader(MemoryReader):
    """通过 /proc/<pid>/maps 与 /proc/<pid>/mem 读取（同用户可读，无需 root）。"""
    available = True
    reason = ""

    def open(self) -> bool:
        self._maps = Path(f"/proc/{self.pid}/maps")
        self._fd = None
        try:
            if not self._maps.is_file():
                return False
            self._fd = os.open(f"/proc/{self.pid}/mem", os.O_RDONLY)
            return True
        except OSError:
            return False

    def iter_regions(self):
        try:
            text = self._maps.read_text(errors="replace")
        except Exception:
            return
        for base, size in parse_proc_maps(text):
            yield base, size

    def read(self, base: int, size: int):
        if self._fd is None:
            return None
        try:
            return _pread(self._fd, size, base)
        except OSError:
            return None

    def close(self) -> None:
        if getattr(self, "_fd", None) is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None


class UnavailableReader(MemoryReader):
    available = False

    def __init__(self, pid, reason=""):
        super().__init__(pid)
        self.reason = reason


def make_reader(pid: int) -> MemoryReader:
    if PLATFORM == "windows":
        return WindowsReader(pid)
    if PLATFORM == "linux":
        return LinuxReader(pid)
    return UnavailableReader(
        pid, "macOS 读取其他进程内存需要 root（task_for_pid），本工具不请求提权")


def memory_supported() -> bool:
    return PLATFORM in ("windows", "linux")


def _extract_jwts(data: bytes, found: set) -> None:
    for m in JWT_RE.finditer(data):
        found.add(m.group(0).decode("ascii", "ignore"))
    if b"e\x00y\x00J\x00" in data:      # UTF-16LE 形态
        u16 = data.decode("utf-16-le", "ignore").encode("ascii", "ignore")
        for m in JWT_RE.finditer(u16):
            found.add(m.group(0).decode("ascii", "ignore"))


def scan_pid_for_jwts(pid: int) -> set:
    """只读扫描指定进程内存，收集 JWT 形态候选。"""
    found = set()
    reader = make_reader(pid)
    if not reader.available:
        return found
    if not reader.open():
        reader.close()
        return found
    total = 0
    try:
        for base, size in reader.iter_regions():
            data = reader.read(base, size)
            if not data:
                continue
            total += len(data)
            _extract_jwts(data, found)
            if total > MAX_TOTAL:
                break
    except Exception:
        pass
    finally:
        reader.close()
    return found


def tokens_from_memory(pids) -> list:
    toks = set()
    for pid in pids:
        try:
            toks |= scan_pid_for_jwts(pid)
        except Exception:
            continue
    return sorted(toks, key=len, reverse=True)[:MAX_MEMORY_CANDIDATES]


# ---------------------------------------------------------------------------
# endpoint（强制白名单）
# ---------------------------------------------------------------------------
def get_endpoint() -> tuple:
    pj = find_product_json()
    try:
        if pj is None:
            cfg = {}
        elif not pj.is_file() or pj.stat().st_size > 4 * 1024 * 1024:
            # 非普通文件（如 FIFO，会阻塞）或异常超大文件：一律视为不可读
            return FALLBACK_ENDPOINT, "fallback(product.json 不可读)"
        else:
            cfg = json.loads(pj.read_text(encoding="utf-8"))
        ep = str(cfg.get("endpoint") or "").strip().rstrip("/")
    except Exception:
        return FALLBACK_ENDPOINT, "fallback(product.json 不可读)"

    if ep:
        try:
            parsed = urlparse(ep)
            valid = (parsed.scheme == "https"
                     and parsed.hostname in OFFICIAL_HOSTS
                     and parsed.port is None
                     and not parsed.query and not parsed.fragment)
        except Exception:
            valid = False
        if valid:
            return f"https://{parsed.hostname}", "product.json"
        log("警告: product.json 的 endpoint 未通过官方域名白名单校验，已回退内置常量")
    return FALLBACK_ENDPOINT, "fallback(白名单不匹配)"


def get_uid_from_config() -> str:
    """从 vscdb 明文配置读取账号 uid(仅作为交叉校验参考)。"""
    try:
        vscdb = find_user_data() / "User" / "globalStorage" / "state.vscdb"
        if not vscdb.is_file():
            return ""
        con = sqlite3.connect(f"file:{vscdb}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            cur.execute("SELECT value FROM ItemTable WHERE key=?",
                        ("Tencent-Cloud.coding-copilot",))
            row = cur.fetchone()
        finally:
            con.close()
        if row:
            cache = (json.loads(row[0]).get("cloud_product_config_cache") or [])
            if cache and isinstance(cache[0], dict) and cache[0].get("userId"):
                return str(cache[0]["userId"])
    except Exception:
        pass
    return ""


def jwt_subject(token: str) -> str:
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(data.get("sub") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 令牌来源 1: 本地加密缓存（仅在存在原生加密时启用）
# ---------------------------------------------------------------------------
def save_cached_token(token: str, uid: str) -> None:
    if not cache_enabled():
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        blob = json.dumps({"token": token, "uid": uid, "savedAt": int(time.time())}).encode()
        enc = secret_protect(blob)
        if not enc:
            return
        TOKEN_CACHE.write_bytes(enc)
        if PLATFORM == "windows":
            try:
                user = os.environ.get("USERNAME", "")
                # 仅接受形如普通用户名的值，避免被环境变量诱导把缓存授权给
                # Everyone/其它主体（例如 USERNAME=*S-1-1-0）。
                if re.fullmatch(r"[A-Za-z0-9._\- ]{1,64}", user):
                    subprocess.run(["icacls", str(TOKEN_CACHE), "/inheritance:r",
                                    "/grant:r", f"{user}:F"], capture_output=True, timeout=15)
            except Exception:
                pass
    except Exception:
        pass


def load_cached_token() -> tuple:
    if not cache_enabled():
        return "", ""
    try:
        if not TOKEN_CACHE.exists():
            return "", ""
        raw = secret_unprotect(TOKEN_CACHE.read_bytes())
        if not raw:
            raise ValueError("unprotect failed")
        blob = json.loads(raw.decode("utf-8"))
        if int(time.time()) - int(blob.get("savedAt", 0)) > TOKEN_TTL_SECONDS:
            raise ValueError("expired")
        return str(blob.get("token") or ""), str(blob.get("uid") or "")
    except Exception:
        try:
            TOKEN_CACHE.unlink(missing_ok=True)
        except Exception:
            pass
        return "", ""


def cleanup_legacy_cache() -> None:
    try:
        legacy = CACHE_DIR / ".live_token"
        if legacy.exists():
            legacy.unlink()
            log("已清理旧版明文令牌缓存文件")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 令牌来源 3: vscdb 持久化（Electron safeStorage 解密；回退路径）
# ---------------------------------------------------------------------------
def _safe_storage_master_key():
    """取得 Electron safeStorage 主密钥（各平台取法不同）。失败返回 None。"""
    ud = find_user_data()
    try:
        if PLATFORM == "windows":
            ls = json.loads((ud / "Local State").read_text(encoding="utf-8"))
            key = base64.b64decode(ls["os_crypt"]["encrypted_key"])
            if key[:5] == b"DPAPI":
                key = key[5:]
            return secret_unprotect(key)
        if PLATFORM == "macos":
            # macOS 上 Electron safeStorage 把密钥放在登录钥匙串
            for svc in ("WorkBuddy Safe Storage", "workbuddy Safe Storage"):
                try:
                    out = subprocess.run(
                        ["security", "find-generic-password", "-s", svc, "-w"],
                        capture_output=True, timeout=15)
                except Exception:
                    continue
                if out.returncode == 0:
                    pw = out.stdout.decode("utf-8", "replace").strip()
                    if pw:
                        return base64.b64decode(pw) if len(pw) >= 16 else pw.encode()
        else:
            # Linux 上 Electron safeStorage 走 libsecret
            if shutil.which("secret-tool"):
                try:
                    out = subprocess.run(
                        ["secret-tool", "lookup", "application", "workbuddy"],
                        capture_output=True, timeout=15)
                    if out.returncode == 0 and out.stdout.strip():
                        return base64.b64decode(out.stdout.strip())
                except Exception:
                    pass
    except Exception:
        return None
    return None


def token_from_vscdb() -> str:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return ""
    try:
        master = _safe_storage_master_key()
        if not master:
            return ""
        vscdb = find_user_data() / "User" / "globalStorage" / "state.vscdb"
        if not vscdb.is_file():
            return ""
        name = 'secret://' + json.dumps(
            {"extensionId": "tencent-cloud.coding-copilot",
             "key": "planning-genie.new.accessTokencn"}, separators=(",", ":"))
        con = sqlite3.connect(f"file:{vscdb}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            cur.execute("SELECT value FROM ItemTable WHERE key=?", (name,))
            row = cur.fetchone()
        finally:
            con.close()
        if not row:
            return ""
        payload = bytes(json.loads(row[0])["data"])
        if payload[:3] not in (b"v10", b"v11"):   # Electron 升级到 v20 时该路径失效
            log("提示: vscdb 加密格式已变化，跳过磁盘令牌回退路径")
            return ""
        plain = AESGCM(master).decrypt(payload[3:15], payload[15:], None)
        session = json.loads(plain.decode("utf-8"))
        return str(session.get("auth", {}).get("accessToken")
                   or session.get("accessToken") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 官方接口调用（带错误分类）
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝跟随任何重定向。

    安全性关键：urllib 默认跟随 3xx，且会把 Authorization 等请求头**原样复制**到
    新主机。若官方域名被劫持/开放重定向，Bearer 令牌会被转发到非官方主机，从而
    绕过 endpoint 白名单这一信任锚。此处直接拒绝，3xx 交由 HTTPError 分支判定失败。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def post(endpoint: str, path: str, token: str, uid: str, timeout: int = HTTP_TIMEOUT):
    """返回 (status, payload, err_kind)；err_kind ∈ {None,'network','bad-json','redirect'}"""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-User-Id": uid,
        "X-Domain": "www.codebuddy.cn",
        "X-Request-ID": uuid.uuid4().hex,
        "User-Agent": UA,
    }
    req = urllib.request.Request(endpoint + path, data=b"{}", headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw), None
            except Exception:
                return r.status, raw[:300], "bad-json"
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        kind = "redirect" if 300 <= e.code < 400 else None
        try:
            return e.code, json.loads(raw), kind
        except Exception:
            return e.code, raw[:300], kind or "bad-json"
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}", "network"


def is_success(http_status: int, payload) -> bool:
    """业务成功 = HTTP 200 且业务码为 0；不把"缺 code"无条件视为成功（防 fail-open）。"""
    if http_status != 200 or not isinstance(payload, dict):
        return False
    if "code" in payload:
        return payload.get("code") == 0
    return isinstance(payload.get("data"), (dict, list))


def is_status_ok(http_status: int, payload) -> bool:
    """响应是否为一份有效的签到状态响应（用于令牌验活）。"""
    if not is_success(http_status, payload):
        return False
    data = payload.get("data")
    return isinstance(data, dict) and ("today_checked_in" in data or "active" in data)


def acquire_token(endpoint: str, uid_hint: str, pids):
    """惰性获取并验活令牌。

    返回 (token, uid, status_payload, diag)；全部失败时 token 为 None。
    diag = {'saw_server': bool, 'err_kind': str|None, 'tried': int, 'no_source': bool}
    """
    diag = {"saw_server": False, "err_kind": None, "tried": 0}
    deadline = time.time() + TIME_BUDGET

    def try_candidates(candidates):
        for tok in candidates:
            if not tok:
                continue
            if time.time() > deadline:
                diag["err_kind"] = diag["err_kind"] or "timeout"
                log("提示: 取令牌超出时间预算，提前结束尝试")
                return None
            diag["tried"] += 1
            sub = jwt_subject(tok)
            req_uid = sub or uid_hint
            code, payload, err = post(endpoint, STATUS_PATH, tok, req_uid)
            if code > 0:
                diag["saw_server"] = True
            if err == "network":
                if not diag["saw_server"]:
                    diag["err_kind"] = "network"
                continue
            if err == "redirect":
                # 官方域名返回 3xx → 视为异常（可能在诱导令牌外发），拒绝继续
                diag["err_kind"] = "redirect"
                continue
            if err == "bad-json":
                diag["err_kind"] = diag["err_kind"] or "bad-json"
                continue
            if is_status_ok(code, payload):
                if sub and uid_hint and sub != uid_hint:
                    log("提示: 令牌账号与配置账号不一致，已采用令牌声明账号")
                if not STATUS_ONLY:
                    save_cached_token(tok, req_uid)
                return tok, req_uid, payload
        return None

    # 1) 加密缓存
    cached_tok, cached_uid = load_cached_token()
    if cached_tok:
        if VERBOSE:
            log(f"候选来源: 缓存 (len={len(cached_tok)})")
        hit = try_candidates([cached_tok])
        if hit:
            return (*hit, diag)

    # 2) 内存（惰性）
    mem = []
    if time.time() <= deadline:
        mem = tokens_from_memory(pids)
    if VERBOSE:
        log(f"候选来源: 内存 {len(mem)} 个 (平台={PLATFORM}, 内存读取支持={memory_supported()})")
    hit = try_candidates(mem)
    if hit:
        return (*hit, diag)

    # 3) vscdb 回退
    disk = ""
    if time.time() <= deadline:
        disk = token_from_vscdb()
    if disk:
        if VERBOSE:
            log(f"候选来源: vscdb (len={len(disk)})")
        hit = try_candidates([disk])
        if hit:
            return (*hit, diag)

    # 预算在扫描之前就已耗尽（例如上层给了极小的 TIME_BUDGET）时，
    # 也必须如实归类为 timeout，而不是落入下面的 network 分支被误报。
    if diag["err_kind"] is None and time.time() > deadline:
        diag["err_kind"] = "timeout"

    return None, None, None, diag

# ---------------------------------------------------------------------------
# 环境自检（--doctor）
# ---------------------------------------------------------------------------
def doctor_report() -> dict:
    ud = find_user_data()
    pj = find_product_json()
    ep, ep_src = get_endpoint()
    state, pids, detail = probe_client()
    ud_cands = [{"path": str(p), "exists": p.is_dir()} for p in user_data_dirs()]
    pj_cands = [{"path": str(p), "exists": p.is_file()} for p in product_json_candidates()]
    rep = {
        "platform": _PLATFORM_NAMES.get(PLATFORM, PLATFORM),
        "python": sys.version.split()[0],
        "supported": True,
        "memory_read": memory_supported(),
        "cache_enabled": cache_enabled(),
        "user_data": str(ud),
        "user_data_candidates": ud_cands,
        "product_json": str(pj) if pj else None,
        "product_json_candidates": pj_cands,
        "endpoint": ep,
        "endpoint_source": ep_src,
        "client_state": state,
        "client_pids": pids,
        "client_detail": detail,
        "warnings": [],
        "verdict": "",
    }
    w = rep["warnings"]
    if PLATFORM == "macos":
        rep["supported"] = False
        w.append("macOS 无法在不提权的情况下读取 WorkBuddy 进程内存，"
                 "自动签到仅能依赖 state.vscdb 中的令牌（可能已失效）。")
    if not pj:
        w.append("未找到 product.json，将使用内置官方 endpoint（不影响正常使用）。")
    if not ud.is_dir():
        w.append("未找到 WorkBuddy userData 目录，可能尚未安装或从未启动过客户端。")
    if state == "not-running":
        w.append("当前未检测到运行中的 WorkBuddy 客户端——签到需要客户端处于运行状态。")
    elif state == "probe-failed":
        w.append(f"进程探测失败：{detail}")
    if rep["supported"] and memory_supported() and state == "running":
        rep["verdict"] = "环境就绪：可自动签到。"
    elif not rep["supported"]:
        rep["verdict"] = "受限：本平台无法开箱即用（见 warnings）。"
    elif state != "running":
        rep["verdict"] = "待确认：请先启动 WorkBuddy 后重试。"
    else:
        rep["verdict"] = "环境基本就绪。"
    return rep


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run() -> dict:
    cleanup_legacy_cache()

    state, pids, detail = probe_client()
    if state == "probe-failed":
        return {"ok": False, "error": f"无法探测 WorkBuddy 进程状态: {detail}",
                "error_kind": "probe",
                "hint": "请确认系统命令可用（Windows: tasklist；macOS/Linux: ps）"}
    if state == "not-running":
        return {"ok": True, "action": "skipped",
                "reason": "WorkBuddy 未运行，按约定跳过签到"}

    if not memory_supported():
        log(f"提示: 平台 {PLATFORM} 不支持内存读取，将仅尝试磁盘令牌路径")

    endpoint, ep_source = get_endpoint()
    uid_hint = get_uid_from_config()
    if not uid_hint:
        log("提示: 未能从配置读取 uid，将以令牌声明为准")

    token, uid, status_payload, diag = acquire_token(endpoint, uid_hint, pids)
    if not token:
        if diag.get("err_kind") == "redirect":
            return {"ok": False,
                    "error": "官方接口返回异常重定向，已中止以避免令牌被转发到非官方主机",
                    "error_kind": "security",
                    "hint": "可能存在网络劫持或开放重定向；请检查网络/代理环境后重试"}
        if diag.get("err_kind") == "timeout":
            return {"ok": False, "error": "取令牌超时(客户端繁忙或网络缓慢)",
                    "error_kind": "timeout",
                    "hint": "稍后重试；若持续出现请检查网络与客户端负载"}
        if diag.get("err_kind") == "bad-json" and diag.get("saw_server"):
            return {"ok": False,
                    "error": "WorkBuddy 服务返回异常响应(非 JSON)，无法确认签到状态",
                    "error_kind": "business",
                    "hint": "服务可能临时故障，请稍后重试"}
        if not diag.get("saw_server"):
            return {"ok": False,
                    "error": "无法连接 WorkBuddy 服务(网络/代理/证书问题)，未取得任何服务端响应",
                    "error_kind": "network",
                    "hint": "检查网络连接或代理设置后重试"}
        if not memory_supported():
            return {"ok": False,
                    "error": "本平台无法读取客户端内存，且磁盘令牌不可用",
                    "error_kind": "platform",
                    "hint": "macOS 需 root 才能读进程内存（本工具不提权）；"
                            "请在 Windows/Linux 上使用，或等待客户端把有效令牌持久化到本地"}
        return {"ok": False,
                "error": "未能获得有效登录令牌",
                "error_kind": "auth",
                "hint": "请确认 WorkBuddy 处于已登录状态；如刚重新登录，请稍后重试"}

    data = status_payload.get("data")
    if not isinstance(data, dict):
        data = {}

    # 字段缺失不等于"未开放"：只有明确 false 才判定活动未开放(fail-safe)
    raw_active = data.get("active")
    raw_checked = data.get("today_checked_in")
    status = {
        "active": raw_active if isinstance(raw_active, bool) else None,
        "today_checked_in": raw_checked if isinstance(raw_checked, bool) else None,
        "streak_days": data.get("streak_days"),
        "daily_credit": data.get("daily_credit"),
        "today_credit": data.get("today_credit"),
        "checkin_dates": data.get("checkin_dates") or [],
    }
    result = {"ok": True, "platform": _PLATFORM_NAMES.get(PLATFORM, PLATFORM),
              "endpoint": endpoint, "endpoint_source": ep_source, "status": status}

    if raw_active is False:
        result["action"] = "activity-inactive"
        return result
    if raw_checked is True:
        result["action"] = "already-checked-in"
        return result
    if STATUS_ONLY:
        result["action"] = "status-only"
        return result

    code, payload, err = post(endpoint, CHECKIN_PATH, token, uid)
    if err == "network":
        return {"ok": False, "error": f"签到请求网络失败: {payload}",
                "error_kind": "network",
                "note": "结果未知；服务端幂等，稍后重跑安全"}
    if err == "bad-json":
        return {"ok": False, "error": f"签到响应非 JSON(HTTP {code})", "error_kind": "business",
                "note": "结果未知；服务端幂等，稍后重跑安全"}

    if is_success(code, payload):
        d = payload.get("data")
        if not isinstance(d, dict):
            d = {}
        granted = d.get("credit")
        for k in ("granted_credit", "daily_credit"):
            if granted is None:
                granted = d.get(k)
        if granted is None:
            granted = status.get("daily_credit")
        result["action"] = "checked-in"
        result["checkin"] = {"granted_credit": granted,
                             "streak_days": d.get("streak_days", status.get("streak_days")),
                             "raw": d}
        return result

    msg = payload.get("msg") if isinstance(payload, dict) else str(payload)
    biz_code = payload.get("code") if isinstance(payload, dict) else None
    if biz_code == 10001 or "已签到" in str(msg):
        result["action"] = "already-checked-in"
        result["note"] = str(msg)
        return result
    return {"ok": False, "error": f"签到失败: {msg}", "error_kind": "business",
            "http_status": code, "code": biz_code}


def main() -> int:
    started = time.time()
    if DOCTOR:
        rep = doctor_report()
        rep["elapsed_ms"] = int((time.time() - started) * 1000)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep.get("supported") else 1
    try:
        result = run()
    except Exception as e:  # 顶层兜底：任何未预期异常都转成结构化失败
        result = {"ok": False, "error": f"未预期异常 {type(e).__name__}: {e}",
                  "error_kind": "internal"}
    result.setdefault("elapsed_ms", int((time.time() - started) * 1000))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    log(f"结果: {result.get('action') or result.get('error')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
