# EvalPack `v1alpha1/v1alpha2` 扩展规范

> 项目：Skill Doctor / Agent Capability EvalOps
> API version：`aceval.dev/v1alpha1`（legacy repair）/ `aceval.dev/v1alpha2`
> 状态：MVP 已实现规范
> 基线日期：2026-08-18

## 1. 目标

`EvalPack` 是 EvalOps Kernel 的声明式领域扩展单元。它定义：

- 被测 Subject 的类型和入口；
- Case prompt、fixture、split 和 timeout；
- 私有 Oracle；
- 使用哪个已注册 Driver 和 Grader；
- Skill 自动优化时允许的修改面和候选预算。

EvalPack 不重新实现 Runtime、状态机、预算、门禁或报告，也不能从 Manifest 自动执行 Python/shell 代码。

这里的“通用”有明确前提：

1. 输入可由 Driver 准备；
2. final output、Trace、artifact 或 workspace state 至少有一种可观察；
3. 用户能提供或确认 Case 和成功标准；
4. 自动优化时存在可约束的修改表面。

不满足观察条件时，Grader 返回 `not_evaluable`；Kernel 不允许模型自行发明 Oracle。

MVP 的声明式通用性只覆盖已注册组件的能力交集。当前内置执行链路是 UTF-8 `SKILL.md`、可选 UTF-8 JSON `subject.json`、UTF-8 workspace 文件工具和现有 Grader；它不等价于任意 Skill 文件树、任意工具或任意输出模态。

## 2. 实现状态

### 2.1 Implemented（MVP）

- 本地声明式 Pack；
- JSON 文档，以及安装 `PyYAML` 后的 YAML 文档；
- 严格 Manifest/Scenario 字段校验；
- 相对路径、symlink 和 traversal 校验；
- Pack 全树 hash、Suite hash、Scenario/fixture/Oracle hash；
- 执行前 Pack 完整性重验；
- `schema_ref`/`rubric_ref` 资源在加载时解析并深冻结；每个 EvalRun 物化经逐文件 hash 校验的 `frozen-pack`；
- 显式进程内 Component Registry；
- `skill` Subject 与 `skill_markdown_v1` Adapter/Optimizer，仅处理 UTF-8 `SKILL.md` 和可选 UTF-8 JSON `subject.json`；
- 两个 workspace Driver；
- 七个确定性 Grader；
- dev/validation/holdout split；
- `auto | repair | tune` improvement policy 与单一 Primary Objective；
- Objective 来源：Grader score/metric、usage、duration、tool-call count；
- Pack Builder：`generic`、`csv-summary`、`security-review` 以及未知类型的 generic fallback；
- Complex Skill Planning：source-grounded Capability Graph、风险加权 Test Requirement、Case 草稿和 Runtime gap；
- Coverage：planned、Runtime-executable、Oracle-ready 和显式 observed requirement coverage；
- Pack 内 `design/` Sidecar 与 `metadata.test_design` 引用；
- Pack Quality Gate：Subject/Plan hash、cross-reference、critical coverage、Oracle trust、Runtime gap、generated holdout 和 family split leakage；
- `draft -> calibrating -> frozen` 生命周期、显式冻结和内容锁；
- Quality blocker 接入 `pack freeze --approve`；
- 公司 Agent Profile 与 Session Log 的独立导入契约；
- Imported Session 的离线 Failure Attribution；
- `pack lint` 与 FakeRuntime `pack test`；
- `security-review` 和 `csv-summary-smoke` 两个内置示例 Pack。

### 2.2 Planned（D40）

- model-assisted richer semantic analysis 和自动 fixture 变换；
- seed expansion、metamorphic Case、Session Case mining 和 Run-to-coverage 自动接线；
- 已知好坏样本区分能力、mutation calibration 和 evaluator flake；
- Imported Session -> EvalRun、Grader Replay 和公司在线 Runtime；
- 受控 argv-only Process Tool 与诊断探针；
- 操作者显式加载的可信 Python Extension；
- 外部 validation/holdout suite resolver；
- AgentSubject/FixedAgentTarget；
- 多文件 Skill bundle、二进制 Subject/artifact 工具和 shell/network/browser/multimodal Runtime capability；
- LLM Judge 与人工校准；
- 不可信插件隔离、签名或 Marketplace；
- 远程 Pack 安装和依赖解析。

当前已有 `plan`、`pack generate --plan`、`pack quality`、质量门禁冻结和 `session diagnose`，但还没有 `--extension`、公司 Runtime Adapter、Grader Replay、Process Tool、经人工标注校准的 LLM Judge 或 Web Console。Runtime 由宿主/CLI 选择，不写入 Pack。

## 3. 三层边界

| 层 | 负责 | 不负责 |
|---|---|---|
| EvalOps Kernel | 执行生命周期、预算、Grader 调度、候选门禁、报告 | 漏洞规则、CSV 字段、业务金标 |
| EvalPack | Case、fixture、Oracle、组件 ID、参数、优化 policy | 模型供应商、凭证、任意代码执行、报告模板 |
| 受信组件 | Subject/Runtime/Driver/Grader/Optimizer 的 Python 实现 | 绕过 Kernel 的预算、split 和 Gate 语义 |

