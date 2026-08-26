# 代码评审 Skill：P0 本地环境与接入手册

## 1. P0 交付边界

P0 先把“输入不可变、环境可验证、结果可确定评分”跑通，不把 EvalPack
生成与 Skill 优化耦合。默认资产由系统生成；用户只需要选择被测 Skill，
提供验收标准，必要时补充 Case。高级用户仍可替换 fixture、Oracle 和评分参数。

当前已实现：

- `aceval code-review init-lab`：生成一个统一 Git Fixture Lab，覆盖 TypeScript Web、
  React Native、微信小程序 JS、Java 后端四类业务栈；每个栈有一个 `base/<stack>`
  分支、2 个缺陷 Case 分支和 1 个 clean 误报控制 Case 分支。每个 Case 有固定
  base/head commit、Oracle、Prompt 和标准 `evals/evals.json`；
- `code_review_findings_v1@1.0.0`：按路径、行区间、类别和严重级别匹配，计算
  precision、recall、critical recall、误报、重复项和无效源码引用；
- `EnvironmentBlueprint`、`CandidateBundle`、`ValidationRequest/Receipt` 和
  `RunEnvelope`：所有关键输入内容寻址，发现内容漂移即拒绝运行；
- `LocalDockerProvider`：每次创建临时容器，禁止网络，候选仓库只读挂载，限制
  CPU、内存、PID、超时和临时盘，结束后强制清理；候选失败与基础设施失败分开；
- `repository.verify/v1`：在容器内核对 Git 仓库、base/head、祖先关系、当前
  HEAD 和变更文件；
- CATX Binding Contract：一个新会话同时挂载候选 Skill 仓库和 Fixture Lab，Agent
  执行前核对两个仓库的 commit，并把只读核对结果写入完整事件日志；无法证明时 fail closed。

CATX 创建会话的多 repository `resources` 字段已经接入，URL、PAT 环境变量名和
唯一 `mount_path` 均由 Profile 配置。候选 Skill 与 Fixture 可复用同一 PAT。

## 2. P0 数据流

```text
生成/选择评测 Case
  -> 冻结 Skill hash + repository tree + base/head
  -> 本地 repository.verify/v1 隔离预检
  -> CATX 创建全新会话（Skill repository + Fixture repository）
  -> 核对 Skill commit + Fixture base/head
  -> 从挂载的候选 SKILL.md 执行评审
  -> 回读绑定证据
  -> 获取完整事件日志和最终 JSON findings
  -> code_review_findings_v1 确定性评分
  -> Failure Attribution
  -> 只修改 Skill 候选
  -> dev / validation / holdout 门禁
```

本地验证环境和 CATX 执行环境可以隔离。二者通过不可变 Skill hash、仓库 tree
hash 和 commit 对关联，不能通过可变分支名默认为“同一份代码”。

## 3. 创建本地评测仓库

从项目根目录执行：

```bash
PYTHONPATH=src python3 -m aceval code-review init-lab \
  --output .aceval/code-review-lab
```

默认生成全部四类栈。也可以重复使用 `--stack` 只选择所需范围，例如：

```bash
PYTHONPATH=src python3 -m aceval code-review init-lab \
  --output .aceval/java-review-lab \
  --stack java-backend
```

可选值为 `typescript-web`、`react-native`、`wechat-miniprogram` 和
`java-backend`。

生成结果：

```text
.aceval/code-review-lab/
  lab.json
  findings.schema.json
  evals/evals.json
  repository/.git
  cases/<case-id>/
    case.json
    oracle.json
```

统一仓库的分支结构：

```text
master                       -> 同时包含四类技术栈基础代码
base/typescript-web
base/react-native
base/wechat-miniprogram
base/java-backend
case/ts-web-dom-xss
case/rn-listener-leak
case/mini-client-auth-trust
case/java-sql-injection
...其余 Case 分支
```

`master` 的 `fixtures/` 目录默认就能看到全部技术栈。推送给 CATX 时仍必须推送全部
分支，例如 `git push origin --all`，不能只推 `master`，
也不能 squash；评测流程根据 Skill 技术栈选择 Case，并使用 Case 元数据中的固定
`base_ref/head_ref` 与 `base_commit/head_commit`。

可以先查看系统基于 Skill 内容的自动选择结果：

```bash
PYTHONPATH=src python3 -m aceval code-review select-cases \
  --lab .aceval/code-review-lab \
  --skill /path/to/reviewer-skill
```

明确知道技术栈时，可以用一个或多个 `--stack` 覆盖自动推断。未识别出具体栈的
通用代码评审 Skill 默认选择全部 Case，避免因错误分类漏测。

每个技术栈都有一个 `*-clean-*` Case，预期零 finding，用来防止以“多报问题”
换取召回率。合成 Case 默认只进入 dev/validation；sealed holdout 应来自独立来源
或人工维护，不能把同源自动生成数据伪装成未知测试。

