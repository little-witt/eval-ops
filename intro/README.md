# FORGE 产品介绍与使用说明

FORGE（ACEval）是一个本地优先的 Skill 评测与自迭代工作台。它面向需要长期维护 Prompt、`SKILL.md`、脚本、参考资料和 Agent 行为的工程团队：每次修改都先经过真实任务评测，再由证据驱动诊断和回归，最终决定是否晋升。

本说明结合当前客户端、`src/aceval/` Kernel 和已通过的桌面/Kernel 测试编写。D2C 具备独立 Worker 与视觉证据能力，但仍是增强项，以下不把它当作核心上手依赖。

## 一、产品解决什么问题

传统做法通常是：准备几条 Prompt → 跑一次 Agent → 看一个总分 → 让模型给出修改建议。这条链路有三个根本问题：

1. **结果不可解释**：不知道失败是 Skill、工具、环境、Fixture 还是评测标准导致。
2. **评测不可信**：日志缺页、仓库版本漂移、Case 没有可信 Oracle 时，仍可能被算成失败或成功。
3. **优化不可控**：候选可能针对单个 Case 过拟合，破坏已经稳定通过的能力。

FORGE 的核心亮点是“评测-自迭代核”：把评测设计、真实执行、证据资格、跨 Case 归因、候选修改、回归和收敛放进同一个有状态、可恢复、可审计的控制面。

## 二、总体设计：评测-自迭代核

```text
目标 / Skill / 用户 Case / 环境
  → Test Obligation Graph
  → EvalPack + Case + 语义执行路径
  → CATX 真实 Agent Session
  → 完整 Trace + 证据有效性
  → Attempt / Case Aggregate / Candidate Comparison
  → 跨 Case 问题簇与修改提案
  → 隔离 worktree 候选
  → 同 Case、同环境回归
  → Champion / Challenger 晋升或安全停止
```

### 2.1 Kernel 和模型的边界

- Kernel 负责任务状态机、版本快照、Case 生命周期、证据完整性、硬 Grader、路径约束、预算、回归、Champion 保护、Git 发布和停止条件。
- 本地 Codex / Claude 负责单一语义维度评分、根因假设、跨 Case 聚类和候选文本生成。
- 模型没有 Shell、Git、CATX 或任意文件权限，不能自行宣布“变好了”。

### 2.2 三层判定，而不是一个总分

| 层级 | 回答的问题 | 当前实现 |
|---|---|---|
| Attempt | 这次会话是否有完整、可信证据？ | Trace、日志完整性、仓库绑定、路径与硬事实 |
| Case Aggregate | 该 Case 是否重复稳定通过？ | `pass@k` / `pass^k`、稳定性复验 |
| Candidate Comparison | 候选是否值得替换 Champion？ | 硬门、关键回归、最小收益、预算与比较上下文 |

证据不足统一显示 `not_evaluable`，不会把基础设施故障错误归因给 Skill。

### 2.3 Case / EvalPack 如何生成

用户不需要从零编写完整测试集。系统从目标、成功标准、Skill 能力、历史失败和当前操作模式编译 Test Obligation Graph，生成或复用 EvalPack，并补齐 Case 与语义部分有序路径。每个 Case 会标明来源、Oracle 可信等级和生命周期：

`Draft → Executable → Calibrated → Frozen → Regression`

只有 Frozen 或有用户明确预期的可信 Case 才能授权 Skill 修改；模型草稿可以探索，但不能直接触发变更。

## 三、安装与启动

macOS arm64 应用位于 `desktop/dist/mac-arm64/FORGE Skill Evolution Studio.app`。应用已内置 arm64 Python Kernel 与 Electron Node；核心流程不要求用户安装 Python 或 Node。Git 用于仓库操作，D2C 预览才需要本机 Chrome/Chromium。

