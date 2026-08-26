# Skill Doctor 总体架构、技术方案与业界对标

> 项目：Agent Capability EvalOps / Skill Doctor  
> 基线日期：2026-08-21  
> 状态：目标架构与实施决策；同时标注当前实现边界

## 1. 执行结论

这件事**有必要，也有明确价值**，但前提不是再做一个通用 LLM Eval 平台，而是解决一个更窄、更完整的问题：

> 用户只提供 Skill、验收标准、可选 Case 和必要的业务环境，系统自动完成评测装配、真实执行、独立验证、失败归因、受控优化和回归门禁，最终交付可解释、可回滚的 Skill 候选。

当前项目已经证明了控制面的主要契约：EvalSuite 与 ExperimentPlan 解耦、无感 Evaluation Compiler、精确复用、复杂 Skill 测试规划、Failure Card/Patch Authorization、候选门禁、CATX 会话和日志接入。下一阶段的主矛盾已从“如何描述评测”转为：

1. 让线上 Agent 执行的**准确候选版本**可追踪；
2. 将执行产物按不可变标识交给本地验证环境；
3. 用统一环境协议支持代码仓库、浏览器、文档和远端状态；
4. 用真实实验而不是 FakeRuntime 证明优化收益。

建议采用“**线上执行 + 本地验证优先 + 未来云端同协议扩展**”的路线。第一条端到端样板选代码评审，第二条选 D2C；不要同时铺开所有热门 Skill。

项目的潜在优势是 Skill-first 的闭环和企业环境整合，不是 EvalPack 数量。现阶段优势仍是架构假设，只有在真实 Skill 上持续证明更高成功率、更低人工介入和可控回归后，才会成为产品优势。

## 2. 产品边界

### 2.1 系统要负责什么

- 从 Skill、标准、预期结果和可选 Case 推断内部评测画像；
- 精确复用冻结 EvalSuite，或从 Blueprint 生成最小草稿；
- 发现缺失的 Runtime、工具、凭证、数据和验证能力；
- 在指定 Agent 环境执行 baseline/candidate，采集完整 Trace、产物和 usage；
- 在独立验证环境验证可观察结果，而不是相信 Agent 自报成功；
- 区分 Skill、模型、工具、权限、环境、网络、评测器和证据缺口；
- 只有在证据允许时修改 Skill，并经过 validation/holdout 门禁；
- 产出候选、差异、评分、失败归因、环境指纹和可回滚依据。

### 2.2 系统不应负责什么

- 不把每个 Skill 都变成一个手写 EvalPack 项目；
- 不让 EvalPack 决定优化策略，也不允许优化器改评测标准；
- 不重做通用 Trace 数据库、容器编排器、浏览器引擎或像素比较算法；
- 不假设本地验证可以替代线上 Agent 执行；两者验证不同层面；
- 不在证据不足、鉴权失败或 Runtime 不支持时“优化”Skill；
- 不在生产数据和高副作用系统上直接进行开放式候选搜索。

## 3. 当前能力基线

| 能力面 | 状态 | 当前说明 |
|---|---|---|
| EvalOps Kernel | 已实现 | baseline、compare、repair/tune、dev/validation/holdout、预算与报告 |
| EvalSuite / ExperimentPlan 解耦 | 已实现 | 评测契约与优化策略独立版本、独立 hash，保留 legacy adapter |
| Evaluation Compiler | 基础已实现 | 自动画像、Cases 可选、精确 Suite 复用、模板复用、自定义入口、Runtime gap 阻断 |
| Test Intelligence | D20 已实现 | Capability Graph、Requirement、Case 草稿、Coverage 和 Freeze Gate |
| Failure Intelligence | D20 已实现 | Failure Card、证据等级、Patch Authorization、非 Skill 失败屏蔽 |
| CATX 连接器 | 已实现连接层 | 创建 Session、发消息、查状态、获取全量事件、SSE、ImportedRunBundle |
| Reference/Fake Runtime | 已实现 | Reference 仅 UTF-8 文件工具；Fake 只用于 conformance，不代表模型效果 |
| CATX Eval RuntimeAdapter | 未完成 | 缺 Scenario 批量执行、候选 Skill 版本绑定、重复采样和 Grader Replay 闭环 |
| 环境抽象与本地验证 | 未完成 | 缺 EnvironmentBlueprint、Provider、CandidateBundle、ValidationReceipt |
| Browser/D2C | 未完成 | 缺固定浏览器镜像、页面启动协议、DOM/交互/截图组合 Grader |
| 云端验证 Provider | 未完成 | 应在本地协议稳定后实现，不能先做第二套云端协议 |
| 真实优化收益 | 未证明 | FakeRuntime 只证明工作流；必须用真实线上 Agent 建基线 |

