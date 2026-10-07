const { app, BrowserWindow, dialog, ipcMain, safeStorage, session } = require("electron");
const { spawn, spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const fsp = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");

const isDev = !app.isPackaged;
const projectRoot = isDev ? path.resolve(__dirname, "../..") : process.resourcesPath;
const rendererRoot = path.resolve(__dirname, "../renderer");
const taskRoot = path.resolve(process.env.ACEVAL_TASK_ROOT || (isDev ? path.join(projectRoot, ".aceval/tasks") : path.join(app.getPath("userData"), "tasks")));
const modelProfileRoot = path.resolve(process.env.ACEVAL_MODEL_PROFILE_ROOT || (isDev ? path.join(projectRoot, ".aceval/model-profiles") : path.join(app.getPath("userData"), "model-profiles")));
const settingsPath = () => path.join(app.getPath("userData"), "secrets.bin");
const catxSettingsPath = () => path.join(app.getPath("userData"), "catx", "default.json");
const catxProfileRoot = () => path.join(app.getPath("userData"), "catx", "task-profiles");
const extensionsPath = () => path.join(app.getPath("userData"), "extensions");
const ALLOWED_SECRET_NAMES = new Set([
  "CATX_API_KEY", "USER_MIS_ID", "CATX_AGENT_ID", "CATX_ENV_ID",
  "CATX_REPOSITORY_AUTHORIZATION_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
]);
const ALLOWED_EXTENSION_PERMISSIONS = new Set(["activeTab", "storage", "scripting", "tabs"]);
const selectedPaths = new Set();

let mainWindow;
let service;
let serviceReady;
let serviceRequestId = 0;
let isQuitting = false;
const pending = new Map();

function usableCodexExecutable(candidate) {
  if (!candidate) return null;
  const resolved = path.resolve(candidate);
  try { fs.accessSync(resolved, fs.constants.X_OK); } catch { return null; }
  const checked = spawnSync(resolved, ["--version"], {
    encoding: "utf8",
    timeout: 2000,
    shell: false,
    env: {
      ...process.env,
      // NVM launchers use `/usr/bin/env node`; make the candidate use its own Node.
      PATH: `${path.dirname(resolved)}${path.delimiter}${process.env.PATH || ""}`,
    },
  });
  const version = String(checked.stdout || checked.stderr || "").trim();
  return checked.status === 0 && /^codex-cli\s+\S+/m.test(version) ? resolved : null;
}

function findCodexExecutable() {
  const candidates = [process.env.ACEVAL_CODEX_EXECUTABLE];
  for (const directory of String(process.env.PATH || "").split(path.delimiter)) {
    if (directory) candidates.push(path.join(directory, process.platform === "win32" ? "codex.exe" : "codex"));
  }
  const home = os.homedir();
  candidates.push(
    path.join(home, ".local/bin/codex"),
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
  );
  const nvmRoot = path.join(home, ".nvm/versions/node");
  try {
    const versions = fs.readdirSync(nvmRoot).sort((left, right) => right.localeCompare(left, undefined, { numeric:true, sensitivity:"base" }));
    for (const version of versions) candidates.push(path.join(nvmRoot, version, "bin/codex"));
  } catch {}
  for (const candidate of candidates) {
    const ready = usableCodexExecutable(candidate);
    if (ready) return ready;
  }
  return null;
}

function sendToRenderer(channel, payload) {
  if (
    isQuitting ||
    !mainWindow ||
    mainWindow.isDestroyed() ||
    !mainWindow.webContents ||
    mainWindow.webContents.isDestroyed()
  ) return false;
  mainWindow.webContents.send(channel, payload);
  return true;
}

function inside(parent, child) {
  const relative = path.relative(path.resolve(parent), path.resolve(child));
  return relative === "" || (!relative.startsWith(".." + path.sep) && relative !== "..");
}

async function readSecrets() {
  try {
    if (!safeStorage.isEncryptionAvailable()) return {};
    const encrypted = await fsp.readFile(settingsPath());
    const parsed = JSON.parse(safeStorage.decryptString(encrypted));
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

async function writeSecrets(values) {
  if (!safeStorage.isEncryptionAvailable()) throw new Error("系统安全存储当前不可用");
  const current = await readSecrets();
  for (const [name, value] of Object.entries(values || {})) {
    if (!ALLOWED_SECRET_NAMES.has(name)) throw new Error(`不支持的密钥名：${name}`);
    if (value === null || value === "") delete current[name];
    else if (typeof value === "string" && value.length <= 16384 && !value.includes("\0")) current[name] = value;
    else throw new Error(`密钥 ${name} 的值无效`);
  }
  await fsp.mkdir(path.dirname(settingsPath()), { recursive: true });
  await fsp.writeFile(settingsPath(), safeStorage.encryptString(JSON.stringify(current)), { mode: 0o600 });
  return Object.keys(current).sort();
}

function safeCatxText(value, label, { required = false } = {}) {
  const text = String(value || "").trim();
  if (!text && !required) return "";
  if (!text || text.length > 1024 || /[\x00-\x1f]/.test(text)) throw new Error(`${label} 无效`);
  return text;
}

function safeVaultIds(value) {
  if (!Array.isArray(value)) throw new Error("Vault IDs 必须是数组");
  const vaultIds = value.map(item => safeCatxText(item, "Vault ID", { required:true }));
  if (!vaultIds.length || new Set(vaultIds).size !== vaultIds.length) throw new Error("至少配置一个不重复的 Vault ID");
  return vaultIds;
}

async function readCatxDefault() {
  try {
    const value = JSON.parse(await fsp.readFile(catxSettingsPath(), "utf8"));
    return { configured:true, vault_ids:safeVaultIds(value?.vault_ids) };
  } catch { return { configured:false, vault_ids:[] }; }
}

async function writeCatxDefault(value) {
  const vaultIds = safeVaultIds(value?.vault_ids);
  await fsp.mkdir(path.dirname(catxSettingsPath()), { recursive:true });
  await fsp.writeFile(catxSettingsPath(), JSON.stringify({ vault_ids:vaultIds }, null, 2), { mode:0o600 });
  return { configured:true, vault_ids:vaultIds };
}

async function prepareCatxProfile(value) {
  const defaults = await readCatxDefault();
  const vaultIds = value?.vault_ids === undefined ? defaults.vault_ids : safeVaultIds(value.vault_ids);
  if (!vaultIds.length) throw new Error("请先在其他服务配置中填写至少一个 Vault ID");
  const agentId = safeCatxText(value?.agent_id, "CATX Agent ID");
  const environmentId = safeCatxText(value?.environment_id, "CATX Environment ID");
  const output = {
    api_version:"aceval.catx-profile/v1",
    name:"forge-catx-default",
    base_url:"https://api.catx.sankuai.com/api/v1",
    api_key_env:"CATX_API_KEY",
    user_mis_id_env:"USER_MIS_ID",
    agent_id_env:"CATX_AGENT_ID",
    environment_id_env:"CATX_ENV_ID",
    vault_ids:vaultIds,
    default_title:"FORGE Skill evaluation",
  };
  const credentials = {};
  if (agentId) { output.agent_id_env = "FORGE_CATX_TASK_AGENT_ID"; credentials.FORGE_CATX_TASK_AGENT_ID = agentId; }
  if (environmentId) { output.environment_id_env = "FORGE_CATX_TASK_ENV_ID"; credentials.FORGE_CATX_TASK_ENV_ID = environmentId; }
  const token = crypto.randomUUID();
  await fsp.mkdir(catxProfileRoot(), { recursive:true });
  if (Object.keys(credentials).length) {
    const credentialsPath = path.join(catxProfileRoot(), `${token}.credentials.json`);
    await fsp.writeFile(credentialsPath, JSON.stringify(credentials), { mode:0o600 });
    output.credentials_file = credentialsPath;
  }
  const profilePath = path.join(catxProfileRoot(), `${token}.json`);
  await fsp.writeFile(profilePath, JSON.stringify(output, null, 2), { mode:0o600 });
  return { profile_path:profilePath, inherited:true, vault_ids:vaultIds };
}

async function stopService() {
  if (!service) return;
  for (const waiter of pending.values()) waiter.reject(new Error("Kernel 服务已重启"));
  pending.clear();
  service.kill("SIGTERM");
  service = null;
  serviceReady = null;
}

async function startService() {
  if (serviceReady) return serviceReady;
  serviceReady = (async () => {
    const sourceRoot = isDev ? path.join(projectRoot, "src") : path.join(process.resourcesPath, "aceval-src");
    const bundledKernel = path.join(process.resourcesPath, "forge-kernel", process.platform === "win32" ? "forge-kernel.exe" : "forge-kernel");
    const command = isDev ? (process.env.ACEVAL_PYTHON || "python3") : bundledKernel;
    const codexExecutable = findCodexExecutable();
    const args = isDev
      ? ["-m", "aceval.desktop_service", "--task-root", taskRoot, "--model-profile-root", modelProfileRoot]
      : ["--task-root", taskRoot, "--model-profile-root", modelProfileRoot];
    if (codexExecutable) args.push("--codex-executable", codexExecutable);
    if (!isDev && !fs.existsSync(bundledKernel)) throw new Error("应用内置 Kernel 缺失，请重新安装完整版本");
    const secrets = await readSecrets();
    service = spawn(command, args, {
      cwd: isDev ? projectRoot : app.getPath("userData"),
      env: {
        ...process.env,
        ...secrets,
        ...(isDev ? { PYTHONPATH: sourceRoot } : {}),
        // Electron already embeds a supported Node runtime.  Reuse it for the
        // isolated D2C driver so packaged users do not depend on an arbitrary
        // (and often obsolete) `node` found in a GUI process PATH.
        ACEVAL_D2C_NODE_EXECUTABLE: process.execPath,
        ACEVAL_D2C_USE_ELECTRON_NODE: "1",
      },
      stdio: ["pipe", "pipe", "pipe"],
      shell: false,
    });
    let startupStderr = "";
    service.stderr.setEncoding("utf8");
    service.stderr.on("data", (chunk) => {
      startupStderr = (startupStderr + String(chunk)).slice(-4000);
      console.error("[kernel]", String(chunk).slice(-4000));
    });
    service.on("exit", (code) => {
      const error = new Error(`Kernel 服务已退出（${code ?? "signal"}）`);
      for (const waiter of pending.values()) waiter.reject(error);
      pending.clear();
      service = null;
      serviceReady = null;
      sendToRenderer("forge:service-exit", { code });
    });
    const lines = readline.createInterface({ input: service.stdout });
    let resolved = false;
    return await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const detail = startupStderr.trim();
        reject(new Error(`Kernel 服务启动超时（60 秒）${detail ? `：${detail}` : ""}`));
      }, 60000);
      lines.on("line", (line) => {
        let message;
        try { message = JSON.parse(line); } catch { return; }
        if (message.type === "ready" && !resolved) {
          resolved = true;
          clearTimeout(timer);
          resolve(message);
          return;
        }
        if (!message.id || !pending.has(message.id)) return;
        const waiter = pending.get(message.id);
        pending.delete(message.id);
        if (message.ok) waiter.resolve(message.result);
        else waiter.reject(new Error(message.error?.message || "Kernel 请求失败"));
      });
      service.once("error", (error) => { clearTimeout(timer); reject(error); });
      service.once("exit", (code, signal) => {
        if (resolved) return;
        clearTimeout(timer);
        const detail = startupStderr.trim();
        reject(new Error(`Kernel 服务启动失败（${code ?? signal ?? "unknown"}）${detail ? `：${detail}` : ""}`));
      });
    });
  })();
  try { return await serviceReady; } catch (error) { serviceReady = null; throw error; }
}

