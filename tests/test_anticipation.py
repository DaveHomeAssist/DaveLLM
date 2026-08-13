import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ANTICIPATION = REPO / "static" / "anticipation.js"


def run_node(script: str) -> None:
    subprocess.run(["node", "-e", script], check=True, cwd=REPO)


def test_predictions_are_deterministic_local_and_capped():
    script = f"""
const assert = require('node:assert/strict');
const anticipation = require({str(ANTICIPATION)!r});
const context = {{
  activeConversationId: null,
  lastConversation: {{ id: 'conversation-1', title: 'Release review' }},
  composer: 'brainstorm options for fixing error.js\\n```js\\nthrow new Error()\\n```',
  attachmentKind: 'image',
  hasMessages: false,
  template: 'general',
  selectedModel: {{ id: 'text-model', vision: false }},
  recommendedVisionModelId: 'vision-model',
  lastAssistant: {{ hasCode: true, isError: true, isLong: true, hasList: true }},
  projectId: 'project-1',
  projectSupportCount: 3,
  projectSupportDefault: false,
  dismissedPredictionIds: []
}};
const before = JSON.stringify(context);
const first = anticipation.getPredictions(context);
const second = anticipation.getPredictions(context);
assert.deepEqual(first, second);
assert.equal(JSON.stringify(context), before);
assert.ok(first.length <= 3);
assert.deepEqual(first.map((item) => item.id), [
  'continue_last',
  'vision_model_suggestion',
  'code_review_template'
]);
assert.ok(first.every((item) => item.kind && item.label && item.reason));
"""
    run_node(script)


def test_prediction_signals_and_active_conversation_guards():
    script = f"""
const assert = require('node:assert/strict');
const anticipation = require({str(ANTICIPATION)!r});

const code = anticipation.getPredictions({{
  composer: 'Please inspect server.py and this Traceback',
  hasMessages: false,
  template: 'general'
}});
assert.ok(code.some((item) => item.id === 'code_review_template'));

const ideation = anticipation.getPredictions({{
  composer: 'Give me five brainstorm options',
  hasMessages: false,
  template: 'general'
}});
assert.ok(ideation.some((item) => item.id === 'brainstorm_template'));

const active = anticipation.getPredictions({{
  composer: 'Review app.js',
  hasMessages: true,
  template: 'general'
}});
assert.ok(!active.some((item) => item.kind === 'select_template'));

const attachment = anticipation.getPredictions({{
  composer: '',
  attachmentKind: 'code',
  hasMessages: false,
  template: 'general'
}});
assert.equal(attachment[0].payload.text, 'Review this code for correctness, security, and maintainability.');

const response = anticipation.getPredictions({{
  composer: '',
  hasMessages: true,
  template: 'general',
  lastAssistant: {{ hasCode: true, isError: false, isLong: true, hasList: true }}
}});
assert.deepEqual(response.map((item) => item.id), ['add_tests', 'summarize_response', 'make_checklist']);

const dismissed = anticipation.getPredictions({{
  composer: '',
  attachmentKind: 'file',
  dismissedPredictionIds: ['review_attachment']
}});
assert.ok(!dismissed.some((item) => item.id === 'review_attachment'));
"""
    run_node(script)


def test_anticipation_record_is_allowlisted_bounded_and_expiring():
    script = f"""
const assert = require('node:assert/strict');
const anticipation = require({str(ANTICIPATION)!r});
const now = Date.parse('2026-08-13T12:00:00Z');
const record = anticipation.normalizeRecord({{
  version: 1,
  updatedAt: '2026-08-13T11:00:00Z',
  prompt: 'private prompt',
  response: 'private response',
  attachmentName: 'secret.txt',
  apiKey: 'secret-key',
  nodeUrl: 'http://private-node',
  systemPrompt: 'hidden instructions',
  preferences: {{
    lastTemplate: 'code_review',
    lastProjectId: 'project-1',
    lastNodeId: 'node-1',
    lastModelByNode: {{ 'node-1': 'model-1' }},
    projectDefaults: {{
      'project-1': {{ template: 'code_review', nodeId: 'node-1', modelId: 'model-1', supportMode: true, prompt: 'drop me' }}
    }}
  }},
  usage: {{
    templateCounts: {{ code_review: 500 }},
    nextActionCounts: {{ add_tests: 4 }},
    supportCountsByProject: {{ 'project-1': 3 }}
  }},
  dismissals: {{
    expired: {{ count: 1, expiresAt: '2026-08-12T12:00:00Z' }},
    active: {{ count: 500, expiresAt: '2027-08-13T12:00:00Z' }}
  }}
}}, now);

const encoded = JSON.stringify(record);
for (const forbidden of ['private prompt', 'private response', 'secret.txt', 'secret-key', 'private-node', 'hidden instructions', 'drop me']) {{
  assert.ok(!encoded.includes(forbidden));
}}
assert.equal(record.usage.templateCounts.code_review, anticipation.MAX_COUNTER);
assert.equal(record.dismissals.active.count, anticipation.MAX_COUNTER);
assert.ok(!record.dismissals.expired);
assert.equal(Date.parse(record.dismissals.active.expiresAt), now + anticipation.DISMISSAL_TTL_MS);

const dismissed = anticipation.dismissPrediction(record, 'vision_model_suggestion', now);
assert.equal(dismissed.dismissals.vision_model_suggestion.count, 1);
assert.equal(Date.parse(dismissed.dismissals.vision_model_suggestion.expiresAt), now + anticipation.DISMISSAL_TTL_MS);
"""
    run_node(script)


def test_frontend_wires_manual_control_storage_and_mobile_accessibility():
    app_source = (REPO / "static" / "app.js").read_text()
    anticipation_source = ANTICIPATION.read_text()
    html_source = (REPO / "static" / "index.html").read_text()
    css_source = (REPO / "static" / "style.css").read_text()

    assert 'const ANTICIPATION_STORAGE_KEY = "davellm_anticipation_v1"' in app_source
    assert 'const MOBILE_TAB_SESSION_KEY = "davellm_mobile_tab_session"' in app_source
    assert 'const DRAFT_SESSION_KEY = "davellm_draft_session"' in app_source
    assert 'localStorage.removeItem(ANTICIPATION_STORAGE_KEY)' in app_source
    assert "localStorage.clear" not in app_source
    assert 'routerEndpoint("/route/decision")' not in app_source
    assert "await switchConversation(lastSessionId)" not in app_source
    assert "window.DaveAnticipation.getPredictions" in app_source
    assert "fetch(" not in anticipation_source
    assert "localStorage" not in anticipation_source
    assert "sessionStorage" not in anticipation_source
    assert "document." not in anticipation_source
    assert 'state.nodeStatus[preferredNodeId]?.status === "online"' in app_source
    assert "window.visualViewport" in app_source

    assert '<script src="anticipation.js"></script>' in html_source
    assert 'role="tablist"' in html_source
    assert html_source.count('role="tab"') == 3
    assert 'aria-label="Toggle theme"' in html_source
    assert 'aria-label="Refresh nodes"' in html_source
    assert 'id="undoPredictionBtn"' in html_source
    assert 'id="resetSuggestionsBtn"' in html_source
    assert 'id="contextStrip"' in html_source

    assert "env(safe-area-inset-bottom)" in css_source
    assert "--keyboard-inset" in css_source
    assert "max-height: 35dvh" in css_source
    assert "min-height: 44px" in css_source
    assert "button:focus-visible" in css_source
