# Skill Doctor Console 可视化操作与结果平台方案

> 状态：D40 方案基线  
> 日期：2026-08-18  
> 结论：建设本地单用户 EvalOps Console；暂不建设完整在线 SaaS。

## 1. 为什么需要 Console

可视化不是单纯的展示包装。当前系统中的以下关系难以通过一条 CLI 输出完整理解：

- `draft -> calibrating -> frozen` Pack 生命周期；
- baseline 自动选择 repair/tune；
- 多轮 candidate lineage；
- dev、validation、holdout Gate；
- hard regression 与 Objective improvement；
- Case 级 Grader evidence；
- 工具调用 Trace；
- Token、成本、耗时和工具调用变化；
- 公司 Session 的 observation completeness。

Console 的核心价值是降低操作门槛、提高评测契约审批质量，并让“为什么接受/拒绝候选”可解释。

## 2. 产品定位与边界

### 2.1 定位

```text
Skill Doctor Console / EvalOps Studio
```

- localhost 默认启动；
- 单用户；
- 面向 Skill 作者、EvalPack 作者和 Agent 研发人员；
- 操作现有 Kernel，而不是创建另一套评分逻辑；
- 读取和生成现有版本化报告；
- 后续可接公司 Agent Runtime 和 Session Replay。

### 2.2 当前不做

- 用户注册、组织和 RBAC；
- 多租户隔离；
- 在线计费；
- 分布式 Worker；
- 浏览器内任意 Shell；
- 在 UI 中保存 API token；
- Marketplace；
- 将本地 Console 描述为生产级托管平台。

## 3. 用户与主要任务

| 用户 | 主要任务 |
|---|---|
| Skill 作者 | 提交 Skill、少量 Case 和 Goal，得到修复/优化候选 |
| EvalPack 作者 | 检查 Oracle、Grader、split 和 Objective，完成校准/冻结 |
| Agent 研发 | 导入 Session、查看 Trace、分析失败归因和性能 |
| 评审/面试官 | 快速理解实验设计、候选变化、回归门禁和可信边界 |

## 4. 信息架构

### 4.1 首页 / 项目列表

展示：

- 最近实验；
- Pack 状态；
- accepted/rejected；
- repair/tune；
- Objective improvement；
- Runtime 和 simulated 标记；
- 快速创建实验、导入 Session。

### 4.2 新建实验向导

步骤：

1. 选择或上传 Skill；
2. 选择已有 Pack，或上传 Cases；
3. 选择任务类型；
4. 输入 Goal；
5. 选择 Runtime/Profile；
6. 设置预算；
7. 预览实验契约；
8. 启动。

页面必须提前展示：

- 当前 Pack 是否允许 optimize；
- Goal 推断出的 Objective；
- 是否会进入 `auto`；
- 缺失的 Oracle/telemetry；
- Runtime capability 风险；
- simulation/real execution 区分。

### 4.3 EvalPack 校准页

顶部摘要：

```text
Status             CALIBRATING
Cases              8
Dev/Validation     5 / 2
Holdout            1
Oracle completeness 87.5%
Hard Graders       4
Objective          trace_count.tool_call / minimize
Freeze blockers    1
```

Case 表格：

| Case | Split | Fixture | Oracle | Hard Grader | Warning |
|---|---|---|---|---|---|

用户可进入 Case 详情检查 Prompt、fixture、expected output/records、Grader 参数和 split。只有 blocker 清零并显式确认后，才能冻结 Pack。

### 4.4 实验实时进度页

```text
✓ Verify Pack lock
✓ Baseline dev
✓ Auto selected: TUNE
✓ Candidate round 1
✗ Candidate round 2: hard regression
✓ Validation paired gate
● Holdout running
```

需要展示：

- 当前阶段；
- 已运行/总 Case 数；
- 累计 Token、费用、工具调用；
- 当前候选；
- 预算剩余；
- cancel 状态；
- 基础设施错误与质量失败的区别。

### 4.5 结果总览

第一屏只回答：

1. 是否接受候选；
2. 选择了 repair 还是 tune；
3. 改了什么；
4. 是否有回退；
5. 效果和成本如何变化。

推荐组件：

