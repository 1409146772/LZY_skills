#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
jla_mail —— 用 lark-cli 发 HTML 汇总邮件

踩过的坑（来自 claude-md-map SKILL.md 的实测记录，必须遵守）：
  1. lark-cli 不在 PATH，路径从 config.paths.lark_cli 取（默认 vendor/lark-cli/lark-cli.exe）。
     该 exe **不随分发包提供**（49MB，且换机器要重新登录），用户按需放入；
     见 vendor/lark-cli/README.md。
  2. `--attach` 只接受 **cwd 相对路径** → 发信前必须 cd 到产物目录。
  3. 正文**必须内联 `--body`**，禁止 `--body-file` 指向工程树内文件：
     公司 DLP 会把文件重新加密，lark-cli 读到密文，正文整个乱码。
  4. 附件要先用 head 检查有无 `%TSD-Header` 密文头，被加密的不能发。
  5. 若响应只有 draft_id 没有 message_id，需补一条 drafts send。

只附小 .md（analysis.md / SUMMARY.md），**不附**几十 MB 的原始日志。
"""

from __future__ import annotations

import html
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import jla_env as env

DLP_MARKER = b"%TSD-Header"


def lark_cli() -> str:
    """
    lark-cli.exe 的路径。**故意不随包分发**（49MB，且换机器要重新登录），
    所以要给一条能自救的报错：告诉用户放哪、或者怎么改配置。
    """
    p = env.cfg_path("paths", "lark_cli")
    if not p:
        raise KeyError("config.paths.lark_cli 未配置")
    if not p.exists():
        raise FileNotFoundError(
            f"lark-cli 不存在: {env.rel_to_tool(p)}\n"
            f"它不随分发包提供。请二选一：\n"
            f"  1) 把 lark-cli.exe 放到 {env.rel_to_tool(env.VENDOR_DIR / 'lark-cli')}/lark-cli.exe\n"
            f"     （见该目录下的 README.md）\n"
            f"  2) 在 config.json 里把 paths.lark_cli 指向你机器上的 lark-cli.exe\n"
            f"跑 `python tools/sync_vendor.py` 可从本机源自动搬（若已配好 vendor/sources.json）。"
        )
    return str(p)


def is_dlp_encrypted(path: Path) -> bool:
    """检查文件是否被 DLP 加密（读前 64 字节找 %TSD-Header）。"""
    try:
        with open(path, "rb") as f:
            return DLP_MARKER in f.read(64)
    except OSError:
        return False


# ---------------------------------------------------------------- HTML 正文


def build_html(date_str: str, tickets: List[Dict[str, Any]], failed: List[Dict[str, Any]],
               notes: List[str], skipped: int = 0) -> str:
    e = html.escape
    L: List[str] = []
    L.append('<div style="font-family:-apple-system,\'Segoe UI\',Roboto,\'Microsoft YaHei\',sans-serif;'
             'font-size:14px;color:#222;line-height:1.6">')
    L.append(f'<h2 style="margin:0 0 4px">工单分析日报 {e(date_str)}</h2>')
    L.append(
        f'<p style="margin:0 0 12px;color:#666">共 {len(tickets) + len(failed)} 单：'
        f'成功 <b style="color:#0a7"> {len(tickets)}</b> / '
        f'失败 <b style="color:#c00"> {len(failed)}</b>'
        + (f' ｜ 另有 {skipped} 单因数量上限未分析' if skipped else '')
        + '</p>'
    )

    if tickets:
        L.append('<table cellpadding="6" cellspacing="0" border="0" '
                 'style="border-collapse:collapse;border:1px solid #ddd;font-size:13px">')
        L.append('<tr style="background:#f5f5f5">'
                 '<th align="left">KEY</th><th align="left">标题</th>'
                 '<th align="left">工程归属</th><th align="left">工作量</th>'
                 '<th align="left">一句话结论</th><th align="left">待确认</th></tr>')
        for t in tickets:
            conf = t.get("confidence", "")
            conf_color = {"high": "#0a7", "medium": "#e80", "low": "#c00"}.get(conf, "#666")
            L.append(
                '<tr style="border-top:1px solid #eee">'
                f'<td><b>{e(t.get("key",""))}</b></td>'
                f'<td>{e(t.get("title",""))}</td>'
                f'<td>{e(t.get("project",""))} '
                f'<span style="color:{conf_color};font-size:12px">({e(conf)})</span></td>'
                f'<td align="center">{e(str(t.get("size","")))}</td>'
                f'<td>{e(t.get("conclusion",""))}</td>'
                f'<td align="center">{e(str(t.get("pending","")))}</td>'
                '</tr>'
            )
        L.append('</table>')

    if notes:
        L.append('<h3 style="margin:18px 0 6px;color:#c00">需要你留意</h3><ul style="margin:0;padding-left:20px">')
        for n in notes:
            L.append(f'<li>{e(n)}</li>')
        L.append('</ul>')

    if failed:
        L.append('<h3 style="margin:18px 0 6px;color:#c00">分析失败（下次运行会自动重试）</h3>'
                 '<ul style="margin:0;padding-left:20px">')
        for f in failed:
            L.append(f'<li><b>{e(f.get("key",""))}</b> — {e(f.get("error",""))}</li>')
        L.append('</ul>')

    L.append('<p style="margin:18px 0 0;color:#888;font-size:12px">'
             '附件为每单的分析报告与当日汇总（Markdown）。原始日志与代码快照留在工具工作区，'
             '报告末尾有路径。</p>')
    L.append('</div>')
    return "\n".join(L)


# ---------------------------------------------------------------- 发送


def send(subject: str, html_body: str, attach: List[str], cwd: Path,
         to: Optional[str] = None, cc: Optional[str] = None,
         dry_run: bool = False) -> Dict[str, Any]:
    """
    attach 必须是 **cwd 相对路径**。发信时 cd 到 cwd。
    dry_run=True 时只打印命令，不真发。
    """
    cli = lark_cli()
    to = to or env.cfg_get("email", "to")
    cc = cc if cc is not None else (env.cfg_get("email", "cc") or "")

    # 附件预检：DLP 加密过的不能发
    safe_attach: List[str] = []
    skipped: List[str] = []
    for a in attach:
        p = (cwd / a) if not Path(a).is_absolute() else Path(a)
        if not p.exists():
            skipped.append(f"{a}（不存在）")
            continue
        if is_dlp_encrypted(p):
            skipped.append(f"{a}（被 DLP 加密，跳过）")
            continue
        safe_attach.append(a if not Path(a).is_absolute() else str(p.relative_to(cwd)))

    cmd = [cli, "mail", "+send", "--as", "user", "--to", to]
    if cc:
        cmd += ["--cc", cc]
    cmd += ["--subject", subject, "--body", html_body]
    if safe_attach:
        cmd += ["--attach", ",".join(safe_attach)]
    cmd += ["--confirm-send"]

    if dry_run:
        return {"dry_run": True, "cwd": str(cwd), "cmd": cmd,
                "attach": safe_attach, "skipped": skipped}

    env.log(f"发送邮件到 {to} …")
    cp = env.run(cmd, cwd=str(cwd), timeout=600)

    out = (cp.stdout or "").strip()
    parsed: Any = None
    try:
        parsed = json.loads(out) if out.startswith(("{", "[")) else None
    except json.JSONDecodeError:
        parsed = None

    result: Dict[str, Any] = {
        "ok": cp.returncode == 0,
        "exit_code": cp.returncode,
        "stdout": out[:2000],
        "stderr": (cp.stderr or "")[:2000],
        "attach": safe_attach,
        "skipped": skipped,
    }
    if parsed:
        result["response"] = parsed

    # 只有 draft_id 没有 message_id → 补发草稿
    draft_id = None
    if isinstance(parsed, dict):
        draft_id = parsed.get("draft_id") or (parsed.get("data") or {}).get("draft_id")
        if parsed.get("message_id") or (parsed.get("data") or {}).get("message_id"):
            draft_id = None
    if draft_id:
        env.log(f"  只拿到 draft_id={draft_id}，补发草稿 …")
        cp2 = env.run(
            [cli, "mail", "user_mailbox.drafts", "send", "--yes",
             "--params", json.dumps({"user_mailbox_id": "me", "draft_id": str(draft_id)})],
            cwd=str(cwd), timeout=600,
        )
        result["draft_send"] = {
            "exit_code": cp2.returncode,
            "stdout": (cp2.stdout or "")[:1000],
            "stderr": (cp2.stderr or "")[:1000],
        }
        result["ok"] = result["ok"] and cp2.returncode == 0

    return result


def collect_receipts(date_dir: Path) -> Dict[str, Any]:
    """从各单目录收 receipt.json，组装邮件输入。"""
    tickets: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    notes: List[str] = []

    for d in sorted(p for p in date_dir.iterdir() if p.is_dir()):
        rp = d / "receipt.json"
        if not rp.exists():
            continue
        try:
            r = env.read_json(rp, {})
        except Exception as e:  # noqa: BLE001
            failed.append({"key": d.name, "error": f"回执解析失败: {e}"})
            continue
        if r.get("status") == "failed":
            failed.append({"key": r.get("key", d.name), "error": r.get("error", "未知")})
            continue
        tickets.append({
            "key": r.get("key", d.name),
            "title": r.get("title", ""),
            "project": r.get("project", ""),
            "confidence": r.get("confidence", ""),
            "size": r.get("size", ""),
            "conclusion": r.get("conclusion", ""),
            "pending": r.get("pending", 0),
        })
        flag = r.get("attention")
        if flag and flag != "无":
            notes.append(f"{r.get('key', d.name)}: {flag}")

    return {"tickets": tickets, "failed": failed, "notes": notes}


def send_daily(date_dir: Path, *, dry_run: bool = False) -> Dict[str, Any]:
    """发当日汇总邮件。cwd 切到 date_dir 以满足 --attach 相对路径要求。"""
    data = collect_receipts(date_dir)
    date_str = date_dir.name

    attach: List[str] = []
    for t in data["tickets"]:
        rel = f"{t['key']}/analysis.md"
        if (date_dir / rel).exists():
            attach.append(rel)
    if (date_dir / "SUMMARY.md").exists():
        attach.append("SUMMARY.md")

    subject = (f"[工单分析] {date_str} 共{len(data['tickets']) + len(data['failed'])}单"
               f"（成功{len(data['tickets'])}/失败{len(data['failed'])}）")
    body = build_html(date_str, data["tickets"], data["failed"], data["notes"])

    return send(subject, body, attach, cwd=date_dir, dry_run=dry_run)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="发工单分析汇总邮件")
    ap.add_argument("date_dir", help="产物日期目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令不发送")
    a = ap.parse_args()
    out = send_daily(Path(a.date_dir), dry_run=a.dry_run)
    print(json.dumps(out, ensure_ascii=False, indent=2)[:3000])
