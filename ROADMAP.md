# Skill Doctor 更新路线图与待办

> 状态日期：2026-08-18  
> 适用版本：`aceval 0.2.0` 之后  
> 本文是 D20 黑客松交付和 D40 求职作品的当前计划基线；旧研究文档中的时间安排保留为决策记录，若有冲突以本文为准。

## 1. 当前产品结论

项目已经提前完成了一批原本属于 D40 的基础能力。下一阶段不应继续横向堆叠工具面，而应优先证明五件事：

1. 在真实模型上，repair/tune 的提升证据可复现；
2. 系统能从复杂 Skill 和少量种子 Case 建立有来源依据的测试计划；
3. EvalPack 的生成、覆盖、校准和冻结足够可信；
4. 系统能把 Skill 可干预失败与 CLI、Agent、Runtime、环境和 evaluator 问题分开；
5. 系统能够接入现实 Agent 环境，并以低门槛方式解释执行过程和结果。

产品形态确定为：

```text
可信 EvalOps Kernel
  + Reference Agent Runtime
  + Skill Analyzer / Test Planner / Coverage
  + EvalPack Builder / Quality / Calibration
  + Failure Attribution / Patch Authorization
  + Company Runtime / Session Connectors
  + Local Visual Console
```

短期不建设多租户 SaaS、通用插件市场或覆盖所有 Agent 平台的适配层。

## 2. 当前已实现

### 2.1 核心执行与优化

- [x] 内置 Reference Agent Runtime；
- [x] FakeRuntime conformance；
- [x] `run`、`compare`、`optimize`；
- [x] `auto | repair | tune`；
- [x] hard Grader guardrail；
- [x] Token、成本、耗时、工具调用和 Grader Objective；
- [x] tune 对 parent 与原 baseline 的双重改善检查；
- [x] validation/holdout paired gate；
- [x] 候选 lineage、预算、补丁与路径约束；
- [x] JSON/Markdown 报告。

### 2.2 EvalPack

- [x] `v1alpha1` legacy repair；
- [x] `v1alpha2` repair/tune；
- [x] `generic`、`csv-summary`、`security-review` Builder 模板；
- [x] 未知类型 generic fallback；
- [x] Goal 到效率 Objective 的保守推断；
- [x] `draft -> calibrating -> frozen`；
- [x] frozen Pack 逐文件内容锁；
- [x] draft/calibrating Pack 禁止优化 Skill；
- [x] `pack generate/calibrate/freeze/lint/test`。

### 2.3 产品入口与公司连接

- [x] `doctor` 串联 Pack、baseline、repair/tune 和报告；
- [x] Company API Profile；
- [x] Execute endpoint 配置；
- [x] Session fetch/import；
- [x] `ImportedRunBundle`；
- [x] Output、Canonical Trace、Usage、Observation Completeness 归一化；
- [x] 204 项自动测试。

### 2.4 当前尚未实现的差异化能力

- [ ] 读取复杂 Skill 并生成 Capability Graph；
- [ ] 从能力、分支、风险和工具依赖编译 Test Requirement；
- [ ] 在少量种子 Case 基础上自动补充 Case/Oracle 草稿；
- [ ] requirement-to-case、Runtime 可执行性和动态路径覆盖；
- [ ] Pack mutation calibration 和已知好坏样本区分能力；
- [ ] 结构化 Failure Card、故障分类和 Skill Patch Authorization；
- [ ] Imported Session 的 Replay、局部重评分和诊断；
- [ ] 受控 CLI Process Tool 与诊断探针。

## 3. 当前真实用户路径

### 3.1 已支持类型：快速路径

用户提供 Skill、Cases、类型、Goal 和 Runtime 配置：

```bash
aceval doctor \
  --subject ./my-skill \
  --cases ./cases.json \
  --type csv-summary \
  --goal '结果准确，并减少工具调用' \
  --pack-output .aceval/packs/my-pack \
  --approve-pack \
  --runtime reference \
  --model-command 'python model_bridge.py'
```

