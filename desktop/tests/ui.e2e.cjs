const { existsSync } = require("node:fs");
const { mkdir } = require("node:fs/promises");
const path = require("node:path");

function loadChromium() {
  const candidates = [
    process.env.PLAYWRIGHT_MODULE,
    "playwright",
    path.resolve(__dirname, "../../node_modules/playwright"),
    path.resolve(__dirname, "../node_modules/playwright"),
  ].filter(Boolean);
  const failures = [];
  for (const candidate of candidates) {
    try {
      const loaded = require(candidate);
      if (loaded?.chromium) return loaded.chromium;
      failures.push(`${candidate}（没有 chromium 导出）`);
    } catch (error) {
      failures.push(`${candidate}（${error.code || error.message}）`);
    }
  }
  throw new Error(`未找到可用的 Playwright。请先在本机安装 playwright，或设置 PLAYWRIGHT_MODULE 指向已安装模块。尝试过：${failures.join("；")}`);
}

let chromium;
try {
  chromium = loadChromium();
} catch (error) {
  console.error(`[ui.e2e] ${error.message}`);
  process.exit(2);
}

const task = { id:"review-skill-001", skill:{ name:"frontend-code-reviewer", source:"ssh://git@example/skill.git@feature/eval" }, scenario:"code-review", goal:"稳定发现高价值缺陷，输出可定位、可执行、无猜测的评审意见", status:"awaiting_confirmation", current_iteration:1 };
const state = { phase:"awaiting_confirmation", iteration:1, champion_commit:"a".repeat(40), challenger_commit:"b".repeat(40), candidate_status:"evaluating", environment_contract_hash:`sha256:${"c".repeat(64)}` };
const design = {
  cases:[{ id:"react-effect-cleanup", prompt:"评审当前分支相对 master 的改动" },{ id:"miniprogram-request-race", prompt:"检查请求竞态与过期响应覆盖" }],
  execution_paths:{
    "react-effect-cleanup":{ steps:[{ label:"读取候选 Skill 指令",kind:"required" },{ label:"检查变更与上下文",kind:"required" },{ label:"只输出证据充分的问题",kind:"forbidden" }] },
    "miniprogram-request-race":{ steps:[{ label:"读取候选 Skill 指令",kind:"required" },{ label:"定位异步状态写入",kind:"recommended" }] },
  },
};
const decision = {
  stable_pass_case_ids:["react-effect-cleanup"], failed_case_ids:["miniprogram-request-race"], flaky_case_ids:[],
  evidence_health:{ valid_attempts:4, invalid_attempts:0 }, token_economy:{ model_calls:1, estimated_prompt_tokens:1830 },
  case_aggregates:[{ case_id:"react-effect-cleanup",status:"pass",stable_pass:true },{ case_id:"miniprogram-request-race",status:"fail",stable_pass:false }],
  candidate_comparison:{ accepted:false, paired_mean_delta:-.05, reasons:["critical_regression"], hard_regression_case_ids:["react-effect-cleanup"] },
  convergence:{ converged:false, remaining_failed_case_ids:["miniprogram-request-race"], max_rounds:5 },
  analysis_artifacts:{ agent_call_receipt_history:["semantic.json","attribution.json","proposal.json"] },
  diagnosis_graph:{ clusters:[{ id:"missing-async-causality",case_ids:["miniprogram-request-race"],root_cause_hypothesis:"Skill 没有要求验证异步响应的时序归属",skill_change_authorized:true }], proposals:[{ id:"proposal-001",target:"references/review-rules.md",change:"增加异步竞态的证据检查表",why:"补齐跨端共用的时序判定" }] },
};
const events = [
  [1,"kernel.created",null],[2,"evaluation.design_compiled",null],[3,"case_run.started","react-effect-cleanup"],[4,"case_run.completed","react-effect-cleanup"],[5,"case_run.started","miniprogram-request-race"],[6,"case_run.completed","miniprogram-request-race"],[7,"analysis.completed",null],[8,"optimization.proposal_ready",null],
].map(([seq,type,case_id]) => ({ seq,event_id:`review-skill-001:${String(seq).padStart(8,"0")}`,timestamp:new Date(2026,7,26,10,20,seq).toISOString(),type,iteration:1,case_id,run_id:case_id?"evaluation":null,payload:type==="evaluation.design_compiled"?{case_count:2,generated_case_count:1,pack_source:"reused"}:type==="analysis.completed"?{stable_pass_case_ids:["react-effect-cleanup"],failed_case_ids:["miniprogram-request-race"],next_action:"await_user_confirmation"}:type==="case_run.started"?{session_id:`session-${seq}`}:{}}));
const snapshot = { task,state,design,decision,events,iterations:[{ name:"iteration-001",session_logs:[{case_id:"react-effect-cleanup",purpose:"evaluation",session_id:"session-3",status:"completed",artifact:"/tmp/log.json"},{case_id:"miniprogram-request-race",purpose:"evaluation",session_id:"session-5",status:"completed",artifact:"/tmp/log2.json"}] }] };
const modelProfile = { id:"default",provider:"codex",ready:true,config_ready:true,auth_ready:true,codex_ready:true,inference_mode:"responses-direct",imports:{config:{sha256:"a".repeat(64)},auth:{sha256:"b".repeat(64)}},models:[{id:"gpt-5-test",model:"gpt-5-test",display_name:"GPT-5 Test",description:"Fixture local analysis model",is_default:true,is_gpt:true,default_reasoning_effort:"medium",probe:{ready:true},reasoning_efforts:[{id:"low",description:"Fast"},{id:"medium",description:"Balanced"},{id:"high",description:"Deep"}]}] };

