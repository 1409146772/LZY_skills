#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
jla_attach —— 附件分类、解包、解析、生成证据摘要

职责边界：
  * 只负责"把附件变成 AI 能读的小体积证据"，不做任何结论推断。
  * 图片不解析（视觉模型直接 Read），只在清单里登记路径。
  * 视频跳过，只登记一行，不编造内容。

分类依据（扩展名 + 内容嗅探，两者取或）：
  图片      .png/.jpg/.jpeg/.gif/.bmp/.webp
  压缩包    .tar.gz/.tgz/.zip/.7z
  BLF       .blf
  DBC       .dbc
  5AA5 日志 .log/.curf/.txt/.log.N / 无扩展名且匹配 ^(main|sys|kernel|...)  / 内容含 5a a5
  视频      .mp4/.avi/.mov/.mkv   → 跳过
  其它      登记为 unknown

为什么要有 digest：
  frame_extractor 产出的 <name>_frames.jsonl 对真实的整车 log 可以到 10MB+，
  整读会冲爆 subagent 上下文。所以这里预先把 jsonl 降维成一份小的 digest.md
  （CMD 频次、异常帧、工单提到的时间点附近的窗口），jsonl 只留作定向 Grep。
"""

from __future__ import annotations

import json
import re
import tarfile
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jla_env as env

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
ARCHIVE_EXTS = {".gz", ".tgz", ".zip", ".7z", ".tar"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv"}
DBC_EXTS = {".dbc"}
BLF_EXTS = {".blf"}
LOG_EXTS = {".log", ".curf", ".txt", ".csv"}

# 无扩展名日志的命名约定（来自 AI_Log_parsing/README.md）
EXTENSIONLESS_LOG_RE = re.compile(
    r"^(main|sys|kernel|bsp|radio|events|apusys|crash|mblog)_log", re.IGNORECASE
)

# 5AA5 帧是**日志里的 hex 文本**，不是二进制字节（实测 raw b'\x5a\xa5' 在这些文件里为 0）。
#
# 真实形态（实测 main_log_1 偏移 59860 处）：
#     ... Write: size:9 ... propid:557862919 data:0x5a 0xa5 0x00 0x00 0x03 0x00 0x00 0x00 0xfd
#
# ⚠ 每个字节都带 `0x` 前缀！所以匹配必须允许 `5a` 与 `a5` 之间有 `0x`，
#   否则 `5a\s*a5` 这种朴素写法完全匹配不到 —— 这正是本模块最初的 bug。
#   此处刻意与 frame_extractor.py 第 55 行的正则保持一致，不要自行简化。
FRAME_TEXT_RE = re.compile(
    rb"(?:(?:0x|0X)?(?:5a|5A))\s+(?:(?:0x|0X)?(?:a5|A5))"
)
SNIFF_BYTES = 512 * 1024   # 每块 512KB
SNIFF_MAX_BLOCKS = 40      # 最多扫 20MB
# 优先嗅探这些文件名（实测帧主要在 main_log / dumplog/dumpsys_* 里）
PRIORITY_SNIFF_RE = re.compile(r"^(main_log|sys_log|events_log|dumpsys_)", re.IGNORECASE)


# ---------------------------------------------------------------- 分类


def sniff_5aa5(path: Path, max_blocks: int = SNIFF_MAX_BLOCKS) -> bool:
    """
    内容嗅探：文件里是否出现 `5a a5` 帧头（作为 hex 文本，非二进制字节）。

    分块扫描而不是只读头部：实测帧在 main_log 里可能出现在很靠后的位置，
    只读头 300KB 会漏判（曾导致 322 个日志 0 命中）。
    """
    try:
        if path.stat().st_size == 0:
            return False
        with open(path, "rb") as f:
            carry = b""
            for _ in range(max_blocks):
                chunk = f.read(SNIFF_BYTES)
                if not chunk:
                    break
                # 保留尾部 8 字节与下一块拼接，避免把跨块边界的匹配切断
                buf = carry + chunk
                if FRAME_TEXT_RE.search(buf):
                    return True
                carry = buf[-8:]
            return False
    except OSError:
        return False


def classify(path: Path) -> str:
    """
    返回类型标签：image / archive / blf / dbc / log5aa5 / video / unknown
    """
    name = path.name
    suffix = path.suffix.lower()
    lower = name.lower()

    if suffix in IMAGE_EXTS:
        return "image"
    if suffix in VIDEO_EXTS:
        return "video"
    if suffix in DBC_EXTS:
        return "dbc"
    if suffix in BLF_EXTS:
        return "blf"

    # 压缩包：.tar.gz / .tgz / .zip 等
    if lower.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")) or suffix in ARCHIVE_EXTS:
        return "archive"

    # 日志候选：扩展名命中，或无扩展名但命名符合约定
    is_log_name = suffix in LOG_EXTS or (not suffix and EXTENSIONLESS_LOG_RE.match(name))
    if suffix == ".log" or re.match(r".*\.log\.\d+$", lower):
        is_log_name = True
    # 无扩展名且不是已知命名约定的（如 main_log_1__2026_0814_153007）也当日志候选
    if not suffix and not is_log_name and name and not name.startswith("."):
        is_log_name = True

    if is_log_name:
        # 进一步确认是不是 5AA5 帧日志；不是也仍按日志登记（可能有其它价值）
        return "log5aa5" if sniff_5aa5(path) else "log_other"

    # 扩展名不认识时，最后用内容嗅探兜底
    if sniff_5aa5(path):
        return "log5aa5"

    return "unknown"


# ---------------------------------------------------------------- 解包


def _bucket(path: Path, origin: str, ticket_dir: Path, ev_dir: Path,
            result: Dict[str, Any]) -> None:
    """
    把单个文件归到对应的证据桶里（图片/视频/DBC/BLF/日志/未识别）。
    日志会就地触发解析。就地归类是为了让"解包产物"与"原始附件"走同一套逻辑，
    同时避免嵌套压缩包被重复处理。
    """
    kind = classify(path)
    try:
        rel = str(path.relative_to(ticket_dir))
    except ValueError:
        rel = str(path)

    if kind == "image":
        result["images"].append({"file": rel, "name": path.name, "origin": origin,
                                 "bytes": path.stat().st_size})
    elif kind == "video":
        result["video"].append({"file": rel, "name": path.name,
                                "bytes": path.stat().st_size})
    elif kind == "dbc":
        result["dbc"].append({"file": rel, "name": path.name, "origin": origin})
    elif kind == "blf":
        result["blf"].append({"file": rel, "name": path.name, "origin": origin})
    elif kind in ("log5aa5", "log_other"):
        result["logs"].append({"file": rel, "name": path.name, "kind": kind,
                               "bytes": path.stat().st_size, "origin": origin})
        # 解析不在这里做：同一目录下的 main_log 与 main_log_1 属于同一次会话，
        # 必须整目录一次解析才能正确按时间合并。见 parse_frame_groups()。
    else:
        result["unknown"].append({"file": rel, "name": path.name, "origin": origin})


def extract_archive(path: Path, dest: Path) -> List[Path]:
    """
    解包压缩包到 dest，返回解出的文件列表。逐项容错。

    流式解包（r|*）而不是先 getmembers()：单个日志包解出来可达 10GB+，
    逐个 member 走 tarfile 的提取路径可以边解边释放，避免把成员表全展开。
    """
    dest.mkdir(parents=True, exist_ok=True)
    out: List[Path] = []
    lower = path.name.lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                for info in z.infolist():
                    if info.filename.startswith(("/", "\\")) or ".." in Path(info.filename).parts:
                        continue
                    z.extract(info, dest)
        else:
            with tarfile.open(path, "r|*") as t:
                for m in t:
                    if m.name.startswith(("/", "\\")) or ".." in Path(m.name).parts:
                        continue
                    # 递归的 tar.gz 由上层 while 循环继续处理，这里只解一层
                    t.extract(m, dest)
        for p in dest.rglob("*"):
            if p.is_file():
                out.append(p)
    except Exception as e:  # noqa: BLE001
        env.log(f"  解包失败 {path.name}: {e}")
    return out


# ---------------------------------------------------------------- 解析器封装


def run_frame_extractor(target: Path, evidence_dir: Path, extra_args: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    调 AI_Log_parsing/frame_extractor.py。
    注意：必须经 env.run_python（统一 PYTHONUTF8=1），否则 cp936 下 argparse 崩溃、退出码 120。
    """
    script = env.cfg_path("paths", "frame_extractor")
    if not script or not script.exists():
        return {"ok": False, "error": f"frame_extractor 不存在: {script}"}
    args = [str(script), str(target), "-o", str(evidence_dir), "-q"]
    if extra_args:
        args.extend(extra_args)

    # 先记下已有的 *_frames.*，跑完只报**新增**的
    # （输出目录是所有解析共用的，直接 glob 会把别的组、甚至子目录父级的产物也算进来）
    before = {p.resolve() for p in evidence_dir.glob("*_frames.*")}

    cp = env.run_python(args, timeout=1800)
    ok = cp.returncode in (0, 3)  # 3 = 成功但 0 帧
    if not ok:
        return {
            "ok": False,
            "error": f"退出码 {cp.returncode}",
            "stderr": (cp.stderr or "")[-1500:],
        }
    after = [p for p in evidence_dir.glob("*_frames.*") if p.resolve() not in before]

    # 若本次没有新文件，说明是重跑且产物已存在（frame_extractor 不覆盖）。
    # 此时按输入目录名反推本次对应的产物，避免"产出 0 个"导致下游拿不到路径。
    if not after:
        stem = Path(target).name
        after = [p for p in evidence_dir.glob(f"{stem}_frames.*")]

    produced = sorted(str(p) for p in after)
    return {"ok": True, "exit_code": cp.returncode, "produced": produced}


