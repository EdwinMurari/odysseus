/**
 * Registry-driven capability catalog, configuration, runs, and schedules.
 *
 * Rendering model: data is refreshed on a timer, but the DOM is only touched
 * when the underlying data actually changed (fingerprint comparison). Run
 * cards are reconciled in place by id instead of being rebuilt wholesale.
 * This keeps scroll position, form edits, focus, expanded panels, and open
 * logs stable across background refreshes — a full innerHTML rebuild on
 * every poll is what previously caused the pane to "reset" while in use.
 */
import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';
import settingsModule from './settings.js';

const API_BASE = window.location.origin;
const ACTIVE = new Set(['queued', 'running', 'cancelling']);
const POLL_INTERVAL_MS = 4000;
const READINESS_DEBOUNCE_MS = 250;

let _open = false;
let _items = [];
let _selectedId = null;
let _detail = null;          // detail view of the selected capability
let _runs = [];              // runs for the selected capability
let _pollTimer = null;
let _escHandler = null;
let _readinessTimer = null;
let _previousFocus = null;
let _catalogSeq = 0;         // stale catalog responses are dropped
let _detailSeq = 0;          // stale detail responses are dropped
let _refreshing = false;     // prevents overlapping refresh cycles
let _formDirty = false;      // user edited the run form since the last structural render
let _fpCatalog = '';         // fingerprint of the rendered catalog list
let _fpStructure = '';       // fingerprint of the rendered detail structure

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

function setHash(value) {
  // replaceState avoids a hashchange event, which would re-enter open().
  history.replaceState(null, '', `#${value}`);
}

function resetDetailState() {
  _detail = null;
  _runs = [];
  _fpStructure = '';
  _formDirty = false;
}

/* ------------------------------------------------------------------ */
/* Data refresh                                                        */
/* ------------------------------------------------------------------ */

async function refreshCatalog() {
  const seq = ++_catalogSeq;
  const data = await api('/api/capabilities');
  if (seq !== _catalogSeq) return;
  _items = data.capabilities || [];
  const reload = document.getElementById('capabilities-reload');
  if (reload) reload.hidden = !data.can_administer;
  if (_selectedId && !_items.some(item => item.id === _selectedId)) {
    _selectedId = null;
    resetDetailState();
  }
  if (!_selectedId && _items.length) _selectedId = _items[0].id;
  renderCatalog();
  syncIndicator();
}

async function refreshDetail() {
  const detailEl = document.getElementById('capability-detail');
  if (!_selectedId) {
    if (detailEl) detailEl.innerHTML = '<div class="cap-empty">No capabilities are available.</div>';
    return;
  }
  const seq = ++_detailSeq;
  const capabilityId = _selectedId;
  try {
    const [item, runsData] = await Promise.all([
      api(`/api/capabilities/${encodeURIComponent(capabilityId)}`),
      api(`/api/capabilities/runs?capability_id=${encodeURIComponent(capabilityId)}&limit=50`),
    ]);
    if (seq !== _detailSeq || capabilityId !== _selectedId) return;
    _detail = item;
    _runs = runsData.runs || [];
    renderDetail();
  } catch (error) {
    if (seq !== _detailSeq || capabilityId !== _selectedId) return;
    // Keep the last good view on transient poll failures; only show the
    // error state when there is nothing rendered yet.
    if (!_detail && detailEl) {
      detailEl.innerHTML = `<div class="cap-empty cap-detail-error">
        <p>Could not load this capability.</p>
        <small>${esc(error.message)}</small>
        <button type="button" data-retry-detail>Try again</button>
      </div>`;
    }
  }
}

async function refreshAll() {
  if (_refreshing) return;
  _refreshing = true;
  try {
    await refreshCatalog();
    if (_open) await refreshDetail();
  } finally {
    _refreshing = false;
  }
}

