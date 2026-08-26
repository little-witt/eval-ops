# FORGE Desktop V1 产品与交互设计

> 设计稿阶段名称为 FORGE，最终产品命名可调整。设计稿不替换现有 ACEval Console，也不代表已开始桌面端生产实现。

## 1. 产品目标

让一名普通研发用户下载应用后，不阅读 EvalPack、Kernel phase 或 CLI 文档，也能完成：

1. 连接本地模型、CATX 和 Git 仓库；
2. 用目标、自然语言预期和可选 Case 创建任务；
3. 看懂系统生成了什么 Case、为什么这样评测；
4. 实时了解当前运行到哪里、哪一维失败、使用了什么证据；
5. 审阅每轮优化假设、具体文件 diff、回归风险和验证结果；
6. 明确批准、拒绝、修改范围或补充诉求；
7. 得到可复现的最终版本、收益结论和后续建议。

## 2. 设计原则

- **任务旅程优先**：首页不展示抽象平台指标，先回答“我的任务在哪里、接下来要做什么”。
- **只暴露必要复杂度**：默认使用自然语言和状态标签；EvalPack、路径规范和批次 ID 放在“高级信息”。
- **原因与结果同屏**：每轮都同时显示失败维度、修改假设、涉及文件、结果变化和下一步。
- **所有写操作可预览**：能力蓝图、优化范围和候选 diff 分别确认；任何 Git 发布前都要审阅具体 diff。
- **异常是正常路径**：失败必须区分环境、远程平台、评测证据、Skill 缺陷和偶现，不用红色大面积制造恐慌。
- **成本可预期**：创建前显示预计 Case、远程会话和 Token；运行中显示已用预算和停止条件。
- **桌面原生感**：固定应用骨架、键盘快捷键、可调整工作区、抽屉式日志，不做网页营销页或大屏看板。

## 3. 视觉方向

主题为 **Quiet Editorial Instrument / 安静的编辑型仪器**。

- 暖灰纸张背景承载长时间阅读；深墨色侧栏形成稳定应用骨架。
- 钴蓝只用于主要动作与选中路径；橙色表示需要判断；绿色只表示已有可信证据的通过。
- 标题使用有编辑感的衬线体，数据和路径使用等宽体，正文使用紧凑人文无衬线体。
- 避免当前版本的全黑工业面板、荧光色泛滥、过密卡片和全部大写英文标签。
- 标志性组件是“Iteration Spine”：一条从输入、评测、基线、每轮假设到收敛的可点击证据脊柱。

## 4. 信息架构

```text
FORGE
├── 任务
│   ├── 任务中心
│   ├── 新建任务
│   └── 任务工作台
│       ├── 旅程总览
│       ├── Case 与标准
│       ├── 实时会话
│       ├── 分析决策
│       └── 候选审批
├── 资源
│   ├── Skill 仓库
│   ├── Fixture / Code 仓库
│   └── EvalPack（高级）
├── 环境
│   ├── 本地模型
│   ├── CATX Agent
│   ├── Git / SSH
│   └── 浏览器验证
└── 设置
    ├── 凭据与 Keychain
    ├── 预算与并发
    └── 诊断与日志导出
```

## 5. 核心页面

### A. 任务中心

用户首先看到三件事：环境是否 Ready、需要自己处理的任务、最近任务的明确下一步。

- 主按钮只有“新建升级任务”。
- 任务以表格呈现，列为 Skill、意图、当前阶段、当前结果、下一步和更新时间。
- “需要你确认”固定置顶，不与运行中或已完成任务混在一起。
- 首页不展示下载量、全局 Token 曲线等与当前动作无关的信息。

### B. 新建任务向导

四步完成：

1. 选择 Skill 与任务意图；
2. 输入目标、标准、Case 和预期；
3. 检查执行/验证环境；
4. 查看预计 Case、会话、Token 和 Git 行为后启动。

默认选择“自动判断”。用户不需要先决定 repair/tune/extend/create。高级用户可以显式覆盖。

### C. 实时迭代工作台

工作台围绕 Iteration Spine 展开：