def run_canlog(blf: Path, dbc: Optional[Path], evidence_dir: Path) -> Dict[str, Any]:
    """调 AI_canlog 的 canlog_tool 解析 BLF。需要 cwd 指向 AI_canlog 目录（-m 模块解析）。"""
    canlog_dir = env.cfg_path("paths", "canlog_dir")
    if not canlog_dir or not canlog_dir.exists():
        return {"ok": False, "error": f"AI_canlog 目录不存在: {canlog_dir}"}
    out_dir = evidence_dir / f"canlog_{blf.stem}"
    args = ["-m", "canlog_tool", "parse", str(blf), "--out", str(out_dir), "--quiet"]
    if dbc and Path(dbc).exists():
        args.extend(["--dbc", str(dbc)])
    cp = env.run_python(args, cwd=str(canlog_dir), timeout=1800)
    if cp.returncode != 0:
        return {
            "ok": False,
            "error": f"退出码 {cp.returncode}",
            "stderr": (cp.stderr or "")[-1500:],
        }
    return {"ok": True, "out_dir": str(out_dir), "dbc_used": str(dbc) if dbc else None}


# ---------------------------------------------------------------- digest


def build_frames_digest(
    jsonl: Path,
    out_md: Path,
    *,
    hint_times: Optional[List[str]] = None,
    top_n: int = 25,
    focus_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    把 *_frames.jsonl 降维成小的 digest.md：
      * CMD 频次 Top-N
      * 异常/ACK 异常帧统计
      * hint_times（工单里提到的时间，如 "15:30"）附近的帧窗口
      * focus_ids（工单里提到的 CMD/ID，如 3D5 / 583 / 0x2315）的**全部**出现记录

    focus_ids 是关键：工单往往点名了具体 ID（本单：监听 3D5 和 583），
    把这些 ID 的帧单独列出来，subagent 就不用去 Grep 那个 8MB 的 jsonl 了。
    jsonl 本身不整读，只留作定向 Grep。
    """
    if not jsonl.exists():
        return {"ok": False, "error": f"找不到 {jsonl}"}

    cmd_counter: Counter = Counter()
    dir_counter: Counter = Counter()
    ack_counter: Counter = Counter()
    total = 0
    per_cmd_example: Dict[str, Dict[str, Any]] = {}
    hint_hits: List[Dict[str, Any]] = []
    focus_hits: Dict[str, List[Dict[str, Any]]] = {}

    hint_pat = _build_hint_patterns(hint_times or [])
    focus_norm = {_norm_id(f) for f in (focus_ids or []) if f}

    with open(jsonl, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            cmd = str(rec.get("cmd", "?"))
            d = str(rec.get("dir", "?"))
            ack = str(rec.get("ack", "-"))
            cmd_counter[cmd] += 1
            dir_counter[d] += 1
            ack_counter[ack] += 1
            if cmd not in per_cmd_example:
                per_cmd_example[cmd] = rec

            if hint_pat:
                ts = str(rec.get("ts", ""))
                if ts and hint_pat.search(ts):
                    if len(hint_hits) < 200:  # 上限，防爆
                        hint_hits.append(rec)

            if focus_norm and _norm_id(cmd) in focus_norm:
                bucket = focus_hits.setdefault(cmd, [])
                if len(bucket) < 100:  # 上限，防爆
                    bucket.append(rec)

    L: List[str] = []
    L.append(f"# 帧日志摘要（digest）")
    L.append("")
    L.append(f"> 源: `{jsonl.name}` | 总帧数: **{total}** | 生成: {datetime.now():%Y-%m-%d %H:%M}")
    L.append(f"> 原始 jsonl 体积较大，**不要整读**；需要细节请用 Grep 定向查 `{jsonl.name}`。")
    L.append("")

    L.append("## 方向分布")
    L.append("")
    for k, v in dir_counter.most_common():
        L.append(f"- {k}: {v}")
    L.append("")

    L.append(f"## CMD 频次 Top {top_n}")
    L.append("")
    L.append("| CMD | 次数 | 占比 | 示例(frame) |")
    L.append("|---|---|---|---|")
    for cmd, cnt in cmd_counter.most_common(top_n):
        pct = f"{cnt / total * 100:.1f}%" if total else "-"
        ex = per_cmd_example.get(cmd, {})
        param = str(ex.get("param_hex", ""))[:48]
        ex_s = f"ts={ex.get('ts', '-')} dir={ex.get('dir', '-')} len={ex.get('length', '-')} param={param}"
        L.append(f"| {cmd} | {cnt} | {pct} | {ex_s} |")
    L.append("")

    L.append("## ACK / 异常统计")
    L.append("")
    for k, v in ack_counter.most_common():
        L.append(f"- ack={k}: {v}")
    L.append("")

    if focus_hits:
        L.append("## 工单点名的 ID —— 全部出现记录")
        L.append("")
        L.append("> 工单里明确提到的报文/命令 ID，**这是最直接的证据**，逐条看。")
        L.append("")
        for cmd, recs in sorted(focus_hits.items(), key=lambda kv: -len(kv[1])):
            L.append(f"### `{cmd}`（{len(recs)} 帧{'+' if len(recs) >= 100 else ''}）")
            L.append("")
            L.append("| ts | dir | mode | ack | length | param_hex | file |")
            L.append("|---|---|---|---|---|---|---|")
            for r in recs[:60]:
                L.append(
                    f"| {r.get('ts','-')} | {r.get('dir','-')} | {r.get('mode','-')} "
                    f"| {r.get('ack','-')} | {r.get('length','-')} "
                    f"| {str(r.get('param_hex',''))[:70]} | {r.get('file','-')} |"
                )
            if len(recs) > 60:
                L.append(f"| … | | | | | 其余 {len(recs)-60} 帧见 jsonl | |")
            L.append("")
    elif focus_norm:
        L.append("## 工单点名的 ID —— 全部出现记录")
        L.append("")
        L.append(f"⚠ 工单提到 {sorted(focus_norm)}，但**本日志里一帧都没有**。")
        L.append("这本身就是重要证据（工单说的报文在该时段没出现，或 ID 归属的是另一个通道）。")
        L.append("")

    if hint_pat:
        L.append("## 工单时间点附近窗口")
        L.append("")
        L.append(f"> 命中工单提到的时间（{', '.join(hint_times or [])}），共 {len(hint_hits)} 帧")
        L.append("")
        if hint_hits:
            L.append("| ts | dir | cmd | mode | ack | length | param_hex |")
            L.append("|---|---|---|---|---|---|---|")
            for r in hint_hits[:60]:
                L.append(
                    f"| {r.get('ts','-')} | {r.get('dir','-')} | {r.get('cmd','-')} "
                    f"| {r.get('mode','-')} | {r.get('ack','-')} | {r.get('length','-')} "
                    f"| {str(r.get('param_hex',''))[:60]} |"
                )
            if len(hint_hits) > 60:
                L.append(f"| … | | | | | | 其余 {len(hint_hits)-60} 帧见 jsonl |")
        else:
            L.append("（该时间窗口内没有解析出帧——可能时间格式不匹配，或该时段无通信）")
        L.append("")

    env.write_text(out_md, "\n".join(L))
    return {
        "ok": True,
        "digest": str(out_md),
        "total_frames": total,
        "cmd_top": cmd_counter.most_common(top_n),
        "hint_hits": len(hint_hits),
    }


def _norm_id(s: str) -> str:
    """把 '0x3D5' / '3D5' / '0x03d5' 统一成 '0x3d5' 便于比较。"""
    t = str(s).strip().lower()
    if t.startswith("0x"):
        t = t[2:]
    t = t.lstrip("0") or "0"
    return f"0x{t}"


# 工单文本里常见的 ID 写法：0x3D5 / 3D5（三到四位十六进制）
#
# ⚠ 两个实测踩过的坑，都不能用 \b：
#   1. \b 在 CJK 与数字之间不成立（中文是 \w）→ "监听了3D5和583" 里的 3D5/583 匹配不到。
#   2. 去掉 \b 后又会把时间戳/文件名里的段落（"…15-44-34-916"）当 ID。
#   解法：用后顾排除 `-` 与 `:`（时间戳/文件名分隔符）和 hex 字符，
#         并先剥掉 wiki 附件引用与文件名。
ID_HINT_RE = re.compile(r"(?<![0-9A-Fa-f:\-])([0-9A-Fa-f]{3,4})(?![0-9A-Fa-f])")
PREFIXED_ID_RE = re.compile(r"0x([0-9A-Fa-f]{1,4})(?![0-9A-Fa-f])")
# 形如 xxx.png / xxx.log / xxx.tar.gz 的 token，先剥掉再抽 ID
FILENAME_TOKEN_RE = re.compile(r"\S+\.(?:png|jpg|jpeg|gif|bmp|webp|log|txt|blf|dbc|gz|zip|csv|xlsx|docx|pdf)", re.I)
# 形如 2026-09-10 / 15:44:34 的时间戳
TIMESTAMP_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}:\d{2}(?::\d{2})?")


def extract_id_hints(*texts: str) -> List[str]:
    """
    从工单描述/评论里抽 CMD/CAN ID 线索（如 "监听了3D5和583"、"0x2315"）。

    只保留看起来像 ID 的（3-4 位十六进制，或 0x 前缀）。
    先剥掉文件名与时间戳，避免把 image-…-916.png / 15:46:13-599 里的数字当 ID。
    """
    out: List[str] = []
    seen = set()
    for t in texts:
        if not t:
            continue
        s = FILENAME_TOKEN_RE.sub(" ", t)
        s = TIMESTAMP_RE.sub(" ", s)

        cands: List[str] = []
        for m in PREFIXED_ID_RE.finditer(s):
            cands.append(m.group(1))
        # 无前缀的候选：再作为兜底补上（3D5 / 583 这类写法没有 0x）
        for m in ID_HINT_RE.finditer(s):
            raw = m.group(1)
            # 4 位纯十进制且落在年份区间 → 当噪声丢掉
            if len(raw) == 4 and raw.isdigit() and 1900 <= int(raw) <= 2100:
                continue
            cands.append(raw)

        for raw in cands:
            n = _norm_id(raw)
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def _build_hint_patterns(hint_times: List[str]) -> Optional[re.Pattern]:
    """把 ['15:30', '15:30:12'] 之类编译成匹配 ts 的正则。"""
    pats = []
    for h in hint_times:
        h = h.strip()
        if not h:
            continue
        # 转义后允许分隔符柔性
        esc = re.escape(h).replace(r"\:", "[:.]")
        pats.append(esc)
    if not pats:
        return None
    return re.compile("|".join(pats))


# ⚠ 不能用 \b：中文字符在 Python 正则里算 \w，所以 "时间15:30" 里
#   中文与数字之间**没有**单词边界，\b15:30\b 匹配不到（实测踩过）。
#   改用前后瞻排除数字/冒号。
TIME_HINT_RE = re.compile(
    r"(?<![0-9:])([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?![0-9:])"
)


def extract_time_hints(*texts: str) -> List[str]:
    """从工单描述/评论里抽 'log时间15:30' 这类时间提示，供 digest 定位窗口。"""
    found: List[str] = []
    for t in texts:
        for m in TIME_HINT_RE.finditer(t or ""):
            hh, mm, ss = m.group(1), m.group(2), m.group(3)
            if ss:
                found.append(f"{int(hh):02d}:{mm}:{ss}")
            else:
                found.append(f"{int(hh):02d}:{mm}")
    # 去重保序
    seen = set()
    out = []
    for x in found:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------- 主流程


def process_ticket(ticket_dir: Path, *, dbc: Optional[Path] = None) -> Dict[str, Any]:
    """
    处理单票的 attachments/，产出 evidence/。
    返回结构化清单，供 bundle 使用。
    """
    att_dir = ticket_dir / "attachments"
    ev_dir = ticket_dir / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {
        "images": [],
        "archives": [],
        "logs": [],
        "blf": [],
        "dbc": [],
        "video": [],
        "unknown": [],
        "parsers": [],
        "notes": [],
    }

    if not att_dir.exists():
        result["notes"].append("没有 attachments/ 目录（该单无附件）")
        env.write_json(ev_dir / "evidence.json", result)
        return result

    # 1) 收集所有待分类文件（含解包后的）
    pending: List[Tuple[Path, str]] = []  # (path, 来源说明)
    for p in sorted(att_dir.rglob("*")):
        if p.is_file():
            pending.append((p, "原始附件"))

    # 先处理压缩包，把解出的内容加进 pending 再统一分类。
    # processed 与 pending 一样存 (Path, 来源) 元组 —— 解包可能产生嵌套压缩包，
    # 会在 while 循环里继续被解，所以用索引式遍历而不是 for。
    processed: List[Tuple[Path, str]] = []
    idx = 0
    while idx < len(pending):
        path, origin = pending[idx]
        idx += 1
        kind = classify(path)
        if kind == "archive":
            sub = ev_dir / f"unpacked_{path.stem}"
            env.log(f"  解包 {path.name} → {sub.name}")
            extracted = extract_archive(path, sub)
            result["archives"].append({"file": path.name, "extracted": len(extracted)})
            for e in extracted:
                pending.append((e, f"来自 {path.name}"))
                # 解出来的内容在这里就地分类，不再进 processed（避免与嵌套解包重复）
                _bucket(path=e, origin=f"来自 {path.name}", ticket_dir=ticket_dir,
                        ev_dir=ev_dir, result=result)
        else:
            processed.append((path, origin))

    # 2) 统一分类处理（原始附件与解包产物都在 processed 里）
    for path, origin in processed:
        _bucket(path=path, origin=origin, ticket_dir=ticket_dir,
                ev_dir=ev_dir, result=result)

    # 2.5) 帧日志按**目录**成组解析
    # 一次事件的日志被拆成 main_log / main_log_1 / sys_log 等同目录多文件；
    # frame_extractor 支持目录输入并会跨文件按时间合并，所以整目录跑一次。
    parse_frame_groups(ticket_dir, ev_dir, result)

    # 3) BLF 解析：DBC 优先用 inputs.json 里指定的那份；没给才退回本单附件自带的
    effective_dbc = dbc
    if effective_dbc is None:
        try:
            effective_dbc = env.input_path("dbc", required=False)
        except (FileNotFoundError, ValueError):
            effective_dbc = None
    if effective_dbc is None and result["dbc"]:
        effective_dbc = ticket_dir / result["dbc"][0]["file"]
        result["notes"].append(
            f"未提供 DBC，退回使用本单附件自带的 {result['dbc'][0]['file']}")
    if result["blf"] and effective_dbc is None:
        result["notes"].append(
            "未提供 DBC 且附件里也没有 .dbc —— CAN 报文只能按裸 ID 解析，"
            "信号名/物理值不可用；若工单涉及报文含义，报告里要写明这一限制")
        env.log("  ⚠ 未提供 DBC，BLF 将按裸 ID 解析")
    for b in result["blf"]:
        env.log(f"  解析 BLF {b['name']} …")
        pr = run_canlog(ticket_dir / b["file"], effective_dbc, ev_dir)
        pr["source"] = b["file"]
        result["parsers"].append({"tool": "canlog_tool", **pr})

    if result["video"]:
        result["notes"].append(
            f"存在 {len(result['video'])} 个视频附件，按既定策略**未分析**"
            f"（{', '.join(v['name'] for v in result['video'])}）"
        )
    if result["unknown"]:
        result["notes"].append(
            f"{len(result['unknown'])} 个附件类型未识别，未解析："
            + ", ".join(u["name"] for u in result["unknown"][:10])
        )

    env.write_json(ev_dir / "evidence.json", result)
    return result


def parse_frame_groups(ticket_dir: Path, ev_dir: Path, result: Dict[str, Any]) -> None:
    """
    把含 5AA5 帧的日志**按所在目录成组**，每组跑一次 frame_extractor。

    为什么按目录而不是按文件：同一次抓取的日志被拆成 main_log / main_log_1 /
    sys_log / events_log 等同目录多文件，帧会跨文件分布。frame_extractor 支持
    目录输入并做跨文件时间合并，整目录跑一次才能得到完整的时间线；
    逐文件跑会把一次会话切碎（实测该单 main_log_1 有 20345 帧、main_log.curf 有 323 帧，
    分属同一次会话）。
    """
    frame_logs = [g for g in result["logs"] if g.get("kind") == "log5aa5"]
    if not frame_logs:
        return

    groups: Dict[str, Dict[str, Any]] = {}
    for g in frame_logs:
        p = ticket_dir / g["file"]
        d = str(p.parent)
        grp = groups.setdefault(d, {"dir": d, "members": [],
                                    "bytes": 0, "frames_sum": 0})
        grp["members"].append(g["name"])
        grp["bytes"] += g.get("bytes", 0)

    for d, grp in groups.items():
        env.log(f"  解析帧日志目录 {Path(d).name}（{len(grp['members'])} 个文件, "
                f"{grp['bytes']/1e6:.1f}MB）…")
        pr = run_frame_extractor(Path(d), ev_dir)
        pr["source"] = str(Path(d).relative_to(ticket_dir))
        pr["members"] = grp["members"]
        result["parsers"].append({"tool": "frame_extractor", **pr})
        if pr.get("ok"):
            # 统计帧数，供日志里汇报
            for f in ev_dir.glob("*_frames.jsonl"):
                try:
                    with open(f, "r", encoding="utf-8", errors="replace") as fh:
                        n = sum(1 for _ in fh)
                    grp["frames_sum"] = max(grp["frames_sum"], n)
                except OSError:
                    pass
            env.log(f"    → 产出 {len(pr.get('produced', []))} 个文件")
        else:
            env.log(f"    ✗ 失败：{pr.get('error','')}")


def build_evidence_index(ticket_dir: Path, evidence: Dict[str, Any],
                         hint_times: Optional[List[str]] = None) -> Path:
    """
    生成 evidence/INDEX.md —— 给 subagent 的"证据索引"：
    告诉它有哪些文件、该读什么、不该整读什么。
    """
    ev_dir = ticket_dir / "evidence"
    L: List[str] = ["# 证据索引", ""]

    L.append("## 图片（用 Read 工具直接看，视觉可读文字）")
    L.append("")
    if evidence["images"]:
        for i in evidence["images"]:
            L.append(f"- `{ticket_dir.name}/{i['file']}` — {i['bytes']:,} B — {i['origin']}")
    else:
        L.append("（无）")
    L.append("")

    L.append("## 日志证据")
    L.append("")
    if evidence["logs"]:
        for g in evidence["logs"]:
            L.append(f"- `{g['file']}` [{g['kind']}] {g['bytes']:,} B — {g['origin']}")
    else:
        L.append("（无）")
    L.append("")

    L.append("## 解析产物")
    L.append("")
    for p in evidence["parsers"]:
        if p.get("ok"):
            L.append(f"- **{p['tool']}** ← `{p.get('source','?')}`：成功")
            for f in p.get("produced", []) or []:
                L.append(f"    - `{f}`")
            if p.get("out_dir"):
                L.append(f"    - 输出目录 `{p['out_dir']}`")
        else:
            L.append(f"- **{p['tool']}** ← `{p.get('source','?')}`：失败 — {p.get('error','')}")
    L.append("")

    # 把 digest 找出来特别提示
    digests = sorted(ev_dir.glob("*_frames_digest.md")) + sorted(ev_dir.glob("*digest*.md"))
    if digests:
        L.append("### ⚠ 阅读顺序（防上下文爆炸）")
        L.append("")
        L.append("1. **先读 digest**（小）：")
        for d in dict.fromkeys(digests):
            L.append(f"   - `{d.relative_to(ticket_dir)}`")
        L.append("2. **再读 `_frames.md`**（中等，约几十 KB）")
        L.append("3. **`_frames.jsonl` 绝对不要整读**（可达 10MB+）—— 需要细节时用 Grep 定向搜，")
        L.append("   例如搜某个 CMD 或时间段，只取命中行。")
        L.append("")

    if evidence["video"]:
        L.append("## 视频（未分析）")
        L.append("")
        for v in evidence["video"]:
            L.append(f"- `{v['name']}` {v['bytes']:,} B — 按既定策略跳过，**不要推测其内容**")
        L.append("")

    if evidence["notes"]:
        L.append("## 备注")
        L.append("")
        for n in evidence["notes"]:
            L.append(f"- {n}")
        L.append("")

    if hint_times:
        L.append("## 工单时间线索")
        L.append("")
        L.append(f"从工单文本里抽到的时间点：{', '.join(hint_times)}")
        L.append("digest 里已按这些时间做了窗口提取，优先看那一段。")
        L.append("")

    out = ev_dir / "INDEX.md"
    env.write_text(out, "\n".join(L))
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="附件分类与解析")
    ap.add_argument("ticket_dir", help="工单 staging 目录")
    ap.add_argument("--dbc", help="指定 DBC 路径")
    a = ap.parse_args()
    td = Path(a.ticket_dir)
    ev = process_ticket(td, dbc=Path(a.dbc) if a.dbc else None)
    build_evidence_index(td, ev)
    env.log("完成")
