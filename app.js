// ======================================================
// DaveLLM UI v4 — Backend-Synced Conversations + Streaming
// ======================================================

// ---------------------------------------------
// GLOBAL STATE
// ---------------------------------------------
const state = {
    nodes: [],
    selectedNode: null,
    sessionId: null,
    conversations: {},     // Synced from backend
    renaming: false,
    streaming: false,       // Track if currently streaming
    abortController: null,  // For stopping streams
    pendingImages: [],      // Images attached to next message
    modelMeta: {},          // Map of modelId -> { vision: bool }
    autoScroll: true,       // Control autoscroll behavior
    stats: {
        decisions: [],
        totalTokens: 0,
        totalMessages: 0
    }
};

// ---------------------------------------------
// CONSTANTS
// ---------------------------------------------
const ROUTER_BASE = (typeof window !== "undefined" && window.__API_BASE__) || "http://127.0.0.1:8000";
const LOCAL_STORAGE_KEY = "dave_convos";
const LAST_SESSION_KEY = "dave_last_session";
const API_KEY_STORAGE_KEY = "dave_api_key";
const THEME_STORAGE_KEY = "dave_theme";
const THEMES = ["dark", "light", "forest"];

// Optional API key support; set via localStorage to avoid hardcoding
let apiKey = localStorage.getItem(API_KEY_STORAGE_KEY) || "";
let routingPrefs = {
    max_cost: parseFloat(localStorage.getItem("dave_route_max_cost") || "0.01"),
    min_quality: parseFloat(localStorage.getItem("dave_route_min_quality") || "0.85")
};
let speechRecognition = null;
let isDictating = false;
let mediaRecorder = null;
let mediaChunks = [];
let mediaStream = null;
let isRecordingFallback = false;
const TEMPLATES = {
    general: { title: "New Conversation", system_prompt: "" },
    code_review: { title: "Code Review Session", system_prompt: "" },
    brainstorm: { title: "Brainstorm", system_prompt: "" }
};

function authHeaders(extra = {}) {
    const headers = { ...extra };
    if (apiKey) headers["X-API-Key"] = apiKey;
    return headers;
}

// Theme handling
function applyTheme(theme) {
    const safeTheme = THEMES.includes(theme) ? theme : "dark";
    document.documentElement.setAttribute("data-theme", safeTheme);
    localStorage.setItem(THEME_STORAGE_KEY, safeTheme);
    if (themeToggle) {
        const iconMap = { dark: "🌞", light: "🌙", forest: "🌲" };
        const titleMap = {
            dark: "Switch to light mode",
            light: "Switch to forest mode",
            forest: "Switch to dark mode"
        };
        themeToggle.textContent = iconMap[safeTheme] || "🌓";
        themeToggle.title = titleMap[safeTheme] || "Toggle theme";
    }
}

function initTheme() {
    const saved = localStorage.getItem(THEME_STORAGE_KEY) || "dark";
    applyTheme(saved);
}

function initRoutingControls() {
    if (routeCostInput) {
        routeCostInput.value = routingPrefs.max_cost;
        routeCostInput.addEventListener("input", (e) => {
            routingPrefs.max_cost = parseFloat(e.target.value) || 0.01;
            localStorage.setItem("dave_route_max_cost", routingPrefs.max_cost);
        });
    }
    if (routeQualityInput) {
        routeQualityInput.value = routingPrefs.min_quality;
        routeQualityInput.addEventListener("input", (e) => {
            routingPrefs.min_quality = parseFloat(e.target.value) || 0.85;
            localStorage.setItem("dave_route_min_quality", routingPrefs.min_quality);
        });
    }
}

function loadStats() {
    const savedStats = localStorage.getItem("dave_routing_stats");
    if (savedStats) {
        try {
            state.stats = JSON.parse(savedStats);
        } catch (e) {
            console.warn("Failed to parse saved stats", e);
        }
    }
}

function persistStats() {
    try {
        localStorage.setItem("dave_routing_stats", JSON.stringify(state.stats));
    } catch (e) {
        console.warn("Failed to persist stats", e);
    }
}

function updateStatsDisplay() {
    if (!statDecisions) return;
    const dCount = state.stats.decisions.length;
    const avgTokens = state.stats.totalMessages ? (state.stats.totalTokens / state.stats.totalMessages) : 0;
    const avgConf = dCount
        ? (state.stats.decisions.reduce((sum, d) => sum + (d.confidence || 0), 0) / dCount)
        : null;

    statDecisions.textContent = dCount;
    statTokens.textContent = Math.round(state.stats.totalTokens);
    statTokensPerMsg.textContent = avgTokens.toFixed(1);
    statConfidence.textContent = avgConf !== null ? `${Math.round(avgConf * 100)}%` : "–";
    const last = state.stats.decisions[dCount - 1];
    statLastRoute.textContent = last ? `${last.model_id} (${Math.round((last.confidence || 0) * 100)}%)` : "–";
    persistStats();
}

function updateScrollBottomButton() {
    if (!scrollBottomBtn || !responseBox) return;
    const atBottom = (responseBox.scrollHeight - responseBox.scrollTop - responseBox.clientHeight) < 40;
    state.autoScroll = atBottom;
    if (atBottom) {
        scrollBottomBtn.classList.remove("visible");
    } else {
        scrollBottomBtn.classList.add("visible");
    }
}

function sendFeedback(score, content, modelId) {
    const fbModel = modelId || modelSelect.value || state.selectedNode || "unknown";
    try {
        fetch(routerEndpoint("/feedback"), {
            method: "POST",
            headers: authHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({
                model_id: fbModel,
                score,
                response: content.slice(0, 500),
                complexity: complexityScoreClient(content || "")
            })
        }).catch(() => {});
        const fb = JSON.parse(localStorage.getItem("dave_feedback") || "[]");
        fb.push({ score, modelId: fbModel, content: content.slice(0, 200), ts: Date.now() });
        localStorage.setItem("dave_feedback", JSON.stringify(fb));
    } catch (e) {}
}

// ---------------------------------------------
// DOM REFERENCES
// ---------------------------------------------
const convoList = document.getElementById("convoList");
const newConvoBtn = document.getElementById("newConvoBtn");
const nodesContainer = document.getElementById("nodesContainer");
const refreshNodesBtn = document.getElementById("refreshNodes");
const responseBox = document.getElementById("responseBox");
const memoryBox = document.getElementById("memoryBox");
const promptInput = document.getElementById("promptInput");
const sendBtn = document.getElementById("sendBtn");
const nodeSelect = document.getElementById("targetNodeSelect");
const modelSelect = document.getElementById("modelSelect");
const loadModelsBtn = document.getElementById("loadModelsBtn");
const imageInput = document.getElementById("imageInput");
const imageStatus = document.getElementById("imageStatus");
const themeToggle = document.getElementById("themeToggle");
const routeCostInput = document.getElementById("routeCost");
const routeQualityInput = document.getElementById("routeQuality");
const routeStatus = document.getElementById("routeStatus");
const statDecisions = document.getElementById("statDecisions");
const statTokens = document.getElementById("statTokens");
const statTokensPerMsg = document.getElementById("statTokensPerMsg");
const statConfidence = document.getElementById("statConfidence");
const statLastRoute = document.getElementById("statLastRoute");
const scrollBottomBtn = document.getElementById("scrollBottomBtn");
const monitorBadge = document.getElementById("monitorBadge");
const templateSelect = document.getElementById("templateSelect");
const createFromTemplateBtn = document.getElementById("createFromTemplate");
const projectSelect = document.getElementById("projectSelect");
const resyncBtn = document.getElementById("resyncProject");
const editProjectBtn = document.getElementById("editProject");
const audioInput = document.getElementById("audioInput");
const transcribeBtn = document.getElementById("transcribeBtn");
const audioStatus = document.getElementById("audioStatus");
const fileInput = document.getElementById("fileInput");
const fileStatus = document.getElementById("fileStatus");
const supportFlag = document.getElementById("supportFlag");
const dictateBtn = document.getElementById("dictateBtn");
const dictateStatus = document.getElementById("dictateStatus");
const hfUrlInput = document.getElementById("hfUrlInput");
const hfDestInput = document.getElementById("hfDestInput");
const hfDownloadBtn = document.getElementById("hfDownloadBtn");
const hfStatus = document.getElementById("hfStatus");

