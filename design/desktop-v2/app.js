const bootParams = new URLSearchParams(window.location.search);
if (bootParams.get("theme") === "v3") {
  const theme = document.createElement("link");
  theme.rel = "stylesheet";
  theme.href = "../desktop-v3/styles.css";
  document.head.appendChild(theme);
  document.body.classList.add("theme-v3");
  document.querySelector(".prototype-flag")?.replaceChildren("DESIGN PROTOTYPE · V3");
}

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const inspector = $("#inspector-content");
const inspectorTitle = $(".inspector-head h3");
const toast = $("#toast");
let toastTimer;
let replayTimers = [];

const liveInspector = () => `
  <div class="case-hero">
    <div class="case-hero-line"><span class="case-origin">USER CASE</span><span class="case-state"><i></i>会话运行中 · 01:18</span></div>
    <h4>跨文件状态竞态</h4>
    <p>验证 Agent 是否按 Skill 清单读取完整改动范围，识别 useEffect 闭包中的旧状态读取，并给出可复核证据。</p>
  </div>
  <section class="inspector-section">
    <div class="inspector-section-head"><strong>预期路径 × 实际执行</strong><span>SESSION ses_7Qf…9a2</span></div>
    <div class="execution-path">
      <div class="execution-step is-done"><i>✓</i><span><strong>读取改动范围</strong><small>实际：git diff + 读取 4 个关联文件</small></span><em>00:07</em></div>
      <div class="execution-step is-done"><i>✓</i><span><strong>建立跨文件调用关系</strong><small>实际：定位 useOrderState → CheckoutPanel</small></span><em>00:31</em></div>
      <div class="execution-step is-active"><i>3</i><span><strong>按 Skill 清单逐项检查</strong><small>当前：状态、闭包与副作用检查 · 6/9</small></span><em>LIVE</em></div>
      <div class="execution-step"><i>4</i><span><strong>证据化输出并去噪</strong><small>等待：文件、行号、触发链、严重度</small></span><em>—</em></div>
    </div>
  </section>
  <section class="inspector-section">
    <div class="inspector-section-head"><strong>实时会话日志</strong><button>完整日志 ↗</button></div>
    <div class="event-log">
      <div class="log-toolbar"><span><i></i>SSE CONNECTED · 100% 完整</span><button>自动滚动 ON</button></div>
      <div class="log-lines">
        <div class="log-line"><time>14:04:21</time><b>status_running</b><span>Agent 开始处理评测任务</span></div>
        <div class="log-line tool"><time>14:04:25</time><b>tool_use</b><span>git diff --name-only origin/master…</span></div>
        <div class="log-line tool"><time>14:04:29</time><b>tool_result</b><span>4 files changed · 96 insertions</span></div>
        <div class="log-line"><time>14:04:34</time><b>path.match</b><span>step_01 · 读取改动范围</span></div>
        <div class="log-line tool"><time>14:04:41</time><b>tool_use</b><span>rg "useOrderState" src/</span></div>
        <div class="log-line is-highlight"><time>14:04:48</time><b>path.match</b><span>step_02 · 建立跨文件调用关系</span></div>
        <div class="log-line"><time>14:04:49</time><b>message.chunk</b><span>检查闭包中的状态读取与依赖…</span></div>
      </div>
    </div>
    <div class="evidence-link"><b>PATH STEP 02</b><span>对应日志 events 18–27 →</span></div>
  </section>`;

