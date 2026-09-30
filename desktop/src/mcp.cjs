"use strict";

const { spawn } = require("node:child_process");

function runEngineBridge(executable, operation, request = null, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, [operation], {
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error("MCP registration helper timed out."));
    }, timeoutMs);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      if (stdout.length > 2_000_000) child.kill();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      let result;
      try {
        result = JSON.parse(stdout.trim());
      } catch {
        reject(new Error(stderr.trim() || "MCP registration helper returned invalid output."));
        return;
      }
      if (code !== 0 || !result.ok) {
        reject(new Error(result.error || stderr.trim() || "MCP registration failed."));
        return;
      }
      resolve(result);
    });
    child.stdin.end(request === null ? "" : JSON.stringify(request));
  });
}

module.exports = { runEngineBridge };