## 4. 目标总体架构

```text
用户输入：Skill + 验收标准 + 可选 Cases + 可选环境覆盖
                         |
                         v
+-------------------- Experience / Control Plane --------------------+
| Evaluation Compiler                                               |
|   -> EvaluationProfile -> Suite exact reuse / Blueprint compile   |
|   -> Environment requirements -> Preflight / missing capabilities |
|                                                                    |
| EvalSuite Registry       ExperimentPlan       Blueprint Registry   |
| cases/oracles/graders    goal/budget/patch    runtime/validator    |
|                                                                    |
|                     EvalOps Orchestrator                           |
+--------------------------+--------------------+--------------------+
                           |                    |
                    execution request    validation request
                           |                    |
             +-------------v------+   +---------v------------------+
             | Execution Plane    |   | Validation Plane           |
             | Reference Runtime  |   | local-docker (first)       |
             | CATX Runtime       |   | repository.verify/v1       |
             | future providers   |   | browser.visual/v1          |
             +-------------+------+   | future cloud provider      |
                           |          +---------+------------------+
                           +---------+----------+
                                     |
                    immutable CandidateBundle / artifacts
                                     |
             +-----------------------v------------------------------+
             | Evidence & Optimization Plane                        |
             | Canonical Trace / ImportedRunBundle / GradeResult    |
             | Failure Card -> Patch Authorization -> Optimizer     |
             | paired compare -> promotion gate -> report           |
             +------------------------------------------------------+
```

五个平面的职责：

1. **体验面**：普通用户只看到目标、标准、预期结果、可选 Case 和缺失前置条件；类型、Pack、Grader ID 默认隐藏。
2. **测试智能面**：把输入编译为评测画像、Requirement、Case/Oracle 草稿、EvalSuite 和环境要求。
3. **执行面**：让目标 Agent 使用冻结的 Skill 版本完成任务，负责行为真实性和完整 Trace。
4. **验证面**：在独立环境检查仓库、页面、文档或远端状态，负责结果真实性。
5. **优化与证据面**：归因、决定是否允许修改、生成候选、成对比较和晋级。

## 5. 核心契约

### 5.1 EvaluationIntent

面向用户的稳定输入，不暴露 EvalPack：

```json
{
  "api_version": "aceval.evaluation-intent/v1",
  "subject": {"uri": "./skills/frontend-code-reviewer"},
  "standards": ["识别阻断性缺陷", "finding 必须引用准确文件和行号"],
  "cases": [],
  "expected_results": [],
  "environment_override": null,
  "risk_policy": "safe-default"
}
```

编译结果是内部 `EvaluationProfile + EvalSuiteRef + ExperimentPlan + EnvironmentRequirements`。Cases 为空时可由确定性规则、Skill source refs、历史去敏 Session 和 Blueprint 生成草稿；语义 Oracle 不可信时停在 calibration，只询问会改变成功标准的问题。

### 5.2 EnvironmentBlueprint

环境是独立的一等资产，不属于 EvalPack，也不属于 Skill：