const caseGenerationInspector = () => `
  <div class="case-hero">
    <div class="case-hero-line"><span class="case-origin">GENERATING</span><span class="case-state"><i></i>Case 7 / 8</span></div>
    <h4>评测 Case 与路径正在生长</h4>
    <p>系统正把用户输入、Skill 必经步骤和 EvalPack 维度编译为独立可执行的测试单元。</p>
  </div>
  <section class="inspector-section">
    <div class="inspector-section-head"><strong>当前生成 · CR-007</strong><span>边界 / 路径反例</span></div>
    <div class="execution-path">
      <div class="execution-step is-done"><i>✓</i><span><strong>目标与来源</strong><small>来源：Skill §3.2「逐项执行检查清单」</small></span><em>DONE</em></div>
      <div class="execution-step is-done"><i>✓</i><span><strong>输入 Fixture</strong><small>构造会诱发“提前下结论”的跨文件改动</small></span><em>DONE</em></div>
      <div class="execution-step is-active"><i>3</i><span><strong>执行路径契约</strong><small>正在生成 4 个必经步骤与证据锚点…</small></span><em>LIVE</em></div>
      <div class="execution-step"><i>4</i><span><strong>Oracle 与去重检查</strong><small>等待路径契约完成</small></span><em>—</em></div>
    </div>
  </section>
  <section class="inspector-section">
    <div class="inspector-section-head"><strong>流式产出</strong><span>自动保存</span></div>
    <div class="event-log">
      <div class="log-toolbar"><span><i></i>LOCAL AGENT STREAM</span><button>12.4k tokens</button></div>
      <div class="log-lines">
        <div class="log-line"><time>14:03:04</time><b>case.goal</b><span>检测未完成清单便提前输出建议</span></div>
        <div class="log-line"><time>14:03:08</time><b>case.input</b><span>fixture/cr-007 · 3 files</span></div>
        <div class="log-line is-highlight"><time>14:03:13</time><b>path.step</b><span>必须枚举 Skill 检查项与实际证据</span></div>
        <div class="log-line"><time>14:03:16</time><b>path.step</b><span>禁止在检查项完成前输出最终结论</span></div>
        <div class="log-line"><time>14:03:19</time><b>oracle</b><span>path_compliance ≥ 0.9</span></div>
      </div>
    </div>
  </section>`;

const decisionInspector = () => `
  <div class="analysis-inspector">
    <div class="decision-hero"><span>ISSUE CLUSTER 01 · HIGH IMPACT</span><h4>跨文件检查没有形成不可跳过的执行门</h4><p>3 个失败 Case 均在读取首个文件后直接给出结论，说明问题不是单个规则缺失，而是路径约束只存在于说明文本，未被脚本和输出协议共同保证。</p></div>
    <section class="inspector-section">
      <div class="inspector-section-head"><strong>证据范围</strong><button>查看全部 11 条证据 →</button></div>
      <div class="problem-evidence"><div><small>失败 Case</small><strong>3 / 8</strong></div><div><small>路径偏离</small><strong>7 steps</strong></div><div><small>影响维度</small><strong>路径 · 召回</strong></div><div><small>置信度</small><strong>0.91</strong></div></div>
    </section>
    <section class="inspector-section">
      <div class="inspector-section-head"><strong>建议的兼容修改</strong><span>已检查建议间冲突</span></div>
      <div class="proposal-list">
        <label class="proposal-item"><input type="checkbox" checked/><span><strong>将跨文件扫描固化为强制门</strong><p>在主路径中增加不可跳过的范围清单；未完成时禁止进入输出阶段。</p><span class="proposal-files"><code>SKILL.md</code><code>scripts/checklist.ts</code></span></span></label>
        <label class="proposal-item"><input type="checkbox" checked/><span><strong>输出协议携带路径完成证据</strong><p>让最终建议引用检查项与文件范围，支持评测器验证步骤执行。</p><span class="proposal-files"><code>references/output-contract.md</code><code>templates/review.md</code></span></span></label>
        <label class="proposal-item"><input type="checkbox" checked/><span><strong>无问题场景增加静默门槛</strong><p>仅保留有代码证据的问题；泛化提醒移动到可选附录。</p><span class="proposal-files"><code>SKILL.md</code></span></span></label>
      </div>
    </section>
    <section class="inspector-section">
      <div class="inspector-section-head"><strong>补充意见或约束</strong><span>可选</span></div>
      <textarea class="feedback-field" placeholder="例如：不要改变现有严重度分级；同时覆盖微信小程序的跨文件检查…"></textarea>
    </section>
    <div class="route-preview"><i>↻</i><span><strong>建议路由：优化后使用同一冻结 Case 集重新评测</strong><small>目标与覆盖未变化，可直接进行可比回归；通过项仍会复测一次。</small></span></div>
    <div class="decision-actions"><button class="approve" data-toast="设计稿：已记录确认，下一步将生成候选 diff">通过 3 项建议并优化</button><button data-toast="设计稿：已进入补充 Case 路由">补充 Case</button></div>
  </div>`;

