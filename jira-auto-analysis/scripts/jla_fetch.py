#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
jla_fetch —— 拉取工单：列表 → 逐单详情/评论/附件 → 落 staging

设计取舍：
  * 一律复用 jira-tool 的 ops 脚本（subprocess 调），不 import python-jira 重写一遍。
    原因：jira-tool 已经把 Server 版的各种坑（datetime 格式、{"name":...} 用户字段、
    createmeta 不可用）处理过了，重造只会引入新 bug。
  * 但下载附件之后要做的"评论里的 !image-xxx.png! → 本地路径"映射，是本工具独有逻辑，
    因为只有它知道附件落在哪个目录。

产出（每单一个目录）：
  <out>/<date>/<KEY>/ticket.json   机器读（完整字段，含 raw）
  <out>/<date>/<KEY>/ticket.md     人/subagent 读（简洁版，图片引用已替换为本地路径）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jla_env as env

# ---------------------------------------------------------------- 正则

# 评论正文里的 Jira wiki 图片引用：!image-2026-09-10-15-44-34-916.png!
WIKI_IMAGE_RE = re.compile(r"!([^!\s]+\.(?:png|jpg|jpeg|gif|bmp|webp))!", re.IGNORECASE)
# 通用附件引用（非图片）也常写成 [^file.log] 或 !file.log!
WIKI_FILE_RE = re.compile(r"[!\[]([^!\]]+\.(?:log|txt|blf|dbc|tar\.gz|tgz|zip|csv|xlsx|docx|pdf))[!\]]", re.IGNORECASE)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}


# ---------------------------------------------------------------- 拉列表


