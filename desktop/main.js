const { app, BrowserWindow, dialog, session } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

let backend = null;
let backendReady = false;
let shuttingDown = false;
const PORT = process.env.DAVE_PORT || "8000";
const HOST = "127.0.0.1";
const API_BASE = `http://${HOST}:${PORT}`;
const STARTUP_TIMEOUT_MS = Number(process.env.DAVE_STARTUP_TIMEOUT_MS || 15000);
process.env.DAVE_API_BASE = API_BASE;

function startBackend() {
  const venvPython =
    process.platform === "win32"
      ? path.join(process.cwd(), "venv", "Scripts", "python.exe")
      : path.join(process.cwd(), "venv", "bin", "python");
  const pythonCmd = process.env.DAVE_PYTHON || venvPython;
  const args = ["-m", "uvicorn", "app:app", "--host", HOST, "--port", PORT];

  backend = spawn(pythonCmd, args, {
    cwd: process.cwd(),
    stdio: "inherit",
    env: {
      ...process.env,
      DAVE_API_BASE: API_BASE,
    },
  });
  return backend;
}

function probeHealth() {
  return new Promise((resolve) => {
    const request = http.get(`${API_BASE}/health`, { timeout: 1000 }, (response) => {
      response.resume();
      resolve(response.statusCode >= 200 && response.statusCode < 300);
    });
    request.on("timeout", () => {
      request.destroy();
      resolve(false);
    });
    request.on("error", () => resolve(false));
  });
}

function waitForBackend(child) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + STARTUP_TIMEOUT_MS;
    let settled = false;

    const finish = (error) => {
      if (settled) return;
      settled = true;
      child.removeListener("error", onSpawnError);
      child.removeListener("exit", onEarlyExit);
      if (error) reject(error);
      else resolve();
    };
    const onSpawnError = (error) => finish(new Error(`Backend spawn failed: ${error.message}`));
    const onEarlyExit = (code, signal) => {
      finish(new Error(`Backend exited before readiness (code=${code}, signal=${signal || "none"})`));
    };
    child.once("error", onSpawnError);
    child.once("exit", onEarlyExit);

    const poll = async () => {
      if (await probeHealth()) {
        backendReady = true;
        finish();
        return;
      }
      if (Date.now() >= deadline) {
        finish(new Error(`Backend readiness timed out after ${STARTUP_TIMEOUT_MS} ms; /health remained unavailable`));
        return;
      }
      setTimeout(poll, 250);
    };
    poll();
  });
}

function installApiKeyInjection() {
  const apiKey = process.env.DAVE_API_KEY;
  if (!apiKey) {
    throw new Error("DAVE_API_KEY is required for the Electron desktop app");
  }
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: [`${API_BASE}/*`] },
    (details, callback) => {
      details.requestHeaders["X-API-Key"] = apiKey;
      callback({ requestHeaders: details.requestHeaders });
    },
  );
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 900,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadURL(API_BASE);
}

async function startDesktop() {
  try {
    installApiKeyInjection();
    const child = startBackend();
    await waitForBackend(child);
    child.once("exit", (code, signal) => {
      backendReady = false;
      if (!shuttingDown) {
        dialog.showErrorBox(
          "DaveLLM backend stopped",
          `The backend exited (code=${code}, signal=${signal || "none"}).`,
        );
        app.quit();
      }
    });
    createWindow();
    const smokeExitMs = Number(process.env.DAVE_ELECTRON_SMOKE_EXIT_MS || 0);
    if (smokeExitMs > 0) setTimeout(() => app.quit(), smokeExitMs);
  } catch (error) {
    dialog.showErrorBox("DaveLLM startup failed", error.message);
    app.quit();
  }
}

app.whenReady().then(() => {
  startDesktop();

  app.on("activate", () => {
    if (backendReady && BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  shuttingDown = true;
  if (backend && !backend.killed) backend.kill();
});
