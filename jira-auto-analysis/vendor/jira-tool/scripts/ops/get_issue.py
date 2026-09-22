# -*- coding: utf-8 -*-
"""读单条 issue 详情 → JSON（只读）。

固定基建脚本（scripts/ops/，用法详见 skill 根目录 INDEX.md；改动后需自检并同步 INDEX.md）。
查 customfield 实际值时必开 --raw（输出 raw_fields）。

用法：
    python ops/get_issue.py AERDM-123
    python ops/get_issue.py AERDM-123 --raw
    python ops/get_issue.py AERDM-123 --no-comments --fields summary,status

入参：
    key              issue key（位置参数，必填）
    --fields         逗号分隔字段白名单；缺省用内建精简集
    --no-comments    不取评论（省流量）
    --comment-limit  最多内嵌 N 条评论，默认 20
    --raw            附 raw_fields（issue 原始 fields，含全部 customfield 实际值）
    --compact        stdout 一行紧凑 JSON

输出 JSON：key/id/url/project/issuetype/status/summary/description/priority/assignee/
           reporter/creator/created/updated/duedate/resolution/labels/components/
           comment_total/comments/comments_truncated[/raw_fields]

退出码：0 成功；2 参数错误；3 鉴权失败(401/403)；4 Jira API 错误；1 其它。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jira_bootstrap as jb  # noqa: E402
import jira_ops as jo  # noqa: E402


def parse_args(argv: list[str] | None = None):
    p = jo.make_parser("读单条 issue 详情（只读）")
    p.add_argument("key", help="issue key，如 AERDM-123")
    p.add_argument("--fields", help="逗号分隔字段白名单；缺省用内建精简集")
    p.add_argument("--no-comments", action="store_true", help="不取评论")
    p.add_argument("--comment-limit", type=int, default=20, help="最多内嵌 N 条评论，默认 20")
    p.add_argument("--raw", action="store_true", help="附 raw_fields（含全部 customfield 实际值）")
    p.add_argument("--compact", action="store_true", help="stdout 一行紧凑 JSON")
    return p.parse_args(argv)


@jb.guard
def main() -> int:
    args = parse_args()
    jira = jb.connect()
    fields = ([s.strip() for s in args.fields.split(",") if s.strip()]
              if args.fields else None)
    data = jo.get_issue(jira, args.key, fields=fields,
                        include_comments=not args.no_comments,
                        comment_limit=max(0, args.comment_limit),
                        include_raw=args.raw)
    jb.out(data, indent=None if args.compact else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