/* ------------------------------------------------------------------ */
/* Catalog rendering                                                   */
/* ------------------------------------------------------------------ */

function catalogItemFp(item) {
  return [
    item.id, item.name, item.category, item.version, item.icon,
    item.enabled, item.removed, item.active_run_count,
    item.readiness?.ready, item.schedules?.[0]?.next_run, item.last_run?.created_at,
  ].join('|');
}

function filteredItems() {
  const query = (document.getElementById('capability-search')?.value || '').trim().toLowerCase();
  const stateFilter = document.getElementById('capability-filter')?.value || 'all';
  return _items.filter(item => {
    const [state] = statusLabel(item);
    const matchesState =
      stateFilter === 'all'
      || (stateFilter === 'active' && item.active_run_count)
      || (stateFilter === 'historical' && item.removed)
      || state === stateFilter;
    return matchesState
      && (!query || `${item.name} ${item.description} ${item.category}`.toLowerCase().includes(query));
  });
}

function renderCatalog() {
  const list = document.getElementById('capability-catalog-list');
  if (!list) return;
  const filtered = filteredItems();
  const count = document.getElementById('capability-count');
  if (count) {
    count.textContent = filtered.length === _items.length
      ? `${_items.length}`
      : `${filtered.length} of ${_items.length}`;
  }
  const fp = JSON.stringify([_selectedId, filtered.map(catalogItemFp)]);
  if (fp === _fpCatalog) return;
  _fpCatalog = fp;
  const scrollTop = list.scrollTop;
  if (!filtered.length) {
    list.innerHTML = '<div class="cap-empty">No capabilities match this search.</div>';
    return;
  }
  list.innerHTML = filtered.map(item => {
    const [state, label] = statusLabel(item);
    return `<button class="cap-catalog-item${item.id === _selectedId ? ' active' : ''}" data-capability-id="${esc(item.id)}">
      <span class="cap-icon">${icon(item.icon)}</span>
      <span class="cap-catalog-copy">
        <span class="cap-catalog-top">
          <strong>${esc(item.name)}</strong>
          ${item.active_run_count ? `<span class="cap-active-count">${esc(item.active_run_count)}</span>` : ''}
          <span class="cap-state ${state}">${label}</span>
        </span>
        <small>${esc(item.category)} · v${esc(item.version)}</small>
        <small class="cap-catalog-meta">${item.active_run_count
          ? `${esc(item.active_run_count)} active`
          : item.schedules?.[0]?.next_run
            ? `Next ${esc(new Date(item.schedules[0].next_run).toLocaleString())}`
            : item.last_run?.created_at
              ? `Last ${esc(new Date(item.last_run.created_at).toLocaleString())}`
              : 'Never run'}</small>
      </span>
    </button>`;
  }).join('');
  list.scrollTop = scrollTop;
}

async function selectCapability(id) {
  if (!id || id === _selectedId) return;
  _selectedId = id;
  resetDetailState();
  renderCatalog();
  const detailEl = document.getElementById('capability-detail');
  if (detailEl) detailEl.innerHTML = '<div class="cap-empty">Loading capability…</div>';
  await refreshDetail();
}

/* ------------------------------------------------------------------ */
/* Detail rendering                                                    */
/* ------------------------------------------------------------------ */

