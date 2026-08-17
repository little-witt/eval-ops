# 复杂 Skill 测试规划与故障归因技术方案

> 项目：Skill Doctor / Agent Capability EvalOps  
> 状态：Planned，作为现有 MVP 的下一阶段设计基线  
> 日期：2026-08-18  
> 适用范围：D20 黑客松增强版与 D40 求职作品

## 1. 方案结论

现有 EvalOps Kernel、Reference Agent Runtime、EvalPack 生命周期和 repair/tune 门禁继续作为可信执行底座。在它们之前和之后分别新增两个独立能力面：

1. **Test Intelligence Plane**：分析复杂 Skill，建立能力图，规划测试义务，生成 Case 草稿并给出可审计覆盖报告；
2. **Failure Intelligence Plane**：从 Observation、Trace、Grader、Runtime 和外部 Session 证据中识别故障边界，生成 Failure Card，并决定是否允许修改 Skill。

目标架构为：

```text
Skill + 少量种子 Case + Goal + Runtime Profile
                       |
                       v
              Skill Analyzer
                       |
              Capability Graph
                       |
              Test Plan / Gaps
                       |
             Case + Oracle Drafts
                       |
          Coverage & Pack Quality Gate
                       |
          EvalPack calibration -> frozen
                       |
                       v
                EvalOps Kernel
        Runtime -> Observation -> Graders
                       |
                       v
              Failure Attribution
                       |
        Failure Cards + Patch Authorization
                       |
            repair / tune / external action
```

这不是把一个大模型直接放在循环中“自己出题、自己判卷、自己修改答案”。模型负责语义理解、候选测试设计和解释；确定性代码负责结构校验、来源追踪、能力检查、覆盖计算、冻结锁、证据门控和修改授权。

## 2. 当前能力与新增能力边界

| 能力 | 当前 MVP | 本方案目标 |
|---|---|---|
| Case 到 EvalPack | 编译用户已提供的 Case | 从 Skill 和种子 Case 生成有来源依据的测试计划与 Case 草稿 |
| 复杂 Skill 分析 | 不读取 `SKILL.md` 规划测试 | 生成 Capability Graph、风险和 Runtime capability gap |
| 路径覆盖 | 无显式模型 | 声明能力、分支、风险、工具和状态转换覆盖 |
| EvalPack 可信度 | 生命周期、内容锁、基础校准 | Oracle 可信级别、mutation score、已知好坏样本区分能力 |
| 非 Skill 失败 | `_has_non_skill_failure()` 布尔阻断 | 证据化分类、置信度、补救面和 Failure Card |
| CLI 问题 | Reference Runtime 不执行任意 CLI | 导入日志可诊断；后续增加受控 argv Process Tool |
| 公司 Session | 已归一化为 `ImportedRunBundle` | Replay、重评分、诊断和 Case 草稿生成 |

任何新增能力都不得改变以下规则：

- draft/calibrating EvalPack 不能优化 Skill；
- 优化器不能修改 Case、Oracle、Grader、validation 或 holdout；
- 缺少证据时输出 `unknown`，不能猜测为 Skill 缺陷；
- 自动生成的语义 Oracle 未经校准时不能成为 frozen hard Grader；
- Runtime 不支持的路径必须报告为 coverage gap，不能伪装为已覆盖。

## 3. 面向用户的完整流程

### 3.1 傻瓜式快速路径

用户只提供：

- Skill 目录或 `SKILL.md`；
- 2–5 个种子 Case；
- 一句话 Goal；
- Runtime/Profile。

系统执行：

```text
1. 扫描 Skill 和引用资源
2. 提取能力、步骤、分支、工具、状态和副作用
3. 对照 Runtime Profile 标出可执行与不可执行路径
4. 将种子 Case 映射到测试义务
5. 在预算内补充边界、异常和高风险 Case
6. 为每个 Case 选择 Oracle 策略并标出可信级别
7. 展示 Test Plan、Coverage Matrix 和 Freeze Blockers
8. 用户只确认未解决的语义问题
9. 校准并冻结 EvalPack
10. 运行 baseline，生成 Failure Cards
11. 仅将允许 Skill 修改的失败交给 repair/tune
12. 经过 validation/holdout 输出候选和报告
```

系统不要求普通用户理解 Driver、Grader ID 或 Manifest。高级用户仍可编辑生成的 Pack 和测试计划。

### 3.2 三种自动化结果

复杂 Skill 不应只有“生成成功/失败”两种结果：

| 结果 | 条件 | 下一步 |
|---|---|---|
| `ready_for_calibration` | Case 可执行，Oracle 可确定或由种子事实推导 | 自动进入校准预览 |
| `needs_user_input` | 成功标准主观、外部业务规则缺失或存在冲突 | 只询问会改变测试语义的问题 |
| `unsupported_runtime` | 当前 Runtime 缺少 CLI、network、browser 等能力 | 生成能力缺口报告，不生成虚假可执行 Case |

## 4. Test Intelligence Plane

