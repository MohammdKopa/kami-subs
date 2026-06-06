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
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
    MODEL_SIZE, DEVICE, COMPUTE_TYPE, TRANSLATOR, NLLB_MODEL,
    HOST, PORT, SAMPLE_RATE, VAD_FILTER, MAX_CHUNK_LAG_S,
    SENTENCE_MAX_CHARS,
)

log = logging.getLogger("kami-subs")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ----- model load (lazy, once per process) ----------------------------------
_model = None
_resolved_device = None   # the device whisper actually loaded on ("cuda"/"cpu")

def get_model():
    global _model, _resolved_device
    if _model is not None:
        return _model
    from faster_whisper import WhisperModel

    # Build an attempt list. "cuda"/"auto" try the GPU first then fall back to
    # CPU so a missing CUDA lib (or a stale "cpu" setting that should've been
    # GPU) can't silently leave turbo crawling at 7s/chunk on the CPU. Explicit
    # "cpu" stays CPU. CRITICAL: large-v3-turbo on CPU is not real-time — the
    # warning below is the single most useful line when captions lag.
    cuda_compute = "float16" if COMPUTE_TYPE in ("int8", "") else COMPUTE_TYPE
    if DEVICE == "cpu":
        attempts = [("cpu", "int8")]
    else:  # "cuda" or "auto"
        attempts = [("cuda", cuda_compute), ("cpu", "int8")]

    last_err = None
    for dev, comp in attempts:
        try:
            log.info("loading whisper model=%s device=%s compute=%s", MODEL_SIZE, dev, comp)
            _model = WhisperModel(MODEL_SIZE, device=dev, compute_type=comp)
            _resolved_device = dev
            if dev == "cpu" and MODEL_SIZE.startswith("large"):
                log.warning("running %s on CPU — this is NOT real-time and "
                            "captions WILL lag. Use a GPU or a smaller model.",
                            MODEL_SIZE)
            log.info("whisper model ready on %s", dev)
            return _model
        except Exception as e:
            last_err = e
            log.warning("whisper load failed on %s (%s); trying next device", dev, e)
    raise RuntimeError(f"could not load whisper on any device: {last_err}")


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
#
# Two backends, selected via KAMI_TRANSLATOR:
#   "google" — deep-translator hits translate.google over the network. Zero
#              setup, decent, but adds a round-trip per sentence and throttles.
#   "nllb"   — Meta NLLB-200 runs locally on the same device as whisper. No
#              network hop (lower, more consistent latency), no rate limit, and
#              stronger Arabic. Heavier first-time setup; falls back to google
#              automatically if its deps/model aren't available.

# ISO-639-1 (what the extension/whisper speak) -> NLLB FLORES-200 codes.
_NLLB_LANG = {
    "ar": "arb_Arab", "en": "eng_Latn", "es": "spa_Latn", "fr": "fra_Latn",
    "de": "deu_Latn", "tr": "tur_Latn", "it": "ita_Latn", "pt": "por_Latn",
    "ru": "rus_Cyrl", "ja": "jpn_Jpan", "ko": "kor_Hang", "zh": "zho_Hans",
    "hi": "hin_Deva", "fa": "pes_Arab", "ur": "urd_Arab", "nl": "nld_Latn",
}

_nllb = None              # (translator, tokenizer) once loaded
_nllb_failed = False      # set True after a load failure so we stop retrying


def _get_nllb():
    """Lazily load NLLB on CTranslate2 (reuses the ct2 already pulled in by
    faster-whisper — no torch at inference time). Returns (translator, tokenizer)
    or None if unavailable, in which case callers fall back to google."""
    global _nllb, _nllb_failed
    if _nllb is not None or _nllb_failed:
        return _nllb
    try:
        from pathlib import Path
        import ctranslate2
        from transformers import AutoTokenizer

        # ct2 needs a converted model dir. Convert once into a cache folder next
        # to this file; subsequent runs load the converted copy directly.
        cache_dir = Path(__file__).resolve().parent / ".nllb_ct2"
        if not (cache_dir / "model.bin").exists():
            log.info("converting %s to CTranslate2 (one-time, ~2.5GB)...", NLLB_MODEL)
            from ctranslate2.converters import TransformersConverter
            TransformersConverter(NLLB_MODEL).convert(str(cache_dir), quantization="int8")

        # Match whatever device whisper actually resolved to; fall back to CPU
        # if the GPU translator can't init (CPU NLLB is fine — it's tiny).
        want_dev = _resolved_device or ("cpu" if DEVICE == "cpu" else "cuda")
        try:
            translator = ctranslate2.Translator(str(cache_dir), device=want_dev)
        except Exception as e:
            log.warning("NLLB on %s failed (%s); using CPU", want_dev, e)
            translator = ctranslate2.Translator(str(cache_dir), device="cpu")
            want_dev = "cpu"
        tokenizer = AutoTokenizer.from_pretrained(NLLB_MODEL)
        _nllb = (translator, tokenizer)
        log.info("NLLB translator ready (device=%s)", want_dev)
    except Exception as e:
        log.warning("NLLB unavailable (%s) — falling back to google. "
                    "Install with: pip install transformers torch sentencepiece", e)
        _nllb_failed = True
        _nllb = None
    return _nllb


