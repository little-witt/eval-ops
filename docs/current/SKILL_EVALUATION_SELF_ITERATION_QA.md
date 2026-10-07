# Skill 评测与自迭代：七个架构问题说明

> 版本：2026-08-28  
> 适用对象：FORGE / ACEval Skill 评测与自迭代流程  
> 状态：基于当前 V2/P0 本地控制面和 V3 目标架构的说明；部分关键链路已实现，完整 V3 与真实远端两轮仍待验收

## 0. 一页结论

这套流程不是“模型读日志、打分、改 Skill，再让模型判断自己是否变好”。更准确的分工是：

> **系统负责可复现的事实、权限、状态、证据、比较、发布和停止；模型负责难以规则化的语义判断、根因假设和候选内容生成；用户负责目标、取舍和高风险修改的授权。**

因此：

- Case 和路径可以借助模型扩展，但不能由模型凭空发明事实、分支、Oracle 或硬门。
- 模型可以提出“应该怎么改”，不能直接决定“这就是根因”“改动已生效”或“候选可以发布”。
- 证据不完整、环境异常、Fixture 污染或 Grader 失效时，系统返回 `not_evaluable`，不能把基础设施问题记成 Skill 失败。
- 优化目标是**在冻结评测契约下安全改进并停止**，不是证明全局最优，也不是保证绝对不会出现未知问题。

本文中的状态标记含义：

| 标记 | 含义 |
|---|---|
| **本地已实现（V2/P0）** | 当前代码和本地测试已有对应控制能力，不等于真实 CATX 或生产业务验收 |
| **部分实现（本地）** | 主干能力已落地，但仍有可信性边界、兼容层或远端验收缺口 |
| **V3 设计** | 已形成契约和方案，但不应当当作全部上线能力 |
| **待补齐** | 仍需工程实现、校准或真实业务验收 |

## 1. 模型与系统的职责边界

### 1.1 评测流水线不是单一模型调用

推荐的流水线如下：

```text
冻结 Skill / 目标 / 环境 / 评测版本
        ↓
确定性资源分析与 Path / Capability IR
        ↓
Test Obligation Graph 与风险驱动 Case 规划
        ↓
模型可选地补充语义 Case 文案或开放问题
        ↓
Case / Path / Oracle / Fixture 质量门与冻结
        ↓
隔离 Session 执行，采集完整 Trace 与产物
        ↓
证据有效性门 + 硬 Grader
        ↓
模型做受限语义评分、归因假设和 Proposal
        ↓
系统生成候选、静态校验、回归、成对比较
        ↓
Champion 晋升、拒绝或安全停止
```

### 1.2 各组件分别做什么

| 阶段 | 系统（确定性控制面） | 模型（受限参与） | 用户/专家 |
|---|---|---|---|
| 资源与路径分析 | 冻结 commit、文件闭包、工具/参数、版本和 hash；解析可追溯的步骤、条件、状态和副作用 | 对无法规则化的自然语言做候选归一化 | 确认歧义、目标和非目标 |
| Case 生成 | 从义务图按风险、覆盖和预算选 Case；校验 schema、可执行性、可观察性、公平性 | 润色任务、提出边界/负向/恢复场景草稿 | 提供用户 Case、标准和敏感约束 |
| 评测执行 | 隔离环境、Session 绑定、超时、重试、日志完整性和权限 | 执行被授权的 Agent 行为；不能改评测契约 | 必要时批准外部副作用 |
| 结果判定 | 状态、产物、Trace、参数、禁止动作等硬事实；`not_evaluable`；版本化聚合 | 单一语义维度 Judge，不能覆盖硬事实 | 校准开放语义标准、复核争议结果 |
| 失败分析 | 失败签名、聚类、归因门、影响图、冲突矩阵 | 提出根因假设和可证伪的修复假设 | 确认目标取舍和是否接受风险 |
| Skill 修改 | allowlist、精确补丁、范围/语法/安全检查、隔离 worktree、不可变候选 | 生成候选文本或补丁内容 | 批准高风险或大范围修改 |
| 晋升与停止 | Champion/Challenger、回归保护、预算、循环检测、Holdout、停止状态 | 无晋升、回滚或停止权限 | 可主动停止或改变目标 |

### 1.3 当前代码对应关系

