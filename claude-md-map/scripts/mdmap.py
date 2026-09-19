#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mdmap.py — claude-md-map skill 唯一脚本(子命令式,stdlib-only)。

子命令:
  detect          判定 INIT / ADOPT / UPDATE / NOOP(并可建工作区)
  scan            扫描目录统计 → tmp/scan.json(stdout ≤30 行摘要)
  plan            聚类模块 → tmp/modules.json(打印划分表)
  brief           生成模块 worker 的 brief 文件(打印路径)
  detect-changes  变更检测 + 模块映射 → tmp/changes.json
  lint            草稿质量闸(死路径/超预算/重复行/空章节/地图对应)
  report          生成用户确认报告(stdout)
  install         草稿落盘 + 备份 + 刷新 state.json

约定:
- 所有相对路径 POSIX 正斜杠;模块 = 一组路径前缀,文件→模块用最长前缀匹配
- stdout 默认人类表格,--json 输出机器 JSON;除 report 外 ≤200 行
- 退出码:0 成功 / 1 lint 不合格 / 2 用法错误 / 3 not-found / 4 需用户决策
- 模块互不重叠按"前缀集合"理解;嵌套前缀(拆分场景)由最长前缀匹配消解
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------- 硬编码常量(不要改) ----------------
ROOT_BUDGET = 60            # 根 CLAUDE.md 行数预算
MODULE_BUDGET = 80          # 模块 CLAUDE.md 行数预算
MIN_MODULES = 4
MAX_MODULES_DEFAULT = 80
MIN_SOURCE_FILES = 5        # 少于此源码文件数 → 非代码工程
SMALL_DIR_FILES = 8         # 顶层目录源码文件少于此数且无包标记 → 并入分组
SPLIT_FILES = 400           # 超过则按二级子目录再切
SPLIT_LOC = 15_000
SPLIT_CHILD_FILES = 2       # 递归拆分阈值: 目录直接源码文件数≥此值 或 含≥2个子模块(路由容器) 才独立成模块
COARSE_TOP = {"RTD", "generate", "board", ".metadata", "Project_Settings"}  # 供应商/生成/构建/配置目录:不深拆
VENDOR_SUBTREE_SUFFIX = ("/Source",)  # 业务树内厂商内核子树(如 OS1_FreeRTOS/Source):只发一层不深拆
SPLIT_MAX_CHILDREN = 8
NEW_DIR_FILES = 15          # 新目录成为新模块的门槛
GROUP_MAX_DIRS = 4          # 小目录分组上限
GROUP_MAX_FILES = 30
CHURN_DAYS = 90
BRIEF_FILE_CAP = 200        # brief 文件清单上限
MAX_WALK_FILES = 50_000     # 超过 → approx 抽样模式
LOC_READ_CAP_PER_DIR = 400  # 每目录 LOC 实读文件数上限
BIG_FILE_BYTES = 1_000_000  # 超过则不计 LOC

EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".claude", ".claude-md-map", ".agents", ".github", ".gitlab",
    "node_modules", "bower_components", "dist", "dist-newstyle", "build", "out", "target",
    "vendor", "vendored", "__pycache__", ".venv", "venv", "env", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "htmlcov", "coverage",
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache", ".angular", ".output",
    "site-packages", ".gradle", ".mvn", ".idea", ".vscode", ".vs", "obj", "bin",
    "Pods", "DerivedData", ".terraform", ".serverless", ".stack-work", ".cabal",
    "deps", "_build", ".elixir_ls", ".expo", ".cache", ".dart_tool", "Debug_FLASH",
}
EXCLUDED_FILE_NAMES = {
    ".ds_store", "thumbs.db", "desktop.ini",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock",
    "poetry.lock", "pipfile.lock", "cargo.lock", "go.sum", "composer.lock",
    "gemfile.lock", "mix.lock", "flake.lock", "gradle.lockfile", "uv.lock",
    "packages.lock.json", "project.assets.json",
}
EXCLUDED_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".icns", ".tif", ".tiff",
    ".svg", ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".jar",
    ".exe", ".dll", ".so", ".dylib", ".a", ".lib", ".obj", ".bin", ".iso", ".msi",
    ".class", ".war", ".pyc", ".pyo", ".pyd", ".wasm", ".node",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".avi", ".mkv", ".mov", ".wav", ".flac", ".ogg", ".webm",
    ".db", ".sqlite", ".sqlite3", ".mdb", ".accdb", ".pdb",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
    ".csv", ".tsv", ".parquet", ".ipynb", ".lock", ".sum", ".map",
    ".o", ".elf", ".axf", ".hex", ".bin", ".d", ".lst", ".crf", ".lnp",
}
SOURCE_EXTS = {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".cs",
    ".java", ".kt", ".kts", ".go", ".rs", ".rb", ".php", ".swift", ".m", ".mm",
    ".scala", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd",
    ".sql", ".r", ".lua", ".pl", ".pm", ".dart", ".vue", ".svelte", ".astro",
    ".proto", ".graphql", ".gql", ".gradle", ".groovy", ".clj", ".cljs", ".edn",
    ".ex", ".exs", ".erl", ".hrl", ".hs", ".zig", ".nim", ".jl",
    ".f90", ".f95", ".vhdl", ".v", ".sv", ".sol", ".tf", ".hcl", ".asm", ".s",
}
PACKAGE_MARKERS = {
    "package.json", "deno.json", "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "go.mod", "cargo.toml", "pom.xml", "build.gradle",
    "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gemfile",
    "composer.json", "mix.exs", "cmakelists.txt", "meson.build", "pubspec.yaml",
    "package.swift", "stack.yaml",
}
ROOT_CONFIG_RE = re.compile(
    r"^(package\.json|pnpm-workspace\.yaml|turbo\.json|nx\.json|lerna\.json|rush\.json"
    r"|cargo\.toml|go\.mod|pyproject\.toml|setup\.py|setup\.cfg|requirements.*\.txt"
    r"|pom\.xml|build\.gradle(\.kts)?|settings\.gradle(\.kts)?|makefile"
    r"|dockerfile.*|docker-compose.*\.ya?ml|\.env\.example|tsconfig.*\.json"
    r"|.*\.config\.(js|ts|mjs|cjs)|jest\.config.*|vitest\.config.*"
    r"|playwright\.config.*|babel\.config.*|\.gitlab-ci\.yml|azure-pipelines\.yml"
    r"|tox\.ini|noxfile\.py|pixi\.toml)$"
)
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
CLAUDE_MD_NAMES = ("claude.md",)  # 大小写不敏感比较用


def out(msg: str = "") -> None:
    print(msg)


