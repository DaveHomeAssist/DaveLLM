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
    nodeStatus: {},         // Map of nodeId -> online/offline/unknown
    autoScroll: true,       // Control autoscroll behavior
    lastSessionId: null,
    lastPredictionUndo: null,
    restoredSelection: false,
    selectionFallback: false,
    stats: {
        decisions: [],
        totalTokens: 0,
        totalMessages: 0
    }
};

// ---------------------------------------------
// CONSTANTS
// ---------------------------------------------
const ROUTER_BASE = (typeof window !== "undefined" && window.__API_BASE__)
    || (typeof window !== "undefined" ? window.location.origin : "http://127.0.0.1:8000");
// Conversation and feedback content live only on the router. These legacy
// browser keys once mirrored them and are purged so no prompt or response
// text remains in persistent browser storage.
const LEGACY_CONTENT_STORAGE_KEYS = ["dave_convos", "dave_feedback"];
const LAST_SESSION_KEY = "dave_last_session";
const API_KEY_SESSION_KEY = "dave_api_key_session";
const THEME_STORAGE_KEY = "dave_theme";
const ANTICIPATION_STORAGE_KEY = "davellm_anticipation_v1";
const DRAFT_SESSION_KEY = "davellm_draft_session";
const MOBILE_TAB_SESSION_KEY = "davellm_mobile_tab_session";
const THEMES = ["dark", "light", "forest"];

// Browser credentials live only for the current tab session. Electron injects
// the header in the main process and never exposes the key to this renderer.
let apiKey = sessionStorage.getItem(API_KEY_SESSION_KEY) || "";
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
const TEMPLATE_LABELS = {
    general: "General",
    code_review: "Code Review",
    brainstorm: "Brainstorm"
};
let projects = [];
let selectedProjectId = localStorage.getItem("dave_project_id") || "";
let anticipationRecord = window.DaveAnticipation.createRecord();
let notepadSaveTimer = null;
let notepadLoadedProjectId = null;
let notepadLoading = false;
let instructionPreviewMode = "layered";
let projectHomeData = null;
let projectHomeProjectId = null;
let projectHomeMotion = null;

function authHeaders(extra = {}) {
    const headers = { ...extra };
    if (apiKey) headers["X-API-Key"] = apiKey;
    return headers;
}

function requestBrowserCredential() {
    if (window.__DAVE_DESKTOP__) return true;
    const entered = window.prompt("Enter the DaveLLM API key for this browser session:", "");
    if (!entered) return false;
    apiKey = entered;
    sessionStorage.setItem(API_KEY_SESSION_KEY, apiKey);
    return true;
}

// Theme handling
function applyTheme(theme) {
    const safeTheme = THEMES.includes(theme) ? theme : "dark";
    document.documentElement.setAttribute("data-theme", safeTheme);
    localStorage.setItem(THEME_STORAGE_KEY, safeTheme);
    if (themeToggle) {
        const titleMap = {
            dark: "Switch to light mode",
            light: "Switch to forest mode",
            forest: "Switch to dark mode"
        };
        themeToggle.dataset.theme = safeTheme;
        themeToggle.title = titleMap[safeTheme] || "Toggle theme";
        themeToggle.setAttribute("aria-label", themeToggle.title);
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
const runToolsBtn = document.getElementById("runToolsBtn");
const runLedger = document.getElementById("runLedger");
const runLedgerStatus = document.getElementById("runLedgerStatus");
const runLedgerReason = document.getElementById("runLedgerReason");
const runLedgerApproval = document.getElementById("runLedgerApproval");
const runLedgerEvents = document.getElementById("runLedgerEvents");
const runLedgerTranscript = document.getElementById("runLedgerTranscript");
const runLedgerRetry = document.getElementById("runLedgerRetry");
const runLedgerStop = document.getElementById("runLedgerStop");
let activeToolRunId = null;
let toolRunState = { runId: null, cursor: 0, events: [] };
let toolRunWatcher = null;
let toolRunStreamAbort = null;
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
const credentialBtn = document.getElementById("credentialBtn");
const suggestionChips = document.getElementById("suggestionChips");
const suggestedNext = document.getElementById("suggestedNext");
const resetSuggestionsBtn = document.getElementById("resetSuggestionsBtn");
const contextStatus = document.getElementById("contextStatus");
const contextProjectValue = document.getElementById("contextProjectValue");
const contextTemplateValue = document.getElementById("contextTemplateValue");
const contextNodeValue = document.getElementById("contextNodeValue");
const contextModelValue = document.getElementById("contextModelValue");
const contextModelMarker = document.getElementById("contextModelMarker");
const topbarStatusDot = document.getElementById("topbarStatusDot");
const topbarNodeValue = document.getElementById("topbarNodeValue");
const topbarStatusValue = document.getElementById("topbarStatusValue");
const topbarModelValue = document.getElementById("topbarModelValue");
const undoPredictionBtn = document.getElementById("undoPredictionBtn");
const attachToolsToggle = document.getElementById("attachToolsToggle");
const attachmentTools = document.getElementById("attachmentTools");
const instructionsBtn = document.getElementById("instructionsBtn");
const instructionsDialog = document.getElementById("instructionsDialog");
const instructionsClose = document.getElementById("instructionsClose");
const globalInstructions = document.getElementById("globalInstructions");
const projectInstructions = document.getElementById("projectInstructions");
const sessionInstructions = document.getElementById("sessionInstructions");
const effectiveInstructions = document.getElementById("effectiveInstructions");
const globalInstructionsCount = document.getElementById("globalInstructionsCount");
const projectInstructionsCount = document.getElementById("projectInstructionsCount");
const sessionInstructionsCount = document.getElementById("sessionInstructionsCount");
const effectiveInstructionsCount = document.getElementById("effectiveInstructionsCount");
const instructionProjectName = document.getElementById("instructionProjectName");
const instructionsStatus = document.getElementById("instructionsStatus");
const saveInstructionsBtn = document.getElementById("saveInstructions");
const revertSessionInstructionsBtn = document.getElementById("revertSessionInstructions");
const resetGlobalInstructionsBtn = document.getElementById("resetGlobalInstructions");
const notepadToggle = document.getElementById("notepadToggle");
const notepadPanel = document.getElementById("notepadPanel");
const notepadClose = document.getElementById("notepadClose");
const notepadInput = document.getElementById("notepadInput");
const notepadStatus = document.getElementById("notepadStatus");
const notepadProjectName = document.getElementById("notepadProjectName");
const sendNotepadBtn = document.getElementById("sendNotepadBtn");
const projectHomeDialog = document.getElementById("projectHomeDialog");
const projectHomeClose = document.getElementById("projectHomeClose");
const projectHomeHeading = document.getElementById("projectHomeHeading");
const projectHomeDescription = document.getElementById("projectHomeDescription");
const projectAttachmentState = document.getElementById("projectAttachmentState");
const attachProjectChatBtn = document.getElementById("attachProjectChat");
const startProjectChatBtn = document.getElementById("startProjectChat");
const projectContextBudget = document.getElementById("projectContextBudget");
const contextBudgetMeter = document.getElementById("contextBudgetMeter");
const projectHomeInstructions = document.getElementById("projectHomeInstructions");
const projectHomeInstructionsCount = document.getElementById("projectHomeInstructionsCount");
const saveProjectInstructionsBtn = document.getElementById("saveProjectInstructions");
const projectFileForm = document.getElementById("projectFileForm");
const projectFileInput = document.getElementById("projectFileInput");
const projectFilesStatus = document.getElementById("projectFilesStatus");
const projectFilesList = document.getElementById("projectFilesList");
const projectArtifactsStatus = document.getElementById("projectArtifactsStatus");
const projectArtifactsList = document.getElementById("projectArtifactsList");
const projectArtifactPreview = document.getElementById("projectArtifactPreview");
const brainPinned = document.getElementById("brainPinned");
const brainActive = document.getElementById("brainActive");
const brainRecent = document.getElementById("brainRecent");
const brainThreshold = document.getElementById("brainThreshold");
const brainRevision = document.getElementById("brainRevision");
const brainRevisionSelect = document.getElementById("brainRevisionSelect");
const projectBrainStatus = document.getElementById("projectBrainStatus");
const saveProjectBrainBtn = document.getElementById("saveProjectBrain");
const compactProjectBrainBtn = document.getElementById("compactProjectBrain");
const restoreProjectBrainBtn = document.getElementById("restoreProjectBrain");
const deleteProjectBrainBtn = document.getElementById("deleteProjectBrain");
const previewProjectContextBtn = document.getElementById("previewProjectContext");
const projectContextPreviewStatus = document.getElementById("projectContextPreviewStatus");
const projectContextPreviewOutput = document.getElementById("projectContextPreviewOutput");
const projectHomeStatus = document.getElementById("projectHomeStatus");
const mobileTabButtons = Array.from(document.querySelectorAll("[data-mobile-tab]"));
const mobilePanels = Array.from(document.querySelectorAll("[data-mobile-panel]"));

// ---------------------------------------------
// UTIL
// ---------------------------------------------
function routerEndpoint(path) {
    return `${ROUTER_BASE}${path}`;
}

function setLucideIcon(button, iconId) {
    if (!button) return;
    let svg = button.querySelector("svg");
    let use = svg?.querySelector("use");
    if (!svg || !use) {
        svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("aria-hidden", "true");
        use = document.createElementNS("http://www.w3.org/2000/svg", "use");
        svg.appendChild(use);
        button.replaceChildren(svg);
    }
    use.setAttribute("href", `vendor/lucide/lucide.svg#${iconId}`);
}

function readAnticipationRecord() {
    try {
        const raw = localStorage.getItem(ANTICIPATION_STORAGE_KEY);
        return window.DaveAnticipation.normalizeRecord(raw ? JSON.parse(raw) : null);
    } catch (error) {
        console.warn("Failed to read suggestion preferences; using defaults", error);
        return window.DaveAnticipation.createRecord();
    }
}

function writeAnticipationRecord(record) {
    anticipationRecord = window.DaveAnticipation.normalizeRecord(record);
    localStorage.setItem(ANTICIPATION_STORAGE_KEY, JSON.stringify(anticipationRecord));
    return anticipationRecord;
}

function updateAnticipationPreferences() {
    anticipationRecord = window.DaveAnticipation.updatePreferences(anticipationRecord, {
        lastTemplate: templateSelect?.value || "general",
        lastProjectId: selectedProjectId || null,
        lastNodeId: state.selectedNode || null,
        lastModelId: modelSelect?.value || null
    });
    if (selectedProjectId) {
        const existingDefault = anticipationRecord.preferences.projectDefaults[selectedProjectId];
        anticipationRecord = window.DaveAnticipation.setProjectDefault(anticipationRecord, selectedProjectId, {
            template: templateSelect?.value || "general",
            nodeId: state.selectedNode || null,
            modelId: modelSelect?.value || null,
            supportMode: existingDefault?.supportMode === true
        });
    }
    writeAnticipationRecord(anticipationRecord);
}

function currentConversation() {
    return state.sessionId ? state.conversations[state.sessionId] : null;
}

function conversationHasMessages() {
    const convo = currentConversation();
    return Boolean(convo && Array.isArray(convo.messages) && convo.messages.length);
}

function detectAttachmentKind() {
    if (state.pendingImages.length || (imageInput?.files && imageInput.files.length)) return "image";
    if (fileInput?.files && fileInput.files.length) {
        const file = fileInput.files[0];
        const codeExtension = /\.(?:js|jsx|ts|tsx|py|go|rs|java|c|cc|cpp|h|hpp|cs|rb|php|swift|kt|kts|sql|sh|html|css|json|ya?ml)$/i;
        const codeMime = /(?:javascript|typescript|json|xml|yaml|shell|python|source)/i;
        return codeExtension.test(file.name || "") || codeMime.test(file.type || "") ? "code" : "file";
    }
    if (audioInput?.files && audioInput.files.length) return "audio";
    return null;
}

function lastAssistantSignals() {
    const convo = currentConversation();
    if (!convo || !Array.isArray(convo.messages)) return null;
    const message = [...convo.messages].reverse().find((item) => item && (item.role === "assistant" || item.role === "error"));
    if (!message || message.isStreaming) return null;
    const content = typeof message.content === "string" ? message.content : "";
    return {
        hasCode: content.includes("```"),
        isError: message.role === "error" || /^\s*(?:error|❌)/i.test(content),
        isLong: content.length > 800,
        hasList: /(?:^|\n)\s*(?:[-*]|\d+[.)])\s+\S/.test(content)
    };
}

function buildPredictionContext(overrides = {}) {
    const lastConversation = state.lastSessionId && state.conversations[state.lastSessionId]
        ? {
            id: state.lastSessionId,
            title: state.conversations[state.lastSessionId].title
        }
        : null;
    const selectedMeta = getSelectedModelMeta();
    const visionModel = Object.values(state.modelMeta).find((meta) => meta.vision);
    const projectDefault = selectedProjectId
        ? anticipationRecord.preferences.projectDefaults[selectedProjectId]
        : null;
    return {
        activeConversationId: state.sessionId,
        lastConversation,
        composer: promptInput?.value || "",
        attachmentKind: detectAttachmentKind(),
        hasMessages: conversationHasMessages(),
        template: templateSelect?.value || "general",
        selectedModel: selectedMeta ? { id: selectedMeta.id, vision: Boolean(selectedMeta.vision) } : null,
        recommendedVisionModelId: visionModel?.id || null,
        lastAssistant: lastAssistantSignals(),
        projectId: selectedProjectId || null,
        projectSupportCount: selectedProjectId
            ? anticipationRecord.usage.supportCountsByProject[selectedProjectId] || 0
            : 0,
        projectSupportDefault: Boolean(projectDefault?.supportMode),
        dismissedPredictionIds: Object.keys(anticipationRecord.dismissals),
        ...overrides
    };
}

function captureControlSnapshot() {
    return {
        sessionId: state.sessionId,
        projectId: selectedProjectId || "",
        template: templateSelect?.value || "general",
        nodeId: state.selectedNode,
        modelId: modelSelect?.value || null,
        supportMode: Boolean(supportFlag?.checked),
        projectSupportDefault: selectedProjectId
            ? anticipationRecord.preferences.projectDefaults[selectedProjectId]?.supportMode === true
            : false,
        composer: promptInput?.value || ""
    };
}

function exposeUndo(snapshot, message) {
    state.lastPredictionUndo = snapshot;
    if (undoPredictionBtn) undoPredictionBtn.classList.remove("hidden");
    if (contextStatus) contextStatus.textContent = message;
}

function clearUndoState() {
    state.lastPredictionUndo = null;
    undoPredictionBtn?.classList.add("hidden");
}

function resizeComposer() {
    if (!promptInput) return;
    promptInput.style.height = "auto";
    const viewportHeight = window.visualViewport?.height || window.innerHeight;
    const maximum = Math.max(120, Math.round(viewportHeight * 0.35));
    promptInput.style.height = `${Math.min(promptInput.scrollHeight, maximum)}px`;
    promptInput.style.overflowY = promptInput.scrollHeight > maximum ? "auto" : "hidden";
}

function updateVisualViewportMetrics() {
    const viewport = window.visualViewport;
    const keyboardInset = viewport
        ? Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop)
        : 0;
    document.documentElement.style.setProperty("--keyboard-inset", `${Math.round(keyboardInset)}px`);
    resizeComposer();
}