function chipListHtml(input, value, item) {
  const deps = (item.dependencies || []).filter(dep => dep.input === input.name);
  if (!deps.length) return null;
  const depMap = new Map();
  for (const dep of deps) {
    for (const val of dep.values || []) depMap.set(String(val), dep);
  }
  const selected = new Set(
    Array.isArray(value) ? value.map(String)
    : typeof value === 'string' ? value.split(',').map(v => v.trim()).filter(Boolean)
    : []
  );
  const allValues = [...depMap.keys()];
  const hiddenValue = allValues.filter(v => selected.has(v)).join(',');
  return `<div class="cap-chip-list" data-cap-chip-input="${esc(input.name)}">
    <input type="hidden" data-cap-input="${esc(input.name)}" data-cap-type="array" value="${esc(hiddenValue)}">
    ${allValues.map(val => {
      const dep = depMap.get(val);
      const active = selected.has(val);
      return `<button type="button" class="cap-chip${active ? ' active' : ''}" data-chip-value="${esc(val)}" data-chip-dep-id="${esc(dep?.id || '')}" title="${esc(dep?.label || val)}">
        <span class="cap-chip-dot" data-chip-dot="${esc(dep?.id || '')}"></span>
        <span class="cap-chip-label">${esc(dep?.label || val)}</span>
      </button>`;
    }).join('')}
  </div>`;
}

