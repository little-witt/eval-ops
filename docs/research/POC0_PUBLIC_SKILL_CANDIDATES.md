# PoC-0 公开 Skill 候选与实验资产登记

**状态**：候选登记；尚未克隆、固定 commit 或纳入实验。  
**日期**：2026-08-26  
**研究边界**：仅使用可公开复现的 Skill 工件；内部 Skills、内部 trace 与 CATX 凭据不进入本清单或公开 artifact。

## 1. PoC-0 选择标准

候选必须满足：

1. 包含 `SKILL.md`，最好同时含 scripts、references 或明确工具工作流；
2. 许可证明确，并在固定 commit 上再次检查；
3. 可以在临时 workspace/受控 fixture 中运行，不依赖内部数据；
4. 能为核心任务建立确定性 oracle，例如 JSON/schema、记录集合、workspace diff、文件内容或 trace checkpoint；
5. 不要求执行攻击、数据外传、破坏性命令或未隔离的外部副作用；
6. 可以把 Skill revision、许可证文本和 EvalPack provenance 纳入公开 artifact。

GitHub star 数不是候选质量或科学代表性的依据。

## 2. 首轮三候选

| 方向 | 仓库/子 Skill | URL | 当前许可证判断 | PoC 任务 | 确定性 oracle | 当前风险 |
|---|---|---|---|---|---|---|
| 防御性代码审查 | `mukul975/Anthropic-Cybersecurity-Skills` 的防御性审查子集 | https://github.com/mukul975/Anthropic-Cybersecurity-Skills | Apache-2.0，纳入前必须在固定 SHA 再次校验 | 对固定 Python/JS 漏洞 fixture 生成结构化 finding | 预期 vulnerability/finding/MITRE technique 集合；禁止攻击步骤 | 仅选择防御性子 Skill；不可使用其中 offensive 内容 |
| 仓库操作 | `initializ/forge` / `skills/code-agent` | https://github.com/initializ/forge | Apache-2.0，纳入前必须在固定 SHA 再次校验 | 微型仓库函数重命名、配置修复、测试修复 | workspace diff、测试输出、文件树/AST 断言 | 只用 `skills/`，不依赖完整联网 Forge runtime |
| 数据处理（条件候选） | `anthropics/skills` / `skills/xlsx` | https://github.com/anthropics/skills | source-available/proprietary；不可假定可再分发 | CSV/XLSX 结构或公式修复 | workbook sheet/formula/schema 精确校验 | 仅当许可证明确允许本研究的引用、patch 和 artifact 处理时纳入；否则替换 |

## 3. 严格开源替代池

| 候选 | URL | 当前许可证判断 | 建议测试方式 | 风险 |
|---|---|---|---|---|
| `anthropics/skills` / `mcp-builder` | https://github.com/anthropics/skills | Apache-2.0 子目录，需固定 SHA 核验 | OpenAPI fixture → MCP tool schema | 需构建/解析校验 |
| `anthropics/skills` / `webapp-testing` | https://github.com/anthropics/skills | Apache-2.0 子目录，需固定 SHA 核验 | 固定 HTML → DOM assertion | 避免以截图 hash 作为唯一 oracle |
| `zhaoxuya520/reverse-skill` / `code-audit` | https://github.com/zhaoxuya520/reverse-skill | MIT，需固定 SHA 核验 | 漏洞源码 → finding 集合 | 依赖工具需在实验环境固定 |
| `OthmanAdi/planning-with-files` | https://github.com/OthmanAdi/planning-with-files | MIT，需固定 SHA 核验 | 任务描述 → plan 文件 schema/status | 自由 Markdown 的质量 oracle 较弱 |
| `K-Dense-AI/scientific-agent-skills` | https://github.com/K-Dense-AI/scientific-agent-skills | MIT，需固定 SHA 核验 | 科学数据/文档子集的结构化产物 | 仓库与依赖较大，适合第 2 阶段 |

## 4. 固定流程（任何候选进入实验前必须完成）

对每个选中候选记录：

```text
repository URL
resolved commit SHA
retrieval timestamp
license file path
license text SHA-256
selected Skill path
Skill subject hash
included file manifest
excluded files and rationale
runtime/container/profile hash
```

冻结后不得无记录地更新远端 HEAD。若仓库许可证、目标 Skill 路径或脚本权限发生变化，视为新 subject revision。

## 5. PoC-0 最小 EvalPack 结构

每个纳入的 Skill 暂定 20 个 case：

| Split | 数量 | 用途 |
|---|---:|---|
| dev | 12 | 诊断、分歧分析与候选修复 |
| validation | 4 | 候选选择与回归门 |
| sealed holdout | 4 | 仅用于最终确认，不向优化器反馈 |

PoC 必须另有 20–30 个带真值的 defect variants，至少三分之一是环境、fixture、grader 或 oracle 故障，以验证系统不会把非 Skill 故障误授权为 Skill patch。

## 6. 下一步决策

1. 对上述三候选做 commit/license 固定；
2. 若 `xlsx` 的许可证不允许公开实验资产，改从严格开源替代池选择一个数据/工具 Skill；
3. 为最终三项各写一份 EvalPack design brief：能力图、20 case、确定性 oracle、split provenance、安全边界；
4. 只有上述资产冻结后，才启动真实 CATX 多路径运行。
