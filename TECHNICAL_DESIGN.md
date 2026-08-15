# Agent Capability EvalOps 初版技术方案

> 黑客松展示名：**Skill Doctor**
> 长期项目名：**Agent Capability EvalOps**
> CLI/包工作名：`aceval`
> 文档状态：Draft v0.1
> 制定日期：2026-08-11

## 1. 项目结论

项目采用“**模块化单体 + 显式状态机 + 可插拔适配器**”架构。

- 对外表现为一个能够评测、诊断和提出修复建议的 Eval Agent。
- 核心控制面是确定性工作流，不依赖多个 Agent 自由对话。
- 通用 `Subject`、`Runtime`、`EvalPack` 和 `Grader` 契约从 D1 建立；Kernel 不依赖安全审查领域代码。
- 20 天版本交付 Kernel v1、一个完整安全审查 Pack、一个强制 CSV smoke Pack 和可信的单 Runtime 闭环。
- 40 天版本通过 Benchmark、capability-gated Agent target、隔离执行、CI 和消融实验形成求职作品。

核心价值主张：

> 将“用户复制失败日志给 Agent、再把修改拿回去重测”的人工循环，变成可复现、可归因、受预算约束、经过隐藏集验证的评测与修复工作流。

## 2. 计划假设

- 1 名核心开发者，每天投入 4–6 小时。
- D1 代表正式开工日；若从 2026-08-12 开始，D20 为 2026-08-31，D40 为 2026-09-20。
- 公司允许使用至少一个支持无头执行的 Agent Runtime，以及一个模型 API。
- 20 天内只支持一种 Agent Skills 目录格式和一个真实 Runtime。
- 现场演示必须具备 Live、Replay、录屏三种降级方式。
- 如果每天只能投入 2–3 小时，优先删除 Web UI、第二 Runtime 和非核心自动化，不删除隐藏集、确定性评分和回归门禁。

## 3. 成功标准

### 3.1 D20 黑客松成功标准

1. `aceval pack lint/test` 能校验任何只引用已注册组件的声明式 EvalPack v1，未知版本和组件 fail closed。
2. 安全审查 Pack 完成基线、评分、归因、候选 Diff、validation、holdout 和报告的完整闭环。
3. CSV Summary Pack 在公共契约冻结后接入，使用不同 Driver，并完成 `2 dev + 1 validation` 的单候选闭环。
4. CSV Pack 接入期间 Kernel 文件和核心协议改动为 0，自定义 Python、新依赖和新 Runtime 均为 0。
5. 至少支持 Schema、JSONPath/record match、artifact、Trace、workspace diff 等通用确定性 Grader，以及一个软指标 LLM Judge。
6. 工作流不把 Oracle/holdout 内容传给 Runtime 或 Optimizer；D20 这是协议隔离，不宣称是恶意 Pack 安全隔离。
7. 原始 Skill 永不被自动覆盖；至少一个安全审查候选因回归被拒绝。
8. 两个 Pack 复用同一 CLI、RunObservation、Grader 聚合、候选状态机和报告器。
9. 报告包含逐 Case 证据、通过率、成本、Token、耗时和实际扩展成本。
10. 完整 Pitch 控制在 6 分钟左右，CSV 扩展性证据不超过 45 秒，现场模型失败时可切换 Replay。

### 3.2 D40 求职作品成功标准

1. 同一 Eval Core 支持 `SkillSubject`，并在 Runtime 可加载配置快照时支持 `AgentSubject`；否则明确注册为 `FixedAgentTarget`。
2. 支持在线执行和离线 Trace 导入两种模式；离线模式按导入数据完整度局部评分，不承诺任意 JSONL 都可完整重评分。
3. 至少一个真实 Runtime Adapter，并支持标准 Replay/Trace Import。
4. Benchmark 包含至少 2 个版本化 EvalPack、12–20 个分层 Case；20–30 个为扩展目标。
5. 关键 Case 重复执行至少 3 次，报告波动、成本和配对提升。
6. 有 20–30 个标注单元的 Judge pilot calibration；已知注入缺陷为诊断消融提供标签，独立归因金标集是扩展目标。
7. 至少完成“仅最终输出 vs Trace-aware”一组消融；第二组为扩展目标。
8. 支持受限 Worker、Evaluator Replay CI 和完整可复现文档；容器化是条件扩展。
9. Benchmark 基础设施执行成功率目标不低于 95%，新用户能在 15 分钟内跑通 Replay 示例。

## 4. 范围定义

### 4.1 D20 必须完成

- `SkillSubject`：解析、校验、快照和新旧版本对照。
- `EvalPackManifest`、Loader、版本/hash、显式 Component Registry 和 conformance tests。
- `repository_workspace`、`artifact_workspace` 两个 Driver，以及一组领域无关的内置 Grader。
- 一个真实 Runtime Adapter。
- YAML Case、fixture、dev/validation/holdout 分层。
- 干净工作区、超时、环境变量白名单和产物收集。
- Canonical Trace 和原始 Runtime Trace。
- 确定性 Grader、结构化 LLM Judge、评分聚合。
- 失败分类、证据引用和候选 `SKILL.md` Diff。
- Live Compare Profile 使用 1 个预生成候选；离线 Benchmark 使用 `beam_width = 2`、`max_rounds = 2`、`max_candidate_snapshots = 4`；两者都有金额/Token/时间预算和提前停止。
- 静态 HTML/Markdown 报告、Replay 和已完成 Run 的最小复用；完整 Cache/Crash Resume 延后。
- `security-review` 完整 Pack 与 `csv-summary-smoke` 最小自迭代 Pack；后者必须零 Kernel 改动接入。

### 4.2 D20 明确不做

- 多租户 SaaS、账号、RBAC、计费和分布式任务队列。
- 任意客户远程环境执行。
- 多个 Agent 自由讨论或投票式编排。
- 自动修改脚本、自动合并 PR 或自动发布生产 Skill。
- 多 Runtime、复杂前端、实时 Trace 大屏。
- 自动生成大量 Case 并将其视为真实评测标准。
- Pack Manifest 自动加载任意 Python/shell、插件市场、通用 DAG 和不可信扩展沙箱。
- XLSX、视觉渲染、外部 SaaS state Driver 和第二个完整 Benchmark。

### 4.3 D21–D40 扩展范围

- 审计并保持 D20 Kernel/EvalPack v1 兼容，在既有 `SubjectAdapter` 上新增 capability-gated `AgentSubject`/`FixedAgentTarget`。
- 增加显式可信 Extension Loader，使新 Driver/Grader 无需修改 Kernel；不自动执行 Pack 自带代码。
- 通用 Replay/Trace Import；第二 Runtime 为扩展目标。
- 离线 Trace Importer、受限 Worker 和确定性 Evaluator Replay CI；容器为条件扩展。
- Judge pilot calibration、重复实验和统计汇总。
- 12–20 Case 的公开 Benchmark、至少一组 Trace-aware 诊断消融、架构/安全/方法论文档。