依赖方向：

```text
EvalPack -> public contracts <- Kernel
                         ^
                explicitly built Registry

Kernel -X-> security-review domain logic
Kernel -X-> csv-summary domain logic
```

## 4. 目录结构

最小 Pack：

```text
evalpacks/<pack-name>/
  pack.yaml
  scenarios/
    dev.yaml
    validation.yaml       # 可选
    holdout.yaml          # 可选
  fixtures/               # 按 Case 需要提供
  oracles/                # 按 Case 需要提供
  schemas/                # 按 Grader 需要提供
  README.md               # 可选
```

`pack.yaml` 文件名固定。文件内容可以是 JSON；若使用 YAML 语法，运行环境必须安装可选依赖 `PyYAML`。当前仓库的两个内置 Pack 使用 JSON 语法，因此核心路径保持零第三方依赖。

Pack 根目录及其任意子项都不能是 symlink。Loader 会 hash Pack 内全部文件，而不只 hash Manifest 引用到的文件；加载后修改、增加或删除任何文件都会导致执行前完整性校验失败。

Loader 会把 Manifest 中的本地 `schema_ref` 解析为结构化资源、把 `rubric_ref` 读取为 UTF-8 文本，并注入不可变的 `FrozenEvalPack.resources`。每次 EvalRun 还会把加载时记录的全部 Pack 文件复制到 `<run-dir>/frozen-pack/`，复制前后逐文件校验 hash；Driver 只从该副本读取 fixture，Grader 只使用冻结资源或该副本中的受控路径。Runtime context 不获得 Pack 根路径。

Subject 必须位于 Pack 外部。示例仓库可以同时提供 baseline/candidate Subject，但它们不是 EvalPack 内容。

### 4.1 生成、校准与冻结

Pack Builder 生成的 Pack 必须在 `metadata.calibration_status` 中声明生命周期：

| 状态 | 允许 | 禁止 |
|---|---|---|
| `draft` | lint、查看/编辑 Case、Oracle、Grader、Objective | Skill optimize |
| `calibrating` | 运行校准、补充正反例、修订评测器 | Skill optimize |
| `frozen` | run、compare、repair/tune | 原地修改评测语义 |

`pack freeze --approve` 会先执行基础校准就绪检查；带 `metadata.test_design` 的 Pack 还必须通过 Pack Quality Gate。通过后命令只改变生命周期元数据并生成 `.aceval-pack-lock.json`，不会自动补 Oracle、改 Grader、推断新 Objective 或删除失败 Case。内容锁覆盖除自身外的全部 Pack 文件；冻结后任一文件变化都会使 Loader fail closed。

内置 legacy `v1alpha1` Pack 没有生命周期字段，为兼容已有资产按 grandfathered frozen 处理。新生成 Pack 不得利用该兼容路径。若需要改 frozen Pack，应创建新版本、重新冻结并从 baseline 重跑；不同 Pack hash 下的 uplift 不可直接续算。

EvalPack 可以迭代，但它是 Skill 实验外层的独立校准循环。单次实验禁止同时优化 Pack 与 Skill：

```text
Pack calibration loop -> frozen Pack hash -> Skill repair/tune loop
        ^                                      |
        +-------- new Pack version ------------+
```

### 4.2 复杂 Skill 测试设计 Sidecar（Implemented）

D20 保持 `v1alpha2` 顶层契约不变，把测试设计作为 Pack 内受内容锁保护的 sidecar：

```text
evalpacks/<pack-name>/
  design/
    capability-graph.json
    test-plan.json
    coverage-target.json
    generation-provenance.json
```

Manifest `metadata.test_design` 保存 Sidecar API version、相对引用和源 Subject hash；Scenario `metadata.aceval_test` 保存 requirement IDs、Case family、origin、Oracle trust、Runtime capability、可执行性和选择来源。`pack quality` 会校验引用留在 Pack 内、Sidecar Subject hash 一致、generation provenance 与 Test Plan hash 一致，以及 Capability -> Requirement -> Case 的 cross-reference。Sidecar 仍由 Pack 全树 hash 和 freeze lock 保护。

设计 Sidecar 的职责是解释 Case 从哪里来、覆盖什么以及有哪些缺口。Kernel 仍只按冻结 Scenario、fixture、Oracle 和 Grader 执行；Planner 不能在运行时改变 Gate。

冻结前当前会检查以下质量 blocker：

- critical Test Requirement 有 Case，或存在带原因的显式 waiver；
- active Case 和 critical Requirement 的 Runtime 可执行性；
- hard Oracle 不是未确认的 `model_proposed`；
- Case family 不跨 split 泄漏；
- Capability -> Requirement -> Case 引用一致，且 Case Oracle/Grader 可评；
- 不可观察和不可执行路径没有被计为已覆盖；
- 自动生成 Case 不能冒充 sealed holdout；
- 源 Subject hash、Test Plan hash 和设计 Sidecar provenance 一致。

