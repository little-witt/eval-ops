# FORGE / ACEval 产品可用性与发布验证

> 日期：2026-08-26  
> 适用范围：单用户、本地优先；不包含多租户、服务端部署和运维平台  
> 当前级别：可实际使用的 macOS arm64 本地产品构建；公开分发签名/公证未纳入本阶段

## 1. 交付物

- 桌面应用：`desktop/dist/mac-arm64/FORGE Skill Evolution Studio.app`
- 桌面源代码：`desktop/electron`、`desktop/renderer`
- 自包含 Kernel 构建：`desktop/build/forge-kernel/forge-kernel`
- Python Kernel：`src/aceval`
- 当前架构与核方案：本目录中的 `FINAL_SYSTEM_ARCHITECTURE.md`、`EVALUATION_SELF_ITERATION_KERNEL_V2.md`

构建产物被 `.gitignore` 排除，源码、锁文件、构建脚本和应用图标进入版本管理。

## 2. 已实现的产品闭环

| 环节 | 当前实现 | 关键证据/门禁 |
|---|---|---|
| 配置 | Skill/代码仓库、分支、PAT 引用、本地模型桥、远程 Agent Profile、系统安全存储 | Renderer 不接触明文密钥；环境变量名持久化 |
| 输入 | 无 Case、目标模式、用户 Case + 预期、自定义 EvalPack、修复/优化/扩展/探索/新建 | 创建后进入真实任务详情 |
| 评测设计 | EvalPack 自动生成/复用/自定义，Case 补充，语义路径 | EvalPack 与每个 Case/Path 独立事件 |
| 线上执行 | 多会话 fan-out、双仓库、完整 commit SHA、恢复式轮询和全日志回收 | 一个 Case/Path 对应一个 Session artifact |
| 判定 | Evidence Validity、Attempt、Case Aggregate、Candidate Comparison | Outcome/Procedure/Grounding/Runtime/Efficiency、pass@k/pass^k |
| 诊断 | 失败事实、根因假设、问题簇、冲突、Proposal、精确 target scope | 硬事实覆盖模型语义意见 |
| 优化 | 用户选择 Proposal 并补充意见，多文件候选，静态/配置验证 | 未批准不生成或发布修改 |
| 发布与收敛 | Champion/Challenger、不可变 SHA、成对门禁、稳定通过保护、预算/循环/耐心值 | 通过则晋升；拒绝则 auditable revert；安全停止 |
| D2C | 预览窗口、插件白名单、独立 Chrome Worker、设计稿 hash、DOM/网络/控制台/截图/diff | 自动判定 Worker 禁用插件和临时 Profile |
| Agent 复用 | `EvaluationSubject`、`SkillSubject`、`AgentSubject` | 核心 Verdict/Diagnosis/Convergence 与对象类型解耦 |

## 3. 独立分发验证

最终 `.app` 的 Electron 主程序和内置 Kernel 均为 arm64。应用没有散装携带 `src/`，而是携带约 11MB 的 PyInstaller onedir Kernel 与内置 D2C driver。启动验证显式把
`ACEVAL_PYTHON` 指向不存在路径，应用仍返回：

```json
{"ok":true,"renderer":"loaded"}
```

这证明打包版不依赖系统 Python。D2C 使用 Electron 内置 Node 22，不依赖 GUI PATH 中的系统 Node。

## 4. 真实浏览器证据

使用最终 `.app` 的 Electron executable 以 Node 模式驱动本机 Chrome，连续执行 Reference 和严格零阈值 Comparison：

- 两次状态均为 `succeeded`；
- 点击动作、页面标题、最终 URL 均通过；
- console errors 为 0；
- DOM、network、console、screenshot 和 driver result 均有 SHA-256；
- Comparison 的 `diff_ratio=0`、`differing_pixels=0`、`max_channel_delta=0`。

证据：

- [`reference-receipt.json`](../verification/d2c-packaged-node/reference-receipt.json)
- [`comparison-receipt.json`](../verification/d2c-packaged-node/comparison-receipt.json)
- [`reference-screenshot.png`](../verification/d2c-packaged-node/reference-screenshot.png)
- [`visual-diff.png`](../verification/d2c-packaged-node/visual-diff.png)

## 5. 安全与恢复

- Electron：`contextIsolation=true`、`nodeIntegration=false`、sandbox、CSP、IPC allowlist。
- 本地服务：stdio JSON-RPC，不开放 localhost API 端口；长任务使用后台 Worker 和持久化事件。
- 文件：产物读取必须位于任务根或用户显式选择路径，限制常规文件和大小。
- 插件：解压目录、manifest、权限/host allowlist、内容 hash、独立预览 Profile。
- Git：工作树/分支/origin/manifest/hash 校验，显式 commit/push，不使用 reset；失败前恢复原文件。
- 远程批次：Session 创建前检查 Prompt 预算；中断在未知创建状态时 fail closed，避免重复 Session。
- 退出：应用退出时检查 quitting/window/webContents 生命周期，已复测修复 `Object has been destroyed` 竞态。

## 6. 验证矩阵

发布前执行：

```bash
PYTHONPATH=src python3 -m pytest -q
node desktop/tests/renderer-contract.test.mjs
node desktop/tests/ui.e2e.cjs
```

并执行：Kernel arm64 构建、Electron arm64 dir 构建、打包应用启动/退出、内置 Node 真实 D2C 双跑、截图人工检查。最终测试数量以本次任务交付消息为准。

## 7. 明确边界

以下不属于本阶段“单用户下载独立使用”的核与客户端完成范围：

- Apple Developer ID 签名、公证、DMG/自动更新；
- Windows/Linux 打包验证；
- 多租户、中心化权限、服务端部署与监控；
- 云端浏览器验证 Worker；
- 领域专家对特定业务语义 Oracle 的替代。

这些边界不影响本地任务创建、真实远程 Agent 评测、完整日志、受控多文件优化、D2C 本地验证和收敛闭环。

