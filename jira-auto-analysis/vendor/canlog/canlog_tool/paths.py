"""路径与文件名处理：Windows 非法字符消毒、CSV 命名、bundle 目录防撞。"""

import re
from pathlib import Path

# Windows 文件名非法字符 + 控制字符
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_NAME = 60


def sanitize(name: str) -> str:
    """把报文/信号名转成安全的文件名片段。"""
    cleaned = _UNSAFE.sub("_", str(name)).strip().rstrip(".")
    if not cleaned:
        cleaned = "unnamed"
    if len(cleaned) > _MAX_NAME:
        cleaned = cleaned[:_MAX_NAME]
    return cleaned


def id_hex(frame_id: int, is_extended: bool) -> str:
    """报文 ID 的 hex 渲染：标准 3 位、扩展 8 位。"""
    return f"0x{frame_id:08X}" if is_extended else f"0x{frame_id:03X}"


def csv_filename(frame_id: int, is_extended: bool, msg_name: str) -> str:
    return f"{id_hex(frame_id, is_extended)}_{sanitize(msg_name)}.csv"


def bundle_dir(output_root: Path, blf_stem: str, when: str) -> Path:
    """创建 output/<blf名>_<时间戳> 目录，同秒重跑时追加 _b2/_b3。"""
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    base = f"{sanitize(blf_stem)}_{when}"
    candidate = root / base
    n = 2
    while candidate.exists():
        candidate = root / f"{base}_b{n}"
        n += 1
    candidate.mkdir(parents=True)
    return candidate