Coverage 当前分为 planned、executable、oracle-ready 和 observed。`observed` 必须来自显式 Case/Requirement evidence；当前尚未在每次 EvalRun 后自动回写动态工具/状态覆盖。自然语言 Agent 路径不可穷举，因此不得把该报告描述为数学意义的“全路径覆盖”。自动生成且与 dev 同源的 Case 默认只能作为 dev/validation 草稿，不能作为独立 sealed holdout。

当前 `mutation_score` 仅作为可选 provenance 数值校验；系统尚未生成 mutant，也没有 known-good/known-bad、mutation detection 或 evaluator flake 校准。这些属于 D40 的高级 Pack Quality。

完整技术方案见 [COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md](./COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md)。

## 5. Manifest

### 5.1 完整示例

```yaml
api_version: aceval.dev/v1alpha2
kind: EvalPack

metadata:
  name: security-review
  version: 0.1.0
  description: Evaluate JSON security-review Skills.
  calibration_status: draft

subject_contract:
  kinds: [skill]
  adapter: skill_markdown_v1
  entrypoint: SKILL.md
  params: {}

driver:
  type: repository_workspace
  required_runtime_capabilities:
    - fresh_session
    - workspace_fixture
    - canonical_trace
  params: {}

suite:
  dev: scenarios/dev.yaml
  validation_ref: scenarios/validation.yaml
  holdout_ref: scenarios/holdout.yaml

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
      collection_path: $.findings
      expected_key: expected_records
      forbidden_key: forbidden_records
      match_fields: [rule_id, file, line]

optimizer_policy:
  adapter: skill_markdown_v1
  mode: auto
  goal: Keep correctness while reducing tokens.
  objective:
    id: token-efficiency
    source: {type: usage, key: total_tokens}
    direction: minimize
    aggregation: mean
    min_delta: 0
    max_case_regression: 0
  mode: auto
  goal: Keep findings correct while reducing tool calls.
  objective:
    id: tool-efficiency
    source:
      type: trace_count
      key: tool_call
    direction: minimize
    aggregation: mean
    min_delta: 0
    max_case_regression: 0
  patchable_components: [skill_instruction]
  allowed_paths: [SKILL.md]
  visible_splits: [dev]
  beam_width: 2
  max_rounds: 2
  max_candidate_snapshots: 4
  max_added_lines: 30
  forbid_case_literals: true
  params: {}
```

JSON 与 YAML 示例表达同一数据结构。Manifest 和 Scenario 的结构字段 fail closed；`metadata`、`params`、Oracle 等明确声明为自由 mapping 的位置允许领域数据。重复 Grader ID 会 fail closed。

### 5.2 字段

| 字段 | 当前约束 |
|---|---|
| `api_version` | `v1alpha1` 为 legacy repair；`v1alpha2` 支持 improvement fields |
| `kind` | 必须等于 `EvalPack` |
| `metadata.name` | 非空字符串 |
| `metadata.version` | 非空字符串 |
| `metadata.description` | 可选字符串 |
| `metadata.labels` | 可选 string-to-string mapping |
| `metadata.calibration_status` | Builder Pack 为 `draft | calibrating | frozen`；frozen 需要内容锁 |
| `subject_contract.kinds` | 当前 Registry 只支持 `skill` |
| `subject_contract.adapter` | 当前为 `skill_markdown_v1` |
| `subject_contract.entrypoint` | 当前示例为 `SKILL.md` |
| `subject_contract.params` | 传给 Subject Adapter 的 `snapshot/materialize`，包括候选与执行前后重验 |
| `driver.type` | 必须是已注册 Driver ID |
| `driver.required_runtime_capabilities` | Runtime 必须全部满足 |
| `driver.params` | 通过 Driver 专用 `RunContext.metadata.driver_params` 传给 `prepare`；不进入 Runtime context |
| `suite` | 声明 dev/validation/holdout 文件或 ref |
| `graders` | Manifest 级 Grader ID、类型、hard 和默认参数 |
| `optimizer_policy` | 可选；缺失时 Pack 为 eval-only |
| `optimizer_policy.mode` | `v1alpha2`：`auto | repair | tune`；`v1alpha1` 不允许 |
| `optimizer_policy.goal` | 用户自然语言目标，只用于候选语义上下文，不单独构成验收标准 |
| `optimizer_policy.objective` | Tune 的可测量主目标；显式 tune 必填，repair 禁止 |
| `optimizer_policy.params` | 通过 `PatchConstraints.metadata.optimizer_params` 传给 `candidate-patch-v1` Optimizer |

Manifest 不保存模型名、API key、凭证或价格。Runtime 和绝对实验预算由 CLI/宿主提供，Pack 只声明所需 capability 和更严格的优化约束。

### 5.3 参数传递边界

`params` 是 Pack 到受信组件的声明式配置，不是任意代码入口：