```json
{
  "api_version": "aceval.environment-blueprint/v1",
  "id": "repository.verify/v1",
  "capabilities": ["git", "process", "workspace_artifact", "canonical_trace"],
  "image": "registry/repository-verify@sha256:...",
  "setup": ["materialize_candidate"],
  "network": "none",
  "secrets": [],
  "limits": {"cpu": 2, "memory_mb": 4096, "timeout_seconds": 900},
  "health_checks": ["git --version"],
  "reset_policy": "per-scenario"
}
```

关键原则：

- Blueprint 声明需要什么；Provider 决定在本地 Docker、未来 Kubernetes 或其他云沙箱如何提供；
- 镜像固定到 digest，浏览器、字体、OS、依赖版本全部进入环境指纹；
- 每个 Scenario 使用干净 workspace；默认无网络；凭证只使用引用；
- 同一 Blueprint 在本地和云端产生同 Schema 的 Receipt；允许基础设施不同，不允许验证语义不同。

### 5.3 CandidateBundle

执行环境和验证环境之间绝不使用“拉最新分支”：

```json
{
  "api_version": "aceval.candidate-bundle/v1",
  "subject_hash": "sha256:...",
  "parent_subject_hash": "sha256:...",
  "repository": {"base_commit": "...", "result_commit": "..."},
  "artifact": {"uri": "file:///.../bundle.tar.zst", "sha256": "sha256:..."},
  "producer_run_id": "run_..."
}
```

本地第一阶段支持 `local_fs` 和 `git_commit` 两种 Transport。后者只接受明确 commit SHA，并验证 checkout 后 tree hash。未来可增加对象存储，但不改变 CandidateBundle；分支名只用于人类导航，不能作为验证身份。

### 5.4 ValidationRequest / ValidationReceipt

```text
ValidationRequest
  = suite_hash + scenario_id + candidate_bundle_hash
  + environment_blueprint_hash + grader_contract + limits

ValidationReceipt
  = request_hash + provider + environment_fingerprint
  + commands + exit/status + artifact_digests + measurements
  + infrastructure_errors + started_at/finished_at
```

Receipt 必须可重放、可审计，并区分 `candidate_failure` 与 `infrastructure_failure`。后者不能拒绝或授权 Skill 修改。

### 5.5 RunEnvelope

统一包裹 Reference、CATX 和未来执行器：

- subject/Skill 版本和 hash；
- agent/model/environment/profile 版本；
- Suite、Scenario、ExperimentPlan hash；
- Canonical Trace 和原始事件引用；
- 输入、最终回复、工具调用、usage、产物和完整性；
- CandidateBundle 引用和所有 Receipt。

## 6. 端到端流程

### 6.1 首次评测

```text
1. 用户选择 Skill，写验收标准；Cases 可选
2. Compiler 扫描 Skill，推断画像、风险和环境要求
3. 精确匹配冻结 Suite；否则复用 Blueprint 生成 draft
4. Preflight 检查 Agent、Skill 版本绑定、工具、凭证、镜像和验证器
5. 需要语义确认时只确认 Oracle/副作用；随后冻结 Suite
6. CATX/Reference Runtime 执行 baseline，保存完整 RunEnvelope
7. CandidateBundle 被内容寻址地传给本地 Validator
8. Grader 汇总输出、Trace、产物和状态证据
9. Failure Attributor 给出根因与修改授权
```

### 6.2 优化闭环

```text
dev 授权证据 -> Optimizer -> frozen candidate Skill
             -> 绑定候选 Skill 版本到执行 Agent
             -> 相同 Scenario / Runtime / Environment 执行
             -> 独立验证 -> 与 baseline 成对比较
             -> validation promotion -> one-shot sealed holdout
             -> 候选、报告和回滚信息
```

优化器只能读取 dev 的授权失败证据或 tuning measurement。它看不到 validation/holdout Oracle，不能修改 Suite、Blueprint、Grader 和环境。

### 6.3 执行与验证隔离

