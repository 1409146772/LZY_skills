#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
jla —— 工单分析流水线统一 CLI（精简版）

只做四件事：**拉 Jira 工单 → 解析 Log 附件 → 结合代码分析 → 发邮件**。

子命令：
  validate                   环境自检（venv / 依赖 / config / Jira 探活 / inputs.json / vendor / 可写）
  list                       只列符合条件的工单，不下载
  fetch      [--keys K1,K2]  拉工单 + 下载附件 → staging
  prepare    [--keys ...]    附件解析 + 组装 bundle/worker_prompt
  run        [--keys ...]    fetch + prepare + summary（一键到"可派发"状态）
  all        [--keys ...]    run + 提示派发（可 --send 发信）
  summary    [--date ...]    从 receipt.json 生成 SUMMARY.md
  mail-only  [--date ...]    只发邮件（含 --dry-run）

**输入路径来自 inputs.json**（调用前由 Claude 会话写好），三个路径全部只读：
  {
    "code": "D:\\\\projects\\\\weiqiao\\\\WB101\\\\workspace\\\\FreeRTOS_S32K312",
    "dbc":  "D:\\\\projects\\\\weiqiao\\\\WB101\\\\dbc\\\\WB101_..._V2.4.dbc",
    "docs": "D:\\\\projects\\\\weiqiao\\\\WB101\\\\doc",
    "keys": ["LH2512024-5408"]
  }
  * code —— 工程代码根（**必需**，含 CLAUDE.md 的 checkout）
  * dbc  —— CAN 矩阵（可选；缺省则 BLF 只能按裸 ID 解析）
  * docs —— 整理好的 md 文档目录（可选）
  * keys —— 只跑指定工单（可选；缺省按 config.jira.jql 拉）
由 jla_env.assert_writable 在写入原语层强制"只读" —— 不是文档约定。

注意：subagent 的"派发"动作由 Claude 会话执行（见 prompts/orchestrator.md），
      本 CLI 负责的是派发前后的确定性工作。

退出码（沿用 jira-tool 约定）：
  0 成功 / 1 脚本错误 / 2 参数错误 / 3 鉴权失败 / 4 上游 API 错误 / 5 有单失败

入口：优先用工具根目录下的 jla.cmd（自动解析工具目录与 .venv），
      或直接 <TOOL>/.venv/Scripts/python.exe <TOOL>/scripts/jla.py …（用绝对路径即可，
      不依赖 cwd）。cwd 在工具目录内最省事（无 jira/ 同名目录遮蔽 import jira）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date
from pathlib import Path
from typing import Any, Dict, List, Optional

import jla_env as env

# 本进程的 stdout 也必须能输出 UTF-8。
# 背景：jla_env.child_env() 只给**子进程**注入 PYTHONUTF8=1；本进程的输出编码取决于
# 控制台代码页（中文 Windows 上是 cp936），而 validate 会打印 ✓/✗ 等非 GBK 字符，
# 会直接 UnicodeEncodeError 崩掉。这里统一把本进程的 stdio 也设成 UTF-8。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# 允许从任意 cwd 运行：把脚本目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import jla_attach  # noqa: E402
import jla_bundle  # noqa: E402
import jla_fetch  # noqa: E402
import jla_mail  # noqa: E402
import jla_summary  # noqa: E402

EXIT_OK, EXIT_ERR, EXIT_ARG, EXIT_AUTH, EXIT_API, EXIT_PARTIAL = 0, 1, 2, 3, 4, 5


# ---------------------------------------------------------------- validate


