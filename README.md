# Skill Doctor

> Agent Capability EvalOps: 在一个可控的参考 Agent 环境中，对 Skill 做可复现评测、成对比较和受控迭代。

当前状态：仓库已经包含可运行的 `aceval 0.2.1` 参考实现、两个手写 EvalPack、复杂 Skill 规划与覆盖链路、Pack Builder/Quality Gate、Failure Attribution、公司 Session 导入与离线诊断、示例 Skill、命令行工具和自动测试。仓库中的 FakeRuntime 结果是确定性模拟，只用于验证 Kernel、Pack、Grader 和门禁流程；它们不是实际模型效果或提升数据。

## 一句话介绍

Skill Doctor 把“人工运行 Skill -> 搬运会话日志 -> 请 Agent 分析 -> 修改 Skill -> 手工回归”的过程，收敛为一套受确定性工作流控制的 EvalOps 系统：加载冻结测试契约，在统一 Runtime 中执行 Skill，采集标准 Trace、产物和 usage；baseline 有硬失败时进入 `repair`，baseline 已可用时按明确 Objective 进入 `tune`，再经过 validation 与可选 holdout 门禁输出候选和证据报告。

## 项目背景

初版 Skill 通常由 Agent 根据 Prompt 直接生成。用户随后建立 Case、运行 Skill、发现失败，再把输出和工具调用日志交给另一个 Agent 分析。这个流程有几个长期问题：

1. 用户在多个 Agent 和环境之间充当消息中转者；
2. 测试、失败证据和修复没有自然沉淀为可重复执行的资产；
3. 模型波动、工具、权限、环境和评测标准问题容易被误判为 Skill 问题；
4. 修改可能只记住少量 Case，缺少 validation 和 holdout 回归门禁；
5. 不同任务反复搭建专用脚本，难以复用执行、评分和报告能力。

Skill Doctor 的目标不是只优化“安全代码审查”这一种 Skill，而是提供稳定的 Reference Agent Runtime、可复用的 EvalOps 生命周期和声明式 EvalPack。安全审查与 CSV 汇总只是两种不同输入输出形态的验证样例。这里的“通用”指 Pack 加载、执行、Observation、评分和门禁状态机不包含领域分支；它不表示当前内置 Subject、Runtime 和 Optimizer 已覆盖任意 Skill 形态。

## 当前实现

### Reference Agent Runtime

项目内置自己的最小无头 Agent Runtime，用固定语义执行被测 Skill：

- 通过内置 `skill_markdown_v1` Adapter 加载 UTF-8 `SKILL.md`，目录模式可额外包含 UTF-8 JSON `subject.json`；
- 将 Skill 指令和 Case Prompt 组成模型消息；
- 执行有最大步数限制的模型/工具循环；
- 只向模型暴露 `list_files`、UTF-8 `read_file`、UTF-8 `write_file`；
- 限制工作区路径逃逸、单次读写字节数和 Trace 事件数；
- 产生 `model_call`、`tool_call`、`tool_result`、`message` 事件，并由 Adapter 转为 Canonical Trace；
- 通过 `CommandModelClient` 接入任意符合 JSON stdin/stdout 协议的模型桥接程序。

这个 Runtime 的目的不是复刻某家 Agent 平台，而是给当前支持面内的 Skill 优化提供一个稳定、可审计、可重复控制的实验环境。内置链路不会快照或物化 `scripts/`、`templates/`、`assets/` 等附属目录，也不支持多文件或二进制 Subject，以及 shell、network、browser、multimodal 工具。需要这些能力时，应实现并显式注册新的受信 Subject/Runtime/Optimizer 组件，或在 D40 阶段扩展；不能只靠新增 EvalPack 获得。

### 通用 EvalOps Kernel

Kernel 通过公共 Contract 和 Registry 组织固定生命周期：

```text
EvalPackLoader + Registry
          |
          v
Subject snapshot/materialize
          |
          v
Driver.prepare -> Runtime.execute -> Driver.collect -> Graders
          |                                  |
          |                                  v
          |                         RunObservation + Canonical Trace
          v
baseline / compare / dev candidate search
          |
          v
validation promotion -> optional one-shot holdout -> report
```

Kernel 负责 Pack 完整性校验、Subject 快照、Runtime capability 检查、工作区生命周期、预算、评分聚合、候选 lineage 和回归门禁。领域知识位于 EvalPack 的 Scenario、fixture、Oracle、Grader 配置和 Optimizer policy 中，Orchestrator 不包含安全审查或 CSV 的条件分支。

