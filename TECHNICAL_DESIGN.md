# Agent Capability EvalOps 技术方案

> 黑客松展示名：**Skill Doctor**
> 长期项目名：**Agent Capability EvalOps**
> CLI/包名：`aceval`
> 文档状态：MVP 实现基线
> 基线日期：2026-08-16

## 1. 项目结论

项目采用“**通用评测内核 + 内置参考 Agent Runtime + 声明式 EvalPack**”架构。

MVP 已经不是只针对安全代码审查的单一工作流：在“UTF-8 `SKILL.md` + 固定 UTF-8 文件工具”这一当前支持面内，安全审查和 CSV 汇总两个不同输入/输出形态的 Skill 共用同一套 Subject、Runtime、Driver、Grader、Optimizer、门禁和报告契约。新增任务仍然需要用户提供 Case、fixture 和可验证的成功标准；Kernel 不会从未知任务中自动发明 Oracle。

核心价值主张是：

> 将“用户转发失败日志、让 Agent 修改 Skill、再手工重测”的循环，变成可复现、受预算约束、具有 dev/validation/holdout 门禁且不覆盖原 Skill 的自动评测与迭代流程。

本方案刻意内置一个稳定 Runtime，而不是在 MVP 适配所有 Agent 平台。当前分数只代表该 Reference Runtime 和对应模型桥接配置下的结果；它可以验证 Skill 的相对改进，但不假设不同 Agent 平台必然得到相同绝对效果。

## 2. 状态标识

本文用以下两个状态区分实现与规划：

- **Implemented（MVP）**：当前仓库已有代码和测试，可通过 CLI 或公共 Python 契约运行。
- **Planned（D40）**：求职作品阶段计划，不属于当前能力，也不能用于描述当前交付。

## 3. MVP 已实现范围

| 能力 | 当前实现 |
|---|---|
| Subject | `skill_markdown_v1`，仅冻结 UTF-8 `SKILL.md` 与可选 UTF-8 JSON `subject.json` |
| Agent Runtime | 项目内置 `ReferenceAgentRuntime`，固定 UTF-8 文件工具 |
| 模型接入 | `CommandModelClient`，JSON stdin/stdout 命令桥接 |
| 模拟 Runtime | `FakeRuntime`，仅用于 conformance、单元测试和离线演示 |
| EvalPack | `aceval.dev/v1alpha1`，本地声明式加载、校验和全树 hash |
| Driver | `repository_workspace`、`artifact_workspace` |
| Grader | `json_schema`、`json_path`、`record_match`、`artifact_exists`、`source_reference`、`trace_assert`、`workspace_diff` |
| Optimizer | `skill_markdown_v1`，只生成或加载 UTF-8 `SKILL.md` 候选；显式声明 proposal contract |
| 门禁 | dev 筛选、validation 晋级、最终 holdout 一次性门禁 |
| 预算 | 实验级 Token、成本、工具调用和墙钟预算 ledger |
| 报告 | 版本化 JSON 和 Markdown 文件 |
| CLI | `pack lint`、`pack test`、`run`、`compare`、`optimize` |
| 示例 Pack | `security-review` 与 `csv-summary-smoke` |

### 3.1 当前明确未实现

- 公司内部 Agent API/Session Adapter；
- Codex、Claude Code 或其他第三方 Agent 平台 Adapter；
- `AgentSubject`、`FixedAgentTarget` 和 Agent 配置自动优化；
- 外部 Session 日志导入、Replay 命令和完整度矩阵；
- SQLite、CAS、Run Repository、跨进程恢复和结果缓存；
- Pydantic、Typer、Jinja2、HTML 报告或 Web UI；
- LLM Judge/`llm_rubric`；
- `pack init`、`report`、`replay`、`--extension` 等 CLI；
- `scripts/`、`templates/`、`assets/` 等多文件 Skill bundle、二进制 Subject 和二进制工具；
- shell、network、browser、multimodal Agent 工具面；
- Docker/Podman 沙箱、独立权限 Worker 和真正的 holdout 权限隔离；
- GitHub Action、在线服务和多租户控制面。

`src/aceval/runtime.py` 中存在库级 `SubprocessRuntime` 辅助实现，但它没有暴露为当前 CLI 的受支持 Runtime，也不等同于公司 Agent API Adapter。MVP 的真实模型执行路径是 `ReferenceAgentRuntime`。

## 4. 总体架构

