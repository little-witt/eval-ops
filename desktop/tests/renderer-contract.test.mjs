import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const html = await readFile(resolve(root, "renderer/index.html"), "utf8");
const renderer = await readFile(resolve(root, "renderer/app.js"), "utf8");
const preload = await readFile(resolve(root, "electron/preload.cjs"), "utf8");
const main = await readFile(resolve(root, "electron/main.cjs"), "utf8");

assert.match(html, /Content-Security-Policy/);
assert.doesNotMatch(html, /onclick\s*=/i);
assert.doesNotMatch(renderer, /require\s*\(/);
assert.match(preload, /contextBridge\.exposeInMainWorld/);
assert.match(main, /contextIsolation:\s*true/);
assert.match(main, /nodeIntegration:\s*false/);
assert.match(main, /sandbox:\s*true/);
assert.match(main, /mainWindow\.isDestroyed\(\)/);
assert.match(main, /mainWindow\.webContents\.isDestroyed\(\)/);
assert.match(main, /isQuitting\s*=\s*true/);
assert.match(main, /safeStorage/);
assert.match(main, /persist:forge-preview/);
assert.match(renderer, /case_aggregates/);
assert.match(renderer, /candidate_comparison/);
assert.match(renderer, /tasks\.log/);
assert.match(renderer, /d2c\.validate/);

process.stdout.write("renderer security and product contracts passed\n");
