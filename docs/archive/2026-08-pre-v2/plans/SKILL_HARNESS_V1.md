# Skill Harness 核方案 V1

## 1. 产品结论

ACEval 的核不是“生成 EvalPack 的 Skill”，而是一套面向 Skill 生命周期的 Harness：用户只描述目标、能力、Case 或预期结果，系统负责把这些输入转成可审阅的能力蓝图、冻结评测、远程证据、跨 Case 决策和受控多文件候选，直到收益收敛。

EvalPack 只是可复用、可替换的评测载体。它负责冻结 Case、Oracle 和来源，不参与修改决策，不随每轮候选重写，也不是优化链路的中心。

V1 覆盖五类用户意图：

| 意图 | 用户想做什么 | 是否已有 Skill | 主要证据 |
|---|---|---:|---|
| `repair` | 修复已知失败或回归 | 是 | 失败 Case、日志、路径证据 |
| `tune` | 提升质量、稳定性或成本 | 是 | 基线、重复运行、跨维度比较 |
| `extend` | 增加明确的新能力 | 是 | 能力 Case、边界 Case、负向触发、旧能力回归 |
| `discover` | 探索值得增加的能力 | 是 | 当前能力缺口、用户价值、风险和多方案选择 |
| `create` | 从目标生成新的 Skill | 否 | 能力蓝图、无 Skill 对照、候选复测 |

`auto` 是桌面端默认选择：没有 `SKILL.md` 且提供了目标、能力、Case 或自定义 EvalPack 时进入 `create`；显式提供 `capabilities` 时进入 `extend`；已有 Skill 且没有任何目标或 Case 时进入 `discover`；其他已有 Skill 先评测，再由失败归因决定 repair/tune 范围。空仓库且没有任何意图时会直接拒绝，避免凭空猜测一个 Skill。

## 2. 核心设计原则

1. Skill 是唯一优化对象。Case、Grader、执行环境和 Harness 自身不能被候选修改。
2. 先冻结“为什么改、改成什么算成功”，再生成候选，避免候选反向污染评测标准。
3. 能力设计和文件实现分离。Capability Architect 生成蓝图；Builder/Optimizer 只能实现已批准的文件计划。
4. 用户确认的是语义范围和最终候选，不需要理解 EvalPack 内部结构。
5. 先用确定性检查，再使用本地分析 Agent；完整日志落盘，模型只接收压缩证据。
6. `not_evaluable` 优先于猜测。环境、日志或 Oracle 不充分时不授权修改 Skill。
7. 新 Skill 必须证明相对通用 Agent 的增量价值，不能只证明“任务本身能完成”。
8. 多 Case 一次汇总决策，禁止逐 Case 打补丁造成能力冲突。

## 3. 分层架构

| 层 | 责任 | 核心契约/产物 |
|---|---|---|
| 配置层 | Skill/代码仓库、本地模型、远程 Agent、密钥引用、预算 | `KernelConfig` |
| 用户意图层 | 操作模式、目标、能力、非目标、Case、标准、自定义 EvalPack | `KernelInput` |
| 能力架构层 | 为 extend/discover/create 生成可审阅能力方案和文件计划 | `capability-blueprint/v1` |
| 评测编译层 | 复用或生成 EvalPack，冻结 Case、Oracle 和执行路径 | `evaluation-design/v1` |
| 执行层 | CATX 并行会话、仓库挂载、可恢复轮询 | `remote-batch/v1` |
| 证据层 | 完整消息/工具日志、输出、路径符合度、环境状态 | `runs/`、trace conformance |
| 决策层 | 确定性判分、跨 Case 根因、冲突、范围、收敛 | `analysis-decision/v1` |
| 变更层 | 受控多文件 create/replace、校验、不可变候选 | `candidate.manifest.json` |
| 发布层 | 父树哈希、原子 stage/commit/push、失败恢复 | Git commit |
| 展示层 | 任务、蓝图、轮次、批次、日志、候选和收敛路径 | `IterationKernel.snapshot()` |

所有写操作只能经过 Kernel 状态机。CLI、当前 localhost Console 和后续桌面壳只消费同一套应用服务与读模型，不重新实现业务判断。

## 4. 能力蓝图

`extend`、`discover` 和 `create` 在生成评测前先形成能力蓝图。每个 proposal 必须包含：

