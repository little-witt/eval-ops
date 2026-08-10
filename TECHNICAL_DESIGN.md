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
- `SkillSubject` 是第一个被测对象，长期扩展到 `AgentSubject`、Prompt 和 Workflow。
- 20 天版本交付可信的单 Runtime 闭环。
- 40 天版本通过 Benchmark、AgentSubject、隔离执行、CI 和消融实验形成求职作品。

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

1. 用户通过一个命令提交 Skill 和 Case，无需人工搬运执行日志。
2. 系统完成基线运行、评分、失败归因、候选 Diff、回归验证和报告。
3. 至少支持两类确定性断言和一个结构化 LLM Judge。
4. 工作流不把 holdout Case 内容传给优化器；D20 这是协议隔离，不宣称是安全隔离。
5. 原始 Skill 永不被自动覆盖，只生成候选快照和 Diff。
6. 至少一个候选因为回归被拒绝，证明系统不是单向“刷分”。
7. 报告包含逐 Case 证据、通过率、成本、Token 和耗时。
8. 完整 Pitch 控制在 6 分钟左右，现场模型失败时可切换 Replay。

### 3.2 D40 求职作品成功标准

1. 同一 Eval Core 同时支持 `SkillSubject` 和 `AgentSubject`。
2. 支持在线执行和离线 Trace 导入两种模式。
3. 至少一个真实 Runtime Adapter，并支持标准 Replay/Trace Import。
4. Benchmark 包含 2–4 个 Subject、20–30 个分层 Case；30–50 个为扩展目标。
5. 关键 Case 重复执行至少 3 次，报告波动、成本和配对提升。
6. 有人工标注的 Judge 校准集和失败归因金标集。
7. 至少完成“仅最终输出 vs Trace-aware”一组消融；第二组为扩展目标。
8. 支持容器或受限 Worker、Replay CI Gate 和完整可复现文档。
9. Benchmark 基础设施执行成功率目标不低于 95%，新用户能在 15 分钟内跑通 Replay 示例。

## 4. 范围定义

### 4.1 D20 必须完成

- `SkillSubject`：解析、校验、快照和新旧版本对照。
- 一个真实 Runtime Adapter。
- YAML Case、fixture、dev/validation/holdout 分层。
- 干净工作区、超时、环境变量白名单和产物收集。
- Canonical Trace 和原始 Runtime Trace。
- 确定性 Grader、结构化 LLM Judge、评分聚合。
- 失败分类、证据引用和候选 `SKILL.md` Diff。
- Live Profile 最多 1 个候选、1 轮；离线 Benchmark 最多 2 个候选、2 轮；两者都有金额/Token/时间预算和提前停止。
- 静态 HTML/Markdown 报告、Replay 和断点恢复。

### 4.2 D20 明确不做

- 多租户 SaaS、账号、RBAC、计费和分布式任务队列。
- 任意客户远程环境执行。
- 多个 Agent 自由讨论或投票式编排。
- 自动修改脚本、自动合并 PR 或自动发布生产 Skill。
- 多 Runtime、复杂前端、实时 Trace 大屏。
- 自动生成大量 Case 并将其视为真实评测标准。

### 4.3 D21–D40 扩展范围

- 抽象通用 `SubjectUnderTest`，新增 `AgentSubject`。
- 通用 Replay/Trace Import；第二 Runtime 为扩展目标。
- 离线 Trace Importer、容器隔离和 CI Gate。
- Judge 校准、失败归因评估、重复实验和统计汇总。
- 20–30 Case 的公开 Benchmark、至少一组消融实验、架构/安全/方法论文档。

## 5. 总体架构