开发模式：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,yaml]'
cd desktop
npm install
npm start
```

## 四、首次配置：从设置到 READY

### Step 1：环境体检

确认 Kernel、Git、Codex CLI 均可用。D2C Worker/Chrome 可以暂时不是 READY，不影响普通 Skill 评测。环境体检的意义是区分“客户端缺依赖”和“远端任务失败”。

### Step 2：配置 CATX 远端评测

在“配置与环境 → 其他服务密钥与远端运行环境”中配置：

| 配置 | 是否必要 | 用途 |
|---|---|---|
| Vault IDs | 必要 | 远端会话可访问的资源集合 |
| `CATX_API_KEY` | 必要 | CATX API 鉴权 |
| `USER_MIS_ID` | 必要 | 用户身份标识 |
| `CATX_AGENT_ID` | 必要 | 选择远端 Agent |
| `CATX_ENV_ID` | 必要 | 选择远端运行环境 |
| `CATX_REPOSITORY_AUTHORIZATION_TOKEN` | 私有仓库必要 | 远端挂载仓库 |

保存后客户端会生成冻结 CATX Profile。初测、无 Skill 基线、候选回归和稳定性复验共享同一份环境契约，避免比较时悄悄换环境。

密钥通过 Electron `safeStorage` 保存，仅在受控子进程调用时注入；不会写入任务 JSON、事件、日志或页面状态。

### Step 3：接入本地分析模型

推荐路径：

1. 点击“自动读取 CC Switch”，识别当前 Codex 或 Claude Code 配置。
2. 配置复制到 FORGE 隔离 Profile，原始 `~/.codex`、`~/.claude` 和项目配置不修改。
3. Codex 用户也可以手动导入 `config.toml` 与 `auth.json`；认证副本权限为 0600。
4. 点击“读取真实模型列表”，选择模型与推理强度。
5. 点击“调用一次验证模型”，确认账户真正可调用，而不是只有目录可见。

本地模型只承担语义评分、根因假设和候选生成，不执行命令、不读取任意路径、不直接修改仓库。

### Step 4：确认 READY

创建任务前至少应满足：

- CATX Vault IDs 与必要密钥已保存；
- 本地 Codex / Claude Profile READY；
- 至少一个真实可用的分析模型；
- Skill 仓库 Git 权限可用。

## 五、新建任务：用户逐步操作

点击左侧“＋”或“新建评测与迭代”。客户端优先读取最近一次成功任务，其次读取本地非敏感草稿，自动填入 Skill 名称、Skill 分支、base-code 分支、操作模式、目标和成功标准。成功创建后，最新字段会成为下一次默认值；CATX 凭证和 Token 永不保存到草稿。

### 5.1 检查候选 Skill

确认 Skill 名称、SSH 地址、分支和本地路径。首次使用必须提供一次真实 Skill 仓库；后续重复评测通常无需重新填写。分支应指向希望作为 Champion 起点的版本。

### 5.2 选择操作模式

- **修复 repair**：针对已知失败问题修复。
- **优化 tune**：在现有能力上提升质量、稳定性或成本。
- **增加功能 extend**：扩展新的能力或场景。
- **探索 discover**：探索未覆盖能力，只收集证据，不自动授权修改。
- **从零生成 create**：为新 Skill 建立首版能力与评测。
- **自动识别 auto**：由 Kernel 根据输入和历史状态选择路径。

默认是“优化”，适合最近一直在迭代的 Skill。

### 5.3 检查目标与成功标准

目标描述“希望改变什么”，成功标准描述“什么算完成”。建议每行一条、可观察、可验证：

```text
目标：提升安全评审 Skill 对高风险缺陷的召回率，并保持证据引用完整