// ---------------------------------------------
// UTIL
// ---------------------------------------------
function routerEndpoint(path) {
    return `${ROUTER_BASE}${path}`;
}

function normalizeModelMeta(model) {
    if (!model) return null;

    const id = typeof model === "string" ? model : (model.id || model.model || model.name);
    if (!id) return null;

    const lowerId = id.toLowerCase();
    const visionFromId = lowerId.includes("vision") || lowerId.includes("multimodal") || lowerId.includes("mm");

    let vision = visionFromId;

    if (typeof model === "object") {
        const caps = model.capabilities || {};
        if (caps.vision || caps.multimodal) vision = true;

        const modalities = model.modalities || model.modality;
        if (typeof modalities === "string") {
            if (["vision", "image", "multimodal"].some((m) => modalities.toLowerCase().includes(m))) {
                vision = true;
            }
        } else if (Array.isArray(modalities)) {
            if (modalities.map((m) => String(m).toLowerCase()).some((m) => ["vision", "image", "multimodal"].includes(m))) {
                vision = true;
            }
        }

        if (model.vision || model.supports_vision) vision = true;
    }

    return { id, vision };
}

function getSelectedModelMeta() {
    const id = modelSelect.value;
    return state.modelMeta[id] || null;
}

function modelSupportsVision(modelId) {
    if (!modelId) return false;
    const meta = state.modelMeta[modelId];
    if (meta) return !!meta.vision;
    return ["vision", "multimodal", "mm"].some((flag) => modelId.toLowerCase().includes(flag));
}

function updateImageSupportNotice() {
    const supportsVision = modelSupportsVision(modelSelect.value);
    if (supportsVision && state.pendingImages.length === 0) {
        imageStatus.textContent = "Model supports images";
    } else if (!supportsVision && state.pendingImages.length === 0) {
        imageStatus.textContent = "Model is text-only";
    }
}

function setDictationStatus(msg) {
    if (dictateStatus) {
        dictateStatus.textContent = msg || "";
    }
}

function initDictation() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
        setDictationStatus("Using mic record + server transcription (browser speech not available).");
        return;
    }

    speechRecognition = new SR();
    speechRecognition.continuous = true;
    speechRecognition.interimResults = true;
    speechRecognition.lang = "en-US";

    speechRecognition.onstart = () => {
        isDictating = true;
        setDictationStatus("Listening…");
        if (dictateBtn) dictateBtn.textContent = "⏹️";
    };

    speechRecognition.onerror = (e) => {
        setDictationStatus(`Dictation error: ${e.error || "unknown"}`);
    };

    speechRecognition.onresult = (event) => {
        let finalText = "";
        let interimText = "";
        for (let i = 0; i < event.results.length; i++) {
            const res = event.results[i];
            if (res.isFinal) {
                finalText += res[0].transcript + " ";
            } else {
                interimText += res[0].transcript + " ";
            }
        }
        if (promptInput) {
            const base = promptInput.value || "";
            const combined = (base + " " + finalText).trim();
            promptInput.value = combined || base;
            if (interimText) {
                setDictationStatus(`Listening… ${interimText.trim()}`);
            } else {
                setDictationStatus("Listening…");
            }
            promptInput.focus();
        }
    };

    speechRecognition.onend = () => {
        isDictating = false;
        if (dictateBtn) dictateBtn.textContent = "🎙️";
        if (!dictateStatus || !dictateStatus.textContent.includes("error")) {
            setDictationStatus("");
        }
    };
}

async function loadProjects() {
    try {
        const res = await fetch(routerEndpoint("/projects"), { headers: authHeaders() });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        projects = data.projects || [];
        if (projectSelect) {
            projectSelect.innerHTML = '<option value=\"\">All Projects</option>';
            projects.forEach(p => {
                const opt = document.createElement("option");
                opt.value = p.project_id;
                opt.textContent = p.name;
                if (selectedProjectId && selectedProjectId === p.project_id) {
                    opt.selected = true;
                }
                projectSelect.appendChild(opt);
            });
            const newOpt = document.createElement("option");
            newOpt.value = "__create__";
            newOpt.textContent = "➕ New project…";
            projectSelect.appendChild(newOpt);
        }
    } catch (e) {
        console.error("Failed to load projects:", e);
    }
}