function initVisualViewport() {
    updateVisualViewportMetrics();
    if (!window.visualViewport) return;
    window.visualViewport.addEventListener("resize", updateVisualViewportMetrics);
    window.visualViewport.addEventListener("scroll", updateVisualViewportMetrics);
}

function persistSessionDraft() {
    if (!promptInput) return;
    if (promptInput.value) sessionStorage.setItem(DRAFT_SESSION_KEY, promptInput.value);
    else sessionStorage.removeItem(DRAFT_SESSION_KEY);
}

function selectComposerText() {
    promptInput.focus();
    promptInput.setSelectionRange(0, promptInput.value.length);
}

function updateContextStrip() {
    const project = projects.find((item) => item.project_id === selectedProjectId);
    const node = state.nodes.find((item) => item.id === state.selectedNode);
    const rawNodeStatus = node ? (state.nodeStatus[node.id]?.status || "unknown") : "unknown";
    const nodeStatus = ["online", "offline"].includes(rawNodeStatus) ? rawNodeStatus : "unknown";
    const statusLabel = nodeStatus === "online" ? "Online" : (nodeStatus === "offline" ? "Offline" : "Unknown");
    if (contextProjectValue) contextProjectValue.textContent = project?.name || "None";
    if (contextTemplateValue) contextTemplateValue.textContent = TEMPLATE_LABELS[templateSelect?.value] || "General";
    if (contextNodeValue) contextNodeValue.textContent = node?.name || "None";
    if (contextModelValue) contextModelValue.textContent = modelSelect?.value || "None";
    if (topbarNodeValue) topbarNodeValue.textContent = node?.name || "No node";
    if (topbarStatusValue) topbarStatusValue.textContent = statusLabel;
    if (topbarModelValue) topbarModelValue.textContent = modelSelect?.value || "No model";
    if (topbarStatusDot) {
        topbarStatusDot.className = `topbar-status-dot status-${nodeStatus}`;
    }
    if (contextModelMarker) {
        contextModelMarker.classList.toggle("hidden", !state.restoredSelection || state.selectionFallback);
    }
    if (contextStatus && !state.lastPredictionUndo) {
        contextStatus.textContent = state.selectionFallback
            ? "Stored selection unavailable; using safe defaults"
            : (state.restoredSelection ? "Using last selection" : "Manual selection");
    }
}

function renderPredictions() {
    if (!suggestionChips || !suggestedNext) return;
    const predictions = window.DaveAnticipation.getPredictions(buildPredictionContext());
    suggestionChips.replaceChildren();

    if (predictions.length === 0) {
        suggestedNext.classList.add("hidden");
        return;
    }
    suggestedNext.classList.remove("hidden");

    predictions.forEach((prediction) => {
        const item = document.createElement("div");
        item.className = "suggestion-item";
        item.setAttribute("role", "listitem");

        const action = document.createElement("button");
        action.type = "button";
        action.className = "suggestion-chip";
        action.textContent = prediction.label;
        action.title = prediction.reason;
        action.addEventListener("click", () => applyPrediction(prediction));

        const dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.className = "suggestion-dismiss";
        dismiss.textContent = "×";
        dismiss.title = `Dismiss ${prediction.label}`;
        dismiss.setAttribute("aria-label", dismiss.title);
        dismiss.addEventListener("click", () => {
            writeAnticipationRecord(window.DaveAnticipation.dismissPrediction(anticipationRecord, prediction.id));
            renderPredictions();
        });

        item.appendChild(action);
        item.appendChild(dismiss);
        suggestionChips.appendChild(item);
    });
}

async function applyPrediction(prediction, options = {}) {
    if (!prediction || !prediction.kind) return;
    const snapshot = captureControlSnapshot();
    const payload = prediction.payload || {};

    if (prediction.kind === "continue_conversation" && state.conversations[payload.conversationId]) {
        await switchConversation(payload.conversationId);
    } else if (prediction.kind === "prefill" && typeof payload.text === "string") {
        if (options.requireEmpty && promptInput.value.trim()) return;
        promptInput.value = payload.text;
        resizeComposer();
        persistSessionDraft();
        selectComposerText();
    } else if (prediction.kind === "select_template" && TEMPLATES[payload.template]) {
        if (conversationHasMessages()) return;
        templateSelect.value = payload.template;
        updateAnticipationPreferences();
    } else if (prediction.kind === "select_model" && state.modelMeta[payload.modelId]) {
        modelSelect.value = payload.modelId;
        updateImageSupportNotice();
        updateAnticipationPreferences();
    } else if (prediction.kind === "remember_support" && selectedProjectId === payload.projectId) {
        anticipationRecord = window.DaveAnticipation.setProjectDefault(anticipationRecord, selectedProjectId, {
            template: templateSelect.value,
            nodeId: state.selectedNode,
            modelId: modelSelect.value,
            supportMode: true
        });
        writeAnticipationRecord(anticipationRecord);
        supportFlag.checked = true;
    } else {
        return;
    }

    if (!options.automatic) {
        anticipationRecord = window.DaveAnticipation.incrementUsage(
            anticipationRecord,
            "nextActionCounts",
            prediction.id
        );
        writeAnticipationRecord(anticipationRecord);
    }
    exposeUndo(snapshot, options.automatic ? "Starter added · Undo available" : `${prediction.label} applied · Undo available`);
    updateContextStrip();
    renderPredictions();
}

async function undoLastPrediction() {
    const snapshot = state.lastPredictionUndo;
    if (!snapshot) return;
    state.lastPredictionUndo = null;
    undoPredictionBtn?.classList.add("hidden");

    const previousProjectId = selectedProjectId;
    selectedProjectId = projects.some((item) => item.project_id === snapshot.projectId) ? snapshot.projectId : "";
    if (projectSelect) projectSelect.value = selectedProjectId;
    localStorage.setItem("dave_project_id", selectedProjectId);
    if (previousProjectId !== selectedProjectId) await loadConversationsFromBackend();

    if (snapshot.sessionId && state.conversations[snapshot.sessionId]) {
        if (state.sessionId !== snapshot.sessionId) await switchConversation(snapshot.sessionId);
        else {
            renderConversationList();
            renderMessages();
        }
    } else {
        state.sessionId = null;
        renderConversationList();
        renderMessages();
    }

    templateSelect.value = TEMPLATES[snapshot.template] ? snapshot.template : "general";
    const targetNode = state.nodes.some((item) => item.id === snapshot.nodeId)
        ? snapshot.nodeId
        : state.nodes[0]?.id;
    if (targetNode) {
        state.selectedNode = targetNode;
        nodeSelect.value = targetNode;
        await loadModelsFromNode(snapshot.modelId);
    }
    supportFlag.checked = snapshot.supportMode;
    if (selectedProjectId) {
        anticipationRecord = window.DaveAnticipation.setProjectDefault(anticipationRecord, selectedProjectId, {
            template: templateSelect.value,
            nodeId: state.selectedNode,
            modelId: modelSelect.value,
            supportMode: snapshot.projectSupportDefault === true
        });
    }
    updateAnticipationPreferences();
    promptInput.value = snapshot.composer;
    resizeComposer();
    persistSessionDraft();
    state.restoredSelection = false;
    state.selectionFallback = false;
    updateContextStrip();
    renderPredictions();
}

function maybePrefillForAttachment(kind) {
    if (!kind || promptInput.value.trim()) return;
    const prediction = window.DaveAnticipation.getPredictions(buildPredictionContext({
        attachmentKind: kind,
        composer: ""
    })).find((item) => item.kind === "prefill");
    if (prediction && !promptInput.value.trim()) {
        applyPrediction(prediction, { automatic: true, requireEmpty: true });
    }
}

function applyProjectSupportDefault() {
    const projectDefault = selectedProjectId
        ? anticipationRecord.preferences.projectDefaults[selectedProjectId]
        : null;
    if (!conversationHasMessages() && supportFlag) {
        supportFlag.checked = Boolean(projectDefault?.supportMode);
    }
}

function syncMobilePanelSemantics() {
    const mobile = window.matchMedia("(max-width: 1024px)").matches;
    mobilePanels.forEach((panel) => {
        if (mobile) {
            panel.setAttribute("role", "tabpanel");
            panel.setAttribute("aria-labelledby", `${panel.dataset.tabId} ${panel.dataset.headingId}`);
            panel.setAttribute("aria-hidden", panel.classList.contains("mobile-active") ? "false" : "true");
        } else {
            panel.removeAttribute("role");
            panel.removeAttribute("aria-hidden");
            panel.setAttribute("aria-labelledby", panel.dataset.headingId);
        }
    });
}

function setMobileTab(tabName, focusTab = false) {
    const valid = mobileTabButtons.some((button) => button.dataset.mobileTab === tabName);
    const selected = valid ? tabName : "chat";
    sessionStorage.setItem(MOBILE_TAB_SESSION_KEY, selected);
    mobileTabButtons.forEach((button) => {
        const active = button.dataset.mobileTab === selected;
        button.setAttribute("aria-selected", active ? "true" : "false");
        button.tabIndex = active ? 0 : -1;
        if (active && focusTab) button.focus();
    });
    mobilePanels.forEach((panel) => {
        panel.classList.toggle("mobile-active", panel.dataset.mobilePanel === selected);
    });
    syncMobilePanelSemantics();
}

function initMobileTabs() {
    setMobileTab(sessionStorage.getItem(MOBILE_TAB_SESSION_KEY) || "chat");
    mobileTabButtons.forEach((button, index) => {
        button.addEventListener("click", () => setMobileTab(button.dataset.mobileTab));
        button.addEventListener("keydown", (event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            let targetIndex = index;
            if (event.key === "ArrowLeft") targetIndex = (index - 1 + mobileTabButtons.length) % mobileTabButtons.length;
            if (event.key === "ArrowRight") targetIndex = (index + 1) % mobileTabButtons.length;
            if (event.key === "Home") targetIndex = 0;
            if (event.key === "End") targetIndex = mobileTabButtons.length - 1;
            setMobileTab(mobileTabButtons[targetIndex].dataset.mobileTab, true);
        });
    });
    window.matchMedia("(max-width: 1024px)").addEventListener("change", syncMobilePanelSemantics);
}

function replaceSelectOptions(select, label, value = "") {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.replaceChildren(option);
}

// Mirrors detect_vision() in app.py: "mm" only as a whole token, so "gemma" is
// not treated as vision-capable, and "vl" with an optional version suffix so
// qwen2.5vl, internvl2 and deepseek-vl2 are.
function modelIdLooksVisionCapable(modelId) {
    const lowerId = String(modelId || "").toLowerCase();
    if (!lowerId) return false;
    if (lowerId.includes("vision") || lowerId.includes("multimodal")) return true;
    if (lowerId.split(/[^a-z0-9]+/).includes("mm")) return true;
    return /vl[0-9]*(?![a-z0-9])/.test(lowerId);
}

function normalizeModelMeta(model) {
    if (!model) return null;

    const id = typeof model === "string" ? model : (model.id || model.model || model.name);
    if (!id) return null;

    let vision = modelIdLooksVisionCapable(id);

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
    return modelIdLooksVisionCapable(modelId);
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
        if (dictateBtn) {
            setLucideIcon(dictateBtn, "square");
            dictateBtn.setAttribute("aria-label", "Stop dictation");
        }
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
        if (dictateBtn) {
            setLucideIcon(dictateBtn, "mic");
            dictateBtn.setAttribute("aria-label", "Dictate using microphone");
        }
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
            replaceSelectOptions(projectSelect, "All Projects");
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
        state.sessionId = null;
        updateAnticipationPreferences();
        renderConversationList();
        renderMessages();
    } catch (e) {
        console.error("Failed to create project:", e);
        alert("Could not create project");
    }
}

function purgeLegacyContentStorage() {
    try {
        LEGACY_CONTENT_STORAGE_KEYS.forEach((key) => localStorage.removeItem(key));
    } catch (e) {}
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
                if (dictateBtn) {
                    setLucideIcon(dictateBtn, "square");
                    dictateBtn.setAttribute("aria-label", "Stop recording");
                }
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
    if (dictateBtn) {
        setLucideIcon(dictateBtn, "mic");
        dictateBtn.setAttribute("aria-label", "Dictate using microphone");
    }
    if (mediaStream) {
        mediaStream.getTracks().forEach((t) => t.stop());
        mediaStream = null;
    }
}