def die(code: int, msg: str):
    print(f"ERROR({code}): {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------- 基础工具 ----------------
def to_posix(p) -> str:
    return str(p).replace("\\", "/").rstrip("/")


def norm(s: str) -> str:
    """大小写不敏感匹配用的归一化(Windows 文件系统)。"""
    return s.replace("\\", "/").rstrip("/").casefold()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def repo_root(explicit) -> Path:
    root = Path(explicit).resolve() if explicit else Path.cwd().resolve()
    if not root.exists() or not root.is_dir():
        die(2, f"目录不存在: {root}")
    home = Path.home().resolve()
    if root == home or root == home.parent or root.parent == root:
        die(4, f"{root} 不是代码工程(home 或盘根)。用 --repo 指定工程根目录。")
    return root


def is_git(root: Path) -> bool:
    d = root
    for _ in range(10):
        if (d / ".git").exists():
            return True
        if d.parent == d:
            break
        d = d.parent
    return False


def git(root: Path, *args: str):
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_head(root: Path):
    s = git(root, "rev-parse", "HEAD")
    return s.strip() if s else None


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return s or "mod"


def workspace_of(root: Path) -> Path:
    return root / ".claude-md-map"


def ensure_workspace(root: Path, state_local: bool = False) -> Path:
    ws = workspace_of(root)
    (ws / "tmp" / "draft").mkdir(parents=True, exist_ok=True)
    gi = ws / ".gitignore"
    # output/ 放统一报告(OutPut_YYYYMMDD.html),属本机运行产物,不入库。
    # gitignore 无 __contains__ 语义,这里语义上等同于 ensure_line,整块确保存在。
    required = ["tmp/", "output/"]
    if state_local:
        required.append("state.json")
    if not gi.exists():
        gi.write_text("\n".join(required) + "\n", encoding="utf-8", newline="\n")
    else:
        existing = gi.read_text(encoding="utf-8").splitlines()
        missing = [ln for ln in required if ln not in existing]
        if missing:
            gi.write_text(
                "\n".join(existing + missing) + "\n", encoding="utf-8", newline="\n"
            )
    return ws


def find_claude_md(directory: Path):
    if not directory.is_dir():
        return None
    for child in directory.iterdir():
        if child.is_file() and child.name.casefold() in CLAUDE_MD_NAMES:
            return child
    return None


def read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def count_source_files(root: Path, cap: int = 5000) -> int:
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for f in filenames:
            if Path(f).suffix.casefold() in SOURCE_EXTS and f.casefold() not in EXCLUDED_FILE_NAMES:
                n += 1
                if n >= cap:
                    return n
    return n


def module_hash(root: Path, rel_path: str) -> str:
    p = root / rel_path
    if not p.exists():
        return ""
    if is_git(root):
        h = git(root, "hash-object", str(p))
        if h:
            return "git:" + h.strip()
    data = p.read_bytes()
    return "sha256:" + hashlib.sha256(data).hexdigest()[:32]


# ---------------- scan ----------------
def walk_source_files(root: Path):
    """返回 ([{rel, top, second, size}], approx)。"""
    files: list = []
    approx = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in EXCLUDED_DIRS and not d.endswith(".egg-info")
        )
        rel_dir = to_posix(os.path.relpath(dirpath, root))
        if rel_dir == ".":
            rel_dir = ""
        for f in sorted(filenames):
            fl = f.casefold()
            if fl in EXCLUDED_FILE_NAMES or fl.endswith(".min.js") or fl.endswith(".min.css"):
                continue
            ext = Path(f).suffix.casefold()
            if ext not in SOURCE_EXTS:
                continue
            if rel_dir == "" and fl in CLAUDE_MD_NAMES:
                continue
            rel = f"{rel_dir}/{f}" if rel_dir else f
            try:
                size = os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                size = 0
            parts = rel.split("/")
            files.append({
                "rel": rel,
                "top": parts[0] if len(parts) > 1 else "",
                "second": "/".join(parts[:2]) if len(parts) > 2 else "",
                "size": size,
            })
            if len(files) >= MAX_WALK_FILES:
                return files, True
    return files, approx


def loc_of(root: Path, rel: str, size: int):
    if size > BIG_FILE_BYTES:
        return None
    try:
        with open(root / rel, "rb") as fh:
            return fh.read().count(b"\n") + 1
    except OSError:
        return None


def git_churn(root: Path) -> dict:
    log = git(root, "log", f"--since={CHURN_DAYS}.days", "--name-only", "--pretty=format:")
    if not log:
        return {}
    churn: dict = {}
    for line in log.splitlines():
        line = line.strip()
        if not line:
            continue
        rel = to_posix(line)
        top = rel.split("/")[0] if "/" in rel else ""
        churn[top] = churn.get(top, 0) + 1
    return churn


def _package_markers(root: Path) -> set:
    """顶层目录含包标记的集合(walk 限定深度 2)。"""
    hits: set = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel = to_posix(os.path.relpath(dirpath, root))
        depth = 0 if rel == "." else rel.count("/") + 1
        if depth > 1:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                       if d not in EXCLUDED_DIRS and not d.endswith(".egg-info")]
        for f in filenames:
            fl = f.casefold()
            if fl in PACKAGE_MARKERS or fl.endswith((".csproj", ".sln", ".fsproj", ".vcxproj")):
                if rel == ".":
                    continue
                hits.add(rel.split("/")[0])
    return hits


def scan_cmd(args) -> int:
    root = repo_root(args.repo)
    files, approx = walk_source_files(root)
    total = len(files)
    if total < MIN_SOURCE_FILES and not args.force:
        die(4, f"源码文件仅 {total} 个(<{MIN_SOURCE_FILES}),不像代码工程。"
               f"确认目录无误可用 --force 重跑。")
    churn = {} if approx else git_churn(root)
    vcs = "git" if is_git(root) else "none"

    tops: dict = {}
    root_files = 0
    for fi in files:
        top = fi["top"]
        if top == "":
            root_files += 1
            continue
        t = tops.setdefault(top, {
            "files": 0, "loc": 0, "loc_capped": False,
            "second": {}, "read": 0,
        })
        t["files"] += 1
        sec = fi["second"]
        if sec:
            s = t["second"].setdefault(sec, {"dir": sec, "files": 0, "loc": 0})
            s["files"] += 1
        if t["read"] < LOC_READ_CAP_PER_DIR and fi["size"] <= BIG_FILE_BYTES:
            t["read"] += 1
            n = loc_of(root, fi["rel"], fi["size"])
            if n is None:
                t["loc_capped"] = True
            else:
                t["loc"] += n
                if sec in t["second"]:
                    t["second"][sec]["loc"] += n
        if t["read"] >= LOC_READ_CAP_PER_DIR:
            t["loc_capped"] = True

    marker_hits = _package_markers(root)
    top_stats = []
    for name, t in tops.items():
        subs = sorted(t["second"].values(), key=lambda s: -s["files"])[:20]
        top_stats.append({
            "dir": name, "files": t["files"], "loc": t["loc"],
            "loc_approx": t["loc_capped"] or approx,
            "package_marker": name in marker_hits,
            "has_claude_md": find_claude_md(root / name) is not None,
            "churn": churn.get(name, 0),
            "subdirs": subs,
        })
    top_stats.sort(key=lambda x: -(x["files"] * (1 + x["churn"])))
    root_config = sorted(
        p.name for p in root.iterdir()
        if p.is_file() and ROOT_CONFIG_RE.match(p.name.casefold())
    ) if root.is_dir() else []

    scan = {
        "generated_at": now_iso(), "repo": str(root), "vcs": vcs, "approx": approx,
        "total_source_files": total, "root_files": root_files,
        "root_config": root_config, "churn_days": CHURN_DAYS,
        "top_dirs": top_stats,
    }
    out_path = Path(args.out) if args.out else workspace_of(root) / "tmp" / "scan.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(scan, ensure_ascii=False, indent=1),
                        encoding="utf-8", newline="\n")

    if args.json:
        print(json.dumps({k: scan[k] for k in (
            "repo", "vcs", "approx", "total_source_files", "root_files")},
            ensure_ascii=False))
        print(json.dumps([{
            "dir": t["dir"], "files": t["files"], "loc": t["loc"],
            "marker": t["package_marker"], "md": t["has_claude_md"],
            "churn": t["churn"]} for t in top_stats], ensure_ascii=False))
    else:
        out(f"repo={scan['repo']}  vcs={vcs}  approx={approx}")
        out(f"源码文件总数={total}  根散文件={root_files}  顶层目录={len(top_stats)}")
        out(f"根配置文件: {', '.join(root_config) or '(无)'}")
        out("-" * 72)
        out(f"{'目录':<28}{'文件':>6}{'LOC':>8}{'churn90d':>9}  包标记  CLAUDE.md")
        for t in top_stats[:24]:
            out(f"{t['dir']:<28}{t['files']:>6}{t['loc']:>8}{t['churn']:>9}"
                f"  {'√' if t['package_marker'] else '-':^4}    "
                f"{'√' if t['has_claude_md'] else '-'}")
        if len(top_stats) > 24:
            out(f"...另有 {len(top_stats) - 24} 个目录,详见 {out_path}")
        out(f"scan.json → {out_path}")
    return 0


