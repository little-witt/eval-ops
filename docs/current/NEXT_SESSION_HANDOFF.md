# FORGE / ACEval 当前基线与下一会话交接

> 更新日期：2026-08-27
> 状态：当前唯一有效的执行交接；工程 P0 已完成，真实业务验收与 P1 继续推进
> 产品架构：[`FINAL_SYSTEM_ARCHITECTURE.md`](./FINAL_SYSTEM_ARCHITECTURE.md)
> Kernel 规则：[`EVALUATION_SELF_ITERATION_KERNEL_V2.md`](./EVALUATION_SELF_ITERATION_KERNEL_V2.md)
> 发布验证：[`PRODUCT_RELEASE_READINESS.md`](./PRODUCT_RELEASE_READINESS.md)

## 1. 当前结论

FORGE 当前不是静态报告 Demo：桌面端、持久化 Kernel、CATX Trial、完整日志、分阶段本地分析、用户审批、多文件候选、Champion/Challenger 和 D2C 浏览器底座均有真实实现及回归测试。

本轮完成的是**工程 P0**。它表示产品闭环、恢复、安全门和最终 arm64 应用已经可运行；不表示所有业务 Skill 已经得到领域校准。最新 P0 尚未用真实 CATX 对目标 Skill 完成一次包含“首轮评测 → 优化批准 → 第二轮评测 → 晋升/拒绝”的业务验收，这应是下一会话第一件事。

## 2. 当前唯一架构

```text
FORGE Desktop
├─ Task Center / Task Detail / Evidence / D2C / Settings
└─ Desktop Service（stdio JSON-RPC，不开放本地端口）
   └─ IterationKernel（最终确定性裁决者）
      ├─ Eval Design：目标义务图 → EvalPack → Case → 语义路径
      ├─ Trial Executor：CATX，多 Case Session、双仓库、完整日志
      ├─ Frozen Trial Environment：每轮冻结，初测/基线/复验共用 hash
      ├─ IterationBrain：证据冻结 → 语义评分 → 跨 Case 归因 → Proposal
      ├─ Approval + Candidate Lab：追加式审批、多文件候选、Git 发布/恢复
      ├─ Candidate Comparison：稳定通过保护、最小收益、晋升/拒绝
      └─ Convergence：预算、最大轮次、耐心值、循环和安全停止
```

职责不可混淆：

- CATX 是 Trial Executor，不是本地分析大脑。
- 本地 IterationBrain 生成有 schema 的语义意见，不拥有最终修改、晋升或停止权。
- Kernel 负责证据资格、硬事实、环境一致性、回归、审批、版本和收敛。
- 初测与通过 Case 复验可以使用不同的新 Session，但必须继承同一份冻结环境契约；不再让用户分别选择一套验证环境。
- EvalPack 是无感生成/精确复用/自定义的内部资产，不主导 Skill 优化逻辑。

## 3. 本轮已完成

### P0 核心

- `IterationBrain` 已从一次大模型调用改为四个真实、可恢复阶段：冻结证据、语义评分、跨 Case 归因、修改提案。
- 每个模型阶段均有严格 JSON 校验、输入/输出 hash、validated/failed manifest、模型/Profile/usage/耗时收据和追加式重试链。
- 有界只读 Evidence Query Port 已进入语义评分上下文；不向模型发送整份日志。
- 模型失败后可以从 `evidence_ready`、`semantic_grading`、`attribution`、`proposal` 恢复，不再卡死或覆盖收据。
- 确定性预期、证据有效性和硬路径事实会在模型结果之后重新覆盖，模型不能反转硬事实。
- `trial_executor` 与 `analysis_executor` 角色已分离；旧 `remote_agent` 仅作为兼容输入，双重配置 fail closed。
- 每轮冻结 Trial Environment Contract：CATX Profile 内容 hash、Skill/代码仓库 revision 与 tree、Fixture、Case revision 集、设计和运行参数。
- 初测、无 Skill 基线、通过 Case 复验必须携带相同环境 hash；文件或 Profile 漂移时停止比较。
- 新迭代会清空上一轮环境 hash 并重新冻结；用户补充诉求后进入干净的新迭代，不复用旧 batch。
- 原始分析 Decision 不可变；用户选择与补充意见记录为追加式 Approval Record 和 approved overlay。
- 所有 Case 来源统一写入 `case_revision`、`content_hash`、`provenance`、`environment_applicability`、`reuse_key`，为 P1 跨任务沉淀提供契约基础。
- 多文件优化继续支持 `SKILL.md`、`scripts/**`、`references/**`、`workflow/**`、`knowledge/**`、`specs/**`、`config/**`、`assets/**`。

### 桌面端与本地模型

- 创建任务只提交 `trial_executor=catx` 与 `analysis_executor=local-forge`，已消除新旧执行器双发导致的真实创建失败。
- 创建页明确显示“本地分析大脑与评测运行环境”；CATX 环境由安全存储字段与 Vault ID 自动生成冻结 Profile，用于初测、基线和复验，不再接受另一份任务 Profile JSON。
- 任务详情显示四阶段分析、调用收据数量、Kernel 最终裁决和冻结环境 hash。
- CC Switch 可自动识别当前 Codex 或 Claude Code 配置；Codex `config.toml` / `auth.json` 分离导入仍保留。两类配置均复制到隔离 Profile，不修改原 Codex/Claude 配置。
- Electron 退出 `Object has been destroyed` 竞态已修复并保留回归。
- D2C 已具备本地 Chrome Worker、预览窗口、受控插件、设计稿/reference/actual/diff、DOM/console/network/screenshot 证据和客户端页签。

