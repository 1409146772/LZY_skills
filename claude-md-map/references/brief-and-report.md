# Dispatch Brief 模板 / 回执契约 / Placeholders 表

原则(沿袭 subagent-driven-development):**你粘贴进 dispatch prompt 的一切都会常驻你的上下文并在后续每轮被重读——产物一律走文件**。brief 文件是唯一需求来源;主会话 dispatch ≤25 行、不含源码内容;worker 详细产出写文件,回复只给 ≤15 行回执。

## Worker dispatch 模板(init/adopt)

```
你负责为模块 <MODULE_ID> 生成 CLAUDE.md 草稿。
1. 先读 <BRIEF_PATH> —— 它是你的完整需求:文件清单、骨架模板、行数预算、输出路径。brief 里的要求逐字生效。
2. 只读 brief 清单内的文件;完整产出写入 brief 指定的草稿路径(DRAFT_PATH)。
3. 完成后按 brief 末尾的回执契约回复(≤15 行)。
禁止派生 subagent;禁止修改 brief 清单之外的任何文件。
```

| Placeholder | 来源 |
|---|---|
| `<MODULE_ID>` | plan 划分表 / `mdmap brief --id --batch` 参数 |
| `<BRIEF_PATH>` | `mdmap brief` 打印的路径(`tmp/draft/<id>/brief.md`) |
| `<DRAFT_PATH>` | brief 内已写明(`tmp/draft/<id>/CLAUDE.md`),dispatch 里再提一次是双保险 |

## Worker dispatch 模板(update)

```
你负责增量更新模块 <MODULE_ID> 的 CLAUDE.md。
1. 先读 <BRIEF_PATH> —— 变更文件清单、更新指令、行数预算都在里面。
2. 在你自己的上下文里跑 git diff 看实际改动;逐条核对既有文档的每条 claim。
3. 只修改失真处;未受影响章节一字不动;<MANUAL_EDIT_NOTE>
4. 产出写入 brief 指定的草稿路径;回执 ≤15 行。
禁止派生 subagent;禁止修改 brief 清单之外的任何文件。
```

| Placeholder | 来源 |
|---|---|
| `<MANUAL_EDIT_NOTE>` | changes.json 该模块 `manual_edit=true` 时填:"现有文档含手写内容,以盘上为准做合并式就地编辑,报告标注保留了哪些手写行";否则填"手写内容必须保留" |

## 根合成 worker / 语义评审的 dispatch

见 init-workflow.md P3 与 P4(根合成禁止读源码,只读模块草稿 + scan 摘要 + 根配置白名单;评审 agent 只读草稿文件)。

## 回执契约(≤15 行,worker 最终回复只允许这些)

```
STATUS: DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED
LINES: <草稿行数>/<预算>
GOTCHAS: <3 条最值得记录的 gotcha,一行一条>
UNCOVERED: <brief 清单里没来得及覆盖的目录,无则写 none>
REPORT: <报告文件路径>
[CONCERNS: <仅 DONE_WITH_CONCERNS 时,≤3 行>]
```

处理:_DONE → 记 ledger,进质量门;DONE_WITH_CONCERNS → 读 REPORT 的 concerns 段再定;NEEDS_CONTEXT → 主会话补上下文重派(同 worker);BLOCKED → 换更强模型重派一次,再失败 → `<!-- TODO: 待补 -->` 占位,不阻塞其余模块。

## ledger(`tmp/ledger.md`)最小格式

```
# claude-md-map ledger — repo: <路径>  started: <日期>
P2: <模块id> done (draft <行数>行) | 待补 | BLOCKED
lint: PASS/FAIL <日期>
review: SCORE <n>/<grade> findings <n>条(C <x>/I <y>/M <z>)
P5: install <日期> mode=<init|update> files=<n>
```

中断/压缩后:先读 ledger,再继续第一个未完成条目——不靠记忆。
