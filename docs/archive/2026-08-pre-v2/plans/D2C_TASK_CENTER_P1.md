# D2C 验证底座、任务中心与自动迭代事件 P1

## 1. 本轮目标

本轮把三个此前分散的能力接成同一条纵向链路：

1. D2C 候选代码在执行环境生成后，以精确 `candidate_commit` 交给独立浏览器验证环境；
2. 浏览器环境稳定采集 screenshot、DOM、console、network、performance 和视觉差异证据；
3. 所有 Skill 共用一个任务中心。评测设计可自动生成、复用或由用户自定义，但 UI 不要求用户理解 EvalPack；
4. 运行器自动写入版本化事件，Console 根据事件展示任务、轮次、case、会话日志、执行路径和决策；
5. 执行路径不按完全相同的工具序列过拟合，而按 required / recommended / alternative / forbidden 语义检查点评分。

## 2. 架构边界

```text
Skill / 用户标准 / 可选 Case
             │
             ▼
        EvaluationTask ───── evaluation: automatic | reused | custom
             │
             ├── Execution provider（CATX / 本地源码环境）
             │                  │ candidate_commit / artifact hash
             │                  ▼
             ├── Validation provider（Local Browser；未来 Remote Browser）
             │                  ├ screenshot + visual-diff
             │                  ├ DOM + console + network
             │                  └ receipt + environment fingerprint
             │
             └── events.jsonl ── Task Center / SSE / Optimization Tree
```

核心不依赖代码评审或 D2C 的字段。`TaskStore` 只管理通用任务与事件；`online_code_review` 和 `d2c` 是两个 adapter。

## 3. D2C 本地浏览器协议

版本化契约：

- Profile：`aceval.d2c-browser-profile/v1`
- Request：`aceval.d2c-validation-request/v1`
- Driver：`aceval.d2c-browser-driver/v1`
- Receipt：`aceval.d2c-validation-receipt/v1`

Profile 固定 Node、Chrome、viewport、DPR、locale、timezone、color scheme、稳定等待时间和超时。驱动使用显式 argv + JSON stdin/stdout，不执行自由 shell。

本地 provider 只允许访问 `127.0.0.1`、`localhost` 或 `::1` 的 HTTP 地址。执行环境与验证环境可以隔离；二者用不可变 commit 和视觉 Oracle SHA-256 关联。未来云端 provider 只需实现相同 Request/Receipt 协议。

每次成功或失败都会写 `receipt.json`，状态严格区分：

- `succeeded`：候选满足已配置断言；
- `candidate_failed`：页面可运行，但标题或视觉阈值不通过；
- `infrastructure_failed`：Node、Chrome、CDP、超时或驱动协议故障。

视觉 Oracle 同时冻结文件路径和 SHA-256。驱动输出 `diff_ratio`、不同像素数、最大通道差、尺寸检查和 `visual-diff.png`。

## 4. 任务中心数据

默认目录：

```text
.aceval/tasks/
  index.json
  <task-id>/
    task.json
    events.jsonl
```

`task.json` 保存 Skill 输入、目标、用户标准、自动/复用/自定义评测设计、环境绑定、状态和当前轮次。`events.jsonl` 是 append-only 事实源，事件包含全局递增 seq、时间、iteration、case、run 和 payload。

当前 runner 会自动产生：

- `task.created`
- `evaluation.design_generated` / `evaluation.design_attached`
- `baseline.started` / `baseline.completed`
- `candidate.started` / `candidate.completed`
- `case_run.started` / `case_run.completed` / `case_run.failed`
- `trace.captured`
- `path_conformance.completed`
- `iteration.planned`
- `decision.recorded`
- `task.converged` / `task.blocked`

代码评审的 `case_run.completed` 会索引 `outputs/session.json`、`outputs/attempts.json`、`grading.json` 和 `path_conformance.json`；D2C 会索引 receipt 及其内容寻址的浏览器产物。

## 5. 执行路径符合度

`ExecutionPathSpec` 支持四类步骤：

- `required`：缺失即失败；
- `recommended`：用于诊断，不作为硬失败；
- `alternative`：同组至少满足一个，避免绑定唯一工具；
- `forbidden`：一旦出现即失败。

步骤可声明前置顺序，通过 event type、tool name、文本片段和结构化字段匹配证据。如果 session trace 缺失或可能被截断，结果是 `not_evaluable`，且 `skill_patch_authorized=false`；基础设施证据不足不能被误判为 Skill 缺陷。

## 6. CLI

检查浏览器环境：

```bash
PYTHONPATH=src python3 -m aceval d2c check --profile browser-profile.json
```

运行 D2C case，并自动登记到任务：

```bash
PYTHONPATH=src python3 -m aceval d2c validate \
  --profile browser-profile.json \
  --request request.json \
  --output .aceval/runs/d2c-home-v1 \
  --task-id d2c-home \
  --iteration 1
```

创建自动评测任务（用户可提供 case）：

```bash
PYTHONPATH=src python3 -m aceval task create \
  --skill-name my-d2c-skill \
  --skill-source /path/to/skill \
  --scenario d2c \
  --goal '还原目标页面' \
  --standard '视觉差异不超过 1%' \
  --cases @cases.json
```

传入 `--evaluation-spec @custom.json` 即进入自定义模式；不传则自动生成 draft，仍保留后续编辑入口。

启动通用任务平台：

```bash
PYTHONPATH=src python3 -m aceval console serve \
  --task-root .aceval/tasks \
  --host 127.0.0.1 \
  --port 8766
```

代码评审在线运行可额外传入 `--task-id`、`--iteration` 和 `--execution-path`，完整 CATX 日志与路径评分会自动进入该任务。

## 7. 已验证与后续边界

本轮真实使用本机 Chrome 151 完成了交互、截图、DOM、console、network、performance、像素零差异、diff 图和任务事件写入，并再次用同一 D2C provider 验证任务中心页面无浏览器 console error。

下一阶段仍需补齐：

1. D2C 应用 server lifecycle（构建、启动、health、回收）adapter，而不是要求操作者提前启动 URL；
2. 多 viewport / 字体包 / 操作系统镜像矩阵与 Oracle 校准；
3. 布局/语义/可访问性 grader，避免把像素差异作为唯一质量信号；
4. Remote Browser provider 和源码 artifact 拉取协议；
5. Optimization Orchestrator 直接调用 `begin_iteration` / `record_decision`，自动形成分叉、回退和收敛树；
6. Console 内的任务创建、配置编辑、日志查看器和图片并排对比。