- **执行环境**回答“这个 Skill 在目标 Agent 上实际做了什么”；
- **验证环境**回答“它产生的结果是否满足标准”；
- **Artifact Transport**证明两边检查的是同一份结果；
- **Environment Fingerprint**证明比较时运行条件是否一致。

代码评审中，线上 Agent 读取测试仓库和 PR diff，本地验证器检查 findings 的行号、可达性、真实缺陷和误报；若 Skill 修改代码，则 checkout 精确结果 SHA 后运行测试。D2C 中，线上 Agent 生成代码，本地固定 Playwright 镜像启动该代码，再做构建、DOM、交互、断点和截图验证。

## 7. 内部评测画像与默认环境

普通用户不选择以下分类；分类是内部能力路由，不是产品入口。

| 画像 | 默认验证 Blueprint | 优先级 |
|---|---|---|
| 代码评审 | `repository.verify/v1` | 第一批 |
| 告警/日志诊断 | `observability.replay/v1` | 第一批 |
| SQL/BI 查询 | `query.sandbox/v1` | 第一批后段 |
| CSV/结构化文件 | `artifact.structured/v1` | 已有回归基线 |
| D2C/UI 还原 | `browser.visual/v1` | 第二条样板 |
| xlsx/docx/pptx | `office.artifact/v1` | 第二批 |
| 在线 Markdown/学城编辑 | `remote.document-state/v1` | 第二批后段 |
| 审批/TT/发布 | `workflow.transaction/v1` | 最后，必须测试租户/dry-run |
| SSO/鉴权依赖 | `auth.contract/v1` | 横向依赖，不按内容 Skill 处理 |

完整 Suite 仅在标准、Cases、fixture、Oracle 和画像签名完全一致时复用；更多时候复用的是 Blueprint、Driver 和 Grader 组合。

## 8. 本地优先实现

### 8.1 为什么先本地验证

- 更容易准备浏览器、字体、仓库、Office renderer 和调试工具；
- 调试周期短，适合稳定 Contract 和环境指纹；
- 不依赖线上基础沙箱是否预装浏览器；
- 可以先证明评测语义，再把同一容器和协议搬到云端。

本地优先不等于本地执行全部流程：目标 Agent 的行为仍应在线上 CATX 环境发生，本地主要承接结果验证和确定性工具。

### 8.2 LocalEnvironmentProvider

第一版使用 Docker/Compose，并提供：

- `prepare(blueprint, candidate_bundle) -> EnvironmentInstance`；
- `healthcheck(instance) -> CapabilityReport`；
- `validate(request) -> ValidationReceipt`；
- `collect(instance) -> artifacts/logs`；
- `destroy(instance)`。

资源上限、网络策略、只读挂载、临时 workspace 和超时由 Provider 强制执行。当前不自研容器调度；可以借鉴或适配 Inspect 的 Provider 边界。

### 8.3 `repository.verify/v1`

- 初始化/物化专用测试仓库；
- 固定 base commit、patch/result commit 和依赖锁；
- Git diff、文件/行号解析、构建/测试命令白名单；
- finding schema、precision/recall、severity、source reachability、误报门禁；
- 每个 Case 对应一个缺陷族，第一版只选一种主要技术栈；
- baseline 与 candidate 在同一 image digest、同一 repository tree 上成对运行。

专用仓库内容不是产品资产，**缺陷族覆盖、稳定 Oracle、独立验证和防泄漏**才是资产。

### 8.4 `browser.visual/v1`

- 固定 Playwright、Chromium、OS、字体、viewport、DPR、locale、timezone；
- 无动画/动态数据稳定化，固定 mock API 和资源；
- build/start/healthcheck 协议；
- DOM/可访问性/关键样式/交互/多断点/截图组合评分；
- golden、actual、diff、trace、console/network errors 全量留存；
- 截图不能单独作为 hard oracle，功能和结构 hard gate 优先。

