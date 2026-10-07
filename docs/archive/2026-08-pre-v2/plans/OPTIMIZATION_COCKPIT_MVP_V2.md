# Skill Optimization Cockpit MVP V2

> 状态：实现基线  
> 日期：2026-08-24  
> 目标：把首次代码评审 Skill MVP 的离线结果页升级为可解释、可追踪、会收敛的本地优化驾驶舱。

## 1. 首次 MVP 暴露的问题

首次线上闭环已经验证了双仓库绑定、完整 CATX 日志、确定性评分、Skill 修改和复评，但产品体验和决策可信度仍有五个明显缺口。

1. **只看到结果，看不到优化因果链。** 静态审阅页能展示 Case 输出，却不能说明为什么选择这些 Case、为什么修改某条规则、修改后为何保留或拒绝。
2. **总分掩盖维度差异。** 候选断言均值从 40.0% 降到 37.8%，同时正式通过从 0/9 增到 2/9、Token 降低 30.1%。单一总分无法表达“内容准确性改善、格式稳定性下降”的真实状态。
3. **单次运行被过度解读。** 同一 RN Case 在相邻版本中出现满分和格式失败，说明存在显著随机波动；一次运行不能支持统计显著性结论。
4. **Skill 失败与平台行为仍需分层。** 最主要残留问题是终态消息在正确 JSON 前添加分析正文。界面必须把领域判断、格式遵从、绑定、环境和成本拆开，不允许所有失败都触发 Skill 修改。
5. **配置和过程过度依赖 CLI。** Profile、仓库挂载、commit、运行次数、预算和晋级规则分散在 JSON、环境变量和命令参数中，新用户很难建立完整心智模型。

## 2. V2 产品决策

先实现本地优先的 Web Console，保留 CLI 和 Python Kernel 作为唯一执行与评分来源。

```text
Browser UI
   ↓ localhost API + SSE
Optimization Graph Compiler
   ↓ read only
workspace / benchmark / grading / binding / session trace / profile metadata
   ↓
existing EvalOps Kernel + CATX runner
```

本轮不引入 Node、React 构建链、数据库或桌面壳。前端使用随 Python 包发布的原生 HTML/CSS/JavaScript，后端使用 Python 标准库 HTTP Server。验证信息架构和动态树价值后，再决定是否迁移到 React/Tauri。

## 3. 用户主流程

### 3.1 输入确认

页面首先展示：

- Skill 名称、来源、ref 和不可变 commit；
- 优化目标；
- 硬门禁、质量目标、成本目标；
- 当前运行环境与仓库挂载状态；
- 重复次数、预算和停止规则。

EvalPack、Oracle 和底层 Profile 默认渐进披露。用户先看到“系统将如何判断成功”，高级用户再查看原始契约。

### 3.2 自动评测设计

展示系统自动选择或生成的：

- 技术栈；
- Case 分组和来源；
- 缺陷 Case 与 clean 控制 Case；
- 每项评分维度；
- Oracle/Grader 是否冻结；
- commit binding 要求。

### 3.3 Baseline 与迭代树

每个节点必须包含：

1. 失败证据；
2. 根因假设；
3. 修改计划；
4. 实际 commit 和 diff 范围；
5. 分维度效果；
6. 晋级、保留、拒绝、剪枝或待复验决策；
7. 下一步。

主路径始终指向当前最佳版本；拒绝分支保留证据但视觉降级。树不能无限增长：相同方向连续无改善、达到预算、硬门禁失败或统计证据不足时，必须显式停止或进入复验节点。

### 3.4 最终交付

最终页回答：

- 当前最佳版本是谁；
- 候选是否可发布；
- 哪些修改被接受或拒绝；
- 相对原版的质量、稳定性和成本变化；
- 仍未解决的问题；
- 结论置信度；
- 下一轮需要新增的 Case 或平台改动。

## 4. 优化图契约

新增 `aceval.optimization-graph/v1`，由工作区产物确定性编译，不复制评分逻辑。