- 用户问题、用户价值、触发语句、输入、输出、工作流和依赖；
- 验收标准、方案依据和风险；
- 精确到路径的 `modify/create` 文件计划；
- 正向能力、边界、负向触发 Case；已有 Skill 还必须包含旧能力回归 Case。

`discover` 至少生成两个方案，且默认不选择任何方案。用户选择一个或多个 proposal 后才转成 `extend`。`extend/create` 可以采用推荐方案，但仍需用户确认蓝图。

Blueprint 中的 Case 会在构建前冻结。Greenfield Builder 能看到目标、工作流和验收标准，但看不到 Case prompt 与 expected output，避免直接把测试答案写进 Skill。

## 5. 三条状态路径

### 5.1 Repair/Tune

```text
created -> design_ready -> evaluation -> analysis
  -> verification（初次通过项复测）
  -> awaiting_confirmation（失败可归因于 Skill）
  -> ready_to_optimize -> candidate_ready -> publish
  -> 下一轮 design/evaluation
  -> converged | needs_evidence | blocked
```

### 5.2 Extend/Discover

```text
created -> blueprint_ready/discovery_ready
  -> 用户批准/选择能力
  -> 冻结能力、边界、负向触发和回归 Case
  -> 现有 Skill baseline evaluation
  -> 跨 Case 分析 -> 用户确认修改范围
  -> 允许修改现有文件并创建蓝图批准的新文件
  -> 发布、回归和收敛
```

### 5.3 Create

```text
created -> blueprint_ready -> 用户批准能力蓝图
  -> ready_to_build -> initial_candidate_ready
  -> 用户审阅首版文件树/差异
  -> publish initial candidate
  -> 冻结评测设计
  -> without-skill-baseline
  -> candidate evaluation + pass verification
  -> 证明增量价值后收敛；否则 needs_evidence/继续优化
```

蓝图批准只授权“生成候选”，不会直接授权发布。首版 Skill 必须经过 `initial_candidate_ready` 二次确认门。

## 6. 评测维度与判定顺序

每个 Case 至少从以下维度中选择适用项：

1. 结果正确性：用户精确预期优先确定性比较，其余按冻结标准语义判断。
2. 执行路径：required/recommended/alternative/forbidden 语义检查，不要求完全相同的工具序列。
3. 触发边界：`negative_trigger` 禁止无关请求读取或强行使用候选 Skill。
4. 回归保护：extend/discover 必须复测已有主能力。
5. 稳定性：首次通过 Case 至少复跑一次，失败视为 flake 并进入统一归因。
6. 增量价值：create 的同一套 Case 先运行 without-Skill baseline。只有候选通过且无 Skill 对照明确失败的 Case 才计入增量价值。
7. 运行成本：记录 Token、耗时、工具调用和环境证据，但不会为节省 Token 牺牲正确性门禁。

若 trace 不完整、远程会话失败、语义基线无法判定或模型证据不足，结果为 `not_evaluable`，不能据此修改 Skill。

## 7. EvalPack 的轻量边界

EvalPack 只承担四件事：

- 保存 Case/Oracle/fixture 引用和来源；
- 为相同 Skill 能力签名复用已有评测资产；
- 在用户自定义时作为稳定入口；
- 在优化期间冻结，防止测试漂移。

不能复用时，Evaluation Compiler 无感生成临时 Pack。用户 Case 始终作为高优先级种子保留，系统补充能力、边界、负向触发和回归覆盖。Harness 的优化决策直接消费冻结设计与运行证据，不依赖为每种 Skill 手写一个重型 EvalPack。

## 8. 多文件变更安全模型

默认可编辑文本范围为 `SKILL.md`、`scripts/`、`references/`、`workflow/`、`knowledge/`、`specs/`、`config/` 和 `assets/`。安全规则如下：

- repair/tune 只能修改扫描到的现有文件；
- extend 只能额外创建蓝图已批准的精确路径；
- create 必须创建 `SKILL.md` 或 `src/SKILL.md`，且只能创建蓝图批准的文件；
- 删除、重命名、链接、二进制、密钥、签名文件和 `.git/` 永远禁止；
- 现有内容使用唯一匹配的 `old_text -> new_text`；新文件使用 `create_file`；
- 候选保存父树哈希、变更路径、创建路径、diff、验证和模型 usage；
- 发布前再次比对整个可编辑树，stage 集合必须与 manifest 完全一致；失败恢复原始字节并删除本轮新文件。

