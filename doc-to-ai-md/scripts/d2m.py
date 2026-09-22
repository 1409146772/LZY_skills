#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
d2m — doc-to-ai-md
把任意目录下的混合格式文档（PDF / Excel / Word / PPT / Markdown / 源码）转换为
AI 可直接阅读的 Markdown，并生成「总索引 + 章节索引 + 分块 + 入口说明」四层导航。

单入口 CLI：
  python d2m.py init       首次全量构建
  python d2m.py update     增量更新（推荐日常使用；源文档变更后重跑）
  python d2m.py all        强制全量重建
  python d2m.py adopt      接管已有产物，反推状态基线（避免重复转换）
  python d2m.py <阶段>     单阶段重跑：scan convert normalize outline structure
                           index chunk master agentdoc validate
常用选项：
  -C <dir>   项目根目录（默认当前目录）
  -c <file>  配置文件（默认 <项目根>/d2m.config.json）
  --force    忽略哈希，强制重跑
  --quiet    精简输出
"""
import os
import re
import sys
import json
import glob as globmod
import hashlib
import shutil
import argparse
import datetime
import collections
import unicodedata
import subprocess

PIPELINE_VERSION = "1.0.0"
STATE_SCHEMA = "1.0"

# =============================================================== 默认配置
DEFAULTS = {
    "project": "项目文档库",
    "output_dir": "ai_docs",
    "work_dir": ".d2m-work",
    "sources": [],
    "terms": [],
    "quick_tables": [],
    "chunk": {"target_lines": 350, "min_doc_lines": 250},
    "index": {"terms_per_doc": 8, "min_hits": 2, "max_sections": 8},
    "normalize": {"enabled": True, "footer_patterns": [], "join_prose": True},
    "agentdoc": {
        "enabled": True,
        "filename": "CLAUDE.md",
        "role": "",
        "ignore_paths": [".d2m-work", ".claude-md-map"],
        "routing": [],
    },
}

# 默认页眉页脚清洗规则（可通过 normalize.footer_patterns 追加）
DEFAULT_FOOTERS = [
    r'^第\s*\d+\s*页\s*/?\s*共\s*\d+\s*页$',
    r'^\d+\s*/\s*\d+$',
    r'^Page\s*\d+\s*(of\s*\d+)?$',
    r'^\d{1,4}$',
]


def deep_merge(base, over):
    """把 over 深度合并进 base 的副本"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def find_root(start=None):
    """向上查找含 d2m.config.json 的目录"""
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, "d2m.config.json")):
            return d
        p = os.path.dirname(d)
        if p == d:
            return None
        d = p


def load_config(root, cfgfile=None):
    p = cfgfile or os.path.join(root, "d2m.config.json")
    if not os.path.exists(p):
        die("找不到配置文件：%s\n请先创建 d2m.config.json（可参考 d2m.config.example.json）" % p)
    try:
        user = json.load(open(p, encoding="utf-8"))
    except Exception as e:
        die("配置文件解析失败：%s\n  %s" % (p, e))
    cfg = deep_merge(DEFAULTS, user)
    cfg["_path"] = p
    cfg["_root"] = root
    if not cfg["sources"]:
        die("配置中没有 sources，无可处理的文档。")
    return cfg


