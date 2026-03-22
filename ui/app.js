/* ── Dark mode (persists via localStorage) ─────────────── */
(function () {
  const saved = localStorage.getItem('bookfm-theme');
  if (saved === 'dark') document.documentElement.dataset.theme = 'dark';
})();

document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('dark-mode-toggle');
  if (!btn) return;
  const updateLabel = () => {
    const isDark = document.documentElement.dataset.theme === 'dark';
    btn.querySelector('.toggle-icon').textContent = isDark ? '○' : '◐';
  };
  updateLabel();
  btn.addEventListener('click', () => {
    const isDark = document.documentElement.dataset.theme === 'dark';
    document.documentElement.dataset.theme = isDark ? '' : 'dark';
    localStorage.setItem('bookfm-theme', isDark ? 'light' : 'dark');
    updateLabel();
  });
});

const pageType = document.body.dataset.page;

const state = {
  liveSocket: null,
  readTimer: null,
  paragraphNodes: [],
  paragraphDurations: [],
  currentParagraphIndex: 0,
  freeReadMode: false,
  blurMode: true,
  paragraphInteractionBound: false,
};


class LivePcmPlayer {
  constructor() {
    this.audioContext = null;
    this.masterGain = null;
    this.started = false;
    this.nextPlayTime = 0;
    this.minBufferSeconds = 1.1;
    this.resumeBufferSeconds = 0.75;
    this.targetChunkSeconds = 0.24;
    this.fadeInSeconds = 0.8;
    this.fadeOutSeconds = 1.2;
    this.pendingChunks = [];
    this.pendingBytes = 0;
    this.streamComplete = false;
    this.sampleRate = 48000;
    this.channels = 2;
    this.paused = false;      // ← pause gate
  }

  async ensureContext() {
    if (!this.audioContext) {
      const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
      this.audioContext = new AudioContextCtor({ sampleRate: this.sampleRate });
      this.masterGain = this.audioContext.createGain();
      this.masterGain.gain.value = 1;
      this.masterGain.connect(this.audioContext.destination);
    }
    // Only resume if we are NOT in a user-initiated pause.
    if (!this.paused && this.audioContext.state === "suspended") {
      await this.audioContext.resume();
    }
  }

  reset() {
    this.started = false;
    this.nextPlayTime = 0;
    this.pendingChunks = [];
    this.pendingBytes = 0;
    this.streamComplete = false;
    this.paused = false;
    if (this.masterGain && this.audioContext) {
      this.masterGain.gain.cancelScheduledValues(this.audioContext.currentTime);
      this.masterGain.gain.setValueAtTime(1, this.audioContext.currentTime);
    }
  }

  /** Suspend Web Audio and stop scheduling new chunks. */
  async pause() {
    this.paused = true;
    if (this.audioContext && this.audioContext.state === "running") {
      await this.audioContext.suspend();
    }
  }

  /** Resume Web Audio and continue scheduling queued chunks. */
  async resume() {
    this.paused = false;
    if (this.audioContext && this.audioContext.state === "suspended") {
      await this.audioContext.resume();
    }
    this.flush();
  }

  pendingDurationSeconds() {
    return this.pendingBytes / (this.sampleRate * this.channels * 2);
  }

  async pushChunk(chunk) {
    await this.ensureContext();
    this.pendingChunks.push(new Uint8Array(chunk));
    this.pendingBytes += chunk.byteLength;
    // While paused: accumulate chunks but don't start playback.
    if (this.paused) return;
    if (!this.started && this.pendingDurationSeconds() >= this.minBufferSeconds) {
      this.startPlayback();
    }
    this.flush();
  }

  startPlayback() {
    if (!this.audioContext || !this.masterGain) {
      return;
    }
    this.started = true;
    this.nextPlayTime = this.audioContext.currentTime + 0.15;
    this.masterGain.gain.cancelScheduledValues(this.audioContext.currentTime);
    this.masterGain.gain.setValueAtTime(0.0001, this.nextPlayTime);
    this.masterGain.gain.exponentialRampToValueAtTime(1, this.nextPlayTime + this.fadeInSeconds);
  }

