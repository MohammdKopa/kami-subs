// Kami Subs — offscreen document
// Captures tab audio via the streamId provided by background.js,
// chunks it into ~3s windows, sends as 16kHz mono PCM over WebSocket to the backend,
// forwards transcripts back to background.js -> content overlay.

let mediaStream = null;
let audioContext = null;
let sourceNode = null;
let processorNode = null;
let passthroughGain = null;
let ws = null;
let settings = null;
let isRunning = false;          // true between start() and stop()
let reconnectAttempts = 0;
let reconnectTimer = null;
const MAX_RECONNECT_DELAY_MS = 5000;

// We buffer 16kHz mono Float32 samples until we hit CHUNK_SECONDS, then emit a chunk.
// 1.0s keeps latency tight on GPU (large-v3-turbo transcribes a 1s chunk in
// well under real-time). The backend re-assembles chunks into whole sentences
// before translating, so a small chunk no longer hurts translation quality.
// Bump back to 2.5–3s if CPU-bound to avoid backlog.
const TARGET_SAMPLE_RATE = 16000;
const CHUNK_SECONDS = 1.0;
const CHUNK_SAMPLES = TARGET_SAMPLE_RATE * CHUNK_SECONDS;
let chunkBuffer = new Float32Array(0);

function concatFloat32(a, b) {
  const out = new Float32Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

// Naive linear-interpolation resampler (input rate -> 16kHz).
// For tighter quality swap in an OfflineAudioContext resample later.
function resampleTo16k(input, inputRate) {
  if (inputRate === TARGET_SAMPLE_RATE) return input;
  const ratio = inputRate / TARGET_SAMPLE_RATE;
  const outLen = Math.floor(input.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const srcIdx = i * ratio;
    const lo = Math.floor(srcIdx);
    const hi = Math.min(lo + 1, input.length - 1);
    const t = srcIdx - lo;
    out[i] = input[lo] * (1 - t) + input[hi] * t;
  }
  return out;
}

function float32ToInt16(float32) {
  const out = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function scheduleReconnect() {
  if (!isRunning) return;            // user hit Stop; don't reconnect
  if (reconnectTimer) return;        // already scheduled
  reconnectAttempts += 1;
  // Exponential backoff capped at 5s: 250, 500, 1000, 2000, 5000, 5000...
  const delay = Math.min(MAX_RECONNECT_DELAY_MS, 250 * Math.pow(2, reconnectAttempts - 1));
  console.warn('[kami-subs offscreen] WS reconnect in', delay, 'ms (attempt', reconnectAttempts + ')');
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (isRunning) openSocket();
  }, delay);
}

function openSocket() {
  const url = (settings && settings.backendUrl) || 'ws://127.0.0.1:8765/ws';
  ws = new WebSocket(url);
  ws.binaryType = 'arraybuffer';

  ws.addEventListener('open', () => {
    reconnectAttempts = 0;
    ws.send(JSON.stringify({
      type: 'config',
      sampleRate: TARGET_SAMPLE_RATE,
      sourceLang: (settings && settings.sourceLang) || 'auto',
      targetLang: (settings && settings.targetLang) || 'ar',
      task: (settings && settings.task) || 'translate'
    }));
    chrome.runtime.sendMessage({ target: 'background', type: 'ws:state', state: 'connected' });
  });

  ws.addEventListener('message', (evt) => {
    try {
      const data = JSON.parse(evt.data);
      if (data.type === 'transcript') {
        chrome.runtime.sendMessage({
          target: 'background',
          type: 'transcript',
          text: data.text,
          isFinal: !!data.isFinal
        });
      } else if (data.type === 'error') {
        chrome.runtime.sendMessage({
          target: 'background',
          type: 'backend:error',
          message: data.message
        });
      }
    } catch (e) {
      console.warn('[kami-subs offscreen] bad ws msg', e);
    }
  });

  ws.addEventListener('error', () => {
    chrome.runtime.sendMessage({ target: 'background', type: 'ws:state', state: 'error' });
    // Don't spam the overlay with errors on transient drops — only the initial
    // connect failure should surface as user-visible. After we've succeeded
    // once (reconnectAttempts started at 0 and got bumped here), reconnect
    // silently in the background.
    if (reconnectAttempts === 0) {
      chrome.runtime.sendMessage({
        target: 'background',
        type: 'backend:error',
        message: 'Cannot reach backend at ' + url + '. Is the server running?'
      });
    }
  });

  ws.addEventListener('close', () => {
    chrome.runtime.sendMessage({ target: 'background', type: 'ws:state', state: 'closed' });
    // If we're still supposed to be running, try to come back. The server
    // session can die without the process dying (one bad chunk → WS closes
    // but uvicorn keeps listening). Reconnect re-attaches to the same server.
    scheduleReconnect();
  });
}

