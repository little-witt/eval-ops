# Agent Skill 评测与回归安全自修复：研究路线

**状态**：研究计划草案（未运行实验）  
**日期**：2026-08-26  
**目标投稿方向**：ICSE / FSE / ASE（优先），ML benchmark venue 为后备选项

## 1. 研究定位

### 1.1 问题

开放 Agent Skills 通常是版本化工件：`SKILL.md` 与其 references、scripts、配置及工具约束。现有 Agent 评测主要回答“端到端 Agent 是否完成任务”，而 Skill 评测还需回答：该工件能否被正确触发、遵循、执行，并在任务正确性、成本、过程约束和安全边界上产生**净增益**。

系统已经具备冻结 EvalPack、远程多会话执行、日志回收、确定性失败归因、候选 patch 约束、dev/validation/holdout 门控、回归检查、人工审批与收敛停止。首篇论文不应主张“支持任意 Skill”或“构建了客户端”，而应验证一个可证伪的方法命题。

### 1.2 核心命题

> 多路径执行的轨迹分歧是否是 Agent Skill 潜在缺陷的可靠信号；使用该信号驱动、受约束的 Skill 工件修复，能否在相同预算下提高未见任务上的安全净效用并减少回归？

### 1.3 不应宣称的内容

- 不宣称任意 Skill 都可安全修复。
- 不宣称当前 CATX 同用户 workspace 隔离是 hostile-code sandbox。
- 不将模型优化“收敛”描述为全局最优；它仅是满足门槛下的安全停止。
- 不将内部 Skill、内部日志或不可公开的运行数据作为公开论文主结果。
- 不将 GUI、CATX 调度或任意模型 API 配置作为方法创新本身。

## 2. 已有研究资产

| 能力 | 已有实现证据 | 研究作用 |
|---|---|---|
| EvalPack 完整性冻结 | `src/aceval/pack.py` | case/oracle/fixture 在实验中不可被优化过程篡改 |
| 三阶段 split | `src/aceval/orchestrator.py` | dev → validation → sealed holdout 防止过拟合 |
| CATX 多会话与 trace 回收 | `src/aceval/catx.py`、`catx_runtime.py` | 采集多路径执行证据与资源消耗 |
| 确定性失败归因 | `src/aceval/failure_attribution.py` | 区分 Skill、环境、fixture、grader 与 oracle 失败 |
| 受限 Skill patch | `src/aceval/optimizer.py`、`skill_tree_optimizer.py` | 路径白名单、行数/字节限制、case literal 防护 |
| 回归与收敛门 | `src/aceval/kernel_v2.py`、`orchestrator.py` | Champion/Challenger、hard regression、fail-closed 停止 |
| 公开样例包 | `evalpacks/security-review/`、`evalpacks/csv-summary-smoke/` | PoC 起点，需扩展为研究规模 |

## 3. 研究问题与假设

### RQ1：轨迹分歧能否预测真实 Skill 缺陷？

**H1**：高轨迹分歧的 `(skill, task)` 对，相比低分歧对，更可能包含经缺陷注入真值或人工审查确认的 Skill 缺陷。

分歧不能直接等同于缺陷。分歧还可能来自采样随机性、环境波动或任务歧义，因此必须建立“分歧—缺陷一致性”数据集，并以确定性 oracle 和带真值的注入缺陷作验证。

### RQ2：多路径证据能否改善失败归因？

**H2**：多路径证据相比单路径归因，提高 Skill-defect attribution precision，并降低将 runtime、fixture、grader 或 oracle 故障错误归因为 Skill 的比例。

### RQ3：分歧引导的受约束修复能否提高 holdout 安全净效用？

**H3**：在相同模型、相同 token/会话预算及相同最大迭代轮数下，完整方法比单路径受约束修复、无归因多路径修复和预算匹配的天真自迭代拥有更高 holdout SafeNetUtility 和更低 critical regression rate。

## 4. 方法定义

### 4.1 轨迹分歧指纹（Trajectory Divergence Fingerprint, TDF）