当前内置 Optimizer 只修改 UTF-8 `SKILL.md`，不会覆盖原 Skill。候选受允许路径、最大新增行数、最大候选数、父版本 hash 和测试字面量泄漏检查约束。`repair` 只能接收 dev 的确定性硬失败证据；`tune` 只能接收 dev 的目标与测量值。validation/holdout 从不反馈给 Optimizer。Tune 在 dev、validation 和 holdout 上都保持 hard Grader 非劣，并对 baseline/candidate 做成对 Objective 比较。

每个 Optimizer 除组件 `id` 外还必须声明 `proposal_contract`。Kernel 接受 `aceval.optimizer/candidate-patch-v1`，也接受内置 `aceval.optimizer/skill-markdown-improver-v2`，由 `SkillOptimizerBridge` 转为前一种契约；旧 `skill-markdown-generator-v1` 保留为 repair 兼容面。缺失、未知或与 improvement mode 不匹配的契约会在生成候选前 fail closed。

当前 `candidate-patch-v1` 的可验证格式仍是刻意收窄的：候选目录提供完整文件快照，`content` 必须是 UTF-8 文本，diff 只能修改单个声明的 entrypoint，Kernel 会从冻结 parent/candidate 重新计算 unified diff 并逐项核对。无效 base/path/hash/diff 会计入 rejected proposal 和 usage 后以结构化协议错误停止。多文件或二进制优化不能只注册一个新 Subject Adapter；D40 需要同时定义显式的候选验证扩展契约（例如 `verify_candidate_patch`）及对应安全测试。

Manifest 中三类自由参数有固定传递边界：`subject_contract.params` 进入 Subject Adapter 的 `snapshot/materialize` 以及候选重验；`driver.params` 只通过 Driver `RunContext.metadata.driver_params` 进入 `prepare`，不会转发给 Runtime；`optimizer_policy.params` 通过 `PatchConstraints.metadata.optimizer_params` 交给 `candidate-patch-v1` Optimizer。当前内置 Adapter/Driver 不消费这些自定义参数。

### EvalPack

`EvalPack v1alpha1` 保持 legacy repair 兼容；`v1alpha2` 新增 `auto | repair | tune`、自然语言 Goal 和单一 Primary Objective。Objective 第一版支持 Grader score/metric、Token/成本、场景耗时和工具调用数；所有 hard Grader 始终是正确性与安全 guardrail。仓库内置两个 Pack：

| Pack | 输入与输出 | Case | 主要 Grader |
|---|---|---:|---|
| `security-review` | Python 文件 -> JSON findings | 6 dev + 2 validation + 2 holdout | JSON Schema、记录匹配、源码行引用、Trace 工具断言 |
| `csv-summary-smoke` | CSV 文件 -> `summary.json` | 2 dev + 1 validation | 产物存在、JSON Schema、JSON Path、workspace diff |

接入新任务时，若 UTF-8 `SKILL.md`、固定文件工具和现有 Driver/Grader 已足够，优先用 Pack Builder 从少量 Case 和 Goal 生成 Pack。已支持 `generic`、`csv-summary`、`security-review` 模板；未知类型会安全降级到 `generic` 草稿。出现新的 Subject 文件形态、工具能力、输入输出模态、评分语义或修改表面时，需要实现并显式注册相应受信组件；目标是保持 Kernel 状态机不随领域变化，而不是宣称扩展永远零代码。

生成的 EvalPack 采用独立生命周期：

```text
draft -> calibrating（可反复改 Case/Oracle/Grader） -> frozen
                                                        |
                                                        v
                                              repair/tune Skill
```

草稿可以 `lint`、`test` 和人工校准，但不能进入 Skill 优化。显式冻结时会生成 `.aceval-pack-lock.json`；冻结后任何 Case、Oracle、Grader、fixture 或 Objective 变化都会使加载失败。需要调整评测器时，应创建新 Pack 版本并重新跑 baseline，不能让模型在同一实验里同时改 EvalPack 和 Skill。

### FakeRuntime 的定位

FakeRuntime 读取 Scenario 中预注册的 baseline/candidate 输出，用于：

- Pack conformance；
- CLI、状态机和报告测试；
- 无模型、无密钥的确定性演示；
- 验证候选是否按 dev/validation/holdout 顺序经过门禁。

它不执行模型，也不证明示例候选能在真实模型上获得同样结果。所有 FakeRuntime 报告都会包含 `simulated: true`。

