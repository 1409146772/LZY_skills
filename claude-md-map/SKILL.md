---
name: claude-md-map
description: "Layered CLAUDE.md management and codebase navigation map for a whole repository, plus file-filter config refresh, Markdown doc audit & auto-fix, and Claude memory maintenance. Workflow: auto-dispatch by default (explicit argument → natural-language keyword → repo state via mdmap detect) into one of 5 modes, running unattended with no confirmation prompts and auto-installing; only when the user explicitly asks to choose manually does it open the interactive menu. claude-md: detect mode (INIT/ADOPT/UPDATE/NOOP) → scan directory stats → cluster into 4-20 prefix modules (超大目录按二级子目录自动拆分成独立子模块) → dispatch parallel subagents to write per-module CLAUDE.md drafts into a git-ignored workspace → synthesize root map file → lint + semantic review gate → show report (auto-install when unattended) → install with backup and state.json baseline. filter: refresh/calibrate the current project's .claudeignore + .claude/settings.json per the 文件过滤 methodology doc. doc-audit: full re-audit of doc\\**\\*.md (incl. README) against the Markdown doc spec, fix issues in place, write review report. memory: consolidate the current project's auto-memory. all: run the four modes in sequence. Every maintenance run ends with an email notification of the per-mode results (收件人/抄送取 config.conf 的 notify_email/notify_cc,已授权直发). Use when the user asks to 初始化 CLAUDE.md / 分层管理 CLAUDE.md / 生成代码库地图 / 项目文档初始化 / 更新 CLAUDE.md / 代码更新后同步文档 / 刷新文件过滤 / 更新 .claudeignore / 文件过滤配置 / 审核文档 / 文档整改 / doc 规范审核 / 更新记忆 / 整理记忆 / 全部维护 / codebase map / layered CLAUDE.md / module docs / CLAUDE.md maintenance for a repo. DO NOT use for: single-file Q&A, editing one CLAUDE.md section by hand (edit directly), or non-repo directories."
tools: Read, Glob, Grep, Bash, Edit, Write, AskUserQuestion
argument-hint: "[claude-md|filter|doc-audit|memory|all] [manual]"
---

# claude-md-map — 工程维护四合一(CLAUDE.md 分层文档 / 文件过滤 / 文档审核 / 记忆)

## 交互入口

本 skill **默认无人值守执行(AUTO)**,不必等用户选择模式。只有用户显式要求手动选择时才进入人工菜单(MANUAL)。
D0 判定的结果写进 `tmp/ledger.md` 首行,**本轮全程不再改变**。

### D0 判定交互模式

命中下列任一措辞 → **MANUAL**;否则 **AUTO**:

- 中文:「手动选择」「手动选」「手动模式」「让我自己选」「我自己选」「弹菜单」「出菜单」「给我选项」「先问我」「先问我要跑哪个」「不要自动执行」「别自动跑」
- 英文:`manual` / `manual mode` / `let me choose` / `I'll pick` / `show me the menu` / `show menu` / `give me options` / `ask me first` / `don't auto-run` / `interactive`
- 参数:裸 token `manual` 或 `手动`(不加 `--manual` 旗标,保持 argument-hint 诚实、无需解析器)

**显式模式参数不是 MANUAL**:`$0` = `claude-md|filter|doc-audit|memory|all` 是确定性指令,仍走 AUTO 门控(即自动落盘)。

### 模式声明行(每次进入必输出,一行,不阻塞)

```
[claude-md-map] 模式=<AUTO|MANUAL> | 目标=<工程根> | 判定=<INIT|ADOPT|UPDATE|NOOP|显式指定> | 执行=<模式名|待选> | 依据=<一句话>
```

```
[claude-md-map] 模式=AUTO | 目标=D:/proj/CDC_7255 | 判定=UPDATE | 执行=claude-md | 依据=无参数无关键词,detect 报 3 个模块受影响
[claude-md-map] 模式=MANUAL | 目标=D:/proj/CDC_7255 | 判定=显式指定 | 执行=待选 | 依据=用户说「让我自己选」
[claude-md-map] 模式=AUTO | 目标=D:/proj/CDC_7255 | 判定=NOOP | 执行=停止 | 依据=自上次审计以来无影响文档的变更
```

门控被跳过时补一行:`[claude-md-map] 门控=AUTO 跳过落盘确认,自动 install 5 个文件(既有文件备份至 tmp/backup-<ts>/)`