```text
CLI / Static Report / GitHub Action / Optional Web API
                         |
                  Run Orchestrator
                  (explicit state machine)
                         |
       +-----------------+------------------+
       |                 |                  |
 Subject Adapter    Runtime Adapter     Scenario Suite
 Skill / Agent      Codex / Claude /    Case + Fixture
                    Subprocess / Replay  Rubric + Assertion
       |                 |
       +---------- Sandbox Runner ---------+
                         |
               Raw Trace + Canonical Trace
                         |
       Deterministic Grader -> LLM Judge -> Aggregator
                         |
                  Failure Analyzer
                         |
                 Candidate Optimizer
                         |
             Dev -> Validation -> Holdout Gate
                         |
             Diff / Report / Human Approval
```

### 5.1 架构原则

1. **工作流负责控制，模型负责语义。** 状态、预算、权限、停止条件和发布门禁由代码控制。
2. **核心不依赖 `SKILL.md`。** Skill 只是一个 Subject Adapter。
3. **确定性评分优先。** 能用 Schema、测试或文件检查解决的问题不交给 LLM Judge。
4. **保留原始证据。** 所有诊断和评分都必须能回指 Trace 或产物。
5. **候选不可污染评测系统。** 优化器不能修改 grader、Case、holdout、Runner 或预算策略。
6. **本地优先。** D40 前不建设云端多租户控制面。
7. **诚实降级。** 无法观测的指标标记为 `not_evaluable`，不能当作通过。

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
| API | FastAPI，D20 可选、D40 完成 | 方便后续 Web、Worker 和第三方集成 |
| 测试 | pytest + golden files | 适合数据协议、状态机和报告快照测试 |
| 隔离 | D20 受限 subprocess；D40 Docker/Podman | 先确保闭环，再补强安全边界 |
| 可观测性 | 内部 Canonical Trace；D40 增加 OTLP 导出 | 不将核心绑定到单一可观测平台 |

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
        scenario: Scenario,
        profile: RuntimeProfile,
        workspace: Path,
    ) -> RunResult: ...

class Grader(Protocol):
    async def evaluate(self, run: RunResult, scenario: Scenario) -> GradeResult: ...

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
| `domain` | Pydantic 模型、枚举、协议、Schema 版本 | 必须 |
| `subjects` | Skill/Agent 的校验、快照、安装、Diff | Skill 必须 |
| `runtimes` | Runtime 命令、事件解析、能力声明 | 一个必须 |
| `runner` | 临时目录、超时、并发、环境过滤、产物收集 | 必须 |
| `traces` | Raw Event 到 Canonical Event 的标准化 | 必须 |
| `graders` | 硬断言、LLM Judge、评分聚合 | 必须 |
| `analysis` | 失败分类、证据提取、根因置信度 | 必须 |
| `optimization` | 候选生成、路径白名单、复杂度限制 | 必须 |
| `orchestration` | 状态机、预算、重试、门禁和停止条件 | 必须 |
| `storage` | SQLite 元数据、Trace 和 artifact 索引 | 必须 |
| `reporting` | HTML/Markdown、Diff 和对照表 | 必须 |
| `api` | CLI、FastAPI 和 GitHub Action 接口 | CLI 必须 |

## 8. 核心数据模型

| 实体 | 关键字段 |
|---|---|
| `SubjectVersion` | `kind`、`uri`、`content_hash`、`parent_hash`、`metadata` |
| `RuntimeProfile` | adapter、model、parameters、tool policy、environment hash |
| `Scenario` | prompt、fixtures、assertions、rubric、split、timeout、tags |
| `SuiteVersion` | suite hash、visible Case、holdout 引用、版本信息 |
| `Run` | subject、runtime、case、attempt、status、tokens、cost、duration |
| `TraceEvent` | seq、type、timestamp、payload、tool、duration、error |
| `Artifact` | relative path、hash、MIME、size、producer |
| `GradeResult` | grader type/version、pass、score、evidence、confidence |
| `Diagnosis` | category、blamed component、evidence refs、confidence |
| `CandidatePatch` | base hash、unified diff、allowed paths、rationale |
| `Experiment` | baseline、candidates、budget、gates、decision、stop reason |

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

