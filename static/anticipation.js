(function (root, factory) {
    const api = factory();
    if (typeof module !== "undefined" && module.exports) module.exports = api;
    if (root) root.DaveAnticipation = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
    "use strict";

    const VERSION = 1;
    const MAX_PREDICTIONS = 3;
    const MAX_COUNTER = 99;
    const MAX_MAP_ENTRIES = 25;
    const DISMISSAL_TTL_MS = 30 * 24 * 60 * 60 * 1000;
    const TEMPLATES = new Set(["general", "code_review", "brainstorm"]);

    function isoNow(now) {
        const date = new Date(now === undefined ? Date.now() : now);
        return Number.isNaN(date.getTime()) ? new Date().toISOString() : date.toISOString();
    }

    function stableId(value) {
        if (typeof value !== "string") return null;
        const normalized = value.trim();
        if (!normalized || normalized.length > 200 || /[\u0000-\u001f\u007f]/.test(normalized)) return null;
        return normalized;
    }

    function cappedCount(value) {
        const parsed = Number.parseInt(value, 10);
        if (!Number.isFinite(parsed) || parsed < 0) return 0;
        return Math.min(MAX_COUNTER, parsed);
    }

    function limitedEntries(value) {
        if (!value || typeof value !== "object" || Array.isArray(value)) return [];
        return Object.entries(value).slice(0, MAX_MAP_ENTRIES);
    }

    function normalizeCountMap(value) {
        const result = {};
        limitedEntries(value).forEach(([rawKey, rawValue]) => {
            const key = stableId(rawKey);
            if (key) result[key] = cappedCount(rawValue);
        });
        return result;
    }

    function createRecord(now) {
        return {
            version: VERSION,
            updatedAt: isoNow(now),
            preferences: {
                lastTemplate: "general",
                lastProjectId: null,
                lastNodeId: null,
                lastModelByNode: {},
                projectDefaults: {}
            },
            usage: {
                templateCounts: {},
                nextActionCounts: {},
                supportCountsByProject: {}
            },
            dismissals: {}
        };
    }

    function normalizeRecord(value, now) {
        const normalized = createRecord(now);
        if (!value || typeof value !== "object" || value.version !== VERSION) return normalized;

        const preferences = value.preferences && typeof value.preferences === "object" ? value.preferences : {};
        const template = stableId(preferences.lastTemplate);
        normalized.preferences.lastTemplate = template && TEMPLATES.has(template) ? template : "general";
        normalized.preferences.lastProjectId = stableId(preferences.lastProjectId);
        normalized.preferences.lastNodeId = stableId(preferences.lastNodeId);

        limitedEntries(preferences.lastModelByNode).forEach(([rawNodeId, rawModelId]) => {
            const nodeId = stableId(rawNodeId);
            const modelId = stableId(rawModelId);
            if (nodeId && modelId) normalized.preferences.lastModelByNode[nodeId] = modelId;
        });

        limitedEntries(preferences.projectDefaults).forEach(([rawProjectId, rawDefault]) => {
            const projectId = stableId(rawProjectId);
            if (!projectId || !rawDefault || typeof rawDefault !== "object") return;
            const defaultTemplate = stableId(rawDefault.template);
            normalized.preferences.projectDefaults[projectId] = {
                template: defaultTemplate && TEMPLATES.has(defaultTemplate) ? defaultTemplate : "general",
                nodeId: stableId(rawDefault.nodeId),
                modelId: stableId(rawDefault.modelId),
                supportMode: rawDefault.supportMode === true
            };
        });

        const usage = value.usage && typeof value.usage === "object" ? value.usage : {};
        normalized.usage.templateCounts = normalizeCountMap(usage.templateCounts);
        normalized.usage.nextActionCounts = normalizeCountMap(usage.nextActionCounts);
        normalized.usage.supportCountsByProject = normalizeCountMap(usage.supportCountsByProject);

        const nowMs = new Date(now === undefined ? Date.now() : now).getTime();
        limitedEntries(value.dismissals).forEach(([rawId, rawDismissal]) => {
            const id = stableId(rawId);
            if (!id || !rawDismissal || typeof rawDismissal !== "object") return;
            const expiresMs = new Date(rawDismissal.expiresAt).getTime();
            if (!Number.isFinite(expiresMs) || expiresMs <= nowMs) return;
            normalized.dismissals[id] = {
                count: cappedCount(rawDismissal.count),
                expiresAt: new Date(Math.min(expiresMs, nowMs + DISMISSAL_TTL_MS)).toISOString()
            };
        });

        normalized.updatedAt = isoNow(value.updatedAt || now);
        return normalized;
    }

    function updatePreferences(record, patch, now) {
        const next = normalizeRecord(record, now);
        const safePatch = patch && typeof patch === "object" ? patch : {};
        const template = stableId(safePatch.lastTemplate);
        const projectId = safePatch.lastProjectId === null ? null : stableId(safePatch.lastProjectId);
        const nodeId = safePatch.lastNodeId === null ? null : stableId(safePatch.lastNodeId);
        const modelId = stableId(safePatch.lastModelId);

        if (template && TEMPLATES.has(template)) next.preferences.lastTemplate = template;
        if (safePatch.lastProjectId === null || projectId) next.preferences.lastProjectId = projectId;
        if (safePatch.lastNodeId === null || nodeId) next.preferences.lastNodeId = nodeId;
        if (nodeId && modelId) next.preferences.lastModelByNode[nodeId] = modelId;
        next.updatedAt = isoNow(now);
        return normalizeRecord(next, now);
    }

    function incrementUsage(record, bucket, key, now) {
        const next = normalizeRecord(record, now);
        const safeKey = stableId(key);
        if (!safeKey || !Object.prototype.hasOwnProperty.call(next.usage, bucket)) return next;
        next.usage[bucket][safeKey] = Math.min(MAX_COUNTER, (next.usage[bucket][safeKey] || 0) + 1);
        next.updatedAt = isoNow(now);
        return normalizeRecord(next, now);
    }

    function dismissPrediction(record, predictionId, now) {
        const next = normalizeRecord(record, now);
        const id = stableId(predictionId);
        if (!id) return next;
        const nowMs = new Date(now === undefined ? Date.now() : now).getTime();
        next.dismissals[id] = {
            count: Math.min(MAX_COUNTER, ((next.dismissals[id] || {}).count || 0) + 1),
            expiresAt: new Date(nowMs + DISMISSAL_TTL_MS).toISOString()
        };
        next.updatedAt = new Date(nowMs).toISOString();
        return normalizeRecord(next, nowMs);
    }

    function setProjectDefault(record, projectId, value, now) {
        const next = normalizeRecord(record, now);
        const id = stableId(projectId);
        if (!id || !value || typeof value !== "object") return next;
        const template = stableId(value.template);
        next.preferences.projectDefaults[id] = {
            template: template && TEMPLATES.has(template) ? template : "general",
            nodeId: stableId(value.nodeId),
            modelId: stableId(value.modelId),
            supportMode: value.supportMode === true
        };
        next.updatedAt = isoNow(now);
        return normalizeRecord(next, now);
    }

    function hasCodeSignal(text) {
        const value = String(text || "");
        return value.includes("```")
            || /\b(?:Traceback|TypeError|ReferenceError|SyntaxError|stack trace)\b/i.test(value)
            || /\b[\w.-]+\.(?:js|jsx|ts|tsx|py|go|rs|java|c|cc|cpp|h|hpp|cs|rb|php|swift|kt|kts|sql|sh|html|css|json|ya?ml)\b/i.test(value);
    }

    function hasIdeationSignal(text) {
        return /\b(?:brainstorm|ideas?|options?|alternatives?|possibilities|concepts?)\b/i.test(String(text || ""));
    }

    function getPredictions(context) {
        const safe = context && typeof context === "object" ? context : {};
        const dismissed = new Set(Array.isArray(safe.dismissedPredictionIds) ? safe.dismissedPredictionIds : []);
        const predictions = [];
        const push = (prediction) => {
            if (!prediction || dismissed.has(prediction.id) || predictions.some((item) => item.id === prediction.id)) return;
            if (predictions.length < MAX_PREDICTIONS) predictions.push(prediction);
        };

        if (!safe.activeConversationId && safe.lastConversation && stableId(safe.lastConversation.id)) {
            push({
                id: "continue_last",
                label: `Continue: ${String(safe.lastConversation.title || "Last chat").slice(0, 48)}`,
                reason: "Your last conversation is available.",
                kind: "continue_conversation",
                payload: { conversationId: safe.lastConversation.id }
            });
        }

        const composerEmpty = !String(safe.composer || "").trim();
        if (composerEmpty && safe.attachmentKind === "code") {
            push({
                id: "review_code_attachment",
                label: "Review attached code",
                reason: "A code file is attached and the composer is empty.",
                kind: "prefill",
                payload: { text: "Review this code for correctness, security, and maintainability." }
            });
        } else if (composerEmpty && safe.attachmentKind === "file") {
            push({
                id: "review_attachment",
                label: "Review attached file",
                reason: "A file is attached and the composer is empty.",
                kind: "prefill",
                payload: { text: "Summarize the attached file and extract decisions and actions." }
            });
        } else if (composerEmpty && safe.attachmentKind === "image") {
            push({
                id: "describe_image",
                label: "Describe attached image",
                reason: "An image is attached and the composer is empty.",
                kind: "prefill",
                payload: { text: "Describe this image and identify anything unusual." }
            });
        } else if (composerEmpty && safe.attachmentKind === "audio") {
            push({
                id: "review_audio",
                label: "Review attached audio",
                reason: "Audio is selected and the composer is empty.",
                kind: "prefill",
                payload: { text: "Transcribe this audio and extract decisions and actions." }
            });
        }

        if (
            safe.attachmentKind === "image"
            && safe.selectedModel
            && safe.selectedModel.vision === false
            && stableId(safe.recommendedVisionModelId)
        ) {
            push({
                id: "vision_model_suggestion",
                label: "Use vision model",
                reason: "Recommended for this image.",
                kind: "select_model",
                payload: { modelId: safe.recommendedVisionModelId }
            });
        }

        if (!safe.hasMessages && hasCodeSignal(safe.composer) && safe.template !== "code_review") {
            push({
                id: "code_review_template",
                label: "Use Code Review",
                reason: "The prompt looks like code or an error trace.",
                kind: "select_template",
                payload: { template: "code_review" }
            });
        } else if (!safe.hasMessages && hasIdeationSignal(safe.composer) && safe.template !== "brainstorm") {
            push({
                id: "brainstorm_template",
                label: "Use Brainstorm",
                reason: "The prompt asks for ideas or options.",
                kind: "select_template",
                payload: { template: "brainstorm" }
            });
        }

        const response = safe.lastAssistant && typeof safe.lastAssistant === "object" ? safe.lastAssistant : {};
        if (response.hasCode) {
            push({
                id: "add_tests",
                label: "Add tests",
                reason: "The last answer contains code.",
                kind: "prefill",
                payload: { text: "Add focused tests for the implementation above, including edge cases." }
            });
        }
        if (response.isError) {
            push({
                id: "retry_concise",
                label: "Retry concise",
                reason: "The last response reported an error.",
                kind: "prefill",
                payload: { text: "Retry with a concise diagnosis and the smallest safe fix." }
            });
        }
        if (response.isLong) {
            push({
                id: "summarize_response",
                label: "Summarize",
                reason: "The last answer is long.",
                kind: "prefill",
                payload: { text: "Summarize the answer above into the key decisions, risks, and next actions." }
            });
        }
        if (response.hasList) {
            push({
                id: "make_checklist",
                label: "Turn into checklist",
                reason: "The last answer contains a structured list.",
                kind: "prefill",
                payload: { text: "Turn the answer above into a concise, ordered execution checklist." }
            });
        }

        if (
            stableId(safe.projectId)
            && cappedCount(safe.projectSupportCount) >= 3
            && safe.projectSupportDefault !== true
        ) {
            push({
                id: "remember_project_support",
                label: "Remember Support",
                reason: "Support was explicitly selected repeatedly for this project.",
                kind: "remember_support",
                payload: { projectId: safe.projectId }
            });
        }

        return predictions;
    }

    return {
        VERSION,
        MAX_COUNTER,
        DISMISSAL_TTL_MS,
        createRecord,
        normalizeRecord,
        updatePreferences,
        incrementUsage,
        dismissPrediction,
        setProjectDefault,
        getPredictions
    };
});
