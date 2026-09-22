# -*- coding: utf-8 -*-
"""列项目 / 看单项目详情（含组件、版本）→ JSON（只读）。

固定基建脚本（scripts/ops/，用法详见 skill 根目录 INDEX.md；改动后需自检并同步 INDEX.md）。

用法：
    python ops/projects.py --filter AE
    python ops/projects.py --key AERDM --with-components --with-versions

入参：
    --key             只取单项目详情（可配 --with-components / --with-versions）
    --filter          key/name 子串过滤（列表模式）
    --limit           最多列出 N 个，默认 500
    --with-components 单项目时附 components
    --with-versions   单项目时附 versions
    --compact         stdout 一行紧凑 JSON

输出 JSON：列表 → total/filtered/filter/truncated/projects[{key,id,name,lead,projectTypeKey}]
           单项目 → key/id/name/lead/projectTypeKey/description[/components/versions]

退出码：0 成功；2 参数错误；3 鉴权失败(401/403)；4 Jira API 错误；1 其它。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jira_bootstrap as jb  # noqa: E402
import jira_ops as jo  # noqa: E402


def parse_args(argv: list[str] | None = None):
    p = jo.make_parser("列项目 / 看单项目详情（只读）")
    p.add_argument("--key", help="只取单项目详情")
    p.add_argument("--filter", help="key/name 子串过滤（列表模式）")
    p.add_argument("--limit", type=int, default=500, help="最多列出 N 个，默认 500")
    p.add_argument("--with-components", action="store_true", help="单项目时附 components")
    p.add_argument("--with-versions", action="store_true", help="单项目时附 versions")
    p.add_argument("--compact", action="store_true", help="stdout 一行紧凑 JSON")
    return p.parse_args(argv)


@jb.guard
def main() -> int:
    args = parse_args()
    jira = jb.connect()
    if args.key:
        data = jo.get_project(jira, args.key, include_components=args.with_components,
                              include_versions=args.with_versions)
    else:
        data = jo.list_projects(jira, text=args.filter, limit=max(1, args.limit))
    jb.out(data, indent=None if args.compact else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