## 9. Token 与并发预算

- Capability Architect 每个能力规划门只调用一次；Builder 每个首版候选一次；每轮 Optimizer 一次。
- Case/路径按批生成，不为每个 Case 单独规划。
- 远程提示不内嵌整份 Skill，只引用挂载点、分支、目标和标准。
- 所有完整日志落盘；分析只发送输出摘要、工具统计、关键证据和 artifact 引用。
- 精确 Oracle、路径和静态校验不调用模型。
- 一批 Case 最多一次跨 Case 分析调用；首次通过项只增加一次复测。
- `max_total_remote_sessions`、并发数、Prompt 大小、证据大小、最大轮数、最小收益和 patience 都是硬预算。

## 10. 执行环境与验证环境

远程 CATX 是候选 Skill 的真实执行环境；源码或 Fixture Lab 作为独立 repository 挂载。本地 Harness 是分析、浏览器验证、候选生成和 Git 发布环境。

两类环境允许隔离：远程执行环境提交或产生结果，本地/D2C 验证环境按 commit/branch 拉取同一快照再验证。所有证据必须记录环境指纹、仓库 ref 和 session/receipt，不能用“本地看起来正常”替代远程运行证据。

## 11. 持久化与桌面读模型

```text
.aceval/tasks/<task-id>/
├── task.json
├── events.jsonl
├── kernel/
│   ├── config.json
│   ├── input.json
│   ├── state.json
│   ├── blueprints/
│   ├── active-design.json
│   └── designs/revision-*/
└── iterations/iteration-*/
    ├── batches/
    ├── runs/
    ├── analysis-decision.json
    └── candidates/
```

`snapshot()` 输出用户可理解的完整读模型：操作模式、当前 phase、蓝图、冻结设计、所有轮次/批次、Case 日志索引、分析决策和候选文件树。Console 的日志接口只接受经过校验的 Task/Iteration/Purpose/Case，不允许任意文件读取。

当前 Console 是 loopback-only 的本地 Web UI，可作为桌面应用的渲染层原型；正式桌面端仍应增加系统 Keychain、配置向导和调用 Kernel 写接口的本地受信任主进程。

## 12. V1 已完成与明确边界

已完成：

- 五种 Harness 意图与 `auto` 路由；
- 能力蓝图、探索方案选择、Case/文件计划冻结；
- greenfield 多文件 Builder 与发布前候选确认；
- extend 创建 references/scripts 等批准资源；
- 多路 CATX 执行、可恢复轮询和完整日志；
- 路径、负向触发、回归、稳定性和无 Skill 对照；
- 低 Token 跨 Case 分析、用户范围确认、多文件校验发布和收敛；
- CLI 与统一任务中心读模型。

仍属于后续产品化范围：

- 原生桌面壳、Keychain 和可编辑配置/Case 表单；
- Console 中直接批准蓝图/候选/范围的受信任写通道；
- D2C 本地浏览器 provider 与 Kernel Case provider 的统一调度；
- sealed holdout、人工标注集和跨模型独立 Judge；
- 云端验证环境、队列、多租户权限和团队 EvalPack Registry；
- 新仓库自动创建。V1 的 create 要求用户先提供一个已初始化、存在目标分支且不含 `SKILL.md` 的干净 Git 仓库。

## 13. 验收标准

1. 用户只给目标即可在空 Skill 分支生成首版候选，且发布前有精确文件预览门。
2. 用户给能力和自定义 Case 时，Case 被保留并与自动边界/回归 Case 一起冻结。
3. discover 不会静默实现推荐方案，必须由用户选择 proposal。
4. extend 可以同时修改 `SKILL.md` 并创建批准的 `references/` 或 `scripts/` 文件。
5. 新 Skill 在没有相对 without-Skill baseline 的可测增益时不能标记收敛成功。
6. 任何不完整日志、未授权路径、测试泄漏、父树漂移或仓库测试失败都阻断发布。
7. 页面能明确显示任务一共运行多少轮、每轮批次/日志、为什么修改、改哪些文件和后续决策。
