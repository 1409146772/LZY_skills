#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
jla_env —— 统一环境封装（本工具的唯一"环境真理源"）

解决的问题（都是实测踩过的坑）：
  1. 解释器割裂：历史上 jira 装在某个外部 venv，cantools 装在全局 Python312
     → 每个脚本都要判断"该用哪个 python"。现在统一到本工具自带的 .venv，
     本模块对外只暴露 venv_python() 一个入口。
  2. cp936 编码崩溃：frame_extractor.py 打印 "SOC↔MCU"，Windows 默认 cp936 下 argparse
     直接崩，退出码 120。实测 PYTHONUTF8=1 可根治（比 PYTHONIOENCODING 更彻底），
     由 child_env() 统一注入，调用方不必记得。
  3. import jira 被目录遮蔽：cwd 下若有名为 jira/ 的非包目录，
     `import jira` 会解析成命名空间包，报 "cannot import name 'JIRA'"。
     由 safe_cwd()（默认工具目录）+ cwd_shadows_jira() 探测统一避开。

**自包含**：本工具不依赖任何工具目录之外的东西（除了下面的 inputs.json）。
外部工具（jira-tool / frame_extractor / canlog_tool）都以源码形式放在 <TOOL>/vendor/ 下，
由 config.json 里的**相对路径**引用。所有路径一律经 tool_path() 解析，
所以整个目录可以 zip 走、解压到任意位置、换个盘符都不用改配置。

**输入路径（inputs.json，只读）**：调用前由 Claude 会话写好 <TOOL>/inputs.json，含三个路径：
  code —— 工程代码（含 CLAUDE.md 的 checkout）
  dbc  —— CAN 矩阵（喂给 BLF 解码）
  docs —— 整理好的 md 文档
这三个路径**严格只读**。这条约束不是文档约定，而是由 assert_writable() 在写入原语层强制的：
任何落到这三个根内的写操作都抛 InputWriteError。它是 tool_path() 规则的唯一例外
（这三个路径按定义在工具之外），所以走单独的解析路径 input_path()。

所有子进程一律经 run()/child_env() 发起，不要在别处直接 subprocess.run。
所有写入一律经 write_text()/write_json()/ensure_dir()，不要裸调 Path.mkdir()，
否则会绕过只读闸门。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# ---------------------------------------------------------------- 路径常量

# 本文件位于 <TOOL>/scripts/jla_env.py
TOOL_DIR = Path(__file__).resolve().parent.parent
SKILL_DIR = TOOL_DIR  # 源目录即安装目录（skills/ 下为副本）
SCRIPTS_DIR = TOOL_DIR / "scripts"
CONFIG_PATH = TOOL_DIR / "config.json"
INPUTS_PATH = TOOL_DIR / "inputs.json"  # 每次调用由 Claude 会话写入
VENDOR_DIR = TOOL_DIR / "vendor"  # 外部工具的本地副本（自包含的关键）

VENV_DIR = TOOL_DIR / ".venv"
VENV_PY = VENV_DIR / "Scripts" / "python.exe"  # Windows
VENV_PY_POSIX = VENV_DIR / "bin" / "python"  # 非 Windows 兜底

# 重建 venv 的兜底：uv 优先走 PATH，找不到再试这几个常见位置（都是相对的，不写死用户目录）
_UV_CANDIDATES = ("uv", "uv.exe")

# inputs.json 里允许的三个路径键，以及哪些是必需的
INPUT_KEYS = ("code", "dbc", "docs")
INPUT_REQUIRED = ("code",)  # 没代码就没法"结合代码分析"，其余可缺

# ---------------------------------------------------------------- 配置读取

