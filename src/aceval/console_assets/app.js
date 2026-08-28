(() => {
  "use strict";

  const state = { graph: null, tasks: [], selectedTask: null, selectedNode: null, view: "tree" };
  const byId = (id) => document.getElementById(id);

  function node(tag, className, text) {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined && text !== null) value.textContent = String(text);
    return value;
  }

  function clear(target) {
    while (target.firstChild) target.removeChild(target.firstChild);
  }

  function list(target, values, emptyText = "—") {
    clear(target);
    const items = Array.isArray(values) && values.length ? values : [emptyText];
    items.forEach((value) => target.appendChild(node("li", "", value)));
  }

  function percent(value) {
    return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "—";
  }

  function normalizedRate(value) {
    return typeof value === "number"
      && Number.isFinite(value)
      && value >= 0
      && value <= 1
      ? value
      : null;
  }

  function compactNumber(value) {
    if (typeof value !== "number") return "—";
    if (Math.abs(value) >= 1000000) return `${(value / 1000000).toFixed(2)}m`;
    if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(1)}k`;
    return String(Math.round(value));
  }

  function formatTime(value) {
    if (typeof value !== "number") return "—";
    if (value >= 60) return `${(value / 60).toFixed(1)}m`;
    return `${value.toFixed(1)}s`;
  }

  const statusLabels = {
    pass: "通过", fail: "未通过", completed: "已完成", running: "运行中", pending: "待运行",
    starting: "正在创建", failed: "失败", blocked: "已阻塞", ready: "就绪", unavailable: "暂无结果",
    needs_revalidation: "需要重新评测", not_evaluable: "证据不足", not_measured: "未测量", measured: "已测量",
    promote: "允许晋升", do_not_promote: "不允许晋升", converged: "已收敛", degraded: "有波动",
    valid: "证据有效", invalid: "证据无效", partial: "证据不完整", review_required: "待审阅",
    awaiting_confirmation: "待确认", ready_to_optimize: "准备优化", semantic_grading: "语义评分",
    attribution: "失败归因", proposal: "生成提案", evaluation_ready: "方案已批准",
    remote_running: "线上评测", remote_collected: "日志已回收", baseline_running: "基线评测",
    baseline_collected: "基线已回收", verification_running: "稳定性复验", verification_collected: "复验已回收",
    evidence_ready: "证据已冻结", initial_candidate_ready: "首版候选待确认", candidate_ready: "候选待发布",
  };
  const purposeLabels = { evaluation: "正式评测", "pass-verification": "稳定性复验", "without-skill-baseline": "无 Skill 基线" };
  const originLabels = { generated: "系统自动生成", reused: "复用已有评测方案", user: "用户提供", custom: "用户自定义", "system-generated reusable Fixture Lab": "系统自动生成的可复用方案" };
  const pathKindLabels = { required: "必须观察", recommended: "建议观察", forbidden: "禁止发生", alternative: "允许替代" };
  const pathSourceLabels = { automatic: "自动规划", generated: "模型生成", model: "模型生成", user: "用户提供", fallback: "确定性规划补充" };
  const dimensionStatusLabels = { pass: "通过", fail: "未通过", degraded: "有波动", not_measured: "未测量", not_evaluable: "证据不足" };
  const reasonLabels = {
    pagination_not_proven_complete: "未能证明所有日志页都已拉取",
    declared_total_mismatch: "服务端总数与实际日志数不一致",
    invalid_sequence: "日志序号格式无效",
    partial_sequence_numbers: "只有部分事件带序号",
    duplicate_sequence: "日志序号重复",
    sequence_gap: "日志序号存在缺口",
    duplicate_event_id: "事件 ID 重复",
    tool_result_missing: "有工具调用但缺少返回",
    orphan_tool_result: "存在找不到对应调用的工具返回",
    required_event_type_missing: "缺少关键会话事件",
    event_log_hash_mismatch: "日志内容哈希与收据不一致",
    event_log_missing: "缺少原始事件日志",
    event_log_hash_missing: "缺少事件日志哈希收据",
    legacy_event_log: "兼容旧日志格式，仅供查看，不能用于晋升",
    malformed_tool_call: "工具调用结构不完整",
    malformed_tool_result: "工具返回结构不完整",
    trace_channel_incomplete: "Trace 通道不完整",
    attempt_manifest_mismatch: "两轮的 Case 或评分契约不一致",
    malformed_aggregate: "评测汇总记录缺少必要字段，不能安全比较",
    "attempt.legacy_event_log": "使用兼容旧日志，不能作为晋升依据",
    insufficient_evidence: "证据不足，不能作出可靠结论",
    insufficient_stability: "候选尚未完成规定次数的稳定性复验",
    critical_regression: "已有稳定能力发生回归",
    legacy_aggregate: "历史汇总缺少逐次尝试收据，不能用于晋升",
    minimum_effect_not_met: "改善幅度未达到门槛",
  };
  const eventLabels = {
    "kernel.created": "任务输入已冻结",
    "kernel.phase_changed": "阶段已切换",
    "evaluation.design_compiled": "评测方案已编译",
    "evaluation.evalpack_ready": "评测方案已就绪",
    "evaluation.case_path_ready": "Case 与执行路径已就绪",
    "evaluation.design_review_required": "等待评测方案确认",
    "evaluation.case_generation_started": "开始生成评测 Case",
    "evaluation.case_generation_completed": "评测 Case 已生成并校验",
    "evaluation.case_generation_failed": "Case 生成失败，已降级规划",
    "case_run.started": "评测会话已创建",
    "case_run.completed": "会话日志已回收",
    "case_run.failed": "评测会话失败",
    "case_run.retried": "失败 Case 已重试",
    "remote.batch_polled": "远端评测进度更新",
    "remote.batch_retry_dispatched": "失败路径已重新创建",
    "trace.captured": "会话证据已归档",
    "path_conformance.completed": "执行路径分析完成",
    "analysis.completed": "证据分析已完成",
    "optimization.proposal_ready": "修改提案已生成",
    "user.change_scope_approved": "修改范围已批准",
    "user.evaluation_design_approved": "评测方案已批准",
    "user.capability_blueprint_approved": "能力方案已批准",
    "environment.contract_frozen": "本轮运行环境已冻结",
    "user.approval_recorded": "用户审批已记录",
    "candidate.created": "候选版本已生成",
    "candidate.rejected": "候选版本未通过",
    "candidate.promoted": "候选版本已晋升",
    "decision.recorded": "收敛决策已记录",
    "candidate.published": "候选版本已发布",
  };
  const displayStatus = (value, fallback = "未知") => statusLabels[value] || String(value || fallback).replaceAll("_", " ");
  const displayPurpose = (value) => purposeLabels[value] || String(value || "未说明").replaceAll("_", " ");
  const displayOrigin = (value) => originLabels[value] || String(value || "未记录来源").replaceAll("_", " ");
  const displayPathKind = (value) => pathKindLabels[value] || String(value || "必须观察").replaceAll("_", " ");
  const displayPathSource = (value) => pathSourceLabels[value] || String(value || "未记录来源").replaceAll("_", " ");
  const displayReason = (value) => reasonLabels[value] || String(value || "未知原因").replaceAll("_", " ");
  const displayEventType = (value) => eventLabels[value] || String(value || "事件").replaceAll(".", " · ").replaceAll("_", " ");
  function traceCompleteness(run) {
    const receipt = run && (run.log_completeness || run.completeness);
    const reasons = Array.isArray(receipt?.reason_codes) ? receipt.reason_codes : [];
    const missing = Array.isArray(receipt?.missing_required_event_types) ? receipt.missing_required_event_types : [];
    const complete = run?.trace_complete === true && receipt?.complete !== false && !reasons.length && !missing.length;
    if (complete || (run?.trace_complete == null && receipt?.complete === true && !reasons.length && !missing.length)) {
      return { label: "日志完整", className: "tag-ok", detail: receipt?.event_count ? `${receipt.event_count} 个事件已核对` : "分页、关键事件和哈希均已核对" };
    }
    if (run?.trace_complete === false || receipt?.complete === false || reasons.length || missing.length) {
      const detail = [...reasons.map(displayReason), ...missing.map(item => `缺少关键事件 ${item}`)].join("；") || "关键事件或日志完整性校验未通过";
      return { label: "日志不完整 · 不可评估", className: "tag-warn", detail };
    }
    return { label: "日志完整性待核验", className: "tag-warn", detail: "尚未收到完整性收据" };
  }
  function titleCase(value) {
    const raw = String(value || "unknown");
    return statusLabels[raw] || ({
      input: "输入", evaluation: "评测", baseline: "基线", candidate: "候选", convergence: "收敛", current_best: "当前稳定版本",
      extend: "扩展", repair: "修复", tune: "优化", discover: "探索", create: "创建", automatic: "自动",
      legacy: "兼容模式", frozen: "已冻结", generated: "已生成", reused: "已复用", completed: "已完成",
      code_review: "代码评审", "code-review": "代码评审", d2c: "D2C 验证", optimization: "优化",
      "evaluation-design": "评测设计", v0: "首版稳定版本", v1: "第 2 版候选", "no-skill-baseline": "无 Skill 基线",
    }[raw] || raw.replaceAll("_", " "));
  }

  function metricValue(metrics) {
    if (!metrics || typeof metrics !== "object") return "—";
    if (typeof metrics.assertion_pass_rate === "number") return percent(metrics.assertion_pass_rate);
    if (typeof metrics.cases === "number") return `${metrics.cases} 个 Case`;
    return "—";
  }

  function setDefinitionList(target, rows) {
    clear(target);
    rows.forEach(([term, description]) => {
      const wrap = node("div");
      wrap.appendChild(node("dt", "", term));
      wrap.appendChild(node("dd", "", description || "—"));
      target.appendChild(wrap);
    });
  }

  function renderHeader(graph) {
    byId("run-title").textContent = graph.run.skill_name;
    byId("run-goal").textContent = graph.input.goal;
    byId("decision-value").textContent = titleCase(graph.convergence.decision);
    byId("best-value").textContent = graph.convergence.current_best;
    byId("confidence-value").textContent = titleCase(graph.run.confidence);
    byId("run-kicker").textContent = `${titleCase(graph.run.status)} · 每个 Case 运行 ${graph.run.repetitions} 次`;
    byId("generated-at").textContent = new Date(graph.generated_at).toLocaleString();
    byId("footer-version").textContent = graph.api_version;
  }

  function renderInput(graph) {
    const skill = graph.input.skill || {};
    setDefinitionList(byId("input-contract"), [
      ["Skill", skill.name],
      ["版本引用", skill.ref || "工作区快照"],
      ["当前稳定版本", skill.current_best],
      ["Case 来源", displayOrigin(graph.evaluation_design.origin)],
      ["评测方案状态", graph.evaluation_design.frozen ? "已冻结" : "未冻结"],
    ]);
    const criteria = graph.input.success_criteria || [];
    byId("criteria-count").textContent = String(criteria.length);
    list(byId("criteria-list"), criteria);
    const blockers = graph.convergence.blockers || [];
    byId("blocker-count").textContent = String(blockers.length);
    list(byId("blocker-list"), blockers, "当前没有阻塞原因");
  }

  function renderTree(graph) {
    const target = byId("optimization-tree");
    clear(target);
    graph.nodes.forEach((item, index) => {
      const column = node("div", "tree-column");
      column.appendChild(node("div", "tree-step", `${String(index + 1).padStart(2, "0")} / ${titleCase(item.kind)}`));
      const button = node("button", `tree-node ${item.status}`);
      button.type = "button";
      button.dataset.nodeId = item.id;
      const label = node("div", "node-index");
      label.appendChild(node("span", "", item.id));
      label.appendChild(node("span", "", titleCase(item.status)));
      button.appendChild(label);
      button.appendChild(node("h4", "", item.title));
      button.appendChild(node("p", "", item.summary));
      const metric = node("div", "metric-line");
      metric.appendChild(node("span", "", item.commit ? item.commit.slice(0, 7) : "未记录版本"));
      metric.appendChild(node("b", "", metricValue(item.metrics)));
      button.appendChild(metric);
      button.addEventListener("click", () => selectNode(item.id));
      column.appendChild(button);
      target.appendChild(column);
    });
  }

  function renderDimensions(graph) {
    const target = byId("dimension-board");
    clear(target);
    graph.dimension_summary.forEach((dimension) => {
      const card = node("article", `dimension-card ${dimension.status}`);
      card.appendChild(node("span", "dimension-state", titleCase(dimension.status)));
      card.appendChild(node("h4", "", dimension.label));
      const bars = node("div", "compare-bars");
      [["无 Skill 基线", dimension.baseline, "baseline"], ["候选版本", dimension.candidate, "candidate"]].forEach(([label, value, key]) => {
        const row = node("div", `bar-row ${key}`);
        row.appendChild(node("span", "", label));
        const track = node("div", "bar-track");
        const fill = node("div", "bar-fill");
        const rate = typeof value.rate === "number" ? value.rate : 0;
        fill.style.width = `${Math.max(0, Math.min(100, rate * 100))}%`;
        track.appendChild(fill);
        row.appendChild(track);
        row.appendChild(node("strong", "", value.measured ? `${value.passed}/${value.measured}` : "未测量"));
        bars.appendChild(row);
      });
      card.appendChild(bars);
      target.appendChild(card);
    });
  }

  function resultPill(payload) {
    payload = payload || {};
    let label = displayStatus(payload.status, "暂无结果");
    let className = "result-pill";
    const rate = normalizedRate(payload.pass_rate);
    const passRate = rate == null ? null : `${Math.round(rate * 100)}%`;
    if (payload.formal_pass === true) {
      label = passRate == null ? "通过" : `通过 · 通过率 ${passRate}`;
      className += " pass";
    } else if (payload.status === "completed") {
      // ``pass_rate`` is a ratio in [0, 1], not a five-point score.  Keep a
      // completed-but-not-formally-passing run visibly distinct from a
      // trusted pass, while never inventing 0% when the rate is absent.
      label = passRate == null ? "已完成 · 通过率未知" : `已完成 · 通过率 ${passRate}`;
      className += " fail";
    }
    return node("span", className, label);
  }

  function scorePill(payload) {
    const rawScore = payload && payload.score ? payload.score.overall : null;
    const score = normalizedRate(rawScore);
    const hardGate = payload && payload.score && payload.score.hard_gate === true;
    const status = hardGate ? "硬门通过" : score == null ? "不可评分" : `${(score * 100).toFixed(0)}%`;
    const className = hardGate ? "result-pill pass" : score == null ? "result-pill" : "result-pill fail";
    return node("span", className, status);
  }

  function pathPill(payload) {
    const status = payload && payload.path_status;
    const pathCoverage = normalizedRate(payload && payload.path_coverage);
    const coverage = pathCoverage == null ? "" : ` ${(pathCoverage * 100).toFixed(0)}%`;
    const className = status === "pass" ? "result-pill pass" : status === "fail" ? "result-pill fail" : "result-pill";
    return node("span", className, `${displayStatus(status, "未测量")}${coverage}`);
  }

  function renderCases(graph) {
    const target = byId("case-table");
    clear(target);
    graph.evaluation_design.cases.forEach((item) => {
      const row = node("tr");
      row.appendChild(node("td", "", item.id));
      row.appendChild(node("td", "", item.group));
      row.appendChild(node("td", "", item.type));
      const baselineCell = node("td");
      baselineCell.appendChild(resultPill(item.baseline));
      row.appendChild(baselineCell);
      const candidateCell = node("td");
      candidateCell.appendChild(resultPill(item.candidate));
      row.appendChild(candidateCell);
      const displayed = item.candidate && item.candidate.status !== "unavailable"
        ? item.candidate
        : item.baseline;
      const pathCell = node("td");
      pathCell.appendChild(pathPill(displayed));
      row.appendChild(pathCell);
      const scoreCell = node("td");
      scoreCell.appendChild(scorePill(displayed));
      row.appendChild(scoreCell);
      const binding = displayed && displayed.binding_verified === true;
      const bindingCell = node("td");
      bindingCell.appendChild(node("span", `result-pill ${binding ? "pass" : "fail"}`, binding ? "已验证" : "未验证"));
      row.appendChild(bindingCell);
      target.appendChild(row);
    });
  }

  function renderPathAnalysis(graph) {
    const target = byId("path-analysis");
    if (!target) return;
    clear(target);
    const design = graph.evaluation_design || {};
    const sourceCases = Array.isArray(design.cases) ? design.cases : [];
    const analysis = graph.path_analysis || {};
    const summary = node("div", "path-analysis-summary");
    const summaryStats = [
      ["评测 Case 数", analysis.case_count ?? sourceCases.length],
      ["有路径", analysis.cases_with_paths ?? sourceCases.filter(item => item.path_graph).length],
      ["路径定义率", typeof analysis.coverage_rate === "number" ? `${(analysis.coverage_rate * 100).toFixed(1)}%` : "—"],
    ];
    summaryStats.forEach(([label, value]) => {
      const stat = node("div", "path-summary-stat");
      stat.appendChild(node("span", "", label));
      stat.appendChild(node("strong", "", value));
      summary.appendChild(stat);
    });
    target.appendChild(summary);

    if (!sourceCases.length) {
      target.appendChild(node("p", "empty-state", "暂无可展示的执行路径。路径定义会在评测设计编译后冻结。"));
      return;
    }
    sourceCases.forEach((item) => {
      const path = item.path || design.paths?.[item.id];
      const graphValue = item.path_graph || design.path_graphs?.[item.id];
      const card = node("article", "path-case-card");
      const header = node("header", "path-case-head");
      const copy = node("div");
      copy.appendChild(node("span", "eyebrow", "执行路径"));
      copy.appendChild(node("h4", "", item.id));
      copy.appendChild(node("p", "path-prompt", item.prompt || "—"));
      header.appendChild(copy);
      const source = node("span", "path-source", displayPathSource(item.path_source));
      header.appendChild(source);
      card.appendChild(header);

      const body = node("div", "path-case-body");
      const route = node("div", "path-route");
      route.appendChild(node("div", "path-route-title", path?.purpose || "预期执行路径"));
      const steps = graphValue?.nodes || path?.steps || [];
      const baseline = sourceCases.find(value => value.id === item.id)?.baseline || item.baseline || {};
      const candidate = sourceCases.find(value => value.id === item.id)?.candidate || item.candidate || {};
      const stepStates = (run) => Object.fromEntries((run?.path_conformance?.steps || []).map(step => [step.id, step]));
      const baselineStates = stepStates(baseline);
      const candidateStates = stepStates(candidate);
      const observedStates = Object.keys(candidateStates).length ? candidateStates : baselineStates;
      const graphFlow = node("div", "path-graph-flow");
      steps.forEach((step, index) => {
        const id = step.id || `step-${index + 1}`;
        const kind = step.kind || "required";
        const observed = observedStates[id];
        const box = node("div", `path-graph-node ${kind}`);
        box.appendChild(node("b", "", String(index + 1).padStart(2, "0")));
        box.appendChild(node("span", "", step.label || id));
        box.title = `${step.label || id} · ${displayPathKind(kind)}`;
        if (observed) box.classList.add(kind === "forbidden" ? (observed.observed ? "violation" : "clear") : observed.observed ? "observed" : "missing");
        graphFlow.appendChild(box);
      });
      route.appendChild(graphFlow);
      const listTarget = node("ol", "path-route-list");
      steps.forEach((step, index) => {
        const id = step.id || `step-${index + 1}`;
        const observed = observedStates[id];
        const kind = step.kind || "required";
        let observedClass = "unmeasured";
        let badge = "未测量";
        if (observed) {
          if (kind === "forbidden") {
            observedClass = observed.observed ? "violation" : "clear";
            badge = observed.observed ? "发生禁止步骤" : "未发生";
          } else if (kind === "required") {
            observedClass = observed.observed ? "observed" : "missing";
            badge = observed.observed ? "已观察" : "缺失";
          } else {
            observedClass = observed.observed ? "observed" : "optional";
            badge = observed.observed ? "已观察" : "可选未发生";
          }
        }
        const row = node("li", `path-route-step ${kind} ${observedClass}`);
        row.appendChild(node("b", "path-step-number", String(index + 1).padStart(2, "0")));
        const detail = node("span", "path-step-detail");
        detail.appendChild(node("strong", "", step.label || id));
        const after = Array.isArray(step.after) && step.after.length
          ? `必须发生在 ${step.after.join("、")} 之后`
          : step.after_explicit ? "无顺序约束" : "兼容旧数据：按列表顺序";
        detail.appendChild(node("small", "", `${displayPathKind(step.kind)} · ${after}`));
        detail.appendChild(node("code", "", JSON.stringify(step.match || {})));
        row.appendChild(detail);
        row.appendChild(node("em", "", badge));
        listTarget.appendChild(row);
      });
      route.appendChild(listTarget);
      const edges = graphValue?.edges || [];
      if (edges.length) {
        const edgeLine = node("p", "path-edges");
        edgeLine.appendChild(node("b", "", "分支 / 顺序："));
        edgeLine.appendChild(node("span", "", edges.map(edge => {
          const annotation = [edge.relation, edge.condition].filter(Boolean).join(" / ");
          return `${edge.source} → ${edge.target}${annotation ? ` [${annotation}]` : ""}`;
        }).join(" · ")));
        route.appendChild(edgeLine);
      }
      if (graphValue?.mermaid) {
        const diagram = node("details", "path-diagram");
        diagram.appendChild(node("summary", "", "嵌入式流程图源码（Mermaid）"));
        diagram.appendChild(node("pre", "path-mermaid", graphValue.mermaid));
        route.appendChild(diagram);
      }
      body.appendChild(route);

      const runs = node("div", "path-run-grid");
      [["无 Skill 基线", baseline], ["候选版本", candidate]].forEach(([label, run]) => {
        const panel = node("section", "path-run-panel");
        panel.appendChild(node("h5", "", label));
        const pills = node("div", "trace-tags");
        pills.appendChild(pathPill(run));
        pills.appendChild(scorePill(run));
        const trace = traceCompleteness(run);
        const tracePill = node("span", `tag ${trace.className}`, trace.label);
        tracePill.title = trace.detail;
        pills.appendChild(tracePill);
        pills.appendChild(node("span", "tag", `重试 ${run.retry_count ?? 0}`));
        const tokenCount = typeof run.tokens === "number" && Number.isFinite(run.tokens)
          ? compactNumber(run.tokens)
          : "—";
        pills.appendChild(node("span", "tag", `Token 用量 ${tokenCount}`));
        pills.appendChild(node("span", "tag", `耗时 ${Number(run.duration_seconds || 0).toFixed(1)} 秒`));
        panel.appendChild(pills);
        const dimensions = run.score_dimensions || run.score?.dimensions || {};
        const scoreList = node("ul", "score-list");
        Object.values(dimensions).forEach((dimension) => {
          if (!dimension || !dimension.label) return;
          const score = typeof dimension.score === "number" ? `${(dimension.score * 100).toFixed(0)}%` : "未测量";
          const row = node("li", `score-row ${dimension.status || "not_measured"}`);
          row.appendChild(node("span", "", dimension.label));
          row.appendChild(node("strong", "", `${score} · ${dimensionStatusLabels[dimension.status] || displayStatus(dimension.status, "未测量")}`));
          scoreList.appendChild(row);
        });
        panel.appendChild(scoreList);
        const session = run.evidence?.session;
        const attempts = run.evidence?.attempts;
        const refs = node("div", "path-evidence-refs");
        refs.appendChild(node("code", "", `会话 ID：${session || "—"}`));
        refs.appendChild(node("code", "", `尝试次数：${attempts || "—"}`));
        panel.appendChild(refs);
        runs.appendChild(panel);
      });
      body.appendChild(runs);
      card.appendChild(body);
      target.appendChild(card);
    });
  }

  function configCard(title, rows) {
    const card = node("article", "config-card");
    card.appendChild(node("h4", "", title));
    const list = node("dl");
    setDefinitionList(list, rows);
    card.appendChild(list);
    return card;
  }

  function referenceLabel(value) {
    if (!value || !value.configured) return "未配置";
    if (value.source === "environment") return `$${value.name}`;
    return value.value || "已配置（值已隐藏）";
  }

  function renderConfiguration(graph) {
    const target = byId("configuration-board");
    clear(target);
    const config = graph.configuration || {};
    target.appendChild(configCard("CATX 运行环境", [
      ["配置名称", config.profile_name || "未配置"],
      ["服务地址", config.base_url || "—"],
      ["Agent", referenceLabel(config.agent)],
      ["运行环境", referenceLabel(config.environment)],
      ["Vault 数量", String(config.vault_count || 0)],
      ["凭据", config.credentials_file_configured ? "已配置（值已隐藏）" : "未配置"],
    ]));
    (config.repositories || []).forEach((repository, index) => {
      target.appendChild(configCard(`代码仓库 ${String(index + 1).padStart(2, "0")}`, [
        ["仓库地址", repository.url],
        ["挂载路径", repository.mount_path],
        ["访问凭据", repository.authorization_configured ? "已配置（值已隐藏）" : "未配置"],
        ["凭据来源", repository.authorization_source || "—"],
      ]));
    });
  }

  function eventSummary(event) {
    const payload = event.payload || {};
    if (event.type === "harness.blueprint_ready") return `${titleCase(payload.operation)} · ${(payload.proposal_ids || []).length} 个能力方案待确认`;
    if (event.type === "user.capability_blueprint_approved") return `${titleCase(payload.operation)} · 已选择 ${(payload.selected_proposal_ids || []).join(", ")}`;
    if (event.type === "harness.initial_candidate_created") return `首版候选已生成 · ${(payload.created_paths || []).length} 个文件待确认`;
    if (event.type === "harness.initial_candidate_published") return `首版 Skill 已发布 · ${(payload.commit || "").slice(0, 7)}`;
    if (event.type === "optimization.proposal_ready") return `优化范围待确认 · ${(payload.target_scope || []).join(", ")}`;
    if (event.type === "kernel.phase_changed") return `进入 ${titleCase(payload.phase)} 阶段`;
    if (event.type === "iteration.planned") return payload.hypothesis || "已生成本轮优化假设";
    if (event.type === "case_run.completed") {
      const rate = normalizedRate(payload.pass_rate);
      return rate == null
        ? `${event.case_id || "Case"} · ${displayStatus(payload.status, "已完成")}`
        : `${event.case_id || "Case"} · 通过率 ${Math.round(rate * 100)}% · ${displayStatus(payload.status, "已完成")}`;
    }
    if (event.type === "path_conformance.completed") return `执行路径 ${displayStatus(payload.result?.status, "未测量")} · 覆盖率 ${percent(payload.result?.coverage)}`;
    if (event.type === "trace.captured") return `会话 ${payload.session_id || "—"} · ${payload.session_log || "日志已归档"}`;
    if (event.type.endsWith(".completed")) return `完成 · ${payload.mean_pass_rate == null ? "" : percent(payload.mean_pass_rate)}`;
    if (Array.isArray(payload.reason_codes) && payload.reason_codes.length) return payload.reason_codes.map(displayReason).join("；");
    if (payload.reason) return displayReason(payload.reason);
    if (payload.status) return `${displayStatus(payload.status)}${payload.message ? ` · ${payload.message}` : ""}`;
    return payload.hypothesis || payload.configuration || "事件已记录";
  }

  function renderBlueprint(target, kernel) {
    const blueprint = kernel.blueprint;
    if (!blueprint) return;
    const selected = new Set(blueprint.selected_proposal_ids || []);
    const section = node("section", "task-blueprint");
    section.appendChild(node("h4", "", `能力蓝图 · ${titleCase(blueprint.operation)}`));
    section.appendChild(node("p", "blueprint-summary", blueprint.summary || blueprint.goal));
    const proposals = node("div", "proposal-grid");
    (blueprint.proposals || []).forEach((proposal) => {
      const card = node("article", `proposal-card ${selected.has(proposal.id) ? "selected" : ""}`);
      const top = node("div", "proposal-head");
      top.appendChild(node("strong", "", proposal.title || proposal.id));
      top.appendChild(node("span", "", selected.has(proposal.id) ? "已选中" : proposal.id));
      card.appendChild(top);
      card.appendChild(node("p", "", proposal.user_value || proposal.problem));
      const files = node("div", "file-plan");
      (proposal.planned_files || []).forEach((file) => files.appendChild(node("code", file.action, `${file.action} ${file.path}`)));
      card.appendChild(files);
      card.appendChild(node("small", "", `${(proposal.cases || []).length} 个 Case · ${(proposal.acceptance_criteria || []).length} 条验收标准`));
      proposals.appendChild(card);
    });
    section.appendChild(proposals);
    target.appendChild(section);
  }

  function renderCandidate(target, kernel) {
    const candidates = (kernel.iterations || []).map((item) => item.candidate).filter(Boolean);
    const candidate = candidates[candidates.length - 1];
    if (!candidate) return;
    const section = node("section", "task-candidate");
    section.appendChild(node("h4", "", candidate.initial_build ? "首版候选预览" : "最新优化候选"));
    section.appendChild(node("p", "", candidate.rationale || "候选已完成受控校验。"));
    const files = node("div", "file-plan");
    (candidate.changed_paths || []).forEach((path) => {
      const action = (candidate.created_paths || []).includes(path) ? "create" : "modify";
      const actionLabel = action === "create" ? "新建" : "修改";
      files.appendChild(node("code", action, `${actionLabel} ${path}`));
    });
    section.appendChild(files);
    section.appendChild(node("small", "", `${(candidate.validation || []).length} 项本地校验 · 被测 Skill 指纹 ${(candidate.subject_hash || "").slice(0, 12) || "—"}`));
    target.appendChild(section);
  }

  function renderSessionLogs(target, task, kernel) {
    const logs = (kernel.iterations || []).flatMap((iteration) =>
      (iteration.session_logs || []).map((item) => ({ ...item, iteration: Number(String(iteration.name || "").replace("iteration-", "")) }))
    );
    if (!logs.length) return;
    const section = node("section", "task-logs");
    section.appendChild(node("h4", "", `完整会话日志 · ${logs.length}`));
    logs.forEach((log, index) => {
      const details = node("details", "session-log-row");
      const summary = node("summary", "", `第 ${log.iteration} 轮 · ${displayPurpose(log.purpose)} · ${log.case_id} · ${displayStatus(log.status)}`);
      details.appendChild(summary);
      if (log.artifact) details.appendChild(node("code", "event-artifact", log.artifact));
      if (window.location.protocol !== "file:" && log.artifact) {
        const button = node("button", "log-load", "加载完整 JSON");
        const output = node("pre", "session-log-output", "");
        button.addEventListener("click", async () => {
          button.disabled = true;
          output.textContent = "正在加载…";
          try {
            const response = await fetch(`/api/tasks/${encodeURIComponent(task.id)}/logs/${log.iteration}/${encodeURIComponent(log.purpose)}/${encodeURIComponent(log.case_id)}`);
            const payload = await response.json();
            output.textContent = JSON.stringify(payload, null, 2);
          } catch (error) {
            output.textContent = String(error);
          } finally {
            button.disabled = false;
          }
        });
        details.appendChild(button);
        details.appendChild(output);
      } else if (!log.artifact) {
        details.appendChild(node("p", "empty-state", "这次远端尝试没有形成日志产物，不能打开完整会话；失败原因和重试记录仍会保留。"));
      }
      const attempts = Array.isArray(log.attempts) ? log.attempts : [];
      if (attempts.length > 1) {
        const history = node("p", "log-attempts", `共 ${attempts.length} 次远端尝试：`);
        attempts.forEach((attempt, attemptIndex) => {
          const reason = attempt.retry_reason ? ` · 重试原因：${displayReason(attempt.retry_reason)}` : "";
          history.appendChild(node("span", "tag", `第 ${attempt.attempt_number || attemptIndex + 1} 次 ${displayStatus(attempt.status)}${reason}`));
        });
        details.appendChild(history);
      }
      if (!attempts.length && (log.retry_reason || log.error)) {
        details.appendChild(node("p", "log-attempts", `失败原因：${displayReason(log.retry_reason || log.error)}`));
      }
      section.appendChild(details);
    });
    target.appendChild(section);
  }

  function renderTaskDetail(entry) {
    const target = byId("task-detail");
    clear(target);
    if (!entry) {
      target.appendChild(node("p", "empty-state", "选择一个任务查看评测与迭代时间线。"));
      return;
    }
    const task = entry.task || {};
    const events = entry.events || [];
    const kernel = entry.kernel || {};
    const kernelState = kernel.state || {};
    const resolvedGoal = kernel.design?.goal || kernel.blueprint?.goal || kernel.input?.goal || task.goal;
    const iterations = Array.isArray(kernel.iterations)
      ? kernel.iterations.length
      : events.reduce((value, event) => Math.max(value, Number(event.iteration || 0)), Number(task.current_iteration || 0));
    const batches = (kernel.iterations || []).reduce((count, item) => count + (item.batches || []).length, 0);
    const logs = (kernel.iterations || []).reduce((count, item) => count + (item.session_logs || []).length, 0);
    const header = node("header", "task-detail-head");
    const copy = node("div");
    copy.appendChild(node("p", "eyebrow", `${titleCase(task.scenario)} / ${titleCase(task.status)}`));
    copy.appendChild(node("h3", "", task.skill?.name || task.id));
    copy.appendChild(node("p", "task-goal", resolvedGoal || "—"));
    header.appendChild(copy);
    const stats = node("div", "task-stats");
    [["轮次", iterations], ["批次", batches || "—"], ["日志", logs || "—"], ["事件", events.length]].forEach(([label, value]) => {
      const box = node("div"); box.appendChild(node("strong", "", value)); box.appendChild(node("span", "", label)); stats.appendChild(box);
    });
    header.appendChild(stats);
    target.appendChild(header);
    const standards = node("section", "task-contract");
    standards.appendChild(node("h4", "", `评测契约 · ${titleCase(task.evaluation?.mode || "automatic")} · ${titleCase(kernelState.intent_mode || "legacy")} · ${titleCase(kernelState.phase || task.status)}`));
    const standardsList = node("ul"); list(standardsList, task.standards, "暂无标准"); standards.appendChild(standardsList);
    target.appendChild(standards);
    renderBlueprint(target, kernel);
    renderCandidate(target, kernel);
    renderSessionLogs(target, task, kernel);
    const timeline = node("section", "event-timeline");
    timeline.appendChild(node("h4", "", "自动迭代事件"));
    events.forEach((event) => {
      const row = node("article", `event-row ${String(event.type).replaceAll(".", "-")}`);
      const marker = node("div", "event-marker", String(event.seq).padStart(2, "0"));
      const body = node("div", "event-body");
      const meta = node("div", "event-meta");
      meta.appendChild(node("strong", "", displayEventType(event.type)));
      meta.appendChild(node("span", "", `第 ${event.iteration ?? "—"} 轮 · ${event.case_id || "任务级事件"}`));
      body.appendChild(meta);
      body.appendChild(node("p", "", eventSummary(event)));
      const logPath = event.payload?.session_log || event.payload?.artifact || event.payload?.grading || event.payload?.receipt;
      if (logPath) body.appendChild(node("code", "event-artifact", logPath));
      row.appendChild(marker); row.appendChild(body); timeline.appendChild(row);
    });
    target.appendChild(timeline);
  }

  function selectTask(taskId) {
    state.selectedTask = taskId;
    document.querySelectorAll(".task-card").forEach((item) => item.classList.toggle("selected", item.dataset.taskId === taskId));
    renderTaskDetail(state.tasks.find((item) => item.task?.id === taskId));
  }

  function renderTasks(tasks) {
    state.tasks = Array.isArray(tasks) ? tasks : [];
    const target = byId("task-list");
    clear(target);
    state.tasks.forEach((entry) => {
      const task = entry.task || {};
      const events = entry.events || [];
      const kernelState = entry.kernel?.state || {};
      const resolvedGoal = entry.kernel?.design?.goal || entry.kernel?.blueprint?.goal || entry.kernel?.input?.goal || task.goal;
      const card = node("button", "task-card");
      card.type = "button"; card.dataset.taskId = task.id;
      const top = node("div", "task-card-top");
      top.appendChild(node("span", "task-scenario", titleCase(task.scenario)));
      top.appendChild(node("span", "task-status", titleCase(task.status)));
      card.appendChild(top);
      card.appendChild(node("h4", "", task.skill?.name || task.id));
      card.appendChild(node("p", "", resolvedGoal || "—"));
      card.appendChild(node("small", "", `${events.length} 个事件 · ${titleCase(kernelState.intent_mode || task.evaluation?.mode || "automatic")} · ${titleCase(kernelState.phase || task.status)}`));
      card.addEventListener("click", () => selectTask(task.id));
      target.appendChild(card);
    });
    const preferred = state.tasks.some((item) => item.task?.id === state.selectedTask) ? state.selectedTask : state.tasks[0]?.task?.id;
    renderTaskDetail(preferred ? state.tasks.find((item) => item.task?.id === preferred) : null);
    if (preferred) selectTask(preferred);
  }

  function addMetric(target, label, value) {
    const box = node("div", "metric-box");
    box.appendChild(node("span", "", label));
    box.appendChild(node("strong", "", value));
    target.appendChild(box);
  }

  function selectNode(id) {
    if (!state.graph) return;
    const selected = state.graph.nodes.find((item) => item.id === id);
    if (!selected) return;
    state.selectedNode = id;
    document.querySelectorAll(".tree-node").forEach((item) => {
      item.classList.toggle("selected", item.dataset.nodeId === id);
    });
    byId("node-kind").textContent = titleCase(selected.kind);
    byId("node-title").textContent = selected.title;
    byId("node-status").textContent = titleCase(selected.status);
    byId("node-summary").textContent = selected.summary || "—";
    byId("node-why").textContent = selected.why || "—";
    byId("node-decision").textContent = titleCase(selected.decision || "pending");
    list(byId("node-plan"), selected.plan, "此节点没有 Skill 修改");
    list(byId("node-next"), selected.next_steps, "没有后续动作");
    list(byId("node-evidence"), selected.evidence_refs, "没有证据引用");
    const metrics = selected.metrics || {};
    const target = byId("node-metrics");
    clear(target);
    addMetric(target, "断言通过率", percent(metrics.assertion_pass_rate));
    addMetric(target, "正式通过", typeof metrics.formal_passes === "number" ? `${metrics.formal_passes}/${metrics.cases || 0}` : "—");
    addMetric(target, "严格 JSON", typeof metrics.strict_json_passes === "number" ? `${metrics.strict_json_passes}/${metrics.cases || 0}` : "—");
    addMetric(target, "Token 用量", compactNumber(metrics.tokens));
    addMetric(target, "运行耗时", formatTime(metrics.duration_seconds));
    addMetric(target, "版本绑定", typeof metrics.bindings_verified === "number" ? `${metrics.bindings_verified}/${metrics.cases || 0}` : "—");
  }

  function setView(view) {
    state.view = view;
    document.body.classList.toggle("task-mode", view === "tasks");
    document.querySelectorAll(".view").forEach((item) => item.classList.toggle("active", item.id === `${view}-view`));
    document.querySelectorAll(".view-tab").forEach((item) => {
      const active = item.dataset.view === view;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
      item.tabIndex = active ? 0 : -1;
    });
  }

  function render(graph) {
    state.graph = graph;
    renderHeader(graph);
    renderInput(graph);
    renderTree(graph);
    renderDimensions(graph);
    renderCases(graph);
    renderPathAnalysis(graph);
    renderConfiguration(graph);
    const preferred = state.selectedNode && graph.nodes.some((item) => item.id === state.selectedNode)
      ? state.selectedNode
      : graph.convergence.latest_candidate;
    selectNode(preferred);
  }

  function connectLive() {
    if (window.location.protocol === "file:") {
      byId("connection-label").textContent = "静态快照";
      return;
    }
    if (!("EventSource" in window)) return;
    const stream = new EventSource("/api/events");
    stream.addEventListener("open", () => {
      byId("live-dot").classList.add("live");
      byId("connection-label").textContent = "实时工作区";
    });
    stream.addEventListener("graph.snapshot", (event) => {
      try { render(JSON.parse(event.data)); } catch (_) { /* keep last valid graph */ }
    });
    stream.addEventListener("graph.error", () => {
      byId("connection-label").textContent = "等待有效快照";
    });
    stream.addEventListener("task.snapshot", (event) => {
      try {
        renderTasks(JSON.parse(event.data));
        byId("live-dot").classList.add("live");
        byId("connection-label").textContent = "实时任务中心";
      } catch (_) { /* keep last valid tasks */ }
    });
    stream.onerror = () => {
      byId("live-dot").classList.remove("live");
      byId("connection-label").textContent = "正在重新连接";
    };
  }

  document.querySelectorAll(".view-tab").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  document.querySelectorAll(".phase").forEach((button, index) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".phase").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      document.querySelectorAll(".phase").forEach((item) => item.setAttribute("aria-current", item === button ? "step" : "false"));
      setView("tree");
      if (state.graph) {
        const targets = ["input", "evaluation-design", "v0", state.graph.convergence.latest_candidate, "convergence"];
        selectNode(targets[index]);
      }
    });
  });

  if (window.__ACEVAL_GRAPH__) render(window.__ACEVAL_GRAPH__);
  renderTasks(window.__ACEVAL_TASKS__ || []);
  if ((window.__ACEVAL_TASKS__ || []).length) setView("tasks");
  connectLive();
})();