const packInspector = () => `
  <div class="case-hero">
    <div class="case-hero-line"><span class="case-origin">EVALPACK V3</span><span class="case-state" style="color:var(--moss)">✓ 已冻结</span></div>
    <h4>代码评审目标契约</h4>
    <p>用户无感生成的评测定义。高级用户仍可查看和自定义评分规则、Scenario 与 Oracle。</p>
  </div>
  <section class="inspector-section"><div class="inspector-section-head"><strong>生成依据</strong><span>3 sources</span></div><div class="problem-evidence"><div><small>用户标准</small><strong>4</strong></div><div><small>Skill 步骤</small><strong>9</strong></div><div><small>复用模板</small><strong>62%</strong></div><div><small>新增规则</small><strong>5</strong></div></div></section>
  <section class="inspector-section"><div class="inspector-section-head"><strong>质量门</strong><span>4 / 4 PASS</span></div><div class="execution-path"><div class="execution-step is-done"><i>✓</i><span><strong>目标覆盖完整</strong><small>所有用户标准均映射到评分维度</small></span><em>PASS</em></div><div class="execution-step is-done"><i>✓</i><span><strong>Case 可执行</strong><small>环境、输入与判定器均可解析</small></span><em>PASS</em></div><div class="execution-step is-done"><i>✓</i><span><strong>Oracle 可信</strong><small>正例、反例与边界完成校准</small></span><em>PASS</em></div><div class="execution-step is-done"><i>✓</i><span><strong>定义冻结</strong><small>hash 71e0…e92 · 后续可复现</small></span><em>PASS</em></div></div></section>`;

const genericInspector = (title, description) => `
  <div class="case-hero"><div class="case-hero-line"><span class="case-origin">TRACE NODE</span><span class="case-state" style="color:var(--moss)">✓ 可追溯</span></div><h4>${title}</h4><p>${description}</p></div>
  <section class="inspector-section"><div class="inspector-section-head"><strong>来源与版本</strong><span>IMMUTABLE SNAPSHOT</span></div><div class="problem-evidence"><div><small>任务修订</small><strong>rev 03</strong></div><div><small>轮次</small><strong>02</strong></div><div><small>事件</small><strong>18</strong></div><div><small>状态</small><strong>已落盘</strong></div></div></section>`;

function renderInspector(kind = "live") {
  if (!inspector) return;
  if (kind === "cases") {
    inspectorTitle.textContent = "Case / 路径 · 流式生成";
    inspector.innerHTML = caseGenerationInspector();
  } else if (kind === "analysis") {
    inspectorTitle.textContent = "问题 01 · 优化决策";
    inspector.innerHTML = decisionInspector();
  } else if (kind === "pack" || kind.startsWith("dimension")) {
    inspectorTitle.textContent = kind.startsWith("dimension") ? "评测维度 · 规则与来源" : "EvalPack v3 · 评测设计";
    inspector.innerHTML = packInspector();
  } else if (kind === "input") {
    inspectorTitle.textContent = "任务输入 · 不可变快照";
    inspector.innerHTML = genericInspector("任务目标与约束", "保存用户的原始目标、Case、标准、非目标和运行配置；后续每次补充都会生成新的修订版本，不覆盖历史。 ");
  } else if (kind === "optimize") {
    inspectorTitle.textContent = "候选变更 · 回归路由";
    inspector.innerHTML = genericInspector("多文件候选尚未生成", "只有用户确认优化范围后才会修改隔离 worktree；每一处 diff 将关联问题、建议和证据，并通过本地验证。 ");
  } else {
    inspectorTitle.textContent = "CR-004 · 执行证据";
    inspector.innerHTML = liveInspector();
  }
  bindDynamicActions();
  inspector.scrollTop = 0;
}