### 4.1 输入与扫描范围

D20 支持：

- UTF-8 `SKILL.md`；
- 当前 `skill_markdown_v1` 可见的 `subject.json`；
- 用户提供的种子 Case、Goal 和 Runtime Profile；
- `SKILL.md` 中可定位的标题、段落和行号。

D40 扩展：

- `scripts/`、`templates/`、`assets/` 等多文件 Skill bundle；
- 工具 Schema、CLI help/version 输出和外部 API 契约；
- 公司 Session 中实际出现过的能力与工具路径。

扫描分为两层：

1. **Deterministic Inventory**：文件、引用、显式工具名、输入输出格式、限制和 source span；
2. **Semantic Extraction**：模型将自然语言说明转换为结构化能力和测试义务。

模型输出必须通过 Schema、ID、引用存在性和 source span 校验。无法对应原文的节点必须标记 `inferred=true`，不能冒充 Skill 明确声明。

### 4.2 Capability Graph

建议新增版本化契约 `aceval.skill-analysis/v1`：

```json
{
  "api_version": "aceval.skill-analysis/v1",
  "subject_hash": "sha256:...",
  "capabilities": [
    {
      "id": "cap.validate-input",
      "name": "校验输入数据",
      "source_refs": [
        {
          "path": "SKILL.md",
          "start_line": 18,
          "end_line": 25,
          "quote_sha256": "sha256:...",
          "binding": "explicit"
        }
      ],
      "inputs": ["workspace/*.csv"],
      "outputs": ["validation result"],
      "preconditions": ["input exists"],
      "steps": ["list files", "read input", "validate rows"],
      "branches": ["valid", "malformed", "empty"],
      "tools": ["list_files", "read_file"],
      "state_transitions": [],
      "side_effects": [],
      "risks": ["silent partial parse"],
      "inferred": false
    }
  ],
  "runtime_gaps": []
}
```

每个能力节点至少表达：

- 输入、输出和前置条件；
- 工作流步骤和顺序约束；
- 条件分支；
- 所需工具或外部依赖；
- 可观察结果；
- 状态变化和副作用；
- 失败、恢复和清理行为；
- 证据来源。

`source_refs` 必须包含稳定 Subject hash 下的 path、行号和 quote hash；`binding` 区分 `explicit | user_confirmed | inferred | unknown`。高风险 hard requirement 不能只建立在 `inferred/unknown` 引用上。

图结构只描述“需要被测试的行为”，不进入 EvalOps Kernel 的运行状态机。

### 4.3 Test Requirement 与 Test Plan

Capability Graph 会展开为可追踪的测试义务：

```json
{
  "id": "req.validate-input.malformed",
  "capability_id": "cap.validate-input",
  "dimension": "negative",
  "binding": "contract",
  "priority": "high",
  "preconditions": ["malformed CSV fixture"],
  "stimulus": "要求处理包含断裂引号的 CSV",
  "expected_observables": ["明确失败", "不写 summary.json"],
  "oracle_strategy": "workspace_state+output_invariant",
  "required_runtime_capabilities": ["workspace_fixture", "canonical_trace"],
  "source_refs": ["cap.validate-input#source-0"],
  "status": "uncovered"
}
```

`binding` 区分：

- `contract`：Skill 明确要求、用户确认或安全关键行为，可以进入 hard gate；
- `diagnostic`：一种推荐实现路径，只用于覆盖和归因，不因 Agent 采用另一条正确路径而判失败。

工具顺序、内部规划等实现细节默认是 `diagnostic`，除非它们与权限、安全、不可逆副作用或用户明确流程要求相关。

默认覆盖维度：

- happy path；
- boundary；
- negative/invalid input；
- permission/authentication；
- timeout/retry；
- partial success；
- wrong tool/tool argument；
- malformed tool result；
- state conflict；
- idempotency/repeated execution；
- recovery/cleanup；
- step ordering；
- output/artifact/trace consistency。

并非每个能力都机械生成全部维度。Planner 根据副作用、外部依赖、分支数量和用户 Goal 计算风险优先级，在 `max_generated_cases` 和执行预算内选择测试义务。

组合爆炸通过以下方式控制：

- 高风险分支优先；
- pairwise 参数组合；
- 等价类与边界值；
- 相似 Case 去重；
- 已有种子 Case 优先复用；
- 低价值组合进入 uncovered risk，不强行生成。

Planner 第一版采用可解释的风险加权 set-cover，而不是让模型自由决定最终测试集：

1. 为每个 requirement 计算 `risk_weight = severity × likelihood × side_effect × goal_relevance`；
2. 先把种子 Case 映射到它能覆盖的 requirement 集合；
3. 为未覆盖 requirement 生成候选 Case family；
4. Feasibility Router 删除当前 Runtime/Driver/Grader 无法执行的候选，并保留 gap；
5. 在 `max_generated_cases` 内贪心选择“新增风险覆盖 / 预计执行成本”最高的 Case；
6. critical requirement 无 Case 时形成 freeze blocker，不能被低风险覆盖率平均掉；
7. 输出选择理由和被预算淘汰的 requirement，便于用户调整预算。

