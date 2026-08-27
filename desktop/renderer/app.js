(() => {
  "use strict";

  const app = document.querySelector("#app");
  const toastNode = document.querySelector("#toast");
  const forge = window.forge;
  const state = {
    bootstrap: null, tasks: [], selectedTaskId: null, snapshot: null,
    view: "empty", tab: "overview", modal: null, busy: null,
    secrets: [], extensions: [], operationSeen: new Map(), planVisited: new Set(), designDraft: {},
    createCases: [], designImage: null, artifactImages: {},
    modelProfiles: [], analysisProfileId: localStorage.getItem("forge.analysisProfileId") || null, createModelId: null, createReasoningEffort: null, modelTest: null, createDraft: {}, catxDefault: { configured:false, vault_ids:[] },
  };

  const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" }[char]));
  const json = (value) => esc(JSON.stringify(value, null, 2));
  const short = (value, count = 10) => value ? String(value).slice(0, count) : "—";
  const formatTime = (value) => { try { return new Intl.DateTimeFormat("zh-CN", { hour:"2-digit", minute:"2-digit", second:"2-digit" }).format(new Date(value)); } catch { return "—"; } };
  const formatDate = (value) => { try { return new Intl.DateTimeFormat("zh-CN", { month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit" }).format(new Date(value)); } catch { return "—"; } };
  const analysisProfile = () => state.modelProfiles.find(item => item.id === state.analysisProfileId) || state.modelProfiles.find(item => item.ready && item.models?.length) || state.modelProfiles.find(item => item.id === "default") || { id:"default", provider:"codex", models:[], ready:false, config_ready:false, auth_ready:false, codex_ready:false };
  const analysisModels = () => (analysisProfile().models || []).filter(item => item.selectable !== false && (item.is_gpt || analysisProfile().provider === "claude"));
  function syncModelSelection() {
    const models = analysisModels();
    if (!models.some(item => item.id === state.createModelId)) state.createModelId = (models.find(item => item.is_default) || models[0])?.id || null;
    const selected = models.find(item => item.id === state.createModelId);
    const efforts = selected?.reasoning_efforts || [];
    if (!efforts.some(item => item.id === state.createReasoningEffort)) state.createReasoningEffort = selected?.default_reasoning_effort || efforts[0]?.id || "medium";
  }
  const phaseLabel = {
    created:"准备设计", blueprint_ready:"能力确认", discovery_ready:"探索确认", ready_to_build:"生成中",
    initial_candidate_ready:"候选确认", candidate_ready:"待发布", design_ready:"设计就绪",
    evaluation_ready:"方案已批准",
    remote_running:"线上评测", baseline_running:"基线评测", verification_running:"稳定性复验",
    remote_collected:"日志已回收", baseline_collected:"基线已回收", verification_collected:"复验已回收",
    evidence_ready:"证据已冻结", semantic_grading:"语义评分", attribution:"失败归因", proposal:"生成提案",
    verification_ready:"等待复验", awaiting_confirmation:"待确认优化", ready_to_optimize:"准备优化",
    needs_evidence:"证据不足", blocked:"已阻塞", converged:"已收敛",
  };
  const eventLabel = {
    "kernel.created":"任务输入已冻结", "evaluation.design_generated":"评测草案已建立",
    "evaluation.evalpack_ready":"EvalPack 已生成或复用", "evaluation.case_path_ready":"Case 与评测路径已就绪", "evaluation.case_generation_started":"开始生成评测 Case / Path", "evaluation.case_generation_completed":"模型生成 Case / Path 已校验", "evaluation.case_generation_failed":"模型生成失败，已显式降级",
    "evaluation.design_review_required":"EvalPack 需要校准或审阅",
    "evaluation.design_compiled":"EvalPack、Case 与路径已编译", "kernel.phase_changed":"核状态流转",
    "user.evaluation_design_approved":"评测方案已批准", "remote.batch_polled":"正在等待远端会话",
    "remote.batch_retry_dispatched":"失败流程已重新创建", "case_run.retried":"失败 Case 已重试",
    "harness.blueprint_ready":"能力蓝图等待确认", "user.capability_blueprint_approved":"能力蓝图已批准",
    "case_run.started":"评测会话已创建", "case_run.completed":"完整会话日志已回收", "case_run.failed":"评测会话失败",
    "environment.contract_frozen":"本轮评测环境已冻结", "user.approval_recorded":"用户审批记录已保存",
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
      design_ready:2, evaluation_ready:3, remote_running:3, baseline_running:3, verification_running:3,
      remote_collected:4, baseline_collected:4, verification_collected:4, verification_ready:3,
      evidence_ready:4, semantic_grading:4, attribution:4, proposal:4,
      awaiting_confirmation:4, needs_evidence:4, ready_to_optimize:5, candidate_ready:5, blocked:4, converged:6,
    };
    return map[phase] ?? 0;
  }

  function stageStrip(phase) {
    const stages = [["输入解析","目标冻结"],["EvalPack","生成 / 复用"],["Case 与路径","可审阅设计"],["线上评测","并行会话"],["分析与决策","归因与建议"],["优化与回归","Champion"]];
    const current = Math.min(stageIndex(phase), stages.length - 1);
    return `<section class="pulse-board"><div class="pulse-board-title"><div><span class="eyebrow dark">LIVE PATH</span><strong>实时路径大盘</strong></div><span class="pulse-caption">${esc(phaseLabel[phase] || phase)}</span></div><div class="stage-track">${stages.map((item, index) => `<div class="stage-step ${index < current ? "is-done" : index === current ? "is-current" : ""}"><i class="stage-dot">${index < current ? "✓" : index === current ? "•" : index + 1}</i><span><strong>${item[0]}</strong><small>${item[1]}</small></span></div>`).join("")}</div></section>`;
  }

  function eventSummary(event) {
    const p = event.payload || {};
    if (event.type === "kernel.phase_changed") return `进入 ${phaseLabel[p.phase] || p.phase || "下一阶段"}`;
    if (event.type === "evaluation.design_compiled") return `${p.case_count || 0} 个 Case，${p.generated_case_count || 0} 个自动补充；EvalPack ${p.pack_source || "generated"}`;
    if (event.type === "evaluation.evalpack_ready") return `${p.pack_source || "generated"} · ${p.lifecycle || "ready"} · ${short(p.signature, 18)}`;
    if (event.type === "evaluation.case_path_ready") return `${event.case_id || p.case_id} · ${p.step_count || 0} 个语义检查点 · ${p.source || "automatic"}`;
    if (event.type === "evaluation.case_generation_started") return `${p.provider || "model"} · ${p.model_id || "—"} · 正在为 ${p.case_count || 0} 个自动 Case 生成互异意图`;
    if (event.type === "evaluation.case_generation_completed") return `${p.strategy || "—"} · ${p.status || "—"} · ${p.usage?.total_tokens || p.usage?.output_tokens || 0} tokens`;
    if (event.type === "evaluation.case_generation_failed") return `${p.model_id || "—"} · ${p.error || "未返回可校验 JSON"} · 已使用显式 fallback`;
    if (event.type === "evaluation.design_review_required") return `${p.evalpack_status || "review_required"} · 可探索运行，未校准 Case 不能单独授权修改 Skill`;
    if (event.type === "case_run.started") return `${event.case_id || "Case"} · ${p.session_id ? `会话 ${short(p.session_id, 14)}` : p.provider || "remote"} · 绑定 ${p.binding_status || "未验证"}`;
    if (event.type === "remote.batch_polled") return `${p.completed || 0}/${p.total || 0} 已完成 · ${p.running || 0} 运行中 · ${p.failed || 0} 失败`;
    if (event.type === "remote.batch_retry_dispatched") return `只重试 ${p.retried || 0} 条失败路径，已完成证据保持不变`;
    if (event.type === "case_run.completed") return `${event.case_id || "Case"} · ${p.status || "日志与证据已持久化"}`;
    if (event.type === "analysis.completed") return `稳定通过 ${p.stable_pass_case_ids?.length || 0}，失败 ${p.failed_case_ids?.length || 0}；下一步 ${p.next_action || "—"}`;
    if (event.type === "candidate.rejected") return `未通过成对门禁；${p.restoration_status === "restored" ? "已自动恢复 Champion 内容" : "Champion 保持不变"}`;
    if (event.type === "candidate.promoted") return `没有硬回归且达到最小收益，候选晋升为 Champion`;
    return Object.keys(p).length ? JSON.stringify(p).slice(0, 220) : "事件已写入不可变任务时间线";
  }

  function traceView(snapshot) {
    const events = snapshot.events || [];
    const design = snapshot.design || {};
    const aggregates = Object.fromEntries((snapshot.decision?.case_aggregates || []).map(item => [item.case_id, item]));
    const batches = (snapshot.iterations || []).flatMap(iteration => (iteration.batches || []).map(batch => ({ ...batch, iteration: iteration.name })));
    const latestBatch = batches.at(-1);
    const failed = events.filter(event => event.type.includes("failed") || event.type.includes("rejected"));
    const cases = design.cases || [];
    const completed = latestBatch?.cases?.filter(item => item.status === "completed").length || 0;
    const running = latestBatch?.cases?.filter(item => ["running", "starting"].includes(item.status)).length || 0;
    const failedRuns = latestBatch?.cases?.filter(item => item.status === "failed").length || failed.length;
    const state = snapshot.state || {};
    const stageState = (key) => key === "input" ? "done" : key === "pack" ? (design.evalpack ? "done" : "active") : key === "cases" ? (cases.length ? "done" : "active") : key === "evaluation" ? (running || latestBatch?.status === "running" ? "active" : completed || failedRuns ? "done" : "pending") : key === "analysis" ? (snapshot.decision ? "done" : ["remote_collected","evidence_ready","semantic_grading","attribution","proposal","awaiting_confirmation"].includes(state.phase) ? "active" : "pending") : "pending";
    const stage = (key, number, title, subtitle, body) => `<article class="evolution-stage ${stageState(key)}"><div class="stage-marker">${stageState(key) === "done" ? "✓" : number}</div><div class="stage-body"><header><div><span class="trace-kicker">${esc(subtitle)}</span><h4>${title}</h4></div><span class="stage-status">${stageState(key) === "done" ? "已完成" : stageState(key) === "active" ? "进行中" : "待开始"}</span></header>${body}</div></article>`;
    const caseChips = cases.slice(0, 8).map(item => { const aggregate = aggregates[item.id]; const status = aggregate?.status || "pending"; const title = item.title || item.metadata?.aceval_test?.title || item.id; return `<button class="case-chip ${status}" data-action="tab" data-tab="cases"><span>${esc(title)}</span><small>${status === "pass" ? "PASS" : status === "fail" ? "FAIL" : status === "pending" ? "待运行" : status} · ${esc(short(item.id, 18))}</small></button>`; }).join("");
    const restart = failedRuns || ["blocked","needs_evidence"].includes(state.phase) ? `<div class="failure-callout"><div><strong>本次流程未完成</strong><p>${failedRuns ? `${failedRuns} 条评测路径失败，已完成证据仍会保留。` : "任务停在阻塞门，可从原始输入重新建立完整流程。"}</p></div><button class="primary-button" data-action="restart-task">从头重新开始</button></div>` : "";
    return `<div class="section-head"><div><span class="eyebrow">Evolution canvas</span><h3>评测 → 证据 → 迭代</h3></div><button class="text-button" data-action="tab" data-tab="logs">查看完整审计 · ${events.length} events →</button></div>${restart}<div class="evolution-flow">
      ${stage("input", 1, "目标与约束已冻结", "INPUT SNAPSHOT", `<p>${esc(snapshot.task?.goal || "任务输入已保存为不可变快照")}</p><div class="stage-tags"><span class="tag">${(snapshot.task?.standards || []).length || "—"} 条成功标准</span><span class="tag">R${Number(state.iteration || 0) + 1}</span></div>`)}
      ${stage("pack", 2, "EvalPack 评测设计", "EVALUATION DESIGN", design.evalpack ? `<p>${esc(design.cases?.length || 0)} 个 Case · ${esc(design.standards?.length || snapshot.task?.standards?.length || 0)} 个评分维度 · 设计已${design.evalpack.lifecycle === "frozen" ? "冻结" : "生成"}</p><div class="stage-tags"><span class="tag">${esc(design.evalpack.source || "generated")}</span><span class="tag">签名 ${esc(short(design.evalpack.signature, 12))}</span></div>` : `<p>评测设计正在生成…</p>`)}
      ${stage("cases", 3, `${cases.length || 0} 个 Case · 路径图谱`, "CASE / PATH GRAPH", cases.length ? `<div class="case-matrix">${caseChips || "<span class=\"muted\">等待评分结果</span>"}</div><button class="stage-link" data-action="tab" data-tab="cases">打开 Case 与路径详情 →</button>` : `<p>EvalPack 编译后会自动补齐 Case 与执行路径。</p>`)}
      ${stage("evaluation", 4, latestBatch ? `${esc(latestBatch.purpose || "线上评测")} · ${completed + failedRuns}/${latestBatch.cases?.length || 0}` : "线上评测会话", "REMOTE EVALUATION", latestBatch ? `<div class="progress-line"><i style="width:${latestBatch.cases?.length ? Math.round((completed + failedRuns) / latestBatch.cases.length * 100) : 0}%"></i></div><div class="stage-stats"><span><b>${completed}</b> 已完成</span><span><b>${running}</b> 运行中</span><span class="bad"><b>${failedRuns}</b> 失败</span></div><p class="muted">会话按 Case 聚合展示；完整工具调用与返回证据请在“会话日志”查看。</p>` : `<p>等待批准评测方案后创建远端会话。</p>`)}
      ${stage("analysis", 5, snapshot.decision ? (snapshot.decision.failed_case_ids?.length ? `发现 ${snapshot.decision.failed_case_ids.length} 条失败路径` : "所有目标路径通过稳定性门禁") : "证据分析与优化决策", "EVIDENCE → DECISION", snapshot.decision ? `<p>${esc(snapshot.decision.next_action || "分析结果已冻结，等待下一步决策")}</p><button class="stage-link" data-action="tab" data-tab="decision">查看问题簇与修改提案 →</button>` : `<p>日志完整回收后，系统会压缩证据并生成跨 Case 归因。</p>`)}
    </div>`;
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
    <section class="panel-section"><h4>冻结评测环境</h4><p>初次评测与稳定性复验使用同一份 Trial Environment Contract；每次会话独立启动，但仓库 revision、Profile 内容、Fixture 与运行参数必须一致。</p><code class="code-line">${esc(snapshot.state.environment_contract_hash || "首个评测批次创建时冻结")}</code></section>
    <section class="panel-section"><h4>版本关系</h4><code class="code-line">Champion ${esc(short(snapshot.state.champion_commit, 16))}</code><code class="code-line">Challenger ${esc(short(snapshot.state.challenger_commit, 16))}</code>${comparison ? `<p class="evidence-status top-gap ${comparison.accepted ? "" : "bad"}"><i></i>${comparison.accepted ? "达到最小收益，无稳定通过回归" : `未晋升：${(comparison.reasons || []).join(", ")}`}</p>` : ""}</section>
    <section class="panel-section"><h4>收敛状态</h4><p>${convergence.converged ? `已停止：${esc(convergence.reason || "target_met")}` : `仍有 ${(convergence.remaining_failed_case_ids || []).length} 个失败路径；最多 ${convergence.max_rounds || "—"} 轮。`}</p></section>
    <section class="panel-section"><h4>Token 经济</h4><p>本轮本地分析模型调用 ${decision.token_economy?.model_calls ?? 0} 次；没有发送完整日志，只发送压缩证据约 ${decision.token_economy?.estimated_prompt_tokens ?? 0} tokens。</p></section>`;
  }

  function casesTab(snapshot) {
    const design = snapshot.design || {};
    const requirements = design.test_design?.test_plan?.requirements || [];
    const requirementMap = Object.fromEntries(requirements.map(item => [item.id, item]));
    const capabilities = design.capability_summary?.capabilities || [];
    const capabilityMap = Object.fromEntries(capabilities.map(item => [item.id, item]));
    const aggregates = Object.fromEntries((snapshot.decision?.case_aggregates || []).map(item => [item.case_id, item]));
    const review = snapshot.state.phase === "design_ready";
    const reviewDraft = state.designDraft[snapshot.task.id];
    const pack = design.evalpack || {};
    const generation = design.case_generation || design.planning?.case_generation || {};
    const receipts = [generation.receipt, ...(snapshot.iterations || []).flatMap(item => item.planning_artifacts?.case_generation_receipt ? [item.planning_artifacts.case_generation_receipt] : [])].filter(Boolean);
    const dimensionLabel = { happy_path:"正常流程", branch:"声明分支", negative:"异常输入 / 安全拒绝", risk:"风险边界", recovery:"工具失败恢复", idempotency:"重复执行一致性", state_transition:"状态迁移", step_ordering:"步骤顺序" };
    const originLabel = { seed:"用户 / 基线 Case", requirement_synthesis:"规划器补充 Case" };
    const pathKindLabel = { required:"必须观察", recommended:"建议观察", forbidden:"禁止发生", alternative:"允许替代" };
    const readable = value => String(value || "").replace(/^cap\./, "").replace(/^req\./, "").replace(/^generated\./, "").replace(/[_\.:-]+/g, " ").replace(/\bitem\s+[a-f0-9]{8,}\b/ig, "能力项").replace(/\s+/g, " ").trim();
    const semantics = (item, test) => {
      const req = requirementMap[(test.requirement_ids || item.requirement_ids || [])[0]];
      const capability = capabilityMap[test.capability_ids?.[0] || req?.capability_id];
      const dimension = test.kind || req?.dimension || "happy_path";
      const capName = capability?.name || readable(capability?.id || req?.capability_id || test.family || item.id) || "Skill 能力";
      const kindName = dimensionLabel[dimension] || readable(dimension);
      const isSeed = test.origin === "seed" || item.id === "auto-primary-goal";
      const provenance = metaGeneration(item, generation);
      const generatedTitle = test.title || item.title;
      const title = item.id === "auto-primary-goal" ? "基线：执行 Skill 的主要目标" : isSeed ? (test.title && !/^seed[ ·.]/i.test(test.title) ? test.title : `基线：${capName}`) : provenance.status === "validated" && generatedTitle ? generatedTitle : `${capName}：${kindName}`;
      const stimulus = req?.stimulus || test.stimulus || item.prompt;
      const observables = test.expected_observables?.length ? test.expected_observables : (req?.expected_observables || []);
      const refs = test.source_ref_details?.length ? test.source_ref_details : (test.source_refs || []).map(ref => ({ label:ref }));
      const sourceLabel = isSeed ? originLabel.seed : provenance.status === "validated" ? "Claude / CC Switch 模型生成" : "确定性规划器补充（非模型生成）";
      const modelLabel = provenance.status === "validated" ? (provenance.model_id || generation.model_id || "已配置模型") : isSeed ? "不适用（种子 Case）" : "未调用模型";
      const difference = isSeed ? "这是基线任务，用来建立整条 Skill 的 Champion 结果；其它 Case 会在此基础上拆分单一能力或风险。" : `只验证「${kindName}」这一维度；与同能力其它 Case 的差异是输入条件、风险边界或预期可观察结果不同。`;
      return { req, capability, dimension, capName, kindName, title, stimulus, observables, refs, provenance, sourceLabel, modelLabel, difference };
    };
    function metaGeneration(item, overall) {
      const meta = item.metadata || {};
      const perCase = meta.generation_provenance;
      if (perCase && typeof perCase === "object") return perCase;
      return { status: overall.status || "fallback", model_id: overall.model_id || "" };
    }
    const grouped = {};
    (design.cases || []).forEach(item => {
      const test = item.metadata?.aceval_test || {};
      const info = semantics(item, test);
      const family = info.capName || "未分组能力";
      (grouped[family] ||= []).push(item);
    });
    const cards = Object.entries(grouped).map(([family, familyCases]) => `<section class="case-family"><header class="family-head"><div><span class="eyebrow">CASE FAMILY</span><h4>${esc(family.replaceAll(".", " · ").replaceAll("_", " "))}</h4></div><span class="family-count">${familyCases.length} 条路径</span></header>${familyCases.map(item => {
      const path = design.execution_paths?.[item.id]; const aggregate = aggregates[item.id];
      const dimensions = aggregate?.attempts?.at(-1)?.dimensions || [];
      const meta = item.metadata || {}; const test = meta.aceval_test || {};
      const info = semantics(item, test);
      const checked = !reviewDraft || reviewDraft.caseIds.includes(String(item.id));
      const sourceDetails = info.refs.map(ref => `${ref.path || "SKILL.md"}:${ref.start_line || "?"}-${ref.end_line || ref.start_line || "?"}${ref.excerpt ? ` · ${ref.excerpt}` : ""}`);
      const requirementIds = test.requirement_ids || item.requirement_ids || [];
      return `<label class="case-card review-case"><header>${review ? `<input type="checkbox" name="case" value="${esc(item.id)}" ${checked ? "checked" : ""}/>` : ""}<div class="case-title"><strong>${esc(info.title)}</strong><small>Case ID · ${esc(item.id)}</small></div><span class="evidence-status ${aggregate?.status === "fail" ? "bad" : ""}"><i></i>${esc(aggregate?.status || (review ? "待审阅" : "待运行"))}</span></header><div class="case-flow"><span class="flow-node skill-node">Skill<br/><small>${esc(info.capName)}</small></span><span class="flow-arrow">→</span><span class="flow-node req-node">测试要求<br/><small>${esc(requirementIds.length ? `${requirementIds.length} 条已映射` : "未映射")}</small></span><span class="flow-arrow">→</span><span class="flow-node case-node">评测维度<br/><small>${esc(info.kindName)}</small></span><span class="flow-arrow">→</span><span class="flow-node path-node">执行路径<br/><small>${(path?.steps || []).length} 个检查点</small></span></div><div class="case-intent-grid"><div class="intent-panel primary"><b>真实测试任务</b><p>${esc(item.prompt)}</p></div><div class="intent-panel"><b>这条 Case 验证什么</b><p>${esc(info.stimulus)}</p>${info.observables.length ? `<ul>${info.observables.map(value => `<li>${esc(value)}</li>`).join("")}</ul>` : ""}</div></div><div class="trace-tags"><span class="tag">${esc(info.sourceLabel)}</span><span class="tag">模型 · ${esc(info.modelLabel)}</span><span class="tag">Oracle · ${esc(test.oracle_level || (test.oracle_ready ? "可评分" : "待校准"))}</span><span class="tag">维度 · ${esc(info.kindName)}</span></div><div class="case-basis"><b>为什么需要它 / 与其它 Case 的差异</b><p>${esc(test.generation_reason || test.selection_reason || "覆盖 Skill 声明的能力与风险")}</p><p class="case-difference">${esc(info.difference)}</p></div><div class="case-basis source-evidence"><b>Skill 依据（可追溯）</b><p class="source-line">${esc(sourceDetails.join(" · ") || (test.source_refs || []).join(" · ") || "没有记录 source refs")}</p>${requirementIds.length ? `<p class="source-line">Requirement · ${esc(requirementIds.join(", "))}</p>` : ""}</div>${item.expected_output != null ? `<div class="case-basis"><b>确定性期望 / Oracle</b><p>${esc(typeof item.expected_output === "string" ? item.expected_output : JSON.stringify(item.expected_output))}</p></div>` : ""}${aggregate ? `<div class="trace-tags"><span class="tag">pass^k ${aggregate.pass_power_k == null ? "—" : Number(aggregate.pass_power_k).toFixed(2)}</span><span class="tag">${aggregate.evaluable_attempt_count}/${aggregate.attempt_count} valid</span>${dimensions.map(dimension => `<span class="tag dimension-${esc(dimension.status)}">${esc(dimension.dimension)} · ${esc(dimension.status)}</span>`).join("")}</div><div class="case-score-vector">${dimensions.map(dimension => `<article class="case-score ${esc(dimension.status)}"><header><span>${esc(dimension.dimension)}</span><strong>${dimension.score == null ? "N/M" : `${Math.round(Number(dimension.score) * 100)}%`}</strong></header><p>${esc(dimension.reason || "没有评分说明")}</p><small>${esc(dimension.grader_id || "unknown grader")} · ${esc(dimension.source || "fact")} · ${dimension.hard ? "HARD GATE" : "SOFT"}</small></article>`).join("")}</div>` : ""}<div class="path-tree"><b>执行路径 · ${esc(path?.purpose || `${info.title} 的逐步证据检查`)}</b><ul class="path-list">${(path?.steps || []).map((step, index) => `<li><i>${index + 1}</i><span>${esc(step.label)}<small>${esc(JSON.stringify(step.match || {}))}</small></span><em>${esc(pathKindLabel[step.kind] || step.kind)}</em></li>`).join("") || "<li>路径尚未生成</li>"}</ul></div></label>`;
    }).join("")}</section>`).join("") || `<p>EvalPack 编译后会在这里出现 Case 和路径。</p>`;
    const generationStatus = generation.status === "validated" ? "模型生成已通过严格 JSON / source refs 校验" : generation.status === "fallback" ? "模型调用失败，已明确降级为确定性规划" : "未调用模型（只有种子 / 用户 Case）";
    return `<section class="panel-section evalpack-summary"><h4>EvalPack · ${esc(pack.source || "generated")} / ${esc(pack.lifecycle || pack.status || "ready")}</h4><p>${design.cases?.length || 0} 个 Case · ${requirements.length} 条测试要求 · 签名 ${esc(short(pack.signature, 22))}</p><div class="generation-banner"><strong>${esc(generationStatus)}</strong><span>${esc(generation.model_id || "未配置")}</span><small>${esc(generation.status === "validated" ? `只对 ${generation.generated_case_ids?.length || 0} 个规划器补充 Case 调用模型；种子 Case 不会被冒充为模型产物。${generation.attempt_count ? ` 共 ${generation.attempt_count} 次模型调用${generation.repair_applied ? "，首轮格式未通过后执行了 1 次受控结构修复" : ""}。` : ""}` : generation.error || "每张 Case 卡片都会标明是种子、模型生成还是确定性规划补充。")}</small>${receipts.length ? `<small>模型收据 · ${esc(receipts[0])}</small>` : ""}</div><div class="trace-tags">${(design.standards || []).map(item => `<span class="tag">规范 · ${esc(item)}</span>`).join("")}</div></section>${review ? `<form id="design-review-form"><div class="review-banner"><strong>远端评测尚未开始</strong><p>逐项审阅 Case、真实任务 Prompt、Skill 依据和语义路径。批准后每个 Case 会创建独立 CATX 会话，并绑定同一组冻结仓库 commit；不需要为 Case 创建 Git 分支。</p></div>${cards}<textarea class="feedback" name="feedback" placeholder="可选：记录本次评测方案的审阅意见">${esc(reviewDraft?.feedback || "")}</textarea><div class="decision-actions"><button class="primary-button" type="submit">批准所选 Case 并开始线上评测</button><button class="danger-button" type="button" data-action="reject-design">退回并阻塞</button></div></form>` : cards}`;
  }

  function batchStatus(snapshot) {
    const batches = (snapshot.iterations || []).flatMap(iteration => (iteration.batches || []).map(batch => ({ ...batch, iteration:iteration.name })));
    const active = batches.sort((a,b) => String(a.updated_at || "").localeCompare(String(b.updated_at || ""))).at(-1);
    if (!active) return "";
    const rows = active.cases || []; const done = rows.filter(row => row.status === "completed").length; const failed = rows.filter(row => row.status === "failed").length; const running = rows.filter(row => ["running","starting"].includes(row.status)).length;
    return `<section class="batch-monitor"><header><div><span class="eyebrow">Remote session monitor</span><h3>${esc(active.purpose)} · ${done + failed}/${rows.length}</h3></div><span class="phase-pill ${failed ? "warn" : active.status === "completed" ? "pass" : ""}">${running ? `${running} 轮询中` : failed ? `${failed} 失败` : "已回收"}</span></header><div class="batch-progress"><i style="width:${rows.length ? Math.round((done + failed) / rows.length * 100) : 0}%"></i></div><div class="session-grid">${rows.map(row => `<div class="session-state ${esc(row.status)}"><b>${esc(row.case_title || row.case_id)}</b><span>${esc(row.status)}</span><small>session · ${esc(short(row.session_id, 15))}</small><small>仓库绑定 · ${esc(row.binding_status || "unknown")} · 首条消息 · ${esc(row.message_status || "unknown")}</small>${row.error ? `<em>${esc(row.error)}</em>` : ""}</div>`).join("")}</div>${failed ? `<button class="ghost-button dark-ghost action-gap" data-action="retry-failed" data-purpose="${esc(active.purpose)}">使用当前配置重试 ${failed} 条失败流程</button>` : ""}</section>`;
  }

  function logsTab(snapshot) {
    const rows = (snapshot.iterations || []).flatMap(iteration => (iteration.session_logs || []).map(log => ({ ...log, iteration:Number(iteration.name.replace("iteration-", "")) })));
    return `<section class="panel-section"><h4>完整会话日志 · ${rows.length}</h4><p>每条日志固定关联 Case、轮次、目的和评测路径；同时展示仓库绑定、首条消息和日志完整性，避免把 prompt 当成绑定证据。</p></section>${rows.map(log => { const path = snapshot.design?.execution_paths?.[log.case_id]; const binding = log.binding_status || log.binding_evidence?.verified; const completeness = log.log_completeness || log.completeness; return `<article class="log-card"><header><strong>${esc(log.case_title || log.case_id)}</strong><span class="phase-pill ${log.status === "completed" ? "pass" : ""}">${esc(log.status)}</span></header><p>R${log.iteration + 1} · ${esc(log.purpose)} · session ${esc(short(log.session_id, 18))}</p><div class="trace-tags"><span class="tag ${binding === "verified" || binding === true ? "tag-ok" : "tag-warn"}">仓库绑定 · ${binding === "verified" || binding === true ? "已验证" : binding === "failed" ? "失败" : "未验证"}</span><span class="tag">消息 · ${esc(log.message_status || "unknown")}</span><span class="tag">日志 · ${completeness ? "完整性已记录" : "待核验"}</span></div><div class="trace-tags">${(path?.steps || []).map(step => `<span class="tag">${esc(step.label)}</span>`).join("") || `<span class="tag">路径未生成</span>`}</div><button class="text-button top-gap" data-action="open-log" data-task="${esc(snapshot.task.id)}" data-iteration="${log.iteration}" data-purpose="${esc(log.purpose)}" data-case="${esc(log.case_id)}">查看 user.message、工具调用、返回与终端事件 →</button></article>`; }).join("") || `<p>会话结束并回收后，完整日志会出现在这里。</p>`}`;
  }

  function decisionTab(snapshot) {
    const decision = snapshot.decision;
    if (!decision) {
      const order = ["evidence_ready","semantic_grading","attribution","proposal"];
      const current = order.indexOf(snapshot.state.phase); const analysisStarted = current >= 0;
      return `<section class="panel-section"><h4>${esc(phaseLabel[snapshot.state.phase] || "分析尚未开始")}</h4><p>完整日志先被编译为冻结证据，再依次经过语义评分、跨 Case 归因和修改提案；页面会随阶段产物流式刷新。</p><div class="analysis-stream">${order.map((stage,index) => `<div class="analysis-step ${analysisStarted && index < current ? "done" : index === current ? "active" : ""}"><i>${analysisStarted && index < current ? "✓" : index + 1}</i><span><b>${esc(phaseLabel[stage])}</b><small>${index === current ? "正在执行并持久化收据" : index < current && analysisStarted ? "产物已冻结" : "等待上游证据"}</small></span></div>`).join("")}</div></section>`;
    }
    const graph = decision.diagnosis_graph || {};
    return `<div class="decision-hero"><small>R${Number(snapshot.state.iteration || 0) + 1} · CROSS-CASE SYNTHESIS</small><h4>${decision.failed_case_ids?.length ? `发现 ${decision.failed_case_ids.length} 条未达标路径` : "所有目标路径满足稳定性门禁"}</h4><p>${decision.intervention_blocker ? esc(decision.intervention_blocker) : "问题事实、根因假设与修改提案分层展示；修改前需要你的明确批准。"}</p></div>
      <section class="panel-section"><h4>分阶段分析证据</h4><div class="trace-tags"><span class="tag">1 冻结证据</span><span class="tag">2 语义评分</span><span class="tag">3 跨 Case 归因</span><span class="tag">4 修改提案</span><span class="tag">${decision.analysis_artifacts?.agent_call_receipt_history?.length || 0} 份模型调用收据</span></div><p>本地分析大脑生成语义意见，最终修改授权、候选晋升与收敛仍由 Kernel 裁决。</p></section>
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
    const atGate = ["created","design_ready","evaluation_ready","remote_collected","verification_collected","ready_to_optimize","candidate_ready"].includes(phase);
    const isFailed = task.status === "failed" || (snapshot.events || []).some(event => event.type.includes("failed")) || ["blocked","needs_evidence"].includes(phase);
    return `<main class="view"><header class="detail-head"><div class="headline-row"><div><span class="eyebrow">${esc(task.scenario)} · ${esc(task.id)}</span><h2>${esc(task.skill?.name || task.skill_name || task.id)}</h2><p>${esc(task.goal)}</p></div><div class="head-actions"><button class="ghost-button" data-action="refresh">刷新</button>${isFailed ? `<button class="primary-button" data-action="restart-task">↻ 从头重跑</button>` : ""}<button class="primary-button" data-action="run-task" ${active || !atGate || phase === "design_ready" ? "disabled" : ""}>${active ? "正在运行" : phase === "converged" ? "已收敛" : phase === "design_ready" ? "请先审阅方案" : "继续自动执行"}</button></div></div>${stageStrip(phase)}</header><div class="detail-grid"><section class="trace-pane">${traceView(snapshot)}</section>${inspector(snapshot)}</div></main>`;
  }

  function createView() {
    syncModelSelection();
    const profile = analysisProfile(); const models = analysisModels(); const selectedModel = models.find(item => item.id === state.createModelId); const efforts = selectedModel?.reasoning_efforts || [];
    return `<main class="view create-view"><header class="create-head"><div><span class="eyebrow">New evolution task</span><h2>创建一次可追溯的 Skill 升级</h2><p>最少只需要 Skill、目标和运行环境；Case 与 EvalPack 可由系统自动补齐。</p></div><button class="ghost-button" data-action="home">取消</button></header><form id="create-form"><div class="create-grid"><div class="form-stack">
      <section class="form-section"><div class="form-title"><b>01</b><div><h3>候选 Skill</h3><p>仓库、分支和本地工作副本</p></div></div><div class="fields"><div class="field full"><label>Skill 名称</label><input name="skill_name" required placeholder="frontend-code-reviewer"/></div><div class="field full"><label>Skill 仓库 SSH</label><input name="skill_ssh" required placeholder="ssh://git@git.example.com/org/skill.git"/></div><div class="field"><label>分支</label><input name="skill_branch" required value="master"/></div><div class="field"><label>本地路径（可空，自动检出）</label><div class="inline-input"><input name="skill_local"/><button type="button" data-action="browse-dir" data-target="skill_local">选择</button></div></div></div></section>
      <section class="form-section"><div class="form-title"><b>02</b><div><h3>目标、标准与 Case</h3><p>支持修复、优化、扩展、探索和从零生成</p></div></div><div class="fields"><div class="field"><label>操作模式</label><select name="operation"><option value="auto">自动识别</option><option value="repair">修复</option><option value="tune">优化</option><option value="extend">增加功能</option><option value="discover">探索提升</option><option value="create">从零生成</option></select></div><div class="field"><label>目标</label><input name="goal" placeholder="让评审结果更准确、可定位"/></div><div class="field full"><label>成功标准（每行一条）</label><textarea name="standards" placeholder="覆盖关键缺陷\n严格遵循 Skill 的关键执行步骤\n不制造无证据结论"></textarea></div><div class="field full"><label>能力诉求（每行一条，可选）</label><textarea name="capabilities" placeholder="新增 Java 服务端并发问题评审能力"></textarea></div><div class="field full"><label>自定义 EvalPack（可选）</label><div class="inline-input"><input name="evalpack_path" placeholder="留空时系统自动生成或复用"/><button type="button" data-action="browse-dir" data-target="evalpack_path">选择目录</button></div><small>自定义入口不会关闭自动补充；用户 Case 仍会合并进评测设计。</small></div></div><div class="case-editor"><strong>用户 Case（可选）</strong><div id="case-rows">${state.createCases.map(caseRow).join("")}</div><button class="add-case" type="button" data-action="add-case">＋ 添加 Case</button></div></section>
      <section class="form-section"><div class="form-title"><b>03</b><div><h3>源码 / Fixture 仓库</h3><p>代码评审、D2C 等场景可挂载第二仓库</p></div></div><div class="fields"><div class="field full"><label>源码仓库 SSH（可选）</label><input name="code_ssh" placeholder="ssh://git@git.example.com/org/fixture-lab.git"/></div><div class="field"><label>分支</label><input name="code_branch" value="master"/></div><div class="field"><label>本地路径</label><div class="inline-input"><input name="code_local"/><button type="button" data-action="browse-dir" data-target="code_local">选择</button></div></div></div></section>
      <section class="form-section"><div class="form-title"><b>04</b><div><h3>本地分析大脑与评测运行环境</h3><p>Codex 或 Claude 负责受限语义分析；CATX 执行真实 Case。初测与复验自动继承同一冻结环境，凭证不写入任务</p></div></div><div class="model-inline-status ${profile.ready && models.length ? "ready" : "needs-setup"}"><i></i><span>${profile.ready && models.length ? `${profile.provider === "claude" ? "Claude Code" : "Codex"} 已就绪 · ${models.length} 个分析模型` : "请在设置中自动读取 CC Switch 或导入 Codex 配置"}</span>${profile.provider !== "claude" && !profile.config_ready ? `<button type="button" data-action="import-codex" data-kind="config">导入 config.toml</button>` : ""}${profile.provider !== "claude" && !profile.auth_ready ? `<button type="button" data-action="import-codex" data-kind="auth">导入 auth.json</button>` : ""}${profile.provider !== "claude" && profile.ready && !models.length ? `<button type="button" data-action="refresh-codex">读取模型</button>` : ""}</div><div class="fields"><div class="field"><label>分析模型</label><select name="model_id" ${models.length ? "required" : "disabled"}>${models.map(item => `<option value="${esc(item.id)}" ${item.id === state.createModelId ? "selected" : ""}>${esc(item.display_name)}${item.is_default ? " · 默认" : ""}${item.probe?.ready ? " · 已验证" : item.probe && !item.probe.ready ? " · 不可用" : ""}</option>`).join("") || `<option>尚未读取模型</option>`}</select><small>${esc(selectedModel?.description || "模型来自本机 CC Switch/Codex 配置，不由 FORGE 写死。")}</small></div><div class="field"><label>推理强度</label><select name="reasoning_effort" ${models.length && efforts.length ? "required" : "disabled"}>${efforts.map(item => `<option value="${esc(item.id)}" ${item.id === state.createReasoningEffort ? "selected" : ""}>${esc(item.id)}</option>`).join("") || `<option value="default">由模型配置决定</option>`}</select><small>${profile.provider === "claude" ? "Claude 使用 CC Switch 当前模型配置。" : "复杂跨 Case 归因可提高推理强度。"}</small></div><div class="field full"><label>CATX 远端运行环境</label><div class="catx-inheritance ${state.catxDefault.configured ? "ready" : "needs-setup"}"><i></i><span>${state.catxDefault.configured ? `已使用安全存储配置 · ${state.catxDefault.vault_ids.length} 个 Vault` : "请在其他服务配置中补充 Vault ID"}</span>${state.catxDefault.configured ? "" : `<button type="button" data-action="settings">前往设置</button>`}</div><small>API Key、MIS、Agent、环境和仓库 Token 来自其他服务配置；创建时生成冻结 Profile，初测、基线和复验共用。</small></div><div class="field"><label>CATX Agent ID（可覆盖默认）</label><input name="catx_agent_id" placeholder="留空则继承其他服务配置"/></div><div class="field"><label>CATX Environment ID（可覆盖默认）</label><input name="catx_environment_id" placeholder="留空则继承其他服务配置"/></div></div></section>
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
    const profile = analysisProfile(); syncModelSelection(); const models = analysisModels(); const selected = models.find(item => item.id === state.createModelId); const efforts = selected?.reasoning_efforts || [];
    return `<div class="modal-backdrop"><section class="modal"><header class="modal-head"><div><span class="eyebrow">Local configuration</span><h3>安全配置与运行环境</h3></div><button data-action="close-modal">×</button></header>
      <section class="panel-section"><h4>环境体检</h4><div class="health-grid"><div><i class="${health.kernel?.ready ? "ok" : "bad"}"></i><span>Kernel</span><code>${esc(health.kernel?.bundled ? `内置 ${health.kernel.version}` : `开发模式 ${health.kernel?.version || "—"}`)}</code></div><div><i class="${health.git?.ready ? "ok" : "bad"}"></i><span>Git</span><code>${esc(health.git?.version || health.git?.error || "未发现")}</code></div><div><i class="${health.d2c?.ready ? "ok" : "bad"}"></i><span>D2C Worker</span><code>${esc(health.d2c?.ready ? `${health.d2c.chrome} · ${health.d2c.node}` : (health.d2c?.missing || []).join("；"))}</code></div><div><i class="${health.codex?.ready ? "ok" : "bad"}"></i><span>Codex CLI</span><code>${esc(health.codex?.version || health.codex?.error || "未发现")}</code></div></div></section>
      <section class="panel-section"><div class="model-section-head"><div><h4>本地分析大脑 · Codex / Claude Code</h4><p>自动识别 CC Switch 当前应用的 Codex 或 Claude Code 配置，复制到 FORGE 隔离 Profile，不修改原文件。</p></div><span class="phase-pill ${profile.ready && models.length ? "pass" : "warn"}">${profile.ready && models.length ? "READY" : "SETUP"}</span></div><div class="codex-source-choice"><button class="primary-button" type="button" data-action="import-codex-ccswitch">自动读取 CC Switch</button><span>依次识别 <code>~/.codex</code> 与 <code>~/.claude/settings.json</code> 的当前代理配置。</span></div>${profile.provider === "claude" ? `<div class="model-test pass"><i></i><span>当前来源：Claude Code · CC Switch · ${esc(profile.inference_mode || "Messages API")}</span></div>` : `<div class="import-grid"><article class="import-card ${profile.config_ready ? "complete" : ""}"><b>01</b><span><strong>config.toml</strong><small>${profile.config_ready ? `已隔离导入 · ${short(profile.imports?.config?.sha256, 12)}` : "Codex 模型与 provider 配置"}</small></span><button type="button" data-action="import-codex" data-kind="config">${profile.config_ready ? "重新导入" : "选择文件"}</button></article><article class="import-card ${profile.auth_ready ? "complete" : ""}"><b>02</b><span><strong>auth.json</strong><small>${profile.auth_ready ? `已安全导入 · ${short(profile.imports?.auth?.sha256, 12)}` : "Codex 认证副本，权限 0600"}</small></span><button type="button" data-action="import-codex" data-kind="auth">${profile.auth_ready ? "重新导入" : "选择文件"}</button></article></div>`}<div class="model-picker"><div class="field"><label>可用分析模型</label><select id="settings-model" ${models.length ? "" : "disabled"}>${models.map(item => `<option value="${esc(item.id)}" ${item.id === state.createModelId ? "selected" : ""}>${esc(item.display_name)}${item.is_default ? " · 默认" : ""}${item.probe?.ready ? " · 已验证" : ""}</option>`).join("") || `<option>导入完成后读取</option>`}</select></div><div class="field"><label>推理强度</label><select id="settings-effort" ${efforts.length ? "" : "disabled"}>${efforts.map(item => `<option value="${esc(item.id)}" ${item.id === state.createReasoningEffort ? "selected" : ""}>${esc(item.id)}</option>`).join("") || `<option>由模型配置决定</option>`}</select></div></div><div class="model-actions">${profile.provider === "codex" ? `<button class="ghost-button dark-ghost" data-action="refresh-codex" type="button" ${profile.ready ? "" : "disabled"}>读取真实模型列表</button>` : ""}<button class="primary-button" data-action="test-codex" type="button" ${models.length ? "" : "disabled"}>调用一次验证模型</button></div>${state.modelTest ? `<div class="model-test ${state.modelTest.ready ? "pass" : "fail"}"><i></i><span>${state.modelTest.ready ? "真实调用成功" : `模型验证未通过：${esc(state.modelTest.error || "响应未通过探针")}`} · ${esc(state.modelTest.model)} · ${Number(state.modelTest.duration_seconds || 0).toFixed(1)}s</span></div>` : ""}</section>
      ${profile.provider === "claude" ? `<div class="model-actions"><button class="ghost-button dark-ghost" data-action="refresh-codex" type="button">读取真实模型列表</button></div>` : ""}
      <form id="secret-form"><section class="panel-section"><div class="model-section-head"><div><h4>其他服务密钥与远端运行环境</h4><p>以下内容直接构成 CATX 默认运行环境；任务只生成隔离 Profile，不再要求额外 Profile JSON。</p></div><span class="phase-pill ${state.catxDefault.configured ? "pass" : "warn"}">${state.catxDefault.configured ? "READY" : "SETUP"}</span></div><div class="field full action-gap"><label>Vault IDs（每行一个）</label><textarea name="vault_ids" required placeholder="vlt_example">${esc((state.catxDefault.vault_ids || []).join("\n"))}</textarea><small>与下方 CATX_API_KEY、USER_MIS_ID、CATX_AGENT_ID、CATX_ENV_ID 和仓库 Token 一起用于所有远端评测。</small></div><div class="settings-grid">${secretNames.map(name => `<div class="field"><label>${name} ${state.secrets.includes(name) ? "· 已配置" : ""}</label><div class="secret-field"><input type="password" name="${name}" autocomplete="off" placeholder="${state.secrets.includes(name) ? "输入新值以替换" : "尚未配置"}"/>${state.secrets.includes(name) ? `<button class="danger-button" type="button" data-action="clear-secret" data-name="${name}">清除</button>` : ""}</div></div>`).join("")}</div><button class="primary-button action-gap" type="submit">保存远端运行配置</button></section></form>
      <section class="panel-section"><h4>预览浏览器插件</h4><p>只接受解压目录、固定 SHA-256 和权限白名单；插件仅进入预览 Profile，D2C 判定 Worker 始终禁用插件。</p>${state.extensions.map(item => `<div class="plugin-row"><span>${esc(item.name)} · ${esc(item.version)}</span><code>${esc(short(item.sha256, 12))}</code></div>`).join("") || `<p>没有安装受控插件。</p>`}<button class="ghost-button action-gap dark-ghost" data-action="install-extension" type="button">安装受控插件</button></section></section></div>`;
    return `<div class="modal-backdrop"><section class="modal"><header class="modal-head"><div><span class="eyebrow">Local configuration</span><h3>安全配置与运行环境</h3></div><button data-action="close-modal">×</button></header><section class="panel-section"><h4>环境体检</h4><div class="health-grid"><div><i class="${health.kernel?.ready ? "ok" : "bad"}"></i><span>Kernel</span><code>${esc(health.kernel?.bundled ? `内置 ${health.kernel.version}` : `开发模式 ${health.kernel?.version || "—"}`)}</code></div><div><i class="${health.git?.ready ? "ok" : "bad"}"></i><span>Git</span><code>${esc(health.git?.version || health.git?.error || "未发现")}</code></div><div><i class="${health.d2c?.ready ? "ok" : "bad"}"></i><span>D2C Worker</span><code>${esc(health.d2c?.ready ? `${health.d2c.chrome} · ${health.d2c.node}` : (health.d2c?.missing || []).join("；"))}</code></div><div><i class="${health.codex?.ready ? "ok" : "bad"}"></i><span>Codex CLI</span><code>${esc(health.codex?.version || health.codex?.error || "未发现")}</code></div></div></section><section class="panel-section"><div class="model-section-head"><div><h4>本地分析大脑 · Codex</h4><p>配置只复制到 FORGE 隔离 Profile，不修改任何原文件。${profile.connection_mode === "cc-switch" ? "当前通过 CC Switch 本地流式代理连接。" : profile.inference_mode === "responses-direct" ? "当前使用低 Token Responses 主路径。" : "当前使用 Codex App Server 兼容路径。"}</p></div><span class="phase-pill ${profile.ready && models.length ? "pass" : "warn"}">${profile.ready && models.length ? "READY" : "SETUP"}</span></div><div class="codex-source-choice"><button class="primary-button" type="button" data-action="import-codex-ccswitch">自动读取 CC Switch</button><span>读取当前 <code>~/.codex</code> 中由 CC Switch 应用的配置；也可继续手动选择下面两个文件。</span></div><div class="import-grid"><article class="import-card ${profile.config_ready ? "complete" : ""}"><b>01</b><span><strong>config.toml</strong><small>${profile.config_ready ? `已隔离导入 · ${short(profile.imports?.config?.sha256, 12)}` : "只提取模型与 provider 配置"}</small></span><button type="button" data-action="import-codex" data-kind="config">${profile.config_ready ? "重新导入" : "选择文件"}</button></article><article class="import-card ${profile.auth_ready ? "complete" : ""}"><b>02</b><span><strong>auth.json</strong><small>${profile.auth_ready ? `已安全导入 · ${short(profile.imports?.auth?.sha256, 12)}` : "独立保存，权限 0600"}</small></span><button type="button" data-action="import-codex" data-kind="auth">${profile.auth_ready ? "重新导入" : "选择文件"}</button></article></div><div class="model-picker"><div class="field"><label>可用 GPT 模型</label><select id="settings-model" ${models.length ? "" : "disabled"}>${models.map(item => `<option value="${esc(item.id)}" ${item.id === state.createModelId ? "selected" : ""}>${esc(item.display_name)}${item.is_default ? " · 默认" : ""}${item.probe?.ready ? " · 已验证" : item.probe && !item.probe.ready ? " · 验证未通过" : ""}</option>`).join("") || `<option>导入完成后读取</option>`}</select></div><div class="field"><label>推理强度</label><select id="settings-effort" ${models.length ? "" : "disabled"}>${efforts.map(item => `<option value="${esc(item.id)}" ${item.id === state.createReasoningEffort ? "selected" : ""}>${esc(item.id)}</option>`).join("") || `<option>medium</option>`}</select></div></div><div class="model-actions"><button class="ghost-button dark-ghost" data-action="refresh-codex" type="button" ${profile.ready ? "" : "disabled"}>读取真实模型列表</button><button class="primary-button" data-action="test-codex" type="button" ${models.length ? "" : "disabled"}>调用一次验证模型</button></div>${state.modelTest ? `<div class="model-test ${state.modelTest.ready ? "pass" : "fail"}"><i></i><span>${state.modelTest.ready ? "真实调用成功" : `模型验证未通过：${esc(state.modelTest.error || "响应未通过探针")}`} · ${esc(state.modelTest.model)} · ${esc(state.modelTest.reasoning_effort)} · ${Number(state.modelTest.duration_seconds || 0).toFixed(1)}s${state.modelTest.usage?.total_tokens ? ` · ${state.modelTest.usage.total_tokens} tokens` : ""}</span></div>` : ""}</section><form id="catx-default-form"><section class="panel-section"><div class="model-section-head"><div><h4>CATX 默认运行环境</h4><p>新建任务自动继承此 Profile 和 Vault IDs；Agent 与环境 ID 可在单个任务中覆盖。密钥、MIS 与仓库 Token 仍只保存在系统安全存储。</p></div><span class="phase-pill ${state.catxDefault.configured ? "pass" : "warn"}">${state.catxDefault.configured ? "READY" : "SETUP"}</span></div><div class="fields"><div class="field full"><label>默认 Profile JSON</label><div class="inline-input"><input name="profile_path" required value="${esc(state.catxDefault.profile_path || "")}" placeholder="选择不含凭据的 CATX Profile JSON"/><button type="button" data-action="browse-file" data-target="profile_path">选择</button></div></div><div class="field full"><label>Vault IDs（每行一个）</label><textarea name="vault_ids" required placeholder="vlt_example">${esc((state.catxDefault.vault_ids || []).join("\n"))}</textarea><small>创建 CATX Session 时将作为 vault_ids 参数传入。</small></div></div><button class="primary-button action-gap" type="submit">保存默认 CATX 环境</button></section></form><form id="secret-form"><section class="panel-section"><h4>其他服务密钥</h4><div class="settings-grid">${secretNames.map(name => `<div class="field"><label>${name} ${state.secrets.includes(name) ? "· 已配置" : ""}</label><div class="secret-field"><input type="password" name="${name}" autocomplete="off" placeholder="${state.secrets.includes(name) ? "输入新值以替换" : "尚未配置"}"/>${state.secrets.includes(name) ? `<button class="danger-button" type="button" data-action="clear-secret" data-name="${name}">清除</button>` : ""}</div></div>`).join("")}</div><button class="primary-button action-gap" type="submit">保存到系统安全存储</button></section></form><section class="panel-section"><h4>预览浏览器插件</h4><p>只接受解压目录、固定 SHA-256 和权限白名单；插件仅进入预览 Profile，D2C 判定 Worker 始终禁用插件。</p>${state.extensions.map(item => `<div class="plugin-row"><span>${esc(item.name)} · ${esc(item.version)}</span><code>${esc(short(item.sha256, 12))}</code></div>`).join("") || `<p>没有安装受控插件。</p>`}<button class="ghost-button action-gap dark-ghost" data-action="install-extension" type="button">安装受控插件</button></section></section></div>`;
  }

  function render() {
    const viewScroll = app.querySelector(".view")?.scrollTop || 0;
    const inspectorScroll = app.querySelector(".inspector-body")?.scrollTop || 0;
    const reviewForm = app.querySelector("#design-review-form");
    if (reviewForm && state.selectedTaskId) {
      const reviewData = new FormData(reviewForm);
      state.designDraft[state.selectedTaskId] = { caseIds:reviewData.getAll("case").map(String), feedback:String(reviewData.get("feedback") || "") };
    }
    const activeForm = app.querySelector("#create-form");
    if (activeForm) {
      for (const element of activeForm.elements) {
        if (element.name && !["case_id","case_prompt","case_expected","model_id","reasoning_effort"].includes(element.name)) state.createDraft[element.name] = element.value;
      }
    }
    const content = state.view === "create" ? createView() : state.view === "detail" ? detailView() : emptyView();
    app.innerHTML = shell(content);
    if (state.view === "detail") {
      const view = app.querySelector(".view"); const inspectorBody = app.querySelector(".inspector-body");
      if (view) view.scrollTop = viewScroll;
      if (inspectorBody) inspectorBody.scrollTop = inspectorScroll;
    }
    if (state.view === "create") {
      const form = app.querySelector("#create-form");
      for (const [name,value] of Object.entries(state.createDraft)) {
        const element = form?.elements.namedItem(name);
        if (element && typeof element.value === "string") element.value = value;
      }
    }
  }

  async function refreshTasks(loadSelected = true) {
    const result = await call("tasks.list");
    state.tasks = result.tasks || [];
    if (state.selectedTaskId && !state.tasks.some(item => item.id === state.selectedTaskId)) state.selectedTaskId = null;
    if (loadSelected && state.selectedTaskId) {
      state.snapshot = await call("tasks.get", { task_id: state.selectedTaskId });
      if (state.snapshot.state.phase === "design_ready" && !state.planVisited.has(state.selectedTaskId)) {
        state.tab = "cases"; state.planVisited.add(state.selectedTaskId);
      }
    }
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
    const profile = analysisProfile(); const modelId = String(data.get("model_id") || "").trim();
    if (!profile.ready || !modelId) throw new Error("请先在设置中读取可用的 Codex 或 Claude Code 分析模型");
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
    const preparedCatx = await forge.prepareCatxProfile({
      agent_id:String(data.get("catx_agent_id") || "").trim(),
      environment_id:String(data.get("catx_environment_id") || "").trim(),
    });
    const config = {
      api_version:"aceval.kernel-config/v1", skill_repository:skill, code_repository:codeSsh ? repository("code", "/workspace/repo") : null,
      local_analysis:{ provider:profile.provider || "codex", profile_id:profile.id, model_id:modelId, reasoning_effort:String(data.get("reasoning_effort") || profile.models?.find(item => item.id === modelId)?.default_reasoning_effort || "medium"), timeout_seconds:180, max_prompt_chars:48000, max_output_chars:24000 },
      analysis_executor:{ provider:"local-forge" },
      trial_executor:{ provider:"catx", profile_path:preparedCatx.profile_path },
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
      else if (action === "new-task") { state.view = "create"; state.createCases = []; state.modal = null; syncModelSelection(); render(); }
      else if (action === "select-task") await selectTask(button.dataset.id);
      else if (action === "tab") { state.tab = button.dataset.tab; render(); }
      else if (action === "refresh") await refreshTasks(true);
      else if (action === "run-task") { await call("tasks.run", { task_id:state.selectedTaskId }); toast("已继续自动执行，遇到确认门会停下"); await refreshTasks(true); }
      else if (action === "restart-task") {
        if (!state.selectedTaskId) throw new Error("未选择任务");
        state.busy = "正在从不可变输入快照重建完整流程…"; render();
        const restarted = await call("tasks.restart", { task_id:state.selectedTaskId });
        state.selectedTaskId = restarted.task.id; state.view = "detail"; state.snapshot = await call("tasks.get", { task_id:state.selectedTaskId }); state.busy = null;
        toast("已创建全新重跑任务，原失败任务与审计记录保持不变"); await refreshTasks(true);
      }
      else if (action === "retry-failed") { await call("tasks.retry_failed", { task_id:state.selectedTaskId, purpose:button.dataset.purpose }); toast("失败流程已按当前配置重建，正在继续轮询"); await refreshTasks(true); }
      else if (action === "reject-design") { await call("tasks.confirm", { task_id:state.selectedTaskId, approve:false }); toast("评测方案已退回，任务停在可追溯阻塞状态"); await refreshTasks(true); }
      else if (action === "open-log") { state.busy = "正在读取完整会话日志…"; render(); const value = await call("tasks.log", { task_id:button.dataset.task, iteration:Number(button.dataset.iteration), purpose:button.dataset.purpose, case_id:button.dataset.case }); state.busy = null; state.modal = { type:"log", title:`${button.dataset.case} · ${button.dataset.purpose}`, value }; render(); }
      else if (action === "close-modal") { state.modal = null; render(); }
      else if (action === "settings") { state.secrets = await forge.secretStatus(); state.catxDefault = await forge.catxDefault(); state.extensions = await forge.listExtensions(); state.modelProfiles = (await forge.modelProfiles()).profiles || []; syncModelSelection(); state.modal = { type:"settings" }; render(); }
      else if (action === "add-case") { syncCasesFromDom(); state.createCases.push({ id:"", prompt:"", expected:"" }); render(); }
      else if (action === "remove-case") { syncCasesFromDom(); state.createCases.splice(Number(button.dataset.index), 1); render(); }
      else if (action === "browse-dir") { const value = await forge.selectDirectory(); if (value) document.querySelector(`[name="${button.dataset.target}"]`).value = value; }
      else if (action === "browse-file") { const value = await forge.selectFile([{ name:"JSON", extensions:["json"] }]); if (value) document.querySelector(`[name="${button.dataset.target}"]`).value = value; }
      else if (action === "reject-decision") { await call("tasks.confirm", { task_id:state.selectedTaskId, approve:false }); toast("本轮修改已拒绝，任务停在可追溯阻塞状态"); await refreshTasks(true); }
      else if (action === "open-preview") { const url = document.querySelector('#d2c-form [name="url"]').value; await forge.openPreview(url); }
      else if (action === "select-design") { const path = await forge.selectImage(); if (path) { const artifact = await forge.readArtifact(path); state.designImage = { path, data_url:artifact.data_url, sha256:await hashDataUrl(artifact.data_url) }; render(); } }
      else if (action === "install-extension") { state.busy = "正在校验插件清单、权限和内容哈希…"; render(); const installed = await forge.installExtension(); state.busy = null; if (installed) { state.extensions = await forge.listExtensions(); toast(`插件 ${installed.name} 已安装到受控预览 Profile`); } render(); }
      else if (action === "import-codex-ccswitch") {
        state.busy = "正在识别 CC Switch 当前 Codex / Claude Code 配置…";
        render();
        const imported = await forge.importCodexCcSwitch();
        state.busy = null;
        if (imported) {
          state.analysisProfileId = imported.id;
          localStorage.setItem("forge.analysisProfileId", imported.id);
          state.modelProfiles = (await forge.modelProfiles()).profiles || [];
          syncModelSelection();
          toast(`CC Switch ${imported.provider === "claude" ? "Claude Code" : "Codex"} 配置已复制到 FORGE 隔离 Profile`);
          render();
          state.busy = `正在通过 CC Switch 读取 ${imported.provider === "claude" ? "Claude" : "Codex"} 真实模型列表…`;
          render();
          try {
            await forge.refreshCodexProfile(imported.id);
            state.modelProfiles = (await forge.modelProfiles()).profiles || [];
            syncModelSelection();
            toast(`CC Switch 已就绪，读取到 ${analysisModels().length} 个分析模型`);
          } catch (refreshError) {
            toast(`CC Switch 配置已导入，但模型列表读取失败：${refreshError.message || String(refreshError)}`, true);
          } finally {
            state.busy = null;
          }
        }
        render();
      }
      else if (action === "import-codex") {
        const filename = button.dataset.kind === "config" ? "config.toml" : "auth.json";
        state.busy = `正在安全导入 Codex ${filename} 副本…`;
        render();
        const imported = await forge.importCodexFile(button.dataset.kind);
        state.busy = null;
        if (imported) {
          state.analysisProfileId = imported.id || "default";
          localStorage.setItem("forge.analysisProfileId", state.analysisProfileId);
          state.modelProfiles = (await forge.modelProfiles()).profiles || [];
          syncModelSelection();
          toast(`${filename} 已隔离导入，原文件未修改`);
          render();
          if (analysisProfile().ready) {
            state.busy = "文件导入成功，正在从 Codex 读取真实可用模型…";
            render();
            try {
              await forge.refreshCodexProfile("default");
              state.modelProfiles = (await forge.modelProfiles()).profiles || [];
              syncModelSelection();
              toast(`配置就绪，已读取 ${analysisModels().length} 个可用分析模型`);
            } catch (refreshError) {
              toast(`文件已导入，但模型列表读取失败：${refreshError.message || String(refreshError)}`, true);
            } finally {
              state.busy = null;
            }
          }
        }
        render();
      }
      else if (action === "refresh-codex") { const selectedProfile = analysisProfile(); state.busy = `正在读取 ${selectedProfile.provider === "claude" ? "Claude" : "Codex"} 真实模型列表…`; render(); await forge.refreshCodexProfile(selectedProfile.id); state.modelProfiles = (await forge.modelProfiles()).profiles || []; state.busy = null; syncModelSelection(); toast(`已读取 ${analysisModels().length} 个可用分析模型`); render(); }
      else if (action === "test-codex") { const model = document.querySelector("#settings-model")?.value || state.createModelId; const effort = document.querySelector("#settings-effort")?.value || state.createReasoningEffort; const selectedProfile = analysisProfile(); state.busy = "正在发起一次最小真实模型调用…"; render(); state.modelTest = await forge.testCodexProfile(selectedProfile.id, model, effort); state.busy = null; state.modelProfiles = (await forge.modelProfiles()).profiles || []; toast(state.modelTest.ready ? `${selectedProfile.provider === "claude" ? "Claude" : "Codex"} 模型调用成功` : (state.modelTest.error || "模型响应未通过探针"), !state.modelTest.ready); render(); }
      else if (action === "clear-secret") { await forge.saveSecrets({ [button.dataset.name]:null }); state.secrets = await forge.secretStatus(); toast(`${button.dataset.name} 已从安全存储移除`); render(); }
    } catch (error) { state.busy = null; toast(error.message || String(error), true); render(); }
  });

  app.addEventListener("change", (event) => {
    if (event.target.name === "model_id" || event.target.id === "settings-model") {
      state.createModelId = event.target.value; state.createReasoningEffort = null; syncModelSelection();
      const picker = document.querySelector(event.target.id === "settings-model" ? "#settings-effort" : '[name="reasoning_effort"]');
      const model = analysisModels().find(item => item.id === state.createModelId); const efforts = model?.reasoning_efforts || [];
      if (picker) picker.innerHTML = efforts.map(item => `<option value="${esc(item.id)}" ${item.id === state.createReasoningEffort ? "selected" : ""}>${esc(item.id)}</option>`).join("") || `<option value="${esc(state.createReasoningEffort || "medium")}">${esc(state.createReasoningEffort || "medium")}</option>`;
    }
    else if (event.target.name === "reasoning_effort" || event.target.id === "settings-effort") state.createReasoningEffort = event.target.value;
  });

  app.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      if (event.target.id === "create-form") await createTask(event.target);
      else if (event.target.id === "catx-default-form") {
        const data = new FormData(event.target);
        state.busy = "正在保存默认 CATX 运行环境…"; render();
        state.catxDefault = await forge.saveCatxDefault({ profile_path:String(data.get("profile_path") || "").trim(), vault_ids:parseLines(data.get("vault_ids")) });
        state.busy = null; toast("默认 CATX 环境已保存；新建任务将自动继承"); render();
      } else if (event.target.id === "decision-form") {
        const data = new FormData(event.target); const selected = data.getAll("proposal");
        if (!selected.length) throw new Error("至少选择一个修改提案");
        await call("tasks.confirm", { task_id:state.selectedTaskId, approve:true, selected_change_ids:selected, user_feedback:String(data.get("feedback") || "").trim() || null, continue:true });
        toast("修改范围已冻结，正在生成受控候选版本"); await refreshTasks(true);
      } else if (event.target.id === "design-review-form") {
        const data = new FormData(event.target); const selected = data.getAll("case");
        if (!selected.length) throw new Error("至少选择一个评测 Case");
        await call("tasks.confirm", { task_id:state.selectedTaskId, approve:true, selected_case_ids:selected, user_feedback:String(data.get("feedback") || "").trim() || null, continue:true });
        toast(`已批准 ${selected.length} 个 Case，正在创建线上会话`); await refreshTasks(true);
      } else if (event.target.id === "secret-form") {
        const data = new FormData(event.target); const values = {};
        for (const [name,value] of data) if (name !== "vault_ids" && String(value).trim()) values[name] = String(value).trim();
        const vaultIds = parseLines(data.get("vault_ids"));
        state.busy = "正在保存远端运行环境并重启 Kernel 服务…"; render();
        state.catxDefault = await forge.saveCatxDefault({ vault_ids:vaultIds });
        if (Object.keys(values).length) state.secrets = await forge.saveSecrets(values);
        state.busy = null; toast("远端运行环境已安全保存，新建任务将自动继承"); render();
      } else if (event.target.id === "d2c-form") {
        const data = new FormData(event.target); let actions = []; if (String(data.get("actions") || "").trim()) actions = JSON.parse(data.get("actions"));
        const request = { api_version:"aceval.d2c-validation-request/v1", case_id:String(data.get("case_id")).trim(), url:String(data.get("url")).trim(), candidate_commit:state.snapshot.state.challenger_commit || state.snapshot.state.champion_commit, actions, expected_title:String(data.get("expected_title") || "").trim() || null, visual_oracle:state.designImage ? { path:state.designImage.path, sha256:state.designImage.sha256, max_diff_ratio:.01, pixel_threshold:16 } : null, metadata:{ source:"desktop" } };
        await call("d2c.validate", { task_id:state.selectedTaskId, iteration:Number(state.snapshot.state.iteration || 0), profile:state.bootstrap.d2c_profile, request }); toast("D2C Worker 已启动，结果会写入任务证据流"); await refreshTasks(true);
      }
    } catch (error) { state.busy = null; toast(error.message || String(error), true); render(); }
  });

  async function init() {
    if (!forge) { app.innerHTML = `<main class="boot-screen"><p>桌面安全桥未加载，请使用 Electron 客户端启动。</p></main>`; return; }
    try {
      state.bootstrap = await call("system.bootstrap"); state.secrets = await forge.secretStatus(); state.catxDefault = await forge.catxDefault(); state.extensions = await forge.listExtensions(); state.modelProfiles = (await forge.modelProfiles()).profiles || []; syncModelSelection();
      const tasks = await call("tasks.list"); state.tasks = tasks.tasks || [];
      if (state.tasks[0]) { state.selectedTaskId = state.tasks[0].id; state.snapshot = await call("tasks.get", { task_id:state.selectedTaskId }); state.view = "detail"; }
      render();
      setInterval(async () => { if (state.view === "detail" && !state.busy && !state.modal) { try { await refreshTasks(true); } catch {} } }, 1800);
    } catch (error) { app.innerHTML = `<main class="boot-screen"><div class="brand-seal">!</div><p>${esc(error.message || error)}</p></main>`; }
  }

  window.__FORGE_READY__ = true;
  init();
})();
