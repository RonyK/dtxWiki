"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("dtxwikiDesktop", {
  getState: () => ipcRenderer.invoke("desktop:get-state"),
  useLocal: () => ipcRenderer.invoke("desktop:use-local"),
  addRemote: (server) => ipcRenderer.invoke("desktop:add-remote", server),
  useRemote: (serverId) => ipcRenderer.invoke("desktop:use-remote", serverId),
  removeRemote: (serverId) => ipcRenderer.invoke("desktop:remove-remote", serverId),
  getMcpState: () => ipcRenderer.invoke("desktop:get-mcp-state"),
  registerMcp: (request) => ipcRenderer.invoke("desktop:register-mcp", request),
  closeMcpWizard: () => ipcRenderer.invoke("desktop:close-mcp"),
});