_config_cache: Optional[Dict[str, Any]] = None


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取 config.json（带缓存）。找不到就抛，不静默兜底。"""
    global _config_cache
    if _config_cache is not None and path is None:
        return _config_cache
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"缺少配置文件 {p}\n"
            f"请从 config.example.json 复制一份并填写。"
        )
    # 容忍 // 注释：先剥掉整行 // 注释
    raw = p.read_text(encoding="utf-8")
    lines = []
    for ln in raw.splitlines():
        if ln.lstrip().startswith("//"):
            continue
        lines.append(ln)
    data = json.loads("\n".join(lines))
    if path is None:
        _config_cache = data
    return data


def cfg_get(*keys: str, default: Any = None) -> Any:
    """按路径取配置，如 cfg_get('paths', 'venv_py')。"""
    cur: Any = load_config()
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


# ---------------------------------------------------------------- 路径解析

def tool_path(value: Any) -> Optional[Path]:
    """
    config 里的路径 → 绝对 Path。**这是本工具自包含的核心。**

    规则：
      * 相对路径 → 相对 TOOL_DIR 解析（如 "vendor/jira-tool/scripts/ops"）
      * 绝对路径 → 原样返回（兼容老配置 / 想指向工具外的高级用法）
      * 空值 → None

    这样整个工具搬到任意目录都不用改配置：config.json 里只写相对路径。
    """
    if value is None or str(value).strip() == "":
        return None
    p = Path(str(value))
    return p if p.is_absolute() else (TOOL_DIR / p)


def cfg_path(*keys: str) -> Optional[Path]:
    """取一个配置里的路径并解析（tool_path + cfg_get 的组合，最常用）。"""
    return tool_path(cfg_get(*keys, default=None))


def rel_to_tool(p: Path) -> str:
    """把工具内路径渲染成相对路径（日志里用，避免输出里出现绝对路径）。"""
    try:
        return str(Path(p).resolve().relative_to(TOOL_DIR.resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return str(p)


# ---------------------------------------------------------------- 输入路径（只读）

# inputs.json 的缓存；resolve_inputs() 解析一次，全进程复用。
_inputs_cache: Optional[Dict[str, Any]] = None
# input_roots() 的缓存。is_under_input() 在 write_text 的热路径上，
# 每次 resolve 一遍太浪费，缓存后只做前缀比较。
_input_roots_cache: Optional[List[Path]] = None

_INPUTS_HOWTO = """请在调用前用 Write 工具写出 <工具目录>/inputs.json，例如：
{
  "code": "D:\\\\projects\\\\weiqiao\\\\WB101\\\\workspace\\\\FreeRTOS_S32K312",
  "dbc":  "D:\\\\projects\\\\weiqiao\\\\WB101\\\\dbc\\\\WB101_IHU_CAN矩阵_V2.4_20260722.dbc",
  "docs": "D:\\\\projects\\\\weiqiao\\\\WB101\\\\doc",
  "keys": ["LH2512024-5408"]
}
  code —— 工程代码根（必需；工具只读，绝不写入）
  dbc  —— CAN 矩阵（可选；缺省则 BLF 解码不完整）
  docs —— 整理好的 md 文档目录（可选）
  keys —— 只跑指定工单（可选；缺省按 config.jira.jql 拉）"""


class InputWriteError(RuntimeError):
    """试图写 inputs.json 里声明的只读路径。这是设计红线，不是可配置项。"""


def load_inputs(required: bool = True) -> Dict[str, Any]:
    """
    读取 inputs.json（带缓存）。找不到且 required → 抛，并给出写法示例。

    刻意不写回、不补全：这个文件由调用方每次覆盖重写，工具只读。
    """
    global _inputs_cache
    if _inputs_cache is not None:
        return _inputs_cache
    if not INPUTS_PATH.exists():
        if required:
            raise FileNotFoundError(
                f"缺少输入声明 {INPUTS_PATH}\n{_INPUTS_HOWTO}"
            )
        return {}
    try:
        data = json.loads(INPUTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{INPUTS_PATH} 不是合法 JSON：{e}\n{_INPUTS_HOWTO}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{INPUTS_PATH} 顶层必须是对象（dict）。\n{_INPUTS_HOWTO}")
    _inputs_cache = data
    return data


def input_path(key: str, required: bool = False) -> Optional[Path]:
    """
    取 inputs.json 里的某个路径并解析成绝对 Path。

    刻意**不走 tool_path()**：这是该规则的唯一例外。tool_path() 的职责是把配置路径
    解析到工具目录之内（自包含），而这三个路径按定义就在工具之外。

    required=True 时：键缺失或路径不存在都抛，且报错发生在 prepare 之前，
    不会跑到一半才发现路径写错。
    """
    raw = load_inputs(required=required).get(key)
    if raw is None or str(raw).strip() == "":
        if required:
            raise FileNotFoundError(
                f"inputs.json 缺少必需的 \"{key}\" 路径。\n{_INPUTS_HOWTO}"
            )
        return None
    cand = Path(str(raw)).expanduser()
    try:
        p = cand if cand.is_absolute() else (Path.cwd() / cand)
        p = p.resolve()
    except OSError:
        p = cand
    if required and not p.exists():
        raise FileNotFoundError(
            f"inputs.json 里的 \"{key}\" 路径不存在：{p}\n"
            f"（请检查路径拼写；工具只读它，不会创建。）"
        )
    return p


def input_roots() -> List[Path]:
    """
    需要保护的全部只读根（resolve 后去重）。

    注意：即使某个键是可选的，只要它在 inputs.json 里出现了，就要保护它 ——
    否则写错一个可选路径反而绕过了闸门。
    """
    global _input_roots_cache
    if _input_roots_cache is not None:
        return _input_roots_cache
    out: List[Path] = []
    data = load_inputs(required=False)
    for key in INPUT_KEYS:
        raw = data.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        cand = Path(str(raw)).expanduser()
        try:
            r = cand.resolve() if cand.is_absolute() else (Path.cwd() / cand).resolve()
        except OSError:
            r = cand
        if r not in out:
            out.append(r)
    _input_roots_cache = out
    return out


def is_under_input(p: Path) -> bool:
    """
    p 是否落在只读输入根内。

    两边都 resolve 后再比 —— 否则 `..\\..\\` 与符号链接能绕过朴素的前缀比较。
    Windows 大小写不敏感，故用 normcase 归一。
    """
    roots = input_roots()
    if not roots:
        return False
    try:
        target = os.path.normcase(str(Path(p).resolve()))
    except OSError:
        target = os.path.normcase(str(p))
    for r in roots:
        rr = os.path.normcase(str(r))
        if target == rr or target.startswith(rr + os.sep):
            return True
    return False


def assert_writable(p: Path, *, why: str = "") -> Path:
    """
    写入前的唯一闸门。落在只读输入根内 → 抛 InputWriteError。

    why 由调用方说明"这是要写什么"，便于从 traceback 直接定位是谁想写。
    返回原 p，方便写成守卫式用法：p = assert_writable(p, why="报告")。
    """
    if is_under_input(p):
        tag = f"（{why}）" if why else ""
        raise InputWriteError(
            f"拒绝写入只读输入路径{tag}：{p}\n"
            f"  受保护的根：{', '.join(str(r) for r in input_roots())}\n"
            f"  code/dbc/docs 是只读的 —— 要产出请写入工具自己的 workspace/ 下。"
        )
    return Path(p)


def ensure_dir(path: Path) -> Path:
    """
    建目录。write_text() 的内部 mkdir 只覆盖叶子，散落的 Path.mkdir() 会绕过闸门，
    所以统一走这里。
    """
    p = assert_writable(path, why="ensure_dir")
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- venv 与解释器


def venv_python() -> str:
    """
    返回本工具 .venv 的解释器。

    约定位置是 <TOOL>/.venv —— 它跟着工具走，所以搬迁后依然有效，这是默认。
    config.paths.venv_py 只在想把 venv 放到工具外时才需要填（可写相对或绝对）。
    两者都不存在则明确报错并给出修复命令。
    """
    configured = cfg_get("paths", "venv_py", default=None)
    candidates: List[Path] = []
    if configured:
        tp = tool_path(configured)
        if tp:
            candidates.append(tp)
    candidates.append(VENV_PY)
    candidates.append(VENV_PY_POSIX)

    for c in candidates:
        if c.exists():
            return str(c)

    uv = uv_exe()
    py = cfg_get("paths", "base_python", default="") or "3.12"
    raise FileNotFoundError(
        f"未找到本工具的 .venv 解释器（找过：{[str(c) for c in candidates]}）。请先建立环境：\n"
        f'  "{uv}" venv --python {py} "{VENV_DIR}"\n'
        f'  "{uv}" pip install --python "{VENV_PY}" -r "{TOOL_DIR / "requirements.txt"}"\n'
        f"\n注：.venv 不随分发包提供，解压后必须自己建一次。"
    )


def self_python() -> str:
    """
    返回"当前正在运行本模块的解释器"。
    如果调用方就是用 venv 跑的，等同于 venv_python()；
    否则退回 venv_python()，保证子进程一致。
    """
    return sys.executable or venv_python()


def uv_exe() -> str:
    """
    返回 uv 可执行文件。优先 config.paths.uv，其次 PATH，最后报错。
    不写死任何用户目录 —— 这是能打包的前提。
    """
    configured = cfg_get("paths", "uv", default=None)
    if configured:
        tp = tool_path(configured)
        if tp and tp.exists():
            return str(tp)
        found = shutil.which(str(configured))
        if found:
            return found
    for name in _UV_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError(
        "找不到 uv（用于重建 .venv）。请安装 uv 或把它加进 PATH：\n"
        "  https://docs.astral.sh/uv/getting-started/installation/\n"
        "  或在 config.json 的 paths.uv 里写 uv 的完整路径。"
    )


# ---------------------------------------------------------------- 子进程


def cwd_shadows_jira(cwd: Optional[Path] = None) -> bool:
    """
    探测「在 cwd 下跑会不会让 `import jira` 被同名目录遮蔽」。

    历史坑：cwd 下若有个名为 jira/ 的子目录（用户的**项目**目录，不是 Python 包），
    `import jira` 会解析成命名空间包，报 "cannot import name 'JIRA'"。
    改用通用探测而不是写死某个具体路径 —— 这样工具搬到哪里都成立。
    """
    try:
        here = Path(cwd or Path.cwd()).resolve()
    except OSError:
        return False
    shadow = here / "jira"
    # 同名目录存在、且不是真正的 jira 包（没有 __init__.py）→ 会遮蔽
    return shadow.is_dir() and not (shadow / "__init__.py").exists()


def safe_cwd() -> str:
    """
    返回安全的 cwd。默认就是 **本工具目录** —— 工具目录下没有 jira/ 子目录，
    天然安全，而且这样所有相对路径都相对工具根解析，搬迁后行为一致。
    若调用方恰好在会被遮蔽的目录里，则退到工具目录。
    """
    try:
        here = Path.cwd().resolve()
    except OSError:
        return str(TOOL_DIR)
    if cwd_shadows_jira(here):
        return str(TOOL_DIR)
    return str(here)


def child_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    构造子进程环境：在父环境基础上强制注入 UTF-8 相关变量。
    PYTHONUTF8=1 是根治 cp936 崩溃的关键（实测 frame_extractor 退出码 120 → 0）。
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if extra:
        env.update(extra)
    return env


def run(
    args: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    env_extra: Optional[Dict[str, str]] = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """
    统一的子进程入口。所有 python 调用都应该走这里。

    cwd 默认用 safe_cwd()（默认工具目录；若当前 cwd 会遮蔽 import jira 则退回工具目录）。
    """
    return subprocess.run(
        list(args),
        cwd=cwd if cwd is not None else safe_cwd(),
        env=child_env(env_extra),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
    )


def run_python(
    args: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    env_extra: Optional[Dict[str, str]] = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """用本工具 venv 的解释器跑脚本/模块。"""
    return run(
        [venv_python(), *args],
        cwd=cwd,
        timeout=timeout,
        env_extra=env_extra,
        check=check,
    )


def run_json(
    args: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    env_extra: Optional[Dict[str, str]] = None,
    allow_empty: bool = False,
) -> Any:
    """
    跑一个"stdout 只出 JSON"的命令并解析结果。

    ⚠ args[0] 必须是**可执行文件**。若要跑 .py 脚本，请用 run_python_json()
      （直接以 .py 作为 args[0] 会得到 WinError 193 —— Windows 不会把 .py 当可执行文件）。

    沿用 jira-tool 的约定：退出码 0 成功 / 1 脚本错误 / 2 参数错误 / 3 鉴权失败 / 4 API 错误。
    失败时抛 RuntimeError，把 stderr 原样带出（不猜原因）。
    """
    cp = run(args, cwd=cwd, timeout=timeout, env_extra=env_extra)
    if cp.returncode != 0:
        raise RuntimeError(
            f"命令失败（退出码 {cp.returncode}）：{' '.join(args)}\n"
            f"stderr: {cp.stderr.strip()[:2000]}"
        )
    out = (cp.stdout or "").strip()
    if not out:
        if allow_empty:
            return None
        raise RuntimeError(f"命令没有输出 JSON：{' '.join(args)}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"输出不是合法 JSON（{e}）：{' '.join(args)}\n"
            f"stdout 前 500 字：{out[:500]}"
        ) from e


# ---------------------------------------------------------------- jira-tool 复用


def run_python_json(
    args: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    env_extra: Optional[Dict[str, str]] = None,
    allow_empty: bool = False,
) -> Any:
    """
    用本工具 venv 的解释器跑脚本，并把 stdout 当 JSON 解析。
    这是跑 jira-tool ops 脚本的正确入口（传脚本路径，不必自己拼解释器）。
    """
    return run_json(
        [venv_python(), *args],
        cwd=cwd, timeout=timeout, env_extra=env_extra, allow_empty=allow_empty,
    )


def jira_ops_dir() -> str:
    """jira-tool 的 ops 脚本目录（工具内 vendor 副本）。"""
    d = cfg_path("paths", "jira_ops")
    if not d:
        raise KeyError("config.paths.jira_ops 未配置")
    if not d.exists():
        raise FileNotFoundError(
            f"找不到 jira-tool ops 目录：{d}\n"
            f"vendor/ 可能不完整，请重跑：python tools/sync_vendor.py"
        )
    return str(d)


def jira_ops_script(name: str) -> str:
    """取 jira-tool 某个 ops 脚本的绝对路径，如 'search.py'。"""
    return str(Path(jira_ops_dir()) / name)


def jira_tool_scripts_dir() -> str:
    """jira-tool 的 scripts/ 目录（可 sys.path.insert 后 import jira_ops）。"""
    return str(Path(jira_ops_dir()).parent)


def import_jira_ops():
    """
    在当前进程内复用 jira-tool 的 jira_bootstrap / jira_ops（vendor 副本）。
    注意：调用方进程的 cwd 必须安全（本模块 import 时不会改 cwd）。
    cwd 是否会遮蔽 import jira 用 cwd_shadows_jira() 探测；工具目录默认安全。
    """
    scripts = jira_tool_scripts_dir()
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import jira_bootstrap as jb  # type: ignore
    import jira_ops as jo  # type: ignore

    return jb, jo


# ---------------------------------------------------------------- 只读 git


def git_readonly(cwd: Path, *args: str, timeout: int = 20) -> Optional[str]:
    """
    在 code 根上跑**只读** git 查询，返回 stdout（失败返回 None）。

    只允许读取类子命令 —— 调用方传进来的 args 由本函数的白名单把关。
    用户的 code 根是他正在用的 checkout（带真实 .git），任何写操作都是灾难。
    """
    allowed = {
        "rev-parse", "rev-list", "log", "status", "show-ref",
        "symbolic-ref", "describe", "ls-files", "branch", "remote",
    }
    if not args or args[0] not in allowed:
        raise ValueError(f"git_readonly 只允许读取类子命令，收到：{args[0] if args else '(空)'}")
    try:
        cp = run(["git", *args], cwd=str(cwd), timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    return (cp.stdout or "").strip()


# ---------------------------------------------------------------- 小工具


def read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, content: str) -> None:
    # 只读闸门：所有文本写入都从这里过，是拦截"写用户输入路径"最有效的单点
    p = assert_writable(path, why="write_text")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def write_json(path: Path, obj: Any) -> None:
    p = assert_writable(path, why="write_json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def workspace_root() -> Path:
    """
    工作区根目录。默认 <TOOL>/workspace —— 跟着工具走，搬迁后依然正确。
    config.paths.workspace_root 可覆盖（相对则按工具根解析）。
    """
    root = cfg_path("paths", "workspace_root") or (TOOL_DIR / "workspace")
    root.mkdir(parents=True, exist_ok=True)
    return root


def out_root() -> Path:
    d = workspace_root() / "out"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log(msg: str) -> None:
    """统一日志前缀，便于从 run.log 里筛。"""
    print(f"[jla] {msg}", flush=True)


if __name__ == "__main__":
    # 自检：直接跑本文件可看环境摘要。
    # 与 jla.py 同理，本进程 stdout 也要 UTF-8 —— 否则 cp936 控制台下 ✓/✗ 直接崩。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    print("TOOL_DIR :", TOOL_DIR)
    print("CONFIG   :", CONFIG_PATH, "(exists)" if CONFIG_PATH.exists() else "(MISSING)")
    try:
        print("VENV_PY  :", venv_python())
    except FileNotFoundError as e:
        print("VENV_PY  : MISSING\n", e)
    print("SAFE_CWD :", safe_cwd())
    print("SHADOWED :", cwd_shadows_jira(), "（cwd 是否会让 import jira 被遮蔽）")
    try:
        print("UV       :", uv_exe())
    except FileNotFoundError as e:
        print("UV       : MISSING\n", e)
    print("WORKSPACE:", rel_to_tool(workspace_root()))
    print("-" * 60)
    print("vendor/ 解析结果（相对工具根）：")
    for key in ("jira_ops", "frame_extractor", "canlog_dir", "lark_cli"):
        p = cfg_path("paths", key)
        if p is None:
            print(f"  {key:16} (未配置)")
        else:
            mark = "✓" if p.exists() else "✗"
            print(f"  {mark} {key:16} {rel_to_tool(p)}")
    print("-" * 60)
    print("输入路径（只读，来自 inputs.json）：")
    print(" ", INPUTS_PATH, "(exists)" if INPUTS_PATH.exists() else "(MISSING)")
    for key in INPUT_KEYS:
        need = key in INPUT_REQUIRED
        try:
            p = input_path(key, required=need)
        except (FileNotFoundError, ValueError) as e:
            print(f"  ✗ {key:6} {e}")
            continue
        if p is None:
            print(f"    {key:6} (未提供，可选)")
        else:
            print(f"  {'✓' if p.exists() else '✗'} {key:6} {p}")
    print(f"  只读保护  {'已启用' if input_roots() else '未启用'}")
