"""CSV 输出：按报文一个文件，懒打开、增量写、LRU 限制同时打开的文件数。"""

import csv
from collections import OrderedDict
from pathlib import Path

FLUSH_EVERY = 5000
MAX_OPEN_FILES = 256


def _fmt_number(value, decimals: int | None) -> str:
    """浮点用最短往返 repr（保住 6e-05 这类小因子）；指定 decimals 时先四舍五入。"""
    if decimals is not None:
        value = round(value, decimals)
    return repr(value)


def _raw_of(sig, value: float) -> int | None:
    """由物理值反推原始值（枚举匹配/raw 列用），不可靠时返回 None。"""
    if not sig.scale:
        return None
    raw = round((value - sig.offset) / sig.scale)
    if abs(raw * sig.scale + sig.offset - value) <= 1e-9 * max(1.0, abs(value)):
        return raw
    return None


def make_signal_formatters(sig, enum_mode: str, include_raw: bool) -> list:
    """为单个信号生成一组列格式化函数，每个函数对应 CSV 中一列。

    返回列表长度 = 该信号占的列数：主列(必有)、_lbl 列(both 模式且有枚举)、_raw 列(可选)。
    统一签名 fn(value, decimals) -> str。
    """
    choices_by_phys: dict[float, str] = {}
    if sig.choices:
        for raw, label in sig.choices.items():
            choices_by_phys[sig.decode_phys(raw)] = label
    choices_by_raw = dict(sig.choices)
    integral = sig.is_integral

    def label_of(value) -> str | None:
        label = choices_by_phys.get(value)
        if label is None:
            raw = _raw_of(sig, value)
            if raw is not None:
                label = choices_by_raw.get(raw)
        return label

    def main_col(value, decimals) -> str:
        if value is None:
            return ""
        if enum_mode == "label" and choices_by_phys:
            label = label_of(value)
            if label is not None:
                return label
        if integral:
            return str(int(value))
        return _fmt_number(value, decimals)

    fns = [main_col]

    if enum_mode == "both" and choices_by_phys:
        def label_col(value, decimals) -> str:
            return label_of(value) or ""

        fns.append(label_col)

    if include_raw:
        def raw_col(value, decimals) -> str:
            if value is None:
                return ""
            raw = _raw_of(sig, value)
            return "" if raw is None else str(raw)

        fns.append(raw_col)

    return fns


class MessageCsvWriter:
    """单报文 CSV 写入器，首行数据到达时才打开文件；被淘汰后重开为追加模式。"""

    def __init__(self, path: Path, header: list[str], sig_formatters: list,
                 decimals: int | None):
        self.path = path
        self.header = header
        self.sig_formatters = sig_formatters  # 与信号值列表对齐：每信号一组列函数
        self.decimals = decimals
        self._fp = None
        self._writer = None
        self._since_flush = 0
        self._opened_once = False
        self.row_count = 0

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 首次写覆盖；被 LRU 淘汰后重开必须追加，否则截断已写数据
        mode = "a" if self._opened_once else "w"
        self._fp = self.path.open(mode, encoding="utf-8", newline="")
        self._writer = csv.writer(self._fp, lineterminator="\n")
        if not self._opened_once:
            self._writer.writerow(self.header)
            self._opened_once = True

    def write_row(self, prefix: list[str], values: list) -> None:
        """prefix = [ts_abs, ts_rel, channel, dlc(, dir)]；values 与信号一一对应。"""
        if self._writer is None:
            self._open()
        row = list(prefix)
        if not self.sig_formatters:  # 简单写入器（如 _unknown.csv）：值列直写
            row.extend(str(v) for v in values)
        else:
            decimals = self.decimals
            for value, fns in zip(values, self.sig_formatters):
                for fn in fns:
                    row.append(fn(value, decimals))
        self._writer.writerow(row)
        self.row_count += 1
        self._since_flush += 1
        if self._since_flush >= FLUSH_EVERY:
            self._fp.flush()
            self._since_flush = 0

    def close(self) -> None:
        if self._fp is not None:
            self._fp.flush()
            self._fp.close()
            self._fp = None
            self._writer = None


class WriterPool:
    """管理全部报文写入器：LRU 关闭最久未用的文件，控制同时打开数。"""

    def __init__(self):
        self._live: OrderedDict = OrderedDict()

    def add(self, key, writer: MessageCsvWriter) -> None:
        self._live[key] = writer
        self._evict_if_needed()

    def get(self, key) -> MessageCsvWriter | None:
        writer = self._live.get(key)
        if writer is not None:
            self._live.move_to_end(key)
            self._evict_if_needed()
        return writer

    def _evict_if_needed(self) -> None:
        while len(self._live) > MAX_OPEN_FILES:
            _, oldest = self._live.popitem(last=False)
            oldest.close()  # 关闭文件但保留对象；下次 write_row 以追加模式重开

    def close_all(self) -> None:
        for writer in self._live.values():
            writer.close()
        self._live.clear()