subject:
  kind: skill
  path: ./examples/security-review/skill

runtime:
  adapter: primary-cli
  model: company-approved-model
  repetitions: 1
  timeout_seconds: 180

budget:
  max_candidates: 1
  max_rounds: 1
  max_total_tokens: 200000
  max_cost_usd: 2.00
  max_wall_time_seconds: 600

suite:
  visible: ./examples/security-review/evals/visible.yaml
  holdout_ref: security-review-holdout-v1

gates:
  require_no_hard_regression: true
  min_validation_uplift: 0.10
  max_cost_increase_ratio: 0.25
```

### 9.2 Case 示例

```yaml
id: command-injection-001
split: dev
prompt: Review the provided server code and return findings as JSON.
fixtures:
  - fixtures/vulnerable_server.py
assertions:
  - type: json_schema
    schema: schemas/review-output.schema.json
    hard: true
  - type: finding_present
    rule_id: command-injection
    file: vulnerable_server.py
    hard: true
  - type: forbidden_finding
    rule_id: sql-injection
    hard: true
rubric:
  - dimension: explanation_quality
    weight: 0.3
  - dimension: remediation_actionability
    weight: 0.7
timeout_seconds: 120
tags: [security, tool-use]
```

`holdout` 不与 visible suite 放在同一文件中。D20 通过独立目录、独立工作区和 Orchestrator 接口避免将内容传给优化器，但这只是协议隔离：同一 OS 用户下的 subprocess 仍可能遍历宿主文件系统。D40 使用独立进程权限或 Worker 建立真正的访问边界。

三类数据的信息边界必须固定：

- Optimizer 只接收 dev Trace、逐 Case 评分和诊断证据；
- validation 只向候选选择器返回晋级结果和聚合指标，不把逐 Case 证据反馈给 Optimizer；
- holdout 仅对最终候选运行一次，只返回最终门禁结果。看到 validation 或 holdout 结果后继续修改，必须开启新的实验和数据集版本，不能继续声称原集合是未见数据。

配置示例是 Live 演示 Profile。完整实验使用单独的 Benchmark Profile，可运行 2 个候选、2 轮和 2–3 次重复，但应在演示前完成并通过 Replay 展示，不能期待在 6 分钟 Pitch 内实时完成。

| Profile | 数据 | 候选/轮次/重复 | 目标 Agent 执行量 | 用途 |
|---|---|---:|---:|---|
| Live | 2 dev + 1 validation + 1 holdout | 1 / 1 / 1 | 约 7–10 次 | 现场只展示关键节点，超时立即转 Replay |
| Benchmark | 6 dev + 2 validation + 2 holdout | 最多 2 / 2 / 2 | 约 50–80 次 | 赛前完整运行，统计和报告 |
| Replay | 已冻结的真实 Run | 0 | 0 次 | 稳定展示完整证据链 |

实际调用量由 Orchestrator 在执行前计算并展示，超过金额、Token 或墙钟预算时拒绝启动。

## 10. 运行目录与可复现性

```text
.aceval/
  aceval.sqlite3
  objects/<sha256>/...
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

每次 Run 必须记录：

- Subject、Suite、grader prompt 和代码的 hash；
- Runtime、模型、参数、工具权限和依赖版本；
- 尝试次数、随机参数、开始/结束时间；
- Token、估算费用、墙钟时间和停止原因；
- Raw Trace、Canonical Trace、产物 hash 和评分证据。

## 11. 工作流状态机

```text
CREATED
  -> VALIDATING
  -> BASELINE_RUNNING
  -> BASELINE_GRADED
  -> ANALYZING
  -> CANDIDATES_GENERATED
  -> CANDIDATES_SCREENED
  -> VALIDATION_RUNNING
  -> CANDIDATE_SELECTED
  -> HOLDOUT_RUNNING
  -> READY_FOR_REVIEW
  -> APPROVED | REJECTED

任意状态 -> FAILED | BUDGET_EXHAUSTED | NO_IMPROVEMENT | UNSAFE_PATCH
```