```text
subject_contract.params
  -> SubjectAdapter.snapshot(..., params)
  -> SubjectAdapter.materialize(..., params)
  -> 候选 snapshot、Gate 间重验、执行前后完整性重验

driver.params
  -> driver-only RunContext.metadata.driver_params
  -> ScenarioDriver.prepare(..., context)
  -X-> Runtime RunContext

optimizer_policy.params
  -> PatchConstraints.metadata.optimizer_params
  -> aceval.optimizer/candidate-patch-v1 Optimizer
```

当前 `skill_markdown_v1` Subject Adapter 和两个内置 workspace Driver 接受相应传递路径，但不消费自定义值；内置 Markdown improver 也不读取 `optimizer_policy.params`。自定义受信组件必须自行 fail closed 校验所支持的键和值。

### 5.4 Improvement mode 与 Objective

`auto` 先运行 baseline：存在确定性 hard FAIL 时进入 repair；全部 hard gate 通过时进入 tune。Repair 不接受自定义 Objective，只允许 hard pass rate 正向提升且无 hard regression。Tune 要求 baseline hard pass，并使用一个 Primary Objective：

```yaml
optimizer_policy:
  mode: tune
  goal: Keep output correct and reduce total tokens.
  objective:
    id: token-efficiency
    source:
      type: usage
      key: total_tokens
    direction: minimize
    aggregation: mean
    min_delta: 10
    target: 100
    max_case_regression: 0
```

MVP 支持的 `source.type`：

| type | 必需字段 | 示例 |
|---|---|---|
| `grader_score` | `grader_id` | 经校准的质量分 |
| `grader_metric` | `grader_id`, `key` | precision/coverage 等数值 metric |
| `usage` | `key` | `total_tokens`、`input_tokens`、`output_tokens`、`cost_usd` |
| `scenario` | `key=duration_seconds` | Case 耗时 |
| `trace_count` | `key=tool_call` | 工具调用数 |

任一 Case 缺值、值非有限、usage 为负数或 Grader 为 `ERROR/NOT_EVALUABLE` 时，Objective 不可评估；缺失值绝不按零处理。`min_delta=0` 表示“任意严格正向改善”，不允许 unchanged candidate 通过。Tune 候选必须同时满足：

1. 所有 hard gate 继续通过；
2. 相对 parent 真正改善，防止多轮搜索倒退；
3. 相对原 baseline 达到 `min_delta` 和可选 `target`；
4. 单 Case 回退不超过 `max_case_regression`；
5. validation 和 holdout 上继续成对通过同一 Objective gate。

自然语言 Goal 不是 Judge。Builder 只会为 Token、成本、工具调用和延迟等直接可测目标做保守推断；主观质量必须绑定经用户确认/校准的 Grader 或 Judge。

### 5.5 Suite 引用

`suite` 支持：

```yaml
suite:
  dev: scenarios/dev.yaml
  validation: scenarios/validation.yaml
  holdout: scenarios/holdout.yaml
```

也支持 `validation_ref`、`holdout_ref`。当前解析语义是：

- ref 指向 Pack 内存在的相对文件时，作为本地 suite 加载并纳入 hash；
- ref 是不对应本地文件的 opaque ID 时，会保留声明，但当前没有外部 resolver；
- CLI `pack lint` 和执行会将未解析 suite 报错，而不是跳过；
- 同一 split 不能同时声明本地字段和 `*_ref`。

内置 Pack 的 `validation_ref`/`holdout_ref` 都指向本地文件。这提供编排层面的可见性边界，但不是 OS 权限意义上的隐藏集。

## 6. Scenario 与 Oracle

Scenario 文件可以是单个 mapping、Scenario list，或包含 `scenarios` list 的 mapping。

```yaml
scenarios:
  - id: path-traversal-dev
    split: dev
    prompt: Review the provided Python file and return JSON.
    fixtures:
      - fixtures/dev/path_traversal.py
    oracle_ref: oracles/dev/path-traversal.json
    grader_ids:
      - output-schema
      - expected-records
      - source-grounding
      - file-inspection-trace
    grader_params: {}
    timeout_seconds: 30
    tags: [python, path-traversal]
    metadata: {}
```

支持的 Scenario 字段只有：

- `id`；
- `split`；
- `prompt`；
- `fixtures`；
- `oracle_ref`；
- `grader_ids`；
- `grader_params`；
- `timeout_seconds`；
- `tags`；
- `metadata`。

规则：

1. Scenario 的 `split` 必须与其 suite 文件所属 split 相同；
2. `fixtures` 当前是 Pack 根目录下的相对路径字符串列表；
3. `oracle_ref` 必须是 Pack 内普通文件；
4. `grader_ids` 必须引用 Manifest 中声明的逻辑 Grader ID；
5. `grader_params` 只能覆盖当前 Scenario 已选择的 Grader；
6. timeout 必须为正整数；
7. 路径不能为绝对路径、包含 NUL 或 `..`；
8. Scenario、Oracle 和 fixture 内容都会冻结 hash。
9. 每个 Scenario 必须至少选择一个 `hard: true` Grader；全软评分不能成为通过门禁。

