"use strict";

function isAllowedNavigation(targetUrl, allowedLocation) {
  if (allowedLocation.startsWith("file:")) return targetUrl === allowedLocation;
  try {
    return new URL(targetUrl).origin === allowedLocation;
  } catch {
    return false;
  }
}

function canWriteClipboard({
  webContents,
  permission,
  requestingUrl,
  mainWindow,
  localOrigin,
  remoteOrigins,
}) {
  if (permission !== "clipboard-sanitized-write" || !webContents) return false;
  if (!mainWindow || mainWindow.isDestroyed() || webContents !== mainWindow.webContents) {
    return false;
  }
  try {
    const requestedOrigin = new URL(requestingUrl || webContents.getURL()).origin;
    const visibleOrigin = new URL(webContents.getURL()).origin;
    return (
      requestedOrigin === visibleOrigin &&
      (requestedOrigin === localOrigin || remoteOrigins.has(requestedOrigin))
    );
  } catch {
    return false;
  }
}

module.exports = { canWriteClipboard, isAllowedNavigation };
