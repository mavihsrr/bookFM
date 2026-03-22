# bookFM

Simple async Python prototype for adaptive reading music.

## What it does
- Accepts pasted text, `.txt`, or `.epub`
- Splits content into reading sections
- Uses Gemini to turn each section into a compact music plan
- Uses Lyria RealTime to generate short PCM clips for reading playback
- Uses strict JSON response schema to keep model outputs valid

## Quick start
1. Create a virtual environment and install the package:

```bash
python3 -m venv .venv
./.venv/bin/pip install -e .
```

2. Set `GEMINI_API_KEY` and `OPENAI_API_KEY` in `.env` or your shell
3. Use the CLI:

```bash
PYTHONPATH=src ./.venv/bin/python -m bookfm.cli inspect --text "Chapter 1\n\nThe rain fell softly over the city."
PYTHONPATH=src ./.venv/bin/python -m bookfm.cli analyze --text-file samples/sample_book.txt
PYTHONPATH=src ./.venv/bin/python -m bookfm.cli generate --text-file samples/sample_book.txt --section-index 0
```

## API server
Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m bookfm.api
```

Open API docs:
- `http://localhost:8000/docs`
- UI: `http://localhost:8000/`

Main endpoints:
- `POST /v1/inspect`
- `POST /v1/generate/live`
- `POST /v1/inspect/upload`
- `POST /v1/generate/live/upload`
- `GET /v1/files/{name}`

## Notes
- Audio output is raw 16-bit stereo PCM at 48kHz.
- This version supports user-provided content only.
- Semantic chunking supports `openai` and `google` embedding backends.
- Reading speed is configurable with `--reading-speed-wpm` and affects chunk sizing and music pacing.