function sendChunkIfReady() {
  while (chunkBuffer.length >= CHUNK_SAMPLES) {
    const chunk = chunkBuffer.slice(0, CHUNK_SAMPLES);
    chunkBuffer = chunkBuffer.slice(CHUNK_SAMPLES);
    const pcm16 = float32ToInt16(chunk);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(pcm16.buffer);
    }
  }
}

async function start(streamId, incomingSettings) {
  settings = incomingSettings || {};
  isRunning = true;
  reconnectAttempts = 0;
  chrome.runtime.sendMessage({ target: 'background', type: 'ws:state', state: 'connecting' });

  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: 'tab',
        chromeMediaSourceId: streamId
      }
    },
    video: false
  });

  audioContext = new AudioContext();
  sourceNode = audioContext.createMediaStreamSource(mediaStream);

  // Keep the user hearing the tab audio (capture mutes the tab by default).
  passthroughGain = audioContext.createGain();
  passthroughGain.gain.value = 1.0;
  sourceNode.connect(passthroughGain).connect(audioContext.destination);

  // Mono mixdown + buffer for chunked send.
  // ScriptProcessorNode is deprecated but works reliably in offscreen contexts
  // without requiring an extra worklet file. Swap to AudioWorklet later if needed.
  processorNode = audioContext.createScriptProcessor(4096, 2, 1);
  processorNode.onaudioprocess = (e) => {
    const inBuf = e.inputBuffer;
    const ch0 = inBuf.getChannelData(0);
    const ch1 = inBuf.numberOfChannels > 1 ? inBuf.getChannelData(1) : ch0;
    const mono = new Float32Array(ch0.length);
    for (let i = 0; i < ch0.length; i++) mono[i] = (ch0[i] + ch1[i]) * 0.5;
    const resampled = resampleTo16k(mono, audioContext.sampleRate);
    chunkBuffer = concatFloat32(chunkBuffer, resampled);
    sendChunkIfReady();
  };
  sourceNode.connect(processorNode);
  // Connect processor to destination with zero gain to keep it running without double audio.
  const sink = audioContext.createGain();
  sink.gain.value = 0;
  processorNode.connect(sink).connect(audioContext.destination);

  openSocket();
}

async function stop() {
  // Flip this FIRST so the WS close handler doesn't schedule a reconnect
  // against a deliberate teardown.
  isRunning = false;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  reconnectAttempts = 0;

  try { if (processorNode) processorNode.disconnect(); } catch (e) {}
  try { if (sourceNode) sourceNode.disconnect(); } catch (e) {}
  try { if (passthroughGain) passthroughGain.disconnect(); } catch (e) {}
  try { if (audioContext) await audioContext.close(); } catch (e) {}
  try { if (mediaStream) mediaStream.getTracks().forEach(t => t.stop()); } catch (e) {}
  try { if (ws && ws.readyState === WebSocket.OPEN) ws.close(); } catch (e) {}

  mediaStream = null;
  audioContext = null;
  sourceNode = null;
  processorNode = null;
  passthroughGain = null;
  ws = null;
  chunkBuffer = new Float32Array(0);
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.target !== 'offscreen') return;
  (async () => {
    try {
      if (msg.type === 'start') {
        await start(msg.streamId, msg.settings);
        sendResponse({ ok: true });
      } else if (msg.type === 'stop') {
        await stop();
        sendResponse({ ok: true });
      }
    } catch (err) {
      console.error('[kami-subs offscreen]', err);
      sendResponse({ ok: false, error: String(err) });
    }
  })();
  return true;
});
