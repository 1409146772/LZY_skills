"""报告生成：OVERVIEW / MESSAGES / SIGNALS / ENUMS / UNKNOWN_IDS + bundle.json + parse_log.txt。"""

import json
from datetime import datetime
from pathlib import Path

from . import __version__
from .dbc import DbcBundle
from .paths import id_hex
from .pipeline import RunStats, build_columns, fmt_ts_abs

BUNDLE_FILES = {
    "overview": "OVERVIEW.md",
    "messages": "MESSAGES.md",
    "signals": "SIGNALS.md",
    "enums": "ENUMS.md",
    "unknown_ids": "UNKNOWN_IDS.md",
    "machine": "bundle.json",
    "parse_log": "parse_log.txt",
}


def _num(value) -> str:
    """数值渲染：整数去掉 .0，其余最短 repr，None → '-'。"""
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value)


def _rel(value) -> str:
    return f"{value:.3f}" if value is not None else "-"


def _or_dash(text: str) -> str:
    return text if text else "-"


def _order_label(sig) -> str:
    return "Motorola" if sig.byte_order == "big_endian" else "Intel"


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def write_overview(out_dir: Path, dbc: DbcBundle, stats: RunStats, settings: dict,
                   csv_files: dict) -> None:
    opts = settings["options"]
    meta = stats.blf_meta
    decoded_total = sum(s.count for s in stats.messages.values())
    lines = [
        "# CAN 日志解析数据包",
        "",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}（canlog_tool v{__version__}）",
        "",
        "## 数据来源",
        "",
        f"- BLF: {meta.get('path', '-')}",
        f"- BLF 大小: {meta.get('size_bytes', '-')} 字节",
    ]
    if meta.get("objects") is not None:
        lines.append(f"- BLF 容器对象数: {meta['objects']}")
    if stats.t0 is not None:
        lines.append(f"- 首帧时间（UTC 渲染）: {fmt_ts_abs(stats.t0)}")
    if stats.t_end is not None:
        lines.append(f"- 末帧时间（UTC 渲染）: {fmt_ts_abs(stats.t_end)}")
    # BLF 容器头自带的时间戳与实际帧时间可能不一致，仅作元数据展示
    if meta.get("start_epoch") is not None:
        lines.append(f"- 容器头起始时间（仅供参考，可能与帧时间不一致）: {fmt_ts_abs(meta['start_epoch'])}")
    lines.append(f"- DBC: {', '.join(dbc.encodings)}")
    for name, enc in dbc.encodings.items():
        lines.append(f"  - {name}（编码 {enc}）")
    lines += [
        "",
        "## 统计",
        "",
        f"- 总帧数: {stats.frames}",
        f"- 成功解码: {decoded_total}",
        f"- 未知 ID 帧（不在 DBC 中）: {stats.unknown_frames}，共 {len(stats.unknown)} 个 ID",
        f"- 被过滤帧（报文/时间窗/通道过滤）: {stats.filtered}",
        f"- 错误帧: {stats.error_frames}，远程帧: {stats.remote_frames}，非 CAN 对象: {stats.non_can}，CAN FD 帧: {stats.fd_frames}",
        f"- 时长: {stats.duration:.3f} s，通道: {sorted(c for c in stats.channels if c is not None)}",
        f"- DBC 报文命中: {len(stats.messages)}/{len(dbc.all_messages)}",
        "",
        "## 时间戳语义（重要）",
        "",
        "- `ts_abs`：BLF 内部把**本地墙钟当作 epoch** 存储，本工具用 UTC 渲染该值，"
        "结果与日志文件名中的时间一致。跨工具对比时请以 `ts_rel` 为准。",
        "- `ts_rel`：相对本日志第一帧的秒数，所有 CSV 可直接按 `ts_rel` 对齐。",
        "",
        "## CSV 列说明",
        "",
        f"- 公共列：`ts_abs`, `ts_rel`, `channel`, `dlc`{', `dir`' if opts.get('include_direction') else ''}",
        f"- 其后为信号列（物理值，已按 DBC 因子/偏移换算），顺序与 SIGNALS.md 中该报文的信号一致",
        f"- 枚举模式: `{opts.get('enum_mode', 'label')}`"
        + ("（枚举信号附加 `<信号>_lbl` 列）" if opts.get("enum_mode") == "both"
           else "（枚举信号直接写标签文本）" if opts.get("enum_mode", "label") == "label"
           else "（枚举信号只写数值）"),
        "- 缺失值（多路复用未选中 / 帧长不足）= 空单元格",
        "",
        "## 文件地图",
        "",
        "| 文件 | 内容 |",
        "|---|---|",
        "| OVERVIEW.md | 本文件，数据包入口 |",
        "| MESSAGES.md | 报文索引：ID → 名称/周期/帧数/时间范围/CSV |",
        "| SIGNALS.md | 信号索引：信号 → 位定义/因子/单位/观测范围/CSV |",
        "| ENUMS.md | 枚举（VAL_）取值翻译表 |",
        "| UNKNOWN_IDS.md | 日志中存在但 DBC 未定义的 ID |",
        "| bundle.json | 机器可读的全部索引汇总 |",
        "| parse_log.txt | 解码错误样本（含原始字节） |",
        "| data/*.csv | 每条报文一个 CSV 时间序列 |",
        "",
        "## 解码问题",
        "",
    ]
    issues = []
    for (fid, ext), stat in sorted(stats.messages.items()):
        entry = dbc.get(fid, ext)
        if entry is None:
            continue
        if stat.dec_errors:
            issues.append(f"- `{entry.id_str}` {entry.name}: 解码错误 {stat.dec_errors} 帧")
        if stat.truncated:
            issues.append(f"- `{entry.id_str}` {entry.name}: 帧长不足（截断）{stat.truncated} 帧，缺失信号为空")
    for c in dbc.conflicts:
        issues.append(
            f"- DBC 重复报文 ID {c['id']}: `{c['dropped']}` 被 `{c['keep']}` 覆盖"
            f"（来自 {', '.join(c['files'])}）"
        )
    for name, msgs in dbc.dup_signal_names.items():
        issues.append(f"- 信号名 `{name}` 同时存在于多个报文: {', '.join(msgs)}（SIGNALS.md 中按报文区分）")
    lines.extend(issues if issues else ["- 无"])
    lines += [
        "",
        "## 使用建议",
        "",
        "1. 先读 MESSAGES.md 找到目标报文 → 再读 SIGNALS.md 确认信号含义与范围 → 最后读对应 `data/*.csv`。",
        "2. 用 grep 在 SIGNALS.md / ENUMS.md 中按信号名或含义定位，比通读更快。",
        "3. 查 UNKNOWN_IDS.md 可知道哪些总线数据无法解码。",
        "",
    ]
    _write(out_dir / BUNDLE_FILES["overview"], "\n".join(lines))


