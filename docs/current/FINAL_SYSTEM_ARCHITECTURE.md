# ACEval / Skill Doctor 最终产品与系统架构

> 状态：唯一有效的产品与架构基线  
> 日期：2026-08-26  
> 适用形态：单用户、本地优先的桌面工具；远程 Agent 作为真实执行环境，本地或独立 Worker 作为验证环境  
> 详细内核：[`EVALUATION_SELF_ITERATION_KERNEL_V2.md`](./EVALUATION_SELF_ITERATION_KERNEL_V2.md)  
> 客户端交互基线：[`PRODUCT_DESIGN_V2.md`](../../design/desktop-v2/PRODUCT_DESIGN_V2.md)

## 1. 最终产品定义

ACEval 不是 EvalPack 生成器，也不是把几个 Agent Prompt 串起来的自动改写脚本。它是一套面向 Skill 生命周期的本地 Eval & Evolution Harness：

> 用户提供 Skill、目标/标准、可选 Case 和必要环境；系统自动设计可信评测、在真实 Agent 上并行执行、回收完整证据、区分 Skill 与环境问题、提出受控多文件候选，并在不破坏已有能力的前提下迭代到明确的安全停止点。

用户最终获得的不是一个评分页面，而是：

- 可复现的当前能力基线；
- 一组来源、Oracle 和执行路径清晰的评测资产；
- 每轮完整 Session、Trace、产物、评分与失败归因；
- 可审阅、可回滚、经过回归门的 Skill commit；
- 为什么继续、晋升、拒绝或停止的完整证据链。

EvalPack 是系统内部冻结 Case、Oracle、Fixture 和 Grader 的可复用载体。它服务于 Skill 优化，但不支配优化逻辑，不要求普通用户理解，也不在每轮重建。

## 2. 系统核心价值

### 2.1 对普通 Skill 用户

- 用目标、标准和少量 Case 开始，不需要先学 EvalPack、Grader 或 Agent 平台协议。
- 自动获得补充 Case、负向边界、稳定性与回归保护。
- 失败时能知道是 Skill、模型波动、工具、权限、环境、Fixture、Oracle 还是日志问题。
- 只在能力范围、优化策略、敏感改动和发布等高价值节点确认。

### 2.2 对 Skill 作者

- 把一次性人工试跑转成可重复的产品工程流程。
- 用真实远程 Agent 行为而非本地假运行驱动优化。
- 同时优化 `SKILL.md`、`scripts/`、`references/`、模板和配置等完整资源树。
- 保留 Champion，不让单 Case 或单轮高分覆盖已知稳定版本。

### 2.3 对团队和平台

- 形成跨 Skill 可复用的 Case 模板、Dimension Profile、环境 Adapter、失败签名和回归资产。
- 将“模型感觉变好了”升级为有评测契约、运行上下文、证据和回滚点的工程决策。
- 以 Skill 为切入口建立可扩展到 Agent 的评测与自迭代控制面。

## 3. 核心壁垒

真正可积累的壁垒不在某一个模型 Prompt，而在以下组合资产：

1. **可信控制面**：Case/Suite 质量门、证据有效性、分层 Verdict、失败归因、硬回归、Champion/Challenger 和受控停止都由确定性系统执行。
2. **证据图谱**：目标 → 测试义务 → Case → 路径 → Session/Attempt → 日志证据 → Verdict → 问题簇 → Proposal → Candidate → 晋升决策全链可追溯。
3. **环境与验证 Adapter**：远程 CATX、代码仓库 Fixture Lab、本地 Chrome、D2C 视觉比较以及后续文档/数据/运维验证环境共享同一协议。
4. **跨 Case 诊断知识**：失败签名、Skill 来源图、问题聚类、冲突矩阵和稳定通过保护集会随真实任务积累，而不是每次从零请模型总结。
5. **低 Token 实验编排**：冻结资产复用、确定性 Grader 优先、受影响 Case 先跑、日志窗口裁剪、问题簇级模型调用和分阶段淘汰候选。
6. **本地优先与安全边界**：代码、日志、凭据和 Holdout 默认留在用户机器；密钥、浏览器扩展、Git 写入和远程会话有独立授权与审计。