Oracle 内容是领域数据，由 Grader 解释。例如安全审查 Pack 的 Oracle 包含 `expected_records`/`forbidden_records`，CSV Pack 的 Oracle 包含预期汇总值。Kernel 不认识这些领域字段。

### 6.1 FakeRuntime metadata

内置示例 Scenario 在 `metadata.fake_runtime` 中保存 baseline/candidate 的预注册行为。该字段只服务 FakeRuntime conformance 和模拟演示：

```yaml
metadata:
  fake_runtime:
    variants:
      baseline:
        final_output: {findings: []}
        trace:
          - kind: tool_call
            name: read_file
            payload: {path: sample.py}
      candidate:
        final_output: {findings: []}
        trace:
          - kind: tool_call
            name: read_file
            payload: {path: sample.py}
```

Reference Runtime 不使用这些预注册输出。报告只要使用 FakeRuntime 就必须标记 `simulated=true`。

## 7. 公共契约

当前核心 Protocol 的等价形态为：

```python
class SubjectAdapter(Protocol):
    id: str
    def snapshot(
        self,
        subject_ref: str,
        params: Optional[Mapping[str, Any]] = None,
    ) -> SubjectSnapshot: ...
    def materialize(
        self,
        snapshot: SubjectSnapshot,
        destination: Path,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Path: ...


class RuntimeAdapter(Protocol):
    id: str
    @property
    def capabilities(self) -> RuntimeCapabilities: ...
    async def execute(
        self,
        prepared: PreparedScenario,
        subject: SubjectSnapshot,
        context: RunContext,
    ) -> RuntimeResult: ...


class ScenarioDriver(Protocol):
    id: str
    def required_capabilities(self, scenario: FrozenScenario) -> FrozenSet[str]: ...
    async def prepare(
        self, scenario: FrozenScenario, context: RunContext
    ) -> PreparedScenario: ...
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
        params: Mapping[str, Any],
    ) -> GradeResult: ...


class Optimizer(Protocol):
    id: str
    proposal_contract: str
    async def propose(
        self,
        base: SubjectSnapshot,
        diagnoses: Sequence[Any],
        constraints: PatchConstraints,
    ) -> Sequence[CandidatePatch]: ...
```

Optimizer 的 `id` 用于匹配 `optimizer_policy.adapter`，`proposal_contract` 用于声明 `propose` 的输入输出形态。当前只接受：

- `aceval.optimizer/candidate-patch-v1`：公共 Optimizer Protocol，直接返回 `CandidatePatch` 序列；
- `aceval.optimizer/skill-markdown-improver-v2`：内置 repair/tune 契约，生成完整 UTF-8 `SKILL.md`，由 `SkillOptimizerBridge` 转为 `candidate-patch-v1`；
- `aceval.optimizer/skill-markdown-generator-v1`：legacy repair 兼容契约。

缺失、未知或不支持当前 improvement mode 的 `proposal_contract` 必须在生成候选前 fail closed。Markdown improver 是受限便利接口，不是多文件 Subject 的通用补丁协议。

MVP 的 `candidate-patch-v1` 不是任意文件补丁格式。Kernel 当前要求 parent/candidate 都提供完整文件快照和 UTF-8 文本 `content`，只允许 diff 触碰声明的单一 entrypoint，并从两份冻结内容重新计算 unified diff。base hash、patch hash、候选目录、真实 Subject hash、changed-files 集合或 diff 任一不一致，均作为可审计的 optimizer protocol error：记入 proposal/rejected/usage 后立即停止，不产生 trial。多文件或二进制 Candidate 需要 D40 新增显式候选验证扩展契约；仅注册 Subject Adapter 不会绕过这组检查。

唯一固定生命周期：

```text
SubjectAdapter.snapshot/materialize
  -> ScenarioDriver.prepare
  -> RuntimeAdapter.execute
  -> ScenarioDriver.collect
  -> ScenarioDriver.cleanup
  -> Grader.evaluate
```

Driver 管理 fixture、临时 workspace、artifact 和 pre/post state；Runtime 管理 Agent Session/loop；Grader 只读取 Observation 与 Oracle。三者不能互相复制职责。

## 8. Registry 与内置组件

MVP 使用 `build_builtin_registry()` 创建显式 Registry。Registry 不读取 entry point，也不根据 Manifest import 模块。

### 8.1 Subject Adapter

| ID | 能力 |
|---|---|
| `skill_markdown_v1` | 读取 UTF-8 `SKILL.md` 和可选 UTF-8 JSON `subject.json`，生成组合 hash，物化冻结副本 |

该 Adapter 会拒绝其冻结快照中的其他文件；`scripts/`、`templates/`、`assets/` 和其他多文件/二进制 Subject 需要新的受信 Subject Adapter，不能依靠修改 `entrypoint` 绕过。

### 8.2 Driver

| ID | 能力 |
|---|---|
| `repository_workspace` | 复制 repository/file fixture，收集完整 pre/post workspace 和 Trace |
| `artifact_workspace` | 在相同生命周期上额外要求并收集声明 artifact |

