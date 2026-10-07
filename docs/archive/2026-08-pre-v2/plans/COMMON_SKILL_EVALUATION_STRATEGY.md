# 常用 Skill 评测与优化优先级建议

## 1. 分类原则

普通用户不选择 Skill 分类。Evaluation Compiler 根据 Skill、验收标准和可选 Cases，在内部按以下四个维度形成评测画像：

- 结果契约：结构化结果、代码/文档产物、诊断结论、远端状态变更；
- 执行形态：只读查询、本地文件处理、浏览器操作、远端事务；
- 验证方式：精确值、记录匹配、源码依据、结构 diff、渲染 diff、前后状态 diff；
- 风险：权限、外部副作用、幂等、回滚、敏感信息和误操作成本。

分类只用于选择 Runtime、Grader、Case 生成策略和风险门禁，不成为用户必须理解的概念。

## 2. 建议实施顺序

### 第一批：最适合验证 Skill 优化主链

1. 代码评审：采用仓库 fixture、结构化 findings、源码行引用、precision/recall 和误报门禁。输入输出清晰，适合先验证自动 repair。
2. 告警与日志诊断：先做只读查询和离线 Replay，评价根因准确性、证据引用、时间范围和工具效率。适合验证复杂工具调用 Skill。
3. BI/SQL 取数：通过固定数据快照或查询沙箱评价结果准确性、SQL 安全、扫描成本和口径一致性。热门程度高，且可复用同一数据查询 Blueprint。
4. CSV/结构化文件处理：当前 Reference Runtime 已支持，适合作为稳定回归基线。

### 第二批：需要专用 Artifact 或 Browser Runtime

1. D2C/UI 还原：必须同时检查构建成功、DOM/样式结构、截图差异、响应式断点和交互，不建议只用截图相似度。
2. xlsx/docx/pptx：应做 OOXML 结构检查、公式/布局/格式保留、渲染预览和文件可打开性；不同文档类型复用 Artifact Runtime，但不复用同一完整 EvalSuite。
3. 在线 Markdown/学城编辑：使用测试空间，记录 before/after 快照、目标页面、内容 diff、权限、幂等和非目标页面零变更。

### 第三批：高副作用工作流

审批、TT、ONES、发布和 DevOps Skill 必须在沙箱项目或 dry-run 中评测，并增加目标确认、权限范围、幂等、回滚和人工批准门禁。它们不应直接用生产写操作作为优化循环。

## 3. 热门 Skill 的特殊处理

- `mtsso-skills-official` 更适合作为所有在线 Skill 的依赖契约测试：凭证注入、过期、权限不足、跨 Skill 隔离和日志脱敏；不建议按普通内容生成 Skill 单独优化。
- `skill-metric-reporter` 应作为横向观测基础设施接入每次实验，而不是主要业务 Benchmark。
- `nocode-cli` 属于编排型 Skill，应按代表性完整工作流评测，重点观察工具选择、参数正确性、失败恢复和副作用范围。
- `citadel` 功能面很大，应拆成搜索/读取、创建、编辑、权限/评论四个能力族；读取与搜索可以先上线，写操作后接沙箱状态门禁。
- BI 系列可以共享 Query Sandbox、数据快照和安全 Grader，但不同业务口径的 Cases/Oracle 必须版本化，不能因为同属“数据查询”就直接整包复用。

## 4. 复用层级

```text
完全相同的标准 + Cases + fixture 内容 + 评测画像
  -> 复用冻结 EvalSuite

同一结果/执行/验证画像，但业务标准不同
  -> 复用 Runtime/Driver/Grader Blueprint，重新生成 Cases/Oracle

画像或 Runtime 能力不匹配
  -> 生成新草稿或报告 Runtime adapter 缺口
```

完整 EvalSuite 只做精确签名复用。分类相同不代表评测标准相同，系统不会把一个 SQL 口径的 Oracle 复用于另一个业务口径，也不会把一个页面的 D2C 截图用于另一页面。

## 5. 当前实现边界

当前已实现：内部画像推断、精确 EvalSuite 复用、模板复用、无 Cases 最小草稿生成、用户 Cases 覆盖、自定义 Pack/ExperimentPlan、Runtime 缺口阻断。

尚需专用实现：Browser/视觉 Runtime、Office Artifact Runtime、Query Sandbox、远端状态快照/回滚和各公司工具的测试租户。系统会把这些能力报告为缺口，不会降级成一个无法证明效果的 generic 评测。
