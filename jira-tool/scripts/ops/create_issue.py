# -*- coding: utf-8 -*-
"""通用创建 issue 🔒（写操作：--data-only → --dry-run → 用户确认 → 实跑）。

固定基建脚本（scripts/ops/，用法详见 skill 根目录 INDEX.md；改动后需自检并同步 INDEX.md）。

分工红线：AERDM 标准任务（固定字段默认值 + AI 推断）走 cvte-jira-create-task skill；
本脚本用于【其它项目 / 其它类型 / 含 customfield】的兜底创建。

用法：
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/create_issue.py --project AESW --type 缺陷 --summary "标题" --data-only
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/create_issue.py --project AERDM --type 任务 --summary "标题" --dry-run
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/create_issue.py --project AERDM --type 任务 --data @D:/tmp/fields.json --dry-run

入参：
    --project      项目 key（如 AERDM / AESW）
    --type         issue 类型名或 id（如 任务 / 3）
    --summary      主题
    --description  正文
    --assignee     经办人 name（Server 用 {"name":...}，不是 accountId）
    --priority     优先级 name（如 中）
    --labels       逗号分隔 labels
    --duedate      截止日期 YYYY-MM-DD
    --data         其余字段 JSON：内联 / @文件 / -（stdin）；键覆盖同名便捷参数（放 customfield_XXXX）
    --data-only    只打印合并后的 fields，不发任何请求（比 dry-run 更安全的第一步）
    --dry-run      合并 fields + 必填 preflight（project_issue_fields），不创建
    --no-check-required  跳过必填 preflight
    --compact      stdout 一行紧凑 JSON

输出 JSON：dry_run/data_only / payload{fields} / [project, issue_type, required, missing_required] /
           [key, id, url]（实跑成功时）

退出码：0 成功；2 参数错误；3 鉴权失败(401/403)；4 Jira API 错误；1 其它。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jira_bootstrap as jb  # noqa: E402
import jira_ops as jo  # noqa: E402


def parse_args(argv: list[str] | None = None):
    p = jo.make_parser("通用创建 issue（写操作，先 --data-only / --dry-run）",
                       epilog="分工红线：AERDM 标准任务走 cvte-jira-create-task；"
                              "本脚本用于其它项目/类型/customfield 兜底。")
    p.add_argument("--project", help="项目 key，如 AERDM")
    p.add_argument("--type", help="issue 类型名或 id，如 任务 / 3")
    p.add_argument("--summary", help="主题")
    p.add_argument("--description", help="正文")
    p.add_argument("--assignee", help="经办人 name（Server 用 name，非 accountId）")
    p.add_argument("--priority", help="优先级 name，如 中")
    p.add_argument("--labels", help="逗号分隔 labels")
    p.add_argument("--duedate", help="截止日期 YYYY-MM-DD")
    p.add_argument("--data", help="其余字段 JSON：内联 / @文件 / -；键覆盖同名便捷参数")
    p.add_argument("--data-only", action="store_true",
                   help="只打印合并后的 fields，不发任何请求")
    jo.add_dry_run(p, help_text="合并 fields + 必填 preflight，不创建")
    p.add_argument("--no-check-required", action="store_true", help="跳过必填 preflight")
    p.add_argument("--compact", action="store_true", help="stdout 一行紧凑 JSON")
    return p.parse_args(argv)


def build_fields(args) -> dict:
    """便捷参数 + --data 合并成 fields dict（--data 的键覆盖便捷参数）。"""
    fields: dict = {}
    if args.project:
        fields["project"] = {"key": args.project}
    if args.type:
        fields["issuetype"] = ({"id": args.type} if args.type.isdigit()
                               else {"name": args.type})
    if args.summary is not None:
        fields["summary"] = args.summary
    if args.description is not None:
        fields["description"] = args.description
    if args.assignee:
        fields["assignee"] = {"name": args.assignee}
    if args.priority:
        fields["priority"] = {"name": args.priority}
    if args.labels:
        fields["labels"] = [s.strip() for s in args.labels.split(",") if s.strip()]
    if args.duedate:
        fields["duedate"] = args.duedate
    if args.data:
        fields.update(jo.load_fields_arg(args.data))
    if not fields:
        jo.arg_fail("没有任何字段：给 --project/--type/--summary/... 或 --data")
    return fields


@jb.guard
def main() -> int:
    args = parse_args()
    fields = build_fields(args)
    out: dict = {"data_only": args.data_only, "dry_run": args.dry_run,
                 "payload": {"fields": fields}}
    if args.data_only:          # 第一步：连请求都不发
        jb.out(out, indent=None if args.compact else 2)
        return 0

    jira = jb.connect()
    project = (fields.get("project") or {}).get("key")
    issue_type = (fields.get("issuetype") or {}).get("name") or \
        (fields.get("issuetype") or {}).get("id")
    # preflight：解析类型 + 必填检查
    if project and issue_type and not args.no_check_required:
        it = jo.resolve_issue_type(jira, project, str(issue_type))
        req = jo.required_fields(jira, project, it["id"])
        missing = jo.missing_required(fields, req)
        out.update({"project": project, "issue_type": it,
                    "required": req, "missing_required": missing})
        if missing and not args.dry_run:
            jo.arg_fail("必填字段缺失（先 --dry-run 看清单）：" +
                        ", ".join(m["id"] for m in missing))
    elif project and issue_type:
        out.update({"project": project, "issue_type": {"name": issue_type},
                    "required": None, "missing_required": None})
    data = jo.create_issue(jira, fields, dry_run=args.dry_run)
    out.update({k: v for k, v in data.items() if k != "payload"})
    jb.out(out, indent=None if args.compact else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
