# -*- coding: utf-8 -*-
"""JQL 搜索 issue → JSON（只读）。

固定基建脚本（scripts/ops/，用法详见 skill 根目录 INDEX.md；改动后需自检并同步 INDEX.md）。

用法：
    python ops/search.py --project AERDM --limit 3
    python ops/search.py --project AERDM --status 进行中 --all --limit 500
    python ops/search.py --jql "project=AERDM AND assignee=luziyu order by updated desc"

入参：
    --jql        完整 JQL；给出时忽略以下全部便利过滤参数
    --project    项目 key，默认 AERDM
    --text       自由文本（text ~ "..."）
    --status     状态名
    --assignee   经办人 name；"-" 或 "unassigned" → assignee is EMPTY
    --reporter   报告人 name
    --type       issue 类型名
    --since      created >= 该值（YYYY-MM-DD 或完整 datetime）
    --order      JQL ORDER BY 片段，默认 "created desc"
    --limit      最多返回 N 条，默认 50（--all 时也受它硬约束）
    --all        翻页拉到 limit 为止（不给则只取第一页）
    --start-at   起始偏移，默认 0
    --fields     逗号分隔字段白名单；缺省用内建精简集（不默认 *all，防 stdout 刷爆）
    --no-validate  关闭服务端 JQL 校验
    --compact    stdout 一行紧凑 JSON（省 token）

输出 JSON：jql / start_at / limit / fetch_all / total / returned / truncated /
           issues[{key,id,url,summary,status,issuetype,priority,assignee,reporter,created,updated,duedate,labels}]

退出码：0 成功；2 参数错误；3 鉴权失败(401/403)；4 Jira API 错误；1 其它。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jira_bootstrap as jb  # noqa: E402
import jira_ops as jo  # noqa: E402


def parse_args(argv: list[str] | None = None):
    p = jo.make_parser("JQL 搜索 issue（只读）", epilog=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--jql", help="完整 JQL；给出时忽略以下便利过滤参数")
    p.add_argument("--project", default="AERDM", help="项目 key，默认 AERDM")
    p.add_argument("--text", help="自由文本（text ~ \"...\"）")
    p.add_argument("--status", help="状态名")
    p.add_argument("--assignee", help="经办人 name；- 或 unassigned → assignee is EMPTY")
    p.add_argument("--reporter", help="报告人 name")
    p.add_argument("--type", help="issue 类型名")
    p.add_argument("--since", help="created >= 该值（YYYY-MM-DD 或完整 datetime）")
    p.add_argument("--order", default="created desc", help="JQL ORDER BY 片段，默认 created desc")
    p.add_argument("--limit", type=int, default=50, help="最多返回 N 条，默认 50")
    p.add_argument("--all", action="store_true", help="翻页拉到 --limit 为止（默认只取第一页）")
    p.add_argument("--start-at", type=int, default=0, help="起始偏移，默认 0")
    p.add_argument("--fields", help="逗号分隔字段白名单；缺省用内建精简集")
    p.add_argument("--no-validate", action="store_true", help="关闭服务端 JQL 校验")
    p.add_argument("--compact", action="store_true", help="stdout 一行紧凑 JSON")
    return p.parse_args(argv)


@jb.guard
def main() -> int:
    args = parse_args()
    jira = jb.connect()
    if args.jql:
        jql = args.jql
    else:
        jql = jo.build_jql(args.project, text=args.text, status=args.status,
                           assignee=args.assignee, reporter=args.reporter,
                           itype=args.type, since=args.since, order=args.order)
    fields = ([s.strip() for s in args.fields.split(",") if s.strip()]
              if args.fields else None)
    data = jo.search_issues(jira, jql, limit=max(1, args.limit), fetch_all=args.all,
                            fields=fields, start_at=max(0, args.start_at),
                            validate_query=not args.no_validate)
    jb.out(data, indent=None if args.compact else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
