#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
frame_extractor.py — SOC↔MCU 数据帧专用提取工具

只做一件事: 从车机日志中找出 5a a5 数据帧（帧提取，不做信号解析、不读 Excel 规则）,
并输出成 AI 友好的文件:
  * <输入名>_frames.md    — Markdown 报告（统计摘要 + 按 CMD 分组的帧列表）
  * <输入名>_frames.jsonl — 每帧一行 JSON（含方向/counter/propid 等完整元数据）

帧格式:
    [0-1]  [2]   [3-4]   [5]    [6-7]   [8..n]     [last]
    SYNC   ACK   LEN     MODE   CMD     PARAM      CHECKSUM
    5a a5        大端 (b[3]<<8)|b[4]
    LEN = MODE+CMD+PARAM; param_len = LEN-3; full_frame_len = LEN + 6
    Checksum = (~sum(ACK..PARAM) + 1) & 0xFF

用法:
    python frame_extractor.py <日志文件|目录|tar.gz> [-o 输出目录] [选项]

纯标准库实现，无第三方依赖。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Sequence, Tuple

__version__ = "1.0.2"
TOOL_NAME = "frame_extractor.py"

# ---------------------------------------------------------------------------
# 常量与正则
# ---------------------------------------------------------------------------

SYNC = b"\x5a\xa5"
HDR_LEN = 8                       # SYNC(2)+ACK(1)+LEN(2)+MODE(1)+CMD(2)

LOG_EXTS = (".log", ".curf", ".txt")
TAR_EXTS = (".tar.gz", ".tgz")
ROTATED_RE = re.compile(r"\.log\.\d+$")                 # app.log.1 轮转日志
KNOWN_LOG_STEM_RE = re.compile(                          # 无扩展名的已知日志前缀
    r"^(?:main|sys|kernel|bsp|radio|events|apusys|crash|mblog)_log", re.I)

# 完整数据帧: 5a a5 后跟 >=1 个 hex 字节（支持 0x 前缀，字节间需有空白）
FRAME_RE = re.compile(
    r"((?:(?:0x|0X)?(?:5a|5A))\s+(?:(?:0x|0X)?(?:a5|A5))(?:\s+(?:0x|0X)?[0-9a-fA-F]{2})+)")
# 旧设备格式: "CMD: 0x.... PARAM: ..."（仅作为跨行合并的补充来源）
LEGACY_CMD_RE = re.compile(r"CMD:\s*(0x[0-9a-fA-F]+)\s+PARAM:\s*([0-9a-fA-F\s]+)")
# 时间戳: Syslog 格式 (Feb 10 11:07:03) 或 Android/Kernel 格式 (01-01 08:00:11.041711)
TIME_RE = re.compile(r"(\w+\s+\d+\s+\d+:\d+:\d+|\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)")
ANDROID_TS_RE = re.compile(r"^(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?$")
SYSLOG_TS_RE = re.compile(r"^([A-Za-z]+)\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})$")
MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}

# 行内 key:value 元数据（宽容扫描 + 白名单过滤；时间戳等误匹配会被白名单排除）
META_RE = re.compile(r"(\w+):(?:\s*)(0x[0-9a-fA-F]+|\d+)")
META_INT_KEYS = ("size", "counter", "propid", "iReadLen", "isSuccess",
                 "isAckAnswer", "isAckReq", "retryCount", "map")
META_HEX_KEYS = ("need_ack", "id", "sub_id", "can_ack", "ack_status")
META_KEYS_ORDER = META_INT_KEYS + META_HEX_KEYS
META_KEYS = set(META_KEYS_ORDER)

# 方向: WriteThread = SOC→MCU (tx); ProcessThread = MCU→SOC (rx)
DIR_TX, DIR_RX, DIR_UNK = "tx", "rx", "?"
TAG_DIRS = (("cviautodatahubwritethread", DIR_TX),
            ("cviautodatahubprocessthread", DIR_RX))
VERB_TX = " write:"
VERB_RX = " onprocess:"

PURE_HEX_RE = re.compile(r"[0-9a-fA-F\s]+")

FLAG_ZH = {
    "short": "缺校验位",
    "merged_hex": "跨行合并",
    "merged_legacy": "跨行合并",
    "truncated_in_log": "日志截断",
    "extra_bytes": "帧尾多余字节",
}

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class Frame:
    """单条数据帧记录（ts_key 仅用于统计排序，不写入 JSONL）"""
    ts: str
    ts_key: Optional[tuple]
    direction: str                 # 'tx' | 'rx' | '?'
    cmd: str                       # 小写 '0x2315'
    mode: int
    ack: int
    length: int                    # LEN 字段值 = MODE+CMD+PARAM
    param_len: int
    param_hex: str                 # 连续无空格小写 hex（省 token）
    checksum: Optional[int]        # 日志中的校验位；缺失为 None
    checksum_calc: Optional[int]
    checksum_ok: Optional[bool]    # 缺失时为 None
    checksum_origin: str           # 'original' | 'missing'
    meta: dict                     # 白名单元数据（行内缺失的键不出现）
    file: str                      # 相对扫描根的路径，'/' 分隔
    line_no: int                   # 1-based
    flags: List[str] = field(default_factory=list)
    raw: Optional[str] = None      # 原始日志行（仅 --include-raw）


def new_stats() -> dict:
    return {
        "files_candidates": 0, "files_scanned": 0, "files_failed": 0,
        "files_with_frames": 0, "total_bytes": 0, "lines_scanned": 0,
        "candidates": 0, "frames": 0, "rejected_short": 0,
        "checksum_ok": 0, "checksum_bad": 0, "checksum_missing": 0,
        "merged_cross_line": 0, "truncated_in_log": 0,
    }

# ---------------------------------------------------------------------------
# 纯解析函数
# ---------------------------------------------------------------------------


def normalize_hex(text: str) -> bytes:
    """'0x5A 0xa5 ...' -> 原始字节（去 0x 前缀、压空白）。失败抛 ValueError。"""
    joined = "".join(text.split()).replace("0x", "").replace("0X", "")
    return bytes.fromhex(joined)


