const WAKE_RE = /\bhey[, ]+jarvis\b/i;
const MODEL = "qwen3-coder-30b-a3b";
const TTS_MODEL = "speaches-ai/Kokoro-82M-v1.0-ONNX";
const TTS_VOICE = "bm_george";
const BASE = (() => {
  const path = window.location.pathname;
  if (path.includes("/jarvis-orb")) {
    return window.location.origin;
  }
  return window.location.origin;
})();

const els = {
  orb: document.getElementById("orb"),
  status: document.getElementById("status"),
  log: document.getElementById("log"),
  apiKey: document.getElementById("apiKey"),
  btnArm: document.getElementById("btnArm"),
  btnStop: document.getElementById("btnStop"),
  btnTestSpeak: document.getElementById("btnTestSpeak"),
  chatLink: document.getElementById("chatLink"),
};

els.chatLink.href = `${BASE}/?chat=1`;

const state = {
  mode: "idle",
  armed: false,
  wake: null,
  command: null,
  audio: null,
  analyser: null,
  raf: 0,
  busy: false,
  speakGen: 0,
};

const storedKey = localStorage.getItem("jarvis_owui_api_key") || "";
if (storedKey) els.apiKey.value = storedKey;
els.apiKey.addEventListener("change", () => {
  const v = els.apiKey.value.trim();
  if (v) localStorage.setItem("jarvis_owui_api_key", v);
  else localStorage.removeItem("jarvis_owui_api_key");
});

function log(msg) {
  const line = `[${new Date().toLocaleTimeString()}] ${msg}`;
  els.log.textContent = `${line}\n${els.log.textContent}`.trim().slice(0, 1200);
}

function setMode(mode) {
  state.mode = mode;
  els.orb.dataset.state = mode;
  els.status.dataset.state = mode;
  const labels = {
    idle: 'Idle — say “Hey Jarvis”',
    listening: "Listening…",
    thinking: "Thinking…",
    speaking: "Speaking…",
  };
  els.status.textContent = labels[mode] || mode;
}

function authHeaders() {
  const headers = { "Content-Type": "application/json" };
  const key = els.apiKey.value.trim();
  if (key) headers.Authorization = `Bearer ${key}`;
  return headers;
}

async function api(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    credentials: "include",
    ...options,
    headers: {
      ...authHeaders(),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = "";
    try {
      detail = await res.text();
    } catch (_) {}
    throw new Error(`${res.status} ${path}: ${detail.slice(0, 240)}`);
  }
  return res;
}

function SpeechRec() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function makeRecognizer({ continuous, interim }) {
  const Ctor = SpeechRec();
  if (!Ctor) throw new Error("Web Speech API unavailable — use Chrome or Edge.");
  const rec = new Ctor();
  rec.lang = "en-GB";
  rec.continuous = continuous;
  rec.interimResults = interim;
  rec.maxAlternatives = 1;
  return rec;
}

function stopRecognizer(rec) {
  if (!rec) return;
  try {
    rec.onresult = null;
    rec.onerror = null;
    rec.onend = null;
    rec.stop();
  } catch (_) {}
}

function armWake() {
  stopRecognizer(state.wake);
  const rec = makeRecognizer({ continuous: true, interim: true });
  state.wake = rec;
  rec.onresult = (ev) => {
    let transcript = "";
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      transcript += ev.results[i][0].transcript;
    }
    if (WAKE_RE.test(transcript) && !state.busy) {
      log(`Wake heard: “${transcript.trim()}”`);
      beginCommandCapture();
    }
  };
  rec.onerror = (ev) => {
    if (ev.error === "not-allowed") {
      log("Microphone permission denied.");
      disarm();
      return;
    }
    if (ev.error !== "no-speech" && ev.error !== "aborted") {
      log(`Wake listener: ${ev.error}`);
    }
  };
  rec.onend = () => {
    if (state.armed && state.mode !== "listening" && !state.busy) {
      try {
        rec.start();
      } catch (_) {}
    }
  };
  rec.start();
  state.armed = true;
  els.btnArm.disabled = true;
  els.btnStop.disabled = false;
  setMode("idle");
  log("Wake listener armed. Say “Hey Jarvis”.");
}

function disarm() {
  state.armed = false;
  state.busy = false;
  state.speakGen += 1;
  stopRecognizer(state.wake);
  stopRecognizer(state.command);
  state.wake = null;
  state.command = null;
  stopSpeakLoop();
  if (state.audio) {
    try {
      state.audio.pause();
    } catch (_) {}
  }
  els.btnArm.disabled = false;
  els.btnStop.disabled = true;
  setMode("idle");
  document.documentElement.style.setProperty("--speak-level", "0");
  log("Stopped.");
}

function beginCommandCapture() {
  state.busy = true;
  stopRecognizer(state.wake);
  setMode("listening");
  const rec = makeRecognizer({ continuous: false, interim: false });
  state.command = rec;
  let finalText = "";
  rec.onresult = (ev) => {
    finalText = "";
    for (let i = 0; i < ev.results.length; i++) {
      finalText += ev.results[i][0].transcript;
    }
  };
  rec.onerror = (ev) => {
    log(`Command capture: ${ev.error}`);
    finishTurn();
  };
  rec.onend = async () => {
    const text = finalText.trim();
    if (!text) {
      log("No command heard after wake.");
      await speak("Standing by.");
      finishTurn();
      return;
    }
    log(`You: ${text}`);
    await handleUtterance(text);
  };
  try {
    rec.start();
  } catch (err) {
    log(String(err));
    finishTurn();
  }
}

