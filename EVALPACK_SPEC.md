# EvalPack v1 扩展规范

> 项目：Skill Doctor / Agent Capability EvalOps
> 目标版本：D20 `v1alpha1`
> 状态：Draft

## 1. 目标

`EvalPack` 是 EvalOps Kernel 的领域扩展单元。它回答“如何准备这类任务、观察什么、什么算正确、允许优化什么”，但不重新实现执行、状态机、预算、数据隔离或报告。

D20 必须交付：

- 一个不包含安全审查领域分支的 EvalOps Kernel v1；
- 一个完成完整自迭代闭环的 `security-review` EvalPack；
- 一个不修改 Kernel 即可接入的 `csv-summary-smoke` EvalPack；
- Pack 脚手架、静态校验和 conformance test。

这里的“可扩展”不是声称无需领域知识评测任意任务。一个任务至少要满足：

1. 输入能够被 Driver 放入隔离场景；
2. 输出、Trace、artifact 或 state 至少有一种可观测；
3. 用户能够提供或批准 Case、Oracle、断言或 rubric；
4. 若要自动优化，被测对象存在明确的可修改表面和 Patch policy。

缺少这些条件时，Kernel 返回 `not_evaluable`，不能让模型自行发明成功标准。

## 2. 三层边界

| 层 | 负责 | 不负责 |
|---|---|---|
| EvalOps Kernel | 状态机、预算、运行、Trace、CAS、Grader 调度、候选 lineage、Gate、Replay、报告 | 漏洞规则、CSV 字段、业务金标、任意领域分支 |
| EvalPack | Case、fixture、Oracle、Driver/Grader ID、rubric、Optimizer policy、报告标签 | 新状态机、模型供应商、数据库表、任意命令执行、报告实现 |
| Adapter/Extension | 新 Subject、Runtime、Driver 或 Grader 的代码实现 | 绕过 Kernel 预算、数据分层和 Gate 语义 |

依赖方向必须是：

```text
EvalPack -> public contracts <- Kernel
Trusted Extension -> public contracts

Kernel -X-> security-review
Kernel -X-> csv-summary-smoke
```

Kernel 不能 import 具体 Pack，也不能出现 `if pack == "security-review"`、`if vulnerability` 或 `if csv` 之类领域分支。

## 3. D20 支持层级

### 3.1 声明式 Pack

D20 正式支持只组合内置 Subject、Driver、Grader 和 Optimizer 的本地声明式 Pack。这是 20 天内承诺的低成本接入路径。

### 3.2 可信代码扩展

公共 Protocol 和 Registry 从 D1 定义，但 D20 不从 Pack Manifest 自动 import Python，不执行 Pack 自带 shell grader，也不建设插件市场。D21 起可由操作者通过显式 `--extension package.module:register` 加载可信扩展；该代码拥有宿主进程权限，必须单独审计。

### 3.3 不可信远程插件

插件签名、依赖隔离、远程执行和沙箱化 Marketplace 属于长期 Roadmap，不是 D20/D40 Core。

## 4. 目录结构

```text
evalpacks/<pack-name>/
  pack.yaml
  scenarios/
    dev.yaml
    validation.ref
    holdout.ref
  fixtures/
  oracles/
  schemas/
  rubrics/
  README.md
```

真实用户的 Subject 与 EvalPack 分离。示例仓库可以在 Pack 外额外保存一个缺陷 Subject，便于重现 Demo。

## 5. Pack Manifest

```yaml
api_version: aceval.dev/v1alpha1
kind: EvalPack

metadata:
  name: security-review
  version: 0.1.0
  description: Evaluate JSON-native repository security review skills.

subject_contract:
  kinds: [skill]
  adapter: skill_markdown_v1
  entrypoint: SKILL.md

driver:
  type: repository_workspace
  required_runtime_capabilities:
    - fresh_session
    - workspace_fixture
    - canonical_trace

suite:
  dev: scenarios/dev.yaml
  validation_ref: security-review-validation-v1
  holdout_ref: security-review-holdout-v1

graders:
  - id: output-schema
    type: json_schema
    hard: true
    params:
      schema_ref: schemas/review-output.schema.json
  - id: expected-records
    type: record_match
    hard: true
    params:
      collection_path: $.findings[*]
  - id: source-grounding
    type: source_reference
    hard: true
    params:
      file_path: $.findings[*].file
      line_path: $.findings[*].line
  - id: explanation
    type: llm_rubric
    hard: false
    params:
      rubric_ref: rubrics/explanation.yaml

optimizer_policy:
  adapter: skill_markdown_v1
  patchable_components: [skill_instruction]
  allowed_paths: [SKILL.md]
  visible_splits: [dev]
  beam_width: 2
  max_rounds: 2
  max_candidate_snapshots: 4
  max_added_lines: 30
  forbid_case_literals: true
```

Manifest 不保存模型、API key、凭证和价格。Runtime profile 和绝对预算由项目配置或命令行提供；Pack policy 只能收紧全局限制，不能放宽它。