def config_hash(cfg):
    c = {k: v for k, v in cfg.items() if not k.startswith("_")}
    raw = json.dumps(c, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def die(msg, code=2):
    sys.stderr.write("d2m 错误：%s\n" % msg)
    sys.exit(code)


# =============================================================== 基础工具
def log(msg, quiet=False):
    if not quiet:
        print(msg)


def ensure_dir(p):
    os.makedirs(p, exist_ok=True)
    return p


def sha256_file(p, blocksize=1 << 20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(blocksize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def read_text(p):
    return open(p, encoding="utf-8", errors="replace").read()


def write_text(p, s):
    ensure_dir(os.path.dirname(p) or ".")
    open(p, "w", encoding="utf-8", newline="\n").write(s)


def cell(v):
    """表格单元格安全化：换行 -> <br>，竖线 -> /"""
    if v is None:
        return ""
    try:
        import math
        if isinstance(v, float) and math.isnan(v):
            return ""
    except Exception:
        pass
    s = str(v).replace("\\n", " ").replace("\n", "<br>").replace("|", "/")
    s = s.replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join(["---"] * len(header)) + " |"]
    for r in rows:
        out.append("| " + " | ".join(r) + " |")
    return out


SEP_ROW = re.compile(r'^\|[\s\-:|]+\|$')


def scan_headings(lines, min_depth=1):
    """扫描 Markdown 标题，跳过代码块内的 # 行（避免把 shell 注释当标题）"""
    heads = []
    fence = False
    for i, l in enumerate(lines):
        if re.match(r'^\s*```', l):
            fence = not fence
            continue
        if fence:
            continue
        m = re.match(r'^(#{1,6})\s+(.*?)\s*$', l)
        if m and len(m.group(1)) >= min_depth:
            heads.append({"line": i + 1, "depth": len(m.group(1)),
                          "text": m.group(2).strip()})
    return heads


def slugify(t):
    s = t.strip().lower()
    s = re.sub(r'[^\w\u4e00-\u9fff\s-]', '', s)
    return re.sub(r'\s+', '-', s)


def safe_name(t, maxlen=80):
    s = re.sub(r'[\\/:*?"<>|]', '_', t).strip()
    return s[:maxlen]


def is_office(p):
    return os.path.splitext(p)[1].lower() in (".docx", ".docm", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".xlsm")


def is_excel(p):
    return os.path.splitext(p)[1].lower() in (".xlsx", ".xls", ".xlsm")


def detect_kind(p):
    e = os.path.splitext(p)[1].lower()
    if e == ".pdf":
        return "pdf"
    if is_excel(p):
        return "excel"
    if e in (".docx", ".docm", ".doc", ".pptx", ".ppt"):
        return "office"
    if e in (".md", ".markdown", ".txt"):
        return "markdown"
    if e in (".py", ".c", ".h", ".cpp", ".js", ".ts", ".java", ".cs", ".go", ".rs", ".bat", ".sh"):
        return "code"
    return "other"


# ==================================================== Unicode 归一化
# 康熙部首 / CJK 部首补充：NFKC 无法还原的手动兜底
RAD_FALLBACK = {
    0x2EC5: '见', 0x2EC6: '角', 0x2ECB: '车', 0x2ED3: '长',
    0x2ED4: '门', 0x2EDB: '风', 0x2EE9: '黄',
}
# 私有区（PUA）符号映射：常见于 Wingdings 项目符号
PUA_MAP = {
    0xF06C: '·', 0xF0B7: '·', 0xF0A7: '■', 0xF075: '◆', 0xF0FC: '·',
    0xF0D8: '→', 0xF0E0: '→',
}


def fix_char(ch):
    o = ord(ch)
    if 0x2E80 <= o <= 0x2FDF:                      # 康熙部首 / CJK部首补充
        try:
            n = unicodedata.normalize('NFKC', ch)
        except Exception:
            n = ch
        if len(n) == 1 and not (0x2E80 <= ord(n) <= 0x2FDF):
            return n
        return RAD_FALLBACK.get(o, ch)
    if o in PUA_MAP:
        return PUA_MAP[o]
    if 0xE000 <= o <= 0xF8FF:                      # 其他私有区字符：丢弃
        return ''
    return ch


# 这些全角字符必须原样保留：NFKC 会把它们压成半角，破坏中文排版与可读性
KEEP_WIDE = set(
    '，。、；：？！（）【】〔〕《》〈〉「」『』“”‘’'      # 中文标点
    '—…～·　'                                        # 破折号 / 省略号 / 波浪号 / 间隔号 / 全角空格
)
# 注意：全角字母数字与全角符号（Ａ１２３％／）**不**在此列，
# 它们会被 NFKC 转成半角 ASCII，以便用 `F103`、`ABC` 这类常见写法直接检索到。


def _keep_wide(o):
    return chr(o) in KEEP_WIDE


def normalize_text(s):
    """Unicode 兼容字形修复 + 按需 NFKC；返回 (新文本, 修复计数 Counter)

    注意：不能无差别做 NFKC。NFKC 会把中文全角标点压成半角
    （`，`→`,`、`：`→`:`、`（）`→`()`），显式破坏中文正文的可读性，
    也会让「原文含全角标点」的检索词失配。因此这里只对**非中文标点**做 NFKC，
    用于修掉 ⻋/⽂ 这类兼容字形，以及全角字母数字 ＡＢＣ１２３ → ABC123。
    """
    counter = collections.Counter()
    out = []
    for ch in s:
        o = ord(ch)
        if (0x2E80 <= o <= 0x2FDF) or (0xE000 <= o <= 0xF8FF):
            counter[ch] += 1
        n = fix_char(ch)
        if n == ch and o > 0x7F and not _keep_wide(o):
            try:
                nn = unicodedata.normalize('NFKC', ch)
                if nn != ch and len(nn) == 1:
                    n = nn
            except Exception:
                pass
        out.append(n)
    return ''.join(out), counter


# =============================================================== 状态文件
class State:
    """记录每个源文件的哈希、编号与各阶段产物哈希，用于增量判断。

    核心不变量：
      · docno 一旦分配即永久冻结，删除的编号进入 retired 不复用，
        这样 md/03 永远是同一篇，跨文档引用与行号不会漂移。
      · 重命名 = 新路径的哈希命中旧记录 → 继承其 docno 与产物。
    """

    def __init__(self, path):
        self.path = path
        self.data = {"schema_version": STATE_SCHEMA,
                     "pipeline_version": PIPELINE_VERSION,
                     "config_hash": "",
                     "docs": {},
                     "retired": [],
                     "orphan_keep": []}
        if os.path.exists(path):
            try:
                d = json.load(open(path, encoding="utf-8"))
                if d.get("schema_version") == STATE_SCHEMA:
                    self.data = d
                else:
                    sys.stderr.write("提示：状态文件 schema 变化（%s → %s），将按全新库处理。\n"
                                     % (d.get("schema_version"), STATE_SCHEMA))
            except Exception as e:
                sys.stderr.write("提示：状态文件损坏（%s），将按全新库处理。\n" % e)
        self.data.setdefault("retired", [])
        self.data.setdefault("docs", {})
        self.data.setdefault("orphan_keep", [])

    def save(self):
        write_text(self.path, json.dumps(self.data, ensure_ascii=False, indent=2))

    # ---- 编号分配 ----
    def next_docno(self, extra_used=None):
        """下一个可用编号。

        extra_used 用于排除「配置声明保留」的编号（preserve_docnos）：
        那些文档由外部脚本产出，本工具既不生成也不清理，但编号不能让别人占了，
        否则产物会被覆盖。
        """
        used = {str(x) for x in (extra_used or [])}
        for r in self.data["docs"].values():
            if r.get("docno"):
                used.add(str(r["docno"]))
        for r in self.data["retired"]:
            used.add(str(r.get("docno")))
        n = 1
        while ("%02d" % n) in used:
            n += 1
        return "%02d" % n

    def find_by_hash(self, h):
        """哈希命中：用于识别重命名"""
        for k, r in self.data["docs"].items():
            if r.get("sha256") == h:
                return k, r
        for r in self.data["retired"]:
            if r.get("sha256") == h:
                return None, r
        return None, None

    def get(self, relpath):
        return self.data["docs"].get(relpath)

    def put(self, relpath, rec):
        self.data["docs"][relpath] = rec

    def retire(self, relpath):
        r = self.data["docs"].pop(relpath, None)
        if r:
            r["retired_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            r["retired_from"] = relpath
            self.data["retired"].append(r)
        return r


def state_file(cfg):
    return os.path.join(cfg["_root"], cfg["output_dir"], ".state.json")


def reserved_docnos(cfg):
    """配置里声明「保留但不接管」的编号（sources[].preserve_docnos）。

    场景：某些编号的产物由别的脚本/人工维护，本工具只做登记与展示，不重新生成、
    也不做孤儿清理。把这些编号从可用池里扣掉，避免新文档占位后覆盖别人的产物。
    用法：在**已声明了 docno 的那条 source 上**写 "preserve_docnos": ["06","07"]，
    或写 "*" 表示「已有产物中所有我没接管的编号都保留」。
    """
    out = set()
    for s in (cfg.get("sources") or []):
        pv = s.get("preserve_docnos")
        if pv is None:
            continue
        if isinstance(pv, str):
            pv = [pv]
        for x in pv:
            if str(x) == "*":
                out.add("*")
            else:
                out.add("%02d" % int(x) if str(x).isdigit() else str(x))
    return out


def expand_reserved(cfg, st):
    """把 "*" 展开为「磁盘上已存在、但状态里没有的编号」"""
    pv = reserved_docnos(cfg)
    if "*" not in pv:
        return pv - {"*"}
    root = cfg["_root"]
    outdir = os.path.join(root, cfg["output_dir"])
    mine = {str((r or {}).get("docno")) for r in st.data["docs"].values()}
    seen = set()
    for sub in ("md", "index"):
        d = os.path.join(outdir, sub)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            m = re.match(r'^(\d+)[_\-]', f)
            if m:
                seen.add("%02d" % int(m.group(1)))
    return (pv - {"*"}) | (seen - mine)


def keep_artifact(st, relpath, reason=""):
    """登记「不算孤儿、不要清理」的产物文件（外部脚本产出）"""
    k = st.data.setdefault("orphan_keep", [])
    if relpath not in k:
        k.append(relpath)
        if reason:
            log("    · 保留外部产物：%s（%s）" % (relpath, reason))


# =============================================================== 扫描
def internal_paths(cfg):
    """本工具自身产物的路径前缀 / 文件名，绝不作为源文档。
    含 output_dir、work_dir，以及 agentdoc 生成的入口文档（如 CLAUDE.md）。
    否则 `*.md` 这类通配会把刚生成的 CLAUDE.md 又当新文档收进来，形成自噬。"""
    root = cfg["_root"]
    pre = []
    for k in ("output_dir", "work_dir"):
        v = cfg.get(k)
        if v:
            pre.append(os.path.normcase(os.path.normpath(os.path.join(root, v))) + os.sep)
    files = set()
    ag = cfg.get("agentdoc") or {}
    if ag.get("enabled", True):
        for fn in [ag.get("filename") or "CLAUDE.md"] + list(ag.get("generated") or []):
            if fn:
                files.add(os.path.normcase(os.path.join(root, fn)))
    return pre, files


def is_internal(cfg, abspath, _cache={}):
    """判断某绝对路径是否属于本工具产物"""
    key = id(cfg)
    if key not in _cache:
        _cache.clear()
        _cache[key] = internal_paths(cfg)
    pre, files = _cache[key]
    p = os.path.normcase(os.path.normpath(abspath))
    if p in files:
        return True
    return any(p.startswith(x) for x in pre)


def expand_sources(cfg):
    """按配置声明的顺序把 glob 展开为源文件列表，保持声明顺序（决定初始编号）"""
    root = cfg["_root"]
    out = []
    seen = set()
    for i, s in enumerate(cfg["sources"]):
        pats = s.get("glob") or []
        if isinstance(pats, str):
            pats = [pats]
        hits = []
        for pat in pats:
            full = pat if os.path.isabs(pat) else os.path.join(root, pat)
            for h in sorted(globmod.glob(full, recursive=True)):
                if os.path.isfile(h) and not is_internal(cfg, h):
                    hits.append(h)
        if s.get("exclude"):
            ex = s["exclude"]
            if isinstance(ex, str):
                ex = [ex]
            keep = []
            for h in hits:
                rel = os.path.relpath(h, root)
                if any(re.search(p, rel) for p in ex):
                    continue
                keep.append(h)
            hits = keep
        if s.get("expand_children") and hits:
            # 目录型条目：把命中目录展开为其下所有文件
            more = []
            for h in hits:
                if os.path.isdir(h):
                    for dp, _dn, fns in os.walk(h):
                        for f in sorted(fns):
                            more.append(os.path.join(dp, f))
                else:
                    more.append(h)
            hits = more
        if s.get("extensions"):
            e = tuple(s["extensions"])
            hits = [h for h in hits if os.path.splitext(h)[1].lower() in e]
        for h in hits:
            rp = os.path.relpath(h, root)
            if rp in seen:
                continue
            seen.add(rp)
            out.append({"relpath": rp, "abspath": h, "spec": s, "spec_index": i})
    return out


def match_spec_for_doc(cfg, relpath):
    """为已编号文档反查其配置条目（按 glob 重新匹配）"""
    root = cfg["_root"]
    for s in cfg["sources"]:
        pats = s.get("glob") or []
        if isinstance(pats, str):
            pats = [pats]
        for pat in pats:
            full = pat if os.path.isabs(pat) else os.path.join(root, pat)
            for h in globmod.glob(full, recursive=True):
                if (os.path.isfile(h) and not is_internal(cfg, h)
                        and os.path.relpath(h, root) == relpath):
                    return s
    return None


def cmd_scan(cfg, st, force=False, quiet=False, report=None):
    """扫描源目录：哈希比对 → 变更集"""
    files = expand_sources(cfg)
    changes = {"added": [], "modified": [], "renamed": [], "unchanged": [], "removed": []}

    cur = {}
    for f in files:
        h = sha256_file(f["abspath"])
        cur[f["relpath"]] = (h, f)

    # 已有记录比对
    for rp, (h, f) in cur.items():
        rec = st.get(rp)
        if rec and rec.get("sha256") == h and not force:
            changes["unchanged"].append(rp)
            continue
        if not rec:
            # 可能是重命名：哈希命中旧记录
            oldk, oldr = st.find_by_hash(h)
            if oldr is not None and oldk != rp:
                changes["renamed"].append({"from": oldk, "to": rp, "rec": oldr, "hash": h, "f": f})
            else:
                changes["added"].append({"relpath": rp, "hash": h, "f": f})
        else:
            changes["modified"].append({"relpath": rp, "hash": h, "f": f,
                                        "rec": rec, "old_hash": rec.get("sha256")})

    # 已消失
    for rp in list(st.data["docs"].keys()):
        if rp not in cur:
            changes["removed"].append(rp)

    # 历史遗留：早期被通配误收的本工具产物（如入口文档）直接剔除，不占编号、不退役
    purged = []
    for rp in list(st.data["docs"].keys()):
        if is_internal(cfg, os.path.join(cfg["_root"], rp)):
            st.data["docs"].pop(rp, None)
            if rp in changes["removed"]:
                changes["removed"].remove(rp)
            purged.append(rp)
    if purged and not quiet:
        log("  · 剔除产物自身（非源文档）：%s" % "、".join(purged))
    changes["purged"] = purged

    if report is not None:
        report["changes"] = changes
    if not quiet:
        log("  新增 %d　修改 %d　重命名 %d　删除 %d　未变 %d"
            % (len(changes["added"]), len(changes["modified"]), len(changes["renamed"]),
               len(changes["removed"]), len(changes["unchanged"])))
    return changes


def apply_changes_to_state(cfg, st, changes, quiet=False):
    """把变更集落到状态：分配/继承编号、标记需重跑的文档"""
    root = cfg["_root"]
    dirty = set()          # 需重跑流水线的 relpath
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1) 重命名：继承编号与产物
    for r in changes["renamed"]:
        oldk, newp = r["from"], r["to"]
        rec = r["rec"]
        if oldk and oldk in st.data["docs"]:
            st.data["docs"].pop(oldk)
        rec = dict(rec)
        rec["sha256"] = r["hash"]
        rec["renamed_from"] = oldk or rec.get("renamed_from")
        rec["mtime"] = os.path.getmtime(r["f"]["abspath"])
        st.put(newp, rec)
        dirty.add(newp)
        log("  ~ 重命名：%s → %s（编号 %s 继承）" % (oldk, newp, rec.get("docno")), quiet)

    # 2) 新增：分配新编号
    for a in changes["added"]:
        rp = a["relpath"]
        no = a["f"]["spec"].get("docno") or st.next_docno()
        if a["f"]["spec"].get("docno"):
            used = {str(r.get("docno")) for r in st.data["docs"].values()}
            if no in used:
                die("配置错误：sources 中 docno=%s 与其他文档冲突。" % no)
        st.put(rp, {"docno": no, "sha256": a["hash"],
                    "size": os.path.getsize(a["f"]["abspath"]),
                    "mtime": os.path.getmtime(a["f"]["abspath"]),
                    "stages": {}, "added_at": now})
        dirty.add(rp)

    # 3) 修改：保留编号，清空阶段产物标记
    for m in changes["modified"]:
        rp = m["relpath"]
        rec = st.get(rp) or {}
        rec = dict(rec)
        rec["sha256"] = m["hash"]
        rec["size"] = os.path.getsize(m["f"]["abspath"])
        rec["mtime"] = os.path.getmtime(m["f"]["abspath"])
        rec["stages"] = {}
        rec["modified_at"] = now
        st.put(rp, rec)
        dirty.add(rp)

    # 4) 删除：编号退役，不复用
    for rp in changes["removed"]:
        r = st.retire(rp)
        if r:
            log("  - 删除：%s（编号 %s 退役，不再复用）" % (rp, r.get("docno")), quiet)

    return dirty


# =============================================================== 阶段 2：转换
def md_from_excel(path, spec, quiet=False):
    """Excel 通用渲染：多 sheet → 每 sheet 一节（供未声明 structured 的表格使用）"""
    import pandas as pd
    xl = pd.ExcelFile(path)
    out = []
    for s in xl.sheet_names:
        g = xl.parse(s, header=None)
        grid = [[cell(v) for v in row] for row in g.values.tolist()]
        grid = [r for r in grid if any(r)]
        out.append("## %s" % s.strip())
        out.append("")
        if not grid:
            out.append("_（空表）_")
            out.append("")
            continue
        ncol = max(len(r) for r in grid)
        grid = [r + [""] * (ncol - len(r)) for r in grid]
        out += md_table(["列%d" % (i + 1) for i in range(ncol)], grid)
        out.append("")
    return "\n".join(out)


def md_from_code(path):
    """源码 → 带语言标注的代码块，便于 AI 直接读"""
    ext = os.path.splitext(path)[1].lower()
    lang = {".py": "python", ".c": "c", ".h": "c", ".cpp": "cpp", ".js": "javascript",
            ".ts": "typescript", ".java": "java", ".cs": "csharp", ".go": "go",
            ".rs": "rust", ".bat": "bat", ".sh": "bash"}.get(ext, "")
    body = read_text(path)
    return "```%s\n%s\n```" % (lang, body.rstrip())


def cmd_convert(cfg, st, dirty, force=False, quiet=False):
    """把源文件转为原始 Markdown，落在 <work_dir>/raw/"""
    import pandas as pd
    root = cfg["_root"]
    work = ensure_dir(os.path.join(root, cfg["work_dir"]))
    raw = ensure_dir(os.path.join(work, "raw"))
    files = {f["relpath"]: f for f in expand_sources(cfg)}
    done = []
    for rp in sorted(dirty):
        f = files.get(rp)
        if f is None:
            continue
        rec = st.get(rp) or {}
        no = rec.get("docno") or "00"
        spec = f["spec"]
        kind = spec.get("kind") or detect_kind(f["abspath"])
        target = os.path.join(raw, "%s_raw.md" % no)
        try:
            if kind == "markdown":
                txt = read_text(f["abspath"])
            elif kind == "code":
                txt = md_from_code(f["abspath"])
            elif kind == "excel":
                if spec.get("sheet") is not None or spec.get("structured"):
                    txt = None          # 交由 structure 阶段专门处理
                else:
                    txt = md_from_excel(f["abspath"], spec, quiet)
            elif kind in ("pdf", "office"):
                txt = markitdown_convert(f["abspath"], quiet)
            else:
                txt = read_text(f["abspath"])
            if txt is not None:
                write_text(target, txt)
                rec.setdefault("stages", {})["convert"] = "ok"
                st.put(rp, rec)
                done.append(rp)
                log("    ✓ 转换 %-52s %6d 行" % (rp[:52], txt.count("\n") + 1), quiet)
            else:
                log("    · 跳过 %-52s（由 structure 阶段处理）" % rp[:52], quiet)
        except Exception as e:
            log("    ✗ 转换失败 %s：%s" % (rp, e), quiet)
    return done


def markitdown_convert(path, quiet=False):
    """调用 markitdown 转 PDF / Office（.exe shim 不可靠，统一走 python -m）"""
    try:
        r = subprocess.run([sys.executable, "-m", "markitdown", path],
                           capture_output=True, timeout=1800)
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(err[-1] if err else "markitdown 返回 %d" % r.returncode)
        return r.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        die("未找到 markitdown。请先安装：pip install markitdown[all]")
    except subprocess.TimeoutExpired:
        die("markitdown 转换超时（1800s）：%s" % path)


# =============================================================== 阶段 3：归一化
def cmd_normalize(cfg, st, dirty, quiet=False):
    """Unicode 归一化 + 页眉页脚清洗，落在 <work_dir>/norm/"""
    root = cfg["_root"]
    raw = os.path.join(root, cfg["work_dir"], "raw")
    nrm = ensure_dir(os.path.join(root, cfg["work_dir"], "norm"))
    if not cfg["normalize"].get("enabled", True):
        log("  （归一化已关闭，直接沿用 raw）", quiet)
    pats = list(DEFAULT_FOOTERS) + list(cfg["normalize"].get("footer_patterns") or [])
    foot = [re.compile(p) for p in pats]
    out = []
    for rp in sorted(dirty):
        rec = st.get(rp) or {}
        no = rec.get("docno") or "00"
        src = os.path.join(raw, "%s_raw.md" % no)
        if not os.path.exists(src):
            continue
        txt = read_text(src)
        fixed, counter = normalize_text(txt) if cfg["normalize"].get("enabled", True) else (txt, {})
        lines = []
        for l in fixed.split("\n"):
            t = l.strip()
            if t and any(p.match(t) for p in foot):
                # 保留表格结构：整行都是页脚的表格行也清掉
                continue
            lines.append(l.rstrip())
        res = collapse_blank(lines)
        write_text(os.path.join(nrm, "%s_norm.md" % no), "\n".join(res))
        rec.setdefault("stages", {})["normalize"] = "ok"
        rec["norm_fixes"] = sum(counter.values())
        st.put(rp, rec)
        out.append(rp)
        extra = "　字符修复 %d" % sum(counter.values()) if counter else ""
        log("    ✓ 归一 %-52s %6d 行%s" % (rp[:52], len(res), extra), quiet)
    return out


def collapse_blank(lines):
    out, blank = [], 0
    for l in lines:
        if l.strip() == "":
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(l)
    return out


# ============================================ 阶段 4：标题注入（outline）
# PDF 提取文本没有标题层级，需按文档目录或编号规则反推，
# 使段落可按标题寻址（否则只能整篇线性读）。

TOC_RE = re.compile(r'^(.*?)\.{4,}\s*(\d+)\s*$')
NUM_RE = re.compile(r'^(\d+(?:\.\d+)*)\.?\s*(.*)$')
NUM_ONLY_RE = re.compile(r'^(\d+(?:\.\d+)*)(\.?)$')
NUM_INLINE_RE = re.compile(r'^(\d{1,3}(?:\.\d+){0,4})[\.\s、:：]{1,4}(\S.*)$')


def detect_toc(lines, lo=0, hi=None):
    """策略 toc：解析点线目录（`标题......页码`），得到 (编号, 标题) 清单"""
    hi = hi if hi is not None else len(lines)
    entries = []
    for l in lines[lo:hi]:
        m = TOC_RE.match(l)
        if not m:
            continue
        m2 = NUM_RE.match(m.group(1).strip())
        if m2:
            entries.append((m2.group(1), m2.group(2).strip()))
        else:
            entries.append((None, m.group(1).strip()))
    return entries


def locate_by_toc(lines, entries, body_start, window=1200):
    """按目录条目在正文中顺序向后查找，避免表格数据行误匹配"""
    NL = len(lines)

    def norm(s):
        return re.sub(r'[\s\|]', '', s)

    def find(num, title, start, stop):
        p = re.compile(r'^[\s\|\-\*#>]*' + re.escape(num) + r'(?!\.?\d)')
        tn = norm(title)
        for i in range(start, min(stop, NL)):
            l = lines[i]
            if not p.match(l):
                continue
            if tn and tn not in norm(l):
                continue
            return i
        return None

    out, cur = [], body_start
    for num, title in entries:
        if not num:
            continue
        hit = find(num, title, cur, cur + window)
        if hit is None:
            hit = find(num, title, body_start, NL)
        if hit is not None and hit >= cur:
            cur = hit
        if hit is not None:
            out.append({"line": hit + 1, "num": num, "title": title})
    return out


def _num_tuple(num):
    """'2.1.3' → (2, 1, 3)，供编号序关系比较"""
    try:
        return tuple(int(x) for x in num.strip().rstrip('.').split('.'))
    except Exception:
        return ()


def _plausible_title(title):
    """标题像不像「章节名」：过滤 `10.5 V`、`3 秒` 这类数值行"""
    if not title:
        return False
    if title[0].isdigit():
        return False
    if not re.match(r'^[\u4e00-\u9fffA-Za-z]', title):
        return False
    has_cjk = bool(re.search(r'[\u4e00-\u9fff]', title))
    if not has_cjk and len(title) < 4:      # 纯英文标题太短多半是单位/符号
        return False
    if re.fullmatch(r'[A-Za-z%℃°]{1,3}', title):
        return False
    if len(title) > 60:
        return False
    return not re.search(r'[。；：,，]', title)


def _heading_seq_filter(cands):
    """按「编号递增 + 层级最多跳一级」过滤候选标题，压制误判。

    PDF 正文里 `10.5 V`、`3 秒` 这类行很容易被误认为标题；
    真正的章节编号总是单调递增且不会连续跨级，据此可滤掉绝大多数噪声。
    """
    out, prev = [], None
    for h in cands:
        t = _num_tuple(h["num"])
        if not t:
            continue
        if prev is not None:
            pt, pl = prev
            if t < pt:                      # 编号回退 → 不是章节
                continue
            if len(t) > len(pt) + 1:        # 跨级 → 丢弃
                continue
        out.append(h)
        prev = (t, len(t))
    return out


def detect_numbered(lines, inline=True):
    """策略 numbered / auto：识别编号标题，两种排版都支持。

    形式一（编号独占一行）：
        1.1
        电源管理
    形式二（编号与标题同行，PDF 常见）：
        1.1  电源管理
    """
    N = len(lines)
    out = []
    for i, l in enumerate(lines):
        s = l.strip()
        m = NUM_ONLY_RE.match(s)
        if m:
            num, trail = m.group(1), m.group(2) == '.'
            parts = num.split('.')
            bad = ((trail and len(parts) != 1) or (not trail and len(parts) < 2)
                   or not all(p.isdigit() and int(p) >= 1 for p in parts))
            if bad:
                continue
            j = i + 1
            while j < N and not lines[j].strip():
                j += 1
            if j >= N:
                continue
            title = lines[j].strip()
            if not _plausible_title(title):
                continue
            out.append({"line": i + 1, "num": num, "title": title,
                        "consume": list(range(i, j + 1))})
            continue
        if not inline:
            continue
        m = NUM_INLINE_RE.match(s)
        if not m:
            continue
        num, title = m.group(1), m.group(2).strip()
        parts = num.split('.')
        if not all(p.isdigit() and int(p) >= 1 for p in parts):
            continue
        if not _plausible_title(title):
            continue
        out.append({"line": i + 1, "num": num, "title": title, "consume": [i]})
    return _heading_seq_filter(out)


def drop_dup_title(lines, title):
    """去掉正文开头与文档标题重复的那一行。

    markitdown 转换 PDF/Word 时，首页大标题或文档标题往往被重复转换一次
    （`# 诊断规范 V1.1` 或裸文本 `整机功能规范 V3.2`），而 cmd_outline 又会
    补一个 `# <title>`，于是同一标题在正文里出现两遍。这里在注入前先删掉。
    """
    def key(s):
        return re.sub(r'[\s_\-–—·．.#]+', '', s or "")

    nt = key(title)
    if not nt:
        return lines
    for i, l in enumerate(lines[:10]):
        s = l.strip()
        if not s:
            continue
        if key(s) == nt and not s.startswith('|'):
            return lines[:i] + lines[i + 1:]
    return lines


def inject_headings(body, heads, table_split=True):
    """按 (行号 -> 标题) 映射把标题行替换为 Markdown 标题"""
    hmap = {h["line"]: h for h in heads}
    drops = set()
    for h in heads:
        if "consume" in h:
            drops.update(h["consume"])
        else:
            drops.add(h["line"])
    drops = {d - 1 for d in drops}

    res, recs, N = [], [], len(body)
    for i in range(N):
        ln1 = i + 1
        h = hmap.get(ln1)
        if h is not None:
            depth = h["num"].rstrip('.').count('.')
            lvl = min(depth + 2, 6)
            title = re.sub(r'\s+', ' ', (h["num"] + ' ' + h["title"])).strip()
            raw = body[i]
            if raw.lstrip().startswith('|') and table_split:
                ncol = max(2, len(raw.strip().strip('|').split('|')))
                res += ['', '#' * lvl + ' ' + title, '',
                        '|' + '  |' * ncol, '|' + ' --- |' * ncol, raw.rstrip()]
                if i + 1 < N and SEP_ROW.match(body[i + 1].strip()):
                    drops.add(i + 1)
            else:
                res += ['', '#' * lvl + ' ' + title, '']
                rest = body[i].strip()
                rest = re.sub(r'^[\s\|\-\*#>]*' + re.escape(h["num"]) + r'\.?\s*', '', rest)
                if h["title"] and h["title"] in rest:
                    rest = rest.replace(h["title"], '', 1).strip(' |')
                if len(rest) > 2 and not SEP_ROW.match(rest):
                    res.append(rest)
            recs.append({"num": h["num"], "title": h["title"], "lvl": lvl,
                         "out_line": len(res)})
            continue
        if i in drops:
            continue
        res.append(body[i].rstrip())
    return res, recs


# --------------------------------------------------- 断行合并（PDF 散文）
NO_JOIN_TAIL = '。；：！？、，）】》”"’…；:;,.)]}>-—*'


def is_prose(s):
    t = s.strip()
    if not t:
        return False
    if t[0] in '|#>-*+·•':
        return False
    if re.match(r'^\d+(\.\d+)*[\.\s、]', t):
        return False
    if re.match(r'^(图|表|注|第|Figure|Table)\s*[\.\d]', t):
        return False
    if len(t) > 200:
        return False
    return True


def join_prose(lines, maxlen=160):
    """把 PDF 硬换行合回整段（保守：行尾为句读/括号时不合并）"""
    out, i, N = [], 0, len(lines)
    while i < N:
        cur = lines[i]
        if not is_prose(cur):
            out.append(cur)
            i += 1
            continue
        buf = cur.rstrip()
        while i + 1 < N:
            nxt = lines[i + 1]
            if not is_prose(nxt):
                break
            if buf and buf[-1] in NO_JOIN_TAIL:
                break
            nn = nxt.strip()
            if not nn or len(buf) + len(nn) > maxlen:
                break
            sep = ' ' if (buf and buf[-1].isascii() and buf[-1].isalnum()
                          and nn[0].isascii() and nn[0].isalnum()) else ''
            buf = buf + sep + nn
            i += 1
        out.append(buf)
        i += 1
    return out


def finalize(lines):
    """收尾：压缩空行 + 丢弃孤立表格分隔行"""
    out = collapse_blank(lines)
    res = []
    for l in out:
        if SEP_ROW.match(l.strip()):
            prev = res[-1] if res else ''
            if not prev.strip().startswith('|'):
                continue
        res.append(l)
    return collapse_blank(res)


def cmd_outline(cfg, st, dirty, quiet=False):
    """norm → md/<NN>_*.md（注入标题、清页眉、合断行）"""
    root = cfg["_root"]
    nrm = os.path.join(root, cfg["work_dir"], "norm")
    raw = os.path.join(root, cfg["work_dir"], "raw")
    md = ensure_dir(os.path.join(root, cfg["output_dir"], "md"))
    rmd = ensure_dir(os.path.join(md, "_raw"))
    files = {f["relpath"]: f for f in expand_sources(cfg)}
    report = {"docs": []}

    for rp in sorted(dirty):
        f = files.get(rp)
        rec = st.get(rp) or {}
        no = rec.get("docno") or "00"
        spec = f["spec"] if f else (match_spec_for_doc(cfg, rp) or {})
        title = spec.get("title") or os.path.splitext(os.path.basename(rp))[0]
        kind = spec.get("kind") or detect_kind(f["abspath"] if f else rp)

        src = os.path.join(nrm, "%s_norm.md" % no)
        if not os.path.exists(src):
            src = os.path.join(raw, "%s_raw.md" % no)
        if not os.path.exists(src):
            log("    · 跳过 %s（无中间产物）" % rp, quiet)
            continue
        lines = read_text(src).split("\n")
        if kind in ("pdf", "office"):
            # PDF/Word 首页大标题常与 title 重复，去掉以免正文里再出现一次
            lines = drop_dup_title(lines, title)

        ol = spec.get("outline") or {}
        strategy = ol.get("strategy", "auto")
        body, recs, quality = lines, [], {}

        try:
            if kind == "code":
                # 源码：整体包一层代码块，不做标题注入
                body = lines
                recs, quality = [], {"skip": "源码文档，不做标题注入"}
            elif kind == "markdown" and ol.get("archive", True) and strategy in ("auto", "manual", "none"):
                # 已是可读 md：仅统一 H1 + 加溯源头，保留原有层级（可选降级）
                body, recs, quality = archive_markdown(lines, title, rp, spec)
            elif strategy in ("toc",):
                # 目录条目从 body 之前的区域提取（toc_lines 指定），
                # 再到 body_start 之后的正文里定位，避免在目录自身上匹配。
                entries = detect_toc(lines, *ol.get("toc_lines", [0, ol.get("body_start", 1)]))
                off = ol.get("body_start", 1) - 1
                b = lines[off:]
                heads = locate_by_toc(lines, entries, off)
                fixed = [{"line": h["line"] - off, "num": h["num"], "title": h["title"]}
                         for h in heads if h["line"] - off >= 1]
                body, recs = inject_headings(b, fixed)
                quality = outline_quality(body, recs, len(entries))
            elif strategy in ("numbered",):
                # 先按 body_start 切片再识别：目录里的点线行（`1.2 文档范围......1`）
                # 会污染 detect_numbered 的「编号单调递增」过滤器，把正文的 1.x 全判成回退。
                off = ol.get("body_start", 1) - 1
                b = lines[off:]
                heads = detect_numbered(b)
                fixed = [{"line": h["line"], "num": h["num"], "title": h["title"],
                          "consume": h["consume"]} for h in heads]
                body, recs = inject_headings(b, fixed)
                quality = outline_quality(body, recs, None)
            elif strategy == "markitdown":
                body = lines
                recs = [{"num": "", "title": h["text"], "lvl": h["depth"], "out_line": h["line"]}
                        for h in scan_headings(lines)]
            elif strategy == "manual":
                # 由 spec["outline"]["headings"] 显式给出 [(行号, 层级, 标题)]
                manual = ol.get("headings") or []
                res = []
                hmap = {int(h[0]): h for h in manual}
                for i, l in enumerate(lines, 1):
                    if i in hmap:
                        _, lvl, t = hmap[i]
                        res += ["", "#" * int(lvl) + " " + t, ""]
                        recs.append({"num": "", "title": t, "lvl": int(lvl), "out_line": len(res)})
                    else:
                        res.append(l.rstrip())
                body = res
            elif strategy == "hybrid":
                # 先目录，缺失部分用编号行启发式补齐。
                # 目录条目取自 body 之前，编号行在 body 之内识别，两者都不碰目录自身。
                off = ol.get("body_start", 1) - 1
                entries = detect_toc(lines, *ol.get("toc_lines", [0, ol.get("body_start", 1)]))
                hits = locate_by_toc(lines, entries, off)
                have = {h["line"] for h in hits}
                extra = [h for h in detect_numbered(lines[off:]) if (h["line"] + off) not in have]
                heads = sorted(hits + [{"line": h["line"] + off, "num": h["num"],
                                        "title": h["title"]} for h in extra],
                               key=lambda x: x["line"])
                fixed = [{"line": h["line"] - off, "num": h["num"], "title": h["title"]}
                         for h in heads if h["line"] - off >= 1]
                b = lines[off:]
                body, recs = inject_headings(b, fixed)
                quality = outline_quality(body, recs, len(entries))
            elif strategy in ("auto", "numbered-auto", "none") or strategy is None:
                # auto：PDF/Office 优先按「编号行」注入，其次目录，最后原样
                # 同样先切 body_start 再识别，避免目录段（含点线页码）污染单调性过滤。
                off = ol.get("body_start", 1) - 1
                nheads = detect_numbered(lines[off:])
                entries = detect_toc(lines, *ol.get("toc_lines", [0, len(lines)]))
                min_n = int(ol.get("min_numbered", 3))
                if len(nheads) >= min_n:
                    fixed = [{"line": h["line"], "num": h["num"], "title": h["title"],
                              "consume": h["consume"]} for h in nheads]
                    body, recs = inject_headings(lines[off:], fixed)
                    quality = outline_quality(body, recs, len(entries) or None)
                    quality["strategy_used"] = "auto→numbered"
                elif len(entries) >= 2:
                    hits = locate_by_toc(lines, entries, ol.get("body_start", 1) - 1)
                    off = ol.get("body_start", 1) - 1
                    fixed = [{"line": h["line"] - off, "num": h["num"], "title": h["title"]}
                             for h in hits if h["line"] - off >= 1]
                    body, recs = inject_headings(lines[off:], fixed)
                    quality = outline_quality(body, recs, len(entries))
                    quality["strategy_used"] = "auto→toc"
                else:
                    body = lines
                    recs = [{"num": "", "title": h["text"], "lvl": h["depth"], "out_line": h["line"]}
                            for h in scan_headings(lines)]
                    quality = outline_quality(body, recs, None)
                    quality["strategy_used"] = "auto→沿用源标题" if recs else "auto→原样"
                    if not recs:
                        quality.setdefault("notes", []).append(
                            "未识别到编号标题或目录；如需注入请改用 outline.strategy = manual")
            else:
                body = lines
                recs = [{"num": "", "title": h["text"], "lvl": h["depth"], "out_line": h["line"]}
                        for h in scan_headings(lines)]
        except Exception as e:
            log("    ✗ 标题注入失败 %s：%s（降级为原样输出）" % (rp, e), quiet)
            body, recs = lines, []

        if kind != "markdown":
            # Office/PPT 自带 H1（文档标题、每页幻灯片标题）会让全篇出现多个 H1，
            # 统一降一级；markdown 归档分支已在 archive_markdown 中去过原 H1。
            demoted = demote_source_h1(body, bool(ol.get("keep_source_h1")))
            if demoted != body:
                body = demoted
                # 重新扫描以拿到降级后的层级，并用标题回填编号，保住 num 信息
                bnum = {r.get("title"): r.get("num") for r in recs}
                recs = [{"num": bnum.get(h["text"], ""), "title": h["text"],
                         "lvl": h["depth"], "out_line": h["line"]}
                        for h in scan_headings(body)]

        if cfg["normalize"].get("join_prose", True) and kind in ("pdf", "office"):
            body = join_prose(body)
        if ol.get("strip_footer", True):
            body = [l for l in body if not any(p.match(l.strip()) for p in
                                               [re.compile(x) for x in DEFAULT_FOOTERS] if l.strip())]

        hdr = ["# " + title, "",
               "> 来源：`%s`（%s）  " % (rp, spec.get("source_note") or kind.upper()),
               "> 转换：%s" % (spec.get("convert_note") or "d2m 自动转换") if spec.get("convert_note")
               else "> 转换：d2m 自动转换（编号 %s）" % no,
               "", "---", ""]
        out = finalize(hdr + body)
        fn = "%s_%s.md" % (no, safe_name(title))
        write_text(os.path.join(md, fn), "\n".join(out))
        # 归一化中间产物留档（未注入标题，供溯源比对）
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(rmd, "%s_%s.raw.md" % (no, safe_name(title))))

        rec.setdefault("stages", {})["outline"] = "ok"
        rec.update({"title": title, "file": "md/" + fn, "lines": len(out),
                    "chars": sum(len(x) for x in out), "headings": len(recs)})
        st.put(rp, rec)
        report["docs"].append({"no": no, "title": title, "file": "md/" + fn,
                               "lines": len(out), "headings": len(recs),
                               "quality": quality, "strategy": strategy})
        q = ""
        if quality:
            q = "　质量 %.2f" % quality.get("score", 0)
        log("    ✓ 成文 %-14s %6d 行  标题 %4d%s" % (fn[:14], len(out), len(recs), q), quiet)
    return report


def demote_source_h1(body, keep=False):
    """把正文里源文档自带的 H1 降为 H2，保证整篇只有注入的那一个 H1。

    markitdown 转换 Word/PPT 时会把文档标题或每页幻灯片标题输出为 H1，
    紧接着 cmd_outline 又会补一个 `# <title>`，导致一篇出现 2~3 个 H1。
    这里统一降级（跳过 ``` 围栏内的内容），recs 之后再重新扫描。
    由 spec["outline"]["keep_source_h1"] = true 可关闭。
    """
    if keep:
        return list(body)
    out, fid = [], False
    for l in body:
        if re.match(r'^\s*(```|~~~)', l):
            fid = not fid
            out.append(l)
            continue
        m = re.match(r'^#(\s+.*)$', l) if not fid else None
        if m:
            out.append('#' + '#' + m.group(1))
        else:
            out.append(l)
    return out


def archive_markdown(lines, title, rp, spec):
    """已是可读 md：去掉原 H1，统一加新 H1 与溯源头；可选整体降级"""
    body = list(lines)
    # 去掉开头的原 H1（仅第一处）
    for i, l in enumerate(body):
        if re.match(r'^#\s+\S', l):
            body.pop(i)
            break
    shift = int((spec.get("outline") or {}).get("demote", 0) or 0)
    if shift > 0:
        fid = False
        res = []
        for l in body:
            if re.match(r'^\s*```', l):
                fid = not fid
            m = re.match(r'^(#{1,6})(\s+.*)$', l) if not fid else None
            if m:
                res.append("#" * min(6, len(m.group(1)) + shift) + m.group(2))
            else:
                res.append(l)
        body = res
    recs = [{"num": "", "title": h["text"], "lvl": h["depth"], "out_line": h["line"]}
            for h in scan_headings(body)]
    return body, recs, {}


def outline_quality(body, recs, expected):
    """标题注入质量评分：层级分布是否合理、编号是否连续、是否有深度跳变"""
    if not recs:
        return {"score": 0.0, "levels": {}, "notes": ["未识别到任何标题"]}
    lv = collections.Counter(r["lvl"] for r in recs)
    nums = [r["num"] for r in recs if r.get("num")]
    notes = []
    score = 1.0
    if expected:
        cov = len(recs) / max(1, expected)
        if cov > 1.2:
            notes.append("识别标题数(%d)显著多于目录条目(%d)，可能存在误判" % (len(recs), expected))
        elif cov < 0.8:
            notes.append("识别标题数(%d)少于目录条目(%d)，可能有漏识别" % (len(recs), expected))
        score *= min(1.0, max(0.0, 1.0 - abs(1 - cov)))
    # 层级跳变检测
    prev = None
    jumps = 0
    for r in recs:
        if prev is not None and r["lvl"] - prev > 2:
            jumps += 1
        prev = r["lvl"]
    if jumps:
        notes.append("存在 %d 处层级跳变（如 ## 直接到 #####）" % jumps)
        score *= max(0.5, 1.0 - jumps * 0.02)
    # 编号连续性：正常应严格递增，出现 a >= b 即异常
    if nums:
        bad = sum(1 for a, b in zip(nums, nums[1:]) if not _num_lt(a, b))
        if bad > len(nums) * 0.05:
            notes.append("编号顺序异常 %d 处" % bad)
            score *= 0.9
    return {"score": round(score, 3), "levels": {str(k): v for k, v in sorted(lv.items())},
            "notes": notes, "count": len(recs)}


def _num_lt(a, b):
    """编号 a 是否小于 b（'2.1' < '10' 需按数值而非字典序比较）"""
    try:
        pa = [int(x) for x in str(a).rstrip('.').split('.')]
        pb = [int(x) for x in str(b).rstrip('.').split('.')]
        return pa < pb
    except Exception:
        return False


# ============================================ 阶段 5：结构化（structure）
def clean_grid(df):
    """把 DataFrame 变为规整的字符串网格：去全空行全空列"""
    grid = [[cell(v) for v in row] for row in df.values.tolist()]
    grid = [r for r in grid if any(r)]
    if not grid:
        return []
    ncol = max(len(r) for r in grid)
    grid = [r + [""] * (ncol - len(r)) for r in grid]
    keep = [c for c in range(ncol) if any(r[c] for r in grid)]
    return [[r[c] for c in keep] for r in grid]


def ne(r):
    return sum(1 for x in r if x)


DATA_RE = re.compile(r'^(\d+|[0-9A-Fa-f]{2,}|0x[0-9A-Fa-f]+)$')


def first_ne(r):
    for i, v in enumerate(r):
        if v:
            return i, v
    return None, None


def is_data_row(r):
    i, v = first_ne(r)
    if i is None or ne(r) < 2:
        return False
    return i <= 2 and bool(DATA_RE.match(v))


def split_header(grid, maxhdr=6):
    """识别多层表头并纵向合并为单行（<br> 分隔）；返回 (前导行, 表头, 注释行, 数据体)"""
    h = None
    for i, r in enumerate(grid):
        if ne(r) >= 3 and (i == 0 or ne(grid[i - 1]) <= 1):
            h = i
            break
    if h is None:
        h = 0
    pre = grid[:h]
    block = [grid[h]]
    notes = []
    j = h + 1
    blanks = 0
    while j < len(grid) and len(block) < maxhdr:
        r = grid[j]
        if is_data_row(r):
            break
        n = ne(r)
        if n == 0:
            blanks += 1
            if blanks >= 2:
                break
        elif n >= 2:
            blanks = 0
            block.append(r)
        else:
            notes.append(r)
        j += 1
    ncol = len(block[0])
    hdr = []
    for c in range(ncol):
        parts = []
        for r in block:
            v = r[c] if c < len(r) else ""
            if v and v not in parts:
                parts.append(v)
        hdr.append("<br>".join(parts))
    return pre, hdr, notes, grid[j:]


def norm_structured(spec):
    """归一化 spec["structured"] 的两种写法，返回 (类型名, 补全后的 spec)。

    支持：
      "structured": "can_matrix"
      "structured": {"kind": "can_matrix", "sheet": "报文", "structured_rules": {...}}
    字典写法里的其余键会被提升到 spec 顶层（与顶层同名键冲突时以字典内为准），
    这样 structure_can_matrix / structure_generic_sheets 无需关心写法差异。
    """
    s = spec.get("structured")
    if not s:
        return "", spec
    if isinstance(s, str):
        return s, spec
    if isinstance(s, dict):
        kind = s.get("kind") or s.get("type") or ""
        out = dict(spec)
        for k, v in s.items():
            if k not in ("kind", "type"):
                out[k] = v
        return kind, out
    return "", spec


def structure_generic_sheets(path, spec, title, quiet=False):
    """通用多表头 sheet 渲染：每 sheet 一节，多层表头纵向合并"""
    import pandas as pd
    xl = pd.ExcelFile(path)
    titles = spec.get("sheet_titles") or {}
    rules = spec.get("structured_rules") or {}
    maxhdr = int(rules.get("header_max_rows", 6))
    sheets = spec.get("sheets") or xl.sheet_names

    out = ["## 工作表索引", "", "| # | 工作表 | 中文名 | 数据行 |",
           "| --- | --- | --- | --- |"]
    parsed = {}
    for i, s in enumerate(sheets, 1):
        g = clean_grid(xl.parse(s, header=None))
        body = split_header(g, maxhdr)[3] if g else []
        body = [r for r in body if any(r)]
        parsed[s] = (g, body)
        out.append("| %d | `%s` | %s | %d |" % (i, s.strip(), titles.get(s, ""), len(body)))
    out.append("")

    for s in sheets:
        g, _ = parsed[s]
        out += ["## %s%s" % (s.strip(), ("　" + titles[s]) if titles.get(s) else ""), ""]
        if not g:
            out += ["_（空表）_", ""]
            continue
        pre, hdr, notes, body = split_header(g, maxhdr)
        for r in pre:
            vals = [x for x in r if x]
            if vals:
                out += ["**%s**" % " ".join(vals), ""]
        for r in notes:
            vals = [x for x in r if x]
            if vals:
                out += ["> %s" % " ".join(vals), ""]
        body = [r for r in body if any(r)]
        if body:
            out += md_table(hdr, body)
        out.append("")
    return "\n".join(out)


def structure_can_matrix(path, spec, title, quiet=False):
    """CAN 矩阵：按报文边界切成每报文一节 + 报文速查表"""
    import pandas as pd
    rules = spec.get("structured_rules") or {}
    sheet = spec.get("sheet", 0)
    xl = pd.ExcelFile(path)
    if isinstance(sheet, str):
        df = xl.parse(sheet, header=None)
    else:
        df = xl.parse(xl.sheet_names[sheet], header=None)
    hr = int(rules.get("column_header_row", 0))
    HDR = [cell(x) for x in df.iloc[hr].tolist()]

    name_col = int(rules.get("name_col", 1))
    id_col = int(rules.get("id_col", 3))
    nlo, nhi = rules.get("node_cols", [27, 38])
    sig_cols = rules.get("signal_cols")
    speed_cols = rules.get("speed_cols") or {}
    explicit_hdr = rules.get("signal_headers")

    def senders(row):
        res = []
        for c in range(int(nlo), int(nhi) + 1):
            v = row.iat[c] if c < len(row) else None
            try:
                if pd.notna(v) and str(v).strip().lower() == "s":
                    res.append(cell(HDR[c]))
            except Exception:
                pass
        return "/".join(res)

    msgs, cur = [], None
    for i in range(hr + 1, df.shape[0]):
        row = df.iloc[i]
        name = row.iat[name_col] if name_col < len(row) else None
        mid = row.iat[id_col] if id_col < len(row) else None
        if pd.notna(name) and pd.notna(mid):
            cur = {"name": cell(name), "id": cell(mid), "row": i, "rows": [i]}
            msgs.append(cur)
        elif cur is not None:
            cur["rows"].append(i)

    if not sig_cols:
        # 未显式声明则自动推断：把非空且非节点列视作信号列
        sig_cols = [c for c in range(len(HDR))
                    if c not in (name_col, id_col) and c < int(nlo)]
    sig_hdr = explicit_hdr or [cell(HDR[c]) if c < len(HDR) else "列%d" % c for c in sig_cols]

    def idkey(m):
        try:
            return int(str(m["id"]), 16)
        except Exception:
            return 0xFFFF

    order = sorted(msgs, key=idkey)
    out = ["## 报文速查表", ""]
    rows = []
    for m in order:
        r0 = df.iloc[m["row"]]
        rows.append([m["id"], m["name"],
                     cell(r0.iat[speed_cols.get("len", 6)]) if "len" in speed_cols else "",
                     cell(r0.iat[speed_cols.get("cycle", 5)]) if "cycle" in speed_cols else "",
                     senders(r0), str(len(m["rows"]))])
    out += md_table(["MessageID", "MessageName", "长度(B)", "CycleTime", "发送节点", "信号数"], rows)
    out.append("")
    for m in order:
        r0 = df.iloc[m["row"]]
        parts = []
        for label, ci in (rules.get("meta_cols") or {}).items():
            parts.append("%s：`%s`" % (label, cell(r0.iat[int(ci)]) or "-"))
        out += ["### %s  %s" % (m["id"], m["name"]), ""]
        if parts:
            out += ["- " + "　".join(parts), ""]
        srows = []
        for i in m["rows"]:
            row = df.iloc[i]
            vals = [cell(row.iat[c]) if c < len(row) else "" for c in sig_cols]
            if not vals[0]:
                continue
            srows.append(vals)
        if srows:
            out += md_table(sig_hdr, srows)
        else:
            out.append("_（无信号行）_")
        out.append("")
    return "\n".join(out)


def cmd_structure(cfg, st, dirty, quiet=False):
    """结构化的表格类文档 → md（每报文/每 sheet 一节）"""
    root = cfg["_root"]
    md = ensure_dir(os.path.join(root, cfg["output_dir"], "md"))
    rmd = ensure_dir(os.path.join(md, "_raw"))
    files = {f["relpath"]: f for f in expand_sources(cfg)}
    report = {"docs": []}
    for rp in sorted(dirty):
        f = files.get(rp)
        rec = st.get(rp) or {}
        no = rec.get("docno") or "00"
        spec = f["spec"] if f else (match_spec_for_doc(cfg, rp) or {})
        stype, spec = norm_structured(spec)
        if not stype:
            continue
        if f is None:
            abspath = os.path.join(root, rp)
        else:
            abspath = f["abspath"]
        title = spec.get("title") or os.path.basename(rp)
        try:
            if stype == "can_matrix":
                body = structure_can_matrix(abspath, spec, title, quiet)
            elif stype == "generic_sheets":
                body = structure_generic_sheets(abspath, spec, title, quiet)
            else:
                log("    · 未知 structured 类型 %s，跳过 %s" % (stype, rp), quiet)
                continue
        except Exception as e:
            log("    ✗ 结构化失败 %s：%s" % (rp, e), quiet)
            continue
        hdr = ["# " + title, "",
               "> 来源：`%s`（%s）  " % (rp, spec.get("source_note") or "Excel"),
               "> 转换：d2m 结构化重建（结构化类型 `%s`）" % stype,
               "> 说明：%s" % spec["convert_note"] if spec.get("convert_note") else "",
               "", "---", ""]
        hdr = [x for x in hdr if x is not None]
        out = finalize(hdr + body.split("\n"))
        fn = "%s_%s.md" % (no, safe_name(title))
        write_text(os.path.join(md, fn), "\n".join(out))
        # 保留 raw 以便溯源
        nrm = os.path.join(root, cfg["work_dir"], "norm", "%s_norm.md" % no)
        rawf = os.path.join(root, cfg["work_dir"], "raw", "%s_raw.md" % no)
        for cand in (nrm, rawf):
            if os.path.exists(cand):
                shutil.copyfile(cand, os.path.join(rmd, "%s_%s.raw.md" % (no, safe_name(title))))
                break
        rec.setdefault("stages", {})["structure"] = "ok"
        rec.update({"title": title, "file": "md/" + fn, "lines": len(out),
                    "chars": sum(len(x) for x in out),
                    "headings": len(scan_headings(out))})
        st.put(rp, rec)
        report["docs"].append({"no": no, "title": title, "file": "md/" + fn,
                               "lines": len(out), "headings": len(scan_headings(out)),
                               "quality": {}, "strategy": "structured:" + stype})
        log("    ✓ 结构 %-14s %6d 行" % (fn[:14], len(out)), quiet)
    return report


# ============================================ 阶段 6：索引（index）
def nearest_head(heads, line):
    """取 line 之前最近的一个标题"""
    best = None
    for h in heads:
        if h["line"] <= line:
            best = h
        else:
            break
    return best


def docs_needing(cfg, st, changed, stage, cfg_changed=False, force=False,
                 expensive=False):
    """判断哪些文档需要重跑某阶段。

    expensive=True 的阶段（convert/normalize/outline/structure）只在源文件变化时重跑；
    聚合类阶段（index/chunk）额外响应配置变化，因为术语表/分块参数变了需要重算。
    """
    out = set()
    for rp, rec in st.data["docs"].items():
        if is_external(rec):          # 外部产物：任何阶段都不重跑
            continue
        if force or rp in changed:
            out.add(rp)
            continue
        if cfg_changed and not expensive:
            out.add(rp)
            continue
        mark = (rec.get("stages") or {}).get(stage)
        if mark == "ok":
            continue
        # adopt 接管来的正文阶段产物是外部脚本生成的，视为已就绪：
        # 不重新转换/注入标题，避免把别人已经修好的正文再跑一遍反而变差。
        if mark == "adopted" and expensive:
            continue
        out.add(rp)
    return out


def doc_records(cfg, st):
    """全部在册文档 + 配置元数据，按编号排序（编号即稳定排序键）"""
    files = {f["relpath"]: f for f in expand_sources(cfg)}
    docs = []
    for rp, rec in st.data["docs"].items():
        spec = (files.get(rp) or {}).get("spec") or match_spec_for_doc(cfg, rp) or {}
        d = dict(rec)
        # 外部产物在台账里用 "<external>/..." 作键，对外仍应显示其真实文件路径
        d["relpath"] = (rec.get("file") or rp) if is_external(rec) else rp
        d["spec"] = spec
        d["title"] = (rec.get("title") or spec.get("title")
                      or os.path.splitext(os.path.basename(rp))[0])
        d["category"] = rec.get("category") or spec.get("category") or "未分类"
        d["summary"] = rec.get("summary") or spec.get("summary") or ""
        # 「何时用」是入口文档里最有价值的一列，允许配置显式给出
        d["when_to_use"] = rec.get("when_to_use") or spec.get("when_to_use") or ""
        if not d.get("keywords"):
            d["keywords"] = spec.get("keywords") or []
        docs.append(d)
    docs.sort(key=lambda x: str(x.get("docno") or "99"))
    return docs


def term_hits_for(cfg, md_path, kws):
    """统计术语在各章节的命中次数 → [(章节标题, 次数)]，用于倒排索引"""
    if not kws or not os.path.exists(md_path):
        return []
    lines = read_text(md_path).split("\n")
    # 排除 H1 文档标题，避免整篇命中都堆到文档标题上（降噪）
    heads = [h for h in scan_headings(lines) if h["depth"] >= 2]
    if not heads:
        return []
    cnt = collections.Counter()
    for i, l in enumerate(lines):
        if any(kw in l for kw in kws):
            h = nearest_head(heads, i + 1)
            if h:
                cnt[h["text"].strip()] += 1
    ic = cfg["index"]
    strong = [(t, n) for t, n in cnt.most_common(ic.get("terms_per_doc", 8))
              if n >= ic.get("min_hits", 2)]
    if not strong:
        strong = cnt.most_common(3)      # 兜底：全弱命中时也给前 3，避免术语彻底消失
    return strong


def term_name(t):
    return t.get("name") or (t.get("keywords") or ["?"])[0]


def is_external(rec):
    """外部产物：磁盘上已有正文/索引，但不由本工具生成（无源文件或源不可解析）。

    典型例子：「PDC 仿真器代码地图」这种由别的脚本从一批源码汇总出来的成文。
    本工具只负责把它登记进台账与总索引，正文与索引一律原样保留、绝不重写、
    绝不清理，编号也不会被别人占用。
    """
    return bool((rec or {}).get("external"))


def index_relpath(cfg, d):
    """该文档章节索引的相对路径。

    adopt 接管的文档沿用**磁盘上原有的**索引文件名（记在 rec["index_file"]），
    否则按 `<NN>_<标题安全化>.md` 计算。这样「先用别的脚本产出、后引入本工具」
    的库不会因为标题写法不同而被重命名一遍，历史引用（CLAUDE.md、外部笔记）
    也不会失效。
    """
    return d.get("index_file") or "index/%s_%s.md" % (
        d.get("docno") or "00", safe_name(d.get("title") or ""))


def write_doc_index(cfg, st, d, quiet=False):
    """生成 index/<NN>_<标题>.md：标题层级分布 + 一级导航 + 全部标题（含行号）"""
    root = cfg["_root"]
    out_dir = ensure_dir(os.path.join(root, cfg["output_dir"], "index"))
    no = d.get("docno") or "00"
    md_path = os.path.join(root, cfg["output_dir"], d["file"]) if d.get("file") else None
    if not md_path or not os.path.exists(md_path):
        return None
    lines = read_text(md_path).split("\n")
    heads = scan_headings(lines)
    ip = os.path.join(root, cfg["output_dir"], index_relpath(cfg, d))
    out = ["# %s — 章节索引" % d["title"], "",
           "> 源文件：`%s/%s`　共 %d 行 / %s 字符"
           % (cfg["output_dir"], d["file"], len(lines), format(sum(len(x) for x in lines), ',')),
           "> 用法：AI 可用本节标题或行号直接定位，无需遍历全文。", "", "---", "",
           "## 标题层级分布", ""]
    for dep in sorted(set(h["depth"] for h in heads)):
        out.append("- `%s` 级标题：%d 个" % ("#" * dep, sum(1 for h in heads if h["depth"] == dep)))
    out.append("")
    if heads:
        top = [h for h in heads if h["depth"] == min(x["depth"] for x in heads)]
        if len(top) > 1:
            out += ["## 一级导航（目录）", "", "| 章节 | 行号 |", "| --- | --- |"]
            for h in top:
                out.append("| %s | %d |" % (h["text"].replace("|", "/"), h["line"]))
            out.append("")
    out += ["## 全部标题（含行号）", "", "| 级 | 标题 | 行号 |", "| --- | --- |"]
    for h in heads:
        out.append("| %s | %s | %d |" % ("#" * h["depth"], h["text"].replace("|", "/"), h["line"]))
    out.append("")
    write_text(ip, "\n".join(out))
    return "index/" + os.path.basename(ip)


def build_manifest(cfg, st, docs):
    """manifest.json：每篇的机器可读元数据 + 顶层章节 + 分块清单"""
    root = cfg["_root"]
    man = {"generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "pipeline_version": PIPELINE_VERSION,
           "root": cfg["output_dir"],
           "documents": []}
    for d in docs:
        md_path = os.path.join(root, cfg["output_dir"], d["file"]) if d.get("file") else None
        if not md_path or not os.path.exists(md_path):
            continue
        lines = read_text(md_path).split("\n")
        heads = scan_headings(lines)
        man["documents"].append({
            "no": d.get("docno") or "00",
            "file": d["file"],
            "relpath": d.get("relpath") or "",
            "title": d["title"],
            "category": d["category"],
            "summary": d["summary"],
            "when_to_use": d.get("when_to_use") or "",
            "lines": len(lines),
            "chars": sum(len(x) for x in lines),
            "headings": len(heads),
            "index": index_relpath(cfg, d),
            "keywords": d.get("keywords") or [],
            "top_sections": [{"text": h["text"], "line": h["line"]}
                             for h in heads if h["depth"] == 2][:60],
            "chunks": (st.get(d["relpath"]) or {}).get("chunks") or [],
        })
    write_text(os.path.join(root, cfg["output_dir"], "manifest.json"),
               json.dumps(man, ensure_ascii=False, indent=2))
    return man


def purge_orphans(cfg, st, quiet=False):
    """清掉已删除/退役文档遗留的产物文件（md / md/_raw / index），避免孤儿。
    编号冻结意味着退役编号不会复用，因此按 docno 判断即可。

    两类编号**不清**：
      · 配置里 preserve_docnos 声明保留的（外部脚本维护的产物）；
      · 已登记在 orphan_keep 里的具体文件。
    """
    root = cfg["_root"]
    outdir = os.path.join(root, cfg["output_dir"])
    alive = {str((r or {}).get("docno")) for r in st.data["docs"].values()}
    alive |= expand_reserved(cfg, st)
    keep = {os.path.normcase(os.path.normpath(x)) for x in (st.data.get("orphan_keep") or [])}
    removed = []
    targets = [os.path.join(outdir, "md"),
               os.path.join(outdir, "md", "_raw"),
               os.path.join(outdir, "index")]
    for dirp in targets:
        if not os.path.isdir(dirp):
            continue
        for f in sorted(os.listdir(dirp)):
            fp = os.path.join(dirp, f)
            if not os.path.isfile(fp) or not f.lower().endswith(".md"):
                continue
            rel = os.path.relpath(fp, root)
            if os.path.normcase(os.path.normpath(rel)) in keep:
                continue
            m = re.match(r'^(\d+)[_\-]', f)
            if m and m.group(1) not in alive:
                os.remove(fp)
                removed.append(rel)
    if removed and not quiet:
        log("    · 清理孤儿产物 %d 个：%s" % (len(removed), "、".join(removed[:6])
                                          + ("…" if len(removed) > 6 else "")))
    return removed


def cmd_index(cfg, st, dirty, cfg_changed=False, force=False, quiet=False):
    """章节索引 + manifest.json + inverted_index.json"""
    root = cfg["_root"]
    outdir = os.path.join(root, cfg["output_dir"])
    terms = cfg["terms"] or []
    purge_orphans(cfg, st, quiet)
    need = docs_needing(cfg, st, dirty, "index", cfg_changed, force)
    indexed = 0

    for d in doc_records(cfg, st):
        rp = d["relpath"]
        if rp not in need:
            continue
        ip = write_doc_index(cfg, st, d, quiet)
        if not ip:
            log("    · 跳过索引 %s（正文缺失）" % rp, quiet)
            continue
        indexed += 1
        md_path = os.path.join(outdir, d["file"])
        th = {}
        for t in terms:
            h = term_hits_for(cfg, md_path, t.get("keywords") or [term_name(t)])
            if h:
                th[term_name(t)] = h
        rec = st.get(rp) or {}
        rec["term_hits"] = th
        rec.setdefault("stages", {})["index"] = "ok"
        st.put(rp, rec)
        log("    ✓ 索引 %-44s" % os.path.basename(ip), quiet)

    docs = doc_records(cfg, st)
    man = build_manifest(cfg, st, docs)

    inv = collections.OrderedDict()
    for t in terms:
        nm = term_name(t)
        hits = collections.OrderedDict()
        for d in docs:
            th = (st.get(d["relpath"]) or {}).get("term_hits") or {}
            if th.get(nm):
                hits[d.get("docno") or "00"] = [{"section": s, "hits": n} for s, n in th[nm]]
        if hits:
            inv[nm] = hits
    write_text(os.path.join(outdir, "index", "inverted_index.json"),
               json.dumps(inv, ensure_ascii=False, indent=2))
    log("    ✓ manifest.json（%d 篇）　倒排术语 %d 项" % (len(man["documents"]), len(inv)), quiet)
    return {"manifest": man, "inverted": inv, "count": indexed}


# ============================================ 阶段 7：分块（chunk）
def cmd_chunk(cfg, st, dirty, cfg_changed=False, force=False, quiet=False):
    """超长文档按章节边界切块 → chunks/<NN>/<NN>-KK.md + CHUNK_INDEX.md"""
    root = cfg["_root"]
    outdir = os.path.join(root, cfg["output_dir"])
    CH = ensure_dir(os.path.join(outdir, "chunks"))
    target = int(cfg["chunk"].get("target_lines", 350))
    mindoc = int(cfg["chunk"].get("min_doc_lines", 250))
    need = docs_needing(cfg, st, dirty, "chunk", cfg_changed, force)

    docs = doc_records(cfg, st)
    rows, alive = [], set()
    for d in docs:
        rp, no = d["relpath"], d.get("docno") or "00"
        rec = st.get(rp) or {}
        md_path = os.path.join(outdir, d["file"]) if d.get("file") else None
        lines = read_text(md_path).split("\n") if (md_path and os.path.exists(md_path)) else []
        n = len(lines)
        alive.add(no)
        # 分块与索引一样属"派生且廉价"的阶段：adopt 来的文档也要按本工具的参数重算，
        # 才能保证块边界/溯源头部一致（只有 convert~structure 这类正文阶段才继承）。
        stale = (rp in need) or (rec.get("stages") or {}).get("chunk") != "ok"
        if stale:
            dn = os.path.join(CH, no)
            if os.path.isdir(dn):
                shutil.rmtree(dn)
            clist = []
            if n >= mindoc:
                heads = scan_headings(lines)
                bounds = sorted({h["line"] for h in heads if h["depth"] >= 2} | {n + 1})
                segs, cur = [], 1
                for b in bounds:
                    if b > cur and b - cur >= target:
                        segs.append((cur, b - 1))
                        cur = b
                if cur <= n:
                    segs.append((cur, n))
                # 尾块过小则并回上一块（避免出现 10 行的碎片块）
                if len(segs) >= 2 and segs[-1][1] - segs[-1][0] + 1 < target // 4:
                    segs = segs[:-2] + [(segs[-2][0], n)]
                # 只有 1 块 == 整篇时，分块没有收益，视为不分块
                if len(segs) >= 2:
                    ensure_dir(dn)
                    for k, (a, b) in enumerate(segs, 1):
                        covered = [h["text"] for h in heads
                                   if a <= h["line"] <= b and h["depth"] >= 2]
                        body = ["<!-- 分块 %s-%02d ｜ 源: %s ｜ 行 %d-%d -->" % (no, k, d["file"], a, b),
                                "# %s · 第 %02d 块（源文件第 %d-%d 行）" % (d["title"], k, a, b), "",
                                "> 源文件：`%s`　本块行号：%d-%d（共 %d 行）"
                                % (d["file"], a, b, b - a + 1),
                                "> 本块覆盖章节：%s"
                                % ("；".join(covered[:12]) + ("…" if len(covered) > 12 else "")
                                   if covered else "（无标题，为表格/附录延续段）"),
                                "", "---", ""]
                        body += lines[a - 1:b]
                        nm = "%s-%02d.md" % (no, k)
                        write_text(os.path.join(dn, nm), "\n".join(body).rstrip() + "\n")
                        clist.append({"no": "%s-%02d" % (no, k),
                                      "file": "chunks/%s/%s" % (no, nm),
                                      "start": a, "end": b, "lines": b - a + 1,
                                      "sections": covered[:40]})
            rec["chunks"] = clist
            rec.setdefault("stages", {})["chunk"] = "ok"
            st.put(rp, rec)
        clist = rec.get("chunks") or []
        rows.append({"no": no, "title": d["title"], "lines": n,
                     "count": len(clist), "chunks": clist})

    # 清理已退役文档遗留的分块目录
    for name in os.listdir(CH):
        p = os.path.join(CH, name)
        if os.path.isdir(p) and name not in alive:
            shutil.rmtree(p)

    # 分块索引 + 回写 manifest
    tgt = "%d" % target
    out = ["# 长文档分块索引", "",
           "> 目的：超长文档按**章节边界**切块（目标 %s 行/块），AI 可按需只读相关块，避免整篇加载。" % tgt,
           "> 用法：先查 `INDEX.md` §三 定位章节 → 再看本表找到包含该章节的块 → 读对应块文件。", "",
           "| # | 文档 | 分块情况 | 块行区间 |", "| --- | --- | --- | --- |"]
    for r in rows:
        rng = "、".join("%s:%d-%d" % (c["no"], c["start"], c["end"]) for c in r["chunks"])
        out.append("| %s | %s | %s | %s |"
                   % (r["no"], r["title"],
                      "%d 块" % r["count"] if r["count"] else "不分块（%d 行，可整篇读取）" % r["lines"],
                      rng))
    out += ["",
            "> 说明：行数 < %d 的文档不分块（整篇读取成本低于分块开销）；"
            "块内 `<!-- 分块 -->` 注释标明溯源与行号。" % mindoc, ""]
    write_text(os.path.join(CH, "CHUNK_INDEX.md"), "\n".join(out) + "\n")

    mf = os.path.join(outdir, "manifest.json")
    if os.path.exists(mf):
        man = json.load(open(mf, encoding="utf-8"))
        byno = {r["no"]: r["chunks"] for r in rows}
        for d in man.get("documents", []):
            d["chunks"] = byno.get(d["no"], [])
        write_text(mf, json.dumps(man, ensure_ascii=False, indent=2))

    total = sum(r["count"] for r in rows)
    log("    ✓ 分块完成：%d 块　→ chunks/CHUNK_INDEX.md" % total, quiet)
    return {"rows": rows, "total": total}


# ============================================ 阶段 8：总索引（master）
def source_rows(cfg, st, docs):
    """原始来源对照表：优先用配置，缺省按源文件自动生成"""
    rows = []
    for d in docs:
        spec = d.get("spec") or {}
        if is_external(d):
            # 外部产物没有源文件，来源对照表取自配置登记在台账里的字段
            rows.append((d.get("docno") or "00", d.get("relpath") or d.get("file") or "",
                         d.get("source_format") or "外部产物",
                         d.get("convert_note") or "由外部脚本生成，本工具只登记与索引"))
        elif spec.get("source_note") or spec.get("convert_note") or spec.get("source_format"):
            rows.append((d.get("docno") or "00", d["relpath"],
                         spec.get("source_format") or detect_kind(os.path.join(cfg["_root"], d["relpath"])),
                         spec.get("convert_note") or ""))
        else:
            kind = detect_kind(os.path.join(cfg["_root"], d["relpath"]))
            rows.append((d.get("docno") or "00", d["relpath"], kind, ""))
    return rows


def extract_quick_table(cfg, outdir, qt):
    """从指定文档的某一节里抽表格行，生成专项速查表（可选附详情行号列）"""
    doc = str(qt.get("doc") or "")
    sec = qt.get("section") or ""
    man_path = os.path.join(outdir, "manifest.json")
    if not os.path.exists(man_path):
        return None
    man = json.load(open(man_path, encoding="utf-8"))
    tgt = None
    for d in man.get("documents", []):
        if str(d.get("no")) == doc:
            tgt = d
            break
    if not tgt:
        return None
    md = os.path.join(outdir, tgt["file"])
    if not os.path.exists(md):
        return None
    lines = read_text(md).split("\n")
    start, end = None, len(lines)
    for i, l in enumerate(lines):
        if start is None:
            if re.match(r'^#{2,4}\s+' + re.escape(sec), l):
                start = i
        elif re.match(r'^#{1,4}\s+', l):
            end = i
            break
    if start is None:
        return None
    prefix = qt.get("row_prefix") or "|"
    rows = [l for l in lines[start:end] if l.startswith(prefix)]
    if not rows:
        return None
    detail = qt.get("link_detail") or {}
    dn = {}
    if detail.get("pattern"):
        rx = re.compile(detail["pattern"])
        for i, l in enumerate(lines):
            m = rx.match(l)
            if m:
                dn[m.group(1)] = i + 1
    hdr = list(qt.get("columns") or [])
    out = ["### %s" % (qt.get("title") or sec), ""]
    if qt.get("note"):
        out += ["> %s" % qt["note"], ""]
    if detail.get("label"):
        out.append("| " + " | ".join(hdr + [detail["label"]]) + " |")
        out.append("|" + " --- |" * (len(hdr) + 1))
        for l in rows:
            c = [x.strip() for x in l.strip().strip('|').split('|')]
            key = c[0].strip('`') if c else ""
            out.append("| " + " | ".join(c[:len(hdr)] + ["%d" % dn.get(key, 0)]) + " |")
    else:
        out.append("| " + " | ".join(hdr) + " |")
        out.append("|" + " --- |" * len(hdr))
        for l in rows:
            c = [x.strip() for x in l.strip().strip('|').split('|')]
            out.append("| " + " | ".join(c[:len(hdr)]) + " |")
    out.append("")
    return "\n".join(out)


def cmd_master(cfg, st, dirty, cfg_changed=False, force=False, quiet=False):
    """生成 <output_dir>/INDEX.md —— AI 检索总入口"""
    root = cfg["_root"]
    outdir = os.path.join(root, cfg["output_dir"])
    docs = doc_records(cfg, st)
    man_path = os.path.join(outdir, "manifest.json")
    man = json.load(open(man_path, encoding="utf-8")) if os.path.exists(man_path) else build_manifest(cfg, st, docs)
    mdocs = man.get("documents", [])
    byno = {str(d["no"]): d for d in mdocs}
    terms = cfg["terms"] or []
    chunk_total = sum(len(d.get("chunks") or []) for d in mdocs)

    L = []
    A = L.append
    A("# %s AI 检索总索引" % (cfg.get("project") or "项目文档"))
    A("")
    A("> 生成时间：%s　|　文档数：%d　|　索引根目录：`%s/`"
      % (man.get("generated", ""), len(mdocs), cfg["output_dir"]))
    A("")
    A("本目录把项目内各类原始文档（PDF / Excel / Word / Markdown / 源码）统一转换为")
    A("**AI 可直接阅读的 Markdown**，并建立本索引表。")
    A("**AI 应先读本文件定位目标文档与章节，再按行号精读，无需遍历全部原文。**")
    A("")
    A("---")
    A("")
    A("## 一、给 AI 的检索协议")
    A("")
    A("1. **第一步**：读本文件，用「§二 文档总览」判断问题属于哪一类文档。")
    A("2. **第二步**：查「§三 关键词倒排索引」把术语映射到「文档号 + 章节标题」。")
    A("3. **第三步**：打开该文档的章节索引 `%s/index/<NN>_*.md`，用标题定位行号。" % cfg["output_dir"])
    A("4. **第四步**：只读取 `%s/md/<NN>_*.md` 中对应行区间（可精确 offset 读取）。" % cfg["output_dir"])
    if chunk_total:
        A("   - **超长文档优先读分块**：见 `%s/chunks/CHUNK_INDEX.md`，单块约 %s 行。"
          % (cfg["output_dir"], cfg["chunk"].get("target_lines", 350)))
    A("5. **结构化文档优先用程序查询**：表格类文档已重建为「速查表 + 分节」，")
    A("   或直接加载 `%s/manifest.json` 做二次检索。" % cfg["output_dir"])
    A("6. **机器可读清单**：`manifest.json` 含每篇的 `file / title / category / lines / "
      "headings / keywords / top_sections / chunks`。")
    A("")
    A("```text")
    A("%s/" % cfg["output_dir"])
    A("├── INDEX.md              ← 总索引（本文件，AI 入口）")
    A("├── manifest.json         ← 机器可读清单（元数据 + 关键词 + 顶层章节 + 分块）")
    A("├── md/                   ← 转换后的 Markdown 正文（AI 阅读主体）")
    A("│   └── _raw/             ← 归一化中间产物（不含注入标题，仅供溯源）")
    A("├── index/                ← 每篇的章节索引（含全部标题 + 行号）")
    A("│   └── inverted_index.json ← 机器可读倒排索引")
    A("└── chunks/               ← 超长文档分块%s"
      % (("（%d 块）" % chunk_total) if chunk_total else ""))
    A("```")
    A("")
    A("---")
    A("")
    A("## 二、文档总览")
    A("")
    A("| # | 文档 | 类别 | 行数 | 标题数 | 章节索引 | 关键内容 |")
    A("| --- | --- | --- | --- | --- | --- | --- |")
    for d in mdocs:
        smy = (d.get("summary") or "")
        A("| %s | [%s](<%s>) | %s | %s | %d | [章节索引](<%s>) | %s |"
          % (d["no"], d["title"], d["file"], d.get("category") or "未分类",
             format(d.get("lines", 0), ","), d.get("headings", 0), d["index"],
             smy[:110] + ("…" if len(smy) > 110 else "")))
    A("")
    A("### 各类文档与原始来源对照")
    A("")
    A("| # | 原始文件 | 格式 | 转换方式 |")
    A("| --- | --- | --- | --- |")
    for no, rp, fmt, note in source_rows(cfg, st, docs):
        A("| %s | `%s` | %s | %s |" % (no, rp, fmt, note))
    A("")
    A("---")
    A("")
    A("## 三、关键词倒排索引")
    A("")
    A("> 用法：找到术语所在行 → 得到「文档号 + 所属章节标题」→ 去 `%s/index/<文档号>_*.md` 查行号。"
      % cfg["output_dir"])
    A("")
    A("| 术语 / 主题 | 命中文档与章节（按相关度排序，括号内为命中次数） |")
    A("| --- | --- |")
    cnt_term = 0
    for t in terms:
        nm = term_name(t)
        parts = []
        for d in mdocs:
            th = (st.get(byno.get(str(d["no"]), {}).get("relpath", "")) or {}).get("term_hits") or {}
            hts = th.get(nm) or []
            if not hts:
                continue
            seg = "；".join("%s(%d)" % (s.strip(), n) for s, n in hts[:5])
            parts.append("**%s** %s" % (d["no"], seg))
        if parts:
            A("| %s | %s |" % (nm, " ／ ".join(parts)))
            cnt_term += 1
    if not cnt_term:
        A("| _（未配置 terms；可在配置中加 `terms` 以生成倒排索引）_ | |")
    A("")
    A("---")
    A("")
    A("## 四、各文档章节速查")
    A("")
    for d in mdocs:
        A("### %s　%s" % (d["no"], d["title"]))
        A("")
        A("文件：`%s`　类别：%s　行数：%s　标题数：%d　章节索引：`%s`"
          % (d["file"], d.get("category") or "未分类", format(d.get("lines", 0), ","),
             d.get("headings", 0), d["index"]))
        A("")
        if d.get("keywords"):
            A("**关键词**：%s" % "、".join(d["keywords"]))
            A("")
        ts = d.get("top_sections") or []
        if ts:
            A("| 章节 | 行号 |")
            A("| --- | --- |")
            for h in ts:
                A("| %s | %d |" % (h["text"].replace("|", "/"), h["line"]))
        else:
            A("_（该文档无二级标题，详见章节索引）_")
        A("")
    A("---")
    A("")
    A("## 五、专项速查表")
    A("")
    qts = cfg.get("quick_tables") or []
    if not qts:
        A("_（未配置 `quick_tables`）_")
        A("")
    for qt in qts:
        blk = extract_quick_table(cfg, outdir, qt)
        if blk:
            A(blk)
        else:
            A("### %s" % (qt.get("title") or qt.get("section") or "专项"))
            A("")
            A("_（未找到对应章节或表格行，请检查 `doc` / `section` / `row_prefix` 配置）_")
            A("")
    A("---")
    A("")
    A("## 六、转换质量说明")
    A("")
    A("| 项 | 处理方式 |")
    A("| --- | --- |")
    for k, v in (cfg.get("quality_notes") or DEFAULT_QUALITY_NOTES):
        A("| %s | %s |" % (k, v))
    A("")
    A("### 已知限制")
    A("")
    for x in (cfg.get("known_limits") or DEFAULT_LIMITS):
        A("- %s" % x)
    A("")
    A("---")
    A("")
    if chunk_total:
        A("## 七、长文档分块索引")
        A("")
        A("> 超长文档按**章节边界**切块（目标 %s 行/块），定位到章节后可直接读单块。"
          "完整清单见 `%s/chunks/CHUNK_INDEX.md`。"
          % (cfg["chunk"].get("target_lines", 350), cfg["output_dir"]))
        A("")
        A("| # | 文档 | 分块 | 块行区间 |")
        A("| --- | --- | --- | --- |")
        for d in mdocs:
            ck = d.get("chunks") or []
            if not ck:
                A("| %s | %s | 不分块 | （%d 行，整篇读取成本更低） |" % (d["no"], d["title"], d.get("lines", 0)))
                continue
            rng = "、".join("%d-%d" % (c["start"], c["end"]) for c in ck)
            A("| %s | %s | %d 块 | %s |" % (d["no"], d["title"], len(ck), rng))
        A("")
        A("---")
        A("")
    A("## %s、清单文件" % ("八" if chunk_total else "七"))
    A("")
    A("| 文件 | 说明 |")
    A("| --- | --- |")
    A("| `%s/INDEX.md` | 本文件，AI 检索总入口 |" % cfg["output_dir"])
    A("| `%s/manifest.json` | 机器可读的文档元数据、关键词与分块清单 |" % cfg["output_dir"])
    A("| `%s/index/<NN>_*.md` | 每篇文档的完整标题索引（含行号） |" % cfg["output_dir"])
    A("| `%s/index/inverted_index.json` | 机器可读倒排索引（术语 → 文档 → 章节 → 命中次数） |" % cfg["output_dir"])
    A("| `%s/md/<NN>_*.md` | 转换后的 Markdown 正文 |" % cfg["output_dir"])
    A("| `%s/md/_raw/<NN>_*.raw.md` | 归一化中间产物（溯源用） |" % cfg["output_dir"])
    if chunk_total:
        A("| `%s/chunks/CHUNK_INDEX.md` | 长文档分块索引 |" % cfg["output_dir"])
        A("| `%s/chunks/<NN>/<NN>-KK.md` | 分块正文（每块含溯源头） |" % cfg["output_dir"])
    if (cfg.get("agentdoc") or {}).get("enabled", True):
        A("| `%s` | AI 入口说明（如何使用本索引库） |" % (cfg["agentdoc"].get("filename") or "CLAUDE.md"))
    A("")
    out = "\n".join(L).rstrip() + "\n"
    write_text(os.path.join(outdir, "INDEX.md"), out)
    log("    ✓ INDEX.md  %d 行 / %s 字符　倒排术语 %d 项"
        % (out.count("\n"), format(len(out), ','), cnt_term), quiet)
    return {"index_md": out, "term_count": cnt_term}


DEFAULT_QUALITY_NOTES = [
    ("字符归一", "Unicode NFKC + 康熙部首/兼容字形映射 + PUA 符号映射，解决 PDF 提取导致的检索失配"),
    ("页眉页脚", "移除「第 X 页 共 Y 页」、纯页码行等，并清理孤立表格分隔行"),
    ("断行合并", "保守式合并 PDF 硬换行（行尾为句读/括号符号时不合并）"),
    ("表格", "保留为 Markdown 表格；单元格内换行转 `<br>`，竖线转 `/` 避免破坏表结构"),
    ("标题注入", "对无标题层级的 PDF/Word，按目录或编号规则反推注入 `##`~`#####`，使段落可按标题寻址"),
    ("图片", "内嵌 base64 图片替换为占位说明（对文本检索无价值且体积巨大）"),
    ("溯源", "原始归一化产物保留在 `md/_raw/`，可直接与转换结果对比"),
]

DEFAULT_LIMITS = [
    "文档中的嵌入图片/图表（界面截图、时序图、算法图）无法转为文本，仅保留占位说明；需要图像信息时请回原始文件。",
    "PDF 表格若跨页断裂，可能被拆为两个 Markdown 表；已尽量保留表头但无法完全还原。",
    "多层表头的合并单元格已纵向拼接，个别跨列合并单元格的语义需结合上下文判断。",
]


# ============================================ 阶段 9：入口说明（agentdoc）
def default_routing(cfg, mdocs):
    """未配置 routing 时，按文档自动生成「任务 → 去处」行"""
    rows = []
    for d in mdocs:
        rows.append(("查询《%s》相关内容" % d["title"], "`%s`，先用 `%s` 定位章节" % (d["file"], d["index"])))
    return rows


def cmd_agentdoc(cfg, st, dirty, cfg_changed=False, force=False, quiet=False):
    """生成项目根下的 AI 入口说明（默认 CLAUDE.md）"""
    ag = cfg.get("agentdoc") or {}
    if not ag.get("enabled", True):
        log("  （agentdoc 已关闭）", quiet)
        return {"skipped": True}
    root = cfg["_root"]
    outdir = cfg["output_dir"]
    fn = ag.get("filename") or "CLAUDE.md"
    man_path = os.path.join(root, outdir, "manifest.json")
    man = json.load(open(man_path, encoding="utf-8")) if os.path.exists(man_path) else {"documents": []}
    mdocs = man.get("documents", [])
    chunk_total = sum(len(d.get("chunks") or []) for d in mdocs)
    maxlines = max([d.get("lines", 0) for d in mdocs] or [0])
    ignore = ag.get("ignore_paths") or [cfg["work_dir"]]

    L = []
    A = L.append
    A("# %s — %s" % (fn.split(".")[0], cfg.get("project") or "项目文档库"))
    A("")
    A("本文件是 AI 在本目录工作时的**唯一入口说明**。目标：**用最少的读取量，精确命中所需信息**。")
    A("")
    if ag.get("role"):
        A(ag["role"])
        A("")
    A("---")
    A("")
    A("## 一、最重要的一条规则")
    A("")
    A("**不要遍历本目录。不要直接读对外的原始文档。**")
    A("")
    A("所有原始文档已转换为 AI 可读的 Markdown，并建有索引与分块。正确做法是三步：")
    A("")
    A("1. 读 `%s/INDEX.md` → 用「§二 文档总览」定类别、「§三 关键词倒排索引」把术语映射到文档号 + 章节" % outdir)
    A("2. 打开 `%s/index/<NN>_*.md` → 拿到该章节的**精确行号**" % outdir)
    A("3. 只读 `%s/md/<NN>_*.md` 的对应行区间（或用 `%s/chunks/` 里的现成分块）"
      % (outdir, outdir))
    A("")
    A("**一次典型查询的读取量应控制在 400 行以内**，而不是打开动辄 %s 行的整篇文档。"
      % format(maxlines, ","))
    A("")
    A("---")
    A("")
    A("## 二、目录地图")
    A("")
    A("```text")
    A("%s/" % outdir)
    A("├── INDEX.md                 ← 总索引：检索协议 / 文档总览 / 术语倒排 / 章节速查 / 专项速查表")
    A("├── manifest.json            ← 机器可读：%d 篇元数据 + 关键词 + 顶层章节 + 分块清单" % len(mdocs))
    A("├── md/                      ← 转换后的 Markdown 正文（AI 阅读主体）")
    A("│   └── _raw/                ← 归一化中间产物，仅供溯源核对，日常不要读")
    A("├── index/                   ← 每篇的完整标题清单（含行号）")
    A("│   └── inverted_index.json  ← 机器可读倒排：术语 → 文档 → 章节 → 命中次数")
    A("└── chunks/                  ← 超长文档分块（%d 块）" % chunk_total)
    A("    ├── CHUNK_INDEX.md       ← 分块索引（块号 ↔ 行区间）")
    A("    └── <NN>/<NN>-KK.md      ← 分块正文")
    A("```")
    A("")
    A("> `%s` 为工具缓存/中间产物，**忽略即可**。" % "`、`".join(ignore))
    A("")
    A("---")
    A("")
    A("## 三、文档清单（%d 篇）" % len(mdocs))
    A("")
    A("| # | 主题 | 正文 | 行数 | 标题 | 分块 | 何时用 |")
    A("| --- | --- | --- | --- | --- | --- | --- |")
    for d in mdocs:
        ck = d.get("chunks") or []
        use = d.get("when_to_use") or d.get("summary") or ""
        A("| %s | %s | `%s` | %s | %d | %s | %s |"
          % (d["no"], d["title"], d["file"], format(d.get("lines", 0), ","),
             d.get("headings", 0), ("%d 块" % len(ck)) if ck else "整篇", use))
    A("")
    A("**来源对照**（原始文件 → 转换方式）见 `%s/INDEX.md` §二 下半部分。" % outdir)
    A("")
    A("---")
    A("")
    A("## 四、常见任务 → 去哪查")
    A("")
    A("| 任务 | 直接去处 |")
    A("| --- | --- |")
    for task, where in (ag.get("routing") or default_routing(cfg, mdocs)):
        A("| %s | %s |" % (task, where))
    if chunk_total:
        A("| 只想读超长文档的某一段 | `%s/chunks/CHUNK_INDEX.md` 找到块号 → 读 `%s/chunks/<NN>/<NN>-KK.md` |"
          % (outdir, outdir))
    A("| 想批量检索/统计 | 直接加载 `%s/manifest.json`，或用脚本对 `md/` 做正则提取 |" % outdir)
    A("")
    A("---")
    A("")
    A("## 五、高效检索技巧")
    A("")
    A("**术语不熟就先查倒排索引。** `INDEX.md` §三 每个主题词都列出「文档号 + 章节名 + 命中次数」，命中越高越相关。"
      "机器可读版本：`%s/index/inverted_index.json`。" % outdir)
    A("")
    A("**长文档优先读分块。** 已按章节边界切块（约 %s 行/块）：" % cfg["chunk"].get("target_lines", 350))
    A("")
    A("```text")
    A("先查 %s/chunks/CHUNK_INDEX.md 找到包含目标章节的块" % outdir)
    A("→ 读 %s/chunks/01/01-07.md 这类单块文件" % outdir)
    A("```")
    A("")
    A("**结构化数据用脚本查比读 Markdown 省。** 表格类文档本质是二维数据，"
      "直接正则/程序提取字段，比通读全文高效得多。")
    A("")
    A("**精确行号读取。** `index/<NN>_*.md` 里每个标题都带行号，可只读目标区间。")
    A("")
    A("---")
    A("")
    A("## 六、格式约定与坑")
    A("")
    A("- **行号语义**：索引与分块中的行号，均指 `%s/md/<NN>_*.md` 正文的行号（从 1 开始）。" % outdir)
    if chunk_total:
        A("- **分块文件的头部偏移**：`chunks/<NN>/<NN>-KK.md` 前 9 行是溯源头部（注释、标题、覆盖章节），"
          "**正文从第 9 行起**。换算：块内行号 = md 行号 − 块起始行 + 9。")
    A("- **`_raw/` 不要读**：那是归一化中间产物（未注入标题），仅在对转换结果存疑时用于比对。")
    A("- **单元格内换行**用 `<br>` 表示；竖线已转 `/` 以免破坏表格结构。")
    A("- **图片为占位**：原文档的界面截图、时序图等无法转文本，正文里是 `> _[原文此处为图片：…]_`。"
      "**需要看图只能回原始文档。**")
    A("- **表格跨页**可能被拆成两个表；多层表头的合并单元格已纵向拼接，个别语义需结合上下文。")
    A("- **字符归一**：源文档存在全角/兼容字形，已做 NFKC 归一化。若搜不到，改用英文关键词试试。")
    A("- **代码块内的 `#`** 是 shell 注释，不是标题，标题扫描已跳过。")
    for extra in (ag.get("pitfalls") or []):
        A("- %s" % extra)
    A("")
    A("---")
    A("")
    A("## 七、维护")
    A("")
    A("原始文档更新后，重新生成 `md/` → `index/` → `manifest.json` → `chunks/` → `INDEX.md`：")
    A("")
    A("```text")
    A("python <skill>/scripts/d2m.py update    # 只重算变更文档，聚合文件自动级联更新")
    A("```")
    A("")
    A("- 文档编号一经分配即**永久冻结**，删除的编号进入退役列表不复用，"
      "因此 `<NN>` 与文档的对应关系长期稳定，跨文档引用不会漂移。")
    A("- 改名会被识别（内容哈希命中），继承原编号与产物，不会重新编号。")
    A("- 若在配置里改了术语表/分块参数，聚合阶段会重跑，正文不受影响。")
    A("")
    A("**改动配置或新增文档后，务必同步**：`%s/INDEX.md`、`%s/manifest.json` 由工具自动重建，"
      "无需手工维护；只需确认配置 `sources` 覆盖到新文档。" % (outdir, outdir))
    A("")
    out = "\n".join(L).rstrip() + "\n"
    write_text(os.path.join(root, fn), out)
    log("    ✓ %s  %d 行" % (fn, out.count("\n")), quiet)
    return {"agentdoc": fn, "lines": out.count("\n")}


# ============================================ 阶段 10：校验（validate）
# 两种 Markdown 链接写法：
#   1. 尖括号包裹：[文本](<md/01_xxx(2715).md>)  —— 文件名含 () 或空格时必须这样写，
#      否则解析器无法判断括号归属。此形式下 URL 一直延伸到最后一个 '>'。
#   2. 裸写法：[文本](md/02_xxx.md) —— 遇到第一个 ')' 结束。
# 早期版本只用裸写法正则，导致含括号的文件名被截断成 `..._CDU(2715`，
# 误报大量「链接失效」。这里必须优先匹配尖括号形式。
LINK_RE = re.compile(r'\]\(\s*<([^>]*)>\s*\)|\]\(\s*([^)\s]+)\s*\)')


def validate_links(cfg, fname):
    """校验 INDEX.md 与入口说明中的相对链接是否都能落到真实文件"""
    root = cfg["_root"]
    out = []
    total, bad = 0, []
    for rel in [fname, os.path.join(cfg["output_dir"], "INDEX.md"),
                os.path.join(cfg["output_dir"], "chunks", "CHUNK_INDEX.md")]:
        p = os.path.join(root, rel)
        if not os.path.exists(p):
            continue
        base = os.path.dirname(p)
        for i, l in enumerate(read_text(p).split("\n"), 1):
            for m in LINK_RE.finditer(l):
                u = (m.group(1) or m.group(2) or "").strip()
                if not u or u.startswith(("http://", "https://", "#", "mailto:", "computer:")):
                    continue
                total += 1
                tgt = os.path.normpath(os.path.join(base, u.split("#")[0]))
                if not os.path.exists(tgt):
                    bad.append((rel, i, u))
    return {"total": total, "bad": bad}


def validate_structure(cfg, st):
    """正文结构完整性：H1 唯一、标题行尾无脏字符、表格分隔行列数匹配"""
    root = cfg["_root"]
    outdir = os.path.join(root, cfg["output_dir"])
    probs = []
    for rp, rec in st.data["docs"].items():
        f = rec.get("file")
        if not f:
            continue
        p = os.path.join(outdir, f)
        if not os.path.exists(p):
            probs.append((f, "正文缺失"))
            continue
        lines = read_text(p).split("\n")
        heads = scan_headings(lines)
        h1 = [h for h in heads if h["depth"] == 1]
        if len(h1) != 1:
            probs.append((f, "H1 数量异常：%d" % len(h1)))
        if not lines or not lines[0].startswith("# "):
            probs.append((f, "首行不是 H1"))
        for i, l in enumerate(lines):
            # 行尾 2 个空格是 Markdown 硬换行（溯源头里刻意使用），不算脏字符
            if l.rstrip() != l and not l.endswith("  "):
                probs.append((f, "第 %d 行行尾有空白" % (i + 1)))
                break
        # 表格分隔行与表头列数一致性抽查
        for i in range(1, len(lines)):
            if SEP_ROW.match(lines[i].strip()) and lines[i - 1].strip().startswith("|"):
                a = lines[i - 1].count("|")
                b = lines[i].count("|")
                if a != b:
                    probs.append((f, "第 %d 行表格列数不匹配（表头 %d / 分隔 %d）" % (i, a, b)))
                    break
    return probs


def validate_terms(cfg, st, sample=3):
    """端到端抽样：倒排索引里的术语能否在 index/ 中定位到正确行号"""
    root = cfg["_root"]
    outdir = os.path.join(root, cfg["output_dir"])
    res = []
    terms = cfg["terms"] or []
    for t in terms[:sample]:
        nm = term_name(t)
        found = None
        for rp, rec in st.data["docs"].items():
            th = (rec.get("term_hits") or {}).get(nm)
            if not th:
                continue
            sec = th[0][0]
            ip = os.path.join(root, cfg["output_dir"], index_relpath(cfg, rec))
            ok = False
            if os.path.exists(ip):
                for l in read_text(ip).split("\n"):
                    if l.startswith("|") and sec.replace("|", "/") in l:
                        ok = True
                        break
            found = (rec.get("docno"), sec, ok)
            break
        res.append((nm, found))
    return res


def validate_speed_table(cfg, st):
    """对声明了 detail_pattern 的速查表，校验「详情行号」确实指向该条目所在行"""
    root = cfg["_root"]
    outdir = os.path.join(root, cfg["output_dir"])
    res = []
    for qt in (cfg.get("quick_tables") or []):
        det = qt.get("link_detail") or {}
        if not det.get("pattern"):
            continue
        doc = str(qt.get("doc") or "")
        md = None
        for rp, rec in st.data["docs"].items():
            if str(rec.get("docno")) == doc:
                md = os.path.join(outdir, rec.get("file") or "")
                break
        if not md or not os.path.exists(md):
            res.append((qt.get("title") or doc, 0, 0, ["正文缺失"]))
            continue
        lines = read_text(md).split("\n")
        rx = re.compile(det["pattern"])
        idx = {}
        for i, l in enumerate(lines, 1):
            m = rx.match(l)
            if m:
                idx[m.group(1)] = i
        im = os.path.join(outdir, "INDEX.md")
        bad, checked = [], 0
        if os.path.exists(im):
            il = read_text(im).split("\n")
            for l in il:
                if not l.startswith("|") or l.startswith("| ---") or l.startswith("| MessageID"):
                    continue
                c = [x.strip() for x in l.strip().strip('|').split('|')]
                if len(c) < len(qt.get("columns") or []) + 1:
                    continue
                key = c[0].strip('`')
                try:
                    ln = int(c[-1])
                except Exception:
                    continue
                checked += 1
                if idx.get(key) != ln:
                    bad.append("%s 期望 %s 实际 %s" % (key, ln, idx.get(key)))
        res.append((qt.get("title") or doc, len(idx), checked, bad))
    return res


def cmd_validate(cfg, st, dirty=None, quiet=False, report=None):
    """四类校验：链接 / 正文结构 / 术语可定位 / 速查表行号"""
    fname = (cfg.get("agentdoc") or {}).get("filename") or "CLAUDE.md"
    v = {}
    v["links"] = validate_links(cfg, fname)
    v["structure"] = validate_structure(cfg, st)
    v["terms"] = validate_terms(cfg, st)
    v["quick"] = validate_speed_table(cfg, st)

    if report is not None:
        report["validate"] = v
    if not quiet:
        L = v["links"]
        log("    · 链接校验：%d 条，失效 %d 条" % (L["total"], len(L["bad"])), quiet)
        for rel, ln, u in L["bad"][:10]:
            log("        ✗ %s:%d → %s" % (rel, ln, u), quiet)
        log("    · 正文结构：%d 处问题" % len(v["structure"]), quiet)
        for f, m in v["structure"][:10]:
            log("        ✗ %s：%s" % (f, m), quiet)
        ok = sum(1 for _, f in v["terms"] if f and f[2])
        log("    · 术语抽样：%d/%d 可定位" % (ok, len([x for x in v["terms"] if x[1]])), quiet)
        for nm, f in v["terms"]:
            if f:
                log("        %s %s → %s 章节「%s」%s" % ("✓" if f[2] else "✗", nm, f[0], f[1], "" if f[2] else "（索引中未找到）"), quiet)
        for t, n, ck, bad in v["quick"]:
            log("    · 速查表「%s」：条目 %d，核对 %d，错误 %d" % (t, n, ck, len(bad)), quiet)
            for b in bad[:5]:
                log("        ✗ %s" % b, quiet)
    return v


# ============================================ 变更报告（UPDATE_REPORT.md）
def write_update_report(cfg, st, changes, report, quiet=False):
    """把本次运行的变更与结果写成 <output_dir>/UPDATE_REPORT.md"""
    root = cfg["_root"]
    outdir = os.path.join(root, cfg["output_dir"])
    ch = changes or {}
    L = []
    A = L.append
    CN = "一二三四五六七八九十"
    _sec = [0]

    def sec(title):
        """按实际出现顺序自动编号，避免条件小节导致「三」缺号"""
        i = _sec[0]
        _sec[0] += 1
        A("## %s、%s" % (CN[i] if i < len(CN) else str(i + 1), title))
        A("")
        return

    A("# 更新报告")
    A("")
    A("> 生成时间：%s　|　命令：`%s`　|　流水线版本：%s"
      % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         report.get("command", "update"), PIPELINE_VERSION))
    A("")
    sec("源文件变更")
    A("| 类型 | 数量 | 明细 |")
    A("| --- | --- | --- |")
    def detail(key, fmt):
        items = ch.get(key) or []
        return "、".join(fmt(x) for x in items[:20]) + ("…" if len(items) > 20 else "") or "—"
    A("| 新增 | %d | %s |" % (len(ch.get("added") or []),
                              detail("added", lambda x: "`%s`" % x["relpath"])))
    A("| 修改 | %d | %s |" % (len(ch.get("modified") or []),
                              detail("modified", lambda x: "`%s`" % x["relpath"])))
    A("| 重命名 | %d | %s |" % (len(ch.get("renamed") or []),
                                detail("renamed", lambda x: "`%s` → `%s`" % (x["from"], x["to"]))))
    A("| 删除 | %d | %s |" % (len(ch.get("removed") or []),
                              detail("removed", lambda x: "`%s`" % x)))
    A("| 未变 | %d | — |" % len(ch.get("unchanged") or []))
    A("")
    if ch.get("purged"):
        A("> 另有 %d 个非源文件（本工具自身产物）被剔除，不占编号：%s"
          % (len(ch["purged"]), "、".join("`%s`" % x for x in ch["purged"])))
        A("")
    sec("重跑范围")
    A("| 阶段 | 重跑文档数 | 说明 |")
    A("| --- | --- | --- |")
    for k, note in [("scan", "始终执行（哈希比对）"), ("convert", "仅变更文档"),
                    ("normalize", "仅变更文档"), ("outline", "仅变更文档"),
                    ("structure", "仅变更文档"), ("index", "变更文档 + 配置变化时全量"),
                    ("chunk", "变更文档 + 配置变化时全量"), ("master", "始终执行（聚合）"),
                    ("agentdoc", "始终执行（聚合）"), ("validate", "始终执行")]:
        A("| %s | %s | %s |" % (k, report.get("counts", {}).get(k, "—"), note))
    A("")
    if report.get("docs"):
        sec("成文结果")
        A("| # | 标题 | 正文 | 行数 | 标题数 | 策略 | 注入质量 |")
        A("| --- | --- | --- | --- | --- | --- | --- |")
        for d in report["docs"]:
            q = d.get("quality") or {}
            qs = ("%.2f" % q["score"]) if q.get("score") is not None else "—"
            A("| %s | %s | `%s` | %s | %d | %s | %s |"
              % (d.get("no"), d.get("title"), d.get("file"),
                 format(d.get("lines", 0), ","), d.get("headings", 0),
                 d.get("strategy", ""), qs))
        A("")
        notes = []
        for d in report["docs"]:
            for n in ((d.get("quality") or {}).get("notes") or []):
                notes.append("`%s`：%s" % (d.get("no"), n))
        if notes:
            A("### 需人工复核的文档")
            A("")
            for n in notes:
                A("- %s" % n)
            A("")
    v = report.get("validate") or {}
    if v:
        sec("校验结果")
        Lk = v.get("links") or {}
        A("- 链接：%d 条，失效 %d 条" % (Lk.get("total", 0), len(Lk.get("bad") or [])))
        A("- 正文结构问题：%d 处" % len(v.get("structure") or []))
        ok = sum(1 for _, f in (v.get("terms") or []) if f and f[2])
        tot = len([x for x in (v.get("terms") or []) if x[1]])
        A("- 术语抽样可定位：%d/%d" % (ok, tot))
        for t, n, ck, bad in (v.get("quick") or []):
            A("- 速查表「%s」：核对 %d，错误 %d" % (t, ck, len(bad)))
        A("")
    sec("编号台账")
    A("| # | 源文件 | 标题 | 状态 |")
    A("| --- | --- | --- | --- |")
    for rp, rec in sorted(st.data["docs"].items(), key=lambda x: str(x[1].get("docno"))):
        A("| %s | `%s` | %s | 在用 |" % (rec.get("docno"), rp, rec.get("title") or ""))
    for rec in st.data.get("retired") or []:
        A("| %s | `%s` | %s | 已退役（编号不复用） |"
          % (rec.get("docno"), rec.get("retired_from") or "—", rec.get("title") or ""))
    A("")
    out = "\n".join(L).rstrip() + "\n"
    write_text(os.path.join(outdir, "UPDATE_REPORT.md"), out)
    log("    ✓ UPDATE_REPORT.md  %d 行" % out.count("\n"), quiet)
    return out