def cmd_validate(_a: argparse.Namespace) -> int:
    ok = True
    print("=" * 60)
    print("环境自检")
    print("=" * 60)

    # 1) cwd 安全性
    print(f"\n[1] 运行目录")
    print(f"    tool dir : {env.TOOL_DIR}")
    print(f"    cwd      : {Path.cwd()}")
    print(f"    safe cwd : {env.safe_cwd()}")
    if env.cwd_shadows_jira():
        print("    ⚠ 当前 cwd 下有 jira/ 同名目录，会让 `import jira` 解析成命名空间包。")
        print(f"      子进程已自动改用安全 cwd，但建议你 cd 到工具目录再跑：{env.TOOL_DIR}")

    # 2) 唯一 venv 与依赖
    print(f"\n[2] venv 与依赖（唯一解释器）")
    try:
        vpy = env.venv_python()
        print(f"    venv python: {vpy}")
    except FileNotFoundError as e:
        print(f"    ✗ {e}")
        return EXIT_ERR

    import importlib
    for m in ("jira", "cantools", "can", "requests"):
        try:
            mod = importlib.import_module(m)
            v = getattr(mod, "__version__", "")
            print(f"    ✓ {m:10} {v}")
        except Exception as e:  # noqa: BLE001
            print(f"    ✗ {m:10} {e}")
            ok = False

    # 3) 配置
    print(f"\n[3] 配置")
    try:
        cfg = env.load_config()
        print(f"    config    : {env.CONFIG_PATH}")
        projs = cfg.get("projects") or []
        print(f"    projects  : {len(projs)} 个")
        for p in projs:
            print(f"      - {p.get('name')}  (范围 {p.get('analysis_scope') or '(全部)'})")
        if not projs:
            print("    ⚠ projects 为空 —— 但仅影响工程归属判断，分析照跑")
        print(f"    JQL       : {cfg['jira']['jql']}")
    except Exception as e:  # noqa: BLE001
        print(f"    ✗ 读 config 失败：{e}")
        return EXIT_ERR

    # 4) 输入路径（inputs.json，只读）
    print(f"\n[4] 输入路径（只读，来自 inputs.json）")
    print(f"    {env.INPUTS_PATH}"
          f"  {'(exists)' if env.INPUTS_PATH.exists() else '(MISSING)'}")
    if not env.INPUTS_PATH.exists():
        print("    ⚠ 还没写 inputs.json —— 用 Claude 会话调用时会自动写；手工跑请自己写")
        ok = False
    else:
        for key in env.INPUT_KEYS:
            need = key in env.INPUT_REQUIRED
            try:
                p = env.input_path(key, required=need)
            except (FileNotFoundError, ValueError) as e:
                print(f"    ✗ {key:6} {e}")
                ok = False
                continue
            if p is None:
                print(f"    ○ {key:6} (未提供，可选)")
                continue
            if p.exists():
                kind = "目录" if p.is_dir() else "文件"
                print(f"    ✓ {key:6} [{kind}] {p}")
            else:
                print(f"    ✗ {key:6} 不存在：{p}")
                ok = False
        # 只读保护自检：不实际尝试写入，只确认闸门已装且能识别输入根
        roots = env.input_roots()
        print(f"    ✓ 只读保护已启用（jla_env.assert_writable，保护 {len(roots)} 个根）")
        # 代码根的版本信息（只读 git，取不到不算错）
        code = env.input_path("code", required=False)
        if code and code.is_dir():
            sha = env.git_readonly(code, "rev-parse", "HEAD")
            br = env.git_readonly(code, "rev-parse", "--abbrev-ref", "HEAD")
            cm = code / "CLAUDE.md"
            if sha:
                print(f"    ✓ code   commit {sha[:8]} @ {br or '-'}")
            else:
                print(f"    ○ code   未读到版本库（目录可能不是 git checkout，不影响分析）")
            print(f"    {'✓' if cm.exists() else '○'} code   CLAUDE.md "
                  f"{'入口 ' + str(cm) if cm.exists() else '(未找到，subagent 将直接读源码)'}")

    # 5) Jira 探活
    print(f"\n[5] Jira 探活")
    try:
        res = env.run_python_json(
            [env.jira_ops_script("search.py"), "--jql",
             "assignee = currentUser() ORDER BY updated desc", "--limit", "1"],
            timeout=120,
        )
        print(f"    ✓ 连通，我的工单总数 {res.get('total')}，本次返回 {res.get('returned')}")
        if res.get("issues"):
            i = res["issues"][0]
            print(f"      最新一条：{i.get('key')} {str(i.get('summary'))[:40]}")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        print(f"    ✗ Jira 探活失败：{msg[:300]}")
        if "退出码 3" in msg or "401" in msg or "403" in msg:
            print("      → 鉴权问题：检查 JIRA_PAT 环境变量或 jira-tool 的 config.json")
            return EXIT_AUTH
        ok = False

    # 6) vendor 内嵌工具（应全部相对工具目录，不该出现绝对路径）
    print(f"\n[6] 内嵌工具 vendor/")
    for label, key, required in [
        ("jira-tool ops", "jira_ops", True),
        ("frame_extractor", "frame_extractor", True),
        ("AI_canlog", "canlog_dir", True),
        ("lark-cli", "lark_cli", False),  # 可选：不随包分发
    ]:
        p = env.cfg_path("paths", key)
        exists = bool(p) and p.exists()
        shown = env.rel_to_tool(p) if p else "(未配置)"
        if exists:
            print(f"    ✓ {label:16} {shown}")
        elif required:
            print(f"    ✗ {label:16} {shown}")
            print(f"      → 重跑 python tools/sync_vendor.py 重建 vendor/")
            ok = False
        else:
            print(f"    ○ {label:16} {shown}（可选，不发邮件可不放）")

    # 7) 目录可写
    print(f"\n[7] 目录可写")
    for label, d in [("workspace", env.workspace_root()), ("out", env.out_root())]:
        try:
            t = d / ".write_test"
            t.write_text("ok", encoding="utf-8")
            t.unlink()
            print(f"    ✓ {label:10} {d}")
        except Exception as e:  # noqa: BLE001
            print(f"    ✗ {label:10} {d} — {e}")
            ok = False

    print("\n" + "=" * 60)
    print("结论：", "全部通过 ✓" if ok else "有问题 ✗（见上）")
    print("=" * 60)
    return EXIT_OK if ok else EXIT_ERR


