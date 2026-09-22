# -*- coding: utf-8 -*-
"""issue 附件 list / add / download（list/download 只读；add 🔒 写操作先 --dry-run）。

固定基建脚本（scripts/ops/，用法详见 skill 根目录 INDEX.md；改动后需自检并同步 INDEX.md）。

用法：
    python ops/attachments.py list AERDM-123
    python ops/attachments.py add AERDM-123 --file D:/tmp/a.log --dry-run
    python ops/attachments.py download --id 10001 --out D:/tmp
    python ops/attachments.py download --key AERDM-123 --name build.log --out D:/tmp

入参：
    action      list | add | download（位置参数，必填）
    key         issue key（list/add 必填；download --id 时可省）
    --file      add：本地文件路径，可多个
    --filename  add：重命名（仅单文件时有效）
    --id        download：附件 id
    --name      download：按 key + 文件名精确定位（需恰好匹配 1 个）
    --out       download：目标目录或完整文件路径；缺省 = 当前目录
    --force     download：覆盖已存在文件
    --dry-run   add：只打印将上传的文件清单与大小
    --compact   stdout 一行紧凑 JSON

输出 JSON：list → key/url/total/attachments[{id,filename,size,mime_type,created,content_url}]
           add → dry_run/would_add|added/missing · download → attachment_id/filename/size/path

退出码：0 成功；2 参数错误；3 鉴权失败(401/403)；4 Jira API 错误；1 其它。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jira_bootstrap as jb  # noqa: E402
import jira_ops as jo  # noqa: E402


def parse_args(argv: list[str] | None = None):
    p = jo.make_parser("issue 附件 list / add / download")
    p.add_argument("action", choices=["list", "add", "download"], help="子命令")
    p.add_argument("key", nargs="?", help="issue key（list/add 必填；download --id 时可省）")
    p.add_argument("--file", nargs="+", help="add：本地文件路径，可多个")
    p.add_argument("--filename", help="add：重命名（仅单文件时有效）")
    p.add_argument("--id", help="download：附件 id")
    p.add_argument("--name", help="download：按 key + 文件名精确定位")
    p.add_argument("--out", help="download：目标目录或完整文件路径；缺省 = 当前目录")
    p.add_argument("--force", action="store_true", help="download：覆盖已存在文件")
    jo.add_dry_run(p, help_text="add：只打印将上传的文件清单与大小")
    p.add_argument("--compact", action="store_true", help="stdout 一行紧凑 JSON")
    return p.parse_args(argv)


@jb.guard
def main() -> int:
    args = parse_args()
    jira = jb.connect()
    if args.action == "list":
        if not args.key:
            jo.arg_fail("list 需要 issue key")
        jb.out(jo.list_attachments(jira, args.key), indent=None if args.compact else 2)
        return 0
    if args.action == "add":
        if not args.key:
            jo.arg_fail("add 需要 issue key")
        if not args.file:
            jo.arg_fail("add 需要 --file <本地路径>（可多个）")
        filenames = [args.filename] if args.filename else None
        jb.out(jo.add_attachments(jira, args.key, args.file, filenames=filenames,
                                  dry_run=args.dry_run),
               indent=None if args.compact else 2)
        return 0
    # download
    data = jo.download_attachment(jira, attachment_id=args.id, key=args.key,
                                  name=args.name, dest=args.out, force=args.force)
    jb.out(data, indent=None if args.compact else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