async function uploadDictationBlob(blob) {
    try {
        const audioBytes = new Uint8Array(await blob.arrayBuffer());
        const uploadBlob = new Blob([audioBytes], { type: blob.type || "audio/webm" });
        const form = new FormData();
        form.append("file", uploadBlob, "dictation.webm");
        const res = await fetch(routerEndpoint("/audio/transcribe"), {
            method: "POST",
            headers: authHeaders(),
            body: form
        });
        if (!res.ok) {
            throw new Error(await apiError(res));
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
 * The router is the only source of truth; a failure leaves the list empty
 * and visible as an error instead of restoring a browser copy.
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
                project_id: c.project_id || null,
                system_prompt: c.system_prompt || ""
            };
        });
        
        console.log(`✅ Loaded ${convos.length} conversations from backend`);
        return true;
        
    } catch (err) {
        console.error("Failed to load conversations from backend:", err);
        state.conversations = {};
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
            project_id: data.project_id || null,
            system_prompt: data.system_prompt || "",
            session_override: data.session_override || ""
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

async function exportConversation(cid, title) {
    try {
        const res = await fetch(
            routerEndpoint(`/conversations/${encodeURIComponent(cid)}/export?format=markdown`),
            { headers: authHeaders() },
        );
        if (!res.ok) {
            const detail = await res.text();
            throw new Error(detail || `HTTP ${res.status}`);
        }
        const blob = await res.blob();
        const objectUrl = URL.createObjectURL(blob);
        const link = document.createElement("a");
        const safeTitle = (title || "conversation").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "conversation";
        link.href = objectUrl;
        link.download = `${safeTitle}.md`;
        try {
            document.body.appendChild(link);
            link.click();
        } finally {
            link.remove();
            URL.revokeObjectURL(objectUrl);
        }
    } catch (error) {
        console.error("Conversation export failed:", error);
        alert(`Export failed: ${error.message}`);
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

    convoList.replaceChildren();

    const sorted = getSortedConversations();

    sorted.forEach(([cid, convo]) => {
        const div = document.createElement("div");
        div.className = "convo-item";
        div.setAttribute("role", "button");
        div.tabIndex = 0;
        div.setAttribute("aria-label", `Open conversation ${convo.title}`);
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
        actionsDiv.style.gap = "4px";
        actionsDiv.style.alignItems = "center";
        actionsDiv.style.flexShrink = "0";
        actionsDiv.style.marginLeft = "8px";

        const renameBtn = document.createElement("button");
        renameBtn.className = "action-icon";
        renameBtn.textContent = "✏️";
        renameBtn.title = "Rename conversation";
        renameBtn.setAttribute("aria-label", `Rename ${convo.title}`);
        renameBtn.onclick = (e) => {
            e.stopPropagation();
            if (state.renaming) return;
            renameConversation(cid, div);
        };
        actionsDiv.appendChild(renameBtn);

        const clearBtn = document.createElement("button");
        clearBtn.className = "action-icon";
        clearBtn.textContent = "🗑️";
        clearBtn.title = "Clear messages";
        clearBtn.setAttribute("aria-label", `Clear messages in ${convo.title}`);
        clearBtn.onclick = (e) => {
            e.stopPropagation();
            clearConversation(cid);
        };
        actionsDiv.appendChild(clearBtn);

        const deleteBtn = document.createElement("button");
        deleteBtn.className = "action-icon";
        deleteBtn.textContent = "❌";
        deleteBtn.title = "Delete conversation";
        deleteBtn.setAttribute("aria-label", `Delete ${convo.title}`);
        deleteBtn.onclick = (e) => {
            e.stopPropagation();
            deleteConversation(cid, div);
        };
        actionsDiv.appendChild(deleteBtn);

        const exportBtn = document.createElement("button");
        exportBtn.className = "action-icon";
        exportBtn.textContent = "📥";
        exportBtn.title = "Export conversation (markdown)";
        exportBtn.setAttribute("aria-label", `Export ${convo.title} as markdown`);
        exportBtn.onclick = async (e) => {
            e.stopPropagation();
            await exportConversation(cid, convo.title);
        };
        actionsDiv.appendChild(exportBtn);

        div.appendChild(titleContainer);
        div.appendChild(actionsDiv);

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

        div.addEventListener("keydown", async (event) => {
            if (event.target !== div || !["Enter", " "].includes(event.key)) return;
            event.preventDefault();
            await switchConversation(cid);
        });

        convoList.appendChild(div);
    });
}

async function switchConversation(cid) {
    if (state.renaming) return;
    clearUndoState();

    const convo = state.conversations[cid];
    if (!convo) {
        console.error(`Conversation ${cid} not found in state`);
        return;
    }

    await flushProjectNotepadSave();

    state.sessionId = cid;
    state.lastSessionId = cid;
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
    applyProjectSupportDefault();
    if (!notepadPanel.hidden) {
        notepadLoadedProjectId = null;
        const projectId = activeNotepadProjectId();
        if (projectId) await loadProjectNotepad(projectId);
        else closeProjectNotepad();
    }
    setMobileTab("chat");
    updateContextStrip();
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
        renderMessages();
    } catch (err) {
        console.error("Failed to clear conversation:", err);
        alert("Failed to clear conversation");
    }
}

async function createNewConversation() {
    if (templateSelect) templateSelect.value = "general";
    return createConversationFromTemplate("general");
}

async function createConversationFromTemplate(name) {
    clearUndoState();
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
            messages: [],
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            project_id: data.project_id || selectedProjectId || null,
            system_prompt: data.system_prompt || ""
        };
        state.sessionId = id;
        state.lastSessionId = id;
        localStorage.setItem(LAST_SESSION_KEY, id);
        if (templateSelect) templateSelect.value = name;
        renderConversationList();
        renderMessages();
        applyProjectSupportDefault();
        updateAnticipationPreferences();
        setMobileTab("chat");
        return id;
    } catch (e) {
        console.error("Failed to create from template:", e);
        return null;
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

async function apiError(response) {
    const text = await response.text();
    try {
        const parsed = JSON.parse(text);
        return parsed.detail || text || `HTTP ${response.status}`;
    } catch (_error) {
        return text || `HTTP ${response.status}`;
    }
}

function projectBudgetQuotas(totalValue) {
    const total = Math.max(0, Number.parseInt(totalValue, 10) || 0);
    const instructions = Math.floor(total * 0.25);
    const brain = Math.floor(total * 0.25);
    const files = Math.round(total * 0.30);
    return {
        project_instructions: instructions,
        brain,
        file_context: files,
        artifact_history: total - instructions - brain - files
    };
}

function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function createProjectEmptyState(message) {
    const empty = document.createElement("div");
    empty.className = "project-empty-state";
    empty.textContent = message;
    return empty;
}

function createRecordButton(label, action, className = "text-btn") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.addEventListener("click", action);
    return button;
}

function resetProjectHomeMotion() {
    if (projectHomeMotion) {
        projectHomeMotion.revert();
        projectHomeMotion = null;
    }
}

function animateProjectHomepageIn() {
    if (!window.gsap || !projectHomeDialog?.open) return;
    resetProjectHomeMotion();
    projectHomeMotion = window.gsap.matchMedia();
    const targets = projectHomeDialog.querySelectorAll(
        ".project-home-header, .project-home-toolbar, .context-budget-panel, .project-component, .project-context-preview, .project-home-ambient span",
    );
    projectHomeMotion.add("(prefers-reduced-motion: no-preference)", () => {
        const timeline = window.gsap.timeline({
            defaults: { ease: "power3.out" },
        });
        timeline
            .fromTo(
                projectHomeDialog.querySelectorAll(".project-home-ambient span"),
                { autoAlpha: 0, scale: 0.72 },
                { autoAlpha: 0.28, scale: 1, duration: 0.9, stagger: 0.1 },
                0,
            )
            .fromTo(
                projectHomeDialog.querySelector(".project-home-header"),
                { autoAlpha: 0, y: -14 },
                { autoAlpha: 1, y: 0, duration: 0.34 },
                0.03,
            )
            .fromTo(
                projectHomeDialog.querySelectorAll(".project-home-toolbar, .context-budget-panel"),
                { autoAlpha: 0, y: 12 },
                { autoAlpha: 1, y: 0, duration: 0.38, stagger: 0.06 },
                0.12,
            )
            .fromTo(
                projectHomeDialog.querySelectorAll(".project-component"),
                { autoAlpha: 0, y: 22, scale: 0.985 },
                {
                    autoAlpha: 1,
                    y: 0,
                    scale: 1,
                    duration: 0.46,
                    stagger: 0.065,
                    clearProps: "transform,opacity,visibility",
                },
                0.2,
            )
            .fromTo(
                projectHomeDialog.querySelectorAll(".component-number"),
                { autoAlpha: 0, scale: 0.65, rotation: -16 },
                {
                    autoAlpha: 1,
                    scale: 1,
                    rotation: 0,
                    duration: 0.3,
                    stagger: 0.055,
                    ease: "back.out(1.45)",
                    clearProps: "transform,opacity,visibility",
                },
                0.28,
            )
            .fromTo(
                projectHomeDialog.querySelectorAll(".budget-segment"),
                { autoAlpha: 0, scaleX: 0.15, transformOrigin: "left center" },
                {
                    autoAlpha: 1,
                    scaleX: 1,
                    duration: 0.34,
                    stagger: 0.045,
                    clearProps: "transform,opacity,visibility",
                },
                0.3,
            )
            .fromTo(
                projectHomeDialog.querySelector(".project-context-preview"),
                { autoAlpha: 0, y: 10 },
                {
                    autoAlpha: 1,
                    y: 0,
                    duration: 0.3,
                    clearProps: "transform,opacity,visibility",
                },
                0.46,
            );
        return () => timeline.kill();
    });
    projectHomeMotion.add("(prefers-reduced-motion: reduce)", () => {
        window.gsap.set(targets, { clearProps: "all" });
    });
}