# ---------------------------------------------------------------- list


def cmd_list(a: argparse.Namespace) -> int:
    cfg = env.load_config()
    jql = a.jql or cfg["jira"]["jql"]
    res = jla_fetch.search_tickets(jql=jql, limit=a.limit)
    print(f"JQL: {jql}")
    print(f"total={res.get('total')} returned={res.get('returned')} transferred={res.get('truncated')}")
    for i in res.get("issues", []):
        comps = ",".join(c.get("name", "") for c in (i.get("components") or []))
        print(f"  {i.get('key'):22} {(i.get('status') or {}).get('name',''):8} "
              f"{(i.get('priority') or {}).get('name',''):4} [{comps}] {str(i.get('summary'))[:40]}")
    return EXIT_OK


# ---------------------------------------------------------------- fetch / prepare / run


def _date_dir(a: argparse.Namespace) -> Path:
    """
    解析产物日期目录。
    `--date 2026-09-13` 只给日期名 → 落在 out_root 下；
    给绝对/相对路径 → 直接用（但一律 resolve，避免相对路径在别处被误解）。
    """
    if getattr(a, "date", None):
        pd = Path(a.date)
        d = pd if pd.is_absolute() or len(pd.parts) > 1 else (env.out_root() / pd)
    else:
        d = env.out_root() / _date.today().isoformat()
    d = d.resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _keys(a: argparse.Namespace) -> Optional[List[str]]:
    if getattr(a, "keys", None):
        return [s.strip() for s in a.keys.split(",") if s.strip()]
    # 命令行没给就看 inputs.json
    raw = env.load_inputs(required=False).get("keys")
    if isinstance(raw, list) and raw:
        return [str(s).strip() for s in raw if str(s).strip()]
    if isinstance(raw, str) and raw.strip():
        return [s.strip() for s in raw.split(",") if s.strip()]
    return None


def cmd_fetch(a: argparse.Namespace) -> int:
    out = jla_fetch.fetch(_keys(a), jql=getattr(a, "jql", None),
                          limit=getattr(a, "limit", None),
                          date_dir=_date_dir(a))
    print(json.dumps({k: v for k, v in out.items() if k != "tickets"},
                     ensure_ascii=False, indent=2))
    for t in out["tickets"]:
        print(f"  ✓ {t['key']} {t['summary'][:40]} 附件{t['attachments_total']}"
              f"（失败{len(t['attachments_failed'])}）")
    return EXIT_PARTIAL if out["errors"] else EXIT_OK


