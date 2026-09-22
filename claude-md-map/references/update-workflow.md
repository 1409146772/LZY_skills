# 增量更新工作流细则

前提:`detect` 返回 UPDATE(有 state.json 且有变更)或 ADOPT 后需周期维护。全程只对**受影响模块**派 worker,未受影响模块一字不动。

## state.json schema(`<repo>/.claude-md-map/state.json`,默认随仓提交)

```json
{
  "version": 1,
  "generated_at": "2026-09-02T15:00:00",
  "vcs": "git",
  "reviewed_commit": "<上次审计时的 git HEAD>",
  "modules": [
    {"id": "auth", "path": "src/auth/", "prefixes": ["src/auth/"],
     "claude_md": "src/auth/CLAUDE.md", "content_hash": "git:abc123...",
     "files": 42}
  ],
  "root_claude_md": {"path": "CLAUDE.md", "content_hash": "git:def456..."}
}
```

- `--state-local` 安装时:.gitignore 会加一行 `state.json`,基线各机独立。
- `reviewed_commit` 失效(rebase/gc)或非 git 工程 → 自动降级为逐模块 content_hash 比对:模块级粒度、无文件清单,worker 指令改为"全模块复核"。

## 流程

### U0 判向 + 变更检测(主会话)

```
mdmap detect-changes --repo <工程根>      # → tmp/changes.json,stdout 摘要
```

stdout 四行:受影响模块 / 新目录 / 已删除模块 / 根文件是否需同步。全空 → NOOP,告知用户"文档已是最新"即结束。
同时跑一次全量 lint(见 U3)——这是未触碰模块的腐烂探测器。

### U1 派发受影响模块 worker

```
mdmap brief --modules tmp/modules.json --changes tmp/changes.json --batch <id1>,<id2> --mode update
```

brief 自动附:现有 CLAUDE.md 路径、变更文件清单(≤80 条)、更新指令。dispatch 要点:

```
你负责增量更新模块 <id> 的 CLAUDE.md。
1. 先读 <brief 路径> —— 变更文件清单、更新指令、行数预算都在里面。
2. 在你自己的上下文里跑 git diff 看实际改动;逐条核对既有文档的每条 claim。
3. 只修改失真处;未受影响章节一字不动;手写内容必须保留。
4. 产出写入 brief 指定的草稿路径;回执 ≤15 行。
```

**手改冲突**:changes.json 里该模块 `manual_edit=true`(盘上 hash ≠ state)→ dispatch 补一句"现有文档含手写内容,以盘上为准做合并式就地编辑,报告里标注保留了哪些手写行"。**绝不整体重生成。**

边界处理:
- `missing_md=true`(state 引用的文档被手删)→ 该模块 brief 用 `--mode adopt` 重新生成(退化为单独重建)。
- `new_dirs` ≥15 源文件 → 新模块:走 plan 增量(或手动在 modules.json 加条目)后按 init 流程给该目录派 worker;`small_new_dirs` → 并入父模块(列进父模块 worker 的 brief)。
- `deleted` 模块 →
  - **MANUAL**:先问用户,确认后删除其 CLAUDE.md 与根地图行,并从 state.modules 移除(用 Edit 改 state.json)。
  - **AUTO**:**不询问,而且绝不 `rm`**。只做两件可回滚的记账——清掉 `state.modules` 条目 + 移除根 CLAUDE.md 的对应地图行;两处改动都必须走「草稿 → `install --mode update`」路径,使落盘前的根 CLAUDE.md 被 R7 备份进 `tmp/backup-<ts>/`。若该模块的 CLAUDE.md 在磁盘上仍存在(目录没删干净)→ AUTO 下**保留文件**并在报告「待人工确认」列出。报告记一条「已自动清理 N 个已删除模块的地图行/基线条目(原文件已备份)」。
- `root_dirty` → 单独派一个小 worker 只做根文件同步(地图行/Gotchas/Commands 受影响处,目标改动 ≤10 行;单模块 single_root_only 时跳过)。

### U2 质量门

与 init 相同:lint(全量)→ FAIL 续修;语义评审只对**本次改动的文件**做,Minor 记 `tmp/ledger.md`。

### U3 全量 lint(每次更新必跑,含未更新模块)

```
mdmap lint --repo <工程根> --modules tmp/modules.json     # 不带 --draft-dir → lint 已装文件
```

抓:死路径(文件/目录被搬走或改名)、超预算、空章节、地图行失配、跨文件重复行。这是文档腐烂的廉价探测器——lint 只能抓死路径,抓不住语义失真,所以建议用户每 3-6 个月或大版本后跑一次 `--full` 全量复核(即对全部模块重跑 init 流程的 P2-P4,ADOPT 模式合并)。

### U4 报告 + 落盘

本轮结果(受影响模块数、worker 结论、lint 结论、install 清单或挂起原因)同时作为 claude-md 章节写入统一报告文件(路径规则见 SKILL.md「统一报告文件」;NOOP 也落一句话结论)。

**MANUAL:向用户展示 report 输出,等确认,用户不确认就不落盘。AUTO:不等确认**——展示 + 写统一报告后直接执行下面的 `install`,并输出门控声明行「AUTO 跳过落盘确认,自动 install <n> 个文件(既有文件备份至 tmp/backup-<ts>/)」。
**R7 在 AUTO 下同样生效**:备份由 `install` 自身完成,落盘永远可回滚。

```
mdmap report --repo <工程根> --draft-dir tmp/draft --modules tmp/modules.json --lint tmp/lint.json
mdmap install --repo <工程根> --draft-dir tmp/draft --modules tmp/modules.json --mode update
```

install 只拷贝草稿目录里存在的文件;未更新模块不动。收尾 state.json:reviewed_commit=HEAD、被更新文件刷新 hash、未更新模块保留旧 hash 条目。

## 变更映射原理(为什么只动受影响模块)

模块 = 一组互不重叠的路径前缀;文件→模块用**最长前缀匹配**(normcase 大小写不敏感)。changes.json 把 git diff 的每个文件路由到所属模块,只在有变更文件的模块上花 worker。嵌套前缀(超大目录拆分场景)由"最长优先"消解,永远路由到最深匹配。