### 4.4 Case 生成策略

Case Generator 不只生成 Prompt，还需要同时生成 fixture、预期观察和 Grader 草稿。建议支持五类策略：

1. **Seed expansion**：对种子 Case 做边界、缺失值、顺序和规模变换；
2. **Requirement synthesis**：根据未覆盖测试义务生成新 Case；
3. **Metamorphic testing**：构造输入变换和应保持/改变的关系，例如行顺序变化不应改变聚合结果；
4. **Fault injection draft**：声明工具超时、错误码、畸形结果等故障场景；仅在 Runtime 支持注入时可执行；
5. **Session mining**：从真实 Session 中抽取匿名化失败模式和 Case 草稿。

每个生成 Case 必须记录：

- 覆盖哪些 requirement；
- 基于哪些 Skill source refs 或种子事实；
- fixture 如何获得；
- Oracle 如何得到；
- 是否需要用户确认；
- 当前 Runtime 是否可执行；
- 建议 split 和泄漏风险。

同一 Analyzer/模型基于 Skill 和种子 Case 生成的变体，不具备独立 holdout 的来源隔离，默认只能进入 dev/validation 草稿。真正 sealed holdout 应来自用户私有 Case、独立历史失败、外部 suite，或在 Planner/Optimizer 都不可见的 evaluator-only 数据源中生成。

### 4.5 Oracle 策略与可信级别

生成 Case 的真正难点不是 Prompt，而是成功标准。Oracle 必须按可信度分层：

| 级别 | 来源 | 能否直接成为 frozen hard gate |
|---|---|---|
| `deterministic` | Schema、精确值、文件状态、工具协议、数学不变量 | 可以 |
| `seed_derived` | 用户提供的已确认结果及其可证明变换 | 可以，需通过一致性检查 |
| `reference_differential` | 受信参考实现或命令 | 可以，需固定版本和 hash |
| `human_confirmed` | 用户确认的 rubric/偏好 | 可以，需保留审批记录 |
| `model_proposed` | 模型根据自然语言推断 | 不可以，只能作为校准草稿 |
| `unobservable` | 当前日志或 Runtime 无法观察 | 不可评测，必须成为 blocker/gap |

优先使用：

- exact/schema；
- invariant/metamorphic relation；
- artifact/workspace state；
- trace contract；
- reference implementation；
- 经用户确认的 rubric。

模型 Judge 只能作为经过校准的补充评分器，不能覆盖确定性 hard Grader，也不能单独授权修改 Skill。

### 4.6 Coverage Matrix

“全路径覆盖”对开放自然语言输入和概率型 Agent 不可证明。本项目提供有边界、可追踪的覆盖声明：

```text
Declared capability coverage
Branch/risk requirement coverage
Tool coverage
State-transition coverage
Oracle-ready coverage
Runtime-executable coverage
Dynamic exercised coverage
Mutation detection score
```

每个测试义务经过以下状态：

```text
declared -> planned -> case_generated -> oracle_ready
         -> executable -> calibrated -> exercised
```

覆盖率按维度分别报告，不用一个模糊总分掩盖缺口。若需要摘要分数，使用风险权重：

```text
weighted coverage = covered requirement weight / total requirement weight
```

报告必须同时给出分子、分母、范围、未覆盖项和原因。例如：

```text
能力覆盖             8 / 9
高风险分支覆盖       6 / 7
Runtime 可执行路径   12 / 16
已校准 Oracle        10 / 12
动态执行             9 / 10

未覆盖：
- browser 登录路径：Reference Runtime 不支持 browser
- CLI 部分成功后的回滚：Skill 未声明预期行为
```

### 4.7 Runtime Capability Gap

Planner 在 Case 生成前把 Capability Graph 与 Runtime Profile 做集合比较：

```text
required capabilities - runtime capabilities = capability gaps
```

缺口分为：

- `unsupported`：当前 Runtime 没有对应工具；
- `unobservable`：能执行但日志不足以评分；
- `unconfigured`：能力存在但权限/Profile 未配置；
- `unsafe`：需要越过当前受信边界；
- `unknown`：Skill 声明不清。

这可以防止系统为 CLI、browser 或网络路径生成看似完整、实际无法执行的 Pack。

Feasibility Router 按顺序检查：

```text
需要的观察通道是否存在
  -> Driver 能否准备输入/状态
  -> Runtime 是否具备工具/故障能力
  -> Grader 能否表达验证路径
  -> Oracle 是否达到可信级别
```

第一版优先复用现有组件：

| 验证目标 | 可用组件 |
|---|---|
| JSON 结构 | `json_schema` |
| JSON 字段/值 | `json_path` |
| 记录集合 | `record_match` |
| artifact 存在与内容 | `artifact_exists` + `json_schema/json_path` |
| 文件/行引用 | `source_reference` |
| 工具调用与顺序 | `trace_assert` |
| 文件副作用 | `workspace_diff` |