Driver 创建临时 workspace，复制声明的 fixture，拒绝 symlink 和越界路径，并对 workspace 与合并后的内存/runtime artifact 设置文件数、单文件大小和总字节限制。每个 Case 使用由安全 hash 命名的独立 fixture 目录；Scenario ID 本身也必须满足安全组件 ID 语法。`collect` 完成后 `cleanup` 删除 workspace；Observation 不依赖已删除路径。Pack 的 `driver.params` 只在 Driver 专用 `RunContext.metadata.driver_params` 中可见；两个内置 Driver 当前不读取这些自定义参数。

Driver 只得到不含 Oracle、grader IDs/params 的 `DriverScenarioView` 和 Case 专属 fixture root。`PreparedScenario`、`RuntimeResult`、`RunObservation`、Trace payload、Artifact metadata/content 与 `GradeResult` 的容器字段会在契约边界深冻结，防止 Runtime 篡改 baseline 或前序 Grader 改写后序 Grader 的证据。受信 Python Driver/Grader 仍属于同进程 TCB；该约束不能抵御主动绕过 Python 对象模型或直接扫描宿主文件系统的恶意组件。

### 8.3 Grader

| 类型 ID | 当前能力 |
|---|---|
| `json_schema` | final output 或 JSON artifact 的受限 JSON Schema 校验 |
| `json_path` | 有限 JSON path 取值和 equals/range/集合/regex 断言 |
| `record_match` | expected/forbidden record 匹配 |
| `artifact_exists` | artifact 路径、大小、MIME 和 hash 断言 |
| `source_reference` | 输出中的文件与行号引用检查 |
| `trace_assert` | 工具包含、次数和有序序列检查 |
| `workspace_diff` | 创建、修改、删除文件的 allow/forbid/require 检查 |

`json_schema` 是项目内置的受限实现，不声称支持 JSON Schema 全部关键字或格式。受支持的 `enum`、`const`、`uniqueItems`、JSONPath equals 和 record matching 使用严格 JSON 相等语义，boolean 不与 number 混同；本地 `$ref` 的 sibling 断言会继续执行。`json_path` 也是受限语法，不是完整 JSONPath 引擎。不支持的 Schema/配置返回 `ERROR`，而不是静默忽略。

MVP 没有 `llm_rubric`。需要语义 Judge 的 Pack 必须等 D40 扩展实现与校准后再声明对应组件。

## 9. 评分语义

每个 Grader 返回：

```text
PASS | FAIL | NOT_EVALUABLE | ERROR
```

其中：

- `PASS`：该断言满足；
- `FAIL`：观察完整，但行为不满足断言；
- `NOT_EVALUABLE`：缺少所需 output、artifact、Trace 或 runtime observation；
- `ERROR`：Grader 配置、实现或生命周期错误。

只有 hard Grader 全部 `PASS`，Scenario 才 hard pass。`NOT_EVALUABLE` 和 `ERROR` 永远不能聚合为 pass；优化流程也不会把它们当作 Skill 缺陷交给 Optimizer。

Manifest 的 Grader `params` 是默认参数，Scenario 的同 ID `grader_params` 可以覆盖它。Kernel 会额外注入受控的 `pack_root`、深冻结 `resources` 与 `hard`，而不会把这些内部路径和资源交给 Runtime。

## 10. Skill 优化 Policy

MVP 唯一优化器 ID 为 `skill_markdown_v1`。

```yaml
optimizer_policy:
  adapter: skill_markdown_v1
  patchable_components: [skill_instruction]
  allowed_paths: [SKILL.md]
  visible_splits: [dev]
  beam_width: 1
  max_rounds: 1
  max_candidate_snapshots: 1
  max_added_lines: 24
  forbid_case_literals: true
  params: {}
```

规则：

1. 未声明 `optimizer_policy` 的 Pack 是 eval-only；
2. `visible_splits` 当前必须且只能为 `[dev]`；
3. 当前安全修改面是 `SKILL.md`；
4. beam、round、snapshot 数必须为正数；
5. candidate 必须携带匹配的 base hash 和 patch hash；
6. 候选目录、真实内容 hash、allowed paths 和新增行数会再次校验；
7. repair 只接收 dev hard `FAIL`；tune 只接收 dev Goal/Objective 测量；
8. validation 结果不反馈给 Optimizer；
9. repair holdout 只执行最终候选；tune holdout 成对执行 baseline/candidate；失败后都不继续迭代；
10. Optimizer 必须声明受支持的 `proposal_contract`；
11. `optimizer_policy.params` 只经 `PatchConstraints.metadata.optimizer_params` 交给 `candidate-patch-v1` Optimizer；
12. 原 Subject 不会被覆盖。

通过全部门禁后，Kernel 将冻结候选再次独立物化到 `selected-candidate`，重验 Subject hash，并在报告中记录交付路径和 hash。内置 Skill Adapter 写入不参与 Subject hash 的受控 candidate provenance marker，使交付副本可独立重跑；原 trial 目录后续变化不会改变交付副本。

