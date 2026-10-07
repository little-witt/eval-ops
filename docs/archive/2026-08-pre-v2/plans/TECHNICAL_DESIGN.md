# Agent Capability EvalOps 技术方案

> 黑客松展示名：**Skill Doctor**
> 长期项目名：**Agent Capability EvalOps**
> CLI/包名：`aceval`
> 文档状态：MVP 实现基线
> 基线日期：2026-08-18

## 1. 项目结论

项目采用“**通用评测内核 + 内置参考 Agent Runtime + 声明式 EvalPack**”架构。

MVP 已经不是只针对安全代码审查的单一工作流：在“UTF-8 `SKILL.md` + 固定 UTF-8 文件工具”这一当前支持面内，安全审查、CSV 汇总和 Builder 生成的 generic JSON 任务共用同一套 Subject、Runtime、Driver、Grader、Optimizer、门禁和报告契约。用户提供少量 Case 和 Goal 即可生成 draft EvalPack；Kernel 不会从未知任务中自动发明并信任 Oracle。

核心价值主张是：

> 将“用户转发失败日志、让 Agent 修改 Skill、再手工重测”的循环，变成同时支持 repair 与 tune、可复现、受预算约束、具有 dev/validation/holdout 门禁且不覆盖原 Skill 的自动评测与迭代流程。

本方案刻意内置一个稳定 Runtime，而不是在 MVP 适配所有 Agent 平台。当前分数只代表该 Reference Runtime 和对应模型桥接配置下的结果；它可以验证 Skill 的相对改进，但不假设不同 Agent 平台必然得到相同绝对效果。

D20 已在 Kernel 外实现“复杂 Skill 测试规划”和“证据化故障归因”两个能力面：前者以确定性、source-grounded 方式把冻结 `SKILL.md` 转成 Capability Graph、Test Plan、Case 草稿和 Coverage Matrix；后者把布尔式非 Skill 失败门控升级为版本化 Failure Card 与 Patch Authorization。公司在线 Replay、受控 Process Tool 和 Web Console 仍属于 D40。详细方案见 [COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md](./COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md)。

## 2. 状态标识

本文用以下两个状态区分实现与规划：

- **Implemented（MVP）**：当前仓库已有代码和测试，可通过 CLI 或公共 Python 契约运行。
- **Planned（D40）**：完整作品阶段计划，不属于当前能力，也不能用于描述当前交付。

## 3. MVP 已实现范围

| 能力 | 当前实现 |
|---|---|
| Subject | `skill_markdown_v1`，仅冻结 UTF-8 `SKILL.md` 与可选 UTF-8 JSON `subject.json` |
| Agent Runtime | 项目内置 `ReferenceAgentRuntime`，固定 UTF-8 文件工具 |
| 模型接入 | `CommandModelClient`，JSON stdin/stdout 命令桥接 |
| 模拟 Runtime | `FakeRuntime`，仅用于 conformance、单元测试和离线演示 |
| EvalPack | `v1alpha1` legacy repair + `v1alpha2` repair/tune，本地声明式加载和全树 hash |
| Pack Builder | 少量 Case + Goal 生成 draft；三种模板、generic fallback、校准状态与冻结内容锁 |
| Complex Skill Planner | `SKILL.md` source refs、Capability Graph、风险加权 Test Requirement、Case 草稿和 Runtime gap |
| Coverage | planned、Runtime-executable、Oracle-ready 和显式 observed requirement coverage |
| Pack Quality | design Sidecar、critical coverage、Oracle trust、Runtime gap、family leakage 和 Sidecar source-subject/plan hash 一致性门禁 |
| Driver | `repository_workspace`、`artifact_workspace` |
| Grader | `json_schema`、`json_path`、`record_match`、`artifact_exists`、`source_reference`、`trace_assert`、`workspace_diff` |
| Optimizer | `skill_markdown_v1`，只生成或加载 UTF-8 `SKILL.md` 候选；显式声明 proposal contract |
| Improvement | `auto | repair | tune`；单一 Objective + hard Grader guardrail |
| 门禁 | dev 搜索、validation 晋级；tune 的 validation/holdout 成对比较 |
| 预算 | 实验级 Token、成本、工具调用和墙钟预算 ledger |
| Failure Attribution | Diagnostic Signal、Failure Card、证据等级、Patch Decision；只把授权失败交给 Optimizer |
| 报告 | 版本化 JSON 和 Markdown 文件，包含诊断聚合和 Scenario Patch Decision |
| 公司连接 | Profile、Execute endpoint、Session fetch/import、Observation completeness、离线 Session diagnose |
| CLI | `plan`、`pack generate/calibrate/freeze/lint/test/quality`、`doctor`、`run/compare/optimize`、`profile/session` |
| 示例 Pack | `security-review` 与 `csv-summary-smoke` |

