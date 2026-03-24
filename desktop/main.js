const { app, BrowserWindow } = require("electron");
const { spawn } = require("child_process");
const path = require("path");

let backend = null;
const PORT = process.env.DAVE_PORT || "8000";
const HOST = "127.0.0.1";

function startBackend() {
  const venvPython = path.join(process.cwd(), "venv", "bin", "python");
  const pythonCmd = process.env.DAVE_PYTHON || venvPython;
  const args = ["-m", "uvicorn", "app:app", "--host", HOST, "--port", PORT];

  backend = spawn(pythonCmd, args, {
    cwd: process.cwd(),
    stdio: "inherit",
    env: {
      ...process.env,
      DAVE_API_BASE: `http://${HOST}:${PORT}`
    }
  });

  backend.on("exit", (code, signal) => {
    console.log(`Backend exited with code ${code} signal ${signal}`);
  });
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 900,
    webPreferences: {
      preload: path.join(__dirname, "preload.js")
    }
  });
  win.loadURL(`http://${HOST}:${PORT}`);
}

app.whenReady().then(() => {
  startBackend();
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("quit", () => {
  if (backend) {
    backend.kill();
  }
});