async function createProjectFlow() {
    const name = prompt("Project name?");
    if (!name) return;
    const systemPrompt = prompt("Optional system instructions for this project?", "") || "";
    const preferredModel = modelSelect?.value || "";
    const description = prompt("Optional description?", "") || "";
    try {
        const res = await fetch(routerEndpoint("/projects"), {
            method: "POST",
            headers: authHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({
                name,
                system_prompt: systemPrompt,
                preferred_model: preferredModel || undefined,
                description
            })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        selectedProjectId = data.project_id;
        localStorage.setItem("dave_project_id", selectedProjectId);
        await loadProjects();
        await loadConversationsFromBackend();
        renderConversationList();
    } catch (e) {
        console.error("Failed to create project:", e);
        alert("Could not create project");
    }
}

function saveAllConversations() {
    localStorage.setItem(LOCAL_STORAGE_KEY, JSON.stringify(state.conversations));
}

function loadAllConversations() {
    const saved = localStorage.getItem(LOCAL_STORAGE_KEY);
    if (saved) {
        state.conversations = JSON.parse(saved);
    }
}

function toggleDictation() {
    if (!speechRecognition) {
        initDictation();
    }

    // Fallback: record audio and send to /audio/transcribe
    if (!speechRecognition) {
        if (isRecordingFallback) {
            setDictationStatus("Stopping recording…");
            if (mediaRecorder && mediaRecorder.state !== "inactive") {
                mediaRecorder.stop();
            }
            return;
        }
        startFallbackRecording();
        return;
    }

    if (isDictating) {
        setDictationStatus("Stopping…");
        try {
            speechRecognition.stop();
        } catch (e) {
            setDictationStatus(`Dictation stop error: ${e.message}`);
        }
        return;
    }

    setDictationStatus("Listening…");
    try {
        speechRecognition.start();
    } catch (e) {
        setDictationStatus(`Dictation error: ${e.message}`);
    }
}

function startFallbackRecording() {
    if (typeof MediaRecorder === "undefined") {
        setDictationStatus("MediaRecorder not supported in this browser");
        return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setDictationStatus("Mic capture not supported in this browser");
        return;
    }

    navigator.mediaDevices.getUserMedia({ audio: true })
        .then((stream) => {
            mediaStream = stream;
            mediaChunks = [];
            try {
                mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus" });
            } catch (e) {
                // Fallback to default if codec hint fails
                mediaRecorder = new MediaRecorder(stream);
            }

            mediaRecorder.onstart = () => {
                isRecordingFallback = true;
                if (dictateBtn) dictateBtn.textContent = "⏹️";
                setDictationStatus("Recording… tap again to stop");
            };

            mediaRecorder.ondataavailable = (e) => {
                if (e.data && e.data.size > 0) {
                    mediaChunks.push(e.data);
                }
            };

            mediaRecorder.onerror = (e) => {
                setDictationStatus(`Record error: ${e.error?.message || e.message || "unknown"}`);
                stopFallbackStream();
            };

            mediaRecorder.onstop = () => {
                const blob = new Blob(mediaChunks, { type: "audio/webm" });
                stopFallbackStream();
                if (blob.size === 0) {
                    setDictationStatus("No audio captured");
                    return;
                }
                setDictationStatus("Transcribing (uploading)…");
                uploadDictationBlob(blob);
            };

            mediaRecorder.start();
        })
        .catch((err) => {
            setDictationStatus(`Mic permission denied: ${err.message}`);
        });
}

function stopFallbackStream() {
    isRecordingFallback = false;
    if (dictateBtn) dictateBtn.textContent = "🎙️";
    if (mediaStream) {
        mediaStream.getTracks().forEach((t) => t.stop());
        mediaStream = null;
    }
}

async function uploadDictationBlob(blob) {
    try {
        const form = new FormData();
        form.append("file", blob, "dictation.webm");
        const res = await fetch(routerEndpoint("/audio/transcribe"), {
            method: "POST",
            headers: authHeaders(),
            body: form
        });
        if (!res.ok) {
            const msg = await res.text();
            throw new Error(msg || `HTTP ${res.status}`);
        }
        const data = await res.json();
        const transcript = data.text || "";
        if (transcript && promptInput) {
            promptInput.value = promptInput.value
                ? `${promptInput.value}\n${transcript}`
                : transcript;
            promptInput.focus();
            setDictationStatus("Transcript added.");
        } else {
            setDictationStatus("No text returned from transcription.");
        }
    } catch (e) {
        setDictationStatus(`Transcription error: ${e.message}`);
    } finally {
        mediaChunks = [];
    }
}

async function downloadModelFromHF() {
    if (!hfUrlInput || !hfStatus) return;
    const url = (hfUrlInput.value || "").trim();
    if (!url) {
        hfStatus.textContent = "Enter a Hugging Face URL.";
        return;
    }
    let dest = hfDestInput ? (hfDestInput.value || "").trim() : "";
    if (!dest) {
        try {
            const file = url.split("/").pop();
            if (file) {
                dest = `models/${file}`;
                if (hfDestInput) hfDestInput.value = dest;
            }
        } catch (e) {
            dest = "";
        }
    }
    hfStatus.textContent = "Starting download…";
    if (hfDownloadBtn) hfDownloadBtn.disabled = true;
    try {
        const res = await fetch(routerEndpoint("/models/download"), {
            method: "POST",
            headers: authHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({ url, dest_path: dest || undefined })
        });
        if (!res.ok) {
            const msg = await res.text();
            throw new Error(msg || `HTTP ${res.status}`);
        }
        const data = await res.json();
        hfStatus.textContent = `Saved to ${data.saved_to} (${Math.round(data.size_bytes / 1024 / 1024)} MB)`;
    } catch (e) {
        hfStatus.textContent = `Download failed: ${e.message}`;
    } finally {
        if (hfDownloadBtn) hfDownloadBtn.disabled = false;
    }
}

// ---------------------------------------------
// BACKEND SYNC FUNCTIONS
// ---------------------------------------------

/**
 * Load all conversations from backend server.
 * Replaces localStorage as source of truth when available.
 */
async function loadConversationsFromBackend() {
    try {
        const res = await fetch(routerEndpoint("/conversations"), {
            headers: authHeaders()
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        
        const convos = await res.json();
        
        state.conversations = {};
        convos.forEach(c => {
            if (selectedProjectId && c.project_id !== selectedProjectId) return;
            state.conversations[c.conversation_id] = {
                title: c.title,
                messages: [],  // messages loaded on-demand
                created_at: c.created_at,
                updated_at: c.updated_at,
                project_id: c.project_id || null
            };
        });
        
        console.log(`✅ Loaded ${convos.length} conversations from backend`);
        return true;
        
    } catch (err) {
        console.error("Failed to load conversations from backend:", err);
        console.warn("⚠️ Falling back to localStorage");
        loadAllConversations();
        return false;
    }
}

/**
 * Load full conversation history from backend by ID.
 */
async function loadConversationHistory(cid) {
    try {
        const res = await fetch(routerEndpoint(`/conversations/${cid}`), {
            headers: authHeaders()
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        
        const data = await res.json();
        
        state.conversations[cid] = {
            title: data.title,
            messages: data.messages || [],
            created_at: data.created_at,
            updated_at: data.updated_at,
            project_id: data.project_id || null
        };
        
        return true;
        
    } catch (err) {
        console.error(`Failed to load conversation ${cid}:`, err);
        return false;
    }
}

/**
 * Sync renamed conversation title to backend.
 */
async function syncRenameToBackend(cid, newTitle) {
    try {
        const res = await fetch(routerEndpoint(`/conversations/${cid}/rename`), {
            method: "POST",
            headers: authHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({ title: newTitle })
        });
        
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        
        console.log(`✅ Renamed conversation ${cid} on backend`);
        return true;
        
    } catch (err) {
        console.error("Failed to sync rename to backend:", err);
        return false;
    }
}

/**
 * Delete conversation from backend.
 */
async function deleteConversationFromBackend(cid) {
    try {
        const res = await fetch(routerEndpoint(`/conversations/${cid}`), {
            method: "DELETE",
            headers: authHeaders()
        });
        
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        
        console.log(`✅ Deleted conversation ${cid} from backend`);
        return true;
        
    } catch (err) {
        console.error("Failed to delete conversation from backend:", err);
        return false;
    }
}

/**
 * Clear conversation on backend.
 */
async function clearConversationOnBackend(cid) {
    try {
        const res = await fetch(routerEndpoint(`/conversations/${cid}/clear`), {
            method: "POST",
            headers: authHeaders()
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        console.log(`✅ Cleared on backend: ${cid}`);
        return true;
    } catch (err) {
        console.error("Failed to clear conversation on backend:", err);
        return false;
    }
}

// ---------------------------------------------
// CONVERSATION LIST UI
// ---------------------------------------------
function getSortedConversations() {
    const items = Object.entries(state.conversations);
    return items.sort((a, b) => {
        const timeA = a[1].updated_at || a[1].created_at || 0;
        const timeB = b[1].updated_at || b[1].created_at || 0;
        return new Date(timeB) - new Date(timeA);
    });
}

function renderConversationList() {
    if (state.renaming) {
        console.log("⏸️ Skipping render during rename");
        return;
    }

    convoList.innerHTML = "";

    const sorted = getSortedConversations();

    sorted.forEach(([cid, convo]) => {
        const div = document.createElement("div");
        div.className = "convo-item";
        if (cid === state.sessionId) div.classList.add("active-convo");

        const titleContainer = document.createElement("div");
        titleContainer.className = "convo-title-container";
        titleContainer.style.display = "flex";
        titleContainer.style.alignItems = "center";
        titleContainer.style.flex = "1";
        titleContainer.style.minWidth = "0";

        const titleSpan = document.createElement("span");
        titleSpan.className = "convo-title";
        titleSpan.textContent = convo.title;
        titleSpan.style.flex = "1";
        titleSpan.style.overflow = "hidden";
        titleSpan.style.textOverflow = "ellipsis";
        titleSpan.style.whiteSpace = "nowrap";

        titleContainer.appendChild(titleSpan);

        const actionsDiv = document.createElement("div");
        actionsDiv.className = "convo-actions";
        actionsDiv.style.display = "none";
        actionsDiv.style.gap = "4px";
        actionsDiv.style.alignItems = "center";
        actionsDiv.style.flexShrink = "0";
        actionsDiv.style.marginLeft = "8px";

        const renameBtn = document.createElement("button");
        renameBtn.className = "action-icon";
        renameBtn.innerHTML = "✏️";
        renameBtn.title = "Rename conversation";
        renameBtn.onclick = (e) => {
            e.stopPropagation();
            if (state.renaming) return;
            renameConversation(cid, div);
        };
        actionsDiv.appendChild(renameBtn);

        const clearBtn = document.createElement("button");
        clearBtn.className = "action-icon";
        clearBtn.innerHTML = "🗑️";
        clearBtn.title = "Clear messages";
        clearBtn.onclick = (e) => {
            e.stopPropagation();
            clearConversation(cid);
        };
        actionsDiv.appendChild(clearBtn);

        const deleteBtn = document.createElement("button");
        deleteBtn.className = "action-icon";
        deleteBtn.innerHTML = "❌";
        deleteBtn.title = "Delete conversation";
        deleteBtn.onclick = (e) => {
            e.stopPropagation();
            deleteConversation(cid, div);
        };
        actionsDiv.appendChild(deleteBtn);

        const exportBtn = document.createElement("button");
        exportBtn.className = "action-icon";
        exportBtn.innerHTML = "📥";
        exportBtn.title = "Export conversation (markdown)";
        exportBtn.onclick = (e) => {
            e.stopPropagation();
            window.open(routerEndpoint(`/conversations/${cid}/export?format=markdown`), "_blank");
        };
        actionsDiv.appendChild(exportBtn);

        div.appendChild(titleContainer);
        div.appendChild(actionsDiv);

        div.addEventListener("mouseenter", () => {
            actionsDiv.style.display = "flex";
        });
        div.addEventListener("mouseleave", () => {
            actionsDiv.style.display = "none";
        });

        div.addEventListener("click", async (e) => {
            if (state.renaming || e.target.classList.contains("action-icon")) {
                e.stopPropagation();
                return;
            }
            await switchConversation(cid);
        });

        div.addEventListener("dblclick", (e) => {
            e.stopPropagation();
            e.preventDefault();
            if (state.renaming) return;
            renameConversation(cid, div);
        });

        convoList.appendChild(div);
    });
}

async function switchConversation(cid) {
    if (state.renaming) return;

    const convo = state.conversations[cid];
    if (!convo) {
        console.error(`Conversation ${cid} not found in state`);
        return;
    }

    state.sessionId = cid;
    localStorage.setItem(LAST_SESSION_KEY, cid);

    // If we don't have messages yet, try loading from backend
    if (!convo.messages || convo.messages.length === 0) {
        console.log(`📥 Loading full history from backend for ${cid}`);
        await loadConversationHistory(cid);
    } else {
        console.log(`✨ Using cached history for ${cid}`);
    }
    
    renderConversationList();
    renderMessages();
}

async function deleteConversation(cid, element) {
    if (!confirm(`Delete conversation "${state.conversations[cid].title}"? This cannot be undone.`)) {
        return;
    }

    console.log(`🗑️ Deleting conversation ${cid}`);

    element.style.opacity = "0";
    element.style.transition = "opacity 0.3s ease";
    element.style.transform = "translateX(-10px)";

    setTimeout(async () => {
        try {
            await deleteConversationFromBackend(cid);
            delete state.conversations[cid];
            saveAllConversations();

            if (state.sessionId === cid) {
                const remaining = Object.keys(state.conversations);
                if (remaining.length > 0) {
                    await switchConversation(remaining[0]);
                } else {
                    await createNewConversation();
                }
            }

            renderConversationList();
        } catch (err) {
            console.error("Failed to delete conversation:", err);
            alert("Failed to delete conversation");
            element.style.opacity = "1";
        }
    }, 300);
}

async function clearConversation(cid) {
    if (!confirm(`Clear all messages from "${state.conversations[cid].title}"? This cannot be undone.`)) {
        return;
    }

    console.log(`🧹 Clearing conversation ${cid}`);

    try {
        await clearConversationOnBackend(cid);
        const convo = state.conversations[cid];
        convo.messages = [];
        saveAllConversations();
        renderMessages();
    } catch (err) {
        console.error("Failed to clear conversation:", err);
        alert("Failed to clear conversation");
    }
}

async function createNewConversation() {
    const id = "convo_" + Date.now();

    state.conversations[id] = {
        title: "New Conversation",
        messages: [],
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        project_id: selectedProjectId || null
    };

    state.sessionId = id;
    localStorage.setItem(LAST_SESSION_KEY, id);
    
    renderConversationList();
    renderMessages();
    
    console.log(`✅ Created new conversation ${id} (backend will create on first message)`);
}

async function createConversationFromTemplate(name) {
    try {
        const res = await fetch(routerEndpoint("/conversations/from_template"), {
            method: "POST",
            headers: authHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({ template_name: name, project_id: selectedProjectId || undefined })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const id = data.conversation_id;
        state.conversations[id] = {
            title: TEMPLATES[name]?.title || data.template || "New Conversation",
            messages: [{ role: "system", content: TEMPLATES[name]?.system_prompt || "" }],
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            project_id: data.project_id || selectedProjectId || null
        };
        state.sessionId = id;
        renderConversationList();
        renderMessages();
    } catch (e) {
        console.error("Failed to create from template:", e);
    }
}

async function resyncConversationInstructions() {
    if (!state.sessionId) return;
    const cid = state.sessionId;
    const targetProject = selectedProjectId || (state.conversations[cid]?.project_id);
    if (!targetProject) {
        alert("Select a project before resyncing instructions.");
        return;
    }
    try {
        const res = await fetch(routerEndpoint(`/conversations/${cid}/resync_project`), {
            method: "POST",
            headers: authHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({ project_id: targetProject })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        // Store system prompt on the conversation metadata for visibility
        if (state.conversations[cid]) {
            state.conversations[cid].system_prompt = data.system_prompt;
            state.conversations[cid].project_id = data.project_id;
        }
        renderConversationList();
        alert("Project instructions resynced for this conversation.");
    } catch (e) {
        console.error("Failed to resync project instructions:", e);
        alert("Failed to resync project instructions");
    }
}

async function editProjectInstructions() {
    const targetProject = selectedProjectId;
    if (!targetProject) {
        alert("Select a project to edit its instructions.");
        return;
    }
    const proj = projects.find(p => p.project_id === targetProject);
    const newPrompt = prompt("Project system instructions:", proj?.system_prompt || "");
    if (newPrompt === null) return;
    const newModel = prompt("Preferred model (optional):", proj?.preferred_model || "") || undefined;
    try {
        const res = await fetch(routerEndpoint(`/projects/${targetProject}`), {
            method: "PUT",
            headers: authHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({
                system_prompt: newPrompt,
                preferred_model: newModel && newModel.trim() ? newModel.trim() : undefined
            })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        await loadProjects();
        alert("Project instructions updated. Use 🔄 to resync the active conversation.");
    } catch (e) {
        console.error("Failed to update project:", e);
        alert("Failed to update project");
    }
}

async function transcribeAudio() {
    if (!audioInput || !audioInput.files || !audioInput.files.length) {
        alert("Select an audio file to transcribe.");
        return;
    }
    const file = audioInput.files[0];
    const form = new FormData();
    form.append("file", file);
    if (transcribeBtn) transcribeBtn.disabled = true;
    if (audioStatus) audioStatus.textContent = "Transcribing...";
    try {
        const res = await fetch(routerEndpoint("/audio/transcribe"), {
            method: "POST",
            headers: authHeaders(),
            body: form
        });
        if (!res.ok) {
            const msg = await res.text();
            throw new Error(`HTTP ${res.status}: ${msg || "Transcription failed"}`);
        }
        const data = await res.json();
        const transcript = data.text || "";
        if (transcript) {
            promptInput.value = promptInput.value ? `${promptInput.value}\n${transcript}` : transcript;
            promptInput.focus();
            if (audioStatus) audioStatus.textContent = "Transcript added to input.";
        } else {
            if (audioStatus) audioStatus.textContent = "No text returned from transcription.";
        }
    } catch (e) {
        console.error("Transcription failed:", e);
        if (audioStatus) audioStatus.textContent = `Transcription error: ${e.message}`;
    } finally {
        if (transcribeBtn) transcribeBtn.disabled = false;
        if (audioInput) audioInput.value = "";
    }
}

// ---------------------------------------------
// RENAME LOGIC (with backend sync)
// ---------------------------------------------
async function renameConversation(cid, element) {
    console.log("🔄 Starting rename for conversation:", cid);
    state.renaming = true;

    const convo = state.conversations[cid];
    if (!convo) {
        console.error("❌ Conversation not found:", cid);
        state.renaming = false;
        return;
    }

    const oldTitle = convo.title;

    const input = document.createElement("input");
    input.type = "text";
    input.value = oldTitle;
    input.className = "rename-input";
    input.setAttribute("aria-label", "Rename conversation");

    element.innerHTML = "";
    element.appendChild(input);

    requestAnimationFrame(() => {
        input.focus();
        try { input.select(); } catch (e) {}
    });

    const finalize = async () => {
        try {
            const newTitle = (input.value || "").trim() || oldTitle;
            
            if (newTitle === oldTitle) {
                console.log("📝 No change in title, skipping sync");
                return;
            }
            
            console.log(`💾 Saving title: "${newTitle}"`);
            convo.title = newTitle;
            
            if (convo.messages && convo.messages.length > 0) {
                const syncSuccess = await syncRenameToBackend(cid, newTitle);
                if (!syncSuccess) {
                    console.warn("⚠️ Backend sync failed, keeping local change");
                }
            } else {
                console.log("ℹ️ New conversation, skipping backend sync (will sync on first message)");
            }
            
            saveAllConversations();
            
        } catch (err) {
            console.error("❌ Error during finalize:", err);
        } finally {
            state.renaming = false;
            renderConversationList();
            renderMessages();
        }
    };

    const cancel = () => {
        console.log("❌ Rename cancelled");
        state.renaming = false;
        renderConversationList();
    };

    const handleKeydown = async (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            console.log("✅ Enter pressed, finalizing rename");
            await finalize();
        } else if (e.key === "Escape") {
            e.preventDefault();
            console.log("🚫 Escape pressed, cancelling rename");
            cancel();
        }
    };

    const handleBlur = async () => {
        console.log("👁️ Blur detected, finalizing rename");
        await new Promise(resolve => setTimeout(resolve, 50));
        await finalize();
    };

    input.addEventListener("keydown", handleKeydown);
    input.addEventListener("blur", handleBlur);
}

// ---------------------------------------------
// NODE HANDLING
// ---------------------------------------------
async function fetchNodes() {
    try {
        const res = await fetch(routerEndpoint("/nodes"), {
            headers: authHeaders()
        });
        
        if (!res.ok) {
            throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        }
        
        const nodes = await res.json();

        state.nodes = nodes;
        renderNodes(nodes);
        renderNodeSelect(nodes);

    } catch (err) {
        console.error("Failed to fetch nodes:", err);
        
        nodesContainer.innerHTML = `
            <div class="error-message" style="color: #ff6b6b; padding: 10px; border: 1px solid #ff6b6b; border-radius: 4px;">
                ⚠️ Connection failed: Is the backend running at ${ROUTER_BASE}?
            </div>
        `;
        
        nodeSelect.innerHTML = '<option value="">No nodes available</option>';
    }
}

function renderNodes(nodes) {
    nodesContainer.innerHTML = "";
    
    if (nodes.length === 0) {
        nodesContainer.innerHTML = '<div class="info-message">No nodes registered yet.</div>';
        return;
    }
    
    fetchNodeStatus(nodes);
}

async function fetchNodeStatus(nodes) {
    try {
        const res = await fetch(routerEndpoint("/nodes/status"), {
            headers: authHeaders()
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        
        const statuses = await res.json();
        
        const statusMap = {};
        statuses.forEach(s => {
            statusMap[s.node_id] = s;
        });
        
        nodesContainer.innerHTML = "";
        
        nodes.forEach((n) => {
            const status = statusMap[n.id] || { status: "offline", latency: null };
            
            const div = document.createElement("div");
            div.className = "node-card";
            
            const statusDot = document.createElement("span");
            statusDot.className = "status-dot " + (status.status === "online" ? "status-online" : "status-offline");
            
            const header = document.createElement("div");
            header.className = "node-header";
            header.style.display = "flex";
            header.style.alignItems = "center";
            header.style.gap = "8px";
            header.style.marginBottom = "8px";
            
            const nameSpan = document.createElement("span");
            nameSpan.className = "node-name";
            nameSpan.textContent = n.name;
            nameSpan.style.flex = "1";
            
            const statusLabel = document.createElement("span");
            statusLabel.style.fontSize = "12px";
            statusLabel.style.color = status.status === "online" ? "#35c759" : "#ff6b6b";
            statusLabel.textContent = status.status === "online" ? "Online" : "Offline";
            
            header.appendChild(statusDot);
            header.appendChild(nameSpan);
            header.appendChild(statusLabel);
            
            const urlSpan = document.createElement("div");
            urlSpan.style.fontSize = "12px";
            urlSpan.style.color = "#9ba4b5";
            urlSpan.style.wordBreak = "break-all";
            urlSpan.textContent = n.url;
            
            div.appendChild(header);
            div.appendChild(urlSpan);
            
            if (status.latency !== null) {
                const latencySpan = document.createElement("div");
                latencySpan.style.fontSize = "11px";
                latencySpan.style.color = "#58a6ff";
                latencySpan.style.marginTop = "4px";
                latencySpan.textContent = `⚡ ${status.latency}ms latency`;
                div.appendChild(latencySpan);
            }
            
            nodesContainer.appendChild(div);
        });
        
    } catch (err) {
        console.error("Failed to fetch node status:", err);
        nodesContainer.innerHTML = "";
        nodes.forEach((n) => {
            const div = document.createElement("div");
            div.className = "node-card";
            div.innerHTML = `<strong>${n.name}</strong><div style="font-size: 12px; color: #9ba4b5;">${n.url}</div>`;
            nodesContainer.appendChild(div);
        });
    }
}

function renderNodeSelect(nodes) {
    nodeSelect.innerHTML = "";

    if (nodes.length === 0) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "No nodes available";
        nodeSelect.appendChild(opt);
        return;
    }

    nodes.forEach((n) => {
        const opt = document.createElement("option");
        opt.value = n.id;
        opt.textContent = n.name;
        nodeSelect.appendChild(opt);
    });

    if (!state.selectedNode && nodes.length) {
        state.selectedNode = nodes[0].id;
        nodeSelect.value = nodes[0].id;
    }
}

async function loadModelsFromNode() {
    if (!state.selectedNode) {
        alert("Please select a node first");
        return;
    }

    modelSelect.innerHTML = '<option value="">Loading models...</option>';
    modelSelect.disabled = true;

    try {
        const res = await fetch(routerEndpoint(`/nodes/${state.selectedNode}/models`), {
            headers: authHeaders()
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const data = await res.json();
        const models = data.models || [];
        const normalizedModels = models
            .map((m) => normalizeModelMeta(m))
            .filter(Boolean);

        modelSelect.innerHTML = "";
        state.modelMeta = {};

        if (normalizedModels.length === 0) {
            const opt = document.createElement("option");
            opt.value = "";
            opt.textContent = "No models available";
            modelSelect.appendChild(opt);
        } else {
            normalizedModels.forEach((meta) => {
                state.modelMeta[meta.id] = meta;
                const opt = document.createElement("option");
                opt.value = meta.id;
                const label = meta.id.split("/").pop().slice(0, 40) + (meta.vision ? " 👁️" : "");
                opt.textContent = label;
                opt.dataset.vision = meta.vision ? "true" : "false";
                modelSelect.appendChild(opt);
            });

            modelSelect.value = normalizedModels[0].id;
        }

        console.log(`✅ Loaded ${normalizedModels.length} models from ${state.selectedNode}`);
        modelSelect.disabled = false;
        updateImageSupportNotice();
    } catch (err) {
        console.error("Failed to load models:", err);
        modelSelect.innerHTML = '<option value="">Error loading models</option>';
        modelSelect.disabled = false;
    }
}

// ---------------------------------------------
// MEMORY BOX
// ---------------------------------------------
async function showRelevantMemories(query) {
    if (!state.sessionId) return;
    const convo = state.conversations[state.sessionId];
    // Skip memory fetch for brand-new or empty conversations not yet persisted
    if (!convo || (convo.messages || []).length === 0) return;

    try {
        const params = new URLSearchParams({ query });
        const res = await fetch(routerEndpoint(`/conversations/${state.sessionId}/memories?${params}`), {
            headers: authHeaders()
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const data = await res.json();
        const memories = data.memories || [];

        if (memories.length === 0) {
            memoryBox.classList.add("hidden");
            return;
        }

        memoryBox.classList.remove("hidden");
        memoryBox.innerHTML = '<div class="memory-header">💾 Relevant memories loaded (used for routing/context)</div>';

        memories.forEach((m) => {
            const item = document.createElement("div");
            item.className = "memory-item";
            
            const role = m.role === "user" ? "👤 You" : "🤖 Assistant";
            const preview = m.content.slice(0, 60) + (m.content.length > 60 ? "..." : "");
            const similarity = (m.similarity * 100).toFixed(0);
            
            item.innerHTML = `<strong>${role}</strong> (${similarity}% match)<br><span style="color: #9ba4b5;">"${preview}"</span>`;
            memoryBox.appendChild(item);
        });

    } catch (err) {
        console.error("Failed to fetch memories:", err);
    }
}

// ---------------------------------------------
// MESSAGE FLOW
// ---------------------------------------------
async function routeQuery(prompt, contextMessages) {
    try {
        const res = await fetch(routerEndpoint("/route/decision"), {
            method: "POST",
            headers: authHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({
                prompt,
                context: (contextMessages || []).slice(-4),
                user_preferences: {
                    max_cost: routingPrefs.max_cost,
                    min_quality: routingPrefs.min_quality,
                    require_vision: state.pendingImages.length > 0
                }
            })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return await res.json();
    } catch (err) {
        console.warn("Routing decision failed, falling back to selected model:", err);
        return null;
    }
}

function pickNodeForModel(modelId) {
    if (!modelId) return state.selectedNode;
    if (modelId.toLowerCase().includes("qwen")) return "qwen-node";
    if (modelId.toLowerCase().includes("llama3") || modelId.toLowerCase().includes("llama")) return "mac-node";
    return state.selectedNode;
}

async function sendMessage() {
    const prompt = promptInput.value.trim();
    if (!prompt && state.pendingImages.length === 0) return;
    let attachedFileText = "";
    if (fileInput && fileInput.files && fileInput.files.length) {
        const file = fileInput.files[0];
        if (file.size > 1024 * 1024) {
            alert("Attached file is too large (max 1MB for inline include).");
            return;
        }
        const text = await file.text();
        attachedFileText = `\n\n[Attached file: ${file.name}]\n${text}`;
    }

    if (!state.sessionId || !state.conversations[state.sessionId]) {
        console.error("No active conversation, creating new one");
        await createNewConversation();
    }

    const convo = state.conversations[state.sessionId];
    if (!convo) {
        console.error("Failed to get conversation, aborting send");
        return;
    }

    const selectedMeta = getSelectedModelMeta();
    if (state.pendingImages.length > 0 && selectedMeta && !selectedMeta.vision) {
        const proceed = confirm("Selected model is marked text-only. Send anyway (images may be ignored)?");
        if (!proceed) {
            state.pendingImages = [];
            imageStatus.textContent = "Images cleared (text-only model selected)";
            imageInput.value = "";
            return;
        }
    }

    // Auto-route based on prefs
    const routeDecision = await routeQuery(prompt, convo.messages);
    if (routeDecision && routeDecision.model_id) {
        modelSelect.value = routeDecision.model_id;
        const suggestedNode = pickNodeForModel(routeDecision.model_id);
        if (suggestedNode) {
            state.selectedNode = suggestedNode;
            nodeSelect.value = suggestedNode;
        }
        if (routeStatus) {
            routeStatus.textContent = `Routing → ${routeDecision.model_id} (conf ${Math.round(routeDecision.confidence * 100)}%, est $${routeDecision.estimated_cost.toFixed(4)})`;
        }

        // Confidence-based fallback messaging
        if (routeDecision.confidence < 0.7) {
            const fallbackModel = routeDecision.model_id.includes("llama") ? modelSelect.value : routeDecision.model_id;
            routeStatus.textContent += " • Low confidence, may fallback to stronger model";
            modelSelect.value = fallbackModel;
        }
        state.stats.decisions.push(routeDecision);
        updateStatsDisplay();
    } else if (routeStatus) {
        routeStatus.textContent = "Routing unavailable, using selected model";
    }
    updateStatsDisplay();

    // Optional budget check
    try {
        const costRes = await fetch(routerEndpoint("/analytics/costs"), { headers: authHeaders() });
        if (costRes.ok) {
            const costData = await costRes.json();
            if (typeof costData.total_cost === "number" && costData.total_cost > 10) {
                const proceed = confirm(`⚠️ Total cost is $${costData.total_cost.toFixed(2)}. Continue?`);
                if (!proceed) return;
            }
        } else {
            console.warn("Cost analytics unavailable:", costRes.status);
        }
    } catch (e) {
        console.warn("Failed to fetch cost analytics:", e);
    }
    updateStatsDisplay();

    promptInput.value = "";
    promptInput.style.height = "auto";
    
    const now = Date.now();

    if (prompt) {
        convo.messages.push({
            role: "user",
            content: supportFlag && supportFlag.checked ? `[SUPPORT] ${prompt}${attachedFileText}` : `${prompt}${attachedFileText}`,
            timestamp: now
        });
        state.stats.totalMessages += 1;
        state.stats.totalTokens += Math.max(1, prompt.length / 4);
    }
    
    state.pendingImages.forEach(img => {
        convo.messages.push({
            role: "user",
            type: "image",
            content: img.image_url,
            timestamp: now
        });
    });

    renderMessages();
    
    if (prompt) showRelevantMemories(prompt);

    const streamingMsg = { role: "assistant", content: "", isStreaming: true };
    convo.messages.push(streamingMsg);

    state.streaming = true;
    state.abortController = new AbortController();

    try {
        const res = await fetch(routerEndpoint("/chat/stream"), {
            method: "POST",
            headers: authHeaders({ "Content-Type": "application/json" }),
            signal: state.abortController.signal,
            body: JSON.stringify({
                conversation_id: state.sessionId,
                prompt: prompt || "",
                max_tokens: 2048,
                temperature: 0.7,
                model: modelSelect.value || undefined,
                node_id: state.selectedNode || undefined,
                project_id: selectedProjectId || undefined,
                images: state.pendingImages.map(i => i.image_url)
            })
        });

        // Clear pending images (UI + state)
        state.pendingImages = [];
        imageStatus.textContent = "";
        imageInput.value = "";
        if (fileInput) {
            fileInput.value = "";
            if (fileStatus) fileStatus.textContent = "";
        }
        if (audioStatus) audioStatus.textContent = "";
        // Remove image-only placeholder messages from convo (optional)
        convo.messages = convo.messages.filter(m => m.type !== "image" || m.role !== "user" || m.content);

        if (!res.ok) {
            throw new Error(`Server error: ${res.status}`);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            
            buffer += decoder.decode(value, { stream: true });

            let newlineIndex;
            while ((newlineIndex = buffer.indexOf("\n")) !== -1) {
                const line = buffer.slice(0, newlineIndex).trimEnd();
                buffer = buffer.slice(newlineIndex + 1);

                if (!line.startsWith("data: ")) continue;

                try {
                    const data = JSON.parse(line.slice(6));
                    if (data.error) throw new Error(data.error);
                    
                    if (data.token && !data.done) {
                        streamingMsg.content += data.token;
                        renderMessages();
                    }
                    
                    if (data.done) {
                        streamingMsg.isStreaming = false;

                        // If this was the first exchange, backend may have created the convo; reload metadata
                        if (typeof data.message_count === "number" &&
                            data.message_count === 2 &&
                            convo.title === "New Conversation") {
                            await loadConversationHistory(state.sessionId);
                            renderConversationList();
                        }

                        renderMessages();
                        break;
                    }
                } catch (e) {
                    console.error("Error parsing SSE:", e);
                }
            }
        }
    } catch (err) {
        if (err.name === "AbortError") {
            console.log("🛑 Streaming stopped by user");
            // Quietly end without noisy markers
            streamingMsg.role = "assistant";
            // Leave content as-is and exit loop
        } else {
            console.error("Chat error:", err);
            streamingMsg.content = `❌ Error: ${err.message}`;
            streamingMsg.role = "error";
        }
        streamingMsg.isStreaming = false;
        renderMessages();
    } finally {
        state.streaming = false;
        state.abortController = null;
    }
}

function renderMessages() {
    const convo = state.conversations[state.sessionId];
    if (!convo) return;

    responseBox.innerHTML = "";

    // Context hygiene: nudge to start fresh on long threads
    if (convo.messages.length > 12) {
        const banner = document.createElement("div");
        banner.className = "info-banner";
        banner.textContent = "💡 Conversation getting long. Consider starting fresh for best context.";
        responseBox.appendChild(banner);
    }

    convo.messages.forEach((m) => {
        if (!m || !m.role) return;

        const msgDiv = document.createElement("div");
        msgDiv.className = `message message-${m.role}`;
        msgDiv.style.padding = "10px";
        msgDiv.style.marginBottom = "10px";
        msgDiv.style.borderRadius = "8px";
        msgDiv.style.whiteSpace = "pre-wrap";
        msgDiv.style.wordWrap = "break-word";

        const header = document.createElement("div");
        header.style.display = "flex";
        header.style.alignItems = "center";
        header.style.gap = "6px";

        const roleLabel = document.createElement("strong");
        roleLabel.textContent = m.role.toUpperCase() + ": ";
        header.appendChild(roleLabel);

        // Feedback buttons for assistant messages
        if (m.role === "assistant" && !m.isStreaming) {
            const fb = document.createElement("div");
            fb.style.display = "flex";
            fb.style.gap = "4px";
            const up = document.createElement("button");
            up.className = "action-icon";
            up.textContent = "👍";
            up.title = "Good answer";
            up.onclick = () => sendFeedback(1, m.content || "", m.model);
            const down = document.createElement("button");
            down.className = "action-icon";
            down.textContent = "👎";
            down.title = "Bad answer";
            down.onclick = () => sendFeedback(-1, m.content || "", m.model);
            fb.appendChild(up);
            fb.appendChild(down);
            header.appendChild(fb);
        }

        msgDiv.appendChild(header);

        const contentSpan = document.createElement("span");
        msgDiv.appendChild(contentSpan);

        const renderImage = (src) => {
            const img = document.createElement("img");
            img.src = src;
            img.alt = "uploaded image";
            img.style.maxWidth = "220px";
            img.style.maxHeight = "220px";
            img.style.display = "block";
            img.style.marginTop = "6px";
            img.style.borderRadius = "6px";
            contentSpan.appendChild(img);
        };

        if (m.type === "image") {
            renderImage(m.content);
        } else if (Array.isArray(m.content)) {
            m.content.forEach((part) => {
                if (part.type === "text") {
                    contentSpan.appendChild(document.createTextNode(part.text || ""));
                } else if (part.type === "image_url" && part.image_url && part.image_url.url) {
                    renderImage(part.image_url.url);
                }
            });
        } else if (typeof m.content === "string" && m.content.startsWith("data:image")) {
            renderImage(m.content);
        } else if (typeof m.content === "string" && m.content.includes("```")) {
            const parts = m.content.split("```");
            parts.forEach((part, idx) => {
                if (idx % 2 === 1) {
                    const pre = document.createElement("pre");
                    const code = document.createElement("code");
                    code.textContent = part;
                    pre.appendChild(code);
                    contentSpan.appendChild(pre);
                } else if (part) {
                    contentSpan.appendChild(document.createTextNode(part));
                }
            });
        } else {
            contentSpan.textContent = m.content;
        }
        
        if (m.isStreaming) {
            const indicator = document.createElement("span");
            indicator.textContent = "▌";
            indicator.style.animation = "blink 1s infinite";
            contentSpan.appendChild(indicator);
        }
        
        responseBox.appendChild(msgDiv);
    });

    if (state.autoScroll) {
        responseBox.scrollTop = responseBox.scrollHeight;
    }
    updateScrollBottomButton();
}

// ---------------------------------------------
// IMAGE HANDLING
// ---------------------------------------------
imageInput.addEventListener("change", (e) => {
    const files = Array.from(e.target.files || []);
    if (files.length === 0) {
        state.pendingImages = [];
        imageStatus.textContent = "";
        updateImageSupportNotice();
        return;
    }

    // Simple: support up to 3 images, base64 data URLs
    const maxImages = 3;
    const selected = files.slice(0, maxImages);
    state.pendingImages = [];
    imageStatus.textContent = "Loading image(s)...";

    let loaded = 0;

    selected.forEach(file => {
        const reader = new FileReader();
        reader.onload = () => {
            state.pendingImages.push({ image_url: reader.result });
            loaded += 1;
            if (loaded === selected.length) {
                const base = `${state.pendingImages.length} image${state.pendingImages.length > 1 ? "s" : ""} attached`;
                const visionNote = modelSupportsVision(modelSelect.value) ? " (vision-enabled)" : " (model may ignore images)";
                imageStatus.textContent = base + visionNote;
            }
        };
        reader.onerror = () => {
            console.error("Failed to read image file");
        };
        reader.readAsDataURL(file);
    });
});

// ---------------------------------------------
// EVENT BINDINGS
// ---------------------------------------------
sendBtn.onclick = () => {
    if (state.streaming && state.abortController) {
        state.abortController.abort();
    } else {
        sendMessage();
    }
};

refreshNodesBtn.onclick = fetchNodes;
newConvoBtn.onclick = createNewConversation;
loadModelsBtn.onclick = loadModelsFromNode;

nodeSelect.addEventListener("change", (e) => {
    state.selectedNode = e.target.value;
    if (state.selectedNode) {
        loadModelsFromNode();
    }
});

modelSelect.addEventListener("change", () => {
    updateImageSupportNotice();
});

promptInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

if (themeToggle) {
    themeToggle.addEventListener("click", () => {
        const current = localStorage.getItem(THEME_STORAGE_KEY) || "dark";
        const idx = THEMES.indexOf(current);
        const next = THEMES[(idx + 1) % THEMES.length] || "dark";
        applyTheme(next);
    });
}

if (scrollBottomBtn && responseBox) {
    scrollBottomBtn.addEventListener("click", () => {
        state.autoScroll = true;
        responseBox.scrollTo({ top: responseBox.scrollHeight, behavior: "smooth" });
    });
    responseBox.addEventListener("scroll", () => {
        updateScrollBottomButton();
        const atBottom = (responseBox.scrollHeight - responseBox.scrollTop - responseBox.clientHeight) < 40;
        state.autoScroll = atBottom;
    });
}

if (createFromTemplateBtn) {
    createFromTemplateBtn.addEventListener("click", () => {
        const name = templateSelect ? templateSelect.value : "general";
        createConversationFromTemplate(name);
    });
}

if (projectSelect) {
    projectSelect.addEventListener("change", async (e) => {
        const chosen = e.target.value;
        if (chosen === "__create__") {
            await createProjectFlow();
            return;
        }
        selectedProjectId = chosen;
        localStorage.setItem("dave_project_id", selectedProjectId);
        await loadConversationsFromBackend();
        renderConversationList();
        renderMessages();
    });
}

if (resyncBtn) {
    resyncBtn.addEventListener("click", resyncConversationInstructions);
}

if (editProjectBtn) {
    editProjectBtn.addEventListener("click", editProjectInstructions);
}

if (transcribeBtn) {
    transcribeBtn.addEventListener("click", transcribeAudio);
}

if (fileInput) {
    fileInput.addEventListener("change", () => {
        if (fileInput.files && fileInput.files.length) {
            const file = fileInput.files[0];
            if (fileStatus) fileStatus.textContent = `Attached: ${file.name}`;
        } else if (fileStatus) {
            fileStatus.textContent = "";
        }
    });
}

if (dictateBtn) {
    dictateBtn.addEventListener("click", toggleDictation);
}

if (hfDownloadBtn) {
    hfDownloadBtn.addEventListener("click", downloadModelFromHF);
}

// Add search UI dynamically into convo panel
function renderSearchBox() {
    const panel = document.querySelector(".panel.conversations");
    if (!panel) return;
    const existing = document.getElementById("globalSearchBox");
    if (existing) return;
    const box = document.createElement("div");
    box.id = "globalSearchBox";
    box.style.display = "flex";
    box.style.gap = "6px";
    box.style.marginBottom = "8px";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "Search all conversations...";
    input.style.flex = "1";
    const btn = document.createElement("button");
    btn.className = "icon-btn";
    btn.textContent = "🔍";
    btn.title = "Search";
    const results = document.createElement("div");
    results.id = "searchResults";
    results.style.maxHeight = "200px";
    results.style.overflowY = "auto";
    results.style.fontSize = "12px";
    btn.onclick = async () => {
        const q = input.value.trim();
        if (!q) return;
        results.textContent = "Searching...";
        try {
            const res = await fetch(routerEndpoint(`/search?query=${encodeURIComponent(q)}`), { headers: authHeaders() });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            results.innerHTML = "";
            data.forEach(item => {
                const div = document.createElement("div");
                div.style.padding = "4px";
                div.style.borderBottom = "1px solid var(--border)";
                div.innerHTML = `<strong>${item.title || item.conversation_id}</strong><br><em>${item.role}</em>: ${item.content.slice(0,120)}...`;
                div.onclick = async () => {
                    await switchConversation(item.conversation_id);
                };
                results.appendChild(div);
            });
            if (data.length === 0) {
                results.textContent = "No results";
            }
        } catch (e) {
            console.error("Search failed:", e);
            results.textContent = "Search failed";
        }
    };
    box.appendChild(input);
    box.appendChild(btn);
    // Insert after header if present
    const header = panel.querySelector(".panel-header");
    if (header && header.nextSibling) {
        panel.insertBefore(box, header.nextSibling);
        panel.insertBefore(results, box.nextSibling);
    } else {
        panel.insertBefore(results, panel.firstChild);
        panel.insertBefore(box, panel.firstChild);
    }
}

async function pollMonitoringBadge() {
    if (!monitorBadge) return;
    try {
        const data = await fetch(routerEndpoint("/monitoring/health"), { headers: authHeaders() }).then(r => r.json());
        const hasFailures = Object.keys(data.model_health || {}).some(
            (k) => (data.model_health[k].failures || 0) > 0
        );
        if (hasFailures || (data.recent_errors && data.recent_errors.length)) {
            monitorBadge.classList.remove("hidden");
        } else {
            monitorBadge.classList.add("hidden");
        }
    } catch (e) {
        monitorBadge.classList.remove("hidden");
    }
}

// ---------------------------------------------
// INITIALIZATION
// ---------------------------------------------
async function init() {
    console.log("🚀 Initializing DaveLLM UI...");
    initTheme();
    initRoutingControls();
    initDictation();
    loadStats();
    updateStatsDisplay();
    renderSearchBox();
    await loadProjects();
    await loadConversationsFromBackend();
    
    const lastSessionId = localStorage.getItem(LAST_SESSION_KEY);
    if (lastSessionId && state.conversations[lastSessionId]) {
        await switchConversation(lastSessionId);
    } else {
        await createNewConversation();
    }
    
    await fetchNodes();
    
    if (state.selectedNode) {
        await loadModelsFromNode();
    }
    
    console.log("✅ DaveLLM UI ready");
    pollMonitoringBadge();
    setInterval(pollMonitoringBadge, 60000);
}

init();
function complexityScoreClient(text = "") {
    const t = text || "";
    let score = 0;
    if (t.includes("```") || (t.includes("{") && t.includes("}"))) score += 0.3;
    if ((t.match(/\?/g) || []).length >= 2) score += 0.2;
    if (t.length > 800) score += 0.2;
    return Math.min(1, score);
}
let projects = [];
let selectedProjectId = localStorage.getItem("dave_project_id") || "";