### 3.1 当前明确未实现

- 公司 Agent API 到完整 RuntimeAdapter 的直接执行接线；
- model-assisted richer semantic extraction、多文件 Skill bundle 分析和自动 fixture 变换；
- EvalRun 完成后自动回写 dynamic tool/state-transition coverage；
- Pack mutation calibration、已知好坏样本区分能力和 evaluator flake；
- Codex、Claude Code 或其他第三方 Agent 平台 Adapter；
- `AgentSubject`、`FixedAgentTarget` 和 Agent 配置自动优化；
- Imported Session 到 EvalRun 的 Replay/重评分接线；
- SQLite、CAS、Run Repository、跨进程恢复和结果缓存；
- Pydantic、Typer、Jinja2、HTML 报告或 Web UI；
- LLM Judge/`llm_rubric`；
- `report`、`replay`、`--extension` 等 CLI；
- `scripts/`、`templates/`、`assets/` 等多文件 Skill bundle、二进制 Subject 和二进制工具；
- shell、network、browser、multimodal Agent 工具面；
- Docker/Podman 沙箱、独立权限 Worker 和真正的 holdout 权限隔离；
- GitHub Action、在线服务和多租户控制面。

`src/aceval/runtime.py` 中存在库级 `SubprocessRuntime` 辅助实现，但它没有暴露为当前 CLI 的受支持 Runtime，也不等同于公司 Agent API Adapter。MVP 的真实模型执行路径是 `ReferenceAgentRuntime`。

## 4. 总体架构

```text
 Skill + Seed Cases + Goal -> Analyzer / Test Planner
                                      |
                       Capability / Coverage / Gaps
                                      |
        Case + Goal -> Pack Builder -> draft/calibrating/frozen
                                      |
                      aceval CLI / doctor
                          |
                EvalOrchestrator + Budget
                          |
          +---------------+----------------+
          |               |                |
    SubjectAdapter   RuntimeAdapter   ComponentRegistry
          |               |          Driver / Grader /
          |               |             Optimizer
          +---------------+----------------+
                          ^
                    FrozenEvalPack
             Scenario / Fixture / Oracle /
             Component IDs / Patch Policy
                          |
             Observation -> Grade -> Gate
                          |
                   Failure Attribution
                          |
          Failure Card -> Patch Authorization
                          |
               dev -> validation -> holdout
                          |
                  JSON + Markdown report

 Company API Profile -> Session fetch/import -> ImportedRunBundle
                                      -> offline Diagnosis (implemented)
                                      -> EvalRun/Grader Replay (planned)
```

依赖方向保持单向：Pack 只引用公共组件 ID；Kernel 不 import `security-review` 或 `csv-summary-smoke`，也不包含漏洞类型、CSV 字段等领域分支。公共契约允许由宿主显式注册新组件，但当前 CLI 只构造内置 Registry；超出内置文件/文本能力时需要新增受信组件或进入 D40 扩展，不是单纯增加 Pack 数据即可完成。

## 5. Reference Agent Runtime

### 5.1 职责

`ReferenceAgentRuntime` 是项目拥有的最小 Agent 循环。它与内置 `skill_markdown_v1` Subject Adapter 配套；后者只快照和物化 UTF-8 `SKILL.md`，目录模式可额外携带 UTF-8 JSON `subject.json`。它负责：

1. 读取冻结的 `SKILL.md` 并注入 system message；
2. 将 Case prompt 作为 user message；
3. 向模型声明固定工具集合；
4. 执行模型返回的工具调用并追加 tool result；
5. 在模型返回最终文本或达到步数限制时结束；
6. 输出 final output、Canonical Trace、usage 和耗时。

内置工具只有：

- `list_files`：列举 Case workspace 中的普通文件；
- `read_file`：读取 workspace 内 UTF-8 文件；
- `write_file`：写入 workspace 内 UTF-8 文件。

工具拒绝绝对路径和 workspace traversal，并限制单次读写字节数、最大 Agent 步数和 Trace 事件数。它不是通用编程 Agent Runtime，也不提供二进制文件、shell、network、browser、multimodal 或任意进程工具。`scripts/`、`templates/`、`assets/` 等 Skill 附属目录也不会由内置 Subject Adapter 冻结和物化；这些场景必须增加新的受信 Subject/Runtime/Optimizer 组件，或作为 D40 能力实现。

### 5.2 JSON 模型桥接

`CommandModelClient` 将模型供应商差异压缩到一个可信命令桥接。桥接命令通过 argv 启动，不经过 shell。

请求从 stdin 接收：

```json
{
  "messages": [{"role": "user", "content": "..."}],
  "tools": [{"name": "read_file", "parameters": {"type": "object"}}]
}
```

