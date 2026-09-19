# -*- coding: utf-8 -*-
"""更新 issue 字段 🔒（写操作：--data-only → --dry-run → 用户确认 → 实跑）。

固定基建脚本（scripts/ops/，用法详见 skill 根目录 INDEX.md；改动后需自检并同步 INDEX.md）。

用法：
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/update_issue.py AERDM-123 --summary "新标题" --dry-run
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/update_issue.py AERDM-123 --add-labels x,y --dry-run
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/update_issue.py AERDM-123 --data @D:/tmp/fields.json --dry-run

入参：
    key               issue key（位置参数，必填）
    --summary         主题
    --description     正文
    --assignee        经办人 name
    --priority        优先级 name
    --duedate         截止日期 YYYY-MM-DD
    --labels          labels 整组替换（与 --add-labels/--remove-labels 互斥）
    --add-labels      逗号分隔，增量加（update 操作）
    --remove-labels   逗号分隔，增量删（update 操作）
    --data            其余字段 JSON：内联 / @文件 / -（如 customfield_XXXX）
    --data-only       只打印合并 payload，不发任何请求
    --dry-run         打印 payload + before 快照，不写
    --no-skip-if-same 关掉「值相同就跳过」幂等防呆（默认开）
    --notify          更新后发通知（默认静默 notify=False）
    --compact         stdout 一行紧凑 JSON

输出 JSON：key / url / dry_run / skipped / reason / payload{fields[,update]} /
           before / after / changed[]

退出码：0 成功；2 参数错误；3 鉴权失败(401/403)；4 Jira API 错误；1 其它。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jira_bootstrap as jb  # noqa: E402
import jira_ops as jo  # noqa: E402


def parse_args(argv: list[str] | None = None):
    p = jo.make_parser("更新 issue 字段（写操作，先 --data-only / --dry-run）")
    p.add_argument("key", help="issue key，如 AERDM-123")
    p.add_argument("--summary", help="主题")
    p.add_argument("--description", help="正文")
    p.add_argument("--assignee", help="经办人 name")
    p.add_argument("--priority", help="优先级 name")
    p.add_argument("--duedate", help="截止日期 YYYY-MM-DD")
    p.add_argument("--labels", help="labels 整组替换（与 --add-labels/--remove-labels 互斥）")
    p.add_argument("--add-labels", help="逗号分隔，增量加")
    p.add_argument("--remove-labels", help="逗号分隔，增量删")
    p.add_argument("--data", help="其余字段 JSON：内联 / @文件 / -")
    p.add_argument("--data-only", action="store_true",
                   help="只打印合并 payload，不发任何请求")
    jo.add_dry_run(p, help_text="打印 payload + before 快照，不写")
    p.add_argument("--no-skip-if-same", action="store_true",
                   help="关掉「值相同就跳过」幂等防呆（默认开）")
    p.add_argument("--notify", action="store_true", help="更新后发通知（默认静默）")
    p.add_argument("--compact", action="store_true", help="stdout 一行紧凑 JSON")
    return p.parse_args(argv)


@jb.guard
def main() -> int:
    args = parse_args()
    if args.labels is not None and (args.add_labels or args.remove_labels):
        jo.arg_fail("--labels（整组替换）与 --add-labels/--remove-labels（增量）互斥")
    fields: dict = {}
    if args.summary is not None:
        fields["summary"] = args.summary
    if args.description is not None:
        fields["description"] = args.description
    if args.assignee is not None:
        fields["assignee"] = {"name": args.assignee}
    if args.priority is not None:
        fields["priority"] = {"name": args.priority}
    if args.duedate is not None:
        fields["duedate"] = args.duedate
    if args.labels is not None:
        fields["labels"] = [s.strip() for s in args.labels.split(",") if s.strip()]
    if args.data:
        fields.update(jo.load_fields_arg(args.data))
    update: dict = {}
    if args.add_labels or args.remove_labels:
        ops: list = [{"add": s.strip()} for s in (args.add_labels or "").split(",") if s.strip()]
        ops += [{"remove": s.strip()} for s in (args.remove_labels or "").split(",") if s.strip()]
        if ops:
            update["labels"] = ops
    if not fields and not update:
        jo.arg_fail("没有任何要更新的内容：给 --summary/--labels/... 或 --data")

    if args.data_only:          # 第一步：连请求都不发
        payload = {"fields": fields}
        if update:
            payload["update"] = update
        jb.out({"key": args.key, "url": jb.browse_url(args.key), "data_only": True,
                "payload": payload}, indent=None if args.compact else 2)
        return 0

    jira = jb.connect()
    data = jo.update_issue(jira, args.key, fields, update=update or None,
                           notify=args.notify, skip_if_same=not args.no_skip_if_same,
                           dry_run=args.dry_run)
    jb.out(data, indent=None if args.compact else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