- accepted/rejected Hero；
- Gate 流程图；
- KPI 卡片；
- baseline/candidate 指标图；
- Case pass matrix；
- 限制和可信度提示；
- selected candidate 下载/打开按钮。

### 4.6 Case 与 Trace 详情

左右对照：

```text
Baseline output       Candidate output
Baseline grades       Candidate grades
Expected result       Actual result
Baseline usage        Candidate usage
```

Trace 使用时间线：

```text
Model call
  └─ read_file(input.csv)
       └─ tool result
Model call
  └─ write_file(summary.json)
Final answer
```

### 4.7 Skill Diff 与候选谱系

展示：

- 原始 `SKILL.md`；
- Candidate diff；
- rationale；
- parent candidate；
- round；
- dev/validation 结果；
- rejection reason。

候选树是 P1。MVP 可先使用按 round 分组的表格。

### 4.8 Session Import / Replay

展示：

- Profile；
- Session ID；
- output；
- Trace；
- usage；
- completeness；
- 哪些 Grader 可以 Replay；
- 哪些因缺失 artifact/workspace 无法重评分。

## 5. 技术架构

### 5.1 目标架构

```text
                    ┌──────────── CLI
                    │
User ── Web UI ── Application Service ── EvalOps Kernel
                    │                       │
                    ├─ Run Repository       ├─ Pack Builder
                    ├─ Event Stream         ├─ Orchestrator
                    └─ Profile Store        └─ Session Connectors
```

CLI 和 Web 都是 Application Service 的客户端。前端不得直接实现 Gate、Objective 或 Pack freeze 逻辑。

### 5.2 必要后端重构

当前部分操作仍位于 `argparse` handler 中。Console 开发前应抽取：

```python
class EvalOpsApplicationService:
    def generate_pack(...): ...
    def begin_pack_calibration(...): ...
    def freeze_pack(...): ...
    def start_experiment(...): ...
    def get_experiment(...): ...
    def cancel_experiment(...): ...
    def import_session(...): ...
    def replay_session(...): ...
```

CLI handler 只负责参数解析、调用 Service 和打印结果。

### 5.3 Run Repository

第一版继续使用文件系统：

```text
.aceval/
  index.json
  packs/
  runs/
    <experiment-id>/
      experiment.json
      events.jsonl
      report.json
      report.detailed.json
      report.html
      selected-candidate/
```

引入 SQLite 前先验证查询需求。文件索引无法满足并发和历史查询时再迁移。

### 5.4 实验事件协议

建议新增版本化事件：

```json
{
  "api_version": "aceval.event/v1",
  "experiment_id": "exp-123",
  "seq": 12,
  "type": "gate.completed",
  "timestamp": "2026-08-18T12:00:00Z",
  "payload": {
    "gate": "validation",
    "status": "pass"
  }
}
```

最小事件集合：

- `experiment.started/completed/failed/cancelled`；
- `pack.generated/calibrating/frozen`；
- `baseline.started/completed`；
- `mode.selected`；
- `candidate.proposed/evaluated/rejected/promoted`；
- `scenario.started/completed`；
- `gate.started/completed`；
- `budget.updated/exhausted`。

浏览器通过 SSE 获取事件。当前任务不需要双向高频通信，WebSocket 不是必需。

### 5.5 API 草案

```text
POST /api/packs/generate
POST /api/packs/{pack_id}/calibrate
POST /api/packs/{pack_id}/freeze
GET  /api/packs/{pack_id}

POST /api/experiments
GET  /api/experiments
GET  /api/experiments/{experiment_id}
GET  /api/experiments/{experiment_id}/events
POST /api/experiments/{experiment_id}/cancel

POST /api/sessions/import
POST /api/sessions/fetch
GET  /api/sessions/{session_id}
POST /api/sessions/{session_id}/replay
```

## 6. 前端技术选择

### 6.1 D20：静态 HTML

目标：2–4 天内完成高收益结果展示。

- 从 `report.json` 生成单文件 HTML；
- 不需要服务端；
- 支持 KPI、Gate、Case matrix、Objective、diff 和 limitations；
- 可作为黑客松离线演示备份。

建议新增：

```bash
aceval report html RUN_DIR
```