def write_messages(out_dir: Path, dbc: DbcBundle, stats: RunStats, csv_files: dict) -> None:
    lines = [
        "# 报文索引",
        "",
        "`Frames = 0` 表示 DBC 中声明但本日志未出现的报文（无 CSV）。",
        "",
        "| ID | Name | DLC | Cycle(ms) | Sender | Frames | First(rel) | Last(rel) | Sig | Enum | DecErr | CSV |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for msg in dbc.all_messages:
        key = (msg.frame_id, msg.is_extended)
        stat = stats.messages.get(key)
        count = stat.count if stat else 0
        first = _rel(stat.first_rel) if stat else "-"
        last = _rel(stat.last_rel) if stat else "-"
        dec_err = stat.dec_errors if stat else 0
        enum_cnt = sum(1 for s in msg.signals if s.choices)
        csv_rel = csv_files.get(key, "-")
        csv_cell = f"[csv]({csv_rel})" if csv_rel != "-" else "-"
        lines.append(
            f"| {msg.id_str} | {msg.name} | {msg.length} | "
            f"{msg.cycle_time if msg.cycle_time else '-'} | {_or_dash(','.join(msg.senders))} | "
            f"{count} | {first} | {last} | {len(msg.signals)} | {enum_cnt or '-'} | "
            f"{dec_err or '-'} | {csv_cell} |"
        )
    _write(out_dir / BUNDLE_FILES["messages"], "\n".join(lines) + "\n")


def write_signals(out_dir: Path, dbc: DbcBundle, stats: RunStats, csv_files: dict) -> None:
    lines = [
        "# 信号索引",
        "",
        "每行一个信号，按信号所在报文的 DBC 定义顺序排列；`DecMin/DecMax` 为本日志中的观测物理范围。",
        "",
        "| Signal | Message | ID | Start | Bits | Order | Scale | Offset | Unit | PhysMin | PhysMax | Enum | DecMin | DecMax | CSV |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for msg in dbc.all_messages:
        key = (msg.frame_id, msg.is_extended)
        stat = stats.messages.get(key)
        csv_rel = csv_files.get(key)
        for sig in msg.signals:
            dec_min = stat.sig_min.get(sig.name) if stat else None
            dec_max = stat.sig_max.get(sig.name) if stat else None
            enum_cell = "mux" if sig.is_multiplexer else (str(len(sig.choices)) if sig.choices else "-")
            csv_cell = f"[csv]({csv_rel})" if csv_rel else "-"
            lines.append(
                f"| {sig.name} | {msg.name} | {msg.id_str} | {sig.start} | {sig.length} | "
                f"{_order_label(sig)} | {_num(sig.scale)} | {_num(sig.offset)} | {_or_dash(sig.unit)} | "
                f"{_num(sig.phys_min)} | {_num(sig.phys_max)} | {enum_cell} | "
                f"{_num(dec_min)} | {_num(dec_max)} | {csv_cell} |"
            )
    _write(out_dir / BUNDLE_FILES["signals"], "\n".join(lines) + "\n")


def write_enums(out_dir: Path, dbc: DbcBundle) -> None:
    lines = [
        "# 枚举取值表（VAL_）",
        "",
        "仅列出 DBC 中定义了取值表的信号；`label` 模式下 CSV 中出现的文本由此翻译回数值。",
        "",
        "| ID | Message | Signal | 取值 |",
        "|---|---|---|---|",
    ]
    for msg in dbc.all_messages:
        for sig in msg.signals:
            if not sig.choices:
                continue
            mapping = "; ".join(f"{raw}={label}" for raw, label in sorted(sig.choices.items()))
            lines.append(f"| {msg.id_str} | {msg.name} | {sig.name} | {mapping} |")
    _write(out_dir / BUNDLE_FILES["enums"], "\n".join(lines) + "\n")


def write_unknown(out_dir: Path, dbc: DbcBundle, stats: RunStats) -> None:
    lines = [
        "# 未知 ID（日志中存在但 DBC 未定义）",
        "",
        "这些帧无法解码，原始数据未写入 data/（除非启用 --dump-unknown）。",
        "",
    ]
    if stats.unknown:
        lines += [
            "| Hex ID | Frames | First(rel) | Last(rel) | DLC(众数) | 样例数据 |",
            "|---|---|---|---|---|---|",
        ]
        for (fid, ext), info in sorted(stats.unknown.items()):
            dlcs = info["dlcs"]
            mode_dlc = max(dlcs, key=dlcs.get) if dlcs else "-"
            lines.append(
                f"| {id_hex(fid, ext)} | {info['count']} | {_rel(info['first'])} | "
                f"{_rel(info['last'])} | {mode_dlc} | `{info['sample']}` |"
            )
    else:
        lines.append("无。")
    absent = [m for m in dbc.all_messages if (m.frame_id, m.is_extended) not in stats.messages]
    if absent:
        lines += [
            "",
            f"## DBC 中声明但本日志未出现的报文（{len(absent)} 条）",
            "",
            "| ID | Name | Sender |",
            "|---|---|---|",
        ]
        for msg in absent:
            lines.append(f"| {msg.id_str} | {msg.name} | {_or_dash(','.join(msg.senders))} |")
    _write(out_dir / BUNDLE_FILES["unknown_ids"], "\n".join(lines) + "\n")


def write_parse_log(out_dir: Path, dbc: DbcBundle, stats: RunStats) -> None:
    lines = ["# 解码问题明细", ""]
    any_issue = False
    for (fid, ext), stat in sorted(stats.messages.items()):
        entry = dbc.get(fid, ext)
        if entry is None:
            continue
        if stat.err_samples:
            any_issue = True
            lines.append(f"## {entry.id_str} {entry.name} — 解码错误 {stat.dec_errors} 帧（最多列 {len(stat.err_samples)} 条）")
            lines.append("")
            lines.append("| ts_rel | data(hex) | 错误 |")
            lines.append("|---|---|---|")
            for rel, hexdata, reason in stat.err_samples:
                lines.append(f"| {rel:.3f} | `{hexdata}` | {reason} |")
            lines.append("")
        if stat.truncated:
            any_issue = True
            lines.append(f"## {entry.id_str} {entry.name} — 帧长不足 {stat.truncated} 帧（按宽松模式解码，缺失信号留空）")
            lines.append("")
    if not any_issue:
        lines.append("全部帧解码成功，无问题。")
    _write(out_dir / BUNDLE_FILES["parse_log"], "\n".join(lines) + "\n")


def write_bundle_json(out_dir: Path, dbc: DbcBundle, stats: RunStats, settings: dict,
                      blf_path: Path, csv_files: dict) -> None:
    from .pipeline import fmt_ts_abs as _ts

    opts = settings["options"]
    decoded_total = sum(s.count for s in stats.messages.values())
    payload = {
        "tool_version": __version__,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "blf": str(blf_path),
            "size_bytes": stats.blf_meta.get("size_bytes"),
            "objects": stats.blf_meta.get("objects"),
            "first_frame_epoch": stats.t0,
            "first_frame_utc": _ts(stats.t0) if stats.t0 is not None else None,
            "last_frame_epoch": stats.t_end,
            "duration_s": round(stats.duration, 6),
            "channels": sorted(c for c in stats.channels if c is not None),
            "fd_frames": stats.fd_frames,
            "error_frames": stats.error_frames,
        },
        "dbc": [
            {"path": name, "encoding": enc} for name, enc in dbc.encodings.items()
        ],
        "options": {k: v for k, v in opts.items() if k in (
            "enum_mode", "include_raw_columns", "include_direction",
            "allow_truncated", "dump_unknown",
        )},
        "ts_abs_semantics": "BLF 将本地墙钟作为 epoch 存储；ts_abs 为该值的 UTC 渲染，与日志文件名时间一致",
        "stats": {
            "frames": stats.frames,
            "decoded_ok": decoded_total,
            "decode_errors": sum(s.dec_errors for s in stats.messages.values()),
            "truncated": sum(s.truncated for s in stats.messages.values()),
            "filtered": stats.filtered,
            "unknown_id_frames": stats.unknown_frames,
            "unknown_ids": [id_hex(fid, ext) for (fid, ext) in sorted(stats.unknown)],
        },
        "files": BUNDLE_FILES,
        "messages": [],
    }
    for msg in dbc.all_messages:
        key = (msg.frame_id, msg.is_extended)
        stat = stats.messages.get(key)
        if not stat:
            continue
        payload["messages"].append({
            "id": msg.id_str,
            "name": msg.name,
            "dlc": msg.length,
            "cycle_ms": msg.cycle_time,
            "frames": stat.count,
            "first_rel": round(stat.first_rel, 6) if stat.first_rel is not None else None,
            "last_rel": round(stat.last_rel, 6) if stat.last_rel is not None else None,
            "csv": csv_files.get(key),
            "columns": build_columns(msg, opts),
        })
    (out_dir / BUNDLE_FILES["machine"]).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_bundle(out_dir: Path, dbc: DbcBundle, stats: RunStats, settings: dict,
                 blf_path: Path, csv_files: dict) -> None:
    write_overview(out_dir, dbc, stats, settings, csv_files)
    write_messages(out_dir, dbc, stats, csv_files)
    write_signals(out_dir, dbc, stats, csv_files)
    write_enums(out_dir, dbc)
    write_unknown(out_dir, dbc, stats)
    write_parse_log(out_dir, dbc, stats)
    write_bundle_json(out_dir, dbc, stats, settings, blf_path, csv_files)