无法映射到现有组件的 requirement 保留为 `blocked_capability`，而不是降级成没有有效 Oracle 的 generic Case。

### 4.8 Pack Quality Gate

自动生成 Pack 后增加独立质量门禁：

1. 所有 capability、requirement、Case 和 Grader 引用可解析；
2. hard Oracle 必须达到允许的可信级别；
3. Case 不重复，split 无明显 fixture/字面量泄漏；
4. baseline 能产生完整 Observation；
5. 已知好样本应通过，已知坏样本应失败；
6. 重复执行时 evaluator flake 在阈值内；
7. Runtime capability gap 已确认或排除；
8. coverage blocker 为零或得到显式豁免。

为了验证生成 Pack 是否真的有区分能力，引入 **Mutation Calibration**：

- 基于能力图生成少量隔离的已知坏 Skill 变体，例如删除关键步骤、使用错误工具、跳过输入校验；
- 这些 mutant 只用于校准 EvalPack，不进入生产候选；
- `mutation score = 被测试捕获的有效 mutant / 可执行有效 mutant`；
- 未捕获 mutant 会转化为新的覆盖缺口，而不是直接修改 Grader 以“刷分”。

模型可以提出 mutation 草稿，但有效性、修改范围和预期缺陷必须由确定性规则或人工确认。

### 4.9 EvalPack 与 Skill 的双循环隔离

```text
测试设计循环：
Skill analysis -> Test Plan -> Pack draft -> calibration -> frozen revision N

Skill 优化循环：
frozen revision N -> baseline -> diagnose -> repair/tune -> validation/holdout
```

如果优化过程中发现新的测试缺口：

1. 当前实验继续使用 revision N，不修改考题；
2. 生成 `PackChangeProposal`；
3. 创建 revision N+1；
4. 重新运行原始 baseline 和已有候选；
5. 新旧结果不能直接混为同一个实验统计。

## 5. Failure Intelligence Plane

### 5.1 为什么需要单独的归因层

当前 `_has_non_skill_failure()` 只能判断 Scenario 是否存在 Runtime error、Grader `ERROR` 或 `NOT_EVALUABLE`。它能够阻止误修，但不能回答：

- CLI 是未安装、版本不兼容还是参数错误；
- 错误参数来自 Skill 指令还是 Agent 临时推理；
- Agent 是单次随机失误还是稳定规划缺陷；
- 失败来自 fixture、环境、运行时还是 evaluator；
- 即使根因不在 Skill，修改 Skill 是否仍可作为缓解手段。

因此归因结果必须把“故障发生在哪”与“允许改什么”分开。

### 5.2 故障分类

| 类别 | 典型证据 | 默认补救面 |
|---|---|---|
| `skill_instruction` | Skill 明确给出错误命令、遗漏必要步骤或规则冲突 | Skill |
| `agent_reasoning` | Skill 规则明确，但 Agent 偶发忽略、顺序错误或结论不一致 | 重试/Agent 配置；重复稳定后可用 Skill 缓解 |
| `agent_planning` | 未满足前置条件、跳过步骤、无恢复计划 | Agent 或 Skill 结构化提示 |
| `tool_selection` | 调用错误工具，且与 Skill/计划要求不一致 | 依据来源决定 Skill 或 Agent |
| `tool_argument` | 参数格式、路径或选项错误 | 依据指令和调用证据决定 |
| `cli_binary` | exit 127、ENOENT、可执行文件不存在 | 环境/安装 |
| `cli_version` | 版本探针或 stderr 表明选项/API 不兼容 | Runtime Profile/依赖锁定 |
| `cli_runtime` | CLI 自身非零退出、崩溃或非法输出 | CLI/输入/环境 |
| `permission` | EACCES、403、只读目录 | 权限/Profile |
| `authentication` | 401、凭证缺失/过期 | Profile/secret 配置 |
| `network` | DNS、连接、限流、服务端错误 | 环境/重试策略 |
| `runtime` | bridge 启动、超时、未知工具、最大步数 | Runtime/Profile |
| `fixture` | 测试数据缺失、损坏或不满足前置条件 | EvalPack fixture |
| `driver` | prepare/collect/cleanup 失败 | Driver |
| `grader` | Grader 配置或实现异常 | EvalPack/Grader |
| `oracle` | 成功标准冲突、缺失或不可观察 | EvalPack calibration |
| `unknown` | 证据不完整或存在多个同等解释 | 人工/追加诊断 |

这些一级类别是对 [SKILL_CATEGORY_AND_ITERATION_DESIGN.md](./SKILL_CATEGORY_AND_ITERATION_DESIGN.md) 中既有 Failure Card 分类的工程化扩展。实现时应提供稳定 reason-code 映射，而不是维护两套相互冲突的“谁背锅”体系。

### 5.3 证据层级

归因不能只依赖模型阅读日志后的主观判断。证据强度定义为：

