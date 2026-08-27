# Skill 评测与自迭代 Kernel V2

> 状态：核心方案基线（2026-08-26）  
> 适用范围：Skill 修复、优化、扩展、探索、新建后的「生成 → 评测 → 诊断 → 候选 → 回归 → 收敛」闭环  
> 客户端基线：`design/desktop-v2`，本方案只补充内核与任务详情信息契约，不改变已确认的 V2 布局和视觉方向

## 1. 核心结论

自迭代 Kernel 不能设计成「一个模型读日志、打总分、修改 Skill，再判断自己是否变好」。这会把 Case 质量、失败归因、修改方向和收敛判断同时交给同一个不稳定组件，既不可追溯，也容易为了当前 Case 过拟合。

V2 采用以下原则：

1. **确定性控制面，模型受限参与**：系统负责契约、证据、状态机、硬门、比较、回归保护和停止条件；模型只处理难以规则化的语义判定、根因假设与候选生成。
2. **结果优先，关键过程受控**：优先验证最终环境状态和产物；仅对安全、权限、业务规则或 Skill 明确要求的步骤做语义路径约束，不把唯一工具序列当成标准答案。
3. **评测先自证可信**：一个 Case 只有在可执行、可观察、Oracle 可信、任务可解且不会泄漏答案后，才允许参与优化决策。
4. **多目标晋升，不靠单一总分**：候选必须同时满足硬门、无关键回归、稳定性、有效收益和预算约束；总分只用于界面摘要，不用于单独决定晋升。
5. **保留 Champion，受控接受 Challenger**：当前最佳版本不可被未验证候选覆盖。每次修改都是带假设、影响范围、预期收益与风险的实验。
6. **收敛是可证明的安全停止，不是全局最优承诺**：系统可以保证在固定评测契约下不接受已知回归并在有限预算内停止，但不能证明找到了理论上的最优 Skill。

整体控制流：

```text
目标 / 用户 Case / Skill 资源 / 环境能力
  → Eval Design Compiler（测试义务、Case、语义路径、Oracle、质量门）
  → Trial Evaluator（证据有效性、确定性 Grader、语义 Grader、稳定性）
  → Diagnosis Graph（失败签名、归因、问题簇、冲突与影响面）
  → Candidate Lab（优化假设、受控多文件补丁、静态验证）
  → Convergence Controller（Champion / Challenger、回归、验证、Holdout、停止）
```

## 2. 业界对标与采用决策

业界没有一套可以直接照搬的「Skill 自迭代标准」，但 Agent Eval、软件工程基准与 Prompt Optimization 已形成可组合的共同方法。

| 对标能力 | 业界实践 | 本系统采用方式 |
|---|---|---|
| Agent Eval 基本单元 | Anthropic 将 task、trial、grader、transcript、outcome 和 harness 分离，并强调最终环境状态与完整轨迹都要保留 | Case、Attempt、Grader、Log、Outcome 分别建模，Session 日志不直接等于结果 |
| Case 质量 | Anthropic 建议任务无歧义、提供 reference solution、正负样本平衡、环境隔离；SWE-bench Verified 用人工筛选排除不公平任务 | 引入 Case 质量门、Reference Run、正负触发平衡、Fixture 隔离和 Eval 健康度 |
| 路径评测 | OpenAI Trace Grading 对端到端决策和工具调用打结构化标签；LangSmith 同时支持 strict、unordered、subset、superset 与 LLM judge | 采用语义部分有序路径：required / alternative / forbidden / recommended，不默认固定完整序列 |
| Grader 组合 | Anthropic、OpenAI、LangSmith 都组合代码规则、模型 Judge 与人工校准 | 硬事实由确定性 Grader 决定；模型按单一维度隔离打分；高风险规则由人工样本校准 |
| 回归保护 | SWE-bench 同时要求 FAIL_TO_PASS 与 PASS_TO_PASS | 新失败能力必须改善，已有稳定通过能力必须保持 |
| 可靠性 | Anthropic 和 τ-bench 使用 pass@k 与 pass^k 区分「偶尔成功」和「稳定成功」 | 生产 Skill 的关键 Case 使用 pass^k；探索能力可补充 pass@k，但不能替代可靠性门 |
| 候选优化 | OpenAI 建议 held-out 数据、pairwise 判断和持续评测；GEPA 用轨迹反思、候选测试和 Pareto frontier | 模型生成优化假设，系统做成对比较、保留非支配候选和唯一 Champion，Holdout 只用于受控晋升 |
| 防止评测投机 | OpenAI Graders 强调 reward hacking；Anthropic 强调阅读轨迹、检查不公平 Grader | 优化器不可看到封闭 Holdout 的内容；硬 Grader、Case 与候选变更均版本化；异常高分触发审计 |

主要参考：