状态转换写入 SQLite，并保证已完成结果可被幂等复用。D20 采用 at-least-once 语义，不承诺外部模型调用 exactly-once：如果模型已计费但进程在结果落库前崩溃，该尝试标记为 `UNKNOWN`，再次运行前明确提示可能产生重复费用。

### 11.1 停止条件

- 达到最大轮数、候选数、Token、费用或墙钟时间；
- 连续两轮没有达到最小有效提升；
- 所有候选出现硬指标回归；
- 根因置信度不足，无法支持安全修改；
- 候选需要修改白名单以外的文件；
- validation 提升但 holdout 失败；
- Runtime 或环境故障占主导，修改 Skill 无法解决。

## 12. 评分体系

### 12.1 分层 Grader

1. **硬断言**：JSON Schema、文件存在、字段值、禁止项、退出码、测试结果。
2. **轨迹指标**：工具错误、无效重试、禁止工具、策略违规、产物缺失。
3. **LLM Judge**：任务完成度、解释质量、可用性和用户 rubric。
4. **人工评审**：低置信度、争议结果和最终补丁审批。

LLM Judge 必须：

- 输出经过 Pydantic 校验的结构化 JSON；
- 对 candidate 身份匿名，比较时随机顺序；
- 为每个分数提供 Trace 或 artifact evidence reference；
- 固定并版本化 judge model、prompt 和参数；
- 与优化器使用独立上下文，D40 优先使用不同模型交叉校验。

### 12.2 候选门禁

```text
硬断言无回退
AND 安全违规为 0
AND validation uplift >= 配置阈值
AND 成本/延迟增幅 <= 配置阈值
AND holdout 达到阈值
```

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
6. D40 使用人工根因金标集计算分类准确率。

## 14. 候选优化策略

1. 基线运行后按失败类型聚类。
2. Analyzer 输出可验证的失败假设，而非直接要求“改进 Prompt”。
3. Live Profile 一轮只生成 1 个最小 Diff；完整离线实验一轮最多生成 2–3 个。
4. D20 只允许修改 `SKILL.md`，限制新增行数、文件数和补丁大小。
5. 静态检查通过后，在 dev Case 上进行低成本筛选；只有 dev 结果可用于后续修改。
6. 候选在 validation 上匿名配对比较，Optimizer 只能获得晋级与聚合结果。
7. 仅最优候选进入 holdout，一次失败即停止自动晋级。
8. 最终只输出候选快照、Diff、证据和推荐，不写回原始 Skill。

候选排序不能只看质量总分，还要惩罚成本、延迟和过度特化。D20 可采用简单的门禁后排序，D40 再引入置信区间和复杂度惩罚。

## 15. Runtime Adapter

最小协议：

```text
capabilities()
prepare(subject, workspace, mode)
execute(case, profile, budget) -> event stream
collect() -> result + artifacts
cancel()
cleanup()
```

能力声明至少包含：

- 是否支持无头执行；
- 是否能提供消息、工具调用、用量和文件事件；
- 是否支持并行、取消和固定模型参数；
- 是否能可靠安装/移除 Skill；
- 是否支持 trigger eval；
- 环境隔离等级。

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

### 16.2 D40 增强

- Docker/Podman 临时容器、只读根文件系统和临时可写目录；
- 默认禁网，按域名或代理显式放行；
- CPU、内存、PID 和磁盘配额；
- 不挂载宿主 Docker socket；
- 任务级短期凭证、日志脱敏和数据保留策略；
- 独立 holdout Worker，优化器进程无读取权限。

## 17. 推荐仓库结构