## 6. Scenario 与 Oracle

```yaml
id: path-traversal-01
split: dev
prompt: Review HEAD against main and return the declared JSON schema.
fixtures:
  - fixtures/repo-01
oracle_ref: oracles/path-traversal-01.json
grader_ids:
  - output-schema
  - expected-records
  - source-grounding
timeout_seconds: 120
tags: [repository, security, path-traversal]
```

规则：

- Runtime 只能看到 prompt、允许的 fixture 和工具配置；
- Oracle、grader 参数、validation 和 holdout 不进入被测工作区；
- Optimizer 只接收 dev 的 Observation、Grade 和 Failure Card；
- validation 只返回晋级结果和聚合指标；
- holdout 只执行一次预注册批次，结果不能生成新候选；
- 所有路径必须相对 Pack 根目录、通过 traversal/symlink 校验并冻结 hash。

## 7. 公共 Protocol

```python
class EvalPackLoader(Protocol):
    def load(self, ref: PackRef) -> FrozenEvalPack: ...
    def validate(self, pack: FrozenEvalPack, registry: ComponentRegistry) -> PackReport: ...


class ComponentRegistry(Protocol):
    def subject_adapter(self, component_id: str) -> "SubjectAdapter": ...
    def driver(self, component_id: str) -> "ScenarioDriver": ...
    def grader(self, component_id: str) -> "Grader": ...
    def optimizer(self, component_id: str) -> "Optimizer": ...


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


class OptimizerPolicy(Protocol):
    def constrain(
        self, pack: FrozenEvalPack, global_budget: Budget
    ) -> PatchConstraints: ...


class Extension(Protocol):
    def register(self, registry: ComponentRegistry) -> None: ...
```

`ScenarioDriver` 只管理 fixture 生命周期和 Observation 收集：

- `prepare` 只复制声明过的输入；
- `collect` 必须在 `cleanup` 前把 Trace、artifact、pre/post state 写入 CAS；
- `cleanup` 必须幂等；
- Driver 不评分、不调用 Judge、不生成 Patch。

Kernel 固定调用顺序为 `SubjectAdapter.snapshot/materialize -> ScenarioDriver.prepare -> RuntimeAdapter.execute -> ScenarioDriver.collect -> ScenarioDriver.cleanup`。Runtime 内部可以管理 Session，但不能再实现一套 fixture/artifact prepare/collect 生命周期。

## 8. Registry 与内置组件

D20 使用显式 Registry，不使用 setuptools entry point、依赖注入容器或通用 DAG：

```python
registry.register_driver("repository_workspace", RepositoryWorkspaceDriver())
registry.register_driver("artifact_workspace", ArtifactWorkspaceDriver())
registry.register_grader("json_schema", JsonSchemaGrader())
registry.register_optimizer("skill_markdown_v1", SkillMarkdownOptimizer())
```

D20 内置两个 Driver：

- `repository_workspace`：准备仓库 fixture，收集文件变化与 Trace；
- `artifact_workspace`：准备普通文件输入，收集声明的输出 artifact。

D20 内置 Grader 控制在以下集合：

| Grader | 能力 |
|---|---|
| `json_schema` | JSON 可解析性、字段、类型和 Schema |
| `json_path` | 指定路径的存在、相等、范围和禁止值 |
| `record_match` | 对 JSON 数组做声明式 expected/forbidden record 匹配 |
| `artifact_exists` | 相对路径、MIME、大小和 hash 存在性 |
| `source_reference` | JSON 中引用的仓库文件和行号真实存在 |
| `trace_assert` | 工具包含、次数和有序子序列，不提供任意表达式 DSL |
| `workspace_diff` | 检查允许/禁止的文件变化 |
| `llm_rubric` | 结构化软评分，不能单独通过硬门禁 |

任意 Grader 的 `error` 都不能聚合成 pass。观察数据不足时返回：

```text
status: not_evaluable
missing: [artifact_snapshot]
```

## 9. 通用 Skill 优化器

D20 只内置 `skill_markdown_v1` Optimizer：它可以修改任意 Pack 所引用 Skill 的 `SKILL.md`，但不能修改脚本、资源、Case、Oracle、Grader、Runner 或预算。

Kernel 强制：

- candidate 包含 base hash、lineage id、round 和 Failure Card 引用；
- Pack policy 只允许收紧全局预算；
- Patch 通过路径、大小、Schema、关键约束和测试字面量泄漏扫描；
- old/new 比较只改变 Subject snapshot；
- validation/holdout 结果不能反馈给 Optimizer；
- 未声明 `optimizer_policy` 的 Pack 为 eval-only，系统不得擅自生成修改。

包含可执行脚本、二进制资源或外部 Agent 配置的自动优化需要新的受约束 Optimizer Adapter，不属于 D20。

## 10. Conformance Test

```bash
aceval pack init evalpacks/my-pack
aceval pack lint evalpacks/security-review
aceval pack test evalpacks/security-review --runtime fake
```