## 4. 构建并验证本地隔离环境

先从 `lab.json` 选择一个 Case 的 base/head，然后构建验证镜像和绑定该 Case 的
Blueprint：

```bash
PYTHONPATH=src python3 -m aceval environment build \
  --base-commit BASE_COMMIT \
  --head-commit HEAD_COMMIT \
  --output .aceval/code-review-runtime/repository-blueprint.json

PYTHONPATH=src python3 -m aceval environment check \
  --blueprint .aceval/code-review-runtime/repository-blueprint.json
```

构建结束后 Blueprint 保存实际 Docker image ID，而不是可变 tag。Provider 不会
在评测阶段自动 pull 镜像。

把 Skill hash 与仓库内容冻结为 CandidateBundle：

```bash
PYTHONPATH=src python3 -m aceval environment bundle \
  --repository .aceval/code-review-lab/repository \
  --subject-hash sha256:SKILL_CONTENT_SHA256 \
  --producer-run-id prepare-001 \
  --base-commit BASE_COMMIT \
  --head-commit HEAD_COMMIT \
  --output .aceval/code-review-runtime/java-sql-injection.candidate.json
```

执行一次完整生命周期：

```bash
PYTHONPATH=src python3 -m aceval environment validate \
  --blueprint .aceval/code-review-runtime/repository-blueprint.json \
  --candidate .aceval/code-review-runtime/java-sql-injection.candidate.json \
  --suite-hash sha256:SUITE_SHA256 \
  --scenario java-sql-injection \
  --run-id validation-001 \
  --output .aceval/code-review-runtime/java-sql-injection.receipt.json
```

只有 Receipt 为 `succeeded` 才说明本地验证环境 ready；`candidate_failed` 表示
仓库契约不成立，`infrastructure_failed` 表示 Docker、镜像或生命周期失败，后者
不能作为优化 Skill 的证据。

## 5. 单独校验一次 Agent 输出

Agent 输出必须是严格 JSON：

```json
{
  "findings": [
    {
      "path": "fixtures/java-backend/src/main/java/com/example/orders/OrderService.java",
      "line": 16,
      "category": "security",
      "severity": "critical",
      "explanation": "..."
    }
  ]
}
```

```bash
PYTHONPATH=src python3 -m aceval code-review grade \
  --repository .aceval/code-review-lab/repository \
  --findings @agent-findings.json \
  --oracle @.aceval/code-review-lab/cases/java-sql-injection/oracle.json
```

## 6. 运行 CATX 线上评测与对比

Profile 同时配置候选 Skill 和 Fixture Lab 两个 repository。运行时传入完整 40 位
Skill commit；每个 Case 的 base/head commit 来自冻结的 `case.json`。Agent 必须在
工具日志中回显三个 commit，缺一即绑定断言失败。

```bash
PYTHONPATH=src python3 -m aceval code-review online-run \
  --profile .aceval/catx-profile.local.json \
  --lab .aceval/code-review-lab \
  --skill-ref feature/test-and-optimize \
  --skill-commit FULL_40_CHAR_COMMIT \
  --workspace .aceval/frontend-code-reviewer-workspace/iteration-1 \
  --configuration with_skill
```

原始 Skill 使用 `--configuration old_skill` 跑入同一 iteration，或把已经冻结的
baseline run 复制进相同 Case 目录。两组齐全后生成对比：

```bash
PYTHONPATH=src python3 -m aceval code-review online-report \
  --workspace .aceval/frontend-code-reviewer-workspace/iteration-1 \
  --skill-name frontend-code-reviewer \
  --skill-path .aceval/candidate-skills/frontend-code-reviewer/src
```

产物包含 `benchmark.json` 和 `benchmark.md`。Pass rate delta 始终按候选减基线；
token 使用 CATX `timing.json` 的真实 usage。默认一次运行只用于方向判断，需要做晋级
决策时应增加重复运行并报告随机方差。

适配器不会修改 Profile 管理的 `agent`、`environment_id`、`vault_ids` 和 title；
回读证据缺失或不匹配时，会话日志仅用于基础设施排障，不作为 Skill 效果证据。

## 7. Ready 判定

开始真实 Skill 执行前必须同时满足：

- 全量自动测试通过；
- Docker daemon 可用，目标 image ID 已存在；
- `environment check` 返回 `ready: true`；
- 至少一个 fixture 的 ValidationReceipt 为 `succeeded`；
- CATX Adapter 能创建双仓库会话，并从完整工具日志回读两个仓库的 commit 证据；
- 用户提供候选 Skill Git ref、完整 commit（或可计算快照 hash）和验收标准。

第五项由会话开头的只读 Git 核对完成；commit 不匹配的 Run 不进入评分。