# ---------------- plan(聚类) ----------------
def _rescan_files(root: Path) -> list:
    files, _ = walk_source_files(root)
    return files


def _group_module(dirs: list, dom: str) -> dict:
    prefixes = [d["dir"] + "/" for d in dirs]
    primary = max(dirs, key=lambda d: d["files"])["dir"]
    return {
        "id": slugify(primary) + ("-grp" if len(dirs) > 1 else ""),
        "prefixes": prefixes,
        "claude_md": f"{primary}/CLAUDE.md",
        "kind": "group",
        "files": sum(d["files"] for d in dirs),
        "loc": sum(d["loc"] for d in dirs),
        "churn": sum(d["churn"] for d in dirs),
        "top_ext": dom,
    }


def _build_file_tree(files):
    """files: list of {'rel': str}。返回目录树 {'loose': [rel...], 'dirs': {name: node}}。"""
    root = {"loose": [], "dirs": {}}
    for fi in files:
        parts = fi["rel"].split("/")
        node = root
        for p in parts[:-1]:
            node = node["dirs"].setdefault(p, {"loose": [], "dirs": {}})
        node["loose"].append(fi["rel"])
    return root


def _coarse_modules(node, prefix, emit_children=True):
    """供应商/生成/构建/配置目录: 发自身模块;emit_children 时发直接子目录模块,不再更深。"""
    out = [{
        "id": slugify(prefix), "prefixes": [prefix + "/"],
        "claude_md": f"{prefix}/CLAUDE.md", "kind": "dir",
        "files": len(node["loose"]), "loc": 0, "churn": 0, "top_ext": "",
        "depth": prefix.count("/") + 1,
    }]
    if emit_children:
        for name, cn in sorted(node["dirs"].items()):
            sub = f"{prefix}/{name}"
            out.append({
                "id": slugify(sub), "prefixes": [sub + "/"],
                "claude_md": f"{sub}/CLAUDE.md", "kind": "dir",
                "files": len(cn["loose"]), "loc": 0, "churn": 0, "top_ext": "",
                "depth": sub.count("/") + 1,
            })
    return out


def _recursive_emit(node, prefix, path, split_child_files, out):
    """递归发射业务目录模块(prefix=模块相对路径, path=厂商子树判定的完整路径)。
    返回本子树发射的模块数。"""
    child_emitted = 0
    for name, cn in sorted(node["dirs"].items()):
        sub = f"{prefix}/{name}" if prefix else name
        sub_path = f"{path}/{name}"
        if sub_path.endswith(VENDOR_SUBTREE_SUFFIX):
            out.extend(_coarse_modules(cn, sub, emit_children=False))
            child_emitted += 1
            continue
        child_emitted += _recursive_emit(cn, sub, sub_path, split_child_files, out)
    is_top = "/" not in prefix
    has_loose = len(node["loose"]) >= split_child_files
    routing = child_emitted >= 2
    if is_top or has_loose or routing:
        out.append({
            "id": slugify(prefix), "prefixes": [prefix + "/"],
            "claude_md": f"{prefix}/CLAUDE.md", "kind": "dir",
            "files": len(node["loose"]), "loc": 0, "churn": 0, "top_ext": "",
            "depth": prefix.count("/") + 1,
        })
        return 1
    return 0


def _merge_small(small: list, file_exts: dict) -> list:
    """小目录按主导扩展名贪心分组。"""
    groups: dict = {}
    for d in small:
        exts = file_exts.get(d["dir"], {})
        dom = max(exts, key=exts.get) if exts else ".x"
        groups.setdefault(dom, []).append(d)
    modules: list = []
    for dom, ds in sorted(groups.items()):
        cur, cur_files = [], 0
        for d in sorted(ds, key=lambda x: x["dir"]):
            if cur and (len(cur) >= GROUP_MAX_DIRS or cur_files + d["files"] > GROUP_MAX_FILES):
                modules.append(_group_module(cur, dom))
                cur, cur_files = [], 0
            cur.append(d)
            cur_files += d["files"]
        if cur:
            modules.append(_group_module(cur, dom))
    return modules


def _converge(modules: list, max_modules: int) -> list:
    """模块数超上限时,把最小模块合并进同父最小兄弟(保留拆分粒度)。"""
    def parent_of(m: dict) -> str:
        p = m["prefixes"][0].rstrip("/")
        return p.rsplit("/", 1)[0] if "/" in p else ""

    while len(modules) > max_modules:
        modules.sort(key=lambda m: m["files"])
        smallest = modules.pop(0)
        sib = [m for m in modules if parent_of(m) == parent_of(smallest)]
        target = min(sib or modules, key=lambda m: m["files"])
        target["prefixes"] = target["prefixes"] + smallest["prefixes"]
        target["files"] += smallest["files"]
        target["loc"] += smallest["loc"]
        target["churn"] += smallest["churn"]
        if smallest.get("single_root_only"):
            target["single_root_only"] = True
    return modules


