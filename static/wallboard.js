/* A local projection consumer. It never contacts a router or a model. */
(() => {
    let lastGood = null, failures = 0, fetching = false;
    const source = document.getElementById("source");
    const allowedStates = ["healthy", "degraded", "outage", "recovered"];
    function valid(payload) {
        const record = (r) => r && Number.isFinite(Date.parse(r.checked_at)) && allowedStates.includes(r.overall)
            && Array.isArray(r.targets) && r.targets.length === 4
            && r.targets.every(t => t && ["Local", "Dominic", "Walter", "Duncan"].includes(t.name)
                && ["healthy", "down", "recovered", "unresponsive"].includes(t.status))
            && new Set(r.targets.map(t => t.name)).size === r.targets.length
            && (r.overall !== "healthy" || r.targets.every(t => t.status === "healthy"));
        return payload && payload.version === 1 && record(payload.current)
            && Array.isArray(payload.history) && payload.history.length <= 168 && payload.history.every(record);
    }
    function render() {
        globalThis.DaveOnCall.render({status: "fulfilled", value: lastGood});
        const rows = document.getElementById("historyRows");
        rows.replaceChildren();
        for (const item of (lastGood?.history || []).slice().reverse()) {
            const row = document.createElement("li");
            row.textContent = `${new Date(item.checked_at).toLocaleString()}: ${item.overall}`;
            rows.append(row);
        }
    }
    async function refresh() {
        if (fetching) return;
        fetching = true;
        try {
            const response = await fetch(`wallboard-status.json?t=${Date.now()}`, {cache: "no-store", signal: AbortSignal.timeout(5000)});
            if (!response.ok) throw new Error("status unavailable");
            const text = await response.text();
            if (text.length > 1024 * 1024) throw new Error("status exceeds limit");
            const payload = JSON.parse(text);
            if (!valid(payload)) throw new Error("invalid projection");
            lastGood = payload; failures = 0;
            source.textContent = "Local status source connected";
            source.dataset.warning = "false";
        } catch (_) {
            failures += 1;
            source.textContent = lastGood ? "Status refresh failed. Showing the last recorded check." : "Local status source unavailable. No check received.";
            source.dataset.warning = String(failures >= 2 || !lastGood);
        } finally { fetching = false; render(); }
    }
    const theme = document.getElementById("theme");
    function setTheme(value) {
        document.documentElement.dataset.theme = value === "dark" ? "dark" : "light";
        theme.textContent = value === "dark" ? "Light theme" : "Dark theme";
    }
    try { setTheme(localStorage.getItem("davellm-wallboard-theme")); } catch (_) { setTheme("light"); }
    theme.addEventListener("click", () => {
        const value = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
        setTheme(value); try { localStorage.setItem("davellm-wallboard-theme", value); } catch (_) { /* Theme remains usable without storage. */ }
    });
    document.getElementById("refresh").addEventListener("click", refresh);
    document.getElementById("endpoints").addEventListener("click", (event) => {
        const visible = document.body.classList.toggle("show-endpoints");
        event.currentTarget.setAttribute("aria-pressed", String(visible));
        event.currentTarget.textContent = visible ? "Hide endpoints" : "Show endpoints";
    });
    document.getElementById("historyToggle").addEventListener("click", (event) => {
        const panel = document.getElementById("historyPanel"); panel.hidden = !panel.hidden;
        event.currentTarget.setAttribute("aria-expanded", String(!panel.hidden));
    });
    refresh(); setInterval(refresh, 15000); setInterval(render, 1000);
})();