let browser;
(async () => {
  const executableCandidates = [
    process.env.PLAYWRIGHT_EXECUTABLE_PATH,
    process.env.CHROME_PATH,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ].filter(Boolean);
  const executablePath = executableCandidates.find((candidate) => existsSync(candidate));
  const launchOptions = { headless:true };
  if (executablePath) launchOptions.executablePath = executablePath;
  try {
    browser = await chromium.launch(launchOptions);
  } catch (error) {
    throw new Error(`无法启动 Playwright 浏览器${executablePath ? `（${executablePath}）` : ""}：${error.message}。可设置 PLAYWRIGHT_EXECUTABLE_PATH，或执行 npx playwright install chromium。`);
  }
  const page = await browser.newPage({ viewport:{ width:1540,height:980 }, deviceScaleFactor:1 });
  const errors = [];
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(({ snapshot, task, state, modelProfile }) => {
    const summary = { id:task.id,skill_name:task.skill.name,status:task.status,current_iteration:1,updated_at:new Date().toISOString(),phase:state.phase,champion_commit:state.champion_commit,active_operation:null };
    window.forge = {
      platform:"darwin",
      rpc:async(method) => {
        if(method==="system.bootstrap") return { version:"0.2.1",d2c_profile:{},d2c_health:{ready:true,versions:{chrome:"Chrome 140",node:"v24"}},environment_health:{kernel:{ready:true,version:"0.2.1"},git:{ready:true,version:"git 2.50"},d2c:{ready:true,chrome:"Chrome 140",node:"v24"},codex:{ready:true,version:"codex-cli 0.148.0"}} };
        if(method==="tasks.list") return {tasks:[summary]};
        if(method==="tasks.get") return snapshot;
        if(method==="tasks.log") return {session:{observation:{output:"发现 1 个问题",trace:[{kind:"tool_call",name:"read_file"},{kind:"tool_result",content:"source"}]}}};
        return {};
      },
      secretStatus:async()=>["CATX_API_KEY"], catxDefault:async()=>({configured:true,vault_ids:["vlt_test"]}), saveCatxDefault:async value=>({configured:true,...value}), prepareCatxProfile:async()=>({profile_path:"/tmp/task-catx.json",inherited:true,vault_ids:["vlt_test"]}), listExtensions:async()=>[], readArtifact:async()=>({data_url:"data:image/png;base64,iVBORw0KGgo="}),
      modelProfiles:async()=>({profiles:[modelProfile]}), importCodexFile:async()=>modelProfile, importCodexCcSwitch:async()=>modelProfile, refreshCodexProfile:async()=>modelProfile, testCodexProfile:async()=>({ready:true,model:"gpt-5-test",reasoning_effort:"medium",duration_seconds:1.2}),
      selectDirectory:async()=>null, selectFile:async()=>null, selectImage:async()=>null, openPreview:async()=>({opened:true}), saveSecrets:async()=>[], installExtension:async()=>null,
    };
  }, { snapshot,task,state,modelProfile });
  await page.goto("http://127.0.0.1:8877/index.html");
  await page.getByRole("heading", { name:"frontend-code-reviewer" }).waitFor();
  await page.getByRole("button", { name:"Case / 路径" }).click();
  await page.locator(".case-card").filter({ hasText:"react-effect-cleanup" }).first().waitFor();
  await page.getByRole("button", { name:"会话日志" }).click();
  await page.getByText("查看用户消息、工具调用、返回和终止事件").first().click();
  await page.getByText("不可变会话证据").waitFor();
  await page.locator('[data-action="close-modal"]').click();
  await page.getByRole("button", { name:"分析 / 决策" }).click();
  await page.getByText("增加异步竞态的证据检查表").waitFor();
  const output = path.resolve(__dirname,"../../docs/screenshots"); await mkdir(output,{recursive:true});
  await page.screenshot({ path:path.join(output,"forge-desktop-task-detail.png"), fullPage:true });
  await page.locator('[data-action="new-task"]').first().click();
  await page.getByRole("heading", { name:"创建一次可追溯的 Skill 升级" }).waitFor();
  await page.screenshot({ path:path.join(output,"forge-desktop-create-task.png"), fullPage:true });
  await page.locator('[data-action="settings"]').first().click();
  await page.getByRole("heading", { name:"安全配置与运行环境" }).waitFor();
  await page.getByText("Codex / Claude Code").waitFor();
  await page.screenshot({ path:path.join(output,"forge-desktop-codex-settings.png"), fullPage:true });
  if (errors.length) throw new Error(`browser errors: ${errors.join(" | ")}`);
  process.stdout.write(JSON.stringify({ok:true,screenshots:3})+"\n");
  await browser.close();
})().catch(async error => { console.error(error); process.exitCode=1; try { await browser?.close(); } catch {} });