def cluster(scan: dict, max_modules: int, min_files: int,
            split_child_files: int = SPLIT_CHILD_FILES):
    """返回 (modules, low_confidence)。嵌套前缀由最长前缀匹配消解。"""
    root = Path(scan["repo"])
    scan["_files"], _ = walk_source_files(root)
    file_exts: dict = {}
    for fi in scan["_files"]:
        ext = Path(fi["rel"]).suffix.casefold()
        file_exts.setdefault(fi["top"], {})
        file_exts[fi["top"]][ext] = file_exts[fi["top"]].get(ext, 0) + 1

    # 递归逐层分解: 业务目录拆到最底层叶子, 供应商/生成/构建/配置目录保持粗粒度
    tree = _build_file_tree(scan["_files"])
    modules: list = []
    for name, node in sorted(tree["dirs"].items()):
        if name == ".metadata" or (name == "src" and len(node["loose"]) <= 1 and not node["dirs"]):
            continue  # 配置噪声(Eclipse 元数据)/薄入口目录不单列
        if name in COARSE_TOP:
            modules.extend(_coarse_modules(node, name))
        else:
            _recursive_emit(node, name, name, split_child_files, modules)
    modules = _converge(modules, max_modules)
    low = len(modules) > max_modules or len(modules) < 2
    if len(modules) == 1 and scan["root_files"] == 0:
        modules[0]["claude_md"] = "CLAUDE.md"
        modules[0]["single_root_only"] = True
    scan.pop("_files", None)
    return modules, low


def plan_cmd(args) -> int:
    scan_path = Path(args.scan)
    if not scan_path.exists():
        die(3, f"scan 文件不存在: {scan_path}")
    scan = json.loads(read_text(scan_path))
    modules, low = cluster(scan, args.max_modules, args.min_files,
                           args.split_child_files)
    plan = {
        "generated_at": now_iso(), "repo": scan["repo"], "vcs": scan["vcs"],
        "root_file": "CLAUDE.md", "low_confidence": low,
        "modules": sorted(modules, key=lambda m: (-m["churn"], -m["files"])),
    }
    out_path = Path(args.out) if args.out else scan_path.parent / "modules.json"
    out_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                        encoding="utf-8", newline="\n")
    if args.json:
        print(json.dumps({"low_confidence": low, "count": len(modules),
                          "out": str(out_path)}, ensure_ascii=False))
    else:
        out(f"{'模块 id':<24}{'前缀':<34}{'文件':>6}{'churn':>7}  类型")
        for m in plan["modules"]:
            out(f"{m['id']:<24}{','.join(m['prefixes'])[:32]:<34}{m['files']:>6}{m['churn']:>7}  {m['kind']}")
        out(f"low_confidence={low}  modules.json → {out_path}")
    return 0 if not low else 4


# ---------------- state / detect-changes ----------------
def load_state(root: Path):
    p = workspace_of(root) / "state.json"
    if not p.exists():
        return None
    try:
        return json.loads(read_text(p))
    except json.JSONDecodeError:
        return None


def longest_prefix_match(modules: list, rel: str):
    nf = norm(rel)
    best, best_len = None, -1
    for m in modules:
        for pre in m.get("prefixes", [m.get("path", "")]):
            npre = norm(pre)
            if npre and nf.startswith(npre) and len(npre) > best_len:
                best, best_len = m, len(npre)
    return best


def changed_files(root: Path, state: dict):
    """返回 (变更文件列表, 降级原因)。"""
    if state.get("vcs") != "git":
        return [], "non-git"
    rc = state.get("reviewed_commit")
    files: list = []
    fallback = None
    if rc:
        d = git(root, "diff", "--name-only", rc, "HEAD")
        if d is None:
            fallback = "baseline-invalid"
        else:
            files.extend(to_posix(x) for x in d.splitlines() if x.strip())
    st = git(root, "status", "--porcelain")
    if st:
        for line in st.splitlines():
            if len(line) < 4:
                continue
            body = line[3:].strip()
            if "->" in body:
                a, b = body.split("->", 1)
                files += [to_posix(a.strip()), to_posix(b.strip())]
            else:
                files.append(to_posix(body))
    seen, uniq = set(), []
    for f in files:
        if f and f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq, fallback


def src_sig(root: Path, module: dict, modules: list) -> str:
    """模块源码签名:rel+size+mtime 排序后 sha256(非 git 变更检测依据)。
    嵌套前缀时,已被更深前缀归属的文件计入更深模块,不算本模块的。"""
    all_pres = [(norm(p), m2["id"]) for m2 in modules for p in m2.get("prefixes", [])]
    own = {norm(p) for p in module.get("prefixes", [])}
    items = []
    for pre in module.get("prefixes", []):
        base = root / pre.rstrip("/")
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames
                           if x not in EXCLUDED_DIRS and not x.endswith(".egg-info")]
            for f in filenames:
                if Path(f).suffix.casefold() not in SOURCE_EXTS:
                    continue
                fp = os.path.join(dirpath, f)
                rel = to_posix(os.path.relpath(fp, root))
                nf = norm(rel)
                best_id, best_len = None, -1
                for npre, mid in all_pres:
                    if nf.startswith(npre) and len(npre) > best_len:
                        best_id, best_len = mid, len(npre)
                if best_id is not None and best_id != module["id"]:
                    continue          # 归属更深模块
                try:
                    data = Path(fp).read_bytes()
                    items.append(f"{rel}:{hashlib.sha256(data).hexdigest()[:16]}")
                except OSError:
                    continue
    items.sort()
    return hashlib.sha256("\n".join(items).encode("utf-8", "replace")).hexdigest()[:32]


def hash_fallback_changes(root: Path, state: dict) -> list:
    """reviewed_commit 失效或非 git:文档 content_hash + 源码签名比对(模块级粒度)。"""
    res = []
    for m in state.get("modules", []):
        exists = (root / m["claude_md"]).exists()
        cur = module_hash(root, m["claude_md"]) if exists else ""
        md_changed = (not exists) or cur != m.get("content_hash", "")
        if "src_hash" in m:
            src_changed = src_sig(root, m, state.get("modules", [])) != m["src_hash"]
        else:
            src_changed = True      # 旧 state 无 src_hash → 该模块全量复核一次
        if md_changed or src_changed:
            res.append({"id": m["id"], "files": None,
                        "manual_edit": (cur != m.get("content_hash", "")) if exists else None,
                        "missing_md": not exists, "src_changed": src_changed})
    return res


def dir_source_count(root: Path, rel_dir: str) -> int:
    cnt = 0
    base = root / rel_dir
    if not base.is_dir():
        return 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [x for x in dirnames if x not in EXCLUDED_DIRS]
        cnt += sum(1 for x in filenames if Path(x).suffix.casefold() in SOURCE_EXTS)
        if cnt > NEW_DIR_FILES * 4:
            break
    return cnt