## 复杂 Skill 规划与故障归因（Implemented，D20）

当前实现已经在 Kernel 外增加两个正式能力面：

```text
Skill + 少量种子 Case + Goal
  -> Capability Graph
  -> Test Plan + bounded Case drafts
  -> Coverage Matrix + Runtime gaps
  -> Pack Quality Gate -> frozen EvalPack

Observation + Trace + Grade
  -> Failure Cards
  -> quality / tool / CLI / Runtime / evaluator / evidence-gap 分类
  -> Patch Authorization
```

`aceval plan` 会确定性读取冻结的 UTF-8 `SKILL.md`，生成带行号和 quote hash 的 Capability Graph，将显式分支、风险、工具和状态声明编译为风险加权 Test Requirement，把种子 Case 映射到 Requirement，并在 `max_generated_cases` 预算内生成可编辑 Case 草稿。规划产物同时给出 planned、Runtime-executable、Oracle-ready 和显式 observed coverage、Runtime gap 与 Freeze Blocker；它不宣称穷举任意自然语言路径，也不会凭空生成并信任语义 Oracle。

`aceval pack generate --plan ...` 会把规划产物编译进 Pack 的 `design/` Sidecar。`aceval pack quality` 和 `pack freeze --approve` 会校验 Sidecar source-subject/Plan hash 一致性、引用、critical coverage、Oracle trust、Runtime gap、generated holdout 和 Case family 跨 split 泄漏；`doctor` 运行前还会比较实际 Subject hash，除非用户显式允许 drift。当前 Quality Gate 还不包含 known-good/known-bad、mutation detection 或 evaluator flake 校准。

Failure Attribution 已接入 Orchestrator：每个 Scenario 生成版本化 Failure Card 和 Patch Decision，只有授权的 dev hard failure 才会进入 Optimizer。CLI 未安装、权限/认证、网络、Runtime、Driver、Grader、Oracle 或证据缺失默认不会触发 Skill 修改；证据不足保持 `needs_more_evidence`。`aceval session diagnose` 可以读取已经归一化的 Imported Session，做 Trace/执行层离线诊断；它不是 EvalRun/Grader Replay，也不会在公司平台在线重跑 Agent。

当前 Planner 和修改授权路径不依赖 LLM：语义更丰富的模型辅助分析只能作为未来草稿层，不能绕过确定性 source-ref、Quality Gate 或 Patch Authorization。

完整方案和 D20/D40 范围见 [COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md](./COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md)。

```bash
aceval plan \
  --subject ./my-skill \
  --cases ./seed-cases.json \
  --goal '结果正确，失败时不留下部分产物' \
  --runtime-profile reference \
  --output .aceval/plans/my-skill

aceval pack generate \
  --plan .aceval/plans/my-skill \
  --type generic \
  --output .aceval/packs/my-skill

aceval pack quality .aceval/packs/my-skill
```

## 安装

要求 Python 3.9 或更高版本。核心包没有必需的第三方运行时依赖，仓库自带 Pack 使用 JSON-compatible YAML，因此仅用 Python 标准库即可运行。

以下安装步骤以及后文的 EvalPack 示例都应从 clone 后的仓库根目录执行。`evalpacks/` 和 `examples/` 是仓库演示资产，不随 `aceval` 核心 Python 包安装。

```bash
git clone https://github.com/little-witt/eval-ops.git
cd eval-ops
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
aceval --version
```

也可以不安装，直接从源码运行：

```bash
PYTHONPATH=src python3 -m aceval --version
```

需要读取非 JSON-compatible 的普通 YAML 时，可选安装 `PyYAML`：

```bash
python -m pip install -e '.[yaml]'
```

## 快速验证 EvalPack

`pack lint` 检查 Manifest、组件 ID、引用、路径、Case、Oracle 和 hash；它不执行 Runtime。`pack test` 使用 FakeRuntime 在 dev/validation 上检查 Pack、Driver、Grader 和报告链路，是模拟 conformance，不是模型 Benchmark，也不会执行 holdout。以下命令需要在 clone 后的仓库根目录执行。

```bash
aceval pack lint evalpacks/security-review
aceval pack test evalpacks/security-review --runtime fake

aceval pack lint evalpacks/csv-summary-smoke
aceval pack test evalpacks/csv-summary-smoke --runtime fake
```

## 从少量 Case 生成 EvalPack

已支持类型只需提供 Case JSON 和 Goal；若 Goal 明确包含 Token、成本、工具调用或延迟，Builder 会生成一个可测量的单一效率 Objective，并保留 hard Grader 作为正确性约束。示例输入见 `examples/cases/generic-cases.example.json`。