function animateProjectContextPreview() {
    if (!window.gsap || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    window.gsap.fromTo(
        projectContextPreviewOutput,
        { autoAlpha: 0, y: 12 },
        {
            autoAlpha: 1,
            y: 0,
            duration: 0.3,
            ease: "power3.out",
            clearProps: "transform,opacity,visibility",
        },
    );
}

function renderProjectBudget() {
    if (!projectHomeData) return;
    const total = Number.parseInt(projectContextBudget.value, 10)
        || projectHomeData.context_budget.total;
    const quotas = projectBudgetQuotas(total);
    const usage = projectHomeData.context_budget.usage;
    const segments = [
        ["Instructions", "project_instructions", 25],
        ["BRAIN", "brain", 25],
        ["Files", "file_context", 30],
        ["Artifacts", "artifact_history", 20]
    ];
    contextBudgetMeter.replaceChildren();
    segments.forEach(([label, key, percentage]) => {
        const segment = document.createElement("div");
        segment.className = "budget-segment";
        segment.style.flexBasis = `${percentage}%`;
        segment.textContent = `${label} ${percentage}%\n${usage[key].toLocaleString()} / ${quotas[key].toLocaleString()}`;
        segment.title = `${label}: ${usage[key].toLocaleString()} used of ${quotas[key].toLocaleString()} tokens`;
        contextBudgetMeter.appendChild(segment);
    });
    const instructionTokens = estimateInstructionTokens(projectHomeInstructions.value);
    projectHomeInstructionsCount.textContent = `${instructionTokens.toLocaleString()} of ${quotas.project_instructions.toLocaleString()} tokens`;
    projectHomeInstructionsCount.classList.toggle("over-budget", instructionTokens > quotas.project_instructions);
    brainThreshold.max = quotas.brain + Math.max(0, quotas.project_instructions - instructionTokens);
}

function renderProjectAttachmentState() {
    const conversation = currentConversation();
    if (!conversation) {
        projectAttachmentState.textContent = "No active chat. Start one here or attach an existing chat later.";
        attachProjectChatBtn.disabled = true;
        attachProjectChatBtn.textContent = "Attach active chat";
        return;
    }
    attachProjectChatBtn.disabled = false;
    if (conversation.project_id === projectHomeProjectId) {
        projectAttachmentState.textContent = `“${conversation.title || "Current chat"}” is attached. Project context applies to future messages.`;
        attachProjectChatBtn.textContent = "Detach active chat";
    } else {
        const attached = projects.find((item) => item.project_id === conversation.project_id);
        projectAttachmentState.textContent = conversation.project_id
            ? `“${conversation.title || "Current chat"}” is attached to ${attached?.name || "another project"}.`
            : `“${conversation.title || "Current chat"}” is a General chat with no project.`;
        attachProjectChatBtn.textContent = "Attach active chat";
    }
}

function renderProjectFiles(files) {
    projectFilesList.replaceChildren();
    if (!files.length) {
        projectFilesList.appendChild(createProjectEmptyState(
            "No references yet. Upload UTF-8 text or code to make it eligible for request context. Other files remain stored but unindexed."
        ));
        return;
    }
    files.forEach((file) => {
        const record = document.createElement("div");
        record.className = "project-record";
        const copy = document.createElement("div");
        const title = document.createElement("strong");
        title.textContent = file.display_name;
        const meta = document.createElement("small");
        meta.textContent = `${file.attached ? "attached" : "detached"} · ${file.status.replaceAll("_", " ")} · ${formatBytes(file.size_bytes)} · ${file.token_count.toLocaleString()} tokens`;
        copy.appendChild(title);
        copy.appendChild(meta);
        const actions = document.createElement("div");
        actions.className = "project-record-actions";
        actions.appendChild(createRecordButton("Reindex", () => reindexProjectReference(file)));
        actions.appendChild(createRecordButton(
            file.attached ? "Detach" : "Attach",
            () => setProjectReferenceAttached(file, !file.attached)
        ));
        actions.appendChild(createRecordButton("Delete", () => deleteProjectReference(file), "text-btn danger-text"));
        record.appendChild(copy);
        record.appendChild(actions);
        projectFilesList.appendChild(record);
    });
}

function renderProjectArtifacts(artifacts) {
    projectArtifactsList.replaceChildren();
    projectArtifactPreview.hidden = true;
    if (!artifacts.length) {
        projectArtifactsList.appendChild(createProjectEmptyState(
            "No outputs yet. Assistant responses from attached chats appear here automatically."
        ));
        return;
    }
    artifacts.forEach((artifact) => {
        const record = document.createElement("div");
        record.className = "project-record";
        const copy = document.createElement("div");
        const title = document.createElement("strong");
        title.textContent = `${artifact.pinned ? "Pinned · " : ""}${artifact.title}`;
        const meta = document.createElement("small");
        meta.textContent = `${artifact.kind.replaceAll("_", " ")} · ${artifact.token_count.toLocaleString()} tokens`;
        copy.appendChild(title);
        copy.appendChild(meta);
        const actions = document.createElement("div");
        actions.className = "project-record-actions";
        actions.appendChild(createRecordButton("Open", () => openProjectArtifact(artifact.artifact_id)));
        actions.appendChild(createRecordButton(
            artifact.pinned ? "Unpin" : "Pin",
            () => updateProjectArtifact(artifact.artifact_id, { pinned: !artifact.pinned })
        ));
        actions.appendChild(createRecordButton(
            "Archive",
            () => updateProjectArtifact(artifact.artifact_id, { archived: true })
        ));
        actions.appendChild(createRecordButton(
            "Delete",
            () => deleteProjectArtifact(artifact),
            "text-btn danger-text"
        ));
        record.appendChild(copy);
        record.appendChild(actions);
        projectArtifactsList.appendChild(record);
    });
}

async function loadBrainRevisions() {
    if (!projectHomeProjectId) return;
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/brain/revisions`),
            { headers: authHeaders() }
        );
        if (!response.ok) throw new Error(await apiError(response));
        const data = await response.json();
        brainRevisionSelect.replaceChildren();
        data.revisions.forEach((revision) => {
            const option = document.createElement("option");
            option.value = revision.revision;
            option.textContent = `Revision ${revision.revision} · ${revision.reason} · ${revision.token_count} tokens`;
            brainRevisionSelect.appendChild(option);
        });
        const prior = data.revisions.find((item) => item.revision < projectHomeData.components.brain.revision);
        if (prior) brainRevisionSelect.value = prior.revision;
        restoreProjectBrainBtn.disabled = !prior;
    } catch (error) {
        projectBrainStatus.textContent = `Revision load failed: ${error.message}`;
    }
}

function populateProjectHomepage(data) {
    projectHomeData = data;
    projectHomeProjectId = data.project_id;
    projectHomeHeading.textContent = data.project.name;
    projectHomeDescription.textContent = data.project.description
        || "One source of truth for the context added to every attached chat.";
    projectContextBudget.value = data.context_budget.total;
    projectHomeInstructions.value = data.components.project_instructions.content || "";
    renderProjectBudget();
    renderProjectAttachmentState();
    renderProjectFiles(data.components.file_context_uploads || []);
    renderProjectArtifacts(data.components.artifact_history || []);
    const brain = data.components.brain;
    const brainDeleted = Boolean(brain.deleted_at);
    brainPinned.value = brainDeleted ? "" : (brain.pinned_text || "");
    brainActive.value = brainDeleted ? "" : (brain.active_text || "");
    brainRecent.value = brainDeleted ? "" : (brain.recent_text || "");
    brainThreshold.value = brain.compact_threshold;
    [brainPinned, brainActive, brainRecent, brainThreshold].forEach((field) => {
        field.disabled = brainDeleted;
    });
    saveProjectBrainBtn.disabled = brainDeleted;
    compactProjectBrainBtn.disabled = brainDeleted;
    deleteProjectBrainBtn.disabled = brainDeleted;
    brainRevision.textContent = `Revision ${brain.revision} · ${brain.token_count.toLocaleString()} tokens`;
    projectBrainStatus.textContent = brainDeleted
        ? "Soft deleted. Restore a prior revision to reactivate it."
        : (brain.should_compact ? "Compaction threshold reached." : "Ready");
    loadBrainRevisions();
}

async function refreshProjectHomepage(message = "") {
    if (!projectHomeProjectId) return;
    if (message) projectHomeStatus.textContent = message;
    const response = await fetch(
        routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/homepage`),
        { headers: authHeaders() }
    );
    if (!response.ok) throw new Error(await apiError(response));
    populateProjectHomepage(await response.json());
}

async function openProjectHomepage() {
    if (!selectedProjectId) {
        alert("Select a project before opening its homepage.");
        projectSelect?.focus();
        return;
    }
    projectHomeProjectId = selectedProjectId;
    projectHomeStatus.textContent = "Loading project context...";
    if (!projectHomeDialog.open) projectHomeDialog.showModal();
    try {
        await refreshProjectHomepage();
        projectHomeStatus.textContent = "All four project components are loaded.";
        animateProjectHomepageIn();
        requestAnimationFrame(() => projectHomeInstructions.focus());
    } catch (error) {
        console.error("Failed to load Project Homepage:", error);
        projectHomeStatus.textContent = `Load failed: ${error.message}`;
    }
}

async function saveProjectInstructions() {
    if (!projectHomeProjectId) return;
    saveProjectInstructionsBtn.disabled = true;
    projectHomeStatus.textContent = "Saving project instructions and budget...";
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}`),
            {
                method: "PUT",
                headers: authHeaders({ "Content-Type": "application/json" }),
                body: JSON.stringify({
                    system_prompt: projectHomeInstructions.value,
                    context_budget_tokens: Number.parseInt(projectContextBudget.value, 10)
                })
            }
        );
        if (!response.ok) throw new Error(await apiError(response));
        await loadProjects();
        await refreshProjectHomepage();
        projectHomeStatus.textContent = "Instructions saved. Attached chats use them on the next message.";
    } catch (error) {
        projectHomeStatus.textContent = `Save failed: ${error.message}`;
    } finally {
        saveProjectInstructionsBtn.disabled = false;
    }
}

async function uploadProjectReference(event) {
    event.preventDefault();
    const file = projectFileInput.files?.[0];
    if (!file || !projectHomeProjectId) {
        projectFilesStatus.textContent = "Choose a reference first.";
        return;
    }
    projectFilesStatus.textContent = `Uploading ${file.name}...`;
    const form = new FormData();
    form.append("file", file);
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/files`),
            { method: "POST", headers: authHeaders(), body: form }
        );
        if (!response.ok) throw new Error(await apiError(response));
        const uploaded = await response.json();
        projectFileInput.value = "";
        const label = projectFileForm.querySelector(".project-file-drop span");
        if (label) label.textContent = "Choose a local reference";
        projectFilesStatus.textContent = uploaded.status === "indexed"
            ? `${uploaded.display_name} indexed for project context.`
            : `${uploaded.display_name} stored with status: ${uploaded.status.replaceAll("_", " ")}.`;
        await refreshProjectHomepage();
    } catch (error) {
        projectFilesStatus.textContent = `Upload failed: ${error.message}`;
    }
}

async function deleteProjectReference(file) {
    if (!confirm(`Delete project reference “${file.display_name}”?`)) return;
    projectFilesStatus.textContent = `Deleting ${file.display_name}...`;
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/files/${encodeURIComponent(file.file_id)}`),
            { method: "DELETE", headers: authHeaders() }
        );
        if (!response.ok) throw new Error(await apiError(response));
        await refreshProjectHomepage();
        projectFilesStatus.textContent = `${file.display_name} deleted.`;
    } catch (error) {
        projectFilesStatus.textContent = `Delete failed: ${error.message}`;
    }
}

async function reindexProjectReference(file) {
    projectFilesStatus.textContent = `Reindexing ${file.display_name}...`;
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/files/${encodeURIComponent(file.file_id)}/reindex`),
            { method: "POST", headers: authHeaders() }
        );
        if (!response.ok) throw new Error(await apiError(response));
        const updated = await response.json();
        await refreshProjectHomepage();
        projectFilesStatus.textContent = `${updated.display_name} reindexed with status: ${updated.status.replaceAll("_", " ")}.`;
    } catch (error) {
        projectFilesStatus.textContent = `Reindex failed: ${error.message}`;
    }
}

async function setProjectReferenceAttached(file, attached) {
    projectFilesStatus.textContent = `${attached ? "Attaching" : "Detaching"} ${file.display_name}...`;
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/files/${encodeURIComponent(file.file_id)}`),
            {
                method: "PUT",
                headers: authHeaders({ "Content-Type": "application/json" }),
                body: JSON.stringify({ attached })
            }
        );
        if (!response.ok) throw new Error(await apiError(response));
        await refreshProjectHomepage();
        projectFilesStatus.textContent = `${file.display_name} ${attached ? "attached" : "detached"}.`;
    } catch (error) {
        projectFilesStatus.textContent = `Update failed: ${error.message}`;
    }
}

async function openProjectArtifact(artifactId) {
    projectArtifactsStatus.textContent = "Loading artifact...";
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/artifacts/${encodeURIComponent(artifactId)}`),
            { headers: authHeaders() }
        );
        if (!response.ok) throw new Error(await apiError(response));
        const artifact = await response.json();
        projectArtifactPreview.textContent = artifact.body || "";
        projectArtifactPreview.hidden = false;
        projectArtifactsStatus.textContent = `Viewing ${artifact.title}.`;
    } catch (error) {
        projectArtifactsStatus.textContent = `Open failed: ${error.message}`;
    }
}

async function updateProjectArtifact(artifactId, updates) {
    projectArtifactsStatus.textContent = "Updating artifact...";
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/artifacts/${encodeURIComponent(artifactId)}`),
            {
                method: "PUT",
                headers: authHeaders({ "Content-Type": "application/json" }),
                body: JSON.stringify(updates)
            }
        );
        if (!response.ok) throw new Error(await apiError(response));
        await refreshProjectHomepage();
        projectArtifactsStatus.textContent = "Artifact updated.";
    } catch (error) {
        projectArtifactsStatus.textContent = `Update failed: ${error.message}`;
    }
}

async function deleteProjectArtifact(artifact) {
    if (!confirm(`Delete artifact “${artifact.title}”?`)) return;
    projectArtifactsStatus.textContent = "Deleting artifact...";
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}`),
            { method: "DELETE", headers: authHeaders() }
        );
        if (!response.ok) throw new Error(await apiError(response));
        await refreshProjectHomepage();
        projectArtifactsStatus.textContent = "Artifact deleted.";
    } catch (error) {
        projectArtifactsStatus.textContent = `Delete failed: ${error.message}`;
    }
}

async function saveProjectBrain() {
    if (!projectHomeData || !projectHomeProjectId) return;
    saveProjectBrainBtn.disabled = true;
    projectBrainStatus.textContent = "Saving BRAIN...";
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/brain`),
            {
                method: "PUT",
                headers: authHeaders({ "Content-Type": "application/json" }),
                body: JSON.stringify({
                    pinned_text: brainPinned.value,
                    active_text: brainActive.value,
                    recent_text: brainRecent.value,
                    compact_threshold: Number.parseInt(brainThreshold.value, 10),
                    expected_revision: projectHomeData.components.brain.revision
                })
            }
        );
        if (!response.ok) throw new Error(await apiError(response));
        const brain = await response.json();
        await refreshProjectHomepage();
        projectBrainStatus.textContent = brain.compaction_queued
            ? "Saved. Threshold compaction queued in FastAPI."
            : "BRAIN saved.";
    } catch (error) {
        projectBrainStatus.textContent = `Save failed: ${error.message}`;
    } finally {
        saveProjectBrainBtn.disabled = false;
    }
}

async function compactProjectBrain() {
    compactProjectBrainBtn.disabled = true;
    projectBrainStatus.textContent = "Creating a recoverable compaction revision...";
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/brain/compact`),
            { method: "POST", headers: authHeaders() }
        );
        if (!response.ok) throw new Error(await apiError(response));
        await refreshProjectHomepage();
        projectBrainStatus.textContent = "Compacted. Pinned and active tiers were preserved verbatim.";
    } catch (error) {
        projectBrainStatus.textContent = `Compaction failed: ${error.message}`;
    } finally {
        compactProjectBrainBtn.disabled = false;
    }
}

async function restoreProjectBrain() {
    const revision = Number.parseInt(brainRevisionSelect.value, 10);
    if (!revision || !confirm(`Restore BRAIN revision ${revision}? The current revision remains recoverable.`)) return;
    projectBrainStatus.textContent = `Restoring revision ${revision}...`;
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/brain/revisions/${revision}/restore`),
            { method: "POST", headers: authHeaders() }
        );
        if (!response.ok) throw new Error(await apiError(response));
        await refreshProjectHomepage();
        projectBrainStatus.textContent = `Revision ${revision} restored as a new revision.`;
    } catch (error) {
        projectBrainStatus.textContent = `Restore failed: ${error.message}`;
    }
}

async function deleteProjectBrain() {
    if (!confirm("Soft delete active BRAIN content? Every revision remains available for restore.")) return;
    projectBrainStatus.textContent = "Soft deleting BRAIN...";
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/brain`),
            { method: "DELETE", headers: authHeaders() }
        );
        if (!response.ok) throw new Error(await apiError(response));
        await refreshProjectHomepage();
        projectBrainStatus.textContent = "BRAIN soft deleted. Restore a revision to reactivate it.";
    } catch (error) {
        projectBrainStatus.textContent = `Delete failed: ${error.message}`;
    }
}

