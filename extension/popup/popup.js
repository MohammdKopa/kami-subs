// Kami Subs — popup controller

const $ = (id) => document.getElementById(id);

const els = {
  toggle: $('toggle'),
  status: $('status'),
  backendStatus: $('backendStatus'),
  sourceLang: $('sourceLang'),
  targetLang: $('targetLang'),
  fontSize: $('fontSize'),
  fontSizeVal: $('fontSizeVal'),
  position: $('position'),
  backendUrl: $('backendUrl'),
  model: $('model'),
  device: $('device'),
};

const DEFAULTS = {
  sourceLang: 'auto',
  targetLang: 'ar',
  fontSize: 28,
  position: 'bottom',
  backendUrl: 'ws://127.0.0.1:8765/ws',
  task: 'translate',
  model: 'small',
  device: 'cuda',
};

// compute_type pairs naturally with device — int8 for CPU (fastest there),
// float16 for GPU (fastest there). Expose if we ever need finer control.
function computeFor(device) { return device === 'cuda' ? 'float16' : 'int8'; }

async function loadSettings() {
  const stored = await chrome.storage.local.get('settings');
  const s = { ...DEFAULTS, ...(stored.settings || {}) };
  els.sourceLang.value = s.sourceLang;
  els.targetLang.value = s.targetLang;
  els.fontSize.value = s.fontSize;
  els.fontSizeVal.textContent = s.fontSize;
  els.position.value = s.position;
  els.backendUrl.value = s.backendUrl;
  els.model.value = s.model;
  els.device.value = s.device;
  return s;
}

async function saveSettings() {
  const device = els.device.value;
  const s = {
    sourceLang: els.sourceLang.value,
    targetLang: els.targetLang.value,
    fontSize: parseInt(els.fontSize.value, 10),
    position: els.position.value,
    backendUrl: els.backendUrl.value.trim() || DEFAULTS.backendUrl,
    task: 'translate',
    model: els.model.value,
    device,
    compute: computeFor(device),
  };
  await chrome.storage.local.set({ settings: s });
  return s;
}

function setStatus(text, kind) {
  els.status.textContent = text;
  els.status.className = 'status ' + (kind || 'idle');
}

function setBackendStatus(state, info) {
  // state: unknown | starting | up | down | unavailable
  const map = {
    unknown:     ['Backend: unknown',                'idle'],
    starting:    ['Backend: starting…',              'starting'],
    up:          [`Backend: up${info && info.pid ? ' (pid ' + info.pid + ')' : ''}`, 'up'],
    down:        ['Backend: down',                   'down'],
    unavailable: ['Backend: native host missing — run install.ps1', 'bad'],
  };
  const [text, cls] = map[state] || map.unknown;
  els.backendStatus.textContent = text;
  els.backendStatus.className = 'backend-status ' + cls;
}

async function refresh() {
  const res = await chrome.runtime.sendMessage({ target: 'background', type: 'capture:status' });
  if (!res) return;
  setBackendStatus(res.backendState, res.backendInfo);
  if (res.isCapturing) {
    els.toggle.textContent = 'Stop';
    els.toggle.classList.add('stop');
    switch (res.wsState) {
      case 'connected':  setStatus('Live', 'live'); break;
      case 'connecting': setStatus('Connecting…', 'idle'); break;
      case 'error':
      case 'closed':     setStatus('Backend offline — start server', 'error'); break;
      default:           setStatus('Capturing (no backend)', 'error');
    }
  } else {
    els.toggle.textContent = 'Start';
    els.toggle.classList.remove('stop');
    setStatus('Idle', 'idle');
  }
}

// Poll while popup is open so status reflects WS state changes in real time.
setInterval(refresh, 1000);

async function onToggle() {
  const settings = await saveSettings();
  const status = await chrome.runtime.sendMessage({ target: 'background', type: 'capture:status' });
  if (status && status.isCapturing) {
    await chrome.runtime.sendMessage({ target: 'background', type: 'capture:stop' });
  } else {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tabs.length) return setStatus('No active tab', 'error');
    const res = await chrome.runtime.sendMessage({
      target: 'background',
      type: 'capture:start',
      tabId: tabs[0].id,
      settings
    });
    if (res && !res.ok) setStatus(res.error || 'Failed to start', 'error');
  }
  await refresh();
}

els.fontSize.addEventListener('input', () => {
  els.fontSizeVal.textContent = els.fontSize.value;
});
['sourceLang','targetLang','fontSize','position','backendUrl','model','device'].forEach(k => {
  els[k].addEventListener('change', saveSettings);
});
els.toggle.addEventListener('click', onToggle);

loadSettings().then(refresh);
