"use strict";

// Presentation helpers share the existing renderer's state and API clients.
// They never own credentials, conversations, tool decisions, or persistence.
let consoleView = "chat";
let historyVisible = true;
let inspectorTab = "context";
let approvalExpiry = null;
let approvalBusy = false;
let approvalTimer = null;
const consoleModelInventory = new Map();
const PANEL_WIDTH_KEY = "dave_panel_widths";
let panelWidthPreferences = {};

// Keep a usable chat column even when saved desktop widths meet a smaller screen.
function consolePanelLayout(available, preferences = {}, visible = {}, wide = false) {
    const minimum = { history: 220, inspector: 280 };
    const limits = { history: 560, inspector: 960 };
    const defaults = { history: wide ? 360 : 272, inspector: wide ? 560 : 340 };
    const widths = {};
    const names = ["history", "inspector"];
    for (const name of names) {
        const requested = Number.isFinite(preferences[name]) ? preferences[name] : defaults[name];
        widths[name] = Math.round(Math.max(minimum[name], Math.min(limits[name], requested)));
    }
    const active = names.filter((name) => visible[name]);
    const room = Math.max(0, available - 480 - active.length * 12);
    const total = active.reduce((sum, name) => sum + widths[name], 0);
    const shrinkable = active.reduce((sum, name) => sum + widths[name] - minimum[name], 0);
    if (total > room && shrinkable > 0) {
        const fraction = Math.min(1, (total - room) / shrinkable);
        for (const name of active) widths[name] -= Math.ceil((widths[name] - minimum[name]) * fraction - 1e-9);
    }
    const maximum = {};
    for (const name of names) {
        const other = active.filter((item) => item !== name).reduce((sum, item) => sum + widths[item], 0);
        maximum[name] = Math.max(minimum[name], Math.min(limits[name], room - other));
    }
    return { widths, minimum, maximum };
}

function updateConsolePanelWidths() {
    const layout = document.getElementById("chatView");
    if (!layout || layout.hidden || window.innerWidth <= 1024) return;
    const inspector = !document.getElementById("inspectorPanel").hidden;
    const history = historyVisible && (!inspector || window.innerWidth > 1280);
    const result = consolePanelLayout(layout.clientWidth - 24, panelWidthPreferences,
        { history, inspector }, window.innerWidth >= 2200);
    for (const name of ["history", "inspector"]) {
        layout.style.setProperty(`--${name}-width`, `${result.widths[name]}px`);
        const handle = document.getElementById(`${name}Resize`);
        handle.setAttribute("aria-valuemin", result.minimum[name]);
        handle.setAttribute("aria-valuemax", result.maximum[name]);
        handle.setAttribute("aria-valuenow", result.widths[name]);
        handle.setAttribute("aria-valuetext", `${result.widths[name]} pixels`);
    }
}

function saveConsolePanelWidths() {
    try { localStorage.setItem(PANEL_WIDTH_KEY, JSON.stringify(panelWidthPreferences)); }
    catch (_) { /* Resizing remains available when browser storage is blocked. */ }
}

function initConsolePanelResizing() {
    try {
        const saved = JSON.parse(localStorage.getItem(PANEL_WIDTH_KEY) || "{}");
        for (const name of ["history", "inspector"]) {
            if (Number.isFinite(saved?.[name])) panelWidthPreferences[name] = saved[name];
        }
    } catch (_) { panelWidthPreferences = {}; }
    for (const name of ["history", "inspector"]) {
        const handle = document.getElementById(`${name}Resize`);
        const direction = name === "history" ? 1 : -1;
        let drag = null;
        const resize = (value) => {
            const minimum = Number(handle.getAttribute("aria-valuemin"));
            const maximum = Number(handle.getAttribute("aria-valuemax"));
            panelWidthPreferences[name] = Math.min(maximum, Math.max(minimum, value));
            updateConsolePanelWidths();
        };
        const reset = () => {
            delete panelWidthPreferences[name];
            updateConsolePanelWidths();
            saveConsolePanelWidths();
        };
        handle.addEventListener("dblclick", reset);
        handle.addEventListener("keydown", (event) => {
            if (event.key === "Enter") { event.preventDefault(); reset(); return; }
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            let value = Number(handle.getAttribute("aria-valuenow"));
            if (event.key === "Home") value = Number(handle.getAttribute("aria-valuemin"));
            else if (event.key === "End") value = Number(handle.getAttribute("aria-valuemax"));
            else value += (event.key === "ArrowRight" ? 1 : -1) * direction * (event.shiftKey ? 50 : 10);
            resize(value);
            saveConsolePanelWidths();
        });
        handle.addEventListener("pointerdown", (event) => {
            if (event.button !== 0) return;
            event.preventDefault();
            updateConsolePanelWidths();
            drag = { x: event.clientX, width: Number(handle.getAttribute("aria-valuenow")),
                preferences: { ...panelWidthPreferences } };
            handle.focus({ preventScroll: true });
            handle.setPointerCapture(event.pointerId);
        });
        handle.addEventListener("pointermove", (event) => {
            if (drag) resize(drag.width + direction * (event.clientX - drag.x));
        });
        handle.addEventListener("pointerup", () => { if (drag) { drag = null; saveConsolePanelWidths(); } });
        handle.addEventListener("pointercancel", () => {
            if (!drag) return;
            panelWidthPreferences = drag.preferences;
            drag = null;
            updateConsolePanelWidths();
        });
        handle.addEventListener("lostpointercapture", () => { drag = null; });
    }
    new ResizeObserver(updateConsolePanelWidths).observe(document.getElementById("chatView"));
    updateConsolePanelWidths();
}