响应写入 stdout：

```json
{
  "content": "final answer or empty while calling tools",
  "tool_calls": [
    {"id": "call-id", "name": "read_file", "arguments": {"path": "input.txt"}}
  ],
  "usage": {"input_tokens": 100, "output_tokens": 30, "cost_usd": 0.01}
}
```

桥接进程负责调用实际模型 API、管理供应商凭证并映射 usage。EvalOps 只允许显式 `--model-env NAME` 指定的环境变量进入桥接进程，并始终提供 `PATH`。MVP 没有内置任何模型厂商 SDK，也没有公司 API 特例。

CLI 使用方式：

```bash
aceval run \
  --pack evalpacks/security-review \
  --subject examples/subjects/security-review-skill \
  --runtime reference \
  --model-command 'your-model-bridge --model your-model' \
  --model-env YOUR_API_KEY
```

### 5.3 FakeRuntime 的证据边界

`FakeRuntime` 根据测试脚本或 Scenario 的 `metadata.fake_runtime` 返回预注册输出。它验证的是：

- Pack 生命周期和组件契约；
- Grader 与门禁逻辑；
- baseline/candidate/validation/holdout 编排；
- 报告生成和错误路径。

它不执行模型，也不测量 Skill 的真实泛化能力。所有包含 FakeRuntime 的报告都会标记 `simulated=true`，其通过率不能作为真实模型质量数据。

## 6. EvalOps 工作流

### 6.1 单次评测

`run` 的执行顺序为：

```text
load + validate + freeze Pack
  -> snapshot + materialize Skill
  -> Driver.prepare fixture workspace
  -> Runtime.execute
  -> Driver.collect Observation
  -> Driver.cleanup
  -> registered Graders
  -> JSON/Markdown report
```

`run` 和 `compare` 默认只运行 dev；CLI 可显式选择 dev 和 validation，但不暴露 holdout split。`pack test` 使用 FakeRuntime 对 Pack 中已解析的 dev/validation 执行生命周期测试；holdout 只做静态加载与 hash 校验，不在 conformance 中执行。

### 6.2 对照评测

`compare` 在共享实验预算下分别运行 baseline 和 candidate，并按相同 Scenario ID 计算：

- hard pass rate 差值；
- 从 hard fail 变为 hard pass 的 improvement；
- 从 hard pass 变为非 hard pass 的 hard regression；
- 是否满足“无硬回退”的接受条件。

### 6.3 自动优化

`optimize` 当前只优化 `SKILL.md`：

1. 要求 Pack 为 legacy 或带内容锁的 frozen 状态；draft/calibrating 只能校准，不能改 Skill；
2. 运行 baseline dev；若出现 Runtime error、Grader `ERROR` 或 `NOT_EVALUABLE`，停止；
3. `auto` 根据 baseline 选择 repair 或 tune；
4. repair 只把 dev hard `FAIL` 交给 Optimizer，候选必须提高 hard pass rate 且无 hard regression；
5. tune 要求 baseline hard pass，只把 Goal、Objective 和 dev 测量交给 Optimizer；
6. tune 候选必须同时优于 parent 和初始 baseline，达到 `min_delta/target`，且单 Case 回退不超限；
7. 生成或加载候选后校验 base/patch/content hash、允许路径和候选根目录；
8. validation 不反馈给 Optimizer；tune 对 baseline/candidate 执行 paired Objective gate；
9. repair 最终只跑 candidate holdout；tune 最终跑一对 baseline/candidate holdout；
10. 原 Skill 永不覆盖，候选写入独立 hash 目录。

当前 Optimizer 可以通过同一 JSON bridge 调用模型，也可以使用 `FrozenCandidateOptimizer` 做确定性演示。Optimizer 不获得 validation 或 holdout Observation。

Optimizer 除 Registry `id` 外必须声明 `proposal_contract`：

- `aceval.optimizer/candidate-patch-v1`：公共 Optimizer 形态，直接接收冻结 `SubjectSnapshot`、dev diagnoses 和 `PatchConstraints`，返回 `CandidatePatch` 序列；
- `aceval.optimizer/skill-markdown-improver-v2`：内置 repair/tune 形态，返回完整 `SKILL.md`，再由 `SkillOptimizerBridge` 转换为 `candidate-patch-v1`；
- `aceval.optimizer/skill-markdown-generator-v1`：legacy repair 兼容形态。

缺失或未知 `proposal_contract` 会在候选生成前失败。第二种契约是当前 UTF-8 `SKILL.md` MVP 的便利层，不代表通用 Optimizer Protocol 支持任意文件树。