- **本地已实现（V2/P0）**：Skill 资源闭包冻结、确定性分析和风险规划；`iteration_brain.py` 将模型工作拆成证据冻结、语义评分、跨 Case 归因、Proposal 四阶段；`kernel_v2.py` 负责候选比较和收敛控制。
- **本地已实现（受限模型生成）**：`iteration_kernel.py::_model_generated_cases` 可让模型润色或补充 Case；失败时回退到确定性结果，模型不能拥有 planner 的 Case 身份。探索 Case 可在设计页逐 Case 补充人能核对的通过标准，未经确认不能授权修改。
- **本地已实现（修改与发布边界）**：`skill_tree_optimizer.py` 支持 allowlist、精确 `replace_text` / `create_file`、唯一旧文本、文件/行/字节限制和静态校验；候选生成后还需第二次人工批准才可发布。
- **部分实现（日志和评分）**：CATX 已有 cursor 分页、`total`、部分 seq、工具配对、整体日志 SHA-256、Attempt 重试历史；证据分析已执行 frozen/legacy EvalPack 内置 Grader、多维评分和 hard gate，但尚无完整事件 hash chain、Reference Run 和统一 V3 评分标尺。
- **V3 设计/兼容原型**：`execution_path.py` 可只读归一化少量 V3 `nodes/edges` 字段；在原生条件分支、重试和状态 Evaluator 完成前，V3 路径强制 `not_evaluable`，不得授权 Skill 修改。

## 2. 问题一：路径分析、Case 生成和结果分析是否都由模型完成？

### 结论

不是。模型只参与开放语义部分；决定评测资格、事实结果和发布结论的必须是系统。

### 系统层面的主要手段

1. **资源冻结与可追溯**：固定 Skill commit、引用资源闭包、工具 Schema、Runtime capability、Policy、解析器版本和内容 hash。
2. **Path/Obligation 编译**：从 `SKILL.md`、脚本、配置和引用资料提取触发条件、步骤、分支、输入输出、错误/重试、状态和副作用，再形成测试义务图。
3. **Case Gate**：逐项检查可执行性、可解性、Oracle 可信度、可观察性、隔离、公平性和反作弊；未通过的 Case 只能做探索，不能授权修改。
4. **证据有效性门**：先判断 Session、环境、Fixture、日志、工具返回和 Grader 是否有效，再判 Skill 质量。
5. **分层 Verdict**：一次运行是 `AttemptVerdict`，同一 Case 多次运行聚合为 `CaseAggregate`，版本比较才产生 `CandidateComparison`。
6. **权限和状态机**：模型输出必须通过 schema、输入/输出 hash、事件收据、版本和 ACL 校验；只有系统状态机能推进候选、发布和停止。

### 模型可以做什么

- 识别规则难以穷举的语义差异；
- 对日志证据给出单一维度的语义判断；
- 提出根因假设、候选 Case 文案和最小修改建议；
- 对候选进行独立 Critique。

### 模型不可以做什么

- 把未在 source ref 中出现的分支或事实写进硬契约；
- 自己创建可信 Oracle，或把 `model_proposed` 当成已校准标准；
- 把环境故障、日志缺失或工具问题归因给 Skill；
- 推翻确定性硬失败、把 `not_evaluable` 改成通过/失败；
- 直接写仓库、发布候选或宣布收敛。

## 3. 问题二：Skill 不是代码模块，怎样保证修改得准？

### 3.1 先把修改定义成“可证伪实验假设”

每个 Proposal 都要绑定：

- 问题簇、失败签名和原始证据；
- 受影响的 requirement、能力、路径步骤和 source ref；
- 预计改善的 Case/维度；
- 必须保护的稳定通过 Case；
- 精确文件范围和修改策略；
- 可能冲突、回归风险、预算和验证计划。

模型给出的是假设，不是结论。系统必须先问“哪些证据能证伪它”，再决定是否生成候选。

### 3.2 用“资源图”代替代码模块边界

Skill 虽然没有函数/模块，却可以建立可控的逻辑边界：

```text
目标/标准
  → 能力节点
  → 义务与路径步骤
  → SKILL.md 段落 / 脚本 / 配置 / 模板 source ref
  → 受影响 Case 与保护 Case
```

这样修改的是某个带来源和影响范围的要求块，而不是让模型重写整份自然语言文档。目标是最小语义 Diff；高级编辑实际创建新 revision，不原地覆盖旧契约。

### 3.3 四层准确性与风险控制

