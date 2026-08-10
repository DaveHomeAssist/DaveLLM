(function exposePromptContract(root, factory) {
    const contract = factory();
    if (typeof module === "object" && module.exports) module.exports = contract;
    if (root) root.DavePrompt = contract;
}(typeof window !== "undefined" ? window : globalThis, function createPromptContract() {
    function buildDisplayedPrompt(prompt, attachedFileText, supportEnabled) {
        const basePrompt = `${prompt || ""}${attachedFileText || ""}`;
        return supportEnabled ? `[SUPPORT] ${basePrompt}`.trimEnd() : basePrompt;
    }

    return { buildDisplayedPrompt };
}));
