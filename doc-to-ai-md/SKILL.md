---
name: doc-to-ai-md
description: "把一个装满文档的目录整体转换成 AI 可读的 Markdown 索引库，让 AI 用最少的 token 精确命中信息。产出四层导航：总索引 INDEX.md（文档总览 + 关键词倒排索引 + 专项速查表）、每篇的章节索引（带精确行号）、超长文档的分块、以及目录下的 AI 入口说明文件。支持增量更新：源文档改动后重跑，只重转变更的那几篇，文档编号永久冻结不漂移，重命名能被识别并继承原编号与产物。Workflow: 首次 init 全量建库（需先写 d2m.config.json）→ 日常 update 增量更新 → 已有产物时先 adopt 接管状态基线。Use when the user asks to 建立文档索引 / 文档转 Markdown / 把文档转成 AI 能看的格式 / 让 AI 读这些文档 / 这个文件夹里的 PDF 太多读不动 / 给文档目录建索引 / 参考资料索引 / 文档库初始化 / 文档更新了重新索引 / build a doc index / convert docs to markdown for AI / make documents AI-readable / index a documentation folder. 关键词触发：一批 PDF / Word / Excel 需要被 AI 检索、想省 token 读长文档、需要按行号精确定位章节。DO NOT use for: 单个文件的临时转换（那种情况直接用 markitdown skill 的 convert_to_markdown.py）；纯代码仓库的文档整理（那是 claude-md-map）。"
---

# doc-to-ai-md — 文档 → AI 可读 Markdown 索引库

把一个目录里的 **PDF / Word / Excel / PPT / Markdown / 源码** 统一转成 AI 可读的 Markdown，
并生成「**总索引 + 章节索引 + 分块 + 入口说明**」四层导航，让 AI 用最少的读取量命中目标。

核心价值是**省 token**：与其打开一篇 18,015 行的 PDF 转换稿，不如先查倒排索引拿到文档号 + 章节名，
再读章节索引拿到**精确行号**，最后只读那 200 行。单次查询的读取量应控制在 400 行以内。

工具本体是同目录的 `scripts/d2m.py`（单文件、无外部依赖声明、约 3000 行）。

---

## 一、环境硬规则（先读这条，否则一定失败）

### R1　必须用这个解释器

```bash
PY="D:/LZY_project/pi_workspace/PI-tomarkdown/.venv/Scripts/python.exe"
```

- **PATH 上的 `python` 没有装 markitdown**，直接用会 `ModuleNotFoundError`。
- 上面这个 venv 是唯一装齐依赖的环境：markitdown 0.1.8b2 / pandas / openpyxl / **xlrd** / pdfplumber。
- 路径统一用**前向斜杠**（Git Bash 下反斜杠会被当转义符）。
- 如果该 venv 被删了，重建：`pip install "markitdown[all]" pandas openpyxl xlrd`。

### R2　脚本一律用绝对路径 + `-C` 指定根目录

```bash
"$PY" C:/Users/user/.trae-cn/skills/doc-to-ai-md/scripts/d2m.py update -C D:/path/to/docs
```

**不要 `cd` 到 `C:\Users\user\.trae-cn\skills` 再跑 python**。那个目录下有个同名的 `markitdown\`
skill 文件夹，会被 Python 解析成命名空间包，导致 `'markitdown' is a package and cannot be directly
executed` 这类误导性报错，浪费大量排查时间。

### R3　`.exe` shim 不可靠

`d2m.py` 内部统一走 `python -m markitdown`（见 `markitdown_convert()`）。
不要在配置里或手工调用 `markitdown.exe`，它在本机会静默返回 1。

---

## 二、主流程

### 流程 A：首次建库（`init`）

**Step 1 — 勘察目录**。先看清有什么，别急着跑：

```bash
find <root> -type f | sed 's/.*\.//' | sort | uniq -c | sort -rn
```

统计扩展名分布，注意剔除源码、`__pycache__`、中间产物。

**Step 2 — 写 `d2m.config.json`** 到项目根。模板见同目录 `d2m.config.example.json`。
最简只要两项：

```json
{ "project": "XX 项目文档库", "sources": [{ "glob": ["*.pdf", "*.docx"] }] }
```

**Step 3 — 跑 init**：

```bash
"$PY" <skill>/scripts/d2m.py init -C <root>
```

大 PDF（20MB+）会慢，`markitdown_convert()` 的超时上限是硬编码的 1800s。若某篇超时，单独重跑该阶段。

**Step 4 — 抽查产物质量**，不要跑完就宣布成功：

- `ai_docs/md/<NN>_*.md` 是否有实际内容（空文件 = 扫描件，需 OCR）
- `ai_docs/INDEX.md` §三 倒排索引是否真有命中
- 结构化 Excel 是否按预期切节（如 CAN 报文应切成 `### 0x???` 小节）