async function previewProjectRequestContext() {
    if (!projectHomeProjectId) return;
    previewProjectContextBtn.disabled = true;
    projectContextPreviewStatus.textContent = "Assembling exact request context...";
    projectContextPreviewOutput.hidden = true;
    try {
        let attachedFileText = "";
        const inlineFile = fileInput?.files?.[0];
        if (inlineFile) {
            if (inlineFile.size > 1024 * 1024) {
                throw new Error("Inline composer file exceeds the 1MB send limit");
            }
            attachedFileText = `\n\n[Attached file: ${inlineFile.name}]\n${await inlineFile.text()}`;
        }
        const displayedQuery = window.DavePrompt.buildDisplayedPrompt(
            promptInput.value.trim(),
            attachedFileText,
            Boolean(supportFlag?.checked),
        );
        const query = displayedQuery || (state.pendingImages.length ? "[image]" : "");
        const conversation = currentConversation();
        const conversationId = conversation?.project_id === projectHomeProjectId
            ? state.sessionId
            : null;
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectHomeProjectId)}/context-preview`),
            {
                method: "POST",
                headers: authHeaders({ "Content-Type": "application/json" }),
                body: JSON.stringify({
                    query,
                    conversation_id: conversationId,
                    model: modelSelect.value || null,
                    max_tokens: 2048,
                }),
            },
        );
        if (!response.ok) throw new Error(await apiError(response));
        const data = await response.json();
        const budget = data.budget || {};
        const limits = budget.available_limits || {};
        const usage = budget.usage || {};
        const budgetLines = [
            `Model context window: ${(budget.model_context_window || 0).toLocaleString()} tokens`,
            `Project request budget: ${(budget.request_total || 0).toLocaleString()} tokens`,
            `Sequential caps after prior unused capacity: instructions ${Number(limits.project_instructions || 0).toLocaleString()}, BRAIN ${Number(limits.brain || 0).toLocaleString()}, files ${Number(limits.file_context || 0).toLocaleString()}, artifacts ${Number(limits.artifact_history || 0).toLocaleString()}`,
            `Included usage: instructions ${Number(usage.project_instructions || 0).toLocaleString()}, BRAIN ${Number(usage.brain || 0).toLocaleString()}, files ${Number(usage.file_context || 0).toLocaleString()}, artifacts ${Number(usage.artifact_history || 0).toLocaleString()}`,
        ];
        const messageLines = data.messages.map((message, index) => {
            const content = typeof message.content === "string"
                ? message.content
                : JSON.stringify(message.content, null, 2);
            return `[${index + 1}] ${String(message.role || "unknown").toUpperCase()}\n${content}`;
        });
        projectContextPreviewOutput.textContent = `${budgetLines.join("\n")}\n\n${messageLines.join("\n\n")}`;
        projectContextPreviewOutput.hidden = false;
        animateProjectContextPreview();
        projectContextPreviewStatus.textContent = `Preview assembled ${data.messages.length} messages. No model request was sent.`;
    } catch (error) {
        projectContextPreviewStatus.textContent = `Preview failed: ${error.message}`;
    } finally {
        previewProjectContextBtn.disabled = false;
    }
}

async function toggleActiveChatProject() {
    const conversation = currentConversation();
    if (!conversation || !state.sessionId) return;
    const nextProjectId = conversation.project_id === projectHomeProjectId
        ? null
        : projectHomeProjectId;
    projectAttachmentState.textContent = nextProjectId ? "Attaching active chat..." : "Detaching active chat...";
    try {
        const response = await fetch(
            routerEndpoint(`/conversations/${encodeURIComponent(state.sessionId)}/project`),
            {
                method: "PUT",
                headers: authHeaders({ "Content-Type": "application/json" }),
                body: JSON.stringify({ project_id: nextProjectId })
            }
        );
        if (!response.ok) throw new Error(await apiError(response));
        const data = await response.json();
        conversation.project_id = data.project_id;
        conversation.system_prompt = data.system_prompt;
        renderProjectAttachmentState();
        renderConversationList();
        updateContextStrip();
    } catch (error) {
        projectAttachmentState.textContent = `Attachment change failed: ${error.message}`;
    }
}

async function startProjectChat() {
    selectedProjectId = projectHomeProjectId;
    projectSelect.value = selectedProjectId;
    localStorage.setItem("dave_project_id", selectedProjectId);
    const conversationId = await createConversationFromTemplate(templateSelect?.value || "general");
    if (conversationId) {
        projectHomeDialog.close();
        updateContextStrip();
    }
}

function estimateInstructionTokens(value) {
    return Math.max(0, Math.ceil(String(value || "").length / 4));
}

function formatInstructionCount(value) {
    const text = String(value || "");
    return `${text.length.toLocaleString()} characters, about ${estimateInstructionTokens(text).toLocaleString()} tokens`;
}

function composeInstructionPreview() {
    if (instructionPreviewMode === "replace" && sessionInstructions.value.trim()) {
        return sessionInstructions.value.trim();
    }
    const parts = [globalInstructions.value.trim()];
    if (!projectInstructions.disabled && projectInstructions.value.trim()) {
        parts.push(`PROJECT INSTRUCTIONS\n${projectInstructions.value.trim()}`);
    }
    if (sessionInstructions.value.trim()) {
        parts.push(`SESSION OVERRIDE\n${sessionInstructions.value.trim()}`);
    }
    return parts.filter(Boolean).join("\n\n");
}

function updateInstructionCounts() {
    globalInstructionsCount.textContent = formatInstructionCount(globalInstructions.value);
    projectInstructionsCount.textContent = formatInstructionCount(projectInstructions.value);
    sessionInstructionsCount.textContent = formatInstructionCount(sessionInstructions.value);
    effectiveInstructions.value = composeInstructionPreview();
    effectiveInstructionsCount.textContent = formatInstructionCount(effectiveInstructions.value);
}

function populateInstructionDialog(data) {
    const layers = data.layers || {};
    instructionPreviewMode = data.mode || "layered";
    globalInstructions.value = layers.global_default?.content || "";
    projectInstructions.value = layers.project_instructions?.content || "";
    projectInstructions.disabled = !layers.project_instructions?.editable;
    instructionProjectName.textContent = data.project_name || "No project attached";
    sessionInstructions.value = layers.session_override?.content || "";
    effectiveInstructions.value = data.effective || "";
    if (state.conversations[data.conversation_id]) {
        state.conversations[data.conversation_id].system_prompt = data.effective || "";
        state.conversations[data.conversation_id].session_override = sessionInstructions.value;
    }
    updateInstructionCounts();
}

async function ensureActiveConversation() {
    if (state.sessionId && state.conversations[state.sessionId]) return state.sessionId;
    return createNewConversation();
}

async function openInstructionsDialog() {
    const conversationId = await ensureActiveConversation();
    if (!conversationId) {
        alert("Could not create a conversation for instruction editing.");
        return;
    }
    instructionsStatus.textContent = "Loading current instructions...";
    try {
        const response = await fetch(
            routerEndpoint(`/conversations/${encodeURIComponent(conversationId)}/instructions`),
            { headers: authHeaders() },
        );
        if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
        const data = await response.json();
        populateInstructionDialog(data);
        instructionsStatus.textContent = data.mode === "replace"
            ? "Legacy snapshot mode is active. Saving or reverting switches this session to visible layered precedence."
            : "Edits apply to the next message without restarting DaveLLM.";
        instructionsDialog.showModal();
        requestAnimationFrame(() => sessionInstructions.focus());
    } catch (error) {
        console.error("Failed to load instructions:", error);
        instructionsStatus.textContent = `Could not load instructions: ${error.message}`;
        alert("Could not load active instructions.");
    }
}

async function saveInstructionLayers() {
    if (!state.sessionId) return;
    saveInstructionsBtn.disabled = true;
    instructionsStatus.textContent = "Saving and applying...";
    const payload = {
        global_default: globalInstructions.value,
        session_override: sessionInstructions.value,
    };
    if (!projectInstructions.disabled) {
        payload.project_instructions = projectInstructions.value;
    }
    try {
        const response = await fetch(
            routerEndpoint(`/conversations/${encodeURIComponent(state.sessionId)}/instructions`),
            {
                method: "PUT",
                headers: authHeaders({ "Content-Type": "application/json" }),
                body: JSON.stringify(payload),
            },
        );
        if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
        const data = await response.json();
        populateInstructionDialog(data);
        await loadProjects();
        instructionsStatus.textContent = "Saved. These exact instructions apply to the next message.";
    } catch (error) {
        console.error("Failed to save instructions:", error);
        instructionsStatus.textContent = `Save failed: ${error.message}`;
    } finally {
        saveInstructionsBtn.disabled = false;
    }
}

async function revertSessionInstructions() {
    if (!state.sessionId) return;
    instructionsStatus.textContent = "Reverting session override...";
    try {
        const response = await fetch(
            routerEndpoint(`/conversations/${encodeURIComponent(state.sessionId)}/instructions/session`),
            { method: "DELETE", headers: authHeaders() },
        );
        if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
        populateInstructionDialog(await response.json());
        instructionsStatus.textContent = "Session override reverted to inherited defaults.";
    } catch (error) {
        instructionsStatus.textContent = `Revert failed: ${error.message}`;
    }
}

async function resetGlobalInstructions() {
    instructionsStatus.textContent = "Restoring source-controlled global default...";
    try {
        const response = await fetch(routerEndpoint("/instructions/global"), {
            method: "DELETE",
            headers: authHeaders(),
        });
        if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
        const data = await response.json();
        globalInstructions.value = data.content || "";
        updateInstructionCounts();
        instructionsStatus.textContent = instructionPreviewMode === "replace"
            ? "Global default restored. This legacy session keeps its exact snapshot until you save or revert it."
            : "Global default restored. It applies to the next message.";
    } catch (error) {
        instructionsStatus.textContent = `Global reset failed: ${error.message}`;
    }
}

function activeNotepadProjectId() {
    return currentConversation()?.project_id || selectedProjectId || "";
}

async function loadProjectNotepad(projectId) {
    notepadLoading = true;
    notepadStatus.textContent = "Loading...";
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectId)}/notepad`),
            { headers: authHeaders() },
        );
        if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
        const data = await response.json();
        notepadInput.value = data.content || "";
        notepadLoadedProjectId = projectId;
        const project = projects.find((item) => item.project_id === projectId);
        notepadProjectName.textContent = project?.name || "Current project";
        notepadStatus.textContent = data.updated_at ? "Saved" : "Ready. Autosaves as you type.";
    } catch (error) {
        console.error("Failed to load notepad:", error);
        notepadStatus.textContent = `Load failed: ${error.message}`;
    } finally {
        notepadLoading = false;
    }
}

async function openProjectNotepad() {
    const projectId = activeNotepadProjectId();
    if (!projectId) {
        alert("Select a project before opening the project notepad.");
        setMobileTab("history");
        projectSelect?.focus();
        return false;
    }
    notepadPanel.hidden = false;
    notepadToggle.setAttribute("aria-expanded", "true");
    if (notepadLoadedProjectId !== projectId) await loadProjectNotepad(projectId);
    notepadInput.focus();
    return true;
}

function closeProjectNotepad() {
    notepadPanel.hidden = true;
    notepadToggle.setAttribute("aria-expanded", "false");
    notepadToggle.focus();
}

async function saveProjectNotepad() {
    const projectId = notepadLoadedProjectId;
    if (!projectId) return;
    notepadStatus.textContent = "Saving...";
    try {
        const response = await fetch(
            routerEndpoint(`/projects/${encodeURIComponent(projectId)}/notepad`),
            {
                method: "PUT",
                headers: authHeaders({ "Content-Type": "application/json" }),
                body: JSON.stringify({ content: notepadInput.value }),
            },
        );
        if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
        const data = await response.json();
        const project = projects.find((item) => item.project_id === projectId);
        if (project) project.notepad = data.content;
        notepadStatus.textContent = "Saved";
    } catch (error) {
        console.error("Failed to save notepad:", error);
        notepadStatus.textContent = `Save failed: ${error.message}`;
    }
}

async function flushProjectNotepadSave() {
    if (!notepadSaveTimer) return;
    clearTimeout(notepadSaveTimer);
    notepadSaveTimer = null;
    await saveProjectNotepad();
}

function scheduleProjectNotepadSave() {
    if (notepadLoading || !notepadLoadedProjectId) return;
    notepadStatus.textContent = "Unsaved changes";
    clearTimeout(notepadSaveTimer);
    notepadSaveTimer = setTimeout(() => {
        notepadSaveTimer = null;
        saveProjectNotepad();
    }, 500);
}

async function appendSelectionToNotepad(messageElement) {
    const selection = window.getSelection();
    const selectedText = selection?.toString().trim() || "";
    const range = selection && selection.rangeCount ? selection.getRangeAt(0) : null;
    if (!selectedText || !range || !messageElement.contains(range.commonAncestorContainer)) {
        alert("Select text inside this message, then choose Add to notepad.");
        return;
    }
    if (!await openProjectNotepad()) return;
    const separator = notepadInput.value && !notepadInput.value.endsWith("\n") ? "\n" : "";
    notepadInput.value += `${separator}${selectedText}\n`;
    scheduleProjectNotepadSave();
    notepadInput.focus();
    notepadInput.setSelectionRange(notepadInput.value.length, notepadInput.value.length);
}

async function sendNotepadToChat() {
    const text = notepadInput.value.trim();
    if (!text) {
        alert("The project notepad is empty.");
        return;
    }
    promptInput.value = text;
    resizeComposer();
    persistSessionDraft();
    await sendMessage();
}

function rawMessageText(message) {
    if (typeof message.content === "string") return message.content;
    if (!Array.isArray(message.content)) return String(message.content || "");
    return message.content.map((part) => {
        if (part?.type === "text") return part.text || "";
        if (part?.type === "image_url") return "[image]";
        return "";
    }).filter(Boolean).join("\n\n");
}

