# Skill 优化主链路与 EvalPack 瘦身迭代方案

## 1. 决策

Skill Doctor 的主产品对象是 Skill，不是 EvalPack。系统成功标准是生成一个经过可复现回归门禁的更优 Skill；EvalPack 只作为测试数据与评分契约的内容寻址快照存在。

本次迭代采用以下单向依赖：

```text
用户标准与预期
  -> Evaluation Compiler
  -> EvalSuite（薄 EvalPack）
  -> ExperimentPlan
  -> baseline / evidence / failure attribution
  -> Skill Optimizer
  -> candidate Skill
  -> validation / holdout
```

禁止从 Optimizer、candidate 或 validation/holdout 反向修改 EvalSuite。评测标准发生变化时必须创建新的 Suite 版本并重新运行 baseline。

## 2. 职责拆分

### 2.1 薄 EvalPack / EvalSuite

只保留：

- Subject 输入契约；
- Driver 和运行所需能力声明；
- Case、fixture、Oracle；
- Grader 与 split；
- 内容 hash 和校准状态。

不再承载：

- 优化模式和自然语言 Goal；
- Objective；
- Optimizer adapter；
- beam、轮数和候选预算；
- 可修改路径、最大新增行数；
- candidate 生成参数。

### 2.2 ExperimentPlan

独立保存一次 Skill 优化实验的控制参数：

- 绑定的 EvalSuite hash；
- repair / tune / auto；
- Goal 和 Objective；
- Optimizer adapter 与参数；
- allowed paths、候选数、轮数和补丁约束；
- 内容 hash 和来源。

ExperimentPlan 可以更换而不改变 EvalSuite。EvalSuite 可以被多个 Skill、模型和 ExperimentPlan 复用。

### 2.3 Skill Optimizer

只接收：

- frozen Subject；
- dev 的授权失败证据，或 dev 的 tuning measurement；
- ExperimentPlan 中的修改约束。

它不能读取或修改 Oracle、Grader、validation、holdout 和 EvalSuite 文件。

## 3. 兼容迁移

### Phase 1：外置 ExperimentPlan（本次实现）

1. 新增 `aceval.experiment-plan/v1` 契约和严格 JSON loader；
2. Orchestrator 优先使用显式 ExperimentPlan；
3. CLI 增加 `--experiment`，并自动发现 `<pack>.experiment.json`；
4. 新 Pack Builder 生成薄 Pack，同时在 Pack 目录旁生成 ExperimentPlan；
5. 旧 Pack 的 `optimizer_policy` 通过 legacy adapter 转成内存 ExperimentPlan；
6. 报告记录 ExperimentPlan hash 和来源；
7. Pack 校准、锁文件和 hash 不包含 ExperimentPlan。

### Phase 2：Evaluation Compiler 自动装配（基础链路已实现）

1. [x] 用户入口支持 `target + standards + expected result`，Cases 可选；
2. [x] 自动推断结果契约、执行特征、验证方法和风险；
3. [x] 基于标准、Cases、fixture 内容和画像签名精确复用冻结 EvalSuite；
4. [x] 无精确匹配时复用内部模板并生成最小 EvalSuite 草稿；
5. [x] 缺少可信 Oracle 时只请求用户补充预期结果；
6. [x] `--type` 降级为高级 override，默认 `auto`；
7. [ ] Browser、Office Artifact、Query Sandbox 和远端状态 Blueprint 的专用 Runtime/Grader；
8. [ ] 从历史 Session 自动挖掘并去敏的 Case 候选。

### Phase 3：线上闭环

1. CATX Session 接入 RuntimeAdapter；
2. Scenario -> Session -> ImportedRunBundle -> Grader Replay；
3. 线上 baseline 批量执行与重复采样；
4. 明确 candidate Skill 的版本绑定或独立 Agent 路由；
5. 完成线上 baseline/candidate/validation/holdout 门禁。

## 4. 文件与接口

```text
my-suite/                       # 薄 EvalPack
  pack.yaml
  scenarios/
  fixtures/
  oracles/
  schemas/

my-suite.experiment.json        # 独立 ExperimentPlan
```

CLI：

```bash
aceval optimize \
  --pack my-suite \
  --experiment my-suite.experiment.json \
  --subject my-skill \
  ...
```

若未传 `--experiment`：

1. 尝试自动发现相邻 ExperimentPlan；
2. 若 Pack 是 legacy，适配其 `optimizer_policy`；
3. 两者均不存在则安全停止，不允许从评测内容猜测优化策略。

## 5. 安全与不耦合约束

- ExperimentPlan 必须绑定 Suite hash，错配时 fail closed；
- Pack freeze 只冻结评测语义，不冻结实验策略；
- Optimizer 只可见 dev；
- validation/holdout 永不进入优化请求；
- 同一实验禁止修改 EvalSuite；
- 自动生成 Oracle 未经可信来源或确认不能成为 hard gate；
- legacy adapter 只用于迁移，不允许新 Builder 继续写 `optimizer_policy`；
- 报告必须同时记录 Suite hash、Subject hash、Runtime Profile 和 ExperimentPlan hash。

## 6. 验收标准

- 新生成 Pack 的 `pack.yaml` 不含 `optimizer_policy`、Goal 或 Objective；
- 相邻 ExperimentPlan 可驱动 `doctor/optimize`；
- 更换 ExperimentPlan 不改变 Pack hash；
- ExperimentPlan 与 Suite hash 不一致时优化前停止；
- 旧内置 Pack 和历史测试继续工作；
- optimizer adapter、候选预算和 patch constraints 全部来自 ExperimentPlan；
- Pack 仍可独立执行 `lint/test/run/compare`；
- 全量自动测试通过。

## 7. 后续产品指标

主指标：

- Skill hard-pass 提升；
- validation/holdout 回归率；
- accepted candidate 比例；
- 单个成功优化的时间和成本；
- 真实用户需要补充标准的次数。

辅助指标：

- EvalSuite 复用率；
- 自动装配成功率；
- Oracle 人工确认率；
- evaluator false-positive / false-negative；
- time-to-first-valid-run。

EvalSuite 生成数量和复杂度不是产品成功指标。