def extract_timestamp(line: str) -> Tuple[str, Optional[tuple]]:
    """返回 (原始时间戳字符串, 可排序元组)。不伪造年份（保持日志原样）。"""
    m = TIME_RE.search(line)
    if not m:
        return "", None
    ts = m.group(1)
    a = ANDROID_TS_RE.match(ts)
    if a:
        return ts, (int(a.group(1)), int(a.group(2)), int(a.group(3)),
                    int(a.group(4)), int(a.group(5)), int(a.group(6) or 0))
    s = SYSLOG_TS_RE.match(ts)
    if s:
        mon = MONTHS.get(s.group(1).lower()[:3])
        if mon:
            return ts, (mon, int(s.group(2)), int(s.group(3)),
                        int(s.group(4)), int(s.group(5)), 0)
    return ts, None


def detect_direction(line: str) -> str:
    """按日志 tag 判断帧方向; 未知返回 '?'。"""
    low = line.lower()
    for tag, d in TAG_DIRS:
        if tag in low:
            return d
    if VERB_TX in low:
        return DIR_TX
    if VERB_RX in low:
        return DIR_RX
    return DIR_UNK


def parse_meta(line: str) -> dict:
    """提取白名单内的 key:value 元数据。十进制 -> int, 0x 值 -> 小写字符串。"""
    meta = {}
    for k, v in META_RE.findall(line):
        if k not in META_KEYS:
            continue
        if v[:2].lower() == "0x":
            meta[k] = v.lower()
        else:
            try:
                meta[k] = int(v)
            except ValueError:
                continue
    return meta


def frame_layout(fb: bytearray) -> Optional[Tuple[int, int, int, str]]:
    """轻量帧头校验。返回 (frame_len, param_len, full_frame_len, cmd_str) 或 None。"""
    if len(fb) < HDR_LEN or fb[0] != 0x5A or fb[1] != 0xA5:
        return None
    frame_len = (fb[3] << 8) | fb[4]
    if frame_len < 3:                     # LEN 至少为 MODE+CMD
        return None
    param_len = frame_len - 3
    return frame_len, param_len, HDR_LEN + param_len + 1, f"0x{fb[6]:02x}{fb[7]:02x}"


def parse_frame_bytes(fb: bytearray) -> Optional[dict]:
    """按帧布局解析（在跨行合并之后调用）。

    返回 cmd/mode/ack/length/param_len/param_hex/checksum/checksum_calc/
    checksum_ok/checksum_origin/flags/full_frame_len；帧头非法返回 None。
    长度超出 full_frame_len 时截断并标记 extra_bytes。
    """
    layout = frame_layout(fb)
    if layout is None:
        return None
    frame_len, param_len, full, cmd = layout
    flags: List[str] = []
    if len(fb) > full:
        flags.append("extra_bytes")
    actual = bytes(fb[:full])
    checksum = actual[full - 1] if len(actual) >= full else None
    if checksum is None:
        flags.append("short")
    cs_calc = (~sum(actual[2:HDR_LEN + param_len]) + 1) & 0xFF
    return {
        "cmd": cmd,
        "mode": actual[5],
        "ack": actual[2],
        "length": frame_len,
        "param_len": param_len,
        "param_hex": actual[HDR_LEN:HDR_LEN + param_len].hex(),
        "checksum": checksum,
        "checksum_calc": cs_calc,
        "checksum_ok": None if checksum is None else checksum == cs_calc,
        "checksum_origin": "original" if checksum is not None else "missing",
        "flags": flags,
        "full_frame_len": full,
    }


class _LineFeeder:
    """支持 peek 的行迭代器（跨行合并需要先看后吃）"""

    def __init__(self, it: Iterator[Tuple[int, str]]):
        self._it = it
        self._buf: Optional[Tuple[int, str]] = None

    def peek(self) -> Optional[Tuple[int, str]]:
        if self._buf is None:
            self._buf = next(self._it, None)
        return self._buf

    def pop(self) -> Optional[Tuple[int, str]]:
        item = self.peek()
        self._buf = None
        return item


def merge_cross_line(feeder: _LineFeeder, fb: bytearray, cmd: str,
                     full_frame_len: int) -> List[str]:
    """跨行合并（长帧被日志换行截断时补齐数据；迭代器驱动、O(1) 内存）。

    ① 下一行若是 "CMD: 0x<cmd> PARAM: ..." 则拼接其 PARAM;
    ② 之后持续拼接纯 HEX 行，直到数据补齐 / 遇到新 5a a5 帧头 / 时间戳 / 非 HEX 行。
    """
    flags: List[str] = []
    item = feeder.peek()
    if item is not None:
        nxt = item[1].strip()
        m = LEGACY_CMD_RE.search(nxt)
        if m and m.group(1).lower() == cmd.lower():
            try:
                fb.extend(normalize_hex(m.group(2)))
            except ValueError:
                m = None
            else:
                feeder.pop()
                flags.append("merged_legacy")
    while len(fb) < full_frame_len:
        item = feeder.peek()
        if item is None:
            break
        nxt = item[1].strip()
        if not nxt:
            break
        low = nxt.lower()
        if ("5a" in low and "a5" in low) or TIME_RE.search(nxt):
            break
        if not PURE_HEX_RE.fullmatch(nxt):
            break
        try:
            extra = normalize_hex(nxt)
        except ValueError:
            break
        feeder.pop()
        fb.extend(extra)
        if "merged_hex" not in flags:
            flags.append("merged_hex")
    return flags

# ---------------------------------------------------------------------------
# 文件发现与提取
# ---------------------------------------------------------------------------