| 强度 | 含义 |
|---|---|
| `direct` | 明确错误码、缺失文件、完整性失败、Grader 状态等直接事实 |
| `corroborated` | 多个独立信号或控制实验支持同一解释 |
| `inferred` | 与 Trace 和规则一致，但仍有其他解释 |
| `insufficient` | 关键 telemetry 缺失，不能安全分类 |

模型可以生成 `inferred` 假设和解释，但不能把它提升为 `direct/corroborated`。

### 5.4 Failure Card 契约

建议新增 `aceval.failure-card/v1`：

```json
{
  "api_version": "aceval.failure-card/v1",
  "failure_id": "f-001",
  "scenario_id": "case-malformed-csv",
  "symptom": "process exited with status 127",
  "category": "cli_binary",
  "origin_component": "external_dependency",
  "remediation_surface": "runtime_profile",
  "confidence": "direct",
  "evidence": [
    {
      "kind": "tool_result",
      "event_seq": 8,
      "tool": "process_exec",
      "exit_code": 127,
      "stderr_excerpt": "command not found"
    }
  ],
  "alternative_hypotheses": [],
  "patch_decision": "deny_skill_intervention",
  "skill_patch_authorized": false,
  "recommended_actions": ["安装并锁定 CLI 版本", "检查 PATH/Profile"],
  "additional_probe": null
}
```

关键字段：

- `origin_component`：失败最可能发生的位置；
- `remediation_surface`：建议修改的组件；
- `patch_decision`：`allow_skill_intervention | deny_skill_intervention | needs_more_evidence | none`；
- `skill_patch_authorized`：供报告使用的派生布尔值，只有 `allow_skill_intervention` 时为 true；
- `evidence`：必须能回指 Observation/Trace/Grade；
- `alternative_hypotheses`：防止把相关性写成唯一因果；
- `additional_probe`：证据不足时下一步最小诊断动作。

### 5.5 归因流水线

```text
Observation / ImportedRunBundle
              |
      1. Integrity & completeness
              |
      2. Runtime/tool health rules
              |
      3. Trace conformance analysis
              |
      4. Outcome/Grader analysis
              |
      5. Repeat or diagnostic probe
              |
      6. Evidence-backed classifier
              |
         Failure Cards
```

执行顺序必须先排除 evaluator 和基础设施问题，再分析 Skill 行为：

1. Pack、fixture、Subject 和运行配置是否完整；
2. Runtime 是否启动，工具是否存在，Observation 是否足够；
3. 实际工具序列是否满足 Test Plan/Skill 的步骤约束；
4. 结果失败是否可由输入、Oracle 或 Grader解释；
5. 必要时进行重复执行或最小控制探针；
6. 仍不确定时输出 `unknown`。

### 5.6 诊断探针

探针只能由受信代码发起，并受独立预算、权限和脱敏策略约束。可用探针包括：

- 同一 Skill/Case 重复执行，区分稳定缺陷与 flake；
- Runtime/Profile health check；
- Profile 声明的 CLI `--version` 或只读健康命令；
- 对同一工具参数做隔离重放；
- 使用固定 Mock 工具结果重放，隔离外部服务；
- 对比实际 Trace 与声明步骤；
- Imported Session 的 grader replay。

不执行以下行为：

- 为了诊断自动安装依赖；
- 自动更改权限、凭证或网络配置；
- 从 Pack 中读取任意 shell 命令后直接运行；
- 未经允许向外部服务发送新的有副作用请求。

### 5.7 Expected Fault

复杂测试可能故意注入 CLI 失败、权限拒绝或超时，用来验证 Agent 的恢复行为。此时外部错误信号本身不能把整个 Case 标记为基础设施阻塞。Scenario/Test Requirement 需要声明预期信号，例如：

```json
{
  "expected_signals": [
    {"code": "cli.permission_denied", "tool": "process_exec"}
  ],
  "expected_recovery": ["不留下部分产物", "返回结构化错误"]
}
```

Attributor 先将匹配信号标为 `expected_fault=true`，再判断 Agent 是否按 Oracle 完成恢复。没有恢复或产生未授权副作用属于 planning/recovery 行为失败，可以形成 Skill intervention；未声明的同类信号仍按外部故障 fail closed。

### 5.8 Skill 修改授权

修改授权不等于根因分类。决策规则建议为：

| 情况 | 是否进入 Skill repair |
|---|---|
| `skill_instruction` 且证据 `direct/corroborated` | 是 |
| 完整 Observation 下的确定性 hard `FAIL`，且无外部故障 | 是 |
| `agent_reasoning/planning` 多次稳定失败，并能定位缺失/歧义指令 | 可以，标记为 mitigation 而非根因修复 |
| CLI、权限、认证、网络、Runtime、Driver、Grader、Oracle 故障 | 否 |
| 单次 Agent flake | 否，先重复执行 |
| `unknown` 或 telemetry 不完整 | 否 |

Optimizer 只接收经过脱敏的可修复 Failure Card 和 dev evidence，不接收 validation/holdout 详情或私有 Oracle。