### 6.2 D40：本地 Web Console

推荐：

- Backend：FastAPI；
- Event：SSE；
- Frontend：React + Vite；
- Chart：ECharts 或 Recharts；
- Diff：Monaco Diff Editor；
- Packaging：`aceval[ui]` 可选依赖；
- 启动：`aceval ui --data-root .aceval`。

若时间不足，可用 FastAPI + Jinja2/HTMX 替代 React，优先保证数据正确和操作闭环。

## 7. 安全边界

- 默认只监听 `127.0.0.1`；
- 不允许 UI 输入任意 Shell 字符串并直接执行；
- 模型命令必须来自已确认的 Runtime Profile；
- API token 继续只从环境变量读取；
- Profile API 永不返回 secret；
- 所有路径仍执行 Pack/Subject 现有安全校验；
- 输出、Trace、Skill diff 和模型内容必须 HTML escape；
- 下载 artifact 时验证相对路径和内容类型；
- cancel 是协作式终止，不宣称可中断已发送的供应商调用；
- UI 必须明显标记 FakeRuntime/simulated；
- UI 不得隐藏 `not_measured`、`not_evaluable` 和 limitations。

## 8. 分阶段交付

### Phase 0：HTML Result Viewer

- [ ] `report.detailed.json`；
- [ ] 单文件 HTML；
- [ ] Gate 流程；
- [ ] KPI；
- [ ] Case matrix；
- [ ] Skill diff；
- [ ] simulation 与 limitations 提示。

### Phase 1：Read-only Console

- [ ] Run index；
- [ ] 历史实验列表；
- [ ] 实验详情；
- [ ] Trace 时间线；
- [ ] Candidate 列表；
- [ ] Session viewer。

### Phase 2：Operational Console

- [ ] 新建实验；
- [ ] Pack 生成/校准/冻结；
- [ ] 实时 SSE；
- [ ] cancel；
- [ ] Runtime/Profile 选择；
- [ ] Session fetch/import/replay。

### Phase 3：Analysis Console

- [ ] 重复运行趋势；
- [ ] flake；
- [ ] p50/p95 与置信区间；
- [ ] Pack Quality Report；
- [ ] Judge agreement；
- [ ] output-only vs trace-aware 消融。

## 9. MVP 验收标准

### HTML Viewer

- [ ] 在无网络环境打开；
- [ ] repair/tune 报告都能渲染；
- [ ] 缺失指标显示为 `not_measured`，不显示为零；
- [ ] FakeRuntime 显著标记；
- [ ] 用户能在 30 秒内回答候选为何接受/拒绝。

### Local Console

- [ ] 新用户不手写 CLI 即可创建一个受支持模板实验；
- [ ] Pack 未冻结时不能启动 optimize；
- [ ] 未知类型必须停在 calibration；
- [ ] 页面刷新后实验状态不丢失；
- [ ] 可查看 Case evidence、Trace、diff 和 Objective；
- [ ] 公司 Session 缺失 telemetry 时正确显示 completeness；
- [ ] UI 与 CLI 对同一实验产生相同 Kernel 结果。

## 10. 风险与控制

| 风险 | 控制 |
|---|---|
| UI 复制 Kernel 语义 | 所有操作通过 Application Service |
| 漂亮页面掩盖模拟结果 | simulation 固定显著标识 |
| 长任务无反馈 | 版本化事件 + SSE |
| 模型输出导致 XSS | 全量转义、严格 CSP |
| 任意命令执行 | Profile allowlist，不接收自由 Shell |
| 引入数据库拖慢进度 | 先用文件索引，按需求迁移 |
| 前端工作挤压真实实验 | 真实 repair/tune 报告作为 UI 开工门槛 |
| 信息过载 | Verdict -> Gate -> Case/Trace 三级渐进披露 |

## 11. 最终决策

可视化平台适合本项目，并被纳入 D40 P0/P1 之间的产品化能力。实施顺序为：

```text
真实实验证据
  -> 静态 HTML
  -> Application Service
  -> Read-only Console
  -> Operational Console
  -> Replay / Statistics / Pack Quality
```

完整在线平台、多用户能力和远程调度继续延后。
