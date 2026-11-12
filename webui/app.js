// AI Page Content Enhancer - frontend logic.
// Talks to the Python backend (see app.py's Api class) via the pywebview
// JS bridge: pywebview.api.<method>(args) returns a Promise. Python pushes
// live progress back by calling window.onPyEvent(...) via evaluate_js.

// Diagnostic marker - app.py's startup probe checks this via evaluate_js to
// tell "app.js never ran at all" apart from "app.js ran but the pywebview
// bridge object never got injected".
window.__appJsLoaded = true;

const CATEGORY_ORDER = [
  'seo_structure', 'ux_conversion', 'ai_llm_visibility', 'eeat_signals',
  'content_clarity', 'cta_strength', 'trust_signals', 'technical_seo',
];
const CATEGORY_LABELS = {
  seo_structure: 'SEO Structure', ux_conversion: 'UX / Conversion',
  ai_llm_visibility: 'AI / LLM Visibility', eeat_signals: 'E-E-A-T Signals',
  content_clarity: 'Content Clarity', cta_strength: 'CTA Strength',
  trust_signals: 'Trust Signals', technical_seo: 'Technical SEO',
};
const STATUS_STYLES = {
  updated: { label: 'Updated', bg: '#ECFDF5', fg: '#047857' },
  already_optimized: { label: 'Page Already Optimized', bg: '#EFF6FF', fg: '#1D4ED8' },
  no_content: { label: 'No Content', bg: '#F1F5F9', fg: '#334155' },
  error: { label: 'Error', bg: '#FEF2F2', fg: '#B91C1C' },
  skipped: { label: 'Skipped', bg: '#F1F5F9', fg: '#334155' },
};

let resultsData = [];
let rowDetailById = {};
let counters = { discovered: 0, completed: 0, errors: 0, tokens: 0, cost: 0.0 };
let isRunning = false;

const $ = (id) => document.getElementById(id);

function fmtMoney(v) { return v ? '$' + Number(v).toFixed(4) : '$0.0000'; }
function fmtTokens(v) { return Number(v || 0).toLocaleString('en-US'); }
function fmtSeconds(v) { return v == null ? '-' : Number(v).toFixed(1) + 's'; }

// ----------------------------------------------------------------- config

async function loadConfig() {
  const cfg = await pywebview.api.load_config();
  if (!cfg) return;
  $('folderInput').value = cfg.folder || '';
  $('fileTypeSelect').value = cfg.file_type || 'Both';
  $('siteUrlInput').value = cfg.site_url || '';
  $('siteContextInput').value = cfg.site_context || '';
  $('effortSelect').value = cfg.effort_label || 'Balanced (recommended)';
  $('modelSelect').value = cfg.model_label || 'Claude Sonnet 4.6 (recommended)';
  if (cfg.save_key && cfg.api_key) {
    $('rememberKeyCheck').checked = true;
    $('apiKeyInput').value = cfg.api_key;
  }
  onModelChanged();
}

function currentSettings() {
  return {
    folder: $('folderInput').value,
    file_type: $('fileTypeSelect').value,
    site_url: $('siteUrlInput').value,
    site_context: $('siteContextInput').value,
    effort_label: $('effortSelect').value,
    model_label: $('modelSelect').value,
    save_key: $('rememberKeyCheck').checked,
    api_key: $('rememberKeyCheck').checked ? $('apiKeyInput').value : '',
  };
}

function saveConfig() {
  pywebview.api.save_config(currentSettings());
}

function onModelChanged() {
  const label = $('modelSelect').value;
  $('modelBadgeSidebar').textContent = 'Model: ' + label;
  $('modelBadgeHeader').textContent = 'Model: ' + label.replace(/\s*\(.*\)$/, '');
}

// ------------------------------------------------------------------ log