内置模型 Optimizer 声明 `aceval.optimizer/skill-markdown-improver-v2`，通过 `CommandModelClient` 返回完整替换版 `SKILL.md` 和 rationale；`FrozenCandidateOptimizer` 可加载预注册候选。`SkillOptimizerBridge` 再生成受 Kernel 校验的 `CandidatePatch`。自动修改 scripts、templates、assets、二进制、多文件 Subject 或 Agent 配置不属于当前内置实现。

## 11. Split 与信息边界

### 11.1 dev

- baseline 与所有候选可多次运行；
- repair 的 hard `FAIL` 或 tune 的目标测量可形成 Optimizer evidence；
- 允许决定下一轮候选。

### 11.2 validation

- 在 `optimize` 中运行 baseline 与从 dev 晋级的候选；普通 `run/compare` 也可显式评测 validation；
- repair 做 hard regression 对照；tune 还做 Objective paired gate；
- 结果只用于晋级，不生成新候选。

### 11.3 holdout

- 普通 `run/compare` CLI 不提供该 split；
- 仅 `optimize` 的最终候选进入；
- repair 最多一个 candidate holdout 批次；tune 最多一对 baseline/candidate holdout；
- 失败即停止，不反馈给 Optimizer。

`pack test` 为了 conformance 只使用 FakeRuntime 运行已解析的 dev/validation；holdout 会被加载、校验并纳入 Pack hash，但不会在该命令中执行。当前所有 Pack/Runtime 位于同一 OS 用户环境，split 边界是 Orchestrator 协议边界，不是对恶意宿主进程的访问控制。

## 12. Runtime capability

Pack 只声明所需能力，不选择 Runtime：

```yaml
driver:
  type: artifact_workspace
  required_runtime_capabilities:
    - fresh_session
    - workspace_fixture
    - artifact_output
    - canonical_trace
```

Orchestrator 会合并 Manifest 与 Driver 的 capability 要求，并在执行前检查 Runtime。缺少任意 capability 时直接失败。

当前 CLI 支持：

- `reference`：内置 Reference Agent Runtime，使用 JSON stdin/stdout model bridge；
- `fake`：模拟/conformance Runtime。

EvalPack 不能声明模型命令、环境变量、API key 或任意 Runtime 初始化代码。

内置 `reference` Runtime 只激活 UTF-8 `SKILL.md`，并提供 `list_files`、UTF-8 `read_file`、UTF-8 `write_file`。二进制文件、shell、network、browser 和 multimodal 任务需要新的受信 Runtime/工具实现及对应 capability；仅在 Pack 中声明一个新 capability 不会自动获得该能力。

## 13. Conformance

### 13.1 命令

```bash
aceval pack lint evalpacks/security-review
aceval pack test evalpacks/security-review --runtime fake

aceval plan --subject ./my-skill --cases seed-cases.json \
  --goal '保持正确并减少工具调用' --runtime-profile reference \
  --output .aceval/plans/my-skill
aceval pack generate --plan .aceval/plans/my-skill \
  --type generic --output .aceval/packs/my-skill
aceval pack quality .aceval/packs/my-skill
```

当前提供 `aceval plan` 和 `aceval pack generate/calibrate/freeze/quality`，不再需要手写最小目录骨架或测试设计 Sidecar。

### 13.2 `pack lint`

`pack lint` 检查：

- API version、kind 和字段结构；
- Pack/Scenario/fixture/Oracle/schema 路径；
- symlink 和 traversal；
- duplicate/unknown Subject、Driver、Grader、Optimizer；
- Scenario 的 Grader 引用和参数；
- optimizer split 约束；
- lifecycle 状态与 frozen 内容锁；
- suite 是否已解析；
- Pack/Suite hash。

### 13.3 `pack test`

`pack test` 使用 FakeRuntime 运行已解析的 dev/validation，并写 JSON/Markdown 报告。其目标是验证 prepare/execute/collect/cleanup、Grader 可执行性和报告链路；holdout 只由 `optimize` 的最终门禁执行。

Pack Case 出现预期的 hard `FAIL` 不一定代表 conformance 失败；出现 `ERROR` 或 `NOT_EVALUABLE` 才说明生命周期或配置不可用。质量基线应通过 `run`、`compare` 或 `optimize` 单独解释。

## 14. 两个 MVP Pack

### 14.1 `security-review`

| 项 | 当前内容 |
|---|---|
| Driver | `repository_workspace` |
| Case | 6 dev + 2 validation + 2 holdout |
| 输出 | final-message JSON |
| Grader | schema、record match、source reference、trace assert |
| Optimizer | 2 beam、2 dev-only rounds、最多 4 snapshots |

### 14.2 `csv-summary-smoke`

| 项 | 当前内容 |
|---|---|
| Driver | `artifact_workspace` |
| Case | 2 dev + 1 validation |
| 输入/输出 | `input.csv` -> `summary.json` |
| Grader | artifact exists、schema、JSON path、workspace diff |
| Optimizer | 1 beam、1 round、1 snapshot |