Playwright 官方明确提示截图会受 OS、版本、设置和硬件影响，因此 golden 和 actual 必须在同一固定环境生成。

## 9. 云端扩展

本地 Contract 稳定后增加 `CloudEnvironmentProvider`，不修改 Orchestrator、Suite 或 Grader：

```text
EnvironmentProvider
  ├── LocalDockerProvider
  └── CloudProvider
        ├── pull CandidateBundle by digest
        ├── launch pinned OCI image
        ├── enforce policy/limits/secrets
        └── return the same ValidationReceipt
```

云端准入条件：本地重复运行稳定；Blueprint/Receipt Schema 冻结；候选传输不依赖绝对路径；镜像、bundle、golden 均可按 digest 获取；本地/云 conformance 通过；成本、并发、清理和凭证隔离有强制策略。

## 10. 安全与可信度

- CATX/API/Supabase 凭证只从 Secret Provider 注入；禁止进入仓库、Pack、报告和 Trace 文本；
- 对已经出现在聊天、终端历史或文档中的长期密钥执行轮换；
- Candidate Skill、仓库代码和网页按不可信输入处理；默认无网络、非 root、资源限制；
- 远端写操作使用测试租户、最小权限、before/after 快照、幂等键和回滚；
- 自生成 Case/Oracle 标注 provenance；只有确定性事实、可信业务来源或用户确认可进入 hard gate；
- validation/holdout 不反馈给 Optimizer；sealed holdout 单次使用后轮换；
- promotion 绑定 subject、suite、experiment、runtime、environment 和 artifact hash；
- 环境故障、认证失败、工具故障和 evaluator 异常不得授权修改 Skill。

## 11. 业界能力对标