## 5. 总体架构

```text
CLI / Static Report / GitHub Action
                  |
          EvalOps Kernel v1
  (Orchestrator / Budget / Gate / CAS)
                  |
      +-----------+-----------+
      |           |           |
 Subject      Runtime     Component Registry
 Adapter      Adapter     Driver / Grader / Optimizer
      |           |           |
      +-----------+-----------+
                  ^
             EvalPack Snapshot
  Scenario / Fixture / Oracle / Grader Spec
       Rubric / Gate Labels / Optimizer Policy
                  |
        RunObservation -> Grade -> Failure Card
                  |
       Dev -> Validation -> Holdout -> Report
```

### 5.1 架构原则

1. **工作流负责控制，模型负责语义。** 状态、预算、权限、停止条件和发布门禁由代码控制。
2. **核心不依赖 `SKILL.md`。** Skill 只是一个 Subject Adapter。
3. **确定性评分优先。** 能用 Schema、测试或文件检查解决的问题不交给 LLM Judge。
4. **保留原始证据。** 所有诊断和评分都必须能回指 Trace 或产物。
5. **候选不可污染评测系统。** 优化器不能修改 grader、Case、holdout、Runner 或预算策略。
6. **本地优先。** D40 前不建设云端多租户控制面。
7. **诚实降级。** 无法观测的指标标记为 `not_evaluable`，不能当作通过。
8. **依赖单向。** Pack 依赖公共契约；Kernel 不 import 具体 Pack，不包含领域名称分支。

### 5.2 Kernel 与 EvalPack

| Kernel 固定能力 | EvalPack 声明内容 |
|---|---|
| 状态机、预算、候选 lineage、validation/holdout 隔离 | Pack 名称、版本、兼容 Subject 和 Runtime capability |
| Runtime 执行、工作区、Trace、CAS、Replay | Scenario、fixture、私有 Oracle 和 rubric |
| Grader 调度、聚合、固定 Gate 语义 | 选择已注册 Grader 及参数 |
| Optimizer 调用、Patch 安全检查 | 是否支持优化、允许路径和更严格的预算 |
| SQLite/manifest、CLI 和报告器 | 报告标签和领域说明 |

D20 的低成本路径是声明式 Pack，只能引用内置组件。D21 起允许操作者显式加载经过审计的 Python Extension；Manifest 本身永远不能静默执行任意代码。完整契约、conformance 和扩展成本指标见 [EVALPACK_SPEC.md](./EVALPACK_SPEC.md)。

“支持任意 Skill”在这里指：只要输入可准备、结果可观察、成功标准可由用户提供或批准、修改表面可约束，就可以通过声明式 Pack 或可信 Extension 接入同一闭环；它不意味着 Kernel 能从未知任务中自动发明 Oracle。

## 6. 技术选型

| 层次 | 选择 | 原因 |
|---|---|---|
| 语言 | Python 3.12 | Agent SDK、数据处理、subprocess 和统计生态成熟 |
| 包管理 | `uv` | 安装快、锁文件清晰、便于干净环境复现 |
| CLI | Typer | 低成本生成类型化命令和帮助文档 |
| 数据模型 | Pydantic v2 | Schema、校验错误和 JSON 序列化完善 |
| 工作流 | Python 显式状态机 + `asyncio` | 可审计、易调试，20 天内不引入 LangGraph/Temporal 复杂度 |
| 元数据 | SQLite WAL | 本地部署足够，保留 Repository 接口便于未来切换 PostgreSQL |
| Trace | JSONL + 版本化事件 Schema | 流式写入、可回放、便于保留 Runtime 原始事件 |
| 产物存储 | 本地 content-addressed 目录 | 通过 hash 去重并保证版本可追踪 |
| 报告 | Jinja2 + 本地静态资源 | 比独立 React 后台更适合 20 天交付和离线演示 |
| API | D20/D40 以 CLI 为主；FastAPI 为 D40 后扩展 | 先完成可复现实验，再扩展 Web 和第三方集成 |
| 测试 | pytest + golden files | 适合数据协议、状态机和报告快照测试 |
| 隔离 | D20 受限 subprocess；D40 受限 Worker，Docker/Podman 为条件扩展 | 先确保闭环，再补强安全边界 |
| 可观测性 | 内部 Canonical Trace；OTLP 为 D40 后扩展 | 不将核心绑定到单一可观测平台 |

Runtime 首选应在 D1 根据公司环境确定。选择标准依次为：稳定的无头模式、机器可读事件、可隔离配置目录、能控制 Skill 安装、可获得用量和耗时。候选包括 Codex CLI、Claude Code 或公司的内部 Agent CLI。

## 7. 核心模块与接口

```python
class SubjectAdapter(Protocol):
    def validate(self, ref: SubjectRef) -> ValidationResult: ...
    def snapshot(self, ref: SubjectRef) -> SubjectSnapshot: ...
    def materialize(self, snapshot: SubjectSnapshot, target: Path) -> None: ...
    def diff(self, base: SubjectSnapshot, candidate: SubjectSnapshot) -> Patch: ...

class RuntimeAdapter(Protocol):
    def capabilities(self) -> RuntimeCapabilities: ...
    async def execute(
        self,
        subject: SubjectSnapshot,
        prepared: PreparedScenario,
        profile: RuntimeProfile,
        budget: RunBudget,
    ) -> RuntimeResult: ...

class EvalPackLoader(Protocol):
    def load(self, ref: PackRef) -> FrozenEvalPack: ...
    def validate(self, pack: FrozenEvalPack, registry: ComponentRegistry) -> PackReport: ...

class ScenarioDriver(Protocol):
    id: str
    def required_capabilities(self, scenario: FrozenScenario) -> set[str]: ...
    async def prepare(self, scenario: FrozenScenario, context: RunContext) -> PreparedScenario: ...
    async def collect(
        self, prepared: PreparedScenario, result: RuntimeResult
    ) -> RunObservation: ...
    async def cleanup(self, prepared: PreparedScenario) -> None: ...

class Grader(Protocol):
    id: str
    version: str
    async def evaluate(
        self,
        observation: RunObservation,
        oracle: FrozenOracle,
        params: dict,
    ) -> GradeResult: ...

class ComponentRegistry(Protocol):
    def subject_adapter(self, component_id: str) -> "SubjectAdapter": ...
    def driver(self, component_id: str) -> "ScenarioDriver": ...
    def grader(self, component_id: str) -> "Grader": ...
    def optimizer(self, component_id: str) -> "Optimizer": ...

class OptimizerPolicy(Protocol):
    def constrain(
        self, pack: FrozenEvalPack, global_budget: Budget
    ) -> PatchConstraints: ...

class Analyzer(Protocol):
    async def analyze(self, experiment: ExperimentResult) -> list[Diagnosis]: ...

class Optimizer(Protocol):
    async def propose(
        self,
        base: SubjectSnapshot,
        diagnoses: list[Diagnosis],
        constraints: PatchConstraints,
    ) -> list[CandidatePatch]: ...
```