当前 `candidate-patch-v1` 只覆盖“完整候选文件快照 + UTF-8 文本 content + 单 entrypoint unified diff”。Kernel 从冻结 parent/candidate 重算 changed-files、Subject hash 和 diff；base/path/hash/diff 不一致会记账后以结构化 optimizer protocol error 停止，避免在没有校验反馈的情况下重复 beam。多文件/二进制 Candidate 的扩展点不能只是 Subject Adapter，D40 还必须提供并注册显式候选验证契约（例如 `verify_candidate_patch`）。

### 6.4 Manifest 参数传递

三类自由 `params` 在 Kernel 中有不同、受控的传递路径：

| Manifest 字段 | 传递路径 | 当前内置组件行为 |
|---|---|---|
| `subject_contract.params` | 传给 Subject Adapter 的每次 `snapshot()`、`materialize()`，包括执行前后完整性校验、候选校验和 Gate 间重验 | `skill_markdown_v1` 接受但忽略自定义值 |
| `driver.params` | 写入 Driver 专用 `RunContext.metadata.driver_params`，只传给 `Driver.prepare()` | 两个内置 workspace Driver 当前不消费；Runtime context 不包含该字段 |
| `optimizer_policy.params` | 写入 `PatchConstraints.metadata.optimizer_params` | `candidate-patch-v1` Optimizer 可解释；内置 Markdown improver 不消费 |

这些参数不会自动成为 Runtime、模型命令或任意代码执行配置。受信组件必须自己校验所支持的键和值；Pack 仍只能引用已注册组件。

### 6.5 预算

一个 `run`、`compare` 或 `optimize` 实验共享同一个预算 ledger。CLI 可限制：

- `--max-tokens`；
- `--max-cost-usd`；
- `--max-tool-calls`；
- Scenario timeout 与 Reference Runtime 模型命令 timeout。

usage 字段由模型桥接提供；缺失 usage 时，报告会将相关指标标记为未测量，而不是推测数值。

账本区分 `measured | partial | not_measured`：只有通过类型和有限数校验的 usage 才计为 measured；有效但已经超预算的调用仍会先记入实验总账，再停止后续执行。Optimizer proposal、rejected proposal 和 Runtime 异常携带的 partial usage 都进入同一实验 ledger。

Reference Runtime 会在工具执行前应用当前 Scenario 剩余的 tool-call 预算。Token 和费用只能在 bridge 返回 usage 后记账，因此 Kernel 可以停止后续 Case/候选，但不能撤销已经完成的单次模型调用；供应商侧的单调用上限由 bridge 负责。

### 6.6 EvalPack Builder 与独立校准循环

Builder 输入为 Skill 之外的少量 Case、Goal、可选 Objective 和 fixture。支持模板直接产生确定性 Grader/Oracle 结构；未知类型降级到 generic JSON draft。Goal 只对 Token、成本、工具调用、延迟做保守指标推断，主观质量不会自动生成自评 Judge。

```text
generate -> draft -> calibrating -> freeze + content lock
                                      |
                                      v
                               Skill repair/tune
```

生成 Pack 缺语义 Oracle 时仍可加载以便编辑，但 freeze fail closed。冻结锁覆盖 Case、Oracle、Grader、fixture、Schema、Objective 等全部文件；Pack 改动后必须新建版本并从 baseline 开始。这样即使 EvalPack 本身需要持续迭代，也不会和 Skill 在同一优化实验里共同漂移。

### 6.7 复杂 Skill 测试规划（Implemented）

现有 Builder 继续作为 EvalPack 编译器，不直接承担开放式 Skill 理解。新增规划面：

```text
Frozen Subject Snapshot
  -> deterministic source-grounded inventory
  -> source-grounded Capability Graph
  -> risk-weighted Test Requirements
  -> bounded Case drafts + preserved seed fixtures/Oracles
  -> Runtime/Driver/Grader feasibility
  -> Coverage + Pack Quality Report
  -> Pack Builder
```

关键契约包括：

- `CapabilityGraph`：能力、步骤、分支、依赖、工具、状态、副作用、风险和 source refs；
- `TestRequirement`：路径类型、风险、预期观察、Oracle 策略和 Runtime capability；
- `CoverageReport`：planned、executable、oracle-ready 和 observed coverage，均包含计数与风险权重分子/分母；
- `PlanningArtifacts`：Capability Graph、Test Plan、Coverage、Case drafts、Runtime gaps 和 generation provenance 的一致性载体。

当前 Analyzer/Planner 不调用模型。Analyzer 只接受冻结 UTF-8 `SKILL.md`，对 heading、显式工具、条件、步骤、输入输出、风险和状态文本生成带行号与 quote hash 的节点；无法可靠确定的内容形成 ambiguity 或 `inferred` 标记。Planner 使用确定性风险权重和 bounded greedy selection 映射种子 Case、补充 requirement-synthesis Case，并把不支持的能力保留为 Runtime gap。生成 Case 的语义 Oracle 默认停在 calibration，不能自动成为 hard gate；同源生成 Case 不能伪装成独立 sealed holdout。