async function rpc(method, params) {
  await startService();
  if (!service?.stdin.writable) throw new Error("Kernel 服务不可用");
  const id = `rpc_${++serviceRequestId}`;
  return await new Promise((resolve, reject) => {
    const timeout = ["tasks.create", "tasks.restart"].includes(method) ? 300000 : method === "models.test_codex" ? 120000 : method === "models.refresh_codex" ? 60000 : 30000;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Kernel 请求超时：${method}`)); }, timeout);
    pending.set(id, {
      resolve(value) { clearTimeout(timer); resolve(value); },
      reject(error) { clearTimeout(timer); reject(error); },
    });
    service.stdin.write(JSON.stringify({ id, method, params }) + "\n");
  });
}

function validateLoopback(raw) {
  let url;
  try { url = new URL(raw); } catch { throw new Error("请输入有效的页面地址"); }
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
    throw new Error("预览仅允许本机 HTTP 地址");
  }
  return url.toString();
}

async function hashDirectory(root) {
  const digest = crypto.createHash("sha256");
  async function walk(directory) {
    const entries = await fsp.readdir(directory, { withFileTypes: true });
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      if (entry.isSymbolicLink()) throw new Error("插件目录不能包含符号链接");
      const full = path.join(directory, entry.name);
      const relative = path.relative(root, full).split(path.sep).join("/");
      if (entry.isDirectory()) await walk(full);
      else if (entry.isFile()) { digest.update(relative); digest.update(await fsp.readFile(full)); }
    }
  }
  await walk(root);
  return digest.digest("hex");
}

async function copyDirectory(source, target) {
  await fsp.cp(source, target, { recursive: true, errorOnExist: true, force: false, dereference: false });
}

async function installExtension() {
  const selected = await dialog.showOpenDialog(mainWindow, { properties: ["openDirectory"], title: "选择解压后的 Chrome 插件目录" });
  if (selected.canceled || !selected.filePaths[0]) return null;
  const source = path.resolve(selected.filePaths[0]);
  const manifestPath = path.join(source, "manifest.json");
  const manifest = JSON.parse(await fsp.readFile(manifestPath, "utf8"));
  if (![2, 3].includes(manifest.manifest_version) || typeof manifest.name !== "string" || typeof manifest.version !== "string") throw new Error("插件 manifest 无效");
  for (const permission of manifest.permissions || []) {
    if (!ALLOWED_EXTENSION_PERMISSIONS.has(permission)) throw new Error(`插件权限不在允许清单：${permission}`);
  }
  for (const host of manifest.host_permissions || []) {
    if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\/\*/.test(host)) throw new Error(`插件只能申请本机预览地址权限：${host}`);
  }
  const sha256 = await hashDirectory(source);
  const target = path.join(extensionsPath(), sha256);
  await fsp.mkdir(extensionsPath(), { recursive: true });
  if (!fs.existsSync(target)) await copyDirectory(source, target);
  const extension = await session.fromPartition("persist:forge-preview").loadExtension(target, { allowFileAccess: false });
  const record = { id: extension.id, name: extension.name, version: extension.version, sha256, path: target, installed_at: new Date().toISOString() };
  await fsp.writeFile(path.join(target, ".forge-install.json"), JSON.stringify(record, null, 2), { mode: 0o600 });
  return record;
}

async function listExtensions() {
  try {
    const directories = await fsp.readdir(extensionsPath(), { withFileTypes: true });
    const result = [];
    for (const entry of directories) {
      if (!entry.isDirectory()) continue;
      try { result.push(JSON.parse(await fsp.readFile(path.join(extensionsPath(), entry.name, ".forge-install.json"), "utf8"))); } catch {}
    }
    return result;
  } catch { return []; }
}

async function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1540,
    height: 980,
    minWidth: 1180,
    minHeight: 760,
    titleBarStyle: "hiddenInset",
    backgroundColor: "#151a17",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (url !== mainWindow.webContents.getURL()) event.preventDefault();
  });
  await mainWindow.loadFile(path.join(rendererRoot, "index.html"));
  mainWindow.once("closed", () => { mainWindow = null; });
  mainWindow.once("ready-to-show", () => mainWindow.show());
  if (process.argv.includes("--smoke-test")) {
    const ready = await mainWindow.webContents.executeJavaScript(`new Promise((resolve) => {
      const deadline = Date.now() + 15000;
      const check = () => {
        if (window.__FORGE_READY__ && document.querySelector('.shell')) return resolve(true);
        if (Date.now() > deadline) return resolve(false);
        setTimeout(check, 100);
      };
      check();
    })`);
    process.stdout.write(JSON.stringify({ ok: ready, renderer: "loaded" }) + "\n");
    app.quit();
  }
}

ipcMain.handle("forge:rpc", (_event, method, params) => rpc(method, params));
ipcMain.handle("forge:model-profiles", () => rpc("models.list", {}));
ipcMain.handle("forge:import-codex-file", async (_event, kind) => {
  if (!["config", "auth"].includes(kind)) throw new Error("不支持的 Codex 配置类型");
  const filename = kind === "config" ? "config.toml" : "auth.json";
  const projectCandidate = isDev ? path.join(projectRoot, "config", filename) : "";
  const userCandidate = path.join(os.homedir(), ".codex", filename);
  const defaultPath = fs.existsSync(projectCandidate) ? projectCandidate : userCandidate;
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ["openFile"],
    title: kind === "config" ? "导入 Codex config.toml 副本" : "导入 Codex auth.json 副本",
    defaultPath,
    filters: kind === "config" ? [{ name:"Codex TOML", extensions:["toml"] }] : [{ name:"Codex Auth JSON", extensions:["json"] }],
  });
  if (result.canceled || !result.filePaths[0]) return null;
  return await rpc("models.import_codex_file", { profile_id:"default", kind, source_path:result.filePaths[0] });
});
ipcMain.handle("forge:import-codex-cc-switch", () => rpc("models.import_cc_switch", {
  profile_id:"default",
  config_path:path.join(os.homedir(), ".codex", "config.toml"),
  auth_path:path.join(os.homedir(), ".codex", "auth.json"),
  claude_settings_path:path.join(os.homedir(), ".claude", "settings.json"),
}));
ipcMain.handle("forge:refresh-codex-profile", (_event, profileId) => rpc("models.refresh_codex", { profile_id:String(profileId || "default") }));
ipcMain.handle("forge:test-codex-profile", (_event, profileId, modelId, reasoningEffort) => rpc("models.test_codex", {
  profile_id:String(profileId || "default"), model_id:String(modelId || ""), reasoning_effort:String(reasoningEffort || "medium"),
}));
ipcMain.handle("forge:select-directory", async () => {
  const result = await dialog.showOpenDialog(mainWindow, { properties: ["openDirectory", "createDirectory"] });
  if (result.canceled || !result.filePaths[0]) return null;
  selectedPaths.add(path.resolve(result.filePaths[0]));
  return result.filePaths[0];
});
ipcMain.handle("forge:select-file", async (_event, filters) => {
  const result = await dialog.showOpenDialog(mainWindow, { properties: ["openFile"], filters: Array.isArray(filters) ? filters : [] });
  if (result.canceled || !result.filePaths[0]) return null;
  selectedPaths.add(path.resolve(result.filePaths[0]));
  return result.filePaths[0];
});
ipcMain.handle("forge:select-image", async () => {
  const result = await dialog.showOpenDialog(mainWindow, { properties: ["openFile"], filters: [{ name: "设计稿", extensions: ["png", "jpg", "jpeg", "webp"] }] });
  if (result.canceled || !result.filePaths[0]) return null;
  selectedPaths.add(path.resolve(result.filePaths[0]));
  return result.filePaths[0];
});
ipcMain.handle("forge:open-preview", async (_event, raw) => {
  const url = validateLoopback(raw);
  const preview = new BrowserWindow({
    width: 1440, height: 900, title: "FORGE · D2C Preview", backgroundColor: "#ffffff",
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true, partition: "persist:forge-preview" },
  });
  preview.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  await preview.loadURL(url);
  return { opened: true, url };
});
ipcMain.handle("forge:read-artifact", async (_event, rawPath) => {
  const target = path.resolve(String(rawPath || ""));
  if (!inside(taskRoot, target) && ![...selectedPaths].some((root) => inside(root, target) || root === target)) throw new Error("无权读取该产物路径");
  const stat = await fsp.stat(target);
  if (!stat.isFile() || stat.size > 20 * 1024 * 1024) throw new Error("产物不是可预览的常规文件");
  const extension = path.extname(target).toLowerCase();
  const mime = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".json": "application/json", ".html": "text/html" }[extension] || "text/plain";
  return { path: target, mime, data_url: `data:${mime};base64,${(await fsp.readFile(target)).toString("base64")}` };
});
ipcMain.handle("forge:secret-status", async () => Object.keys(await readSecrets()).sort());
ipcMain.handle("forge:catx-default", readCatxDefault);
ipcMain.handle("forge:save-catx-default", (_event, value) => writeCatxDefault(value));
ipcMain.handle("forge:prepare-catx-profile", (_event, value) => prepareCatxProfile(value));
ipcMain.handle("forge:save-secrets", async (_event, values) => {
  const names = await writeSecrets(values);
  await stopService();
  await startService();
  return names;
});
ipcMain.handle("forge:install-extension", installExtension);
ipcMain.handle("forge:list-extensions", listExtensions);

app.whenReady().then(async () => {
  await startService();
  await createMainWindow();
  app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createMainWindow(); });
}).catch((error) => { dialog.showErrorBox("FORGE 启动失败", error.stack || String(error)); app.quit(); });

app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
app.on("before-quit", () => {
  isQuitting = true;
  if (service) service.kill("SIGTERM");
});
