"""
Kami Subs — local Whisper + translation backend.

WebSocket protocol:
  client -> server (text JSON):
    { "type": "config",
      "sampleRate": 16000,
      "sourceLang": "auto" | "en" | "es" | ...,
      "targetLang": "ar",
      "task": "transcribe" | "translate" }
  client -> server (binary): raw little-endian Int16 PCM, mono, sampleRate Hz

  server -> client (text JSON):
    { "type": "transcript", "text": "...", "isFinal": true }
    { "type": "error", "message": "..." }
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

# Make pip-installed NVIDIA libs discoverable by ctranslate2 on Windows.
# Without this, faster-whisper crashes with "cublas64_12.dll not found"
# even though the package is installed.
def _register_nvidia_dll_dirs() -> None:
    if sys.platform != "win32":
        return
    # `nvidia.cublas` etc are themselves PEP-420 namespace packages — they have
    # no __init__.py, so __file__ is None. Use __path__ (which IS populated for
    # namespace packages) to locate the install root.
    nvidia_root: Path | None = None
    for mod_name in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc"):
        try:
            mod = __import__(mod_name, fromlist=["__path__"])
        except ImportError:
            continue
        paths = list(getattr(mod, "__path__", []) or [])
        if paths:
            nvidia_root = Path(paths[0]).resolve().parent
            break
    if nvidia_root is None:
        print("[kami-subs] nvidia packages not installed; running on CPU only")
        return
    print(f"[kami-subs] nvidia root: {nvidia_root}")

    bin_dirs = [nvidia_root / sub for sub in (
        "cuda_runtime/bin", "cublas/bin", "cudnn/bin", "cuda_nvrtc/bin",
    )]
    bin_dirs = [d for d in bin_dirs if d.exists()]

    for d in bin_dirs:
        try:
            os.add_dll_directory(str(d))
        except (AttributeError, OSError):
            pass
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")

    # Preload the critical DLLs explicitly. `os.add_dll_directory` doesn't
    # always reach the threads ctranslate2 spawns for GPU work, so we force
    # them into the process address space here. Once loaded, the OS resolves
    # the same name to the in-memory module from any thread.
    import ctypes
    # ORDER MATTERS: load CUDA runtime first since cuBLAS/cuDNN depend on it.
    preload = [
        ("cuda_runtime/bin", "cudart64_12.dll"),
        ("cublas/bin",       "cublas64_12.dll"),
        ("cublas/bin",       "cublasLt64_12.dll"),
        ("cuda_nvrtc/bin",   "nvrtc64_120_0.dll"),
        ("cudnn/bin",        "cudnn64_9.dll"),
        ("cudnn/bin",        "cudnn_cnn64_9.dll"),
        ("cudnn/bin",        "cudnn_ops64_9.dll"),
        ("cudnn/bin",        "cudnn_engines_precompiled64_9.dll"),
        ("cudnn/bin",        "cudnn_engines_runtime_compiled64_9.dll"),
        ("cudnn/bin",        "cudnn_graph64_9.dll"),
        ("cudnn/bin",        "cudnn_heuristic64_9.dll"),
        ("cudnn/bin",        "cudnn_adv64_9.dll"),
    ]
    for sub, name in preload:
        full = nvidia_root / sub / name
        if not full.exists():
            continue
        try:
            ctypes.WinDLL(str(full))
        except OSError as e:
            print(f"[kami-subs] failed to preload {name}: {e}")

_register_nvidia_dll_dirs()

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from config import (
    MODEL_SIZE, DEVICE, COMPUTE_TYPE, TRANSLATOR,
    HOST, PORT, SAMPLE_RATE, VAD_FILTER,
)

log = logging.getLogger("kami-subs")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ----- model load (lazy, once per process) ----------------------------------
_model = None

def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log.info("loading whisper model=%s device=%s compute=%s", MODEL_SIZE, DEVICE, COMPUTE_TYPE)
        _model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
        log.info("whisper model ready")
    return _model


# ----- hallucination filter -------------------------------------------------
#
# Whisper was trained on millions of fansub files that ended with translator
# credits. On uncertain audio (intro music, accents, silence the gate missed)
# it "completes" by generating those credits — most commonly:
#   "ترجمة موقع xxxx.com" / "ترجمة وتعديل ..." / "Subtitles by ..."
#   "Amara.org community" / "addic7ed.com" / "opensubtitles..."
#   "Thanks for watching" / "Please subscribe" / "شكرا للمشاهدة"
#
# Strategy: only drop the chunk if the ENTIRE transcript looks like a credit
# line. Don't filter on substring match — real video content might genuinely
# reference a website (e.g. a news clip saying "from cnn.com").

_HALLUCINATION_PATTERNS = [
    # Arabic subtitle credits — the dominant hallucination in the user's case.
    # ترجمة (sing), ترجمات (plural), ترجم (verb), ترجمها (he translated it) —
    # all valid lead-ins to a credit. \S* covers the suffix variants.
    re.compile(r"^\s*ترجم\S*\s+(?:موقع|من|بواسطة|وتعديل|تعديل|وعدل|عدل|"
               r"ورفع|وتوقيت|وتدقيق|وإنتاج|فيلم|الفيلم|الحلقة|للعربية)",
               re.IGNORECASE),
    re.compile(r"^\s*ترجم\S*\s+\S+\s*$", re.IGNORECASE),
    re.compile(r"^\s*شكر[ا]?\s+(?:للمشاهدة|على المشاهدة)\s*[!.\s]*$", re.IGNORECASE),
    # English subtitle credits
    re.compile(r"^\s*(?:subtitles?|captions?)\s+(?:by|provided\s+by|from)\b", re.IGNORECASE),
    re.compile(r"^\s*subtitled\s+by\b", re.IGNORECASE),
    re.compile(r"^\s*(?:transcript|translation)\s+by\b", re.IGNORECASE),
    re.compile(r"^\s*(?:thank\s+you|thanks)\s+for\s+watching[!.\s]*$", re.IGNORECASE),
    re.compile(r"^\s*(?:please\s+)?(?:like\s+and\s+)?subscribe\b", re.IGNORECASE),
    # Whole text is just a known subtitle-site domain
    re.compile(r"^\s*(?:www\.|https?://)?(?:amara\.org|addic7ed\.com|opensubtitles|subscene|"
               r"podnapisi|subdl|subtitleseeker|yifysubtitles)\b\S*\s*$", re.IGNORECASE),
    # Whole text is a single bare domain (e.g. "xxx.com")
    re.compile(r"^\s*(?:www\.|https?://)?[a-z0-9-]{2,}\.(?:com|net|org|tv|io|co|me)\b\S*\s*$",
               re.IGNORECASE),
    # Music tags whisper sometimes emits in transcribe mode
    re.compile(r"^\s*[\[\(]?\s*(?:music|applause|silence|♪+)\s*[\]\)]?\s*$", re.IGNORECASE),
]


def looks_like_hallucination(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return any(p.search(t) for p in _HALLUCINATION_PATTERNS)


# ----- translation ----------------------------------------------------------
def translate(text: str, src: str, tgt: str) -> str:
    if not text.strip() or src == tgt:
        return text
    if TRANSLATOR == "none":
        return text
    try:
        if TRANSLATOR == "google":
            from deep_translator import GoogleTranslator
            src_arg = "auto" if src in (None, "", "auto") else src
            return GoogleTranslator(source=src_arg, target=tgt).translate(text)
    except Exception as e:
        log.warning("translation failed (%s -> %s): %s", src, tgt, e)
    return text


# ----- session --------------------------------------------------------------
@dataclass
class Session:
    sample_rate: int = SAMPLE_RATE
    source_lang: str = "auto"
    target_lang: str = "ar"
    task: str = "transcribe"
    chunk_id: int = 0
    history: list[str] = field(default_factory=list)  # rolling last N final lines

    def add_final(self, line: str):
        self.history.append(line)
        if len(self.history) > 8:
            self.history = self.history[-8:]

    @property
    def initial_prompt(self) -> str:
        # Soft prompt to help whisper keep context coherent across chunks.
        return " ".join(self.history)[-400:] if self.history else ""


def transcribe_chunk(session: Session, pcm_int16: np.ndarray) -> tuple[str, str]:
    """Returns (raw_text, detected_lang)."""
    if pcm_int16.size == 0:
        return "", session.source_lang or "auto"
    audio = pcm_int16.astype(np.float32) / 32768.0
    model = get_model()
    lang = None if session.source_lang in (None, "", "auto") else session.source_lang
    segments, info = model.transcribe(
        audio,
        language=lang,
        task=session.task if session.task in ("transcribe", "translate") else "transcribe",
        vad_filter=VAD_FILTER,
        beam_size=1,                   # fast; bump to 5 for quality
        condition_on_previous_text=False,
        initial_prompt=session.initial_prompt or None,
        no_speech_threshold=0.6,
        # Hallucination guardrails — when whisper is uncertain it tends to
        # output fansub credits (training-data artifact). Tight thresholds
        # force a bail-out instead of fabrication:
        #   - compression_ratio > 2.4 → output is repetitive/garbage → drop
        #   - avg_logprob < -1.0     → low confidence → drop
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
    )
    parts = [seg.text for seg in segments]
    text = "".join(parts).strip()
    return text, (info.language if info and info.language else (lang or "auto"))


async def _handle_chunk(ws: WebSocket, session: Session, loop, raw_bytes: bytes) -> None:
    """One audio chunk: silence-gate -> transcribe -> translate -> send."""
    pcm = np.frombuffer(raw_bytes, dtype=np.int16)
    session.chunk_id += 1
    cid = session.chunk_id

    if pcm.size:
        rms = float(np.sqrt(np.mean((pcm.astype(np.float32) / 32768.0) ** 2)))
        peak = float(np.max(np.abs(pcm)) / 32768.0)
    else:
        rms = peak = 0.0

    # Silence gate. Whisper hallucinates on silent audio by repeating the
    # initial_prompt context — exactly what causes "the last word spams
    # when the video pauses." Skip transcribe for sub-threshold chunks and
    # send empty text so the overlay clears.
    SILENCE_RMS = 0.005   # voice/music typically > 0.05
    if rms < SILENCE_RMS:
        log.info("chunk #%d: silence skip (rms=%.4f peak=%.3f)", cid, rms, peak)
        await ws.send_text(json.dumps({
            "type": "transcript",
            "text": "", "raw": "",
            "chunkId": cid, "isFinal": True, "silence": True,
        }))
        return

    raw, detected = await loop.run_in_executor(None, transcribe_chunk, session, pcm)
    if not raw:
        log.info("chunk #%d: empty transcript (lang=%s) rms=%.4f peak=%.3f",
                 cid, detected, rms, peak)
        return

    # Drop whole-chunk fansub-credit hallucinations BEFORE translating or
    # adding to history. If we add them to history they prime more
    # hallucinations on subsequent chunks via initial_prompt context.
    if looks_like_hallucination(raw):
        log.info("chunk #%d: hallucination filter dropped raw=%r", cid, raw)
        await ws.send_text(json.dumps({
            "type": "transcript",
            "text": "", "raw": raw,
            "chunkId": cid, "isFinal": True, "filtered": "hallucination",
        }, ensure_ascii=False))
        return

    src = detected if session.source_lang == "auto" else session.source_lang
    if session.task == "translate" and session.target_lang == "en":
        out = raw  # whisper already translated to English
    else:
        out = await loop.run_in_executor(
            None, translate, raw, src, session.target_lang
        )

    log.info("chunk #%d [%s->%s] raw=%r out=%r", cid, src, session.target_lang, raw, out)
    session.add_final(raw)
    await ws.send_text(json.dumps({
        "type": "transcript",
        "text": out, "raw": raw,
        "detectedLang": detected,
        "chunkId": cid, "isFinal": True,
    }, ensure_ascii=False))


# ----- websocket loop -------------------------------------------------------
async def handle_socket(ws: WebSocket):
    await ws.accept()
    session = Session()
    log.info("client connected")

    try:
        # First message must be config (text JSON).
        first = await ws.receive()
        if "text" in first and first["text"]:
            try:
                cfg = json.loads(first["text"])
                if cfg.get("type") == "config":
                    session.sample_rate = int(cfg.get("sampleRate", SAMPLE_RATE))
                    session.source_lang = cfg.get("sourceLang", "auto") or "auto"
                    session.target_lang = cfg.get("targetLang", "ar") or "ar"
                    session.task = cfg.get("task", "transcribe") or "transcribe"
                    log.info("config: rate=%s src=%s tgt=%s task=%s",
                             session.sample_rate, session.source_lang,
                             session.target_lang, session.task)
            except json.JSONDecodeError:
                pass

        # Main loop: receive binary PCM chunks, transcribe, translate, push back.
        loop = asyncio.get_running_loop()
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            if "bytes" in msg and msg["bytes"]:
                # Process each chunk in its own try block — a single bad
                # chunk (translator throw, whisper edge case, etc) used to
                # kill the entire WS session. Now we just log and continue.
                try:
                    await _handle_chunk(ws, session, loop, msg["bytes"])
                except Exception as e:
                    log.exception("chunk handler error: %s", e)
                    try:
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "message": f"chunk processing failed: {e}",
                        }))
                    except Exception:
                        pass
                    # Don't break — let the session keep running for the next chunk.

            elif "text" in msg and msg["text"]:
                # Allow runtime reconfig.
                try:
                    cfg = json.loads(msg["text"])
                    if cfg.get("type") == "config":
                        session.source_lang = cfg.get("sourceLang", session.source_lang)
                        session.target_lang = cfg.get("targetLang", session.target_lang)
                        session.task = cfg.get("task", session.task)
                except json.JSONDecodeError:
                    pass

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("session error: %s", e)
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass
    finally:
        log.info("client disconnected")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Warm the model up front so the first chunk isn't slow.
    try:
        get_model()
    except Exception as e:
        log.warning("model warmup failed: %s", e)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {
        "ok": True,
        "model": MODEL_SIZE,
        "device": DEVICE,
        "compute": COMPUTE_TYPE,
        "translator": TRANSLATOR,
        "ws": f"ws://{HOST}:{PORT}/ws",
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await handle_socket(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=HOST, port=PORT, log_level="info")