| 层 | 控制 |
|---|---|
| 归因 | 只对 `Skill-attributed` 且证据充分的问题授权；环境、模型随机性、工具、Fixture、Oracle 和证据问题不通过 |
| 变更 | allowlist、精确替换、唯一旧文本、文件/行/字节上限；默认禁止任意删除、重命名、仓库外写入和敏感文件修改 |
| 本地验证 | Markdown/脚本解析、静态检查、引用完整性、冲突/重复规则、敏感信息和 Git worktree 原子提交 |
| 行为验证 | 先跑受影响 Dev Case，再跑完整 Dev、Regression、Validation，必要时重复和 Holdout；按同一冻结契约做 Champion/Challenger 比较 |

### 3.4 能保证到什么程度

系统可以显著降低“改错对象、改大范围、掩盖环境问题和引入已知回归”的概率，并保证未过门的候选不会覆盖 Champion；不能证明自然语言修改没有任何未知副作用。对高风险 Skill，应增加人工审阅、独立 Judge、故障注入、变异测试和分阶段发布。

### 3.5 当前边界

- **本地已实现（V2/P0）**：多文件候选、静态/范围保护、修改范围批准、候选发布二次批准、不可变 commit、Champion/Challenger 比较和拒绝恢复。
- **本地已实现（可信授权）**：评测设计页支持逐 Case 人工通过标准；能力 Blueprint 中由模型提出的精确期望保持 `model_proposed`，批准能力方案不会把它隐式升级为可信 Oracle。
- **V3/待补齐**：更精细的语义影响图、完整 Case/Suite preflight、Reference Run、原生路径条件/状态评估、Judge 专家校准，以及跨 revision 的历史失败自动晋升与逐条追踪。

## 4. 问题三：下一轮 Path/Case 会不会变化？怎样保证上一轮缺陷没有丢？

### 4.1 先区分三种变化

| 变化 | Path/Case 处理 | 是否可直接和上一轮比较 |
|---|---|---|
| 仅修改 Skill 实现，目标/标准/能力范围不变 | 复用同一 Frozen Case、Path、Oracle 和 Grader revision；只替换运行中的 `skill_commit` | 可以，前提是环境和模型/工具等运行上下文 hash 不变 |
| 发现原设计覆盖缺口，但目标不变 | 保留旧套件，新增缺口 Case/Path revision；旧 Case 不删除 | 旧 Case 可直接比较，新 Case 从加入时起建立基线 |
| 用户目标、标准、能力范围、环境或 Grader 实质变化 | 创建新的 `eval_design_revision`，重新编译相关义务和路径；旧结果只作历史参考 | 不可把新旧总分当作同一指标；需重新校准 |

Case 不是每轮自动重写的清单。实现方式变化时保持设计契约稳定，才能测量改动是否真的改善；设计变化时必须显式 fork，并保留旧版本。

### 4.2 缺陷的跨轮生命周期

```text
失败 Attempt
  → Failure Signature / Problem Cluster
  → open（待验证）
  → fixed（候选在原 Case 上修复并通过保护门）
  → Regression（跨批次稳定通过后升级保护）
  ↘ not_fixed / reopened（新一轮再次失败）
  ↘ invalidated（Oracle、Fixture、目标或环境契约变化，需重新校准）
```

每个问题应有稳定的 `obligation_id`、`case_id + case_revision`、失败签名、source refs 和状态历史。候选至少要证明：

1. 原失败 Case 的目标维度改善；
2. 原失败对应的安全/副作用硬门没有退化；
3. 与其共享能力节点、资源或规则的保护 Case 没有出现 critical regression；
4. 在新的 design revision 中，旧问题被显式 carry-forward、重新校准或标记为 invalidated，并有原因。

### 4.3 当前实现的诚实边界

当前迭代会在补充诉求或新设计 revision 时清空旧环境 hash、重新冻结环境，并在同一 Case 集、证据有效性、hard gate 和 comparison context 下比较 Champion/Challenger，不会只按一个总分晋升候选。但仍有两个边界：第一，下一轮 CATX binding 必须绑定当前实际被测 Skill 快照 hash，不能继续沿用初始设计的 `source_subject_hash`；第二，“上一轮失败 Case 自动晋升 Regression 并逐条追踪 open/fixed/invalidated”尚未完全落地。在这些功能完成前，应显式核对被测 commit/hash 和 carry-forward 清单，不能假设它们自动正确。

## 5. 问题四：怎样保证收敛，而不是无限评测—优化循环？

### 5.1 收敛控制器的硬约束

