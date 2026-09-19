# -*- coding: utf-8 -*-
"""通过 python-jira 在 jira.cvte.com 上创建 Jira 任务。

用法（AI / 调用方拼好 payload 后调用）：

    python create_issue.py --payload <json 文件路径> --expect-summary "<主题核心子串>"
    python create_issue.py --payload '{"project": {...}, ...}'   # 直接传 JSON 字符串
    python create_issue.py --payload <file> --dry-run             # 只打印 payload，不创建

防误建守卫：--expect-summary 指定后，真实创建前校验 payload 的 summary（归一化后）
必须包含该子串，不匹配则退出码 2、拒绝调 API——用于拦截「读错/过期 payload 文件」。
建议传不含代号前缀的核心中文（如「按CAN ID升序全量重排」），避免【】/[]格式差异。

凭证优先级：环境变量 JIRA_PAT / JIRA_SERVER > 本目录 config.json。

payload 结构 = jira.create_issue(fields=...) 的 fields 字典（完整 REST 2 格式）。
创建成功打印：CREATED:AERDM-XXXXX 与 URL；失败透传 Jira 错误信息。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Windows 下 stdout/stderr 默认用 GBK 编码会把中文输出成乱码，这里强制 UTF-8，
# 确保 AI/harness 能正确读取 payload 与错误信息。
PYTHON_IO = os.environ.get("PYTHONIOENCODING", "").lower()
if not PYTHON_IO:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent


def load_credential() -> dict[str, str]:
    """返回 {server, token}，环境变量优先于 config.json。"""
    env_server = os.environ.get("JIRA_SERVER", "").strip()
    env_token = os.environ.get("JIRA_PAT", "").strip()

    server = env_server
    token = env_token

    cfg_path = SCRIPT_DIR / "config.json"
    if (not server or not token) and cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        server = server or cfg.get("server", "")
        token = token or cfg.get("token", "")

    if not server:
        raise SystemExit("❌ 缺少 Jira 服务器地址：请设置环境变量 JIRA_SERVER 或 config.json 的 server。")
    if not token:
        raise SystemExit("❌ 缺少 Jira PAT：请设置环境变量 JIRA_PAT 或 config.json 的 token。")
    return {"server": server, "token": token}


def load_payload(raw: str) -> dict:
    """raw 若是现有文件则读文件，否则按 JSON 字符串解析。"""
    p = Path(raw)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        data = json.loads(raw)
    # 兼容 {"fields": {...}} 形式，直接使用 fields 字典
    if isinstance(data, dict) and "fields" in data and isinstance(data["fields"], dict):
        data = data["fields"]
    if not isinstance(data, dict):
        raise SystemExit("❌ payload 必须是 JSON 对象。")
    return data


# 项目/车型代号开头 → 统一为 [代号]描述（幂等：已带 [代号] 前缀则不再改动）
_LEADING_CODE_RE = re.compile(r"^([A-Z0-9]{2,})_")     # 如 WB101_ / WY113_ / 250MY24_
_LEADING_FULLWIDTH_RE = re.compile(r"^【([^】]+)】")   # 兼容旧的【代号】前缀


def normalize_summary(summary: str) -> str:
    """把 summary 归一化为统一 [代号]描述 格式。

    仅处理【开头】的代号：`WB101_标题` → `[WB101]标题`。
    代号在中间/重复出现、以及内部下划线一律保留原样。
    幂等：结果以 `[` 开头且无紧跟下划线，二次处理保持不变。
    """
    s = summary or ""
    m = _LEADING_CODE_RE.match(s)
    if m:
        return f"[{m.group(1)}]{s[m.end():]}"
    m2 = _LEADING_FULLWIDTH_RE.match(s)
    if m2:
        return f"[{m2.group(1)}]{s[m2.end():]}"
    return s


def main() -> int:
    parser = argparse.ArgumentParser(description="在 Jira 上创建任务")
    parser.add_argument("--payload", required=True, help="JSON 字段字典（文件路径或 JSON 字符串）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 payload，不创建")
    parser.add_argument("--expect-summary", default=None,
                        help="创建前守卫：summary（归一化后）必须包含此子串，否则拒绝创建（退出码 2）。"
                             "建议传不含代号前缀的核心中文，避免【】/[]格式差异")
    args = parser.parse_args()

    fields = load_payload(args.payload)
    cred = load_credential()

    # 归一化 summary（幂等），确保主题统一为 [代号]描述
    if isinstance(fields.get("summary"), str):
        fields["summary"] = normalize_summary(fields["summary"])

    print("=== Jira Payload ===")
    print(json.dumps(fields, ensure_ascii=False, indent=2))

    if args.dry_run:
        print("\n[dry-run] 未做任何变更。")
        return 0

    if args.expect_summary:
        actual = fields.get("summary") or ""
        if args.expect_summary not in actual:
            print(f"\n❌ summary 守卫失败：期望包含「{args.expect_summary}」，实际「{actual}」"
                  "\n   —— payload 文件可能过期/错位，已拒绝创建（退出码 2）", file=sys.stderr)
            return 2
        print("✅ summary 守卫通过")

    from jira import JIRA
    from jira.exceptions import JIRAError

    jira = JIRA(server=cred["server"], token_auth=cred["token"])
    try:
        issue = jira.create_issue(fields=fields)
    except JIRAError as e:
        print(f"\n❌ Jira 创建失败：HTTP {e.status_code} {e.text}", file=sys.stderr)
        return 1
    except Exception as e:  # 网络/其他异常
        print(f"\n❌ 创建失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"\nCREATED:{issue.key}")
    print(f"URL:https://{cred['server'].replace('https://', '').replace('http://', '')}/browse/{issue.key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