```bash
aceval pack generate \
  --type generic \
  --cases examples/cases/generic-cases.example.json \
  --goal '保持答案正确并减少 token' \
  --output .aceval/packs/generic-answer

aceval pack calibrate .aceval/packs/generic-answer
aceval pack lint .aceval/packs/generic-answer
aceval pack freeze .aceval/packs/generic-answer --approve
```

`--type` 可以传尚未支持的领域名；此时系统会一键生成 `generic` draft，并记录请求类型，但不会允许 `doctor --approve-pack` 直接用它优化 Skill。用户需要先补齐/确认语义 Oracle 和 Grader，再单独冻结。主观“更好”不会被悄悄翻译成模型自己定义、自己打分的标准。

## 从复杂 Skill 生成 Test Plan

当用户只有 Skill、少量种子 Case 和目标时，先运行规划链路。Planner 会输出 Capability Graph、Test Plan、Case 草稿、Coverage、Runtime gap 和生成来源，再将它们作为 `design/` Sidecar 编译进 EvalPack：

```bash
aceval plan \
  --subject ./my-skill \
  --cases ./seed-cases.json \
  --goal '结果必须准确，并覆盖失败恢复路径' \
  --runtime-profile reference \
  --output .aceval/plans/my-skill

aceval pack generate \
  --plan .aceval/plans/my-skill \
  --type generic \
  --output .aceval/packs/my-skill

aceval pack quality .aceval/packs/my-skill
aceval pack calibrate .aceval/packs/my-skill
aceval pack freeze .aceval/packs/my-skill --approve
```

若存在未确认 Oracle、critical coverage 缺口、Runtime 不支持的关键路径、Subject/Plan hash 不一致或 split 泄漏，`pack quality`/`freeze` 会阻断。用户应修订新 Pack 版本或校准草稿，不应通过删除失败 Case 来绕过门禁。自动生成 Case 只进入 dev/validation 草稿，不会成为 sealed holdout。

## 傻瓜式 repair/tune 入口

`doctor` 把 Pack 生成、生命周期检查、baseline、自动模式选择和候选门禁串在一起。已支持模板可通过一次显式确认直接启动；baseline 有硬失败时选择 repair，全部 hard gate 已通过时选择 tune。

```bash
aceval doctor \
  --subject ./my-skill \
  --cases ./cases.json \
  --type csv-summary \
  --goal '结果必须准确，并尽量减少工具调用' \
  --pack-output .aceval/packs/my-csv-pack \
  --approve-pack \
  --runtime reference \
  --model-command 'python path/to/model_bridge.py' \
  --model-env MODEL_API_KEY
```

若生成的 Pack 仍需校准，命令会停在 `calibration_required`，不会同时修改评测器和 Skill。

希望一次命令先分析复杂 Skill 时，增加 `--auto-plan`；系统仍会在任何规划或质量 blocker 处安全停止：

```bash
aceval doctor \
  --subject ./my-skill \
  --cases ./seed-cases.json \
  --type generic \
  --goal '修复错误，并在正确性不回退的前提下减少 token' \
  --auto-plan \
  --plan-output .aceval/plans/my-skill \
  --pack-output .aceval/packs/my-skill \
  --runtime reference \
  --model-command 'python path/to/model_bridge.py'
```

## 公司 Agent API 与 Session 日志

配置示例见 `examples/company-api-profile.example.json`。Profile 只保存密钥所在的环境变量名，不保存 token 值；Execute 和 Session Log 的字段位置通过受限 JSONPath 映射。

```bash
export COMPANY_AGENT_TOKEN='...'

aceval profile validate examples/company-api-profile.example.json

aceval session fetch \
  --profile examples/company-api-profile.example.json \
  --session-id SESSION_ID \
  --output .aceval/imported/session.json

aceval session import \
  --profile examples/company-api-profile.example.json \
  --session-id SESSION_ID \
  --input downloaded-session.json \
  --output .aceval/imported/session.json

aceval session diagnose \
  --input .aceval/imported/session.json \
  --output .aceval/imported/session-diagnosis.json
```

导入结果统一为 `RunObservation` 语义：`output + canonical trace + usage + completeness`。离线 `session diagnose` 已能对结构化 Runtime/tool/CLI 错误生成 Failure Card；把导入 Observation 接入 Pack/Scenario、完整 EvalRun、Grader Replay 或公司在线 Runtime 仍是 D40 扩展面。

