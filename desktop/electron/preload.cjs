const { contextBridge, ipcRenderer } = require("electron");

const RPC_METHODS = new Set([
  "system.bootstrap", "tasks.list", "tasks.get", "tasks.events", "tasks.create",
  "tasks.run", "tasks.confirm", "tasks.log", "operations.get", "d2c.check", "d2c.validate",
]);

contextBridge.exposeInMainWorld("forge", Object.freeze({
  rpc(method, params = {}) {
    if (!RPC_METHODS.has(method)) return Promise.reject(new Error("Unsupported desktop method"));
    return ipcRenderer.invoke("forge:rpc", method, params);
  },
  selectDirectory() { return ipcRenderer.invoke("forge:select-directory"); },
  selectFile(filters = []) { return ipcRenderer.invoke("forge:select-file", filters); },
  selectImage() { return ipcRenderer.invoke("forge:select-image"); },
  openPreview(url) { return ipcRenderer.invoke("forge:open-preview", url); },
  readArtifact(path) { return ipcRenderer.invoke("forge:read-artifact", path); },
  secretStatus() { return ipcRenderer.invoke("forge:secret-status"); },
  saveSecrets(values) { return ipcRenderer.invoke("forge:save-secrets", values); },
  installExtension() { return ipcRenderer.invoke("forge:install-extension"); },
  listExtensions() { return ipcRenderer.invoke("forge:list-extensions"); },
  platform: process.platform,
}));