def detect_changes_core(root: Path, state: dict) -> dict:
    plan_modules = state.get("modules", [])
    files, fallback = changed_files(root, state)
    root_md = state.get("root_claude_md", {}).get("path", "CLAUDE.md")

    # 降级:非 git / 基线失效 → content_hash 比对
    if (not files and state.get("vcs") != "git") or fallback == "baseline-invalid" \
            or (state.get("vcs") == "git" and not state.get("reviewed_commit")):
        hash_changed = hash_fallback_changes(root, state)
        root_cur = module_hash(root, root_md) if (root / root_md).exists() else ""
        root_dirty = root_cur != state.get("root_claude_md", {}).get("content_hash", "")
        return {
            "fallback": fallback or "non-git", "files_scanned": 0,
            "changed_modules": hash_changed, "new_dirs": [], "small_new_dirs": [],
            "deleted": [], "root_dirty": root_dirty,
            "root_dirty_reasons": ["content_hash 比对(模块级粒度,无文件清单)"],
        }

    prefixes = [(norm(p), p, m["id"]) for m in plan_modules for p in m.get("prefixes", [])]
    by_mod: dict = {}
    new_dirs: list = []
    root_dirty_reasons: list = []
    for f in files:
        fl = f.casefold()
        if fl in CLAUDE_MD_NAMES:
            root_dirty_reasons.append(f"根 CLAUDE.md 被改动: {f}")
            continue
        if f.startswith(".claude-md-map/"):
            continue
        if "/" not in f:
            if ROOT_CONFIG_RE.match(fl):
                root_dirty_reasons.append(f"根配置变更: {f}")
            continue
        hit_mid, hit_len = None, -1
        nf = norm(f)
        for npre, pre, mid in prefixes:
            if nf.startswith(npre) and len(npre) > hit_len:
                hit_mid, hit_len = mid, len(npre)
        if hit_mid:
            by_mod.setdefault(hit_mid, []).append(f)
        else:
            top = f.split("/")[0]
            if top not in new_dirs:
                new_dirs.append(top)

    changed_modules = []
    for mid, fs in sorted(by_mod.items()):
        m = next((x for x in plan_modules if x["id"] == mid), None)
        md_rel = m["claude_md"] if m else ""
        exists = (root / md_rel).exists() if md_rel else False
        cur = module_hash(root, md_rel) if exists else ""
        changed_modules.append({
            "id": mid, "files": fs[:BRIEF_FILE_CAP],
            "manual_edit": (cur != m.get("content_hash", "")) if (exists and m) else None,
            "missing_md": not exists,
        })
        if exists and m and cur != m.get("content_hash", ""):
            root_dirty_reasons.append(f"模块文档被手改: {md_rel}")

    new_dir_details = [{"dir": d, "files": dir_source_count(root, d)} for d in new_dirs]
    deleted = [m["id"] for m in plan_modules
               if not any((root / p.rstrip("/")).exists() for p in m.get("prefixes", []))]

    return {
        "fallback": None, "files_scanned": len(files),
        "changed_modules": changed_modules,
        "new_dirs": [d for d in new_dir_details if d["files"] >= NEW_DIR_FILES],
        "small_new_dirs": [d for d in new_dir_details if d["files"] < NEW_DIR_FILES],
        "deleted": deleted,
        "root_dirty": bool(root_dirty_reasons),
        "root_dirty_reasons": root_dirty_reasons[:10],
    }


def detect_cmd(args) -> int:
    root = repo_root(args.repo)
    n_files = count_source_files(root)
    if n_files < MIN_SOURCE_FILES and not args.force:
        die(4, f"源码文件仅 {n_files} 个(<{MIN_SOURCE_FILES}),不像代码工程。"
               f"确认目录无误可用 --force 重跑。")
    state = load_state(root)
    vcs = "git" if is_git(root) else "none"
    if state:
        ch = detect_changes_core(root, state)
        busy = bool(ch["changed_modules"] or ch["new_dirs"] or ch["deleted"] or ch["root_dirty"])
        mode = "UPDATE" if busy else "NOOP"
        reasons = [f"state.json 存在(基线 {state.get('reviewed_commit') or 'content_hash'})"]
        if not busy:
            reasons.append("自上次审计以来无影响文档的变更")
        else:
            reasons.append(f"受影响模块 {len(ch['changed_modules'])} 个"
                           f"/ 新目录 {len(ch['new_dirs'])} / 根需同步={ch['root_dirty']}")
    elif find_claude_md(root):
        mode, reasons = "ADOPT", ["存在根 CLAUDE.md 但无 state.json → 合并式初始化"]
    else:
        mode, reasons = "INIT", ["无根 CLAUDE.md、无 state.json → 全新初始化"]
    ws = None
    if args.ensure_workspace and mode != "NOOP":
        ws = str(ensure_workspace(root, args.state_local))
    payload = {"mode": mode, "repo": str(root), "vcs": vcs,
               "source_files": n_files, "reasons": reasons,
               "workspace": ws or str(workspace_of(root))}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        out(f"MODE = {mode}")
        for r in reasons:
            out(f"  - {r}")
        out(f"repo={root}  vcs={vcs}  源码文件≈{n_files}")
    return 0


def detect_changes_cmd(args) -> int:
    root = repo_root(args.repo)
    state = load_state(root)
    if not state:
        die(3, "state.json 不存在——先完成一次初始化,或用 detect 判定模式。")
    ch = detect_changes_core(root, state)
    ch["generated_at"] = now_iso()
    ch["repo"] = str(root)
    ws = workspace_of(root)
    (ws / "tmp").mkdir(parents=True, exist_ok=True)
    out_path = ws / "tmp" / "changes.json"
    out_path.write_text(json.dumps(ch, ensure_ascii=False, indent=1),
                        encoding="utf-8", newline="\n")
    if args.json:
        print(json.dumps({k: ch[k] for k in (
            "changed_modules", "new_dirs", "deleted", "root_dirty", "fallback")},
            ensure_ascii=False))
    else:
        if ch["fallback"]:
            out(f"降级模式: {ch['fallback']}(模块级粒度,无文件清单)")
        out(f"受影响模块: {', '.join(m['id'] for m in ch['changed_modules']) or '(无)'}")
        nd = ", ".join(f"{d['dir']}({d['files']}文件)" for d in ch["new_dirs"]) or "(无)"
        out(f"新目录: {nd}")
        out(f"已删除模块: {', '.join(ch['deleted']) or '(无)'}")
        out(f"根文件需同步: {'是 — ' + '; '.join(ch['root_dirty_reasons'][:3]) if ch['root_dirty'] else '否'}")
        out(f"changes.json → {out_path}")
    return 0


# ---------------- brief ----------------
SKELETON_MODULE = """\
# <模块名>(中文一句话:这个模块是干什么的)

## Commands
| Command | Description |
|---------|-------------|
| `<真实命令>` | <说明> |

## Architecture
```
<dir>/
  <sub>/    # <用途>
```

## Key Files
- `<path>` - <用途>

## Gotchas
- <非显而易见的坑/顺序依赖/配置怪癖>
"""


