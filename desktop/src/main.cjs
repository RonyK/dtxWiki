"use strict";

const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { app, BrowserWindow, dialog, ipcMain, Menu, session, shell } = require("electron");
const electronUpdater = require("electron-updater");

const {
  defaultConfig,
  readConfig,
  upsertRemoteServer,
  writeConfig,
} = require("./config.cjs");
const {
  resolveEngineExecutable,
  startLocalEngine,
  stopLocalEngine,
} = require("./engine.cjs");
const { runEngineBridge } = require("./mcp.cjs");
const { canWriteClipboard, isAllowedNavigation } = require("./security.cjs");
const { createUpdateManager } = require("./updater.cjs");

const CONFIG_FILENAME = "desktop-config.json";
const WELCOME_PATH = path.join(__dirname, "welcome", "index.html");
const WELCOME_URL = pathToFileURL(WELCOME_PATH).href;
const MCP_PATH = path.join(__dirname, "mcp", "index.html");
const MCP_URL = pathToFileURL(MCP_PATH).href;

let configPath;
let desktopConfig = defaultConfig();
let localEngine = null;
let pendingEngine = null;
let mainWindow = null;
let mcpWindow = null;
let quitting = false;
let updateManager = null;
let connectionGeneration = 0;

function publicState() {
  return {
    lastConnection: desktopConfig.lastConnection,
    remoteServers: desktopConfig.remoteServers,
  };
}

function isWelcomeSender(event) {
  return event.senderFrame?.url === WELCOME_URL;
}

function requireWelcomeSender(event) {
  if (!isWelcomeSender(event)) {
    throw new Error("This desktop action is only available from the connection screen.");
  }
}

function isMcpSender(event) {
  return event.senderFrame?.url === MCP_URL;
}

function requireMcpSender(event) {
  if (!isMcpSender(event)) {
    throw new Error("This desktop action is only available from the MCP wizard.");
  }
}

function saveConfig() {
  desktopConfig = writeConfig(configPath, desktopConfig);
}

function safeExternalUrl(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}

function lockWindowToLocation(window, allowedLocation) {
  window.webContents.on("will-navigate", (event, targetUrl) => {
    if (isAllowedNavigation(targetUrl, allowedLocation)) return;
    event.preventDefault();
    const externalUrl = safeExternalUrl(targetUrl);
    if (externalUrl) {
      void shell.openExternal(externalUrl);
    }
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    const externalUrl = safeExternalUrl(url);
    if (externalUrl) {
      void shell.openExternal(externalUrl);
    }
    return { action: "deny" };
  });
}

function createBrowserWindow(options = {}) {
  const { webPreferences = {}, ...windowOptions } = options;
  return new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 760,
    minHeight: 560,
    show: false,
    backgroundColor: "#f7f8fc",
    title: "dtxWiki",
    autoHideMenuBar: process.platform !== "darwin",
    ...windowOptions,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      navigateOnDragDrop: false,
      ...webPreferences,
    },
  });
}

function engineExecutable() {
  return resolveEngineExecutable({
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    appPath: app.getAppPath(),
  });
}

function replaceWindow(nextWindow, { alreadyReady = false } = {}) {
  const previous = mainWindow;
  mainWindow = nextWindow;
  const reveal = () => {
    nextWindow.show();
    if (previous && !previous.isDestroyed()) {
      previous.destroy();
    }
  };
  if (alreadyReady) {
    reveal();
  } else {
    nextWindow.once("ready-to-show", reveal);
  }
  nextWindow.on("closed", () => {
    if (mainWindow === nextWindow) {
      mainWindow = null;
    }
  });
}

async function showWelcome() {
  const generation = ++connectionGeneration;
  await stopAllEngines();
  if (generation !== connectionGeneration || quitting) return;
  const window = createBrowserWindow({
    webPreferences: { preload: path.join(__dirname, "preload.cjs") },
  });
  lockWindowToLocation(window, WELCOME_URL);
  replaceWindow(window);
  await window.loadFile(WELCOME_PATH);
  rebuildMenu();
}

async function stopAllEngines() {
  const engines = [...new Set([localEngine, pendingEngine].filter(Boolean))];
  localEngine = null;
  pendingEngine = null;
  await Promise.all(engines.map((engine) => stopLocalEngine(engine)));
}