function inputControl(input, value, item) {
  const attrs = [
    `data-cap-input="${esc(input.name)}"`,
    `data-cap-type="${esc(input.type)}"`,
    input.required ? 'required' : '',
    input.placeholder ? `placeholder="${esc(input.placeholder)}"` : '',
    input.minimum != null ? `min="${esc(input.minimum)}"` : '',
    input.maximum != null ? `max="${esc(input.maximum)}"` : '',
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
  if (input.type === 'array' && item) {
    const chips = chipListHtml(input, value, item);
    if (chips) return chips;
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
        ${inputControl(input, defaults[input.name], item)}
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

function snapshotForm() {
  const form = document.getElementById('capability-run-form');
  if (!form) return null;
  const values = new Map();
  form.querySelectorAll('[data-cap-input]').forEach(field => {
    values.set(field.dataset.capInput, field.dataset.capType === 'boolean' ? field.checked : field.value);
  });
  const active = document.activeElement;
  const focused = active && form.contains(active) ? active : null;
  return {
    values,
    focusName: focused?.dataset?.capInput || null,
    selectionStart: focused?.selectionStart ?? null,
    selectionEnd: focused?.selectionEnd ?? null,
  };
}

function restoreForm(snapshot) {
  const form = document.getElementById('capability-run-form');
  if (!snapshot || !form) return;
  form.querySelectorAll('[data-cap-input]').forEach(field => {
    if (!snapshot.values.has(field.dataset.capInput)) return;
    const value = snapshot.values.get(field.dataset.capInput);
    if (field.dataset.capType === 'boolean') field.checked = !!value;
    else field.value = value;
  });
  // Sync chip button active classes with restored hidden input values
  for (const chipList of form.querySelectorAll('.cap-chip-list')) {
    const hidden = chipList.querySelector('input[type="hidden"]');
    if (!hidden) continue;
    const active = new Set(hidden.value.split(',').map(v => v.trim()).filter(Boolean));
    for (const chip of chipList.querySelectorAll('.cap-chip')) {
      chip.classList.toggle('active', active.has(chip.dataset.chipValue));
    }
  }
  if (!snapshot.focusName) return;
  const field = form.querySelector(`[data-cap-input="${CSS.escape(snapshot.focusName)}"]`);
  if (!field) return;
  field.focus();
  if (snapshot.selectionStart != null && typeof field.setSelectionRange === 'function') {
    try { field.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd ?? snapshot.selectionStart); } catch {}
  }
}

function readinessHtml(readiness) {
  const checks = readiness?.checks || [];
  if (!checks.length) return '<div class="cap-check ready"><span></span>No declared dependencies</div>';
  return checks.map(check => `<div class="cap-check ${check.available === false ? 'blocked' : (check.ready ? 'ready' : 'blocked')}">
    <span></span><strong>${esc(check.label)}</strong><small>${esc(check.detail)}</small>
  </div>`).join('');
}

function applyReadiness(readiness, item = _detail) {
  const card = document.getElementById('cap-readiness-checks');
  if (card) {
    const fp = JSON.stringify(readiness?.checks || []);
    if (card.dataset.fp !== fp) {
      card.dataset.fp = fp;
      card.innerHTML = readinessHtml(readiness);
    }
  }
  const start = document.querySelector('#capability-run-form button[type="submit"]');
  if (start) start.disabled = !readiness?.ready || !item?.enabled;
  const state = document.getElementById('cap-detail-state');
  if (state && item) {
    const [status, label] = statusLabel({ ...item, readiness });
    state.className = `cap-state ${status}`;
    state.textContent = label;
  }
  // Update source availability without trapping an already-selected blocked
  // source: it stays clickable until the user turns it off.
  const checks = readiness?.checks || [];
  const checkById = new Map(checks.map(c => [c.id?.replace('dependency:', ''), c]));
  for (const button of document.querySelectorAll('.cap-chip[data-chip-dep-id]')) {
    const depId = button.dataset.chipDepId;
    const check = checkById.get(depId);
    const available = !!check?.available;
    const active = button.classList.contains('active');
    button.disabled = !available && !active;
    button.title = check?.detail || button.title;
    const dot = button.querySelector('.cap-chip-dot');
    if (!dot) continue;
    dot.classList.toggle('available', !!check?.available);
    dot.classList.toggle('blocked', check ? !check.available : false);
    dot.classList.toggle('unknown', !check);
    dot.title = check?.detail || '';
  }
}

function applySchedules(item) {
  const wrap = document.getElementById('cap-schedules');
  if (!wrap) return;
  const schedules = item.schedules || [];
  const fp = JSON.stringify(schedules);
  if (wrap.dataset.fp === fp) return;
  wrap.dataset.fp = fp;
  wrap.innerHTML = schedules.map(schedule =>
    `<button type="button" data-task-id="${esc(schedule.id)}"><strong>${esc(schedule.name)}</strong><small>${esc(schedule.status)} · ${esc(schedule.next_run ? new Date(schedule.next_run).toLocaleString() : schedule.trigger_type)}</small></button>`
  ).join('') || '<div class="cap-muted">No schedules yet.</div>';
}

function structureFp(item) {
  return JSON.stringify([
    item.id, item.name, item.version, item.category, item.icon,
    item.description, item.short_description, item.enabled, item.removed,
    item.can_administer, item.triggers, item.inputs, item.saved_defaults,
    item.report_actions,
    item.supports_cancel, item.supports_retry,
  ]);
}

function structureHtml(item) {
  const [state, stateText] = statusLabel(item);
  return `
    <section class="cap-hero">
      <div class="cap-hero-icon">${icon(item.icon, 22)}</div>
      <div><span class="cap-kicker">${esc(item.category)} · v${esc(item.version)}</span>
        <h2>${esc(item.name)}</h2><p>${esc(item.short_description || item.description)}</p></div>
      <div class="cap-hero-actions"><span id="cap-detail-state" class="cap-state ${state}">${stateText}</span>
        ${item.can_administer ? `<button type="button" id="cap-toggle-enabled">${item.enabled ? 'Disable' : 'Enable'}</button>` : ''}</div>
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
          <div class="cap-card-title"><h3>Schedules</h3>${item.triggers?.includes('schedule') ? '<button type="button" id="cap-open-tasks">Create schedule</button>' : ''}</div>
          <div class="cap-schedules" id="cap-schedules"></div>
        </section>
      </aside>
    </div>`}
    <section class="cap-runs-section"><div class="cap-section-heading"><h3>Run history</h3><span id="cap-runs-count"></span></div>
      <div class="cap-runs" id="cap-runs-list"></div>
    </section>`;
}

function renderDetail() {
  const detailEl = document.getElementById('capability-detail');
  if (!detailEl || !_detail) return;
  const item = _detail;
  const fp = structureFp(item);
  if (fp !== _fpStructure) {
    // Structural change (new selection, registry update, enable toggle).
    // Carry the user's in-progress edits and scroll across the rebuild.
    const snapshot = _formDirty ? snapshotForm() : null;
    const scrollTop = detailEl.scrollTop;
    detailEl.innerHTML = structureHtml(item);
    _fpStructure = fp;
    detailEl.scrollTop = scrollTop;
    if (snapshot) restoreForm(snapshot);
  }
  // While the user is editing the form, the debounced readiness check for
  // their draft input owns the readiness card — don't clobber it with the
  // saved-defaults readiness from the poll.
  if (!_formDirty) applyReadiness(item.readiness, item);
  applySchedules(item);
  syncRuns(item);
}

/* ------------------------------------------------------------------ */
/* Run history reconciliation                                          */
/* ------------------------------------------------------------------ */

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
      ${run.document_id && reportActions.has('open') ? `<button type="button" data-open-report="${esc(run.id)}">Open report</button>` : ''}
      ${run.document_id && reportActions.has('chat') ? `<button type="button" data-chat-document="${esc(run.document_id)}">Discuss</button>` : ''}
      ${run.document_id && reportActions.has('export') ? `<button type="button" data-export-document="${esc(run.document_id)}">Export</button>` : ''}
      ${run.document_id && reportActions.has('archive') ? `<button type="button" data-archive-document="${esc(run.document_id)}">Archive</button>` : ''}
      ${run.document_id && reportActions.has('delete') ? `<button type="button" class="danger" data-delete-document="${esc(run.document_id)}">Delete</button>` : ''}
      ${run.scheduled_task_id ? `<button type="button" data-open-task="${esc(run.scheduled_task_id)}">Schedule</button>` : ''}
      <button type="button" data-run-log="${esc(run.id)}">Logs</button>
      ${ACTIVE.has(run.status) && capability.supports_cancel ? `<button type="button" class="danger" data-cancel-run="${esc(run.id)}">Cancel</button>` : ''}
      ${terminal && capability.supports_retry ? `<button type="button" data-rerun="${esc(run.id)}">Run again</button>` : ''}
    </footer>
    <pre class="cap-run-log" hidden></pre>
  </article>`;
}

function buildRunEl(run, capability, fp) {
  const template = document.createElement('template');
  template.innerHTML = runHtml(run, capability).trim();
  const el = template.content.firstElementChild;
  el.dataset.fp = fp;
  return el;
}

function carryRunState(oldEl, freshEl) {
  if (oldEl.querySelector('.cap-run-details')?.open) {
    freshEl.querySelector('.cap-run-details')?.setAttribute('open', '');
  }
  const oldLog = oldEl.querySelector('.cap-run-log');
  const newLog = freshEl.querySelector('.cap-run-log');
  if (oldLog && newLog && !oldLog.hidden) {
    newLog.hidden = false;
    newLog.textContent = oldLog.textContent;
    newLog.scrollTop = oldLog.scrollTop;
  }
}

function syncRuns(item) {
  const wrap = document.getElementById('cap-runs-list');
  if (!wrap) return;
  const countEl = document.getElementById('cap-runs-count');
  if (countEl) countEl.textContent = `${_runs.length} shown`;
  if (!_runs.length) {
    if (!wrap.querySelector('.cap-empty')) wrap.innerHTML = '<div class="cap-empty">No runs yet.</div>';
    return;
  }
  wrap.querySelector('.cap-empty')?.remove();
  const wanted = new Set(_runs.map(run => String(run.id)));
  for (const el of [...wrap.children]) {
    if (!wanted.has(el.dataset.runId)) el.remove();
  }
  let cursor = wrap.firstElementChild;
  for (const run of _runs) {
    const id = String(run.id);
    const fp = JSON.stringify(run);
    let el = wrap.querySelector(`.cap-run[data-run-id="${CSS.escape(id)}"]`);
    if (el && el.dataset.fp !== fp) {
      const fresh = buildRunEl(run, item, fp);
      carryRunState(el, fresh);
      el.replaceWith(fresh);
      el = fresh;
    } else if (!el) {
      el = buildRunEl(run, item, fp);
    }
    if (el === cursor) cursor = cursor.nextElementSibling;
    else wrap.insertBefore(el, cursor);
  }
  // Live-tail any open log of a still-active run.
  const byId = new Map(_runs.map(run => [String(run.id), run]));
  for (const article of wrap.querySelectorAll('.cap-run')) {
    const run = byId.get(article.dataset.runId);
    const pre = article.querySelector('.cap-run-log');
    if (run && pre && !pre.hidden && ACTIVE.has(run.status)) refreshRunLog(pre, run.id);
  }
}

async function refreshRunLog(pre, runId) {
  if (pre.dataset.loading) return;
  pre.dataset.loading = '1';
  try {
    const data = await api(`/api/capabilities/runs/${encodeURIComponent(runId)}/log`);
    if (pre.isConnected && !pre.hidden) {
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
      pre.textContent = data.output || '(no output yet)';
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }
  } catch {} finally {
    delete pre.dataset.loading;
  }
}

/* ------------------------------------------------------------------ */
/* Actions (event-delegated; handlers are installed once per overlay)  */
/* ------------------------------------------------------------------ */

async function checkFormReadiness() {
  const capabilityId = _selectedId;
  if (!capabilityId) return;
  try {
    const readiness = await api(`/api/capabilities/${encodeURIComponent(capabilityId)}/readiness`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: readForm() }),
    });
    if (capabilityId !== _selectedId) return;
    applyReadiness(readiness);
  } catch {
    const start = document.querySelector('#capability-run-form button[type="submit"]');
    if (start) start.disabled = true;
  }
}