async function handleUtterance(text) {
  setMode("thinking");
  const t0 = performance.now();
  try {
    const reply = await chatAndSpeak(text);
    log(
      `Jarvis (${Math.round(performance.now() - t0)}ms to first audio intent): ${reply}`
    );
  } catch (err) {
    log(String(err));
    await speak("I am having trouble reaching my systems.");
  } finally {
    finishTurn();
  }
}

function finishTurn() {
  state.busy = false;
  stopRecognizer(state.command);
  state.command = null;
  if (state.armed) {
    setMode("idle");
    armWake();
  } else {
    setMode("idle");
  }
}

function extractSentences(buffer) {
  const sentences = [];
  let rest = buffer;
  const re = /[.!?…](?:["')\]]+)?(?=\s|$)/;
  while (rest.length) {
    const m = re.exec(rest);
    if (!m) break;
    const end = m.index + m[0].length;
    const piece = rest.slice(0, end).trim();
    rest = rest.slice(end).trimStart();
    if (piece) sentences.push(piece);
  }
  return { sentences, rest };
}

async function chatAndSpeak(message) {
  const body = {
    model: MODEL,
    messages: [
      {
        role: "system",
        content:
          "You are Jarvis, a concise professional butler-style assistant. Keep spoken replies under 2 short sentences unless asked for detail.",
      },
      { role: "user", content: message },
    ],
    stream: true,
  };

  let res;
  try {
    res = await api("/api/chat/completions", {
      method: "POST",
      body: JSON.stringify(body),
    });
  } catch (_) {
    res = await api("/openai/chat/completions", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  if (!res.body) {
    const data = await res.json();
    const content =
      data?.choices?.[0]?.message?.content ||
      data?.choices?.[0]?.text ||
      "";
    const text = String(content).replace(/<[^>]+>/g, "").trim() || "Acknowledged.";
    await speak(text);
    return text;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  let full = "";
  let carry = "";
  let speakChain = Promise.resolve();
  let firstSpeak = true;
  const gen = state.speakGen;

  const enqueueSpeak = (sentence) => {
    speakChain = speakChain.then(async () => {
      if (gen !== state.speakGen) return;
      if (firstSpeak) {
        firstSpeak = false;
        setMode("speaking");
      }
      await speak(sentence, { continueMode: true });
    });
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    pending += decoder.decode(value, { stream: true });
    const chunks = pending.split("\n");
    pending = chunks.pop() || "";
    for (const rawLine of chunks) {
      const line = rawLine.trim();
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === "[DONE]") continue;
      let parsed;
      try {
        parsed = JSON.parse(payload);
      } catch (_) {
        continue;
      }
      const delta =
        parsed?.choices?.[0]?.delta?.content ||
        parsed?.choices?.[0]?.message?.content ||
        parsed?.choices?.[0]?.text ||
        "";
      if (!delta) continue;
      const clean = String(delta).replace(/<[^>]+>/g, "");
      full += clean;
      carry += clean;
      const { sentences, rest } = extractSentences(carry);
      carry = rest;
      for (const s of sentences) enqueueSpeak(s);
    }
  }

  const leftover = carry.replace(/<[^>]+>/g, "").trim();
  if (leftover) enqueueSpeak(leftover);
  await speakChain;
  const text = full.replace(/<[^>]+>/g, "").trim() || "Acknowledged.";
  if (!text || firstSpeak) {
    await speak(text || "Acknowledged.");
  }
  return text;
}

async function speak(text, { continueMode = false } = {}) {
  if (!continueMode) setMode("speaking");
  const payload = {
    model: TTS_MODEL,
    input: text,
    voice: TTS_VOICE,
  };
  const res = await api("/api/v1/audio/speech", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  try {
    await playAudio(url);
  } finally {
    URL.revokeObjectURL(url);
  }
}

function stopSpeakLoop() {
  if (state.raf) cancelAnimationFrame(state.raf);
  state.raf = 0;
  state.analyser = null;
}

async function playAudio(url) {
  stopSpeakLoop();
  const audio = new Audio(url);
  state.audio = audio;
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const src = ctx.createMediaElementSource(audio);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  src.connect(analyser);
  analyser.connect(ctx.destination);
  state.analyser = analyser;
  const data = new Uint8Array(analyser.frequencyBinCount);

  const tick = () => {
    analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i++) {
      const v = (data[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / data.length);
    const level = Math.min(1, rms * 4.5);
    document.documentElement.style.setProperty("--speak-level", level.toFixed(3));
    state.raf = requestAnimationFrame(tick);
  };
  state.raf = requestAnimationFrame(tick);

  await new Promise((resolve, reject) => {
    audio.onended = resolve;
    audio.onerror = () => reject(new Error("Audio playback failed"));
    audio.play().catch(reject);
  });
  stopSpeakLoop();
  document.documentElement.style.setProperty("--speak-level", "0");
  try {
    await ctx.close();
  } catch (_) {}
}

async function tryAutoArm() {
  if (!SpeechRec()) {
    log("This browser lacks SpeechRecognition. Use Chrome or Edge for “Hey Jarvis”.");
    return;
  }
  try {
    await navigator.mediaDevices.getUserMedia({ audio: true });
    armWake();
  } catch (_) {
    log('Ready. Click “Start listening” to allow the microphone, then say “Hey Jarvis”.');
  }
}

els.btnArm.addEventListener("click", async () => {
  try {
    await navigator.mediaDevices.getUserMedia({ audio: true });
    armWake();
  } catch (err) {
    log(`Mic error: ${err.message || err}`);
  }
});
els.btnStop.addEventListener("click", disarm);
els.btnTestSpeak.addEventListener("click", async () => {
  try {
    state.busy = true;
    await speak("At your service. Systems are online.");
  } catch (err) {
    log(String(err));
  } finally {
    state.busy = false;
    setMode(state.armed ? "idle" : "idle");
  }
});

tryAutoArm();