function showToast(message) {
  clearTimeout(toastTimer);
  $("p", toast).textContent = message;
  toast.classList.add("is-visible");
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 2400);
}

function bindDynamicActions() {
  $$('[data-toast]', inspector).forEach((button) => {
    button.onclick = () => showToast(button.dataset.toast);
  });
}

function showScreen(name) {
  $$(".screen").forEach((screen) => screen.classList.remove("is-visible"));
  const target = name === "task" ? $("#task-screen") : name === "create" ? $("#create-screen") : $("#placeholder-screen");
  target.classList.add("is-visible");
  $$(".rail-button").forEach((button) => button.classList.toggle("is-active", button.dataset.screen === name || (name === "create" && button.dataset.screen === "task")));
  if (name === "task") renderInspector(document.body.dataset.moment === "decision" ? "analysis" : document.body.dataset.moment === "cases" ? "cases" : "live");
}

function setMoment(moment, { announce = false } = {}) {
  replayTimers.forEach(clearTimeout);
  replayTimers = [];
  document.body.dataset.moment = moment;
  $$("[data-moment]").forEach((button) => button.classList.toggle("is-active", button.dataset.moment === moment));
  if (moment === "cases") {
    renderInspector("cases");
    if (announce) showToast("正在流式生成 Case 与执行路径");
  } else if (moment === "decision") {
    renderInspector("analysis");
    if (announce) showToast("评测完成，3 项优化建议等待确认");
  } else {
    renderInspector("live");
    if (announce) showToast("8 个线上会话正在并发评测");
  }
}

function replay() {
  setMoment("cases", { announce: true });
  replayTimers.push(setTimeout(() => setMoment("evaluation", { announce: true }), 2200));
  replayTimers.push(setTimeout(() => setMoment("decision", { announce: true }), 4600));
}

$$('[data-screen]').forEach((button) => button.addEventListener("click", () => showScreen(button.dataset.screen)));
$$('[data-moment]').forEach((button) => button.addEventListener("click", () => setMoment(button.dataset.moment)));
$$("[data-replay]").forEach((button) => button.addEventListener("click", replay));

$$('[data-focus]').forEach((button) => {
  button.addEventListener("click", () => {
    const target = $(`[data-node="${button.dataset.focus}"]`);
    target?.scrollIntoView({ behavior: "smooth", block: "center" });
    renderInspector(button.dataset.focus === "evaluation" ? "live" : button.dataset.focus);
  });
});

$$('[data-node]').forEach((node) => {
  node.addEventListener("click", (event) => {
    event.stopPropagation();
    renderInspector(node.dataset.node);
  });
});

$$('[data-case]').forEach((node) => {
  node.addEventListener("click", (event) => {
    event.stopPropagation();
    $$(".case-node, .session-row").forEach((item) => item.classList.remove("is-selected"));
    node.classList.add("is-selected");
    renderInspector("live");
  });
});

$$('[data-issue]').forEach((node) => node.addEventListener("click", () => renderInspector("analysis")));
$$('[data-toast]').forEach((button) => button.addEventListener("click", () => showToast(button.dataset.toast)));

$("[data-launch]")?.addEventListener("click", () => {
  showScreen("task");
  setMoment("cases");
  $("#task-screen").scrollTop = 0;
  showToast("任务 task_4F9C 已创建，正在生成 EvalPack 与 Case 路径");
});

const initialView = bootParams.get("view") || "task";
const initialMoment = bootParams.get("moment") || "evaluation";
showScreen(initialView === "create" ? "create" : initialView === "task" ? "task" : "placeholder");
if (initialView !== "create") setMoment(["cases", "evaluation", "decision"].includes(initialMoment) ? initialMoment : "evaluation");
