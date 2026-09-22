#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
jla_summary —— 回执解析与当日汇总

诚实说明本模块的能力边界：
  Python 进程**无法直接调用 Claude Code 的 Agent 工具**（那是 REPL 内的能力）。
  所以真实运行时，派发动作由 Claude 会话按 prompts/orchestrator.md 执行：
      Claude 调 `jla.py prepare` → 拿到每单的 prompt + bundle
      → Claude 用 Agent 工具逐个派发 subagent
      → subagent 写 analysis.md + receipt.json
      → Claude 调 `jla.py summary` 汇总 → `jla.py mail-only` 发信

本模块提供：
  * parse_receipt_text()：把 subagent 的文本回执解析成字段
  * write_receipt()：把回执写进 receipt.json（含失败态）
  * write_summary()：从各单 receipt.json 生成 SUMMARY.md
  * prepare_dispatch_materials()：落盘派发材料，供 Claude 读取

原有的"水位 state.json / 增量跳过"已删除：那份实现中 should_skip/mark_ok 从未被调用过
（grep 核实），保留只会让人以为增量生效了。失败的单本来就会在下次重跑时被覆盖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import jla_env as env


# ---------------------------------------------------------------- 回执


RECEIPT_TEMPLATE: Dict[str, Any] = {
    "key": "",
    "title": "",
    "project": "",
    "confidence": "",
    "size": "",
    "conclusion": "",
    "pending": 0,
    "attention": "无",
    "status": "ok",
    "error": None,
}


def write_receipt(ticket_dir: Path, **fields: Any) -> Path:
    rec = dict(RECEIPT_TEMPLATE)
    rec.update(fields)
    p = ticket_dir / "receipt.json"
    env.write_json(p, rec)
    return p


def parse_receipt_text(text: str) -> Dict[str, Any]:
    """
    从 subagent 的回执文本里解析字段。
    回执是 KEY: ... / 工程归属: ... / 工作量: ... 这种键值行。
    """
    out: Dict[str, Any] = {}
    mapping = {
        "工程归属": "project",
        "复杂度": "size",  # 兼容旧写法
        "工作量": "size",
        "一句话结论": "conclusion",
        "待确认数": "pending",
        "报告": "report",
        "需人工注意": "attention",
        "KEY": "key",
    }
    for line in (text or "").splitlines():
        if ":" not in line and "：" not in line:
            continue
        # 中文冒号也认
        line = line.replace("：", ":", 1)
        k, _, v = line.partition(":")
        k = k.strip().lstrip("-* ").strip()
        v = v.strip()
        if k in mapping:
            key = mapping[k]
            if key == "project":
                # "S32K312 车载仪表主控平台 / 置信度 high"
                if "/" in v and "置信度" in v:
                    proj, _, conf = v.partition("置信度")
                    out["project"] = proj.strip().rstrip("/").strip()
                    out["confidence"] = conf.strip().strip("/").strip()
                else:
                    out["project"] = v
            elif key == "pending":
                try:
                    out["pending"] = int("".join(ch for ch in v if ch.isdigit()) or 0)
                except ValueError:
                    out["pending"] = 0
            else:
                out[key] = v
    return out


# ---------------------------------------------------------------- 汇总


def collect_receipts(date_dir: Path):
    """
    扫描 <date_dir>/*/receipt.json，分成 (rows, failed, pending_missing)。

    失败/未产出回执的单必须能体现出来 —— 静默省略会让人以为"全跑过了"。
    """
    rows: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    pending_missing: List[str] = []

    if not date_dir.exists():
        return rows, failed, pending_missing

    for d in sorted(p for p in date_dir.iterdir() if p.is_dir()):
        if not (d / "ticket.json").exists() and not (d / "receipt.json").exists():
            continue
        rp = d / "receipt.json"
        if not rp.exists():
            # 尚未分析（或 subagent 没写回执）
            pending_missing.append(d.name)
            continue
        try:
            r = env.read_json(rp, {})
        except Exception as e:  # noqa: BLE001
            failed.append({"key": d.name, "error": f"回执解析失败: {e}"})
            continue
        if r.get("status") == "failed":
            failed.append({"key": r.get("key", d.name), "error": r.get("error", "未知")})
            continue
        rows.append(r)
    return rows, failed, pending_missing


def write_summary(date_dir: Path, skipped_by_limit: int = 0) -> Path:
    """
    从各单 receipt.json 生成当日 SUMMARY.md。
    由脚本生成而不是靠编排者手写：保证失败/跳过/超限的单**不会被静默省略**。
    """
    rows, failed, pending_missing = collect_receipts(date_dir)

    L: List[str] = [f"# 工单分析汇总 {date_dir.name}", ""]
    total = len(rows) + len(failed) + len(pending_missing)
    L.append(f"共 {total} 单：成功 {len(rows)} / 失败 {len(failed)}"
             + (f" / 待分析 {len(pending_missing)}" if pending_missing else "")
             + (f"（另有 {skipped_by_limit} 单因数量上限未分析）" if skipped_by_limit else ""))
    L.append("")

    if rows:
        L.append("| KEY | 标题 | 工程归属(置信度) | 工作量 | 一句话结论 | 待确认 | 报告 |")
        L.append("|---|---|---|---|---|---|---|")
        for r in rows:
            L.append(
                f"| {r.get('key','')} | {r.get('title','')} "
                f"| {r.get('project','')} ({r.get('confidence','')}) "
                f"| {r.get('size','')} | {r.get('conclusion','')} "
                f"| {r.get('pending',0)} | [analysis.md]({r.get('key','')}/analysis.md) |"
            )
        L.append("")

    notes = [f"{r.get('key')}: {r['attention']}" for r in rows
             if r.get("attention") and r["attention"] != "无"]
    L.append("## 需要你留意")
    L.append("")
    if notes:
        for n in notes:
            L.append(f"- {n}")
    else:
        L.append("- （无）")
    L.append("")
    if failed:
        L.append("### 分析失败（下次运行会自动重试）")
        L.append("")
        for f in failed:
            L.append(f"- **{f['key']}** — {f['error']}")
        L.append("")
    if pending_missing:
        L.append(f"### 待分析（未产出回执）：{', '.join(pending_missing)}")
        L.append("")
    if skipped_by_limit:
        L.append(f"### 因数量上限未分析：另有 {skipped_by_limit} 单")
        L.append("")

    env.ensure_dir(date_dir)
    out = date_dir / "SUMMARY.md"
    env.write_text(out, "\n".join(L))
    return out


# ---------------------------------------------------------------- 派发材料


def prepare_dispatch_materials(date_dir: Path) -> Dict[str, Any]:
    """
    收集每单的派发材料，写 <date_dir>/_dispatch.json，
    供 Claude 读取后用 Agent 工具派发（一次一票，顺序执行）。
    """
    items: List[Dict[str, Any]] = []
    if date_dir.exists():
        for d in sorted(p for p in date_dir.iterdir() if p.is_dir()):
            bundle = d / "bundle.md"
            prompt = d / "worker_prompt.md"
            if not bundle.exists():
                continue
            items.append({
                "key": d.name,
                "dir": str(d),
                "bundle": str(bundle),
                "prompt": str(prompt) if prompt.exists() else None,
                "analysis": str(d / "analysis.md"),
                "receipt": str(d / "receipt.json"),
                "has_prompt": prompt.exists(),
            })

    out = {"date_dir": str(date_dir), "items": items}
    env.write_json(date_dir / "_dispatch.json", out)
    return out