### 流程 B：日常增量更新（`update`）— 最常用

```bash
"$PY" <skill>/scripts/d2m.py update -C <root>
```

**源文档改动后，这就是唯一的日常入口。** 机制：

- `cmd_scan()` 按 `sha256` 比对，产出 `新增 / 修改 / 重命名 / 删除 / 未变` 变更集
- 只有变更的文档进 `dirty` 集合被重转；未变的正文字节不动
- **文档编号（`docno`）一经分配永久冻结**，删除的编号进 `retired` 不复用 →
  `md/03` 永远是同一篇，跨文档引用和行号不会漂移
- **重命名靠哈希命中识别**，继承原编号与产物，不会重新编号
- 聚合阶段（index / chunk / master / agentdoc）自动级联重建

**已实测的增量行为**（可直接据此向用户说明）：

| 操作 | 结果 |
|---|---|
| 什么都不改重跑 | `未变 5`，零正文重转，仅聚合文件重建 |
| 改 1 篇 | `修改 1 / 未变 N`，只重转该篇，其余哈希不变 |
| 重命名（内容不变） | `重命名 1`，**继承原编号**，`retired` 不增加 |
| 删除 1 篇 + 新增 1 篇 | 删除的编号进 `retired` 不复用；新增的拿到**下一个未用编号**，不会占用退役编号 |

> ⚠️ 改了 `sources` 的 glob 或某篇的 `outline`/`structured` 配置后，**光跑 `update` 不够**：
> 配置变化只会触发聚合阶段重算，正文阶段按哈希判定为「未变」会跳过。
> 这种情况要加 `--force`，或单独重跑对应阶段（如 `structure`）。

### 流程 C：接管已有产物（`adopt`）

目录里已经有 `ai_docs/`（比如上一轮跑过、或有人手工维护过）时，先 `adopt` 反推状态基线，
避免重复转换或编号冲突：

```bash
"$PY" <skill>/scripts/d2m.py adopt -C <root>
"$PY" <skill>/scripts/d2m.py index && "$PY" <skill>/scripts/d2m.py chunk \
  && "$PY" <skill>/scripts/d2m.py master && "$PY" <skill>/scripts/d2m.py agentdoc
```

### 单阶段重跑

`scan convert normalize outline structure index chunk master agentdoc validate`
—— 调试某个阶段时用，正常流程不需要。

---

## 三、命令速查

| 命令 | 用途 | 何时用 |
|---|---|---|
| `init` | 首次全量构建 | 空目录第一次建库 |
| `update` | 增量更新 | **日常默认**，源文档改动后 |
| `all` | 强制全量重建 | 产物损坏、想推倒重来 |
| `adopt` | 接管已有产物 | 目录里已有 `ai_docs/` |
| `scan` | 只扫描不改产物 | 想先看看会变更什么 |
| `validate` | 校验链接 / 结构 / 术语 | 产物存疑时 |

通用选项：`-C <root>` 根目录 ｜ `-c <file>` 指定配置 ｜ `--force` 忽略哈希强制重跑 ｜ `--quiet` ｜ `--no-report`。

---

## 四、配置要点

完整模板见 `d2m.config.example.json`（每个键都带说明）。这里只列**最容易踩坑的**：

### `sources` — 决定收录哪些文档，声明顺序决定初始编号

```json
{ "glob": ["*.pdf", "子目录/*.docx"], "exclude": ["_converted", "__pycache__"] }
```