系统生成并冻结受支持模板 Pack，运行 baseline，自动选择 repair/tune，并执行 dev、validation 和可选 holdout 门禁。

### 3.2 已支持类型：推荐可信路径

对于正式实验，先查看评测契约再冻结：

```text
pack generate
  -> inspect/edit
  -> pack calibrate
  -> pack lint/test
  -> pack freeze --approve
  -> doctor --pack ...
```

### 3.3 未支持类型

未知类型一键生成 generic draft，但 `doctor` 即使收到 `--approve-pack` 也会停在 `calibration_required`。用户必须补充或确认语义 Oracle/Grader，单独冻结后才能运行 Skill 优化。

### 3.4 已有 frozen Pack

用户只需 Skill、Pack 和 Runtime：

```bash
aceval doctor \
  --pack ./evalpacks/my-pack \
  --subject ./my-skill \
  --runtime reference \
  --model-command 'python model_bridge.py'
```

### 3.5 公司 Session

当前可以配置、拉取并归一化 Session：

```text
profile validate
  -> session fetch/import
  -> ImportedRunBundle
```

尚未完成的闭环是：`ImportedRunBundle -> EvalRun -> Grader Replay -> Doctor/Report`。

### 3.6 目标复杂 Skill 路径（Planned）

```text
Skill + Goal + 2–5 个种子 Case + Runtime Profile
  -> Capability Graph
  -> Test Plan + Case/Oracle drafts
  -> Coverage Matrix + Runtime gaps + Freeze blockers
  -> 用户只确认高风险未决项
  -> Pack Quality Gate + freeze
  -> baseline + Failure Cards
  -> 仅 patch-authorized 失败进入 repair/tune
  -> validation/holdout + coverage/diagnosis report
```

复杂 Skill 的自动化目标是声明能力、关键风险、工具和状态路径的可追踪覆盖，不承诺开放自然语言空间的数学意义全路径覆盖。详细设计见 [COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md](./COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md)。

## 4. 已被前置实现的原 D40 内容

| 原 D40 能力 | 当前状态 | 新的后续目标 |
|---|---|---|
| Repair 与性能优化 | 已实现 | 多次采样、统计与更强目标策略 |
| Token/成本/延迟/工具调用评测 | 已实现单样本 | p50/p95、方差、置信区间、flake |
| Pack 初始化 | 已实现 Builder | Complex Skill Planner、Coverage 与 Pack Quality Gate |
| Goal 到 Objective | 已实现效率目标推断 | 主观 Rubric 澄清与人工确认 |
| EvalPack 生命周期 | 已实现 | 版本差异和校准质量报告 |
| Doctor 工作流 | 已实现 CLI | 可视化操作台和更友好的交互 |
| 公司 API 配置 | 已实现 | CompanyRuntimeAdapter |
| Session 日志导入 | 已实现 | EvalRun/Replay/Diagnosis/Case 生成接线 |
| Observation completeness | 已实现 | 可重评分范围、归因证据等级与门禁策略 |
| Paired validation/holdout | 已实现 | 重复实验和统计显著性 |

## 5. D20 剩余待办：黑客松可信演示

### P0：必须完成