def _is_log_file(name: str, include_extensionless: bool, extra_ext: Sequence[str]) -> bool:
    low = name.lower()
    if low.endswith(LOG_EXTS):
        return True
    if ROTATED_RE.search(low):
        return True
    if low.startswith("logs.log"):
        return True
    if extra_ext and low.endswith(tuple(e.lower() for e in extra_ext)):
        return True
    # 无扩展名但命中已知日志前缀（如 94MB 的 main_log_1__2026_0814_153007）
    if include_extensionless and "." not in name and KNOWN_LOG_STEM_RE.match(name):
        return True
    return False


def discover_files(path: str, include_extensionless: bool = True,
                   extra_ext: Sequence[str] = ()) -> Tuple[str, List[str]]:
    """归一化输入路径，返回 (扫描根, [绝对文件路径])。"""
    abspath = os.path.abspath(path)
    if os.path.isfile(abspath):
        return os.path.dirname(abspath), [abspath]
    files = []
    for root, _dirs, names in os.walk(abspath):
        for name in names:
            if _is_log_file(name, include_extensionless, extra_ext):
                files.append(os.path.join(root, name))
    files.sort()
    return abspath, files


def _tar_stem(path: str) -> str:
    base = os.path.basename(path)
    if base.lower().endswith(".tar.gz"):
        return base[:-7]
    if base.lower().endswith(".tgz"):
        return base[:-4]
    return os.path.splitext(base)[0]


def maybe_extract(path: str) -> Optional[str]:
    """解压 tar.gz/tar 到同名目录。

    成功解压返回目录路径；同名目录已存在时告警跳过并返回 None
    （避免与已解压目录重复扫描），调用方以此判断本轮是否有新解压。
    """
    extract_dir = os.path.join(os.path.dirname(os.path.abspath(path)), _tar_stem(path))
    if os.path.isdir(extract_dir):
        warn(f"目录已存在，跳过解压（注意可能与已解压内容重复扫描）: {extract_dir}")
        return None
    os.makedirs(extract_dir, exist_ok=True)
    with tarfile.open(path, "r:gz") as tar:
        try:
            tar.extractall(extract_dir, filter="data")
        except TypeError:                     # Python < 3.12 无 filter 参数
            tar.extractall(extract_dir)
    log_msg(f"已解压: {path} -> {extract_dir}")
    return extract_dir


def extract_frames_from_file(path: str, rel: str, stats: dict, *,
                             merge: bool = True, include_raw: bool = False,
                             encoding: str = "utf-8",
                             progress_every: int = 0) -> List[Frame]:
    """流式扫描单个日志文件（绝不 readlines），提取全部数据帧。"""
    frames: List[Frame] = []
    fh = open(path, "r", encoding=encoding, errors="replace")
    feeder = _LineFeeder(enumerate(fh, 1))
    try:
        while True:
            item = feeder.pop()
            if item is None:
                break
            line_no, raw_line = item
            line = raw_line.strip()
            if not line:
                continue
            stats["lines_scanned"] += 1
            low = line.lower()
            if "5a" not in low or "a5" not in low:      # 廉价预过滤
                continue
            matches = list(FRAME_RE.finditer(line))
            if not matches:
                continue
            stats["candidates"] += 1

            # 每行只解析一次元数据（同一行多帧共享）
            direction = detect_direction(line)
            meta = parse_meta(line)
            ts_raw, ts_key = extract_timestamp(line)

            for mi, m in enumerate(matches):
                try:
                    fb = bytearray(normalize_hex(m.group(1)))
                except ValueError:
                    continue
                is_last = (mi == len(matches) - 1)
                rest_after = line[m.end():].lstrip()
                truncated_by_log = rest_after.startswith("...") or rest_after.startswith("…")

                # 一行内可能拼接多帧: 从帧头起逐帧切分（贪婪正则只会产生一个 match）
                first_in_chain = True
                while True:
                    layout = frame_layout(fb)
                    if layout is None:
                        # 帧头正确但连完整头部(8B)都不到 → 计入过短丢弃
                        if len(fb) >= 2 and fb[0] == 0x5A and fb[1] == 0xA5:
                            stats["rejected_short"] += 1
                        break
                    _frame_len, _param_len, full, _cmd = layout
                    flags: List[str] = []

                    # 数据不足: 检查日志自身省略号截断 + （仅行末匹配）跨行合并
                    if len(fb) < full:
                        if truncated_by_log:
                            flags.append("truncated_in_log")
                            stats["truncated_in_log"] += 1
                            truncated_by_log = False
                        if merge and is_last and first_in_chain:
                            mflags = merge_cross_line(feeder, fb, _cmd, full)
                            if mflags:
                                flags.extend(mflags)
                                stats["merged_cross_line"] += 1

                    # 允许缺 1 字节校验位（帧尾校验位缺失时仍接受）
                    if len(fb) < full - 1:
                        stats["rejected_short"] += 1
                        break

                    parsed = parse_frame_bytes(fb)
                    if parsed is None:
                        break
                    flags.extend(parsed["flags"])

                    ok = parsed["checksum_ok"]
                    if ok is True:
                        stats["checksum_ok"] += 1
                    elif ok is False:
                        stats["checksum_bad"] += 1
                    else:
                        stats["checksum_missing"] += 1

                    frames.append(Frame(
                        ts=ts_raw, ts_key=ts_key, direction=direction,
                        cmd=parsed["cmd"], mode=parsed["mode"], ack=parsed["ack"],
                        length=parsed["length"], param_len=parsed["param_len"],
                        param_hex=parsed["param_hex"], checksum=parsed["checksum"],
                        checksum_calc=parsed["checksum_calc"], checksum_ok=ok,
                        checksum_origin=parsed["checksum_origin"], meta=meta,
                        file=rel, line_no=line_no, flags=flags,
                        raw=line if include_raw else None))
                    stats["frames"] += 1
                    if progress_every and (stats["frames"] % progress_every == 0):
                        _progress_line(f"{rel}: 已提取 {stats['frames']} 帧")

                    # 帧尾之后紧跟新帧头 → 继续解析下一帧；否则剩余字节按 extra_bytes 截断
                    first_in_chain = False
                    if len(fb) > full and fb[full:full + 2] == b"\x5a\xa5":
                        fb = fb[full:]
                        continue
                    break
    finally:
        fh.close()
    return frames


