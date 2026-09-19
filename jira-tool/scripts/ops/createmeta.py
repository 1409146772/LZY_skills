# -*- coding: utf-8 -*-
"""查项目 + issue 类型的字段元数据（必填字段 / 枚举）→ JSON（只读）。

固定基建脚本（scripts/ops/，用法详见 skill 根目录 INDEX.md；改动后需自检并同步 INDEX.md）。
实现说明：Server 9.12.1 上 jira.createmeta() 会抛 Unsupported JIRA version，
本脚本用 project_issue_types / project_issue_fields 替代；字段 id 取 raw["fieldId"]。

用法：
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/createmeta.py --project AERDM --list-types
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/createmeta.py --project AERDM --type 任务 --required-only --with-allowed

入参：
    --project       项目 key，默认 AERDM
    --list-types    列该项目全部 issue 类型（--type 缺省时的默认行为）
    --type          类型名或 id → 输出该类型字段元数据
    --required-only 只列 required 字段
    --all-fields    optional 字段也全量列出（缺省只给 id/name/type）
    --fields-filter 字段名/fieldId 子串过滤
    --with-allowed  optional 字段也带 allowedValues（required 字段默认带）
    --limit         翻页硬上限，默认 500
    --compact       stdout 一行紧凑 JSON

输出 JSON：--list-types → project/total/issue_types[{id,name,subtask,description}]
           --type → project/issue_type/total_fields/required[{id,name,type,allowed_values}]/optional

退出码：0 成功；2 参数错误；3 鉴权失败(401/403)；4 Jira API 错误；1 其它。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jira_bootstrap as jb  # noqa: E402
import jira_ops as jo  # noqa: E402


def parse_args(argv: list[str] | None = None):
    p = jo.make_parser("查项目+类型的必填字段与枚举（只读；createmeta 的 9.12.1 替代）")
    p.add_argument("--project", default="AERDM", help="项目 key，默认 AERDM")
    p.add_argument("--list-types", action="store_true", help="列该项目全部 issue 类型")
    p.add_argument("--type", help="类型名或 id → 字段元数据")
    p.add_argument("--required-only", action="store_true", help="只列 required 字段")
    p.add_argument("--all-fields", action="store_true", help="optional 字段也全量列出")
    p.add_argument("--fields-filter", help="字段名/fieldId 子串过滤")
    p.add_argument("--with-allowed", action="store_true",
                   help="optional 字段也带 allowedValues（required 默认带）")
    p.add_argument("--limit", type=int, default=500, help="翻页硬上限，默认 500")
    p.add_argument("--compact", action="store_true", help="stdout 一行紧凑 JSON")
    return p.parse_args(argv)


@jb.guard
def main() -> int:
    args = parse_args()
    jira = jb.connect()
    if not args.type:
        data = jo.list_issue_types(jira, args.project, hard_cap=max(50, args.limit))
        jb.out(data, indent=None if args.compact else 2)
        return 0

    it = jo.resolve_issue_type(jira, args.project, args.type)
    meta = jo.list_issue_fields(jira, args.project, it["id"],
                                hard_cap=max(200, args.limit))
    fields = meta["fields"]
    if args.fields_filter:
        t = args.fields_filter.lower()
        fields = [f for f in fields
                  if t in (f.get("id") or "").lower() or t in (f.get("name") or "").lower()]
    required = [f for f in fields if f.get("required")]
    optional = [f for f in fields if not f.get("required")]

    def fmt(f: dict, *, slim: bool, keep_allowed: bool) -> dict:
        out = dict(f)
        if not keep_allowed:
            out.pop("allowed_values", None)
        if slim:
            out = {"id": f.get("id"), "name": f.get("name"), "type": f.get("type"),
                   "required": False}
        return out

    data = {
        "project": args.project,
        "issue_type": it,
        "total_fields": meta["total"],
        "required": [fmt(f, slim=False, keep_allowed=True) for f in required],
        "optional": [fmt(f, slim=not args.all_fields, keep_allowed=args.with_allowed)
                     for f in optional],
    }
    if args.required_only:
        data.pop("optional")
    jb.out(data, indent=None if args.compact else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