def prepare_one(ticket_dir: Path, projects: List[Dict[str, Any]],
                assigned: Dict[str, Any],
                code_info: Optional[Dict[str, Any]],
                claude_md: Optional[str],
                dbc_choice: str,
                docs_dir: Optional[str]) -> Dict[str, Any]:
    """对单票做：读 ticket.json → 附件解析 → 组装 bundle + prompt。"""
    key = ticket_dir.name
    data = env.read_json(ticket_dir / "ticket.json", {})
    issue = data.get("issue", {})

    # 1) 附件解析（DBC 从 inputs.json 取，见 jla_attach.process_ticket）
    ev = jla_attach.process_ticket(ticket_dir)

    # 2) 时间线索（给 digest 定位窗口）
    texts = [issue.get("description") or ""]
    for c in issue.get("comments") or []:
        texts.append(c.get("body") or "")
    hints = jla_attach.extract_time_hints(*texts)
    id_hints = jla_attach.extract_id_hints(*texts)
    if id_hints:
        env.log(f"  工单点名的 ID: {', '.join(id_hints)}")

    # 3) 对每个帧日志解析产物补 digest（按目录成组解析，产物名来自目录名）
    for p in ev.get("parsers", []):
        if p.get("tool") != "frame_extractor" or not p.get("ok"):
            continue
        for produced in p.get("produced", []):
            if not produced.endswith("_frames.jsonl"):
                continue
            jsonl = Path(produced)
            if jsonl.exists():
                jla_attach.build_frames_digest(
                    jsonl, jsonl.parent / f"{jsonl.stem}_digest.md",
                    hint_times=hints, focus_ids=id_hints)

    jla_attach.build_evidence_index(ticket_dir, ev, hint_times=hints)

    # 4) bundle + prompt
    analysis_scope = ""
    proj_cfg = next((p for p in projects
                     if p.get("name") == (assigned.get("project") or "")), None)
    if proj_cfg:
        analysis_scope = proj_cfg.get("analysis_scope") or ""

    jla_bundle.build_bundle_md(
        ticket_dir, ticket_key=key, issue=issue, projects=projects,
        assigned=assigned, code_info=code_info, claude_md=claude_md,
        dbc_choice=dbc_choice, docs_dir=docs_dir, evidence=ev,
        hint_times=hints, status="等待派发 subagent",
    )
    prompt = jla_bundle.build_ticket_worker_prompt(
        ticket_dir, ticket_key=key, title=issue.get("summary", ""),
        jira_url=issue.get("url", ""), projects=projects, assigned=assigned,
        code_info=code_info, claude_md=claude_md, dbc_choice=dbc_choice,
        docs_dir=docs_dir, analysis_scope=analysis_scope, hint_times=hints,
    )
    env.write_text(ticket_dir / "worker_prompt.md", prompt)

    return {"key": key, "evidence": ev, "claude_md": claude_md,
            "hints": hints, "id_hints": id_hints, "assigned": assigned}