def scan_input(path: str, args, stats: dict) -> Tuple[str, List[Frame]]:
    """发现文件 -> (按需解压) -> 逐文件提取 -> 汇总。"""
    abspath = os.path.abspath(path)
    if not os.path.exists(abspath):
        raise FileNotFoundError(f"输入路径不存在: {abspath}")

    # 压缩包输入: 解压成目录后按目录扫描
    if os.path.isfile(abspath) and abspath.lower().endswith(TAR_EXTS):
        scan_root = maybe_extract(abspath)
        if scan_root is None:                 # 同名目录已存在, 直接扫描该目录
            scan_root = os.path.join(os.path.dirname(abspath), _tar_stem(abspath))
    else:
        scan_root = abspath

    # 嵌套压缩包: 默认逐层解压并解析（--no-recursive-extract 可关闭）。
    # 固定点迭代: 同名目录已存在的包被 maybe_extract 告警跳过（返回 None），
    # 一轮无新解压即收敛，不会死循环。
    if not args.no_recursive_extract and os.path.isdir(scan_root):
        while True:
            nested = [os.path.join(r, f) for r, _d, fs in os.walk(scan_root)
                      for f in fs if f.lower().endswith(TAR_EXTS)]
            new_dirs = [d for d in (maybe_extract(t) for t in nested) if d]
            if not new_dirs:
                break

    scan_root, files = discover_files(scan_root, not args.no_extensionless,
                                      args.extra_ext.split(",") if args.extra_ext else ())
    stats["files_candidates"] = len(files)
    if not files:
        raise FileNotFoundError("未找到候选日志文件（支持 .log/.log.N/.curf/.txt/logs.log*"
                                " 及已知前缀的无扩展名日志；可用 --extra-ext 扩展）")

    frames: List[Frame] = []
    total = len(files)
    show_file_lines = total > 1 or not args.quiet
    for i, fp in enumerate(files, 1):
        rel = os.path.relpath(fp, scan_root).replace(os.sep, "/")
        t0 = time.monotonic()
        try:
            size = os.path.getsize(fp)
            n_before = stats["frames"]
            stats["files_scanned"] += 1
            stats["total_bytes"] += size
            new_frames = extract_frames_from_file(
                fp, rel, stats, merge=not args.no_merge,
                include_raw=args.include_raw, encoding=args.encoding,
                progress_every=0 if args.quiet else args.progress)
            frames.extend(new_frames)
            if new_frames:
                stats["files_with_frames"] += 1
            if show_file_lines:
                dt = time.monotonic() - t0
                log_msg(f"[{i}/{total}] {rel} ... {len(new_frames)} 帧 "
                        f"({size / 1048576:.1f} MB, {dt:.1f}s)")
        except OSError as e:
            stats["files_failed"] += 1
            warn(f"读取失败 {rel}: {e}")
        _clear_progress_line()
    return scan_root, frames

# ---------------------------------------------------------------------------
# 统计与输出
# ---------------------------------------------------------------------------


def build_summary(frames: List[Frame]) -> dict:
    per = {}
    s = {"total": len(frames), "tx": 0, "rx": 0, "unk": 0,
         "ck_ok": 0, "ck_bad": 0, "ck_missing": 0, "flags": Counter(), "cmds": per}
    for f in frames:
        if f.direction == DIR_TX:
            s["tx"] += 1
        elif f.direction == DIR_RX:
            s["rx"] += 1
        else:
            s["unk"] += 1
        if f.checksum_ok is True:
            s["ck_ok"] += 1
        elif f.checksum_ok is False:
            s["ck_bad"] += 1
        else:
            s["ck_missing"] += 1
        s["flags"].update(f.flags)
        c = per.get(f.cmd)
        if c is None:
            c = per[f.cmd] = {"count": 0, "tx": 0, "rx": 0, "unk": 0,
                              "pmin": f.param_len, "pmax": f.param_len,
                              "first_key": None, "first_ts": ""}
        c["count"] += 1
        c[f.direction if f.direction in ("tx", "rx", "unk") else "unk"] += 1
        c["pmin"] = min(c["pmin"], f.param_len)
        c["pmax"] = max(c["pmax"], f.param_len)
        if f.ts_key is not None and (c["first_key"] is None or f.ts_key < c["first_key"]):
            c["first_key"] = f.ts_key
            c["first_ts"] = f.ts
    return s


def frame_to_record(f: Frame, include_raw: bool) -> dict:
    """JSONL 记录: 固定键序 + 元数据扁平化（行内缺失的键不出现）。"""
    rec = {
        "ts": f.ts, "dir": f.direction, "cmd": f.cmd, "mode": f.mode,
        "ack": f.ack, "length": f.length, "param_len": f.param_len,
        "param_hex": f.param_hex, "checksum": f.checksum,
        "checksum_calc": f.checksum_calc, "checksum_ok": f.checksum_ok,
        "checksum_origin": f.checksum_origin,
    }
    for k in META_KEYS_ORDER:
        if k in f.meta:
            rec[k] = f.meta[k]
    rec["file"] = f.file
    rec["line_no"] = f.line_no
    rec["flags"] = f.flags
    rec["raw"] = f.raw if include_raw else None
    return rec


def write_jsonl(frames: List[Frame], path: str, include_raw: bool) -> int:
    n = 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for f in frames:
            fh.write(json.dumps(frame_to_record(f, include_raw),
                                ensure_ascii=False, separators=(",", ":")) + "\n")
            n += 1
    return n


def _param_display(f: Frame, max_param: int) -> str:
    if f.param_len == 0:
        return "-"
    hx = f.param_hex
    limit = max_param * 2 if max_param else len(hx)
    shown = " ".join(hx[i:i + 2] for i in range(0, min(len(hx), limit), 2))
    if len(hx) > limit:
        shown += f" …(+{f.param_len - max_param}B more)"
    return shown