function onDetailInput(event) {
  if (!event.target.closest('#capability-run-form')) return;
  _formDirty = true;
  clearTimeout(_readinessTimer);
  _readinessTimer = setTimeout(checkFormReadiness, READINESS_DEBOUNCE_MS);
}

async function onDetailSubmit(event) {
  if (event.target.id !== 'capability-run-form') return;
  event.preventDefault();
  const capabilityId = _selectedId;
  try {
    const run = await api(`/api/capabilities/${encodeURIComponent(capabilityId)}/runs`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: readForm() }),
    });
    uiModule?.showToast?.(`${_detail?.name || 'Capability'} started`);
    setHash(`capability-run-${run.id}`);
    await refreshAll();
    document.getElementById(`capability-run-${run.id}`)?.scrollIntoView({ block: 'nearest' });
  } catch (error) { uiModule?.showError?.(error.message); }
}

async function saveDefaults() {
  const capabilityId = _selectedId;
  try {
    const values = readForm();
    await api(`/api/capabilities/${encodeURIComponent(capabilityId)}/defaults`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: values }),
    });
    if (_detail && capabilityId === _selectedId) {
      // Keep the local structure fingerprint in sync so the next poll does
      // not trigger a needless structural rebuild of the form.
      _detail.saved_defaults = values;
      _fpStructure = structureFp(_detail);
      _formDirty = false;
    }
    uiModule?.showToast?.('Capability defaults saved');
  } catch (error) { uiModule?.showError?.(error.message); }
}