def cmd_prepare(a: argparse.Namespace) -> int:
    date_dir = _date_dir(a)
    cfg = env.load_config()
    projects = cfg.get("projects") or []

    # 输入路径：**先校验**，路径写错要在这里就报，不要跑到一半才发现
    try:
        inputs = env.load_inputs(required=True)
        env.input_path("code", required=True)
    except (FileNotFoundError, ValueError) as e:
        print(f"\n✗ 输入路径准备失败：{e}", file=sys.stderr)
        return EXIT_ERR

    code_info = jla_bundle.build_code_info(inputs)
    claude_md = jla_bundle.resolve_claude_md(code_info)
    docs_dir = jla_bundle.resolve_docs_dir(inputs)
    dbc_choice = jla_bundle.resolve_dbc_choice(inputs)

    env.log(f"代码: {code_info['src'] if code_info else '(未提供)'}")
    env.log(f"CLAUDE.md: {claude_md or '(未找到)'}")
    env.log(f"DBC: {dbc_choice or '(未提供 —— BLF 只能按裸 ID 解析)'}")
    env.log(f"文档: {docs_dir or '(未提供)'}")

    # 需要哪些单：优先用已 fetch 的目录；--keys 指定则过滤
    keys = _keys(a)
    dirs = [d for d in sorted(date_dir.iterdir()) if d.is_dir() and (d / "ticket.json").exists()]
    if keys:
        dirs = [d for d in dirs if d.name in keys]
    if not dirs:
        print(f"没有可处理的工单目录（{date_dir}）。先跑 fetch。", file=sys.stderr)
        return EXIT_ARG

    # 工程归属：优先用 Claude 复核过的 _assignments.json；
    # 没有就退回占位（并由 orchestrator 提示 Claude 复核）
    assignments_file = date_dir / "_assignments.json"
    reviewed = env.read_json(assignments_file, {}) if assignments_file.exists() else {}

    assignments: Dict[str, Dict[str, Any]] = {}
    for d in dirs:
        prev = reviewed.get(d.name) or {}
        if prev.get("project"):
            assignments[d.name] = prev  # 保留 Claude 的复核结果
        else:
            assignments[d.name] = {
                "project": projects[0]["name"] if len(projects) == 1 else "",
                "confidence": "low" if len(projects) == 1 else "",
                "reason": ("config 里只有一个工程，暂按它处理，待 Claude 复核"
                           if len(projects) == 1 else "待 Claude 判断（见 orchestrator.md 第一步）"),
            }
    env.write_json(assignments_file, assignments)
    unreviewed = [k for k, v in assignments.items()
                  if v.get("reason", "").startswith(("config 里只有", "待 Claude"))]
    if unreviewed:
        env.log(f"⚠ 以下单的工程归属尚未由 Claude 复核：{', '.join(unreviewed)}"
                f"（见 orchestrator.md 第一步）")
    else:
        env.log("工程归属已全部复核")

    results = []
    failures = []
    for d in dirs:
        try:
            env.log(f"—— 处理 {d.name} ——")
            r = prepare_one(d, projects, assignments.get(d.name, {}),
                            code_info, claude_md, dbc_choice, docs_dir)
            results.append(r)
            env.log(f"  {d.name} 就绪")
        except Exception as e:  # noqa: BLE001
            env.log(f"  {d.name} 处理失败：{e}")
            failures.append({"key": d.name, "error": str(e)})
            jla_summary.write_receipt(d, key=d.name, status="failed", error=str(e))

    mats = jla_summary.prepare_dispatch_materials(date_dir)
    print(json.dumps({"date_dir": str(date_dir), "ready": len(results),
                      "failed": failures, "dispatch": mats["items"]},
                     ensure_ascii=False, indent=2)[:4000])
    return EXIT_PARTIAL if failures else EXIT_OK


def cmd_run(a: argparse.Namespace) -> int:
    """
    一键：fetch → prepare → summary（到"可派发"状态为止）。
    不含 subagent 派发与发邮件 —— 那两步由 Claude 会话执行/确认。
    """
    rc = cmd_fetch(a)
    if rc not in (EXIT_OK, EXIT_PARTIAL):
        return rc
    rc2 = cmd_prepare(a)
    # 即便 prepare 有失败，也生成 SUMMARY，保证失败单不丢失
    try:
        date_dir = _date_dir(a)
        fs = env.read_json(date_dir / "_fetch_summary.json", {}) or {}
        out = jla_summary.write_summary(date_dir, fs.get("skipped_by_limit", 0))
        env.log(f"SUMMARY → {out}")
    except Exception as e:  # noqa: BLE001
        env.log(f"SUMMARY 生成失败：{e}")
    return max(rc, rc2)


def cmd_all(a: argparse.Namespace) -> int:
    """
    全流程：fetch → prepare → summary → 派发（需 Claude）→ mail。
    本命令只做到 summary + 可选发信；派发必须由 Claude 用 Agent 工具完成，
    所以这里会在需要派发时**明确提示**，不假装能自动派发。
    """
    rc = cmd_run(a)
    date_dir = _date_dir(a)
    mats = jla_summary.prepare_dispatch_materials(date_dir)
    ready = [i for i in mats["items"] if i["has_prompt"]]
    if ready:
        print("\n" + "=" * 60)
        print(f"有 {len(ready)} 单已就绪，等待派发 subagent：")
        for i in ready:
            print(f"  - {i['key']}  prompt: {i['prompt']}")
        print("\n下一步（必须由 Claude 会话执行，见 prompts/orchestrator.md）：")
        print("  对每单用 Agent 工具派 general-purpose subagent，prompt 取对应的 worker_prompt.md；")
        print("  一次一单、顺序执行。subagent 会写 analysis.md 与 receipt.json。")
        print("  全部完成后：")
        print(f'    jla.py summary --date {date_dir.name}')
        print(f'    jla.py mail-only --date {date_dir.name}')
        print("=" * 60)
    if getattr(a, "send", False):
        rc3 = cmd_mail(a)
        rc = max(rc, rc3)
    return rc