def write_markdown(frames: List[Frame], summary: dict, stats: dict,
                   header: dict, path: str) -> None:
    """生成 AI 友好的 MD 报告: 摘要 + CMD 索引 + 查询指南（明细按需查 JSONL）。

    刻意不包含逐帧明细（那是 JSONL 的职责），保证 MD 体积只随 CMD 种类数增长。
    """
    L: List[str] = []
    ap = L.append
    jsonl_name = header.get("jsonl_name", "")

    ap("# SOC↔MCU 数据帧提取报告")
    ap("")
    ap(f"- 输入: `{header['input']}`")
    ap(f"- 扫描时间: {header['scan_time']}   工具: {TOOL_NAME} v{__version__}")
    ap(f"- 扫描文件: {stats['files_candidates']} 候选 / {stats['files_with_frames']} 含帧"
       f" / {stats['files_failed']} 失败 ({stats['total_bytes'] / 1048576:.1f} MB)")
    ap(f"- 帧总数: **{summary['total']}**   TX {summary['tx']} (SOC→MCU)   "
       f"RX {summary['rx']} (MCU→SOC)   方向未知 {summary['unk']}")
    ap(f"- 校验: OK {summary['ck_ok']} / 不符 {summary['ck_bad']} / 缺失 {summary['ck_missing']}")
    diag = (f"- 诊断: 跨行合并 {stats['merged_cross_line']}"
            f" / 日志内截断 {stats['truncated_in_log']}"
            f" / 过短丢弃 {stats['rejected_short']}"
            f" / 帧尾多余字节 {summary['flags'].get('extra_bytes', 0)}")
    if header.get("cmd_filter"):
        diag += f"\n- CMD 过滤: {header['cmd_filter']}"
    ap(diag)
    ap(f"- 数据文件: {jsonl_name} ({header.get('jsonl_desc', '未生成')})"
       f" —— 本报告仅为摘要与索引, 帧明细请按下文查询方法检索")
    ap("- 图例: → SOC→MCU(tx)   ← MCU→SOC(rx)   ? 方向未知")
    ap("")

    # ---- CMD 总览（索引） ----
    per = summary["cmds"]
    ordered = sorted(per.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    max_c = header.get("max_cmds", 0)
    shown = ordered[:max_c] if max_c else ordered
    ap(f"## CMD 总览 ({len(ordered)})")
    ap("")
    ap("| CMD | 次数 | TX | RX | param_len | 首次时间 |")
    ap("|---|---:|---:|---:|---|---|")
    for cmd, c in shown:
        plen = str(c["pmin"]) if c["pmin"] == c["pmax"] else f"{c['pmin']}..{c['pmax']}"
        ap(f"| {cmd} | {c['count']} | {c['tx']} | {c['rx']} | {plen} | {c['first_ts'] or '-'} |")
    if max_c and len(ordered) > max_c:
        ap(f"... (+{len(ordered) - max_c} 个 CMD 未列出, 可统计 {jsonl_name} 或去掉 --max-cmds)")
    ap("")

    # ---- 数据文件与查询方法 ----
    ap("## 数据文件与查询方法")
    ap("")
    ap(f"全部帧明细在同级数据文件 `{jsonl_name}` 中（每帧一行 JSON）, 按需检索:")
    ap("")
    ap("```bash")
    ap(f"# 某个 CMD 的全部帧（可配 head/tail 限量）")
    ap(f"grep '\"cmd\":\"0x2315\"' {jsonl_name}")
    ap(f"# 某 CMD + 方向 (tx=SOC→MCU, rx=MCU→SOC; 管道过滤与键序无关)")
    ap(f"grep '\"cmd\":\"0x8c00\"' {jsonl_name} | grep '\"dir\":\"rx\"'")
    ap(f"# 某时间段（时间戳前缀匹配, 注意结尾不加引号）")
    ap(f"grep '\"ts\":\"08-14 15:30' {jsonl_name}")
    ap(f"# 按参数字节查")
    ap(f"grep '\"param_hex\":\"20\"' {jsonl_name}")
    ap("```")
    ap("")
    ap("```python")
    ap(f"# 统计各 CMD 次数")
    ap(f"python -c \"import json,collections,io; "
       f"c=collections.Counter(json.loads(l)['cmd'] for l in io.open('{jsonl_name}',encoding='utf-8')); "
       f"print(c.most_common(20))\"")
    ap(f"# 按时间排序查看某 CMD 全部帧")
    ap(f"python -c \"import json,io; fs=[json.loads(l) for l in io.open('{jsonl_name}',encoding='utf-8')]; "
       f"[print(json.dumps(f,ensure_ascii=False)) for f in "
       f"sorted((f for f in fs if f['cmd']=='0x2315'), key=lambda f:f['ts'])]\"")
    ap("```")
    ap("")
    ap("注意: JSONL 行序 = 扫描顺序（按文件路径排序，非全局时间序）；"
       "不同 boot 的时间戳格式可能不同（如 08-14 与 01-01），跨时间分析请用 python 排序。")
    ap("")

    # ---- JSONL 字段速查 ----
    ap("## JSONL 字段速查")
    ap("")
    ap("| 字段 | 类型 | 含义 |")
    ap("|---|---|---|")
    ap("| ts | string | 日志原始时间戳（无时间戳为空串） |")
    ap("| dir | string | tx=SOC→MCU（WriteThread）/ rx=MCU→SOC（ProcessThread）/ ? |")
    ap("| cmd | string | 小写十六进制，如 0x2315 |")
    ap("| mode / ack / length / param_len | int | 帧头字段；length 为 LEN 字段值，param_len=LEN-3 |")
    ap("| param_hex | string | PARAM 连续小写 hex（无空格），param_len=0 时为空串 |")
    ap("| checksum / checksum_calc / checksum_ok | int\\|null / int\\|null / bool\\|null | 校验位实得/计算/是否一致，缺失为 null |")
    ap("| checksum_origin | string | original / missing（帧尾缺校验位） |")
    ap("| size / need_ack / id / sub_id / can_ack / ack_status / retryCount / counter / map / propid / iReadLen / isSuccess / isAckAnswer / isAckReq | int 或 string | 日志行内元数据（0x 开头为 string，否则 int；行内缺失的键不出现） |")
    ap("| file / line_no | string / int | 来源文件（相对扫描根路径，/ 分隔）与行号（1-based） |")
    ap("| flags | list | short 缺校验位 / merged_hex、merged_legacy 跨行合并 / truncated_in_log 日志内截断 / extra_bytes 帧尾多余字节 |")
    ap("| raw | string\\|null | 原始日志行（仅 --include-raw 时非 null） |")
    ap("")

    # ---- 数据样例 ----
    samples = []
    for want in (DIR_TX, DIR_RX):
        for f in frames:
            if f.direction == want:
                samples.append(json.dumps(frame_to_record(f, False),
                                          ensure_ascii=False, separators=(",", ":")))
                break
    if samples:
        ap("## 数据样例")
        ap("")
        ap("```json")
        L.extend(samples)
        ap("```")
        ap("")

    # ---- 异常帧 ----
    abnormal = [f for f in frames if f.checksum_ok is not True or f.flags]
    if abnormal:
        ap(f"## 异常帧 ({len(abnormal)})")
        ap("")
        for f in abnormal[:200]:
            why = []
            if f.checksum_ok is False:
                why.append(f"校验不符(算得0x{f.checksum_calc:02x}/实得0x{f.checksum:02x})")
            elif f.checksum_ok is None:
                why.append("校验缺失")
            why += [FLAG_ZH.get(x, x) for x in f.flags]
            ap(f"- `{f.file}:{f.line_no}` {f.cmd} {'、'.join(why)} param={_param_display(f, 8)}")
        if len(abnormal) > 200:
            ap(f"- ... (+{len(abnormal) - 200} more, 见 {jsonl_name})")
        ap("")

    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L))
        fh.write("\n")