“覆盖”限定为声明能力、关键风险、工具依赖和状态转换的可追踪覆盖。报告必须展示分子、分母、不可执行/不可观察项和 waiver，不宣称穷举任意自然语言路径。

### 6.8 故障归因与修改授权（Implemented）

`_has_non_skill_failure()` 的 fail-closed 语义保留，并优先使用结构化 DiagnosticReport：

```text
Observation / Trace / Grade / Imported Session
  -> completeness and integrity
  -> deterministic diagnostic signals
  -> failure classification
  -> Failure Card
  -> Skill Patch Authorization
```

Failure Card 分开表达：

- `observed_component`：故障在哪个阶段被观察到；
- `category/reason_code` 与 `alternative_hypotheses`：基于证据的分类和可证伪替代解释；
- `remediation_surface`：建议修改 Skill、Agent、Runtime、CLI、环境还是 evaluator；
- `patch_decision`：允许、拒绝、需要更多证据或无干预；
- `skill_patch_authorized`：只有允许 Skill intervention 时才为 true 的派生字段。

Runtime、Driver、fixture、Grader、Oracle、missing evidence、CLI binary、permission、authentication 和 network 故障默认禁止修改 Skill。当前规则对 unknown tool/bad argument 保持 `needs_more_evidence`；只有 Observation 可评、无外部阻断的 dev hard `FAIL` 才允许受约束 Skill intervention。证据不足一律 fail closed。

修改授权路径完全由确定性规则执行。Orchestrator 为每个 Scenario 保存 DiagnosticReport，只把 `eligible_skill_failures` 摘要附到 dev FailureEvidence；validation/holdout 诊断仍不得反馈给 Optimizer。未来模型可以解释 Failure Card 或提出探针，但不能提升证据等级、覆盖确定性分类或独立授权 Patch。

完整 Schema、CLI、受控 Process Tool、Session Replay 和 D20/D40 验收见 [COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md](./COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md)。

## 7. 技术选型（Implemented）

| 层次 | 当前选择 |
|---|---|
| 语言 | Python 3.9+ |
| 构建 | setuptools |
| 数据模型 | stdlib `dataclasses` + `Enum` + `Protocol` |
| CLI | stdlib `argparse` |
| 异步编排 | stdlib `asyncio` |
| 配置解析 | JSON；安装可选 `PyYAML` 后支持 YAML |
| Schema 评分 | 内置受限 JSON Schema 实现 |
| 存储 | 每次运行的本地目录；无数据库 |
| 报告 | JSON + Markdown 文件 |
| 测试 | stdlib `unittest`；`pytest` 仅为可选开发依赖 |

核心运行路径没有第三方依赖。`PyYAML` 在 `pyproject.toml` 中是可选依赖；当前内置 Pack 使用 JSON 语法，因此不需要安装它即可运行。

## 8. 模块映射（Implemented）

| 文件 | 职责 |
|---|---|
| `src/aceval/contracts.py` | dataclass、枚举和公共 Protocol |
| `src/aceval/agent_runtime.py` | Agent loop、工具执行和 JSON 模型桥接 |
| `src/aceval/runtime.py` | Reference/Fake Runtime Adapter 和 RuntimeResult 标准化 |
| `src/aceval/pack.py` | Manifest/Scenario 解析、路径校验、hash 和 Registry |
| `src/aceval/pack_builder.py` | Case/Goal 或 PlanningArtifacts 到 draft Pack、设计 Sidecar、质量门禁与显式冻结 |
| `src/aceval/pack_lifecycle.py` | 生命周期状态、逐文件 freeze lock 与篡改检测 |
| `src/aceval/objectives.py` | Tune Objective 提取、聚合和 paired comparison |
| `src/aceval/connections.py` | 公司 API Profile、Session fetch/import、保存结果重载与 Observation completeness |
| `src/aceval/skill_analysis.py` | 确定性 Subject inventory、Capability Graph 和 source-ref 校验 |
| `src/aceval/test_planning.py` | 风险加权 Test Requirement、seed mapping、Case budget 和 Runtime gaps |
| `src/aceval/case_generation.py` | 将 TestPlan 编译为可编辑 Case 草稿并保留 provenance |
| `src/aceval/coverage.py` | planned/executable/oracle-ready/observed Coverage Matrix |
| `src/aceval/pack_quality.py` | design cross-reference、Oracle trust、coverage/runtime/split freeze blocker |
| `src/aceval/planning_workflow.py` | `plan` 文件工作流和版本化规划产物一致性检查 |
| `src/aceval/failure_attribution.py` | Diagnostic Signal、Failure Card 和 Patch Authorization |
| `src/aceval/subjects.py` | Skill 快照和物化 |
| `src/aceval/drivers.py` | fixture workspace 和 artifact/state 收集 |
| `src/aceval/graders.py` | 七个确定性 Grader |
| `src/aceval/optimizer.py` | 模型候选、冻结候选和 Patch 约束 |
| `src/aceval/orchestrator.py` | evaluate/compare/optimize、预算和门禁 |
| `src/aceval/reporting.py` | 确定性 JSON/Markdown 报告 |
| `src/aceval/cli.py` | argparse CLI |

