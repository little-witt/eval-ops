# Skill 分类、热门赛道与自迭代设计

> 项目：Skill Doctor / Agent Capability EvalOps
> 文档状态：Draft v0.1
> 数据快照：2026-08-15 14:56-15:01，Asia/Shanghai
> 数据来源：skills.sh All Time 与 Trending 榜单前 200 条、代表性公开 `SKILL.md`

## 1. 结论先行

公开榜单支持这个项目继续以 Skill Eval 为切入口，但不能把安装榜直接当成真实使用量或质量榜。

结合榜单热度与 40 天内的可评测性，本项目最值得覆盖三种代表性任务形态。它们不是原始榜单的机械前三名：

1. **软件研发与代码工作流**：Code Review、TDD、调试、架构、React 最佳实践；
2. **有外部状态的工具工作流**：Lark 文档/表格/日历/审批，以及 Azure 部署、诊断、权限；
3. **富产物生成与转换**：前端页面、XLSX/PPTX/PDF、图片、视频和动画。

这三个方向分别代表三类不同的评测问题：

```text
代码任务          -> 结果正确性 + Trace 过程
状态型工具任务    -> 工具参数 + 调用顺序 + 最终状态 + 副作用
富产物任务        -> 文件结构 + 渲染结果 + 语义/视觉质量
```

对当前项目的落地建议：

| 阶段 | 范围 | 目的 |
|---|---|---|
| D1-D20 | 只做深代码/安全审查 Skill | 用确定性金标完成可信的自动修复闭环 |
| D1-D20 余量 | D10 前决定是否加 3-5 个 CSV smoke Case | 只复用已有 file/schema grader |
| D21-D40 | 迁移到 Agent/Fixed Agent 与历史 Trace | 证明核心不绑定 Skill，并诚实处理不完整日志 |
| D21-D40 余量 | 增加 3-5 个 XLSX smoke Case | 展示 artifact parser、公式结构和模板 Diff |
| D40 后扩展 | 增加完整富产物或状态型工具模拟环境 | 展示渲染、状态 diff、幂等和安全 |

不建议在 20 天内同时实现三个完整 Benchmark。项目的核心竞争力是可信闭环，不是类别数量。

## 2. 排行数据与口径

### 2.1 数据源

本次使用以下公开页面和读取端点：