# ---------------------------------------------------------------------------
# 控制台输出辅助
# ---------------------------------------------------------------------------


def _make_stdio_robust() -> None:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(errors="replace")
            except Exception:
                pass


def log_msg(msg: str) -> None:
    print(msg, file=sys.stderr)


def warn(msg: str) -> None:
    print(f"[警告] {msg}", file=sys.stderr)


def _progress_line(msg: str) -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\r" + msg)
        sys.stderr.flush()


def _clear_progress_line() -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\n")
        sys.stderr.flush()

# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------


def run_selftest() -> int:
    """内置断言自测（合成用例 + 真实行样例）。返回 0/1。"""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="frame_extractor_selftest_")

    cases = []

    def case(name):
        def deco(fn):
            cases.append((name, fn))
            return fn
        return deco

    def scan_lines(content: str, **kw) -> Tuple[dict, List[Frame]]:
        p = os.path.join(tmp, "t.log")
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        st = new_stats()
        return st, extract_frames_from_file(p, "t.log", st, **kw)

    TX_LINE = ("08-14 15:30:07.661684   316   329 D CviautoDataHubWriteThread: Write: "
               "size:10 need_ack:0x00 id:0x00 sub_id:0x03 can_ack:0x2C ack_status:0x00 "
               "retryCount:0x00 counter:11, map:1 propid:557862928 "
               "data:0x5a 0xa5 0x2c 0x00 0x04 0x00 0x00 0x03 0x00 0xcd \n")
    RX_LINE = ("08-14 15:30:08.030315   316   326 D CviautoDataHubProcessThread: "
               "onProcess:0x5a 0xa5 0xf6 0x00 0x03 0x00 0x23 0x15 0xcf  "
               "counter:29, iReadLen:9 isSuccess:1 map:1 propid:561009664 "
               "isAckAnswer:1 isAckReq:0\n")

    @case("标准帧解析 + 校验和 + 元数据 + 方向(tx)")
    def _c1():
        st, fs = scan_lines(TX_LINE)
        assert len(fs) == 1, f"帧数 {len(fs)}"
        f = fs[0]
        assert (f.cmd, f.param_len, f.param_hex) == ("0x0003", 1, "00"), f.cmd
        assert f.checksum == 0xCD and f.checksum_calc == 0xCD and f.checksum_ok is True
        assert f.direction == "tx" and f.ts_key == (8, 14, 15, 30, 7, 661684)
        assert f.meta.get("counter") == 11 and f.meta.get("propid") == 557862928
        assert f.meta.get("size") == 10 and f.meta.get("can_ack") == "0x2c"
        # 强不变量: TX 行 size == full_frame_len
        assert f.meta.get("size") == f.length + 6
        assert f.checksum_origin == "original" and not f.flags

    @case("校验和算法 (0xcd)")
    def _c2():
        assert (~sum(b"\x2c\x00\x04\x00\x00\x03\x00") + 1) & 0xFF == 0xCD

    @case("0x 前缀混合大小写")
    def _c3():
        st, fs = scan_lines("0X5A 0xA5 0x2C 0x00 0x04 0x00 0x00 0x03 0x00 0xCD\n")
        assert len(fs) == 1 and fs[0].cmd == "0x0003" and fs[0].checksum_ok is True

    @case("跨行合并: 纯 HEX 延续行")
    def _c4():
        content = ("5a a5 00 00 03 00 23 15\n"
                   "c5\n")
        st, fs = scan_lines(content)
        assert len(fs) == 1 and fs[0].cmd == "0x2315" and fs[0].param_len == 0
        assert fs[0].checksum == 0xC5 and fs[0].checksum_ok is True
        assert "merged_hex" in fs[0].flags and st["merged_cross_line"] == 1

    @case("跨行合并: CMD/PARAM 行 (旧格式)")
    def _c5():
        content = ("5a a5 2c 00 04 00 00 03\n"
                   "CMD: 0x0003 PARAM: 00 cd\n")
        st, fs = scan_lines(content)
        assert len(fs) == 1 and fs[0].cmd == "0x0003" and fs[0].checksum_ok is True
        assert "merged_legacy" in fs[0].flags and st["merged_cross_line"] == 1

    @case("缺校验位: 接受并标记 missing/short")
    def _c6():
        st, fs = scan_lines("5a a5 2c 00 04 00 00 03 00\n")
        assert len(fs) == 1
        f = fs[0]
        assert f.checksum is None and f.checksum_ok is None
        assert f.checksum_origin == "missing" and "short" in f.flags

    @case("过短帧: 丢弃并计数")
    def _c7():
        st, fs = scan_lines("5a a5 2c 00 04 00 00\n")
        assert len(fs) == 0 and st["rejected_short"] == 1

    @case("一行多帧")
    def _c8():
        st, fs = scan_lines("5a a5 2c 00 04 00 00 03 00 cd 5a a5 2c 00 04 00 00 03 00 cd\n")
        assert len(fs) == 2 and all(f.cmd == "0x0003" for f in fs)

    @case("方向(rx) + RX 元数据 + id+sub_id==CMD 不变量")
    def _c9():
        st, fs = scan_lines(RX_LINE)
        assert len(fs) == 1
        f = fs[0]
        assert f.direction == "rx" and f.cmd == "0x2315" and f.param_len == 0
        assert f.meta.get("iReadLen") == 9 == f.length + 6
        assert f.meta.get("isAckAnswer") == 1 and f.meta.get("counter") == 29
        assert f.checksum_ok is True

    @case("帧尾多余字节: 截断并标记 extra_bytes")
    def _c10():
        st, fs = scan_lines("5a a5 2c 00 04 00 00 03 00 cd aa\n")
        assert len(fs) == 1 and "extra_bytes" in fs[0].flags
        assert fs[0].param_hex == "00"

    @case("Syslog 时间戳解析")
    def _c11():
        st, fs = scan_lines("Feb 10 11:07:03 host tag: 5a a5 2c 00 04 00 00 03 00 cd\n")
        assert len(fs) == 1 and fs[0].ts == "Feb 10 11:07:03"
        assert fs[0].ts_key == (2, 10, 11, 7, 3, 0)

    @case("--cmd 归一化")
    def _c12():
        assert normalize_cmd_list(["0X2315", "2315", "0x8C00"]) == ["0x2315", "0x8c00"]

    @case("嵌套压缩包默认解压并解析")
    def _c13():
        inner_src = os.path.join(tmp, "nest_inner_src")
        os.makedirs(inner_src, exist_ok=True)
        with open(os.path.join(inner_src, "inner.log"), "w", encoding="utf-8") as fh:
            fh.write("5a a5 f6 00 03 00 23 15 cf\n")             # 0x2315

        def make_tree(name: str) -> str:
            base = os.path.join(tmp, name)
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, "outer.log"), "w", encoding="utf-8") as fh:
                fh.write("5a a5 2c 00 04 00 00 03 00 cd\n")      # 0x0003
            with tarfile.open(os.path.join(base, "inner.tar.gz"), "w:gz") as tar:
                tar.add(inner_src, arcname="inner")
            return base

        def _args(no_recursive: bool) -> argparse.Namespace:
            return argparse.Namespace(
                no_recursive_extract=no_recursive, no_extensionless=False,
                extra_ext="", no_merge=True, include_raw=False,
                encoding="utf-8", quiet=True, progress=0)

        def scan_dir(base: str, no_recursive: bool):
            st = new_stats()
            _root, fs = scan_input(base, _args(no_recursive), st)
            return st, fs

        st, fs = scan_dir(make_tree("nest_a"), False)   # 默认: 嵌套包被解压并解析
        assert st["frames"] == 2, f"帧数 {st['frames']}"
        assert {f.cmd for f in fs} == {"0x0003", "0x2315"}
        st2, fs2 = scan_dir(make_tree("nest_b"), True)  # 全新目录 + 关闭: 不解压嵌套包
        assert st2["frames"] == 1 and fs2[0].cmd == "0x0003"
        # 回归: 压缩包输入且同名目录已存在（解压跳过）时应直接扫描已解压目录
        st3, fs3 = scan_dir(os.path.join(tmp, "nest_a", "inner.tar.gz"), False)
        assert st3["frames"] == 1 and fs3[0].cmd == "0x2315", \
            (st3["frames"], [f.cmd for f in fs3])

    @case("MD 报告: 摘要+索引+查询指南, 无逐帧明细")
    def _c14():
        st, fs = scan_lines(TX_LINE + RX_LINE)
        assert len(fs) == 2
        md_path = os.path.join(tmp, "t_frames.md")
        header = {"input": "t.log", "scan_time": "2026-09-11 00:00:00",
                  "max_cmds": 0, "cmd_filter": "",
                  "jsonl_name": "t_frames.jsonl", "jsonl_desc": "0.0 MB, 2 行"}
        write_markdown(fs, build_summary(fs), st, header, md_path)
        with open(md_path, encoding="utf-8") as fh:
            md = fh.read()
        for marker in ("数据文件与查询方法", "JSONL 字段速查", "数据样例", "CMD 总览"):
            assert marker in md, f"缺少段落: {marker}"
        assert 'grep \'"cmd":"0x2315"\' t_frames.jsonl' in md    # 查询示例带真实文件名
        assert '| grep \'"dir":"rx"\'' in md                      # cmd+dir 用管道（键序无关）
        assert "grep '\"ts\":\"08-14 15:30' t_frames.jsonl" in md  # 时间前缀模式正确（无多余尾引号）
        assert "| 0x0003 |" in md and "| 0x2315 |" in md          # 索引表含各 CMD
        assert "## 0x0003 (" not in md and "## 0x2315 (" not in md  # 逐帧明细已移除
        assert '{"ts":"' in md and '"dir":"tx"' in md             # 数据样例为原始 JSONL 行
        assert '"dir":"rx"' in md

    passed, failed = 0, []
    try:
        for name, fn in cases:
            try:
                fn()
                passed += 1
                log_msg(f"  PASS {name}")
            except AssertionError as e:
                failed.append(name)
                log_msg(f"  FAIL {name}: {e}")
            except Exception as e:  # noqa: BLE001
                failed.append(name)
                log_msg(f"  FAIL {name}: 异常 {e!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log_msg(f"自测: {passed}/{len(cases)} 通过")
    return 0 if not failed else 1

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def normalize_cmd_list(tokens: Sequence[str]) -> List[str]:
    out, seen = [], set()
    for t in tokens:
        t = t.strip().lower()
        if not t:
            continue
        if not t.startswith("0x"):
            t = "0x" + t
        body = t[2:]
        if not body or any(c not in "0123456789abcdef" for c in body):
            raise ValueError(f"非法 CMD: {t!r}")
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="SOC↔MCU 数据帧专用提取工具: 找出日志中的 5a a5 数据帧，"
                    "输出 AI 友好的 Markdown + JSONL（不做信号解析）。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", nargs="?", help="日志文件 / 目录 / .tar.gz 压缩包")
    p.add_argument("-o", "--output", metavar="DIR",
                   help="输出目录（默认: 目录输入→目录本身, 文件输入→其父目录）")
    g = p.add_argument_group("输出")
    g.add_argument("--no-jsonl", action="store_true", help="不输出 .jsonl")
    g.add_argument("--no-md", action="store_true", help="不输出 .md")
    g.add_argument("--include-raw", action="store_true",
                   help="JSONL 中包含原始日志行（行可达数 KB，文件会显著变大）")
    g = p.add_argument_group("过滤/显示")
    g.add_argument("--cmd", metavar="LIST",
                   help="仅输出指定 CMD, 逗号分隔, 如 0x2315,0x8C00")
    g.add_argument("--max-cmds", type=int, default=0, metavar="N",
                   help="MD 的 CMD 总览表最多列出的 CMD 数 (默认 0=不限)")
    g = p.add_argument_group("解析")
    g.add_argument("--no-merge", action="store_true", help="关闭跨行合并")
    g.add_argument("--no-extensionless", action="store_true",
                   help="跳过无扩展名的已知前缀日志（默认包含, 如 main_log_1__...）")
    g.add_argument("--extra-ext", metavar="EXTS", default="",
                   help="额外的日志扩展名, 逗号分隔, 如 .db,.out")
    g.add_argument("--encoding", default="utf-8", help="输入编码 (默认 utf-8)")
    g.add_argument("--no-recursive-extract", action="store_true",
                   help="不解压扫描目录内的嵌套 tar.gz（默认解压并解析; 同名目录已存在时自动跳过）")
    g = p.add_argument_group("其他")
    g.add_argument("--progress", type=int, default=5000, metavar="N",
                   help="每 N 帧打印一次进度 (默认 5000, 0=关闭)")
    g.add_argument("--selftest", action="store_true", help="运行内置自测后退出")
    g.add_argument("-q", "--quiet", action="store_true", help="静默进度输出")
    p.add_argument("-V", "--version", action="version",
                   version=f"{TOOL_NAME} v{__version__}")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _make_stdio_robust()

    if args.selftest:
        return run_selftest()
    if not args.path:
        log_msg("错误: 缺少输入路径（日志文件/目录/tar.gz）。用 -h 查看用法。")
        return 2

    try:
        cmds = normalize_cmd_list(args.cmd.split(",")) if args.cmd else []
    except ValueError as e:
        log_msg(f"错误: {e}")
        return 2

    stats = new_stats()
    t0 = time.monotonic()
    try:
        scan_root, frames = scan_input(args.path, args, stats)
    except (FileNotFoundError, tarfile.TarError) as e:
        log_msg(f"错误: {e}")
        return 1

    filter_desc = ""
    if cmds:
        before = len(frames)
        frames = [f for f in frames if f.cmd in cmds]
        filter_desc = f"{','.join(cmds)} (扫描 {before} 帧 → 命中 {len(frames)})"

    # 输出路径
    abspath = os.path.abspath(args.path)
    if os.path.isdir(abspath):
        stem = os.path.basename(abspath.rstrip("\\/"))
        out_dir = abspath
    else:
        stem = _tar_stem(abspath) if abspath.lower().endswith(TAR_EXTS) \
            else os.path.splitext(os.path.basename(abspath))[0]
        out_dir = os.path.dirname(abspath)
    if args.output:
        out_dir = os.path.abspath(args.output)
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, f"{stem}_frames.md")
    jsonl_path = os.path.join(out_dir, f"{stem}_frames.jsonl")

    header = {
        "input": abspath, "scan_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "max_cmds": args.max_cmds,
        "cmd_filter": filter_desc, "jsonl_name": os.path.basename(jsonl_path),
    }
    summary = build_summary(frames)
    # 先写 JSONL, 让 MD 头部能给出数据文件的大小/行数
    if not args.no_jsonl:
        write_jsonl(frames, jsonl_path, args.include_raw)
        header["jsonl_desc"] = (f"{os.path.getsize(jsonl_path) / 1048576:.1f} MB, "
                                f"{summary['total']} 行")
    else:
        header["jsonl_desc"] = "未生成"
    if not args.no_md:
        write_markdown(frames, summary, stats, header, md_path)

    dt = time.monotonic() - t0
    log_msg(f"共 {summary['total']} 帧 "
            f"({stats['files_with_frames']}/{stats['files_candidates']} 文件含帧), "
            f"校验 OK {summary['ck_ok']} / 不符 {summary['ck_bad']} / "
            f"缺失 {summary['ck_missing']}, 耗时 {dt:.1f}s")
    if not args.no_md:
        log_msg(f"写出: {md_path}")
    if not args.no_jsonl:
        log_msg(f"写出: {jsonl_path}")
    return 0 if frames else 3


if __name__ == "__main__":
    sys.exit(main())
