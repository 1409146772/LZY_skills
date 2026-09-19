# -*- coding: utf-8 -*-
"""issue 指派 / 取消指派 🔒（--list-assignable 只读；指派是写操作，先 --dry-run）。

固定基建脚本（scripts/ops/，用法详见 skill 根目录 INDEX.md；改动后需自检并同步 INDEX.md）。
安全：python-jira 内部是【模糊】用户搜索且多命中取第一个 —— 实跑前强制精确 name 匹配校验。

用法：
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/assign.py AERDM-123 --list-assignable --query luzi
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/assign.py AERDM-123 --user luziyu --dry-run
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/assign.py AERDM-123 --unassign --dry-run

入参：
    key               issue key（位置参数，必填）
    --user            目标用户 name（Server 用 name，非 accountId）
    --unassign        取消指派（与 --user 互斥）
    --list-assignable 只读：列出该 issue 可指派用户（可配 --query）
    --query           --list-assignable 的过滤串
    --dry-run         校验精确匹配 + 打印 before，不写

输出 JSON：--list-assignable → key/url/candidates[{name,displayName,email,active}]
           指派 → key/url/dry_run/skipped/assignee_before/assignee_after/resolved_name

退出码：0 成功；2 参数错误（含用户不精确匹配）；3 鉴权失败；4 Jira API 错误；1 其它。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jira_bootstrap as jb  # noqa: E402
import jira_ops as jo  # noqa: E402


def parse_args(argv: list[str] | None = None):
    p = jo.make_parser("issue 指派 / 取消指派（写操作，先 --dry-run）")
    p.add_argument("key", help="issue key，如 AERDM-123")
    p.add_argument("--user", help="目标用户 name（与 --unassign 互斥）")
    p.add_argument("--unassign", action="store_true", help="取消指派")
    p.add_argument("--list-assignable", action="store_true",
                   help="只读：列出该 issue 可指派用户")
    p.add_argument("--query", help="--list-assignable 的过滤串")
    jo.add_dry_run(p, help_text="校验精确匹配 + 打印 before，不写")
    p.add_argument("--compact", action="store_true", help="stdout 一行紧凑 JSON")
    return p.parse_args(argv)


@jb.guard
def main() -> int:
    args = parse_args()
    jira = jb.connect()
    if args.list_assignable:
        cands = jo.resolve_assignable(jira, args.query or "", key=args.key, max_results=50)
        jb.out({"key": args.key, "url": jb.browse_url(args.key),
                "query": args.query, "total": len(cands), "candidates": cands},
               indent=None if args.compact else 2)
        return 0

    if args.user and args.unassign:
        jo.arg_fail("--user 与 --unassign 互斥")
    if not args.user and not args.unassign:
        jo.arg_fail("需要 --user <name> 或 --unassign")
    user = None if args.unassign else args.user
    jo.exact_user_check(jira, user, key=args.key, assignable_only=True)  # 匹配失败 → 退出码 2
    data = jo.assign_issue(jira, args.key, user, dry_run=args.dry_run)
    jb.out(data, indent=None if args.compact else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
