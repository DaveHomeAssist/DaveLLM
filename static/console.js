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

function setConsoleView(view) {
    consoleView = ["chat", "projects", "cluster", "settings"].includes(view) ? view : "chat";
    document.body.dataset.view = consoleView;
    for (const name of ["chat", "projects", "cluster", "settings"]) {
        document.getElementById(`${name}View`).hidden = name !== consoleView;
    }
    document.querySelectorAll("[data-view]").forEach((button) => {
        if (button.tagName !== "BUTTON") return;
        if (button.dataset.view === consoleView) button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
    });
    if (consoleView === "projects") renderProjectCards();
    if (consoleView === "cluster") refreshCluster();
    updateConsoleContext();
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
    runToolsBtn.disabled = approvalBusy;
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

function appendMarkdown(container, source) {
    // Safe, deliberately small Markdown subset. Model HTML never becomes markup.
    const inline = (target, text) => {
        for (const part of text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g)) {
            if (part.startsWith("`") && part.endsWith("`")) target.append(uiElement("code", "", part.slice(1, -1)));
            else if (part.startsWith("**") && part.endsWith("**")) target.append(uiElement("strong", "", part.slice(2, -2)));
            else target.append(document.createTextNode(part));
        }
    };
    const parts = String(source || "").split("```");
    parts.forEach((part, index) => {
        if (index % 2) {
            const match = part.match(/^([\w+-]*)\n/);
            const text = match ? part.slice(match[0].length).replace(/\n$/, "") : part;
            const block = uiElement("div", "code-block");
            const bar = uiElement("div", "code-toolbar");
            const copy = uiElement("button", "text-btn", "Copy");
            copy.addEventListener("click", async () => {
                try { await navigator.clipboard.writeText(text); copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy"; }, 2000); }
                catch (_) { notifyConsole("Copy failed. Select the code to copy it manually."); }
            });
            bar.append(uiElement("span", "", match?.[1] || "code"), copy);
            const pre = uiElement("pre"); pre.append(uiElement("code", "", text));
            block.append(bar, pre); container.append(block);
        } else {
            for (const paragraph of part.split(/\n\s*\n/).filter((value) => value.trim())) {
                const heading = paragraph.match(/^(#{1,3})\s+(.+)$/);
                const p = uiElement(heading ? `h${heading[1].length + 1}` : "p");
                inline(p, heading ? heading[2] : paragraph); container.append(p);
            }
        }
    });
}

function initConsoleShell() {
    const narrow = window.matchMedia("(max-width: 1024px)");
    historyVisible = !narrow.matches;
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
    if (location.hash === "#cluster") setConsoleView("cluster");
}
