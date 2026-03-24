const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("__API_BASE__", process.env.DAVE_API_BASE || "http://127.0.0.1:8000");