对于固定的 Skill `S`、case `t` 与运行配置 `c`，运行 `K` 条独立路径，得到轨迹集合 `T = {τ_1, …, τ_K}`。从归一化 `TraceEvent` 提取：

- outcome（确定性 verifier 结果）；
- required execution-path checkpoint 的遵循情况；
- 工具调用序列与关键参数哈希；
- 首次偏离 required checkpoint 的位置；
- usage：token、工具调用、重试、时延；
- `FailureAttributor` 的确定性失败类别。

初始分歧分数：

```text
D(S, t) = α · outcome_disagreement
        + β · required_checkpoint_divergence
        + γ · tool_sequence_divergence
        + δ · normalized_cost_variance
```

其中权重仅在 dev 数据预先确定，冻结后不根据 holdout 调整。优先使用 required checkpoint 之后的局部行为分歧，而不是全轨迹文本相似度，降低无关语言变化噪声。

### 4.2 Evidence-gated repair

只有在以下条件同时满足时才允许发起候选 patch：

1. EvalPack、Skill revision、runtime/model profile 已冻结或哈希可核验；
2. 失败归因指向 Skill，而非环境、fixture、grader 或 oracle；
3. 多路径 evidence 达到阈值，或高置信硬失败已有足够确定性证据；
4. patch 仅修改声明的 Skill editable scope；
5. patch 不包含 case/oracle 字面量，不扩大权限、网络或系统副作用。

### 4.3 回归安全候选选择

候选必须依次通过：

1. dev：只用于诊断与生成；
2. validation：选择 Candidate/Champion，验证增益不来自局部刷分；
3. regression set：稳定通过 case 不得出现 PASS → FAIL；
4. sealed holdout：仅一次或预注册次数的最终确认，不得反馈给 optimizer；
5. divergence regression check：修复不得在既有稳定 case 上显著增加分歧。

### 4.4 主指标

```text
SafeNetUtility = ΔTaskUtility
               - λc · ΔCost
               - λr · CriticalRegressionRisk
               - λs · SafetyViolation
```

- `TaskUtility` 优先为确定性 artifact/record/schema/trace oracle 通过率；
- `Cost` 为 token、远程会话、工具调用、时延；
- `CriticalRegressionRisk` 为稳定通过 case 的失败翻转率；
- `SafetyViolation` 为受控安全 fixture 中的策略违规；
- λ 在实验前固定并公开。主表还须单独报告所有分量，不能只给一个黑箱总分。

## 5. 方法迭代路线：V0–V3

### V0：基线复现与分歧采集

**目标**：确认现有 EvalPack 中是否存在可观测、有意义的路径分歧。

**新增最小能力**：

- 对同一 Case、同一 Skill hash、同一模型 profile 重复发起 `K ≥ 3` 次 CATX 会话；
- 生成 TDF：工具序列编辑距离、checkpoint 偏离、outcome disagreement、cost variance；
- 将 TDF 与 `FailureAttributor` 的结果汇总为分歧—失败关联表。

**判定门**：

- G0.1：至少 30% 的非平凡 Case 出现可解释的轨迹/结果分歧；
- G0.2：高分歧 Case 的确定性 hard-fail / 注入缺陷比例高于低分歧 Case；
- G0.3：环境、fixture、grader 故障不能被错误计为 Skill defect。

**失败转向**：若分歧不存在，转向单路径确定性归因与回归安全评测；若分歧与缺陷不关联，则将分歧仅作为可靠性描述指标，不用于修复授权。

### V1：分歧预测与归因增强

**目标**：将跨路径证据正式输入 failure attribution，而不是仅做可视化。

**新增模块建议**：`divergence_predictor.py`

```text
DivergenceReport {
  case_id,
  attempt_ids,
  divergence_score,
  divergence_points,
  outcome_matrix,
  predicted_defect_surface,
  evidence_confidence
}
```

**关键原则**：分歧证据可以提升归因置信度，但不能在没有 Skill-level evidence 时绕过 `DENY_SKILL_INTERVENTION`。

**判定门**：

