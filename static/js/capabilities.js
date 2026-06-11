/**
 * Registry-driven capability catalog, configuration, runs, and schedules.
 */
import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';
import settingsModule from './settings.js';

const API_BASE = window.location.origin;
const ACTIVE = new Set(['queued', 'running', 'cancelling']);
let _open = false;
let _items = [];
let _selectedId = null;
let _pollTimer = null;
let _escHandler = null;
let _readinessTimer = null;
let _previousFocus = null;

const esc = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#39;');

async function api(path, options) {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'same-origin',
    ...options,
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { message = (await response.json()).detail || message; } catch {}
    throw new Error(message);
  }
  return response.json();
}

function icon(name, size = 15) {
  const paths = {
    radar: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M22 12h-3M12 22v-3M2 12h3"/><path d="m14 10 5-5"/>',
    chart: '<path d="M4 19V9M10 19V5M16 19v-7M22 19H2"/>',
    code: '<path d="m8 9-3 3 3 3M16 9l3 3-3 3M14 5l-4 14"/>',
    sparkles: '<path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8Z"/><path d="m19 16 .8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8Z"/>',
  };
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.sparkles}</svg>`;
}

function statusLabel(item) {
  if (item.removed) return ['historical', 'Historical'];
  if (!item.enabled) return ['disabled', 'Disabled'];
  if (item.readiness?.ready) return ['ready', 'Ready'];
  return ['not-ready', 'Needs setup'];
}

async function loadCatalog(selectId) {
  const data = await api('/api/capabilities');
  _items = data.capabilities || [];
  const reload = document.getElementById('capabilities-reload');
  if (reload) reload.hidden = !data.can_administer;
  if (selectId && _items.some(item => item.id === selectId)) _selectedId = selectId;
  if (!_selectedId || !_items.some(item => item.id === _selectedId)) {
    _selectedId = _items[0]?.id || null;
  }
  renderCatalog();
  await renderDetail();
  syncIndicator();
}

function renderCatalog() {
  const list = document.getElementById('capability-catalog-list');
  if (!list) return;
  const query = (document.getElementById('capability-search')?.value || '').trim().toLowerCase();
  const stateFilter = document.getElementById('capability-filter')?.value || 'all';
  const filtered = _items.filter(item => {
    const [state] = statusLabel(item);
    const matchesState =
      stateFilter === 'all'
      || (stateFilter === 'active' && item.active_run_count)
      || (stateFilter === 'historical' && item.removed)
      || state === stateFilter;
    return matchesState
      && (!query || `${item.name} ${item.description} ${item.category}`.toLowerCase().includes(query));
  });
  if (!filtered.length) {
    list.innerHTML = '<div class="cap-empty">No capabilities match this search.</div>';
    return;
  }
  list.innerHTML = filtered.map(item => {
    const [state, label] = statusLabel(item);
    return `<button class="cap-catalog-item${item.id === _selectedId ? ' active' : ''}" data-capability-id="${esc(item.id)}">
      <span class="cap-icon">${icon(item.icon)}</span>
      <span class="cap-catalog-copy">
        <strong>${esc(item.name)}</strong>
        <small>${esc(item.category)} · v${esc(item.version)}</small>
        <small>${item.active_run_count
          ? `${esc(item.active_run_count)} active`
          : item.schedules?.[0]?.next_run
            ? `Next ${esc(new Date(item.schedules[0].next_run).toLocaleString())}`
            : item.last_run?.created_at
              ? `Last ${esc(new Date(item.last_run.created_at).toLocaleString())}`
              : 'Never run'}</small>
      </span>
      ${item.active_run_count ? `<span class="cap-active-count">${item.active_run_count}</span>` : ''}
      <span class="cap-state ${state}">${label}</span>
    </button>`;
  }).join('');
  list.querySelectorAll('[data-capability-id]').forEach(button => {
    button.addEventListener('click', async () => {
      _selectedId = button.dataset.capabilityId;
      renderCatalog();
      await renderDetail();
    });
  });
}

function inputControl(input, value) {
  const attrs = [
    `data-cap-input="${esc(input.name)}"`,
    `data-cap-type="${esc(input.type)}"`,
    input.required ? 'required' : '',
    input.placeholder ? `placeholder="${esc(input.placeholder)}"` : '',
    input.minimum !== null ? `min="${esc(input.minimum)}"` : '',
    input.maximum !== null ? `max="${esc(input.maximum)}"` : '',
  ].filter(Boolean).join(' ');
  if (input.choices?.length) {
    return `<select class="cap-input" ${attrs}>${input.choices.map(choice =>
      `<option value="${esc(choice)}"${String(choice) === String(value) ? ' selected' : ''}>${esc(choice)}</option>`
    ).join('')}</select>`;
  }
  if (input.type === 'boolean') {
    return `<label class="cap-switch"><input type="checkbox" ${attrs}${value ? ' checked' : ''}><span></span></label>`;
  }
  if (input.type === 'text') {
    return `<textarea class="cap-input" rows="3" ${attrs}>${esc(value ?? '')}</textarea>`;
  }
  const type = ['integer', 'number'].includes(input.type) ? 'number' : 'text';
  const step = input.type === 'number' ? ' step="any"' : '';
  return `<input class="cap-input" type="${type}"${step} value="${esc(value ?? '')}" ${attrs}>`;
}

function formHtml(item) {
  const groups = new Map();
  for (const input of item.inputs || []) {
    if (!groups.has(input.group)) groups.set(input.group, []);
    groups.get(input.group).push(input);
  }
  const defaults = { ...(item.inputs || []).reduce((out, input) => {
    if (input.default !== null && input.default !== undefined) out[input.name] = input.default;
    return out;
  }, {}), ...(item.saved_defaults || {}) };
  return [...groups.entries()].map(([group, inputs]) => `
    <fieldset class="cap-fieldset${inputs.every(input => input.advanced) ? ' cap-advanced' : ''}">
      <legend>${esc(group)}</legend>
      ${inputs.map(input => `<label class="cap-field">
        <span class="cap-field-copy"><strong>${esc(input.label)}${input.required ? ' *' : ''}</strong>
          ${input.description || input.help ? `<small>${esc(input.description || input.help)}</small>` : ''}
        </span>
        ${inputControl(input, defaults[input.name])}
      </label>`).join('')}
    </fieldset>`).join('');
}

function readForm() {
  const values = {};
  document.querySelectorAll('#capability-run-form [data-cap-input]').forEach(field => {
    const type = field.dataset.capType;
    if (type === 'boolean') values[field.dataset.capInput] = field.checked;
    else if (field.value !== '') {
      values[field.dataset.capInput] = type === 'integer'
        ? parseInt(field.value, 10)
        : type === 'number' ? parseFloat(field.value) : type === 'array'
          ? field.value.split(',').map(value => value.trim()).filter(Boolean)
          : field.value;
    }
  });
  return values;
}

function readinessHtml(readiness) {
  const checks = readiness?.checks || [];
  if (!checks.length) return '<div class="cap-check ready"><span></span>No declared dependencies</div>';
  return checks.map(check => `<div class="cap-check ${check.available === false ? 'blocked' : (check.ready ? 'ready' : 'blocked')}">
    <span></span><strong>${esc(check.label)}</strong><small>${esc(check.detail)}</small>
  </div>`).join('');
}

function applyReadiness(readiness) {
  const card = document.getElementById('cap-readiness-checks');
  if (card) card.innerHTML = readinessHtml(readiness);
  const start = document.querySelector('#capability-run-form button[type="submit"]');
  const selected = _items.find(item => item.id === _selectedId);
  if (start) start.disabled = !readiness?.ready || !selected?.enabled;
  const state = document.getElementById('cap-detail-state');
  if (state) {
    const [status, label] = statusLabel({ ...selected, readiness });
    state.className = `cap-state ${status}`;
    state.textContent = label;
  }
}

function runHtml(run, capability) {
  const terminal = !ACTIVE.has(run.status);
  const metrics = Object.entries(run.metrics || {});
  const failures = Object.entries(run.failures || {});
  const inputs = Object.entries(run.input || {});
  const progress = Object.entries(run.progress || {});
  const provenance = Array.isArray(run.provenance) ? run.provenance : [];
  const reportActions = new Set(capability.report_actions || []);
  return `<article class="cap-run" id="capability-run-${esc(run.id)}" data-run-id="${esc(run.id)}">
    <header>
      <span class="cap-run-status ${esc(run.outcome || run.status)}">${esc(run.outcome || run.status)}</span>
      <strong>${esc(run.summary || run.error || 'Capability run')}</strong>
      <time>${esc(new Date(run.created_at).toLocaleString())}</time>
    </header>
    ${metrics.length ? `<div class="cap-metrics">${metrics.map(([key, value]) =>
      `<span><strong>${esc(value)}</strong>${esc(key.replaceAll('_', ' '))}</span>`).join('')}</div>` : ''}
    ${progress.length ? `<div class="cap-run-progress">${progress.map(([key, value]) =>
      `<span><strong>${esc(key.replaceAll('_', ' '))}</strong>${esc(typeof value === 'object' ? JSON.stringify(value) : value)}</span>`).join('')}</div>` : ''}
    ${run.warnings?.length ? `<div class="cap-run-note warning">${run.warnings.map(esc).join('<br>')}</div>` : ''}
    ${failures.length ? `<div class="cap-run-note error">${failures.map(([key, value]) => `${esc(key)}: ${esc(typeof value === 'string' ? value : JSON.stringify(value))}`).join('<br>')}</div>` : ''}
    ${inputs.length || provenance.length ? `<details class="cap-run-details"><summary>Inputs and provenance</summary>
      ${inputs.length ? `<dl>${inputs.map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(typeof value === 'object' ? JSON.stringify(value) : value)}</dd></div>`).join('')}</dl>` : ''}
      ${provenance.length ? `<div class="cap-provenance">${provenance.map(item =>
        `<span>${esc(typeof item === 'object' ? [item.role, item.model, item.provider].filter(Boolean).join(' · ') || JSON.stringify(item) : item)}</span>`
      ).join('')}</div>` : ''}
    </details>` : ''}
    <footer>
      ${run.document_id && reportActions.has('open') ? `<button data-open-document="${esc(run.document_id)}">Open report</button>` : ''}
      ${run.document_id && reportActions.has('chat') ? `<button data-chat-document="${esc(run.document_id)}">Discuss</button>` : ''}
      ${run.document_id && reportActions.has('export') ? `<button data-export-document="${esc(run.document_id)}">Export</button>` : ''}
      ${run.document_id && reportActions.has('archive') ? `<button data-archive-document="${esc(run.document_id)}">Archive</button>` : ''}
      ${run.document_id && reportActions.has('delete') ? `<button class="danger" data-delete-document="${esc(run.document_id)}">Delete</button>` : ''}
      ${run.scheduled_task_id ? `<button data-open-task="${esc(run.scheduled_task_id)}">Schedule</button>` : ''}
      <button data-run-log="${esc(run.id)}">Logs</button>
      ${ACTIVE.has(run.status) && capability.supports_cancel ? `<button class="danger" data-cancel-run="${esc(run.id)}">Cancel</button>` : ''}
      ${terminal && capability.supports_retry ? `<button data-rerun="${esc(run.id)}">Run again</button>` : ''}
    </footer>
    <pre class="cap-run-log" hidden></pre>
  </article>`;
}