- 最大轮次、候选数、Session 数、重试次数、Token、运行时间和模型反馈次数均有上限；
- 每轮保留不可变 Champion，Challenger 不通过门禁就拒绝，不覆盖 Champion；
- 晋升同时要求硬门通过、无 critical regression、目标维度达到最小实际收益、关键 Case 的 `pass^k` 达标、成本在预算内；
- 同一 `hypothesis_signature + target_scope + semantic_diff + affected_cluster` 重复失败时剪枝；
- 连续若干轮低于最小收益进入 plateau；目标互相冲突时停止并报告冲突，不继续盲改；
- Holdout（启用时）只用于受控最终检查，优化模型不可见其内容。

### 5.2 明确的停止状态

`target_met`、`plateau`、`cycle_detected`、`budget_exhausted`、`no_skill_attributed_failure`、`insufficient_evidence`、`eval_saturated`、`objective_conflict` 和 `human_stopped`。

### 5.3 “安全停止”不等于“全局最优”

系统能保证在有限预算内停止、保护已知能力和给出停止原因；不能保证搜索空间中不存在更好的 Skill。若用户改变目标、权衡或评测契约，应开启新的 design revision，而不是偷偷延长旧循环。

## 6. 问题五：与软件工程自动化测试有什么区别？

它借用了软件测试的成熟思想，但不是把单元测试/CI 原样搬过来。

| 方面 | 传统软件自动化测试 | Skill/Agent 评测 |
|---|---|---|
| 被测对象 | 相对确定的代码、接口和构建产物 | 自然语言规则、模型、工具调用、环境状态和资源树 |
| Oracle | 常有精确期望值或断言 | 允许多种合法解，需要状态、语义 Judge、参考运行和关系型 Oracle 组合 |
| 重复性 | 同输入通常应同输出 | 存在模型随机性、远程工具波动，要区分 `pass@k` 与 `pass^k` |
| 过程 | 函数/控制流/数据流 | 语义路径、工具参数、状态迁移、失败恢复、禁止副作用和完整 Trace |
| 失败归因 | 多数归到代码/依赖 | 必须区分 Skill、模型、工具、环境、Fixture、Oracle、Grader 和证据问题 |
| 修改面 | 函数/模块边界较清晰 | 文本、脚本、模板、配置可能共享语义，需资源图和影响分析 |
| 安全 | 通常测试环境内副作用可控 | Agent 可能越权、泄密、执行危险操作，安全/权限必须是硬门 |
| 优化方式 | 开发者修改代码、CI 回归 | 模型提出候选，系统做成对实验、回归、审批和停止 |
| 评测健康 | 测试失败通常直接有意义 | 先判断 Case 是否可解、Oracle 是否可信、日志是否完整，否则为 `not_evaluable` |

可复用的部分包括需求追溯、风险驱动、边界/故障测试、回归、变异测试、CI 门禁和成对比较；新增的核心层是 Path IR、证据有效性、语义 Judge 校准、失败归因、稳定性统计、权限硬门和候选收敛控制。路径覆盖仍不能证明 Skill 实现正确，自动化全绿也不能证明没有未知缺陷。

## 7. 问题六：能否迁移到 AI Coding Review 或 Agent 评测自迭代？

### 结论

可以迁移，真正可复用的是控制面和数据契约，不是把“修改 `SKILL.md`”硬套到所有对象上。

### 7.1 复用的公共内核

`EvaluationSubject` 可抽象为：

```text
EvaluationSubject
├─ SkillSubject（当前主产品）
└─ AgentSubject（模型/Prompt/工具/Harness 的组合）
```

两类对象都可以复用 Test Obligation、Case/Suite Gate、Fixture、Attempt/Trace、Evidence、Grader、Failure Signature、Diagnosis、Candidate Comparison、Regression/Holdout 和 Convergence。

### 7.2 AI Coding Review 的适配

- Case 使用带种子缺陷的仓库、无缺陷负样本、边界变更和权限/敏感信息样本；保留隐藏测试避免过拟合。
- 维度包括缺陷召回、误报率、严重级别、文件/行定位、源码证据、建议可执行性和不修改非目标区域。
- Candidate 可以是 Skill 规则、Prompt、审查策略、工具调用策略或 Grader 配置；每类候选需独立授权。
- 结果同时看评审文本、定位和仓库状态；不能因为“说得像对的”而忽略硬测试和安全门。

### 7.3 通用 Agent 的适配