### 8.1 D40 计划新增模块

| 文件 | 职责 |
|---|---|
| `src/aceval/diagnostic_probes.py` | 重复执行、health check、Mock/replay 探针 |
| `src/aceval/replay.py` | Imported Session 到 EvalRun/Grader replay |
| `src/aceval/process_tool.py` | D40 argv-only、allowlisted CLI 工具 |

## 9. 报告与运行目录（Implemented）

默认输出根目录为 `.aceval/runs`。每个 EvalRun 或 Optimization 会创建独立目录，保存物化 Subject 和最终报告；Scenario workspace 位于运行目录下，并在收集 Observation 后清理。

CLI 当前写出：

```text
<run-dir>/report.json
<run-dir>/report.md
```

报告 Schema 为 `aceval.report/v1`，包含 split 通过率、硬门禁结果、对照 uplift、回归数、Token/成本/耗时/工具调用的测量状态、候选门禁、诊断聚合和限制说明。Markdown Run 报告展示每个 Scenario 的 Patch Decision、Failure Card 数和 blocked reason。优化报告显式记录 mode、Goal、Objective baseline/candidate 值、signed improvement、逐 Case 回退与 validation attempts。Repair 的 holdout 批次数为 `0/1`；Tune 另外记录 baseline/candidate 的 `holdout_pair_count=0/1`。单次 Case 执行只形成样本结果，不声明统计显著。

默认 summary 报告不会嵌入 Observation、Trace、artifact 内容、评分证据正文或 Patch 正文；它只保留必要的计数、hash 和元数据。这降低了报告泄漏与体积风险，但当前还没有独立 Trace store、CAS 或 Replay。

## 10. 安全与隔离边界（Implemented）

当前已实现：

- Pack 全树拒绝 symlink，所有本地引用拒绝绝对路径和 `..`；
- Pack 加载时冻结文件 hash，执行前重新校验完整性；
- Builder Pack 未冻结时禁止 Skill optimize；frozen Pack 的独立内容锁阻止跨加载篡改；
- 每个 Scenario 使用 Driver 创建的新 workspace，结束后清理；
- Runtime 收到 prompt、准备后的 workspace 和运行上下文，不收到 Oracle 或 Pack 根路径；
- Driver 收到 gold-free Scenario view 与 Case 专属 fixture root；Scenario ID 不直接参与目录拼接；
- Prepared/Runtime/Observation/Grade 容器在组件边界深冻结，避免 Runtime 篡改 baseline 或 Grader 顺序造成证据漂移；
- workspace 与 runtime/in-memory artifact 统一执行文件数、单文件和总字节上限；
- Skill 物化到运行目录，执行前后校验内容未被 Runtime 修改；
- Reference Runtime 文件工具限制在 workspace 内；
- 模型桥接使用 argv 而非 shell，并使用环境变量白名单；
- 候选只允许修改 Pack policy 声明的 `SKILL.md`，并验证 lineage/hash；
- 普通 `run/compare` CLI 无法选择 holdout；holdout 由 optimize 的最终门禁触发；
- Runtime/Grader 失败不会伪装为 Skill 失败并触发修改。

最终通过门禁的候选会从冻结快照独立物化到 `selected-candidate`，重验并记录 `selected_candidate_hash`；内置 Skill Adapter 的 provenance marker 不参与 Subject hash，但保证该交付目录可作为 candidate 独立重跑。

当前边界不是恶意代码沙箱：模型桥接命令是操作者信任的本地进程，所有组件仍运行在同一 OS 用户下，Pack 文件也存在于同一仓库。MVP 提供协议隔离和路径约束，不宣称提供对恶意桥接、恶意扩展或宿主进程的强安全隔离。

## 11. CLI（Implemented）