### 7.1 模块职责

| 模块 | 职责 | D20 |
|---|---|---:|
| `contracts` | Pydantic 模型、枚举、Pack/Subject/Runtime/Grader 协议、Schema 版本 | 必须 |
| `packs` | Manifest Loader、hash、显式 Registry、Pack lint/conformance | 必须 |
| `subjects` | Skill 的校验、快照、安装、Diff；Agent 为 D21+ | Skill 必须 |
| `runtimes` | Runtime 命令、事件解析、能力声明 | 一个必须 |
| `runner` | 临时目录、超时、取消、环境过滤、产物收集；D20 串行优先 | 必须 |
| `traces` | Raw Event 到 Canonical Event 的标准化 | 必须 |
| `graders` | 内置硬断言、Trace 指标、LLM Judge、评分聚合 | 必须 |
| `analysis` | 失败分类、证据提取、可修复证据等级 | 必须 |
| `optimization` | 候选生成、路径白名单、复杂度限制 | 必须 |
| `orchestration` | 状态机、预算、重试、门禁和停止条件 | 必须 |
| `storage` | SQLite 元数据、Trace 和 artifact 索引 | 必须 |
| `reporting` | HTML/Markdown、Diff 和对照表 | 必须 |
| `api` | CLI pack/run/optimize/compare/replay、GitHub Action 和后续 FastAPI 接口 | CLI 必须 |

## 8. 核心数据模型

| 实体 | 关键字段 |
|---|---|
| `EvalPackVersion` | pack name/version/hash、manifest、component refs、policy |
| `SubjectVersion` | `kind`、`uri`、`content_hash`、`parent_hash`、`metadata` |
| `RuntimeProfile` | adapter、model、parameters、tool policy、environment hash |
| `Scenario` | prompt、fixtures、oracle ref、grader ids、split、timeout、tags |
| `FrozenScenario` | pack hash、Case/Oracle 的不可变快照、suite hash、fixture/CAS 引用 |
| `SuiteVersion` | suite hash、dev/validation/holdout 引用、版本信息 |
| `Run` | pack、subject、runtime、case、attempt、status、tokens、cost、duration |
| `RunObservation` | final output、Canonical Trace 引用、artifact/state CAS 引用、usage/error |
| `ImportedRunBundle` | Observation、可选 Case/Subject/Runtime 引用、completeness flags、源 Schema |
| `TraceEvent` | seq、type、timestamp、payload、tool、duration、error |
| `Artifact` | relative path、hash、MIME、size、producer |
| `GradeResult` | grader id/version、status (`pass/fail/not_evaluable/error`)、score、metrics、evidence refs |
| `Diagnosis` | category、blamed component、evidence refs、evidence level、optional calibrated confidence |
| `CandidatePatch` | base hash、unified diff、allowed paths、rationale |
| `CandidateSnapshot` | lineage id、snapshot/parent hash、round index、patch hash、dev result ref、freeze status |
| `Experiment` | pack hash、baseline、lineages、beam/round/snapshot budget、gates、decision、stop reason |

Canonical `TraceEvent.type` 至少支持：

```text
message
model_call
tool_call
tool_result
file_change
artifact
runtime_error
policy_violation
usage
```

系统不采集或依赖模型隐藏思维链。诊断只使用可观测消息、工具调用、结果、文件变化、错误和最终产物。

## 9. Case 与配置协议

### 9.1 项目配置示例

```yaml
version: 1
project: security-review-demo
mode: optimize

pack_ref: ./evalpacks/security-review

subject_ref:
  kind: skill
  uri: ./examples/subjects/security-review-skill

runtime:
  adapter: primary-cli
  model: company-approved-model
  repetitions: 1
  timeout_seconds: 180

budget:
  beam_width: 2
  max_rounds: 2
  max_candidate_snapshots: 4
  max_total_tokens: 200000
  max_cost_usd: 2.00
  max_wall_time_seconds: 7200

```

`pack_ref` 决定 Scenario、fixture、Oracle、Grader 和更严格的 Optimizer policy；`subject_ref` 只决定本次评测的被测快照。模型、凭证、价格和绝对预算属于运行配置，不写入 Pack。CLI 参数可以覆盖这两个 ref，但不能改变已冻结 Pack 的评分标准。

Live 不复用上述优化入口，而是只对两个冻结快照做一次显式比较：

```yaml
mode: compare
pack_ref: ./evalpacks/security-review
comparison:
  subject_ref: sha256:<baseline-subject-hash>
  candidate_ref: sha256:<frozen-candidate-hash>
  case_ids: [command-injection-anchor]
runtime:
  adapter: primary-cli
  model: company-approved-model
  repetitions: 1
  timeout_seconds: 180
```

### 9.2 Case 示例

```yaml
id: command-injection-001
split: dev
prompt: Review the provided server code and return findings as JSON.
fixtures:
  - fixtures/vulnerable_server.py
oracle_ref: oracles/command-injection-001.yaml
grader_ids:
  - output-schema
  - expected-records
  - severity-domain
  - explanation-quality
timeout_seconds: 120
tags: [security, tool-use]
```

对应 Oracle 仍使用领域数据，但通过通用 `record_match` 和 `json_path` Grader 解释：

```yaml
expected_records:
  - {rule_id: command-injection, file: vulnerable_server.py}
forbidden_records:
  - {rule_id: sql-injection}
json_paths:
  - path: $.findings[*].severity
    operator: values_in
    value: [low, medium, high, critical]
```

Grader 类型、参数和 rubric 在 Pack Manifest 中版本化；Scenario 只引用 Grader ID 和私有 Oracle。Kernel 不认识 `rule_id` 或 `findings`，也不把 Oracle 内容交给 Runtime。

`holdout` 不与 visible suite 放在同一文件中。D20 通过独立目录、独立工作区和 Orchestrator 接口避免将内容传给优化器，但这只是协议隔离：同一 OS 用户下的 subprocess 仍可能遍历宿主文件系统。D40 使用独立进程权限或 Worker 建立真正的访问边界。

三类数据的信息边界必须固定：

- Optimizer 只接收 dev Trace、逐 Case 评分和诊断证据；
- validation 只向候选选择器返回晋级结果和聚合指标，不把逐 Case 证据反馈给 Optimizer；
- holdout 仅对最终候选运行一次预注册评测批次，只返回最终门禁结果；批次内部可预先规定随机任务的重复次数。看到 validation 或 holdout 结果后继续修改，必须开启新的实验和数据集版本，不能继续声称原集合是未见数据。