  consumePendingBytes(targetBytes) {
    let remaining = targetBytes;
    const parts = [];

    while (remaining > 0 && this.pendingChunks.length > 0) {
      const head = this.pendingChunks[0];
      if (head.byteLength <= remaining) {
        parts.push(head);
        this.pendingChunks.shift();
        remaining -= head.byteLength;
        continue;
      }
      parts.push(head.slice(0, remaining));
      this.pendingChunks[0] = head.slice(remaining);
      remaining = 0;
    }

    const size = targetBytes - remaining;
    const merged = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) {
      merged.set(part, offset);
      offset += part.byteLength;
    }
    this.pendingBytes -= size;
    return merged.buffer;
  }

  scheduleBuffer(arrayBuffer) {
    if (!this.audioContext || !this.masterGain) {
      return;
    }
    const audioBuffer = pcm16ToAudioBuffer(this.audioContext, arrayBuffer, this.channels, this.sampleRate);
    const source = this.audioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(this.masterGain);
    const startTime = Math.max(this.nextPlayTime, this.audioContext.currentTime + 0.04);
    source.start(startTime);
    this.nextPlayTime = startTime + audioBuffer.duration;
  }

  flush(force = false) {
    if (!this.audioContext) {
      return;
    }

    const chunkBytes = Math.round(this.sampleRate * this.channels * 2 * this.targetChunkSeconds);

    if (this.started && !this.streamComplete && this.nextPlayTime <= this.audioContext.currentTime + 0.06) {
      if (this.pendingDurationSeconds() < this.resumeBufferSeconds) {
        this.started = false;
        return;
      }
      this.startPlayback();
    }

    if (!this.started && this.pendingDurationSeconds() >= this.minBufferSeconds) {
      this.startPlayback();
    }

    while (this.started && (this.pendingBytes >= chunkBytes || (force && this.pendingBytes > 0))) {
      const bytesToSchedule = force ? this.pendingBytes : chunkBytes;
      this.scheduleBuffer(this.consumePendingBytes(bytesToSchedule));
    }
  }

  complete() {
    this.streamComplete = true;
    this.flush(true);
    if (!this.audioContext || !this.masterGain || !this.nextPlayTime) {
      return;
    }
    const fadeStart = Math.max(this.audioContext.currentTime, this.nextPlayTime - this.fadeOutSeconds);
    const currentValue = Math.max(0.0001, this.masterGain.gain.value);
    this.masterGain.gain.cancelScheduledValues(fadeStart);
    this.masterGain.gain.setValueAtTime(currentValue, fadeStart);
    this.masterGain.gain.exponentialRampToValueAtTime(0.0001, this.nextPlayTime + 0.02);
  }
}


function pcm16ToAudioBuffer(audioContext, arrayBuffer, channels, sampleRate) {
  const pcm = new Int16Array(arrayBuffer);
  const frameCount = pcm.length / channels;
  const audioBuffer = audioContext.createBuffer(channels, frameCount, sampleRate);
  for (let channel = 0; channel < channels; channel += 1) {
    const output = audioBuffer.getChannelData(channel);
    for (let i = 0; i < frameCount; i += 1) {
      output[i] = pcm[(i * channels) + channel] / 32768;
    }
  }
  return audioBuffer;
}


const livePlayer = new LivePcmPlayer();


function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}


function paragraphDurationMs(text, wordsPerMinute) {
  const words = Math.max(1, text.trim().split(/\s+/).length);
  const seconds = (words / Math.max(wordsPerMinute, 1)) * 60;
  return Math.max(2400, seconds * 1000);
}


function basePayload(readingSpeed) {
  return {
    reading_speed_wpm: Number(readingSpeed),
    semantic: true,
    embed_backend: "openai",
    embed_model: null,
  };
}


function resetReaderProgress() {
  if (state.readTimer) {
    clearTimeout(state.readTimer);
    state.readTimer = null;
  }
  state.paragraphNodes = [];
  state.paragraphDurations = [];
  state.currentParagraphIndex = 0;
}