function uiElement(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text) element.textContent = text;
    return element;
}

function notifyConsole(message) {
    const notice = document.getElementById("consoleNotice");
    notice.querySelector("span").textContent = String(message);
    notice.hidden = false;
}

function chooseDialog(dialog) {
    return new Promise((resolve) => {
        dialog.returnValue = "";
        dialog.addEventListener("close", () => resolve(dialog.returnValue), { once: true });
        dialog.showModal();
    });
}

function focusConsoleView() {
    document.getElementById(`${consoleView}View`).focus({ preventScroll: true });
}

function skipToConsoleContent(event) {
    event.preventDefault();
    focusConsoleView();
}

function setConsoleView(view) {
    const nextView = ["chat", "projects", "cluster", "settings"].includes(view) ? view : "chat";
    const changed = nextView !== consoleView;
    consoleView = nextView;
    document.body.dataset.view = consoleView;
    for (const name of ["chat", "projects", "cluster", "settings"]) {
        document.getElementById(`${name}View`).hidden = name !== consoleView;
    }
    document.querySelectorAll("[data-view]").forEach((button) => {
        if (button.tagName !== "BUTTON") return;
        if (button.dataset.view === consoleView) button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
    });
    document.querySelector(".skip-link").setAttribute("href", `#${consoleView}View`);
    if (consoleView === "projects") renderProjectCards();
    if (consoleView === "cluster") refreshCluster();
    updateConsoleContext();
    if (changed) focusConsoleView();
}

function showHistory(show = !historyVisible, activate = true) {
    if (activate) setConsoleView("chat");
    historyVisible = show;
    const panel = document.getElementById("historyPanel");
    panel.hidden = !show;
    panel.setAttribute("aria-hidden", String(!show));
    document.getElementById("historyToggle").setAttribute("aria-expanded", String(show));
    document.getElementById("chatView").classList.toggle("history-closed", !show);
    document.body.classList.toggle("history-open", show);
    if (show && window.innerWidth <= 1024) {
        document.getElementById("inspectorPanel").hidden = true;
        document.getElementById("chatView").classList.remove("inspector-open");
        document.getElementById("inspectorToggle").setAttribute("aria-expanded", "false");
    }
    updateConsolePanelWidths();
}

function showInspector(tab = inspectorTab, open = true, loadNote = true) {
    setConsoleView("chat");
    inspectorTab = tab;
    const panel = document.getElementById("inspectorPanel");
    panel.hidden = !open;
    document.getElementById("inspectorToggle").setAttribute("aria-expanded", String(open));
    document.getElementById("chatView").classList.toggle("inspector-open", open);
    for (const name of ["context", "run", "notepad"]) {
        document.getElementById(`inspector${name[0].toUpperCase()}${name.slice(1)}`).hidden = name !== tab;
        document.querySelector(`[data-inspector="${name}"]`).setAttribute("aria-pressed", String(name === tab));
    }
    if (open && window.innerWidth <= 1024) showHistory(false);
    if (open && tab === "notepad" && loadNote) openProjectNotepad();
    if (open) document.getElementById("inspectorClose").focus();
    updateConsolePanelWidths();
}