`AgentSubject` 至少冻结：模型及版本、system/developer Prompt、Skill 集、工具 Schema、权限、记忆、Harness 配置、运行环境和依赖版本。Trace 还要包含 Thread/Role 关系、工具调用/返回、状态迁移和外部副作用。

需要特别注意：模型版本变化可能比 Skill Diff 影响更大；工具和权限变更不能由普通质量 Proposal 自动批准；不能把平台不可见行为或模型权重变化伪装成可控的配置修改。当前抽象已存在，但通用 Agent 的真实候选 Adapter、发布和两轮业务验收仍是后续工作。

## 8. 问题七：相较业界是否有创新？实用价值如何？

### 8.1 创新性应如何表述

不宜宣称某个单独算法或基础理论是全新发明。Agent Evals、Trace Grading、回归保护、Holdout、Prompt 优化、Pareto 候选和分布式 Trace 都已有业界或学术基础。

更可信的创新点是**面向 Skill 生命周期的系统级组合与工程闭环**：

1. 以 source-grounded Path/Obligation 图把自然语言 Skill、Case、证据和修改影响连起来；
2. 用证据有效性和失败归因把“评测坏了”与“Skill 变差了”分离；
3. 将语义模型限制在判断/假设/候选生成，把多文件修改、回归、权限、晋升和停止留在确定性控制面；
4. 把自然语言资源的修改纳入类似 Git 的候选、差异、回滚和 Champion/Challenger 实验；
5. 在本地优先、真实远程 Agent、完整 Trace 和跨轮保护之间形成一条可审计链。

这些属于系统创新或产品工程差异，是否构成研究创新或专利新颖性，仍需正式的先前技术检索、消融实验和同行评审，不能仅凭架构描述下结论。

### 8.2 与常见业界方案的差异

| 方案类型 | 常见强项 | 本流程补充的部分 |
|---|---|---|
| 通用 Agent Eval/Trace 平台 | Trial、Trace、Grader、可观测性 | 直接关联 Skill source ref、问题簇、候选 Diff 和发布门 |
| Coding Benchmark/自动测试 | 硬测试、可重复、回归清晰 | 扩展到非代码 Skill、多种合法路径、语义标准和副作用安全 |
| Prompt/程序优化器 | 反思、候选搜索、自动比较 | 不让优化器自行定义 Oracle、归因、权限或晋升；引入保护集和安全停止 |
| 日志/监控系统 | 运行可见性和告警 | 增加 Case 质量、证据资格、因果归因和可验证修改闭环 |

### 8.3 实用价值与适用边界

实用价值在以下场景最高：Skill 数量多、失败成本高、工具有副作用、需要审计或多人协作，且能够提供稳定 Fixture、Oracle 和回归资产。对一次性、低风险、完全确定性的简单 Skill，完整流程的建设成本可能高于收益。

早期落地应优先衡量：

- 已知缺陷发现率和修复后回归率；
- Skill/环境误归因率；
- 关键 Case 的 `pass^k` 和 flaky 率；
- 每轮 Token、Session、耗时和人工确认时间；
- 从失败到可发布候选的轮次；
- 与“仅人工评测”或“仅模型评分”基线相比的净收益。

只有真实业务两轮验收、Judge 校准、跨任务回归资产和消融对比完成后，才能把“有潜力”升级为“已证明实用”。

## 9. 当前状态、缺口与验收建议

