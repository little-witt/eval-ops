# 多文件 Skill 优化设计与 alert-level-with-kb 示例

## 为什么初版只有 SKILL.md

初版把 `SKILL.md` 当作完整 Subject，候选契约只要求模型返回完整 `skill_markdown`，Subject hash 也只覆盖入口和可选 `subject.json`。这样能先建立父版本校验、Case 泄漏防护、用户确认和单文件 Git 回滚，但也使分析器、物化器和发布器同时形成单文件边界；它不是 Skill 的合理长期边界。

## 真实仓库证明了什么

`ssh://git@git.sankuai.com/nibfe/alert-level-with-kb.git` 的 `feature/lzn/auto-better` 在读取时指向 commit `1db17de307ca4b3d32a5d5b66a0f9e198fb14870`。该分支的行为分布在约 50 个资源文件中：

| 问题类型 | 应优先检查/修改的资源 |
|---|---|
| 路由和总流程不清晰 | `SKILL.md` |
| 数据采集、截断、解析、趋势计算错误 | `scripts/collect.py`、`scripts/collectors/*.py`、`scripts/lib/*.py` |
| 风险分级逻辑错误 | `workflow/fast/stage-3-grade.md`、`workflow/level-evidence/*.md` |
| 快速流程开关错误 | `config/fast-flow-whitelist.json` |
| 平台/指标知识不完整 | `knowledge/**/*.md` |
| 输出格式不一致 | `specs/RISK-RESULT-TEMPLATE.md` |
| 防回归 | `scripts/tests/*.py` |

例如“48h 新增异常名称被截断”如果根因位于 `scripts/collectors/group_b_js.py`，只在 `SKILL.md` 增加一句说明既不能修复确定性脚本行为，还会增加模型绕开脚本的概率。正确的候选可能需要同时修改 collector、分级 workflow 和对应测试。

## 新内核的处理方式

1. 从受管 Skill checkout 生成可编辑资源清单；`.git/`、`.env*`、链接、二进制文件、`skill.manifest` 和 `skill.sig` 不可编辑。
2. 跨 Case 分析 Agent 只能从清单选择精确 `target_scope`，并将完整文件列表交给用户确认。
3. 优化 Agent 只看到确认文件及失败簇证据，输出唯一匹配的 `old_text -> new_text` 操作；不回传未修改文件，降低 Token。
4. 候选保存完整只读 Skill 树、父树/候选树 hash、统一 diff、修改原因和验证回执。
5. 自动执行 UTF-8、JSON、Python 语法检查；仓库单测由 `optimization_validation_commands` 配置，命令使用 argv 启动且不经过 shell。
6. 发布前确认 checkout 的 origin、branch、clean status 和父树 hash；一次提交 manifest 声明的全部文件。提交前失败恢复全部原始字节，push 失败可直接重试。
7. 新会话绑定更新后的同一 Skill 分支，重新执行完整 Case，而不是只回归被修复 Case。
8. repair/tune 仍禁止创建文件；extend/create 只能通过已批准能力蓝图的 `create_file` 操作增加精确路径，manifest 会独立记录 `created_paths`。

当前版本不允许模型自主删除、重命名文件，也不编辑二进制资源和签名/manifest。新建文件只在 extend/create 的能力蓝图和候选门内授权，不能混入普通文本修复。

## 该仓库建议配置

```json
{
  "optimization_editable_patterns": [
    "SKILL.md",
    "scripts/**",
    "workflow/**",
    "knowledge/**",
    "specs/**",
    "config/**"
  ],
  "optimization_validation_commands": [
    ["python3", "-m", "unittest", "discover", "-s", "scripts/tests"]
  ],
  "max_optimization_changed_files": 8,
  "max_optimization_added_lines": 300,
  "max_optimization_changed_bytes": 524288,
  "max_optimization_context_chars": 120000
}
```

建议将 `scripts/tests/*.py` 保持为可编辑资源，但分析阶段只有在“实现修改必须同步补回归测试”时才把具体测试文件加入 `target_scope`。这能避免每轮把近两千行测试文件无条件送入模型。
