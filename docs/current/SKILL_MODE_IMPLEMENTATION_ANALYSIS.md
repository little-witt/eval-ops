# Skill 模式实现差异与闭环审计

## 结论先行

当前分支已经把“评测 → 证据 → 分析 → 候选 → 发布 → 下一轮评测”的关键控制面接到 Electron 界面，并补上了几个人工安全门；因此不能再把当前桌面端描述成“缺少蓝图和候选发布入口”。更准确的分层判断是：

1. **设计与 Kernel 层**：`repair`、`extend`、`discover`、`create` 的状态分流、Case/路径编译、证据有效性、多维评分、修改授权、候选发布和 Champion/Challenger 比较已有本地实现与测试。
2. **Electron Renderer**：已经提供能力蓝图/探索方案选择、EvalPack 与 Case 审阅、逐 Case 通过标准校准、修改范围选择、首版候选和普通候选的 Diff/校验/发布二次确认，以及下一轮继续执行按钮。
3. **严格 `tune` 语义**：仍只在旧 `EvalOrchestrator`/EvalPack 优化契约中完整；桌面 `IterationKernel` 的 `operation=tune` 尚没有 objective 字段和成对 objective 门禁，当前行为应被理解为“通用证据驱动迭代”，不能宣传为 Token/延迟/成本优化实验。
4. **V3 与真实远端验收**：Path IR、完整 EventJournal、Reference Run、Suite/Case Gate、Judge 专家校准和真实 CATX 两轮 E2E 仍未完成，不能据此宣称完整 V3 或生产闭环。

因此，当前版本可以诚实地称为：**本地关键闭环可用，桌面主要人工门已接通；严格 tune 和完整 V3/真实 CATX 仍是明确的后续工作。**

## 1. 两个容易混淆的“模式”

### 生命周期操作模式 `operation`

`KernelInput.operation` 支持 `auto / repair / tune / extend / discover / create`。`intent_mode()` 会结合仓库是否已有 `SKILL.md` 和输入材料解析实际路径：

| 输入 | 解析结果 |
|---|---|
| 显式 `repair/tune/extend/discover`，但没有已有 Skill | 拒绝 |
| 显式 `create`，但已有 Skill | 拒绝 |
| `auto` + 没有 Skill + 有目标/能力/Case/EvalPack | `create` |
| `auto` + 已有 Skill + 有 `capabilities` | `extend` |
| `auto` + 已有 Skill + 没有目标、标准、Case、EvalPack | `discover` |
| 其他已有 Skill 输入 | `auto`（普通评测迭代） |

所以 `auto` 是意图解析入口，不是一种绕过人工门的修改策略。

### 输入丰富度 `KernelInput.mode`

`goal_only`、`cases_with_expected`、`custom_evalpack`、`exploratory` 只描述输入材料，不等于修复、优化或新增能力等生命周期操作。

## 2. 各模式的当前实现与桌面入口

| 模式 | 前置条件 | Kernel 主链 | 当前 Electron 状态 |
|---|---|---|---|
| **自动识别 `auto`** | 由仓库和输入决定 | 路由到 `create`、`extend`、`discover` 或普通评测 | 创建页可选“自动识别”；解析出的人工门会在详情页停住 |
| **修复 `repair`** | 已有 `SKILL.md` | 编译 EvalPack/Case → CATX → 证据与多维评分 → 归因/提案 → 修改范围确认 → 候选生成 → 候选发布确认 → 成对回归 | 主链可用；设计审阅、修改范围和候选发布均有界面 |
| **通用迭代 `tune`** | 已有 `SKILL.md` | 与 repair 共用 `compile_design → evaluate → analyze → optimize` | 下拉项可用，但没有 objective 输入、objective 计算或 paired objective gate；不得当作严格成本/延迟优化 |
| **增加功能 `extend`** | 已有 `SKILL.md` | `created → blueprint_ready → created → design_ready → …`；选择的能力会带回归 Case、边界和文件计划 | `decision` 页显示能力卡片，可多选、填写反馈并批准/拒绝；批准后进入设计审阅 |
| **自动探索 `discover`** | 已有 `SKILL.md` | `created → discovery_ready`；至少两个 proposal，显式选择后转为 extend 链 | 同一能力审阅界面显示多个探索方向；默认选择仅为建议，提交时才记录批准 |
| **从零新增 `create`** | 不得已有 `SKILL.md`，且必须有目标/能力/Case/EvalPack 之一 | `created → blueprint_ready → ready_to_build → initial_candidate_ready → candidate_ready → created`；发布后先跑无 Skill 基线 | 能力蓝图批准、首版候选 Diff/校验二次确认、发布后基线与正式评测均有入口 |

核心状态分流为：

```text
created
├─ extend / discover → blueprint_ready → created → 评测设计
├─ create           → blueprint_ready → ready_to_build → initial_candidate_ready
└─ repair / tune / auto → 直接进入评测设计
```

`ready_to_build` 是自动生成阶段，不是缺少 UI 的人工确认门。

## 3. 公共评测—自迭代闭环

除蓝图/首版构建差异外，各路径最终汇入同一条链：

```text
输入与配置 hash 冻结
  → EvalPack / Case / Execution Path
  → 用户审阅、Case 选择与必要的通过标准校准
  → CATX 多会话执行
  → 完整日志、仓库绑定和环境证据回收
  → Evidence validity / Attempt / Case aggregate
  → 各维度评分、跨 Case 归因和修改提案
  → 用户批准最小修改范围
  → 受控候选生成（路径/行数/语法/敏感内容校验）
  → 用户查看 Diff 并批准候选发布
  → 下一轮同 Case、同冻结环境的 Champion/Challenger 评测
  → 晋升、拒绝恢复、证据不足等待补证或收敛
```

