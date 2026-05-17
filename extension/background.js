// Kami Subs — background service worker
// Coordinates: popup <-> offscreen (audio capture) <-> content (overlay)
//             popup -> native host (spawns the Python backend)

const OFFSCREEN_DOC = 'offscreen.html';
const NATIVE_HOST   = 'com.kamisubs.host';

let activeTabId = null;
let isCapturing = false;
let wsState = 'idle';           // idle | connecting | connected | error | closed
let backendState = 'unknown';   // unknown | starting | up | down | unavailable
let backendInfo = {};           // { pid?, wsUrl?, lastError? }
let nativePort = null;          // chrome.runtime.Port to native host, or null

async function hasOffscreenDocument() {
  if (chrome.runtime.getContexts) {
    const contexts = await chrome.runtime.getContexts({
      contextTypes: ['OFFSCREEN_DOCUMENT'],
      documentUrls: [chrome.runtime.getURL(OFFSCREEN_DOC)]
    });
    return contexts.length > 0;
  }
  return false;
}

async function ensureOffscreen() {
  if (await hasOffscreenDocument()) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_DOC,
    reasons: ['USER_MEDIA'],
    justification: 'Capture tab audio for live subtitle generation'
  });
}

async function ensureContentScript(tabId) {
  // First try to ping the content script.
  try {
    await chrome.tabs.sendMessage(tabId, { type: 'ping' });
    return; // already there
  } catch (e) {
    // Not loaded — inject programmatically. Required for tabs opened before
    // the extension was installed/reloaded.
  }
  try {
    await chrome.scripting.insertCSS({
      target: { tabId },
      files: ['content.css']
    });
  } catch (e) { /* ignore — may already be injected */ }
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ['content.js']
  });
}

// ---- Native Messaging: spawn the Python backend on demand ------------------
//
// Graceful degradation: if the user hasn't installed the native host, every
// Start still works as long as they launched the backend manually. The host
// is a nice-to-have, not a hard dependency.

function connectNative() {
  // chrome.runtime.connectNative is synchronous — failure shows up on the
  // onDisconnect listener with chrome.runtime.lastError set.
  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST);
  } catch (e) {
    backendState = 'unavailable';
    backendInfo = { lastError: String(e) };
    nativePort = null;
    return false;
  }

  nativePort.onMessage.addListener((msg) => {
    switch (msg.type) {
      case 'started':
        backendState = 'up';
        backendInfo = { pid: msg.pid, wsUrl: msg.wsUrl };
        break;
      case 'already_up':
        backendState = 'up';
        backendInfo = { wsUrl: msg.wsUrl, note: 'attached to existing backend' };
        break;
      case 'stopped':
        backendState = 'down';
        backendInfo = {};
        break;
      case 'status':
        backendState = msg.running ? 'up' : 'down';
        backendInfo = { pid: msg.pid || null, wsUrl: msg.wsUrl };
        break;
      case 'error':
        backendState = 'down';
        backendInfo = { lastError: msg.message };
        console.error('[kami-subs native]', msg.message);
        break;
      case 'log':
        // Backend stdout/stderr, surfaced for debugging. Comment out if noisy.
        console.log('[kami-backend]', msg.line);
        break;
    }
  });

  nativePort.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    if (err) {
      // Most common: "Specified native messaging host not found." — the user
      // hasn't run install.ps1 yet. Mark unavailable so we stop trying.
      backendState = 'unavailable';
      backendInfo = { lastError: err.message || String(err) };
      console.warn('[kami-subs] native host unavailable:', err.message);
    } else if (backendState !== 'down') {
      backendState = 'down';
    }
    nativePort = null;
  });

  return true;
}

async function ensureBackend(settings) {
  // Already attempted and unavailable — don't keep retrying; user will start
  // the backend manually or run install.ps1.
  if (backendState === 'unavailable') return false;
  if (backendState === 'up' && nativePort) return true;

  if (!nativePort) {
    if (!connectNative()) return false;
  }

  // Pull whisper settings off the popup settings object if present.
  const startMsg = {
    type: 'start',
    model:      settings.model      || undefined,
    device:     settings.device     || undefined,
    compute:    settings.compute    || undefined,
    translator: settings.translator || undefined,
  };
  backendState = 'starting';
  try {
    nativePort.postMessage(startMsg);
  } catch (e) {
    backendState = 'unavailable';
    backendInfo = { lastError: String(e) };
    nativePort = null;
    return false;
  }

  // Block startCapture until the backend confirms ready (or errors out).
  // The launcher.py only sends 'started' after the port is actually accepting
  // connections, so 'up' = WS will succeed. Without this wait, the offscreen
  // WS open call races whisper model load (1-4s warm, 10s+ cold large-v3).
  // 60s ceiling matches launcher's deadline.
  await new Promise((resolve) => {
    const t0 = Date.now();
    const tick = setInterval(() => {
      if (backendState === 'up' || backendState === 'unavailable' || Date.now() - t0 > 60000) {
        clearInterval(tick);
        resolve();
      }
    }, 150);
  });
  return backendState !== 'unavailable';
}