function updateConsoleContext() {
    const title = document.getElementById("viewTitle");
    if (!title) return;
    title.textContent = consoleView === "chat"
        ? (currentConversation()?.title || "New conversation")
        : consoleView[0].toUpperCase() + consoleView.slice(1);
    title.title = title.textContent;
    const empty = !conversationHasMessages();
    document.getElementById("chatPanel").classList.toggle("is-empty", empty);
    const startModes = document.getElementById("startingModes");
    if (startModes) startModes.hidden = !empty;
    const selected = templateSelect.value || "general";
    document.querySelectorAll("[data-start-mode]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.dataset.startMode === selected));
    });
    const detail = document.getElementById("emptyRuntime");
    if (detail) {
        const node = state.nodes.find((item) => item.id === state.selectedNode);
        const project = projects.find((item) => item.project_id === selectedProjectId);
        detail.textContent = node && modelSelect.value
            ? `This chat goes to ${modelSelect.value} on ${node.name}. ${project ? `Project: ${project.name}.` : "No project attached."}`
            : "Choose a node and model to start a conversation.";
    }
}

function conversationAge(conversation, now = Date.now()) {
    const timestamp = Date.parse(conversation.updated_at || conversation.created_at || "");
    if (!Number.isFinite(timestamp)) return { band: "unknown", label: "Date unavailable", text: "—" };
    const hours = Math.max(0, (now - timestamp) / 3600000);
    const band = hours < 24 ? "fresh" : hours < 72 ? "recent" : hours <= 168 ? "week" : "old";
    const labels = { fresh: "Updated under 24 hours", recent: "Updated 1 to 3 days", week: "Updated 3 to 7 days", old: "Updated over 7 days" };
    return { band, label: labels[band], text: hours < 1 ? `${Math.floor(hours * 60)}m` : hours < 24 ? `${Math.floor(hours)}h` : `${Math.floor(hours / 24)}d` };
}

function renderProjectCards() {
    const container = document.getElementById("projectCards");
    container.replaceChildren();
    if (!projects.length) container.append(uiElement("p", "empty-hint", "No projects yet. Create one to keep instructions and context together."));
    for (const project of projects) {
        const card = uiElement("article", "card");
        const open = uiElement("button", "primary", "Open project");
        open.addEventListener("click", async () => {
            selectedProjectId = project.project_id;
            projectSelect.value = selectedProjectId;
            await openProjectHomepage();
        });
        card.append(uiElement("h3", "", project.name), uiElement("p", "", project.description || "Project instructions, files, artifacts, and BRAIN."), open);
        container.append(card);
    }
}

async function openChatProjectPicker() {
    const picker = document.getElementById("chatProjectPicker");
    picker.replaceChildren();
    const none = uiElement("option", "", "No project");
    none.value = "";
    picker.append(none);
    for (const project of projects) {
        const option = uiElement("option", "", project.name);
        option.value = project.project_id;
        picker.append(option);
    }
    picker.value = (currentConversation() ? currentConversation().project_id : selectedProjectId) || "";
    document.getElementById("chatProjectStatus").textContent = "";
    document.getElementById("projectPickerDialog").showModal();
}

async function applyChatProject() {
    const next = document.getElementById("chatProjectPicker").value;
    const status = document.getElementById("chatProjectStatus");
    const button = document.getElementById("applyChatProject");
    button.disabled = true;
    try {
        await flushProjectNotepadSave();
        if (state.sessionId) {
            const response = await fetch(routerEndpoint(`/conversations/${encodeURIComponent(state.sessionId)}/project`), {
                method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
                body: JSON.stringify({ project_id: next || null })
            });
            if (!response.ok) throw new Error(await apiError(response));
            const result = await response.json();
            Object.assign(currentConversation(), { project_id: result.project_id, system_prompt: result.system_prompt });
        }
        selectedProjectId = next;
        projectSelect.value = next;
        notepadLoadedProjectId = null;
        closeProjectNotepad();
        updateAnticipationPreferences();
        updateContextStrip();
        document.getElementById("projectPickerDialog").close();
    } catch (error) { status.textContent = `Project change failed: ${error.message}`; }
    finally { button.disabled = false; }
}

