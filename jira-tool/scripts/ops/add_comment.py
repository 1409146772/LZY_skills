# -*- coding: utf-8 -*-
"""给 issue 加评论 🔒（写操作：先 --dry-run，把 payload 给用户确认后才实跑）。

固定基建脚本（scripts/ops/，用法详见 skill 根目录 INDEX.md；改动后需自检并同步 INDEX.md）。
注意：Jira 评论没有 notify 开关，实跑总会触发通知。

用法：
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/add_comment.py AERDM-123 --body "简短评论" --dry-run
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/scripts/ops/add_comment.py AERDM-123 --body-file D:/tmp/comment.txt --dry-run

入参：
    key                 issue key（位置参数，必填）
    --body              评论正文（与 --body-file 二选一；都缺 → 退出码 2）
    --body-file         正文文件路径（或 - 读 stdin）；长评论/含引号必须用这个
    --visibility-type   可见性类型，如 role（与 --visibility-value 成对）
    --visibility-value  可见性值，如 Administrators
    --dry-run           只打印 payload，不实际写

输出 JSON：key / url / dry_run / skipped / payload{body[,visibility]} / comment{...}|null

退出码：0 成功；2 参数错误；3 鉴权失败(401/403)；4 Jira API 错误；1 其它。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jira_bootstrap as jb  # noqa: E402
import jira_ops as jo  # noqa: E402


def parse_args(argv: list[str] | None = None):
    p = jo.make_parser("给 issue 加评论（写操作，先 --dry-run）")
    p.add_argument("key", help="issue key，如 AERDM-123")
    p.add_argument("--body", help="评论正文（与 --body-file 二选一）")
    p.add_argument("--body-file", help="正文文件路径（或 - 读 stdin）")
    p.add_argument("--visibility-type", help="可见性类型，如 role")
    p.add_argument("--visibility-value", help="可见性值，如 Administrators")
    jo.add_dry_run(p)
    return p.parse_args(argv)


@jb.guard
def main() -> int:
    args = parse_args()
    if args.body and args.body_file:
        jo.arg_fail("--body 与 --body-file 只能二选一")
    if args.body_file:
        body = jo.read_body_file(args.body_file)
    elif args.body is not None and args.body != "":
        body = args.body
    else:
        jo.arg_fail("缺少评论正文：给 --body 或 --body-file")
    visibility = None
    if args.visibility_type or args.visibility_value:
        if not (args.visibility_type and args.visibility_value):
            jo.arg_fail("--visibility-type 与 --visibility-value 必须成对给出")
        visibility = {"type": args.visibility_type, "value": args.visibility_value}
    jira = jb.connect()
    data = jo.add_comment(jira, args.key, body, visibility=visibility,
                          dry_run=args.dry_run)
    jb.out(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