function stopBackend() {
  if (!nativePort) return;
  try { nativePort.postMessage({ type: 'stop' }); } catch (e) { /* ignore */ }
  try { nativePort.disconnect(); } catch (e) { /* ignore */ }
  nativePort = null;
  backendState = 'down';
}

async function startCapture(tabId, settings) {
  // Best-effort: try to spawn the backend before we start capturing. If the
  // native host isn't installed, fall through — user may have launched it
  // manually, in which case the WS connect still works.
  await ensureBackend(settings);

  await ensureOffscreen();

  // Make sure the overlay is mounted before we start sending transcripts.
  try {
    await ensureContentScript(tabId);
  } catch (e) {
    console.warn('[kami-subs] could not inject content script (restricted page?):', e);
  }

  const streamId = await new Promise((resolve, reject) => {
    chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (id) => {
      if (chrome.runtime.lastError) return reject(chrome.runtime.lastError);
      resolve(id);
    });
  });

  await chrome.runtime.sendMessage({
    target: 'offscreen',
    type: 'start',
    streamId,
    settings
  });

  activeTabId = tabId;
  isCapturing = true;
  wsState = 'connecting';
  await chrome.storage.local.set({ isCapturing: true, activeTabId: tabId });

  // Tell the content script in that tab to mount the overlay
  try {
    await chrome.tabs.sendMessage(tabId, { type: 'overlay:mount', settings });
  } catch (e) {
    console.warn('[kami-subs] could not message content script yet:', e);
  }
}

async function stopCapture() {
  if (await hasOffscreenDocument()) {
    await chrome.runtime.sendMessage({ target: 'offscreen', type: 'stop' });
  }
  if (activeTabId != null) {
    try {
      await chrome.tabs.sendMessage(activeTabId, { type: 'overlay:unmount' });
    } catch (e) { /* tab may be gone */ }
  }
  // Tear down the backend too — Stop should fully clean up, not leave the
  // Python process running silently. If the user prefers always-on, they can
  // launch server.py manually and we'll attach via 'already_up' next Start.
  stopBackend();
  isCapturing = false;
  wsState = 'idle';
  await chrome.storage.local.set({ isCapturing: false, activeTabId: null });
  activeTabId = null;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (msg.target && msg.target !== 'background') return;

      switch (msg.type) {
        case 'capture:start': {
          const tab = msg.tabId
            ? await chrome.tabs.get(msg.tabId)
            : (await chrome.tabs.query({ active: true, currentWindow: true }))[0];
          await startCapture(tab.id, msg.settings || {});
          sendResponse({ ok: true });
          break;
        }
        case 'capture:stop': {
          await stopCapture();
          sendResponse({ ok: true });
          break;
        }
        case 'capture:status': {
          sendResponse({ isCapturing, activeTabId, wsState, backendState, backendInfo });
          break;
        }
        case 'ws:state': {
          wsState = msg.state;
          break;
        }
        case 'transcript': {
          // Forward transcript from offscreen -> content script overlay
          if (activeTabId != null) {
            try {
              await chrome.tabs.sendMessage(activeTabId, {
                type: 'overlay:text',
                text: msg.text,
                isFinal: msg.isFinal
              });
            } catch (e) { /* ignore */ }
          }
          break;
        }
        case 'backend:error': {
          if (activeTabId != null) {
            try {
              await chrome.tabs.sendMessage(activeTabId, {
                type: 'overlay:error',
                message: msg.message
              });
            } catch (e) { /* ignore */ }
          }
          break;
        }
      }
    } catch (err) {
      console.error('[kami-subs bg]', err);
      sendResponse({ ok: false, error: String(err) });
    }
  })();
  return true; // keep channel open for async sendResponse
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  if (tabId === activeTabId) await stopCapture();
});