def brief_text(root: Path, m: dict, mode: str, file_list: list, total: int,
               changes_by_id: dict) -> str:
    ws = workspace_of(root)
    lines = [
        f"# MODULE BRIEF — {m['id']}",
        f"- mode: {mode}",
        f"- 前缀: {', '.join(m['prefixes'])}",
        f"- 文档目标(相对仓库根): {m['claude_md']}",
        f"- 草稿输出(写到这里,不要写进仓库): {ws}/tmp/draft/{m['id']}/CLAUDE.md",
        f"- 报告输出: {ws}/tmp/report-{m['id']}.md",
        f"- 行数预算: ≤{MODULE_BUDGET} 行(target 50);章节标题可英文,正文中文",
        "",
        "## 文件清单" + (f"(共 {total} 个,列前 {len(file_list)})"
                       if total > len(file_list) else f"({total})"),
    ]
    lines += [f"- `{f}`" for f in file_list]
    if mode in ("adopt", "update"):
        lines += ["", f"## 既有文档(merge 基准,相对仓库根): {m['claude_md']}"]
        if mode == "update":
            ch = changes_by_id.get(m["id"]) or {}
            cf = ch.get("files")
            if cf is None:
                lines += ["- 变更文件清单不可用(content_hash 粒度):请自行全模块复核"]
            else:
                lines += [f"- 自基线以来变更的文件({len(cf)}):"]
                lines += [f"  - `{f}`" for f in cf[:80]]
            lines += [
                "",
                "## 更新指令",
                "- 在你自己的上下文运行 git diff 看这些文件的实际改动",
                "- 逐条核对既有文档的每条 claim(命令/路径/约定/gotcha),只修改失真处",
                "- 未受影响的章节一字不动;手写内容(无法从代码推导的行)必须保留",
                f"- 总行数仍 ≤{MODULE_BUDGET}",
            ]
        if mode == "adopt":
            lines += [
                "",
                "## ADOPT 指令",
                "- 先读既有文档全文;有价值的手写内容全部保留",
                "- 用骨架结构重排:既有内容映射进对应章节,缺失章节按模板补齐",
                "- 禁止删除既有 gotcha/约定,除非你验证过它们已失真(在报告里说明)",
            ]
    lines += [
        "",
        "## 骨架模板(按需取用章节)",
        "```markdown",
        SKELETON_MODULE.rstrip(),
        "```",
        "",
        "## 硬规则",
        "1. 只写**非显然**信息:类名自解释的不写;通用最佳实践不写;一次性修复不写",
        "2. Commands 必须真实可执行:从构建脚本/工程文件核实,禁止编造",
        "3. Key Files/Gotchas 的路径必须真实存在(你负责验证)",
        "4. 完整产出写入草稿路径;最终回复只给 ≤15 行回执:",
        "   STATUS: DONE|DONE_WITH_CONCERNS|NEEDS_CONTEXT|BLOCKED / 行数 / 3 条最值得记录的 gotcha / 未覆盖目录 / 报告路径",
        "5. 禁止派生 subagent;禁止修改 brief 清单之外的文件",
    ]
    return "\n".join(lines) + "\n"


def brief_cmd(args) -> int:
    modules_path = Path(args.modules)
    if not modules_path.exists():
        die(3, f"modules 文件不存在: {modules_path}")
    plan = json.loads(read_text(modules_path))
    root = Path(plan["repo"])
    mode = args.mode
    if mode == "init" and find_claude_md(root):
        mode = "adopt"
    scan = json.loads(read_text(Path(args.scan))) if args.scan and Path(args.scan).exists() else None
    changes = json.loads(read_text(Path(args.changes))) if args.changes and Path(args.changes).exists() else None

    ids = []
    if args.id:
        ids = [args.id]
    elif args.batch:
        ids = [x.strip() for x in args.batch.split(",") if x.strip()]
    else:
        die(2, "需要 --id ID 或 --batch id1,id2")
    mods = {m["id"]: m for m in plan["modules"]}
    missing = [i for i in ids if i not in mods]
    if missing:
        die(3, f"模块不存在: {', '.join(missing)}")
    if mode == "update" and not changes:
        die(2, "update 模式需要 --changes changes.json")

    ws = ensure_workspace(root)
    changes_by_id = {c["id"]: c for c in (changes or {}).get("changed_modules", [])}
    paths = []
    for mid in ids:
        m = mods[mid]
        # 模块级模式:以该模块自己的 CLAUDE.md 是否存在为准(update 且文档被手删 → 退化为 adopt)
        if mode == "update":
            mode_m = "update" if (root / m["claude_md"]).exists() else "adopt"
        elif (root / m["claude_md"]).exists():
            mode_m = "adopt"
        else:
            mode_m = "init"
        if mode_m == "update":
            entry = changes_by_id.get(mid)
            if not entry:
                die(2, f"模块 {mid} 不在变更列表中,无需 brief")
            flist = entry.get("files") or []
            total = len(flist)
        else:
            if not scan:
                die(2, "init/adopt 模式需要 --scan scan.json 以生成文件清单")
            allf = scan.get("_files") or _rescan_files(root)
            pref = [norm(p) for p in m["prefixes"]]
            flist = sorted({f["rel"] for f in allf
                            if any(norm(f["rel"]).startswith(p) for p in pref)})
            flist = [f for f in flist if f not in {mm["claude_md"] for mm in plan["modules"]}]
            total = len(flist)
        shown = flist[:BRIEF_FILE_CAP]
        txt = brief_text(root, m, mode_m, shown, total, changes_by_id)
        bp = ws / "tmp" / "draft" / mid / "brief.md"
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text(txt, encoding="utf-8", newline="\n")
        paths.append(str(bp))
    for p in paths:
        out(p)
    return 0


# ---------------- lint ----------------
def _extract_paths(text: str) -> list:
    res = []
    for s in BACKTICK_RE.findall(text):
        s = s.strip().rstrip(".,;:")
        if not s or "://" in s or any(c in s for c in '*?<>| (){}"…=&→#'):
            # 含代码特征字符(= & → #)的反引号串是宏/函数简写, 非文件路径
            continue
        if "/" not in s and "\\" not in s:
            continue
        if ".." in s:            # ../ 或省略号式上下穿越引用,不校验
            continue
        if re.fullmatch(r"/[A-Za-z0-9._-]+", s):
            continue   # /skill 或 /命令 引用(如 /s32ds-compile),非文件路径
        res.append(s.replace("\\", "/"))
    return res


_EXT_SHORT = re.compile(r"^\.[A-Za-z0-9]{1,4}$")
_ABS_WIN = re.compile(r"^[A-Za-z]:[/\\]")
_IDENT_SEG = re.compile(r"^[\w.{}\[\],…\-]+$")


def _build_bases(root: Path, module_dir) -> list:
    """解析基:模块目录链 + 全仓库深度≤4 的目录(排除噪声目录)。"""
    bases = []
    if module_dir:
        d = Path(module_dir)
        bases.append(root / d)
        while len(d.parts) > 1:
            d = d.parent
            bases.append(root / d)
    bases.append(root)
    stack = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        if depth >= 4:
            continue
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            if e.is_dir() and e.name not in EXCLUDED_DIRS and not e.name.endswith(".egg-info"):
                bases.append(Path(e.path))
                stack.append((Path(e.path), depth + 1))
    return bases[:3000]


