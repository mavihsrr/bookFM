from __future__ import annotations

import os
from pathlib import Path

try:
    import certifi
except ImportError:
    certifi = None

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> bool:
        return False


load_dotenv()
if certifi is not None:
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())


LYRIA_MODEL = "models/lyria-realtime-exp"
DEFAULT_ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "gemini-2.5-flash")
ALLOW_LITE_ANALYSIS_MODEL = os.getenv("ALLOW_LITE_ANALYSIS_MODEL", "0") == "1"
DEFAULT_EMBED_MODEL = "gemini-embedding-001"
DEFAULT_OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-ada-002")


OUTPUT_DIR = Path(".bookfm_output")
OUTPUT_DIR.mkdir(exist_ok=True)
MAX_ANALYSIS_CHARS = 3500
MAX_COMPOSER_PROMPT_CHARS = 260
DEFAULT_READING_SPEED_WPM = 220
DEFAULT_SECTION_TARGET_SECONDS = 28
DEFAULT_SECTION_MIN_SECONDS = 18
DEFAULT_SECTION_MAX_SECONDS = 45
DEFAULT_SECTION_MAX_CHARS = 2600
SECTION_MIN_STREAM_SECONDS = max(6, int(os.getenv("SECTION_MIN_STREAM_SECONDS", "10")))
SECTION_MAX_STREAM_SECONDS = max(SECTION_MIN_STREAM_SECONDS, int(os.getenv("SECTION_MAX_STREAM_SECONDS", "95")))
READING_PACE_BUFFER_RATIO = max(1.0, min(1.7, float(os.getenv("READING_PACE_BUFFER_RATIO", "1.12"))))
PER_SECTION_OVERHEAD_SECONDS = max(0.0, min(4.0, float(os.getenv("PER_SECTION_OVERHEAD_SECONDS", "0.9"))))
DEFAULT_PREFETCH_COUNT = 3
DEFAULT_CROSSFADE_SECONDS = 5
DEFAULT_STREAM_GAIN = max(0.2, min(1.0, float(os.getenv("STREAM_GAIN", "0.58"))))
ENABLE_SEMANTIC_CHUNKING = os.getenv("ENABLE_SEMANTIC_CHUNKING", "1") == "1"
SEMANTIC_CHUNK_MAX_BLOCKS = 160
SAMPLE_RATE = 48_000
CHANNELS = 2
BYTES_PER_SAMPLE = 2