### 5.9 与现有 Orchestrator 的集成

当前：

```python
_has_non_skill_failure(run) -> bool
```

目标：

```python
FailureAttributor.attribute(run, context) -> DiagnosticReport

DiagnosticReport:
    failure_cards
    evaluable
    patch_decision
    skill_patch_allowed
    eligible_skill_failures
    blocked_reasons
    recommended_actions
```

Orchestrator 行为：

1. baseline dev 后生成 DiagnosticReport；
2. `evaluable=false` 时停止候选生成；
3. 只把 `eligible_skill_failures` 传给 Optimizer；
4. validation/holdout 出现外部故障时将实验标记为 inconclusive，而不是 candidate regression；
5. 报告同时展示 quality failure 与 infrastructure/evaluator failure；
6. `unknown` 默认 fail closed。

## 6. CLI 与真实工具支持

### 6.1 受控 Process Tool

为诊断 Skill 内 CLI 问题，D40 增加 `process_exec_v1`，但不开放自由 shell：

- Runtime Profile 声明可执行文件 allowlist；
- 只接受 argv 数组，不解析 shell 字符串；
- 禁止 `|`、重定向、命令替换和隐式 shell；
- 固定 workspace/cwd 边界；
- 环境变量 allowlist，secret 不写入 Trace；
- stdout/stderr、执行时间和文件变化有大小限制；
- 超时后终止进程；
- 可选只读或网络隔离 Worker；
- EvalPack 只能声明需要 `process_exec` capability，不能从 Manifest 注册任意可执行文件。

Canonical Trace 至少记录：

```json
{
  "kind": "tool_result",
  "tool": "process_exec",
  "payload": {
    "executable_id": "git",
    "argv_redacted": ["status", "--short"],
    "exit_code": 0,
    "timed_out": false,
    "stdout_ref": "blob:sha256:...",
    "stderr_ref": null
  }
}
```

日志保存引用和受限摘要，避免默认把大段源码、secret 或二进制嵌入报告。

### 6.2 公司 Session

现有 `ImportedRunBundle` 接入以下流程：

```text
session fetch/import
  -> completeness gate
  -> RuntimeResult/Observation
  -> EvalRun replay（可用的 Grader）
  -> Failure Attribution
  -> Failure Cards
  -> 可选 Case Draft
```

如果 Session 只有 output 而没有 Trace，只允许结果评分和低置信度分析；不能判断工具或 CLI 根因。若缺 artifact/workspace，则相应 Grader 必须保持 `NOT_EVALUABLE`。

Profile 的 Trace mapping 后续应支持标准化以下字段：

- tool name；
- argv/arguments；
- exit code/status；
- stdout/stderr 摘要或引用；
- timeout；
- HTTP status；
- duration；
- error kind；
- retry/attempt。

## 7. 新增模块与公共契约

建议模块映射：

| 文件 | 职责 |
|---|---|
| `src/aceval/skill_analysis.py` | Inventory、语义分析、Capability Graph 校验 |
| `src/aceval/test_planning.py` | Test Requirement、风险优先级和 split 规划 |
| `src/aceval/case_generation.py` | Case/fixture/Oracle 草稿和来源追踪 |
| `src/aceval/coverage.py` | 静态/动态 Coverage Matrix 与 gap |
| `src/aceval/pack_quality.py` | Oracle 可信级别、泄漏、flake、mutation calibration |
| `src/aceval/failure_attribution.py` | 规则、分类、Failure Card 和修改授权 |
| `src/aceval/diagnostic_probes.py` | 受控重复、health check 和 replay |
| `src/aceval/process_tool.py` | D40 allowlisted argv Process Tool |
| `src/aceval/replay.py` | Imported Session 到 EvalRun/Grader replay |

建议 Protocol：

```python
class SkillAnalyzer(Protocol):
    def analyze(self, subject, seed_cases, goal, runtime_profile): ...

class TestPlanner(Protocol):
    def plan(self, analysis, seed_cases, constraints): ...

class CaseGenerator(Protocol):
    def generate(self, plan, constraints): ...

class CoverageAnalyzer(Protocol):
    def evaluate(self, analysis, plan, pack=None, runs=()): ...

class FailureAttributor(Protocol):
    def attribute(self, run, context): ...
```

这些扩展属于应用/分析层。Kernel 仍只消费 frozen EvalPack、RuntimeResult、RunObservation 和 GradeResult。

## 8. CLI 与 Application Service 草案

### 8.1 CLI

