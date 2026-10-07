import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const API_VERSION = "aceval.d2c-browser-driver/v1";
const input = await new Promise((resolve, reject) => {
  let text = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => { text += chunk; });
  process.stdin.on("end", () => { try { resolve(JSON.parse(text)); } catch (error) { reject(error); } });
  process.stdin.on("error", reject);
});

let chrome;
let socket;
let userDataDir;
const consoleEvents = [];
const networkEvents = [];
const pending = new Map();
let messageId = 0;

function send(method, params = {}) {
  const id = ++messageId;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

function evaluate(expression, awaitPromise = true) {
  return send("Runtime.evaluate", { expression, awaitPromise, returnByValue: true });
}

async function waitForFile(path, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try { return await readFile(path, "utf8"); } catch { await delay(50); }
  }
  throw new Error("Chrome DevTools port did not become ready");
}

async function waitForSelector(selector, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const result = await evaluate(`Boolean(document.querySelector(${JSON.stringify(selector)}))`);
    if (result.result?.result?.value) return;
    await delay(100);
  }
  throw new Error(`selector not found: ${selector}`);
}

async function action(item) {
  await waitForSelector(item.selector, item.timeout_ms);
  const selector = JSON.stringify(item.selector);
  if (item.type === "click") {
    const result = await evaluate(`(() => { const el = document.querySelector(${selector}); el.click(); return true; })()`);
    if (result.exceptionDetails) throw new Error(`click failed: ${item.selector}`);
  } else if (item.type === "fill") {
    const value = JSON.stringify(item.value);
    const result = await evaluate(`(() => { const el = document.querySelector(${selector}); const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set; if (setter) setter.call(el, ${value}); else el.value = ${value}; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); return true; })()`);
    if (result.exceptionDetails) throw new Error(`fill failed: ${item.selector}`);
  }
}

async function compareScreenshots(actualBase64, oracle) {
  const expression = `(async () => {
    const load = (src) => new Promise((resolve, reject) => { const image = new Image(); image.onload=()=>resolve(image); image.onerror=()=>reject(new Error('cannot decode screenshot')); image.src=src; });
    const actual = await load('data:image/png;base64,${actualBase64}');
    const expected = await load('data:image/png;base64,${oracle.png_base64}');
    if (actual.width !== expected.width || actual.height !== expected.height) return {dimensions_match:false,actual_width:actual.width,actual_height:actual.height,expected_width:expected.width,expected_height:expected.height,diff_ratio:1,differing_pixels:actual.width*actual.height,diff_png_base64:null};
    const a=document.createElement('canvas'), e=document.createElement('canvas'), d=document.createElement('canvas');
    for (const c of [a,e,d]) { c.width=actual.width; c.height=actual.height; }
    const ac=a.getContext('2d'), ec=e.getContext('2d'), dc=d.getContext('2d'); ac.drawImage(actual,0,0); ec.drawImage(expected,0,0);
    const ap=ac.getImageData(0,0,a.width,a.height), ep=ec.getImageData(0,0,e.width,e.height), diff=dc.createImageData(d.width,d.height);
    let count=0, maxDelta=0;
    for(let i=0;i<ap.data.length;i+=4){ const delta=Math.max(Math.abs(ap.data[i]-ep.data[i]),Math.abs(ap.data[i+1]-ep.data[i+1]),Math.abs(ap.data[i+2]-ep.data[i+2]),Math.abs(ap.data[i+3]-ep.data[i+3])); maxDelta=Math.max(maxDelta,delta); const changed=delta>${oracle.pixel_threshold}; if(changed) count++; diff.data[i]=changed?255:ap.data[i]*.18; diff.data[i+1]=changed?55:ap.data[i+1]*.18; diff.data[i+2]=changed?35:ap.data[i+2]*.18; diff.data[i+3]=255; }
    dc.putImageData(diff,0,0);
    return {dimensions_match:true,actual_width:a.width,actual_height:a.height,expected_width:e.width,expected_height:e.height,diff_ratio:count/(a.width*a.height),differing_pixels:count,max_channel_delta:maxDelta,diff_png_base64:d.toDataURL('image/png').split(',')[1]};
  })()`;
  const response = await evaluate(expression, true);
  if (response.exceptionDetails) throw new Error("visual comparison failed");
  return response.result?.result?.value;
}

