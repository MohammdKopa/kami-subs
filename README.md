# Kami Subs ✨

> Live AI-generated subtitles, overlaid on any browser video, translated into any language — running locally on your machine.

Captures the audio of whatever's playing in your browser tab, transcribes it with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), translates the output, and overlays it on the video. No cloud, no API keys, no per-minute fees. Works on anything that isn't DRM-protected — YouTube, Twitch, podcasts, lectures, vimeo embeds, news streams.

![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)
![Manifest](https://img.shields.io/badge/Chrome-MV3-orange.svg)

---

## Why this exists

Chrome's built-in Live Caption is English-only and doesn't translate. Whisper desktop apps work on files, not live audio. Most "translate any video" tools are paid SaaS that don't work on private content. There's no good free option for "live subs, your language, any video, running on your own GPU."

This is that.

---

## How it works

```
┌────────────────────────┐    PCM 16k mono      ┌──────────────────────────┐
│ Chrome extension       │ ──── WebSocket ────▶ │ Python backend           │
│  • tabCapture audio    │                      │  • faster-whisper        │
│  • chunk + resample    │                      │  • translate → target    │
│  • overlay subtitles   │ ◀── JSON transcripts │  • FastAPI WS @ :8765    │
└────────────────────────┘                      └──────────────────────────┘
        ▲                                                  ▲
        │  click Start                       launches via  │
        └─────────────  Native Messaging ──────────────────┘
```

The backend stays on your machine. The extension talks to it over `ws://127.0.0.1:8765/ws`. The Native Messaging host lets the extension itself spawn the backend when you click Start, so you don't have to run a separate terminal.

---

## Requirements

- **OS:** Windows 10/11 (Linux/macOS support hasn't been written — see [Contributing](#contributing))
- **Chrome or Edge** (Chromium ≥ 116)
- **Python 3.10+** on PATH
- **GPU strongly recommended** for real-time performance — an NVIDIA card with CUDA 12 + cuDNN 9 gets you sub-second latency. CPU works but introduces 3-5s delay with the `small` model.
- ~500 MB disk for the `small` Whisper model (downloaded on first run). Larger models up to ~3 GB.

---

## Install

Clone the repo, then:

### 1. Backend dependencies (one-time)

```powershell
cd kami-subs\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

First Whisper run downloads model weights into your local cache.

### 2. Load the extension

1. Open `chrome://extensions`
2. Toggle **Developer mode** (top right)
3. Click **Load unpacked**
4. Pick the `extension/` folder of this repo
5. Pin the **Kami Subs** action button to your toolbar

The extension ID is pinned to `cbahglicegngghebkgnegbbgbpfdncka` via the manifest `key`, so it stays stable across reloads.

### 3. Register the Native Messaging host (one-time)

This lets the extension auto-start the backend when you click Start.

```powershell
powershell -ExecutionPolicy Bypass -File extension\native\install.ps1
```

That writes `extension\native\com.kamisubs.host.json` and registers it under `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.kamisubs.host` (plus the Edge equivalent). No admin needed — current-user registry only.

To remove, run `uninstall.ps1` from the same folder.

---

## Use

1. Open a tab with a video
2. Click the **Kami Subs** icon → pick model + device → **Start**
3. Backend boots in the background, popup shows `Backend: up (pid …)`
4. Subtitles appear over the video within a chunk or two
5. Hit **Stop** when done — backend is killed automatically

**Skip step 3 (Native Messaging install) if you prefer manual control** — just run `python server.py` in `backend/` yourself. The popup will say `Backend: native host missing` but capture still works.

---

## Settings (popup)

| Field | Default | Notes |
|---|---|---|
| Source language | Auto detect | Set explicitly to skip Whisper's lang detection |
| Target language | Arabic | Translation target |
| Whisper model | small | `tiny` for speed, `large-v3` for quality (GPU only realistically) |
| Device | GPU (CUDA) | `cpu` if you don't have an NVIDIA GPU |
| Font size | 28px | Slider 14–56 |
| Position | Bottom | Bottom / Top |
| Backend URL | `ws://127.0.0.1:8765/ws` | Change if running the backend remotely |

---

## Env vars (backend)

Set these before running `server.py` manually if you want to override defaults. The Native Messaging launcher reads them from the popup settings, so these only matter if you start the backend yourself.

| Var | Default | Options |
|---|---|---|
| `KAMI_MODEL` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3` |
| `KAMI_DEVICE` | `cpu` | `cpu`, `cuda` |
| `KAMI_COMPUTE` | `int8` | `int8` (CPU), `float16` (GPU), `int8_float16` (low VRAM GPU) |
| `KAMI_TRANSLATOR` | `google` | `google` (deep-translator), `none` |
| `KAMI_HOST` | `127.0.0.1` | bind host |
| `KAMI_PORT` | `8765` | bind port |
| `KAMI_VAD` | `false` | Enable Silero VAD pre-filtering |

---

## Troubleshooting

**`Backend: native host missing — run install.ps1`**
The Native Messaging host isn't registered. Re-run `extension\native\install.ps1`. If you previously installed it, check that the registry key `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.kamisubs.host` points to the right path.

**`cublas64_12.dll not found` or `cudnn64_9.dll not found` when launching with `KAMI_DEVICE=cuda`**
The CUDA DLLs need to be in a directory Python can find. The backend tries to locate them automatically inside `.venv/Lib/site-packages/nvidia/*/bin` (installed as part of `nvidia-cublas-cu12` / `nvidia-cudnn-cu12`). If you installed faster-whisper without those pip packages, install them: `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`.

**Subtitles are delayed and getting further behind real-time**
Your model is bigger than your hardware can handle in real-time. Either pick a smaller model in the popup, or switch to GPU device. End-to-end latency on CPU with `small` is ~3-5s and grows under dense speech.

**Subtitles repeat the last word over and over when the video pauses**
Should be fixed in current version (silence gate). If it returns, check that `backend/server.py` has the `SILENCE_RMS` constant and the `_handle_chunk` helper.

**Nothing happens — no transcripts, no errors**
- Confirm the video is playing audio: the extension passes captured audio through, so you should hear normal sound. Silence means tab capture didn't get the audio stream.
- DRM-protected sites (Netflix, Disney+, Shahid, most paid streamers) return silence via `tabCapture` because Widevine encrypts the audio. Works fine on YouTube, Twitch, free streaming sites, lectures, news.

**`Specified native messaging host not found.`**
The host JSON has `allowed_origins` baked in for extension ID `cbahglicegngghebkgnegbbgbpfdncka`. If you changed the manifest `key` and got a different ID, edit `extension/native/com.kamisubs.host.json` (or the `.template`) to match, then re-run `install.ps1`.

---

## Known limitations

- **Windows only** for the auto-start launcher. The extension + backend work cross-platform, but the Native Messaging install script is PowerShell + Windows registry. Linux/macOS users can run the backend manually.
- **DRM sites are silent** — fundamental tab capture limitation, not solvable from this extension.
- **Google Translate rate-limits** — the free `deep-translator` endpoint will throttle if you burn through a lot of long videos. Argos Translate (fully offline) is on the roadmap.
- **Single tab at a time** — Chrome's tabCapture only allows one active capture per extension instance.
- **ScriptProcessorNode is deprecated** but works reliably in offscreen documents today.

---

## Roadmap

- [ ] Argos Translate offline mode (no internet needed for translation)
- [ ] AudioWorklet replacement for ScriptProcessorNode
- [ ] macOS / Linux Native Messaging installer
- [ ] Bilingual mode (original + translation stacked)
- [ ] Per-site overlay position memory
- [ ] Whisper streaming mode (partial transcripts before chunk-end)
- [ ] VAD-aware adaptive chunking instead of fixed 1.5s

---

## File map

```
kami-subs/
├── extension/
│   ├── manifest.json
│   ├── background.js          # MV3 service worker — orchestrates everything
│   ├── offscreen.html         # required host for getUserMedia in MV3
│   ├── offscreen.js           # tab audio capture + chunking + WebSocket client
│   ├── content.js             # subtitle overlay mounted on the active video
│   ├── content.css            # overlay styling (RTL-aware)
│   ├── popup/                 # extension popup UI
│   ├── icons/                 # 16/48/128 PNG — drop your own
│   └── native/                # Native Messaging host (auto-start backend)
│       ├── launcher.bat
│       ├── launcher.py
│       ├── com.kamisubs.host.json.template
│       ├── install.ps1
│       └── uninstall.ps1
└── backend/
    ├── server.py              # FastAPI + WebSocket + whisper + translator
    ├── config.py              # env-driven runtime config
    └── requirements.txt
```

---

## Contributing

PRs welcome, especially:
- Cross-platform installers (macOS / Linux Native Messaging)
- Argos Translate integration for fully-offline mode
- Better resampler than the current linear-interpolation hack
- Streaming partial transcripts

Open an issue first if you're planning anything substantial so we can align.

---

## License

[MIT](./LICENSE) — do what you want, just don't blame me if it breaks. ✨
