# -*- coding: utf-8 -*-
"""issue 状态流转 🔒（--list 只读；执行是破坏性操作，先 --dry-run 征得用户确认）。

固定基建脚本（scripts/ops/，用法详见 skill 根目录 INDEX.md；改动后需自检并同步 INDEX.md）。

用法：
    python ops/transition.py AERDM-123 --list
    python ops/transition.py AERDM-123 --transition-id 31 --expect-status 进行中 --dry-run
    python ops/transition.py AERDM-123 --transition-id 31 --resolution 完成 --comment "已完成"

入参：
    key              issue key（位置参数，必填）
    --list           只读：列当前用户可用流转（含流转屏必填字段）；与 --transition-id 互斥
    --transition-id  流转 id（推荐）或流转名
    --comment        流转同时加评论
    --resolution     解决结果 name（如 完成）
    --assignee       流转同时改经办人 name
    --data           流转屏其它字段 JSON：内联 / @文件 / -（必填字段看 --list 输出）
    --expect-status  执行前校验当前状态，不符 → 退出码 2（幂等防呆）
    --data-only      只打印 payload（不发任何请求）
    --dry-run        打印 payload + 可用流转清单，不执行

输出 JSON：--list → key/url/current_status/transitions[{id,name,to_status,required_fields,...}]
           执行 → key/url/dry_run/skipped/transition/before_status/after_status/payload

退出码：0 成功；2 参数错误；3 鉴权失败(401/403)；4 Jira API 错误；1 其它。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jira_bootstrap as jb  # noqa: E402
import jira_ops as jo  # noqa: E402


def parse_args(argv: list[str] | None = None):
    p = jo.make_parser("issue 状态流转（--list 只读；执行破坏性，先 --dry-run）")
    p.add_argument("key", help="issue key，如 AERDM-123")
    p.add_argument("--list", action="store_true", help="只读：列可用流转与流转屏必填字段")
    p.add_argument("--transition-id", help="流转 id（推荐）或流转名")
    p.add_argument("--comment", help="流转同时加评论")
    p.add_argument("--resolution", help="解决结果 name，如 完成")
    p.add_argument("--assignee", help="流转同时改经办人 name")
    p.add_argument("--data", help="流转屏其它字段 JSON：内联 / @文件 / -")
    p.add_argument("--expect-status", help="执行前校验当前状态，不符则拒绝（幂等防呆）")
    p.add_argument("--data-only", action="store_true",
                   help="只打印 payload，不发任何请求")
    jo.add_dry_run(p, help_text="打印 payload + 可用流转清单，不执行")
    p.add_argument("--compact", action="store_true", help="stdout 一行紧凑 JSON")
    return p.parse_args(argv)


@jb.guard
def main() -> int:
    args = parse_args()
    if args.list and args.transition_id:
        jo.arg_fail("--list 与 --transition-id 互斥")
    if not args.list and not args.transition_id:
        jo.arg_fail("需要 --list（看可用流转）或 --transition-id（执行流转）")

    jira = jb.connect()
    if args.list:
        jb.out(jo.list_transitions(jira, args.key), indent=None if args.compact else 2)
        return 0

    fields = dict(jo.load_fields_arg(args.data)) if args.data else {}
    if args.data_only:          # 零请求，只组 payload
        payload: dict = {"transition": {"id": args.transition_id}}
        if fields:
            payload["fields"] = fields
        if args.comment:
            payload["comment"] = args.comment
        jb.out({"key": args.key, "url": jb.browse_url(args.key), "data_only": True,
                "payload": payload}, indent=None if args.compact else 2)
        return 0
    data = jo.do_transition(jira, args.key, args.transition_id, fields=fields,
                            comment=args.comment, resolution=args.resolution,
                            assignee=args.assignee, expect_status=args.expect_status,
                            dry_run=args.dry_run)
    jb.out(data, indent=None if args.compact else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