- [ ] 接入至少一个真实模型 Bridge；
- [ ] 冻结模型 ID、参数、Runtime Profile 和预算；
- [ ] 完成一个真实 repair 实验；
- [ ] 完成一个真实 tune 实验；
- [ ] 关键 Case 至少重复 3 次；
- [ ] 报告真实 Token、延迟、工具调用和失败率；
- [ ] 收集一个 Runtime/Grader 故障不会触发 Skill 修改的反例；
- [ ] 将 FakeRuntime 结果显式标记为 simulation；
- [ ] 对一个处于当前文件 Runtime 能力范围内的复杂 Skill 生成 Capability Graph；
- [ ] 从 2–5 个种子 Case 生成 6–12 条 Test Requirement 和 Case 草稿；
- [ ] 输出 requirement-to-case Coverage Matrix、Runtime gap 和 Freeze blockers；
- [ ] 所有能力节点具有 Skill source ref 或显式 `inferred` 标记；
- [ ] model-proposed Oracle 保持待确认，不能自动成为 frozen hard gate；
- [ ] 生成结构化 Failure Card，并区分 origin、remediation 和 Skill patch authorization；
- [ ] 演示一张可触发 Skill repair 的 Failure Card；
- [ ] 导入一条 CLI/环境故障 Session，演示不可触发 Skill 修改的 Failure Card；
- [ ] 完成 6 分钟演示脚本和录屏备份。

### P1：高收益增强

- [ ] 生成只读 HTML 实验报告；
- [ ] 展示 baseline/candidate Skill diff；
- [ ] 展示 Gate 时间线和 Case 结果矩阵；
- [ ] 展示 Capability/Test Requirement/Case 覆盖矩阵；
- [ ] 展示 Failure Card 和证据引用；
- [ ] 展示一个过度修复候选被 validation 拒绝；
- [ ] 生成少量受限 mutant 并报告 mutation score；
- [ ] 记录第二类 Pack 的接入文件和工时。

### D20 完成定义

- 真实 repair 和 tune 各至少一份可复现报告；
- 从 Skill + Goal + 种子 Case 到 draft Pack 不需要用户手写 Manifest；
- 关键未覆盖路径、不可执行 Runtime 能力和未确认 Oracle 不会被隐藏；
- 一张 Skill 行为失败 Card 可以进入 Optimizer，一张 CLI/Runtime/Grader Card 明确阻止修改；
- 所有提升数字都能关联 Pack hash、Subject hash 和 Runtime Profile；
- 演示中能说明为什么候选被接受或拒绝；
- 没有把 FakeRuntime 或单次样本描述成真实 Benchmark；
- 新用户能按 README 在 15 分钟内复现离线演示。

## 6. 更新后的 D21–D40 计划

### D21–D24：规划/归因契约与真实证据

- [ ] 扩大真实 repair/tune Benchmark；
- [ ] 设计重复执行 API；
- [ ] 固化 `CapabilityGraph`、`TestPlan`、`CoverageReport`、`FailureCard` 版本化契约；
- [ ] 建立 3–5 个手工标注复杂 Skill 的 Planner Benchmark；
- [ ] 建立 Runtime、CLI、Driver、Grader 和证据缺失的 canonical failure fixtures；
- [ ] 完成 source-ref、Case provenance、Runtime gap 和保守 Patch Authorization；
- [ ] 增加 Run index 和版本化 detailed report；
- [ ] 持久化完整 Trace、Grader evidence、Patch 和关键 artifact 引用；
- [ ] 完成静态 HTML 报告。

### D25–D28：Case 生成与 Pack Quality

- [ ] seed expansion、boundary 和 metamorphic Case 生成；
- [ ] requirement-to-case、Oracle-ready 和 Runtime-executable coverage；
- [ ] known-good/known-bad 区分能力；
- [ ] mutation calibration、split family 泄漏和 Pack revision diff；
- [ ] critical gap、未确认 Oracle 和 capability mismatch 冻结门禁；
- [ ] 从 CLI 抽取 `ApplicationService`；
- [ ] CLI、Web、未来 API 共用同一服务层；
- [ ] 静态 HTML 展示 Test Plan、Coverage 和 Failure Cards；
- [ ] 完成 Read-only Console 骨架。

### D29–D32：公司 Agent 闭环

- [ ] 实现 `CompanyRuntimeAdapter`；
- [ ] Execute -> session_id -> fetch -> RuntimeResult；
- [ ] Imported Session -> EvalRun；
- [ ] Grader Replay；
- [ ] Replay completeness 和可重评分范围门禁；
- [ ] Trace-only Session 的局部 Failure Attribution；
- [ ] Session 到候选 Case 草稿；
- [ ] Console 展示 Session Trace 与 completeness。

