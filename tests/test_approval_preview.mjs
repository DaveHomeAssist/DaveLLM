import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

// A minimal DOM: elements record text and children. There is no innerHTML, so
// any attempt to render markup would throw instead of passing silently.
class FakeElement {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.textContent = "";
        this.className = "";
    }
    appendChild(child) { this.children.push(child); return child; }
    append(...children) { this.children.push(...children); }
}

function loadPreview() {
    const source = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
    const start = source.indexOf("function approvalPreview(pending) {");
    const end = source.indexOf("\nfunction renderToolRun(run)", start);
    assert.ok(start >= 0 && end > start);
    const context = { document: { createElement: (tag) => new FakeElement(tag) } };
    vm.createContext(context);
    vm.runInContext(`${source.slice(start, end)}\nglobalThis.approvalPreview = approvalPreview;`, context);
    return context.approvalPreview;
}

const summary = (element) => element.children.map((child) => [child.tagName, child.className, child.textContent]);

test("a pending file.edit shows the path, count, and exact before and after text", () => {
    const approvalPreview = loadPreview();
    const hostile = "<img src=x onerror=alert(1)>\n</pre><script>alert(2)</script>";
    const preview = approvalPreview({
        tool_name: "file.edit",
        arguments: { path: "notes/plan.md", old_text: hostile, new_text: "status: final\n", expected_count: 3 },
    });
    assert.deepEqual(summary(preview), [
        ["P", "edit-preview-target", "Edit notes/plan.md · replaces 3 occurrences"],
        ["H5", "", "Before"],
        ["PRE", "edit-preview-before", hostile],
        ["H5", "", "After"],
        ["PRE", "edit-preview-after", "status: final\n"],
    ]);
    assert.equal(preview.className, "edit-preview");
    assert.ok(preview.children.every((child) => child.children.length === 0));
});

test("the default count reads as one occurrence and an empty replacement reads as a deletion", () => {
    const approvalPreview = loadPreview();
    const preview = approvalPreview({
        tool_name: "file.edit", arguments: { path: "a.txt", old_text: "remove me", new_text: "" },
    });
    assert.deepEqual(summary(preview), [
        ["P", "edit-preview-target", "Edit a.txt · replaces 1 occurrence"],
        ["H5", "", "Before"],
        ["PRE", "edit-preview-before", "remove me"],
        ["H5", "", "After"],
        ["P", "edit-preview-empty", "Nothing: the text above is deleted."],
    ]);
});

test("other tools and malformed edit arguments keep the plain argument view", () => {
    const approvalPreview = loadPreview();
    assert.equal(approvalPreview({ tool_name: "file.write", arguments: { path: "a", content: "b" } }), null);
    assert.equal(approvalPreview({ tool_name: "file.edit", arguments: { path: "a", old_text: 1, new_text: "b" } }), null);
    assert.equal(approvalPreview({ tool_name: "file.edit", arguments: { old_text: "a", new_text: "b" } }), null);
    assert.equal(approvalPreview({ tool_name: "file.edit" }), null);
});