第一个配置示例是 Benchmark Optimize Profile。现场 Compare Profile 使用赛前 dev 阶段已生成并冻结的候选，不用单次 baseline 失败声称“失败可重复”。D20 完整矩阵只运行一个预注册批次；只对目标失败和关键 Gate Case 做 2–3 次重复。全矩阵重复与统计区间留到 D40，现场通过 Replay 展示，不能期待在 6 分钟 Pitch 内实时完成。

| Profile | 数据 | beam/轮次/新 snapshot/重复 | 目标 Agent 执行量 | 用途 |
|---|---|---:|---:|---|
| Live Compare | 1 个锚点 Case | 0 / 0 / 0 / 1 | baseline/candidate 约 2 次 | 使用两个冻结 ref 展示配对执行；validation/holdout 使用 Replay |
| Benchmark | 6 dev + 2 validation + 2 holdout | 2 / 2 / 4 / 1 | 约 35–45 次 | 赛前一个预注册完整批次，关键 Case 另行重复 |
| Replay | 已冻结的真实 Run | 0 | 0 次 | 稳定展示完整证据链 |

实际调用量由 Orchestrator 在执行前计算并展示，超过金额、Token 或墙钟预算时拒绝启动。

## 10. 运行目录与可复现性

```text
.aceval/
  aceval.sqlite3
  objects/
    dev/<sha256>/...
    evaluator/<sha256>/...   # validation/holdout，不向 Optimizer 暴露
  runs/<run-id>/
    manifest.json
    subject/
    workspace/
    raw-trace.jsonl
    trace.jsonl
    artifacts/
    grades.json
    diagnoses.json
    candidates/
    experiment.json
    report.html
```

每次 Run 的 `manifest.json` 必须冻结：

- EvalPack manifest/version/hash、Subject hash 和 Suite hash；
- 每个 Scenario、fixture、Oracle、Schema、rubric、Gate 配置和 Grader 实现/prompt 的 hash；
- Runtime、模型、参数、工具权限和依赖版本；
- 尝试次数、随机参数、开始/结束时间；
- Token、估算费用、墙钟时间和停止原因；
- Raw Trace、Canonical Trace、产物 hash 和评分证据。

Runner 必须在 fixture cleanup 前将 Canonical Trace、artifact、pre/post state 和冻结的 Case/Oracle 写入 CAS。`RunObservation` 只引用不可变对象；Grader 读取 `RunObservation + FrozenScenario/Oracle`，不能依赖已删除的临时路径，也不能把 Agent 自报的 state diff 当作事实。

CAS 至少分为 optimizer-readable `dev` namespace 和 evaluator-only `validation/holdout` namespace。Hash 不是权限：Optimizer/被测进程只能拿到 Orchestrator 发放的 opaque object handle，不能枚举 evaluator CAS 或看到宿主路径。D20 仍只是同一 OS 用户下的协议隔离，必须披露可绕过；D40 受限 Worker 用独立进程身份、文件权限或 object broker 强制此边界，且不挂载 evaluator CAS。

### 10.1 Trace Import 与完整度

Importer 先将外部 Session 日志映射为 `ImportedRunBundle`，字段包括 `RunObservation`、可选的 Subject/Runtime/Scenario hash、artifact/state CAS 引用、源 Trace Schema 和 completeness flags。各 Grader 声明所需观察并逐项检查：

| 可用数据 | 可以评测 | 不可宣称 |
|---|---|---|
| 仅消息与工具序列 | 工具错误、顺序、延迟、Token、受限语义诊断 | 任务正确率、state diff、配对提升 |
| 加冻结 Case/Oracle | 输出断言、rubric；前提是所需证据齐全 | 缺失 artifact/state 的维度 |
| 加 artifact/state snapshot | 对应 artifact/state Grader | 未提供的外部副作用 |
| 完整 Subject/Runtime hash 和配对 Run | 受控制约束支持时的 old/new 比较 | 日志本身证明因果 |

缺少必需字段时该 Grader 返回 `not_evaluable` 和缺失项，不能默认通过。重新评分产生的新 `EvaluationResult` 记录实际 grader version/hash，不冒充原 Session 当时的评分。

## 11. 工作流状态机

```text
CREATED
  -> VALIDATING
  -> BASELINE_RUNNING
  -> BASELINE_GRADED
       |-- eval-only -----------------------------> READY_FOR_REVIEW
       `-- optimizer declared -> ANALYZING
           -> CANDIDATE_ROUND_STARTED(round=n)
           -> SNAPSHOTS_GENERATED
           -> DEV_SCREENING
           -> ROUND_DECIDED
                |-- budget remains --> CANDIDATE_ROUND_STARTED(round=n+1)
                `-- stop/final round -> CANDIDATE_POOL_FROZEN
                                         -> VALIDATION_RUNNING
                                         -> CANDIDATE_SELECTED
                                              |-- holdout declared -> HOLDOUT_RUNNING
                                              |                       -> READY_FOR_REVIEW
                                              `-- no holdout --------> READY_FOR_REVIEW
                                                                      -> APPROVED | REJECTED