```text
                      aceval CLI
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
               dev -> validation -> holdout
                          |
                  JSON + Markdown report
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

1. 运行 baseline dev；
2. 若出现 Runtime error、Grader `ERROR` 或 `NOT_EVALUABLE`，停止并保持原 Skill；
3. 只把 dev 中 hard `FAIL` 的摘要和证据交给 Optimizer；
4. 生成模型候选，或加载 `--candidate` 指定的冻结候选；
5. 校验 base hash、patch hash、候选实际内容 hash、允许路径和候选根目录；
6. 候选在 dev 有正向提升、无硬回退且全部 hard gate 通过后才晋级；
7. validation 仅用于晋级，不把结果反馈给 Optimizer；
8. 选中的最终候选最多运行一个 holdout 批次；
9. holdout 失败即结束当前实验，不继续修改；
10. 原 Skill 永不覆盖，候选写入独立 hash 目录。

当前 Optimizer 可以通过同一 JSON bridge 调用模型，也可以使用 `FrozenCandidateOptimizer` 做确定性演示。Optimizer 不获得 validation 或 holdout Observation。

Optimizer 除 Registry `id` 外必须声明 `proposal_contract`：

- `aceval.optimizer/candidate-patch-v1`：公共 Optimizer 形态，直接接收冻结 `SubjectSnapshot`、dev diagnoses 和 `PatchConstraints`，返回 `CandidatePatch` 序列；
- `aceval.optimizer/skill-markdown-generator-v1`：内置 Skill Markdown 生成器兼容形态，返回完整 `SKILL.md`，再由 `SkillOptimizerBridge` 转换为 `candidate-patch-v1`。

缺失或未知 `proposal_contract` 会在候选生成前失败。第二种契约是当前 UTF-8 `SKILL.md` MVP 的便利层，不代表通用 Optimizer Protocol 支持任意文件树。

当前 `candidate-patch-v1` 只覆盖“完整候选文件快照 + UTF-8 文本 content + 单 entrypoint unified diff”。Kernel 从冻结 parent/candidate 重算 changed-files、Subject hash 和 diff；base/path/hash/diff 不一致会记账后以结构化 optimizer protocol error 停止，避免在没有校验反馈的情况下重复 beam。多文件/二进制 Candidate 的扩展点不能只是 Subject Adapter，D40 还必须提供并注册显式候选验证契约（例如 `verify_candidate_patch`）。

### 6.4 Manifest 参数传递

三类自由 `params` 在 Kernel 中有不同、受控的传递路径：

| Manifest 字段 | 传递路径 | 当前内置组件行为 |
|---|---|---|
| `subject_contract.params` | 传给 Subject Adapter 的每次 `snapshot()`、`materialize()`，包括执行前后完整性校验、候选校验和 Gate 间重验 | `skill_markdown_v1` 接受但忽略自定义值 |
| `driver.params` | 写入 Driver 专用 `RunContext.metadata.driver_params`，只传给 `Driver.prepare()` | 两个内置 workspace Driver 当前不消费；Runtime context 不包含该字段 |
| `optimizer_policy.params` | 写入 `PatchConstraints.metadata.optimizer_params` | `candidate-patch-v1` Optimizer 可解释；内置 `skill-markdown-generator-v1` 不消费 |

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
| `src/aceval/subjects.py` | Skill 快照和物化 |
| `src/aceval/drivers.py` | fixture workspace 和 artifact/state 收集 |
| `src/aceval/graders.py` | 七个确定性 Grader |
| `src/aceval/optimizer.py` | 模型候选、冻结候选和 Patch 约束 |
| `src/aceval/orchestrator.py` | evaluate/compare/optimize、预算和门禁 |
| `src/aceval/reporting.py` | 确定性 JSON/Markdown 报告 |
| `src/aceval/cli.py` | argparse CLI |

## 9. 报告与运行目录（Implemented）

默认输出根目录为 `.aceval/runs`。每个 EvalRun 或 Optimization 会创建独立目录，保存物化 Subject 和最终报告；Scenario workspace 位于运行目录下，并在收集 Observation 后清理。

CLI 当前写出：

```text
<run-dir>/report.json
<run-dir>/report.md
```

报告 Schema 为 `aceval.report/v1`，包含 split 通过率、硬门禁结果、对照 uplift、回归数、Token/成本/耗时的测量状态、候选门禁和限制说明。优化报告显式记录 holdout 批次数为 `0/1`；由于 baseline 不执行 holdout，成对的 `hidden_regression_rate` 为 `null/not_measured`。工具调用用于预算 ledger，但 MVP summary 报告尚未提供独立的工具调用汇总字段。

默认 summary 报告不会嵌入 Observation、Trace、artifact 内容、评分证据正文或 Patch 正文；它只保留必要的计数、hash 和元数据。这降低了报告泄漏与体积风险，但当前还没有独立 Trace store、CAS 或 Replay。

## 10. 安全与隔离边界（Implemented）

当前已实现：

- Pack 全树拒绝 symlink，所有本地引用拒绝绝对路径和 `..`；
- Pack 加载时冻结文件 hash，执行前重新校验完整性；
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
5. 候选不能修改原 Skill、Case、Oracle、Grader 或预算；
6. validation 不产生新候选，holdout 只运行最终候选；
7. 报告明确标注 simulated、未测量指标和跨 Runtime 限制；
8. CSV Pack 不要求安全审查领域分支；
9. Optimizer 缺失/伪造 proposal contract、参数越界转发和候选内容不一致均 fail closed。

真实模型的质量提升、成本和稳定性必须通过 Reference Runtime 实验另行测量，不能由 FakeRuntime 通过率替代。

## 14. D40 规划（Planned）

D40 的目标是把当前 Skill MVP 扩展为可用于求职展示的 Agent EvalOps 项目，同时保持 D20 契约可兼容。

### 14.1 优先能力

1. **真实实验基线**：选择一个模型 bridge，冻结模型/参数/环境，重复关键 Case 并报告波动、成本和失败率。
2. **Session 日志导入**：定义 `ImportedRunBundle` 和 completeness flags，将公司 Agent 的消息、工具调用、usage 和 artifact 映射为 Canonical Observation。
3. **Agent target 契约**：Runtime 能原子加载并回报 Agent 配置 hash 时支持 `AgentSubject`；否则只注册 `FixedAgentTarget`，不做虚假的版本归因。
4. **持久化与 Replay**：增加版本化 Run manifest、Trace/artifact store 和确定性 Replay；具体存储可以从文件 manifest 起步，是否使用 SQLite 由实现验证决定。
5. **受限 Worker**：加强进程身份、目录权限、资源限制和 evaluator-only 数据边界；容器化是条件能力。
6. **受信能力扩展**：为多文件 Skill bundle、二进制 artifact，以及 shell/network/browser/multimodal 需求定义新的 Subject/Runtime/Optimizer、候选验证扩展契约和 capability，不扩大内置 Runtime 的隐含权限。
7. **可信扩展入口**：由操作者显式注册新 Subject/Driver/Grader/Runtime/Optimizer，不允许 Pack Manifest 静默执行代码。
8. **Benchmark 与消融**：12–20 个冻结 Case，关键 Case 至少 3 次；完成 output-only 与 trace-aware 诊断对照。
9. **Judge 校准**：只有完成人工标注与 agreement 测量后，才加入结构化 LLM Judge。
10. **CI**：零模型执行 Pack lint、单元测试和确定性 Replay；在线模型评测单独受凭证和预算控制。

### 14.2 D21–D40 建议节奏

| 阶段 | 交付 |
|---|---|
| D21–D24 | 冻结 MVP Schema，补 Reference Runtime 真实 E2E、重复执行和能力矩阵 |
| D25–D28 | `ImportedRunBundle`、公司 Session 日志映射、完整度门禁 |
| D29–D31 | AgentSubject/FixedAgentTarget 边界、可信扩展入口 |
| D32–D34 | Run manifest、Trace/artifact 持久化、Replay、受限 Worker |
| D35–D37 | 12–20 Case Benchmark、统计聚合和 Trace-aware 消融 |
| D38–D40 | CI、教程、演示视频、限制说明和版本化 Release |

### 14.3 D40 仍不承诺

- 覆盖所有 Agent 平台；
- 任意 JSON 日志都能完整重评分；
- 对恶意第三方插件提供完善沙箱；
- 自动生成并直接信任 Oracle；
- 无人工审批自动发布 Skill/Agent；
- 多租户 SaaS、RBAC、计费和分布式调度。

## 15. 可迁移价值

即使未来 Skill 这种封装形式变化，项目的核心经验仍可迁移：

- 版本化 Subject 与运行配置；
- Case/Oracle/Observation 分离；
- Runtime capability contract；
- Canonical Trace 与完整度检查；
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

> 一个项目内置的 Reference Agent Runtime，可以驱动通用 EvalPack 生命周期，对两类 UTF-8 文件任务执行评测、`SKILL.md` 候选生成、回归门禁和报告；扩展边界与未支持工具面均有明确声明。

40 天求职版本负责进一步证明：

> 同一核心可以接收真实 Runtime 运行和外部 Session 日志，并通过显式受信组件、proposal contract、capability、完整度和安全边界扩展到 Agent 评测，而不是把当前 `SKILL.md` Runtime 夸大为任意平台适配层。