```bash
# Pack 静态加载、hash 和组件引用校验
aceval pack lint evalpacks/security-review

# Case + Goal -> draft -> calibration -> frozen
aceval pack generate --type generic --cases cases.json \
  --goal '保持正确并减少 token' --output .aceval/packs/demo
aceval pack calibrate .aceval/packs/demo
aceval pack freeze .aceval/packs/demo --approve

# 复杂 Skill + 种子 Case -> Test Plan -> Pack Quality -> frozen
aceval plan --subject ./my-skill --cases cases.json \
  --goal '保持正确并减少 token' --runtime-profile reference \
  --output .aceval/plans/demo
aceval pack generate --plan .aceval/plans/demo \
  --type generic --output .aceval/packs/planned-demo
aceval pack quality .aceval/packs/planned-demo

# FakeRuntime 生命周期/conformance 测试
aceval pack test evalpacks/security-review --runtime fake

# 单 Subject 评测；默认 dev
aceval run \
  --pack evalpacks/security-review \
  --subject examples/subjects/security-review-skill \
  --runtime fake

# baseline/candidate 配对比较
aceval compare \
  --pack evalpacks/security-review \
  --subject examples/subjects/security-review-skill \
  --candidate examples/subjects/security-review-skill-candidate \
  --runtime fake \
  --split dev --split validation

# 冻结候选的确定性优化链路演示
aceval optimize \
  --pack evalpacks/security-review \
  --subject examples/subjects/security-review-skill \
  --candidate examples/subjects/security-review-skill-candidate \
  --runtime fake

# 一体化入口；根据 baseline 自动 repair/tune
aceval doctor --subject ./my-skill --cases cases.json --type generic \
  --goal '保持正确并减少 token' --approve-pack \
  --runtime reference --model-command 'python model_bridge.py'

# 公司 API 配置与 Session 日志归一化
aceval profile validate company-profile.json
aceval session fetch --profile company-profile.json --session-id ID \
  --output .aceval/imported/session.json
aceval session diagnose --input .aceval/imported/session.json \
  --output .aceval/imported/session-diagnosis.json
```

将 `--runtime fake` 换成 `--runtime reference --model-command '...'` 才会执行真实模型。FakeRuntime 命令只展示评测内核和门禁行为。

## 12. 两个 MVP EvalPack

### 12.1 `security-review`

- 6 dev + 2 validation + 2 holdout；
- repository fixture 输入；
- JSON final output；
- Schema、record matching、source reference 和 Trace 断言；
- 最多 2 个 beam、2 轮和 4 个候选快照；
- 作为黑客松主 Demo，展示完整门禁链路。

### 12.2 `csv-summary-smoke`

- 2 dev + 1 validation；
- CSV fixture 输入与 `summary.json` artifact 输出；
- artifact、Schema、JSON path 和 workspace diff 评分；
- 1 个 beam、1 轮和 1 个候选快照；
- 用于证明不同输入/输出形态可复用同一 Kernel。

两个 Pack 都包含 FakeRuntime 的预注册行为，以便零模型 conformance 和演示。它们的 FakeRuntime 结果不是 Benchmark 结果。

## 13. MVP 验收标准

MVP 的工程验收是：

1. 两个 Pack 均能 `lint`，未知 API version 或组件 fail closed；
2. 两个 Pack 均能通过 FakeRuntime 生命周期测试；
3. Reference Runtime 能通过 JSON bridge 完成至少一个真实 Case；
4. Runtime/Grader 不可评测错误会阻止优化；
5. 候选不能修改原 Skill、Case、Oracle、Grader 或预算；draft/calibrating Pack 不能启动 Skill 优化；
6. repair/tune 均不从 validation 生成候选；tune validation/holdout 必须 paired；
7. 报告明确标注 simulated、未测量指标和跨 Runtime 限制；
8. CSV Pack 不要求安全审查领域分支；
9. Optimizer 缺失/伪造 proposal contract、参数越界转发和候选内容不一致均 fail closed；
10. 公司 Session 缺 output/trace/usage 映射或出现非法 telemetry 时 fail closed，不猜测缺失值；
11. `plan` 产物具有稳定 Subject/source ref、风险 Requirement、Runtime gap、Coverage 和 Freeze Blocker；
12. 带设计 Sidecar 的 Pack 未通过 Quality Gate 时不能冻结；
13. Runtime/CLI/Grader/缺证据 Failure Card 不授权 Skill Patch，只有可评 dev hard failure 可进入 Optimizer；
14. Imported Session 可离线诊断，但不会被包装成完整 EvalRun/Replay。

真实模型的质量提升、成本和稳定性必须通过 Reference Runtime 实验另行测量，不能由 FakeRuntime 通过率替代。

## 14. D40 规划（Planned）

D20 已前置完成 repair/tune、复杂 Skill 规划与静态覆盖、基础 Pack Quality、Failure Attribution、Doctor、公司 Profile/Session Import/离线 Diagnosis 和 paired gates。D40 的目标因此调整为：用标注 Benchmark 和真实实验增强这些能力，并补齐高级 Pack 校准、现实 Agent 在线 Replay、受控 Process Tool 和本地可视化操作台，同时保持 D20 契约兼容。

