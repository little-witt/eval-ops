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

  function titleCase(value) {
    return String(value || "unknown").replaceAll("_", " ").toUpperCase();
  }

  function metricValue(metrics) {
    if (!metrics || typeof metrics !== "object") return "—";
    if (typeof metrics.assertion_pass_rate === "number") return percent(metrics.assertion_pass_rate);
    if (typeof metrics.cases === "number") return `${metrics.cases} CASES`;
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
    byId("run-kicker").textContent = `${titleCase(graph.run.status)} / ${graph.run.repetitions} RUN PER CASE`;
    byId("generated-at").textContent = new Date(graph.generated_at).toLocaleString();
    byId("footer-version").textContent = graph.api_version;
  }

  function renderInput(graph) {
    const skill = graph.input.skill || {};
    setDefinitionList(byId("input-contract"), [
      ["Skill", skill.name],
      ["Ref", skill.ref || "workspace snapshot"],
      ["Current best", skill.current_best],
      ["Case origin", graph.evaluation_design.origin],
      ["Frozen", graph.evaluation_design.frozen ? "yes" : "no"],
    ]);
    const criteria = graph.input.success_criteria || [];
    byId("criteria-count").textContent = String(criteria.length);
    list(byId("criteria-list"), criteria);
    const blockers = graph.convergence.blockers || [];
    byId("blocker-count").textContent = String(blockers.length);
    list(byId("blocker-list"), blockers, "No active blocker");
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
      metric.appendChild(node("span", "", item.commit ? item.commit.slice(0, 7) : "CONTRACT"));
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
      [["BASELINE", dimension.baseline], ["CANDIDATE", dimension.candidate]].forEach(([label, value]) => {
        const row = node("div", `bar-row ${label.toLowerCase()}`);
        row.appendChild(node("span", "", label));
        const track = node("div", "bar-track");
        const fill = node("div", "bar-fill");
        const rate = typeof value.rate === "number" ? value.rate : 0;
        fill.style.width = `${Math.max(0, Math.min(100, rate * 100))}%`;
        track.appendChild(fill);
        row.appendChild(track);
        row.appendChild(node("strong", "", value.measured ? `${value.passed}/${value.measured}` : "N/M"));
        bars.appendChild(row);
      });
      card.appendChild(bars);
      target.appendChild(card);
    });
  }

  function resultPill(payload) {
    let label = payload.status || "unavailable";
    let className = "result-pill";
    if (payload.formal_pass) {
      label = "pass";
      className += " pass";
    } else if (payload.status === "completed") {
      label = `${Math.round((payload.pass_rate || 0) * 5)}/5`;
      className += " fail";
    }
    return node("span", className, label);
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
      const binding = item.candidate.binding_verified || item.baseline.binding_verified;
      const bindingCell = node("td");
      bindingCell.appendChild(node("span", `result-pill ${binding ? "pass" : "fail"}`, binding ? "verified" : "missing"));
      row.appendChild(bindingCell);
      target.appendChild(row);
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
    if (!value || !value.configured) return "not configured";
    if (value.source === "environment") return `$${value.name}`;
    return value.value || "configured";
  }

  function renderConfiguration(graph) {
    const target = byId("configuration-board");
    clear(target);
    const config = graph.configuration || {};
    target.appendChild(configCard("CATX Runtime", [
      ["Profile", config.profile_name || "not configured"],
      ["Endpoint", config.base_url || "—"],
      ["Agent", referenceLabel(config.agent)],
      ["Environment", referenceLabel(config.environment)],
      ["Vaults", String(config.vault_count || 0)],
      ["Credentials", config.credentials_file_configured ? "configured / hidden" : "not configured"],
    ]));
    (config.repositories || []).forEach((repository, index) => {
      target.appendChild(configCard(`Repository ${String(index + 1).padStart(2, "0")}`, [
        ["URL", repository.url],
        ["Mount", repository.mount_path],
        ["Authorization", repository.authorization_configured ? "configured / hidden" : "not configured"],
        ["Source", repository.authorization_source || "—"],
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
    if (event.type === "case_run.completed") return payload.pass_rate == null ? `${event.case_id || "case"} · ${payload.status || "completed"}` : `${event.case_id || "case"} · ${Math.round(payload.pass_rate * 100)}% · ${payload.status || "completed"}`;
    if (event.type === "path_conformance.completed") return `执行路径 ${titleCase(payload.result?.status)} · coverage ${percent(payload.result?.coverage)}`;
    if (event.type === "trace.captured") return `会话 ${payload.session_id || "—"} · ${payload.session_log || "日志已归档"}`;
    if (event.type.endsWith(".completed")) return `完成 · ${payload.mean_pass_rate == null ? "" : percent(payload.mean_pass_rate)}`;
    return payload.hypothesis || payload.reason || payload.configuration || "事件已记录";
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
      top.appendChild(node("span", "", selected.has(proposal.id) ? "SELECTED" : proposal.id));
      card.appendChild(top);
      card.appendChild(node("p", "", proposal.user_value || proposal.problem));
      const files = node("div", "file-plan");
      (proposal.planned_files || []).forEach((file) => files.appendChild(node("code", file.action, `${file.action} ${file.path}`)));
      card.appendChild(files);
      card.appendChild(node("small", "", `${(proposal.cases || []).length} CASES · ${(proposal.acceptance_criteria || []).length} CRITERIA`));
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
      files.appendChild(node("code", action, `${action} ${path}`));
    });
    section.appendChild(files);
    section.appendChild(node("small", "", `${(candidate.validation || []).length} VALIDATIONS · ${(candidate.subject_hash || "").slice(0, 12)}`));
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
      const summary = node("summary", "", `ITER ${log.iteration} · ${log.purpose} · ${log.case_id} · ${log.status}`);
      details.appendChild(summary);
      details.appendChild(node("code", "event-artifact", log.artifact || "—"));
      if (window.location.protocol !== "file:") {
        const button = node("button", "log-load", "加载完整 JSON");
        const output = node("pre", "session-log-output", "");
        button.addEventListener("click", async () => {
          button.disabled = true;
          output.textContent = "Loading…";
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
      meta.appendChild(node("strong", "", event.type));
      meta.appendChild(node("span", "", `ITER ${event.iteration ?? "—"} · ${event.case_id || "TASK"}`));
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
      card.appendChild(node("small", "", `${events.length} EVENTS · ${kernelState.intent_mode || task.evaluation?.mode || "automatic"} · ${kernelState.phase || task.status}`));
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
    list(byId("node-plan"), selected.plan, "No Skill change in this node");
    list(byId("node-next"), selected.next_steps, "No further action");
    list(byId("node-evidence"), selected.evidence_refs, "No evidence reference");
    const metrics = selected.metrics || {};
    const target = byId("node-metrics");
    clear(target);
    addMetric(target, "ASSERTIONS", percent(metrics.assertion_pass_rate));
    addMetric(target, "FORMAL PASS", typeof metrics.formal_passes === "number" ? `${metrics.formal_passes}/${metrics.cases || 0}` : "—");
    addMetric(target, "STRICT JSON", typeof metrics.strict_json_passes === "number" ? `${metrics.strict_json_passes}/${metrics.cases || 0}` : "—");
    addMetric(target, "TOKENS", compactNumber(metrics.tokens));
    addMetric(target, "TIME", formatTime(metrics.duration_seconds));
    addMetric(target, "BINDINGS", typeof metrics.bindings_verified === "number" ? `${metrics.bindings_verified}/${metrics.cases || 0}` : "—");
  }

  function setView(view) {
    state.view = view;
    document.body.classList.toggle("task-mode", view === "tasks");
    document.querySelectorAll(".view").forEach((item) => item.classList.toggle("active", item.id === `${view}-view`));
    document.querySelectorAll(".view-tab").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  }

  function render(graph) {
    state.graph = graph;
    renderHeader(graph);
    renderInput(graph);
    renderTree(graph);
    renderDimensions(graph);
    renderCases(graph);
    renderConfiguration(graph);
    const preferred = state.selectedNode && graph.nodes.some((item) => item.id === state.selectedNode)
      ? state.selectedNode
      : graph.convergence.latest_candidate;
    selectNode(preferred);
  }

  function connectLive() {
    if (window.location.protocol === "file:") {
      byId("connection-label").textContent = "STATIC SNAPSHOT";
      return;
    }
    if (!("EventSource" in window)) return;
    const stream = new EventSource("/api/events");
    stream.addEventListener("open", () => {
      byId("live-dot").classList.add("live");
      byId("connection-label").textContent = "LIVE WORKSPACE";
    });
    stream.addEventListener("graph.snapshot", (event) => {
      try { render(JSON.parse(event.data)); } catch (_) { /* keep last valid graph */ }
    });
    stream.addEventListener("graph.error", () => {
      byId("connection-label").textContent = "WAITING FOR VALID SNAPSHOT";
    });
    stream.addEventListener("task.snapshot", (event) => {
      try {
        renderTasks(JSON.parse(event.data));
        byId("live-dot").classList.add("live");
        byId("connection-label").textContent = "LIVE TASK CENTER";
      } catch (_) { /* keep last valid tasks */ }
    });
    stream.onerror = () => {
      byId("live-dot").classList.remove("live");
      byId("connection-label").textContent = "RECONNECTING";
    };
  }

  document.querySelectorAll(".view-tab").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  document.querySelectorAll(".phase").forEach((button, index) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".phase").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
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
