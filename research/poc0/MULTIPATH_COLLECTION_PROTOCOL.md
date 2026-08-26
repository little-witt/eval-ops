# PoC-0 多路径采集协议

**状态**：运行前协议；未执行真实 CATX 会话。  
**版本**：`aceval.poc0-multipath-protocol/v1`  
**研究目的**：在冻结 Skill、EvalPack 和运行环境条件下，收集独立执行路径，检验轨迹分歧与带真值 Skill 缺陷之间的关联。

## 1. 前置条件

在任何远程执行前，必须完成：

1. `subjects.lock.json` 中每个 subject 的 commit、entrypoint 和 license hash 校验；
2. 每个 EvalPack 完成 calibration、quality gate 与 freeze，获得 immutable `pack_hash`；
3. 记录固定运行条件：CATX profile/binding、Agent harness revision、模型 ID、模型参数、工具 registry hash、容器/fixture image hash；
4. 使用合成 fixture，确认不存在生产凭据、内部数据、真实域名、攻击载荷或外传路径；
5. 每个 case 均已标记 split，优化器只可见 dev，validation 仅用于选择，holdout 不得反馈到 patch 生成；
6. 运行前记录预算：最大 session 数、最大 token、单 case timeout、最大并发数；
7. 确认当前环境对所有副作用有明确限制。当前系统不是 hostile-code sandbox；不得执行真实破坏性或对外操作。

若任一项不满足，运行标为 `blocked`，不产生可用于论文分析的样本。

## 2. 单位与固定条件

分析单位是：

```text
(subject_revision, evalpack_hash, case_id, run_context_hash, path_index)
```

其中相同 `(subject_revision, evalpack_hash, case_id, run_context_hash)` 的各 path 必须仅在独立 CATX session ID 上不同。下列字段不得在 K 条路径之间漂移：

- Skill commit / subject hash；
- fixture、oracle、case prompt 与 execution-path spec；
- Agent harness、系统提示、工具 registry；
- model ID、provider、temperature、top-p、max token；
- runtime profile、容器 image / CATX binding；
- timeout、权限、网络策略与 workspace policy。

模型采样若不可强制确定，必须记录 seed；若 provider 不支持 seed，记录 `seed: null` 和 provider capability。

## 3. K=3 采集流程

PoC-0 初始使用 `K=3`。高分歧或高风险 case 可在预注册规则下增至 K=5；不得根据 holdout 成绩临时增加或删除路径。

对每一个 case：

1. 创建隔离 workspace，并验证 subject/repository binding；
2. 为 `path-1`、`path-2`、`path-3` 各创建独立 CATX session；
3. 完整回收 session event stream、final output、artifacts、usage、error 和 binding evidence；
4. 对每条路径分别运行确定性 graders、execution-path conformance 与 `FailureAttributor`；
5. 若 trace 不完整或运行环境错误，保留原始证据，并按现有 contract 标记 `not_evaluable` 或环境失败；不得将其转换成 Skill fail；
6. 将三条路径整理为 `aceval path divergence` 的 attempts 输入；
7. 将 `DivergenceReport` 与每个 path 的独立 grader/attribution 结果关联存档；
8. 仅在 dev split 上允许下一阶段的 patch 生成。多路径分歧本身不授权 patch。

## 4. 数据格式

每条 path 至少记录以下结构：

```json
{
  "subject_id": "repository-code-agent",
  "subject_commit": "<sha>",
  "subject_hash": "<hash>",
  "evalpack_hash": "<hash>",
  "case_id": "rename-python-dev",
  "split": "dev",
  "path_index": 1,
  "attempt_id": "path-1",
  "run_context_hash": "<hash>",
  "catx_session_id": "<id>",
  "model_profile_hash": "<hash>",
  "trace_complete": true,
  "outcome": "pass|fail|not_evaluable|error",
  "trace": [],
  "usage": {},
  "grader_results": [],
  "path_conformance": {},
  "failure_attribution": {},
  "artifact_refs": [],
  "binding_evidence": {}
}
```

`attempt_id` must be stable and path-indexed. Raw CATX responses containing credentials, private URLs, internal paths or user data must be redacted before release. Maintain a private-to-public redaction manifest with content hashes.

## 5. Divergence analysis

Use the deterministic analyzer:

```bash
PYTHONPATH=src python -m aceval path divergence \
  --case-id <case-id> \
  --attempts @attempts.json \
  --spec execution-path.json \
  --output divergence-report.json
```

The report exposes outcome disagreement, execution checkpoint divergence, tool-sequence divergence and cost variance. It must be interpreted as an **observational evidence report**:

- `predicted_defect_surface` names the most divergent observable surface, not a root cause;
- `evidence_confidence` reflects path count and score, not correctness of a defect hypothesis;
- a repair remains forbidden unless independent deterministic attribution authorizes a Skill-level intervention;
- environment/fixture/grader/oracle faults remain negative controls.

## 6. PoC-0 analysis plan

### Primary analysis

Use the defect-variant set with ground-truth labels. For each `(subject, case)` aggregate only paths with a matching run context. Compare the divergence score distribution between:

1. true Skill defects;
2. non-Skill negative controls (fixture, runtime, grader, oracle faults);
3. clean subjects/cases.

Pre-register a rank-based effect size and a ROC/AUPRC analysis; report confidence intervals clustered by subject. Do not claim a causal defect predictor from a small PoC. The aim is a Go/No-Go signal for the larger benchmark.

### Secondary analysis

- Outcome variance vs trace-only variance;
- attribution precision for `ALLOW_SKILL_INTERVENTION` with and without multipath evidence;
- case cost for K=3 and any pre-registered K=5 escalation;
- qualitative inspection of first divergence points.

## 7. Go / No-Go gates

Move to V1 only when all conditions hold:

1. At least three subjects and the planned defect/negative-control variants complete with valid provenance;
2. no environment, fixture, grader or oracle fault is automatically promoted as a Skill patch decision;
3. high-divergence cases show a pre-registered, non-trivial association with true Skill defects, with uncertainty reported;
4. traces, hashes and metadata can be replayed into identical divergence reports;
5. K=3 cost is within the pre-registered budget and no safety policy is violated.

If the association is absent, retain divergence only as a reliability descriptor and do not build a defect-directed patch policy around it. If provenance or safety conditions fail, stop collection and repair the infrastructure rather than rerunning until a desired result appears.

## 8. Budget and stopping rules

Initial budget target, to be finalized before first CATX run:

| Item | PoC cap |
|---|---:|
| selected Skills | 3 |
| nominal cases per Skill | 20 |
| independent paths per case | 3 |
| base case-attempts | 180 |
| defect/negative-control variants | 20–30 total |
| additional case-attempts for variants | 60–90 |
| estimated total attempts | 240–270 |
| initial concurrent CATX sessions | 8 |

Stop a batch if repeated binding drift, incomplete traces, evaluator failures, unexpected egress, budget exhaustion, or any policy violation occurs. Preserve the failed batch as infrastructure evidence; do not silently drop it.

## 9. Post-run artifact checklist

- [ ] frozen subject, pack, runtime and model hashes;
- [ ] attempt records and redacted trace archive;
- [ ] deterministic grader and attribution outputs;
- [ ] divergence reports and report hashes;
- [ ] budget/cost summary;
- [ ] negative-control outcome table;
- [ ] exceptions, dropped runs and exact exclusion reason;
- [ ] no holdout feedback entered the optimization loop.