def _path_exists_fuzzy(bases: list, cand: str) -> bool:
    """候选路径按段解析;支持:行号后缀、任意模块相对路径、绝对盘符路径、
    'A.elf/.bin/.map' 文件后缀列表、'A/B/C' 式标识符或列表、'/.../' 缩写。"""
    c = re.sub(r":\d+$", "", cand)
    if _ABS_WIN.match(cand):
        return Path(cand.rstrip("/")).exists()
    c = c.lstrip("/")
    if not c or "/.../" in f"/{c}" or c.startswith(".../"):
        return True
    m = re.match(r"^(.*):\w+\[\]?$", c)   # file.c:Symbol / file.c:Symbol[] 后缀
    if m and not _ABS_WIN.match(m.group(1)):
        c = m.group(1)
    segs = [s for s in c.split("/") if s]
    if not segs or any(len(s) < 2 for s in segs):
        return True
    first_found = False
    for base in bases:
        p, kind = base, None
        for i, s in enumerate(segs):
            p2 = p / s
            if p2.is_dir():
                p, kind = p2, "dir"
                if i == 0:
                    first_found = True
                continue
            if p2.is_file():
                p, kind = p2, "file"
                if i == 0:
                    first_found = True
                continue
            rest = segs[i:]
            if kind == "file":            # 文件后面还挂着段 → 后缀/别名列表
                return True
            if all(_EXT_SHORT.match(x) for x in rest):   # .h/.c 式后缀列表
                return True
            break
        else:
            return True                    # 全段命中
    # 任何 base 连首段都不存在 → 标识符或列表/目录名简写
    if len(segs) >= 2 and all(_IDENT_SEG.match(s) for s in segs):
        return True
    # 目录名(或后缀)简写:GenerateCode/ → App_CAN_WB101_GenerateCode/,_WY113/ → App_CAN_WY113/
    for s in segs:
        if len(s) >= 4 and any(d.name.endswith(s) for d in bases):
            return True
    return False


def lint_one(root: Path, md_path: Path, kind: str, budget: int,
             prefixes, module_dir: str | None = None) -> dict:
    text = read_text(md_path)
    lines = text.splitlines()
    n = len(lines)
    findings = {
        "path": str(md_path), "kind": kind, "lines": n, "budget": budget,
        "over": max(0, n - budget), "dead_paths": [], "empty_sections": [],
        "missing_map_prefixes": [], "unknown_map_paths": [],
    }
    # 解析基:模块目录链 + 全仓库深度≤4 目录(根文档 module_dir=None)
    bases = _build_bases(root, module_dir if kind == "module" else None)
    for cand in dict.fromkeys(_extract_paths(text)):
        c = cand[2:] if cand.startswith("./") else cand
        if not _path_exists_fuzzy(bases, c):
            findings["dead_paths"].append(cand)
    for i, ln in enumerate(lines):
        if ln.startswith("##"):
            nxt = next((x for x in lines[i + 1:] if x.strip()), None)
            if nxt is None or nxt.startswith("##"):
                findings["empty_sections"].append(ln.lstrip("# ").strip())
    if prefixes:  # 仅根文件:地图行 ↔ 模块前缀对应
        low = text.casefold()
        for p in prefixes:
            if p.rstrip("/").casefold() not in low:
                findings["missing_map_prefixes"].append(p)
        known = {norm(p.rstrip("/")) for p in prefixes}
        top_dirs = [t for t in os.listdir(root) if (root / t).is_dir()]
        for cand in _extract_paths(text):
            if cand.endswith("/"):
                c = cand.rstrip("/").lstrip("/").removeprefix("./")
                if norm(c) in known or not c:
                    continue
                if (root / c).exists() or any((root / t / c).exists() for t in top_dirs):
                    continue
                findings["unknown_map_paths"].append(cand)
    return findings


BOILER_PREFIXES = ("#", "|", "```", "-", "*", ">", "!", "STATUS:",
                   "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.", "0.")