- [Anthropic：Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [OpenAI：Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
- [OpenAI：Trace grading](https://developers.openai.com/api/docs/guides/trace-grading)
- [OpenAI：Graders](https://developers.openai.com/api/docs/guides/graders)
- [OpenAI：Introducing SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/)
- [LangSmith：Trajectory evaluations](https://docs.langchain.com/langsmith/trajectory-evals)
- [τ-bench](https://arxiv.org/abs/2406.12045)
- [GEPA](https://arxiv.org/abs/2507.19457)

## 3. 当前实现审计

### 3.1 可直接保留的基础

当前代码已经具备以下正确方向：

- `test_planning.py`：从 Skill 能力图生成测试需求，覆盖正常、负向、恢复、幂等、状态转换和顺序等维度，并用「未覆盖风险 / 预计成本」做有界 Case 选择。
- `execution_path.py`：支持 required、recommended、alternative、forbidden 和顺序约束，按语义检查点匹配真实 Trace。
- `graders.py`：提供 JSON、记录、产物、来源、Trace、Workspace Diff 等确定性 Grader。
- `pack_quality.py`：已区分 Oracle 可信度、Case 来源、Holdout 与质量阻塞项。
- `evidence_analysis.py`：精确预期和确定性路径失败不可被模型反转；未决 Case 最多进行一次跨 Case 模型分析；通过项会复测。
- `failure_attribution.py`：环境、Driver、Fixture、Evaluator 和证据缺失不会误授权修改 Skill。
- `orchestrator.py`：已有 baseline/candidate、dev/validation/holdout、硬回归和目标比较能力。
- `iteration_kernel.py`：已有轮次、Token/会话预算、最小收益、耐心值和最大轮次停止条件。
- `iteration_brain.py`：本地分析已拆为冻结证据、语义评分、跨 Case 归因和 Proposal 四阶段；每阶段使用严格 JSON、输入/输出 hash、可恢复 manifest 与追加式模型调用收据，模型失败不会覆盖既有证据。
- `trial_environment.py`：每轮首次 Trial 冻结 Profile 内容、仓库 revision/tree、Fixture、Case revision 集与运行参数；初测、无 Skill 基线和通过 Case 复验必须使用同一 contract hash，跨轮重新冻结。

### 3.2 V2 工程落地结果与边界

本轮已经补齐统一的 Evidence Validity、AttemptVerdict、CaseAggregate、CandidateComparison、Diagnosis Graph、Champion/Challenger、不可变 commit、通过项复验、硬回归保护、循环/预算/耐心值停止和拒绝候选可审计恢复。客户端直接展示上述 Read Model，不依赖页面重算。

未校准 EvalPack 可以执行探索 Trial 以补证据，但 `model_proposed` / `unobservable` Case 会从修改授权中确定性剔除，不能单独驱动 Skill 变更；只有带用户明确预期或可信 Oracle 的可执行 Case 才进入授权保护集。自动/自定义 Case 与语义路径逐项进入 append-only 事件。

仍作为 P1/P2 演进而非本地产品可用性的阻塞项：

1. 更丰富的 Fixture 合成、metamorphic/fault injection 和历史失败自动挖掘；
2. 开放语义 Oracle 的专家校准集与自动 Reference Run；
3. 风险自适应重复、统计置信区间和小规模 Pareto frontier；
4. sealed Holdout 默认策略与 Validation 反馈预算的更严格统一；
5. 文档、数据、运维等新场景的专用验证 Adapter。

## 4. 问题一：Case 与评测路径的生成标准

### 4.1 生成对象不是 Case 列表，而是 Test Obligation Graph

系统先把输入编译为「测试义务图」，再选择最小 Case 集覆盖高风险义务。测试义务来自：

- 用户目标、标准、非目标和显式 Case；
- Skill 的触发条件、能力、步骤、资源、工具、输入输出与约束；
- 运行环境能力、Fixture 和可观察状态；
- 历史失败、线上会话、已修复回归与同类型 Skill 模板；
- 当前操作模式：repair、tune、extend、discover、create。

义务类型至少包括：

| 家族 | 目的 |
|---|---|
| `capability_happy` | 核心能力正常完成 |
| `boundary_positive` | 应触发时能正确触发 |
| `boundary_negative` | 不应触发时不越权、不误用 |
| `branch_and_edge` | 分支、边界值、输入差异与长上下文 |
| `precondition_violation` | 前置条件不满足时正确阻止或提示 |
| `tool_failure_recovery` | 工具失败、超时、空结果后的恢复与降级 |
| `state_transition` | 环境状态变化符合目标，无额外副作用 |
| `idempotency_repeat` | 重复执行不会重复创建、破坏或漂移 |
| `metamorphic_robustness` | 等价输入变化不应改变核心结论 |
| `adversarial_safety` | 提示注入、越权、敏感信息和危险操作 |
| `regression` | 已稳定通过和已修复问题不退化 |
| `efficiency` | 工具次数、Token、耗时或无效步骤受控 |

系统默认保证正常与负向触发成对设计，避免只优化「会做」而造成「什么都做」。用户 Case 始终保留原始语义和来源，自动补充 Case 不能静默改写用户预期。

### 4.2 每个 Case 的最小契约

`aceval.case-contract/v2` 必须包含：

| 字段组 | 必要内容 |
|---|---|
| 追溯 | `case_id`、revision、来源、关联 requirement/capability/用户标准 |
| 刺激 | Prompt、输入资源、Fixture、初始状态、运行环境能力 |
| 结果 | 预期 Outcome、允许范围、禁止结果、副作用约束 |
| Oracle | 类型、可信等级、证据来源、是否已校准、Grader 绑定 |
| 路径 | 语义检查点、先后约束、允许替代、禁止动作、证据匹配器 |
| 执行 | split、重复次数、重置策略、超时、预计会话/Token 成本 |
| 风险 | severity、weight、泄漏风险、适用模式、保护级别 |

Case 生命周期：

```text
Draft → Executable → Calibrated → Frozen → Regression
  └──────────────→ Invalidated / Retired
```

- `Draft`：模型或规则提出，不能用于自动修改 Skill。
- `Executable`：Fixture、环境和观察面已就绪。
- `Calibrated`：根据 Oracle 类型满足对应的可信校准要求，而不是仅由生成模型自证正确。
- `Frozen`：Prompt、Fixture、Oracle、Grader、路径和哈希全部固定，可参与版本比较。
- `Regression`：能力长期稳定通过后升级为高保护 Case。
- `Invalidated`：Fixture、Oracle、Grader、路径或依赖环境发生实质变化，需要 fork 新 revision 并重新校准。
- `Retired`：目标已删除、分布已失效或被更高质量 Case 替代，仅保留历史追溯。

确定性 Oracle/Reference Run 可由系统自动完成校准；开放语义标准必须使用已校准 Judge 或由用户/领域专家确认。Frozen 对象不可原地编辑，高级编辑入口实际执行 `fork revision`；新 revision 通过校准前不替换旧版本，也不能沿用旧比较基线。`Regression` 的默认晋升门由版本化 Policy 定义，至少要求跨两个独立批次稳定通过且无环境相关失败，具体 `k` 随风险 Profile 调整。

Oracle 可信等级至少分为：`deterministic`（状态/测试/Schema 等硬断言）、`reference_derived`（已知成功解与可复现 Reference Run）、`human_confirmed`、`model_calibrated`、`model_proposed` 和 `unobservable`。前四类在各自校准记录有效时可以成为 Oracle-ready；`model_calibrated` 必须在带专家标签的校准集上达到 Policy 一致性阈值；后两类不能冻结，也不能驱动修改。

### 4.3 Case Gate 与 Suite Gate

Case 级硬门包括：

1. **Traceability**：每个断言可追溯到用户标准、Skill 契约或已确认风险。
2. **Solvability**：Reference Run 或专家检查证明任务可解；连续大量 0 分先检查 Case，而不是直接判定 Skill 无能力。
3. **Executability**：Fixture、工具、权限、分支、依赖和清理策略可运行。
4. **Oracle readiness**：硬断言有可信 Oracle；语义 Judge 有隔离 Rubric 和 `not_evaluable` 出口。
5. **Observability**：所需 Outcome、Artifact、Tool 和 Trace 证据可以采集且完整。
6. **Isolation**：每次 Trial 从干净状态开始，不共享会污染结果的缓存、文件和提交历史。
7. **Fairness**：Grader 不依赖 Prompt 未声明的隐藏格式或唯一实现路径。
8. **Anti-cheat**：通过必须真正达成目标，不能只输出声明、硬编码 Case 文本或利用测试漏洞。

Suite 级硬门包括：

1. **Balance**：正触发/负触发、正常/异常、能力/回归比例没有明显单边偏置。
2. **Coverage**：所有 critical obligation 至少有一个可执行且 Oracle-ready 的 Case。
3. **Independence**：并行或重复 Trial 不共享会导致相关失败的状态。
4. **Budget**：风险覆盖收益满足预算，低价值重复项被去重或降频。
5. **Split integrity**：dev、validation、regression、holdout 的来源和可见性符合隔离规则。

`Executable` Case 可以用于环境预跑和 Reference Run；只有 EvalPack 中的 `Frozen` Case，或在本任务 revision 内由用户明确预期冻结、并标记为 Oracle-ready 的 Case，才能授权 Skill 修改与进入保护集。未校准自动草稿可以显示探索分数，但不得独立授权修改。每个 Gate 输出稳定 reason code，客户端和状态机不解析自然语言决定资格。

### 4.4 评测路径标准

评测路径使用 `aceval.path-contract/v2` 的语义部分有序图，而不是完整固定序列：

- `required`：安全、权限、业务约束或 Skill 明确承诺的必经检查点；
- `alternative`：多条合法方案的等价分组，满足其一即可；
- `forbidden`：越权工具、错误文件、危险副作用或明确禁止行为；
- `recommended`：用于诊断与效率评分，不单独导致功能失败；
- `order_constraints`：只声明有业务意义的局部先后关系。

合法实现差异不能高于 Outcome，但安全和权限硬路径门也不能被“结果碰巧正确”覆盖。判定优先级固定为：

| 优先级 | 条件 | 结果 |
|---:|---|---|
| 0 | 证据、环境或 Grader 无效 | `not_evaluable`，不归因 Skill |
| 1 | 安全、权限、禁止副作用或强制业务步骤违规 | 硬失败，即使 Outcome 正确 |
| 2 | 最终环境状态/产物未达成 | 硬失败，即使文本声称成功 |
| 3 | 非安全 required/alternative 未满足 | 按 Profile 配置硬失败或部分分 |
| 4 | recommended、效率或表达不足 | 诊断/连续分，不单独否定 Outcome |

因此，如果 Agent 用不同合法方法达成同一结果，不应因工具序列不同失败；严格步骤遵循只用于 Skill 本身将该步骤定义为目标、政策或安全边界的场景。

### 4.5 生成与复用策略

- `input_contract_hash + capability_contract_hash + environment_contract + planner_version` 未变化时复用已冻结设计。实现文件树与 `skill_commit` 属于 Trial 运行上下文，不进入 Case 身份哈希。
- 仅实现方式变化：复用同一 Frozen Case 集，保证前后可比。
- 用户目标、标准或能力范围变化：生成新的 design revision，补充义务后重新冻结。
- 发现覆盖缺口：只生成缺口 Case，不重建全部 EvalPack。
- 历史失败转为回归 Case 前，必须清理生产隐私、重建稳定 Fixture 并校准 Oracle。
- Holdout 对优化模型不可见；自动生成 Case 不能伪装成 sealed Holdout。

## 5. 问题二：每条评测路径的评测维度

### 5.1 先做证据有效性门，再做质量评分

`evidence_validity` 是前置门，不是质量分。Session 绑定错误、日志缺失、环境未准备、工具不可用、Fixture 污染或 Grader 异常时，结果必须是 `not_evaluable`，不能计为 Skill 失败。

有效性按层级判断：Session/环境/Fixture 等全局证据失效会使整个 Attempt 无效；只缺少某一非硬维度所需证据时，仅该维度为 `not_evaluable`，其余维度仍保留，但需要该维度的晋升门被阻塞。Controller 按版本化重试上限尝试补抓或重跑；超过上限进入 `insufficient_evidence`，无效 Attempt 不进入质量分母，但其会话成本仍记入预算。

评测结果分三层，避免把单次运行和跨运行指标混为一谈：

```text
AttemptVerdict（一次 Session）
  → CaseAggregate（同一 Case 的多次 Attempt）
  → CandidateComparison（Champion / Challenger 或 with / without Skill）
```

证据有效后，每个 Session Attempt 生成 `aceval.attempt-verdict/v2`：

| 维度 | 核心问题 | 优先 Grader | 默认作用 |
|---|---|---|---|
| Outcome correctness | 最终目标和环境状态是否达成 | 状态、测试、Schema、Artifact 硬检查 | 硬门 + 连续分 |
| Procedure conformance | 必经、禁止、替代和局部顺序是否符合契约 | 语义 Trace matcher | 安全步骤为硬门，其余诊断分 |
| Tool/API correctness | 工具选择、参数、权限、返回值使用是否正确 | Tool event 与参数规则 | 可配置硬门 |
| Artifact/evidence quality | 结果是否有来源、定位、可验证产物和证据 | 静态检查 + 引用检查 | 质量分/硬门 |
| Safety & side effects | 是否越权、泄密、执行危险动作或产生额外变更 | Policy/Workspace diff | 硬门 |
| Robustness & recovery | 失败恢复、降级、幂等和状态一致性如何 | 故障注入 + 重复运行 | 质量分/关键 Case 硬门 |
| Relevance & communication | 输出是否准确、清晰、格式符合用户要求且不过度噪声 | 规则 + 隔离语义 Judge | 连续分 |
| Efficiency | Token、耗时、工具次数、无效步骤是否合理 | 运行统计 | 预算门 + 连续分 |

`aceval.case-aggregate/v2` 在同一 Case revision、运行上下文和候选上聚合 Reliability，包括 pass@1、pass@k、pass^k、波动与 flaky；`aceval.candidate-comparison/v2` 再计算 with/without Skill 或 Champion/Challenger 的 Incremental Value、paired delta 和回归。二者都不是单次 Attempt 的属性。

单个大模型不能一次给出所有维度的总分。每个开放语义维度使用独立 Rubric；硬事实先判，模型不能推翻；证据不足返回 `not_evaluable`。界面可以显示一个摘要分，但控制器必须保留完整向量、硬门和各 Grader 版本。

### 5.2 不同 Skill 类型使用 Dimension Profile

公共维度不等于所有 Skill 权重相同。系统根据能力图和验证环境选择 Profile：

- **代码评审**：缺陷召回、误报率、文件/行定位、严重级别、修复建议可行性、源码证据、规则遵循；测试仓库用 seeded finding 和无缺陷负样本成对覆盖。
- **D2C**：视觉差异、布局与响应式、交互状态、可访问性、构建/运行、代码结构和非目标区域回归。
- **在线 Markdown 文档编辑**：内容准确、结构与格式保留、目标范围、引用/链接、权限与副作用、幂等。
- **数据/运维 Skill**：查询或诊断正确性、时间范围、指标口径、工具参数、证据链、只读边界、失败恢复。

Profile 只定义维度与 Grader Adapter；Case 仍由目标义务生成，避免为每个 Skill 手写一个完全独立 EvalPack。

## 6. 问题三：跨路径诊断是否完全依赖模型

答案是 **不应该，也不需要**。模型能力决定语义分析上限，但系统必须先形成可复现的结构化诊断，再让模型补足无法规则化的部分。

### 6.1 系统先完成的八步诊断

1. **证据校验**：检查 Session、日志、Outcome、Fixture、Grader 和环境快照完整性。
2. **失败归因门**：先分类为 Skill、模型随机性、远程 Agent、工具/API、环境、Fixture、Oracle/Grader 或证据缺口。
3. **失败签名归一化**：形成 `dimension + requirement + path_step + tool + error_code + artifact + case_family` 签名。
4. **确定性聚类**：按相同硬失败、共享能力节点、共享步骤和相同受影响资源构建问题簇。
5. **影响图**：从问题簇映射到 Skill source refs、可编辑资源、稳定通过 Case 与潜在冲突。
6. **成对差异**：比较同一 Case 下 Champion/Challenger 或 without-Skill/with-Skill 的 Outcome、Trace 和成本差异。
7. **变更授权**：只有 Skill-attributed 且证据充分的问题簇才能产生 Skill 修改建议；环境问题不能靠改 Skill 掩盖。
8. **冲突检测**：任何建议都要计算预计改善 Case、可能退化 Case、互斥建议和修改范围重叠。

归因允许多标签、主因和置信度；无法确定时保持 `unresolved`。计划内故障注入属于 Case stimulus，Agent 未按契约恢复可归因于 Skill；非计划的真实基础设施故障属于环境问题，不得借同一个错误码混淆。

### 6.2 模型的受限职责

模型只承担：

- 为确定性问题簇提出可读的根因假设；
- 判断规则无法覆盖的语义质量；
- 从系统给出的候选资源与证据中选择最小修改范围；
- 生成一个或少量带预期收益/风险的候选补丁；
- 对高风险候选进行独立 Critique，但不能自行批准晋升。

模型输出必须符合 `aceval.diagnosis-graph/v2` 和 Proposal Schema，并明确区分：

- `FACT`：硬 Grader、日志、环境状态、Diff 等确定性事实；
- `INFERENCE`：模型基于事实提出的根因或收益假设；
- `HUMAN`：用户/专家确认的目标、标准或判定。

### 6.3 优化建议不是结论，而是实验假设

每个 Proposal 必须包含：

- 对应问题簇与证据；
- 预计改善的 Case/维度；
- 受保护的稳定通过 Case；
- 精确文件范围与修改策略；
- 可能冲突、回归风险和验证计划；
- 失败后是否继续该方向的判定条件。

系统先从能力来源图和资源清单推导可编辑候选文件，模型不得凭空指定仓库外文件。多文件补丁仍经过现有 `SkillTreeOptimizer` 的范围、语法、敏感信息、删除/重命名和 Git 原子发布保护。

## 7. 问题四：如何保证优化收敛

### 7.1 可保证与不可保证的边界

系统不能保证搜索到全局最优 Skill；可以保证：

- 未通过晋升门的候选不会覆盖当前最佳版本；
- 固定 Eval 版本内，已知关键回归不会被总分掩盖；
- 同一失败方向不会无限重复；
- 达到成功、平台期、循环、证据不足或预算上限时给出明确停止原因；
- Eval 版本变化时不伪造跨版本收益结论。

上述有限停止保证成立的前提是最大候选数、轮次、重试、Validation 反馈、会话等待和外部调用均有硬上限与超时；外部系统永久无响应时由超时转入 `insufficient_evidence` 或 `budget_exhausted`，不能无限等待。

### 7.2 Champion / Challenger 晋升规则

每轮至少保留一个不可变 Champion。Challenger 按以下顺序执行：

```text
本地静态/范围验证
  → 受影响 Dev Case
  → 完整 Dev Case
  → 稳定通过回归 Case
  → Frozen Validation 成对多次运行
  → 最终或阶段性 Holdout
```

只有同时满足以下条件才晋升：

1. 证据有效性门全部通过；
2. 安全、权限、关键 Outcome 等硬门全部通过；
3. 不存在 critical regression，稳定通过保护集达到配置阈值；
4. 目标维度的成对收益达到 `minimum_effect`，不能只依赖总分微小波动；
5. 关键 Case 的 `pass^k` 达到可靠性下限；
6. 结果的不确定性不跨越拒绝边界；样本不足时标记 `insufficient_evidence`，而不是宣布变好；
7. Token、会话、时延和变更范围不超过预算；
8. Holdout 存在时通过最终门，且优化模型未见其内容。

OpenAI 建议 LLM Judge 更适合做 pairwise、分类或按明确标准打分。因此开放语义维度的晋升优先做 Champion/Challenger 盲化成对比较，并交换显示顺序以降低位置偏差；规则化维度仍直接比较硬结果。

### 7.3 数据集分层

| Split | 优化器是否可见 | 用途 |
|---|---:|---|
| `dev` | 是 | 定位失败、生成建议、快速反馈 |
| `validation` | Patch 生成器不可见原始 Case/Oracle；控制器按反馈预算暴露聚合结果 | 候选晋升与成对比较 |
| `regression` | Case 可见但不可改写标准 | 保护稳定通过和已修复能力 |
| `holdout` | 否 | 最终泛化检查和防止过拟合 |

新能力先进入 capability suite；连续稳定通过后升级到 regression suite。Eval 饱和时应增加更难或更真实的能力 Case，而不是继续对 100% 的套件优化总分。

Split 可见性是角色 ACL：Evaluator/Controller 可读取其负责的冻结资产；语义 Judge 只获得当前维度所需的 Case、Rubric 和证据；Patch Generator 只能读取 dev 与系统授权的问题签名；客户端在任务结束前只展示 Holdout 数量、健康度和门状态，不展示 Prompt/Oracle。用户主动解封 Holdout 后可以审阅原文，但该 revision 永久失去 sealed 状态，后续优化必须换用新的 Holdout。Validation 对同一目标的反馈轮次和粒度由 Policy 限制，避免反复试探过拟合。

### 7.4 自适应重复与统计策略

- 首轮默认每 Case 一次；确定性硬失败不盲目重复。
- 首次通过的 Case 至少复测一次；不一致即标记 flaky。
- 临界、模型 Judge 分歧、基础设施偶发或高风险 Case 自适应增加 Trial。
- 关键生产路径使用 `pass^k`，探索性「多方案求一解」才使用 `pass@k`。
- 小样本阶段以成对逐 Case 差异、硬回归和最小实际收益为主；样本增大后再启用 paired bootstrap、符号检验或置信/可信区间。
- 任何统计结论都记录样本量、重复次数、环境、模型和不确定性，不能只显示一个百分比。

### 7.5 循环检测、方向剪枝与 Pareto 候选

系统维护 `hypothesis_signature + target_scope + semantic_diff + affected_cluster`：

- 同一签名连续失败，不再生成等价补丁；
- 某方向改善一个维度但反复破坏高优先级维度时剪枝；
- 允许短暂保留 2–3 个非支配候选，例如「质量更高但成本略高」与「成本更低但质量相当」；
- 仍然只有一个正式 Champion，Pareto 候选不能绕过硬门；
- 用户改变目标或权衡时，从保留候选重新选择，不必重复生成。

### 7.6 明确停止状态

`aceval.convergence-state/v2` 至少支持：

- `target_met`：目标、硬门、回归和稳定性达到要求；
- `plateau`：连续若干已验证候选低于最小实际收益；
- `cycle_detected`：重复同类失败方向；
- `budget_exhausted`：会话、Token、时间或轮次用尽；
- `no_skill_attributed_failure`：失败不授权修改 Skill；
- `insufficient_evidence`：Case、Oracle、日志或统计证据不足；
- `eval_saturated`：当前套件无优化信号，需要增加能力 Case；
- `objective_conflict`：用户目标或维度不可同时满足；
- `human_stopped`：用户主动停止。

## 8. 低 Token 策略

可信度升级不应显著放大 Token：

- Case 义务与路径优先由静态 Skill 图、模板和规则生成，只对未决语义做一次批量模型补全。
- Frozen Case、Oracle、路径和 Grader 按哈希复用；实现修改不重新生成设计。
- 先跑受影响 Case 和确定性 Grader，只有未决 Trial 才发送给语义 Judge。
- 日志完整落盘，模型只接收失败签名、关键证据窗口和可追溯引用。
- 跨 Case 聚类先由系统完成，模型一次处理问题簇，而不是逐 Case 重复总结。
- Challenger 分阶段淘汰，未过本地或 Dev 门不消耗 Validation/Holdout 会话。
- 高风险或临界结果才增加重复次数；确定性明显失败不做无意义复测。

## 9. 客户端任务详情的呈现契约

V2 客户端继续以「任务详情 + 进化轨迹」为主场，但必须把可信性和因果链作为一级信息，而不是隐藏在高级 JSON 中。

### 9.1 Eval 设计节点：为什么生成这些 Case

默认展示：

- 测试义务覆盖图：用户标准 / Skill 能力 → 义务 → Case；
- Case 来源：`USER`、`GENERATED`、`REUSED`、`PRODUCTION`；
- 家族、风险、split、Oracle 可信等级和质量门状态；
- 生成/复用原因、覆盖缺口、被预算排除的低优先级义务；
- `Draft → Executable → Calibrated → Frozen → Regression` 的实时状态，以及 Invalidated/Retired 原因。

用户必须能回答：「这个 Case 为什么存在、它在保护什么、结果凭什么可信？」

### 9.2 Case / 路径节点：预期与实际一一对应

- 左侧显示语义路径图：required、alternative、forbidden、recommended 与局部顺序；
- 运行后在同一图上叠加实际 Trace，显示通过、绕行、缺失和禁止步骤；
- 点击检查点直接跳转到对应 Session Log 的事件区间；
- Outcome 与路径分开显示，避免「结果正确但过程违规」或「过程完整但结果错误」被总分掩盖；
- 每个 Attempt 独立保存，重试不覆盖历史。

### 9.3 Trial Verdict：维度、证据和 Grader

- 顶部先显示证据有效性横幅；无效时不展示误导性的红色 Skill 失败分。
- 展示维度向量、硬门、连续分、Grader 类型/版本、置信或不确定状态。
- 使用 `FACT`、`INFERENCE`、`HUMAN` 标签区分证据性质。
- 显示 pass@1、pass^k、重复次数和 flaky，而不是只显示一次成功。

### 9.4 汇总诊断：系统事实与模型建议分层

每个问题簇卡片显示：

- 失败归因与授权状态；
- 受影响 Case、维度、路径步骤、工具、文件和日志证据；
- 确定性事实、模型根因假设和置信度；
- 与其他问题簇/建议的冲突；
- 预计改善范围、保护 Case、修改文件和验证计划。

用户确认的是「优化假设 + 修改范围 + 风险」，而不是一句泛化的“优化 Skill”。

### 9.5 收敛面板：为什么继续、晋升或停止

- 版本树明确标识 Champion、Challenger、Rejected 和 Pareto candidate；
- 每条边展示优化假设、目标问题簇、实际收益和拒绝原因；
- 同屏展示硬门、critical regressions、paired delta、pass^k、预算和 Holdout 状态；
- 路由说明是 `rerun_same_cases`、`regenerate_cases` 还是 `converged`，并给出证据；
- 停止时显示具体状态和下一步建议，不使用模糊的“模型认为已收敛”。

### 9.6 Eval 健康度独立展示

任务详情保留独立的 Eval Health 区域：

- 不可执行、Oracle 未校准、日志不完整、Grader 分歧和 Fixture 污染；
- 能力覆盖、正负平衡、回归保护和 Holdout 可用性；
- Case/Grader 最近修改者、版本和 Reference Run；
- 需要用户确认或专家校准的项目。

这一区域用于防止用户把「评测坏了」误解成「Skill 变差了」。

## 10. 数据契约与实时事件

### 10.1 新增或升级契约

- `aceval.test-obligation-graph/v2`
- `aceval.case-contract/v2`
- `aceval.path-contract/v2`
- `aceval.attempt-verdict/v2`
- `aceval.case-aggregate/v2`
- `aceval.diagnosis-graph/v2`
- `aceval.proposal/v2`
- `aceval.candidate-comparison/v2`
- `aceval.convergence-state/v2`

设计对象携带 `task_revision`、`eval_design_revision`、`subject_contract_hash`、自身内容哈希，不把具体 `skill_commit` 写入 Case 身份。Attempt、Aggregate、Comparison 和 Candidate 等运行对象另外携带 `skill_commit`、`environment_snapshot`、`model_profile`、工具/Grader 版本与 `run_context_hash`；任一比较关键上下文变化都会使旧 paired result 失去直接可比资格。

事件信封必须定义 `event_id`、`schema_version`、`task_id`、对象 id/revision、单调对象序号、发生时间和因果父事件。消费端按 `event_id` 幂等去重，按对象序号处理乱序，并通过版本化 Snapshot + 水位重放恢复；失败、取消、失效和重试同样使用正式事件，不能只写日志文本。

### 10.2 客户端实时事件

- `eval_design.source_extracted`
- `obligation.created`
- `obligation.coverage_updated`
- `case.drafted`
- `case.calibrated`
- `case.frozen`
- `path.checkpoint_emitted`
- `attempt.evidence_validated`
- `attempt.dimension_graded`
- `attempt.verdict_completed`
- `case.aggregate_updated`
- `candidate.comparison_updated`
- `diagnosis.signature_created`
- `diagnosis.cluster_updated`
- `proposal.ready`
- `candidate.validation_updated`
- `candidate.promoted`
- `candidate.rejected`
- `convergence.updated`

事件只追加，客户端从事件重建动态路径；刷新或断线后不丢失正在生成的节点。

## 11. 实施状态与演进优先级

### P0：先让 Kernel 的判断可信

状态：本地产品主流程已落地；以下条目是已实现控制面的验收索引。

1. **P0.0 契约底座**：V2 JSON Schema、枚举、状态机、Policy、事件幂等/重放、V1 只读兼容和 Feature Flag。
2. **P0.1 Eval 资格**：Case/Suite Gate、完整生命周期、失效/fork、Reference Run 和运行资格。
3. **P0.2 证据底座**：Session Attempt、日志完整性、环境快照、Trace 规范化和检查点证据索引。
4. **P0.3 分层判定**：AttemptVerdict、CaseAggregate、CandidateComparison、Dimension Profile 和 Grader Matrix。
5. **P0.4 系统诊断**：Failure Signature、归因、Diagnosis Graph、影响图、冲突矩阵和 Skill 修改授权。
6. **P0.5 候选实验**：worktree 多文件候选、静态/范围检查、paired validation、稳定通过回归保护和失败回滚。
7. **P0.6 受控停止**：Champion/Challenger、最小实际收益、pass^k、反馈预算、Holdout、循环检测和停止状态。
8. **P0.7 客户端闭环**：稳定 Read Model、动态事件、证据跳转、用户确认和安全路由覆盖；页面不重算内核逻辑。

客户端壳层和只读轨迹可在 P0.0 后并行开发，但“线上可用闭环”必须等 P0.2–P0.6 完成。版本化 Policy 至少配置 critical 定义、`minimum_effect`、保护集阈值、每风险级别的 `k`、flaky 判定、重试/反馈/轮次上限、预算和 Holdout 是否必选；默认值通过首批基准校准，不散落在业务代码中。

### P1：提高 Case 自动化和泛化能力

1. Fixture 合成与 Reference Run 自动校准。
2. 种子 Case 扩展、metamorphic、fault injection、idempotency 与对抗样本生成。
3. 从失败会话挖掘、脱敏、去重并晋升回归 Case。
   P0 已为全部 Case 写入 `case_revision`、`content_hash`、`provenance`、`environment_applicability` 与 `reuse_key`，P1 在此基础上建设跨任务候选池与晋升策略，不改变当前任务内 EvalPack 精确复用能力。
4. 语义 Judge 校准集、专家一致性指标、pairwise 盲化与偏差检测。
5. 自适应重复与统计不确定性，保留小规模 Pareto frontier。

### P2：扩展类型与持续学习

1. Code Review、D2C、Markdown/文档、数据/运维等 Dimension Profile 与验证 Adapter。
2. 生产反馈和真实失败自动进入待校准池，监控 Eval 饱和与数据漂移。
3. 跨 Skill 复用 Case 模板、失败签名和修复知识，但保留目标/环境的适配校验。

## 12. P0 验收标准

P0 完成不能只以「代码路径跑通」验收，必须满足：

- 任意 Case 可解释其来源、覆盖义务、Oracle 和质量门；
- 任意 Trial 可区分证据无效、Outcome 失败、路径违规和语义质量不足；
- 任意优化建议可回溯到问题簇、Case、路径步骤和日志证据；
- 环境/Fixture/Grader 问题不会授权修改 Skill；
- 候选不能因总分上升掩盖关键回归；
- 同一 Eval revision 下可复现 Champion/Challenger 的晋升或拒绝理由；
- Eval revision 改变后系统明确重建比较基线；
- 达到成功、平台、循环、预算或证据不足时能稳定停止；
- 客户端无需读取内部目录即可还原完整任务轨迹和所有因果关系；
- 典型 20–50 Case 的初始套件可运行，并通过增量执行、缓存和证据裁剪控制 Token。

## 13. 决策记录

1. EvalPack 继续作为内部可复用资产和冻结载体，不成为用户必须理解或每轮重建的重型流程。
2. Case/路径可以自动生成，但自动生成结果必须经过可执行性与 Oracle 质量门，不能直接驱动 Skill 修改。
3. 路径遵循是正式评测维度，但采用语义检查点和合法替代，不采用默认唯一工具序列。
4. 跨 Case 分析不完全依赖模型；模型被限制在系统证据、失败签名、资源清单和 Schema 内。
5. 收敛采用 Champion/Challenger 与多目标硬门；总分用于摘要，不能单独决定晋升。
6. 客户端 V2 结构保持不变，任务详情新增 Case 依据、Trial 维度、诊断图、Eval Health 和收敛控制面。
