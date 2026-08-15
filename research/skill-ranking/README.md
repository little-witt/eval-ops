# skills.sh 排行快照

本目录冻结了 `SKILL_CATEGORY_AND_ITERATION_DESIGN.md` 使用的原始数据和分类规则。

| 文件 | 抓取时间（Asia/Shanghai） | SHA-256 |
|---|---|---|
| `all-time-top-200.json` | 2026-08-15 14:56:57 +08:00 | `5aa6bc9e597af3d2183512f2a89c86f1959a815fa2d4999392c27c649aff6134` |
| `trending-top-200.json` | 2026-08-15 15:01:12 +08:00 | `7b7e6d229d1040221bd5229b47e9e470cd93364415dc538b684ed650cd0aca64` |

抓取端点：

- `https://skills.sh/api/skills/all-time/0`
- `https://skills.sh/api/skills/trending/0`

这两个 URL 是站点使用的未文档化只读端点，不是稳定 API 契约。官方文档化的 `/api/v1` API 当前要求 Vercel OIDC。

复算统计：

```bash
python3 research/skill-ranking/analyze.py \
  research/skill-ranking/all-time-top-200.json

python3 research/skill-ranking/analyze.py \
  research/skill-ranking/trending-top-200.json
```

导出逐项主分类：

```bash
python3 research/skill-ranking/analyze.py \
  research/skill-ranking/all-time-top-200.json \
  --csv /tmp/all-time-classified.csv
```

分类是面向评测设计的人工规则，不是 skills.sh 官方标签。每个 Skill 为统计目的只分配一个主类别，正式 Case 可以使用多个领域标签。
