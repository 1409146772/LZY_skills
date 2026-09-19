# 初始化工作流细则(P0-P5)

前置事实(已核实,code.claude.com/docs/en/memory):根 CLAUDE.md 与工作目录以上每一级的 CLAUDE.md 每次会话自动加载;**子目录 CLAUDE.md 只在 Claude 读到该子树文件时按需注入**;多层文件拼接生效而非覆盖;`@path` import 递归上限 4 跳。这就是"根=地图+指针、细节下沉到子目录"成立的技术前提。

脚本统一用 `python <本skill目录>/scripts/mdmap.py <子命令>` 调用(下称 `mdmap`)。

## P0 判向 + 建工作区(主会话)

```
mdmap detect --repo <工程根> --ensure-workspace
```

- 返回 `MODE = INIT` → 走本流程;`ADOPT` → 同样走本流程,但 P2 的 brief 生成用 `--mode adopt`(worker 做合并式重排,禁覆盖手写内容);`UPDATE/NOOP` → 不属于初始化,见 update-workflow.md。
- 退出码 4 = 不是代码工程(home、<5 个源码文件)→ 与用户确认目录,不要自作主张换目录。
- 工作区为 `<repo>/.claude-md-map/`:`state.json`(默认随仓提交,团队共享基线;`--state-local` 可关)+ `tmp/`(自带 .gitignore,git 忽略)。

## P1 扫描 + 划分(主会话)

```
mdmap scan  --repo <工程根>            # → tmp/scan.json,stdout ≤30 行摘要
mdmap plan  --scan tmp/scan.json       # → tmp/modules.json,打印划分表
```

- 主会话只读这两个 stdout,不读 scan.json 全文(R3)。
- 划分算法(scan→plan 内部规则,供你向用户解释):
  1. 含包标记(package.json/pyproject.toml/go.mod/Cargo.toml/pom.xml…)的顶层目录 → 独立模块
  2. 源码文件 <8 且无包标记的目录 → 按主导扩展名贪心分组(每组 ≤4 目录 / ≤30 文件)
  3. >400 文件或 >15k LOC → 按二级子目录(≥3 文件,`--split-child-files` 可调)再切,最多 8 个孩子,余量留在父级 `-core`(嵌套前缀由最长前缀匹配消解)
  4. 总数收敛 4-12;超出自动提高门槛重聚,仍超出 → plan 退出码 4(low_confidence)
  5. 90 天 git churn Top5 的模块在根地图排前,优先挖 Gotchas
- **plan 退出码 4 或划分明显不合理时**:用 AskUserQuestion 让用户确认/调整(可给 `--min-files`/`--max-modules` 重跑);划分合理则打印表格直接继续,不要额外打断。
- 向用户复述划分表(一行一模块:id / 前缀 / 文件数 / churn),这是用户了解"谁负责写哪块"的唯一窗口。

## P2 模块 worker(并行派发)

1. 生成 brief(init 模式;ADOPT 见下):

   ```
   mdmap brief --modules tmp/modules.json --scan tmp/scan.json --batch <id1>,<id2>,... --mode init
   ```

   每个 brief 落在 `tmp/draft/<id>/brief.md`,脚本打印路径。<8 源文件的小模块在 `--batch` 里合并进同一个 brief。
2. **每个(或每批)模块派一个 general-purpose subagent**(需要 Write 权限;显式指定 model,机械型模块用 sonnet,复杂模块用更强模型)。dispatch prompt ≤25 行、不含任何源码内容,格式:

   ```
   你负责为模块 <id> 生成 CLAUDE.md 草稿。
   1. 先读 <brief 路径> —— 它是你的完整需求:文件清单、骨架模板、行数预算、输出路径。
   2. 只读 brief 清单内的文件;完整产出写入 brief 指定的草稿路径。
   3. 完成后按 brief 里的回执契约回复(≤15 行)。
   禁止派生 subagent;禁止修改 brief 之外任何文件。
   ```
