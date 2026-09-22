"""DBC 加载：编码探测、cantools strict=False、自建 FrameMap、冲突检测、信号元数据。"""

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path

import cantools

from .errors import DbcError
from .paths import id_hex

# 编码探测顺序：带 BOM 的 UTF-8 → UTF-8 → GBK（国内 CAN 矩阵常见）→ cp1252
_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "cp1252")


def detect_encoding(path: Path) -> str:
    """读原始字节依次尝试解码，返回第一个成功的编码名。"""
    raw = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    raise DbcError(f"{path}: 无法识别文件编码")


def _raw_range(sig) -> tuple[int, int]:
    """cantools 不直接给 raw 范围，按位长/符号性自算。"""
    if sig.is_float:
        return (0, 0)  # 浮点无意义，标记为无
    span = 1 << sig.length
    return (-(span >> 1), (span >> 1) - 1) if sig.is_signed else (0, span - 1)


@dataclass
class SignalInfo:
    name: str
    start: int
    length: int
    byte_order: str          # 'big_endian' / 'little_endian'
    scale: float
    offset: float
    unit: str
    phys_min: float | None   # DBC 声明的物理范围
    phys_max: float | None
    raw_min: int
    raw_max: int
    is_signed: bool
    is_float: bool
    choices: dict[int, str] = field(default_factory=dict)  # 原始值 → 标签
    is_multiplexer: bool = False
    multiplexer_ids: object = None

    @property
    def is_integral(self) -> bool:
        """整数物理值（scale/offset 均为整数且非浮点信号）→ CSV 写 int。"""
        return (
            not self.is_float
            and float(self.scale).is_integer()
            and float(self.offset).is_integer()
        )

    def decode_phys(self, raw: int) -> float | int:
        return raw * self.scale + self.offset


@dataclass
class MsgEntry:
    name: str
    frame_id: int
    is_extended: bool
    length: int
    cycle_time: int | None
    senders: list[str]
    signals: list[SignalInfo]
    source: str  # 来源 DBC 文件名
    cantools_msg: object = field(default=None, repr=False, compare=False)  # 供 decode 用

    @property
    def id_str(self) -> str:
        return id_hex(self.frame_id, self.is_extended)


@dataclass
class DbcBundle:
    by_id: dict[tuple[int, bool], MsgEntry]
    all_messages: list[MsgEntry]           # 按 (frame_id, is_extended) 排序
    conflicts: list[dict]                  # 重复 ID 冲突记录
    dup_signal_names: dict[str, list[str]]  # 跨报文同名信号 → 报文名列表
    encodings: dict[str, str]              # DBC 文件名 → 编码

    def get(self, frame_id: int, is_extended: bool) -> MsgEntry | None:
        return self.by_id.get((frame_id, is_extended))

    def find_signals(self, pattern: str) -> list[tuple[MsgEntry, SignalInfo]]:
        """按子串或通配匹配信号名（不区分大小写）。"""
        pat = pattern.lower()
        has_wild = any(c in pat for c in "*?[")
        matches = []
        for msg in self.all_messages:
            for sig in msg.signals:
                hit = (
                    fnmatch.fnmatch(sig.name.lower(), pat)
                    if has_wild
                    else pat in sig.name.lower()
                )
                if hit:
                    matches.append((msg, sig))
        return matches


def _to_signal_info(sig) -> SignalInfo:
    choices: dict[int, str] = {}
    if sig.choices:
        for raw, text in sig.choices.items():
            try:
                choices[int(raw)] = str(text)
            except (TypeError, ValueError):
                continue
    rmin, rmax = _raw_range(sig)
    return SignalInfo(
        name=sig.name,
        start=sig.start,
        length=sig.length,
        byte_order=sig.byte_order,
        scale=sig.scale,
        offset=sig.offset,
        unit=sig.unit or "",
        phys_min=sig.minimum,
        phys_max=sig.maximum,
        raw_min=rmin,
        raw_max=rmax,
        is_signed=sig.is_signed,
        is_float=sig.is_float,
        choices=choices,
        is_multiplexer=bool(sig.is_multiplexer),
        multiplexer_ids=sig.multiplexer_ids,
    )


def load_dbc(paths, prefer_names: list[str] | None = None) -> DbcBundle:
    """加载一个或多个 DBC，合并成统一的 FrameMap。

    重复 ID：后载入的胜出（与 cantools 行为一致）；
    若 --prefer-message 指定了报文名，则该名字优先。
    """
    prefer = {name.lower() for name in (prefer_names or [])}
    by_id: dict[tuple[int, bool], MsgEntry] = {}
    conflicts: list[dict] = []
    name_seen: dict[str, str] = {}
    dup_signal_names: dict[str, list[str]] = {}
    encodings: dict[str, str] = {}

    for path_str in paths:
        path = Path(path_str)
        if not path.exists():
            raise DbcError(f"文件不存在: {path}")
        enc = detect_encoding(path)
        encodings[path.name] = enc
        # 重复 ID 由我们自己的 FrameMap 检测并报告，屏蔽 cantools 的重复告警避免刷屏
        logging.getLogger("cantools").setLevel(logging.ERROR)
        try:
            db = cantools.database.load_file(str(path), strict=False, encoding=enc)
        except Exception as exc:  # cantools 抛错种类多，统一归为 DbcError
            raise DbcError(f"{path.name}: {exc}") from exc

        for msg in db.messages:
            entry = MsgEntry(
                name=msg.name,
                frame_id=msg.frame_id,
                is_extended=bool(msg.is_extended_frame),
                length=msg.length,
                cycle_time=msg.cycle_time,
                senders=list(msg.senders or []),
                signals=[_to_signal_info(s) for s in msg.signals],
                source=path.name,
                cantools_msg=msg,
            )
            key = (entry.frame_id, entry.is_extended)
            if key in by_id:
                existing = by_id[key]
                winner = (
                    entry if entry.name.lower() in prefer
                    else existing if existing.name.lower() in prefer
                    else entry  # 后载入优先
                )
                conflicts.append({
                    "id": entry.id_str,
                    "keep": winner.name,
                    "dropped": existing.name if winner is entry else entry.name,
                    "files": [existing.source, entry.source],
                    "winner_is_new": winner is entry,
                    "new_entry": entry,
                    "existing_entry": existing,
                })
                by_id[key] = winner
            else:
                by_id[key] = entry

            # 跨报文同名信号统计（供 OVERVIEW 提示，解码不受影响）
            for sig in entry.signals:
                prev = name_seen.get(sig.name)
                if prev and prev != entry.name:
                    dup_signal_names.setdefault(sig.name, [prev])
                    if entry.name not in dup_signal_names[sig.name]:
                        dup_signal_names[sig.name].append(entry.name)
                name_seen.setdefault(sig.name, entry.name)

    if not by_id:
        raise DbcError("没有加载到任何报文定义")

    all_messages = sorted(by_id.values(), key=lambda m: (m.frame_id, m.is_extended))
    return DbcBundle(
        by_id=by_id,
        all_messages=all_messages,
        conflicts=conflicts,
        dup_signal_names=dup_signal_names,
        encodings=encodings,
    )