async function connectLocal() {
  const generation = ++connectionGeneration;
  await stopAllEngines();
  if (generation !== connectionGeneration || quitting) return;
  const loading = createBrowserWindow();
  replaceWindow(loading);
  await loading.loadFile(path.join(__dirname, "welcome", "loading.html"));

  const executable = engineExecutable();
  try {
    localEngine = await startLocalEngine({
      executable,
      logsDirectory: app.getPath("logs"),
      preferredPort: desktopConfig.localPort,
      onSpawn: (engine) => {
        if (generation !== connectionGeneration || quitting) {
          throw new Error("local engine start cancelled");
        }
        pendingEngine = engine;
      },
    });
    const candidateEngine = localEngine;
    if (pendingEngine === candidateEngine) pendingEngine = null;
    if (
      generation !== connectionGeneration ||
      quitting ||
      mainWindow !== loading ||
      loading.isDestroyed()
    ) {
      if (localEngine === candidateEngine) localEngine = null;
      await stopLocalEngine(candidateEngine);
      return;
    }
    lockWindowToLocation(loading, localEngine.origin);
    await loading.loadURL(localEngine.origin);
    desktopConfig.localPort = localEngine.port;
    desktopConfig.lastConnection = { type: "local" };
    saveConfig();
    rebuildMenu();
  } catch (error) {
    if (generation !== connectionGeneration || quitting) return;
    await stopAllEngines();
    dialog.showErrorBox(
      "dtxWiki 로컬 엔진을 시작할 수 없습니다",
      `${error.message}\n\n로그: ${app.getPath("logs")}`,
    );
    await showWelcome();
  }
}

async function connectRemote(serverId) {
  const generation = ++connectionGeneration;
  const server = desktopConfig.remoteServers.find((item) => item.id === serverId);
  if (!server) {
    throw new Error("저장된 서버를 찾을 수 없습니다.");
  }
  if (pendingEngine) {
    const engine = pendingEngine;
    pendingEngine = null;
    await stopLocalEngine(engine);
  }
  if (generation !== connectionGeneration || quitting) return;
  const window = createBrowserWindow();
  lockWindowToLocation(window, new URL(server.url).origin);
  try {
    await window.loadURL(server.url);
  } catch (error) {
    window.destroy();
    throw new Error(`서버를 열 수 없습니다: ${error.message}`);
  }
  if (generation !== connectionGeneration || quitting) {
    window.destroy();
    return;
  }
  await stopAllEngines();
  if (generation !== connectionGeneration || quitting) {
    window.destroy();
    return;
  }
  replaceWindow(window, { alreadyReady: true });
  desktopConfig.lastConnection = { type: "remote", serverId: server.id };
  saveConfig();
  rebuildMenu();
}

async function showMcpWizard() {
  if (mcpWindow && !mcpWindow.isDestroyed()) {
    mcpWindow.show();
    mcpWindow.focus();
    return;
  }
  mcpWindow = createBrowserWindow({
    width: 820,
    height: 780,
    minWidth: 620,
    minHeight: 620,
    parent: mainWindow ?? undefined,
    webPreferences: { preload: path.join(__dirname, "preload.cjs") },
  });
  lockWindowToLocation(mcpWindow, MCP_URL);
  mcpWindow.on("closed", () => {
    mcpWindow = null;
  });
  await mcpWindow.loadFile(MCP_PATH);
  mcpWindow.show();
}

async function openLastConnection() {
  const last = desktopConfig.lastConnection;
  if (last?.type === "local") {
    await connectLocal();
    return;
  }
  if (last?.type === "remote") {
    try {
      await connectRemote(last.serverId);
      return;
    } catch (error) {
      dialog.showErrorBox("원격 dtxWiki에 연결할 수 없습니다", error.message);
    }
  }
  await showWelcome();
}