两个 Pack 的 FakeRuntime 行为只证明它们能复用公共生命周期和门禁。要形成真实 Benchmark，必须改用 Reference Runtime，并冻结模型 bridge、参数、预算和重复次数。

## 15. 新 Pack 的扩展成本

| 情况 | 当前所需工作 |
|---|---|
| 已支持模板 | 用户给少量 Case + Goal；Builder 生成 draft，用户校准/冻结 |
| 未支持但 generic JSON 契约足够 | 一键 generic fallback，再补/确认语义 Oracle 与 Grader |
| 现有 Subject + Driver + Grader 足够但无模板 | 新增/生成 Manifest、Scenario、fixture、Oracle/schema |
| 需要新确定性断言 | 编写并注册新 Grader，补单元与 conformance 测试 |
| 需要新输入/状态生命周期 | 编写并注册新 Driver，补清理、路径和大小限制测试 |
| 需要新 Agent 平台 | 实现 RuntimeAdapter，并在宿主/CLI 显式接线 |
| 需要新的候选生成方式 | 实现并声明 `candidate-patch-v1`；完整 `SKILL.md` improver 可使用 v2 bridge 契约 |
| 需要多文件/二进制 Subject | 实现新的 Subject Adapter、Runtime 工具和受约束 Optimizer/candidate 校验 |
| 需要 shell/network/browser/multimodal | 实现新的受信 Runtime/工具与 capability，并在宿主显式接线 |

MVP 已证明第一种路径可以跨安全审查和 CSV artifact 两类任务复用。其余路径仍然需要代码开发；当前没有插件市场或无需改宿主的动态加载机制。

因此，“任意 Skill 可扩展”的准确表述是：

> 当 UTF-8 `SKILL.md`、固定文件工具和现有 Driver/Grader 足以表达输入、观察和成功标准时，新任务可以仅用声明式 Pack 接入；超出已有组件能力时，通过公共 Protocol 和明确的 proposal contract 增加受信组件。当前没有无需改宿主的动态加载，也不保证所有扩展都只增加 Pack 文件。

## 16. D40 规划（Planned）

本节只保留 EvalPack 相关 backlog；跨模块的最新路线图、优先级和完成定义见 [ROADMAP.md](./ROADMAP.md)，复杂 Skill 规划与归因见 [COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md](./COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md)，可视化 Pack 校准方案见 [VISUAL_CONSOLE_DESIGN.md](./VISUAL_CONSOLE_DESIGN.md)。

1. 扩大 `v1alpha1` legacy repair 与 `v1alpha2` repair/tune 的兼容性测试；
2. 在现有 `design/` Sidecar 与 cross-reference freeze gate 上增加 schema migration 和 Pack revision diff；
3. 增加 seed expansion/metamorphic fixture、known-good/known-bad、mutation calibration 和 evaluator flake；
4. 将已接入离线 Failure Card 的 Session `ObservationCompleteness` 继续接入 EvalRun、Grader Replay 与 Case mining；
5. 增加 Run manifest、Trace/artifact store、公司在线 Runtime 和 Replay；
6. 增加显式可信 Extension Loader，但 Manifest 仍不得自动 import 代码；
7. 为多文件 Skill bundle、二进制 artifact 和受控 process/network/browser 工具定义受信组件与 capability，并为非单 entrypoint 文本 Candidate 定义显式 `verify_candidate_patch` 类扩展契约；
8. 增加外部 suite resolver 和 evaluator-only 数据读取边界；
9. 增加 AgentSubject/FixedAgentTarget capability contract；
10. 经人工校准后增加结构化 LLM Judge、受限 Worker 和更强 holdout 隔离；
11. 建立 12–20 Case 的真实 Reference Runtime Benchmark。

在这些能力落地前，不应声称 EvalPack 已支持任意 Python 插件、外部隐藏集解析、Agent 配置优化、HTML 报告或跨平台等价评测。

## 17. MVP 明确不做

- Manifest 自动加载任意 Python 或 shell；
- 通用 DAG、插件市场和远程 Pack 安装；
- 数学意义的任意自然语言全路径覆盖；
- 自动递归修改并直接冻结生成它自己的 EvalPack；
- 把同源模型生成 Case 作为独立 sealed holdout；
- 自动从自然语言生成并直接信任 Oracle；
- 自动修改脚本、测试、Case、Oracle 或 Grader；
- 使用内置组件自动修改 `scripts/`、`templates/`、`assets/`、二进制或多文件 Subject；
- 使用内置 Reference Runtime 执行 shell、network、browser 或 multimodal 工具；
- Agent 配置自动优化；
- LLM-only 总分覆盖所有任务；
- 将 FakeRuntime 通过率描述为真实 Agent 质量；
- 将同一 OS 用户下的 split 目录描述为安全沙箱。

用户可以借助模型生成 Manifest、Case 或 rubric 草稿，但进入 validation/holdout 前必须由用户确认成功标准并冻结 Pack。
