# FORGE Skill Kernel｜Agent Skill 评测与进化核

## 作品名称

**FORGE Skill Kernel｜Agent Skill 评测与进化核**

英文名：**FORGE Skill Kernel — Evidence-Driven Evaluation & Evolution for Agent Skills**

一句话口号：**把 Agent Skill 锻造成可验证、可回滚、可持续进化的能力。**

## 作品简介

FORGE Skill Kernel 是一个本地优先的 Agent Skill 评测与进化桌面工作台。它以 ACEval 确定性 Kernel 为核心，把 Skill 的测试设计、真实 Agent 运行、完整证据回收、失败归因、受控修改和回归晋升串成一条可审计闭环。

面对一个复杂的 Agent Skill，FORGE Skill Kernel 不让模型自己出题、自己打分、自己改写并宣布成功，而是先建立可追溯的评测契约，再用真实运行结果回答：哪里失败、为什么失败、应该改哪些资源、修改后是否真的更好，以及有没有破坏原有能力。

## 作品详情

### 我们解决的问题

Agent Skill 往往由一份 `SKILL.md`、脚本、参考资料、模板和配置共同组成，实际行为还受到模型波动、工具权限、运行环境和外部服务影响。传统做法通常只有一次试跑或一个总分，难以回答几个关键问题：

- 复杂分支是否真的被覆盖，而不是只有一个“看起来覆盖”的 Case？
- 失败来自 Skill 本身，还是模型、工具、权限、环境或证据缺失？
- 修改是否提升了目标能力，同时伤害了已有能力？
- 一次偶然成功，是否被误认为稳定可靠？
- 结果、日志、产物和修改之间，能否逐层追溯？

FORGE Skill Kernel 将这些问题转化为可执行、可复现、可回滚的工程流程。

### 核心工作流

```text
Skill / 目标 / 标准 / 环境
          ↓
EvalPack 与测试义务生成或复用
          ↓
Case、语义路径、Fixture、Oracle
          ↓
真实远程 Agent 多会话执行
          ↓
逐 Case Trace、日志、产物与成本证据
          ↓
硬事实判定 + 多维评分 + 跨 Case 诊断
          ↓
用户批准的最小范围多文件候选
          ↓
Champion / Challenger 成对回归
          ↓
Promote、Reject 或 Safe Stop
```

### 关键创新点

1. **路径先于 Case**：围绕触发、前置条件、正常、边界、错误、重试、降级、状态、副作用、安全和清理等分支族组织测试，减少“假覆盖”。
2. **Kernel 守住评测边界**：ACEval Kernel 将 Skill、Case、Path、Trace、Verdict、Diagnosis 和 Candidate 串成证据链，确保每次结论都有来源、有上下文、有回滚点。
3. **证据优先的判定**：Attempt、Case Aggregate、Candidate Comparison 分层保存；日志或环境不完整时返回 `not_evaluable`，不把基础设施问题误记为 Skill 失败。
4. **真实 Agent 而非本地假运行**：支持 CATX 多会话、双仓库挂载、完整日志回收和冻结运行环境，让评测更接近真实使用条件。
5. **受控多文件进化**：不仅能调整 `SKILL.md`，还可在授权范围内修改脚本、references、workflow、模板和配置，并保留 diff、hash、验证回执与 Git 回滚点。
6. **质量与效率一起看**：除 Outcome、Procedure、Grounding、Runtime 外，还可以比较 Token、工具调用和耗时；候选必须在收益、稳定性、安全和成本之间取得可解释的平衡。
7. **从 Skill 延展到 Agent**：通过 `EvaluationSubject` 契约，未来可复用到 Prompt、工具策略、模型参数和 Agent Harness 的版本化评测与优化。

### 产品形态

- **任务中心**：查看任务状态、最新收益、预算和待确认事项。
- **任务详情**：查看 EvalPack、Case/Path、远程 Session、Trace、诊断、Proposal 和 Champion/Challenger。
- **证据检查器**：从结论跳转到原始日志、事件区间、产物、截图、差异图和 Grader。
- **本地优先与安全边界**：密钥使用隔离 Profile 和系统安全存储；Renderer 无 Shell/Git/任意文件读取权限；副作用通过白名单和审计事件完成。
- **D2C 验证**：对页面类 Skill 使用隔离 Chrome Worker，采集 DOM、console、network、截图和视觉差异证据。

### 典型使用场景

- 复杂代码审查 Skill 的分支覆盖、证据引用和安全边界验证；
- 需要压缩 Token、工具调用或响应耗时，同时保持结果准确的 Skill 调优；
- 输出格式、报告结构和细节层级的可验证调整；
- 需要从一次性 Prompt 试验升级为可复现工程流程的团队；
- 希望将 Skill 评测资产沉淀为团队 Case、失败签名和回归保护集的平台。

### 当前完成度与验证

当前项目包含 Python ACEval Kernel、Electron 桌面端、CATX 远程执行、受控候选实验室、Git 发布与回滚、D2C 浏览器验证和完整测试套件。发布验证文档记录了 Python 单元/集成测试、Renderer 契约测试、真实 Chrome D2C 双跑、macOS arm64 自包含应用启动与退出烟测等证据。

### 作品价值

FORGE Skill Kernel 的核心价值不是“让模型多写几条测试”，而是把 Agent Skill 的能力、路径、证据、评分、诊断和修改连接成可积累的资产链。它让团队可以用工程方法持续回答“这次变好了吗”“为什么变好”“有没有破坏什么”，并在不确定时安全停止，而不是依赖一次 Demo 的主观印象。

## 关键词

Agent Evaluation · Skill Evolution · Traceability · Regression Guard · Local-first · Evidence Graph · Champion/Challenger · Safe Stop
