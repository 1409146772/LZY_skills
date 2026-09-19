# -*- coding: utf-8 -*-
"""jira-bootstrap：让一次性脚本以最小胶水连接到 python-jira 库（jira.cvte.com）。

本模块解决每个「临时脚本都要做但不想重复写」的基建工作：
  1. 强制 UTF-8 输出（Windows 默认 GBK 会把中文打成乱码，导致 AI/harness 读不到正确结果）。
  2. 凭证解析：环境变量 JIRA_PAT / JIRA_SERVER 优先，否则读本 skill 自己的 scripts/config.json（完全独立，与外部解耦）。
  3. 已鉴权的 JIRA 客户端（token_auth=PAT），带进程内缓存。
  4. 机器可读输出 helper（JSON，ensure_ascii=False）。
  5. 统一异常包装 guard()，把「写脚本」的错误处理收敛到一行。

安全约定（务必遵守）：
  - 【绝不】打印 PAT / Authorization 头 / 完整 config dump。
  - token 只在 load_credential() 内部取用，并直接传给 JIRA(token_auth=...)。
  - 需要向用户展示连接信息时用 describe_connection()（只含 server + 当前用户名）。

用法（临时脚本）：
    import sys
    sys.path.insert(0, r"C:\\Users\\user\\.claude\\skills\\jira-tool\\scripts")
    import jira_bootstrap as jb
    @jb.guard
    def main():
        jira = jb.connect()
        ...
        jb.out({...})        # stdout 只出 JSON
    raise SystemExit(main())
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# --- 1. 强制 UTF-8 输出（Windows 下 GBK 会乱码，这里统一 UTF-8）---
_io_enc = os.environ.get("PYTHONIOENCODING", "").lower()
if not _io_enc:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# --- 常量 ---
SKILL_DIR = Path(__file__).resolve().parent          # <skill>/scripts
OPS_DIR = SKILL_DIR / "ops"                          # 固定 CLI 脚本（scripts/ops/*.py）
_CONFIG = SKILL_DIR / "config.json"                  # 本 skill 独立凭证（勿提交/勿外发）
DEFAULT_SERVER = "https://jira.cvte.com"
_CLIENT = None                                       # 进程内缓存


def _mask(token: str) -> str:
    """mask token 用于（局部）调试；默认不对外暴露。"""
    return (token[:3] + "…" + token[-3:]) if token and len(token) > 8 else "***"


def load_credential() -> dict[str, str]:
    """返回 {server, token}。

    优先级：环境变量 JIRA_SERVER / JIRA_PAT > 本 skill scripts/config.json。
    缺失则 SystemExit 并给出补齐指引；【永不打印 token】。email 字段忽略。
    完全解耦：不读任何外部 skill 的凭证文件。
    """
    server = os.environ.get("JIRA_SERVER", "").strip()
    token = os.environ.get("JIRA_PAT", "").strip()

    if (not server or not token) and _CONFIG.exists():
        try:
            cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))
        except Exception as e:
            raise SystemExit(f"❌ 读取凭证文件失败：{_CONFIG}：{e}")
        server = server or (cfg.get("server") or "").strip()
        token = token or (cfg.get("token") or "").strip()

    if not server:
        raise SystemExit(
            "❌ 缺少 Jira 服务器：请设置环境变量 JIRA_SERVER，"
            f"或在本 skill 的 scripts/config.json 提供 server（当前默认 {DEFAULT_SERVER}）。"
        )
    if not token:
        raise SystemExit(
            "❌ 缺少 Jira PAT：请设置环境变量 JIRA_PAT，"
            "或在本 skill 的 scripts/config.json 提供 token。"
        )
    return {"server": server, "token": token}


def connect() -> "JIRA":
    """构建并缓存已鉴权客户端（token_auth）。鉴权失败会在此抛 JIRAError/SystemExit。

    成功返回 JIRA 实例；【不泄漏 token】。失败信息截断到首行、去掉多余内容。
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    from jira import JIRA

    cred = load_credential()
    from jira.exceptions import JIRAError
    try:
        _CLIENT = JIRA(server=cred["server"], token_auth=cred["token"])
    except JIRAError as e:
        status = getattr(e, "status_code", None) or 0
        first_line = (str(e).strip().splitlines() or [""])[0][:200]
        if status in (401, 403):
            # 鉴权失败 → 退出码 3（调用方据此提示换 PAT）
            print(f"❌ Jira 鉴权失败 HTTP {status}：PAT 过期或权限不足。"
                  "请更新环境变量 JIRA_PAT 或本 skill scripts/config.json 的 token。",
                  file=sys.stderr)
            raise SystemExit(3)
        raise SystemExit(f"❌ 连接 Jira 失败：HTTP {status or '?'}: {first_line}")
    except Exception as e:  # 不要把 token/Authorization 外泄；只给一行的类型+摘要
        first_line = str(e).strip().splitlines()[0] if str(e).strip() else ""
        raise SystemExit(f"❌ 连接 Jira 失败：{type(e).__name__}: {first_line[:200]}")
    return _CLIENT