function logLine(text) {
  const box = $('logBox');
  const line = document.createElement('div');
  line.textContent = '> ' + text;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function clearLog() {
  $('logBox').innerHTML = '';
  logLine('Ready to enhance your content...');
}

// ---------------------------------------------------------------- kpi cards

function refreshStatCards() {
  $('kpiDiscovered').textContent = counters.discovered;
  $('kpiCompleted').textContent = counters.completed;
  $('kpiErrors').textContent = counters.errors;
  $('kpiTokens').textContent = fmtTokens(counters.tokens);
  $('kpiCost').textContent = fmtMoney(counters.cost);
}

// -------------------------------------------------------------------- table

function statusBadgeHtml(status) {
  const sty = STATUS_STYLES[status] || { label: status, bg: '#F1F5F9', fg: '#334155' };
  return `<span class="inline-block text-xs font-semibold px-2.5 py-1 rounded-full" style="background:${sty.bg};color:${sty.fg}">${sty.label}</span>`;
}

function refreshTable() {
  const body = $('resultsBody');
  body.innerHTML = '';
  rowDetailById = {};
  resultsData.forEach((r, idx) => {
    const before = r.score_before != null ? r.score_before : '-';
    const after = r.score_after != null ? r.score_after : '-';
    const tr = document.createElement('tr');
    tr.className = 'row-fade-in border-b border-slate-50 hover:bg-slate-50 cursor-pointer';
    tr.dataset.idx = idx;
    tr.innerHTML = `
      <td class="py-2.5 font-medium text-slate-800 truncate max-w-[220px]">${escapeHtml(fileName(r.path))}</td>
      <td class="text-center">${statusBadgeHtml(r.status)}</td>
      <td class="text-center text-slate-700">${before}</td>
      <td class="text-center text-slate-700">${after}</td>
      <td class="text-slate-700 truncate max-w-[160px]">${escapeHtml(r.focus_keyword || '')}</td>
      <td class="text-center text-slate-500">${fmtSeconds(r.time_taken_sec)}</td>
      <td class="text-right text-slate-500">${fmtTokens(r.total_tokens)}</td>
      <td class="text-right text-slate-500">${fmtMoney(r.cost_usd)}</td>
    `;
    tr.addEventListener('dblclick', () => showDetailModal(r));
    body.appendChild(tr);
  });
  $('emptyState').classList.toggle('hidden', resultsData.length > 0);
}

function fileName(path) {
  if (!path) return '';
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1];
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// ----------------------------------------------------------------- export

async function exportResults() {
  if (!resultsData.length) {
    alert('There are no results yet.');
    return;
  }
  const rows = resultsData.map((r) => ({
    file: fileName(r.path),
    status_label: (STATUS_STYLES[r.status] || {}).label || r.status,
    score_before: r.score_before, score_after: r.score_after,
    focus_keyword: r.focus_keyword || '', time_taken: (r.time_taken_sec || 0).toFixed(2),
    tokens: r.total_tokens || 0, cost: (r.cost_usd || 0).toFixed(4),
  }));
  const savedPath = await pywebview.api.export_csv(rows);
  if (savedPath) alert('Results exported to:\n' + savedPath);
}

// ------------------------------------------------------------- detail modal

function showDetailModal(r) {
  $('modalFileName').textContent = fileName(r.path);
  $('modalFilePath').textContent = r.path;
  $('modalStatus').textContent = (STATUS_STYLES[r.status] || {}).label || r.status;
  $('modalKeyword').textContent = r.focus_keyword || 'n/a';
  $('modalTime').textContent = fmtSeconds(r.time_taken_sec);
  $('modalTokens').textContent = fmtTokens(r.total_tokens);
  $('modalCost').textContent = fmtMoney(r.cost_usd);
  $('modalScoreBefore').textContent = r.score_before != null ? r.score_before : '-';
  $('modalScoreAfter').textContent = r.score_after != null ? r.score_after : '-';

  if (r.error) {
    $('modalErrorRow').classList.remove('hidden');
    $('modalError').textContent = r.error;
  } else {
    $('modalErrorRow').classList.add('hidden');
  }

  const before = r.breakdown_before || {};
  const after = r.breakdown_after || {};
  const tbody = $('modalBreakdownBody');
  tbody.innerHTML = '';
  CATEGORY_ORDER.forEach((key, i) => {
    const b = before[key], a = after[key];
    const improved = b != null && a != null && a > b;
    const tr = document.createElement('tr');
    tr.className = i % 2 === 1 ? 'bg-slate-50' : '';
    tr.innerHTML = `
      <td class="py-2 px-4 text-slate-800">${CATEGORY_LABELS[key]}</td>
      <td class="py-2 px-4 text-right text-slate-500">${b != null ? b : '-'}</td>
      <td class="py-2 px-4 text-right font-bold" style="color:${improved ? '#16A34A' : '#0F172A'}">${a != null ? a : '-'}</td>
    `;
    tbody.appendChild(tr);
  });

  $('detailModal').classList.remove('hidden');
}

function hideDetailModal() {
  $('detailModal').classList.add('hidden');
}

// --------------------------------------------------------------- start/stop

async function startProcessing() {
  saveConfig();
  const settings = currentSettings();
  const result = await pywebview.api.start_processing(settings);
  if (result && result.error) {
    alert(result.error);
    return;
  }
  resultsData = [];
  refreshTable();
  clearLog();
  logLine(`Starting batch: ${result.total} file(s) found in ${settings.folder}`);

  counters = { discovered: result.total, completed: 0, errors: 0, tokens: 0, cost: 0.0 };
  refreshStatCards();

  $('progressBar').style.width = '0%';
  $('progressPct').textContent = '0%';
  $('progressText').textContent = `0 / ${result.total} files`;
  $('startBtn').disabled = true;
  $('stopBtn').disabled = false;
  isRunning = true;
  $('statusText').textContent = 'Processing...';
}

function stopProcessing() {
  pywebview.api.stop_processing();
  $('stopBtn').disabled = true;
}

// --------------------------------------------------------- python -> js bridge

window.onPyEvent = function (event) {
  const { type, data } = event;

  if (type === 'file_start') {
    $('currentFile').textContent = data.name;
    $('scoreBefore').textContent = '-';
    $('scoreAfter').textContent = '-';
    $('progressText').textContent = `${data.index - 1} / ${data.total} files`;
    logLine('Processing: ' + data.name);

  } else if (type === 'status') {
    if (data.score_before != null) $('scoreBefore').textContent = data.score_before + ' / 100';
    if (data.message) logLine('  ' + data.message);
    if (data.score_after != null) $('scoreAfter').textContent = data.score_after + ' / 100';

  } else if (type === 'file_done') {
    const result = data.result;
    const frac = data.total ? data.index / data.total : 0;
    $('progressBar').style.width = Math.round(frac * 100) + '%';
    $('progressPct').textContent = Math.round(frac * 100) + '%';
    $('progressText').textContent = `${data.index} / ${data.total} files`;

    resultsData.push(result);
    refreshTable();

    if (result.status === 'error') {
      counters.errors += 1;
      logLine('  ERROR: ' + result.error);
    } else {
      counters.completed += 1;
    }
    counters.tokens += result.total_tokens || 0;
    counters.cost += result.cost_usd || 0;
    refreshStatCards();

    const keyword = result.focus_keyword || '';
    if (result.status === 'updated') {
      $('scoreAfter').textContent = result.score_after + ' / 100';
      logLine(`  Done in ${fmtSeconds(result.time_taken_sec)}: ${result.score_before} -> ${result.score_after} ` +
        `(keyword: ${keyword || 'n/a'}, ${fmtTokens(result.total_tokens)} tokens, ${fmtMoney(result.cost_usd)})`);
    } else if (result.status === 'already_optimized') {
      $('scoreAfter').textContent = result.score_after + ' / 100';
      logLine(`  Page Already Optimized (score ${result.score_before}/100) - skipped, no changes made.`);
    } else if (result.status === 'no_content') {
      logLine('  No editable text content found - left unchanged.');
    }

  } else if (type === 'batch_done') {
    $('startBtn').disabled = false;
    $('stopBtn').disabled = true;
    isRunning = false;
    $('statusText').textContent = 'Ready';
    $('currentFile').textContent = 'Idle';
    logLine('Batch finished.');

  } else if (type === 'log') {
    logLine(data.text);
  }
};

// ----------------------------------------------------------------- wiring

function wireEvents() {
  $('browseBtn').addEventListener('click', async () => {
    const folder = await pywebview.api.browse_folder();
    if (folder) $('folderInput').value = folder;
  });
  $('toggleKeyBtn').addEventListener('click', () => {
    const input = $('apiKeyInput');
    input.type = input.type === 'password' ? 'text' : 'password';
  });
  $('startBtn').addEventListener('click', startProcessing);
  $('stopBtn').addEventListener('click', stopProcessing);
  $('exportBtn').addEventListener('click', exportResults);
  $('clearLogBtn').addEventListener('click', clearLog);
  $('closeModalBtn').addEventListener('click', hideDetailModal);
  $('detailModal').addEventListener('click', (e) => { if (e.target.id === 'detailModal') hideDetailModal(); });
  $('modelSelect').addEventListener('change', () => { onModelChanged(); saveConfig(); });
  ['folderInput', 'fileTypeSelect', 'siteUrlInput', 'siteContextInput', 'effortSelect', 'rememberKeyCheck', 'apiKeyInput']
    .forEach((id) => $(id).addEventListener('change', saveConfig));

  clearLog();
  refreshTable();
}

window.addEventListener('pywebviewready', async () => {
  wireEvents();
  await loadConfig();
});

// Fallback: some pywebview backends fire DOMContentLoaded before pywebview
// is injected but 'pywebviewready' is the documented signal - guard in case
// the page loads with pywebview already present (e.g. on reload).
if (window.pywebview) {
  wireEvents();
  loadConfig();
}
