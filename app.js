// Simple state container for nodes, router URL, and streaming status
const state = {
  nodes: [],
  selectedNode: null,
  autoRefreshId: null,
  isStreaming: false,
};

// DOM references
const routerUrlInput = document.getElementById('router-url');
const refreshButton = document.getElementById('refresh-nodes');
const nodeList = document.getElementById('node-list');
const nodeCount = document.getElementById('node-count');
const nodeSelect = document.getElementById('node-select');
const promptInput = document.getElementById('prompt');
const temperatureInput = document.getElementById('temperature');
const topKInput = document.getElementById('top-k');
const topPInput = document.getElementById('top-p');
const maxTokensInput = document.getElementById('max-tokens');
const sendButton = document.getElementById('send');
const responseBox = document.getElementById('response');
const copyButton = document.getElementById('copy-output');
const clearButton = document.getElementById('clear-output');
const settingsPanel = document.getElementById('settings-panel');
const settingsToggle = document.getElementById('toggle-settings');
const consoleLog = document.getElementById('console-log');

// Utility: append log lines to the console panel with timestamps
function log(message, level = 'info') {
  const entry = document.createElement('div');
  entry.className = 'log-entry';
  const ts = document.createElement('span');
  ts.className = 'log-timestamp';
  ts.textContent = new Date().toLocaleTimeString();
  const body = document.createElement('span');
  body.textContent = `[${level.toUpperCase()}] ${message}`;
  entry.appendChild(ts);
  entry.appendChild(body);
  consoleLog.appendChild(entry);
  consoleLog.scrollTop = consoleLog.scrollHeight;
}

// Persist router URL across sessions
function loadRouterUrl() {
  const saved = localStorage.getItem('routerUrl');
  if (saved) routerUrlInput.value = saved;
}

function saveRouterUrl() {
  localStorage.setItem('routerUrl', routerUrlInput.value.trim());
}

// Build full endpoint from router URL helper
function routerEndpoint(path) {
  return `${routerUrlInput.value.trim().replace(/\/$/, '')}${path}`;
}

// Render node list cards and dropdown options
function renderNodes(nodes) {
  nodeList.innerHTML = '';
  nodeSelect.innerHTML = '';

  nodes.forEach((node) => {
    const card = document.createElement('div');
    card.className = 'node-card';
    if (node.id === state.selectedNode) card.classList.add('active');
    card.addEventListener('click', () => selectNode(node.id));

    // Header with name and status dot
    const header = document.createElement('div');
    header.className = 'node-header';
    const name = document.createElement('div');
    name.className = 'node-name';
    name.textContent = node.name || node.id;
    const status = document.createElement('span');
    status.className = `status-dot ${node.status === 'online' ? 'status-online' : 'status-offline'}`;
    const statusWrap = document.createElement('div');
    statusWrap.style.display = 'flex';
    statusWrap.style.alignItems = 'center';
    statusWrap.appendChild(status);
    const statusLabel = document.createElement('span');
    statusLabel.textContent = node.status || 'unknown';
    statusLabel.style.color = '#9ba4b5';
    statusWrap.appendChild(statusLabel);
    header.appendChild(name);
    header.appendChild(statusWrap);

    // Meta information grid
    const meta = document.createElement('div');
    meta.className = 'node-meta';
    meta.innerHTML = `
      <div class="node-metric">💻 IP: <strong>${node.ip || 'N/A'}</strong></div>
      <div class="node-metric">📦 Model: <strong>${node.model || 'N/A'}</strong></div>
      <div class="node-metric">⚡️ TPS: <strong>${node.metrics?.tps ?? '—'}</strong></div>
      <div class="node-metric">⏱ Latency: <strong>${node.metrics?.latency_ms ?? '—'} ms</strong></div>
      <div class="node-metric">🕒 Last Response: <strong>${node.lastResponse || '—'}</strong></div>
    `;

    card.appendChild(header);
    card.appendChild(meta);
    nodeList.appendChild(card);

    // Dropdown option
    const option = document.createElement('option');
    option.value = node.id;
    option.textContent = `${node.name || node.id} (${node.status || 'unknown'})`;
    nodeSelect.appendChild(option);
  });

  nodeCount.textContent = `${nodes.filter((n) => n.status === 'online').length} online`;

  // Maintain selection
  if (!state.selectedNode && nodes.length) {
    state.selectedNode = nodes[0].id;
  }
  nodeSelect.value = state.selectedNode || '';
}