function setCurrentParagraph(index, options = {}) {
  const { scroll = true } = options;
  if (!state.paragraphNodes.length) {
    return;
  }

  const safeIndex = Math.max(0, Math.min(index, state.paragraphNodes.length - 1));
  state.currentParagraphIndex = safeIndex;

  state.paragraphNodes.forEach((node, nodeIndex) => {
    node.classList.toggle("is-current", nodeIndex === safeIndex);
    node.classList.toggle("is-near", Math.abs(nodeIndex - safeIndex) === 1);
    node.classList.toggle("is-past", nodeIndex < safeIndex - 1);
    node.classList.toggle("is-future", nodeIndex > safeIndex + 1);
  });

  const currentNode = state.paragraphNodes[safeIndex];
  if (currentNode && scroll) {
    currentNode.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // Update reading progress bar
  const progressFill = document.getElementById("reading-progress-fill");
  if (progressFill && state.paragraphNodes.length > 1) {
    const pct = Math.round((safeIndex / (state.paragraphNodes.length - 1)) * 100);
    progressFill.style.width = `${pct}%`;
  }
}


function runReadingGuide(startIndex = 0) {
  if (!state.paragraphNodes.length || state.freeReadMode) {
    return;
  }

  const safeIndex = Math.max(0, Math.min(startIndex, state.paragraphNodes.length - 1));
  setCurrentParagraph(safeIndex);

  const delay = state.paragraphDurations[safeIndex] || 2600;
  if (safeIndex >= state.paragraphNodes.length - 1) {
    return;
  }

  if (state.readTimer) {
    clearTimeout(state.readTimer);
  }
  state.readTimer = window.setTimeout(() => runReadingGuide(safeIndex + 1), delay);
}


function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}


function ensureGsap() {
  if (!window.gsap || prefersReducedMotion()) {
    return null;
  }
  if (window.ScrollTrigger) {
    window.gsap.registerPlugin(window.ScrollTrigger);
  }
  return window.gsap;
}


function splitWords(element) {
  if (!element || element.dataset.splitReady === "true") {
    return element ? Array.from(element.querySelectorAll(".headline-word")) : [];
  }

  const text = element.textContent.trim();
  const words = text.split(/\s+/).filter(Boolean);
  element.textContent = "";

  const fragment = document.createDocumentFragment();
  const spans = words.map((word, index) => {
    const span = document.createElement("span");
    span.className = "headline-word";
    span.textContent = word;
    span.style.display = "inline-block";
    span.style.willChange = "transform, opacity";
    fragment.appendChild(span);
    if (index < words.length - 1) {
      fragment.appendChild(document.createTextNode(" "));
    }
    return span;
  });

  element.appendChild(fragment);
  element.dataset.splitReady = "true";
  return spans;
}


function animateEntrance(selector, options = {}) {
  const gsap = ensureGsap();
  if (!gsap) {
    return;
  }
  const elements = gsap.utils.toArray(selector);
  if (!elements.length) {
    return;
  }
  gsap.from(elements, {
    opacity: 0,
    y: 24,
    duration: 0.8,
    ease: "power3.out",
    stagger: 0.08,
    ...options,
  });
}


function initHomeAnimations() {
  const gsap = ensureGsap();
  if (!gsap) {
    return;
  }

  const leadHeadline = document.querySelector(".lead-headline");
  const leadWords = splitWords(leadHeadline);

  animateEntrance(".paper-header", { y: -12, duration: 0.65 });
  animateEntrance(".edition-bar span", { y: 10, duration: 0.5, stagger: 0.06 });
  animateEntrance(".masthead-row > *", { y: 14, duration: 0.62, delay: 0.06 });
  animateEntrance(".section-bar > *", { y: 10, duration: 0.6, delay: 0.12 });

  if (leadWords.length) {
    gsap.from(leadWords, {
      opacity: 0,
      yPercent: 92,
      duration: 0.74,
      stagger: 0.03,
      ease: "power4.out",
      delay: 0.08,
    });
  }

  animateEntrance(".lead-story .kicker", { delay: 0.14 });
  animateEntrance(".lead-story .standfirst", { delay: 0.2 });
  animateEntrance(".lead-actions", { delay: 0.28 });
  animateEntrance(".lead-column .print-card", { y: 20, delay: 0.2, stagger: 0.1 });

  if (window.ScrollTrigger) {
    gsap.from(".briefs-row article", {
      opacity: 0,
      y: 16,
      duration: 0.8,
      stagger: 0.1,
      ease: "power3.out",
      scrollTrigger: {
        trigger: ".briefs-row",
        start: "top 88%",
      },
    });

    gsap.utils.toArray(".story-step").forEach((step) => {
      gsap.from(step.children, {
        opacity: 0,
        y: 28,
        duration: 0.8,
        stagger: 0.08,
        ease: "power3.out",
        scrollTrigger: {
          trigger: step,
          start: "top 78%",
        },
      });
    });

    gsap.from(".story-preview", {
      opacity: 0,
      x: -20,
      duration: 0.8,
      ease: "power3.out",
      scrollTrigger: {
        trigger: ".story-layout",
        start: "top 80%",
      },
    });

    gsap.from(".closing-layout > *", {
      opacity: 0,
      y: 18,
      duration: 0.78,
      stagger: 0.1,
      ease: "power3.out",
      scrollTrigger: {
        trigger: ".closing-layout",
        start: "top 82%",
      },
    });
  }
}