| 能力 | 代表方案 | 对本项目的启示 |
|---|---|---|
| Dataset/experiment/evaluator | [LangSmith](https://docs.langchain.com/langsmith/evaluation)、[Braintrust](https://www.braintrust.dev/docs/loop)、[Phoenix](https://arize.com/docs/phoenix/evaluation/evals) | 不重做通用实验台；优先兼容导入/导出或 OpenTelemetry |
| Agent Trace grading | [OpenAI agent evals](https://developers.openai.com/api/docs/guides/agent-evals)、[trace grading](https://developers.openai.com/api/docs/guides/trace-grading) | Canonical Trace 与 workflow-level Grader 是基础能力，不是单独壁垒 |
| 自动提示优化 | [Braintrust Loop](https://www.braintrust.dev/docs/loop)、[DSPy](https://dspy.ai/) | 自动优化已存在；必须覆盖完整 Skill、工具/环境失败和晋级门禁，而不只改 prompt |
| Eval sandbox/provider | [Inspect sandboxing](https://inspect.aisi.org.uk/sandboxing.html) | Task/Sample 绑定环境、Provider 扩展、本地 Docker 到 K8s/云是经过验证的架构 |
| 代码任务可复现验证 | [SWE-bench Docker harness](https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/docker_setup.md) | 仓库快照、容器、独立测试和资源管理比“PR 分支名”更重要 |
| Browser Agent benchmark | [WebArena](https://webarena.dev/og/)、[BrowserGym](https://github.com/ServiceNow/BrowserGym) | 浏览器能力需要可重置网站、功能性 Oracle 和统一 action/observation，不只是截图 |
| 视觉回归 | [Playwright visual comparisons](https://playwright.dev/docs/test-snapshots)、[Docker](https://playwright.dev/docs/docker) | 固定浏览器镜像和同环境生成 golden 是 D2C 的最低可信条件 |
| 云端 Agent evaluation | [Vertex AI](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/evaluate)、[AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations.html)、[Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/agents/evaluation) | 云厂商正补齐 Trace、rubric 和本地/云评测；“支持云”本身不是优势 |
| Skill 运行模型 | [Anthropic Agent Skills](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview) | Skill 包括指令、脚本、模板和资源；当前只优化 `SKILL.md` 的范围必须明确 |

OpenAI 当前官方文档显示旧 Evals 平台计划于 2026-10-31 只读、2026-11-30 下线，并引导新用户转向 Datasets。这说明供应商产品面会变化，本项目的核心 Contract 不应绑定某一家 Eval API。

### 11.1 对标矩阵

评分含义：强 = 产品核心能力；中 = 可实现或部分覆盖；弱 = 不是主要目标。矩阵基于公开官方资料，不代表对所有私有能力的穷举。

| 能力 | Eval/Observability 平台 | Benchmark/Sandbox | Prompt Optimizer | Skill Doctor 目标 |
|---|---:|---:|---:|---:|
| 数据集、实验比较、Trace UI | 强 | 中 | 中 | 中，优先集成 |
| 多步工具/Agent 评分 | 强 | 强 | 中 | 强 |
| 可复现任务环境 | 弱到中 | 强 | 弱 | 强 |
| 用户标准 -> 无感 Suite 装配 | 中 | 弱 | 中 | 强 |
| 完整 Skill 而非单 Prompt | 弱到中 | 中 | 弱 | 强，当前仅完成 `SKILL.md` |
| 非 Skill 故障归因与修改授权 | 中 | 弱到中 | 弱 | 强 |
| 自动修改 + validation/holdout | 中 | 弱 | 强于 prompt | 强 |
| 线上执行 + 独立本地验证 | 弱 | 中 | 弱 | 强 |
| 企业内部 Agent/工具/鉴权 | 依平台而定 | 弱 | 弱 | 强 |
| 高副作用安全晋级 | 中 | 中 | 弱 | 强 |

### 11.2 可形成差异化的部分

1. **Skill-first，而非 Eval-first**：用户目标是把 Skill 变好，Suite 是内部证据资产。
2. **Evaluation Compiler + Blueprint 复用**：复用环境/验证方法，必要时自动生成 Suite。
3. **Failure Card + Patch Authorization**：先证明问题属于 Skill，再允许优化。
4. **执行/验证分离但证据绑定**：目标 Agent 保真执行，独立环境验证结果。
5. **企业内部闭环**：CATX、SSO、Vault、学城、BI、监控等接入形成适配价值。
6. **长期数据飞轮**：失败模式、Blueprint、人工确认 Oracle 和接受结果提高后续装配和归因质量。

需要保持克制：前四点目前是设计和部分实现；第五点刚完成 CATX 连接层；第六点尚未形成规模，因此还不能称为壁垒。

## 12. 是否值得自研

### 12.1 值得自研

- Skill bundle 快照、候选协议和受限修改；
- Evaluation Compiler、Suite/Blueprint 精确复用和用户无感入口；
- Failure Attribution、Patch Authorization 和 promotion policy；
- CATX/内部工具 RuntimeAdapter 与企业权限语义；
- 执行结果到独立验证环境的证据链；
- 热门 Skill 的默认 Blueprint 和组织级最佳实践。

### 12.2 应优先复用

- Docker/OCI/Kubernetes 环境底座；
- Playwright 浏览器、Trace 和截图 diff；
- Git、测试框架、OOXML parser/renderer；
- OpenTelemetry/OpenInference Trace 标准；
- 通用 LLM-as-judge、统计库和实验可视化；
- 对象存储、Secret Manager、队列和容器调度。

### 12.3 不值得做的情形

如果组织只有少量简单 Prompt、没有统一 Skill 生命周期、没有真实工具/副作用且人工回归成本低，直接使用成熟 Eval 平台更经济。项目在以下至少两项成立时价值显著：Skill 数量和迭代频率高；环境/工具复杂且失败经常误归因；多团队重复搭建脚本；需要自动修复和安全回归；数据不能交给外部 SaaS；需要线上执行与私有验证结合。

根据已给出的 SkillHub 热度、Skill 类型跨度和 CATX 环境约束，当前场景满足多项条件，因此继续建设合理。

## 13. 待办优先级

### P0：证明一个真实、可重复的优化闭环

| 顺序 | 工作项 | 完成标准 |
|---:|---|---|
| 1 | 冻结环境与制品 Contract | EnvironmentBlueprint、CandidateBundle、ValidationRequest/Receipt、RunEnvelope v1 的 Schema、hash、错误分类和 conformance tests 完成 |
| 2 | 实现 LocalDockerProvider | 网络/资源/超时/清理可控；同 Case 重跑一致 |
| 3 | 实现 `repository.verify/v1` | 专用代码评审仓库、缺陷族、源码/测试 Grader；误报和行号可验证 |
| 4 | CATX RuntimeAdapter | Scenario -> session -> events -> RunEnvelope -> grader replay；支持批量、终态、超时和完整性 |
| 5 | 候选 Skill 版本绑定 | 每次运行可证明实际使用的 Skill hash，可回滚 |
| 6 | 端到端 code-review 实验 | 至少一个真实 Skill 有可复现提升，且 validation/holdout 无 hard regression |
| 7 | 安全与可运维基线 | 密钥不落盘/不入报告；预算、取消、清理和中断恢复可用 |

第 5 项是当前最大外部依赖和 go/no-go 点：如果 CATX 无法精确绑定候选 Skill，系统只能做 baseline 诊断，不能声称完成线上自动优化闭环。

### P1：第二种环境形态和评测可信度

1. `browser.visual/v1` 与固定 Playwright 镜像；
2. D2C 组合 Grader：build、DOM、交互、viewport、视觉、console/network；
3. 重复采样、置信区间、paired comparison 和 flake quarantine；
4. known-good/known-bad、mutation test 和 evaluator calibration；
5. Session mining、去敏 Case 候选和人工确认队列；
6. Blueprint Registry、缓存、环境预热和能力发现；
7. OpenTelemetry/OpenInference 导出，复用现有 Trace/实验平台。

### P2：高价值工作 Skill 与云端 Provider

1. `observability.replay/v1` 和告警/日志诊断；
2. `query.sandbox/v1` 和 BI/SQL 只读评测；
3. `office.artifact/v1`；
4. `remote.document-state/v1` 和在线 Markdown 测试空间；
5. CloudEnvironmentProvider，与本地 conformance；
6. 组织级 Suite/Blueprint 注册、权限和审计。

### P3：高副作用与平台化

1. 审批、TT、ONES、发布等测试租户和 transaction Blueprint；
2. dry-run、人工审批、幂等、补偿和回滚；
3. Web Console、自助校准、差异审阅和 promotion；
4. 多租户配额、队列、成本治理和 SLA；
5. 组织级质量看板和持续线上抽样评测。

不应优先：为每类热门 Skill 手写完整 Pack、先做大而全 Console、先建云调度平台、先做纯 LLM Judge 市场，或在候选版本无法绑定时扩大线上 Case 数量。

## 14. 可行度

| 范围 | 可行度 | 关键风险 |
|---|---|---|
| 代码评审本地验证 | 高 | Case 泄漏、行号漂移、多语言扩张过早 |
| CATX baseline 批量评测 | 高 | SSE 完整性、限流、Session 清理、usage 口径 |
| CATX candidate 自动闭环 | 中 | 候选 Skill 上传/绑定/回滚 API 尚未证明 |
| D2C 本地验证 | 中高 | 字体/渲染 flake、视觉 Oracle 主观、启动协议多样 |
| 日志诊断/BI 只读评测 | 中高 | 真实口径、时效数据、权限、工具返回波动 |
| 在线文档编辑 | 中 | 测试租户、权限、幂等、外部状态清理 |
| Office 文档 | 中 | 跨平台 renderer 差异、主观布局评分 |
| 高副作用工作流 | 中低 | 测试租户和回滚成本决定可行性 |
| 本地到云端迁移 | 中高 | 制品、网络、Secret、字体/GPU 的环境等价性 |

总体工程可行度为**中高**。不确定性集中在外部平台版本绑定、业务环境可测试性和 Oracle 质量，而不是 Python Orchestrator 本身。

## 15. 风险、反证与停止条件

主要风险：大量成本花在生成/调参评测器；环境/权限失败被写进 Skill；同一模型生成 Case、Oracle、候选并判分；使用最新分支或浮动镜像；为了代理分数损害真实成功率；覆盖面过早扩张；CATX 无候选版本 API；重复采样和多候选造成成本失控。

P0 完成后，若连续多个代表性 Skill 出现以下结果，应缩小平台化投入：

- 相对人工没有缩短 time-to-valid-candidate；
- Failure Attribution 不能减少误改；
- validation/holdout 候选接受率低；
- Suite/Blueprint 复用率低，每个 Skill 仍需大量定制；
- 线上 Agent 无法精确绑定候选版本；
- 基础设施成本长期高于人工回归收益。

## 16. 成功指标与阶段门

北极星指标：`verified_skill_improvement_rate`，即进入优化流程的 Skill 中，通过真实 validation/holdout、无 hard regression 且被用户接受的候选比例。

关键指标包括：time-to-first-valid-run、time-to-verified-candidate、hard-pass/回归率、candidate acceptance、Failure Attribution precision/unknown rate、非 Skill 故障阻止误改比例、用户补充语义次数、Blueprint/Suite 复用率、evaluator flake、单个成功候选的总成本。EvalPack、自动 Case 和 Trace 数量都不是成功指标。

阶段门：

- **Gate A：连接可信**——CATX RunEnvelope 完整，能证明 Skill 版本和终态；
- **Gate B：验证可信**——本地 Provider 对同一 bundle 重跑稳定，基础设施错误隔离；
- **Gate C：优化可信**——真实代码评审 Skill 候选通过 sealed holdout；
- **Gate D：跨画像**——D2C 使用同一环境/制品协议完成闭环；
- **Gate E：云等价**——本地/云 Provider conformance 和结果容差达标；
- **Gate F：平台价值**——多 Skill 数据证明人工介入、时间或质量持续改善。

## 17. 近期实施切片

按工作包而非日历承诺推进：

1. **WP1：Contract + Local Provider**——四个核心 Schema、hash、Provider 和 conformance；
2. **WP2：Code Review Fixture Lab**——单技术栈专用仓库、缺陷族与 `repository.verify/v1`；
3. **WP3：CATX Eval Adapter**——连接器升级为 RuntimeAdapter，补齐候选版本绑定探针；
4. **WP4：真实闭环**——至少 3 次重复 baseline/candidate，运行 validation/holdout，记录成本与人工介入；
5. **WP5：Browser Blueprint**——只有 Gate C 通过后再实现 D2C；
6. **WP6：Cloud Provider**——Repository/Browser 两种本地 Blueprint 稳定后再开始。

## 18. 最终判断

- **必要性：高**。Skill 数量多、环境复杂、人工中转和误归因成本真实存在。
- **用户价值：高但待实证**。最强价值是从标准到可信候选的省心和安全，不是自动生成 EvalPack。
- **业务价值：中高**。可降低重复评测工程、回归和定位成本，提高热门 Skill 的可信发布速度。
- **差异化：中等，具备变强路径**。当前来自架构组合和企业接入；未来来自 Blueprint/失败数据/Oracle/接受反馈飞轮。
- **可行度：中高**。代码评审与本地验证很可行，D2C 需稳定环境；高副作用流程受测试租户和回滚条件限制。
- **最大风险**：做成“优化 EvalPack 的系统”，或候选版本不可证明时声称线上闭环完成。

建议继续，但严格采用 P0 的单一真实样板和阶段门：先证明 Skill 能被真实、可重复、安全地优化，再扩大 Skill 类型和云端规模。