任意状态 -> FAILED | BUDGET_EXHAUSTED | NO_IMPROVEMENT | UNSAFE_PATCH
```

状态转换和每个 `CandidateSnapshot` 写入 SQLite，并保证已完成结果可被幂等复用。是否进入优化和 holdout 由冻结 Pack 决定，但 Pack 不能新增状态或改写转换语义。每个 snapshot 不可变，第二轮以选定 parent hash 创建新对象。每条 lineage 在所有 dev-only 轮次结束后，从通过硬门禁的 snapshot 中选择 dev utility 最高者冻结；并列时依次选择累计 Diff 更小、绝对执行成本更低、round 更早者。选择只使用 dev 证据，validation 不得改变 lineage 或生成新 snapshot。

D20 采用 at-least-once 语义，不承诺外部模型调用 exactly-once：如果模型已计费但进程在结果落库前崩溃，该尝试标记为 `UNKNOWN`，再次运行前明确提示可能产生重复费用。

### 11.1 停止条件

- 达到最大轮数、beam、candidate snapshot、Token、费用或墙钟时间；
- 连续两轮没有达到最小有效提升；
- 所有候选出现硬指标回归；
- 可修复证据不完整或失败无法重复；
- 候选需要修改白名单以外的文件；
- validation 提升但 holdout 失败；
- Runtime 或环境故障占主导，修改 Skill 无法解决。

## 12. 评分体系

### 12.1 分层 Grader

D20 的 Kernel 只认识组件 ID、输入能力和 `GradeResult`，不认识“漏洞”“finding”或 CSV 字段。内置集合为：

| Grader | 通用能力 |
|---|---|
| `json_schema` | JSON 可解析性、字段与类型约束 |
| `json_path` | 路径存在、相等、范围和禁止值 |
| `record_match` | JSON 数组的 expected/forbidden record 匹配 |
| `artifact_exists` | 相对路径、MIME、大小与 hash |
| `source_reference` | 输出中的文件/行号引用是否存在 |
| `trace_assert` | 工具包含、次数和有序子序列 |
| `workspace_diff` | 允许与禁止的文件变化 |
| `llm_rubric` | 结构化语义软评分，不能单独通过硬门禁 |

人工评审处理争议结果和最终补丁审批。Pack 只能选择 Registry 中的组件并提供参数；未知 Grader、所需 Observation 缺失或执行错误分别返回加载失败、`not_evaluable` 或 `error`，均不能折算成通过。

LLM Judge 必须：

- 输出经过 Pydantic 校验的结构化 JSON；
- 对 candidate 身份匿名，比较时随机顺序；
- 为每个分数提供 Trace 或 artifact evidence reference；
- 固定并版本化 judge model、prompt 和参数；
- 与优化器使用独立上下文，D40 优先使用不同模型交叉校验。

### 12.2 候选门禁

```text
所有声明的 hard grader 通过
AND 相对 baseline 无 hard regression
AND validation 逐 Case 门禁通过
AND （样本量足够时）validation uplift >= 可选配置阈值
AND 绝对调用量/Token/金额/墙钟预算未超限
AND （Pack 声明 holdout 时）holdout 达到阈值
```

D20 的 validation/holdout 样本较少，使用 Pack 预注册的逐 Case 硬门禁，不用百分点 uplift。领域规则存在于 Pack 的 Oracle 和断言参数中，Kernel 只执行固定聚合语义。holdout 失败后状态为 `REJECTED`，不得把结果反馈给 Optimizer 继续修改同一实验；未声明 holdout 的 CSV smoke Pack 在 validation 后停止。

报告同时呈现：

- `task_pass_rate`
- `paired_uplift`
- `hard_regression_count`
- `hidden_regression_rate`
- `flake_rate`
- Token、费用、延迟
- D40 增加置信区间、`diagnosis_top1_accuracy`、`judge_human_agreement`

## 13. 失败分类与归因

```text
TRIGGER_MISS
FALSE_TRIGGER
INSTRUCTION_GAP
INSTRUCTION_AMBIGUITY
INSTRUCTION_CONFLICT
SCRIPT_OR_ASSET_ERROR
TOOL_FAILURE
PERMISSION_FAILURE
ENVIRONMENT_DRIFT
MODEL_VARIANCE
EVAL_SPEC_DEFECT
UNKNOWN
```

日志分析只能生成根因假设，不能直接证明因果。系统通过以下方式提高归因可信度：

1. 固定模型、工具和环境，只改变 Skill 版本；
2. 运行 with/without Skill 或 old/new Skill 配对对照；
3. 重复随机性较高的 Case；
4. 将环境/工具错误与任务失败分开计数；
5. 要求 Diagnosis 引用具体事件或产物；
6. D40 先用人工注入缺陷的已知标签完成 Trace-aware 诊断消融；独立人工根因金标集是扩展目标。

## 14. 候选优化策略

1. 基线运行后按失败类型聚类。
2. Analyzer 输出可验证的失败假设，而非直接要求“改进 Prompt”。
3. 生成前要求基础设施正常、证据齐全，且 baseline 失败可重复或有 without/with、历史版本对照支持；此时不要求尚不存在的 candidate 配对。
4. Live Compare Profile 使用 1 个预生成最小 Diff；完整 D20 实验使用 `beam_width = 2`、`max_rounds = 2`、`max_candidate_snapshots = 4`，snapshot 一经执行不可覆盖。
5. D20 只允许修改 `SKILL.md`，限制新增行数、文件数和补丁大小。
6. 静态检查通过后，在 dev Case 上进行低成本筛选；只有 dev 结果可用于第二轮生成。
7. 每条 lineage 最多冻结一个最终 snapshot，validation 最多比较两个候选；old/new 配对必须仅改变 Skill，结果只供 Orchestrator 选择，Optimizer 不接收并继续修改。
8. validation 无候选通过时实验结束；Pack 声明 holdout 时，仅最优候选进入一次不可反馈批次，失败即停止自动晋级；未声明时 validation 通过即进入人工评审。
9. 最终只输出候选快照、Diff、证据和推荐，不写回原始 Skill。

候选排序不能只看质量总分，还要惩罚成本、延迟和过度特化。D20 可采用简单的门禁后排序，D40 再引入置信区间和复杂度惩罚。

## 15. Runtime Adapter

最小协议：

```text
capabilities()
execute(subject_snapshot, prepared_scenario, runtime_profile, run_budget)
  -> RuntimeResult(final_output + raw event stream + output handles + usage/error)
cancel(run_id)
```

唯一固定生命周期是：

```text
SubjectAdapter.snapshot/materialize
  -> ScenarioDriver.prepare
  -> RuntimeAdapter.execute
  -> ScenarioDriver.collect
  -> ScenarioDriver.cleanup
```

Runtime Adapter 可以在 `execute` 内部创建/关闭 Session 和激活 Subject，但不复制 fixture、不选择 artifact、不读取 Oracle，也不评分。Driver 拥有场景工作区和 fixture/artifact 生命周期，将 `RuntimeResult` 收集为 `RunObservation`；两者不能各自实现一套 prepare/collect/cleanup。

能力声明至少包含：

- 是否支持无头执行；
- 是否能提供消息、工具调用、用量和文件事件；
- 是否支持并行、取消和固定模型参数；
- 是否能可靠安装/移除 Skill；
- 是否支持 trigger eval；
- 环境隔离等级。

`AgentSubject` 版本对照要求 Runtime 支持原子 `select_agent_config_snapshot`，或同时支持 `inject_system_prompt`、`select_model`、`inject_tool_policy`，并回报实际生效的配置 hash。`SubjectAdapter.materialize_agent_config` 不能替代 Runtime 加载能力。缺少这些能力时，Adapter 必须暴露 `fixed_agent_only`，系统只允许 Case 评测与诊断，不允许配置版本归因或 Agent 自动优化。

Adapter 同时保存原始事件和 Canonical Trace。Runtime 缺少某类事件时，对应指标标记为 `not_evaluable`。

## 16. 安全边界

### 16.1 D20 最低要求

- 每个 Case 使用新的临时工作区；
- Subject 快照只读，输出目录单独可写；
- 超时、输出大小、并发和子进程数限制；
- 环境变量白名单，不继承整个开发环境；
- 不向被测 Agent 暴露 grader、holdout 和其他候选目录；
- 校验 symlink、绝对路径和 path traversal；
- Patch 只允许修改 `SKILL.md`；
- Trace 脱敏常见密钥格式；
- 报告将 Trace 和 artifact 当作不可信内容转义。

D20 的 subprocess 隔离不是生产级沙箱，必须在文档和演示中明确这一限制。

### 16.2 D40 条件增强

- Docker/Podman 临时容器、只读根文件系统和临时可写目录；
- 默认禁网，按域名或代理显式放行；
- CPU、内存、PID 和磁盘配额；
- 不挂载宿主 Docker socket；
- 任务级短期凭证、日志脱敏和数据保留策略；
- 独立 holdout Worker，优化器进程无读取权限。

D40 Core 先完成可测试的受限 Worker，并用独立进程身份或 object broker 保护 evaluator-only CAS；若 D27 容器路径仍不稳定，继续使用权限收紧的 subprocess，把容器能力留在 Roadmap。

## 17. 推荐仓库结构

```text
agent-capability-evalops/
  pyproject.toml
  uv.lock
  README.md
  src/aceval/
    contracts/
      models.py
      protocols.py
      schemas.py
    kernel/
      orchestration/
      runner/
      traces/
      analysis/
      optimization/
      storage/
      reporting/
      packs.py
    builtins/
      subjects/
        skill.py
        agent.py            # D21+
      runtimes/
        primary_cli.py
        replay.py
      drivers/
        repository_workspace.py
        artifact_workspace.py
      graders/
      optimizers/
        skill_markdown.py
    cli.py
    api.py                  # D21+
  evalpacks/
    security-review/
    csv-summary-smoke/
  examples/
    subjects/
      security-review-skill/
      csv-summary-skill/
  extensions/              # D21+，由操作者显式加载的可信代码
  benchmarks/              # D34+
  tests/
    unit/
    integration/
    conformance/
    golden/
  reports/sample-run/
  docs/
    architecture.md
    evaluation-methodology.md
    security.md
    limitations.md
    adr/