成功标准：
覆盖关键缺陷类型
结论引用本次会话中的不可变证据
严格遵循必要的安全检查步骤
重复运行结果稳定，不引入已通过能力回归
```

如果留空，客户端会使用最近任务的标准或通用默认标准；如要改变评测契约，必须明确修改并在后续 Case 审阅中确认。

### 5.4 配置可选 Case 与 base-code

没有 Case 也可以创建任务，系统会自动生成。需要绑定具体 PR 或 Fixture 时，再填写 base-code 仓库 SSH、分支和本地路径；代码评审 Case 还需提供独立 fixture 分支、base commit 和 head commit（40 位且不同）。

### 5.5 创建并开始：详情页关注什么

点击“创建并开始生成”后客户端立即跳转任务详情，不等待后台全部完成。按以下顺序关注：

1. **EvalPack**：覆盖目标、标准和关键风险；来源与生命周期是否合理。
2. **Case / Path**：路径是语义部分有序；关注 required、forbidden 和预期证据。
3. **CATX Session**：会话完成只代表传输结束，不代表 Case 通过；需等待日志完整性与绑定检查。
4. **Evidence / Verdict**：区分 Attempt、Case Aggregate 和 Candidate Comparison，不把单次成功当稳定通过。
5. **Diagnosis**：问题以跨 Case 问题簇呈现，并绑定日志、路径和受影响维度。
6. **Proposal / Diff**：修改前会停在确认门；检查文件范围、影响 Case 和回归保护集合。
7. **Regression / Promote**：候选复用同一批冻结 Case 和同一环境；无硬回归且达到最小收益才晋升。

## 六、任务详情中的人工决策点

- **评测方案确认**：选择要运行的 Case，可为探索 Case 补充人工确认标准。
- **能力蓝图确认**：从零生成或扩展任务中确认能力范围。
- **优化范围确认**：选择问题簇对应的最小修改集合。
- **候选发布确认**：查看多文件 Diff、本地校验、提交信息和影响面。
- **证据不足处理**：优先定向重试证据或返回 Case 审阅，不要把 `not_evaluable` 当成失败。

所有确认都会写入审计事件；拒绝候选不会破坏 Champion，失败流程可从已保留证据恢复或创建全新重跑任务。

## 七、常见问题与排障

**新用户真的可以只配置 CATX 和本地模型吗？**  
对于已有最近任务的客户端，可以直接继承 Skill、base-code、目标和标准。全新安装仍需首次提供一个 Skill 仓库地址和 Git 权限，这是评测对象本身，不能凭空推断。

**为什么模型列表为空？**  
确认 CC Switch/Codex 配置导入成功，再点击“读取真实模型列表”。目录读取失败不会影响原始配置；可执行一次模型探针定位鉴权、provider 或模型可用性问题。

**为什么 CATX READY 但 Session 创建失败？**  
检查 Vault、Agent、Environment、MIS 和仓库 Token 是否属于同一环境；私有仓库还需确认远端授权。任务详情中的错误会保留，不会静默重建未知状态的 Session。

**为什么显示 `not_evaluable`？**  
通常是日志缺页、Trace 不完整、仓库绑定无法证明、Oracle 未就绪或环境漂移。系统会先补证据，再决定是否归因。

**为什么候选分数更高却没有晋升？**  
晋升不是单一总分门禁。候选还必须通过关键回归、稳定性、最小实际收益、预算和比较上下文一致性检查。

**本地模型会不会看到密钥或修改仓库？**  
不会。Renderer 无 Node/Shell/Git 权限；模型只接收裁剪后的结构化证据，Git 和候选写入由 Kernel 受控执行。

**D2C 是否必须安装 Chrome 或插件？**  
普通 Skill 评测不需要。D2C 预览与评分 Worker 是独立增强能力，插件只进入受控预览 Profile，评分 Worker 默认禁用插件。

## 八、验证与进一步阅读

```bash
PYTHONPATH=src python3 -m pytest -q
node desktop/tests/renderer-contract.test.mjs
node desktop/tests/ui.e2e.cjs
```

- [最终系统架构](../docs/current/FINAL_SYSTEM_ARCHITECTURE.md)
- [Kernel V2 方案](../docs/current/EVALUATION_SELF_ITERATION_KERNEL_V2.md)
- [发布就绪与验证证据](../docs/current/PRODUCT_RELEASE_READINESS.md)
- [桌面端开发说明](../desktop/README.md)
