"""Runtime config for Kami Subs backend. Override via env vars."""
import os

# faster-whisper model: tiny | base | small | medium | large-v3
# Start with "small" for a good speed/quality balance on CPU.
# Bump to "large-v3" if you have a decent GPU.
MODEL_SIZE = os.getenv("KAMI_MODEL", "small")

# "cpu" | "cuda" | "auto"
# Default to "cpu" — "auto" tries CUDA first and crashes if cublas DLLs aren't installed.
# Set KAMI_DEVICE=cuda explicitly only if you have CUDA + cuBLAS on PATH.
DEVICE = os.getenv("KAMI_DEVICE", "cpu")

# "int8" (CPU), "float16" (GPU), "int8_float16" (GPU low VRAM), "float32"
COMPUTE_TYPE = os.getenv("KAMI_COMPUTE", "int8")

# Translation backend: "google" (deep-translator via web, zero setup) | "none"
# Future: "argos" for fully offline.
TRANSLATOR = os.getenv("KAMI_TRANSLATOR", "google")

HOST = os.getenv("KAMI_HOST", "127.0.0.1")
PORT = int(os.getenv("KAMI_PORT", "8765"))

# Input audio from the extension is always 16kHz mono PCM Int16.
SAMPLE_RATE = 16000

# VAD trims silence/music before transcription. Enabled by default because
# whisper hallucinates fansub credits on non-speech audio — Silero VAD
# kills that at the source by simply not feeding non-speech to the model.
# Set KAMI_VAD=false to disable if you need to caption noisy/quiet content.
VAD_FILTER = os.getenv("KAMI_VAD", "true").lower() in ("1", "true", "yes")

# Max age of a chunk (seconds) before we drop it instead of transcribing.
# Prevents unbounded backlog: if processing slips behind real-time, we
# discard stale chunks so the user always sees what's *currently* playing
# instead of subs from 30 seconds ago.
MAX_CHUNK_LAG_S = float(os.getenv("KAMI_MAX_LAG_S", "3.0"))