async function renderDetail() {
  const detail = document.getElementById('capability-detail');
  if (!detail) return;
  if (!_selectedId) {
    detail.innerHTML = '<div class="cap-empty">No capabilities are available.</div>';
    return;
  }
  detail.innerHTML = '<div class="cap-empty">Loading capability...</div>';
  const [item, runsData] = await Promise.all([
    api(`/api/capabilities/${encodeURIComponent(_selectedId)}`),
    api(`/api/capabilities/runs?capability_id=${encodeURIComponent(_selectedId)}&limit=50`),
  ]);
  const [state, stateText] = statusLabel(item);
  detail.innerHTML = `
    <section class="cap-hero">
      <div class="cap-hero-icon">${icon(item.icon, 22)}</div>
      <div><span class="cap-kicker">${esc(item.category)} · v${esc(item.version)}</span>
        <h2>${esc(item.name)}</h2><p>${esc(item.short_description || item.description)}</p></div>
      <div class="cap-hero-actions"><span id="cap-detail-state" class="cap-state ${state}">${stateText}</span>
        ${item.can_administer ? `<button id="cap-toggle-enabled">${item.enabled ? 'Disable' : 'Enable'}</button>` : ''}</div>
    </section>
    ${item.removed ? '<div class="cap-run-note warning">This capability is no longer registered. Historical runs and reports remain available.</div>' : `<div class="cap-detail-grid">
      <section class="cap-card">
        <div class="cap-card-title"><h3>Run now</h3><span>${esc((item.triggers || []).join(' · '))}</span></div>
        <form id="capability-run-form">${formHtml(item)}
          <div class="cap-form-actions">
            <button type="button" id="cap-save-defaults">Save defaults</button>
            <button type="submit" class="primary" ${item.readiness?.ready && item.enabled ? '' : 'disabled'}>Start run</button>
          </div>
        </form>
      </section>
      <aside>
        <section class="cap-card"><div class="cap-card-title"><h3>Readiness</h3></div><div id="cap-readiness-checks">${readinessHtml(item.readiness)}</div>
          <div class="cap-form-actions">
            ${item.model_roles?.length ? '<button type="button" id="cap-open-model-settings">Model settings</button>' : ''}
            ${item.dependencies?.length ? '<button type="button" id="cap-open-integration-settings">Integration settings</button>' : ''}
          </div>
        </section>
        <section class="cap-card">
          <div class="cap-card-title"><h3>Schedules</h3>${item.triggers?.includes('schedule') ? '<button id="cap-open-tasks">Create schedule</button>' : ''}</div>
          <div class="cap-schedules">${(item.schedules || []).map(schedule =>
            `<button data-task-id="${esc(schedule.id)}"><strong>${esc(schedule.name)}</strong><small>${esc(schedule.status)} · ${esc(schedule.next_run ? new Date(schedule.next_run).toLocaleString() : schedule.trigger_type)}</small></button>`
          ).join('') || '<div class="cap-muted">No schedules yet.</div>'}</div>
        </section>
      </aside>
    </div>`}
    <section class="cap-runs-section"><div class="cap-section-heading"><h3>Run history</h3><span>${runsData.runs.length} shown</span></div>
      <div class="cap-runs">${runsData.runs.map(run => runHtml(run, item)).join('') || '<div class="cap-empty">No runs yet.</div>'}</div>
    </section>`;
  wireDetail(item);
  const hashRun = location.hash.match(/^#capability-run-(.+)$/)?.[1];
  if (hashRun) setTimeout(() => document.getElementById(`capability-run-${CSS.escape(hashRun)}`)?.scrollIntoView(), 50);
}

function wireDetail(item) {
  document.getElementById('capability-run-form')?.addEventListener('input', () => {
    clearTimeout(_readinessTimer);
    _readinessTimer = setTimeout(async () => {
      try {
        const readiness = await api(`/api/capabilities/${encodeURIComponent(item.id)}/readiness`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ input: readForm() }),
        });
        applyReadiness(readiness);
      } catch (error) {
        const start = document.querySelector('#capability-run-form button[type="submit"]');
        if (start) start.disabled = true;
      }
    }, 250);
  });
  document.getElementById('capability-run-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const run = await api(`/api/capabilities/${encodeURIComponent(item.id)}/runs`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input: readForm() }),
      });
      uiModule?.showToast?.(`${item.name} started`);
      location.hash = `capability-run-${run.id}`;
      await loadCatalog(item.id);
    } catch (error) { uiModule?.showError?.(error.message); }
  });
  document.getElementById('cap-save-defaults')?.addEventListener('click', async () => {
    try {
      await api(`/api/capabilities/${encodeURIComponent(item.id)}/defaults`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input: readForm() }),
      });
      uiModule?.showToast?.('Capability defaults saved');
    } catch (error) { uiModule?.showError?.(error.message); }
  });
  document.getElementById('cap-open-tasks')?.addEventListener('click', () =>
    window.tasksModule?.openCapabilitySchedule(item.id));
  document.getElementById('cap-open-model-settings')?.addEventListener('click', () =>
    settingsModule.open('ai'));
  document.getElementById('cap-open-integration-settings')?.addEventListener('click', () =>
    settingsModule.open('integrations'));
  document.getElementById('cap-toggle-enabled')?.addEventListener('click', async () => {
    try {
      await api(`/api/capabilities/${encodeURIComponent(item.id)}/enabled`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !item.enabled }),
      });
      await loadCatalog(item.id);
    } catch (error) { uiModule?.showError?.(error.message); }
  });
  document.querySelectorAll('[data-task-id]').forEach(button =>
    button.addEventListener('click', () => window.tasksModule?.openTasks(button.dataset.taskId)));
  document.querySelectorAll('[data-open-task]').forEach(button =>
    button.addEventListener('click', () => window.tasksModule?.openTasks(button.dataset.openTask)));
  document.querySelectorAll('[data-open-document]').forEach(button =>
    button.addEventListener('click', () => window.documentModule?.loadDocument(button.dataset.openDocument)));
  document.querySelectorAll('[data-chat-document]').forEach(button => button.addEventListener('click', async () => {
    await window.documentModule?.loadDocument(button.dataset.chatDocument);
    close();
    const input = document.getElementById('message-input');
    if (input) {
      input.value = 'Help me interpret this capability report. Summarize the findings, caveats, provenance, and useful next actions.';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus();
    }
  }));
  document.querySelectorAll('[data-export-document]').forEach(button => button.addEventListener('click', async () => {
    try {
      const response = await fetch(`${API_BASE}/api/documents/export-zip`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [button.dataset.exportDocument] }),
      });
      if (!response.ok) throw new Error(`Export failed (${response.status})`);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = 'capability-report.zip';
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { uiModule?.showError?.(error.message); }
  }));
  document.querySelectorAll('[data-archive-document]').forEach(button => button.addEventListener('click', async () => {
    try {
      await api(`/api/document/${encodeURIComponent(button.dataset.archiveDocument)}/archive?archived=true`, { method: 'POST' });
      uiModule?.showToast?.('Report archived');
      await loadCatalog(item.id);
    } catch (error) { uiModule?.showError?.(error.message); }
  }));
  document.querySelectorAll('[data-delete-document]').forEach(button => button.addEventListener('click', async () => {
    if (!window.confirm('Delete this report? The capability run history remains.')) return;
    try {
      await api(`/api/document/${encodeURIComponent(button.dataset.deleteDocument)}`, { method: 'DELETE' });
      uiModule?.showToast?.('Report deleted');
      await loadCatalog(item.id);
    } catch (error) { uiModule?.showError?.(error.message); }
  }));
  document.querySelectorAll('[data-run-log]').forEach(button => button.addEventListener('click', async () => {
    const pre = button.closest('.cap-run')?.querySelector('.cap-run-log');
    if (!pre) return;
    if (!pre.hidden) { pre.hidden = true; return; }
    const data = await api(`/api/capabilities/runs/${encodeURIComponent(button.dataset.runLog)}/log`);
    pre.textContent = data.output || '(no output yet)';
    pre.hidden = false;
  }));
  document.querySelectorAll('[data-cancel-run]').forEach(button => button.addEventListener('click', async () => {
    await api(`/api/capabilities/runs/${encodeURIComponent(button.dataset.cancelRun)}/cancel`, { method: 'POST' });
    await loadCatalog(item.id);
  }));
  document.querySelectorAll('[data-rerun]').forEach(button => button.addEventListener('click', async () => {
    const run = await api(`/api/capabilities/runs/${encodeURIComponent(button.dataset.rerun)}/rerun`, { method: 'POST' });
    location.hash = `capability-run-${run.id}`;
    await loadCatalog(item.id);
  }));
}