async function toggleEnabled() {
  if (!_detail) return;
  try {
    await api(`/api/capabilities/${encodeURIComponent(_detail.id)}/enabled`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !_detail.enabled }),
    });
    await refreshAll();
  } catch (error) { uiModule?.showError?.(error.message); }
}

function openReport(runId) {
  // The report is rendered on demand from its stored markdown into the same
  // magazine-style HTML page deep research uses (see capability_routes
  // /runs/{run_id}/report.html). Opening that endpoint — rather than the raw
  // markdown document — is the single, canonical way to view a report.
  window.open(`${API_BASE}/api/capabilities/runs/${encodeURIComponent(runId)}/report.html`, '_blank', 'noopener');
}

async function discussDocument(documentId) {
  await window.documentModule?.loadDocument(documentId);
  close();
  const input = document.getElementById('message-input');
  if (input) {
    input.value = 'Help me interpret this capability report. Summarize the findings, caveats, provenance, and useful next actions.';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
  }
}

async function exportDocument(documentId) {
  try {
    const response = await fetch(`${API_BASE}/api/documents/export-zip`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: [documentId] }),
    });
    if (!response.ok) throw new Error(`Export failed (${response.status})`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = 'capability-report.zip';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { uiModule?.showError?.(error.message); }
}

async function archiveDocument(documentId) {
  try {
    await api(`/api/document/${encodeURIComponent(documentId)}/archive?archived=true`, { method: 'POST' });
    uiModule?.showToast?.('Report archived');
    await refreshAll();
  } catch (error) { uiModule?.showError?.(error.message); }
}