# 语义别名
get_client = connect


def browse_url(key: str) -> str:
    """给定 issue key 返回 browse 链接：https://<host>/browse/<KEY>。"""
    base = load_credential()["server"].replace("https://", "").replace("http://", "")
    return f"https://{base}/browse/{key}"


def json_out(payload, *, title: str | None = None, indent: int = 2) -> str:
    """payload → 字符串。含中文不转义（ensure_ascii=False），缩进可读。"""
    s = json.dumps(payload, ensure_ascii=False, indent=indent)
    return (title + "\n" + s) if title else s


def out(payload, *, title: str | None = None, indent: int = 2) -> None:
    """成功路径：把 payload 作为 JSON 打印到 stdout（机器可读）。"""
    print(json_out(payload, title=title, indent=indent))


def err(message: str, exc: BaseException | None = None, *, code: int = 1) -> int:
    """失败路径：打印 ❌ 到 stderr，返回指定退出码（默认 1）。

    退出码约定（固定脚本严格遵守）：
      0=成功 · 1=脚本错误 · 2=参数错误 · 3=Jira 鉴权失败(401/403) · 4=Jira API 错误
    """
    extra = f"：{type(exc).__name__}: {exc}" if exc is not None else ""
    print(f"❌ {message}{extra}".rstrip(), file=sys.stderr)
    return code


def guard(fn):
    """装饰器：把函数包装成「返回退出码」的可运行入口。

    - 函数返回 None/int → 退出码（None→0）。
    - 捕获 jira.exceptions.JIRAError → 401/403 返回 3（鉴权失败），其余返回 4（API 错误）。
    - 捕获 SystemExit：
        · int 码 → 原样透传（argparse 的 2、connect 鉴权失败的 3 不再被降级成 1）；
        · 字符串消息（凭证缺失等）→ 打印到 stderr，返回 1。
    - 捕获其它 Exception → 打 stderr，返回 1。
    """
    from jira.exceptions import JIRAError

    def _wrapped(*a, **k):
        try:
            rc = fn(*a, **k)
            return 0 if rc is None else int(rc)
        except JIRAError as e:
            status = getattr(e, "status_code", None) or 0
            code = 3 if status in (401, 403) else 4
            detail = getattr(e, "text", None) or getattr(e, "response", None) or str(e)
            return err(f"Jira API 错误 HTTP {status or '?'}", detail, code=code)
        except SystemExit as e:
            code = e.code
            if code is None:
                return 0
            if isinstance(code, int):
                return code  # 消息已打印过；透传 2（参数）/3（鉴权）等语义退出码
            s = str(code).strip()
            if not s or s == "0":
                return 0
            print(s, file=sys.stderr)
            return 1
        except Exception as e:
            return err(f"脚本执行失败：{type(e).__name__}", e)

    return _wrapped


def describe_connection() -> str:
    """返回给用户的安全连接摘要：已连接 <server>，当前用户：<name>。不含 token。"""
    jira = connect()
    me = jira.myself()
    name = me.get("name") or me.get("displayName", "?")
    server = (
        getattr(jira, "server_url", None)
        or getattr(jira, "server", None)
        or load_credential()["server"]
    )
    return f"已连接 {server}，当前用户：{name}"


if __name__ == "__main__":  # 自检：python jira_bootstrap.py（鉴权失败→退出码 3）
    @guard
    def _selfcheck() -> int:
        print(describe_connection())
        return 0

    raise SystemExit(_selfcheck())