async function copyRawMessage(message, button, statusElement) {
    const text = rawMessageText(message);
    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(text);
        } else {
            const fallback = document.createElement("textarea");
            fallback.value = text;
            fallback.setAttribute("readonly", "");
            fallback.style.position = "fixed";
            fallback.style.opacity = "0";
            document.body.appendChild(fallback);
            fallback.select();
            const copied = document.execCommand("copy");
            fallback.remove();
            if (!copied) throw new Error("Clipboard unavailable");
        }
        const original = button.textContent;
        button.textContent = "Copied";
        button.setAttribute("aria-label", `${message.role} message copied`);
        statusElement.textContent = `${message.role} message copied`;
        setTimeout(() => {
            button.textContent = original;
            button.setAttribute("aria-label", `Copy ${message.role} message raw markdown`);
            statusElement.textContent = "";
        }, 2000);
    } catch (error) {
        console.error("Copy failed:", error);
        statusElement.textContent = "Copy failed";
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
            throw new Error(await apiError(res));
        }
        const data = await res.json();
        const transcript = data.text || "";
        if (transcript) {
            promptInput.value = promptInput.value ? `${promptInput.value}\n${transcript}` : transcript;
            promptInput.focus();
            resizeComposer();
            persistSessionDraft();
            renderPredictions();
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

    element.replaceChildren();
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
        renderNodeSelect(nodes);
        await renderNodes(nodes);
        updateContextStrip();
        renderPredictions();

    } catch (err) {
        console.error("Failed to fetch nodes:", err);
        state.nodes = [];
        state.selectedNode = null;
        state.modelMeta = {};
        state.nodeStatus = {};

        const error = document.createElement("div");
        error.className = "error-message";
        error.style.color = "#ff6b6b";
        error.style.padding = "10px";
        error.style.border = "1px solid #ff6b6b";
        error.style.borderRadius = "4px";
        error.textContent = `⚠️ Connection failed: Is the backend running at ${ROUTER_BASE}?`;
        nodesContainer.replaceChildren(error);
        replaceSelectOptions(nodeSelect, "No nodes available");
        replaceSelectOptions(modelSelect, "No models available");
    }
}

async function renderNodes(nodes) {
    nodesContainer.replaceChildren();
    
    if (nodes.length === 0) {
        state.nodeStatus = {};
        const info = document.createElement("div");
        info.className = "info-message";
        info.textContent = "No nodes registered yet.";
        nodesContainer.appendChild(info);
        return;
    }
    
    await fetchNodeStatus(nodes);
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
        state.nodeStatus = statusMap;
        
        nodesContainer.replaceChildren();
        
        nodes.forEach((n) => {
            const status = statusMap[n.id] || { status: "offline", latency: null };
            
            const div = document.createElement("div");
            div.className = "node-card";
            if (n.id === state.selectedNode) div.classList.add("active");
            
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
            statusLabel.className = `node-status-label ${status.status === "online" ? "node-status-online" : "node-status-offline"}`;
            statusLabel.textContent = status.status === "online" ? "Online" : "Offline";
            
            header.appendChild(statusDot);
            header.appendChild(nameSpan);
            header.appendChild(statusLabel);
            
            const urlSpan = document.createElement("div");
            urlSpan.className = "node-url";
            urlSpan.textContent = n.url;
            
            div.appendChild(header);
            div.appendChild(urlSpan);
            
            if (status.latency !== null) {
                const latencySpan = document.createElement("div");
                latencySpan.className = "node-latency";
                latencySpan.textContent = `⚡ ${status.latency}ms latency`;
                div.appendChild(latencySpan);
            }
            
            nodesContainer.appendChild(div);
        });
        
    } catch (err) {
        console.error("Failed to fetch node status:", err);
        state.nodeStatus = Object.fromEntries(nodes.map((node) => [node.id, { status: "unknown", latency: null }]));
        nodesContainer.replaceChildren();
        nodes.forEach((n) => {
            const div = document.createElement("div");
            div.className = "node-card";
            if (n.id === state.selectedNode) div.classList.add("active");
            const name = document.createElement("strong");
            name.textContent = n.name;
            const url = document.createElement("div");
            url.className = "node-url";
            url.textContent = n.url;
            div.appendChild(name);
            div.appendChild(url);
            nodesContainer.appendChild(div);
        });
    }
}