```bash
# Skill + 少量 Case -> 能力图、测试计划和覆盖缺口
aceval plan \
  --subject ./my-skill \
  --cases seed-cases.json \
  --goal '准确处理输入，并在失败时不留下部分产物' \
  --runtime-profile reference.json \
  --output .aceval/plans/my-skill

# 从已确认计划生成 EvalPack draft
aceval pack generate \
  --from-plan .aceval/plans/my-skill/test-plan.json \
  --output .aceval/packs/my-skill-v1

# 查看 Pack 质量和覆盖门禁
aceval pack quality .aceval/packs/my-skill-v1

# 对已有运行或公司 Session 做故障归因
aceval diagnose --run .aceval/runs/exp-123
aceval session diagnose --input .aceval/imported/session.json --pack ./evalpack

# 快速入口；有 blocker 时停在计划/校准页
aceval doctor \
  --subject ./my-skill \
  --cases seed-cases.json \
  --goal '修复失败并减少工具调用' \
  --auto-plan \
  --runtime reference
```

### 8.2 Application Service

```python
analyze_skill(...)
create_test_plan(...)
generate_cases(...)
get_coverage_report(...)
generate_pack_from_plan(...)
calibrate_pack(...)
diagnose_run(...)
run_diagnostic_probe(...)
replay_session(...)
```

CLI、Console 和未来 API 共用这些服务，不各自实现规划、冻结或归因逻辑。

## 9. 本地数据布局

```text
.aceval/
  analyses/<analysis-id>/
    inventory.json
    capability-graph.json
    runtime-gaps.json
  plans/<plan-id>/
    test-plan.json
    case-drafts.json
    coverage.json
    unresolved-questions.json
  packs/<pack-id>/
  runs/<experiment-id>/
    report.json
    diagnostics.json
    failure-cards.json
    coverage-observed.json
```

所有分析产物记录：

- subject hash；
- seed Case hash；
- analyzer/model profile；
- Runtime Profile hash；
- 生成参数和预算；
- 父 revision。

分析结果不是冻结 EvalPack 的一部分，除非通过显式 `generate_pack_from_plan` 编译并校准。

## 10. D20 黑客松交付范围

### 10.1 P0：必须可演示

1. `SKILL.md + 2–5 seed cases + Goal` 生成 schema-valid Capability Graph；
2. 每个能力节点具有 source ref 或显式 `inferred` 标记；
3. 生成 happy/boundary/negative/step-order 四类测试义务；
4. 在现有文件工具和内置 Grader 能力内生成 6–12 个 Case 草稿；
5. 输出 Coverage Matrix、Runtime Gap 和 Freeze Blockers；
6. 只有 deterministic/seed-derived Oracle 可以自动进入 hard gate；
7. 对当前 Runtime、Driver、Grader、missing evidence 和常见 tool error 生成 Failure Card；
8. 用 Failure Card 替代黑盒布尔提示，已知非 Skill 故障不产生候选；
9. 导入一条带结构化 CLI/tool error 的公司 Session，生成不可修 Skill 的 Failure Card；
10. HTML 报告展示能力图摘要、覆盖缺口和故障归因；
11. 使用一个多步骤 Skill 展示“少量 Case -> 自动补 Case -> 发现缺口 -> 冻结 -> repair/tune”。

### 10.2 P1：时间允许

- 从已导入公司 Session 生成可 Replay 的 EvalRun/Case 草稿；
- 生成少量受限 mutant 并计算 mutation score；
- 在 Console/静态 HTML 中展示 requirement-to-case 矩阵；
- 对 canonical CLI trace 做 `cli_binary/permission/auth/network` 规则分类；
- 一个 Profile 固定、无副作用的 CLI health probe。

### 10.3 D20 明确不做

- 任意多文件/binary Skill bundle 自动分析；
- 自由 shell 或任意 CLI 执行；
- browser/network 全工具面；
- 自动冻结 model-proposed 主观 Oracle；
- 数学意义的全路径覆盖；
- 从单次日志宣称强因果根因。

## 11. D21–D40 求职作品范围

### D21–D24：规划与归因协议工程化

- 完成版本化 Analysis/TestPlan/Coverage/FailureCard Schema；
- 建立 3–5 个手工标注复杂 Skill 的 analyzer benchmark；
- 完成来源追踪、Case 去重、风险优先级和 Runtime gap；
- 建立故障规则库和 canonical failure fixture suite。

### D25–D28：Pack Quality 与产品入口

- Case/Oracle 草稿编译；
- mutation calibration；
- known-good/known-bad 区分能力；
- Application Service；
- 静态 HTML 和 Test Plan/Coverage 页面。

### D29–D32：公司 Session 闭环

- Imported Session -> EvalRun；
- 可用 Grader replay；
- Session Failure Cards；
- Session -> Case Draft；
- telemetry completeness 对归因置信度的门控。

### D33–D35：受控 CLI 与诊断探针

- `process_exec_v1`；
- executable/env allowlist；
- health probe、timeout、exit code、stdout/stderr 引用；
- CLI fault taxonomy；
- Mock tool replay 和最小对照诊断。

### D36–D38：重复执行与动态覆盖

- flake、均值、方差和置信区间；
- dynamic tool/state coverage；
- 相同 Case 的重复归因一致性；
- 经人工确认的主观 rubric Pilot。

### D39–D40：求职作品化

