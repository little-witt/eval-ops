# 复杂 Skill 测试规划与故障归因技术方案

> 项目：Skill Doctor / Agent Capability EvalOps  
> 状态：D20 核心已实现；D40 扩展项在文中单独标注
> 日期：2026-08-18  
> 适用范围：D20 黑客松增强版与 D40 完整作品

## 1. 方案结论

现有 EvalOps Kernel、Reference Agent Runtime、EvalPack 生命周期和 repair/tune 门禁继续作为可信执行底座。D20 已在它们之前和之后分别实现两个独立能力面：

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

这不是把一个大模型直接放在循环中“自己出题、自己判卷、自己修改答案”。当前 D20 Planner 和 Failure Attribution 均为确定性实现：代码负责 source-grounded inventory、风险 Requirement、Case 选择、覆盖计算、冻结锁、故障规则和修改授权。D40 可以增加模型辅助语义理解、候选测试设计和解释，但其输出仍只能是草稿，不能绕过确定性门禁。

## 2. 当前能力与新增能力边界

| 能力 | D20 当前实现 | D40 后续增强 |
|---|---|---|
| Case 到 EvalPack | `plan -> pack generate --plan`，保留种子 Case 并生成 bounded Case 草稿 | seed expansion、metamorphic fixture 和 Session mining |
| 复杂 Skill 分析 | 确定性读取冻结 `SKILL.md`，生成 source-grounded Capability Graph、ambiguity 和 Runtime gap | 模型辅助语义层、多文件 Skill bundle 和外部契约 |
| 路径覆盖 | planned、executable、oracle-ready、显式 observed requirement coverage | EvalRun 自动动态工具/状态覆盖和统计聚合 |
| EvalPack 可信度 | design Sidecar、Oracle trust、critical coverage、Runtime/split/hash Freeze Gate | mutation、known-good/bad、evaluator flake 和 revision diff |
| 非 Skill 失败 | Diagnostic Signal、Failure Card、证据等级和 Patch Authorization 已接入 Orchestrator | 重复归因、探针和标注 Benchmark |
| CLI 问题 | Imported Session 可离线分类常见 CLI/tool 错误；Reference Runtime 不执行任意 CLI | 受控 argv Process Tool 和只读健康探针 |
| 公司 Session | fetch/import、completeness 和离线 `session diagnose` | CompanyRuntimeAdapter、EvalRun/Grader Replay 和 Case mining |

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
1. 冻结并扫描 UTF-8 SKILL.md
2. 提取能力、步骤、分支、工具、状态和副作用
3. 对照 Runtime Profile 标出可执行与不可执行路径
4. 将种子 Case 映射到测试义务
5. 在预算内补充 requirement-synthesis Case 草稿
6. 为每个 Case 标出 Oracle 策略、可信级别和待确认项
7. 展示 Test Plan、Coverage Matrix 和 Freeze Blockers
8. 用户只确认未解决的语义问题
9. 校准并冻结 EvalPack
10. 运行 baseline，生成 Failure Cards
11. 仅将允许 Skill 修改的失败交给 repair/tune
12. 经过 validation/holdout 输出候选和报告
```

对应 CLI 已实现为 `aceval plan`、`aceval pack generate --plan`、`aceval pack quality` 和 `aceval doctor --auto-plan`。系统不要求普通用户手写 Driver、Grader ID 或 Manifest；若生成草稿缺少语义 Oracle，或存在 critical/runtime blocker，会停在 calibration，用户仍需确认会改变成功标准的内容。

### 3.2 三种自动化结果

复杂 Skill 不应只有“生成成功/失败”两种结果：

| 结果 | 条件 | 下一步 |
|---|---|---|
| `ready_for_calibration` | Case 可执行，Oracle 可确定或由种子事实推导 | 自动进入校准预览 |
| `needs_user_input` | 成功标准主观、外部业务规则缺失或存在冲突 | 只询问会改变测试语义的问题 |
| `unsupported_runtime` | 当前 Runtime 缺少 CLI、network、browser 等能力 | 生成能力缺口报告，不生成虚假可执行 Case |

## 4. Test Intelligence Plane

### 4.1 输入与扫描范围（Implemented）

D20 当前支持：

- UTF-8 `SKILL.md`；
- `skill_markdown_v1` 冻结的可选 `subject.json` 参与 Subject hash，但当前不从中提取语义能力；
- 用户提供的种子 Case、Goal 和 Runtime Profile；
- `SKILL.md` 中可定位的标题、段落和行号。

D40 扩展：

- `scripts/`、`templates/`、`assets/` 等多文件 Skill bundle；
- 工具 Schema、CLI help/version 输出和外部 API 契约；
- 公司 Session 中实际出现过的能力与工具路径。

扫描分为两层，其中第一层已实现，第二层属于 D40：

1. **Deterministic Inventory（Implemented）**：标题、显式工具名、输入输出、前置条件、步骤、条件、状态/副作用/风险文本和 source span；
2. **Semantic Extraction（D40）**：模型辅助把更隐含的自然语言说明转换为结构化能力草稿。

模型输出必须通过 Schema、ID、引用存在性和 source span 校验。无法对应原文的节点必须标记 `inferred=true`，不能冒充 Skill 明确声明。

### 4.2 Capability Graph（Implemented）

当前版本化契约为 `aceval.skill-analysis/v1`：

```json
{
  "api_version": "aceval.skill-analysis/v1",
  "subject_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "source_path": "SKILL.md",
  "sections": [],
  "capabilities": [
    {
      "id": "cap.validate-input",
      "name": "校验输入数据",
      "source_refs": [
        {
          "path": "SKILL.md",
          "start_line": 18,
          "end_line": 25,
          "quote_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
          "binding": "explicit"
        }
      ],
      "inputs": ["workspace/*.csv"],
      "outputs": ["validation result"],
      "preconditions": ["input exists"],
      "steps": ["list files", "read input", "validate rows"],
      "branches": [
        {
          "id": "cap.validate-input.branch.malformed",
          "condition": "输入 malformed 时明确失败",
          "source_ref": {
            "path": "SKILL.md",
            "start_line": 23,
            "end_line": 23,
            "quote_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
            "binding": "explicit"
          }
        }
      ],
      "tools": ["list_files", "read_file"],
      "state_transitions": [],
      "side_effects": [],
      "risks": ["silent partial parse"],
      "inferred": false
    }
  ],
  "tools": [],
  "ambiguities": []
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

### 4.3 Test Requirement 与 Test Plan（Implemented）

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

Planner 已采用可解释的风险加权 greedy set-cover，而不是让模型自由决定最终测试集：

1. 为每个 requirement 计算 `risk_weight = severity × likelihood × side_effect × goal_relevance`；
2. 先把种子 Case 映射到它能覆盖的 requirement 集合；
3. 为未覆盖 requirement 生成候选 Case family；
4. Feasibility Router 删除当前 Runtime/Driver/Grader 无法执行的候选，并保留 gap；
5. 在 `max_generated_cases` 内贪心选择“新增风险覆盖 / 预计执行成本”最高的 Case；
6. critical requirement 无 Case 时形成 freeze blocker，不能被低风险覆盖率平均掉；
7. 输出选择理由和被预算淘汰的 requirement，便于用户调整预算。

### 4.4 Case 生成策略

当前 `compile_case_drafts()` 会保留种子 Case 的 fixture 和精确期望值，并为选中的未覆盖 Requirement 生成 Prompt、expected observables、来源、Runtime 要求和 Oracle trust 元数据；它不会自动发明 fixture 内容或语义金标。当前已实现 `Requirement synthesis`，其余策略属于 D40：

1. **Requirement synthesis（Implemented）**：根据未覆盖且当前 Runtime 可执行的测试义务生成 bounded Case 草稿；
2. **Seed expansion（D40）**：对种子 Case 做边界、缺失值、顺序和规模变换；
3. **Metamorphic testing（D40）**：构造输入变换和应保持/改变的关系，例如行顺序变化不应改变聚合结果；
4. **Fault injection draft（D40）**：声明工具超时、错误码、畸形结果等故障场景；仅在 Runtime 支持注入时可执行；
5. **Session mining（D40）**：从真实 Session 中抽取匿名化失败模式和 Case 草稿。

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

### 4.6 Coverage Matrix（Implemented core）

“全路径覆盖”对开放自然语言输入和概率型 Agent 不可证明。本项目提供有边界、可追踪的覆盖声明：

```text
Requirement planned coverage
Runtime-executable coverage
Oracle-ready coverage
Explicit observed coverage
```

每个测试义务经过以下状态：

```text
requirement -> planned case -> executable case -> oracle-ready case
                                      -> explicit observed evidence
```

覆盖率按维度分别报告，不用一个模糊总分掩盖缺口。若需要摘要分数，使用风险权重：

```text
weighted coverage = covered requirement weight / total requirement weight
```

报告必须同时给出分子、分母、范围、未覆盖项和原因。例如：

```text
Planned Requirement  8 / 9
Runtime 可执行路径   7 / 9
Oracle-ready         5 / 9
显式 observed        3 / 9

未覆盖：
- browser 登录路径：Reference Runtime 不支持 browser
- CLI 部分成功后的回滚：Skill 未声明预期行为
```

当前 `CoverageReport` 同时给出普通计数和风险权重分子/分母。`observed_case_ids` 或 `observed_requirement_ids` 必须由调用方显式提供，且不能把未知或不可执行 Case 标为 observed；EvalRun 完成后自动提取 tool/state transition 并回写 Coverage 尚属 D40。

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

### 4.8 Pack Quality Gate（Implemented core）

自动生成 Pack 后已经增加独立质量门禁。当前检查：

1. design Sidecar 引用、源 Subject hash、Test Plan hash 和 provenance 一致；
2. Capability、Requirement 和 Case cross-reference 可解析；
3. hard Oracle 达到允许的可信级别并存在实际断言；
4. generated Case 不进入 sealed holdout，Case family 不跨 split；
5. critical Requirement 有映射 Case，且 Runtime gap/不可执行 Case 不被隐藏；
6. Planner Freeze Blocker 为零。

`pack quality` 会输出 blocker/warning 以及 planned、critical、Oracle-ready coverage；`pack freeze --approve` 会再次执行同一门禁。没有 `metadata.test_design` 的手写/legacy Pack 保持原生命周期兼容行为。

D40 高级 Quality Gate 再增加 known-good/known-bad、evaluator flake、Pack revision diff 和 **Mutation Calibration**：

- 基于能力图生成少量隔离的已知坏 Skill 变体，例如删除关键步骤、使用错误工具、跳过输入校验；
- 这些 mutant 只用于校准 EvalPack，不进入生产候选；
- `mutation score = 被测试捕获的有效 mutant / 可执行有效 mutant`；
- 未捕获 mutant 会转化为新的覆盖缺口，而不是直接修改 Grader 以“刷分”。

当前实现不会自动生成 mutant。`generation-provenance.json` 中如存在 `mutation_score`，Quality Gate 只验证它是 `[0, 1]` 内的有限数；不能把该字段描述为已完成 mutation calibration。未来模型可以提出 mutation 草稿，但有效性、修改范围和预期缺陷必须由确定性规则或人工确认。

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

D20 之前 `_has_non_skill_failure()` 只能判断 Scenario 是否存在 Runtime error、Grader `ERROR` 或 `NOT_EVALUABLE`。当前已实现的 Attributor 能进一步生成结构化 Card 和修改授权，但单次日志仍不能可靠回答所有因果问题，例如：

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

当前 Failure Card 的 `category` 使用 `execution | evaluator | evidence_gap | tool_or_dependency | quality_failure | recovery_failure` 等稳定大类，上表更细的名称通过 `reason_code`、`observed_component` 和 `remediation_surface` 表达。规则覆盖 runtime/driver/fixture/grader/oracle、command not found、permission/auth/network/timeout、unknown tool/bad argument、missing evidence、普通 hard failure 和 Expected Fault recovery。它是对 [SKILL_CATEGORY_AND_ITERATION_DESIGN.md](./SKILL_CATEGORY_AND_ITERATION_DESIGN.md) 中既有分类的工程化映射，不维护两套相互冲突的“谁背锅”体系。

### 5.3 证据层级

归因不能只依赖模型阅读日志后的主观判断。证据强度定义为：

| 强度 | 含义 |
|---|---|
| `direct` | 明确错误码、缺失文件、完整性失败、Grader 状态等直接事实 |
| `corroborated` | 多个独立信号或控制实验支持同一解释 |
| `inferred` | 与 Trace 和规则一致，但仍有其他解释 |
| `insufficient` | 关键 telemetry 缺失，不能安全分类 |

当前规则引擎直接产生这些等级，不调用模型。未来模型可以生成 `inferred` 假设和解释，但不能把它提升为 `direct/corroborated`。

### 5.4 Failure Card 契约

当前契约为 `aceval.failure-card/v1`，DiagnosticReport 为 `aceval.diagnostic-report/v1`：

```json
{
  "api_version": "aceval.failure-card/v1",
  "failure_id": "f-001",
  "scenario_id": "case-malformed-csv",
  "symptom": "process exited with status 127",
  "category": "tool_or_dependency",
  "observed_component": "cli",
  "remediation_surface": "runtime_profile",
  "confidence": "direct",
  "evidence": [
    {
      "kind": "diagnostic_signal",
      "ref": "trace:8",
      "code": "cli.binary_not_found",
      "data": {
        "tool": "process_exec",
        "exit_code": 127,
        "error_type": "command_not_found"
      }
    }
  ],
  "alternative_hypotheses": [],
  "patch_decision": "deny_skill_intervention",
  "skill_patch_authorized": false,
  "recommended_actions": ["安装并锁定 CLI 版本", "检查 PATH/Profile"],
  "reason_code": "cli.binary_not_found",
  "additional_probe": null,
  "expected_fault": false
}
```

关键字段：

- `observed_component`：失败信号被观察到的位置；
- `remediation_surface`：建议修改的组件；
- `patch_decision`：`allow_skill_intervention | deny_skill_intervention | needs_more_evidence | none`；
- `skill_patch_authorized`：供报告使用的派生布尔值，只有 `allow_skill_intervention` 时为 true；
- `evidence`：必须能回指 Observation/Trace/Grade；
- `alternative_hypotheses`：防止把相关性写成唯一因果；
- `additional_probe`：证据不足时下一步最小诊断动作。

### 5.5 归因流水线（Implemented core）

```text
Observation / ImportedRunBundle
              |
      1. Integrity & completeness
              |
      2. Runtime/tool health rules
              |
      3. Structured/common tool error rules
              |
      4. Outcome/Grader analysis
              |
      5. Evidence-backed classifier
              |
         Failure Cards
```

执行顺序必须先排除 evaluator 和基础设施问题，再分析 Skill 行为：

1. Pack、fixture、Subject 和运行配置是否完整；
2. Runtime 是否启动，工具是否存在，Observation 是否足够；
3. Trace 中是否有 expected/unexpected tool error、unknown tool 或 bad argument；
4. Grader 是否 `ERROR/NOT_EVALUABLE`，或是否存在无外部阻断的 hard `FAIL`；
5. 仍不确定时输出 `needs_more_evidence`。

当前未自动执行重复运行或控制探针，也未把 Test Plan 步骤约束自动编译成 Trace automaton；这些属于 D40。

### 5.6 诊断探针（D40）

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

修改授权不等于根因分类。当前决策规则为：

| 情况 | 是否进入 Skill repair |
|---|---|
| 完整 Observation 下的确定性 hard `FAIL`，且无外部故障 | 是 |
| CLI、权限、认证、网络、Runtime、Driver、Grader、Oracle 故障 | 否 |
| unknown tool 或 bad argument | `needs_more_evidence`，不修改 |
| telemetry/Observation 不完整 | 否 |
| 未匹配的执行异常 | `needs_more_evidence`，不修改 |

Optimizer 只接收经过脱敏的可修复 Failure Card 和 dev evidence，不接收 validation/holdout 详情或私有 Oracle。

### 5.9 与现有 Orchestrator 的集成（Implemented）

当前：

```python
FailureAttributor.attribute_scenario(evaluation, metadata) -> DiagnosticReport

DiagnosticReport:
    failure_cards
    evaluable
    patch_decision
    skill_patch_allowed
    eligible_skill_failures
    blocked_reasons
```

Orchestrator 行为：

1. 每个 Scenario 完成 Observation/Grade 后生成 DiagnosticReport；
2. `evaluable=false` 时停止候选生成；
3. 只把 `eligible_skill_failures` 的 Card 摘要附到 dev FailureEvidence 并传给 Optimizer；
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

当前已实现的离线流程是：

```text
session fetch/import
  -> completeness gate
  -> 保存/重载 canonical Observation
  -> session diagnose
  -> Failure Cards
```

离线命令不需要 Pack，也不执行 Grader；如果 Session 只有 output 而没有 Trace，不能判断工具或 CLI 根因，completeness 缺口会阻止 Skill Patch。D40 才接入 `ImportedRunBundle -> Pack/Scenario -> EvalRun -> Grader Replay`，届时若缺 artifact/workspace，则相应 Grader 必须保持 `NOT_EVALUABLE`。

当前 Importer 会保留 Canonical Trace 的 `payload` 扩展字段，Attributor 已能消费常见 `exit_code`、错误文本和 tool result；D40 的公司 Profile/Runtime 仍应稳定标准化以下字段：

- tool name；
- argv/arguments；
- exit code/status；
- stdout/stderr 摘要或引用；
- timeout；
- HTTP status；
- duration；
- error kind；
- retry/attempt。

## 7. 模块与公共契约

当前模块映射：

| 文件 | 职责 |
|---|---|
| `src/aceval/skill_analysis.py` | 确定性 Inventory、Capability Graph 和 source-ref 校验 |
| `src/aceval/test_planning.py` | Test Requirement、风险优先级和 split 规划 |
| `src/aceval/case_generation.py` | TestPlan 到可编辑 Case 草稿和来源追踪 |
| `src/aceval/coverage.py` | 静态/动态 Coverage Matrix 与 gap |
| `src/aceval/pack_quality.py` | 基础 Oracle/coverage/runtime/split/hash Freeze Gate |
| `src/aceval/failure_attribution.py` | 规则、分类、Failure Card 和修改授权 |
| `src/aceval/planning_workflow.py` | 规划产物写入、重载和一致性检查 |

D40 计划新增：

| 文件 | 职责 |
|---|---|
| `src/aceval/diagnostic_probes.py` | 受控重复、health check 和 replay |
| `src/aceval/process_tool.py` | D40 allowlisted argv Process Tool |
| `src/aceval/replay.py` | Imported Session 到 EvalRun/Grader replay |

当前类/函数入口等价于：

```python
class SkillAnalyzer:
    def analyze(self, source, *, source_path="SKILL.md"): ...

class TestPlanner:
    def plan(self, graph, *, seed_cases, goal, runtime_capabilities): ...

def compile_case_drafts(plan, seed_cases): ...

def build_coverage_report(plan, *, observed_case_ids=(), observed_requirement_ids=()): ...

class FailureAttributor:
    def attribute_scenario(self, evaluation, metadata=None): ...
```

这些扩展属于应用/分析层。Kernel 仍只消费 frozen EvalPack、RuntimeResult、RunObservation 和 GradeResult。

## 8. CLI 与 Application Service

### 8.1 CLI（Implemented）

```bash
# Skill + 少量 Case -> 能力图、测试计划和覆盖缺口
aceval plan \
  --subject ./my-skill \
  --cases seed-cases.json \
  --goal '准确处理输入，并在失败时不留下部分产物' \
  --runtime-profile reference \
  --output .aceval/plans/my-skill

# 从已确认计划生成 EvalPack draft
aceval pack generate \
  --plan .aceval/plans/my-skill \
  --type generic \
  --output .aceval/packs/my-skill-v1

# 查看 Pack 质量和覆盖门禁
aceval pack quality .aceval/packs/my-skill-v1

# 对已经归一化并保存的公司 Session 做离线故障归因
aceval session diagnose \
  --input .aceval/imported/session.json \
  --output .aceval/imported/session-diagnosis.json

# 快速入口；有 blocker 时停在计划/校准页
aceval doctor \
  --subject ./my-skill \
  --cases seed-cases.json \
  --goal '修复失败并减少工具调用' \
  --auto-plan \
  --approve-pack \
  --runtime reference \
  --model-command 'python model_bridge.py'
```

当前没有 `aceval diagnose --run`，也没有 `session diagnose --pack`；普通 EvalRun 的 Failure Attribution 已由 Orchestrator 自动执行。公司 Session 的 Pack/Grader Replay 属于 D40。

### 8.2 Application Service（D40）

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

当前 CLI 直接复用规划、Quality 和 Attribution 的 Python 模块；D40 抽取 Application Service 后，CLI、Console 和未来 API 共用服务层，不各自实现规划、冻结或归因逻辑。

## 9. 本地数据布局

```text
.aceval/
  plans/<plan-id>/
    capability-graph.json
    test-plan.json
    case-drafts.json
    coverage.json
    runtime-gaps.json
    generation-provenance.json
    seed-cases.json
    summary.json
  packs/<pack-id>/
    design/
      capability-graph.json
      test-plan.json
      coverage-target.json
      generation-provenance.json
  runs/<experiment-id>/
    report.json
    report.md
```

当前规划产物记录：

- subject hash；
- seed Case hash；
- analyzer/planner API version；
- Runtime capability 声明或 unknown 状态；
- Case 生成参数、选择决策和预算淘汰项。

独立规划目录不是冻结 EvalPack 的一部分；通过 `pack generate --plan` 编译后，四个 `design/` Sidecar 会进入 Pack 全树 hash 和 freeze lock。独立 Trace store、`diagnostics.json`、`failure-cards.json` 和自动 `coverage-observed.json` 尚属 D40。

## 10. D20 黑客松交付状态

### 10.1 已实现能力

1. `SKILL.md + seed cases + Goal` 生成 schema-valid Capability Graph；
2. 每个能力节点具有 line/quote hash source ref 或显式 `inferred` 标记；
3. 生成 happy、branch/negative、risk、recovery、idempotency、state-transition 和 diagnostic step-order Requirement；
4. 在当前 Runtime 能力与 `max_generated_cases` 预算内生成 bounded Case 草稿；
5. 输出 planned/executable/oracle-ready/observed Coverage、Runtime Gap 和 Freeze Blockers；
6. 未确认 Oracle、critical gap、Runtime gap、generated holdout、family leakage 和 hash mismatch 阻止冻结；
7. 对 Runtime、Driver、fixture、Grader、Oracle、missing evidence 和常见 tool error 生成 Failure Card；
8. Failure Card 与 Patch Authorization 已接入 Orchestrator，已知非 Skill 故障不产生候选；
9. 已保存的 Imported Session 可通过 `session diagnose` 生成不可修 Skill 的 Failure Card；
10. 全量自动测试通过。

### 10.2 黑客松仍需完成的演示交付

- 接入真实模型并完成可复现 repair/tune；
- 用一个多步骤 Skill 现场展示“少量 Case -> 自动补 Case 草稿 -> 发现缺口 -> 校准/冻结 -> repair/tune”；
- 准备一条离线 CLI/环境故障 Session 与一张允许 Skill intervention 的对照 Card；
- 生成静态 HTML 或录屏友好的结果页；当前只有 JSON/Markdown/CLI 输出。

### 10.3 D20 未实现、移入 D40

- 从已导入公司 Session 生成可 Replay 的 EvalRun/Case 草稿；
- 生成少量受限 mutant 并计算 mutation score；
- 在 Console/静态 HTML 中展示 requirement-to-case 矩阵；
- Web Console；
- 一个 Profile 固定、无副作用的 CLI health probe。

### 10.4 D20 明确不做

- 任意多文件/binary Skill bundle 自动分析；
- 自由 shell 或任意 CLI 执行；
- browser/network 全工具面；
- 自动冻结 model-proposed 主观 Oracle；
- 数学意义的全路径覆盖；
- 从单次日志宣称强因果根因。

## 11. D21–D40 完整作品范围

### D21–D24：规划与归因 Benchmark

- 保持已实现 Analysis/TestPlan/Coverage/FailureCard v1 兼容；
- 建立 3–5 个手工标注复杂 Skill 的 analyzer benchmark；
- 测量 capability/requirement recall、source-ref validity 和 seed mapping accuracy；
- 扩展 canonical failure fixture suite，测量分类、拒判和误授权率；
- 增加详细 Failure Card 持久化和 Run index。

### D25–D28：高级 Case/Pack Quality 与产品入口

- seed expansion、boundary/metamorphic fixture 变换；
- mutation calibration；
- known-good/known-bad 区分能力；
- evaluator flake 和 Pack revision diff；
- Application Service；
- 静态 HTML 和 Test Plan/Coverage 页面。

### D29–D32：公司 Session 闭环

- Imported Session -> EvalRun；
- 可用 Grader replay；
- 在线/Replay Session Failure Cards；
- Session -> Case Draft；
- Grader 级 telemetry completeness 对可重评分范围的门控。

### D33–D35：受控 CLI 与诊断探针

- `process_exec_v1`；
- executable/env allowlist；
- health probe、timeout、exit code、stdout/stderr 引用；
- CLI fault taxonomy；
- Mock tool replay 和最小对照诊断。

### D36–D38：重复执行与动态覆盖

- flake、均值、方差和置信区间；
- dynamic tool/state coverage；
- EvalRun -> observed coverage 自动接线；
- 相同 Case 的重复归因一致性；
- 经人工确认的主观 rubric Pilot。

### D39–D40：完整作品化

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

1. 复杂 Skill 自动生成已经通过独立 Skill Analyzer、Test Planner、Coverage 和 Pack Quality 模块实现，Pack Builder 继续只负责把确认后的设计编译成 EvalPack；
2. 自动化目标是“有边界的高风险路径覆盖”，不承诺任意自然语言全路径覆盖；
3. Failure Attribution 已替代优化主路径中的粗粒度布尔提示，同时保留 fail closed fallback；
4. 根因组件与补救组件分开，非 Skill 根因不等于永远不能用 Skill 缓解；
5. D20 已覆盖当前 UTF-8 文件工具能力，并能明确报告 Runtime gap；
6. D40 再增加受控 CLI、公司 Session Replay、诊断探针和动态覆盖；
7. EvalPack 与 Skill 永远使用两个独立 revision/优化循环。
