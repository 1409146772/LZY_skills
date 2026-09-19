# 六维评分(语义评审用,30 行内联版)

完整版(每维 20/15/10/5/0 档描述 + Red Flags 清单)见:
`C:\Users\user\.claude\skills\claude-md-improver\references\quality-criteria.md`
该加什么/不该加什么的细则见:
`C:\Users\user\.claude\skills\claude-md-improver\references\update-guidelines.md`

## 评审 dispatch 要点

派 **Explore 只读** subagent;让它读全部草稿 CLAUDE.md(只这些文件);按六维逐文件打分并汇总。

## 六维(总分 100)

| 维度 | 满分 | 一句话判据 |
|---|---|---|
| Commands/Workflows | 20 | build/test/lint/dev 命令齐全且带上下文 |
| Architecture clarity | 20 | 目录地图清晰、模块关系/入口点说明 |
| Non-obvious patterns | 15 | gotcha/变通/顺序依赖/“为什么这么写” |
| Conciseness | 15 | 无废话、每行都有价值、不复述代码 |
| Currency | 15 | 与当前代码库一致(命令能跑、文件存在) |
| Actionability | 15 | 命令可直接复制执行、路径真实 |

等级:A 90-100 / B 70-89 / C 50-69 / D 30-49 / F 0-29。

## findings 分级与硬门槛

- **Critical**(任一出现 → 必须续修):会失败的命令、引用不存在的文件/目录、地图行指向不存在目录、模板原样未定制
- **Important**:关键命令缺失、章节内容空泛、明显过时的说法
- **Minor**:措辞冗长、可再精简、非关键 gotcha 缺失 → 记 ledger,不阻塞落盘

输出格式:

```
SCORE: <总分>/<等级>
FILES: <文件数>
FINDINGS:
- [Critical|Important|Minor] <文件> <file:line 或章节> <问题描述>
```

## 交叉文件检查(评审额外职责)

- 根地图行的描述与模块草稿首行说法是否一致(矛盾 → Important)
- 同一命令在根与模块文档里是否冲突(冲突 → Critical,以可执行者为准)
- 手改保留内容(update 场景)是否被 worker 意外删除(删除 → Critical)