async function refreshCluster() {
    const checked = document.getElementById("clusterChecked");
    checked.textContent = "Checking cluster…";
    await fetchNodes();
    await Promise.allSettled(state.nodes.map(async (node) => {
        if (state.nodeStatus[node.id]?.status !== "online") { consoleModelInventory.delete(node.id); return; }
        try {
            const response = await fetch(routerEndpoint(`/nodes/${encodeURIComponent(node.id)}/models`), {
                headers: authHeaders(), signal: AbortSignal.timeout(15000)
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            consoleModelInventory.set(node.id, { models: (data.models || []).map(normalizeModelMeta).filter(Boolean) });
        } catch (_) { consoleModelInventory.set(node.id, { error: true }); }
    }));
    if (state.nodes.length) renderConsoleNodes(state.nodes);
    const results = await Promise.allSettled(["/monitoring/health", "/analytics/costs"].map(async (path) => {
        const response = await fetch(routerEndpoint(path), { headers: authHeaders(), signal: AbortSignal.timeout(15000) });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
    }));
    const healthBox = document.getElementById("clusterHealth");
    const errorBox = document.getElementById("clusterErrors");
    const costBox = document.getElementById("clusterCosts");
    const row = (container, label, value) => {
        const line = uiElement("div", "metric-row");
        line.append(uiElement("span", "", label), uiElement("strong", "", value));
        container.append(line);
    };
    healthBox.replaceChildren(); errorBox.replaceChildren(); costBox.replaceChildren();
    if (results[0].status === "fulfilled") {
        const data = results[0].value;
        for (const [model, health] of Object.entries(data.model_health || {})) {
            row(healthBox, model, `${health.failures} failures${health.last_error ? ` · ${health.last_error}` : ""}`);
        }
        if (!healthBox.childElementCount) healthBox.textContent = "No model health records. This is not proof of successful inference.";
        for (const error of (data.recent_errors || []).slice(-10).reverse()) {
            const item = uiElement("div", "error-record");
            item.append(uiElement("strong", "", String(error.detail || error.event)), uiElement("small", "", String(error.timestamp || "Time unavailable")));
            errorBox.append(item);
        }
        if (!errorBox.childElementCount) errorBox.textContent = "No recent errors recorded.";
    } else {
        healthBox.textContent = errorBox.textContent = `Unavailable: ${results[0].reason.message}`;
    }
    if (results[1].status === "fulfilled") {
        const data = results[1].value;
        row(costBox, "Total recorded estimate", Number.isFinite(data.total_cost) ? `$${data.total_cost.toFixed(2)}` : "No data");
        for (const [model, cost] of Object.entries(data.by_model || {})) row(costBox, model, Number.isFinite(cost) ? `$${cost.toFixed(4)}` : "No data");
    } else costBox.textContent = `Unavailable: ${results[1].reason.message}`;
    const online = Object.values(state.nodeStatus).filter((node) => node.status === "online").length;
    checked.textContent = `${online} of ${state.nodes.length} nodes online · checked ${new Date().toLocaleTimeString()}${results.some((r) => r.status === "rejected") ? " · some metrics unavailable" : ""}`;
}

function renderConsoleNodes(nodes) {
    nodesContainer.replaceChildren();
    if (!nodes.length) { nodesContainer.append(uiElement("p", "", "No nodes registered.")); return; }
    for (const node of nodes) {
        const status = state.nodeStatus[node.id] || { status: "unknown", latency: null };
        const card = uiElement("article", `node-card ${node.id === state.selectedNode ? "active" : ""}`);
        const header = uiElement("div", "node-header");
        header.append(uiElement("h3", "node-name", node.name), uiElement("span", `node-status-label node-status-${status.status}`, status.status.toUpperCase()));
        const metrics = uiElement("div", "node-metrics");
        const latency = uiElement("div");
        latency.append(uiElement("small", "", "API ROUND TRIP"), uiElement("strong", "node-latency", Number.isFinite(status.latency) ? `${status.latency} ms` : "—"));
        metrics.append(latency, uiElement("p", "", status.status === "online" ? "Online at last status check. Inference speed depends on the selected model." : status.status === "offline" ? "Not answering right now. Models return when the node is available." : "Status unavailable. Refresh to check again."));
        const details = uiElement("details");
        details.append(uiElement("summary", "", "Connection details"), uiElement("code", "node-url", node.url || "Address unavailable"));
        const use = uiElement("button", "secondary", node.id === state.selectedNode ? "Selected for chat" : "Use for chat");
        use.disabled = status.status !== "online";
        use.addEventListener("click", async () => {
            state.selectedNode = node.id; nodeSelect.value = node.id;
            await loadModelsFromNode(); updateContextStrip(); updateAnticipationPreferences();
            renderConsoleNodes(state.nodes); document.getElementById("runtimeDialog").showModal();
        });
        const inventory = consoleModelInventory.get(node.id);
        const models = uiElement("div", "node-models");
        if (inventory?.models) {
            models.append(uiElement("small", "", `${inventory.models.length} models`));
            for (const model of inventory.models) models.append(uiElement("code", "model-tag", `${model.id}${model.vision ? " · vision" : ""}`));
        } else models.append(uiElement("small", "", status.status !== "online" ? "Model inventory unavailable while offline." : inventory?.error ? "Model inventory unavailable. Refresh to retry." : "Open Cluster to check model inventory."));
        card.append(header, metrics, models, details, use);
        nodesContainer.append(card);
    }
}

async function previewInspector() {
    const projectId = (currentConversation() ? currentConversation().project_id : selectedProjectId);
    const status = document.getElementById("inspectorBudgetStatus");
    if (!projectId) { status.textContent = "No project attached. Use Instructions to inspect the active instruction layers."; return; }
    projectHomeProjectId = projectId;
    await previewProjectRequestContext();
    status.textContent = projectContextPreviewStatus.textContent;
}

function renderInspectorBudget(data) {
    const container = document.getElementById("inspectorBudget");
    container.replaceChildren();
    const budget = data.budget || {};
    const table = uiElement("table");
    const head = uiElement("tr");
    for (const label of ["Category", "Used", "Limit", "Left"]) head.append(uiElement("th", "", label));
    const thead = uiElement("thead"); thead.append(head); table.append(thead);
    const body = uiElement("tbody");
    for (const [key, label] of [["project_instructions", "Instructions"], ["brain", "BRAIN"], ["file_context", "Files"], ["artifact_history", "Artifacts"]]) {
        const limit = budget.available_limits?.[key] || 0;
        const used = budget.usage?.[key] || 0;
        const row = uiElement("tr", used >= limit * .9 && limit > 0 ? "budget-warning" : "");
        for (const value of [label, used, limit, Math.max(0, limit - used)]) row.append(uiElement("td", "", String(value)));
        body.append(row);
    }
    table.append(body);
    container.append(table, uiElement("p", "", "Project context only. Unused capacity carries forward to BRAIN, files, then artifacts. Preview again after changing the draft or context."));
    const details = uiElement("details");
    details.append(uiElement("summary", "", "Exact assembled messages"), uiElement("pre", "", JSON.stringify(data.messages, null, 2)));
    container.append(details);
}

function updateApprovalPresentation(run) {
    const pending = run.snapshot?.pending_call;
    approvalBusy = !TERMINAL_TOOL_RUNS.has(run.status);
    approvalExpiry = pending?.expires_at || null;
    document.getElementById("chatPanel").classList.toggle("approval-pending", !!pending);
    runLedger.dataset.status = run.status;
    sendBtn.disabled = approvalBusy;
    toolPickerToggle.disabled = approvalBusy;
    if (pending) {
        let timer = document.getElementById("approvalCountdown");
        if (!timer) {
            timer = uiElement("p", "approval-countdown"); timer.id = "approvalCountdown";
            runLedgerApproval.prepend(timer);
            runLedgerApproval.append(uiElement("p", "approval-explanation", "Approve once applies to this exact call only. Reject stops the run. On expiry nothing is written. Your draft stays editable; sending is disabled until this run ends."));
        }
        if (consoleView !== "chat") setConsoleView("chat");
        tickApprovalClock();
    }
    const mirror = document.getElementById("inspectorRun");
    mirror.replaceChildren(uiElement("p", "", `${run.status.replaceAll("_", " ")} · ${run.reason_code || ""}`));
    for (const event of toolRunState.events) mirror.append(uiElement("p", "", `${event.sequence}. ${event.kind}${event.tool_name ? ` · ${event.tool_name}` : ""}`));
}

function tickApprovalClock() {
    const timer = document.getElementById("approvalCountdown");
    if (!timer || !approvalExpiry) return;
    const remaining = Math.max(0, Math.ceil((Date.parse(approvalExpiry) - Date.now()) / 1000));
    timer.textContent = remaining > 0 ? `Expires in ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}` : "Approval expired. Waiting for router confirmation; nothing will be approved.";
    if (remaining === 0) runLedgerApproval.querySelectorAll("button").forEach((button) => { button.disabled = true; });
}

// Inline Markdown: escapes, code, bold, strikethrough, italics, links and bare URLs, tried in that order.
const MARKDOWN_INLINE = /\\([\\`*_~[\]()#>|!-])|`([^`\n]+)`|\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<![\w])__(?=\S)(.+?)(?<=\S)__(?![\w])|~~(?=\S)(.+?)(?<=\S)~~|\*(?=[^\s*])(.+?)(?<=[^\s*])\*|(?<![\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w])|\[([^\]\n]+)\]\(\s*([^()\s]+(?:\([^()\s]*\)[^()\s]*)*)(?:\s+"[^"\n]*")?\s*\)|(https?:\/\/[^\s<>]*[^\s<>.,;:!?"'()[\]])/g;
const MARKDOWN_SAFE_HREF = /^(https?:\/\/|mailto:)/i;
const MARKDOWN_FENCE = /^\s{0,3}(`{3,}|~{3,})\s*([^`\s]*)[^`]*$/;
const MARKDOWN_HEADING = /^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/;
const MARKDOWN_RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const MARKDOWN_QUOTE = /^\s{0,3}>\s?/;
const MARKDOWN_LIST_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const MARKDOWN_TABLE_DIVIDER = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;

function appendMarkdownInline(target, text, allowLinks = true) {
    let last = 0;
    for (const match of text.matchAll(MARKDOWN_INLINE)) {
        if (match.index > last) target.append(document.createTextNode(text.slice(last, match.index)));
        last = match.index + match[0].length;
        const [, escaped, code, bold, boldAlt, struck, italic, italicAlt, label, href, bareUrl] = match;
        if (escaped !== undefined) target.append(document.createTextNode(escaped));
        else if (code !== undefined) target.append(uiElement("code", "", code));
        else if (bold !== undefined || boldAlt !== undefined) {
            const strong = uiElement("strong"); appendMarkdownInline(strong, bold ?? boldAlt, allowLinks); target.append(strong);
        } else if (struck !== undefined) {
            const del = uiElement("del"); appendMarkdownInline(del, struck, allowLinks); target.append(del);
        } else if (italic !== undefined || italicAlt !== undefined) {
            const em = uiElement("em"); appendMarkdownInline(em, italic ?? italicAlt, allowLinks); target.append(em);
        } else if (label !== undefined) {
            if (allowLinks && MARKDOWN_SAFE_HREF.test(href)) {
                const link = markdownLink(href); appendMarkdownInline(link, label, false); target.append(link);
            } else appendMarkdownInline(target, label, allowLinks);
        } else target.append(allowLinks ? markdownLink(bareUrl, bareUrl) : document.createTextNode(bareUrl));
    }
    if (last < text.length) target.append(document.createTextNode(text.slice(last)));
}

function markdownLink(href, text = "") {
    // Only http(s) and mailto become links; Electron hands them to the default browser.
    const link = uiElement("a", "", text);
    link.setAttribute("href", href);
    link.setAttribute("target", "_blank");
    link.setAttribute("rel", "noopener noreferrer");
    return link;
}

function splitMarkdownRow(row) {
    let cells = row.trim();
    if (cells.startsWith("|")) cells = cells.slice(1);
    if (cells.endsWith("|") && !cells.endsWith("\\|")) cells = cells.slice(0, -1);
    return cells.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
}

function isMarkdownTableStart(lines, index) {
    const header = lines[index];
    const divider = lines[index + 1];
    if (!header?.includes("|") || divider === undefined || !divider.includes("-") || !MARKDOWN_TABLE_DIVIDER.test(divider)) return false;
    return splitMarkdownRow(header).length === splitMarkdownRow(divider).length;
}

function startsMarkdownBlock(lines, index) {
    const line = lines[index];
    return MARKDOWN_FENCE.test(line) || MARKDOWN_HEADING.test(line) || MARKDOWN_RULE.test(line)
        || MARKDOWN_QUOTE.test(line) || MARKDOWN_LIST_ITEM.test(line) || isMarkdownTableStart(lines, index);
}

function appendMarkdownCode(container, language, text) {
    const block = uiElement("div", "code-block");
    const bar = uiElement("div", "code-toolbar");
    const copy = uiElement("button", "text-btn", "Copy");
    copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(text); copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy"; }, 2000); }
        catch (_) { notifyConsole("Copy failed. Select the code to copy it manually."); }
    });
    bar.append(uiElement("span", "", language || "code"), copy);
    const pre = uiElement("pre"); pre.append(uiElement("code", "", text));
    block.append(bar, pre); container.append(block);
}

function appendMarkdownTable(container, lines, index) {
    const alignments = splitMarkdownRow(lines[index + 1]).map((cell) => (
        cell.startsWith(":") && cell.endsWith(":") ? "center" : cell.endsWith(":") ? "right" : cell.startsWith(":") ? "left" : ""
    ));
    const row = (cells, tag) => {
        const tr = uiElement("tr");
        alignments.forEach((align, column) => {
            const cell = uiElement(tag);
            if (align) cell.style.textAlign = align;
            appendMarkdownInline(cell, cells[column] || "");
            tr.append(cell);
        });
        return tr;
    };
    const thead = uiElement("thead"); thead.append(row(splitMarkdownRow(lines[index]), "th"));
    const tbody = uiElement("tbody");
    let next = index + 2;
    while (next < lines.length && lines[next].includes("|") && lines[next].trim()) {
        tbody.append(row(splitMarkdownRow(lines[next]), "td"));
        next += 1;
    }
    const table = uiElement("table"); table.append(thead, tbody);
    const wrap = uiElement("div", "table-wrap"); wrap.append(table);
    container.append(wrap);
    return next;
}

function buildMarkdownList(items, from) {
    // Items deeper than the first one nest inside the previous item; a shallower item or a
    // different list type at the same depth ends this list.
    const first = items[from];
    const list = uiElement(first.ordered ? "ol" : "ul");
    if (first.ordered && first.number !== 1) list.setAttribute("start", String(first.number));
    let index = from;
    while (index < items.length && items[index].indent >= first.indent) {
        const item = items[index];
        if (item.indent > first.indent) {
            let parent = list.lastElementChild;
            if (!parent) { parent = uiElement("li"); list.append(parent); }
            const [nested, next] = buildMarkdownList(items, index);
            parent.append(nested);
            index = next;
            continue;
        }
        if (item.ordered !== first.ordered) break;
        const li = uiElement("li");
        appendMarkdownInline(li, item.text);
        list.append(li);
        index += 1;
    }
    return [list, index];
}

function appendMarkdownList(container, lines, index) {
    const items = [];
    let next = index;
    while (next < lines.length) {
        const line = lines[next].replace(/\t/g, "    ");
        const marker = line.match(MARKDOWN_LIST_ITEM);
        if (marker) {
            const ordered = /\d/.test(marker[2]);
            items.push({ indent: marker[1].length, ordered, number: ordered ? parseInt(marker[2], 10) : 0, text: marker[3] });
            next += 1;
        } else if (line.trim() && items.length && !startsMarkdownBlock(lines, next)) {
            items[items.length - 1].text += `\n${line.trim()}`;
            next += 1;
        } else if (!line.trim() && items.length && /^(?:\s*([-*+]|\d{1,9}[.)])\s|\s{2,}\S)/.test(lines[next + 1] || "")) {
            next += 1;
        } else break;
    }
    let item = 0;
    while (item < items.length) {
        const [list, after] = buildMarkdownList(items, item);
        container.append(list);
        item = after;
    }
    return next;
}

function appendMarkdown(container, source) {
    // Safe Markdown subset built only from DOM nodes and text. Model HTML never becomes markup.
    const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    let index = 0;
    while (index < lines.length) {
        const line = lines[index];
        if (!line.trim()) { index += 1; continue; }
        const fence = line.match(MARKDOWN_FENCE);
        if (fence) {
            // An unclosed fence (a reply still streaming) runs to the end of the text.
            const body = [];
            index += 1;
            while (index < lines.length && !(lines[index].trim().startsWith(fence[1][0].repeat(fence[1].length)) && !lines[index].trim().replace(/[`~]/g, ""))) {
                body.push(lines[index]);
                index += 1;
            }
            index += 1;
            appendMarkdownCode(container, fence[2], body.join("\n"));
            continue;
        }
        const heading = line.match(MARKDOWN_HEADING);
        if (heading) {
            const title = uiElement(`h${Math.min(heading[1].length + 1, 6)}`);
            appendMarkdownInline(title, heading[2]);
            container.append(title);
            index += 1;
            continue;
        }
        if (MARKDOWN_RULE.test(line)) { container.append(uiElement("hr")); index += 1; continue; }
        if (MARKDOWN_QUOTE.test(line)) {
            const quoted = [];
            while (index < lines.length && MARKDOWN_QUOTE.test(lines[index])) {
                quoted.push(lines[index].replace(MARKDOWN_QUOTE, ""));
                index += 1;
            }
            const quote = uiElement("blockquote");
            appendMarkdown(quote, quoted.join("\n"));
            container.append(quote);
            continue;
        }
        if (isMarkdownTableStart(lines, index)) { index = appendMarkdownTable(container, lines, index); continue; }
        if (MARKDOWN_LIST_ITEM.test(line)) { index = appendMarkdownList(container, lines, index); continue; }
        const paragraph = [line];
        index += 1;
        while (index < lines.length && lines[index].trim() && !startsMarkdownBlock(lines, index)) {
            paragraph.push(lines[index]);
            index += 1;
        }
        const p = uiElement("p");
        appendMarkdownInline(p, paragraph.map((value) => value.trimStart()).join("\n"));
        container.append(p);
    }
}

