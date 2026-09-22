#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sync_vendor —— 从本机上游源码刷新 vendor/（开发期用）

为什么需要：vendor/ 里的三个外部工具（jira-tool / frame_extractor / canlog）都在
本机别处独立维护，会各自更新。手动 cp 容易漏文件、漏 __pycache__、或者把**活凭据**
一起搬过来。这个脚本把映射固定下来，一条命令刷新。

它做的事：
  1. 按下面的 SOURCES 逐项复制，跳过 __pycache__ / .pyc
  2. **绝不复制 jira-tool 的 config.json**（里面有活的 PAT）；只放 config.example.json
  3. 复制前对目标做一次清理，避免上游删掉的文件在 vendor/ 里残留
  4. 复位 canlog_config.json 里机器相关的字段（output_root / default_dbc / history）

用法：
  python tools/sync_vendor.py            # 刷新
  python tools/sync_vendor.py --dry-run  # 只看会做什么
  python tools/sync_vendor.py --check    # 只校验 vendor/ 完整性（不复制）

不随分发：这是开发期脚本，分发包里的 vendor/ 已经是同步好的结果。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

TOOL_DIR = Path(__file__).resolve().parent.parent
VENDOR = TOOL_DIR / "vendor"

# 上游 → vendor 的映射。路径写死在本机是刻意的：sources.json 那套间接层
# 在这个规模的工具里只会增加"配置写错却静默跳过"的风险。
SOURCES: List[Dict[str, Any]] = [
    {
        "name": "jira-tool",
        "src": Path(r"D:\LZY_project\AI\tool\jira-tool\scripts"),
        "dest": VENDOR / "jira-tool" / "scripts",
        "include": ["ops", "jira_bootstrap.py", "jira_ops.py"],
        # config.json 里有活的 PAT，绝不复制
        "never": ["config.json", "__pycache__"],
        # 上游没有 config.example.json（它只有那份带真 token 的 config.json），
        # 所以模板由本工具自带。清目标时要跳过它，否则每次同步都会把它删掉 ——
        # 而它是新机器配凭据的唯一指引。
        "keep": ["config.example.json"],
    },
    {
        "name": "frame_extractor",
        "src": Path(r"D:\LZY_project\AI\tool\AI_Log_parsing"),
        "dest": VENDOR / "frame_extractor",
        "include": ["frame_extractor.py"],
        "never": ["__pycache__", "example"],
    },
    {
        "name": "canlog",
        "src": Path(r"D:\LZY_project\AI\tool\AI_canlog"),
        "dest": VENDOR / "canlog",
        "include": ["canlog_tool", "canlog_config.json"],
        "never": ["__pycache__", "output", "example", "DBC"],
    },
]

SKIP_SUFFIXES = {".pyc", ".pyo"}


def _iter(src: Path):
    """遍历上游目录，跳过噪声文件。"""
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        if any(part == "__pycache__" or part.endswith(".egg-info") for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIXES:
            continue
        yield p


def reset_canlog_config() -> List[str]:
    """
    复位 canlog 配置里机器相关的字段。

    上游那份记着开发机的 output_root / default_dbc / history。留着它们，
    "没给 --dbc" 时会静默指向一个本机不存在的 DBC，比直接报错更难查。
    """
    p = VENDOR / "canlog" / "canlog_config.json"
    if not p.exists():
        return ["canlog_config.json 不存在，跳过复位"]
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"canlog_config.json 不是合法 JSON：{e}"]
    changed = []
    for key, value in (("default_dbc", []), ("output_root", "output"), ("history", [])):
        if cfg.get(key) != value:
            cfg[key] = value
            changed.append(key)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return [f"已复位 {k}" for k in changed] or ["无需复位"]


def check() -> int:
    """只校验 vendor/ 是否完整，不复制。"""
    ok = True
    required = [
        VENDOR / "jira-tool" / "scripts" / "jira_bootstrap.py",
        VENDOR / "jira-tool" / "scripts" / "jira_ops.py",
        VENDOR / "jira-tool" / "scripts" / "ops" / "search.py",
        VENDOR / "jira-tool" / "scripts" / "ops" / "get_issue.py",
        VENDOR / "jira-tool" / "scripts" / "ops" / "attachments.py",
        VENDOR / "frame_extractor" / "frame_extractor.py",
        VENDOR / "canlog" / "canlog_tool" / "__main__.py",
        VENDOR / "canlog" / "canlog_config.json",
    ]
    for p in required:
        mark = "✓" if p.exists() else "✗"
        if not p.exists():
            ok = False
        print(f"  {mark} {p.relative_to(TOOL_DIR)}")
    # 凭据不该在 vendor 里出现
    leak = VENDOR / "jira-tool" / "scripts" / "config.json"
    print(f"  ! {leak.relative_to(TOOL_DIR)} "
          f"{'(存在 —— 含活 PAT，请确认已 gitignore)' if leak.exists() else '(不存在，符合预期)'}")
    lark = VENDOR / "lark-cli" / "lark-cli.exe"
    print(f"  {'✓' if lark.exists() else '○'} {lark.relative_to(TOOL_DIR)}"
          f"{'' if lark.exists() else '（可选，不发邮件可不放）'}")
    print("\n结论：", "完整 ✓" if ok else "不完整 ✗ —— 重跑 python tools/sync_vendor.py")
    return 0 if ok else 1


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(description="从本机上游刷新 vendor/")
    ap.add_argument("--dry-run", action="store_true", help="只列出会做什么")
    ap.add_argument("--check", action="store_true", help="只校验完整性")
    a = ap.parse_args(argv)

    if a.check:
        return check()

    total = 0
    for item in SOURCES:
        src, dest, name = item["src"], item["dest"], item["name"]
        if not src.exists():
            print(f"✗ {name}: 上游不存在 {src}", file=sys.stderr)
            continue
        never = set(item.get("never", [])) | set(item.get("keep", []))

        # 清理目标，避免上游已删除的文件在 vendor/ 里残留
        if dest.exists() and not a.dry_run:
            for child in dest.iterdir():
                if child.name in never:
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass
        elif not dest.exists() and not a.dry_run:
            dest.mkdir(parents=True, exist_ok=True)

        n = 0
        for inc in item["include"]:
            s = src / inc
            d = dest / inc
            if not s.exists():
                print(f"  ⚠ {name}: 上游缺少 {inc}")
                continue
            if s.is_dir():
                for f in _iter(s):
                    rel = f.relative_to(src)
                    if any(part in never for part in rel.parts):
                        continue
                    if a.dry_run:
                        print(f"  {rel}")
                    else:
                        tgt = dest / rel
                        tgt.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, tgt)
                    n += 1
            else:
                if a.dry_run:
                    print(f"  {inc}")
                else:
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(s, d)
                n += 1
        total += n
        print(f"{'（dry-run）' if a.dry_run else '✓'} {name:16} {n} 个文件 ← {src}")

    if not a.dry_run:
        for line in reset_canlog_config():
            print(f"  · {line}")
        print(f"\n✓ 共同步 {total} 个文件到 {VENDOR.relative_to(TOOL_DIR)}/")
        print("  注：jira-tool 的 config.json（活 PAT）永不复制；canlog 的机器相关字段已复位。")
        print("  下一步：python tools/sync_vendor.py --check 校验完整性")
    return 0


if __name__ == "__main__":
    sys.exit(main())