```text
agent-capability-evalops/
  pyproject.toml
  uv.lock
  README.md
  src/aceval/
    domain/
    subjects/
      skill.py
      agent.py              # D21+
    runtimes/
      base.py
      primary_cli.py
      replay.py
    runner/
    traces/
    graders/
    analysis/
    optimization/
    orchestration/
    storage/
    reporting/
    cli.py
    api.py                  # D21+
  examples/
    security-review/
  benchmarks/              # D34+
  tests/
    unit/
    integration/
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
# 初始化项目和示例配置
aceval init ./my-skill

# 零模型调用的静态校验
aceval validate --config aceval.yaml

# 执行 with/without 或 old/new 基线实验
aceval run --config aceval.yaml --mode both

# 受预算约束地生成和验证候选
aceval optimize --config aceval.yaml --max-rounds 2

# 从已有 Run 生成报告
aceval report --run <run-id>

# 现场网络故障时从原始 Trace 回放
aceval replay --run <run-id>
```

D20 最重要的入口是：

```bash
aceval optimize --config examples/security-review/aceval.yaml
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

完整矩阵用于赛前 Benchmark 和现场 Replay。现场 Live 只运行 4 个锚点 Case、1 个候选、1 轮、1 次尝试；其余真实历史结果通过 Replay 呈现，并在界面中明确标注为回放数据。

必须展示两类候选：

1. 过度宽泛候选：召回提高但误报正常代码，被 validation 拒绝；
2. 精确候选：修复漏报且不增加硬回归，进入 holdout。

## 20. D1–D20 黑客松计划

| 天 | 工作 | 当日验收 |
|---:|---|---|
| D1 | 锁定 Runtime、模型、Demo Skill、成功指标和 Scope；写 3 个锚点 Case | 一页 Scope；漏洞、安全实现、非 Skill 故障各一个 |
| D2 | 定义 Case、Trace、Run、Grade Schema，并冻结锚点金标 | YAML 可校验，锚点预期和证据明确 |
| D3 | Case Loader、run_id、目录和 artifact hash | 单次 Run 的输入/输出可落盘 |
| D4 | Runtime Adapter 技术尖峰 | 干净会话可执行一个 Case |
| D5 | Canonical Trace 和用量采集 | 记录消息、工具、产物、耗时和错误 |
| D6 | 确定性 Grader | 至少支持 Schema、finding 和文件断言 |
| D7 | 结构化 LLM Judge | 输出分数、证据、理由、置信度 |
| D8 | baseline/candidate 配对，完成 visible suite | 可展示逐 Case 差异；至少 6 个 visible Case |
| D9 | 失败分类和诊断器 | 输出带证据和置信度的诊断假设，不宣称证明因果 |
| D10 | 独立候选工作区和安全 Diff；冻结 validation/holdout | 不修改原 Skill；测试集此后不再按候选调整 |
| D11 | 候选补丁生成 | 根据证据生成最小 `SKILL.md` Diff |
| D12 | 优化状态机、预算和停止条件 | 超预算、无提升均能正确停止 |
| D13 | dev/validation/holdout 信息边界 | Optimizer 只看 dev；validation/holdout 不返回逐 Case 证据 |
| D14 | Regression Gate | 回归候选被拒绝，优胜候选可晋级 |
| D15 | CLI 和静态 HTML 报告 | `run/optimize/report` 可用 |
| D16 | 清理数据、生成完整 Benchmark Run 和 Live 子集 | 完整矩阵可 Replay；Live 固定为 4 个 Case |
| D17 | Cache、Resume 和 Replay | 网络失败后可继续或回放 |
| D18 | 连续运行、测试和 P0 修复 | 完整闭环连续 3 次无基础设施失败 |
| D19 | Pitch、架构图、录屏和演练 | 6 分钟完成演示，有降级预案 |
| D20 | 代码冻结和参赛包 | Live、Replay、录屏均可用 |

### 20.1 黑客松冻结点

- D4 未跑通 Runtime：立即切换到最熟悉的 CLI，不继续封装当前方案。
- D9 前 Judge 不稳定：降低语义评分权重，以确定性 Grader 保证 Demo。
- D12 后不增加黑客松功能。
- D14 尚未闭环：立即取消所有 UI 工作。
- D17 后不修改核心数据协议。
- D20 只修 P0，不做重构。

## 21. D21–D40 求职作品计划

| 天 | 工作 | 当日验收 |
|---:|---|---|
| D21 | 复盘并清理 Demo 耦合 | Core 不包含 Demo 名称和特例 |
| D22 | 抽象 `SubjectUnderTest` | `SkillSubject` 通过通用契约测试 |
| D23 | 实现 `AgentSubject` | 可评 system prompt、model、tools 配置 |
| D24 | Subject/Runtime conformance tests | prepare/run/collect/cleanup 行为统一 |
| D25 | 离线 Trace Importer | 已有 JSONL 日志可直接评分和诊断 |
| D26 | Docker/受限 Worker | 超时、只读目录、环境过滤、资源限制 |
| D27 | 稳定 AgentSubject、Importer 和 Worker | 三条路径的最小 E2E 通过 |
| D28 | Grader 插件化和人工校准集 | 至少 20–30 条人工标签 |
| D29 | Judge 校准和误差分析 | 输出 agreement、混淆矩阵和失败样例；目标 kappa >= 0.6 |
| D30 | 重复实验和统计聚合 | 报告均值、方差和成功区间 |
| D31 | 扩充 Benchmark 第一批 | 2–4 个 Subject、至少 15 个 Case |
| D32 | 完成 20–30 Case Benchmark | Case、金标和版本冻结 |
| D33 | 失败归因金标评测 | 30 条左右标注 Trace；top-1 目标 >= 70% |
| D34 | Replay GitHub Action/CI Gate | PR 中执行确定性回放并阻止硬回退 |
| D35 | Trace-aware 消融实验 | Output-only 与 Trace-aware 价值可量化 |
| D36 | 安全和恢复测试 | 脱敏、路径逃逸、超时、原文件保护有测试 |
| D37 | 完整 Benchmark 和稳定性运行 | 基础设施成功率目标 >= 95% |
| D38 | README、教程、ADR 和限制说明 | 新用户 15 分钟内跑通 Replay 示例 |
| D39 | 干净环境复现和演示视频 | 完整安装到报告流程成功 |
| D40 | Tag/Release 和作品包装 | Benchmark、视频、简历指标、Roadmap 完整 |

### 21.1 D40 优先级

必须：

- `AgentSubject`；
- Replay/Trace Import；
- 20–30 Case Benchmark 和重复实验；
- Judge/归因校准；
- 容器或受限 Worker；
- Replay CI Gate；
- 一组 Trace-aware 消融实验和完整文档。

应做：

- 第二个真实 Runtime 或标准 subprocess Adapter；
- 扩展到 30–50 个 Case；
- 第二组“LLM-only vs 混合 Grader”消融；
- 生成但需人工确认的 Case 草稿；
- FastAPI 查询接口；
- 简单历史趋势页。

不做：

- 多租户云平台；
- 企业 SSO/RBAC；
- 分布式调度；
- 无审批自动发布；
- 覆盖所有 Agent 框架。

## 22. Benchmark 设计

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

D40 必须完成 Output-only vs Trace-aware diagnosis/optimization。LLM-only grading vs deterministic + LLM hybrid grading 是扩展实验，有余量再做。

## 23. 测试策略

### 23.1 单元测试

- Schema 校验和向后兼容；
- hash、快照、Diff 和路径白名单；
- 状态机合法/非法转换；
- 预算、超时、停止条件；
- Trace 转换、脱敏和证据引用；
- Grader 聚合和门禁逻辑。

### 23.2 集成测试

- Fake Runtime 的成功、超时、工具失败和中断；
- Skill with/without 安装隔离；
- 候选目录不可访问 holdout；
- Resume 会复用已落盘结果；`UNKNOWN` 外部调用会提示可能重复计费；
- 报告可从冻结的 Run 完全重建。

### 23.3 Golden/E2E 测试

- 固定 Trace 的已知 Grade；
- 固定失败的已知 Diagnosis；
- 从基线到候选报告的完整 Demo；
- D40 在容器中运行最小 E2E。

## 24. GitHub Action 策略

- 每个 PR：零模型静态校验、单元测试、Schema、确定性 Replay。
- 标记为 `eval-required` 或发布分支：运行小型在线 eval。
- 手工/夜间：完整 Benchmark、重复实验和成本报告。
- Gate 默认只阻止硬指标回退；语义分数变化先作为提示，校准稳定后再参与阻断。

## 25. 黑客松演示脚本

1. **0:00–0:30 痛点**：展示人工复制日志和反复测试的现状。
2. **0:30–1:00 输入**：缺陷 Skill、visible Case 数量、不可见 holdout 数量。
3. **1:00–1:45 基线**：漏报、确定性断言和对应 Trace 证据。
4. **1:45–2:30 诊断与 Diff**：问题归因到 Skill 指令，展示最小修改。
5. **2:30–3:30 候选筛选**：过度修复因 validation 误报被拒绝。
6. **3:30–4:20 Holdout**：最优候选首次执行隐藏集且无硬回退。
7. **4:20–5:10 报告**：质量、成本、耗时、证据和人工审批。
8. **5:10–6:00 延展**：同一 Core 可评测完整 Agent、导入历史日志并进入 CI。

现场准备：

- 一个最小 Live Case；
- 一次完整 Run 的 `aceval replay`；
- 一段完整闭环录屏；
- 一份成功修复报告和一份“无法安全修复、主动停止”报告。

## 26. 风险与止损

| 风险 | 预警 | 应对 |
|---|---|---|
| Runtime/API 不稳定 | D4 仍不能稳定执行 | 立即更换到最熟悉 Runtime |
| LLM Judge 自我偏袒 | 修改后语义分总是上涨 | 硬断言优先、匿名比较、人工校准 |
| 少量 Case 过拟合 | dev 上升、validation 下降 | 分层数据集、最小 Diff、复杂度限制 |
| 成本/耗时失控 | 单轮超过预算或 10 分钟 | 逐级淘汰、缓存、少候选、提前停止 |
| Trace 格式碎片化 | Adapter 充满特判 | Raw + Canonical 双轨、能力声明 |
| 修改范围失控 | 候选修改脚本或测试 | D20 只允许 `SKILL.md` |
| UI 拖累核心 | D14 仍未闭环 | 删除 Web，只保留静态报告 |
| 现场网络失败 | 彩排超时 | Live + Replay + 录屏 |
| 项目像 LLM 套壳 | 无客观结果和对照 | 强化配对实验、隐藏集、Benchmark、消融 |
| D40 范围膨胀 | D25 仍未完成通用接口 | 砍第二 Runtime、在线 CI 和第二消融，保留 AgentSubject/Trace Import/Benchmark |

## 27. 求职作品交付物

- 可安装的 CLI 和版本化 Release；
- 20–30 Case 的公开 Benchmark，30–50 Case 作为后续扩展；
- 一份 Benchmark 与消融实验报告；
- `docs/architecture.md`、`evaluation-methodology.md`、`security.md`、`limitations.md`；
- 30–60 秒 GIF、3 分钟产品视频、10 分钟技术视频；
- 可复现的 sample run、Trace、Diff 和 HTML 报告；
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

> 失败证据 -> 根因 -> 最小 Diff -> 回归拒绝/通过 -> 人工审批。

40 天版本负责让面试官相信：

> 这不是一个只会改 Prompt 的 Agent，而是一套有隔离、对照、统计、Benchmark、安全边界和可迁移接口的 Agent EvalOps 工程系统。
