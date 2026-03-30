# 📖 BookFM 

**Read with music, not noise.**  
An open-source, async Python prototype that generates live, context-aware ambient music matched perfectly to your reading material and pace. Powered by Google's Gemini Lyria RealTime API.

---

## ✨ Features

- **Context-Aware Ambient Sound:** Analyzes your text to generate music that matches the mood, tone, and pacing of the story.
- **Scroll-Paced Reading Guide:** A beautifully designed web UI that highlights the active paragraph while dimming the rest, keeping you in a flow state.
- **BYOK Security (Bring Your Own Key):** Built from the ground up to be privacy-first. We never store your API keys. They are only used live during your session.
- **Adaptive Pacing:** Set your reading speed (WPM) to align the music generation and chunk sizes with your natural reading flow.
- **Long-Form Support:** Paste large bodies of text directly, or upload `.txt` and `.md` files (perfect for chapters, essays, drafts, or full manuscripts).
- **Semantic Chunking:** Built-in support for multiple embedding backends (`openai` and `google`) to intelligently split chapters into logical reading pieces.

## 🚀 Quick Start

### 1. Installation

Create a virtual environment and install the package:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Environment Setup

Copy the example environment file or create your own `.env` file in the root directory:

```ini
GEMINI_API_KEY=your_gemini_key_here
OPENAI_API_KEY=your_openai_key_here # Optional: For OpenAI embedding backend
```

### 3. Run the API & Web UI

Start the local server (FastAPI):

```bash
PYTHONPATH=src python -m bookfm.api
```

- **Web UI:** `http://localhost:8000/`
- **API Docs:** `http://localhost:8000/docs`

## 🛠️ CLI Usage

You can also test the engine directly from the command line:

**Inspect a short text:**
```bash
PYTHONPATH=src python -m bookfm.cli inspect --text "Chapter 1\n\nThe rain fell softly over the city."
```

**Analyze a full document:**
```bash
PYTHONPATH=src python -m bookfm.cli analyze --text-file samples/sample_book.txt
```

**Generate a music snippet for a specific document section:**
```bash
PYTHONPATH=src python -m bookfm.cli generate --text-file samples/sample_book.txt --section-index 0
```

## 🏗️ Architecture & Tech Stack

- **Backend:** FastAPI (Python), AsyncIO, WebSockets
- **Frontend:** Vanilla HTML/CSS/JS, GSAP (for scroll-triggered animations)
- **AI Models:** Gemini Pro (for content analysis and prompt engineering), Lyria RealTime API (for raw 16-bit stereo PCM audio generation at 48kHz).

## 🔒 Security & Privacy (BYOK Mode)

BookFM is designed with an API-Key-first architecture:
- Your API keys are strictly used to authenticate your current session and securely hit the Google Cloud/Gemini APIs.
- Keys are transmitted via headers securely and validated on the fly.
- **We do not save, log, or persist your API keys.** All server logs are scrubbed of any sensitive authorization credentials to guarantee your privacy.

## 🚢 Deployment

BookFM is built to be easily deployable on modern PaaS platforms like **Render**, Railway, or Heroku. Make sure that your hosting provider allows persistent WebSocket connections, as this is required for the live real-time audio streams.