模型可以替换，Agent 平台可以替换，验证 Worker 也可以替换；上述契约、证据和累计资产仍然成立。这是系统相对“优化 Skill 的 Skill”或一次性脚本的主要差异。

## 4. 业界对标与采用结论

| 业界能力 | 已验证方法 | ACEval 采用 | ACEval 的补充 |
|---|---|---|---|
| Anthropic Agent Evals | task/trial/grader/transcript/outcome 分离；确定性、模型与人工 Grader 组合；正负平衡；reference solution；pass@k/pass^k | Case、Attempt、Outcome、Log、Grader 独立；关键能力用 pass^k | 把这些规则接到 Skill 多文件候选和 Git 晋升 |
| OpenAI Evals / Trace Grading | held-out、pairwise、按明确维度评分、端到端 Trace 标签、持续评测 | Validation/Holdout、盲化成对语义 Judge、语义路径 Verdict | 日志证据与 Skill source refs、问题簇、候选 Diff 直接关联 |
| LangSmith | Final response、single step、trajectory；strict/unordered/subset/superset 与 LLM judge | required/alternative/forbidden/recommended 的部分有序语义路径 | 只对安全/权限/业务强制步骤使用硬路径门，避免工具序列过拟合 |
| SWE-bench Verified | well-specified task、稳定环境、FAIL_TO_PASS + PASS_TO_PASS、人工筛除不公平 Case | 能力改善 + 稳定通过回归保护，Case 先通过公平性与可解性门 | 扩展到非代码 Skill 与真实远程 Agent Session |
| τ-bench | 以最终状态判定工具 Agent，pass^k 衡量一致性 | Outcome 优先、关键 Case 稳定性门 | 同时评估 Skill 的明确步骤遵循和副作用 |
| GEPA | 基于轨迹反思生成候选，测试更新，保留 Pareto frontier | 问题簇级反思、少量非支配候选、实验化 Proposal | 模型不能自行归因、修改或晋升，控制面负责安全停止 |

采用边界：业界不存在一个可直接照搬的“Skill 自迭代标准”。ACEval 组合 Agent Eval、软件测试、实验比较与受控 Prompt/资源优化，但不会把公开 Benchmark 排名当成单个业务 Skill 的成功标准。

参考文档：