- 顶部显示任务状态、当前轮次、预算和暂停/停止。
- 中部脊柱展示输入冻结、Case 生成、baseline、每一轮和最终结论。
- 选中某轮后，左侧显示“为什么开始这一轮”，中部显示维度/Case，右侧显示会话与工具证据。
- 每个失败必须有责任类型；环境问题不会进入 Skill 优化建议。
- 用户可以切换总览、Case、会话和文件，但不离开同一任务上下文。

### D. 候选审批

这是产品最重要的安全页面：

- 顶部摘要：解决哪些失败、预计改善哪些 Case、是否影响已通过能力。
- 左侧按根因簇显示“为什么改”。
- 中间是逐文件 diff，支持文件树和行级展开。
- 右侧是发布清单：仓库测试、回归、泄漏、父树、预算和目标分支。
- 默认按钮为“批准并创建任务分支”，不是直接推送主分支。
- 可选择“要求调整”，直接补充自然语言诉求并重新生成候选，不需要重建任务。

### E. 设置与诊断

- 凭据只显示 Keychain 状态和最后验证时间。
- 每个连接都有“测试连接”和明确错误修复建议。
- 环境检查结果可导出为脱敏诊断包。
- 运行依赖由应用管理，用户不需要配置 `PYTHONPATH` 或启动 localhost 服务。

## 6. 核与桌面的边界

桌面端不得自行推断状态或修改任务文件，只调用应用服务：

- `createTask / updateIntent / preflight`
- `run / pause / cancel / retry`
- `approveBlueprint / selectCapabilities`
- `approveScope / requestScopeChange`
- `approveCandidate / rejectCandidate`
- `getSnapshot / subscribeEvents / getSessionLog`

Kernel 需要补充的生产状态：

- `preflight_required / preflight_failed / ready_to_start`
- `scope_review_ready`
- `candidate_review_ready`（适用于所有模式，而非只有 create）
- `paused / cancelling / cancelled / retry_ready`
- `publishing / publish_failed / completed`

## 7. 单机生产架构

```text
Desktop Renderer
      │ typed IPC
Trusted Desktop Main Process
      ├── Keychain
      ├── SQLite WAL + event journal
      ├── Background task worker + lease
      ├── Repository worktree manager
      └── Kernel application service
              ├── Local model provider
              ├── CATX gateway
              ├── Browser validator
              └── Git publisher / PR adapter
```

不在 Renderer 中保存 Token，不让页面直接访问文件系统、Git 或 CATX。任务状态由 SQLite 事务驱动，完整会话日志和截图保存在应用数据目录，数据库只保存索引与摘要。

## 8. 实施阶段

### P0-A：桌面可操作闭环

- 桌面壳、统一组件与四个核心页面；
- 本地 SQLite 任务/事件/凭据引用；
- 配置向导与 Preflight；
- 创建、运行、暂停、取消、重试；
- 蓝图、范围、所有候选 diff 三类确认门；
- 完整日志、Case 矩阵、动态迭代脊柱。

### P0-B：真实线上闭环

- 内置本地模型 Provider；
- CATX 超时、重试、断线续取和会话去重；
- 每任务独立 worktree/分支；
- 代码评审真实 Skill 全链路；
- 失败归因、稳定复测和发布回滚。

### P0-C：好用性验收

- 新用户无需 CLI 完成首次任务；
- 从安装到启动首次评测不超过十个可理解动作；
- 所有失败都有责任分类和下一步；
- 每次发布前都能看到具体 diff 与验证；
- 应用重启后任务可继续且不会重复创建会话；
- 用真实前端/Java 代码评审任务验证稳定正收益。

### P1

- D2C 多 viewport 与视觉差异审批；
- 文档编辑/数据查询等远端状态型 Provider；
- EvalPack Registry、模板和团队导入导出；
- 云端验证环境适配。

## 9. 本次设计确认项

在进入生产开发前，需要确认：

1. 整体视觉方向是否接受；
2. 左侧导航 + 单任务工作台结构是否直观；
3. Iteration Spine 是否能清楚表达轮次和原因；
4. 新建任务四步向导是否足够简单；
5. 候选审批页面的信息密度和确认方式是否合适；
6. 产品暂用名 FORGE 是否保留。