def lint_cmd(args) -> int:
    root = repo_root(args.repo)
    draft = Path(args.draft_dir) if args.draft_dir else None
    plan = json.loads(read_text(Path(args.modules))) if args.modules and Path(args.modules).exists() else None
    jobs: list = []
    single_root_only = bool(plan and plan.get("modules") and
                            plan["modules"][0].get("single_root_only"))
    root_md = (draft / "_root" / "CLAUDE.md") if draft else find_claude_md(root)
    if root_md and root_md.exists() and not single_root_only:
        prefixes_all = [p for m in (plan or {}).get("modules", [])
                        if m.get("depth", 99) <= 2 for p in m["prefixes"]] or None
        jobs.append((root_md, "root", ROOT_BUDGET, prefixes_all, None))
    if plan:
        for m in plan["modules"]:
            p = (draft / m["id"] / "CLAUDE.md") if draft else (root / m["claude_md"])
            if p.exists():
                mdir = to_posix(Path(m["claude_md"]).parent)
                jobs.append((p, "module", MODULE_BUDGET, None, mdir))
            else:
                out(f"LINT-SKIP(文件缺失): {p}")
    if not jobs:
        die(3, "没有可 lint 的 CLAUDE.md(draft-dir 为空且仓库内无文件)")

    results = [lint_one(root, p, k, b, pre, md) for p, k, b, pre, md in jobs]

    dupes: list = []
    if args.dupes:
        seen: dict = {}
        for r in results:
            for ln in read_text(Path(r["path"])).splitlines():
                s = ln.strip()
                if len(s) < 12 or any(s.startswith(b) for b in BOILER_PREFIXES):
                    continue
                seen.setdefault(s, set()).add(r["path"])
        dupes = [s for s, ps in seen.items() if len(ps) >= 2][:20]

    def bad(r):
        return (r["over"] or r["dead_paths"] or r["empty_sections"]
                or r["missing_map_prefixes"] or r["unknown_map_paths"])

    ok = all(not bad(r) for r in results) and not dupes
    if args.json:
        print(json.dumps({"ok": ok, "files": results, "dupes": dupes}, ensure_ascii=False))
    else:
        for r in results:
            out(f"{'✅' if not bad(r) else '❌'} {r['kind']:<6} {r['path']}  {r['lines']}/{r['budget']} 行")
            if r["over"]:
                out(f"   超预算 +{r['over']} 行")
            for d in r["dead_paths"][:8]:
                out(f"   死路径: {d}")
            for s in r["empty_sections"][:5]:
                out(f"   空章节: {s}")
            for p in r["missing_map_prefixes"][:5]:
                out(f"   地图缺行: {p}")
            for p in r["unknown_map_paths"][:5]:
                out(f"   地图指向未知目录: {p}")
        for d in dupes[:10]:
            out(f"   重复行: {d[:70]}")
        out(f"LINT = {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


# ---------------- report / install ----------------
def report_cmd(args) -> int:
    draft = Path(args.draft_dir)
    plan = json.loads(read_text(Path(args.modules)))
    lint_json = None
    if args.lint and Path(args.lint).exists():
        try:
            lint_json = json.loads(read_text(Path(args.lint)))
        except json.JSONDecodeError:
            lint_json = None   # 传入了人类可读输出而非 --json,忽略
    lint_by_path = {r["path"]: r for r in (lint_json or {}).get("files", [])}

    single = bool(plan.get("modules") and plan["modules"][0].get("single_root_only"))
    root_md = draft / "_root" / "CLAUDE.md"
    out("# claude-md-map 落盘确认报告")
    out("")
    if single:
        out("单模块工程:模块文档即根文档。")
    elif root_md.exists():
        text = read_text(root_md)
        out(f"## 根文件 CLAUDE.md({len(text.splitlines())}/{ROOT_BUDGET} 行)全文:")
        out("```markdown")
        out(text.rstrip())
        out("```")
    else:
        out("## 根文件:草稿缺失(检查 worker 是否完成)")
    out("")
    out(f"## 模块文档({len(plan['modules'])} 个,全文在草稿目录,此处仅预览)")
    for m in plan["modules"]:
        p = draft / m["id"] / "CLAUDE.md"
        if not p.exists():
            out(f"### {m['id']} — ❌ 草稿缺失(目标 {m['claude_md']})")
            continue
        text = read_text(p)
        n = len(text.splitlines())
        lr = lint_by_path.get(str(p), {})
        dead = lr.get("dead_paths", []) if isinstance(lr, dict) else []
        heads = [ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("## ")]
        out(f"### {m['id']} → {m['claude_md']}")
        out(f"- {n}/{MODULE_BUDGET} 行 {'✅' if n <= MODULE_BUDGET else '❌ 超预算'}"
            f"{' | lint 死路径: ' + ', '.join(dead[:3]) if dead else ' | lint ✅'}")
        out(f"- 章节: {' / '.join(heads) or '(无)'}")
        for x in text.splitlines()[:6]:
            out(f"  {x}")
        out(f"- 全文: {p}")
    out("")
    out("确认后执行 install 落盘(自动备份既有文件到 tmp/backup-*/)。")
    return 0


def install_cmd(args) -> int:
    draft = Path(args.draft_dir)
    root = repo_root(args.repo)
    plan = json.loads(read_text(Path(args.modules)))
    ensure_workspace(root, args.state_local)
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = workspace_of(root) / "tmp" / f"backup-{ts}"
    single = bool(plan.get("modules") and plan["modules"][0].get("single_root_only"))

    jobs: list = []
    root_md = draft / "_root" / "CLAUDE.md"
    if root_md.exists() and not single:
        jobs.append((root_md, root / "CLAUDE.md", None))
    for m in plan["modules"]:
        src = draft / m["id"] / "CLAUDE.md"
        if src.exists():
            is_first_single = single and m is plan["modules"][0]
            dst_rel = "CLAUDE.md" if is_first_single else m["claude_md"]
            jobs.append((src, root / dst_rel, dst_rel))
        elif args.mode == "init":
            out(f"MISSING: {src}(该模块无草稿,跳过)")
    if not jobs:
        die(3, "草稿目录里没有任何 CLAUDE.md 可落盘")

    written = []
    for src, dst, dst_rel in jobs:
        if dst.exists():
            bdir = backup_dir / Path(dst_rel or "CLAUDE.md").parent
            bdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, bdir / dst.name)
            out(f"BACKUP {dst_rel or 'CLAUDE.md'} → {bdir / dst.name}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        written.append(dst_rel or "CLAUDE.md")
        out(f"WROTE {dst_rel or 'CLAUDE.md'}")

    # 刷新 state.json(update 模式下未更新模块保留旧条目)
    old_state = load_state(root) or {}
    vcs = "git" if is_git(root) else "none"
    state_mods = []
    for m in plan["modules"]:
        is_first_single = single and m is plan["modules"][0]
        dst_rel = "CLAUDE.md" if is_first_single else m["claude_md"]
        if (root / dst_rel).exists():
            state_mods.append({
                "id": m["id"], "path": m["prefixes"][0], "prefixes": m["prefixes"],
                "claude_md": dst_rel,
                "content_hash": module_hash(root, dst_rel),
                "src_hash": src_sig(root, m, plan["modules"]),
                "files": m["files"],
            })
        else:
            old = next((x for x in old_state.get("modules", []) if x["id"] == m["id"]), None)
            if old:
                state_mods.append(old)
    root_hash = module_hash(root, "CLAUDE.md") if (root / "CLAUDE.md").exists() else ""
    state = {
        "version": 1, "generated_at": now_iso(), "vcs": vcs,
        "reviewed_commit": git_head(root) if vcs == "git" else None,
        "modules": state_mods,
        "root_claude_md": {"path": "CLAUDE.md", "content_hash": root_hash},
    }
    (workspace_of(root) / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    out(f"STATE {workspace_of(root) / 'state.json'}(reviewed_commit={state['reviewed_commit'] or 'n/a'})")
    out(f"共落盘 {len(written)} 个文件;备份目录 {backup_dir if backup_dir.exists() else '(无覆盖,未建)'}")
    return 0


# ---------------- main ----------------
def main(argv: list) -> int:
    try:
        if sys.stdout.encoding and sys.stdout.encoding.casefold() not in ("utf-8", "utf8"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr.encoding and sys.stderr.encoding.casefold() not in ("utf-8", "utf8"):
            sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(prog="mdmap.py", description="claude-md-map skill 脚本")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("detect", help="判定 INIT/ADOPT/UPDATE/NOOP")
    p.add_argument("--repo"); p.add_argument("--json", action="store_true")
    p.add_argument("--ensure-workspace", action="store_true")
    p.add_argument("--state-local", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=detect_cmd)

    p = sub.add_parser("scan", help="目录统计 → scan.json")
    p.add_argument("--repo"); p.add_argument("--out"); p.add_argument("--json", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=scan_cmd)

    p = sub.add_parser("plan", help="聚类模块 → modules.json")
    p.add_argument("--scan", required=True); p.add_argument("--out")
    p.add_argument("--json", action="store_true")
    p.add_argument("--max-modules", type=int, default=MAX_MODULES_DEFAULT)
    p.add_argument("--min-files", type=int, default=SMALL_DIR_FILES)
    p.add_argument("--split-child-files", type=int, default=SPLIT_CHILD_FILES,
                  help="二级子目录独立成模块的源码文件数门槛(默认 %(default)s)")
    p.set_defaults(fn=plan_cmd)

    p = sub.add_parser("brief", help="生成模块 worker 的 brief 文件")
    p.add_argument("--modules", required=True); p.add_argument("--scan")
    p.add_argument("--changes"); p.add_argument("--id"); p.add_argument("--batch")
    p.add_argument("--mode", choices=["init", "adopt", "update"], default="init")
    p.set_defaults(fn=brief_cmd)

    p = sub.add_parser("detect-changes", help="变更检测 + 模块映射")
    p.add_argument("--repo"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=detect_changes_cmd)

    p = sub.add_parser("lint", help="草稿/已装文档质量闸")
    p.add_argument("--repo"); p.add_argument("--draft-dir"); p.add_argument("--modules")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dupes", action=argparse.BooleanOptionalAction, default=True)
    p.set_defaults(fn=lint_cmd)

    p = sub.add_parser("report", help="用户确认报告(stdout)")
    p.add_argument("--repo"); p.add_argument("--draft-dir", required=True)
    p.add_argument("--modules", required=True); p.add_argument("--lint")
    p.set_defaults(fn=report_cmd)

    p = sub.add_parser("install", help="草稿落盘 + 备份 + 刷新 state.json")
    p.add_argument("--repo"); p.add_argument("--draft-dir", required=True)
    p.add_argument("--modules", required=True)
    p.add_argument("--mode", choices=["init", "update"], default="init")
    p.add_argument("--state-local", action="store_true")
    p.set_defaults(fn=install_cmd)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