```

## 18. CLI 设计

```bash
# 创建 Pack 脚手架
aceval pack init ./evalpacks/my-pack

# 零模型调用的静态校验与契约测试
aceval pack lint ./evalpacks/my-pack
aceval pack test ./evalpacks/my-pack --runtime fake

# 执行 with/without 或 old/new 基线实验
aceval run --pack ./evalpacks/security-review \
  --subject ./examples/subjects/security-review-skill --mode both

# 现场只比较两个已冻结 Subject，不调用 Optimizer
aceval compare --pack ./evalpacks/security-review \
  --subject sha256:<baseline> --candidate sha256:<candidate>

# 受预算约束地生成和验证候选
aceval optimize --pack ./evalpacks/security-review \
  --subject ./examples/subjects/security-review-skill --max-rounds 2

# 从已有 Run 生成报告
aceval report --run <run-id>

# 现场网络故障时从原始 Trace 回放
aceval replay --run <run-id>
```

D20 最重要的入口是：

```bash
aceval optimize --pack evalpacks/security-review \
  --subject examples/subjects/security-review-skill
```

一条命令应完成可见集执行、候选筛选、最终 holdout 门禁和报告生成。

## 19. 旗舰 Demo 设计

选择“**安全代码审查 Skill**”：

- 原 Skill 漏报路径穿越或命令注入；
- Agent 输出统一 JSON，便于确定性评分；
- dev 包含明确漏洞样例；
- validation 包含安全实现，拒绝“见到输入就报漏洞”的过度修复；
- holdout 使用不同语法和调用方式表达同类漏洞；
- LLM Judge 只评解释质量和修复建议，不负责判断漏洞是否存在。

推荐数据量：

- 6 个 dev Case；
- 2 个 validation Case；
- 2 个 holdout Case；
- 1 个注入工具超时的非 Skill 故障，用于展示系统拒绝错误修复。

完整矩阵用于赛前 Benchmark 和现场 Replay。现场 Live 只选择 1 个锚点 Case，运行 baseline/candidate 两次执行；validation、holdout 和其余真实历史结果通过 Replay 呈现，并在界面中明确标注为回放数据。

必须展示两类候选：

1. 过度宽泛候选：召回提高但误报正常代码，被 validation 拒绝；
2. 精确候选：修复漏报且不增加硬回归，进入 holdout。

主链结束后用 30–45 秒展示扩展性证据：运行 `csv-summary-smoke` 的 baseline、单候选 dev 修复和 validation 结果，再展示接入 Diff/扩展成本报告。评委应能直接看到它复用了同一 CLI、Runtime、状态机、Observation、Grader 聚合和报告器，且 `src/aceval/kernel/**` 改动为 0。CSV 不是第二个完整 Benchmark，也不挤占安全审查主叙事。

## 20. D1–D20 黑客松计划

| 天 | 工作 | 当日验收 |
|---:|---|---|
| D1 | 锁定 Kernel/EvalPack 边界、两个 Pack 和范围 | 3 个安全锚点 + 1 个 CSV 锚点；完成 Pack v1 草案 |
| D2 | 真实 Runtime 技术尖峰与 artifact kill gate | 干净 Session 能读 fixture、写出并收集 `summary.json`、返回机器可读事件 |
| D3 | 通用领域模型 | `EvalPackManifest/Scenario/Observation/Grade/CandidateSnapshot` 可序列化 |
| D4 | EvalPack Loader 和 Schema | 合法 Pack 可加载；未知版本、未知组件明确失败 |
| D5 | FakeRuntime 通用执行路径 | 任意声明式 Pack 可完成 prepare/run/collect |
| D6 | 接入真实 Runtime、Trace 和最小 hash store | 一个安全 Case 的 Trace、输出、用量可落盘 |
| D7 | 通用 Grader Registry（第一批） | 支持 Schema、JSONPath、record match、artifact 和 workspace diff |
| D8 | 安全所需通用 Grader 与 baseline | 补齐 source reference/Trace assert；3 个锚点确定性评分；Kernel 无安全领域分支 |
| D9 | 可选结构化 Judge 和聚合 | Judge 可关闭；Pack 未声明时不调用模型 |
| D10 | Failure Card 和单簇诊断 | 安全失败产生证据化、可修复假设 |
| D11 | 候选工作区、Patch、lineage/snapshot | 原 Skill 不变；候选快照不可覆盖 |
| D12 | 两轮 dev-only 优化和预算 | `beam=2/rounds=2/snapshots=4`、停止条件通过测试 |
| D13 | validation/holdout 与 Regression Gate | 过度候选被拒；最佳候选一次性进入 holdout |
| D14 | 通用报告、Replay 和 Live Compare | 预生成候选可 Compare；冻结 Run 可重建同一报告 |
| D15 | 完整安全闭环并冻结 Kernel/Pack API v1 | 6/2/2 与金标冻结；打 `kernel-contract-v1` 基线 |
| D16 | 仅用冻结 Pack API 接入 CSV smoke | 2 dev + 1 validation 完成单候选闭环；Kernel 目录零修改 |
| D17 | 最小结果复用和扩展成本报告 | 已完成 Run 可复用；CSV 实际工时和 Diff 可审计；不承诺完整 crash recovery |
| D18 | Pack conformance、E2E 和稳定性测试 | 两 Pack 均通过；锚点闭环连续 3 次无基础设施失败 |
| D19 | Pitch、架构图、录屏和回放 | 6 分钟脚本完成；CSV 展示不超过 45 秒 |
| D20 | 代码冻结、参赛包和能力矩阵 | Live、Replay、录屏、Pack 接入 Diff 证据齐全 |

预计总投入约 100–110 小时，按每天 4–6 小时接近单人 20 天上限；D10 后不得再增加组件种类或演示功能。

### 20.1 黑客松冻结点

- D2 未跑通 Runtime：立即切换到最熟悉的 CLI；若不能收集文件 artifact，在 D3 前把第二 Pack 改为 final-message JSON smoke，不伪装文件能力。
- D9 前 Judge 不稳定：降低语义评分权重，以确定性 Grader 保证 Demo。
- D15 冻结 Kernel/Pack API、安全 Suite 和功能范围；此后新 Pack 不得推动 Kernel 契约变更。
- D16 CSV 接入需要修改 Kernel：扩展性验收失败，优先修正 Pack/内置组件边界，不新增特例。
- D17 只做已完成 Run 复用，不补完整 Cache/Resume。
- D20 只修 P0，不做重构。

## 21. D21–D40 求职作品计划

| 天 | 工作 | 当日验收 |
|---:|---|---|
| D21 | Kernel v1 兼容性审计和 Pack Schema versioning | D20 两 Pack 在兼容测试中不变；发布 v1 兼容规则 |
| D22 | capability-gated `AgentSubject` | Runtime 可注入配置时，实际配置 hash 可验证 |
| D23 | `FixedAgentTarget` 与 Agent Adapter 边界 | 不可注入配置时只允许评测/诊断，不伪装成版本优化 |
| D24 | Subject/Runtime/Pack conformance | Skill、Agent/Fixed Agent 与两个 Pack 通过既有契约测试 |
| D25 | `ImportedRunBundle` 和 completeness | 完整、部分、缺失三种日志得到正确评分或 `not_evaluable` |
| D26 | 受限 Worker | 超时、只读目录、环境过滤和资源限制有测试 |
| D27 | 核心路径门禁 | Skill 在线、Agent/Fixed Agent、Import/Replay 的支持边界通过最小 E2E |
| D28 | 可信 Extension Loader 和 Benchmark 设计 | 仅显式 `--extension module:register` 加载；未知组件继续 fail closed |
| D29 | Benchmark 第一批 | 至少 12 个 Case，覆盖两个版本化 EvalPack |
| D30 | 冻结 12–20 Case Benchmark | Case、已知注入缺陷标签和版本冻结 |
| D31 | Judge pilot calibration | 20–30 个标注单元，输出 agreement、kappa、区间和失败样例，不设伪精确硬阈值 |
| D32 | 重复实验和统计聚合 | 关键 Case 至少三次，报告方差、成本和基础设施失败 |
| D33 | Trace-aware 诊断消融 | Output-only 与 Trace-aware 的已知缺陷识别效果可比较 |
| D34 | 安全和恢复测试 | 脱敏、路径逃逸、超时、原文件保护有测试 |
| D35 | Evaluator Replay GitHub Action/CI | PR 中只执行确定性回放并阻止评测框架硬回退 |
| D36 | 完整 Benchmark | 生成冻结报告和可复现命令 |
| D37 | 稳定性运行和 P0 修复 | 基础设施成功率目标 >= 95%，未达标则如实报告 |
| D38 | README、教程、ADR 和限制说明 | 新用户 15 分钟内跑通 Replay 示例 |
| D39 | 干净环境复现和演示视频 | 完整安装到报告流程成功 |
| D40 | Tag/Release 和作品包装 | Benchmark、视频、简历指标、Roadmap 完整 |

### 21.1 D40 优先级

必须：

- Agent target capability contract；Runtime 支持配置注入时启用 `AgentSubject`，否则使用 `FixedAgentTarget`；
- 带 completeness flags 和逐 Grader capability check 的 Replay/Trace Import；
- 12–20 Case Benchmark 和关键 Case 重复实验；
- Judge pilot calibration；
- 受限 Worker；
- Evaluator Replay CI；
- 一组 Trace-aware 诊断消融实验和完整文档。

应做：

- 3–5 Case XLSX 结构化产物 Grader；
- Docker/Podman Worker；
- 独立失败归因金标集；
- 第二个真实 Runtime 或标准 subprocess Adapter；
- 扩展到 20–30 个 Case；
- 第二组“LLM-only vs 混合 Grader”消融；
- 生成但需人工确认的 Case 草稿；
- 简单历史趋势页。

不做：

- 多租户云平台；
- 企业 SSO/RBAC；
- 分布式调度；
- 无审批自动发布；
- 覆盖所有 Agent 框架。

D27 若核心路径门禁失败，降级为通用 Subject conformance、`FixedAgentTarget` Case 评测、Trace Import 局部指标和 Replay 报告；同时取消 XLSX、独立归因金标集、容器化和在线 CI。Capability/completeness 矩阵必须出现在最终报告中。

D40 后 Roadmap 才包含 FastAPI、OTLP、带凭证和预算的 Subject Online Eval Gate，以及远程执行控制面。

## 22. Benchmark 设计

公开 Skill 热度、双轴分类、代表类别的输入输出契约和分类自迭代细节见 [SKILL_CATEGORY_AND_ITERATION_DESIGN.md](./SKILL_CATEGORY_AND_ITERATION_DESIGN.md)。本节保留跨类别的统一 Benchmark 原则。

### 22.1 任务类型

1. **结构化产物**：JSON、CSV、报告和代码文件，适合确定性评分。
2. **工具工作流**：工具选择、参数、顺序、失败恢复和权限处理。
3. **语义任务**：代码审查、规范遵循和策略判断。

### 22.2 故障类型

- description 不清导致未触发；
- 遗漏关键约束；
- 错误工具或参数指导；
- 缺少异常处理；
- 指令冲突或上下文噪声；
- 脚本/资源错误；
- 工具超时、权限不足等非 Skill 故障。

建议数据来源约为 70% 人工注入的已知缺陷和 30% 真实历史失败。人工缺陷用于建立根因金标，真实失败用于验证外部有效性。

### 22.3 对照与消融

```text
无 Skill
原始 Skill
仅最终答案的优化器
Trace-aware 优化器
人工修复版本
```

D40 必须完成 Output-only vs Trace-aware diagnosis。沿用 D20 的 Skill optimization 结果展示自动改进闭环。只有另行实现 Agent Optimizer Adapter、配置 Patch policy 和回归边界后，才可实验 Agent optimization；否则留在 Roadmap。LLM-only grading vs deterministic + LLM hybrid grading 也是有余量再做的实验。

## 23. 测试策略

### 23.1 单元测试

- Schema 校验和向后兼容；
- Pack Manifest/Scenario/Oracle 引用、hash 和路径逃逸；
- 未知 API version、Subject kind、Driver、Grader、Optimizer 必须 fail closed；
- hash、快照、Diff 和路径白名单；
- 状态机合法/非法转换；
- 预算、超时、停止条件；
- Trace 转换、脱敏和证据引用；
- Grader 聚合和门禁逻辑。

### 23.2 集成测试

- Fake Runtime 的成功、超时、工具失败和中断；
- 每个 Pack 通过 `lint` 和 FakeRuntime conformance；prepare/collect/cleanup 可重复且不泄漏 Oracle；
- Kernel 包禁止 import `evalpacks` 或出现安全审查/CSV 领域分支；删除任一 Pack 不影响另一个 Pack；
- Skill with/without 安装隔离；
- 候选目录不可访问 holdout；
- 已完成 Run 可复用；`UNKNOWN` 外部调用会提示可能重复计费，不承诺完整 crash resume；
- 报告可从冻结的 Run 完全重建。

### 23.3 Golden/E2E 测试

- 固定 Trace 的已知 Grade；
- 固定失败的已知 Diagnosis；
- security-review 从基线到 holdout 报告的完整 Demo；
- csv-summary-smoke 的 baseline、单候选和 validation E2E，接入时 Kernel Diff 为 0；
- D40 在受限 Worker 中运行最小 E2E；容器实现可用时再增加容器 E2E。

## 24. CI 策略

- **Evaluator Replay CI（D40 Core）**：每个 PR 执行零模型静态校验、单元测试、Schema 和确定性 Replay，只验证 Eval Core、Grader 和报告没有回归。
- **Subject Online Eval Gate（D40 后/条件能力）**：只有具备凭证、Runtime capability、隔离和预注册预算时，才对 Skill/Agent 新版本运行真实 Case；不能用 Replay CI 代替行为验证。
- 完整在线 Benchmark 和重复实验默认手工触发；只有团队批准成本与凭证管理后才进入定时任务。
- Replay Gate 可阻止确定性评分和协议回退；Subject 行为 Gate 才能阻止被测版本的硬指标回退。语义分数校准稳定前只作提示。

## 25. 黑客松演示脚本

1. **0:00–0:30 痛点**：展示人工复制日志和反复测试的现状。
2. **0:30–1:00 输入**：缺陷 Skill、visible Case 数量、不可见 holdout 数量。
3. **1:00–1:45 基线**：漏报、确定性断言和对应 Trace 证据。
4. **1:45–2:30 诊断与 Diff（切 Replay）**：回放赛前真实 Run 的证据归因和最小修改。
5. **2:30–3:30 候选筛选（Replay）**：回放过度修复候选因 validation 误报被拒绝的记录。
6. **3:30–4:20 Holdout（Replay）**：回放最优候选首次执行隐藏集的冻结记录及其硬门禁结果。
7. **4:20–5:10 报告**：质量、成本、耗时、证据和人工审批。
8. **5:10–5:45 扩展证据**：运行/回放 CSV smoke Pack，展示 Pack 接入 Diff、conformance 和零 Kernel 改动。
9. **5:45–6:00 延展**：同一 Kernel 可增加 capability-gated Agent target，并按完整度分析历史日志。

现场准备：

- 一个最小 Live Case；
- 一次完整 Run 的 `aceval replay`；
- 一段完整闭环录屏；
- 一份成功修复报告和一份“无法安全修复、主动停止”报告。

## 26. 风险与止损

| 风险 | 预警 | 应对 |
|---|---|---|
| Runtime/API 不稳定 | D2 仍不能稳定执行 | 立即更换到最熟悉 Runtime |
| LLM Judge 自我偏袒 | 修改后语义分总是上涨 | 硬断言优先、匿名比较、人工校准 |
| 少量 Case 过拟合 | dev 上升、validation 下降 | 分层数据集、最小 Diff、复杂度限制 |
| 成本/耗时失控 | 单轮超过预算或 10 分钟 | 逐级淘汰、复用已完成 Run、少候选、提前停止 |
| Trace 格式碎片化 | Adapter 充满特判 | Raw + Canonical 双轨、能力声明 |
| 修改范围失控 | 候选修改脚本或测试 | D20 只允许 `SKILL.md` |
| UI 拖累核心 | D15 仍未闭环 | 删除 Web，只保留静态报告 |
| 现场网络失败 | 彩排超时 | Live + Replay + 录屏 |
| 项目像 LLM 套壳 | 无客观结果和对照 | 强化配对实验、隐藏集、Benchmark、消融 |
| 为通用而过度抽象 | D10 前持续增加协议和组件系统 | D20 只做固定闭环、显式 Registry 和两个内置 Driver，不做 DAG/插件市场 |
| 新 Pack 迫使修改 Kernel | CSV 接入要改状态机、存储或报告 | 视为 D20 架构验收失败；回到 Pack/内置组件边界修正，不掩盖为“适配” |
| D40 范围膨胀 | D27 核心路径未全部通过 | 启用固定降级线，取消 XLSX、独立归因金标、容器和在线 CI |

## 27. 求职作品交付物

- 可安装的 CLI 和版本化 Release；
- 12–20 Case 的公开 Benchmark，20–30 Case 作为后续扩展；
- 一份 Benchmark 与消融实验报告；
- `docs/architecture.md`、`evaluation-methodology.md`、`security.md`、`limitations.md`；
- 30–60 秒 GIF、3 分钟产品视频、10 分钟技术视频；
- 可复现的 sample run、Trace、Diff 和 HTML 报告；
- 两个 Pack 的 conformance 结果与 `extension-cost-report.md`；
- GitHub Action 示例；
- 一篇 Case Study：背景、设计选择、失败、实验、局限和迁移到 Agent 的路径。

简历指标只使用 D34–D39 的真实实验结果，优先记录：

- `median_time_to_fix`；
- `fix_success_within_budget`；
- holdout uplift 和回归率；
- `diagnosis_top1_accuracy`；
- `judge_human_agreement`；
- 单次可接受修复成本；
- Runtime 执行成功率。

## 28. D1 必须确认的四个决策

1. 公司允许使用哪个无头 Agent Runtime，能否导出机器可读 Trace？
2. 模型/API、预算、网络和容器是否受黑客松规则限制？
3. 是否可以使用脱敏后的真实失败日志，还是完全使用公开 Demo 数据？
4. 最终演示时间和评审维度更偏业务价值、技术创新还是工程完成度？

这些问题不改变总体架构，但会决定第一个 Runtime Adapter、Demo 数据和现场执行策略。

## 29. 最终取舍原则

20 天版本负责让评委看懂并记住：

> Kernel v1 + 完整 security-review Pack + 零 Kernel 改动的 csv-summary-smoke；失败证据 -> 根因 -> 最小 Diff -> 回归拒绝/通过 -> 人工审批。

40 天版本负责让面试官相信：

> 这不是一个只会改 Prompt 的 Agent，而是一套有隔离、对照、统计、Benchmark、安全边界和可迁移接口的 Agent EvalOps 工程系统。
