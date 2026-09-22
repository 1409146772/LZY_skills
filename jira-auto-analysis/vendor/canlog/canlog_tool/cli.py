"""CLI 入口：argparse 定义、config/args 合并、异常→退出码。无业务逻辑。"""

import argparse
import json
import sys

from . import __version__, config as cfg_mod


def _setup_stdio() -> None:
    """Windows 控制台默认 cp936，打印 ℃/° 会崩，强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _parse_kv(pairs: list[str] | None) -> dict:
    """--set KEY=VALUE 解析：尽量转成 bool/int/float/None，失败则保留字符串。"""
    result: dict = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"错误: --set 需要 KEY=VALUE 格式，收到: {item}")
        key, _, raw = item.partition("=")
        try:
            result[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            result[key.strip()] = raw
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canlog_tool",
        description="BLF(CAN 日志) + DBC → 供 AI 导航的数据包（Markdown 索引 + 按报文 CSV）",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    common_dbc = argparse.ArgumentParser(add_help=False)
    common_dbc.add_argument(
        "--dbc",
        action="append",
        metavar="PATH",
        help="DBC 文件路径（可重复；缺省时用 config 中保存的 default_dbc）。命令行给出时替换配置而非追加",
    )

    p_parse = sub.add_parser(
        "parse", parents=[common_dbc], help="解码 BLF，生成完整数据包（索引表 + CSV）"
    )
    p_parse.add_argument("blf", nargs="+", metavar="BLF", help="BLF 文件路径（可多个）")
    p_parse.add_argument("--out", metavar="DIR", help="输出根目录（默认取配置 output_root）")
    p_parse.add_argument(
        "--messages", action="append", metavar="SPEC",
        help="只处理指定报文：名称、通配(如 IHU_*)或 ID(0x0A0/160)；可重复",
    )
    p_parse.add_argument(
        "--exclude", action="append", metavar="SPEC", help="排除报文，规则同 --messages；可重复"
    )
    p_parse.add_argument("--start", type=float, metavar="S", help="起始时间（相对首帧，秒）")
    p_parse.add_argument("--end", type=float, metavar="S", help="结束时间（相对首帧，秒）")
    p_parse.add_argument("--channel", type=int, action="append", metavar="N", help="只保留指定通道")
    p_parse.add_argument(
        "--enum-mode", choices=["label", "number", "both"], metavar="MODE",
        help="枚举信号输出方式：label=标签文本 / number=数值 / both=两列",
    )
    p_parse.add_argument("--include-raw", action="store_true", help="追加 <信号>_raw 原始值列")
    p_parse.add_argument("--include-direction", action="store_true", help="追加 dir (Rx/Tx) 列")
    p_parse.add_argument("--dump-unknown", action="store_true", help="把未知 ID 的原始数据也写成 _unknown.csv")
    p_parse.add_argument(
        "--prefer-message", action="append", metavar="NAME",
        help="DBC 存在重复报文 ID 时，指定以哪个报文名为准；可重复",
    )
    p_parse.add_argument(
        "--no-allow-truncated", action="store_true",
        help="严格模式：帧长不足报文长度时计为解码错误（默认宽松，缺失信号留空）",
    )
    p_parse.add_argument("--quiet", action="store_true", help="不打印进度与摘要")

    p_info = sub.add_parser(
        "info", parents=[common_dbc], help="快速扫描 BLF（不写任何文件），stdout 输出统计"
    )
    p_info.add_argument("blf", nargs="+", metavar="BLF")
    p_info.add_argument("--messages", action="append", metavar="SPEC")

    p_signals = sub.add_parser("signals", help="按名称/通配查找 DBC 中的信号")
    p_signals.add_argument("pattern", metavar="PATTERN", help="信号名子串或通配（如 SAS_1_*）")
    p_signals.add_argument("--dbc", action="append", metavar="PATH")
    p_signals.add_argument(
        "--bundle", metavar="DIR", help="可选：同时给出历史数据包中对应 CSV 的绝对路径与行数"
    )

    p_config = sub.add_parser("config", help="查看/设置持久化配置")
    p_config.add_argument("--dbc", action="append", metavar="PATH", help="保存默认 DBC（可重复，整体替换）")
    p_config.add_argument("--out", metavar="DIR", help="保存默认输出根目录")
    p_config.add_argument("--set", action="append", metavar="KEY=VALUE", help="设置 options 键，如 enum_mode=both")
    p_config.add_argument("--show", action="store_true", help="显示当前生效配置")
    p_config.add_argument("--reset", action="store_true", help="恢复全部默认配置")
    p_config.add_argument("--history", action="store_true", help="显示最近解析记录")

    return parser


def _cmd_signals(args) -> int:
    from pathlib import Path

    from . import dbc, paths

    settings = cfg_mod.resolve(args)
    if not settings["dbc_paths"]:
        print("错误: 未指定 DBC（--dbc 或先运行 config --dbc <路径>）", file=sys.stderr)
        return 3
    bundle = dbc.load_dbc(settings["dbc_paths"], prefer_names=[])
    matches = bundle.find_signals(args.pattern)
    if not matches:
        print(f"未找到匹配 {args.pattern!r} 的信号")
        return 0

    bundle_csv = None
    if getattr(args, "bundle", None):
        bdir = Path(args.bundle)
        if (bdir / "bundle.json").exists():
            bundle_csv = json.loads((bdir / "bundle.json").read_text(encoding="utf-8"))

    print(f"{'信号':<30} {'报文':<20} {'ID':<12} {'起始':>4} {'位长':>4} {'因子':<12} {'偏移':<10} 单位")
    for msg_entry, sig in matches:
        print(
            f"{sig.name:<30} {msg_entry.name:<20} {paths.id_hex(msg_entry.frame_id, msg_entry.is_extended):<12} "
            f"{sig.start:>4} {sig.length:>4} {sig.scale:<12g} {sig.offset:<10g} {sig.unit or ''}"
        )
        if bundle_csv is not None:
            for m in bundle_csv.get("messages", []):
                if m["name"] == msg_entry.name and sig.name in m.get("columns", []):
                    csv_path = bdir / m["csv"]
                    rows = sum(1 for _ in csv_path.open(encoding="utf-8")) - 1
                    print(f"    → {csv_path.resolve()}  ({rows} 行)")
    print(f"\n共 {len(matches)} 个匹配信号")
    return 0


def _cmd_config(args) -> int:
    cfg = cfg_mod.load()
    if args.reset:
        cfg = cfg_mod.DEFAULT_CONFIG.copy()
        cfg_mod.save(cfg)
        print("已恢复默认配置")
    if args.dbc:
        cfg["default_dbc"] = [str(p) for p in args.dbc]
    if args.out:
        cfg["output_root"] = str(args.out)
    if args.set:
        cfg["options"].update(_parse_kv(args.set))
    if args.dbc or args.out or args.set:
        cfg_mod.save(cfg)
        print(f"配置已保存到 {cfg_mod.CONFIG_PATH}")

    if args.show or not (args.dbc or args.out or args.set or args.reset or args.history):
        print(f"配置文件: {cfg_mod.CONFIG_PATH}")
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
    if args.history:
        print(f"\n最近 {len(cfg.get('history', []))} 次解析:")
        for h in cfg.get("history", []):
            print(f"  {h.get('time', '?')}  {h.get('blf', '?')}")
            print(f"      → {h.get('bundle', '?')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)
    args.set_options = _parse_kv(getattr(args, "set", None))

    from .errors import BlfError, DbcError, UsageError

    try:
        if args.command == "parse":
            from .pipeline import run_parse
            return run_parse(args)
        if args.command == "info":
            from .pipeline import run_info
            return run_info(args)
        if args.command == "signals":
            return _cmd_signals(args)
        if args.command == "config":
            return _cmd_config(args)
        return 2
    except UsageError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except DbcError as exc:
        print(f"错误: DBC 加载失败 — {exc}", file=sys.stderr)
        return 3
    except BlfError as exc:
        print(f"错误: BLF 读取失败 — {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
