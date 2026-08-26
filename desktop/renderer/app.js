(() => {
  "use strict";

  const app = document.querySelector("#app");
  const toastNode = document.querySelector("#toast");
  const forge = window.forge;
  const state = {
    bootstrap: null, tasks: [], selectedTaskId: null, snapshot: null,
    view: "empty", tab: "overview", modal: null, busy: null,
    secrets: [], extensions: [], operationSeen: new Map(),
    createCases: [], designImage: null, artifactImages: {},
  };

  const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" }[char]));
  const json = (value) => esc(JSON.stringify(value, null, 2));
  const short = (value, count = 10) => value ? String(value).slice(0, count) : "—";
  const formatTime = (value) => { try { return new Intl.DateTimeFormat("zh-CN", { hour:"2-digit", minute:"2-digit", second:"2-digit" }).format(new Date(value)); } catch { return "—"; } };
  const formatDate = (value) => { try { return new Intl.DateTimeFormat("zh-CN", { month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit" }).format(new Date(value)); } catch { return "—"; } };
  const phaseLabel = {
    created:"准备设计", blueprint_ready:"能力确认", discovery_ready:"探索确认", ready_to_build:"生成中",
    initial_candidate_ready:"候选确认", candidate_ready:"待发布", design_ready:"设计就绪",
    remote_running:"线上评测", baseline_running:"基线评测", verification_running:"稳定性复验",
    remote_collected:"日志已回收", baseline_collected:"基线已回收", verification_collected:"复验已回收",
    verification_ready:"等待复验", awaiting_confirmation:"待确认优化", ready_to_optimize:"准备优化",
    needs_evidence:"证据不足", blocked:"已阻塞", converged:"已收敛",
  };
  const eventLabel = {
    "kernel.created":"任务输入已冻结", "evaluation.design_generated":"评测草案已建立",
    "evaluation.evalpack_ready":"EvalPack 已生成或复用", "evaluation.case_path_ready":"Case 与评测路径已就绪",
    "evaluation.design_review_required":"EvalPack 需要校准或审阅",
    "evaluation.design_compiled":"EvalPack、Case 与路径已编译", "kernel.phase_changed":"核状态流转",
    "harness.blueprint_ready":"能力蓝图等待确认", "user.capability_blueprint_approved":"能力蓝图已批准",
    "case_run.started":"评测会话已创建", "case_run.completed":"完整会话日志已回收", "case_run.failed":"评测会话失败",
    "analysis.completed":"本地分析 Agent 完成归因", "optimization.proposal_ready":"优化建议等待确认",
    "user.change_scope_approved":"用户批准优化范围", "candidate.created":"多文件候选版本已生成",
    "candidate.published":"候选版本已发布并进入复评", "candidate.promoted":"候选通过门禁并晋升",
    "candidate.rejected":"候选未通过门禁，Champion 已保护", "decision.recorded":"收敛决策已记录",
  };

  function toast(message, error = false) {
    toastNode.textContent = message;
    toastNode.className = `toast show${error ? " error" : ""}`;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { toastNode.className = "toast"; }, 3600);
  }

  async function call(method, params = {}) {
    try { return await forge.rpc(method, params); }
    catch (error) { toast(error.message || String(error), true); throw error; }
  }

  function phaseClass(task) {
    if (task.phase === "converged") return "pass";
    if (["blocked", "needs_evidence", "awaiting_confirmation"].includes(task.phase)) return "warn";
    return "";
  }

  function shell(content) {
    return `<div class="shell">
      <aside class="rail">
        <button class="rail-logo" data-action="home" aria-label="任务中心">F</button>
        <nav>
          <button class="rail-button ${state.view !== "settings" ? "active" : ""}" data-action="home" title="任务中心"><svg viewBox="0 0 24 24"><path d="M5 6.5h14M5 12h9M5 17.5h11"/></svg></button>
          <button class="rail-button" data-action="new-task" title="创建任务"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button>
          <button class="rail-button" data-action="settings" title="配置与环境"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.6-2-3.4-2.5 1A7 7 0 0 0 14.8 6L14.5 3h-5L9.2 6A7 7 0 0 0 7.6 7L5 6 3 9.4 5.1 11a7 7 0 0 0 0 2L3 14.6 5 18l2.6-1A7 7 0 0 0 9.2 18l.3 3h5l.3-3a7 7 0 0 0 1.6-1L19 18l2-3.4-2.1-1.6a7 7 0 0 0 .1-1Z"/></svg></button>
        </nav>
        <span class="rail-health" title="本地 Kernel 已连接"></span>
      </aside>
      ${sidebar()}
      <section class="workspace">${content}</section>
      ${state.modal ? modalView() : ""}
      ${state.busy ? `<div class="busy"><div class="busy-card"><div class="spinner"></div><p>${esc(state.busy)}</p></div></div>` : ""}
    </div>`;
  }

  function sidebar() {
    return `<aside class="sidebar">
      <header class="side-head"><span class="eyebrow">Skill evolution studio</span><h1>任务中心</h1><button class="primary-button new-task" data-action="new-task"><span>新建评测与迭代</span><b>＋</b></button></header>
      <div class="task-filter"><button class="active">全部 ${state.tasks.length}</button><button>运行中 ${state.tasks.filter(t => t.active_operation).length}</button><button>待确认 ${state.tasks.filter(t => t.phase === "awaiting_confirmation").length}</button></div>
      <div class="task-list">${state.tasks.length ? state.tasks.map(task => `<button class="task-item ${task.id === state.selectedTaskId ? "active" : ""}" data-action="select-task" data-id="${esc(task.id)}">
        <span class="task-item-head"><strong>${esc(task.skill_name || task.id)}</strong><em class="phase-pill ${phaseClass(task)}">${esc(phaseLabel[task.phase] || task.status || "未知")}</em></span>
        <p>R${Number(task.current_iteration || 0) + 1} · ${esc(formatDate(task.updated_at))} · ${esc(short(task.champion_commit, 8))}</p>
      </button>`).join("") : `<div class="task-item"><p>还没有任务。先创建一次真实评测。</p></div>`}</div>
      <footer class="side-foot"><div class="health-row"><i></i><span>Kernel ${esc(state.bootstrap?.version || "—")} · ${state.bootstrap?.d2c_health?.ready ? "D2C Ready" : "D2C 待配置"}</span></div></footer>
    </aside>`;
  }

  function emptyView() {
    return `<main class="view empty-view"><div class="monogram">F</div><h2>让 Skill 用证据变好</h2><p>输入目标或 Case，系统会生成可审阅的评测设计，批量运行真实 Agent，会话日志逐路径回收，再由确定性门禁控制优化与收敛。</p><button class="primary-button" data-action="new-task">创建第一个任务</button></main>`;
  }

  function stageIndex(phase) {
    const map = {
      created:0, blueprint_ready:1, discovery_ready:1, ready_to_build:1, initial_candidate_ready:1,
      design_ready:2, remote_running:3, baseline_running:3, verification_running:3,
      remote_collected:4, baseline_collected:4, verification_collected:4, verification_ready:3,
      awaiting_confirmation:4, needs_evidence:4, ready_to_optimize:5, candidate_ready:5, blocked:4, converged:6,
    };
    return map[phase] ?? 0;
  }

  function stageStrip(phase) {
    const stages = [["输入","目标冻结"],["EvalPack","生成 / 复用"],["Case / Path","可审阅设计"],["线上评测","并行会话"],["证据分析","归因与建议"],["优化候选","受控多文件"],["收敛","Champion"]];
    const current = stageIndex(phase);
    return `<div class="phase-strip">${stages.map((item, index) => `<div class="stage ${index < current ? "done" : index === current ? "active" : ""}"><b>${index < current ? "✓" : index + 1}</b><strong>${item[0]}</strong><small>${item[1]}</small></div>`).join("")}</div>`;
  }

  function eventSummary(event) {
    const p = event.payload || {};
    if (event.type === "kernel.phase_changed") return `进入 ${phaseLabel[p.phase] || p.phase || "下一阶段"}`;
    if (event.type === "evaluation.design_compiled") return `${p.case_count || 0} 个 Case，${p.generated_case_count || 0} 个自动补充；EvalPack ${p.pack_source || "generated"}`;
    if (event.type === "evaluation.evalpack_ready") return `${p.pack_source || "generated"} · ${p.lifecycle || "ready"} · ${short(p.signature, 18)}`;
    if (event.type === "evaluation.case_path_ready") return `${event.case_id || p.case_id} · ${p.step_count || 0} 个语义检查点 · ${p.source || "automatic"}`;
    if (event.type === "evaluation.design_review_required") return `${p.evalpack_status || "review_required"} · 可探索运行，未校准 Case 不能单独授权修改 Skill`;
    if (event.type === "case_run.started") return `${event.case_id || "Case"} · ${p.session_id ? `会话 ${short(p.session_id, 14)}` : p.provider || "remote"}`;
    if (event.type === "case_run.completed") return `${event.case_id || "Case"} · ${p.status || "日志与证据已持久化"}`;
    if (event.type === "analysis.completed") return `稳定通过 ${p.stable_pass_case_ids?.length || 0}，失败 ${p.failed_case_ids?.length || 0}；下一步 ${p.next_action || "—"}`;
    if (event.type === "candidate.rejected") return `未通过成对门禁；${p.restoration_status === "restored" ? "已自动恢复 Champion 内容" : "Champion 保持不变"}`;
    if (event.type === "candidate.promoted") return `没有硬回归且达到最小收益，候选晋升为 Champion`;
    return Object.keys(p).length ? JSON.stringify(p).slice(0, 220) : "事件已写入不可变任务时间线";
  }

  function traceView(snapshot) {
    const events = (snapshot.events || []).slice(-60).reverse();
    return `<div class="section-head"><div><span class="eyebrow">Live audit trail</span><h3>评测与迭代路径</h3></div><span>${events.length} / ${snapshot.events?.length || 0} EVENTS</span></div>
      <div class="trace-list">${events.map((event, index) => {
        const failed = event.type.includes("failed") || event.type.includes("rejected");
        const passed = event.type.includes("completed") || event.type.includes("promoted") || event.type === "decision.recorded";
        const working = index === 0 && snapshot.state.phase !== "converged";
        return `<article class="trace-event ${failed ? "failed" : passed ? "passed" : working ? "working" : ""}"><span class="trace-dot">${failed ? "!" : passed ? "✓" : event.seq}</span><div class="trace-card"><header class="trace-card-head"><strong>${esc(eventLabel[event.type] || event.type)}</strong><time>${esc(formatTime(event.timestamp))} · #${event.seq}</time></header><p>${esc(eventSummary(event))}</p><div class="trace-tags"><span class="tag">R${Number(event.iteration || 0) + 1}</span>${event.case_id ? `<span class="tag">${esc(event.case_id)}</span>` : ""}${event.run_id ? `<span class="tag">${esc(event.run_id)}</span>` : ""}</div></div></article>`;
      }).join("") || `<p>任务时间线正在初始化。</p>`}</div>`;
  }

  function overviewTab(snapshot) {
    const decision = snapshot.decision || {};
    const health = decision.evidence_health || {};
    const comparison = decision.candidate_comparison;
    const convergence = decision.convergence || {};
    return `<div class="metric-grid">
      <div class="metric"><small>当前轮次</small><strong>R${Number(snapshot.state.iteration || 0) + 1}</strong></div>
      <div class="metric"><small>稳定通过</small><strong class="good">${decision.stable_pass_case_ids?.length ?? "—"}</strong></div>
      <div class="metric"><small>有效 Attempt</small><strong>${health.valid_attempts ?? "—"}</strong></div>
      <div class="metric"><small>失败 / 波动</small><strong class="${decision.failed_case_ids?.length ? "bad" : ""}">${decision.failed_case_ids?.length ?? "—"}</strong></div>
    </div>
    <section class="panel-section"><h4>当前控制面</h4><p>${esc(phaseLabel[snapshot.state.phase] || snapshot.state.phase)}。模型负责语义判断与修改提案；证据有效性、硬回归、候选晋升和停止条件由 Kernel 判定。</p></section>
    <section class="panel-section"><h4>版本关系</h4><code class="code-line">Champion ${esc(short(snapshot.state.champion_commit, 16))}</code><code class="code-line">Challenger ${esc(short(snapshot.state.challenger_commit, 16))}</code>${comparison ? `<p class="evidence-status top-gap ${comparison.accepted ? "" : "bad"}"><i></i>${comparison.accepted ? "达到最小收益，无稳定通过回归" : `未晋升：${(comparison.reasons || []).join(", ")}`}</p>` : ""}</section>
    <section class="panel-section"><h4>收敛状态</h4><p>${convergence.converged ? `已停止：${esc(convergence.reason || "target_met")}` : `仍有 ${(convergence.remaining_failed_case_ids || []).length} 个失败路径；最多 ${convergence.max_rounds || "—"} 轮。`}</p></section>
    <section class="panel-section"><h4>Token 经济</h4><p>本轮本地分析模型调用 ${decision.token_economy?.model_calls ?? 0} 次；没有发送完整日志，只发送压缩证据约 ${decision.token_economy?.estimated_prompt_tokens ?? 0} tokens。</p></section>`;
  }

  function casesTab(snapshot) {
    const design = snapshot.design || {};
    const aggregates = Object.fromEntries((snapshot.decision?.case_aggregates || []).map(item => [item.case_id, item]));
    return `<section class="panel-section"><h4>Case 与期望路径 · ${design.cases?.length || 0}</h4><p>用户 Case 与自动补充 Case 使用同一套可审阅路径；推荐步骤不是脆弱的精确工具序列。</p></section>${(design.cases || []).map(item => {
      const path = design.execution_paths?.[item.id]; const aggregate = aggregates[item.id];
      const dimensions = aggregate?.attempts?.at(-1)?.dimensions || [];
      return `<article class="case-card"><header><strong>${esc(item.id)}</strong><span class="evidence-status ${aggregate?.status === "fail" ? "bad" : ""}"><i></i>${esc(aggregate?.status || "待运行")}</span></header><p>${esc(item.prompt)}</p>${aggregate ? `<div class="trace-tags"><span class="tag">pass^k ${aggregate.pass_power_k == null ? "—" : Number(aggregate.pass_power_k).toFixed(2)}</span><span class="tag">${aggregate.evaluable_attempt_count}/${aggregate.attempt_count} valid</span>${dimensions.map(dimension => `<span class="tag dimension-${esc(dimension.status)}">${esc(dimension.dimension)} · ${esc(dimension.status)}</span>`).join("")}</div>` : ""}<ul class="path-list">${(path?.steps || []).map((step, index) => `<li><i>${index + 1}</i><span>${esc(step.label)}</span><em>${esc(step.kind)}</em></li>`).join("") || "<li>路径尚未生成</li>"}</ul></article>`;
    }).join("") || `<p>EvalPack 编译后会在这里流式出现 Case 和路径。</p>`}`;
  }

  function logsTab(snapshot) {
    const rows = (snapshot.iterations || []).flatMap(iteration => (iteration.session_logs || []).map(log => ({ ...log, iteration:Number(iteration.name.replace("iteration-", "")) })));
    return `<section class="panel-section"><h4>完整会话日志 · ${rows.length}</h4><p>每条日志固定关联 Case、轮次、目的和评测路径，不用摘要替代原始证据。</p></section>${rows.map(log => { const path = snapshot.design?.execution_paths?.[log.case_id]; return `<article class="log-card"><header><strong>${esc(log.case_id)}</strong><span class="phase-pill ${log.status === "completed" ? "pass" : ""}">${esc(log.status)}</span></header><p>R${log.iteration + 1} · ${esc(log.purpose)} · ${esc(short(log.session_id, 18))}</p><div class="trace-tags">${(path?.steps || []).map(step => `<span class="tag">${esc(step.label)}</span>`).join("") || `<span class="tag">路径未生成</span>`}</div><button class="text-button top-gap" data-action="open-log" data-task="${esc(snapshot.task.id)}" data-iteration="${log.iteration}" data-purpose="${esc(log.purpose)}" data-case="${esc(log.case_id)}">查看消息、工具调用与返回证据 →</button></article>`; }).join("") || `<p>会话结束并回收后，完整日志会出现在这里。</p>`}`;
  }

  function decisionTab(snapshot) {
    const decision = snapshot.decision;
    if (!decision) return `<section class="panel-section"><h4>分析尚未开始</h4><p>多路日志回收完成后，本地 Agent 才会进行一次跨 Case 汇总；确定性事实会覆盖模型猜测。</p></section>`;
    const graph = decision.diagnosis_graph || {};
    return `<div class="decision-hero"><small>R${Number(snapshot.state.iteration || 0) + 1} · CROSS-CASE SYNTHESIS</small><h4>${decision.failed_case_ids?.length ? `发现 ${decision.failed_case_ids.length} 条未达标路径` : "所有目标路径满足稳定性门禁"}</h4><p>${decision.intervention_blocker ? esc(decision.intervention_blocker) : "问题事实、根因假设与修改提案分层展示；修改前需要你的明确批准。"}</p></div>
      <section class="panel-section"><h4>问题簇</h4>${(graph.clusters || []).map(cluster => `<article class="case-card"><header><strong>${esc(cluster.id)}</strong><span class="phase-pill ${cluster.skill_change_authorized ? "warn" : ""}">${cluster.skill_change_authorized ? "允许改 Skill" : "需补证据"}</span></header><p>${esc(cluster.root_cause_hypothesis)}</p><div class="trace-tags">${(cluster.case_ids || []).map(id => `<span class="tag">${esc(id)}</span>`).join("")}</div></article>`).join("") || `<p>没有失败簇。</p>`}</section>
      ${snapshot.state.phase === "awaiting_confirmation" ? `<section class="panel-section"><h4>选择本轮修改提案</h4><form id="decision-form">${(graph.proposals || []).map(proposal => `<label class="proposal-card"><input type="checkbox" name="proposal" value="${esc(proposal.id)}" checked/><span><b>${esc(proposal.change)}</b><p>${esc(proposal.why)}</p><code>${esc(proposal.target)}</code></span></label>`).join("")}<textarea class="feedback" name="feedback" placeholder="可选：补充修改约束、范围或反例。该意见会进入候选生成上下文。"></textarea><div class="decision-actions"><button class="primary-button" type="submit">批准所选范围并继续</button><button class="danger-button" type="button" data-action="reject-decision">拒绝并停止</button></div></form></section>` : ""}
      <section class="panel-section"><h4>候选对比</h4><p>${decision.candidate_comparison ? `成对均值变化 ${Number(decision.candidate_comparison.paired_mean_delta || 0).toFixed(3)}；硬回归 ${(decision.candidate_comparison.hard_regression_case_ids || []).length}。` : "首轮建立 Champion 基线后，后续候选才进行成对比较。"}</p></section>`;
  }

  function d2cTab(snapshot) {
    const events = (snapshot.events || []).filter(event => event.payload?.provider === "local-browser" && event.type === "case_run.completed");
    const latest = events.at(-1);
    const receiptPath = latest?.payload?.receipt;
    const base = receiptPath ? receiptPath.slice(0, receiptPath.lastIndexOf("/")) : null;
    const screenshot = base && latest.payload.artifacts?.["screenshot.png"] ? `${base}/screenshot.png` : null;
    const diff = base && latest.payload.artifacts?.["visual-diff.png"] ? `${base}/visual-diff.png` : null;
    return `<section class="panel-section"><h4>D2C 独立浏览器验证</h4><p>预览窗口可加载受控插件；真正评分使用无插件、临时 Profile 的独立 Chrome Worker，并记录 DOM、网络、控制台和像素差异。</p></section>
      <form id="d2c-form"><div class="field"><label>本地页面 URL</label><div class="inline-input"><input name="url" value="http://127.0.0.1:3000/" required/><button type="button" data-action="open-preview">打开预览</button></div></div><div class="fields"><div class="field"><label>Case ID</label><input name="case_id" value="d2c-visual-1" required/></div><div class="field"><label>期望标题（可选）</label><input name="expected_title"/></div><div class="field full"><label>交互动作 JSON（可选）</label><textarea name="actions" placeholder='[{"type":"click","selector":"[data-test=submit]"}]'></textarea></div><div class="field full"><label>设计稿视觉 Oracle</label><div class="inline-input"><input name="design_path" value="${esc(state.designImage?.path || "")}" readonly/><button type="button" data-action="select-design">选择设计稿</button></div><small>${state.designImage ? esc(state.designImage.sha256) : "不选设计稿时只验证运行、标题、控制台和网络证据"}</small></div></div><button class="primary-button" type="submit" ${state.bootstrap?.d2c_health?.ready ? "" : "disabled"}>运行独立浏览器验证</button></form>
      <section class="panel-section"><h4>最近一次结果</h4><div class="comparison-board"><figure>${state.designImage?.data_url ? `<img src="${esc(state.designImage.data_url)}" alt="设计稿"/>` : `<div class="empty-artifact">未选择设计稿</div>`}<figcaption>Reference · Oracle</figcaption></figure><figure>${screenshot ? artifactImage(screenshot, "运行截图") : `<div class="empty-artifact">等待运行截图</div>`}<figcaption>Actual · Candidate</figcaption></figure><figure>${diff ? artifactImage(diff, "像素差异") : `<div class="empty-artifact">选择设计稿后显示差异图</div>`}<figcaption>Visual diff</figcaption></figure></div>${latest ? `<p class="top-gap">${esc(latest.payload.status)} · ${esc(latest.case_id)}</p>` : ""}</section>`;
  }

  function artifactImage(path, alt) {
    const data = state.artifactImages[path];
    if (!data) setTimeout(() => loadArtifact(path), 0);
    return data ? `<img src="${esc(data)}" alt="${esc(alt)}"/>` : `<div class="empty-artifact">正在读取产物…</div>`;
  }

  async function loadArtifact(path) {
    if (state.artifactImages[path] || state.artifactImages[path] === null) return;
    state.artifactImages[path] = null;
    try { state.artifactImages[path] = (await forge.readArtifact(path)).data_url; render(); }
    catch { delete state.artifactImages[path]; }
  }

  function inspector(snapshot) {
    const tabs = [["overview","总览"],["cases","Case / Path"],["logs","会话日志"],["decision","分析 / 决策"],["d2c","D2C"]];
    const content = { overview:overviewTab, cases:casesTab, logs:logsTab, decision:decisionTab, d2c:d2cTab }[state.tab](snapshot);
    return `<aside class="inspector"><nav class="tabs">${tabs.map(([id,label]) => `<button class="${state.tab === id ? "active" : ""}" data-action="tab" data-tab="${id}">${label}</button>`).join("")}</nav><div class="inspector-body">${content}</div></aside>`;
  }

  function detailView() {
    const snapshot = state.snapshot;
    if (!snapshot) return `<main class="boot-screen"><p>正在加载任务详情…</p></main>`;
    const task = snapshot.task; const phase = snapshot.state.phase; const active = state.tasks.find(item => item.id === task.id)?.active_operation;
    const atGate = ["created","design_ready","remote_collected","verification_collected","ready_to_optimize","candidate_ready"].includes(phase);
    return `<main class="view"><header class="detail-head"><div class="headline-row"><div><span class="eyebrow">${esc(task.scenario)} · ${esc(task.id)}</span><h2>${esc(task.skill?.name)}</h2><p>${esc(task.goal)}</p></div><div class="head-actions"><button class="ghost-button" data-action="refresh">刷新</button><button class="primary-button" data-action="run-task" ${active || !atGate ? "disabled" : ""}>${active ? "正在运行" : phase === "converged" ? "已收敛" : "继续自动执行"}</button></div></div>${stageStrip(phase)}</header><div class="detail-grid"><section class="trace-pane">${traceView(snapshot)}</section>${inspector(snapshot)}</div></main>`;
  }

  function createView() {
    return `<main class="view create-view"><header class="create-head"><div><span class="eyebrow">New evolution task</span><h2>创建一次可追溯的 Skill 升级</h2><p>最少只需要 Skill、目标和运行环境；Case 与 EvalPack 可由系统自动补齐。</p></div><button class="ghost-button" data-action="home">取消</button></header><form id="create-form"><div class="create-grid"><div class="form-stack">
      <section class="form-section"><div class="form-title"><b>01</b><div><h3>候选 Skill</h3><p>仓库、分支和本地工作副本</p></div></div><div class="fields"><div class="field full"><label>Skill 名称</label><input name="skill_name" required placeholder="frontend-code-reviewer"/></div><div class="field full"><label>Skill 仓库 SSH</label><input name="skill_ssh" required placeholder="ssh://git@git.example.com/org/skill.git"/></div><div class="field"><label>分支</label><input name="skill_branch" required value="master"/></div><div class="field"><label>本地路径（可空，自动检出）</label><div class="inline-input"><input name="skill_local"/><button type="button" data-action="browse-dir" data-target="skill_local">选择</button></div></div></div></section>
      <section class="form-section"><div class="form-title"><b>02</b><div><h3>目标、标准与 Case</h3><p>支持修复、优化、扩展、探索和从零生成</p></div></div><div class="fields"><div class="field"><label>操作模式</label><select name="operation"><option value="auto">自动识别</option><option value="repair">修复</option><option value="tune">优化</option><option value="extend">增加功能</option><option value="discover">探索提升</option><option value="create">从零生成</option></select></div><div class="field"><label>目标</label><input name="goal" placeholder="让评审结果更准确、可定位"/></div><div class="field full"><label>成功标准（每行一条）</label><textarea name="standards" placeholder="覆盖关键缺陷\n严格遵循 Skill 的关键执行步骤\n不制造无证据结论"></textarea></div><div class="field full"><label>能力诉求（每行一条，可选）</label><textarea name="capabilities" placeholder="新增 Java 服务端并发问题评审能力"></textarea></div><div class="field full"><label>自定义 EvalPack（可选）</label><div class="inline-input"><input name="evalpack_path" placeholder="留空时系统自动生成或复用"/><button type="button" data-action="browse-dir" data-target="evalpack_path">选择目录</button></div><small>自定义入口不会关闭自动补充；用户 Case 仍会合并进评测设计。</small></div></div><div class="case-editor"><strong>用户 Case（可选）</strong><div id="case-rows">${state.createCases.map(caseRow).join("")}</div><button class="add-case" type="button" data-action="add-case">＋ 添加 Case</button></div></section>
      <section class="form-section"><div class="form-title"><b>03</b><div><h3>源码 / Fixture 仓库</h3><p>代码评审、D2C 等场景可挂载第二仓库</p></div></div><div class="fields"><div class="field full"><label>源码仓库 SSH（可选）</label><input name="code_ssh" placeholder="ssh://git@git.example.com/org/fixture-lab.git"/></div><div class="field"><label>分支</label><input name="code_branch" value="master"/></div><div class="field"><label>本地路径</label><div class="inline-input"><input name="code_local"/><button type="button" data-action="browse-dir" data-target="code_local">选择</button></div></div></div></section>
      <section class="form-section"><div class="form-title"><b>04</b><div><h3>本地分析 Agent 与远程 Agent</h3><p>密钥只从系统安全存储注入，任务配置只保存环境变量名</p></div></div><div class="fields"><div class="field full"><label>本地模型桥命令（JSON argv）</label><input name="model_command" required placeholder='["my-model-bridge","--json"]'/></div><div class="field"><label>模型标识</label><input name="model_id" required placeholder="analysis-model"/></div><div class="field"><label>远程 Agent Profile JSON</label><div class="inline-input"><input name="remote_profile" required/><button type="button" data-action="browse-file" data-target="remote_profile">选择</button></div></div></div></section>
      </div><aside class="launch-panel"><span class="eyebrow">Automatic route</span><h3>创建后立即进入详情</h3><ol class="launch-flow"><li><b>1</b><span><strong>生成 / 复用 EvalPack</strong><small>用户无感，可随时自定义</small></span></li><li><b>2</b><span><strong>补齐 Case 与路径</strong><small>重要产出直接展示</small></span></li><li><b>3</b><span><strong>批量真实评测</strong><small>一条路径对应一条完整日志</small></span></li><li><b>4</b><span><strong>跨 Case 决策</strong><small>修改前停在用户确认门</small></span></li></ol><button class="primary-button" type="submit">创建并开始生成 →</button></aside></div></form></main>`;
  }

  function caseRow(item = {}, index) {
    return `<div class="case-row" data-index="${index}"><input name="case_id" value="${esc(item.id || "")}" placeholder="case-id"/><input name="case_prompt" value="${esc(item.prompt || "")}" placeholder="用户输入"/><input name="case_expected" value="${esc(item.expected || "")}" placeholder="预期 JSON / 文本（可空）"/><button type="button" data-action="remove-case" data-index="${index}">×</button></div>`;
  }

  function modalView() {
    if (state.modal.type === "log") return `<div class="modal-backdrop"><section class="modal wide"><header class="modal-head"><div><span class="eyebrow">Immutable session evidence</span><h3>${esc(state.modal.title)}</h3></div><button data-action="close-modal">×</button></header><pre class="log-viewer">${json(state.modal.value)}</pre></section></div>`;
    if (state.modal.type === "settings") return settingsModal();
    return "";
  }

  function settingsModal() {
    const secretNames = ["CATX_API_KEY","USER_MIS_ID","CATX_AGENT_ID","CATX_ENV_ID","CATX_REPOSITORY_AUTHORIZATION_TOKEN","OPENAI_API_KEY","ANTHROPIC_API_KEY"];
    const health = state.bootstrap?.environment_health || {};
    return `<div class="modal-backdrop"><section class="modal"><header class="modal-head"><div><span class="eyebrow">Local configuration</span><h3>安全配置与运行环境</h3></div><button data-action="close-modal">×</button></header><section class="panel-section"><h4>环境体检</h4><div class="health-grid"><div><i class="${health.kernel?.ready ? "ok" : "bad"}"></i><span>Kernel</span><code>${esc(health.kernel?.bundled ? `内置 ${health.kernel.version}` : `开发模式 ${health.kernel?.version || "—"}`)}</code></div><div><i class="${health.git?.ready ? "ok" : "bad"}"></i><span>Git</span><code>${esc(health.git?.version || health.git?.error || "未发现")}</code></div><div><i class="${health.d2c?.ready ? "ok" : "bad"}"></i><span>D2C Worker</span><code>${esc(health.d2c?.ready ? `${health.d2c.chrome} · ${health.d2c.node}` : (health.d2c?.missing || []).join("；"))}</code></div></div></section><form id="secret-form"><div class="settings-grid">${secretNames.map(name => `<div class="field"><label>${name} ${state.secrets.includes(name) ? "· 已配置" : ""}</label><div class="secret-field"><input type="password" name="${name}" autocomplete="off" placeholder="${state.secrets.includes(name) ? "输入新值以替换" : "尚未配置"}"/>${state.secrets.includes(name) ? `<button class="danger-button" type="button" data-action="clear-secret" data-name="${name}">清除</button>` : ""}</div></div>`).join("")}</div><button class="primary-button action-gap" type="submit">保存到系统安全存储</button></form><section class="panel-section"><h4>预览浏览器插件</h4><p>只接受解压目录、固定 SHA-256 和权限白名单；插件仅进入预览 Profile，D2C 判定 Worker 始终禁用插件。</p>${state.extensions.map(item => `<div class="plugin-row"><span>${esc(item.name)} · ${esc(item.version)}</span><code>${esc(short(item.sha256, 12))}</code></div>`).join("") || `<p>没有安装受控插件。</p>`}<button class="ghost-button action-gap dark-ghost" data-action="install-extension" type="button">安装受控插件</button></section></section></div>`;
  }

  function render() {
    const content = state.view === "create" ? createView() : state.view === "detail" ? detailView() : emptyView();
    app.innerHTML = shell(content);
  }

  async function refreshTasks(loadSelected = true) {
    const result = await call("tasks.list");
    state.tasks = result.tasks || [];
    if (state.selectedTaskId && !state.tasks.some(item => item.id === state.selectedTaskId)) state.selectedTaskId = null;
    if (loadSelected && state.selectedTaskId) state.snapshot = await call("tasks.get", { task_id: state.selectedTaskId });
    render();
    for (const task of state.tasks) {
      const operation = task.active_operation;
      if (!operation) continue;
      const old = state.operationSeen.get(operation.id);
      if (operation.status !== old) state.operationSeen.set(operation.id, operation.status);
    }
  }

  async function selectTask(taskId) {
    state.selectedTaskId = taskId; state.view = "detail"; state.snapshot = null; render();
    await refreshTasks(true);
  }

  function syncCasesFromDom() {
    state.createCases = [...document.querySelectorAll(".case-row")].map(row => ({
      id: row.querySelector('[name="case_id"]').value,
      prompt: row.querySelector('[name="case_prompt"]').value,
      expected: row.querySelector('[name="case_expected"]').value,
    }));
  }

  function parseLines(value) { return String(value || "").split(/\r?\n/).map(item => item.trim()).filter(Boolean); }
  function parseExpected(value) { const text = String(value || "").trim(); if (!text) return { present:false }; try { return { present:true, value:JSON.parse(text) }; } catch { return { present:true, value:text }; } }

  async function createTask(form) {
    const data = new FormData(form); syncCasesFromDom();
    let modelCommand; try { modelCommand = JSON.parse(data.get("model_command")); } catch { throw new Error("本地模型桥命令必须是 JSON argv 数组"); }
    if (!Array.isArray(modelCommand) || !modelCommand.length) throw new Error("本地模型桥命令不能为空");
    const caseValues = state.createCases.filter(item => item.id.trim() || item.prompt.trim()).map(item => {
      if (!item.id.trim() || !item.prompt.trim()) throw new Error("每个 Case 都需要稳定 ID 和用户输入");
      const expected = parseExpected(item.expected); const result = { id:item.id.trim(), prompt:item.prompt.trim(), metadata:{ source:"user" } };
      if (expected.present) result.expected_output = expected.value;
      return result;
    });
    const repository = (prefix, mount) => ({
      ssh_url:String(data.get(`${prefix}_ssh`) || "").trim(), branch:String(data.get(`${prefix}_branch`) || "master").trim(),
      local_path:String(data.get(`${prefix}_local`) || "").trim() || null,
      authorization_token_env:"CATX_REPOSITORY_AUTHORIZATION_TOKEN", mount_path:mount,
    });
    const skill = repository("skill", "/workspace/skill"); const codeSsh = String(data.get("code_ssh") || "").trim();
    const config = {
      api_version:"aceval.kernel-config/v1", skill_repository:skill, code_repository:codeSsh ? repository("code", "/workspace/repo") : null,
      local_analysis:{ model_command:modelCommand, model_id:String(data.get("model_id")).trim(), api_key_env:"OPENAI_API_KEY", env_allowlist:["OPENAI_API_KEY","ANTHROPIC_API_KEY"], timeout_seconds:180, max_prompt_chars:48000, max_output_chars:24000 },
      remote_agent:{ profile_path:String(data.get("remote_profile")).trim(), max_parallel:8, poll_interval_seconds:5, max_wait_seconds:1200 },
      policy:{ max_generated_cases:12, max_rounds:5, pass_verification_runs:1, max_analysis_evidence_chars_per_case:4000, max_remote_prompt_chars:12000, max_total_remote_sessions:200, min_improvement:.01, convergence_patience:2 },
    };
    const input = {
      api_version:"aceval.kernel-input/v1", skill_name:String(data.get("skill_name")).trim(), operation:String(data.get("operation")),
      goal:String(data.get("goal") || "").trim() || null, standards:parseLines(data.get("standards")), cases:caseValues,
      capabilities:parseLines(data.get("capabilities")), non_goals:[], notes:null, evalpack_path:String(data.get("evalpack_path") || "").trim() || null,
    };
    state.busy = "正在准备仓库并建立可恢复任务…"; render();
    try {
      const created = await call("tasks.create", { config, input });
      state.selectedTaskId = created.task.id; state.view = "detail"; state.snapshot = await call("tasks.get", { task_id:created.task.id }); state.busy = null; render();
      await call("tasks.run", { task_id:created.task.id });
      toast("任务已创建，EvalPack 与评测路径正在生成");
      await refreshTasks(true);
    } catch (error) { state.busy = null; render(); throw error; }
  }

  async function hashDataUrl(dataUrl) {
    const base64 = dataUrl.slice(dataUrl.indexOf(",") + 1); const bytes = Uint8Array.from(atob(base64), char => char.charCodeAt(0));
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return "sha256:" + [...new Uint8Array(digest)].map(item => item.toString(16).padStart(2,"0")).join("");
  }

  app.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-action]"); if (!button) return;
    const action = button.dataset.action;
    try {
      if (action === "home") { state.view = state.selectedTaskId ? "detail" : "empty"; state.modal = null; render(); }
      else if (action === "new-task") { state.view = "create"; state.createCases = []; state.modal = null; render(); }
      else if (action === "select-task") await selectTask(button.dataset.id);
      else if (action === "tab") { state.tab = button.dataset.tab; render(); }
      else if (action === "refresh") await refreshTasks(true);
      else if (action === "run-task") { await call("tasks.run", { task_id:state.selectedTaskId }); toast("已继续自动执行，遇到确认门会停下"); await refreshTasks(true); }
      else if (action === "open-log") { state.busy = "正在读取完整会话日志…"; render(); const value = await call("tasks.log", { task_id:button.dataset.task, iteration:Number(button.dataset.iteration), purpose:button.dataset.purpose, case_id:button.dataset.case }); state.busy = null; state.modal = { type:"log", title:`${button.dataset.case} · ${button.dataset.purpose}`, value }; render(); }
      else if (action === "close-modal") { state.modal = null; render(); }
      else if (action === "settings") { state.secrets = await forge.secretStatus(); state.extensions = await forge.listExtensions(); state.modal = { type:"settings" }; render(); }
      else if (action === "add-case") { syncCasesFromDom(); state.createCases.push({ id:"", prompt:"", expected:"" }); render(); }
      else if (action === "remove-case") { syncCasesFromDom(); state.createCases.splice(Number(button.dataset.index), 1); render(); }
      else if (action === "browse-dir") { const value = await forge.selectDirectory(); if (value) document.querySelector(`[name="${button.dataset.target}"]`).value = value; }
      else if (action === "browse-file") { const value = await forge.selectFile([{ name:"JSON", extensions:["json"] }]); if (value) document.querySelector(`[name="${button.dataset.target}"]`).value = value; }
      else if (action === "reject-decision") { await call("tasks.confirm", { task_id:state.selectedTaskId, approve:false }); toast("本轮修改已拒绝，任务停在可追溯阻塞状态"); await refreshTasks(true); }
      else if (action === "open-preview") { const url = document.querySelector('#d2c-form [name="url"]').value; await forge.openPreview(url); }
      else if (action === "select-design") { const path = await forge.selectImage(); if (path) { const artifact = await forge.readArtifact(path); state.designImage = { path, data_url:artifact.data_url, sha256:await hashDataUrl(artifact.data_url) }; render(); } }
      else if (action === "install-extension") { state.busy = "正在校验插件清单、权限和内容哈希…"; render(); const installed = await forge.installExtension(); state.busy = null; if (installed) { state.extensions = await forge.listExtensions(); toast(`插件 ${installed.name} 已安装到受控预览 Profile`); } render(); }
      else if (action === "clear-secret") { await forge.saveSecrets({ [button.dataset.name]:null }); state.secrets = await forge.secretStatus(); toast(`${button.dataset.name} 已从安全存储移除`); render(); }
    } catch (error) { state.busy = null; render(); }
  });

  app.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      if (event.target.id === "create-form") await createTask(event.target);
      else if (event.target.id === "decision-form") {
        const data = new FormData(event.target); const selected = data.getAll("proposal");
        if (!selected.length) throw new Error("至少选择一个修改提案");
        await call("tasks.confirm", { task_id:state.selectedTaskId, approve:true, selected_change_ids:selected, user_feedback:String(data.get("feedback") || "").trim() || null, continue:true });
        toast("修改范围已冻结，正在生成受控候选版本"); await refreshTasks(true);
      } else if (event.target.id === "secret-form") {
        const values = {}; for (const [name,value] of new FormData(event.target)) if (String(value).trim()) values[name] = String(value).trim();
        if (!Object.keys(values).length) throw new Error("没有需要保存的新密钥");
        state.busy = "正在写入系统安全存储并重启 Kernel 服务…"; render(); state.secrets = await forge.saveSecrets(values); state.busy = null; toast("密钥已安全保存，任务文件中不会出现明文"); render();
      } else if (event.target.id === "d2c-form") {
        const data = new FormData(event.target); let actions = []; if (String(data.get("actions") || "").trim()) actions = JSON.parse(data.get("actions"));
        const request = { api_version:"aceval.d2c-validation-request/v1", case_id:String(data.get("case_id")).trim(), url:String(data.get("url")).trim(), candidate_commit:state.snapshot.state.challenger_commit || state.snapshot.state.champion_commit, actions, expected_title:String(data.get("expected_title") || "").trim() || null, visual_oracle:state.designImage ? { path:state.designImage.path, sha256:state.designImage.sha256, max_diff_ratio:.01, pixel_threshold:16 } : null, metadata:{ source:"desktop" } };
        await call("d2c.validate", { task_id:state.selectedTaskId, iteration:Number(state.snapshot.state.iteration || 0), profile:state.bootstrap.d2c_profile, request }); toast("D2C Worker 已启动，结果会写入任务证据流"); await refreshTasks(true);
      }
    } catch (error) { state.busy = null; render(); }
  });

  async function init() {
    if (!forge) { app.innerHTML = `<main class="boot-screen"><p>桌面安全桥未加载，请使用 Electron 客户端启动。</p></main>`; return; }
    try {
      state.bootstrap = await call("system.bootstrap"); state.secrets = await forge.secretStatus(); state.extensions = await forge.listExtensions();
      const tasks = await call("tasks.list"); state.tasks = tasks.tasks || [];
      if (state.tasks[0]) { state.selectedTaskId = state.tasks[0].id; state.snapshot = await call("tasks.get", { task_id:state.selectedTaskId }); state.view = "detail"; }
      render();
      setInterval(async () => { if (state.view === "detail" && !state.busy && !state.modal) { try { await refreshTasks(true); } catch {} } }, 1800);
    } catch (error) { app.innerHTML = `<main class="boot-screen"><div class="brand-seal">!</div><p>${esc(error.message || error)}</p></main>`; }
  }

  window.__FORGE_READY__ = true;
  init();
})();