function initRoomAnimations() {
  const gsap = ensureGsap();
  if (!gsap) {
    return;
  }

  const roomTitle = document.querySelector(".room-title");
  const roomWords = splitWords(roomTitle);

  animateEntrance(".paper-header", { y: -12, duration: 0.68 });
  animateEntrance(".edition-bar span", { y: 10, duration: 0.5, stagger: 0.06 });
  animateEntrance(".masthead-row > *", { y: 12, duration: 0.62 });
  animateEntrance(".section-bar > *", { y: 10, duration: 0.58, delay: 0.08 });
  animateEntrance(".room-intro .hero-meta span");
  animateEntrance(".room-intro .kicker", { delay: 0.12 });

  if (roomWords.length) {
    gsap.from(roomWords, {
      opacity: 0,
      yPercent: 110,
      duration: 0.9,
      stagger: 0.035,
      ease: "power4.out",
      delay: 0.08,
    });
  }

  animateEntrance(".room-intro .standfirst", { delay: 0.2 });
  animateEntrance(".room-notes p", { delay: 0.28 });
  animateEntrance(".room-side-preview", { y: 24, delay: 0.24, duration: 0.95 });
  animateEntrance(".room-form > *", { y: 18, delay: 0.18, stagger: 0.06, duration: 0.8 });
}


function animateReaderOpen(elements) {
  const gsap = ensureGsap();
  if (!gsap) {
    return;
  }
  gsap.from(elements, {
    opacity: 0,
    y: 24,
    duration: 0.8,
    ease: "power3.out",
    stagger: 0.08,
  });
}


function revealMusicRail(musicRail) {
  const gsap = ensureGsap();
  if (!gsap) {
    return;
  }
  gsap.fromTo(
    musicRail,
    { opacity: 0, y: 28 },
    { opacity: 1, y: 0, duration: 0.6, ease: "power3.out" }
  );
}


function initHomePage() {
  const jumpButtons = Array.from(document.querySelectorAll("[data-jump]"));
  const storySteps = Array.from(document.querySelectorAll(".story-step"));
  const previewLines = Array.from(document.querySelectorAll(".reader-story-preview .preview-line"));
  const previewWrap = document.querySelector(".reader-story-preview");

  jumpButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const id = button.dataset.jump;
      const target = id ? document.getElementById(id) : null;
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });

  if (storySteps.length && previewWrap) {
    const gsap = ensureGsap();
    const setActiveStep = (index) => {
      const safeIndex = Math.max(0, Math.min(index, storySteps.length - 1));
      previewWrap.dataset.step = String(safeIndex);
      storySteps.forEach((step, stepIndex) => {
        step.classList.toggle("is-active", stepIndex === safeIndex);
      });

      previewLines.forEach((line, lineIndex) => {
        line.classList.toggle("preview-line-current", lineIndex === safeIndex);
        line.classList.toggle("preview-line-near", Math.abs(lineIndex - safeIndex) === 1);
        line.classList.toggle("preview-line-far", Math.abs(lineIndex - safeIndex) > 1);
      });

      if (gsap) {
        gsap.to(previewLines, {
          duration: 0.28,
          ease: "power2.out",
          opacity: (_, target) => target.classList.contains("preview-line-current") ? 1 : 0.72,
          y: (_, target) => target.classList.contains("preview-line-current") ? -1 : 0,
          stagger: 0.02,
        });
      }
    };

    if (gsap && window.ScrollTrigger) {
      const media = gsap.matchMedia();
      media.add("(min-width: 981px)", () => {
        window.ScrollTrigger.create({
          trigger: ".story-layout",
          start: "top top+=120",
          end: "bottom bottom-=120",
          pin: previewWrap,
          pinSpacing: false,
        });
      });

      storySteps.forEach((step, index) => {
        window.ScrollTrigger.create({
          trigger: step,
          start: "top 62%",
          end: "bottom 38%",
          onEnter: () => setActiveStep(index),
          onEnterBack: () => setActiveStep(index),
        });
      });
      setActiveStep(0);
    } else {
      const observer = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) {
            return;
          }
          const activeIndex = Number(entry.target.dataset.step || 0);
          setActiveStep(activeIndex);
        });
      }, { threshold: 0.45 });

      storySteps.forEach((step) => observer.observe(step));
      setActiveStep(0);
    }
  }

  initHomeAnimations();
}