- G1.1：在 dev 的带真值缺陷集上，预测 defect 的 AUC ≥ 0.70 或具有预注册的中等效应量；
- G1.2：`ALLOW_SKILL_INTERVENTION` 的 precision 相比单路径至少提升 5 个百分点；
- G1.3：环境/fixture/grader 失败的 deny 率不下降。

### V2：分歧引导的回归安全修复

**目标**：把分歧证据转成更小、更可解释且更稳定的 Skill patch。

**新增点**：

- 根据 predicted defect surface 收窄 editable scope；
- 高分歧 Case 提高 `pass^k` 要求；
- candidate comparison 增加 divergence regression gate；
- 提示修复器引用具体 evidence，而非给自由的“改好 Skill”指令。

**判定门**：

- G2.1：在相同预算下，完整方法的 holdout SafeNetUtility 优于单路径受约束修复；
- G2.2：无 critical regression；
- G2.3：高分歧 Case 的分歧或失败率下降，而非仅单次成功；
- G2.4：完整方法额外 token/会话成本不超过预注册上限。

### V3：泛化、消融与论文定稿

**目标**：在多个开放 Skill 类型上验证，并完成公开 artifact。

**消融矩阵**：

| 组别 | 多路径证据 | 归因 | 受限 patch | 回归门 | 目的 |
|---|---:|---:|---:|---:|---|
| B0 原始 Skill | 否 | 否 | 否 | 否 | 无优化锚点 |
| B1 单路径受约束修复 | 否 | 单路径 | 是 | 是 | 隔离多路径净收益 |
| B2 多路径无归因修复 | 是 | 否 | 是 | 是 | 隔离归因净收益 |
| B3 完整方法 | 是 | 是 | 是 | 是 | 主方法 |
| B4 预算匹配天真迭代 | 可选 | 否 | 弱/否 | 否 | 排除“只是多跑模型” |
| B5 不受限 patch | 是 | 是 | 否 | 弱 | 附录：验证约束价值 |

**判定门**：

- G3.1：B3 相对 B1、B2 在多个 Skill 类型上同方向改善；
- G3.2：固定 pack/subject/model/runtime hash 后，判定与结果可重放；
- G3.3：所有研究工件、许可证、trace 脱敏和数据卡可公开。

## 6. Benchmark 设计

### 6.1 三层数据集

1. **可控缺陷集**：10 个公开 Skill 的 3–5 个真实感 defect variant，共 40–60 个带真值样本。包括触发边界、步骤遗漏、约束冲突、环境假设、输入鲁棒性、无界重试、敏感日志与不可信指令处理等。
2. **真实开放 Skill 集**：公开、可复现、许可证明确、可隔离运行、可定义确定性 oracle 的 Skills。
3. **安全压力集**：只使用无害模拟 fixture（提示注入文本、伪敏感配置、受控路径穿越、模拟命令注入、假外传 endpoint）。不执行真实破坏性命令或数据外传。

### 6.2 主实验规模

| 维度 | PoC（第 1 阶段） | 论文主实验（第 2 阶段） |
|---|---:|---:|
| 公开 Skills | 3 | 10 |
| 每 Skill 主 case | 20 | 35–45 |
| 注入缺陷变体 | 20–30 总计 | 40–60 总计 |
| path / case | 3 | 3–5（高分歧增至 5） |
| 优化 seeds | 3 | 3 |
| 主对照 | B0、B1、B3 | B0–B4 |

每个 Pack 采用：dev 15–20、validation 8–10、sealed holdout 10–15、可选 security/stress 5–10。holdout 必须与 dev 来自不同来源；不得将模型自动生成的 case 冒充 sealed holdout。

### 6.3 统计协议

- 主分析单位：Skill-level paired result，避免把同一 Skill 的大量 case 误当独立样本；
- 主比较：B3 vs B1，B3 vs B2，B3 vs B4；
- 使用 Wilcoxon signed-rank test 与 Cliff’s delta；
- case-level 使用以 Skill 为 cluster 的 block bootstrap，报告 95% CI；
- 多重主假设采用 Holm–Bonferroni；
- 报告各 seed 的 mean ± std；禁止只选择最佳 seed 或最佳优化轮；
- 对所有方法严格匹配模型、max rounds、token、会话和 patch 预算，并报告实际消耗。