- Local Console 完整串联 Plan、Pack、Run、Diagnosis；
- CI Gate 和一键 Replay；
- FixedAgentTarget 最小迁移验证；
- 项目教程、视频、消融、限制和面试材料；
- 发布 `aceval 0.3.0`。

## 12. 验收与评测方法

### 12.1 Planner 自身评测

为 3–5 个代表性 Skill 手工建立 gold capability/test-requirement 标注，测量：

- capability precision/recall；
- source-ref validity；
- seed Case mapping accuracy；
- high-risk requirement recall；
- duplicate Case rate；
- unsupported Runtime path detection；
- invalid/untrusted Oracle rate；
- requirement-to-case traceability。

不以“生成 Case 数量”作为主要成功指标。

### 12.2 EvalPack 质量

- known-good pass rate；
- known-bad detection rate；
- mutation score；
- evaluator error/not-evaluable rate；
- evaluator flake；
- split leakage；
- coverage gap 数量和风险等级。

### 12.3 故障归因质量

建立标准故障 fixture：

- bridge 启动失败/超时/非法返回；
- unknown tool/max steps；
- command not found/version mismatch/non-zero exit；
- permission/auth/network；
- fixture/driver/grader/oracle；
- Agent 单次 flake；
- Skill 明确错误命令。

测量：

- 分类 precision/recall；
- `unknown` 的合理拒判率；
- 非 Skill 故障误授权修改率；
- Failure Card evidence 可追踪率；
- 重复运行归因一致性。

最重要的安全指标是：

```text
known non-skill failure -> skill_patch_authorized 必须为 false
insufficient evidence   -> skill_patch_authorized 必须为 false
```

## 13. 安全、隐私与信息边界

- Analyzer 不读取 Skill 目录外文件；
- 所有模型输入记录文件/字节上限；
- source refs 只引用已冻结 Subject 内容；
- Session 和 CLI 输出进入模型前执行 secret/path/PII 脱敏；
- Failure Card 默认保存摘要和 blob ref，不复制完整 stderr/output；
- diagnostic probe 使用独立预算和显式 capability；
- Runtime Profile 决定工具权限，EvalPack 不授予新权限；
- planner/optimizer 不读取 validation/holdout 明文；
- model-proposed Oracle 不得自动升级为 hard gate；
- analysis revision、Pack revision 和实验结果分别 hash，禁止静默漂移。

## 14. 关键风险与控制

| 风险 | 控制 |
|---|---|
| 模型发明 Skill 未声明能力 | source refs + `inferred` 标记 + Schema 校验 |
| 自动生成大量低价值 Case | 风险预算、pairwise、去重和 Case 上限 |
| 自动 Oracle 自证正确 | 可信级别、freeze blocker、known-good/bad 和 mutation calibration |
| 覆盖率数字虚高 | 分维度报告、明确分母和 unsupported/unobservable 项 |
| 把 Agent 随机性误判为 Skill | 重复执行、flake 检查和保守授权 |
| 把 CLI/环境问题误修进 Skill | Failure Card、origin/remediation 分离和 fail closed |
| 归因模型输出看似权威 | 模型只能产生 inferred hypothesis，硬门控使用确定性证据 |
| Process Tool 扩大攻击面 | argv-only、allowlist、env 隔离、限制和 Worker |
| Pack 与 Skill 同时优化 | revision 隔离、冻结锁和重新 baseline |
| 公司日志不完整 | completeness gate 和 `unknown/not_evaluable` |

## 15. 可迁移价值

这套能力不依赖 Skill 永久存在。未来迁移到 Agent、Workflow、Tool-chain 或自动化流程时，可以复用：

- 能力图与 requirement-to-case traceability；
- 风险驱动测试规划；
- Runtime capability gap；
- Oracle 可信级别与 Pack Quality Gate；
- 动态工具/状态覆盖；
- evidence-backed failure attribution；
- origin 与 remediation surface 分离；
- dev/validation/holdout 和不可变评测 revision；
- 受控诊断探针与跨 Runtime Replay。

长期核心不是“自动改一份 `SKILL.md`”，而是：

> 将一个由模型驱动、依赖工具且行为不完全确定的能力，转换成可执行测试契约；在证据足够时定位可修改面，并在不可变评测标准下持续改进。

## 16. 最终决策

1. 复杂 Skill 自动生成不是直接扩展现有 Pack Builder，而是新增 Skill Analyzer、Test Planner、Coverage Analyzer 和 Pack Quality Gate；
2. 自动化目标是“有边界的高风险路径覆盖”，不承诺任意自然语言全路径覆盖；
3. Failure Attribution 替代当前粗粒度布尔门控，但保持 fail closed；
4. 根因组件与补救组件分开，非 Skill 根因不等于永远不能用 Skill 缓解；
5. D20 先覆盖当前 UTF-8 文件工具能力，并能明确报告 Runtime gap；
6. D40 再增加受控 CLI、公司 Session Replay、诊断探针和动态覆盖；
7. EvalPack 与 Skill 永远使用两个独立 revision/优化循环。