3. ADOPT 模式:`brief --mode adopt`,dispatch 里补一句"现有文档内容是 merge 基准,只重排补齐,不删手写内容"。
4. 回执处理:`DONE` → 记 ledger;`DONE_WITH_CONCERNS` → 读报告文件里 concern 段再定;`NEEDS_CONTEXT` → 补上下文重派;`BLOCKED` → 换更强模型重派一次,再失败标记该模块「待补」(`<!-- TODO: 待补 -->` 占位草稿),不阻塞其余模块,并在最终报告告知用户。
5. **不要并行派超过 8 个 worker**;分批跑。每完成一批把 `Task P2: <id> done/待补` 追加进 `tmp/ledger.md`。

## P3 根文件合成(单个 worker)

所有模块草稿就绪后,派 1 个 general-purpose subagent(最强模型),dispatch 要点:

```
任务:为工程根生成 CLAUDE.md 草稿(地图+指针,≤60 行,target 45)。
1. 读工作区 tmp/draft/ 下每个模块的 CLAUDE.md 草稿(只有这些,不要读任何源码)。
2. 读 tmp/scan.json 的 stdout 摘要字段(repo/vcs/root_config)与仓库根的配置文件白名单
   (package.json/Makefile/pyproject.toml 等实际存在的根配置)。
3. 写 <workspace>/tmp/draft/_root/CLAUDE.md,骨架:
   # <工程名>(中文一句话)
   ## Commands(全局命令表:build/test/lint/dev,必须真实)
   ## Architecture(代码库地图:每个模块前缀一行,一行描述;churn Top5 排前)
   ## Gotchas(跨模块的坑:构建顺序/环境要求/公共约定)
4. 地图行必须与 modules.json 的模块前缀一一对应;不写只有单个模块才知道的细节(那属于子目录文档)。
```

单模块工程(single_root_only):跳过 P3,P5 直接把模块草稿装到仓库根。

## P4 质量门

1. **确定性 lint(主会话跑,零上下文)**:

   ```
   mdmap lint --repo <工程根> --draft-dir tmp/draft --modules tmp/modules.json
   ```

   检查:行数预算、反引号死路径、空章节、根地图行↔模块前缀对应、跨文件重复行。FAIL → 把 findings 写进文件 `tmp/lint-findings.md`,SendMessage 给对应 worker 续修(只发文件路径)。
2. **语义评审(Explore 只读 subagent)**:dispatch 让它读全部草稿文件,按 scoring-digest.md 六维打分,输出 `SCORE: <n>/<grade>` + findings(Critical/Important/Minor,file:line)。硬门槛=任一 Critical(会失败的命令、不存在的路径、指向不存在目录的地图行)→ 原 worker 续修一轮;Minor 记入 `tmp/ledger.md`,修后只重跑 lint(改动 >20 行才二次语义评审)。

## P5 报告 + 确认 + 落盘(主会话 + 用户)

本轮结果(判向、模块数、lint/评审结论、install 清单或挂起原因)同时作为 claude-md 章节写入统一报告文件(路径规则见 SKILL.md「统一报告文件」)。

```
mdmap lint  ... > 记 stdout
mdmap report --repo <工程根> --draft-dir tmp/draft --modules tmp/modules.json --lint tmp/lint.json
```

- 向用户展示 report 输出(根文件全文 + 模块大纲/行数/预览),等确认。**用户不确认就不落盘。**
- 确认后:

  ```
  mdmap install --repo <工程根> --draft-dir tmp/draft --modules tmp/modules.json --mode init
  ```

  install 自动:既有同名文件备份到 `tmp/backup-<ts>/`、写 state.json(reviewed_commit=HEAD、各文件 content_hash)、打印 manifest。
- 收尾向用户报告:落盘文件清单、备份位置、下次更新方式("代码改动后重新运行本 skill 即可增量更新")。