| 字段 | 说明 |
|---|---|
| `glob` | 相对根的 glob，支持 `**` 递归；可传字符串或数组 |
| `exclude` | 正则数组，匹配相对路径即排除。**反斜杠要写成 `\\`** |
| `extensions` | 扩展名白名单（小写含点） |
| `expand_children` | 把命中的目录展开为其下全部文件 —— **会吞整棵子树，务必配合 `extensions` 收窄** |
| `structured` | 结构化渲染，见下 |
| `preserve_docnos` | 保留编号不接管，如 `["06","07"]` 或 `"*"` |

**务必排除中间产物**：形如 `*_converted.csv` 的派生文件会与原文档重复，污染倒排索引。

### `structured` — Excel 的两种渲染方式

- **`can_matrix`**：CAN 矩阵专用。按报文边界切节 + 生成报文速查表。
  需配 `structured_rules` 的列索引（**列号必须实地勘察，不能照抄**）：
  `column_header_row` / `name_col` / `id_col` / `node_cols` / `speed_cols` / `meta_cols`。
  勘察方法：
  ```bash
  "$PY" -c "import pandas as pd;d=pd.read_excel(r'<xls>',sheet_name='Matrix',header=None);[print(i,repr(v)) for i,v in enumerate(d.iloc[0])]"
  ```
- **`generic_sheets`**：多 sheet 通用渲染，每 sheet 一节，自动纵向合并多层表头。
  表头不在第一行也能处理（`header_max_rows` 默认 6）。诊断表 / 需求表这类用这个。

### `outline` — 长 PDF 的标题注入策略（最容易踩的坑）

PDF 转出来是一大片没有层级的散文。工具靠「编号行」（`1.2 文档范围`）反推标题层级，
但**中文技术文档前面往往有一份点线目录**（`1.2 文档范围..........1`），会和正文抢匹配。

```json
{ "outline": { "strategy": "numbered", "body_start": 1269 } }
```

| 字段 | 说明 |
|---|---|
| `strategy` | `auto`（默认）/ `numbered` / `toc` / `hybrid` / `manual` / `markitdown` / `none` |
| `body_start` | **正文起始行号（1 起）**。跳过版本记录 + 目录，从正文第一个标题开始 |
| `toc_lines` | `[起, 止]`，目录所在行区间。`toc` / `hybrid` / `auto` 用 |
| `min_numbered` | `auto` 下判定「编号行足够多」的阈值，默认 3 |
| `headings` | `strategy: manual` 时显式给出 `[行号, 层级, 标题]` 列表 |

**怎么定 `body_start`**：找正文第一章标题的行号（去掉点线后的那个）。

```bash
grep -n "^1\.文档信息" <root>/.d2m-work/norm/<NN>_norm.md
```

> ⚠️ **行号必须基于 `.d2m-work/norm/<NN>_norm.md`**（归一化后），不是原始 PDF，
> 也不是 `md/_raw/`。三者行号不同，用错会切在正文中间。
>
> **判断是否踩坑**：跑完看 `outline` 阶段输出的「质量」。质量 < 0.5 或标题数远少于
> 目录条目数，就是 `body_start` 没设对。修正后必须 `--force` 或单跑 `outline` 阶段。

### `agentdoc.filename` — 入口文件名

默认 `CLAUDE.md`（Claude Code 会自动加载）。**想生成 `Cloud.md` 就改成 `"Cloud.md"`。**

> ⚠️ 该文件**每次运行都会整体覆盖**。如果你往里面手工补了内容，下次 `update` 会丢掉。
> 需要长期保留的定制内容，写进 `agentdoc.role`（前缀段落）或 `agentdoc.routing`（常见任务表）。

### `terms` — 倒排索引的术语表

```json
{ "terms": [ { "name": "诊断", "keywords": ["DTC", "UDS"] } ] }
```

留空则总索引里没有关键词倒排表 —— 而这是**最省 token 的入口**，建议务必填。

---

## 五、产物结构与你该怎么读

