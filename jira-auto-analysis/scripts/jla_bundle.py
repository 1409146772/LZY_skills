#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
jla_bundle —— 组装每单 subagent 的输入包

产出（在 <ticket_dir>/ 下）：
  bundle.md         给 subagent 的总入口（含工程归属、代码位置、DBC、证据索引指向）
  worker_prompt.md  可直接派发的 subagent prompt（占位符已替换）

关键：bundle.md 里**只放摘要与指针，不放日志正文**。
     日志细节留在 evidence/ 里，由 subagent 按 INDEX.md 的阅读顺序自己取。

代码 / DBC / 文档三个路径来自 inputs.json（调用前由 Claude 会话写好），
本模块不做任何探测 —— 你怎么给，就怎么用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import jla_env as env
import jla_attach


def projects_block(projects: List[Dict[str, Any]]) -> str:
    """把全部候选工程渲染成给 AI 看的描述块。"""
    L: List[str] = []
    for i, p in enumerate(projects, 1):
        L.append(f"{i}. **{p.get('name','')}**")
        L.append(f"   - 描述：{p.get('desc','')}")
        if p.get("analysis_scope"):
            L.append(f"   - 分析范围：`{p['analysis_scope']}`")
    return "\n".join(L) if L else "（config.projects 为空，请先填写）"