function rebuildMenu() {
  const connectionItems = [
    {
      label: "내 컴퓨터",
      type: "radio",
      checked: desktopConfig.lastConnection?.type === "local",
      click: () => void connectLocal(),
    },
    ...desktopConfig.remoteServers.map((server) => ({
      label: server.name,
      type: "radio",
      checked:
        desktopConfig.lastConnection?.type === "remote" &&
        desktopConfig.lastConnection.serverId === server.id,
      click: () => {
        void connectRemote(server.id).catch((error) => {
          dialog.showErrorBox("원격 dtxWiki에 연결할 수 없습니다", error.message);
        });
      },
    })),
    { type: "separator" },
    { label: "연결 관리…", click: () => void showWelcome() },
  ];

  const template = [
    ...(process.platform === "darwin"
      ? [{ label: app.name, submenu: [{ role: "about" }, { type: "separator" }, { role: "quit" }] }]
      : []),
    { label: "연결", submenu: connectionItems },
    {
      label: "도구",
      submenu: [
        { label: "MCP 연결 설정…", click: () => void showMcpWizard() },
        { type: "separator" },
        { label: "업데이트 확인…", click: () => void updateManager?.check({ manual: true }) },
      ],
    },
    {
      label: "보기",
      submenu: [
        { role: "reload" },
        { role: "forceReload" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    { label: "창", submenu: [{ role: "minimize" }, { role: "close" }] },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function registerIpc() {
  ipcMain.handle("desktop:get-state", (event) => {
    requireWelcomeSender(event);
    return publicState();
  });
  ipcMain.handle("desktop:use-local", async (event) => {
    requireWelcomeSender(event);
    await connectLocal();
    return { ok: true };
  });
  ipcMain.handle("desktop:add-remote", async (event, candidate) => {
    requireWelcomeSender(event);
    const server = upsertRemoteServer(desktopConfig, candidate ?? {});
    saveConfig();
    rebuildMenu();
    await connectRemote(server.id);
    return { ok: true };
  });
  ipcMain.handle("desktop:use-remote", async (event, serverId) => {
    requireWelcomeSender(event);
    await connectRemote(String(serverId));
    return { ok: true };
  });
  ipcMain.handle("desktop:remove-remote", (event, serverId) => {
    requireWelcomeSender(event);
    desktopConfig.remoteServers = desktopConfig.remoteServers.filter(
      (server) => server.id !== String(serverId),
    );
    if (
      desktopConfig.lastConnection?.type === "remote" &&
      desktopConfig.lastConnection.serverId === String(serverId)
    ) {
      desktopConfig.lastConnection = null;
    }
    saveConfig();
    rebuildMenu();
    return publicState();
  });
  ipcMain.handle("desktop:get-mcp-state", async (event) => {
    requireMcpSender(event);
    const inventory = await runEngineBridge(engineExecutable(), "mcp-detect");
    return { remoteServers: desktopConfig.remoteServers, clients: inventory.clients };
  });
  ipcMain.handle("desktop:register-mcp", async (event, candidate) => {
    requireMcpSender(event);
    const serverIds = Array.isArray(candidate?.serverIds)
      ? [...new Set(candidate.serverIds.map(String))]
      : [];
    const servers = serverIds.map((serverId) => {
      const server = desktopConfig.remoteServers.find((item) => item.id === serverId);
      if (!server) throw new Error("저장된 원격 서버를 찾을 수 없습니다.");
      return { name: server.name, mcpName: server.mcpName, url: `${server.url}/mcp` };
    });
    const auth = candidate?.auth == null ? "" : String(candidate.auth);
    const token = candidate?.token == null ? null : String(candidate.token);
    if (token && token.length > 8192) throw new Error("Access token이 너무 깁니다.");
    return runEngineBridge(engineExecutable(), "mcp-register", {
      auth,
      clients: Array.isArray(candidate?.clients) ? candidate.clients.map(String) : [],
      servers,
      token,
    });
  });
  ipcMain.handle("desktop:close-mcp", (event) => {
    requireMcpSender(event);
    mcpWindow?.close();
  });
}

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) {
      void openLastConnection();
      return;
    }
    if (mainWindow.isMinimized()) {
      mainWindow.restore();
    }
    mainWindow.show();
    mainWindow.focus();
  });

  app.on("before-quit", (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    connectionGeneration += 1;
    void stopAllEngines().finally(() => app.quit());
  });

  app.on("window-all-closed", () => {
    app.quit();
  });

  app.whenReady().then(async () => {
    app.setAppUserModelId("io.dtxsuite.dtxwiki");
    const clipboardPermission = (webContents, permission, requestingUrl) =>
      canWriteClipboard({
        webContents,
        permission,
        requestingUrl,
        mainWindow,
        localOrigin: localEngine?.origin ?? null,
        remoteOrigins: new Set(desktopConfig.remoteServers.map((server) => server.url)),
      });
    session.defaultSession.setPermissionCheckHandler(
      (webContents, permission, requestingOrigin) =>
        clipboardPermission(webContents, permission, requestingOrigin),
    );
    session.defaultSession.setPermissionRequestHandler(
      (webContents, permission, callback, details) => {
        callback(clipboardPermission(webContents, permission, details?.requestingUrl));
      },
    );
    configPath = path.join(app.getPath("userData"), CONFIG_FILENAME);
    desktopConfig = readConfig(configPath);
    updateManager = createUpdateManager({
      app,
      autoUpdater: electronUpdater.autoUpdater,
      dialog,
      getWindow: () => mainWindow,
      beforeInstall: async () => {
        quitting = true;
        connectionGeneration += 1;
        await stopAllEngines();
      },
    });
    registerIpc();
    rebuildMenu();
    await openLastConnection();
    updateManager.schedule();
  });
}