```json
{
  "api_version": "aceval.optimization-graph/v1",
  "run": {"id": "frontend-code-reviewer", "status": "needs_revalidation"},
  "input": {"skill": {}, "goal": "...", "success_criteria": []},
  "evaluation_design": {"cases": [], "dimensions": []},
  "nodes": [],
  "edges": [],
  "dimension_summary": [],
  "convergence": {
    "current_best": "v0",
    "decision": "do_not_promote",
    "blockers": [],
    "next_steps": []
  },
  "configuration": {"profile": {}, "repositories": []}
}
```

数据来源：

| UI 信息 | 权威来源 |
|---|---|
| Case 和断言 | `eval_metadata.json` |
| Case 状态和评分 | `grading.json` |
| Token 和耗时 | `timing.json` |
| 精确 commit | `binding.json` |
| 版本输赢 | `history.json` |
| 假设和修改计划 | 可选 `optimization-plan.json`，缺失时保守推断 |
| 完整证据 | `outputs/session.json`、`final_output.txt` |
| 环境配置 | Profile 白名单字段，永不读取或返回凭据值 |

## 5. Console 界面

### 5.1 核心区域

- **Run Header**：运行状态、当前最佳版本、置信度和是否可发布；
- **Phase Rail**：输入、评测设计、Baseline、迭代、收敛五阶段；
- **Optimization Tree**：版本谱系、假设、指标和决策；
- **Dimension Board**：格式、召回、源码引用、误报、绑定、正式通过和成本；
- **Case Matrix**：技术栈、Case 类型、baseline/candidate 结果和证据入口；
- **Node Inspector**：为什么改、改什么、效果、决策、下一步和文件引用；
- **Configuration**：Agent、environment、仓库和 mount 的脱敏摘要；
- **Evidence**：原始 JSON、日志和报告路径。

### 5.2 实时更新

`console serve` 默认监听 `127.0.0.1`。浏览器连接 `/api/events`：

- 初次连接发送 `graph.snapshot`；
- 工作区文件指纹变化后重新编译并发送新 snapshot；
- Case 只有 `request.json` 时显示 running；
- 出现 `grading.json` 后更新维度和节点；
- JSON 正在写入或暂时不完整时保留上一份有效快照。

WebSocket 不是 MVP 必需。

## 6. 安全边界

- 只允许 loopback host；
- UI 不接受任意 Shell；
- Console 只读，不修改 Skill、Profile 或评测结果；
- 不加载 credentials file；
- API 只返回 Profile 白名单字段；
- token、authorization、api key 等键和值统一脱敏；
- 动态内容只通过 DOM `textContent` 渲染；
- 静态服务进行路径归一化，禁止目录穿越；
- 页面显式展示单次运行限制和非晋级状态。

## 7. CLI

```bash
aceval console build \
  --workspace .aceval/frontend-code-reviewer-workspace \
  --plan .aceval/frontend-code-reviewer-workspace/optimization-plan.json \
  --profile .aceval/catx-profile.local.json \
  --output .aceval/frontend-code-reviewer-workspace/console

aceval console serve \
  --workspace .aceval/frontend-code-reviewer-workspace \
  --plan .aceval/frontend-code-reviewer-workspace/optimization-plan.json \
  --profile .aceval/catx-profile.local.json \
  --port 8765
```

`build` 生成可离线打开的页面和冻结 graph；`serve` 提供动态 graph 和 SSE。

## 8. MVP 验收标准

- 用户在 30 秒内能回答候选为何没有晋级；
- 用户能看到输入、成功标准、自动 Case 和评分路径；
- 每轮能看到假设、修改、效果、决策和下一步；
- 树明确标出 current best、拒绝分支和收敛状态；
- 维度面板不会用总分掩盖格式或绑定失败；
- 单次运行必须显示低置信度提示；
- 配置页面不泄露任何凭据；
- 静态 Console 可离线打开；
- 动态 Console 能通过 SSE 感知工作区更新；
- CLI 和 UI 对同一工作区使用同一 graph compiler；
- 全量测试通过。

## 9. 后续优先级

1. P1：新建任务向导、Case/标准确认和预算设置；
2. P1：重复运行、flake、置信区间和自动复验节点；
3. P1：结构化失败聚类与 Patch Authorization；
4. P2：Diff、Trace 深度查看和反馈回写；
5. P2：Tauri 桌面包装与系统钥匙串；
6. P3：云端协作、多用户和远程调度。