def build_bundle_md(
    ticket_dir: Path,
    *,
    ticket_key: str,
    issue: Dict[str, Any],
    projects: List[Dict[str, Any]],
    assigned: Dict[str, Any],
    code_info: Optional[Dict[str, Any]],
    claude_md: Optional[str],
    dbc_choice: str,
    docs_dir: Optional[str],
    evidence: Dict[str, Any],
    hint_times: List[str],
    status: str,
) -> Path:
    L: List[str] = []
    L.append(f"# 分析输入包 {ticket_key}")
    L.append("")
    L.append(f"> {issue.get('summary','')}")
    L.append(f"> 状态: {(issue.get('status') or {}).get('name','-')} | "
             f"优先级: {(issue.get('priority') or {}).get('name','-')}")
    comps = ", ".join(c.get("name", "") for c in (issue.get("components") or [])) or "-"
    L.append(f"> 项目: {(issue.get('project') or {}).get('name','-')} | 组件: {comps}")
    L.append("")

    L.append("## 你要读的文件（按顺序）")
    L.append("")
    L.append(f"1. `{ticket_dir.name}/ticket.md` — 工单正文 + 评论（图片引用已替换为本地路径）")
    L.append(f"2. `{ticket_dir.name}/evidence/INDEX.md` — 证据索引（**必读**，告诉你该怎么读大文件）")
    if claude_md:
        L.append(f"3. `{claude_md}` — 代码库 CLAUDE.md 入口")
    if docs_dir:
        L.append(f"4. `{docs_dir}` — 你的 md 文档目录（按需 Read/Grep）")
    L.append("")
    L.append("**不要整读** `*_frames.jsonl`（可达 10MB+）。需要细节用 Grep 定向搜。")
    L.append("")

    L.append("## 工程归属判断")
    L.append("")
    L.append(f"- 判定：**{assigned.get('project','（未判定）')}**")
    L.append(f"- 置信度：{assigned.get('confidence','-')}")
    L.append(f"- 理由：{assigned.get('reason','-')}")
    L.append("")
    L.append("全部候选工程（若你认为判错了，在第 9 节提改判建议并给依据）：")
    L.append("")
    L.append(projects_block(projects))
    L.append("")

    if code_info and code_info.get("src"):
        L.append("## 代码工作区")
        L.append("")
        if code_info.get("project"):
            L.append(f"- 工程：`{code_info['project']}`")
        if code_info.get("note"):
            L.append(f"- 说明：{code_info['note']}")
        L.append(f"- 分支：`{code_info.get('branch') or '-'}`")
        L.append(f"- commit：`{code_info.get('sha8') or '-'}`")
        L.append(f"- 源码目录：`{code_info['src']}`")
        L.append("")
        L.append("> ⚠ 这是**用户自己的工作区目录**（不是工具内的快照），而且它很可能带一个"
                 "**真实的 `.git`**。**严禁任何 git 写操作**（commit/checkout/stash/clean…），"
                 "也不要修改、创建、删除任何文件 —— 只读。")
        if not code_info.get("has_git"):
            L.append(">")
            L.append("> 注：本次没在该目录下读到版本库，commit 无法与仓库对账。")
        L.append("")
    else:
        L.append("## 代码工作区")
        L.append("")
        L.append("**无代码可读**（inputs.json 未提供 `code`）。"
                 "请仅基于工单文本与附件证据分析，并在报告中明确标注「未经代码验证」。")
        L.append("")

    if claude_md:
        L.append("## CLAUDE.md")
        L.append("")
        L.append(f"入口：`{claude_md}`")
        L.append("")
        L.append("**先读根 CLAUDE.md 的架构地图定位模块，再读相关模块的 CLAUDE.md（尤其 Gotchas）**，"
                 "然后才去 Grep/Read 源码。CLAUDE.md 里的已知结论要复用，不要重新发现。")
        L.append("")

    L.append("## DBC")
    L.append("")
    L.append(f"- 本次使用：{dbc_choice or '（未提供 DBC）'}")
    L.append("")
    if not dbc_choice:
        L.append("> ⚠ 未提供 DBC → CAN 报文可能解不出信号名（只见裸 ID）。"
                 "若工单涉及报文含义，请在报告里说明这一限制。")
        L.append("")

    L.append("## 文档资料")
    L.append("")
    if docs_dir:
        L.append(f"- 目录：`{docs_dir}`")
        L.append("")
        L.append("用户已整理好的 md 文档，直接 Read/Grep。引用时写清**哪个文件 + 哪一节**，"
                 "不要只说\"参考了某文档\"。")
    else:
        L.append("（本次未提供文档目录）")
    L.append("")

    L.append("## 证据摘要")
    L.append("")
    L.append(f"- 图片：{len(evidence.get('images', []))} 张" +
             ("（**每张都要 Read 看，抄下可读文字/数值**）" if evidence.get("images") else ""))
    L.append(f"- 日志：{len(evidence.get('logs', []))} 个"
             f"（其中 5AA5 帧日志 {sum(1 for g in evidence.get('logs',[]) if g.get('kind')=='log5aa5')} 个）")
    L.append(f"- BLF：{len(evidence.get('blf', []))} 个")
    L.append(f"- 压缩包：{len(evidence.get('archives', []))} 个（已解包并重新分类）")
    if evidence.get("video"):
        L.append(f"- 视频：{len(evidence['video'])} 个 —— **未分析**，不要推测其内容，"
                 "只在报告里记一行")
    if evidence.get("unknown"):
        L.append(f"- 未识别：{len(evidence['unknown'])} 个（未解析）")
    L.append("")
    if hint_times:
        L.append(f"**工单时间线索**：{', '.join(hint_times)} —— digest 已按这些时间做了窗口提取，优先看。")
        L.append("")
    for n in evidence.get("notes", []):
        L.append(f"- ⚠ {n}")
    L.append("")

    L.append("## 当前状态")
    L.append("")
    L.append(f"`{status}`")
    L.append("")

    out = ticket_dir / "bundle.md"
    env.write_text(out, "\n".join(L))
    return out