## 双 Pack 可复制演示

下面所有涉及 Subject 执行的命令都显式选择 `--runtime fake`，避免把模拟误认为真实评测。`run`、`compare` 和 `pack test` 只允许 `dev`、`validation`；面向被测 Subject 的 holdout 只能由 `optimize` 的最终门禁触发。

### Security Review

运行冻结候选：

```bash
aceval run \
  --pack evalpacks/security-review \
  --subject examples/subjects/security-review-skill-candidate \
  --split dev --split validation \
  --runtime fake
```

成对比较 baseline 与 candidate：

```bash
aceval compare \
  --pack evalpacks/security-review \
  --subject examples/subjects/security-review-skill \
  --candidate examples/subjects/security-review-skill-candidate \
  --split dev --split validation \
  --runtime fake
```

用冻结候选演示 dev、validation、holdout 优化门禁：

```bash
aceval optimize \
  --pack evalpacks/security-review \
  --subject examples/subjects/security-review-skill \
  --candidate examples/subjects/security-review-skill-candidate \
  --runtime fake
```

### CSV Summary

运行冻结候选：

```bash
aceval run \
  --pack evalpacks/csv-summary-smoke \
  --subject examples/subjects/csv-summary-skill-candidate \
  --split dev --split validation \
  --runtime fake
```

成对比较 baseline 与 candidate：

```bash
aceval compare \
  --pack evalpacks/csv-summary-smoke \
  --subject examples/subjects/csv-summary-skill \
  --candidate examples/subjects/csv-summary-skill-candidate \
  --split dev --split validation \
  --runtime fake
```

用冻结候选演示 dev 和 validation 优化门禁。该 Pack 未声明 holdout：

```bash
aceval optimize \
  --pack evalpacks/csv-summary-smoke \
  --subject examples/subjects/csv-summary-skill \
  --candidate examples/subjects/csv-summary-skill-candidate \
  --runtime fake
```

上述 `optimize --candidate` 不调用模型生成候选，只验证一个预注册候选和完整门禁。真实候选生成需要 Reference Runtime 与模型桥接。

## 接入真实模型

`--runtime reference` 必须同时提供 `--model-command`。CommandModelClient 每个模型步骤启动一次受控命令，把完整请求写到 stdin，并从 stdout 读取一个 JSON 对象；命令不经过 shell。

```bash
aceval run \
  --pack evalpacks/security-review \
  --subject examples/subjects/security-review-skill \
  --split dev \
  --runtime reference \
  --model-command 'python path/to/model_bridge.py' \
  --model-id 'provider/model-version' \
  --model-env MODEL_API_KEY \
  --model-timeout 60 \
  --max-steps 8
```

模型桥接收到的请求结构：

```json
{
  "messages": [
    {"role": "system", "content": "...<skill>...</skill>"},
    {"role": "user", "content": "Review the files in the workspace..."}
  ],
  "tools": [
    {
      "name": "read_file",
      "description": "Read a UTF-8 workspace file.",
      "parameters": {
        "type": "object",
        "required": ["path"],
        "properties": {"path": {"type": "string"}},
        "additionalProperties": false
      }
    }
  ]
}
```

请求工具调用时，桥接程序在 stdout 返回：

```json
{
  "content": "",
  "tool_calls": [
    {
      "id": "call_1",
      "name": "read_file",
      "arguments": {"path": "target.py"}
    }
  ],
  "usage": {"input_tokens": 320, "output_tokens": 24, "total_tokens": 344}
}
```

Runtime 执行工具后，会在下一次请求的 `messages` 中附上 `role: tool` 的结果。任务完成时返回无工具调用的最终回复：

```json
{
  "content": "{\"findings\": []}",
  "tool_calls": [],
  "usage": {"input_tokens": 510, "output_tokens": 38, "total_tokens": 548}
}
```

stdout 必须只包含该 JSON；调试日志应写入 stderr。`--model-env NAME` 可重复使用，只有显式 allowlist 的环境变量和 `PATH` 会传入桥接进程。

`--model-id` 是操作者声明的模型标识，用于报告和 Runtime profile hash；v1 bridge 无法从供应商侧验证该值，也无法证明 temperature、seed、上下文裁剪等实际推理参数。因此 Reference Runtime 的 `profile_complete` 保持 `false`，报告会明确提示该实验尚不能完全复现。模型桥接画像只持久化 executable、argv 数量和 argv SHA-256，不保存原始 argv 或环境变量值，避免把命令行凭证写入报告。

