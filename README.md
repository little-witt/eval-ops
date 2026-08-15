# Skill Doctor

> Agent Capability EvalOps: 面向 Agent Skill 的持续评测、故障归因与受控优化系统。

项目阶段：技术方案完成，黑客松 MVP 待实现。

## 一句话介绍

Skill Doctor 将目前依赖人工复制日志、反复对话和手工重测的 Skill 调试过程，转化为一个可复现、可归因、有预算限制、经过隐藏回归验证的自动评测与候选修复工作流。

## 项目背景

在日常 Skill 开发中，初版通常由 Agent 根据用户 Prompt 生成。用户随后建立测试 Case、运行 Skill、发现失败，再把会话日志交给另一个 Agent 分析和修改。

这个流程存在三个突出问题：

1. 用户在多个 Agent 和环境之间充当日志与上下文的搬运者；
2. 修复过程没有自然沉淀为可重复执行的回归资产；
3. 失败经常被直接归因给 Skill，但真实原因也可能是模型波动、工具异常、权限、环境或评测标准本身。

随着 Agent Skills 逐渐标准化、数量增加并跨 Runtime 使用，Skill 的版本回归、兼容性和质量门禁会从个人调试问题演变为团队工程问题。

## 解决方案

用户提供一个 Skill、少量种子 Case 和成功标准，Skill Doctor 自动完成：

```text
Skill + Case + 成功标准
          |
          v
建立 without/with 或 old/new 基线
          |
          v
隔离执行并采集 Agent 输出、工具调用、错误和产物
          |
          v
确定性断言 + 轨迹指标 + 结构化 LLM Judge
          |
          v
生成带 Trace 证据和证据等级的失败假设
          |
          v
生成受约束的最小候选 Diff
          |
          v
dev 筛选 -> validation 门禁 -> holdout 验证
          |
          v
输出报告、候选补丁和人工审批建议
```

系统不会自动覆盖或发布生产 Skill。自动化负责执行、分析和验证，用户保留成功标准确认和最终修改审批权。

## 核心价值

- **减少人工中转**：一次运行完成日志收集、诊断、候选修改和回归验证。
- **让失败可复现**：冻结 Skill、Case、模型、工具、环境和 Grader 版本。
- **让诊断有证据**：每个评分和失败假设都能回指工具事件、错误或产物。
- **避免错误修复**：区分 Skill、模型、工具、权限、环境和 Eval Spec 故障。
- **防止测试集刷分**：Optimizer 只看 dev，validation 只返回晋级结果，holdout 最终运行。
- **控制成本与风险**：候选数、轮数、Token、费用、时间和修改范围均有硬上限。
- **沉淀团队资产**：真实失败转化为版本化 Case；D40 先进入 Evaluator Replay CI，具备凭证、Runtime 能力和预算后再进入 Subject Online Eval Gate。

## 核心创新点

### 1. Skill-native 评测

系统理解的不只是最终回答，还包括 Skill 描述、指令、脚本、参考资料、资源文件、触发行为和工具权限。

### 2. Trace-aware 故障归因

系统基于 Agent 输出、工具调用与结果、文件变化、异常和最终产物构建标准化 Trace，生成带证据的根因假设，而不是只把最终答案交给 LLM 打分。

### 3. 受控自动优化

Agent 只负责语义诊断、Judge 和候选补丁生成；状态、预算、权限、数据边界、停止条件和回归门禁由确定性工作流控制。

### 4. 隐藏回归与反向门禁

项目会特意展示一个“表面提升但产生误报”的候选被 validation 拒绝，证明系统不仅会生成修改，也能发现修改带来的回归。

### 5. 可迁移到完整 Agent

核心抽象面向 `SubjectUnderTest`，Skill 只是第一种实现。后续同一套 Runner、Trace 和 Grader 可迁移到 system prompt、工具配置、Workflow 和完整 Agent；D40 对 Agent 只承诺 capability-gated 评测与诊断，受控优化目前只支持 `SkillSubject`。

## 系统形态

Skill Doctor 不是重新实现一个 Agent Runtime，也不是多个 Agent 自由聊天。它采用：

> 确定性评测工作流 + 受约束的语义 Agent 节点 + 可插拔 Subject/Runtime/Grader。

```text
CLI / Report / CI
        |
Evaluation Orchestrator
        |
Subject Adapter --- Runtime Adapter --- Scenario Suite
        |                  |
        +----------- Sandbox Runner
                           |
                  Trace + Artifacts
                           |
       Deterministic Grader + LLM Judge
                           |
               Analyzer + Optimizer
                           |
                  Regression Gate
```

公司内部 Agent API 将作为首个 Runtime：评测系统负责创建独立 Session、指定被测版本、提交 Case、获取工具调用与 Agent 输出序列，并将公司日志转换为统一 Trace。

## 为什么不是 API Wrapper

Agent Runtime 解决“Agent 如何执行任务”，Skill Doctor 解决的是：