def build_ticket_worker_prompt(
    ticket_dir: Path,
    *,
    ticket_key: str,
    title: str,
    jira_url: str,
    projects: List[Dict[str, Any]],
    assigned: Dict[str, Any],
    code_info: Optional[Dict[str, Any]],
    claude_md: Optional[str],
    dbc_choice: str,
    docs_dir: Optional[str],
    analysis_scope: str,
    hint_times: List[str],
) -> str:
    """读 prompts/ticket_worker.md 并替换占位符，返回可直接派发的 prompt。"""
    tpl_path = env.TOOL_DIR / "prompts" / "ticket_worker.md"
    tpl = env.read_text(tpl_path)

    ci = code_info or {}
    repl = {
        "{{TICKET_DIR}}": str(ticket_dir),
        "{{TICKET_KEY}}": ticket_key,
        "{{TITLE}}": title,
        "{{JIRA_URL}}": jira_url,
        "{{PROJECTS}}": projects_block(projects),
        "{{ASSIGNED_PROJECT}}": assigned.get("project", "（未判定）"),
        "{{ASSIGNED_CONFIDENCE}}": assigned.get("confidence", "-"),
        "{{ASSIGNED_REASON}}": assigned.get("reason", "-"),
        "{{REPO_SRC}}": ci.get("src") or "(无代码)",
        "{{REPO_PROJECT}}": ci.get("project") or "-",
        "{{REPO_BRANCH}}": ci.get("branch") or "-",
        "{{REPO_SHA}}": ci.get("sha8") or "-",
        "{{CLAUDE_MD}}": claude_md or "(无)",
        "{{ANALYSIS_SCOPE}}": analysis_scope or "(整个仓库)",
        "{{DBC_CHOICE}}": dbc_choice or "（未提供 DBC）",
        "{{DOCS_DIR}}": docs_dir or "(未提供)",
        "{{TIME_HINTS}}": ", ".join(hint_times) if hint_times else "（无）",
    }
    for k, v in repl.items():
        tpl = tpl.replace(k, str(v))
    return tpl


# ---------------------------------------------------------------- 代码信息

def build_code_info(inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    从 inputs.json 的三个路径组装给 subagent 的代码信息。

    branch / sha 用**只读** git 现取；取不到就留空，不猜、不报错 ——
    目录可能根本不是 git 仓库（用户给了个导出的快照），那不是错误。
    """
    code = env.input_path("code", required=False)
    if code is None:
        return None
    src = str(code)
    info: Dict[str, Any] = {
        "src": src,
        "project": inputs.get("code_project") or "",
        "note": inputs.get("code_note") or "",
        "branch": "",
        "sha": "",
        "sha8": "",
        "has_git": False,
    }
    branch = env.git_readonly(code, "rev-parse", "--abbrev-ref", "HEAD")
    sha = env.git_readonly(code, "rev-parse", "HEAD")
    if sha:
        info["has_git"] = True
        info["sha"] = sha
        info["sha8"] = sha[:8]
    if branch:
        info["branch"] = branch
    return info


def resolve_claude_md(code_info: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    CLAUDE.md 入口：就用 <code>/CLAUDE.md。

    刻意不做 rglob 探测 —— 原版会扫整棵树找 CLAUDE.md，但那是"用户自己维护 CLAUDE.md"
    这条约定之前的历史包袱。现在你给哪个目录，就看那个目录的 CLAUDE.md；
    没有就如实告诉 subagent「无」，让它直接读源码。
    """
    if not code_info or not code_info.get("src"):
        return None
    entry = Path(code_info["src"]) / "CLAUDE.md"
    return str(entry) if entry.exists() else None


def resolve_docs_dir(inputs: Dict[str, Any]) -> Optional[str]:
    """文档目录（原样透传，不扫描、不降维）。"""
    p = env.input_path("docs", required=False)
    return str(p) if p else None


def resolve_dbc_choice(inputs: Dict[str, Any]) -> str:
    """
    DBC 描述串。

    你给的那个路径就是本次要用的 —— 工具不再做"多份 DBC 里挑一份"的索引与选型
    （选型逻辑见 prompts/orchestrator.md，由 Claude 判断并在报告里写明理由）。
    """
    p = env.input_path("dbc", required=False)
    if p is None:
        return ""
    if not p.exists():
        return f"{p}（⚠ 路径不存在，CAN 将按裸 ID 解析）"
    return f"`{p}`（由 inputs.json 指定，AI 未做选型；若 CAN 证据指向另一份请在报告第 9 节提出）"
