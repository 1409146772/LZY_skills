"""流式主循环：分类 → 过滤 → 解码 → 写行 → 累计统计。内存 O(报文数)，无全量驻留。"""

import fnmatch
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import can
from cantools.database.errors import DecodeError

from . import config as cfg_mod
from .csvout import MessageCsvWriter, WriterPool, make_signal_formatters
from .dbc import MsgEntry, load_dbc
from .errors import BlfError, UsageError
from .paths import bundle_dir, csv_filename, id_hex

PROGRESS_EVERY = 200_000
ERR_SAMPLE_MAX = 50


def fmt_ts_abs(ts: float) -> str:
    """BLF 把本地墙钟当 epoch 存，必须用 UTC 渲染才与文件名时间一致（差 8 小时是常见错）。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


@dataclass
class MsgStat:
    count: int = 0
    first_rel: float | None = None
    last_rel: float | None = None
    dec_errors: int = 0
    truncated: int = 0
    err_samples: list = field(default_factory=list)   # (ts_rel, data_hex)，最多 ERR_SAMPLE_MAX 条
    sig_min: dict = field(default_factory=dict)       # 信号名 → 观测物理最小值
    sig_max: dict = field(default_factory=dict)


@dataclass
class RunStats:
    frames: int = 0
    non_can: int = 0
    error_frames: int = 0
    remote_frames: int = 0
    fd_frames: int = 0
    filtered: int = 0
    unknown_frames: int = 0
    channels: set = field(default_factory=set)
    t0: float | None = None
    t_end: float | None = None
    messages: dict = field(default_factory=dict)      # (frame_id, ext) → MsgStat
    unknown: dict = field(default_factory=dict)       # (frame_id, ext) → {count, first/last, dlcs, sample}
    blf_meta: dict = field(default_factory=dict)      # BLF 容器元信息（object_count 等）

    @property
    def duration(self) -> float:
        return (self.t_end - self.t0) if self.t0 is not None and self.t_end is not None else 0.0


def _match_spec(spec: str, msg: MsgEntry) -> bool:
    """--messages/--exclude 规则：0x 十六进制 ID、十进制 ID、精确名或通配。"""
    spec_l = spec.strip().lower()
    if spec_l.startswith("0x"):
        return int(spec_l, 16) == msg.frame_id
    if spec_l.isdigit():
        return int(spec_l) == msg.frame_id
    if any(c in spec_l for c in "*?["):
        return fnmatch.fnmatch(msg.name.lower(), spec_l)
    return spec_l == msg.name.lower()


def _select_messages(dbc, include: list[str] | None, exclude: list[str] | None) -> dict:
    selected = {}
    for msg in dbc.all_messages:
        if include and not any(_match_spec(s, msg) for s in include):
            continue
        if exclude and any(_match_spec(s, msg) for s in exclude):
            continue
        selected[(msg.frame_id, msg.is_extended)] = msg
    if not selected:
        raise UsageError("报文过滤条件没有匹配到任何 DBC 报文")
    return selected


def _resolve_settings(args) -> dict:
    settings = cfg_mod.resolve(args)
    opts = settings["options"]
    if getattr(args, "enum_mode", None):
        opts["enum_mode"] = args.enum_mode
    if getattr(args, "include_raw", False):
        opts["include_raw_columns"] = True
    if getattr(args, "include_direction", False):
        opts["include_direction"] = True
    if getattr(args, "dump_unknown", False):
        opts["dump_unknown"] = True
    if getattr(args, "no_allow_truncated", False):
        opts["allow_truncated"] = False
    if getattr(args, "prefer_message", None):
        opts["prefer_messages"] = list(args.prefer_message)
    if getattr(args, "quiet", False):
        opts["quiet"] = True
    if not settings["dbc_paths"]:
        raise UsageError("未指定 DBC：请用 --dbc <路径> 或先运行 python -m canlog_tool config --dbc <路径>")
    return settings


def _iter_frames(blf_path: Path):
    """流式读取 BLF，附容器元信息。"""
    try:
        with can.BLFReader(str(blf_path)) as reader:
            meta = {
                "path": str(blf_path),
                "size_bytes": blf_path.stat().st_size,
                "objects": getattr(reader, "object_count", None),
                "start_epoch": getattr(reader, "start_timestamp", None),
                "stop_epoch": getattr(reader, "stop_timestamp", None),
                "uncompressed_size": getattr(reader, "uncompressed_size", None),
            }
            for m in reader:
                yield m, meta
    except BlfError:
        raise
    except Exception as exc:
        raise BlfError(f"{blf_path.name}: {exc}") from exc


def _classify(m, stats: RunStats) -> bool:
    """非 CAN 数据 / 错误帧 / 远程帧只计数，返回 False 表示跳过解码。"""
    if not isinstance(m, can.Message):
        stats.non_can += 1
        return False
    if getattr(m, "is_error_frame", False):
        stats.error_frames += 1
        return False
    if getattr(m, "is_remote_frame", False):
        stats.remote_frames += 1
        return False
    return True


def _record_unknown(m, rel: float, stats: RunStats) -> None:
    key = (m.arbitration_id, bool(m.is_extended_id))
    entry = stats.unknown.setdefault(
        key, {"count": 0, "first": rel, "last": rel, "dlcs": {}, "sample": ""}
    )
    entry["count"] += 1
    entry["last"] = rel
    entry["dlcs"][len(m.data)] = entry["dlcs"].get(len(m.data), 0) + 1
    if not entry["sample"]:
        entry["sample"] = bytes(m.data).hex(" ").upper()


def _update_minmax(stat: MsgStat, entry: MsgEntry, decoded: dict) -> None:
    for sig in entry.signals:
        value = decoded.get(sig.name)
        if value is None:
            continue
        lo = stat.sig_min.get(sig.name)
        if lo is None or value < lo:
            stat.sig_min[sig.name] = value
        hi = stat.sig_max.get(sig.name)
        if hi is None or value > hi:
            stat.sig_max[sig.name] = value


def _scan(reader_factory, dbc, selected: dict, opts: dict, stats: RunStats,
          pool: WriterPool | None, out_data_dir: Path | None) -> None:
    """共享的流式扫描循环。pool 为 None 时只统计不写 CSV（info 模式）。"""
    include_dir = opts.get("include_direction")
    allow_truncated = opts.get("allow_truncated", True)
    enum_mode = opts.get("enum_mode", "label")
    decimals = opts.get("csv_float_decimals")
    time_start, time_end = opts.get("time_start"), opts.get("time_end")
    channel_filter = opts.get("channel_filter")
    dump_unknown = opts.get("dump_unknown") and pool is not None
    unknown_writer = None

    for m, meta in reader_factory():
        if not stats.blf_meta:
            stats.blf_meta = meta  # 容器元信息取自第一条
        if not _classify(m, stats):
            continue
        ts = m.timestamp
        if stats.t0 is None:
            stats.t0 = ts
        rel = ts - stats.t0
        stats.t_end = ts
        stats.frames += 1
        if stats.frames % PROGRESS_EVERY == 0 and not opts.get("quiet"):
            print(f"  已处理 {stats.frames} 帧…", file=sys.stderr)

        channel = getattr(m, "channel", None)
        stats.channels.add(channel)

        # 时间窗/通道过滤：保证整个数据包与所请求窗口一致
        if time_start is not None and rel < time_start:
            stats.filtered += 1
            continue
        if time_end is not None and rel > time_end:
            stats.filtered += 1
            continue
        if channel_filter and channel not in channel_filter:
            stats.filtered += 1
            continue

        key = (m.arbitration_id, bool(m.is_extended_id))
        entry = dbc.get(*key)
        if entry is None or key not in selected:
            if entry is None:
                stats.unknown_frames += 1
                _record_unknown(m, rel, stats)
                if dump_unknown:
                    if unknown_writer is None:
                        unknown_writer = _make_unknown_writer(out_data_dir, include_dir)
                    unknown_writer.write_row(_prefix(m, rel, stats, include_dir),
                                             [id_hex(*key), bytes(m.data).hex(" ").upper()])
            else:
                stats.filtered += 1  # DBC 有但被 --messages/--exclude 过滤
            continue

        if getattr(m, "is_fd", False):
            stats.fd_frames += 1

        data = bytes(m.data)
        stat = stats.messages.get(key)
        if stat is None:
            stat = stats.messages[key] = MsgStat()
        stat.count += 1
        if stat.first_rel is None:
            stat.first_rel = rel
        stat.last_rel = rel

        try:
            decoded = entry.cantools_msg.decode(
                data, decode_choices=False, scaling=True, allow_truncated=allow_truncated
            )
        except DecodeError as exc:
            stat.dec_errors += 1
            if len(stat.err_samples) < ERR_SAMPLE_MAX:
                stat.err_samples.append((rel, data.hex(" ").upper(), str(exc)))
            continue
        if len(data) < entry.length:
            stat.truncated += 1

        _update_minmax(stat, entry, decoded)

        if pool is not None:
            writer = pool.get(key)
            if writer is None:
                writer = _make_writer(entry, out_data_dir, opts)
                pool.add(key, writer)
            writer.write_row(_prefix(m, rel, stats, include_dir),
                             [decoded.get(s.name) for s in entry.signals])

    if unknown_writer is not None:
        unknown_writer.close()


def _prefix(m, rel: float, stats: RunStats, include_dir: bool) -> list[str]:
    prefix = [
        fmt_ts_abs(m.timestamp),
        f"{rel:.6f}",
        str(getattr(m, "channel", "")),
        str(len(m.data)),
    ]
    if include_dir:
        prefix.append("Rx" if getattr(m, "is_rx", True) else "Tx")
    return prefix


def _base_columns(include_dir: bool) -> list[str]:
    cols = ["ts_abs", "ts_rel", "channel", "dlc"]
    if include_dir:
        cols.append("dir")
    return cols


def build_columns(entry: MsgEntry, opts: dict) -> list[str]:
    """CSV 列清单（与 formatters 生成顺序一致），报告也复用。"""
    enum_mode = opts.get("enum_mode", "label")
    include_raw = opts.get("include_raw_columns")
    cols = _base_columns(opts.get("include_direction"))
    for sig in entry.signals:
        cols.append(sig.name)
        if enum_mode == "both" and sig.choices:
            cols.append(f"{sig.name}_lbl")
        if include_raw:
            cols.append(f"{sig.name}_raw")
    return cols


def _make_writer(entry: MsgEntry, out_data_dir: Path, opts: dict) -> MessageCsvWriter:
    sig_formatters = [
        make_signal_formatters(sig, opts.get("enum_mode", "label"),
                               opts.get("include_raw_columns"))
        for sig in entry.signals
    ]
    return MessageCsvWriter(
        path=out_data_dir / csv_filename(entry.frame_id, entry.is_extended, entry.name),
        header=build_columns(entry, opts),
        sig_formatters=sig_formatters,
        decimals=opts.get("csv_float_decimals"),
    )


def _make_unknown_writer(out_data_dir: Path, include_dir: bool) -> MessageCsvWriter:
    header = _base_columns(include_dir) + ["id", "data_hex"]
    return MessageCsvWriter(
        path=out_data_dir / "_unknown.csv",
        header=header,
        sig_formatters=[],
        decimals=None,
    )


def _scan_blf(blf_path: Path, dbc, selected: dict, opts: dict,
              write_csv: bool) -> tuple[RunStats, Path | None, WriterPool | None]:
    """扫描单个 BLF。write_csv 时返回 bundle 目录与 writer 池。"""
    stats = RunStats()
    pool = None
    out_dir = None
    if write_csv:
        out_dir = bundle_dir(
            Path(opts["output_root"]), blf_path.stem,
            datetime.now().strftime("%Y%m%d_%H%M%S"),
        )
        pool = WriterPool()

    def reader_factory():
        return _iter_frames(blf_path)

    _scan(reader_factory, dbc, selected, opts, stats, pool,
          out_dir / "data" if out_dir else None)

    if pool is not None:
        pool.close_all()
    return stats, out_dir, pool


def _print_summary(blf_path: Path, stats: RunStats, dbc, out_dir: Path | None) -> None:
    decoded_total = sum(s.count for s in stats.messages.values())
    print(f"\n[{blf_path.name}]")
    print(f"  帧 {stats.frames}（解码 {decoded_total}，未知 ID {stats.unknown_frames}，"
          f"过滤 {stats.filtered}，错误帧 {stats.error_frames}，远程帧 {stats.remote_frames}，"
          f"非 CAN {stats.non_can}，FD {stats.fd_frames}）")
    print(f"  时长 {stats.duration:.3f}s，通道 {sorted(stats.channels)}")
    print(f"  DBC 报文命中 {len(stats.messages)}/{len(dbc.all_messages)}，"
          f"未知 ID {len(stats.unknown)} 个")
    if out_dir is not None:
        print(f"  输出: {out_dir}")
        print(f"  索引: OVERVIEW.md / MESSAGES.md / SIGNALS.md / ENUMS.md / UNKNOWN_IDS.md")


def run_info(args) -> int:
    settings = _resolve_settings(args)
    opts = settings["options"]
    opts.setdefault("quiet", False)
    dbc = load_dbc(settings["dbc_paths"], prefer_names=opts.get("prefer_messages", []))
    selected = _select_messages(dbc, getattr(args, "messages", None), None)
    if dbc.conflicts:
        print(f"警告: DBC 存在 {len(dbc.conflicts)} 处重复报文 ID"
              f"（{'、'.join(c['id'] for c in dbc.conflicts)}）", file=sys.stderr)
    for blf_path in (Path(p) for p in args.blf):
        if not blf_path.exists():
            raise BlfError(f"文件不存在: {blf_path}")
        stats, _, _ = _scan_blf(blf_path, dbc, selected, opts, write_csv=False)
        _print_summary(blf_path, stats, dbc, None)
    return 0


def run_parse(args) -> int:
    from . import report  # 函数内导入：info 子命令无需报告模块

    settings = _resolve_settings(args)
    opts = settings["options"]
    opts["output_root"] = str(settings["output_root"])
    opts["time_start"] = getattr(args, "start", None)
    opts["time_end"] = getattr(args, "end", None)
    opts["channel_filter"] = set(args.channel) if getattr(args, "channel", None) else None

    dbc = load_dbc(settings["dbc_paths"], prefer_names=opts.get("prefer_messages", []))
    selected = _select_messages(dbc, getattr(args, "messages", None),
                                getattr(args, "exclude", None))
    if dbc.conflicts and not opts.get("quiet"):
        for c in dbc.conflicts:
            print(f"警告: 重复报文 ID {c['id']}: {c['dropped']} 被 {c['keep']} 覆盖"
                  f"（{', '.join(c['files'])}）", file=sys.stderr)

    for blf_path in (Path(p) for p in args.blf):
        if not blf_path.exists():
            raise BlfError(f"文件不存在: {blf_path}")
        stats, out_dir, _ = _scan_blf(blf_path, dbc, selected, opts, write_csv=True)
        # 报告写入（pool.close_all 已在 _scan_blf 中完成）
        csv_files = {
            key: f"data/{csv_filename(key[0], key[1], dbc.get(*key).name)}"
            for key in stats.messages
        }
        report.write_bundle(out_dir, dbc, stats, settings, blf_path, csv_files)
        if not opts.get("quiet"):
            _print_summary(blf_path, stats, dbc, out_dir)

        cfg_mod.append_history(settings["config"], {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "blf": str(blf_path.resolve()),
            "dbc": [str(Path(p).resolve()) for p in settings["dbc_paths"]],
            "bundle": str(out_dir.resolve()),
        })
        cfg_mod.save(settings["config"])
    return 0