真实生成候选时，不传 `--candidate`。可以用独立的 `--optimizer-command`，未提供时复用 `--model-command`：

```bash
aceval optimize \
  --pack evalpacks/security-review \
  --subject examples/subjects/security-review-skill \
  --runtime reference \
  --model-command 'python path/to/model_bridge.py' \
  --model-id 'provider/model-version' \
  --optimizer-command 'python path/to/optimizer_bridge.py' \
  --model-env MODEL_API_KEY \
  --max-rounds 2
```

Optimizer 使用同一 JSON envelope，但 `tools` 为空；其最终 `content` 必须是包含完整 `skill_markdown` 和简短 `rationale` 的 JSON 字符串。

## 报告与退出码

默认输出根目录是 `.aceval/runs`，也可通过 `--output-root PATH` 修改。CLI 的 stdout 会给出本次运行的准确路径：

```json
{
  "report_json": "/absolute/path/to/project/.aceval/runs/run-<id>/report.json",
  "report_markdown": "/absolute/path/to/project/.aceval/runs/run-<id>/report.md",
  "simulated": false
}
```

- `run` 报告位于 `<output-root>/run-<id>/`；
- `compare` 报告位于 `<output-root>/compare-<id>/`；
- `optimize` 报告位于 `<output-root>/optimize-<id>/`；
- 优化候选位于 `optimize-<id>/candidates/<candidate-id>/`，包含新的 `SKILL.md`、`candidate.patch` 和 lineage 元数据；
- 最终选择会独立物化到 `optimize-<id>/selected-candidate/`，报告记录 `selected_candidate_path` 与 `selected_candidate_hash`；该副本带受控 provenance marker，可作为 `aceval run --subject ...` 的 candidate 重新执行，且不依赖可变的 trial 目录；
- 每个 EvalRun 在 `<run-dir>/frozen-pack/` 保存逐文件 hash 校验后的 Pack 副本；本次 Driver、fixture 和 `schema_ref`/`rubric_ref` 评分都基于这个冻结版本；
- 原始 Subject 不会被覆盖。

`run/compare` 的质量失败退出码为 `1`，输入、配置、执行错误或不可比较结果通常为 `2`，便于接入 CI。Gate 状态区分 `pass | fail | error | not_evaluable | not_run`；baseline 或 candidate 出现基础设施错误时，compare 不计算 paired uplift，也不会把错误恢复记成 improvement。报告明确记录 Runtime profile 与内容 hash、configured budget、Pack/Subject hash、分层结果、硬回归、usage 的 `measured | partial | not_measured` 状态以及限制项。优化报告还记录 mode、Goal、Objective baseline/candidate 值、signed improvement、逐 Case 回退、所有 validation attempts、proposal/rejected/duplicate 数量和全实验累计 usage。Repair 的 holdout 仍只运行最终候选；Tune 的 validation/holdout 都成对运行 baseline/candidate，并记录 `holdout_pair_count`。单次测量不宣称统计显著。

## 安全与有效性边界

- Reference Runtime 是同机、同用户的受限工作区执行器，不是抵御恶意代码的生产级沙箱；只应运行可信的模型桥接命令。
- 模型只能通过三种内置文件工具访问 Scenario 工作区，但桥接进程本身仍拥有当前操作系统用户的权限。若桥接程序不可信，需要额外容器或隔离 Worker。
- 当前三种工具只处理 UTF-8 文本；二进制文件、多模态输入输出、shell、网络和浏览器任务不在内置 Reference Runtime 的能力范围内。
- 命令使用 argv 启动且不经过 shell；环境变量需要显式 allowlist，但这不等于完整进程沙箱。
- `--max-tool-calls` 会进入 Reference Agent loop；Token 和费用预算依赖模型 bridge 回报 usage，只能阻止后续 Case/候选，不能撤销已完成的单次模型调用。供应商侧的单调用 Token/费用上限仍应由 bridge 配置。
- 墙钟预算是 soft deadline：超时后系统会等待受控 bridge worker 完成，或等待模型命令自身的 timeout，以避免后台线程继续修改已进入清理阶段的 workspace；因此进程实际返回时间可能超过墙钟预算。
- Token 字段必须是非负整数；存在 `total_tokens`/输入输出别名或费用别名时按可观测最大值保守计费，并在报告中标记冲突。
- Oracle 和 validation/holdout 内容不会进入 Optimizer 请求；本地仓库中的测试文件对有主机文件权限的恶意进程并不构成密码学隐藏。
- 生成的 Pack 在 draft/calibrating 阶段不能优化 Skill；frozen Pack 受逐文件内容锁保护。修改 Pack 后必须创建新版本并重新跑 baseline，历史 uplift 不可沿用。
- 自然语言 Goal 只有在能映射到确定性效率指标时才会自动生成 Objective；主观质量需要经人工确认/校准的 Rubric 或 Judge，当前不会让同一模型自定标准并自证成功。
- Driver 只接收不含 Oracle/Grader 配置的 Scenario view 和 Case 专属 fixture 副本；`PreparedScenario`、Runtime result、Observation 和 Grade 数据在边界处深冻结，避免后序组件修改已采集证据。自定义 Python Driver/Grader 仍是同进程可信计算基（TCB），这不是对恶意组件的隔离承诺。
- Scenario ID、fixture/artifact 路径都必须是受控相对标识；内存 artifact 与 workspace artifact 统一受文件数、单文件大小和总字节上限约束。
- 单次运行不能测量模型波动；真实结论需要固定模型参数并重复运行。
- 在 Reference Runtime 上通过只说明该 Skill 在当前 Runtime profile 下有效。不同平台的 system prompt、模型、工具协议、上下文裁剪和权限可能改变结果，不能推导为跨 Runtime 同等最优。
- FakeRuntime 的通过、提升率和 accepted 状态只验证预注册模拟数据与工作流，不得写成真实模型 Benchmark 成果。