async function deleteDocument(documentId) {
  if (!window.confirm('Delete this report? The capability run history remains.')) return;
  try {
    await api(`/api/document/${encodeURIComponent(documentId)}`, { method: 'DELETE' });
    uiModule?.showToast?.('Report deleted');
    await refreshAll();
  } catch (error) { uiModule?.showError?.(error.message); }
}

async function toggleRunLog(button) {
  const pre = button.closest('.cap-run')?.querySelector('.cap-run-log');
  if (!pre) return;
  if (!pre.hidden) { pre.hidden = true; return; }
  pre.hidden = false;
  if (!pre.textContent) pre.textContent = 'Loading log…';
  try {
    const data = await api(`/api/capabilities/runs/${encodeURIComponent(button.dataset.runLog)}/log`);
    pre.textContent = data.output || '(no output yet)';
  } catch (error) {
    pre.textContent = `Could not load log: ${error.message}`;
  }
}

async function cancelRun(runId) {
  try {
    await api(`/api/capabilities/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' });
    await refreshAll();
  } catch (error) { uiModule?.showError?.(error.message); }
}

async function rerunRun(runId) {
  try {
    const run = await api(`/api/capabilities/runs/${encodeURIComponent(runId)}/rerun`, { method: 'POST' });
    setHash(`capability-run-${run.id}`);
    await refreshAll();
    document.getElementById(`capability-run-${run.id}`)?.scrollIntoView({ block: 'nearest' });
  } catch (error) { uiModule?.showError?.(error.message); }
}

async function reloadRegistry() {
  try {
    await api('/api/capabilities/reload', { method: 'POST' });
    await refreshAll();
    uiModule?.showToast?.('Capability registry reloaded');
  } catch (error) { uiModule?.showError?.(error.message); }
}

function onDetailClick(event) {
  const button = event.target.closest('button');
  if (!button) return;
  const data = button.dataset;
  // Chip toggle for source selection
  if (data.chipValue !== undefined) {
    button.classList.toggle('active');
    const chipList = button.closest('.cap-chip-list');
    if (chipList) {
      const hiddenInput = chipList.querySelector('input[type="hidden"]');
      if (hiddenInput) {
        const activeChips = [...chipList.querySelectorAll('.cap-chip.active')];
        hiddenInput.value = activeChips.map(c => c.dataset.chipValue).join(',');
        _formDirty = true;
        clearTimeout(_readinessTimer);
        _readinessTimer = setTimeout(checkFormReadiness, READINESS_DEBOUNCE_MS);
      }
    }
    return;
  }
  if (button.id === 'cap-toggle-enabled') return void toggleEnabled();
  if (button.id === 'cap-save-defaults') return void saveDefaults();
  if (button.id === 'cap-open-tasks') return void window.tasksModule?.openCapabilitySchedule(_selectedId);
  if (button.id === 'cap-open-model-settings') return void settingsModule.open('ai');
  if (button.id === 'cap-open-integration-settings') return void settingsModule.open('integrations');
  if (data.retryDetail !== undefined) return void refreshDetail();
  if (data.taskId) return void window.tasksModule?.openTasks(data.taskId);
  if (data.openTask) return void window.tasksModule?.openTasks(data.openTask);
  if (data.openReport) return void openReport(data.openReport);
  if (data.chatDocument) return void discussDocument(data.chatDocument);
  if (data.exportDocument) return void exportDocument(data.exportDocument);
  if (data.archiveDocument) return void archiveDocument(data.archiveDocument);
  if (data.deleteDocument) return void deleteDocument(data.deleteDocument);
  if (data.runLog) return void toggleRunLog(button);
  if (data.cancelRun) return void cancelRun(data.cancelRun);
  if (data.rerun) return void rerunRun(data.rerun);
}

function onCatalogClick(event) {
  const button = event.target.closest('[data-capability-id]');
  if (button) selectCapability(button.dataset.capabilityId);
}

/* ------------------------------------------------------------------ */
/* Indicator, polling, lifecycle                                       */
/* ------------------------------------------------------------------ */

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
    if (document.hidden) return;
    if (_open) { refreshAll().catch(() => {}); return; }
    // Pane is closed: keep the rail indicator fresh only while runs are
    // active, then stop instead of polling forever.
    if (window._capabilityRunsActive) refreshCatalog().catch(() => {});
    else stopPolling();
  }, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = null;
}

export async function open(selectId) {
  if (_open) {
    if (selectId && selectId !== _selectedId) await selectCapability(selectId);
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
        <button id="capabilities-reload" type="button" title="Reload registry" hidden>Reload registry</button>
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
      <main id="capability-detail" class="capability-detail"><div class="cap-empty">Loading capabilities…</div></main>
    </div>
  </div>`;
  document.body.appendChild(overlay);
  const pane = overlay.querySelector('.capabilities-pane');
  makeWindowDraggable(pane, overlay.querySelector('.capabilities-header'));
  overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
  document.getElementById('capabilities-close')?.addEventListener('click', close);
  document.getElementById('capabilities-reload')?.addEventListener('click', reloadRegistry);
  document.getElementById('capability-search')?.addEventListener('input', renderCatalog);
  document.getElementById('capability-filter')?.addEventListener('change', renderCatalog);
  const catalogList = document.getElementById('capability-catalog-list');
  catalogList?.addEventListener('click', onCatalogClick);
  const detailEl = document.getElementById('capability-detail');
  detailEl?.addEventListener('click', onDetailClick);
  detailEl?.addEventListener('input', onDetailInput);
  detailEl?.addEventListener('submit', onDetailSubmit);
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
  const hashCapability = location.hash.match(/^#capability-(?!run-)([^/]+)$/)?.[1];
  const target = selectId || hashCapability;
  if (target && target !== _selectedId) {
    _selectedId = target;
    resetDetailState();
  }
  _fpCatalog = '';
  _fpStructure = '';
  await refreshAll();
  const hashRun = location.hash.match(/^#capability-run-(.+)$/)?.[1];
  if (hashRun) document.getElementById(`capability-run-${hashRun}`)?.scrollIntoView({ block: 'nearest' });
  document.getElementById('capability-search')?.focus();
  startPolling();
}

export function close() {
  _open = false;
  // Invalidate any in-flight detail fetch so it cannot render later.
  _detailSeq += 1;
  document.getElementById('capabilities-overlay')?.remove();
  if (_escHandler) document.removeEventListener('keydown', _escHandler);
  _escHandler = null;
  clearTimeout(_readinessTimer);
  _readinessTimer = null;
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
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && _open) refreshAll().catch(() => {});
  });
  if (location.hash.startsWith('#capability-')) open();
  refreshCatalog().catch(() => {});
}

const capabilityModule = { init, open, close, toggle, isOpen };
window.capabilityModule = capabilityModule;
export default capabilityModule;
