/* Shared on-call rendering for Cluster and the read-only wallboard. */
(() => {
const uiElement = (tag, className = "", text = "") => { const e = document.createElement(tag); e.className = className; e.textContent = text; return e; };
function monitorAge(checkedAt) {
    const timestamp = Date.parse(checkedAt || "");
    if (!Number.isFinite(timestamp) || timestamp > Date.now() + 60000) return { label: "Time unavailable", stale: true };
    const minutes = Math.max(0, (Date.now() - timestamp) / 60000);
    if (minutes < 2) return { label: "Updated just now", stale: false };
    if (minutes < 60) return { label: `Updated ${Math.floor(minutes)} minutes ago`, stale: false };
    const hours = Math.round(minutes / 60);
    return { label: `Updated ${hours} ${hours === 1 ? "hour" : "hours"} ago`, stale: minutes > 120 };
}

function render(result) {
    const stateLabel = document.getElementById("onCallState");
    const freshness = document.getElementById("onCallFreshness");
    const summary = document.getElementById("onCallSummary");
    const targets = document.getElementById("onCallTargets");
    const recovery = document.getElementById("onCallRecovery");
    const history = document.getElementById("onCallHistory");
    targets.replaceChildren(); history.replaceChildren(); recovery.replaceChildren(); recovery.hidden = true;
    const payload = result?.status === "fulfilled" ? result.value : null;
    const current = payload?.current;
    if (!current) {
        stateLabel.className = "oncall-state oncall-state-unknown";
        stateLabel.textContent = "Waiting";
        freshness.textContent = payload?.error || result?.reason?.message || "Hourly status unavailable.";
        summary.textContent = "The next scheduled check will publish here.";
        return;
    }
    const age = monitorAge(current.checked_at);
    const delayed = !age.stale && Date.now() - Date.parse(current.checked_at) > 75 * 60000;
    const state = age.stale || delayed ? "degraded" : String(current.overall || "unknown");
    stateLabel.className = `oncall-state oncall-state-${state}`;
    stateLabel.textContent = age.stale ? "Stale" : delayed ? "Delayed" : state;
    const checkedAt = Date.parse(current.checked_at || "");
    freshness.textContent = Number.isFinite(checkedAt)
        ? `${age.label} · ${new Date(checkedAt).toLocaleString()}`
        : age.label;
    summary.textContent = String(current.summary || "Hourly check recorded.");
    for (const target of current.targets || []) {
        const row = uiElement("div", "oncall-target");
        const reading = Number.isFinite(target.latency_ms)
            ? `${Number(target.latency_ms).toFixed(1)} ms · HTTP ${target.http_status || "—"}`
            : `${String(target.status || "unknown").toUpperCase()} · HTTP ${target.http_status || "—"}`;
        row.append(
            uiElement("strong", "", String(target.name || "Unnamed target")),
            uiElement("code", "", String(target.endpoint || "Endpoint unavailable")),
            uiElement("span", `oncall-target-reading${target.status === "down" || target.status === "unresponsive" ? " is-down" : ""}`, reading),
        );
        row.append(uiElement("span", "oncall-target-state", String(target.status || "unknown").toUpperCase()));
        targets.append(row);
    }
    if (!targets.childElementCount) targets.append(uiElement("p", "", "No target records were included."));
    if (current.recovery) {
        recovery.hidden = false;
        const elapsed = Number.isFinite(current.recovery.elapsed_seconds) ? ` · ${current.recovery.elapsed_seconds}s` : "";
        recovery.textContent = `${current.recovery.node || "Recovery"}${elapsed}: ${current.recovery.steps || "Recovery details unavailable."}`;
    }
    for (const item of (payload.history || []).slice(-24)) {
        const bar = uiElement("span", "oncall-history-bar");
        const historyState = ["healthy", "degraded", "outage", "recovered"].includes(item.overall) ? item.overall : "unknown";
        bar.dataset.state = historyState;
        bar.title = `${item.checked_at || "Time unavailable"}: ${item.summary || item.overall || "Unknown"}`;
        history.append(bar);
    }
    if (!history.childElementCount) history.textContent = "No history yet.";
}

globalThis.DaveOnCall = { render, age: monitorAge };
})();