## 核心价值与可迁移性

- 减少人工中转：统一执行、Trace、评分、候选和回归门禁；
- 让失败可复现：冻结 Pack、Subject、Case 和 Oracle，并显式记录 Runtime 与 usage；
- 控制错误优化：repair 只看 dev 硬失败；tune 只看 dev Objective，且 hard gate 不得回退；validation/holdout 保持隔离；
- 控制成本与风险：限制步骤、时间、Token、费用、工具调用、候选数和补丁范围；
- 降低受支持范围内的扩展成本：新领域优先通过 EvalPack 接入，超出内置能力时增加显式受信组件；
- 迁移到完整 Agent：未来可为 system prompt、工具配置、Workflow 或 Agent snapshot 实现新的 `SubjectAdapter`，复用 Driver、Runtime、Trace、Grader、门禁和报告。

即使 Skill 这一封装形态变化，实验控制、Trace 标准化、混合评分、回归门禁、数据隔离和候选 lineage 仍是 Agent 评测与优化的通用工程能力。

## 20 天黑客松路线

当前代码已经提供 Reference Runtime、repair/tune Kernel、复杂 Skill Analyzer/Test Planner/Coverage、Pack Builder/Quality/冻结锁、Failure Attribution、离线 Session diagnose、双 Pack、确定性 Grader、FakeRuntime conformance、受控优化门禁和 JSON/Markdown 报告。D20 剩余重点是把这些能力打磨成可信演示，而不是继续补同层契约：

1. 接入一个真实模型桥接并冻结模型参数，完成双 Pack 的重复实验；
2. 用一个复杂文件型 Skill 演示 `plan -> pack generate --plan -> pack quality -> freeze`，展示 critical gap、未确认 Oracle 和 Runtime gap；
3. 演示一张允许 Skill intervention 的 Failure Card，以及一张来自离线 Session 的 CLI/Runtime/Grader 阻断 Card；
4. 校验候选在 dev、validation、holdout 上的实际表现，不预填提升数字；
5. 固化 6 分钟演示、失败降级方案、录屏和可复现实验说明；
6. 展示第二 Pack 的实际接入改动与工时，证明扩展边界而非口头宣称通用。

黑客松提交应把 FakeRuntime 演示标记为 simulation，并将任何实际提升数字绑定到可复现的真实模型报告。

## 40 天完整作品路线

D20 已经前置实现 repair/tune、复杂 Skill 规划/覆盖、基础 Pack Quality、Failure Attribution、Doctor、公司 Profile、Session Import/离线 Diagnosis 和 paired gates。D40 不再重复实现这些契约，而改为：

1. 完成真实模型 repair/tune Benchmark 和重复执行统计；
2. 用标注集评测并增强 Analyzer/Planner，加入 seed expansion、metamorphic Case 和自动动态覆盖接线；
3. 将基础 Pack Quality 扩展到 known-good/known-bad、mutation、evaluator flake 和 Pack revision diff；
4. 将公司 Execute/Session 接成 `CompanyRuntimeAdapter`、EvalRun、Grader Replay 和 Case mining；
5. 增加受控 argv-only Process Tool 和只读诊断探针；
6. 抽取 CLI/Web 共用的 Application Service，建设静态 HTML 和本地 Skill Doctor Console；
7. 用重复统计、主观 Judge Pilot、`FixedAgentTarget`、CI、教程、视频和消融完成完整作品化。