当前已落地的关键保护包括：

- 模型建议的 Case/Blueprint 预期保持 `model_proposed`，不会因批准方案而自动获得可信 Oracle；评测页可逐 Case 写入人工通过标准。
- Draft 或发生漂移的 EvalPack/Grader 不能授权修改；已经被完整性检查识别为无效的环境、日志或 Grader 结果不会被当成 Skill 失败。
- 每轮冻结 Trial Environment；下一轮 CATX binding 使用当前实际 Skill 资源闭包 hash，不沿用设计期旧 hash。
- 候选生成和候选发布分为两个门；未批准发布不会写入 Git。
- 成对比较遇到 Case 集不一致、环境上下文不一致或证据不足时，保留 Challenger 并进入 `needs_evidence`，不会误判为质量失败或自动恢复覆盖。

## 4. 严格 `tune` 为什么仍要单独看

旧 `EvalOrchestrator` 与 EvalPack 优化契约支持：

- 先验证 baseline 是否通过硬门；
- 要求可测的 `ObjectiveSpec`（例如 Token、延迟或成本）；
- 在 dev、validation 和可选 holdout 上做 baseline/candidate paired objective gate；
- 同时限制逐 Case 回退和安全硬门。

当前桌面创建请求只把 `operation`、目标、标准、能力、Case 和 EvalPack 路径传给 `KernelInput`；`KernelPolicy` 没有 objective 字段，`IterationKernel` 也没有 tune 专用 eligibility 或 paired objective gate。因此：

- 选择“优化”目前会走通用质量/证据迭代链；
- 要提供严格 tune，需把 objective schema、指标采集、dev/validation/holdout 比较和 UI 目标输入接到同一 Kernel；
- 在此之前，产品文案应明确写“通用迭代”，避免把质量改善误报为成本或延迟优化。

## 5. 当前 Electron 已接通的人工门

Renderer 的 `confirmationPhases` 已覆盖：

`blueprint_ready`、`discovery_ready`、`initial_candidate_ready`、`candidate_ready`、`design_ready`、`awaiting_confirmation`。

对应交互如下：

- **能力蓝图 / 探索方案**：`blueprintReview()` 展示问题、用户价值、触发条件、完成标准、工作流、预计文件、独立 Case 和风险；用户可多选并提交 `selected_capability_ids`。
- **EvalPack / Case 设计**：`casesTab()` 展示 Case 来源、映射、执行路径、可观察结果、Oracle 资格和证据；对未校准 Case 提供逐 Case 人工通过标准，提交 `selected_case_ids` 与 `case_calibrations`。
- **修改范围**：`decisionTab()` 展示问题事实、根因假设、目标文件、保护 Case 和提案；用户提交 `selected_change_ids`。
- **候选发布**：`candidatePublicationReview()` 同时用于首版和普通候选，展示变更文件、完整文本 Diff、本地校验和发布后行为；提交前不会写入仓库。
- **循环下一轮**：发布批准后，Kernel 进入 `evaluation_ready`；Renderer 的“继续自动执行”会重新调度同一 Case 与冻结运行条件。`run_until_gate()` 对未批准的 `candidate_ready` 显式停住。

因此，旧文档中“Electron 缺蓝图展示/批准、缺首版候选二次确认、普通候选自动发布”的判断已不再适用。

## 6. 仍未完成、但不应隐藏的边界

### V3 目标能力

- 原生 `path-analysis/v3`：条件边、exit code、retry/timeout、状态迁移、失败边、cleanup、循环上限和分支终点仍未全部成为可执行评测器；当前少量兼容输入不能替代完整 Path IR。
- 完整 EventJournal：尚无逐事件 hash chain、强制 terminal 配对、原始日志/规范化 Trace 分层和有界补抓状态机。
- CATX 绑定事件校验已收紧为结构化 tool-call/result 配对和 `git rev-parse` 结果校验；自由文本不会作为生产级绑定证明。尚未补齐的是逐事件 hash chain、terminal 配对和有界补抓；缺少原始事件或日志 hash 的旧兼容产物仍会在正式评分前显式转为 `not_evaluable`。
- 模型生成 Path 仍需与系统强制的 `load-skill` / 禁止发布安全步骤合并，不能允许模型输出弱化这些步骤的路径。
- Case/Suite Gate、Reference Run、统一评分锚点/置信区间、Judge 专家校准、跨 revision 失败 carry-forward 和完整 Holdout 资产管理仍需补齐。
- Trace overlay、Path Graph 导出和维度到事件的深链尚未完成；当前界面已经显化路径和证据，但仍以列表/卡片为主。

### 真实运行验收

- 真实 CATX Session 创建、两轮“首轮评测 → 人工批准 → 下一轮评测 → 晋升/拒绝”仍需在可用内网环境完成 E2E；本地 fake publisher/runner 测试不能替代它。
- 需要继续验证远端 403、分页不完整、重试失败、绑定漂移、恢复失败、候选污染和并发任务等异常路径。
- 当前可声明的是本地关键控制链和测试覆盖，不能声明真实远端生产闭环。

## 7. 最小后续路线

1. 给桌面 `tune` 接入显式 objective 与 paired objective gate；若暂不实现，持续使用“通用迭代”文案。
2. 实现原生 Path IR 与可执行 Path evaluator，并将覆盖缺口、首次偏离和 Trace overlay 接到 Case 页面。
3. 完成 EventJournal hash chain、terminal pairing、有界补抓和原始/规范化 Trace 分层。
4. 增加 Case/Suite preflight、Reference Run、Judge 校准和历史失败 carry-forward。
5. 在内网恢复后跑真实 CATX 两轮 E2E、视觉 E2E 与 20–50 Case 性能验证，并把结果写入不可变审计记录。