# ============================================ adapt：接管已有产物
def cmd_adopt(cfg, st, quiet=False):
    """接管已存在的 ai_docs：为每个源文件分配编号并与现有 md/index 对齐。

    用于「先用别的脚本跑出了产物，之后才引入本工具」的场景：
    不重新转换，只把现有产物登记进状态文件作为基线。要点：

      · 编号优先取 spec.docno；否则按现有 md 的编号顺序就近分配，最后才新开编号。
      · 正文文件名 `md/<NN>_*.md` 与索引 `index/<NN>_*.md` **沿用磁盘原样**
        （分别记入 rec["file"] / rec["index_file"]），不按标题重算改名，
        这样外部已有的引用不会失效。
      · 磁盘上有、但没有任何源文件对应的编号（如另用脚本生成的代码地图），
        按「配置声明保留 + 自动登记」处理，既不占编号也不会被当孤儿清掉。
    """
    root = cfg["_root"]
    outdir = os.path.join(root, cfg["output_dir"])
    files = expand_sources(cfg)
    if not files:
        die("没有匹配到任何源文件，请检查 sources 配置。")
    md_dir = os.path.join(outdir, "md")
    idx_dir = os.path.join(outdir, "index")

    def scan_dir(dirp, pref):
        """列出目录里 <NN>_* 的产物 → {编号: 文件名}"""
        d = {}
        if os.path.isdir(dirp):
            for f in sorted(os.listdir(dirp)):
                m = re.match(r'^(\d+)[_\-]', f)
                if m and f.lower().endswith(".md"):
                    d.setdefault("%02d" % int(m.group(1)), f)
        return d

    existing = scan_dir(md_dir, "md/")
    existing_idx = scan_dir(idx_dir, "index/")
    reserved = reserved_docnos(cfg) - {"*"}

    adopted, missing, orphan = [], [], []
    for f in files:
        rp = f["relpath"]
        spec = f["spec"]
        no = spec.get("docno")
        if not no:
            if len(adopted) < len(existing):
                no = sorted(existing.keys())[len(adopted)]
            else:
                no = st.next_docno(reserved)
        h = sha256_file(f["abspath"])
        rec = {"docno": "%02d" % int(no) if str(no).isdigit() else no,
               "sha256": h, "size": os.path.getsize(f["abspath"]),
               "mtime": os.path.getmtime(f["abspath"]),
               "title": spec.get("title") or os.path.splitext(os.path.basename(rp))[0],
               "stages": {}, "adopted_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        mf = existing.pop(rec["docno"], None)
        if mf:
            rec["file"] = "md/" + mf
            p = os.path.join(md_dir, mf)
            lines = read_text(p).split("\n")
            rec["lines"] = len(lines)
            rec["chars"] = sum(len(x) for x in lines)
            rec["headings"] = len(scan_headings(lines))
            rec["stages"] = {"convert": "adopted", "normalize": "adopted",
                             "outline": "adopted", "structure": "adopted",
                             "index": "adopted", "chunk": "adopted"}
            # 索引文件同样按磁盘原样沿用；缺失则由 index 阶段补生成
            f_idx = existing_idx.pop(rec["docno"], None)
            if f_idx:
                rec["index_file"] = "index/" + f_idx
            adopted.append((rec["docno"], rp, mf))
        else:
            rec["stages"] = {}
            missing.append((rec["docno"], rp))
        st.put(rp, rec)

    # 配置显式声明的外部产物：只登记台账，不参与转换（正文/索引原样保留）
    externals = []
    for s in (cfg.get("sources") or []):
        if not s.get("external"):
            continue
        rel = s.get("file") or ""
        # 与普通文档一致：file 相对 output_dir（如 "md/10_xxx.md"）
        absp = os.path.join(outdir, rel) if rel else ""
        if not rel or not os.path.exists(absp):
            log("  ! 外部产物缺失，跳过：%s" % (rel or "(未声明 file)"), quiet)
            continue
        lines = read_text(absp).split("\n")
        no = "%02d" % int(s["docno"]) if str(s.get("docno", "")).isdigit() \
            else (s.get("docno") or st.next_docno(reserved))
        rec = {"docno": no, "external": True,
               "file": rel,
               "title": s.get("title") or os.path.splitext(os.path.basename(rel))[0],
               "category": s.get("category") or "未分类",
               "summary": s.get("summary") or "",
               "when_to_use": s.get("when_to_use") or "",
               "source_format": s.get("source_format") or "外部产物",
               "convert_note": s.get("convert_note")
                               or "由外部脚本生成，本工具只登记台账与索引，正文/索引原样保留",
               "keywords": s.get("keywords") or [],
               "lines": len(lines),
               "chars": sum(len(x) for x in lines),
               "headings": len(scan_headings(lines)),
               "stages": {k: "external" for k in
                          ("convert", "normalize", "outline", "structure", "index", "chunk")},
               "adopted_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        key = s.get("key") or ("<external>/" + os.path.basename(rel))
        existing.pop(no, None)                 # 该编号已由本条目认领
        f_idx = existing_idx.pop(no, None)
        if f_idx:
            rec["index_file"] = "index/" + f_idx
        elif s.get("index_file"):
            rec["index_file"] = s["index_file"]
        st.put(key, rec)
        keep_artifact(st, rel, "配置声明为外部产物")
        if f_idx:
            keep_artifact(st, "index/" + f_idx, "配置声明为外部产物")
        externals.append((rec["docno"], rec["title"], rel))

    # 没有源文件对应的编号：登记为「外部产物」，既不清理也不复用其编号
    leftovers = sorted(set(existing.keys())) + sorted(set(existing_idx.keys()))
    for no in sorted(set(leftovers)):
        for sub, nm in (("md", existing.get(no)), ("index", existing_idx.get(no))):
            if nm:
                rel = "%s/%s" % (sub, nm)
                keep_artifact(st, rel, "无对应源文件，外部脚本产出")
                orphan.append(rel)

    if not quiet:
        for no, rp, mf in adopted:
            log("  ✓ 接管 %s  %-46s → %s" % (no, rp[:46], mf), quiet)
        for no, rp in missing:
            log("  · 登记 %s  %-46s（无现有产物，下次 update 会转换）" % (no, rp[:46]), quiet)
        for no, ti, rel in externals:
            log("  ✓ 外部 %s  %-46s → %s（原样保留）" % (no, ti[:46], rel), quiet)
        for rel in orphan:
            log("  · 外部产物 %s（保留，不清理）" % rel, quiet)
    return {"adopted": adopted, "missing": missing,
            "external": orphan, "externals": externals}


# ============================================ 流水线编排
STAGES = ["scan", "convert", "normalize", "outline", "structure",
          "index", "chunk", "master", "agentdoc", "validate"]

EXPENSIVE = {"convert", "normalize", "outline", "structure"}


def run_pipeline(cfg, st, command="update", force=False, quiet=False,
                 only=None, changes=None):
    """按顺序执行各阶段，返回带计数的 report"""
    report = {"command": command, "counts": {}, "docs": [], "changes": changes}
    dirty = set()
    if changes is None:
        changes = cmd_scan(cfg, st, force=force, quiet=quiet, report=report)
    report["changes"] = changes
    dirty = apply_changes_to_state(cfg, st, changes, quiet)

    prev_hash = st.data.get("config_hash") or ""
    cur_hash = config_hash(cfg)
    cfg_changed = bool(prev_hash) and prev_hash != cur_hash
    if prev_hash and cfg_changed and not quiet:
        log("  · 配置已变化（%s → %s），聚合阶段将全量重算%s"
            % (prev_hash, cur_hash, "；正文阶段不受影响" if only is None else ""))

    def run(name, fn, **kw):
        """执行一个阶段。dirty 为需重跑的文档集合；聚合阶段自行决定重算范围。"""
        if only and name not in only:
            return
        t0 = datetime.datetime.now()
        log("  [%s]" % name, quiet)
        r = fn(cfg, st, dirty, **kw)
        if isinstance(r, dict) and r.get("docs"):
            report["docs"].extend(r["docs"])
        if isinstance(r, dict) and r.get("manifest") is not None:
            report["manifest"] = r["manifest"]
        if isinstance(r, dict) and r.get("rows") is not None:
            report["chunks"] = r
        if isinstance(r, dict) and r.get("agentdoc"):
            report["agentdoc"] = r
        # 计数：正文阶段看 dirty 大小；聚合阶段优先取阶段自报的实际处理量
        if name in EXPENSIVE:
            report["counts"][name] = len(dirty)
        elif isinstance(r, dict) and isinstance(r.get("count"), int):
            report["counts"][name] = r["count"]
        elif isinstance(r, dict) and isinstance(r.get("total"), int):
            report["counts"][name] = r["total"]
        else:
            report["counts"][name] = "全量"
        log("  [%s] 完成（%.1fs）" % (name, (datetime.datetime.now() - t0).total_seconds()), quiet)
        return r

    # 阶段调度
    if only is None or "convert" in only:
        run("convert", cmd_convert, force=force, quiet=quiet)
    if only is None or "normalize" in only:
        run("normalize", cmd_normalize, quiet=quiet)
    if only is None or "outline" in only:
        run("outline", cmd_outline, quiet=quiet)
    if only is None or "structure" in only:
        run("structure", cmd_structure, quiet=quiet)
    if only is None or "index" in only:
        run("index", cmd_index, cfg_changed=cfg_changed, force=force, quiet=quiet)
    if only is None or "chunk" in only:
        run("chunk", cmd_chunk, cfg_changed=cfg_changed, force=force, quiet=quiet)
    if only is None or "master" in only:
        run("master", cmd_master, cfg_changed=cfg_changed, force=force, quiet=quiet)
    if only is None or "agentdoc" in only:
        run("agentdoc", cmd_agentdoc, cfg_changed=cfg_changed, force=force, quiet=quiet)
    if only is None or "validate" in only:
        run("validate", cmd_validate, quiet=quiet, report=report)

    st.data["config_hash"] = cur_hash
    st.data["pipeline_version"] = PIPELINE_VERSION
    st.data["last_run"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.save()
    return report


# ============================================ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="d2m", add_help=True,
        description="d2m — 文档 → AI 可读 Markdown 转换与索引流水线（支持增量更新）")
    ap.add_argument("command", nargs="?",
                    default="update",
                    help="init / update / all / adopt / 或单个阶段名：%s" % " ".join(STAGES))
    ap.add_argument("-C", "--root", default=None, help="项目根目录（默认按配置向上查找，否则当前目录）")
    ap.add_argument("-c", "--config", default=None, help="配置文件（默认 <root>/d2m.config.json）")
    ap.add_argument("--force", action="store_true", help="忽略哈希，强制重跑")
    ap.add_argument("--quiet", action="store_true", help="精简输出")
    ap.add_argument("--no-report", action="store_true", help="不写 UPDATE_REPORT.md")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root) if args.root else None
    if root is None:
        root = find_root(os.getcwd()) or os.getcwd()
    cfg = load_config(root, args.config)
    st = State(state_file(cfg))
    quiet = args.quiet

    log("d2m %s　根目录：%s" % (PIPELINE_VERSION, cfg["_root"]), quiet)
    log("配置：%s" % cfg["_path"], quiet)

    cmd = args.command
    report = None
    t0 = datetime.datetime.now()
    if cmd == "adopt":
        r = cmd_adopt(cfg, st, quiet)
        st.data["config_hash"] = config_hash(cfg)
        st.save()
        log("接管完成：%d 篇已登记（%d 篇待转换）%s"
            % (len(r["adopted"]), len(r["missing"]),
               "，另有 %d 个外部产物保留" % len(r["external"]) if r["external"] else ""), quiet)
        log("建议接着执行：python d2m.py index && python d2m.py chunk && python d2m.py master && python d2m.py agentdoc", quiet)
    elif cmd == "init":
        report = run_pipeline(cfg, st, "init", force=True, quiet=quiet)
    elif cmd == "all":
        report = run_pipeline(cfg, st, "all", force=True, quiet=quiet)
    elif cmd == "update":
        report = run_pipeline(cfg, st, "update", force=args.force, quiet=quiet)
    elif cmd in STAGES:
        if cmd == "scan":
            ch = cmd_scan(cfg, st, force=args.force, quiet=quiet, report={})
            apply_changes_to_state(cfg, st, ch, quiet)
            st.save()
            report = {"command": "scan", "changes": ch, "counts": {"scan": len(ch["added"])}}
            log("扫描完成（未执行后续阶段）", quiet)
        else:
            report = run_pipeline(cfg, st, "stage:" + cmd, force=args.force,
                                  quiet=quiet, only={cmd})
    else:
        die("未知命令：%s\n可用：init / update / all / adopt / %s" % (cmd, " / ".join(STAGES)))

    if cmd != "adopt" and not args.no_report:
        changes = report.get("changes") if isinstance(report, dict) else None
        write_update_report(cfg, st, changes, report or {}, quiet)

    log("总耗时 %.1fs" % (datetime.datetime.now() - t0).total_seconds(), quiet)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()