## 7. 推荐候选公开 Skills

选择条件：有 `SKILL.md` 且包含相关 references/scripts 或明确工作流；许可证明确；可在临时 workspace 中运行；可构造确定性 oracle；不依赖内部数据；不以 GitHub star 作为质量信号。

### 7.1 PoC 首选

| 方向 | 候选 | URL | 许可证状态 | 可测任务与确定性 oracle | 风险 |
|---|---|---|---|---|---|
| 代码/安全审查 | `mukul975/Anthropic-Cybersecurity-Skills` 的防御性代码审查子集 | https://github.com/mukul975/Anthropic-Cybersecurity-Skills | Apache-2.0（需复现前再次核验） | 固定漏洞源码 fixture → 预期 finding / MITRE 技术 ID 集合 | 仅选防御性子 skill；不得执行攻击流程 |
| 仓库/脚本操作 | `initializ/forge` 的 `skills/code-agent` | https://github.com/initializ/forge | Apache-2.0（需复现前再次核验） | 微型 repo 中重命名/修改函数 → 预期 workspace diff | 新项目；只使用 Skill 目录而非完整联网 runtime |
| 数据处理 | `anthropics/skills` 的 `xlsx` | https://github.com/anthropics/skills | Proprietary/source-available（不能假定为开源） | CSV/XLSX fixture → 公式、sheet、结构化输出的精确校验 | 只有在许可证允许研究使用与必要分发时纳入；否则用开源替代 |

### 7.2 严格开源时的数据处理替代

如果 `xlsx` 的许可证不支持公开 benchmark 再分发，PoC 先使用 `initializ/forge` 的 code-agent 编写固定 CSV 转换脚本，或从候选池中另行筛选 MIT/Apache/BSD 的纯数据 Skill。此选择必须在收集/发布前进行许可证复核，不能仅依赖仓库页面摘要。

### 7.3 扩展候选池

| 候选 | URL | 已知许可证状态 | 建议用途 | 注意事项 |
|---|---|---|---|---|
| `anthropics/skills` / `mcp-builder` | https://github.com/anthropics/skills | Apache-2.0（子目录 license 需复核） | OpenAPI fixture → MCP tool schema | 生成代码需构建校验 |
| `anthropics/skills` / `webapp-testing` | https://github.com/anthropics/skills | Apache-2.0（子目录 license 需复核） | 固定 HTML → DOM assertions | 优先 DOM，不以截图 hash 为主 oracle |
| `anthropics/skills` / `skill-creator` | https://github.com/anthropics/skills | Apache-2.0（子目录 license 需复核） | Skill metadata / eval 文件结构 | 生成式质量只能做辅助评估 |
| `zhaoxuya520/reverse-skill` / `code-audit` | https://github.com/zhaoxuya520/reverse-skill | MIT（需复核） | 静态漏洞 finding | 工具依赖与中文说明需隔离处理 |
| `OthmanAdi/planning-with-files` | https://github.com/OthmanAdi/planning-with-files | MIT（需复核） | 计划文件 schema / 状态字段 | 自由 Markdown 的 oracle 较弱 |
| `K-Dense-AI/scientific-agent-skills` | https://github.com/K-Dense-AI/scientific-agent-skills | MIT（需复核） | 科学数据/文档子集 | 仓库和依赖较大，作为后续扩展 |

所有 URL、许可证与目录结构须在正式纳入 benchmark 前重新进行 commit-level 复核，记录 commit SHA、license 文件与许可证文本哈希。

## 8. 伦理、合规与安全

- 仅纳入允许研究使用、可明确再分发或可发布 evaluation metadata 的公开 Skill；
- 原始 Skill、patch、EvalPack、trace 和结果分别记录 license/provenance；
- 内部 Skill 不进入公开 benchmark；如需内部统计，仅用脱敏聚合且不作为主结论；
- 修复的可编辑范围限定在 Skill 文本、明确可编辑 reference/配置；首篇论文不把任意脚本重写作为主实验；
- 禁止扩展网络、shell、文件系统权限或外部副作用；
- current CATX isolation 不是 hostile-code sandbox，所有安全 case 均为无害模拟；
- trace 发布前对 token、URL userinfo、凭据、内部路径和用户数据做审计与脱敏。