完整排期、完成定义和优先级见 [ROADMAP.md](./ROADMAP.md)。可视化产品与技术方案见 [VISUAL_CONSOLE_DESIGN.md](./VISUAL_CONSOLE_DESIGN.md)。

## 可视化操作与结果平台

项目将建设本地单用户 `Skill Doctor Console`，而不是当前阶段的多租户 SaaS。Console 重点覆盖：

- Capability Graph、Test Plan、Coverage Matrix 和 Runtime gap；
- EvalPack 生成、质量、校准和冻结确认；
- baseline -> repair/tune -> validation -> holdout 实时进度；
- Case、Grader evidence、Trace 时间线和 Failure Card；
- baseline/candidate 指标、Skill diff 和候选谱系；
- 公司 Session 导入、completeness、Diagnosis 和 Replay。

实施顺序为：真实实验数据 -> 规划/归因 Benchmark 与高级 Pack Quality -> 公司在线 Replay/Process Tool -> 静态 HTML -> Application Service -> Read-only Console -> Operational Console。当前仓库没有 Web Console；未来 UI 也不重新实现 Gate、Objective、归因授权或 Pack 生命周期语义。

## 开发与测试

零第三方依赖运行完整测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/aceval-pyc PYTHONPATH=src python3 -m compileall -q src tests
git diff --check
```

## 仓库文档

- [ROADMAP.md](./ROADMAP.md)：更新后的 D20/D40 路线图、已前置能力、待办与完成定义；
- [COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md](./COMPLEX_SKILL_EVAL_AND_DIAGNOSIS_DESIGN.md)：复杂 Skill 测试规划、覆盖、Pack Quality、故障归因和 CLI/Session 方案；
- [VISUAL_CONSOLE_DESIGN.md](./VISUAL_CONSOLE_DESIGN.md)：本地可视化操作台的产品、架构、API、安全和分阶段方案；
- [EVALPACK_SPEC.md](./EVALPACK_SPEC.md)：EvalPack 边界、Manifest 和公共 Protocol；
- [TECHNICAL_DESIGN.md](./TECHNICAL_DESIGN.md)：完整技术方案、状态机和验收标准；
- [SKILL_CATEGORY_AND_ITERATION_DESIGN.md](./SKILL_CATEGORY_AND_ITERATION_DESIGN.md)：Skill 分类、输入输出契约和自迭代设计；
- [research/skill-ranking](./research/skill-ranking)：公开 Skill 榜单快照和可复算分类脚本；
- [src/aceval/agent_runtime.py](./src/aceval/agent_runtime.py)：Reference Agent Runtime 与模型桥接协议；
- [src/aceval/orchestrator.py](./src/aceval/orchestrator.py)：通用执行、比较与优化门禁；
- [src/aceval/pack_builder.py](./src/aceval/pack_builder.py)：EvalPack 草稿生成、校准和冻结；
- [src/aceval/connections.py](./src/aceval/connections.py)：公司 API Profile 与 Session 日志归一化；
- [evalpacks](./evalpacks)：内置双 Pack 与 fixture/Oracle。

## 项目边界

- 不采集或依赖模型隐藏思维链；
- 不将合成 Case 自动视为真实质量标准；
- 不把声明能力/风险覆盖描述为任意自然语言的数学全路径覆盖；
- 不把同源模型生成 Case 伪装为独立 sealed holdout；
- 不从单次日志宣称 Skill、Agent 或 CLI 的唯一强因果根因；
- 不允许 Optimizer 修改 Case、Grader、validation、holdout 或 Runner；
- 不自动覆盖、合并或发布生产 Skill；
- 不把单 Runtime 的最优结果宣称为所有 Agent 平台上的最优结果；
- 不把 UTF-8 `SKILL.md` + 固定文件工具的 MVP 描述为已支持多文件、二进制、shell、network、browser 或 multimodal Skill；
- 当前版本不宣称已经取得任何真实模型提升。

长期定位：以稳定的 Reference Agent Runtime 为实验基准，以通用 Kernel + EvalPack 为扩展核心，逐步覆盖 Skill、能力组件和完整 Agent 的持续评测、故障归因与受控优化。
