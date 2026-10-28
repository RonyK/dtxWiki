"use strict";

function createUpdateManager({ app, autoUpdater, dialog, getWindow, beforeInstall }) {
  let manualCheck = false;
  let downloading = false;

  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;
  if (process.platform === "darwin") {
    autoUpdater.channel = process.arch === "arm64" ? "latest-arm64" : "latest-x64";
  }

  function parentWindow() {
    const window = getWindow();
    return window && !window.isDestroyed() ? window : undefined;
  }

  async function message(options) {
    const parent = parentWindow();
    return parent ? dialog.showMessageBox(parent, options) : dialog.showMessageBox(options);
  }

  autoUpdater.on("update-available", async (info) => {
    manualCheck = false;
    const result = await message({
      type: "info",
      title: "dtxWiki 업데이트",
      message: `dtxWiki ${info.version} 버전을 사용할 수 있습니다.`,
      detail: "지금 다운로드할까요? 다운로드하는 동안 계속 사용할 수 있습니다.",
      buttons: ["다운로드", "나중에"],
      defaultId: 0,
      cancelId: 1,
    });
    if (result.response === 0) {
      downloading = true;
      await autoUpdater.downloadUpdate();
    }
  });

  autoUpdater.on("update-not-available", async () => {
    if (!manualCheck) return;
    manualCheck = false;
    await message({
      type: "info",
      title: "dtxWiki 업데이트",
      message: "현재 최신 버전을 사용하고 있습니다.",
      buttons: ["확인"],
    });
  });

  autoUpdater.on("download-progress", (progress) => {
    const window = parentWindow();
    if (window) window.setProgressBar(Math.max(0, Math.min(1, progress.percent / 100)));
  });

  autoUpdater.on("update-downloaded", async (info) => {
    downloading = false;
    const window = parentWindow();
    if (window) window.setProgressBar(-1);
    const result = await message({
      type: "info",
      title: "dtxWiki 업데이트 준비 완료",
      message: `dtxWiki ${info.version} 다운로드가 완료되었습니다.`,
      detail: "지금 다시 시작해 업데이트를 설치하거나, 앱을 종료할 때까지 미룰 수 있습니다.",
      buttons: ["지금 다시 시작", "나중에"],
      defaultId: 0,
      cancelId: 1,
    });
    if (result.response === 0) {
      try {
        await beforeInstall();
      } catch (error) {
        await message({
          type: "error",
          title: "dtxWiki 종료 실패",
          message: "로컬 엔진 종료를 확인하지 못해 업데이트 설치를 보류했습니다.",
          detail: error?.message ?? String(error),
          buttons: ["확인"],
        });
        return;
      }
      autoUpdater.quitAndInstall();
    }
  });

  autoUpdater.on("error", async (error) => {
    const shouldReport = manualCheck || downloading;
    manualCheck = false;
    downloading = false;
    const window = parentWindow();
    if (window) window.setProgressBar(-1);
    if (shouldReport) {
      await message({
        type: "error",
        title: "업데이트를 확인할 수 없습니다",
        message: "업데이트 서버에 연결하지 못했습니다.",
        detail: error?.message ?? String(error),
        buttons: ["확인"],
      });
    }
  });

  async function check({ manual = false } = {}) {
    if (!app.isPackaged) {
      if (manual) {
        await message({
          type: "info",
          title: "dtxWiki 업데이트",
          message: "개발 빌드에서는 자동 업데이트를 확인하지 않습니다.",
          buttons: ["확인"],
        });
      }
      return;
    }
    manualCheck = manual;
    try {
      await autoUpdater.checkForUpdates();
    } catch (error) {
      autoUpdater.emit("error", error);
    }
  }

  function schedule() {
    if (!app.isPackaged) return;
    const timer = setTimeout(() => void check(), 10_000);
    timer.unref?.();
  }

  return { check, schedule };
}

module.exports = { createUpdateManager };