def _translate_nllb(text: str, src: str, tgt: str) -> str:
    bundle = _get_nllb()
    if bundle is None:
        return _translate_google(text, src, tgt)
    translator, tokenizer = bundle
    src_code = _NLLB_LANG.get(src)
    tgt_code = _NLLB_LANG.get(tgt)
    if tgt_code is None:
        log.warning("NLLB has no FLORES code for target %r — using google", tgt)
        return _translate_google(text, src, tgt)
    # NLLB needs a source language tag. If detection gave us something we don't
    # map (or "auto"), let the tokenizer default and rely on the target tag.
    if src_code:
        tokenizer.src_lang = src_code
    tokens = tokenizer.convert_ids_to_tokens(tokenizer.encode(text))
    results = translator.translate_batch(
        [tokens], target_prefix=[[tgt_code]], beam_size=1, max_decoding_length=256,
    )
    out_tokens = results[0].hypotheses[0]
    if out_tokens and out_tokens[0] == tgt_code:
        out_tokens = out_tokens[1:]   # strip the target-lang tag we prefixed
    return tokenizer.decode(tokenizer.convert_tokens_to_ids(out_tokens),
                            skip_special_tokens=True)


def _translate_google(text: str, src: str, tgt: str) -> str:
    from deep_translator import GoogleTranslator
    src_arg = "auto" if src in (None, "", "auto") else src
    return GoogleTranslator(source=src_arg, target=tgt).translate(text)


def translate(text: str, src: str, tgt: str) -> str:
    if not text.strip() or src == tgt or TRANSLATOR == "none":
        return text
    try:
        if TRANSLATOR == "nllb":
            return _translate_nllb(text, src, tgt)
        if TRANSLATOR == "google":
            return _translate_google(text, src, tgt)
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
    # Live-caption display model: one line at a time, like normal subtitles.
    # `pending` is the sentence currently being spoken (source text). It's
    # re-translated and shown in full every chunk so the line grows readably;
    # when the sentence finishes we clear it and the next sentence *replaces*
    # the old line on screen (the overlay keeps the last line visible in the
    # gap until then). No stacking of multiple sentences.
    pending: str = ""
    last_detected: str = "auto"


# Sentence-final marks across the languages we caption — Latin, Arabic (؟ ،),
# and CJK fullwidth (。！？). A trailing one of these means "translate now".
_SENTENCE_END = ".!?…。！？؟،;:"


def ends_sentence(text: str) -> bool:
    return text.rstrip().endswith(tuple(_SENTENCE_END))


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
        # initial_prompt is intentionally OMITTED. It primes whisper with the
        # rolling transcript history, which is the #1 cause of fansub-credit
        # hallucinations in live captioning — past credit-shaped fragments
        # in the prompt produce more credit-shaped output. Cross-chunk name
        # consistency loss is worth the trade.
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        # When whisper is uncertain it tends to fabricate fansub credits.
        # Tight thresholds force a bail-out:
        #   - compression_ratio > 2.4 → output is repetitive/garbage → drop
        #   - avg_logprob < -1.0     → low confidence → drop
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
    )
    parts = [seg.text for seg in segments]
    text = "".join(parts).strip()
    return text, (info.language if info and info.language else (lang or "auto"))