def search_tickets(jql: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
    """调 jira-tool 的 search.py，返回 {total, returned, issues:[...]}。"""
    cfg = env.load_config()
    jql = jql or cfg["jira"]["jql"]
    limit = limit or cfg["jira"].get("max_tickets", 5)
    return env.run_python_json(
        [env.jira_ops_script("search.py"), "--jql", jql, "--limit", str(limit)],
        timeout=120,
    )


def get_issue(key: str) -> Dict[str, Any]:
    """调 get_issue.py 取详情（含评论）。"""
    return env.run_python_json(
        [env.jira_ops_script("get_issue.py"), key, "--compact"],
        timeout=120,
    )


def list_attachments(key: str) -> List[Dict[str, Any]]:
    """调 attachments.py list。"""
    data = env.run_python_json(
        [env.jira_ops_script("attachments.py"), "list", key],
        timeout=120,
    )
    return data.get("attachments", []) or []


def download_attachment(att_id: str, out_dir: Path) -> Dict[str, Any]:
    """调 attachments.py download --id <id> --out <dir>。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    return env.run_python_json(
        [
            env.jira_ops_script("attachments.py"),
            "download",
            "--id",
            str(att_id),
            "--out",
            str(out_dir),
        ],
        timeout=600,  # 23MB 的 tar.gz 要走一会儿
    )


# ---------------------------------------------------------------- 组装


def _fmt_user(u: Optional[Dict[str, Any]]) -> str:
    if not u:
        return "-"
    return u.get("displayName") or u.get("name") or "-"


def _comment_wiki_to_local(body: str, local_names: Dict[str, str]) -> str:
    """
    把评论正文里的 !image-xxx.png! / !file.log! 换成 `[附件: 本地相对路径]`。
    local_names: 文件名(小写) -> 本地相对路径
    """
    if not body:
        return ""

    def repl_img(m: re.Match) -> str:
        fn = m.group(1)
        local = local_names.get(fn.lower())
        if local:
            return f"`[图片附件: {local}]`"
        return f"`[图片引用（未找到对应附件）: {fn}]`"

    body = WIKI_IMAGE_RE.sub(repl_img, body)

    def repl_file(m: re.Match) -> str:
        fn = m.group(1)
        local = local_names.get(fn.lower())
        if local:
            return f"`[附件: {local}]`"
        return f"`[附件引用（未找到）: {fn}]`"

    body = WIKI_FILE_RE.sub(repl_file, body)
    return body


def build_ticket_md(
    issue: Dict[str, Any],
    attachments: List[Dict[str, Any]],
    local_names: Dict[str, str],
    repo_rel: Optional[str] = None,
) -> str:
    """生成人/subagent 可读的 ticket.md。"""
    L: List[str] = []
    key = issue.get("key", "?")
    L.append(f"# {key} {issue.get('summary', '')}")
    L.append("")
    L.append(f"> 来源: {issue.get('url', '')}")
    L.append(
        f"> 状态: {issue.get('status', {}).get('name', '-')}"
        f" | 类型: {issue.get('issuetype', {}).get('name', '-')}"
        f" | 优先级: {issue.get('priority', {}).get('name', '-')}"
    )
    proj = issue.get("project") or {}
    comps = ", ".join(c.get("name", "") for c in (issue.get("components") or [])) or "-"
    L.append(f"> 项目: {proj.get('name', '-')} ({proj.get('id', '-')}) | 组件: {comps}")
    L.append(
        f"> 经办人: {_fmt_user(issue.get('assignee'))}"
        f" | 报告人: {_fmt_user(issue.get('reporter'))}"
        f" | 创建: {issue.get('created', '-')} | 更新: {issue.get('updated', '-')}"
    )
    if repo_rel:
        L.append(f"> 代码工作区: {repo_rel}")
    L.append("")

    L.append("## 描述")
    L.append("")
    L.append((issue.get("description") or "（空）").strip())
    L.append("")

    L.append(f"## 评论（共 {len(issue.get('comments') or [])} 条）")
    L.append("")
    for c in issue.get("comments") or []:
        L.append(
            f"### [{c.get('id', '?')}] {_fmt_user(c.get('author'))} @ {c.get('created', '-')}"
        )
        L.append("")
        L.append(_comment_wiki_to_local(c.get("body", ""), local_names).strip())
        L.append("")

    L.append(f"## 附件清单（共 {len(attachments)} 个）")
    L.append("")
    L.append("| 文件名 | 大小 | 上传人 | 时间 | 本地路径 | 类型 |")
    L.append("|---|---|---|---|---|---|")
    for a in attachments:
        fn = a.get("filename", "")
        local = local_names.get(fn.lower(), "（未下载）")
        size = a.get("size") or 0
        kind = "图片" if Path(fn).suffix.lower() in IMAGE_EXTS else Path(fn).suffix.lower().lstrip(".")
        L.append(
            f"| {fn} | {size:,} | {_fmt_user(a.get('author'))} "
            f"| {a.get('created', '-')} | {local} | {kind} |"
        )
    L.append("")
    return "\n".join(L)


def attachments_not_referenced(
    issue: Dict[str, Any], attachments: List[Dict[str, Any]]
) -> List[str]:
    """找出没被任何评论/描述引用过的附件（尤其图片），提醒 subagent 也要看。"""
    text = (issue.get("description") or "") + "\n"
    for c in issue.get("comments") or []:
        text += (c.get("body") or "") + "\n"
    text_l = text.lower()
    missing = []
    for a in attachments:
        fn = (a.get("filename") or "").lower()
        if fn and fn not in text_l:
            missing.append(a.get("filename", ""))
    return missing


# ---------------------------------------------------------------- 主流程


def fetch_one(
    key: str,
    date_dir: Path,
    *,
    download: bool = True,
) -> Dict[str, Any]:
    """
    拉取单张工单并落 staging。返回 summary 字典。
    单票失败由调用方捕获，不影响其它票。
    """
    env.log(f"拉取工单 {key} …")
    ticket_dir = date_dir / key
    ticket_dir.mkdir(parents=True, exist_ok=True)
    att_dir = ticket_dir / "attachments"

    issue = get_issue(key)
    attachments = list_attachments(key)

    # 下载附件 + 建立 文件名 -> 本地相对路径 映射
    local_names: Dict[str, str] = {}
    failed: List[str] = []
    if download:
        for a in attachments:
            fn = a.get("filename", "")
            aid = a.get("id")
            if not fn or aid is None:
                continue
            try:
                download_attachment(str(aid), att_dir)
                rel = f"attachments/{fn}"
                # 实际落盘名可能与 filename 不完全一致，找一下
                if not (ticket_dir / rel).exists():
                    cand = [p for p in att_dir.iterdir() if p.name.lower() == fn.lower()]
                    if cand:
                        rel = f"attachments/{cand[0].name}"
                local_names[fn.lower()] = rel
            except Exception as e:  # noqa: BLE001 - 逐附件容错
                failed.append(f"{fn}: {e}")
                env.log(f"  附件下载失败 {fn}: {e}")

    env.write_json(ticket_dir / "ticket.json", {
        "issue": issue,
        "attachments": attachments,
        "attachments_failed": failed,
        "local_names": local_names,
    })

    md = build_ticket_md(issue, attachments, local_names)
    env.write_text(ticket_dir / "ticket.md", md)

    unreferenced = attachments_not_referenced(issue, attachments)

    summary = {
        "key": key,
        "summary": issue.get("summary", ""),
        "status": (issue.get("status") or {}).get("name", ""),
        "priority": (issue.get("priority") or {}).get("name", ""),
        "project": (issue.get("project") or {}).get("name", ""),
        "components": [c.get("name", "") for c in (issue.get("components") or [])],
        "updated": issue.get("updated", ""),
        "dir": str(ticket_dir),
        "attachments_total": len(attachments),
        "attachments_failed": failed,
        "attachments_unreferenced": unreferenced,
    }
    env.log(
        f"  {key}: {len(attachments)} 附件（失败 {len(failed)}），"
        f"未引用 {len(unreferenced)} 个"
    )
    return summary


def fetch(
    keys: Optional[List[str]] = None,
    *,
    jql: Optional[str] = None,
    limit: Optional[int] = None,
    date_dir: Optional[Path] = None,
    download: bool = True,
) -> Dict[str, Any]:
    """
    拉取入口。keys 给了就只拉这些；否则按 JQL 搜。
    返回 {"date_dir":…, "tickets":[…], "skipped_by_limit":N, "errors":[…]}
    """
    cfg = env.load_config()
    from datetime import date as _date

    date_dir = date_dir or (env.out_root() / _date.today().isoformat())
    date_dir.mkdir(parents=True, exist_ok=True)

    errors: List[Dict[str, str]] = []
    skipped_by_limit = 0

    if keys:
        targets = list(keys)
    else:
        res = search_tickets(jql=jql, limit=limit)
        all_issues = res.get("issues", []) or []
        max_n = limit or cfg["jira"].get("max_tickets", 5)
        targets = [i.get("key") for i in all_issues[:max_n] if i.get("key")]
        # 不静默截断：超出的记数，由 SUMMARY 明确列出
        skipped_by_limit = max(0, len(all_issues) - len(targets))

    tickets: List[Dict[str, Any]] = []
    for k in targets:
        try:
            tickets.append(fetch_one(k, date_dir, download=download))
        except Exception as e:  # noqa: BLE001 - 逐单容错
            env.log(f"  工单 {k} 拉取失败: {e}")
            errors.append({"key": k, "error": str(e)})

    result = {
        "date_dir": str(date_dir),
        "tickets": tickets,
        "skipped_by_limit": skipped_by_limit,
        "errors": errors,
    }
    env.write_json(date_dir / "_fetch_summary.json", result)
    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="拉取 Jira 工单到 staging")
    ap.add_argument("--keys", help="逗号分隔的工单 key；省略则按 JQL 搜")
    ap.add_argument("--jql", help="覆盖 config 里的 JQL")
    ap.add_argument("--limit", type=int, help="最多拉几张")
    ap.add_argument("--no-download", action="store_true", help="只列附件不下载")
    a = ap.parse_args()

    ks = [s.strip() for s in a.keys.split(",")] if a.keys else None
    out = fetch(ks, jql=a.jql, limit=a.limit, download=not a.no_download)
    env.log(f"完成：{len(out['tickets'])} 单，失败 {len(out['errors'])}")