### 最终产物与验证

- 最终应用：`desktop/dist/mac-arm64/FORGE Skill Evolution Studio.app`
- Electron 主程序：arm64。
- 内置 PyInstaller Kernel：arm64，且与最新 `desktop/build/forge-kernel/forge-kernel` SHA-256 一致。
- 最终 `.app` 在 `ACEVAL_PYTHON` 指向不存在路径时仍通过隐藏启动/退出烟测：`renderer=loaded`。
- Python：`381 passed`，`127 subtests passed`；7 条既有 collection warning，无失败。
- Renderer 安全/产品契约、JS 语法、Python compileall、`git diff --check` 通过。
- 真实 Chrome UI E2E 通过，3 张发布截图已更新。
- 仓库外未发现本轮用户提供的 CATX/PAT 明文进入源码或文档。

## 4. 剩余待办与优先级

### P0-Acceptance：真实业务验收（下一会话立即执行）

1. 用最新版客户端对 `frontend-code-reviewer` 发起真实 CATX 任务。
2. 至少覆盖一个稳定通过 Case、一个失败 Case、一个需要语义路径判断的 Case。
3. 完成用户审批、多文件候选、发布、第二轮真实评测与 Champion 晋升/拒绝。
4. 在分析阶段和远程轮询阶段各做一次应用重启，验证真实任务恢复及收据不覆盖。
5. 记录 bad case、误归因、Token、Session 数、耗时和用户操作数，作为 P1 排序依据。

完成标准：不是“接口返回成功”，而是一个真实 Skill 的完整两轮证据链可在客户端逐项查看，失败能解释且不会错误修改 Skill。

### P1.1：D2C 自动编排生产化

- 当前浏览器/视觉底座已完成；尚需把 D2C Dimension Profile 与浏览器 Grader 自动接入 Case 分类、Attempt Verdict、Candidate Comparison 和收敛主循环。
- 冻结浏览器版本、viewport、字体、Fixture、阈值与代码 revision；跨进程运行可以隔离，但比较变量必须一致。
- 用一个真实 D2C Skill 完成设计稿 → 修改代码 → 浏览器验证 → 视觉/交互回归 → 晋升的两轮验收。

### P1.2：跨任务 Case 资产库与回归晋升

- P0 已有身份与适用性字段，并已有 EvalPack 精确复用；尚未实现从多任务失败日志自动挖掘、脱敏、去重、校准和晋升 Regression Case。
- 建议在完成 2–3 个真实 Skill 任务后开始，避免在没有真实数据时过早设计空泛的“Case 平台”。
- 必须按目标、Skill/能力 revision、环境适用性和 Oracle 可信度过滤，不能因为“跑过”就永久复用。

### P1.3：Judge 校准与统计可靠性

- 建立代码评审/D2C 的专家标注校准集，测量误报、漏报和语义 Judge 一致性。
- 增加按风险自适应重复、置信区间、flaky 分类和必要的 sealed Holdout。
- 根据真实 Token/收益数据决定是否为四个分析阶段配置不同模型，而不是先增加模型编排复杂度。

### P1.4：客户端证据深钻与使用体验

- 将 semantic verdict、attribution、proposal、environment contract 和每次 agent-call receipt 做成可点击证据检查器，而不只显示数量/状态。
- 增加错误恢复建议、环境漂移 diff、任务预算趋势和 bad-case 一键反馈入口。
- 继续基于真实使用优化视觉细节；当前 UI 结构与第二版风格保留为基线。

### P2：Agent 评测与自迭代

- `EvaluationSubject`/`AgentSubject` 契约已有，但目前只证明控制面可复用，尚未完成通用 Agent 的可编辑面、工具/策略 Diff、发布 Adapter 与真实晋升闭环。
- 先复用 Case、Attempt、Evidence、Diagnosis、Candidate Comparison、Convergence；不要直接复用 Skill 文件补丁器。

### 非当前产品核优先级

- Apple Developer ID 签名、公证、DMG、自动更新。
- Windows/Linux 打包。
- 多租户、中心部署、运维监控。

上述不属于当前“每人下载、独立使用”的核验收范围。

## 5. 下一会话建议起点

新会话先读取：

1. 本文件；
2. `docs/current/FINAL_SYSTEM_ARCHITECTURE.md`；
3. `docs/current/EVALUATION_SELF_ITERATION_KERNEL_V2.md`；
4. `docs/current/PRODUCT_RELEASE_READINESS.md`。

建议首条指令：

> 基于 `docs/current/NEXT_SESSION_HANDOFF.md` 继续，不重新设计 P0。先启动最终 arm64 客户端，使用现有本地配置对 frontend-code-reviewer 完成 P0-Acceptance 真实两轮任务，记录全部 bad case、Token、Session、耗时和恢复结果；发现阻断先修复并补回归。

注意：配置文件与系统安全存储中已有的密钥可以引用，但任何日志、文档、提交和交接消息都不得打印实际值。