async def _render(session: Session, loop) -> str:
    """Translate the in-progress sentence (session.pending) as a whole clause.
    Returns the translated text (or the raw text when no translation applies)."""
    if not session.pending:
        return ""
    src = session.last_detected if session.source_lang == "auto" else session.source_lang
    if session.task == "translate" and session.target_lang == "en":
        return session.pending   # whisper already produced English
    return await loop.run_in_executor(
        None, translate, session.pending, src, session.target_lang
    )


async def _send_line(ws: WebSocket, session: Session, partial: str, is_final: bool) -> None:
    """Push the single visible line (the current sentence, translated)."""
    await ws.send_text(json.dumps({
        "type": "transcript",
        "text": partial, "raw": session.pending,
        "detectedLang": session.last_detected,
        "chunkId": session.chunk_id, "isFinal": is_final,
    }, ensure_ascii=False))


async def commit_pending(ws: WebSocket, session: Session, loop) -> None:
    """Finalize the current sentence: translate + send it as the final line,
    then clear the buffer so the NEXT sentence replaces it on screen. The
    overlay keeps showing this line until the next sentence arrives (or it
    times out on its own — content.js). Used on sentence end and pauses."""
    if not session.pending:
        return
    partial = await _render(session, loop)
    log.info("commit #%d raw=%r out=%r", session.chunk_id, session.pending, partial)
    await _send_line(ws, session, partial, is_final=True)
    session.pending = ""


async def _handle_chunk(
    ws: WebSocket, session: Session, loop, raw_bytes: bytes, arrived_at: float
) -> None:
    """One audio chunk: silence-gate -> transcribe -> buffer -> flush sentence."""
    session.chunk_id += 1
    cid = session.chunk_id

    # Backlog drop. If processing has slipped behind real-time, this chunk
    # is already stale by the time we get to it. Subs from 15s ago are
    # worse UX than no subs at all — drop and let the next (fresher) chunk
    # catch us up. Flush whatever's buffered so we don't lose it.
    lag = time.monotonic() - arrived_at
    if lag > MAX_CHUNK_LAG_S:
        # Behind real-time: skip this chunk and keep the current line on screen.
        log.info("chunk #%d: dropped (lag=%.2fs > %.1fs)", cid, lag, MAX_CHUNK_LAG_S)
        return

    pcm = np.frombuffer(raw_bytes, dtype=np.int16)

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
        # A pause ends the current utterance: finalize whatever's building so it
        # shows in full, then the overlay clears itself after CLEAR_AFTER_MS of
        # no further updates (content.js).
        await commit_pending(ws, session, loop)
        return

    raw, detected = await loop.run_in_executor(None, transcribe_chunk, session, pcm)
    if not raw:
        log.info("chunk #%d: empty transcript (lang=%s) rms=%.4f peak=%.3f",
                 cid, detected, rms, peak)
        return

    # Drop whole-chunk fansub-credit hallucinations BEFORE buffering them.
    # If they entered the buffer they'd corrupt the translated sentence and
    # (via concatenation) the surrounding real words too.
    if looks_like_hallucination(raw):
        # Skip the hallucinated fragment; keep the current line on screen.
        log.info("chunk #%d: hallucination filter dropped raw=%r", cid, raw)
        return

    # Append this chunk to the sentence being spoken, re-translate the WHOLE
    # sentence (full-clause context — the fix for "translation is off"), and
    # show it growing in real time. The translator always sees a complete
    # phrase, never a 1s shard, but the user still gets an update every chunk.
    session.last_detected = detected
    session.pending = (session.pending + " " + raw).strip() if session.pending else raw

    partial = await _render(session, loop)
    await _send_line(ws, session, partial, is_final=False)
    log.info("chunk #%d: pending=%r out=%r", cid, session.pending, partial)

    # When the sentence completes (punctuation) or grows long, clear the buffer
    # so the next sentence starts a fresh line and replaces this one on screen.
    if ends_sentence(session.pending) or len(session.pending) >= SENTENCE_MAX_CHARS:
        session.pending = ""


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
                # Stamp arrival time NOW so the chunk handler can detect
                # backlog. If we stamped inside _handle_chunk, the time would
                # already include the previous chunk's processing wait.
                arrived_at = time.monotonic()
                # Process each chunk in its own try block — a single bad
                # chunk (translator throw, whisper edge case, etc) used to
                # kill the entire WS session. Now we just log and continue.
                try:
                    await _handle_chunk(ws, session, loop, msg["bytes"], arrived_at)
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