### D33–D35：受控 CLI 与诊断探针

- [ ] `process_exec_v1` argv-only 工具；
- [ ] executable/env allowlist、超时和输出大小限制；
- [ ] exit code、stderr ref、tool version 和 retryable 结构化 Trace；
- [ ] CLI binary/version/permission/auth/network 分类；
- [ ] Runtime/Profile health check；
- [ ] 同配置重跑、Mock tool replay 和最小只读依赖探针；
- [ ] 证据不足时保持 `unknown`，不授权 Skill Patch。

### D36–D38：统计、动态覆盖与 Console

- [ ] 重复运行聚合；
- [ ] p50/p95、均值、方差和置信区间；
- [ ] flake rate；
- [ ] dynamic tool/state-transition coverage；
- [ ] 归因稳定性和 failure fingerprint 聚合；
- [ ] 一个经用户确认 Rubric 的主观任务；
- [ ] A/B 偏好或人工标注；
- [ ] Judge agreement 和不确定性处理；
- [ ] Operational Console、Experiment 事件协议和 SSE；
- [ ] Pack 校准、Coverage 和 Diagnosis 页面。

### D39–D40：求职作品化

- [ ] FixedAgentTarget 最小迁移验证；
- [ ] CI Gate；
- [ ] 一键 Replay；
- [ ] 完整教程、架构图、演示视频和限制说明；
- [ ] 发布 `aceval 0.3.0`；
- [ ] 准备项目介绍、技术难点、消融实验和面试讲解材料。

## 7. 可视化 Console 决策

Console 被纳入 D40，但定位为本地单用户 EvalOps 操作台，不是在线 SaaS。

优先展示：

1. Pack 校准与冻结；
2. Capability Graph、Test Plan、Coverage Matrix 和 Runtime gap；
3. baseline -> repair/tune -> validation -> holdout 进度；
4. Case、Grader evidence、Trace 和 Failure Card；
5. baseline/candidate 指标与 Skill diff；
6. 历史 Run、Session Import、Diagnosis 和 Replay。

详细方案见 [VISUAL_CONSOLE_DESIGN.md](./VISUAL_CONSOLE_DESIGN.md)。

## 8. 明确延后

下列能力不进入 D40 P0：

- [ ] 完整多文件/二进制 Skill 优化；
- [ ] shell/network/browser/multimodal 全工具面；
- [ ] 自由 shell 和 Pack 自带任意命令执行；
- [ ] 数学意义的任意自然语言全路径覆盖；
- [ ] 自动递归优化并直接冻结 EvalPack；
- [ ] 将同源自动生成 Case 作为 sealed holdout；
- [ ] 从单次日志宣称唯一强因果根因；
- [ ] 通用插件市场；
- [ ] 自动优化完整 Agent 配置；
- [ ] 生产级恶意代码沙箱；
- [ ] 多租户 SaaS、RBAC、计费和分布式调度；
- [ ] 任意 Agent 平台的统一适配承诺。

## 9. 决策原则

- 真实证据优先于新增功能；
- Pack 和 Skill 不在同一实验中共同漂移；
- Planner 结论必须有 source ref 或显式标记为推断；
- 覆盖率必须给出范围、分子、分母和未覆盖原因；
- 故障发生位置、根因假设和补救面分别表达；
- LLM 可以提出测试与归因草稿，不能独立冻结 Oracle 或授权修改；
- UI 不重新实现 Kernel 语义；
- 缺失 telemetry 不推断为零；
- 主观标准必须经过用户确认和校准；
- 同源合成 Case 默认只作为 dev/validation 草稿，不冒充独立 holdout；
- 单 Runtime 结果不外推为跨平台最优；
- 每个演示数字必须可追溯到不可变实验输入。