```
<root>/
├── CLAUDE.md              ← AI 入口说明（本工具的产物，告诉 AI 怎么用这个库）
├── .d2m-work/             ← 状态与中间产物，不要读
└── ai_docs/               ← 【AI 主工作区】
    ├── INDEX.md           ← 总索引：检索协议 / 文档总览 / 术语倒排 / 章节速查 / 专项速查表
    ├── manifest.json      ← 机器可读元数据（文档号 / 关键词 / 顶层章节 / 分块清单）
    ├── md/<NN>_*.md       ← 转换后的正文（AI 阅读主体）
    │   └── _raw/          ← 归一化中间产物，仅溯源用，日常不要读
    ├── index/<NN>_*.md    ← 每篇的完整标题清单（带行号）
    │   └── inverted_index.json
    └── chunks/            ← 超长文档的分块
        ├── CHUNK_INDEX.md ← 块号 ↔ 行区间
        └── <NN>/<NN>-KK.md
```

### 三步检索法（省 token 的核心）

1. 读 `ai_docs/INDEX.md` → 「文档总览」定类别、「关键词倒排索引」把术语映射到**文档号 + 章节**
2. 打开 `ai_docs/index/<NN>_*.md` → 拿到该章节的**精确行号**
3. 只读 `ai_docs/md/<NN>_*.md` 的对应行区间（或用现成分块）

**目标：单次查询读取量 ≤ 400 行。**

---

## 六、读取产物的硬规则与坑

| 规则 | 说明 |
|---|---|
| **行号语义** | 索引与分块里的行号，均指 `ai_docs/md/<NN>_*.md` 从 1 开始的正文行号 |
| **分块头部偏移** | `chunks/<NN>/<NN>-KK.md` 前 8 行是溯源头部，**正文从第 9 行起**。换算：块内行号 = md 行号 − 块起始行 + 9 |
| **`_raw/` 不要读** | 归一化中间产物（未注入标题），仅在怀疑转换结果时用于比对 |
| **表格** | 单元格内换行用 `<br>`；竖线已转 `/` 以免破坏表格结构 |
| **图片是占位** | 原 PDF 的截图 / 时序图 / 算法图无法转文本，正文里是 `> _[原文此处为图片：…]_`。**需要看图只能回原始文档** |
| **跨页表格** | PDF 表格跨页可能被拆成两个表 |
| **中文标点** | 源 PDF 存在全角 / 兼容字形（如 `⻋`、`⽂`），已做 NFKC 归一化。搜不到时换用字号或英文关键词（如用 `F103` 而非「配置字」） |
| **代码块内的 `#`** | 是注释不是标题，标题扫描已跳过 |

---

## 七、失败处理

| 症状 | 原因与处置 |
|---|---|
| `ModuleNotFoundError: markitdown` | 用错解释器了，回到 **R1**，用 venv 的 python |
| `'markitdown' is a package and cannot be directly executed` | cwd 在 `skills` 目录下，回到 **R2**，改用绝对路径 + `-C` |
| 找不到配置文件 | 先写 `<root>/d2m.config.json`，可复制 `d2m.config.example.json` |
| 配置解析失败 | JSON 语法错误 —— 注意**不能写 `//` 注释**，说明性文字用 `_` 开头的键 |
| 某篇 md 转出来是空的 | 大概率是**扫描件 PDF**（图片型），需 OCR。**如实告知用户，不要假装成功** |
| 大 PDF 转换超时 | `markitdown_convert()` 硬编码 1800s 上限。单独重跑该阶段，或拆小源文件 |
| `.xls` 读不了 | 需要 **xlrd**（venv 里有）。`.xls` 不能走 openpyxl |
| Excel 切节不符合预期 | `structured_rules` 列号配错了，回到 §四 实地勘察列索引 |
| 倒排索引没命中 | `terms` 没配，或 `index.min_hits` 太高（默认 2） |
| 入口文件被覆盖丢了手工内容 | 见 §四 `agentdoc` 条 —— 长期内容应写进 `role` / `routing` |

---

## 八、汇报纪律

跑完要如实汇报，不要只说"成功了"：

- 报**实际转换了几篇**，以及**哪几篇失败 / 产出为空**
- 空产出、扫描件、超时这类降级情况**必须显式说明**，不能把不完整的产物说成完整
- 引用内容时只贴**相关片段**，不要把整篇 Markdown 倒出来（那正好违背了本 skill 的设计目的）
- 告知用户产物位置与下一步怎么用（通常是：以后改完文档跑 `update` 即可）
