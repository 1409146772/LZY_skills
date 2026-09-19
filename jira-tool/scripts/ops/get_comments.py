# -*- coding: utf-8 -*-
"""读 issue 评论 → JSON（只读）。

固定基建脚本（scripts/ops/，用法详见 skill 根目录 INDEX.md；改动后需自检并同步 INDEX.md）。

用法：
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/get_comments.py AERDM-123
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/get_comments.py AERDM-123 --limit 50
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/get_comments.py AERDM-123 --id 10501

入参：
    key            issue key（位置参数，必填）
    --id           只取某条评论 id（精确单条）
    --limit        最多返回 N 条，默认 200（超出 truncated=true）
    --offset       起始偏移，默认 0
    --order        评论排序：created / -created，默认 created
    --body-max-len 单条 body 截断长度，默认 4000
    --no-all       不自动续拉翻页（只取第一页）
    --compact      stdout 一行紧凑 JSON

输出 JSON：key / url / total / returned / truncated / offset /
           comments[{id,author,created,updated,body,visibility}]

退出码：0 成功；2 参数错误；3 鉴权失败(401/403)；4 Jira API 错误；1 其它。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jira_bootstrap as jb  # noqa: E402
import jira_ops as jo  # noqa: E402


def parse_args(argv: list[str] | None = None):
    p = jo.make_parser("读 issue 评论（只读）")
    p.add_argument("key", help="issue key，如 AERDM-123")
    p.add_argument("--id", help="只取某条评论 id（精确单条）")
    p.add_argument("--limit", type=int, default=200, help="最多返回 N 条，默认 200")
    p.add_argument("--offset", type=int, default=0, help="起始偏移，默认 0")
    p.add_argument("--order", choices=["created", "-created"], default="created",
                   help="评论排序，默认 created；倒序必须用等号写法 --order=-created"
                        "（空格写法会被当成参数名）")
    p.add_argument("--body-max-len", type=int, default=jo.BODY_TRUNC_DEFAULT,
                   help="单条 body 截断长度，默认 4000")
    p.add_argument("--no-all", action="store_true", help="不自动续拉翻页（只取第一页）")
    p.add_argument("--compact", action="store_true", help="stdout 一行紧凑 JSON")
    return p.parse_args(argv)


@jb.guard
def main() -> int:
    args = parse_args()
    jira = jb.connect()
    if args.id:
        data = jo.get_comment(jira, args.key, args.id)
    else:
        data = jo.get_comments(jira, args.key, limit=max(1, args.limit),
                               offset=max(0, args.offset), order=args.order,
                               body_trunc=args.body_max_len, fetch_all=not args.no_all)
    jb.out(data, indent=None if args.compact else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