所有 D20 Pack 必须通过：

1. Manifest、Scenario、Oracle Schema 和引用校验；
2. 未知 API version、Driver、Grader 或 Subject kind fail closed；
3. `prepare -> execute -> collect -> cleanup` 可重复且 cleanup 幂等；
4. Subject 原文件不变，运行目录看不到 Oracle/validation/holdout；
5. 冻结 Observation Replay 得到一致的确定性 Grade；
6. `not_evaluable` 和 `error` 不会被聚合为 pass；
7. 越界路径、错误 base hash、超预算 Patch 被拒绝；
8. Optimizer 只能读取 dev 证据；
9. validation/holdout 的调用次数和信息返回符合协议；
10. FakeRuntime 和 GoldenOptimizer 能验证状态机，不依赖模型恰好生成特定文本。

## 11. D20 两个 Pack

### 11.1 `security-review`

| 项 | 范围 |
|---|---|
| Driver | `repository_workspace` |
| Case | 6 dev + 2 validation + 2 holdout；另有一个不计入优化数据层的 infra sentinel |
| Grader | Schema、record match、source reference、Trace、软 Judge |
| Optimizer | 2 lineages、2 dev-only rounds、最多 4 snapshots |
| 验收 | 过度修复候选被拒绝，精确候选通过一次 holdout，原 Skill 不覆盖 |

这是黑客松旗舰 Demo 和完整自迭代证据。

### 11.2 `csv-summary-smoke`

任务：读取销售 CSV，生成 `summary.json`；初版 Skill 会错误处理金额、空地区或分组汇总。

| 项 | 范围 |
|---|---|
| Driver | `artifact_workspace` |
| Case | 2 dev + 1 validation，无 holdout |
| Grader | `artifact_exists`、`json_schema`、`json_path`、`workspace_diff` |
| Optimizer | 复用 `skill_markdown_v1`，1 lineage、1 round、1 snapshot |
| 验收 | baseline 失败，候选修复 dev 且 validation 无硬回退 |

它不是第二个完整 Benchmark，也不进入现场主链。它只证明：在 Kernel API 冻结后，一个不同输入形态和 Driver 的 Skill 可以通过声明式 Pack 接入评测与最小自迭代，且不修改 Kernel。

## 12. 扩展成本验收

D15 在完整安全闭环通过后冻结 `EvalPack v1alpha1` 公共契约并记录 `kernel_contract_hash`。D16 才接入 CSV Pack，并记录：

```text
engineering_hours
pack_loc
test_data_loc
kernel_files_touched
kernel_loc_changed
new_dependencies
new_runtime_capabilities
time_to_first_graded_run
time_to_first_validated_candidate
conformance_pass_rate
```

D20 目标：

| 指标 | 目标 |
|---|---:|
| CSV Pack 接入期间 Kernel 文件改动 | 0 |
| 自定义 Python | 0 行 |
| 新 Runtime/依赖 | 0 |
| 从空 Pack 到首个可评分 Run | <= 4 工时 |
| 从空 Pack 到一次候选 validation | <= 8 工时 |
| Pack conformance | 100% |
| 同一 CLI、RunObservation、聚合和报告 | 100% 复用 |

以上是预注册目标，不是尚未测量就写入简历的结果。最终报告必须同时给出实际值和偏差原因。

预计扩展成本按类型分层：

| 类型 | 所需工作 | 目标成本 |
|---|---|---:|
| 内置 Driver + 内置 Grader | Manifest、Case、fixture、Oracle | 2–8 工时，另计领域金标准备 |
| 已有 Driver + 新 Grader | 可信 Grader 扩展、golden test、校准 | 2–5 天 |
| 新 fixture/state 生命周期 | 新 Driver、清理、Oracle、隔离测试 | 3–7 天 |
| 新 Runtime 或视觉/视频模态 | Adapter、环境、稳定性和 Judge 校准 | 1–2 周以上 |

如果接入 CSV Pack 必须修改 Orchestrator、状态机、存储协议或报告器，D20 的通用 Kernel 验收失败，不能用“可扩展架构”代替实际证据。

`csv-summary-smoke` 依赖 Runtime 能读取输入 fixture 并收集文件 artifact。D2 必须用真实公司 API 验证该能力；若不支持，应在公共 Schema 冻结前将第二 Pack 改为输出 final-message JSON 的 smoke 任务，并同步改名和验收，不能把缺失 artifact 记为通过。

## 13. D20 明确不做

- 任意 Python Pack 自动加载和不可信插件沙箱；
- 通用 DAG、插件市场、依赖解析和远程 Pack 安装；
- 自动从自然语言生成并直接信任 Oracle；
- XLSX、视觉渲染、外部 SaaS state Driver；
- Agent 配置自动优化；
- 一个总分适配所有任务。

用户可以提供少量 Case 和诉求，由系统生成 Manifest、断言和 rubric 草稿，但在进入 validation/holdout 前必须由用户确认成功标准。