| 能力 | 当前状态 | 说明 |
|---|---|---|
| Skill 资源冻结与候选保留 | **本地已实现（P0）** | `SKILL.md` 与资源闭包共同参与 hash；候选保留多文件资源树并经过范围/静态校验 |
| CATX 日志完整性 | **部分实现（本地）** | cursor 分页、`total`、部分 seq、工具配对、整体日志 SHA-256、Attempt/重试已有测试；hash chain、terminal、补抓和真实远端仍缺 |
| Oracle 与修改授权边界 | **本地已实现（P0 主链）** | 探索 Case 可逐 Case 人工校准；Blueprint 模型预期不会因能力审批而升级；draft 或内容漂移的 EvalPack 不能授权修改。Reference Run 与完整 Suite Gate 仍待补 |
| EvalPack 硬 Grader | **本地已实现，待完整校准门** | frozen/legacy Pack 的内置 Grader 已执行并映射多维分数/hard gate；Reference Run、Judge 校准和完整 Suite Gate 未完成 |
| 模型分阶段分析 | **本地已实现（P0）** | 四阶段、严格 JSON、输入/输出 hash、调用收据和可恢复状态；模型不能翻转确定性结果 |
| Skill 多文件受控修改与发布 | **本地已实现（P0）** | allowlist、精确补丁、静态检查、修改范围审批、候选发布二次审批和不可变提交 |
| 候选比较与恢复 | **本地主链已实现** | Case 集、证据、hard gate、comparison context 和逐维 delta 已比较；拒绝时保护/恢复 Champion，待真实两轮验收 |
| 桌面评测闭环 | **主要信息架构已实现** | Case 来源、Oracle 资格、人工校准、路径、评分、证据、Diff、审批、历史和 Attempt/重试原因已显化，主要原因码已翻译；图形深链和视觉 E2E 仍需补 |
| Path-first 原生执行 | **V3 设计/兼容原型** | 当前 V3 `nodes/edges` 只读兼容；条件分支、重试、状态 Evaluator 和完整 Trace 仍需补齐 |
| Case preflight / Judge 校准 | **待补齐** | 需要更完整的可执行性、可解性、专家一致性和 Reference Run 校准 |
| 历史失败跨 revision 自动 Regression | **待补齐** | 当前可记录身份和 revision，但自动 carry-forward、逐条 open/fixed/invalidated 尚未完整实现 |
| 通用 Agent 自迭代闭环 | **架构可复用/待验收** | `EvaluationSubject` / `AgentSubject` 已有抽象，真实 Adapter 和发布流程尚未完成 |
| 真实目标 Skill 两轮业务验收 | **待完成** | 不能仅用单元测试或模拟数据宣称生产闭环已被业务验证 |

### 9.1 本轮可声明与不可声明的边界

可以声明：

- 本地关键链路已覆盖资源冻结、Case 规划/生成、路径命令匹配、日志分页和完整性收据、证据 fail-closed、可信 EvalPack Grader、多维评分、修改授权过滤、候选静态校验、成对比较与主要桌面 read model；
- 本次环境中的 Python `unittest` 回归测试共 `464 tests`，全部通过；测试通过临时依赖目录提供 `tomli` 兼容包，以支持当前 Python 3.9 环境。`node --check desktop/renderer/app.js`、renderer contract 和 `git diff --check` 通过。

不能声明：

- 真实 CATX Session 创建、远端日志回收和完整两轮业务闭环通过；当前环境不在内网，未执行这项验收；
- 原生 V3 Path-first、完整事件 hash chain、Reference Run、Judge 校准、Case/Suite Gate、可交互 Trace overlay 已完成；
- Playwright 视觉 E2E、20–50 Case 性能和生产实用性已经证明。

建议的最小验收顺序：

1. 用一个真实 Skill 完成“首轮评测 → 失败归因 → 用户批准 → 候选 → 第二轮评测 → 晋升/拒绝”的完整两轮链路；
2. 人为注入日志缺失、环境漂移、工具超时和错误 Oracle，确认均能 `not_evaluable` 或阻断，而不会错误修改 Skill；
3. 注入已知 Skill 缺陷和回归缺陷，确认 Failure Signature、保护 Case 和拒绝原因可追溯；
4. 对 Path IR、确定性硬门和模型归因做消融，量化它们对误归因、回归和成本的影响；
5. 再决定是否扩大到跨任务 Case 资产库、D2C 自动编排和 AgentSubject。

## 10. 参考文档

- [Skill 评测与自迭代 Kernel V2](./EVALUATION_SELF_ITERATION_KERNEL_V2.md)
- [Skill 评测 V3：路径契约驱动方案](./SKILL_EVALUATION_CASE_PIPELINE_V3.md)
- [ACEval 最终产品与系统架构](./FINAL_SYSTEM_ARCHITECTURE.md)
- [当前基线与下一会话交接](./NEXT_SESSION_HANDOFF.md)
- [产品可用性与发布验证](./PRODUCT_RELEASE_READINESS.md)

业界参考入口（正式对外发布前应再次核对版本和访问日期）：

- [Anthropic — Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [OpenAI — Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
- [OpenAI — Trace grading](https://developers.openai.com/api/docs/guides/trace-grading)
- [LangSmith — Trajectory evaluations](https://docs.langchain.com/langsmith/trajectory-evals)
- [τ-bench](https://arxiv.org/abs/2406.12045)
- [GEPA](https://arxiv.org/abs/2507.19457)