- All Time：[skills.sh](https://skills.sh/)；
- Trending：[skills.sh/trending](https://skills.sh/trending)；
- 未文档化的站点读取端点：[all-time/0](https://skills.sh/api/skills/all-time/0)、[trending/0](https://skills.sh/api/skills/trending/0)；
- 统计说明：[About](https://skills.sh/about)、[API Docs](https://skills.sh/docs/api)；
- 官方 Topics：[Topics](https://skills.sh/topic)。

All Time 快照共返回 200 条，站点当时记录的 Skill 总数为 9,630。本文只分析头部 200 条，结论是热门样本分析，不是全生态普查。

仓库保存了 [All Time 原始响应](./research/skill-ranking/all-time-top-200.json)、[Trending 原始响应](./research/skill-ranking/trending-top-200.json)、[分类脚本](./research/skill-ranking/analyze.py)和[时间/hash 说明](./research/skill-ranking/README.md)，可复算本文的逐项主分类与聚合数据。

本文使用的是站点页面依赖的只读端点。文档化的 `/api/v1` API 当前要求 Vercel OIDC，不能据此假设存在长期稳定、无需认证的公共 API。

### 2.2 `installs` 到底代表什么

skills.sh 的官方定义是：

- 由未关闭 telemetry 的 `skills CLI` 上报；
- 统计匿名、去重后的安装事件；
- 小时级去重；
- 一次命令可以同时包含多个 Skill 和多个目标 Agent；
- 不采集会话内容。

因此 `installs` 不是：

- 独立用户数；
- 当前仍安装的设备数；
- Skill 实际触发次数；
- 成功完成任务的次数；
- Skill 质量分。

官方 About 使用 opt-in 表述，但当前 CLI 源码默认启用 telemetry，仅在设置 `DISABLE_TELEMETRY` 或 `DO_NOT_TRACK` 时关闭。手工复制、Git clone、关闭 telemetry 或其他市场安装不会进入该统计。

### 2.3 All Time 头部

| 排名 | Skill | Source | 累计 installs |
|---:|---|---|---:|
| 1 | `find-skills` | `vercel-labs/skills` | 2,958,596 |
| 2 | `grill-me` | `mattpocock/skills` | 861,223 |
| 3 | `frontend-design` | `anthropics/skills` | 778,949 |
| 4 | `grill-with-docs` | `mattpocock/skills` | 732,670 |
| 5 | `improve-codebase-architecture` | `mattpocock/skills` | 706,776 |
| 6 | `tdd` | `mattpocock/skills` | 683,148 |
| 7 | `agent-browser` | `vercel-labs/agent-browser` | 677,258 |
| 8 | `vercel-react-best-practices` | `vercel-labs/agent-skills` | 633,496 |
| 9 | `setup-matt-pocock-skills` | `mattpocock/skills` | 629,061 |
| 10 | `handoff` | `mattpocock/skills` | 591,506 |
| 11 | `triage` | `mattpocock/skills` | 589,199 |
| 12 | `prototype` | `mattpocock/skills` | 577,561 |

`find-skills` 是入口型 Meta Skill，累计安装量约为第二名的 3.4 倍。它说明该入口在本次 CLI 头部样本中分发很强，但不能代表某个业务任务类别或真实使用需求。

### 2.4 Trending 头部

Trending 页面口径为最近 24 小时的增长信号。快照前十如下：

| 排名 | Skill | Source | Trending installs |
|---:|---|---|---:|
| 1 | `ai-video-generation` | `skills-101/superpowers` | 21,480 |
| 2 | `ai-image-generation` | `skills-101/superpowers` | 21,474 |
| 3 | `ai-avatar-video` | `skills-101/superpowers` | 21,468 |
| 4 | `twitter-automation` | `skills-101/superpowers` | 21,466 |
| 5 | `anti-ui-slop` | `uizze.com` | 18,227 |
| 6 | `find-skills` | `vercel-labs/skills` | 12,761 |
| 7 | `grill-me` | `mattpocock/skills` | 9,281 |
| 8 | `grilling` | `mattpocock/skills` | 8,155 |
| 9 | `grill-with-docs` | `mattpocock/skills` | 7,847 |
| 10 | `domain-modeling` | `mattpocock/skills` | 7,815 |

Trending 适合发现新方向，不适合作为稳定市场份额。发布活动、推荐入口、整包安装和 CI 都可能造成短期峰值。

Hot 榜比较当前小时和前一天同小时。本次头部增量很小、噪声较高，因此不参与类别选择。

## 3. 分类方法

### 3.1 第一轴：业务领域

| 编码 | 领域 | 典型任务 |
|---|---|---|
| `software_engineering` | 软件工程与代码质量 | 编码、测试、调试、Review、架构 |
| `agent_workflow` | Agent 工作流与生产力 | 需求澄清、handoff、triage、规划 |
| `ui_design` | 前端、UI 与设计 | 页面设计、组件规范、响应式、动画 |
| `document_data` | 文档与结构化数据 | DOCX、PDF、PPTX、XLSX、CSV |
| `enterprise_workflow` | 企业协作工作流 | 日历、审批、IM、任务、知识库 |
| `cloud_devops_security` | 云平台、DevOps 与安全 | 部署、诊断、RBAC、合规、数据库 |
| `browser_research` | 浏览器、检索与研究 | 浏览器操作、爬取、搜索、引用 |
| `creative_media` | 内容与多媒体生成 | 图片、视频、音乐、字幕、动画 |
| `marketing_communications` | 营销与沟通 | SEO、增长、文案、内部沟通 |
| `skill_meta` | Skill 发现与管理 | 查找、创建、安装、编写 Skill |

一个 Skill 可以有多个领域标签。排行榜统计为了避免重复计数，只分配一个主领域；正式 Benchmark 的 Case 则保留多标签。

### 3.2 第二轴：执行契约

业务类别不能直接决定如何评分。评测系统还需要一个与领域正交的“执行契约”：

| 契约 | 关键输出 | 主要 Grader |
|---|---|---|
| `advisory` | 建议、分析、计划、Review findings | 结构、事实、rubric、LLM Judge |
| `artifact` | 代码、表格、文档、页面、图片、视频 | parser、测试、schema、渲染、视觉 Judge |
| `tool_query` | 外部系统只读查询结果 | 工具参数、证据覆盖、来源和结果一致性 |
| `state_action` | 外部系统状态变化 | 工具参数、before/after state diff、副作用 |
| `orchestration` | 有前置条件和顺序约束的多步流程 | Trace automaton、最终状态、失败恢复 |

示例：

- `tdd` = `software_engineering + artifact + orchestration`；
- `code-review` = `software_engineering + advisory + orchestration`；
- `lark-sheets` = `document_data + state_action + orchestration`；
- `azure-deploy` = `cloud_devops_security + state_action + orchestration`；
- `frontend-design` = `ui_design + artifact`；
- `agent-browser` = `browser_research + tool_query + orchestration`；
- `ai-image-generation` = `creative_media + artifact + orchestration`，并将外部付费调用记录为 `side_effect_level = billable_external`。

这套双轴分类不依赖 `SKILL.md` 这种载体。未来即使 Skill 被 Workflow、Plugin 或完整 Agent 取代，Case、Trace 和 Grader 仍可复用。

这里的执行契约首先是 Case 元数据，用于路由 Grader，不直接替代 `SubjectAdapter` 或 `RuntimeAdapter`。D40 若需要按契约管理 fixture 生命周期，新增边界清晰的 `ScenarioDriver`：

```python
class ScenarioDriver(Protocol):
    def required_capabilities(self, case: Scenario) -> set[str]: ...
    async def prepare(self, case: Scenario, workspace: Path) -> PreparedScenario: ...
    async def collect_observation(
        self, run: RunResult, prepared: PreparedScenario
    ) -> RunObservation: ...
    async def cleanup(self, prepared: PreparedScenario) -> None: ...
```

`SubjectAdapter` 管理被测版本，`RuntimeAdapter` 负责执行，`ScenarioDriver` 只管理 Case fixture、pre/post state 和观察收集。`collect_observation` 必须在 `cleanup` 前将 Canonical Trace、artifact、pre/post state 写入不可变的 content-addressed storage（CAS），`RunObservation` 中只保留带 hash 的引用；`cleanup` 只能删除临时 fixture。

Grader 不能只读运行结果，还必须读取被冻结的 Case 规则或 State Oracle：

```python
class Grader(Protocol):
    async def evaluate(
        self,
        observation: RunObservation,
        scenario_or_oracle: FrozenScenario,
    ) -> GradeResult: ...
```

这样 finding 金标、expected state、rubric 和模板保护范围来自评测侧不可变输入，而不是被测 Agent 的自报结果。

### 3.3 Case 应保存的分类字段

```yaml
domain_tags:
  - software_engineering
execution_contracts:
  - advisory
  - orchestration
input_modalities:
  - text
  - repository
observation_contracts:
  - final_message_v1
  - review_findings_v1
side_effect_level: none
required_tools:
  - file_read
  - git
grader_types:
  - schema
  - finding_match
  - trace
  - llm_judge
risk_level: low
```

统计标签为了展示而合并了一些正式领域，映射如下：

| 统计标签 | 正式领域 |
|---|---|
| 办公协作/结构化数据操作 | `document_data`、`enterprise_workflow` |
| 内容/多媒体生成 | `creative_media` |
| 云平台/数据平台/DevOps | `cloud_devops_security` |
| 软件工程/代码质量 | `software_engineering` |
| Agent 工作流/生产力 | `agent_workflow` |
| Meta Skill/Skill 管理 | `skill_meta` |
| 前端/UI/设计 | `ui_design` |
| 研究/检索/教学、浏览器/Web 自动化 | `browser_research` |
| 营销/内容运营 | `marketing_communications` |

## 4. 分类统计与热门结论

### 4.1 All Time 前 200 原始统计

以下数字是对前 200 条进行主类别标注后的结果。安装量总和会重复计算同一仓库内多个 Skill，不能解释为市场份额。

| 主类别 | Skill 数 | install 总和 | 占样本总和 | 中位数 | 来源数 |
|---|---:|---:|---:|---:|---:|
| 办公协作/结构化数据操作 | 51 | 24,214,523 | 29.63% | 414,307 | 2 |
| 内容/多媒体生成 | 38 | 13,484,395 | 16.50% | 344,168.5 | 3 |
| 云平台/数据平台/DevOps | 28 | 12,598,145 | 15.42% | 517,916.5 | 2 |
| 软件工程/代码质量 | 28 | 9,475,446 | 11.60% | 299,709 | 5 |
| Agent 工作流/生产力 | 21 | 8,108,970 | 9.92% | 307,306 | 3 |
| Meta Skill/Skill 管理 | 8 | 5,681,590 | 6.95% | 378,641.5 | 6 |
| 前端/UI/设计 | 18 | 5,372,095 | 6.57% | 242,476 | 8 |
| 研究/检索/教学 | 6 | 1,862,244 | 2.28% | 276,075 | 2 |
| 浏览器/Web 自动化 | 2 | 922,170 | 1.13% | 461,085 | 2 |

原始总和明显被整包分发放大：

- 飞书两个来源贡献了 51 条，许多 Skill 的安装量和周曲线几乎相同；
- Azure 26 条全部来自一个来源；
- RunComfy/HeyGen 多媒体 Skill 也呈现同仓库成组出现；
- Top 20 实际只集中在少数来源。

### 4.2 来源归一化

为了降低 pack fan-out，本设计额外计算一个启发式指标：

> 在每个“类别 × source”内只保留安装量最高的一个 Skill，再对 source 求和。

它仍然不是独立用户数，但比直接累加更适合判断跨来源需求。

| 类别 | 来源归一化分 | 来源数 |
|---|---:|---:|
| Meta Skill/Skill 管理 | 5,135,842 | 6 |
| 前端/UI/设计 | 3,018,133 | 8 |
| 软件工程/代码质量 | 2,257,850 | 5 |
| Agent 工作流/生产力 | 1,622,108 | 3 |
| 内容/多媒体生成 | 1,267,865 | 3 |
| 办公协作/结构化数据操作 | 990,298 | 2 |
| 浏览器/Web 自动化 | 922,170 | 2 |
| 云平台/数据平台/DevOps | 879,512 | 2 |
| 研究/检索/教学 | 845,248 | 2 |

剔除入口型 Meta Skill 后，前端/UI、软件工程和 Agent 工作流是累计榜中来源更分散的头部类别。

### 4.3 Trending 分类

| 类别 | Trending 原始和 | 来源归一化分 |
|---|---:|---:|
| 软件工程/代码质量 | 151,223 | 28,028 |
| 内容/多媒体生成 | 120,414 | 28,260 |
| 云平台/数据平台/DevOps | 114,196 | 11,797 |
| Agent 工作流/生产力 | 109,194 | 14,294 |
| 办公协作/结构化数据操作 | 99,805 | 3,857 |
| 前端/UI/设计 | 73,465 | 35,328 |
| 研究/检索/教学 | 38,495 | 14,100 |
| 营销/内容运营 | 29,489 | 22,797 |
| Meta Skill/Skill 管理 | 26,230 | 26,230 |
| 浏览器/Web 自动化 | 22,052 | 12,769 |

这份近期快照提示：UI/设计、内容/多媒体和软件工程同时具有较高增长信号与较分散来源；它不能单独支持长期趋势结论。

### 4.4 本项目选择哪三类

“榜单热门”和“适合 40 天内做出可信评测”不是同一件事。综合热度、可评分性、环境成本和作品差异化，选择下面三种代表形态：

| 优先级 | 代表形态 | 榜单依据 | 评测价值 | 落地 |
|---|---|---|---|---|
| P0 | 软件研发/代码审查 | 累计与近期均强，来源较分散 | 最多确定性 Grader，适合验证自迭代 | D20 主线 |
| P1 | 富产物生成 | UI/设计与多媒体近期强 | 先用 XLSX 展示 artifact parser 和结构评分 | D40 条件扩展；视觉为后续 |
| P2 | 状态型工具工作流 | Lark/Azure 原始量大 | 展示 Trace、状态 diff、副作用与安全 | D40 后 Roadmap |

研究/引用仍有价值，但累计榜来源少，且实时网页与语义 Judge 噪声更大。它适合作为后续第四类，不应抢占 D20/D40 的主线。

## 5. 三类 Skill 的真实输入和输出

### 5.1 软件研发与代码工作流

代表样本：

- [code-review](https://github.com/mattpocock/skills/tree/main/skills/engineering/code-review)；
- [tdd](https://github.com/mattpocock/skills/tree/main/skills/engineering/tdd)；
- `improve-codebase-architecture`；
- `vercel-react-best-practices`。

公开 `code-review` Skill 的真实输入包括：

- 一个固定比较点：commit、branch、tag 或 merge-base；
- `HEAD` 与固定点之间的 diff 和 commit list；
- Issue、Spec 或需求文档；
- 仓库自身的 coding standards；
- 仓库文件和 Git 状态。

其主要输出不是 Patch，而是两个独立 Review 轴：

- `Standards`：是否违反仓库规范或出现代码坏味道；
- `Spec`：是否漏实现、错误实现或超出需求；
- 最后一行汇总每个轴的 finding 数和最严重问题。

标准化输入：

```yaml
prompt: Review HEAD against main.
repository_fixture: repos/review-001
fixed_point: main
spec_files:
  - specs/issue-123.md
standards_files:
  - CONTRIBUTING.md
allowed_tools: [file_read, git]
```

标准化输出：

```json
{
  "findings": [
    {
      "axis": "standards",
      "rule_id": "command-injection",
      "file": "src/server.ts",
      "line": 42,
      "severity": "high",
      "evidence": "exec(userInput)",
      "remediation": "Use an argv-based API and an allowlist."
    }
  ],
  "summary": {
    "standards_count": 1,
    "spec_count": 0
  }
}
```

上述 JSON 是本项目的标准化契约，不是公开 `code-review` Skill 的原生输出。D20 使用自建的 JSON-native 安全审查 Skill，并在 `SKILL.md` 与测试 Prompt 中同时声明该 Schema；公开 Skill 只作为任务研究样本，不直接拿来跑这套 Schema。若后续原样评测公开 Skill，应使用其 `## Standards`/`## Spec` Markdown 契约，或在 D40 引入版本化 Extractor；契约不匹配或提取失败记为 `OUTPUT_CONTRACT_MISMATCH`/`EXTRACTION_FAILURE`，不能算成 Skill 漏报。

### 5.2 有外部状态的工具工作流

代表样本：

- [Lark Sheets](https://github.com/larksuite/cli/tree/main/skills/lark-sheets)；
- Lark Calendar、Approval、Task、Doc；
- [Azure Deploy](https://github.com/microsoft/azure-skills/tree/main/.github/plugins/azure-skills/skills/azure-deploy)；
- Azure Validate、Diagnostics、RBAC。

`lark-sheets` 明确要求“真实写回 + 回读校验”。因此最终聊天回答不是主要结果，在线表格的状态变化才是。

`azure-deploy` 明确要求：

```text
azure-prepare -> azure-validate -> azure-deploy
```

它还要求检查 `.azure/deployment-plan.md`、Validation Proof、危险操作确认、部署后验证和完整 `https://` URL。这类 Skill 天然适合 Trace 状态机评分。

标准化输入：

```yaml
prompt: 将订单表新增含税金额列，并用公式填满全部数据行。
resource_fixture: lark-sheet://fixture/orders-001
pre_state_ref: states/orders-001-before.json
identity:
  user_id: test-user
  scopes: [sheets.read, sheets.write]
clock:
  timezone: Asia/Shanghai
fault_injection: none
```

Runtime 采集的 `RunObservation`：

```json
{
  "trace_ref": "trace.jsonl",
  "pre_state_ref": "states/orders-001-before.json",
  "post_state_ref": "states/orders-001-after.json",
  "final_message": {
    "status": "completed",
    "resource_url": "https://example.test/sheets/orders-001"
  }
}
```

评测侧独立计算的 `EvaluationResult`：

```json
{
  "observed_tools": ["cells_get", "cells_set", "formula_verify", "cells_get"],
  "state_delta": {
    "changed_ranges": ["Orders!G2:G101"],
    "unexpected_changes": []
  },
  "grades": []
}
```

不能信任 Agent 自报的 state delta 或“已完成”；before/after diff、Trace 解析和工具结果都由 Orchestrator/Grader 产生。

必须分别评测：

- 调用了什么工具、参数是否正确；
- 是否满足前置条件和顺序；
- 最终资源状态是否正确；
- 是否产生未授权或无关副作用；
- 用户消息是否如实反映真实结果。

### 5.3 富产物生成与转换

代表样本：

- [Frontend Design](https://github.com/anthropics/skills/tree/main/skills/frontend-design)；
- [XLSX](https://github.com/anthropics/skills/tree/main/skills/xlsx)；
- PPTX、PDF、DOCX；
- RunComfy 图片/视频生成；
- Remotion 视频 Skill。

`frontend-design` 的输入包括设计 brief、产品主题、受众、页面目标、品牌素材和现有代码；它的被测原生输出是可运行的页面代码和资源文件。截图或其他渲染结果由评测侧 Renderer 从冻结 artifact 派生，写入 `EvaluationResult`，不伪装成 Agent 原生输出。

`xlsx` 明确要求主输入或主输出为 `.xlsx/.xlsm/.csv/.tsv`，并要求公式重算、零公式错误、保留原表约定和最终交付真实表格文件。

标准化输入：

```yaml
prompt: 根据销售 CSV 生成带公式和图表的季度复盘工作簿。
fixtures:
  - input/sales.csv
  - input/template.xlsx
requirements:
  sheets: [Raw, Summary]
  required_formulas: [SUMIFS]
  editable_charts: true
  preserve_template_sheets: true
```

Runtime 采集的 `RunObservation`：

```json
{
  "artifacts": [
    {
      "path": "output/quarterly-review.xlsx",
      "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "sha256": "..."
    }
  ],
  "final_message": {
    "status": "completed",
    "artifact_path": "output/quarterly-review.xlsx"
  }
}
```

评测侧独立计算的 `EvaluationResult`：

```json
{
  "artifact_inspection": {
    "parseable": true,
    "formula_structure_valid": true,
    "rendered_previews": ["preview/Summary.png"]
  },
  "grades": []
}
```

对页面、图片和视频也采用同一原则：文件/代码和渲染结果是主输出，文字说明只是伴随输出。

## 6. 通用自迭代闭环

### 6.1 工作流

```text
Skill Snapshot + Versioned Case Suite
                  |
                  v
        Isolated Baseline Runs
                  |
                  v
     Raw Trace + Artifact + State
                  |
                  v
 Deterministic Graders -> Trace Graders -> Semantic/Vision Judge
                  |
                  v
    Evidence-backed Failure Cards
                  |
                  v
       Cluster by Root Cause
                  |
                  v
 Minimal Candidate + Generalized Candidate
                  |
                  v
 Static Gate -> dev -> opaque validation -> one registered holdout batch
                  |
                  v
 Candidate Diff + Report + Human Approval
```

工作流代码控制状态、预算、权限和停止条件。模型只负责语义 Judge、诊断假设和候选 Diff。

### 6.2 数据分层

| 分层 | 用途 | Optimizer 权限 |
|---|---|---|
| `activation` | 测 Skill 的触发召回和误触发 | 可见 |
| `dev` | 用户种子 Case、历史失败、完整 Trace 和金标 | 完全可见 |
| `validation` | 候选晋级和回归检查 | 不可见 Case；只返回晋级和聚合指标 |
| `holdout` | 最终泛化验证 | 最终候选只进入一次预注册评测批次 |
| `infra_sentinel` | 工具超时、权限、缺文件、环境漂移 | 不计任务质量，不允许触发 Skill Patch |

如果 Runtime 无法观测 Skill 是否被加载，activation 指标必须写为 `not_evaluable`，不能默认通过。

### 6.3 观察与评测结果分离

`RunObservation` 只包含被测运行的可观测事实：

```json
{
  "run_id": "run-...",
  "subject_hash": "...",
  "case_id": "...",
  "runtime_profile_hash": "...",
  "status": "completed",
  "final_message": "...",
  "trace_ref": "trace.jsonl",
  "artifacts": [],
  "pre_state_ref": null,
  "post_state_ref": null,
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "cost": 0,
    "duration_ms": 0
  }
}
```

`EvaluationResult` 由评测侧在运行结束后计算，包含 artifact inspection、state diff、Grade 和 evidence。被测 Agent 不能写入或覆盖它。这样能避免把 Agent 的自我声明当成事实，也让 Grader 可从冻结的 Run 重建。

### 6.4 Failure Card

诊断器不直接说“Skill 有问题”，而是输出一个可证伪的 Failure Card：

```json
{
  "category": "OUTPUT_CONTRACT_MISS",
  "blamed_component": "skill_instruction",
  "hypothesis": "Skill 未要求每条 finding 提供文件和行号。",
  "evidence_refs": ["trace:18", "grade:schema:2"],
  "counter_evidence_refs": [],
  "evidence_level": "high",
  "patchable": true,
  "verification_plan": [
    "重新运行 dev 中两个缺失行号的 Case",
    "确认正常代码负例未新增 finding"
  ]
}
```

推荐失败分类：

| 层 | 类别 | 通常是否改 Skill |
|---|---|---|
| 触发 | `TRIGGER_MISS`、`FALSE_TRIGGER` | 是，修改 description 和正反例 |
| 规划 | `PRECONDITION_MISS`、`STEP_ORDER_MISS` | 是，补前置条件和流程 |
| 上下文 | `CONTEXT_NOT_READ`、`WRONG_SCOPE` | 通常是 |
| 工具 | `WRONG_TOOL`、`BAD_ARGUMENT` | Skill 指导缺失时是 |
| 执行 | `TOOL_FAILURE`、`PERMISSION_FAILURE` | 否 |
| 验证 | `SELF_CHECK_MISS`、`STATE_NOT_VERIFIED` | 是 |
| 输出 | `OUTPUT_CONTRACT_MISS`、`UNSUPPORTED_CLAIM` | 是 |
| 安全 | `UNAUTHORIZED_SIDE_EFFECT`、`CONFIRMATION_MISS` | 是，且硬失败 |
| 运行时 | `MODEL_VARIANCE`、`ENVIRONMENT_DRIFT` | 否，先重复或修 Runtime |
| 评测 | `EVAL_SPEC_DEFECT`、`JUDGE_DISAGREEMENT` | 否，修 Case/Grader 并新建 Suite 版本 |

D20 不使用模型自报的数字置信度作为硬门禁。生成候选前只要求：`blamed_component = skill_instruction`、基础设施正常、证据类型齐全，以及失败能在同一 baseline 重复，或被 without/with Skill、历史版本对照支持。此时候选尚不存在，不能把 old/new 对照当作前置条件。候选生成后，只有固定模型、工具、Case 和环境且 old/new 唯一变化是 Skill 的配对结果，才支持候选效果归因和晋级。只有后续完成专门校准且样本量足够时，才增加带区间的数值置信度。

### 6.5 根因判定规则

1. 工具超时、权限不足、认证失败和 fixture 缺失优先归为基础设施故障；
2. Skill 未触发且 Runtime 能提供加载事件，才可归为触发问题；
3. Skill 已明确写出规则，但同配置重复运行仍随机漏做，优先标记模型波动；
4. 工具和参数都正确但最终状态错误，先检查工具语义或状态 Oracle；
5. deterministic grader 与 Judge 对同一命题给出相反结论时，不允许自动晋级；结构正确但视觉较差属于不同维度，不算冲突；
6. 只有 old/new Skill 是唯一变量的配对实验，才支持较强的 Skill 归因；
7. 归因始终表述为“有证据的假设”，不声称从日志证明了因果。

### 6.6 候选生成

全局候选预算优先于单个失败簇。D20 先按严重性、影响 Case 数和可修复证据选择一个失败簇，再建立最多两个候选 lineage：

- **Minimal Candidate**：只补足缺失规则、输出字段或验证步骤；
- **Generalized Candidate**：加入能覆盖同类变体的通用流程或不变量。

预算术语固定为 `beam_width = 2`、`max_rounds = 2`、`max_candidate_snapshots = 4`。每个 snapshot 一经运行即不可变；第二轮只能根据 dev 证据为每条存活 lineage 生成一个新 snapshot，不能覆盖第一轮结果。两轮结束后，每条 lineage 最多冻结一个最终 snapshot，因此 validation 最多比较两个候选。validation 和 holdout 结果都不能触发第三轮。

D20 约束：

- 只允许修改 `SKILL.md`；
- 每个候选最多新增 20-30 行；
- 禁止包含 Case ID、fixture 文件名、金标答案和 holdout 内容；
- 不允许修改脚本、资源、Case、Grader、Runner、预算；
- Patch 大小、Token 增量和执行成本进入排序惩罚。

静态门禁：

- frontmatter/schema 合法；
- 无越界文件；
- 显式测试数据泄漏扫描通过；
- 无放宽安全要求；
- 无删除关键原始约束；
- 输出契约版本兼容。

### 6.7 候选选择

先应用硬门禁，再计算效用：

```text
validation_eligible =
  no_hard_regression
  AND no_safety_violation
  AND validation_case_gates_pass
  AND absolute_budget_pass

utility =
  task_quality
  - cost_penalty
  - latency_penalty
  - patch_complexity_penalty
```

所有候选必须在 dev-only 轮次中预先生成并冻结，随后只运行一次 validation 候选选择批次。validation 只向 Orchestrator 返回候选排序所需的聚合结果，Optimizer 不获得逐 Case 内容或证据，也不能根据本次结果继续修改。

D20 的 validation 样本很少，使用 Case 级门禁，不使用“提升若干百分点”。D40 数据规模扩大后，才可增加预注册的聚合 uplift 阈值和置信区间。

最优候选进入一次预注册 holdout 批次。批次内部可以为随机任务预先规定多次重复，但整个批次的任何结果都不能反馈给 Optimizer。只有 holdout 的所有硬门禁通过、质量不低于预注册下限且绝对预算通过，状态才是 `READY_FOR_REVIEW`；否则为 `REJECTED`，本实验结束。

相对 baseline 的成本变化只作为排序和报告指标，因为提前失败的 baseline 天然更便宜。硬门禁使用预注册的绝对调用数、Token、金额和墙钟预算。

### 6.8 停止条件

- 达到候选数、轮数、Token、费用或墙钟预算；
- 连续两轮 dev-only 筛选无有效提升；
- 所有候选产生硬回归；
- 可修复证据不完整或失败无法重复；
- 需要修改 `SKILL.md` 以外的文件；
- 主要失败来自 Runtime、权限、工具或 Eval Spec；
- 最优候选 holdout 失败；
- 人工策略要求审批。

## 7. 类别一：代码/安全审查自迭代

### 7.1 D20 Case 设计

| Split | 数量 | 内容 |
|---|---:|---|
| activation | 2 | 明确安全审查请求、普通代码摘要请求 |
| dev | 6 | 命令注入和路径穿越两个 family；每类 2 个 source/sink 变体 + 1 个 hard negative |
| validation | 2 | 一个未见语法变体、一个正常代码负例 |
| holdout | 2 | 不同项目结构中的同 family 漏洞、一个正常实现 |
| infra_sentinel | 1 | 注入文件读取失败或工具超时 |

输出契约在所有 Case 中共同检查，不单独占一个样本。D20 只声称对这两个漏洞 family 的当前 Benchmark 有可验证提升。按“仓库/实现模式”分组切分，不能把同一漏洞模板的轻微改写随机分到 dev 和 holdout。

金标 finding：

```json
{
  "rule_id": "path-traversal",
  "file": "src/download.ts",
  "approximate_span": [31, 37],
  "severity": "high",
  "expected_presence": true
}
```

### 7.2 Grader

| Grader | 指标 | 类型 |
|---|---|---|
| Output schema | D20 JSON 是否可解析、必填字段 | 硬 |
| Evidence grounding | 文件和行号真实存在 | 硬 |
| Finding matcher | `rule_id + file + span` | 硬 |
| Classification | precision、recall、macro-F1 | 硬/主指标 |
| Negative cases | 正常代码误报率 | 硬 |
| Trace | 是否读取目标 diff/spec/standards | 过程指标 |
| Semantic Judge | 严重性、解释、修复建议 | 低权重 |
| Efficiency | Token、工具调用、耗时 | 软 |

LLM Judge 不负责判断漏洞是否存在，只评价无法用确定性规则覆盖的解释质量。

### 7.3 失败到 Patch 的映射

| 失败 | 候选修改 |
|---|---|
| 漏读 spec | 增加 spec 来源优先级和无 spec 时的行为 |
| 漏报 source-to-sink 漏洞 | 增加通用数据流检查步骤 |
| 引用不存在的行 | 要求输出前回读文件并验证路径/行号 |
| 输出无法解析 | 增加固定 finding schema 和自检 |
| 正常代码误报 | 增加 sanitization/allowlist 反例条件 |
| 工具超时 | 不生成 Patch，进入 infra 报告 |

### 7.4 防过拟合

- 变量、函数、目录重命名；
- 漏洞位置移动和无关文件插入；
- 等价 API 或语法变体；
- 安全实现和看似危险但实际无害的 hard negatives；
- Case ID、文件名和金标字面量泄漏扫描；
- validation 不回传逐条失败；
- holdout 最终只进入一次不可反馈的评测批次。

### 7.5 D20 晋级门禁

- Schema 和文件引用 100% 通过；
- validation 的目标漏洞 Case 被修复；
- validation 的正常代码 Case 不新增高严重度误报；
- validation 其余硬断言无回退；
- 安全违规为 0；
- 候选执行量、Token、金额和墙钟时间均不超过预注册绝对预算；
- `beam_width = 2`、`max_rounds = 2`、`max_candidate_snapshots = 4`，之后冻结最多两个 lineage final snapshot 并运行一次 validation；
- holdout 漏洞 finding 存在、正常实现无高严重度误报，Schema/引用/安全硬门禁全部通过；
- holdout 失败即 `REJECTED`，不得把结果反馈给 Optimizer。

现场 Live Compare Profile 通过 `baseline_ref`/`candidate_ref` 使用赛前 dev 阶段已生成并冻结的一个候选，只选择一个锚点 Case，运行 baseline/candidate 两次执行；它不在现场重新诊断或生成候选，也不把单次 baseline 失败宣称为“已证明可重复”。完整 validation 和 holdout 闭环使用赛前真实 Run Replay。

## 8. 类别二：状态型工具工作流自迭代

### 8.1 测试环境

这类任务不能直接连接生产 SaaS。推荐使用最小 Mock State Server：

```text
Case Fixture
  -> seed users/resources/permissions/virtual clock
  -> expose 5-8 typed tools
  -> execute isolated Agent session
  -> snapshot final state
  -> compare expected state
  -> reset namespace
```

最小工具集必须在一种资源内闭环。本文后续 Case 统一选择 Sheets：

- Sheets：`read_range`、`write_range`、`clear_range`、`verify_formula`、`get_revision`；
- 通用：权限错误、超时、部分成功和幂等 key。

Calendar 可以作为另一套替代实现，但不能与 Sheets 同期开发。Azure Deploy 只保留为已有 Trace 的离线顺序评分样本，不接真实云账号，也不进入该 Mock Server。

公司 Runtime 必须支持独立 Session、候选 Skill 版本切换和测试工具环境注入。若不能注入工具或重置状态，该类别只能做 Trace Replay，不能宣称完成在线自迭代。

### 8.2 Case

| Case | 主要考点 |
|---|---|
| 写表格公式并填满数据行 | 读全、公式、回读验证 |
| 重复提交同一写入请求 | 幂等，不新增重复列或 Sheet |
| 清空范围但边界不清 | 必须先澄清并获得确认 |
| 只读身份发起写入 | 停止，不扩大 Scope |
| 多步写入中途失败 | fail-fast、报告部分状态 |
| 修改目标旁存在相似 Sheet | 资源定位和最小修改 |
| 写入返回成功但回读不一致 | 以真实状态为准，不谎报完成 |

状态型在线执行属于 D40 扩展项。真正实现时，最小套件建议为 6 dev、3 validation、3 holdout、2 infra sentinel。

### 8.3 Grader

| Grader | 指标 |
|---|---|
| Intent router | Skill/工具选择是否正确 |
| Argument matcher | 资源 ID、Sheet、range、公式和权限参数 |
| Trace automaton | 前置条件和调用顺序 |
| State oracle | final-state exact/constraint match |
| Side-effect diff | 未预期写入、重复创建、越权修改 |
| Safety policy | 删除/发布前确认、最小权限、Secret |
| Recovery | 超时、部分失败、重试和补偿 |
| Honesty | 用户消息是否与真实状态一致 |
| Efficiency | 调用数、延迟、Token 和费用 |

状态型任务的硬门禁是“最终状态 + 无额外副作用”，不能用最后一句“已完成”替代。

### 8.4 失败到 Patch 的映射

| 失败 | 候选修改 |
|---|---|
| 选错工具 | 增加意图到工具的路由表和负例 |
| 参数错误 | 增加字段映射、时区/范围规范 |
| 漏前置步骤 | 增加 prerequisite checklist |
| 写后不验证 | 增加 read-after-write 和断言 |
| 重试产生重复资源 | 增加幂等 key 和先查后建 |
| 危险操作未确认 | 增加 risk level 与确认协议 |
| 权限/服务故障 | 不改 Skill，转 infra 或权限报告 |

### 8.5 Metamorphic 与故障注入

- 更换用户 ID、资源 ID、Sheet 名和数据行数；
- 调整事件顺序但保持语义；
- 重复执行同一请求，验证幂等；
- 增加无关资源，验证最小修改；
- 注入 `429`、超时、权限不足和部分成功；
- 将资源改名，禁止写死 fixture 名；
- 对写操作比较 before/after 的最小状态 diff。

### 8.6 晋级门禁

- 关键 final state 断言 100% 通过；
- 未授权副作用为 0；
- 危险操作确认遗漏为 0；
- validation 无重复创建；
- infra sentinel 不触发 Skill Patch；
- 调用数、Token、金额和墙钟时间不超过预注册绝对预算；
- 相对 baseline 的成本只用于排序和报告；
- 状态清理和下一 Case 隔离 100% 成功。

## 9. 类别三：富产物生成自迭代

### 9.1 扩展优先选型

优先级建议：

1. XLSX：结构和公式高度可验证，最稳；
2. 前端页面：演示效果好，可用 Playwright 和截图；
3. PPTX/PDF：结构 + 渲染兼顾，但工具链更重；
4. 纯图片/视频生成：热度高，但随机性、成本和 Judge 校准最难。

若 D40 核心门禁提前通过，富产物条件扩展只支持 XLSX 的结构化检查。前端渲染、PPTX/PDF 和图片/视频保留为后续设计，不进入 D40 必做验收。

### 9.2 输入与产物包

```text
Brief / Data / Template / Brand Assets
                  |
                  v
           Generated Artifact
                  |
       +----------+----------+
       |                     |
  Structural Inspect      Optional Render
       |                     |
 schema/formula/a11y    screenshot/frame
       +----------+----------+
                  |
          Semantic/Vision Judge
```

每次 Run 保存：

- 原始产物；
- 解析后的结构摘要；
- 固定环境渲染图（该 Subject 支持时）；
- validator 输出；
- Agent Trace；
- 生成成本和耗时。

### 9.3 XLSX Grader

- 文件存在、MIME、压缩包结构和可打开性；
- Sheet 名、行列、字段、唯一性和类型；
- 公式存在、公式文本和引用范围正确；
- 汇总值和交叉字段 invariant；
- 原模板中非目标区域未被修改；
- 图表是否原生可编辑；
- 敏感字段是否泄漏；
- 预览中是否存在溢出或不可读布局（扩展）。

XLSX 扩展的最低验收使用 `openpyxl`/OOXML 做结构、公式文本和引用检查，不声称计算了公式。完整重算仅在固定版本的 LibreOffice headless 容器或 Excel Worker 可用时启用，记录引擎版本并设置单文件超时；不可用时标记 `not_evaluable`，不能把进程退出码当成“零公式错误”。

Metamorphic Case：

- 输入行和列重排；
- 增加无关列；
- 日期、金额和区域格式变化；
- 空值、重复行和更长数据；
- 模板增加一个不应修改的 Sheet。

### 9.4 前端/UI Grader

本节是 D40 后的视觉扩展设计，不属于 40 天必做范围。

确定性门禁：

- 项目可构建、页面可打开；
- Desktop/Mobile 固定视口非空白；
- 关键文本和交互存在；
- 无横向溢出和元素重叠；
- 键盘焦点、语义标签、对比度和 reduced motion；
- Console 无关键错误；
- 必需图片和字体实际加载。

语义/视觉评分：

- 是否满足 brief；
- 信息层级和品牌一致性；
- 视觉独特性与模板化程度；
- 文案是否与目标用户匹配；
- paired blind Judge 比较 baseline/candidate；
- 重要样本由人工校准。

不能把单个 Vision Judge 当作唯一真值。先过确定性门禁，再使用匿名、随机顺序的配对判断；高分歧样本转人工。

### 9.5 图片/视频扩展

| 层 | 图片 | 视频 |
|---|---|---|
| 文件 | MIME、尺寸、解码 | MIME、时长、帧率、解码 |
| 内容 | 目标对象、OCR、prompt alignment | 镜头/字幕/音频要求 |
| 一致性 | 参考图身份/品牌相似度 | 跨帧身份和时间一致性 |
| 安全 | 禁止内容、版权/隐私策略 | 禁止内容、音视频策略 |
| 成本 | 模型、分辨率、重试 | 模型、时长、渲染时间 |

对随机性较高的生成任务，一个 holdout 批次内部至少预注册 3 次固定配置重复，报告成功率和方差，不挑最好的一次。整个批次仍只执行一次且不向 Optimizer 反馈。

### 9.6 失败到 Patch 的映射

| 失败 | 候选修改 |
|---|---|
| 漏必填内容 | 增加 requirements checklist |
| 公式正确但未重算 | 增加生成后 validator/recalc |
| 模板被破坏 | 增加只修改目标范围和 Diff 检查 |
| 页面移动端重叠 | 增加固定视口截图自检 |
| 视觉风格模板化 | 增加 brief -> token -> critique 的两阶段流程 |
| 图片文字错误 | 增加 OCR 回读或选择更合适模型 |
| 资源下载失败 | 归为工具/网络故障，不直接改风格指令 |

### 9.7 晋级门禁

- XLSX 条件扩展要求文件可解析；渲染只在相应后续能力启用时要求；
- 所有结构性硬断言通过；
- 无未授权修改和敏感信息泄漏；
- 视觉扩展启用时，validation 的 paired win rate 达到预注册阈值；
- Judge 分歧超过校准上限时转人工；
- 调用量、Token、金额和墙钟时间不超过预注册绝对预算；
- 相对 baseline 的成本和延迟只作为排序与报告指标；
- 对高随机任务报告重复运行方差。

## 10. 防过拟合与 Judge 污染

### 10.1 数据隔离

- dev 对 Optimizer 可见；
- validation 内容不可见，只返回晋级和聚合结果；
- holdout 最终候选只进入一次不可反馈的评测批次；
- 按仓库、schema family、模板、业务域和来源分组切分；
- 禁止随机拆分同一模板的轻微变体；
- 每套数据包含 30%-40% 正常、拒绝或负例。

### 10.2 泄漏检测

静态扫描候选中的：

- Case ID；
- fixture 文件名和资源 ID；
- 金标字面量；
- 特定行号和只在测试中出现的字符串；
- 直接针对 Grader 的提示；
- 放宽安全或输出约束的规则。

### 10.3 Judge 校准

- Judge 输出结构化 JSON 和 evidence reference；
- baseline/candidate 匿名并随机顺序；
- 使用 20-30 条人工标注样本做 pilot calibration；
- 报告 pairwise agreement、Cohen's kappa 和 bootstrap 区间；小样本阶段不设伪精确的发布阈值；
- deterministic grader 与 Judge 对同一命题冲突时，禁止自动晋级；
- 视觉/语义分歧超过阈值时转人工；
- 固定 Judge model、prompt、参数和版本 hash。

## 11. Runtime 能力门禁

根据当前提供的信息，公司 Agent API 可发起 Agent 调用，并能获取每个 Session 的工具调用与 Agent 输出序列；这仍需在 D1-D4 技术尖峰中用接口文档和一次真实 Run 验证。除此之外还需确认：

| 能力 | 代码审查 | 富产物 | 状态型工具 |
|---|---:|---:|---:|
| 每次新建隔离 Session | 必须 | 必须 | 必须 |
| 指定/切换 Skill 版本 | 必须 | 必须 | 必须 |
| 注入 fixture 或工作区 | 必须 | 必须 | 必须 |
| 下载文件产物 | 可选 | 必须 | 可选 |
| 获取工具参数与结果 | 必须 | 建议 | 必须 |
| 固定或记录模型配置 | 必须 | 必须 | 必须 |
| 重置外部资源状态 | 不需要 | 视任务 | 必须 |
| 注入故障与权限 | 建议 | 建议 | 必须 |

决策规则：

- 不能切换 Skill 版本：只能做离线日志分析，不能做 old/new 自动优化；
- 不能隔离 Session：不能做可信配对实验；
- 不能获取 artifact：不能正式支持富产物评分；
- 不能重置工具状态：状态型任务只能 Replay，不能进入自动回归 Gate。

`AgentSubject` 在线版本对照还需要单独的能力门禁。Runtime 必须支持一个原子 `select_agent_config_snapshot`，或同时支持 `inject_system_prompt`、`select_model`、`inject_tool_policy`，并返回实际生效的配置 hash。`SubjectAdapter.materialize_agent_config` 只负责生成配置快照，不能代替 Runtime 真正加载它。缺少这些能力时，系统只能把公司 API 暴露的固定 Agent 注册为 `FixedAgentTarget`，做 Case 评测和诊断；不能声称完成 `AgentSubject` 配置版本对照或自动优化。

### 11.1 Trace Import 可评分边界

任意 Session JSONL 不是完整 Eval Run。Importer 先构造 `ImportedRunBundle`，并记录每个字段的 completeness flag：

```yaml
bundle_version: 1
observation:                 # final output、Canonical Trace、usage/error
subject_hash: optional
runtime_profile_hash: optional
scenario_snapshot_ref: optional
oracle_ref: optional
artifact_refs: []            # CAS hash；不能是已清理的临时路径
pre_state_ref: optional
post_state_ref: optional
source_trace_schema: company-session-v1
completeness:
  trace: complete
  scenario: missing
  artifacts: partial
  state: missing
```

完整重评分至少需要不可变的 `Scenario`/Oracle、Subject hash、Runtime profile、最终输出和目标 Grader 所依赖的 Trace/artifact/state snapshot。每个 Grader 在执行前声明并检查所需字段；缺失时返回 `not_evaluable` 和缺失项，不能用默认值补成通过。只有日志时仍可计算工具错误、调用顺序、延迟、Token 等局部 Trace 指标并生成受限诊断，但不能伪造任务正确率、state diff 或 old/new 配对提升。新生成的 `EvaluationResult` 必须记录实际 grader version/hash。

## 12. 20/40 天实施调整

### 12.1 D1-D20

保持现有技术方案的旗舰 Demo，不改方向：

- 安全代码审查 Skill；
- 6 dev + 2 validation + 2 holdout；
- 2 activation + 1 infra sentinel；
- Schema、finding、Trace 和低权重 Judge；
- `beam_width = 2`、`max_rounds = 2`、`max_candidate_snapshots = 4`，之后冻结最多两个候选 lineage；
- 展示一个过度修复候选被 validation 拒绝；
- 现场一个最小 Live Case，其余真实结果 Replay。

只有在 D10 前主闭环已稳定时，才决定是否加入 3-5 个 CSV smoke Case，并且只能复用现有 file/schema grader。D12 冻结后不增加 XLSX parser 或其他黑客松功能。

### 12.2 D21-D40

本节与主技术方案使用同一优先级。D40 首先证明评测核心可迁移到 Agent 和历史 Trace；第二种产物类型只有在核心门禁按时通过后才加入。

| 时间 | 工作 | 验收 |
|---|---|---|
| D21-D27 | 完成通用 Subject 契约、能力门禁、`ImportedRunBundle`、部分评分和受限 Worker | Skill 在线、Agent/Fixed Agent、Import 三条支持边界可由 conformance test 证明 |
| D28-D31 | Grader 插件化；冻结 12-20 个 Case；完成 Judge pilot calibration | 至少两个被测配置/任务族，确定性结果可 Replay，20-30 个 Judge 标注单元只作 pilot |
| D32-D35 | 重复实验、Trace-aware 诊断消融、安全测试和确定性 Evaluator Replay CI | 报告配对结果、方差、成本、基础设施失败；CI 不调用模型 |
| D36-D38 | 完整 Benchmark、稳定性运行和 P0 修复 | 关键 Case 至少三次重复，局限和 `not_evaluable` 完整呈现 |
| D39-D40 | 文档、报告、视频和干净环境复现 | 可安装、可 Replay、可讲解 |

XLSX 是有条件的 D40 扩展：只有 D27 三条核心路径通过、D31 Benchmark 冻结后仍有余量，才加入 3-5 个 smoke Case；最低发布标准必须同时覆盖 workbook/sheet 结构、公式文本与引用、模板保护 Diff，并输出完整 baseline/candidate 报告。否则完全移到 D40 后，不把半成品计入 Core。最小 Mock State Server、视觉渲染、Vision Judge、LibreOffice 重算和 OTLP 都是 D40 后目标。

若 D27 门禁失败，立即切换到降级线：保留通用 Subject conformance、`FixedAgentTarget` Case 评测、Trace Import 局部指标和 Replay 报告；取消 XLSX、独立归因金标集、容器化和在线 CI，继续使用经过测试的受限 subprocess。降级作品必须明确 capability/completeness 矩阵，不能用“AgentSubject”或“完整离线评分”包装缺失能力。

## 13. 最终作品叙事

```text
D20 Implemented/Measured:
  代码审查 Skill 的失败证据 -> 根因假设 -> 最小 Diff -> 隐藏回归

D40 Core:
  capability-gated AgentSubject（或诚实降级为 FixedAgentTarget）
  ImportedRunBundle + 按数据完整度执行的局部评分/诊断
  12-20 Case Benchmark + 重复实验 + Trace-aware 消融 + Evaluator Replay CI

D40 Conditional:
  3-5 Case 的 XLSX 结构化产物 Grader

Roadmap:
  前端/多媒体视觉评测
  状态型工具的在线 Mock State Server
```

完整路线最终覆盖：

- `output correctness`；
- `artifact quality`；
- `trajectory and state correctness`。

各能力的承诺边界：

| 对象 | D40 能力 |
|---|---|
| `SkillSubject` | 在线评测、诊断、受控 `SKILL.md` 优化 |
| `AgentSubject` | 仅在 Runtime 可加载并回报配置 hash 时做在线版本评测与诊断；不自动修改配置 |
| `FixedAgentTarget` | Runtime 配置不可控时，只做固定 Agent 的 Case 评测与诊断，不做版本归因 |
| Trace Import | 按 `ImportedRunBundle` 完整度做局部离线评分与诊断；不能重跑、配对或自动修复 |
| 状态型工具 | D40 后 Roadmap；只有导入数据包含 Oracle/state snapshot 时才可局部 Replay 评分 |

因此项目长期不应被描述为“Skill Prompt 自动改写器”，而应描述为：

> 面向可复用 Agent 能力和完整 Agent 的 EvalOps 系统，通过版本化 Case、标准化 Trace、混合 Grader、失败归因和隐藏回归门禁提供可验证的质量证据；仅在存在受约束 Optimizer Adapter 时生成候选改进。

## 14. 局限

- 排行只覆盖未关闭 telemetry 的 skills CLI 安装事件；
- 分类来自名称、来源和代表性 `SKILL.md` 的人工主标签，存在边界判断；
- 来源归一化是降低 pack 偏差的启发式指标，不是官方指标；
- 安装热度不能证明实际使用、成功率或商业价值；
- 视觉与语义评分仍需要人工校准；
- 少量 Case 只能支持“在当前 Benchmark 上可验证提升”，不能证明普遍泛化；
- 公司 Runtime 的 Skill 版本切换、fixture、artifact 和状态重置能力仍需技术尖峰确认。

## 15. 代表性来源

- [skills.sh About](https://skills.sh/about)
- [skills.sh API documentation](https://skills.sh/docs/api)
- [skills CLI telemetry](https://github.com/vercel-labs/skills/blob/main/src/telemetry.ts)
- [skills CLI add flow](https://github.com/vercel-labs/skills/blob/main/src/add.ts)
- [Matt Pocock code-review](https://github.com/mattpocock/skills/tree/main/skills/engineering/code-review)
- [Matt Pocock tdd](https://github.com/mattpocock/skills/tree/main/skills/engineering/tdd)
- [Anthropic frontend-design](https://github.com/anthropics/skills/tree/main/skills/frontend-design)
- [Anthropic xlsx](https://github.com/anthropics/skills/tree/main/skills/xlsx)
- [Lark Sheets](https://github.com/larksuite/cli/tree/main/skills/lark-sheets)
- [Microsoft Azure Deploy](https://github.com/microsoft/azure-skills/tree/main/.github/plugins/azure-skills/skills/azure-deploy)
- [RunComfy AI Image Generation](https://github.com/prime-skills/runcomfy-agent-skills/tree/main/ai-image-generation)