## 9. 时间表（单人、保守估计）

| 周期 | 工作 | 交付与停止门 |
|---|---|---|
| 第 1–2 周 | 研究预注册、defect taxonomy、Skill 选择、许可证核验 | 3 个公开 Skill 与 EvalPack provenance 固定 |
| 第 3–4 周 | 构建 3 个 PoC Pack 与 20–30 个注入缺陷 | 确定性 grader、split、holdout 来源审查完成 |
| 第 5–6 周 | V0：多路径采集与分歧—缺陷验证 | 若无关联，停止扩大并修订命题 |
| 第 7–9 周 | V1/V2：预测、归因增强和修复消融 | B3 相对 B1 无净增益则调整方法而非扩样本 |
| 第 10–13 周 | 扩至 10 Skill、B0–B4、完整统计 | 公开 benchmark / artifact 草案 |
| 第 14–16 周 | 安全探针、复跑、论文与 artifact | 投稿版本 |

## 10. 论文叙事与 artifact

### 论文结构

1. Introduction：开放 Skill 的版本化、可评测与可修复性问题；
2. Background/Related Work：SkillsBench、OpenSkillEval、SWE-Skills-Bench、Skill-Use、HarnessFix、τ²-bench、AgentBoard；
3. Threat Model and Evaluation Contract：冻结 EvalPack、execution environment、边界；
4. Trajectory Divergence Fingerprint：定义与预测；
5. Divergence-Guided Regression-Safe Repair：evidence gate、受限 patch、回归门；
6. Benchmark and Experimental Protocol；
7. Results：RQ1/RQ2/RQ3、消融、成本与失败案例；
8. Threats/Ethics/Limitations；
9. Conclusion。

### 公开 artifact 最低内容

- 冻结 EvalPacks、case/fixture/oracle/schema 及 pack hash；
- Skill commit SHAs、许可证与来源记录；
- 多路径 trace 的脱敏版本或结构化 trace 摘要；
- `DivergenceReport`、failure cards、patch 与 candidate comparison；
- run context：模型、参数、runtime、tool registry、seed、预算；
- 一键复现实验脚本；
- benchmark card / data card / ethics statement。

## 11. 关键相关工作（起点，投稿前必须全文复核）

- SkillsBench: https://arxiv.org/abs/2602.12670
- SWE-Skills-Bench: https://arxiv.org/abs/2603.15401
- OpenSkillEval: https://arxiv.org/abs/2605.23657
- Skill-Use: https://arxiv.org/abs/2608.04828
- HarnessFix: https://arxiv.org/abs/2606.06324
- Evaluating Skills, Not Just Agents: https://arxiv.org/abs/2608.20614
- AgentBoard: https://arxiv.org/abs/2401.13178
- τ²-bench: https://github.com/sierra-research/tau2-bench

## 12. 下一步实验：PoC-0

**目的**：不修改生产 Skill，不做大规模线上评测，先验证核心因果链。

1. 许可证/commit 固定：从候选中选择 3 个公开 Skill，记录 license 和 SHA；
2. 每个 Skill 建 20 个 case：12 dev、4 validation、4 sealed holdout；
3. 仅用确定性 graders；
4. 生成 20–30 个具真值 defect variants，其中至少 1/3 是非 Skill 的环境/fixture/grader 故障；
5. 同一 `(skill, case, config)` 运行 K=3 条路径；
6. 运行 B0、B1、B3 的预算匹配比较；
7. 预注册 Go/No-Go：高分歧与真值 Skill defect 有实质关联，且 B3 不劣于 B1 并无 critical regression。

若 PoC-0 未满足条件，优先修订分歧定义和 attribution gate；不要直接扩大到 10 Skill 主实验。