// Fetch nodes from router
async function fetchNodes(showToast = true) {
  try {
    const res = await fetch(routerEndpoint('/nodes'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.nodes = data;
    renderNodes(data);
    if (showToast) log(`Loaded ${data.length} node(s)`);
  } catch (err) {
    log(`Failed to fetch nodes: ${err.message}`, 'error');
  }
}

// Select node via click or dropdown
function selectNode(id) {
  state.selectedNode = id;
  nodeSelect.value = id;
  renderNodes(state.nodes);
  log(`Selected node ${id}`);
}

// Send prompt to router and stream response
async function sendPrompt() {
  if (state.isStreaming) return;
  const node = nodeSelect.value;
  const prompt = promptInput.value.trim();
  if (!node) return log('Please select a node first', 'warn');
  if (!prompt) return log('Prompt is empty', 'warn');

  state.isStreaming = true;
  responseBox.textContent = '';
  toggleInputs(true);
  log(`Sending prompt to ${node}`);

  const payload = {
    node,
    prompt,
    params: {
      temperature: parseFloat(temperatureInput.value),
      top_k: parseInt(topKInput.value, 10),
      top_p: parseFloat(topPInput.value),
      max_tokens: parseInt(maxTokensInput.value, 10),
    },
  };

  try {
    const res = await fetch(routerEndpoint('/generate'), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });

    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let lastChunkAt = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, { stream: true });
      appendStreamChunk(chunk);
      lastChunkAt = new Date();
    }

    markLastResponse(node, lastChunkAt);
    log('Completed streaming response');
  } catch (err) {
    log(`Generate failed: ${err.message}`, 'error');
  } finally {
    state.isStreaming = false;
    toggleInputs(false);
  }
}

// Append streamed server-sent events data to the response window
function appendStreamChunk(chunk) {
  // SSE lines may include "data:" prefixes; handle both raw text and SSE
  const lines = chunk.split('\n');
  lines.forEach((line) => {
    if (!line.trim()) return;
    const text = line.startsWith('data:') ? line.replace(/^data:\s*/, '') : line;
    responseBox.textContent += text;
  });
  responseBox.scrollTop = responseBox.scrollHeight;
}

// Update node card with last response time for quick status context
function markLastResponse(nodeId, date) {
  if (!date) return;
  state.nodes = state.nodes.map((n) => (n.id === nodeId ? { ...n, lastResponse: date.toLocaleTimeString() } : n));
  renderNodes(state.nodes);
}

// Enable/disable controls during streaming
function toggleInputs(disabled) {
  [sendButton, refreshButton, routerUrlInput, promptInput, nodeSelect].forEach((el) => {
    el.disabled = disabled;
  });
}

// Copy response text to clipboard
async function copyResponse() {
  try {
    await navigator.clipboard.writeText(responseBox.textContent);
    log('Response copied to clipboard');
  } catch (err) {
    log(`Copy failed: ${err.message}`, 'error');
  }
}

// Auto-refresh nodes every 3 seconds
function startAutoRefresh() {
  if (state.autoRefreshId) clearInterval(state.autoRefreshId);
  state.autoRefreshId = setInterval(() => fetchNodes(false), 3000);
}

// Toggle settings visibility
function toggleSettings() {
  settingsPanel.classList.toggle('hidden');
}

// Bind all UI events
function bindEvents() {
  routerUrlInput.addEventListener('change', () => {
    saveRouterUrl();
    fetchNodes();
  });
  refreshButton.addEventListener('click', () => fetchNodes());
  nodeSelect.addEventListener('change', (e) => selectNode(e.target.value));
  sendButton.addEventListener('click', sendPrompt);
  copyButton.addEventListener('click', copyResponse);
  clearButton.addEventListener('click', () => {
    responseBox.textContent = '';
    log('Cleared output');
  });
  settingsToggle.addEventListener('click', toggleSettings);
  settingsToggle.addEventListener('keypress', (e) => {
    if (e.key === 'Enter' || e.key === ' ') toggleSettings();
  });
}

// Initialize page
function init() {
  loadRouterUrl();
  bindEvents();
  fetchNodes();
  startAutoRefresh();
  log('UI ready');
}

document.addEventListener('DOMContentLoaded', init);
