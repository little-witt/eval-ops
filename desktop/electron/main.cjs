const { app, BrowserWindow, dialog, ipcMain, safeStorage, session } = require("electron");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const readline = require("node:readline");

const isDev = !app.isPackaged;
const projectRoot = isDev ? path.resolve(__dirname, "../..") : process.resourcesPath;
const rendererRoot = path.resolve(__dirname, "../renderer");
const taskRoot = path.resolve(process.env.ACEVAL_TASK_ROOT || (isDev ? path.join(projectRoot, ".aceval/tasks") : path.join(app.getPath("userData"), "tasks")));
const settingsPath = () => path.join(app.getPath("userData"), "secrets.bin");
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
    const args = isDev
      ? ["-m", "aceval.desktop_service", "--task-root", taskRoot]
      : ["--task-root", taskRoot];
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
    service.stderr.setEncoding("utf8");
    service.stderr.on("data", (chunk) => console.error("[kernel]", String(chunk).slice(-4000)));
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
      const timer = setTimeout(() => reject(new Error("Kernel 服务启动超时")), 15000);
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
    });
  })();
  try { return await serviceReady; } catch (error) { serviceReady = null; throw error; }
}

async function rpc(method, params) {
  await startService();
  if (!service?.stdin.writable) throw new Error("Kernel 服务不可用");
  const id = `rpc_${++serviceRequestId}`;
  return await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Kernel 请求超时：${method}`)); }, method === "tasks.create" ? 300000 : 30000);
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
