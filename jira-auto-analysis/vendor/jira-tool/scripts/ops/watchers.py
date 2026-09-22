# -*- coding: utf-8 -*-
"""issue 关注者 list / add / remove（list 只读；add/remove 🔒 写操作先 --dry-run）。

固定基建脚本（scripts/ops/，用法详见 skill 根目录 INDEX.md；改动后需自检并同步 INDEX.md）。
安全：add 前强制精确 name 匹配校验（python-jira 内部是模糊搜索）。

用法：
    python ops/watchers.py list AERDM-123
    python ops/watchers.py add AERDM-123 --user luziyu --dry-run
    python ops/watchers.py remove AERDM-123 --user luziyu

入参：
    action    list | add | remove（位置参数，必填）
    key       issue key（位置参数，必填）
    --user    add/remove 的用户 name，可多个
    --dry-run 只打印将执行的操作，不写
    --compact stdout 一行紧凑 JSON

输出 JSON：list → key/url/watch_count/watchers[{name,displayName}]
           add/remove → added[]/removed[]/failed[{name,op,why}]

退出码：0 成功；2 参数错误；3 鉴权失败(401/403)；4 Jira API 错误；1 其它。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jira_bootstrap as jb  # noqa: E402
import jira_ops as jo  # noqa: E402


def parse_args(argv: list[str] | None = None):
    p = jo.make_parser("issue 关注者 list / add / remove")
    p.add_argument("action", choices=["list", "add", "remove"], help="子命令")
    p.add_argument("key", help="issue key，如 AERDM-123")
    p.add_argument("--user", nargs="+", help="add/remove 的用户 name，可多个")
    jo.add_dry_run(p, help_text="只打印将执行的操作，不写")
    p.add_argument("--compact", action="store_true", help="stdout 一行紧凑 JSON")
    return p.parse_args(argv)


@jb.guard
def main() -> int:
    args = parse_args()
    jira = jb.connect()
    if args.action == "list":
        jb.out(jo.list_watchers(jira, args.key), indent=None if args.compact else 2)
        return 0
    if not args.user:
        jo.arg_fail(f"{args.action} 需要 --user <name>（可多个）")
    add = args.user if args.action == "add" else None
    remove = args.user if args.action == "remove" else None
    data = jo.modify_watchers(jira, args.key, add=add, remove=remove,
                              dry_run=args.dry_run)
    jb.out(data, indent=None if args.compact else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
