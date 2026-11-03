# 让 Skill 改进有迹可循：FORGE / ACEval 评测与自迭代工程实践

> 本文面向第一次了解 FORGE / ACEval 的读者，重点说明为什么要做、如何评测、怎样从 Bad Case 找到问题，以及修改如何安全进入下一轮。  
> 叙事结构参考《[让改进有迹可循：Storage Agent Family Eval 工程实践](https://mp.weixin.qq.com/s/pjSP60ycITOafaJwXxW23g)》，内容、案例和结论均来自当前项目。

开发一个 Skill，最初看起来可能只是写好一份 `SKILL.md`。但真正投入使用后，影响 Agent 行为的往往不止一份提示词，还包括脚本、参考资料、模板、配置、工具权限和运行环境。

这些部分彼此关联：今天补充一条规则，可能修好一个 Case，也可能让原本正常的 Case 开始误报；换一个模型、工具版本或 Fixture，还可能让两次评测失去可比性。

如果每次修改后只手工问几个问题，或者只看最终回答是否流畅，很难回答三个关键问题：

1. 这次修改到底提升了什么？
2. 新版本有没有破坏已有能力？
3. Bad Case 究竟是 Skill 的问题，还是模型波动、工具、环境或评测本身的问题？

FORGE / ACEval 围绕这三个问题建立了一套本地优先的评测与自迭代闭环：将目标转成可复用的 EvalPack，让真实 Agent 在冻结环境中执行 Case，回收 Trace 和产物，区分硬事实与语义判断，再由用户和 Kernel 共同控制候选修改、回归与晋升。

当前仓库已经实现 V2 / P0 的本地控制主链和远程 Trial 底座；完整 V3 以及目标业务 Skill 的真实 CATX 多轮验收仍待继续完成。下文会明确区分当前实现、FakeRuntime 编排样例和目标能力。

```text
目标 / 标准 / Skill
  → EvalPack、Case 与语义路径
  → 真实远程 Agent 执行
  → Trace、状态与产物回收
  → 多维裁决与失败归因
  → 用户批准修改范围
  → Candidate 生成与发布
  → Champion / Challenger 回归
  → 晋升、拒绝或安全停止
```

## 01｜如何构建覆盖结果、过程和安全的评测体系

先看项目内置的 Security Review Skill 示例。用户给 Agent 一个 Python 文件，要求识别安全问题，并返回包含 `findings` 数组的 JSON。

表面上，这个任务只需要“找出漏洞”。实际上，一个可信的安全审查至少要同时满足：

- **任务结果正确**：识别真实的 SQL 注入、命令注入、路径穿越等问题；
- **控制典型误报**：预注册的参数化 SQL、白名单命令和安全路径对照不应命中特定禁止记录；
- **证据可定位**：每条发现必须指向真实文件和行号；
- **过程可观察**：当前 Pack 至少检查 Trace 中出现 `read_file`，目标是进一步证明读取了正确 Fixture；
- **输出可消费**：结果必须符合约定 JSON Schema；
- **安全边界明确**：目标设计中不能通过执行危险代码来证明代码危险；当前 Pack 尚未配置 `forbidden_tools` 来单独验证这一点。

因此，FORGE 不把评测理解为“准备几个问题，再让模型打一个总分”，而是将它拆成 EvalPack、Case、路径和 Grader。

### 1.1 EvalPack：一套版本化的考试包

EvalPack 用于冻结一组可重复使用的评测资产：

| 组成 | 作用 | Security Review 示例 |
|---|---|---|
| Case | 一道具体测试任务 | 审查 `command_injection.py` |
| Fixture | 测试输入和环境 | 含种子缺陷或安全写法的 Python 文件 |
| Oracle | 可信预期 | 应发现 `command-injection`，文件和行号必须匹配 |
| Path | 关键行为约束 | 必须调用 `read_file`，不限定唯一阅读顺序 |
| Grader | 执行判定 | Schema、记录匹配、源码引用、Trace 检查 |
| Split | 控制优化与泛化验证 | Dev、Validation、Holdout |

EvalPack 可以由用户提供，也可以根据 Skill、目标、标准和用户 Case 自动生成。subject commit、Case revision、Fixture、Grader、Schema 或关键环境发生变化时，系统会建立新的设计 revision，不能继续把旧分数当作同一指标。

### 1.2 Case：不仅测“会不会”，也测“会不会乱说”

项目内置的 Security Review EvalPack 包含：

- 6 个 Dev Case：5 个真实缺陷和 1 个安全参数化 SQL 对照；
- 2 个 Validation Case：未见过的编码路径穿越和白名单命令安全对照；
- 2 个 Holdout Case：`os.popen` 命令注入和经过包含校验的安全路径。

这种正例与负例配对非常重要。只放漏洞正例，Agent 可能学会“见到 SQL 或 subprocess 就报错”；加入安全对照后，才能检查这些已注册场景中的典型误报。当前 `record_match` 仍允许额外 finding，尚不能据此宣称所有误报都被拦截。

### 1.3 路径：观察关键行为，而不是背诵唯一步骤

Agent 完成同一任务可能有多条合理路线，因此目标路径契约可以采用下面的语义约束：

```text
required     必须读取目标文件
alternative 可以采用不同分析工具或阅读顺序
forbidden    不能编造不存在的文件、行号或漏洞
recommended 最好给出可执行的修复建议
局部顺序     必须先获得授权，再执行任何写操作
```

最终结果、安全、权限和强制业务步骤可以是一票否决的硬门；普通工具顺序不会被错误地当成唯一标准答案。

当前实现已经具备能力分析、风险规划、测试义务和语义路径。目标 V3 会进一步做到完整的“路径先于 Case”：先从 Skill 资源树提取分支、条件、状态、副作用和恢复路径，再生成覆盖这些路径的 Case。完整 Path IR 仍在建设中，当前部分路径会在 Case 生成后补齐。

### 1.4 Grader：规则判事实，模型判开放语义

FORGE 框架将评分职责分开：

- **确定性 Grader**：检查状态、Schema、文件、行号、工具参数、禁止动作和产物；
- **语义 Judge**：判断任务理解、回答是否有依据、建议是否可执行等开放问题；
- **人工校准**：确认业务标准、处理争议评分和高风险修改。

Security Review Pack 当前实际配置的是四个确定性硬 Grader：JSON Schema、预期/禁止记录匹配、源码文件与行号引用、`read_file` 调用次数。语义 Judge 是框架级能力，不是这个 Pack 当前的评分组成。

模型不能推翻硬事实。例如 Agent 的文字解释很专业，但 Trace 中没有出现 `read_file`，`file-inspection-trace` 仍会直接失败；如果日志不完整，则返回 `not_evaluable`，不会猜测为 Skill 失败。当前 Grader 只检查出现了 `read_file`，尚未证明读取目标一定是正确 Fixture，这属于后续 Trace 契约需要补强的边界。

## 02｜如何让每次 Skill 修改都有据可查

一次可信的回归实验，需要把“被测版本、题目、环境、证据和结论”绑定在一起。

### 2.1 三个角色共同完成一次实验

| 角色 | 负责什么 | 不负责什么 |
|---|---|---|
| FORGE Desktop | 任务创建、审批、进度、证据查看和版本对比 | 不直接执行 Shell、Git 或远程 Session |
| ACEval Kernel | 冻结契约、调度 Trial、验证证据、比较候选和控制收敛 | 不自由改写 Skill，不替用户扩大授权范围 |
| 真实 Agent / CATX | 在隔离 Session 中执行 Case，产生回复、工具调用和产物 | 不修改 Oracle，不决定自己是否通过 |

本地分析模型负责开放语义评分、跨 Case 根因假设和候选内容，但没有证据资格、晋升或停止权。

### 2.2 冻结可比较的运行条件

开始执行前，系统冻结 Trial Environment Contract，包括：

- Skill commit、资源树和内容 hash；
- 代码仓库、Fixture 和 Case revision；
- Agent、模型、工具 Schema、权限和运行参数；
- EvalPack、Grader 和评分配置；
- 远程执行绑定和环境标识。

Champion 和 Challenger 只有在 Case 集、重复预算、Grader 版本和比较上下文一致时，才能直接计算差异。环境漂移不会被包装成“模型能力变化”。

### 2.3 每个 Case 独立执行并保留 Attempt 历史

系统按 Case 分发远程 Session，并回收：

- 最终回答和结构化产物；
- 工具调用、参数和返回结果；
- Session 状态、错误和耗时；
- 日志分页、`total`、部分序号/工具配对检查和整体 hash 收据；
- Attempt 编号、重试原因和历史产物。

执行和评分相互分离。只要本次 Trace、状态和产物通过现有完整性门，调整语义评分规则时可以重新分析，不必重新执行同一个远程任务。当前尚无逐事件 hash chain、强 terminal 配对和完整可重放 EventJournal，因此不能把整体 hash 等同于完整防篡改日志。

### 2.4 三层裁决避免“一次跑分定生死”

评测结论分为三层：

#### Attempt：判断一次执行

一次 Case 执行产生一个 Attempt Verdict，包括 Evidence Validity、Outcome、Procedure、Grounding、Runtime 和可选 Efficiency。

#### Case Aggregate：判断是否稳定

同一 Case 的多个 Attempt 被聚合，得到观察成功率、波动状态和稳定通过结果。

- `pass@k` 估计运行 k 次至少成功一次的概率；
- `pass^k` 估计运行 k 次全部成功的概率；
- 实际晋升使用更严格的 `stable_pass`，要求冻结的重复次数全部通过。

`pass@k` 和 `pass^k` 依赖独立同分布等统计近似，只是辅助指标，不能替代实测 Attempt。

#### Candidate Comparison：判断新版本能否取代旧版本

当前稳定版本是 Champion，修改后的版本是 Challenger。比较结果只有三种：

- `accepted`：无硬回归、证据充分、稳定性达标且收益超过门槛；
- `rejected`：出现退化、硬门失败或收益不足，恢复 Champion；
- `inconclusive`：证据不足或上下文不可比，保留 Challenger 等待补证据。

当前轮次还提供一个直观摘要：

```text
quality_score = 稳定通过的 Case 数 / 本轮参与分析的 Case 数
```

同一 Case 的多个 Attempt 在分母中只算一个 Case；`not_evaluable` 无法成为稳定通过，因此会压低该分数。不同 Case 集或 revision 的 `quality_score` 不可直接横向比较。

但总分不拥有最终决定权。一个安全 Case 退化，不能靠其他 Case 的高分抵消。

### 2.5 用内置 EvalPack 看一次版本对比

Security Review EvalPack 在 `scenario.metadata.fake_runtime.variants` 中预置了下面这组模拟行为，用于验证编排和比较契约：

| Split | 基线行为 | 候选行为 |
|---|---:|---:|
| Dev | 仅能识别明显 SQL 拼接，并命中安全 SQL 的禁止记录，模拟结果 1/6 通过 | 覆盖 5 类缺陷且避开该禁止记录，模拟结果 6/6 通过 |
| Validation | 漏掉编码后的路径穿越，并命中白名单命令的禁止记录，模拟结果 0/2 通过 | 两个模拟输出均满足 Oracle，模拟结果 2/2 通过 |
| Holdout | 漏掉 `os.popen` 注入并命中安全路径的禁止记录，模拟结果 0/2 通过 | 两个模拟输出均满足 Oracle，模拟结果 2/2 通过 |

这些结果明确标记为 `simulated=true`：真实 Reference Runtime 会忽略 `fake_runtime` 元数据。因此它们只能证明评测、比较和优化链路能处理这组输入输出，不能证明真实 Skill 或模型已经达到对应成绩，也不能单独证明泛化能力。

这组设计同时回答两类问题：新版本能否发现更多真实漏洞，以及它是否通过“逢危险 API 就报错”换取表面召回率。

## 03｜如何从一个 Bad Case 定位到具体 Skill 问题

评测报告只能告诉我们“哪里没通过”，Trace、Oracle 和 Skill source ref 才能帮助回答“为什么没通过、该改哪里”。

### 3.1 一个命令注入漏报示例

在 `command-injection-dev` 中，Fixture 包含一段将不可信 host 插入命令，并通过 `shell=True` 执行的代码。Oracle 要求输出：

```text
rule_id = command-injection
file    = command_injection.py
line    = 5
```

FakeRuntime 为基线预置的 Trace 记录了对 `command_injection.py` 的 `read_file` 调用，因此当前过程门通过；但最终 `findings` 为空，结果门失败。

这个 Case 不能只得到一句“模型没识别出来”。系统需要继续判断：

1. Session 和日志是否完整；
2. Agent 是否真实读取了目标文件；
3. Oracle、Fixture 和行号是否有效；
4. Skill 是否已经声明了从不可信输入到危险执行点的数据流规则；
5. 是单次随机漏判，还是同类 Case 都存在一致缺口。

如果前四类证据有效，并且多个命令执行相关 Case 都显示 Skill 缺少数据流检查指引，分析模型才可以提出“补充命令注入识别规则”的根因假设。Kernel 只负责确认这个假设是否具备修改资格，不能把模型假设包装成确定事实。

### 3.2 Bad Case 不等于 Skill 有错

| 观察结果 | 处理方式 | 是否修改 Skill |
|---|---|---|
| Session、日志、Fixture 或 Grader 不完整 | `not_evaluable`，先修证据链 | 否 |
| 网络、权限或外部工具明确失败 | 环境/依赖问题 | 否 |
| 同一契约下有时通过、有时失败 | flaky，增加重复验证 | 暂不直接修改 |
| 行为违反 Skill 明确要求 | Agent 执行偏离，结合跨 Case 证据判断 | 不自动等同 Skill 缺陷 |
| 多个有效 Case 指向相同规则缺口，且可定位 source ref | Skill-attributed 候选问题 | 可以提出修改，仍需用户批准 |
| 证据冲突或原因不唯一 | unknown / 人工复核 | 否 |

模型没有一套预设的业务答案。它使用的是 Case 标准、Trace、状态、Skill 资源和通用失败特征；具体业务 Oracle 必须来自用户标准、权威规范或校准过的 EvalPack。

### 3.3 修改不是重写整份 Skill，而是一个可证伪实验

每个 Proposal 需要说明：

- 对应哪些失败 Case 和证据；
- 推测缺失的是哪项能力或规则；
- 预计改善哪些维度；
- 哪些稳定 Case 必须保护；
- 允许修改哪些文件；
- 用什么回归结果证明假设成立或不成立。

用户批准后，修改模型只能在 allowlist 范围内执行精确 `replace_text` 或经过批准的 `create_file`。系统限制修改文件、行数和字节数，并检查语法、引用、敏感信息和仓库状态。

Security Review EvalPack 还启用了 `forbid_case_literals`：候选不能把具体题目中的文件名、行号或测试字面量写进 Skill，避免针对固定题目“背答案”。Validation 和 Holdout 再用不同语法、不同安全对照检验修改是否真正泛化。

### 3.4 下一轮到底基于哪个版本修改

```text
Champion
   └─ 生成 Challenger
        ├─ accepted     → Challenger 成为新 Champion，下一轮在它上面继续
        ├─ rejected     → Git revert 恢复 Champion 内容，下一轮从 Champion 继续
        └─ inconclusive → 保留同一 Challenger 补证据，不重新生成另一个补丁
```

系统不会在已知退化的候选上继续叠加修改，也不会因为一次语义高分就覆盖稳定版本。

## 04｜如何处理中断、重试和幂等

评测链路包含远程 Session、日志回收、模型分析、候选生成和 Git 发布，任何一步都可能中断。FORGE 的原则不是“失败后整轮重来”，而是从最近的可信阶段恢复。

| 中断位置 | 恢复策略 |
|---|---|
| 远程 Session 已有 ID | 继续轮询和拉日志，不重新创建 |
| 部分 Case 因传输或基础设施失败 | 只为这些 Case 创建新 Attempt |
| 日志拉取失败 | 继续拉取原 Session 的日志 |
| 本地分析模型失败 | 复用冻结 Trace，从失败阶段重新分析 |
| 候选生成失败 | 保留审批和分析结果，生成新的候选产物 |
| Challenger 证据不足 | 保留同一 commit，补充证据后继续比较 |

请求 fingerprint、环境 hash、Case revision、Session ID、Attempt ID、原子写入和 Candidate parent hash 用于识别重复请求、输入漂移和错误父版本；追加式事件主要用于审计与恢复定位，不能单独提供幂等保证。

有效证据下的业务 `fail` 不会为了挑一次成功而自动重跑；基础设施恢复产生的新 Attempt 也不会覆盖旧历史。

当前仍有一个真实边界：CATX 创建 Session 没有原生幂等键。如果远端已经创建 Session、但本地尚未保存 ID 时进程崩溃，系统无法证明是否成功。此时会 fail closed，拒绝自动再次创建。人工确认后可以重试，但远端不能保证 exactly-once，有副作用的 Case 必须先确认隔离或回滚条件。

## 05｜如何让每次失败沉淀为下一轮资产

一次评测的价值不仅是得到分数，更重要的是留下可复用资产：

- Case、Fixture、Oracle 和 Grader revision；
- 原始 Session、Trace、产物和证据引用；
- 失败维度、Failure Card 和跨 Case Problem Cluster；
- Skill source ref 与受影响范围；
- Candidate Diff、比较结果和拒绝原因；
- 已稳定通过的 Regression 保护 Case。

当同一个问题被修复并稳定通过后，对应 Case 会进入回归保护。下一次修改不能只证明新问题解决了，还要证明这些历史能力没有被破坏。

对于线上或真实业务中新发现的问题，推荐的回流流程是：

```text
真实失败或人工发现
  → 去敏、去重和复现
  → 补充独立 Oracle 与测试条件
  → 通过 Case / Suite 质量门
  → 建立新 Case revision
  → 加入 Dev / Validation / Regression
```

当前项目已经具备 Case provenance、revision、content hash、失败卡片/问题簇和回归保护的核心契约；规范化 Failure Signature、完整跨任务 Case 晋升、Reference Run、Suite Gate 和线上会话自动回流仍属于后续建设范围。

## 06｜这套方法与主流方案有什么不同

FORGE / ACEval 没有发明一种全新的基础评分算法，它的差异在于把多种成熟方法组合成 Skill 生命周期闭环。

| 本文对比方向 | 主要关注点 | FORGE / ACEval 额外关注什么 |
|---|---|---|
| Agent Eval / Trace 平台 | 得到 Trial、Trace 和评分 | 将证据连到 source ref、修改范围、候选 Diff 和晋升门 |
| 固定 Benchmark | 比较不同 Agent 的排行榜成绩 | 为具体业务 Skill 建立可演进的评测资产和回归保护 |
| Prompt / Skill Optimizer | 反思轨迹并生成新版本 | 不让优化模型定义 Oracle、决定归因、扩大权限或自行晋升 |
| 传统 CI | 精确断言和确定性回归 | 支持多种合法路径、模型随机性、语义 Judge 和 `not_evaluable` |

最核心的分工是：

> 系统负责可复现的事实、权限、状态、比较和停止；模型负责难以规则化的语义判断、根因假设和候选内容；用户负责目标、取舍和高风险修改授权。

## 结语

衡量一个 Skill 的质量，不能只看最终回答是否像样，也不能只看一次运行是否成功。更关键的是：它能否在真实环境中完成任务、给出有依据的结论、遵守关键路径和安全边界，并在多次执行中保持稳定。

FORGE / ACEval 将这些要求沉淀为版本化 EvalPack、真实 Trial、可追溯证据、分层 Verdict 和 Champion / Challenger 实验，让每一次修改都能回答：为什么改、改了什么、改善了什么、有没有退化，以及为什么最终被接受或拒绝。

当前工程已经实现 V2 / P0 的本地闭环主链、恢复门禁和远程执行底座；这不等于已完成目标业务 Skill 的真实多轮远端验收。下一阶段重点是补齐完整 Path IR、可重放 EventJournal、Case / Suite 质量门、Reference Run，以及更多真实业务 Skill 的多轮 CATX 验收。