- 什么结果算正确；
- 如何进行可控的新旧版本对照；
- 如何从不完整 Trace 中定位失败步骤；
- 如何校准 LLM Judge；
- 如何区分 Skill 缺陷和非 Skill 故障；
- 如何防止候选对少量 Case 过拟合；
- 如何在预算、安全和回归约束内停止优化。

项目的技术重点是实验控制、Trace 语义化、混合评测、故障归因和受控优化，而不是重复建设模型调用与工具循环。

## 黑客松旗舰 Demo

首个 Demo 选择“安全代码审查 Skill”：

- 原始 Skill 漏报路径穿越或命令注入；
- 确定性 Grader 判断漏洞是否被正确报告；
- LLM Judge 只评价解释质量和修复建议；
- 一个过度宽泛候选因为误报安全代码而被拒绝；
- 一个精确候选进入 holdout 并完成最终验证；
- 一个工具超时 Case 被判断为非 Skill 故障，系统拒绝错误修改。

现场采用三层演示保障：一个最小 Live Case、一次真实历史 Run Replay，以及完整流程录屏。

## 技术架构

计划技术栈：

| 层次 | 方案 |
|---|---|
| 核心语言 | Python 3.12、Pydantic v2 |
| CLI | Typer |
| 工作流 | 显式状态机、`asyncio` |
| Runtime | 公司 Agent API Adapter、Replay Adapter |
| Trace | Raw JSONL + Canonical JSONL |
| 评分 | 确定性 Grader、轨迹指标、结构化 LLM Judge |
| 存储 | SQLite + content-addressed artifacts |
| 报告 | Jinja2 静态 HTML/Markdown |
| 隔离 | MVP 受限工作区；求职版受限 Worker，容器为条件扩展 |
| CI | pytest、Golden Replay、GitHub Action |

完整接口、数据模型、状态机、安全边界和逐日计划见 [TECHNICAL_DESIGN.md](./TECHNICAL_DESIGN.md)。公开 Skill 排行、分类口径、代表任务输入输出与分类自迭代方案见 [SKILL_CATEGORY_AND_ITERATION_DESIGN.md](./SKILL_CATEGORY_AND_ITERATION_DESIGN.md)。

## 交付计划

### 20 天：黑客松 MVP

- 一个真实 Agent Runtime Adapter；
- Skill 新旧版本配对执行；
- 机器可读 Case、Trace 和 artifact；
- 确定性 Grader + 结构化 LLM Judge；
- 带证据和证据等级的失败诊断假设；
- 候选 `SKILL.md` Diff、validation 和 holdout 门禁；
- 预算、超时、停止条件、静态报告和 Replay；
- 6 分钟演示脚本与录屏降级方案。

### 40 天：求职作品

- 新增 capability-gated `AgentSubject`；若公司 API 配置不可控，则明确降级为 `FixedAgentTarget`；
- 支持在线执行和带 completeness flags 的离线 Trace Import，缺少 Case/artifact/state 时只做局部评分；
- 受限 Worker；容器为条件扩展；
- 至少 2 个被测配置或任务族、12–20 个分层 Case；
- 20–30 个标注单元的 Judge pilot calibration；
- 多次重复实验、成本和波动统计；
- Output-only vs Trace-aware 消融实验；
- Evaluator Replay CI Gate、完整文档和可复现实验报告；Subject Online Eval Gate 为 D40 后条件能力。

XLSX 结构化产物 Grader、第二真实 Runtime、20–30 Case、独立归因金标集、容器、第二组消融和 Web 历史趋势页属于扩展目标，不影响 D40 核心交付。

## 评测指标

项目最终不会只展示一个总分，而会报告：

- `task_pass_rate`
- `paired_uplift`
- `hard_regression_count`
- `hidden_regression_rate`
- `flake_rate`
- `diagnosis_top1_accuracy`
- `judge_human_agreement`
- `median_time_to_fix`
- `fix_success_within_budget`
- Token、费用和延迟

所有提升数字都将在真实 Benchmark 完成后填写，不使用未经实验验证的宣传数据。

## 当前仓库内容

- [README.md](./README.md)：黑客松项目简介与仓库首页；
- [TECHNICAL_DESIGN.md](./TECHNICAL_DESIGN.md)：初版技术方案、D1–D40 计划和验收标准；
- [SKILL_CATEGORY_AND_ITERATION_DESIGN.md](./SKILL_CATEGORY_AND_ITERATION_DESIGN.md)：Skill 排行分析、双轴分类、输入输出契约和自迭代设计；
- [research/skill-ranking](./research/skill-ranking)：榜单原始快照、时间/hash 和可复算分类脚本。

## 项目边界

- 不采集或依赖模型隐藏思维链；
- 不将合成 Case 自动视为真实标准；
- 不让 Optimizer 修改 Case、Grader、holdout 或 Runner；
- 不自动覆盖、合并或发布生产 Skill；
- MVP 的同机目录隔离不是生产级安全沙箱，该限制会被明确披露。

## 长期定位

> Agent Capability EvalOps：面向 Agent 能力组件和完整 Agent 的持续评测与故障归因框架；仅在存在受约束 Optimizer Adapter 时提供候选优化，首个可优化对象是 Agent Skills。