function renderNodeSelect(nodes) {
    nodeSelect.replaceChildren();

    if (nodes.length === 0) {
        state.selectedNode = null;
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

    if (!nodes.some((node) => node.id === state.selectedNode)) {
        state.selectedNode = nodes[0].id;
    }
    nodeSelect.value = state.selectedNode;
}

async function loadModelsFromNode(preferredModelId = null) {
    if (!state.selectedNode || !state.nodes.some((node) => node.id === state.selectedNode)) {
        alert("Please select a node first");
        return;
    }

    const previousModel = preferredModelId || modelSelect.value;
    replaceSelectOptions(modelSelect, "Loading models...");
    modelSelect.disabled = true;

    try {
        const res = await fetch(routerEndpoint(`/nodes/${state.selectedNode}/models`), {
            headers: authHeaders()
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const data = await res.json();
        const models = data.models || [];
        const nodeError = data.error || null;
        const normalizedModels = models
            .map((m) => normalizeModelMeta(m))
            .filter(Boolean);

        modelSelect.replaceChildren();
        state.modelMeta = {};

        if (normalizedModels.length === 0) {
            const opt = document.createElement("option");
            opt.value = "";
            // An unreachable node and a node with nothing pulled both yield an
            // empty list; say which one happened.
            opt.textContent = nodeError ? "Node unreachable" : "No models pulled on node";
            opt.title = nodeError || "";
            modelSelect.appendChild(opt);
            if (nodeError) console.error(`Node ${state.selectedNode}: ${nodeError}`);
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

            modelSelect.value = state.modelMeta[previousModel]
                ? previousModel
                : normalizedModels[0].id;
            if (preferredModelId && !state.modelMeta[preferredModelId]) {
                state.selectionFallback = true;
            }
        }

        console.log(`✅ Loaded ${normalizedModels.length} models from ${state.selectedNode}`);
        modelSelect.disabled = false;
        updateImageSupportNotice();
        updateContextStrip();
        renderPredictions();
        return modelSelect.value;
    } catch (err) {
        console.error("Failed to load models:", err);
        state.modelMeta = {};
        replaceSelectOptions(modelSelect, "Error loading models");
        modelSelect.disabled = false;
        updateContextStrip();
        renderPredictions();
        return null;
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
        const header = document.createElement("div");
        header.className = "memory-header";
        header.textContent = "💾 Relevant memories loaded (used for routing/context)";
        memoryBox.replaceChildren(header);

        memories.forEach((m) => {
            const item = document.createElement("div");
            item.className = "memory-item";
            
            const role = m.role === "user" ? "👤 You" : "🤖 Assistant";
            const preview = m.content.slice(0, 60) + (m.content.length > 60 ? "..." : "");
            const similarity = (m.similarity * 100).toFixed(0);
            
            const roleLabel = document.createElement("strong");
            roleLabel.textContent = role;
            const match = document.createTextNode(` (${similarity}% match)`);
            const lineBreak = document.createElement("br");
            const previewLabel = document.createElement("span");
            previewLabel.style.color = "#9ba4b5";
            previewLabel.textContent = `"${preview}"`;
            item.appendChild(roleLabel);
            item.appendChild(match);
            item.appendChild(lineBreak);
            item.appendChild(previewLabel);
            memoryBox.appendChild(item);
        });

    } catch (err) {
        console.error("Failed to fetch memories:", err);
    }
}

// ---------------------------------------------
// MESSAGE FLOW
// ---------------------------------------------
async function sendMessage() {
    const prompt = promptInput.value.trim();
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
    const basePrompt = `${prompt}${attachedFileText}`;
    if (!basePrompt.trim() && state.pendingImages.length === 0) return;
    const effectivePrompt = window.DavePrompt.buildDisplayedPrompt(
        prompt,
        attachedFileText,
        Boolean(supportFlag && supportFlag.checked),
    );

    const pendingConversation = currentConversation();
    const selectedTemplate = templateSelect?.value || "general";
    if (
        selectedTemplate !== "general"
        && (!pendingConversation || (pendingConversation.messages || []).length === 0)
        && !pendingConversation?.system_prompt
    ) {
        const replaceableId = state.sessionId;
        const createdId = await createConversationFromTemplate(selectedTemplate);
        if (
            createdId
            && replaceableId
            && replaceableId !== createdId
            && state.conversations[replaceableId]
            && (state.conversations[replaceableId].messages || []).length === 0
        ) {
            delete state.conversations[replaceableId];
            renderConversationList();
        }
    } else if (!state.sessionId || !state.conversations[state.sessionId]) {
        console.log("No active conversation, creating a new General conversation");
        await createNewConversation();
    }

    const convo = state.conversations[state.sessionId];
    if (!convo) {
        console.error("Failed to get conversation, aborting send");
        return;
    }

    const selectedNodeExists = state.nodes.some((node) => node.id === state.selectedNode);
    const selectedModelExists = Boolean(state.modelMeta[modelSelect.value]);
    if (!selectedNodeExists || !selectedModelExists) {
        const message = "Select a node and one of that node's loaded models before sending.";
        if (routeStatus) routeStatus.textContent = message;
        alert(message);
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

    if (routeStatus) routeStatus.textContent = `Using manual selection: ${modelSelect.value}`;

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

    anticipationRecord = window.DaveAnticipation.incrementUsage(
        anticipationRecord,
        "templateCounts",
        templateSelect?.value || "general"
    );
    if (supportFlag?.checked && selectedProjectId) {
        anticipationRecord = window.DaveAnticipation.incrementUsage(
            anticipationRecord,
            "supportCountsByProject",
            selectedProjectId
        );
    }
    writeAnticipationRecord(anticipationRecord);
    updateAnticipationPreferences();

    promptInput.value = "";
    promptInput.style.height = "auto";
    sessionStorage.removeItem(DRAFT_SESSION_KEY);
    state.lastPredictionUndo = null;
    undoPredictionBtn?.classList.add("hidden");
    
    const now = Date.now();

    if (effectivePrompt) {
        convo.messages.push({
            role: "user",
            content: effectivePrompt,
            timestamp: now
        });
        state.stats.totalMessages += 1;
        state.stats.totalTokens += Math.max(1, effectivePrompt.length / 4);
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
    
    if (effectivePrompt) showRelevantMemories(effectivePrompt);

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
                prompt: effectivePrompt,
                max_tokens: 2048,
                temperature: 0.7,
                model: modelSelect.value || undefined,
                node_id: state.selectedNode || undefined,
                project_id: convo.project_id ?? undefined,
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
            const detail = await res.text();
            throw new Error(detail || `Server error: ${res.status}`);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        
        streamLoop: while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            
            buffer += decoder.decode(value, { stream: true });

            let newlineIndex;
            while ((newlineIndex = buffer.indexOf("\n")) !== -1) {
                const line = buffer.slice(0, newlineIndex).trimEnd();
                buffer = buffer.slice(newlineIndex + 1);

                if (!line.startsWith("data: ")) continue;

                let data;
                try {
                    data = JSON.parse(line.slice(6));
                } catch (error) {
                    console.warn("Ignoring malformed SSE event:", error);
                    continue;
                }
                if (data.error) throw new Error(data.error);

                if (data.token && !data.done) {
                    streamingMsg.content += data.token;
                    renderMessages();
                }

                if (data.done) {
                    streamingMsg.isStreaming = false;
                    if (convo.title === "New Conversation") {
                        await loadConversationHistory(state.sessionId);
                        renderConversationList();
                    }
                    renderMessages();
                    break streamLoop;
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
        renderPredictions();
    }
}

function renderEmptyChatState() {
    const empty = document.createElement("section");
    empty.className = "empty-chat";

    const heading = document.createElement("h3");
    heading.textContent = state.sessionId ? "Start this conversation" : "What would you like to do?";
    const detail = document.createElement("p");
    detail.textContent = "Choose a starting point or type directly below. Nothing is submitted automatically.";
    const actions = document.createElement("div");
    actions.className = "empty-chat-actions";

    const addAction = (label, handler, primary = false) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        if (primary) button.className = "primary";
        button.addEventListener("click", handler);
        actions.appendChild(button);
    };

    if (state.lastSessionId && state.lastSessionId !== state.sessionId && state.conversations[state.lastSessionId]) {
        const last = state.conversations[state.lastSessionId];
        addAction(`Continue ${last.title}`, () => switchConversation(state.lastSessionId), true);
    }
    if (!state.sessionId) addAction("Start General", createNewConversation, true);
    addAction("Code Review", () => createConversationFromTemplate("code_review"));
    addAction("Brainstorm", () => createConversationFromTemplate("brainstorm"));

    const project = projects.find((item) => item.project_id === selectedProjectId);
    if (!state.sessionId && project) {
        addAction(`Start in ${project.name}`, createNewConversation);
    }

    empty.appendChild(heading);
    empty.appendChild(detail);
    empty.appendChild(actions);
    responseBox.appendChild(empty);
}

function renderMessages() {
    const convo = state.conversations[state.sessionId];

    responseBox.replaceChildren();

    if (!convo || !Array.isArray(convo.messages) || convo.messages.length === 0) {
        renderEmptyChatState();
        updateContextStrip();
        if (!state.streaming) renderPredictions();
        updateScrollBottomButton();
        return;
    }

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

        const header = document.createElement("div");
        header.className = "message-header";

        const roleLabel = document.createElement("strong");
        roleLabel.textContent = m.role.toUpperCase() + ": ";
        header.appendChild(roleLabel);

        const messageActions = document.createElement("div");
        messageActions.className = "message-actions";
        const actionStatus = document.createElement("span");
        actionStatus.className = "visually-hidden";
        actionStatus.setAttribute("role", "status");
        actionStatus.setAttribute("aria-live", "polite");

        if (["user", "assistant"].includes(m.role) && !m.isStreaming) {
            const copyButton = document.createElement("button");
            copyButton.type = "button";
            copyButton.className = "message-action";
            copyButton.textContent = "Copy";
            copyButton.setAttribute("aria-label", `Copy ${m.role} message raw markdown`);
            copyButton.addEventListener("click", () => copyRawMessage(m, copyButton, actionStatus));
            messageActions.appendChild(copyButton);

            const noteButton = document.createElement("button");
            noteButton.type = "button";
            noteButton.className = "message-action";
            noteButton.textContent = "Add to notepad";
            noteButton.setAttribute("aria-label", `Append selected text from ${m.role} message to project notepad`);
            noteButton.addEventListener("click", () => appendSelectionToNotepad(msgDiv));
            messageActions.appendChild(noteButton);
        }

        if (m.role === "assistant" && !m.isStreaming) {
            const fb = document.createElement("div");
            fb.className = "message-feedback";
            const up = document.createElement("button");
            up.className = "action-icon";
            up.textContent = "👍";
            up.title = "Good answer";
            up.setAttribute("aria-label", "Mark answer as good");
            up.onclick = () => sendFeedback(1, m.content || "", m.model);
            const down = document.createElement("button");
            down.className = "action-icon";
            down.textContent = "👎";
            down.title = "Bad answer";
            down.setAttribute("aria-label", "Mark answer as bad");
            down.onclick = () => sendFeedback(-1, m.content || "", m.model);
            fb.appendChild(up);
            fb.appendChild(down);
            messageActions.appendChild(fb);
        }

        if (messageActions.childElementCount) {
            messageActions.appendChild(actionStatus);
            header.appendChild(messageActions);
        }

        msgDiv.appendChild(header);

        const contentSpan = document.createElement("span");
        contentSpan.className = "message-content";
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
    updateContextStrip();
    if (!state.streaming) renderPredictions();
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
        renderPredictions();
        return;
    }

    // Simple: support up to 3 images, base64 data URLs
    const maxImages = 3;
    const selected = files.slice(0, maxImages);
    state.pendingImages = [];
    imageStatus.textContent = "Loading image(s)...";

    let loaded = 0;
    const finishImageLoad = () => {
        if (loaded !== selected.length) return;
        if (state.pendingImages.length === 0) {
            imageStatus.textContent = "Images could not be read";
        } else {
            const base = `${state.pendingImages.length} image${state.pendingImages.length > 1 ? "s" : ""} attached`;
            const visionNote = modelSupportsVision(modelSelect.value) ? " (vision-enabled)" : " (model may ignore images)";
            imageStatus.textContent = base + visionNote;
            maybePrefillForAttachment("image");
        }
        renderPredictions();
    };

    selected.forEach(file => {
        const reader = new FileReader();
        reader.onload = () => {
            state.pendingImages.push({ image_url: reader.result });
            loaded += 1;
            finishImageLoad();
        };
        reader.onerror = () => {
            console.error("Failed to read image file");
            loaded += 1;
            finishImageLoad();
        };
        reader.readAsDataURL(file);
    });
});

// ---------------------------------------------
// EVENT BINDINGS
// ---------------------------------------------
const TERMINAL_TOOL_RUNS = new Set([
    "completed", "model_timeout", "model_error", "error_budget", "step_limit",
    "budget_exceeded", "cancelled", "cancellation_failed", "approval_rejected",
    "run_conflict", "run_expired"
]);

async function toolRunRequest(path, options = {}) {
    const response = await fetch(routerEndpoint(path), {
        ...options,
        headers: authHeaders({ "Content-Type": "application/json", ...(options.headers || {}) })
    });
    if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Run request failed (${response.status})`);
    }
    return response.json();
}

// A readable before/after view of a pending file.edit, so an approval covers the
// exact change. Text nodes only: the model supplies every string shown here.
function approvalPreview(pending) {
    const args = pending.arguments || {};
    if (pending.tool_name !== "file.edit"
        || typeof args.path !== "string"
        || typeof args.old_text !== "string"
        || typeof args.new_text !== "string") {
        return null;
    }
    const count = Number.isInteger(args.expected_count) ? args.expected_count : 1;
    const preview = document.createElement("div");
    preview.className = "edit-preview";
    const target = document.createElement("p");
    target.className = "edit-preview-target";
    target.textContent = `Edit ${args.path} · replaces ${count} ${count === 1 ? "occurrence" : "occurrences"}`;
    preview.appendChild(target);
    for (const [label, text, kind] of [["Before", args.old_text, "before"], ["After", args.new_text, "after"]]) {
        const heading = document.createElement("h5");
        heading.textContent = label;
        preview.appendChild(heading);
        if (text === "") {
            const note = document.createElement("p");
            note.className = "edit-preview-empty";
            note.textContent = "Nothing: the text above is deleted.";
            preview.appendChild(note);
            continue;
        }
        const block = document.createElement("pre");
        block.className = `edit-preview-${kind}`;
        block.textContent = text;
        preview.appendChild(block);
    }
    return preview;
}

function renderToolRun(run) {
    if (run.run_id !== activeToolRunId) return;
    runLedger.classList.remove("hidden");
    const snapshot = run.snapshot || {};
    runLedgerStatus.textContent = run.status.replaceAll("_", " ");
    runLedgerReason.textContent = TERMINAL_TOOL_RUNS.has(run.status)
        ? `Finished: ${run.reason_code.replaceAll("_", " ")}`
        : `Run ${run.run_id.slice(0, 13)}… · ${snapshot.steps || 0} model steps`;
    runLedgerEvents.replaceChildren(...toolRunState.events.map((event) => {
        const item = document.createElement("li");
        item.textContent = `${event.sequence}. ${event.kind.replaceAll("_", " ")}${event.tool_name ? ` · ${event.tool_name}` : ""}${event.status ? ` · ${event.status}` : ""}`;
        return item;
    }));
    runLedgerTranscript.replaceChildren(...(snapshot.transcript || [])
        .filter((item) => item.role === "assistant" || item.role === "tool")
        .slice(-8).map((item) => {
            const block = document.createElement("p");
            const content = typeof item.content === "string" ? item.content : "[tool call]";
            block.textContent = `${item.role}: ${content.slice(0, 2000)}`;
            return block;
        }));
    const pending = snapshot.pending_call;
    runLedgerApproval.classList.toggle("hidden", !pending);
    if (!pending) {
        runLedgerApproval.replaceChildren();
        delete runLedgerApproval.dataset.callId;
        delete runLedgerApproval.dataset.runId;
    } else if (
        runLedgerApproval.dataset.callId !== pending.call_id
        || runLedgerApproval.dataset.runId !== run.run_id
    ) {
        runLedgerApproval.replaceChildren();
        runLedgerApproval.dataset.callId = pending.call_id;
        runLedgerApproval.dataset.runId = run.run_id;
        const heading = document.createElement("h4");
        heading.tabIndex = -1;
        heading.textContent = "Approval required";
        const summary = document.createElement("p");
        summary.textContent = `${pending.tool_name} · permission: ${pending.permission} · call: ${pending.call_id}`;
        const argumentsBlock = document.createElement("pre");
        argumentsBlock.textContent = JSON.stringify(pending.arguments, null, 2);
        const actions = document.createElement("div");
        actions.className = "run-ledger-actions";
        for (const [label, decision] of [["Approve once", "approve"], ["Reject", "reject"]]) {
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = label;
            button.className = decision === "approve" ? "primary" : "secondary";
            button.addEventListener("click", async () => {
                actions.querySelectorAll("button").forEach((item) => { item.disabled = true; });
                try {
                    const exact = Object.fromEntries([
                        "call_id", "digest", "definition_fingerprint", "permission", "nonce"
                    ].map((key) => [key, pending[key]]));
                    const decided = await toolRunRequest(
                        `/tools/agent/runs/${run.run_id}/decisions`,
                        { method: "POST", body: JSON.stringify({ ...exact, decision }) }
                    );
                    if (activeToolRunId === run.run_id) renderToolRun(decided);
                } catch (error) {
                    if (activeToolRunId !== run.run_id) return;
                    runLedgerReason.textContent = error.message;
                    actions.querySelectorAll("button").forEach((item) => { item.disabled = false; });
                }
            });
            actions.appendChild(button);
        }
        const preview = approvalPreview(pending);
        if (preview) {
            const exactArguments = document.createElement("details");
            const exactSummary = document.createElement("summary");
            exactSummary.textContent = "Exact arguments";
            exactArguments.append(exactSummary, argumentsBlock);
            runLedgerApproval.append(heading, summary, preview, actions, exactArguments);
        } else {
            runLedgerApproval.append(heading, summary, actions, argumentsBlock);
        }
        heading.focus();
    }
    runLedgerStop.disabled = TERMINAL_TOOL_RUNS.has(run.status) || run.status === "approval_required";
    runLedgerStop.title = run.status === "approval_required"
        ? "Reject the pending call to stop this run" : "Stop the active run";
}

async function refreshToolRun(runId) {
    if (activeToolRunId !== runId) return null;
    const run = await toolRunRequest(`/tools/agent/runs/${runId}`);
    if (activeToolRunId !== runId) return null;
    renderToolRun(run);
    return run;
}

async function watchToolRun(runId) {
    const runState = toolRunState;
    const abort = new AbortController();
    toolRunStreamAbort = abort;
    let failures = 0;
    runLedgerRetry.classList.add("hidden");
    try {
        while (activeToolRunId === runId && toolRunState === runState && !abort.signal.aborted) {
            try {
                const response = await fetch(
                    routerEndpoint(`/tools/agent/runs/${runId}/events?stream=true&after=${runState.cursor}`),
                    { headers: authHeaders({ "Accept": "text/event-stream" }), signal: abort.signal }
                );
                if (activeToolRunId !== runId || toolRunState !== runState || abort.signal.aborted) return;
                if (!response.ok || !response.body) throw new Error("Event stream unavailable");
                failures = 0;
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = "";
                while (activeToolRunId === runId && toolRunState === runState && !abort.signal.aborted) {
                    const { done, value } = await reader.read();
                    if (activeToolRunId !== runId || toolRunState !== runState || abort.signal.aborted) return;
                    if (done) break;
                    buffer += decoder.decode(value, { stream: true });
                    let boundary;
                    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
                        const frame = buffer.slice(0, boundary);
                        buffer = buffer.slice(boundary + 2);
                        const data = frame.split("\n").find((line) => line.startsWith("data: "));
                        if (!data) continue;
                        const event = JSON.parse(data.slice(6));
                        if (event.sequence <= runState.cursor) continue;
                        runState.cursor = event.sequence;
                        runState.events.push(event);
                        await refreshToolRun(runId);
                        if (activeToolRunId !== runId || toolRunState !== runState) return;
                        if (event.kind === "terminal") return;
                    }
                }
                const current = await refreshToolRun(runId);
                if (!current || TERMINAL_TOOL_RUNS.has(current.status)) return;
            } catch (error) {
                if (activeToolRunId !== runId || toolRunState !== runState || abort.signal.aborted) return;
                failures += 1;
                if (failures >= 3) {
                    runLedgerReason.textContent = "Event connection lost. Reconnect to continue this run.";
                    runLedgerRetry.classList.remove("hidden");
                    return;
                }
                await new Promise((resolve) => setTimeout(resolve, 1000));
            }
        }
    } finally {
        if (toolRunStreamAbort === abort) toolRunStreamAbort = null;
    }
}

async function startToolRun() {
    const prompt = promptInput.value.trim();
    if (!prompt || !state.selectedNode || !modelSelect.value) {
        runLedger.classList.remove("hidden");
        runLedgerReason.textContent = "Enter a message and select a node and model first.";
        return;
    }
    runToolsBtn.disabled = true;
    try {
        const history = (currentConversation()?.messages || [])
            .filter((item) => ["user", "assistant"].includes(item.role) && typeof item.content === "string")
            .slice(-24).map((item) => ({ role: item.role, content: item.content }));
        const run = await toolRunRequest("/tools/agent/runs", {
            method: "POST",
            body: JSON.stringify({
                messages: [...history, { role: "user", content: prompt }],
                node_id: state.selectedNode, model: modelSelect.value,
                project_id: currentConversation()?.project_id || null,
                conversation_id: state.sessionId || null
            })
        });
        toolRunStreamAbort?.abort();
        activeToolRunId = run.run_id;
        toolRunState = { runId: run.run_id, cursor: 0, events: [] };
        runLedgerRetry.classList.add("hidden");
        renderToolRun(run);
        runLedger.scrollIntoView({ block: "nearest", behavior: "instant" });
        toolRunWatcher = watchToolRun(run.run_id);
    } catch (error) {
        runLedger.classList.remove("hidden");
        runLedgerReason.textContent = error.message;
    } finally {
        runToolsBtn.disabled = false;
    }
}

runToolsBtn.addEventListener("click", startToolRun);
runLedgerRetry.addEventListener("click", () => {
    if (activeToolRunId) {
        toolRunStreamAbort?.abort();
        toolRunWatcher = watchToolRun(activeToolRunId);
    }
});
runLedgerStop.addEventListener("click", async () => {
    if (!activeToolRunId) return;
    runLedgerStop.disabled = true;
    try {
        renderToolRun(await toolRunRequest(
            `/tools/agent/runs/${activeToolRunId}/cancel`, { method: "POST" }
        ));
    } catch (error) {
        runLedgerReason.textContent = error.message;
        runLedgerStop.disabled = false;
    }
});

sendBtn.onclick = () => {
    if (state.streaming && state.abortController) {
        state.abortController.abort();
    } else {
        sendMessage();
    }
};

refreshNodesBtn.onclick = fetchNodes;
newConvoBtn.onclick = createNewConversation;
loadModelsBtn.onclick = () => loadModelsFromNode();

nodeSelect.addEventListener("change", (e) => {
    clearUndoState();
    state.selectedNode = e.target.value;
    state.restoredSelection = false;
    state.selectionFallback = false;
    if (state.selectedNode) {
        loadModelsFromNode().then(() => {
            updateAnticipationPreferences();
            updateContextStrip();
        });
    }
});

modelSelect.addEventListener("change", () => {
    clearUndoState();
    state.restoredSelection = false;
    state.selectionFallback = false;
    updateImageSupportNotice();
    updateAnticipationPreferences();
    updateContextStrip();
    renderPredictions();
});

promptInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});
promptInput.addEventListener("input", () => {
    resizeComposer();
    persistSessionDraft();
    renderPredictions();
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

if (templateSelect) {
    templateSelect.addEventListener("change", () => {
        clearUndoState();
        state.restoredSelection = false;
        state.selectionFallback = false;
        updateAnticipationPreferences();
        updateContextStrip();
        renderPredictions();
    });
}

if (projectSelect) {
    projectSelect.addEventListener("change", async (e) => {
        const snapshot = captureControlSnapshot();
        clearUndoState();
        await flushProjectNotepadSave();
        const chosen = e.target.value;
        if (chosen === "__create__") {
            await createProjectFlow();
            return;
        }
        selectedProjectId = chosen;
        localStorage.setItem("dave_project_id", selectedProjectId);
        if (!notepadPanel.hidden) {
            notepadLoadedProjectId = null;
            if (selectedProjectId) await loadProjectNotepad(selectedProjectId);
            else closeProjectNotepad();
        }
        await loadConversationsFromBackend();
        state.sessionId = null;
        state.restoredSelection = false;
        state.selectionFallback = false;
        const storedDefault = selectedProjectId
            ? anticipationRecord.preferences.projectDefaults[selectedProjectId]
            : null;
        if (storedDefault) {
            const storedTemplate = TEMPLATES[storedDefault.template] ? storedDefault.template : "general";
            let appliedStoredDefault = storedTemplate !== templateSelect.value;
            templateSelect.value = storedTemplate;
            if (
                storedDefault.nodeId
                && state.nodes.some((item) => item.id === storedDefault.nodeId)
                && state.nodeStatus[storedDefault.nodeId]?.status === "online"
            ) {
                state.selectedNode = storedDefault.nodeId;
                nodeSelect.value = storedDefault.nodeId;
                await loadModelsFromNode(storedDefault.modelId);
                appliedStoredDefault = true;
            } else if (storedDefault.nodeId) {
                state.selectionFallback = true;
            }
            if (appliedStoredDefault) {
                state.restoredSelection = true;
                exposeUndo(
                    snapshot,
                    state.selectionFallback
                        ? "Stored selection unavailable; using safe fallback · Undo available"
                        : "Using last selection · Undo available"
                );
            }
        }
        applyProjectSupportDefault();
        updateAnticipationPreferences();
        renderConversationList();
        renderMessages();
        updateContextStrip();
    });
}

if (resyncBtn) {
    resyncBtn.addEventListener("click", resyncConversationInstructions);
}

if (editProjectBtn) {
    editProjectBtn.addEventListener("click", openProjectHomepage);
}

if (projectHomeClose) {
    projectHomeClose.addEventListener("click", () => {
        resetProjectHomeMotion();
        projectHomeDialog.close();
    });
}

if (projectHomeDialog) {
    projectHomeDialog.addEventListener("close", resetProjectHomeMotion);
}

if (projectHomeInstructions) {
    projectHomeInstructions.addEventListener("input", renderProjectBudget);
}

if (projectContextBudget) {
    projectContextBudget.addEventListener("input", renderProjectBudget);
}

if (saveProjectInstructionsBtn) {
    saveProjectInstructionsBtn.addEventListener("click", saveProjectInstructions);
}

if (projectFileForm) {
    projectFileForm.addEventListener("submit", uploadProjectReference);
}

if (projectFileInput) {
    projectFileInput.addEventListener("change", () => {
        const name = projectFileInput.files?.[0]?.name;
        const label = projectFileForm.querySelector(".project-file-drop span");
        if (label) label.textContent = name || "Choose a local reference";
    });
}

if (saveProjectBrainBtn) {
    saveProjectBrainBtn.addEventListener("click", saveProjectBrain);
}

if (compactProjectBrainBtn) {
    compactProjectBrainBtn.addEventListener("click", compactProjectBrain);
}

if (restoreProjectBrainBtn) {
    restoreProjectBrainBtn.addEventListener("click", restoreProjectBrain);
}

if (deleteProjectBrainBtn) {
    deleteProjectBrainBtn.addEventListener("click", deleteProjectBrain);
}

if (previewProjectContextBtn) {
    previewProjectContextBtn.addEventListener("click", previewProjectRequestContext);
}

if (attachProjectChatBtn) {
    attachProjectChatBtn.addEventListener("click", toggleActiveChatProject);
}

if (startProjectChatBtn) {
    startProjectChatBtn.addEventListener("click", startProjectChat);
}

if (instructionsBtn) {
    instructionsBtn.addEventListener("click", openInstructionsDialog);
}

if (instructionsClose) {
    instructionsClose.addEventListener("click", () => instructionsDialog.close());
}

[globalInstructions, projectInstructions, sessionInstructions].forEach((field) => {
    field?.addEventListener("input", () => {
        instructionPreviewMode = "layered";
        updateInstructionCounts();
    });
});

if (saveInstructionsBtn) {
    saveInstructionsBtn.addEventListener("click", saveInstructionLayers);
}

if (revertSessionInstructionsBtn) {
    revertSessionInstructionsBtn.addEventListener("click", revertSessionInstructions);
}

if (resetGlobalInstructionsBtn) {
    resetGlobalInstructionsBtn.addEventListener("click", resetGlobalInstructions);
}

if (notepadToggle) {
    notepadToggle.addEventListener("click", () => {
        if (notepadPanel.hidden) openProjectNotepad();
        else closeProjectNotepad();
    });
}

if (notepadClose) {
    notepadClose.addEventListener("click", closeProjectNotepad);
}

if (notepadInput) {
    notepadInput.addEventListener("input", scheduleProjectNotepadSave);
}

if (sendNotepadBtn) {
    sendNotepadBtn.addEventListener("click", sendNotepadToChat);
}

if (transcribeBtn) {
    transcribeBtn.addEventListener("click", transcribeAudio);
}

if (fileInput) {
    fileInput.addEventListener("change", () => {
        if (fileInput.files && fileInput.files.length) {
            const file = fileInput.files[0];
            if (fileStatus) fileStatus.textContent = `Attached: ${file.name}`;
            maybePrefillForAttachment(detectAttachmentKind());
        } else if (fileStatus) {
            fileStatus.textContent = "";
        }
        renderPredictions();
    });
}

if (audioInput) {
    audioInput.addEventListener("change", () => {
        if (audioInput.files && audioInput.files.length) {
            audioStatus.textContent = "Audio selected for transcription.";
            maybePrefillForAttachment("audio");
        } else {
            audioStatus.textContent = "";
        }
        renderPredictions();
    });
}

if (supportFlag) {
    supportFlag.addEventListener("change", renderPredictions);
}

if (dictateBtn) {
    dictateBtn.addEventListener("click", toggleDictation);
}

if (hfDownloadBtn) {
    hfDownloadBtn.addEventListener("click", downloadModelFromHF);
}

if (credentialBtn) {
    credentialBtn.hidden = Boolean(window.__DAVE_DESKTOP__);
    credentialBtn.addEventListener("click", () => {
        if (requestBrowserCredential()) window.location.reload();
    });
}

if (resetSuggestionsBtn) {
    resetSuggestionsBtn.addEventListener("click", () => {
        localStorage.removeItem(ANTICIPATION_STORAGE_KEY);
        anticipationRecord = window.DaveAnticipation.createRecord();
        state.restoredSelection = false;
        state.selectionFallback = false;
        state.lastPredictionUndo = null;
        undoPredictionBtn?.classList.add("hidden");
        updateContextStrip();
        renderPredictions();
    });
}

if (undoPredictionBtn) {
    undoPredictionBtn.addEventListener("click", undoLastPrediction);
}

document.querySelectorAll("[data-context-target]").forEach((button) => {
    button.addEventListener("click", () => {
        const targetId = button.dataset.contextTarget;
        if (["projectSelect", "templateSelect"].includes(targetId)) setMobileTab("history");
        if (["targetNodeSelect", "modelSelect"].includes(targetId)) setMobileTab("runtime");
        const target = document.getElementById(targetId);
        requestAnimationFrame(() => {
            target?.focus();
            target?.click();
        });
    });
});

if (attachToolsToggle && attachmentTools) {
    attachToolsToggle.addEventListener("click", () => {
        const expanded = attachToolsToggle.getAttribute("aria-expanded") === "true";
        attachToolsToggle.setAttribute("aria-expanded", expanded ? "false" : "true");
        attachmentTools.classList.toggle("mobile-tools-open", !expanded);
    });
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
    input.setAttribute("aria-label", "Search all conversations");
    input.style.flex = "1";
    const btn = document.createElement("button");
    btn.className = "icon-btn";
    btn.textContent = "🔍";
    btn.title = "Search";
    btn.setAttribute("aria-label", "Search conversations");
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
            results.replaceChildren();
            data.forEach(item => {
                const div = document.createElement("button");
                div.type = "button";
                div.className = "search-result";
                div.style.padding = "4px";
                div.style.borderBottom = "1px solid var(--border)";
                const title = document.createElement("strong");
                title.textContent = item.title || item.conversation_id;
                const lineBreak = document.createElement("br");
                const role = document.createElement("em");
                role.textContent = item.role;
                const preview = document.createTextNode(`: ${String(item.content || "").slice(0, 120)}...`);
                div.appendChild(title);
                div.appendChild(lineBreak);
                div.appendChild(role);
                div.appendChild(preview);
                div.addEventListener("click", async () => {
                    await switchConversation(item.conversation_id);
                });
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
    initMobileTabs();
    initVisualViewport();
    anticipationRecord = readAnticipationRecord();
    if (!window.__DAVE_DESKTOP__ && !apiKey && !requestBrowserCredential()) {
        if (routeStatus) routeStatus.textContent = "A session API key is required to load DaveLLM.";
        return;
    }
    initRoutingControls();
    fetch(routerEndpoint("/tools"), { headers: authHeaders() })
        .then((response) => { runToolsBtn.classList.toggle("hidden", !response.ok); })
        .catch(() => { runToolsBtn.classList.add("hidden"); });
    initDictation();
    loadStats();
    updateStatsDisplay();
    renderSearchBox();
    await loadProjects();

    let restoredAnything = false;
    const preferredProjectId = anticipationRecord.preferences.lastProjectId || selectedProjectId;
    if (preferredProjectId && projects.some((item) => item.project_id === preferredProjectId)) {
        selectedProjectId = preferredProjectId;
        projectSelect.value = preferredProjectId;
        restoredAnything = true;
    } else if (preferredProjectId) {
        selectedProjectId = "";
        projectSelect.value = "";
        state.selectionFallback = true;
    }

    const startupProjectDefault = selectedProjectId
        ? anticipationRecord.preferences.projectDefaults[selectedProjectId]
        : null;
    const preferredTemplate = startupProjectDefault?.template || anticipationRecord.preferences.lastTemplate;
    if (TEMPLATES[preferredTemplate]) {
        templateSelect.value = preferredTemplate;
        restoredAnything = restoredAnything || preferredTemplate !== "general";
    }

    purgeLegacyContentStorage();
    await loadConversationsFromBackend();

    const lastSessionId = localStorage.getItem(LAST_SESSION_KEY);
    state.lastSessionId = lastSessionId && state.conversations[lastSessionId] ? lastSessionId : null;
    state.sessionId = null;

    const preferredNodeId = startupProjectDefault?.nodeId || anticipationRecord.preferences.lastNodeId;
    if (preferredNodeId) state.selectedNode = preferredNodeId;
    await fetchNodes();

    if (preferredNodeId) {
        const configured = state.nodes.some((item) => item.id === preferredNodeId);
        const healthy = state.nodeStatus[preferredNodeId]?.status === "online";
        if (!configured || !healthy) {
            const safeNode = state.nodes.find((item) => state.nodeStatus[item.id]?.status === "online") || state.nodes[0];
            state.selectedNode = safeNode?.id || null;
            nodeSelect.value = state.selectedNode || "";
            state.selectionFallback = true;
        } else {
            restoredAnything = true;
        }
    }

    if (state.selectedNode) {
        const preferredModelId = startupProjectDefault?.nodeId === state.selectedNode
            ? startupProjectDefault.modelId
            : (anticipationRecord.preferences.lastModelByNode[state.selectedNode] || null);
        await loadModelsFromNode(preferredModelId);
        restoredAnything = restoredAnything || Boolean(preferredModelId && state.modelMeta[preferredModelId]);
    }

    const draft = sessionStorage.getItem(DRAFT_SESSION_KEY);
    if (draft && !promptInput.value) {
        promptInput.value = draft;
        resizeComposer();
    }
    applyProjectSupportDefault();
    state.restoredSelection = restoredAnything;
    if (restoredAnything) {
        exposeUndo({
            sessionId: null,
            projectId: "",
            template: "general",
            nodeId: state.nodes[0]?.id || null,
            modelId: Object.keys(state.modelMeta)[0] || null,
            supportMode: false,
            composer: promptInput.value
        }, state.selectionFallback
            ? "Stored selection unavailable; using safe fallback · Undo available"
            : "Using last selection · Undo available");
    }
    renderConversationList();
    renderMessages();
    updateContextStrip();
    renderPredictions();

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
