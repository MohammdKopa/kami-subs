"""Runtime config for Kami Subs backend. Override via env vars."""
import os

# faster-whisper model: tiny | base | small | medium | large-v3 | large-v3-turbo
# Default is "large-v3-turbo": large-v3 with the decoder pruned 32->4 layers,
# ~8x faster than large-v3 at near-large-v3 accuracy, and it keeps every
# language (unlike distil-whisper / Parakeet, which drop Arabic). On GPU it's
# also faster than "small" while transcribing much better — pure win for the
# live-captioning case. CPU users should drop to "small" or "base" in the popup.
MODEL_SIZE = os.getenv("KAMI_MODEL", "large-v3-turbo")

# "cpu" | "cuda" | "auto"
# Default "auto": the server tries CUDA first and falls back to CPU on its own
# (get_model handles the failure gracefully now, and the DLL preloader above
# makes cuBLAS/cuDNN discoverable), so a GPU is used whenever one is available
# without risking a hard crash on CPU-only machines. Force with KAMI_DEVICE=cpu.
DEVICE = os.getenv("KAMI_DEVICE", "auto")

# "int8" (CPU), "float16" (GPU), "int8_float16" (GPU low VRAM), "float32"
COMPUTE_TYPE = os.getenv("KAMI_COMPUTE", "int8")

# Translation backend:
#   "google" — deep-translator via web, zero setup, decent quality, rate-limited
#   "nllb"   — Meta NLLB-200 running locally (offline, no rate limit, stronger
#              Arabic). Needs one-time `pip install transformers torch sentencepiece`
#              and a ~2.5GB model download on first use. Falls back to google if
#              those aren't present, so it's safe to select either way.
#   "none"   — pass the transcript through untranslated
# Default is "nllb": local, offline, no per-sentence network hop, stronger
# Arabic. It auto-falls-back to "google" if the deps/model aren't present yet,
# so this default is safe even before the one-time NLLB setup completes.
TRANSLATOR = os.getenv("KAMI_TRANSLATOR", "nllb")

# HuggingFace id for the NLLB model used when TRANSLATOR=nllb. The distilled
# 600M variant is the sweet spot for live use; bump to
# "facebook/nllb-200-distilled-1.3B" for better quality at higher latency/VRAM.
NLLB_MODEL = os.getenv("KAMI_NLLB_MODEL", "facebook/nllb-200-distilled-600M")

HOST = os.getenv("KAMI_HOST", "127.0.0.1")
PORT = int(os.getenv("KAMI_PORT", "8765"))

# Input audio from the extension is always 16kHz mono PCM Int16.
SAMPLE_RATE = 16000

# VAD trims silence/music before transcription. Enabled by default because
# whisper hallucinates fansub credits on non-speech audio — Silero VAD
# kills that at the source by simply not feeding non-speech to the model.
# Set KAMI_VAD=false to disable if you need to caption noisy/quiet content.
VAD_FILTER = os.getenv("KAMI_VAD", "true").lower() in ("1", "true", "yes")

# --- Sentence-aware translation buffering -----------------------------------
# Whisper transcribes each ~1s chunk independently (condition_on_previous_text
# is off to avoid hallucinations), so translating per-chunk means feeding the
# translator sentence *shards* — the #1 cause of "the translation is off",
# because word order / agreement (especially in Arabic) need the whole clause.
#
# Instead we accumulate raw transcript across chunks and only translate +
# display when the buffer looks like a complete thought: it ends in sentence
# punctuation, a speech pause (silence chunk) ends the utterance, or one of the
# bounds below trips so latency stays capped even mid-monologue.
SENTENCE_MAX_CHUNKS = int(os.getenv("KAMI_SENT_MAX_CHUNKS", "2"))  # ~2s worst case
SENTENCE_MAX_CHARS = int(os.getenv("KAMI_SENT_MAX_CHARS", "160"))

# Consecutive silent chunks (~1s each) after which we drop the last shown
# sentence, so stale text doesn't flash back when speech resumes after a pause.
SILENCE_CLEAR_CHUNKS = int(os.getenv("KAMI_SILENCE_CLEAR_CHUNKS", "3"))

# Max age of a chunk (seconds) before we drop it instead of transcribing.
# Prevents unbounded backlog: if processing slips behind real-time, we
# discard stale chunks so the user always sees what's *currently* playing
# instead of subs from 30 seconds ago.
MAX_CHUNK_LAG_S = float(os.getenv("KAMI_MAX_LAG_S", "3.0"))