async function main() {
  if (input.api_version !== API_VERSION) throw new Error("driver api_version mismatch");
  userDataDir = await mkdtemp(join(tmpdir(), "aceval-d2c-"));
  const width = input.viewport.width;
  const height = input.viewport.height;
  chrome = spawn(input.chrome_executable, [
    "--headless=new", "--no-first-run", "--no-default-browser-check",
    "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
    "--disable-extensions", "--disable-sync", "--metrics-recording-only", "--mute-audio",
    "--hide-scrollbars", "--remote-debugging-port=0", `--user-data-dir=${userDataDir}`,
    `--window-size=${width},${height}`, `--force-device-scale-factor=${input.device_scale_factor}`,
    `--lang=${input.locale}`, "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  let chromeError = "";
  chrome.stderr.on("data", (chunk) => { chromeError = (chromeError + chunk.toString()).slice(-4000); });
  const activePort = await waitForFile(join(userDataDir, "DevToolsActivePort"));
  const port = activePort.split(/\r?\n/)[0].trim();
  const response = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" });
  if (!response.ok) throw new Error(`cannot create Chrome target: ${response.status}`);
  const target = await response.json();
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  socket.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.id && pending.has(event.id)) {
      const waiter = pending.get(event.id); pending.delete(event.id);
      if (event.error) waiter.reject(new Error(event.error.message)); else waiter.resolve(event);
      return;
    }
    if (event.method === "Runtime.consoleAPICalled") {
      consoleEvents.push({ type: event.params.type, timestamp: event.params.timestamp, args: event.params.args.map((arg) => arg.value ?? arg.description ?? arg.type) });
    } else if (event.method === "Runtime.exceptionThrown") {
      consoleEvents.push({ type: "exception", timestamp: event.params.timestamp, text: event.params.exceptionDetails?.text || "exception" });
    } else if (event.method === "Network.responseReceived") {
      const r = event.params.response;
      if (!r.url.startsWith("data:")) networkEvents.push({ type: "response", url: r.url, status: r.status, mime_type: r.mimeType, protocol: r.protocol });
    } else if (event.method === "Network.loadingFailed") {
      networkEvents.push({ type: "failed", request_id: event.params.requestId, error_text: event.params.errorText, canceled: Boolean(event.params.canceled) });
    }
  };
  await Promise.all([
    send("Page.enable"), send("Runtime.enable"), send("Network.enable"),
    send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: input.device_scale_factor, mobile: false }),
    send("Emulation.setTimezoneOverride", { timezoneId: input.timezone }),
    send("Emulation.setEmulatedMedia", { media: "screen", features: [
      { name: "prefers-color-scheme", value: input.color_scheme },
      { name: "prefers-reduced-motion", value: "reduce" },
    ] }),
  ]);
  const loadPromise = new Promise((resolve) => {
    const listener = (message) => {
      try { if (JSON.parse(message.data).method === "Page.loadEventFired") { socket.removeEventListener("message", listener); resolve(); } } catch {}
    };
    socket.addEventListener("message", listener);
  });
  await send("Page.navigate", { url: input.url });
  await Promise.race([loadPromise, delay(15000).then(() => { throw new Error("page load timed out"); })]);
  await evaluate(`(() => { const s=document.createElement('style'); s.dataset.aceval='stability'; s.textContent='*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}'; document.documentElement.appendChild(s); })()`);
  for (const item of input.actions || []) await action(item);
  await delay(input.stability_wait_ms);
  const titleResult = await evaluate("document.title");
  const title = titleResult.result?.result?.value || "";
  const domResult = await evaluate("document.documentElement.outerHTML");
  const dom = domResult.result?.result?.value || "";
  const metricsResult = await evaluate(`({url:location.href,title:document.title,width:innerWidth,height:innerHeight,devicePixelRatio,navigation:performance.getEntriesByType('navigation')[0]?.toJSON?.()||null})`);
  const screenshot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false, fromSurface: true });
  await writeFile(join(input.output_dir, "screenshot.png"), Buffer.from(screenshot.result.data, "base64"));
  await writeFile(join(input.output_dir, "dom.html"), dom, "utf8");
  await writeFile(join(input.output_dir, "console.json"), JSON.stringify(consoleEvents, null, 2) + "\n", "utf8");
  await writeFile(join(input.output_dir, "network.json"), JSON.stringify(networkEvents, null, 2) + "\n", "utf8");
  const titleMatched = input.expected_title == null || title === input.expected_title;
  let visual = null;
  if (input.visual_oracle) {
    visual = await compareScreenshots(screenshot.result.data, input.visual_oracle);
    if (visual?.diff_png_base64) {
      await writeFile(join(input.output_dir, "visual-diff.png"), Buffer.from(visual.diff_png_base64, "base64"));
      delete visual.diff_png_base64;
    }
    visual.reference_sha256 = input.visual_oracle.sha256;
    visual.max_diff_ratio = input.visual_oracle.max_diff_ratio;
    visual.pixel_threshold = input.visual_oracle.pixel_threshold;
    visual.passed = visual.dimensions_match && visual.diff_ratio <= input.visual_oracle.max_diff_ratio;
  }
  const success = titleMatched && (visual == null || visual.passed);
  const result = {
    api_version: API_VERSION,
    success,
    title,
    title_matched: titleMatched,
    final_url: metricsResult.result?.result?.value?.url || input.url,
    viewport: { width, height, device_scale_factor: input.device_scale_factor },
    console_events: consoleEvents.length,
    console_errors: consoleEvents.filter((item) => item.type === "error" || item.type === "exception").length,
    network_events: networkEvents.length,
    metrics: metricsResult.result?.result?.value || {},
    visual,
    failure: !titleMatched ? `expected title ${input.expected_title}, received ${title}` : (visual && !visual.passed ? `visual diff ratio ${visual.diff_ratio} exceeds ${visual.max_diff_ratio}` : null),
    chrome_stderr_tail: chromeError,
  };
  await writeFile(join(input.output_dir, "driver-result.json"), JSON.stringify(result, null, 2) + "\n", "utf8");
  return result;
}

let finalResult;
let finalExitCode = 0;
try {
  finalResult = await Promise.race([
    main(),
    delay(Number(input.timeout_ms || 58000)).then(() => { throw new Error("D2C driver deadline exceeded"); }),
  ]);
} catch (error) {
  finalResult = { api_version: API_VERSION, success: false, infrastructure_error: String(error?.stack || error) };
  finalExitCode = 2;
} finally {
  try { socket?.close(); } catch {}
  if (chrome) {
    try { chrome.kill("SIGTERM"); } catch {}
    if (chrome.exitCode == null) {
      await Promise.race([
        new Promise((resolve) => chrome.once("exit", resolve)),
        delay(1500),
      ]);
    }
    if (chrome.exitCode == null) {
      try { chrome.kill("SIGKILL"); } catch {}
    }
  }
  if (userDataDir) await rm(userDataDir, { recursive: true, force: true });
}
await new Promise((resolve) => process.stdout.write(JSON.stringify(finalResult), resolve));
process.exit(finalExitCode);