- [Anthropic — Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [OpenAI — Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
- [OpenAI — Trace grading](https://developers.openai.com/api/docs/guides/trace-grading)
- [OpenAI — Graders](https://developers.openai.com/api/docs/guides/graders)
- [OpenAI — Introducing SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/)
- [LangSmith — Trajectory evaluations](https://docs.langchain.com/langsmith/trajectory-evals)
- [τ-bench](https://arxiv.org/abs/2406.12045)
- [GEPA](https://arxiv.org/abs/2507.19457)

## 5. 最终分层架构

```text
ACEval Desktop
├─ Task Center / Task Detail / Evidence Inspector / Settings
├─ Secure IPC / OS Secret Storage / Desktop Browser Windows
└─ Local Application Service
   ├─ Configuration & Credential References
   ├─ Task State Machine + Append-only Events + Read Model
   ├─ Eval Design Compiler
   │  ├─ Capability & Procedure Graph
   │  ├─ Test Obligation Graph
   │  ├─ Case/Path Generation & Reuse
   │  └─ Case/Suite Quality Gates
   ├─ Remote Trial Coordinator
   │  ├─ CATX Sessions / Repository Mounts
   │  ├─ Poll + SSE + Full Log Recovery
   │  └─ Attempt Evidence Validation
   ├─ Grading & Diagnosis
   │  ├─ Deterministic Graders
   │  ├─ Bounded Semantic Judges
   │  ├─ Case Aggregate / Candidate Comparison
   │  └─ Failure Signature / Attribution / Conflict Graph
   ├─ Candidate Lab
   │  ├─ Skill Source Map / Multi-file Patch
   │  ├─ Static & Repository Validation
   │  └─ Git Worktree / Commit / Rollback
   ├─ Convergence Controller
   │  ├─ Champion / Challenger / Pareto Set
   │  ├─ Regression / Validation / Holdout
   │  └─ Promotion / Rejection / Safe Stop
   └─ Validation Worker Interface
      ├─ Code Review Fixture Lab
      ├─ D2C Local Browser Worker
      └─ Future Document / Data / Ops Adapters
```

关键边界：Renderer 不直接读取密钥、不执行 Shell、不操作 Git、不创建远程 Session；所有副作用经桌面主进程和 Python 服务的版本化命令完成。页面只消费 Read Model 和事件，不重算评测结论。

## 6. 自迭代 Kernel 最终规则

详细算法与契约见内核 V2 文档，最终不可妥协的规则如下：

1. 先生成 Test Obligation Graph，再选择风险/成本最优的 Case，不直接让模型自由列清单。
2. Case 按 `Draft → Executable → Calibrated → Frozen → Regression` 流转；只有 Frozen Case 或任务内带用户明确预期、Oracle-ready 的冻结输入可以授权修改，自动草稿只用于探索。
3. 路径是语义部分有序图，不默认等于参考工具序列；安全、权限、业务强制步骤仍是硬门。
4. 证据无效先返回 `not_evaluable`，不能给 Skill 记失败，也不能授权修改。
5. Verdict 分为 Attempt、Case Aggregate 和 Candidate Comparison 三层。
6. 硬状态/产物/Trace 由确定性 Grader 判定；模型按单一语义维度隔离评分，不能推翻硬事实。
7. 系统先做失败签名、归因、聚类、影响面和冲突分析；模型只生成根因假设和候选。
8. Champion 不可被未验证 Challenger 覆盖；关键回归、稳定性、最小实际收益、预算和 Holdout 共同决定晋升。
9. 目标、Eval、环境、模型、工具、Fixture 或 Grader 实质变化会使旧比较失去直接可比资格。
10. 收敛表示在硬上限下受控停止和已知能力保护，不宣称全局最优。

## 7. 桌面客户端最终方案

### 7.1 技术选型

- **桌面壳**：Electron，macOS 首先可用，保留 Windows 适配。
- **Renderer**：原生语义 HTML/CSS/JavaScript；不引入重型 UI 框架，减少包体、供应链和运行成本，并保留后续 TypeScript 迁移边界。
- **主进程**：严格 IPC allowlist、`contextIsolation=true`、`nodeIntegration=false`、Renderer sandbox。
- **Kernel 服务**：Python `aceval` 以 PyInstaller arm64 onedir 内置到应用，由桌面主进程通过继承的 stdin/stdout JSON-RPC 启动/关闭，不开放 HTTP 端口；开发模式仍可从源码启动。
- **存储**：任务与证据沿用内容寻址目录和 append-only events；Renderer 不直接拼文件路径。
- **密钥**：使用 Electron `safeStorage`/系统凭据能力加密，仅在发起命令时注入子进程；任何 Read Model、日志和异常都必须脱敏。

### 7.2 核心页面

1. **首次运行与环境体检**：基础体检显示内置 Kernel、Git、内置 Node 和 Chrome；CATX Profile、模型桥和仓库权限在任务创建/首次使用前按配置校验，浏览器插件使用受控安装入口。
2. **任务中心**：真实任务、状态、需要操作、最新收益、预算和错误；不注入演示任务。
3. **创建任务**：Skill/代码仓库、操作模式、目标、Case、标准、远程 Agent 与验证环境；创建后立即进入详情。
4. **任务详情**：实时路径大盘、进化轨迹、Case/路径、Session/日志、Trial Verdict、诊断、Proposal、Diff 和 Champion/Challenger。
5. **证据检查器**：任意结论跳转到原始事件、日志区间、产物、截图、Diff 和 Grader。
6. **资源与设置**：仓库、环境 Profile、Eval 资产、浏览器插件、模型和密钥引用。

### 7.3 D2C 页面运行与设计稿对比

D2C 由两个不同视图组成：

- **交互预览窗口**：桌面端打开隔离的浏览器窗口运行候选页面，用户可以人工探索；它不作为自动评分证据。
- **验证 Worker**：使用干净临时 Chrome Profile、冻结 viewport/locale/timezone/color scheme，自动执行动作并生成可复现证据。

验证结果在客户端以三栏呈现：

```text
设计稿 / Reference | 候选实际截图 | 差异图 / 透明叠加
```

必须支持：

- 导入 PNG 设计稿并记录 hash、尺寸、viewport 和 scale；
- actual/reference/diff 三栏视图；overlay/slider 属于后续视觉分析增强；
- 像素差异比例、尺寸不一致、最大通道差与阈值；
- DOM、console、network、title、URL 和运行性能证据；
- 当前支持冻结 viewport 和交互动作；多 viewport 矩阵由同一 Worker 契约扩展；
- 后续可配置动态区域 mask、字体就绪和布局语义 Grader；
- 设计稿、实际图和差异图与 Case、Attempt、candidate commit 一一关联。

### 7.4 浏览器插件与依赖管理

“安装插件”必须是受控能力，不允许 Renderer 任意执行安装命令：

- 插件使用版本化 manifest，声明 id、版本、来源、SHA-256、所需权限、浏览器参数和验证能力。
- 内置 D2C Driver 无第三方 npm 依赖，打包版复用 Electron Node 22，只外部需要 Chrome；环境体检优先发现系统 Chrome。
- 浏览器扩展从用户选择的本地目录或已校验安装包导入，复制到 ACEval 管理目录；启动专用 Chrome 时用 allowlist 加载，不污染用户日常浏览器 Profile。
- 安装、升级、权限变化和删除均需用户确认并写审计事件；校验失败或权限超出 manifest 时 fail closed。
- 自动下载能力必须显示来源、版本、体积、hash 和命令，下载/安装失败不影响非 D2C 任务。

## 8. 为什么它不是 Demo 或玩具

以下是发布硬门，不满足时不能标记为“可用版本”：

### 8.1 数据与恢复

- 默认 UI 不依赖 FakeRuntime 或硬编码任务；演示数据必须有显式 `SIMULATED` 标记和独立入口。
- 任务、事件、Session、日志、截图、Diff、Candidate 和 Decision 可在崩溃后恢复。
- 所有远程建会话、轮询、日志抓取、候选提交和发布命令幂等或可安全重试。
- 资产有 Schema 版本、内容 hash、迁移策略和损坏检测。

### 8.2 安全

- 密钥不写入任务 JSON、事件、日志、Renderer 状态或报错。
- Renderer 无 Node/Shell/Git 权限；外部 URL、文件路径、IPC 和浏览器扩展严格校验。
- Git 修改使用隔离 worktree、精确路径授权、静态检查、原子 commit 和失败回滚。
- Holdout、生产日志和设计稿有可见性、解封、脱敏和保留策略。

### 8.3 可信评测

- 任何分数都能回到 Grader、证据和运行上下文。
- 环境、Fixture、日志或 Grader 故障不能被计为 Skill 失败。
- 候选不能用总分覆盖 critical regression。
- 模型生成的 Case、Oracle、归因或候选都不能自行成为硬事实。

### 8.4 可用性与性能

- 创建任务后立即进入详情；长操作有实时事件、取消、恢复和明确下一步。
- 50 Case 任务的 Read Model 首屏目标低于 2 秒；大日志使用分页/虚拟列表，不一次渲染完整文件。
- 键盘可操作、焦点可见、对比度达标、错误信息包含修复动作。
- 环境缺失时允许使用其他 Skill 功能，不因某个可选插件阻塞整个应用。

### 8.5 工程验收

- Python 单元/集成测试全绿；新增 V2 契约、状态机、归因和收敛有故障/篡改测试。
- Electron 主进程、preload 和 Renderer 有单元测试；关键用户路径有 Playwright E2E。
- D2C 使用真实 Chrome 完成页面加载、交互、截图和设计稿差异 smoke test。
- macOS 可生成自包含 `.app`；构建不包含本地密钥、测试仓库或用户任务数据。公开分发的 Developer ID 签名/公证属于独立发布工程。

## 9. 对 Agent 评测与自迭代的复用

该能力可以复用于 Agent，但应做 Subject 泛化，不能把“Skill 文件修改”直接冒充“Agent 优化”。

### 9.1 可直接复用

- Test Obligation、Case/Suite Gate、Fixture、Oracle、Path Contract；
- Session/Attempt、完整 Trace、证据有效性和多维 Grader；
- Failure Signature、归因、问题簇、影响图和冲突；
- dev/validation/regression/holdout、paired comparison、pass^k；
- Champion/Challenger、预算、循环检测、受控停止；
- 桌面任务中心、证据检查器、浏览器验证和审计事件。

### 9.2 必须新增的 Agent Adapter

`AgentSubject` 需要冻结：模型及版本、system/developer Prompt、Skill 集、工具 Schema、权限、记忆、Agent Harness 配置和环境。Candidate Surface 可以是 Prompt、工具策略、Skill 组合、模型参数或 Harness 配置，但每类都要独立授权和验证，不能默认全部可编辑。

### 9.3 更严格的 Agent 风险

- 模型版本变化可能比 Skill Diff 影响更大，必须进入 `run_context_hash`。
- 工具/权限修改属于安全面，不允许由普通质量 Proposal 自动批准。
- 多 Agent 编排需要 Thread/Role 级 Trace 和因果关系，不能只看最后输出。
- 不能优化模型权重或不可见平台行为；系统只能优化声明的 Agent 配置表面。

因此最终抽象应是：

```text
EvaluationSubject
├─ SkillSubject（当前主产品）
└─ AgentSubject（复用控制面，新增快照/候选/运行 Adapter）
```

Skill 仍是第一落点，因为修改表面更可控、价值闭环更短；Agent 复用作为架构兼容目标，不应稀释当前产品交付。

## 10. 实施结果与后续演进

### Phase A：冻结契约与清理资产

状态：已完成。

- 完成 Kernel V2 Schema、状态机、Policy、事件和 Read Model。
- 将旧版、已完成和不采纳方案移动到 `docs/archive/`，根目录只保留 README 和工程文件。
- README 只指向本最终架构、当前使用手册和版本状态。

### Phase B：Kernel V2 P0

状态：本地产品主流程已完成。更严格的 sealed Holdout 策略继续保留在高级 Orchestrator/后续 Policy，不作为所有任务的默认硬门。

- Case/Suite Gate 与生命周期；
- Attempt / Case Aggregate / Candidate Comparison；
- Failure Signature / Diagnosis Graph；
- Champion/Challenger 与受控停止；
- 将已有 Orchestrator 的 paired validation/holdout 接入用户主流程。

### Phase C：桌面客户端

状态：已完成 macOS arm64 客户端、内置 Kernel、D2C 与本地发布构建。

- Electron 安全壳、Python 服务生命周期、环境体检和密钥存储；
- 任务中心、创建任务、详情轨迹、证据检查器和用户确认；
- D2C 浏览器预览、插件管理、设计稿/实际/差异三栏；
- 桌面打包与迁移。

### Phase D：真实验收

状态：代码与连接器回归、打包应用启动/退出、浏览器 E2E 和安装包内 Node 的真实 D2C 双跑已完成；Apple 签名/公证不在本阶段范围。

- 代码评审完整线上任务；
- D2C 本地 Chrome 完整任务；
- 崩溃恢复、断网、日志缺失、插件失败、Git 回滚和 Holdout 回归；
- 真实用户路径走查和性能/可访问性修复。

## 11. 当前版本的完成定义

本轮工程只有在以下条件同时满足时完成：

1. 本文与 Kernel V2 成为唯一有效方案，旧方案完成归档；
2. Kernel V2 的 P0 控制面已进入主流程并有自动测试；
3. 桌面应用可以创建/读取真实任务、打开任务详情、驱动或恢复 Kernel；
4. D2C 可以从桌面完成环境体检、打开隔离页面、执行验证并查看设计稿差异；
5. 不依赖硬编码演示数据，错误/空状态/权限/恢复路径可用；
6. Python、桌面 E2E 和真实 Chrome smoke tests 通过；
7. README 清楚说明安装、配置、运行、限制和问题恢复。
