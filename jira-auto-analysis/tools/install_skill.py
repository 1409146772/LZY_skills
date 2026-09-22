#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
install_skill —— 把本工具镜像到 ~/.claude/skills/jira-log-analysis/（开发期用）

为什么需要这个：库里此前**没有**同步脚本，于是 `~/.claude/skills/` 下的副本悄悄漂移了
（同一个脚本后来在本仓库的姊妹工具 jira-ticket-analysis 上真的踩到过：
副本比仓库旧了几十 KB，用户调用时跑的是旧副本，改动等于没生效）。用户调用 skill 时跑的是副本 —— 副本旧，改动就等于没生效。

与 tools/sync_vendor.py 是同一类东西：**开发期**在本机跑，不随分发。

用法：
  python tools/install_skill.py            # 镜像到默认位置
  python tools/install_skill.py --target <路径>
  python tools/install_skill.py --dry-run  # 只看会做什么
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
SKILL_NAME = "jira-log-analysis"

# 不进镜像的东西。.venv 每台机器自己建；workspace/ 与 inputs.json 是本机运行时产物，
# 含绝对路径与工单数据，不该进 skill 目录；config.json 里有本机 email 与
# vendor 凭据文件，也只应留在本地。
SKIP_DIRS = {".git", ".venv", "workspace", "dist", "__pycache__", ".pytest_cache",
             ".mypy_cache", ".idea", ".vscode"}
SKIP_FILES = {"inputs.json"}
SKIP_SUFFIXES = {".pyc", ".pyo"}

# 精简版已删除的模块，绝不该再出现在镜像里（陈旧副本会静默漂移）。
# 用**拒绝运行**而不是静默清理，免得哪天有人以为它们还有用又把文件放回来。
FORBIDDEN_DIRS = {"dbc", "docs_seed"}
FORBIDDEN_FILES = {"scripts/jta_env.py", "scripts/jta_workspace.py", "scripts/jta_repo.py",
                   "scripts/jta_refs.py", "scripts/jta_dbc.py", "scripts/jta_dispatch.py",
                   "scripts/jtat.py"}


def default_target() -> Path:
    return Path.home() / ".claude" / "skills" / SKILL_NAME


def check_forbidden(target: Path) -> list[str]:
    """镜像目标里若还留着已删除的目录，报出来让调用方中止。"""
    return [d for d in sorted(FORBIDDEN_DIRS) if (target / d).exists()]


def check_forbidden_files(target: Path) -> list[str]:
    """镜像目标里若还留着已删除的旧模块文件，报出来让调用方中止。"""
    return [f for f in sorted(FORBIDDEN_FILES) if (target / f).exists()]


def looks_like_skill(target: Path) -> bool:
    """
    目标是否像本 skill 的安装点。

    为什么必须有这个检查：本脚本会**清空目标目录再重拷**，而 `--target` 是用户可传的。
    没有这道闸门的话，`--target D:\\some\\important\\dir` 会直接把那个目录清掉。
    判据：空目录放行（首次安装），非空目录必须带着本 skill 的标志文件。
    """
    if not target.exists():
        return True
    try:
        if not any(target.iterdir()):
            return True
    except OSError:
        return False
    return (target / "SKILL.md").exists() and (target / "scripts" / "jla.py").exists()


def iter_files(root: Path):
    """遍历要镜像的文件，跳过 SKIP_DIRS 与 SKIP_SUFFIXES。"""
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIXES:
            continue
        if rel.as_posix() in SKIP_FILES or p.name in SKIP_FILES:
            continue
        yield rel


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(description="把本工具镜像到 ~/.claude/skills/")
    ap.add_argument("--target", help=f"目标目录（默认 {default_target()}）")
    ap.add_argument("--dry-run", action="store_true", help="只列出会做什么")
    ap.add_argument("--force", action="store_true",
                    help="即使目标里有已废弃目录也继续（不推荐）")
    a = ap.parse_args(argv)

    target = Path(a.target).expanduser().resolve() if a.target else default_target()

    # 闸门 1：目标必须是本 skill 的安装点 —— 我们马上要清空它，认错地方就是灾难
    if not looks_like_skill(target):
        print(f"✗ 目标不像 {SKILL_NAME} 的安装点（无 SKILL.md / scripts/jla.py）：{target}",
              file=sys.stderr)
        print(f"  本脚本会**清空目标目录再重拷**，所以拒绝往一个不像 skill 的目录里写。",
              file=sys.stderr)
        print(f"  确认无误请手工清空该目录后重跑。", file=sys.stderr)
        return 1

    # 闸门 2：目标里若还留着已废弃目录，让"删掉 dbc/ docs_seed/"这件事被强制，而不是靠人记得
    stale = check_forbidden(target)
    if stale and not a.force:
        print(f"✗ 目标里还有已废弃的目录：{stale}", file=sys.stderr)
        print(f"  它们在本版本已被删除（DBC/CLAUDE.md 改从 inputs.json 指定的路径取）。", file=sys.stderr)
        print(f"  请先删除：{target / stale[0]}", file=sys.stderr)
        print(f"  （确认要保留就加 --force）", file=sys.stderr)
        return 1

    # 闸门 3：目标里若还留着精简版已删除的旧模块，说明副本陈旧。
    # 不清掉它们的话，副本会带着 jta_*.py 静默漂移，用户跑的还是旧工具。
    stale_files = check_forbidden_files(target)
    if stale_files and not a.force:
        print(f"✗ 目标里还有本版本已删除的旧模块：", file=sys.stderr)
        for f in stale_files:
            print(f"    {f}", file=sys.stderr)
        print(f"  它们属于 jira-ticket-analysis，不该出现在 {SKILL_NAME} 里。", file=sys.stderr)
        print(f"  请先删掉这些文件（或用 --force 让镜像流程覆盖清理）。", file=sys.stderr)
        return 1

    files = list(iter_files(TOOL_DIR))
    print(f"源     : {TOOL_DIR}")
    print(f"目标   : {target}")
    print(f"待镜像 : {len(files)} 个文件")

    if a.dry_run:
        for rel in files:
            print(f"  {rel}")
        print("\n（--dry-run，未做任何改动）")
        return 0

    # 清掉目标的旧内容（保留 .venv —— 每台机器自建，重拷很贵）
    if target.exists():
        for child in target.iterdir():
            if child.name in SKIP_DIRS:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
    else:
        target.mkdir(parents=True, exist_ok=True)

    # 逐文件复制。**不要用 shutil.copytree** —— DLP 会给部分 .py 加 %TSD-Header 密文头，
    # 逐文件 copy2 走的是和 Python 一样的透明解密路径，行为已在 sync_vendor.py 验证过。
    n = 0
    for rel in files:
        src = TOOL_DIR / rel
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        n += 1

    print(f"\n✓ 已镜像 {n} 个文件 → {target}")
    print(f"  下一次调用 skill 时生效（SKILL_DIR = TOOL_DIR，一切相对镜像解析）。")
    print(f"  注：镜像不含 .venv / workspace/ / inputs.json —— 在镜像目录里首次使用要先建 venv。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