function syncIndicator() {
  const active = _items.reduce((sum, item) => sum + (item.active_run_count || 0), 0);
  for (const id of ['rail-capabilities', 'tool-capabilities-btn']) {
    const button = document.getElementById(id);
    if (!button) continue;
    button.classList.toggle('rail-notify', active > 0);
    button.classList.toggle('capabilities-active', active > 0);
  }
  window._capabilityRunsActive = active > 0;
}

function startPolling() {
  stopPolling();
  _pollTimer = setInterval(() => {
    if (_open || window._capabilityRunsActive) loadCatalog(_selectedId).catch(() => {});
  }, 4000);
}
function stopPolling() { if (_pollTimer) clearInterval(_pollTimer); _pollTimer = null; }

export async function open(selectId) {
  if (_open) {
    if (selectId) { _selectedId = selectId; await loadCatalog(selectId); }
    return;
  }
  _open = true;
  _previousFocus = document.activeElement;
  const overlay = document.createElement('div');
  overlay.id = 'capabilities-overlay';
  overlay.className = 'modal capabilities-overlay';
  overlay.innerHTML = `<div class="modal-content capabilities-pane" role="dialog" aria-modal="true" aria-labelledby="capabilities-title">
    <div class="modal-header capabilities-header">
      <h4 id="capabilities-title">${icon('sparkles')}<span>Capabilities</span></h4>
      <div class="cap-header-actions">
        <button id="capabilities-reload" type="button" title="Reload registry">Reload registry</button>
        <button id="capabilities-close" class="close-btn" title="Close" aria-label="Close capabilities">&#x2716;</button>
      </div>
    </div>
    <div class="capabilities-shell">
      <aside class="cap-catalog">
        <div class="cap-catalog-heading"><strong>Catalog</strong><span id="capability-count"></span></div>
        <input id="capability-search" class="cap-search" type="search" placeholder="Search capabilities" aria-label="Search capabilities">
        <select id="capability-filter" class="cap-search" aria-label="Filter capabilities">
          <option value="all">All states</option>
          <option value="ready">Ready</option>
          <option value="not-ready">Needs setup</option>
          <option value="disabled">Disabled</option>
          <option value="active">Active runs</option>
          <option value="historical">Historical</option>
        </select>
        <div id="capability-catalog-list" class="cap-catalog-list"></div>
      </aside>
      <main id="capability-detail" class="capability-detail"></main>
    </div>
  </div>`;
  document.body.appendChild(overlay);
  const pane = overlay.querySelector('.capabilities-pane');
  makeWindowDraggable(pane, overlay.querySelector('.capabilities-header'));
  overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
  document.getElementById('capabilities-close')?.addEventListener('click', close);
  document.getElementById('capabilities-reload')?.addEventListener('click', async () => {
    try {
      await api('/api/capabilities/reload', { method: 'POST' });
      await loadCatalog(_selectedId);
      uiModule?.showToast?.('Capability registry reloaded');
    } catch (error) { uiModule?.showError?.(error.message); }
  });
  document.getElementById('capability-search')?.addEventListener('input', renderCatalog);
  document.getElementById('capability-filter')?.addEventListener('change', renderCatalog);
  _escHandler = event => {
    if (event.key === 'Escape') close();
    if (event.key !== 'Tab') return;
    const focusable = [...overlay.querySelectorAll('button:not([disabled]):not([hidden]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
      .filter(element => element.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  document.addEventListener('keydown', _escHandler);
  const hashCapability = location.hash.match(/^#capability-([^/]+)$/)?.[1];
  await loadCatalog(selectId || hashCapability);
  document.getElementById('capability-count').textContent = `${_items.length}`;
  document.getElementById('capability-search')?.focus();
  startPolling();
}

export function close() {
  _open = false;
  document.getElementById('capabilities-overlay')?.remove();
  if (_escHandler) document.removeEventListener('keydown', _escHandler);
  _escHandler = null;
  if (_previousFocus instanceof HTMLElement) _previousFocus.focus();
  _previousFocus = null;
  if (!window._capabilityRunsActive) stopPolling();
}
export function toggle() { return _open ? close() : open(); }
export function isOpen() { return _open; }

export function init() {
  document.getElementById('tool-capabilities-btn')?.addEventListener('click', toggle);
  window.addEventListener('hashchange', () => {
    const match = location.hash.match(/^#capability-(?!run-)(.+)$/);
    if (match) open(match[1]);
    else if (location.hash.startsWith('#capability-run-')) open();
  });
  if (location.hash.startsWith('#capability-')) open();
  loadCatalog().catch(() => {});
}

const capabilityModule = { init, open, close, toggle, isOpen };
window.capabilityModule = capabilityModule;
export default capabilityModule;
