// Backlog: "Prevent stale notepad loads from crossing project selections".
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");

function extract(name) {
    const start = source.search(new RegExp(`^(async )?function ${name}\\(`, "m"));
    assert.ok(start >= 0, `${name} is defined`);
    const end = source.indexOf("\n}\n", start);
    return source.slice(start, end + 2);
}

// loadProjectNotepad against stubs; each fetch waits until the test answers it.
function harness() {
    const pending = new Map();
    const context = {
        notepadLoadSequence: 0, notepadLoading: false, notepadLoadedProjectId: null,
        selectedProjectId: "", currentConversation: () => null,
        notepadInput: { value: "" }, notepadStatus: { textContent: "" }, notepadProjectName: { textContent: "" },
        projects: [{ project_id: "A", name: "Alpha" }, { project_id: "B", name: "Beta" }],
        routerEndpoint: (path) => path, authHeaders: () => ({}), encodeURIComponent,
        console: { error() {} },
        fetch: (url) => new Promise((resolve) => pending.set(url.split("/")[2], resolve)),
    };
    vm.createContext(context);
    vm.runInContext(`${extract("activeNotepadProjectId")}\n${extract("loadProjectNotepad")}\n`
        + "globalThis.loadProjectNotepad = loadProjectNotepad;", context);
    const answer = (projectId, content) => pending.get(projectId)({
        ok: true, json: async () => ({ content, updated_at: "t" }), text: async () => "",
    });
    const fail = (projectId) => pending.get(projectId)({ ok: false, status: 500, text: async () => "boom" });
    return { context, answer, fail };
}

test("a late answer for the previous project never replaces the current project's notes", async () => {
    const { context, answer } = harness();
    context.selectedProjectId = "A";
    const loadA = context.loadProjectNotepad("A");
    context.selectedProjectId = "B";  // fast switch before A answers
    context.notepadLoadedProjectId = null;
    const loadB = context.loadProjectNotepad("B");
    answer("B", "notes for B");
    await loadB;
    answer("A", "notes for A");
    await loadA;
    assert.equal(context.notepadInput.value, "notes for B");
    assert.equal(context.notepadLoadedProjectId, "B");  // autosave keeps writing to B
    assert.equal(context.notepadProjectName.textContent, "Beta");
    assert.equal(context.notepadLoading, false);
});

test("an older load finishing first does not end the newer load's loading state", async () => {
    const { context, answer } = harness();
    context.selectedProjectId = "A";
    const first = context.loadProjectNotepad("A");
    context.selectedProjectId = "B";
    const second = context.loadProjectNotepad("B");
    answer("A", "notes for A");
    await first;
    assert.equal(context.notepadLoading, true);  // B is still loading, so typing is not autosaved yet
    assert.equal(context.notepadLoadedProjectId, null);
    answer("B", "notes for B");
    await second;
    assert.equal(context.notepadLoading, false);
    assert.equal(context.notepadLoadedProjectId, "B");
});

test("a late failure for the previous project does not overwrite the current status", async () => {
    const { context, answer, fail } = harness();
    context.selectedProjectId = "A";
    const loadA = context.loadProjectNotepad("A");
    context.selectedProjectId = "B";
    const loadB = context.loadProjectNotepad("B");
    answer("B", "notes for B");
    await loadB;
    fail("A");
    await loadA;
    assert.equal(context.notepadStatus.textContent, "Saved");
});

test("a single load still fills the notepad for the active project", async () => {
    const { context, answer } = harness();
    context.selectedProjectId = "A";
    const load = context.loadProjectNotepad("A");
    answer("A", "notes for A");
    await load;
    assert.equal(context.notepadInput.value, "notes for A");
    assert.equal(context.notepadLoadedProjectId, "A");
});
