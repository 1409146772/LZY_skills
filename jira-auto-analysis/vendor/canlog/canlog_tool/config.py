"""持久化配置：加载/保存/合并（CLI 参数 > 配置文件 > 内置默认）、历史记录。"""

import copy
import json
from pathlib import Path

# 配置文件与包同级（工具根目录）
CONFIG_PATH = Path(__file__).resolve().parent.parent / "canlog_config.json"
HISTORY_MAX = 20

DEFAULT_CONFIG = {
    "version": 1,
    "default_dbc": [],
    "output_root": str(Path(__file__).resolve().parent.parent / "output"),
    "options": {
        "enum_mode": "label",
        "include_raw_columns": False,
        "include_direction": False,
        "unknown_id_report": True,
        "dump_unknown": False,
        "allow_truncated": True,
        "prefer_messages": [],
        "csv_float_decimals": None,
    },
    "history": [],
}


def load() -> dict:
    """读配置，缺失键用默认补齐（前向兼容），未知键保留。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cfg
        for key in ("version", "default_dbc", "output_root", "options", "history"):
            if key in stored:
                if key == "options" and isinstance(stored[key], dict):
                    cfg["options"].update(stored[key])
                else:
                    cfg[key] = stored[key]
        # 保留无法识别的顶层键
        for key, value in stored.items():
            if key not in cfg:
                cfg[key] = value
    return cfg


def save(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def resolve(args) -> dict:
    """按 CLI 参数 > 配置文件 > 内置默认 合并出本次运行的设置。

    命令行 --dbc 是替换（不与配置中的 default_dbc 合并）。
    """
    cfg = load()
    settings = {
        "dbc_paths": list(getattr(args, "dbc", None) or cfg["default_dbc"]),
        "output_root": Path(
            getattr(args, "out", None) or cfg["output_root"]
        ),
        "options": dict(cfg["options"]),
        "config": cfg,
    }
    opts = getattr(args, "set_options", None) or {}
    settings["options"].update({k: v for k, v in opts.items() if v is not None})
    return settings


def append_history(cfg: dict, entry: dict) -> dict:
    """成功 parse 后追加一条历史（最新在前，最多 HISTORY_MAX 条）。不落盘，由调用方 save。"""
    history = cfg.setdefault("history", [])
    history.insert(0, entry)
    del history[HISTORY_MAX:]
    return cfg