function initRoomPage() {
  const brandHome        = document.getElementById("brand-home");
  const composeForm      = document.getElementById("compose-form");
  const fileInput        = document.getElementById("file-input");
  const fileSummary      = document.getElementById("file-summary");
  const readingSpeed     = document.getElementById("reading-speed");
  const readingSpeedValue = document.getElementById("reading-speed-value");
  const composeNote      = document.getElementById("compose-note");
  const loadingScreen    = document.getElementById("loading-screen");
  const loadingText      = document.getElementById("loading-text");
  // compose-stage is not hidden/shown as a whole – we toggle reader-view
  const composeStage     = document.querySelector(".compose-stage");
  const readerView       = document.getElementById("reader-view");
  const readerArticle    = document.getElementById("reader-article");
  const readerTitle      = document.getElementById("reader-title");
  const readerMeta       = document.getElementById("reader-meta");
  const readerModeToggle = document.getElementById("reader-mode-toggle");
  const readerBlurToggle = document.getElementById("reader-blur-toggle");
  const backButton       = document.getElementById("back-button");
  const musicRail        = document.getElementById("music-rail");
  const musicTitle       = document.getElementById("music-title");
  const musicStatus      = document.getElementById("music-status");
  const textInput        = document.getElementById("text-input");
  const playPauseBtn     = document.getElementById("play-pause-btn");
  const iconPause        = document.getElementById("icon-pause");
  const iconPlay         = document.getElementById("icon-play");
  const uploadPanel      = document.getElementById("upload-panel");
  const apiKeyInput      = document.getElementById("gemini-api-key");

  if (apiKeyInput) {
    apiKeyInput.value = localStorage.getItem("bookfm_gemini_key") || "";
  }

  /* ── Play / Pause ───────────────────────────────────── */
  let musicPaused = false;

  const setPlayPauseState = (paused) => {
    musicPaused = paused;
    if (iconPause) iconPause.style.display = paused ? 'none' : '';
    if (iconPlay)  iconPlay.style.display  = paused ? '' : 'none';
    if (playPauseBtn) playPauseBtn.setAttribute('aria-label', paused ? 'Resume music' : 'Pause music');
    if (musicRail) musicRail.classList.toggle('music-paused', paused);
  };

  playPauseBtn?.addEventListener('click', async () => {
    if (!livePlayer.audioContext) return;
    if (livePlayer.paused) {
      // Resume: un-gate the player first, THEN resume the audio context
      await livePlayer.resume();
      setPlayPauseState(false);
      setMusicStatus(musicTitle.textContent || 'Reading session', 'Streaming now');
    } else {
      // Pause: set the gate first so incoming chunks don't fight the suspend
      await livePlayer.pause();
      setPlayPauseState(true);
      setMusicStatus(musicTitle.textContent || 'Reading session', 'Paused');
    }
  });


  /* Speed display */
  readingSpeedValue.textContent = `${readingSpeed.value} WPM`;

  const setFreeReadMode = (enabled) => {
    state.freeReadMode = enabled;
    readerArticle.classList.toggle("is-free-read", enabled);
    if (readerModeToggle) {
      readerModeToggle.textContent = enabled ? "Follow Pace: OFF" : "Follow Pace: ON";
    }

    if (state.readTimer) {
      clearTimeout(state.readTimer);
      state.readTimer = null;
    }

    if (!enabled && state.paragraphNodes.length) {
      runReadingGuide(state.currentParagraphIndex);
    }
  };

  const setBlurMode = (enabled) => {
    state.blurMode = enabled;
    readerArticle.classList.toggle("no-blur-mode", !enabled);
    if (readerBlurToggle) {
      readerBlurToggle.textContent = enabled ? "Blur: ON" : "Blur: OFF";
    }
  };

  const showReader = (visible) => {
    if (composeStage) composeStage.classList.toggle("hidden", visible);
    readerView.classList.toggle("hidden", !visible);
    document.body.classList.toggle("is-reading", visible);
    if (visible) {
      window.scrollTo({ top: 0, behavior: "instant" });
    }
  };

  const setLoading = (isLoading, message = "Buffering the first moments of the session.") => {
    loadingScreen.classList.toggle("hidden", !isLoading);
    loadingText.textContent = message;
  };

  const hideMusicRail = () => {
    musicRail.classList.add("hidden");
  };

  const setMusicStatus = (title, status) => {
    const wasHidden = musicRail.classList.contains("hidden");
    musicTitle.textContent = title;
    musicStatus.textContent = status;
    musicRail.classList.remove("hidden");
    if (wasHidden) {
      revealMusicRail(musicRail);
    }
  };

  const jumpToParagraph = (index) => {
    if (!state.paragraphNodes.length) {
      return;
    }
    const safeIndex = Math.max(0, Math.min(index, state.paragraphNodes.length - 1));
    setCurrentParagraph(safeIndex);

    if (!state.freeReadMode) {
      runReadingGuide(safeIndex);
    }
  };

  const bindParagraphInteraction = () => {
    state.paragraphNodes.forEach((node, index) => {
      node.dataset.index = String(index);
      node.tabIndex = 0;
      node.setAttribute("role", "button");
      node.setAttribute("aria-label", `Jump to paragraph ${index + 1}`);
    });

    if (state.paragraphInteractionBound) {
      return;
    }
    state.paragraphInteractionBound = true;

    readerArticle.addEventListener("click", (event) => {
      const target = event.target.closest(".reader-paragraph");
      if (!target) {
        return;
      }
      const index = Number(target.dataset.index || 0);
      jumpToParagraph(index);
    });

    readerArticle.addEventListener("keydown", (event) => {
      const target = event.target.closest(".reader-paragraph");
      if (!target) {
        return;
      }
      const currentIndex = Number(target.dataset.index || 0);
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        jumpToParagraph(currentIndex);
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        jumpToParagraph(currentIndex + 1);
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        jumpToParagraph(currentIndex - 1);
      }
    });
  };

  const renderReader = (documentPayload) => {
    resetReaderProgress();
    setFreeReadMode(false);
    const sections = documentPayload.sections || [];
    const title = documentPayload.title || "Reading session";

    // Topbar (minimal)
    readerTitle.textContent = title;
    readerMeta.textContent = `${readingSpeed.value} WPM`;

    // Reading masthead (full-size, in article area)
    const displayTitle = document.getElementById("reading-display-title");
    const displayWpm   = document.getElementById("reading-display-wpm");
    if (displayTitle) displayTitle.textContent = title;
    if (displayWpm)   displayWpm.textContent   = `${readingSpeed.value} WPM`;

    const blocks = [];
    sections.forEach((section, sectionIndex) => {
      const paragraphs = section.text
        .split(/\n\s*\n/g)
        .map((part) => part.trim())
        .filter(Boolean);

      paragraphs.forEach((paragraph) => {
        blocks.push(`<p class="reader-paragraph is-future">${escapeHtml(paragraph)}</p>`);
      });

      if (sectionIndex < sections.length - 1) {
        blocks.push('<div class="reader-section-break" aria-hidden="true"></div>');
      }
    });

    readerArticle.innerHTML = blocks.join("");
    state.paragraphNodes = Array.from(readerArticle.querySelectorAll(".reader-paragraph"));
    state.paragraphDurations = state.paragraphNodes.map((node) =>
      paragraphDurationMs(node.textContent || "", Number(readingSpeed.value))
    );

    // Show paragraph count in masthead
    const pgCounter = document.getElementById("reading-pg-counter");
    if (pgCounter) pgCounter.textContent = `${state.paragraphNodes.length} paragraphs`;

    // Reset progress fill
    const progressFill = document.getElementById("reading-progress-fill");
    if (progressFill) progressFill.style.width = "0%";

    bindParagraphInteraction();

    if (state.paragraphNodes.length > 0) {
      runReadingGuide(0);
    }
  };

  const getSourceText = async () => {
    const pasted = textInput.value.trim();
    const file = fileInput.files[0];

    if (pasted) {
      return pasted;
    }

    if (!file) {
      throw new Error("Upload a text file or paste text to begin.");
    }

    const lowerName = file.name.toLowerCase();
    if (!lowerName.endsWith(".txt") && !lowerName.endsWith(".md")) {
      throw new Error("Use a .txt or .md file for the reading room.");
    }

    return file.text();
  };

  const beginReadingSession = async () => {
    const sourceText = await getSourceText();

    if (state.liveSocket) {
      state.liveSocket.close();
      state.liveSocket = null;
    }

    livePlayer.reset();
    resetReaderProgress();
    setLoading(true, "Opening the reading room and starting the ambient stream.");
    setMusicStatus("Ambient stream", "Buffering");

    await new Promise((resolve, reject) => {
      let settled = false;
      let started = false;
      const protocol = window.location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${protocol}://${window.location.host}/v1/stream/live`);
      socket.binaryType = "arraybuffer";
      state.liveSocket = socket;

      const resolveOnce = () => {
        if (settled) {
          return;
        }
        settled = true;
        resolve();
      };

      const rejectOnce = (error) => {
        if (settled) {
          return;
        }
        settled = true;
        reject(error);
      };

      socket.addEventListener("open", () => {
        const apiKey = apiKeyInput ? apiKeyInput.value.trim() : "";
        if (apiKey) {
          localStorage.setItem("bookfm_gemini_key", apiKey);
        }

        socket.send(JSON.stringify({
          ...basePayload(readingSpeed.value),
          text: sourceText,
          gemini_api_key: apiKey,
          section_index: 0,
          count: 4,
          show_prompts: false,
        }));
      });

      socket.addEventListener("message", async (event) => {
        if (typeof event.data !== "string") {
          try {
            await livePlayer.pushChunk(event.data);
            setMusicStatus(musicTitle.textContent || "Reading session", "Streaming now");
          } catch (error) {
            rejectOnce(error);
          }
          return;
        }

        const payload = JSON.parse(event.data);

        if (payload.event === "session_start") {
          started = true;
          renderReader(payload.document);
          showReader(true);
          setLoading(false);
          setMusicStatus(payload.document.title || "Reading session", "Streaming now");
          animateReaderOpen([".reader-topbar", ".reader-article"]);
          return;
        }

        if (payload.event === "complete") {
          livePlayer.complete();
          setPlayPauseState(false);
          setMusicStatus(readerTitle.textContent || "Reading session", "Session complete");
          resolveOnce();
          return;
        }

        if (payload.event === "error") {
          rejectOnce(new Error(payload.detail));
        }
      });

      socket.addEventListener("close", () => {
        state.liveSocket = null;
        if (!started) {
          setLoading(false);
        }
        resolveOnce();
      }, { once: true });

      socket.addEventListener("error", () => {
        rejectOnce(new Error("Could not start the reading session."));
      }, { once: true });
    });
  };

  brandHome?.addEventListener("click", (event) => {
    event.preventDefault();
    window.location.href = "/";
  });

  readerModeToggle?.addEventListener("click", () => {
    setFreeReadMode(!state.freeReadMode);
  });

  readerBlurToggle?.addEventListener("click", () => {
    setBlurMode(!state.blurMode);
  });

  backButton.addEventListener("click", () => {
    if (state.liveSocket) {
      state.liveSocket.close();
    }
    livePlayer.reset();
    resetReaderProgress();
    setFreeReadMode(false);
    setBlurMode(true);
    setLoading(false);
    hideMusicRail();
    showReader(false);
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    fileSummary.textContent = file ? file.name : "No file selected";
    if (uploadPanel) uploadPanel.classList.toggle("is-active", !!file);
  });

  readingSpeed.addEventListener("input", (event) => {
    readingSpeedValue.textContent = `${event.target.value} WPM`;
    if (!readerView.classList.contains("hidden")) {
      readerMeta.textContent = `${event.target.value} WPM`;
      state.paragraphDurations = state.paragraphNodes.map((node) =>
        paragraphDurationMs(node.textContent || "", Number(event.target.value))
      );
      if (!state.freeReadMode && state.paragraphNodes.length) {
        runReadingGuide(state.currentParagraphIndex);
      }
    }
  });

  /* Reset play/pause state when starting a new session */
  composeForm.addEventListener("submit", () => {
    setPlayPauseState(false);
  }, { capture: true });

  composeForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    composeNote.textContent = " ";
    try {
      await beginReadingSession();
    } catch (error) {
      setLoading(false);
      hideMusicRail();
      showReader(false);
      composeNote.textContent = error.message;
    }
  });

  window.addEventListener("beforeunload", () => {
    if (state.liveSocket) {
      state.liveSocket.close();
    }
  });

  hideMusicRail();
  showReader(false);
  setFreeReadMode(false);
  initRoomAnimations();
}


if (pageType === "home") {
  initHomePage();
} else if (pageType === "room") {
  initRoomPage();
}