function initConsoleShell() {
    const narrow = window.matchMedia("(max-width: 1024px)");
    historyVisible = !narrow.matches;
    document.querySelector(".skip-link").addEventListener("click", skipToConsoleContent);
    document.querySelectorAll(".app-nav [data-view]").forEach((button) => button.addEventListener("click", () => setConsoleView(button.dataset.view)));
    document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => document.getElementById(button.dataset.closeDialog).close()));
    document.getElementById("historyToggle").addEventListener("click", () => showHistory());
    document.getElementById("inspectorToggle").addEventListener("click", () => showInspector(inspectorTab, document.getElementById("inspectorPanel").hidden));
    document.getElementById("inspectorClose").addEventListener("click", () => { showInspector(inspectorTab, false); document.getElementById("inspectorToggle").focus(); });
    document.querySelectorAll("[data-inspector]").forEach((button) => button.addEventListener("click", () => showInspector(button.dataset.inspector)));
    document.getElementById("inspectorNotepad").append(notepadPanel);
    document.getElementById("runtimeSelectors").append(document.querySelector(".model-selector-row"), modelSelect);
    document.getElementById("templateControls").append(document.querySelector(".template-row"));
    document.getElementById("suggestionSettings").append(resetSuggestionsBtn);
    const modes = uiElement("section", "starting-modes"); modes.id = "startingModes";
    modes.append(uiElement("h3", "", "Choose a starting mode"), uiElement("p", "", "Sets instructions for this chat. Nothing is sent until you press Send."));
    const grid = uiElement("div", "mode-grid");
    for (const [mode, label, description] of [["general", "General", "Global instructions only."], ["code_review", "Code Review", "Review a diff or file you attach."], ["brainstorm", "Brainstorm", "Open-ended ideas, wider range."]]) {
        const button = uiElement("button", "mode-card"); button.dataset.startMode = mode;
        button.append(uiElement("strong", "", label), uiElement("span", "", description));
        button.addEventListener("click", () => { templateSelect.value = mode; updateAnticipationPreferences(); updateContextStrip(); }); grid.append(button);
    }
    modes.append(grid); document.querySelector(".chat-footer").append(modes);
    // Short windows retain every context control behind a native disclosure.
    const contextDisclosure = document.createElement("details");
    contextDisclosure.id = "contextDisclosure";
    contextDisclosure.append(uiElement("summary", "", "Context and mode"));
    const contextBody = uiElement("div", "compact-context-body");
    const contextStrip = document.getElementById("contextStrip");
    contextStrip.before(contextDisclosure);
    contextBody.append(document.getElementById("suggestedNext"), contextStrip);
    const closeContext = uiElement("button", "text-btn", "Done");
    closeContext.id = "closeCompactContext";
    closeContext.type = "button";
    closeContext.addEventListener("click", () => { contextDisclosure.open = false; contextDisclosure.querySelector("summary").focus(); });
    contextBody.append(closeContext);
    contextDisclosure.append(contextBody);
    const shortWindow = window.matchMedia("(max-height: 500px)");
    const fitContext = () => { contextDisclosure.open = !shortWindow.matches; };
    shortWindow.addEventListener("change", fitContext);
    fitContext();
    document.getElementById("topbarRuntime").addEventListener("click", () => document.getElementById("runtimeDialog").showModal());
    document.getElementById("searchToggle").addEventListener("click", () => { showHistory(true); renderSearchBox(); document.querySelector("#globalSearchBox input")?.focus(); });
    document.getElementById("inspectorInstructions").addEventListener("click", openInstructionsDialog);
    document.getElementById("inspectorPreview").addEventListener("click", previewInspector);
    document.getElementById("applyChatProject").addEventListener("click", applyChatProject);
    document.getElementById("newProjectButton").addEventListener("click", async () => { await createProjectFlow(); renderProjectCards(); });
    document.getElementById("settingsTheme").addEventListener("click", () => themeToggle.click());
    document.getElementById("settingsCredential").addEventListener("click", () => credentialBtn.click());
    document.getElementById("settingsCredential").hidden = Boolean(window.__DAVE_DESKTOP__);
    document.getElementById("consoleNotice").querySelector("button").addEventListener("click", () => { document.getElementById("consoleNotice").hidden = true; });
    refreshNodesBtn.onclick = refreshCluster;
    narrow.addEventListener("change", () => showHistory(!narrow.matches, false));
    document.addEventListener("keydown", (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); document.getElementById("searchToggle").click(); }
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n") { event.preventDefault(); setConsoleView("chat"); createNewConversation(); }
        if (event.key === "Escape" && consoleView === "chat" && !document.querySelector("dialog[open]")) { showInspector(inspectorTab, false); if (narrow.matches) showHistory(false); }
    });
    approvalTimer = setInterval(tickApprovalClock, 1000);
    setInterval(() => { if (consoleView === "cluster" && !document.hidden && (apiKey || window.__DAVE_DESKTOP__)) refreshCluster(); }, 60000);
    showHistory(historyVisible);
    initConsolePanelResizing();
    if (location.hash === "#cluster") setConsoleView("cluster");
}