跨模块的详细排期、待办和完成定义以 [ROADMAP.md](./ROADMAP.md) 为准；复杂 Skill 与归因设计见 [COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md](./COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md)；Console 设计以 [VISUAL_CONSOLE_DESIGN.md](./VISUAL_CONSOLE_DESIGN.md) 为准。

### 14.1 优先能力

1. **真实实验基线**：冻结模型、参数、环境和预算，完成真实 repair/tune、多次采样和消融。
2. **Planner/Attribution Benchmark**：标注复杂 Skill 与 failure fixtures，测量 source-ref、Requirement recall、分类和保守拒判。
3. **Case 与动态覆盖增强**：seed expansion、boundary/metamorphic fixture、Session mining 和 Run-to-coverage 自动接线。
4. **高级 EvalPack Quality**：known-good/known-bad 区分能力、mutation score、evaluator flake 和 revision diff。
5. **公司 Agent 闭环**：CompanyRuntimeAdapter、Imported Session -> EvalRun、Grader Replay、Diagnosis 与 Case 草稿。
6. **受控 CLI 与诊断探针**：argv-only Process Tool、allowlist、结构化错误、重复和只读健康检查。
7. **Application Service 与 Console**：先静态 HTML，再实现本地 Read-only/Operational Console；UI 只调用 Service，不复制 Kernel 语义。
8. **统计、迁移与 CI**：p50/p95、方差、flake、FixedAgentTarget、Run index、Replay 和 CI Gate。

### 14.2 D21–D40 建议节奏

| 阶段 | 交付 |
|---|---|
| D21–D24 | 真实 Benchmark；Planner/Attribution 标注集与 detailed diagnostic report |
| D25–D28 | seed/metamorphic Case、高级 Pack Quality、Application Service、静态 HTML |
| D29–D32 | CompanyRuntimeAdapter、Session EvalRun/Grader Replay 与 Case mining |
| D33–D35 | 受控 Process Tool、CLI 分类和诊断探针 |
| D36–D38 | 重复执行统计、动态覆盖、Operational Console、主观 Judge Pilot |
| D39–D40 | FixedAgentTarget、CI、教程、视频、消融和版本化 Release |

### 14.3 D40 仍不承诺

- 覆盖所有 Agent 平台；
- 数学意义的任意自然语言全路径覆盖；
- 任意 JSON 日志都能完整重评分；
- 从单次日志证明唯一强因果根因；
- 允许 Pack 自带自由 shell 或任意命令；
- 对恶意第三方插件提供完善沙箱；
- 自动生成并直接信任 Oracle；
- 无人工审批自动发布 Skill/Agent；
- 多租户 SaaS、RBAC、计费和分布式调度。

## 15. 可迁移价值

即使未来 Skill 这种封装形式变化，项目的核心经验仍可迁移：

- 版本化 Subject 与运行配置；
- Case/Oracle/Observation 分离；
- Runtime capability contract；
- Capability Graph、风险驱动 Test Requirement 和 requirement-to-case traceability；
- Oracle 可信级别、Pack Quality 和动态覆盖；
- Canonical Trace 与完整度检查；
- evidence-backed Failure Card、补救面和修改授权；
- 确定性 Grader 和结构化 Judge 校准；
- dev/validation/holdout 信息边界；
- 候选 lineage、回归门禁和预算控制；
- 跨 Runtime 的可复现性与外部有效性分析。

Skill 只是当前最小、可控的优化表面；EvalOps Kernel 才是长期资产。

## 16. 测试与复现

当前测试基于 stdlib `unittest`：

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPYCACHEPREFIX=/tmp/aceval-pycache \
PYTHONPATH=src \
python3 -m unittest discover -s tests -v
```

发布前还应运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m compileall -q src tests
git diff --check
```

## 17. 最终取舍

20 天黑客松版本负责证明：

> 一个项目内置的 Reference Agent Runtime，可以驱动通用 EvalPack 生命周期；系统能从当前支持面内的复杂 Skill 和少量种子 Case 生成可审计测试计划，明确覆盖/Runtime 缺口，并将可用 Skill 干预的行为失败与 CLI、Runtime、Grader 等外部故障分开，再执行受控候选生成和回归门禁。

40 天完整版本负责进一步证明：

> 同一核心可以把 Skill、Agent 或 Workflow 转换为有来源和覆盖声明的测试契约，接收真实 Runtime 运行和外部 Session 日志，通过证据化归因选择正确干预面，并在不可变评测 revision 下持续优化，而不是把当前 `SKILL.md` Runtime 夸大为任意平台适配层。