def cmd_summary(a: argparse.Namespace) -> int:
    """从 receipt.json 生成 SUMMARY.md（发邮件前跑）。"""
    date_dir = _date_dir(a)
    fs = env.read_json(date_dir / "_fetch_summary.json", {}) or {}
    out = jla_summary.write_summary(date_dir, fs.get("skipped_by_limit", 0))
    print(f"SUMMARY → {out}")
    print(env.read_text(out)[:2000])
    return EXIT_OK


def cmd_mail(a: argparse.Namespace) -> int:
    date_dir = _date_dir(a)
    # 发信前确保 SUMMARY 存在且是最新的（失败/跳过不会被漏掉）
    fs = env.read_json(date_dir / "_fetch_summary.json", {}) or {}
    jla_summary.write_summary(date_dir, fs.get("skipped_by_limit", 0))
    r = jla_mail.send_daily(date_dir, dry_run=a.dry_run)
    print(json.dumps(r, ensure_ascii=False, indent=2)[:3000])
    return EXIT_OK if r.get("ok") or r.get("dry_run") else EXIT_ERR


# ---------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="jla", description="Jira 工单 → Log 解析 → 代码感知分析 → 邮件 流水线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="环境自检").set_defaults(fn=cmd_validate)

    p = sub.add_parser("list", help="只列工单")
    p.add_argument("--jql"); p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_list)

    def add_common(sp):
        sp.add_argument("--keys", help="逗号分隔的工单 key（缺省则读 inputs.json 的 keys）")
        sp.add_argument("--jql", help="覆盖 JQL")
        sp.add_argument("--limit", type=int, help="最多几张")
        sp.add_argument("--date", help="产物日期目录 YYYY-MM-DD（默认今天）")
        return sp

    add_common(sub.add_parser("fetch", help="拉工单+附件")).set_defaults(fn=cmd_fetch)
    add_common(sub.add_parser("prepare", help="解析附件+组装 bundle")).set_defaults(fn=cmd_prepare)
    add_common(sub.add_parser("run", help="fetch+prepare+summary")).set_defaults(fn=cmd_run)

    pa = add_common(sub.add_parser("all", help="run + 提示派发（可 --send 发信）"))
    pa.add_argument("--send", action="store_true", help="派发完成后自动发邮件")
    pa.set_defaults(fn=cmd_all)

    ps = sub.add_parser("summary", help="从 receipt 生成 SUMMARY.md")
    ps.add_argument("--date"); ps.set_defaults(fn=cmd_summary)

    pm = sub.add_parser("mail-only", help="只发邮件")
    pm.add_argument("--date"); pm.add_argument("--dry-run", action="store_true")
    pm.set_defaults(fn=cmd_mail)

    a = ap.parse_args(argv)

    # 输入路径的只读闸门：解析一次，让 assert_writable 从第一刻起生效。
    # 刻意**不在这里报错** —— validate 需要能在没有 inputs.json 时也跑起来；
    # 真正缺路径的错误由 prepare 给出（那里才知道必需哪个）。
    try:
        env.input_roots()
    except (FileNotFoundError, ValueError):
        pass

    # cwd 安全检查：cwd 下若有 jira/ 同名目录，会让 `import jira` 解析成命名空间包
    try:
        if env.cwd_shadows_jira():
            print(f"✗ 当前 cwd（{Path.cwd()}）下有 jira/ 同名目录，"
                  f"会让 `import jira` 解析成命名空间包。\n"
                  f"  请 cd 到工具目录再跑：{env.TOOL_DIR}\n"
                  f"  （注：子进程已自动用安全 cwd，但本进程内的 import 仍会被遮蔽）",
                  file=sys.stderr)
            return EXIT_ARG
    except OSError:
        pass

    try:
        return a.fn(a)
    except FileNotFoundError as e:
        print(f"✗ {e}", file=sys.stderr)
        return EXIT_ERR
    except RuntimeError as e:
        msg = str(e)
        print(f"✗ {msg}", file=sys.stderr)
        if "退出码 3" in msg or "401" in msg or "403" in msg:
            return EXIT_AUTH
        if "退出码 4" in msg:
            return EXIT_API
        return EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