### 工程根怎么定

① 用户给的显式路径 → ② 否则 cwd 在 git 工作树内 → `git rev-parse --show-toplevel`(只读 Bash)→ ③ 否则 cwd。
传给 `detect` 后,**以 stdout 的 `repo=<abs>` 为后续每步 `--repo` 的准绳**。
(必要性:`mdmap.py` 的 `repo_root()` 只 resolve 显式值/cwd,**不向父目录找仓库根**,在子目录里跑会静默把子目录当工程根。)

**守卫(skill 自身/其宿主仓不可作为目标)**:解析结果落在 `<本skill目录>` **或其任一祖先目录**时 → 停止并提示「<路径> 是 skill 自身所在仓库,请指定目标工程」,**不得自动 fallback 到别的目录或换模式**。
(本副本住在 `D:/LZY_project/AI/skills/claude-md-map`,而 `git rev-parse --show-toplevel` 会解析到**宿主的 skills 仓**,所以只挡住"等于 skill 目录"是不够的——从 skill 目录裸跑会把整个 skills 集合当成目标工程测绘。
判定用真实路径比较,注意 Windows 大小写与 `\` / `/` 差异;MANUAL 下同样先过守卫。)

### D1 模式解析顺序(AUTO,首个命中即停)

1. `$0` 命中模式名(`claude-md|filter|doc-audit|memory|all`)→ 该模式
2. **自然语言直入(免菜单)**:用户说下列明确措辞 → 直接进入对应模式:
   - 「初始化/同步/更新 CLAUDE.md」「代码库地图」→ claude-md(走下文「流程总控」,先 `mdmap detect` 判向)
   - 「刷新文件过滤」「更新 .claudeignore」「文件过滤配置」→ filter → 读 `references/file-filter-workflow.md`
   - 「审核文档」「文档整改」「doc 规范审核」→ doc-audit → 读 `references/doc-audit-workflow.md`
   - 「更新记忆」「整理记忆」→ memory → 读 `references/memory-update-workflow.md`
   - 「全部维护」「四个都跑」「一键维护」→ all
3. 都未命中 → 跑 `mdmap detect --repo <工程根> --ensure-workspace`,按判向落表:

| detect 结果 | AUTO 动作 |
|---|---|
| `INIT` | claude-md(init-workflow P0-P5) |
| `ADOPT` | claude-md(brief 用 `--mode adopt`) |
| `UPDATE` | claude-md(update-workflow U0-U4) |
| `NOOP` | **报告并停止**,零改动 |
| 退出码 4 | **停止并报告**(真实阻塞,见失败处理表) |

未识别的非空 `$0` 不是错误:落到第 3 步,并在声明行「依据」里回显那个被忽略的 token。

**为什么 NOOP 是「报告并停止」而非升级为 `all`**:

1. **detect 只懂一个维度**。NOOP 完全由 CLAUDE.md 基线算出(变更模块/新目录/已删/根脏),它是**关于 claude-md 的否定证据**,对 filter / doc-audit / memory 零信息量。据此升级 = 从一个不提及它们的信号里推断三个模式。
2. **会退化成「永远 all」**。仓库一旦有了 `state.json` 且工作区干净,几乎每次调用都返回 NOOP;`NOOP→all` 对成熟仓库**就是**「永远 all」。
3. **NOOP 下不写任何东西**(`detect --ensure-workspace` 在 NOOP 时不建工作区)。报告并停止是唯一能让裸调用保持无副作用的解。

NOOP 输出 = 声明行 + 一行非阻塞提示「如需其他模式,回复 filter / doc-audit / memory / 全部维护」。无 AskUserQuestion、无 worker、无 install。
仍按下文「统一报告文件」写 NOOP 章节并触发邮件(既有「每轮必发」规则不变)。

### MANUAL:人工菜单

用 AskUserQuestion 让用户选择:

| 选项 | 模式 | 说明 |
|------|------|------|
| CLAUDE.md 更新 | claude-md | 分层 CLAUDE.md 初始化/增量/复核(现有实现) |
| 文件过滤刷新 | filter | 当前工程 .claudeignore + .claude/settings.json 刷新/校准 |
| 文档审核整改 | doc-audit | 当前工程 doc\ 下 .md 全量规范审核 + 直接整改 + 报告 |
| 记忆更新 | memory | 当前工程 auto-memory 整理 |
| 全部执行 | all | 依次执行前四个模式 |

**实现约束(AskUserQuestion 每个问题最多 4 个选项)**:菜单虽有 5 项,弹出时须**拆两问**:
- 第 1 问「范围」:全部执行 / 单个模式
- 选「单个模式」后第 2 问:CLAUDE.md 更新 / 文件过滤刷新 / 文档审核整改 / 记忆更新
- 不得一次传 5 项(工具报 too_big);单独「记忆更新」可在第 2 问直接选。

### all 模式

按菜单序依次执行 claude-md → filter → doc-audit → memory;单模式失败不阻塞后续,结束输出各模式状态汇总。每轮结束触发「运行结果邮件通知」(见下节)。

- claude-md 的落盘确认门**按本轮模式**:MANUAL 保留,AUTO 跳过(auto-install);其余三模式本就无人值守。
- **all 内部的 claude-md 子步骤不适用 D1 的「NOOP → 报告并停止」**;NOOP 仅表示该子步骤零打扰,不阻断其余三模式。
- **AUTO 下 all 是影响面最大的一条路径:声明行必须逐个列出将执行的四个模式后再开始。**

### 门控规则(按本轮模式)

| 门控 | MANUAL | AUTO |
|---|---|---|
| plan 退出码 4(低置信划分) | 展示划分表问用户 | 不弹框,走确定性回退(`references/init-workflow.md` P1) |
| P5 / U4 落盘前确认 | 展示 report 等确认,**不确认不落盘** | 不等确认,展示 + 写报告后直接 `install` |
| UPDATE 的 `deleted` 模块 | 先问用户再删 | 不询问、**绝不 `rm`**,只清地图行与基线条目(R7 备份) |
| detect 退出码 4(非代码工程) | AskUserQuestion 确认目录 | 停止并报告,不询问 |

### 交互强制规则(按模式分列)

**AUTO**:
- **不得**弹 AskUserQuestion 做模式选择或落盘确认;必须按 D1 执行。
- 遇真实阻塞(detect 退出码 4)只报告不询问,不自行换目录、不用 `--force` 硬闯。
- **不得因「没拿到确认」而停下不落盘**;落盘后输出门控声明行(含备份路径)。

**MANUAL**:
- 必须弹出真实 AskUserQuestion 交互,不得根据上下文「猜测」或「默认代选」。
- 不得复用上一轮会话中的选择结果;每次进入都要重新询问。
- 若用户取消交互(cancel/close),立即停止执行,不得继续执行任何子命令、不发邮件。

**两模式共同**:派发任何子命令前先输出模式声明行。

## 统一报告文件(每模式必落)

**每个模式(claude-md/filter/doc-audit/memory,含 all 的各子模式与 NOOP)结束时,把本模式报告写入统一报告文件 `OutPut_YYYYMMDD.html`**(当日日期;目录不存在则创建)。**路径规则(同 jira-commit 约定)**:当前工程在 git 仓库内 → `<项目根>/.claude-md-map/output/OutPut_YYYYMMDD.html`(该目录已入 .gitignore);仓库外/非 git → 回退 `<本skill目录>/output/OutPut_YYYYMMDD.html`。

- **单文件中文 HTML**:自包含、零外部资源(无外链 css/js/字体/图片),浏览器直接可开。
- **固定模板(必用)**:以 `<本skill目录>/templates/report_template.html` 为骨架——复制其 `<style>` 与页面结构,只填章节内容,不得另起一套样式。
  - 保留模板的 `<style>`(勿改配色/字体/类名)与 4 对模式锚点 `<!-- MODE: xxx -->`…`<!-- /MODE -->`;本轮没跑的模式整段删除,NOOP 保留章节一句话结论。
  - 正文只用模板类名:`<section class="card">` 章节、`<table class="tbl">` 表格、`<span class="badge ok|fail|noop|warn">` 状态徽章、`<code>` 行内代码;禁止逐标签内联 `style=`。
  - 模板缺失时回退为等价手写:浅灰底白卡、主色蓝 `#2f54eb`、表头 `#e8edff`,徽章 成功绿/失败红/NOOP 灰/警告橙。
- **章节固定顺序,从上往下**:①CLAUDE.md 更新 ②文件过滤 ③文档审核 ④记忆更新;**本轮没跑的模式不出现**(只跑 memory 就只有 memory 章节)。NOOP 也落章节,一句话结论即可。
- **同日多次运行合并**:文件已存在时,按章节锚点注释(`<!-- MODE: claude-md -->`…`<!-- /MODE -->` 等 4 对)替换本模式章节、保留其他模式章节,并按固定顺序重排;不存在则新建仅含本轮模式章节。
- **各章节内容**:
  - claude-md:判向结果、受影响/更新模块数、lint 与语义评审结论、install 文件清单(或 NOOP/挂起原因);
  - filter:是否变更、决策表摘要、改动文件、token 节省估算、待复核项;
  - doc-audit:汇总表(文件|类型|严重/建议/提示数|整改状态)+ 逐文件整改明细 + 待人工确认清单;
  - memory:变更清单表(操作|条目|原因)+ 校准结论。
- 邮件通知的 `--attach` 固定附此文件(文件存在即附)。

## 配置(config/config.conf)

读 `<本skill目录>/config/config.conf`(模板 `config.conf.example`,随仓提交;本机文件已被 .gitignore 忽略)。
**所有键均可留空,留空即走缺省行为;严禁在任何产物(文档、报告、邮件)中写死姓名。**

| 键 | 用途 | 缺省(留空/未配置) |
|---|---|---|
| `notify_email` | 结果邮件收件人 | 不发信,静默跳过,仅会话内报告 |
| `notify_cc` | 结果邮件抄送(英文逗号分隔,可空) | 无抄送 |
| `author` | doc-audit:设计类文档版本历史表「作者」列 | 单元格写 `-` |
| `reviewer` | doc-audit:设计类文档版本历史表「审查人」列 | 写「待审查」 |
| `maintainer` | doc-audit:其他文档三行版本信息的「维护者」行 | 整行省略,记入报告「待人工确认」清单 |

署名缺省为**静态判定,不弹交互**——保证 AUTO / `all` 无人值守不中断。细则见 `references/doc-audit-workflow.md` 整改执行第 5 条。

## 运行结果邮件通知(每轮必发)

**触发**:任意一轮维护结束(all 或任一单模式,含 NOOP)后发一封结果邮件。用户已明确授权直发(2026-09-11 确认:直接发送、每次运行都发),无需再逐次确认。

**配置**:`notify_email` / `notify_cc` 取 `<本skill目录>/config/config.conf`(见上节「配置」)。

**发送**(`lark-cli` 不在 PATH,必须用绝对路径;路径含空格风险低但仍加引号):

```
LARK_CLI="C:/Users/user/.workbuddy/binaries/node/cli-connector-packages/node_modules/@larksuite/cli/bin/lark-cli.exe"
"$LARK_CLI" mail +send --as user --to <notify_email> [--cc <notify_cc>] \
  --subject '[claude-md-map] <工程名> 维护结果 <YYYY-MM-DD> (成功n/失败m)' \
  --body '<HTML 正文>' \
  --attach <本轮实际生成的报告文件,cwd 内相对路径> \
  --confirm-send
```

**前置检查**:先跑 `"$LARK_CLI" auth status`,确认 `identities.user.status == "ready"`。为 `missing`(refresh token 过期)时**直接跳过发信**并在会话内提示用户跑 `auth login`,不要重试。
发信只需 `mail:user_mailbox.message:send`;可用 `"$LARK_CLI" auth check --scope "<scope1> <scope2>"` 精确核对(**多个 scope 用空格分隔,逗号会被当成单个 scope 字面量**)。
**重新授权注意**:`auth login --scope X` 是**全量覆盖**而非增量合并,只传一个 scope 会把此前已授予的其他 scope 清掉。补权限时须把要保留的 scope 一并列出,例如:
`"$LARK_CLI" auth login --scope "mail:user_mailbox.message:send mail:user_mailbox:readonly mail:user_mailbox.message:modify" --no-wait --json`
(token 过期时同样适用。已授权状态见 `auth status`;`auth scopes` 查的是**应用侧**已启用 scope,与当前 token 实际授予的不是一回事。)

**正文结构**(HTML,≤30 行):
1. 模式状态表:模式(claude-md/filter/doc-audit/memory) | 结果(成功/失败/NOOP) | 一句话结论;
2. 主要改动:各模式落盘产物(install 更新文件数与清单摘要、doc-audit 整改文件数、lint 结论、记忆变更条数);
3. 失败模式附一行原因。
附件:固定附统一报告文件(路径规则见上节);文件确实不存在才省略 `--attach`。

**发送硬规则(2026-09-11 实测踩坑)**:
- `--attach` **只接受 cwd 内相对路径**;绝对路径会被静默忽略(不报错、不附上)。
- 正文**永远用内联 `--body` 字符串**(命令行传参不过文件系统,不受 DLP/编码影响);**禁用 `--body-file` 传项目树内文件**——DLP 加密后 lark-cli 读到密文,正文整个乱码。
- **DLP 工程附件前置检查**:发件前用 `head -c 40 <报告文件>` 验证 bash 视角是明文;读到 `%TSD-Header` 密文头则**不附附件**,改为把本模式报告章节内容内嵌进 `--body` 正文兜底(注意:手动解密后任何重写都会被 DLP 立即重新加密)。
- `mail +send` 可能只存草稿不直发:返回含 `draft_id` 而无 `message_id` 时,补 `mail user_mailbox.drafts send --yes --params '{"user_mailbox_id":"me","draft_id":"<id>"}'`。

**失败处理**:发信失败不阻塞、不重试,会话内提示一次;返回含 `automation_send_disable_reason` 时如实报告「被邮箱设置拦截」并给出草稿链接;主题/正文禁止包含密钥。

## 目标

对当前工程建立/维护**分层 CLAUDE.md**:根文件只放「地图 + 全局命令 + 跨模块坑」(≤60 行),每个模块子目录放自己的局部文档(≤80 行,按需加载)。主会话只做统筹,**读源码、写文档全部分发给 subagent**,产物落盘,上下文零污染。

## 适用 / 不适用

- ✅ 整个工程的文档初始化、代码更新后的增量同步、周期性全量复核
- ❌ 只改某个 CLAUDE.md 的一句话(直接 Edit);非代码目录(home、纯笔记库)
- 会话开头先跑 `detect` 判向,不要凭感觉选流程

## 调用方式

用户说「给这个工程建 CLAUDE.md / 代码库地图」「代码更新了,同步一下文档」等即调用。脚本统一:

```
python <本skill目录>/scripts/mdmap.py <detect|scan|plan|brief|detect-changes|lint|report|install> ...
```

两条流程的完整步骤见 `references/init-workflow.md`(P0-P5)与 `references/update-workflow.md`(U0-U4);brief/回执模板见 `references/brief-and-report.md`;评分见 `references/scoring-digest.md`。本文件只写主控逻辑与硬规则。

## 关键背景知识(规则 | 原因)

| 规则 | 原因 |
|---|---|
| 根 CLAUDE.md 常驻每次会话;子目录 CLAUDE.md **按需加载**(模块粒度默认到二层子目录,一层目录过大会自动拆成子模块) | 官方已核实(code.claude.com/docs/en/memory);根必须瘦,细节必须下沉 |
| 模块 = 一组路径前缀;文件→模块用**最长前缀匹配** | 增量更新映射正确性的根基;划分错则映射全错 |
| 状态基线在 `<repo>/.claude-md-map/state.json`(默认随仓提交) | 团队共享 reviewed_commit;`--state-local` 可各机独立 |
| 草稿先写 `tmp/draft/`,install 才落盘并自动备份 | 用户确认前不碰仓库任何文件;备份防手写内容丢失 |
| 产物走文件路径,不走粘贴 | 粘贴内容常驻主会话上下文并在每轮重读(最贵的失败来源) |
| Windows 下脚本强制 UTF-8 | cp936 会炸中文;脚本已内置 reconfigure |

## 上下文保护硬规则(违反前必须先在 ledger 记 Ruling)

- **R1** 主会话禁 Read/Grep/Glob 业务源码。白名单仅:脚本 stdout、CLAUDE.md、state.json/scan.json 等工作区 JSON 的摘要、subagent 回执
- **R2** 跨 agent 一律走文件:brief 文件 + 报告文件;dispatch prompt ≤25 行、不含源码内容
- **R3** 主会话只消费脚本 stdout;脚本输出 >200 行视为异常,停下来查
- **R4** 行数预算:根 ≤60(target 45)/ 模块 ≤80(target 50)/ worker 回执 ≤15 行
- **R5** worker 禁止再派 subagent(dispatch 里写明)
- **R6** 主会话不写文档内容:草稿由 worker 写,落盘由 `install` 完成
- **R7** install 前不修改仓库内任何 CLAUDE.md;既有文件自动备份到 `tmp/backup-<ts>/`
- **R8** 中断/压缩后恢复靠 `tmp/ledger.md`,不靠记忆;每完成一步追加一行

## 流程总控(细则在 references,此处只列主干)

**判向**(每次会话先跑,不要猜):

```
mdmap detect --repo <工程根> --ensure-workspace     # → INIT | ADOPT | UPDATE | NOOP
```

**INIT**(无根 CLAUDE.md)→ init-workflow.md P0-P5:
P0 detect+工作区 → P1 scan+plan(划分表进会话与报告;plan 退出码 4 的门控见「交互入口」)→ P2 并行模块 worker(general-purpose,显式 model,brief 由脚本生成,≤8 个并发)→ P3 单个根合成 worker(只读模块草稿,不读源码)→ P4 lint + Explore 评审(双质量门)→ P5 report →(MANUAL:用户确认 / AUTO:直接)→ install。

**ADOPT**(有根 CLAUDE.md 无 state.json)→ 同 INIT,brief 用 `--mode adopt`:现有内容是 merge 基准,只重排补齐,禁删手写内容。

**UPDATE**(有 state.json 且有变更)→ update-workflow.md U0-U4:
detect-changes → 只对受影响模块派 update worker(手改冲突=盘上为准就地合并;新目录 ≥15 文件建新模块;deleted 按模式处理:AUTO 只清地图行+基线不删文件 / MANUAL 先问用户)→ 全量 lint(腐烂探测)→ report →(MANUAL:确认 / AUTO:直接)→ install --mode update。

**NOOP** → 告知"文档已是最新",结束。

**质量门**(两道,都不可跳):
1. `mdmap lint`(确定性,零上下文):死路径/超预算/空章节/地图失配/重复行 — FAIL 必修
2. Explore 语义评审(六维评分,Critical 硬门槛)— 见 scoring-digest.md

## 失败处理表(场景 | 处置)

| 场景 | 处置 |
|---|---|
| detect 退出码 4(home/<5 源文件) | 真实阻塞:**AUTO 停止 + 一行说明**;MANUAL 用 AskUserQuestion 确认目录;两者都不自行换目录、不发邮件 |
| plan 退出码 4(低置信) | **AUTO 不弹框**:单模块继续(记 warning)/ 0 模块自动降 --min-files 重跑一次,仍 0 则停止报告;MANUAL 展示划分表问用户,按反馈调 --min-files/--max-modules 重跑 |
| worker BLOCKED 两次 | 标记「待补」`<!-- TODO -->` 占位 + 地图行标注,不阻塞其余模块;最终报告告知 |
| reviewed_commit 失效 / 非 git | 脚本自动降级 content_hash 比对(模块级粒度);照常走流程 |
| state 引用的 CLAUDE.md 被手删 | 该模块 brief 退化为 adopt 模式重建 |
| 用户手改过文档(hash 失配) | 盘上为准、合并式就地编辑、报告标注保留项;绝不整体重生成 |
| install 失败(只读仓) | 打印待写 manifest 请用户手工应用 |
| 单模块工程(single_root_only) | 跳过根合成;模块文档直接装到仓库根 |
| 邮件发送失败(网络/权限/scope) | 不阻塞、不重试;会话内提示一次,拦截时给草稿链接 |
| config.conf 未配 notify_email | 静默跳过发信,仅会话内报告 |

## 常见自我合理化(Excuse | Reality)

| Excuse | Reality |
|---|---|
| "我直接读几个源文件更快" | 一个文件不大,十个就淹了主会话;这正是本 skill 存在的原因(R1) |
| "lint PASS 就不用语义评审了" | lint 抓死路径,抓不住"说错了";两道门抓不同类问题 |
| "worker 报告太长,贴给我看看" | 贴进来就常驻上下文了;让它写文件,你读路径(R2) |
| "只更新改动模块,别的肯定没问题" | 没改动的模块也会腐烂;所以每次 update 全量跑 lint |
| "划分错了下次再改" | 划分错 → 前缀路由错 → 增量更新从根上失效;AUTO 下低置信走确定性回退并记 warning,MANUAL 下必须问;两种模式都不许静默丢弃划分结果 |
| "NOOP 也把 worker 跑一遍保险" | 零变更跑 worker 是纯浪费;信 state.json 基线 |
