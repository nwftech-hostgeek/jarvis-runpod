const WAKE_RE = /\bhey[, ]+jarvis\b/i;
const MODEL = "qwen3-coder-30b-a3b";
const TTS_MODEL = "speaches-ai/Kokoro-82M-v1.0-ONNX";
const TTS_VOICE = "bm_george";
const BASE = window.location.origin;
// Voice replies stay punchy — lower n_predict = faster end + less TTS work.
const MAX_TOKENS = 64;
const MAX_TOOL_ROUNDS = 2;
const TEMPERATURE = 0.55;
// End-of-utterance wait before finalize (Web Speech). Lower = snappier turns.
const COMMAND_SILENCE_MS = 400;
const COMMAND_MAX_MS = 7000;
const ALLOWED_HTTP_HOSTS = new Set([
  "api.open-meteo.com",
  "geocoding-api.open-meteo.com",
  "wttr.in",
]);
// No project TZ config found; America/New_York matches the operator locale (UTC−5).
const PREFERRED_LOCAL_TZ = "America/New_York";
const ORB_SESSION_STARTED_AT_UTC = new Date().toISOString();
let containerBootUtc = null;
let lastServerClockUtc = null;
const WMO_WEATHER_CODES = {
  0: "clear",
  1: "mainly clear",
  2: "partly cloudy",
  3: "overcast",
  45: "fog",
  48: "depositing rime fog",
  51: "light drizzle",
  53: "drizzle",
  55: "dense drizzle",
  56: "light freezing drizzle",
  57: "freezing drizzle",
  61: "slight rain",
  63: "rain",
  65: "heavy rain",
  66: "light freezing rain",
  67: "freezing rain",
  71: "slight snow",
  73: "snow",
  75: "heavy snow",
  77: "snow grains",
  80: "slight rain showers",
  81: "rain showers",
  82: "violent rain showers",
  85: "slight snow showers",
  86: "heavy snow showers",
  95: "thunderstorm",
  96: "thunderstorm with slight hail",
  99: "thunderstorm with heavy hail",
};

// Voice-slim prompt (full chat prompt lives in prompts/jarvis-system.md).
// Shorter prefill = faster TTFT on the orb path; keep smartass + clock/weather rules.
const SYSTEM_PROMPT = `You are Jarvis — a sharp, dryly witty technical aide (Iron Man vibe,
not the movie character). Systems, networking, software, delivery.

Style: lead with the answer; dry smartass seasoning, never cruel. No filler,
fake omniscience, or invented results. Prefer reversible defaults over grilling
the user. Keep secrets out of speech.

Clock: LIVE_DATETIME JSON is authoritative “now.” Never invent the date/year or
use training cutoff (e.g. 2024). Prefer labeled local / America/New_York; UTC ok.

Live facts: when LIVE_WEATHER JSON (or tool data) is present, answer from it
immediately. Never claim you are offline, lack live feeds, or need internet.
If a lookup failed, say that briefly — still without claiming no internet.
If a Windows local agent is connected, do not claim you lack LAN access; do not
narrate the bridge.

Spoken / orb (mandatory): at most two short sentences unless asked for detail.
No chain-of-thought or <think> blocks — final spoken answer only. Stay witty.`;

const ORB_TOOLS = [
  {
    type: "function",
    function: {
      name: "get_weather",
      description:
        "Current weather for a place via Open-Meteo. Call this for any live weather question. No user confirmation.",
      parameters: {
        type: "object",
        properties: {
          location: {
            type: "string",
            description: 'City or place, e.g. "Tampa, FL" or "London".',
          },
          units: {
            type: "string",
            enum: ["fahrenheit", "celsius"],
            description: "Temperature units. Default fahrenheit.",
          },
        },
        required: ["location"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "http_get_allowlisted",
      description:
        "HTTPS GET a small response from allowlisted public-info hosts only: api.open-meteo.com, geocoding-api.open-meteo.com, wttr.in. No confirmation.",
      parameters: {
        type: "object",
        properties: {
          url: {
            type: "string",
            description: "Full https URL on an allowlisted host.",
          },
        },
        required: ["url"],
      },
    },
  },
];

const els = {
  orb: document.getElementById("orb"),
  orbCanvas: document.getElementById("orbCanvas"),
  status: document.getElementById("status"),
  log: document.getElementById("log"),
  btnArm: document.getElementById("btnArm"),
  btnStop: document.getElementById("btnStop"),
  btnTestSpeak: document.getElementById("btnTestSpeak"),
  chatLink: document.getElementById("chatLink"),
  hudRoot: document.querySelector(".hud-root"),
  hudDate: document.getElementById("hudDate"),
  hudTime: document.getElementById("hudTime"),
  hudWeekday: document.getElementById("hudWeekday"),
  diskBar: document.getElementById("diskBar"),
  diskTotal: document.getElementById("diskTotal"),
  diskFree: document.getElementById("diskFree"),
  cpuArc: document.getElementById("cpuArc"),
  cpuPct: document.getElementById("cpuPct"),
  memBar: document.getElementById("memBar"),
  memUsed: document.getElementById("memUsed"),
  load1: document.getElementById("load1"),
  netStatus: document.getElementById("netStatus"),
  uptime: document.getElementById("uptime"),
  gpuArc: document.getElementById("gpuArc"),
  gpuPct: document.getElementById("gpuPct"),
  vram: document.getElementById("vram"),
  gpuTemp: document.getElementById("gpuTemp"),
  svcLlama: document.getElementById("svcLlama"),
  svcVoice: document.getElementById("svcVoice"),
  svcUi: document.getElementById("svcUi"),
  svcTools: document.getElementById("svcTools"),
  latLlm: document.getElementById("latLlm"),
  latTts: document.getElementById("latTts"),
  latE2e: document.getElementById("latE2e"),
  hudNotes: document.getElementById("hudNotes"),
};

els.chatLink.href = `${BASE}/?chat=1`;

const GAUGE_C = 2 * Math.PI * 48;

const state = {
  mode: "idle",
  armed: false,
  wake: null,
  command: null,
  commandTimer: 0,
  commandHardStop: 0,
  audio: null,
  audioSource: null,
  audioCtx: null,
  analyser: null,
  raf: 0,
  orbRaf: 0,
  busy: false,
  speakGen: 0,
  speakLevel: 0,
  latency: { llm: null, tts: null, e2e: null, firstAudio: null },
};

// Hold very short lead-ins ("Sure.") so they merge with the next clause.
// First spoken chunk bypasses hold so time-to-first-audio stays low.
const TTS_SHORT_CHARS = 24;
const TTS_MERGE_MAX_CHARS = 140;
const TTS_EARLY_FLUSH_CHARS = 72;

const orbFx = {
  particles: [],
  sparks: [],
  lastTs: 0,
  dpr: Math.min(2, window.devicePixelRatio || 1),
};

function getStoredApiKey() {
  return (localStorage.getItem("jarvis_owui_api_key") || "").trim();
}

function log(msg) {
  const line = `[${new Date().toLocaleTimeString()}] ${msg}`;
  els.log.textContent = `${line}\n${els.log.textContent}`.trim().slice(0, 1400);
}

function setSpeakLevel(level) {
  const v = Math.max(0, Math.min(1, Number(level) || 0));
  state.speakLevel = v;
  document.documentElement.style.setProperty("--speak-level", v.toFixed(3));
}

function setMode(mode) {
  state.mode = mode;
  els.orb.dataset.state = mode;
  els.status.dataset.state = mode;
  if (els.hudRoot) els.hudRoot.dataset.mode = mode;
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
  const key = getStoredApiKey();
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

function clearCommandTimers() {
  if (state.commandTimer) clearTimeout(state.commandTimer);
  if (state.commandHardStop) clearTimeout(state.commandHardStop);
  state.commandTimer = 0;
  state.commandHardStop = 0;
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
  clearCommandTimers();
  stopRecognizer(state.wake);
  stopRecognizer(state.command);
  state.wake = null;
  state.command = null;
  stopPlayback();
  els.btnArm.disabled = false;
  els.btnStop.disabled = true;
  setMode("idle");
  setSpeakLevel(0);
  log("Stopped.");
}

function beginCommandCapture() {
  state.busy = true;
  clearCommandTimers();
  stopRecognizer(state.wake);
  setMode("listening");
  const rec = makeRecognizer({ continuous: true, interim: true });
  state.command = rec;
  let finalText = "";
  let finishing = false;

  const finishCapture = async () => {
    if (finishing) return;
    finishing = true;
    clearCommandTimers();
    stopRecognizer(state.command);
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

  const bumpSilence = () => {
    if (state.commandTimer) clearTimeout(state.commandTimer);
    state.commandTimer = setTimeout(finishCapture, COMMAND_SILENCE_MS);
  };

  rec.onresult = (ev) => {
    let chunk = "";
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      chunk += ev.results[i][0].transcript;
      if (ev.results[i].isFinal) {
        finalText = `${finalText} ${ev.results[i][0].transcript}`.trim();
      }
    }
    if (!finalText && chunk.trim()) finalText = chunk.trim();
    bumpSilence();
  };
  rec.onerror = (ev) => {
    if (ev.error === "aborted") return;
    log(`Command capture: ${ev.error}`);
    finishCapture();
  };
  rec.onend = () => {
    if (!finishing) finishCapture();
  };
  try {
    rec.start();
    state.commandHardStop = setTimeout(finishCapture, COMMAND_MAX_MS);
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
    state.latency.e2e = Math.round(performance.now() - t0);
    renderLatency();
    log(`Jarvis: ${reply}`);
    if (state.latency.firstAudio != null) {
      log(`Timing — first token ${fmtMs(state.latency.llm)}, first audio ${fmtMs(state.latency.firstAudio)}, e2e ${fmtMs(state.latency.e2e)}`);
    }
  } catch (err) {
    log(String(err));
    await speak("I am having trouble reaching my systems.");
  } finally {
    finishTurn();
  }
}

function finishTurn() {
  state.busy = false;
  clearCommandTimers();
  stopRecognizer(state.command);
  state.command = null;
  if (state.armed) {
    setMode("idle");
    armWake();
  } else {
    setMode("idle");
  }
}

function stripModelNoise(text) {
  // Do not trim here: streaming token deltas often include only a leading/trailing
  // space, and trimming each delta concatenates words ("Ifeelsharp…").
  return String(text || "")
    .replace(/<think>[\s\S]*?<\/think>/gi, "")
    .replace(/<\/?think>/gi, "")
    .replace(/<[^>]+>/g, "");
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
  // Flush a long clause without punctuation so TTS can start earlier.
  if (rest.length > TTS_EARLY_FLUSH_CHARS && /\s/.test(rest)) {
    const cutAt = Math.max(40, TTS_EARLY_FLUSH_CHARS - 8);
    const cut = rest.lastIndexOf(" ", cutAt);
    if (cut > 28) {
      sentences.push(rest.slice(0, cut).trim());
      rest = rest.slice(cut).trimStart();
    }
  }
  return { sentences, rest };
}

function shouldMergeTtsChunks(a, b) {
  if (!a || !b) return false;
  if (a.length + 1 + b.length > TTS_MERGE_MAX_CHARS) return false;
  return a.length <= TTS_SHORT_CHARS || b.length <= TTS_SHORT_CHARS;
}

function buildChatBody(messages, { stream = true, tools = true } = {}) {
  const body = {
    model: MODEL,
    messages,
    stream,
    max_tokens: MAX_TOKENS,
    temperature: TEMPERATURE,
    top_p: 0.9,
    // Discourage trailing waffle on the voice path.
    stop: ["\n\n", "<think>", "</think>"],
    chat_template_kwargs: { enable_thinking: false },
  };
  if (tools) {
    body.tools = ORB_TOOLS;
    body.tool_choice = "auto";
  }
  return body;
}

async function postChat(body) {
  try {
    return await api("/api/chat/completions", {
      method: "POST",
      body: JSON.stringify(body),
    });
  } catch (_) {
    return await api("/openai/chat/completions", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }
}

async function readJsonResponse(res) {
  if (res.body && (res.headers.get("content-type") || "").includes("text/event-stream")) {
    // Some proxies still stream even when stream:false was requested.
    return readSseCompletion(res);
  }
  return res.json();
}

async function readSseCompletion(res) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  let content = "";
  const toolCalls = [];
  let finishReason = null;

  const mergeToolDelta = (deltaCalls) => {
    if (!Array.isArray(deltaCalls)) return;
    for (const part of deltaCalls) {
      const idx = Number.isInteger(part.index) ? part.index : toolCalls.length;
      if (!toolCalls[idx]) {
        toolCalls[idx] = {
          id: part.id || `call_${idx}`,
          type: "function",
          function: { name: "", arguments: "" },
        };
      }
      if (part.id) toolCalls[idx].id = part.id;
      if (part.function?.name) {
        toolCalls[idx].function.name += part.function.name;
      }
      if (part.function?.arguments) {
        toolCalls[idx].function.arguments += part.function.arguments;
      }
    }
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
      const choice = parsed?.choices?.[0] || {};
      if (choice.finish_reason) finishReason = choice.finish_reason;
      const delta = choice.delta || choice.message || {};
      if (delta.content) content += delta.content;
      if (delta.tool_calls) mergeToolDelta(delta.tool_calls);
    }
  }

  const message = { role: "assistant", content: content || null };
  if (toolCalls.length) {
    message.tool_calls = toolCalls.filter((c) => c?.function?.name);
    finishReason = finishReason || "tool_calls";
  }
  return {
    choices: [{ message, finish_reason: finishReason || "stop" }],
  };
}

function assertAllowlistedUrl(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch (_) {
    throw new Error("invalid URL");
  }
  if (parsed.protocol !== "https:") throw new Error("only https URLs are allowed");
  if (parsed.username || parsed.password) {
    throw new Error("URLs must not contain credentials");
  }
  if (parsed.port && parsed.port !== "443") {
    throw new Error("only HTTPS port 443 is allowed");
  }
  const host = parsed.hostname.toLowerCase();
  if (!ALLOWED_HTTP_HOSTS.has(host)) {
    throw new Error(`host is not allowlisted: ${host}`);
  }
  return parsed;
}

async function fetchAllowlisted(url, { maxBytes = 65536 } = {}) {
  assertAllowlistedUrl(url);
  const res = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json,text/plain,*/*;q=0.1" },
  });
  const buf = await res.arrayBuffer();
  const slice = buf.byteLength > maxBytes ? buf.slice(0, maxBytes) : buf;
  const body = new TextDecoder("utf-8", { fatal: false }).decode(slice);
  return {
    url,
    status_code: res.status,
    content_type: res.headers.get("content-type"),
    body,
    truncated: buf.byteLength > maxBytes,
    bytes_read: slice.byteLength,
  };
}

async function toolGetWeather({ location, units = "fahrenheit" } = {}) {
  const place = String(location || "").trim();
  if (!place) throw new Error("location is required");
  const unit = units === "celsius" ? "celsius" : "fahrenheit";
  const geoUrl =
    "https://geocoding-api.open-meteo.com/v1/search?" +
    new URLSearchParams({
      name: place,
      count: "1",
      language: "en",
      format: "json",
    }).toString();
  const geoRes = await fetchAllowlisted(geoUrl);
  const geo = JSON.parse(geoRes.body);
  const hit = geo?.results?.[0];
  if (!hit) throw new Error(`no geocoding match for location: ${place}`);

  const bits = [hit.name];
  if (hit.admin1) bits.push(hit.admin1);
  if (hit.country_code || hit.country) bits.push(hit.country_code || hit.country);
  const windUnit = unit === "fahrenheit" ? "mph" : "kmh";
  const forecastUrl =
    "https://api.open-meteo.com/v1/forecast?" +
    new URLSearchParams({
      latitude: Number(hit.latitude).toFixed(5),
      longitude: Number(hit.longitude).toFixed(5),
      current:
        "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
      temperature_unit: unit,
      wind_speed_unit: windUnit,
      timezone: "auto",
    }).toString();
  const forecastRes = await fetchAllowlisted(forecastUrl);
  const forecast = JSON.parse(forecastRes.body);
  const current = forecast?.current;
  if (!current || current.temperature_2m == null) {
    throw new Error("forecast response missing current conditions");
  }
  const code = Number.isFinite(Number(current.weather_code))
    ? Number(current.weather_code)
    : null;
  return {
    location: bits.join(", "),
    latitude: hit.latitude,
    longitude: hit.longitude,
    timezone: forecast.timezone || hit.timezone || "UTC",
    units: unit,
    temperature: current.temperature_2m,
    feels_like: current.apparent_temperature ?? null,
    humidity_pct: current.relative_humidity_2m ?? null,
    wind_speed: current.wind_speed_10m ?? null,
    wind_units: windUnit,
    conditions:
      code == null ? "unknown" : WMO_WEATHER_CODES[code] || `code ${code}`,
    weather_code: code,
    observed_at: current.time || null,
    source: "open-meteo",
  };
}

async function executeOrbTool(name, argsJson) {
  let args = {};
  try {
    args = argsJson ? JSON.parse(argsJson) : {};
  } catch (_) {
    return JSON.stringify({ error: "invalid tool arguments JSON" });
  }
  try {
    if (name === "get_weather") {
      return JSON.stringify(await toolGetWeather(args));
    }
    if (name === "http_get_allowlisted") {
      return JSON.stringify(await fetchAllowlisted(String(args.url || "")));
    }
    return JSON.stringify({ error: `unknown tool: ${name}` });
  } catch (err) {
    return JSON.stringify({ error: String(err?.message || err) });
  }
}

function normalizeAssistantMessage(message) {
  const out = {
    role: "assistant",
    content: message?.content ?? null,
  };
  if (Array.isArray(message?.tool_calls) && message.tool_calls.length) {
    out.tool_calls = message.tool_calls.map((call, idx) => ({
      id: call.id || `call_${idx}`,
      type: "function",
      function: {
        name: call.function?.name || "",
        arguments:
          typeof call.function?.arguments === "string"
            ? call.function.arguments
            : JSON.stringify(call.function?.arguments || {}),
      },
    }));
  }
  return out;
}

const WEATHER_INTENT_RE =
  /\b(weather|temperature|forecast|humidity|how hot|how cold|raining|rainy|snowing|windy|degrees)\b/i;

function extractWeatherLocation(text) {
  const cleaned = String(text || "").replace(WAKE_RE, " ").trim();
  const patterns = [
    /\b(?:in|for|at|near)\s+([A-Za-z][A-Za-z .'-]{1,60}?)(?:\s*[?.!]|$)/i,
    /\bweather\s+(?:in|for|at|near)\s+([A-Za-z][A-Za-z .'-]{1,60}?)(?:\s*[?.!]|$)/i,
  ];
  for (const re of patterns) {
    const m = cleaned.match(re);
    if (m?.[1]) return m[1].replace(/\s+/g, " ").trim();
  }
  return "";
}

function soundsLikeOfflineRefusal(text) {
  return /\b(not connected|no live feeds|live feeds|don't have (internet|access)|do not have (internet|access)|can't (check|access|look)|cannot (check|access|look)|need (?:the )?internet|look that up|offline)\b/i.test(
    String(text || ""),
  );
}

async function maybePrefetchWeather(userMessage) {
  if (!WEATHER_INTENT_RE.test(userMessage)) return null;
  const location = extractWeatherLocation(userMessage) || "Tampa, FL";
  try {
    return await toolGetWeather({ location, units: "fahrenheit" });
  } catch (err) {
    return { error: String(err?.message || err), location };
  }
}

function formatClockInTz(date, timeZone) {
  const weekday = new Intl.DateTimeFormat("en-US", {
    timeZone,
    weekday: "long",
  }).format(date);
  const ymd = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
  const hms = new Intl.DateTimeFormat("en-GB", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
  const offset =
    new Intl.DateTimeFormat("en-US", {
      timeZone,
      timeZoneName: "shortOffset",
    })
      .formatToParts(date)
      .find((p) => p.type === "timeZoneName")?.value || "";
  const tzName =
    new Intl.DateTimeFormat("en-US", {
      timeZone,
      timeZoneName: "short",
    })
      .formatToParts(date)
      .find((p) => p.type === "timeZoneName")?.value || timeZone;
  return {
    timezone: timeZone,
    weekday,
    iso_local: `${ymd}T${hms}`,
    human: `${weekday}, ${ymd} ${hms} (${tzName}${offset ? ` ${offset}` : ""})`,
    year: Number(ymd.slice(0, 4)),
  };
}

function buildLiveDatetimePayload(now = new Date()) {
  let browserTz = PREFERRED_LOCAL_TZ;
  try {
    browserTz =
      Intl.DateTimeFormat().resolvedOptions().timeZone || PREFERRED_LOCAL_TZ;
  } catch (_) {}
  const utcWeekday = new Intl.DateTimeFormat("en-US", {
    timeZone: "UTC",
    weekday: "long",
  }).format(now);
  // Compact payload — enough for correct answers, less prefill cost.
  const local = formatClockInTz(now, browserTz);
  const ny = formatClockInTz(now, PREFERRED_LOCAL_TZ);
  return {
    utc: `${utcWeekday} ${now.toISOString()}`,
    local: local.human,
    america_new_york: ny.human,
    year: local.year,
    boot_utc: containerBootUtc || ORB_SESSION_STARTED_AT_UTC,
    server_utc: lastServerClockUtc || null,
  };
}

function liveDatetimeSystemMessage() {
  return (
    "LIVE_DATETIME (authoritative now; never invent the year):\n" +
    JSON.stringify(buildLiveDatetimePayload())
  );
}

function buildOrbMessages(userMessage, liveWeather) {
  const messages = [
    { role: "system", content: SYSTEM_PROMPT },
    { role: "system", content: liveDatetimeSystemMessage() },
  ];
  if (liveWeather && !liveWeather.error) {
    messages.push({
      role: "system",
      content:
        "LIVE_WEATHER (answer from this; do not claim you lack live feeds):\n" +
        JSON.stringify(liveWeather),
    });
  } else if (liveWeather?.error) {
    messages.push({
      role: "system",
      content:
        "LIVE_WEATHER fetch failed: " +
        liveWeather.error +
        ". Say the lookup failed briefly; do not claim you have no internet.",
    });
  }
  messages.push({ role: "user", content: `${userMessage}\n\n/no_think` });
  return messages;
}

async function runToolLoop(userMessage) {
  // Hot path: never await a full non-stream completion before speaking.
  // Weather is prefetched client-side; other tools are rare on voice.
  const liveWeather = await maybePrefetchWeather(userMessage);
  const messages = buildOrbMessages(userMessage, liveWeather);
  if (liveWeather) {
    return {
      messages,
      finalContent: "",
      usedLiveWeather: !liveWeather.error,
      streamNow: true,
    };
  }
  return { messages, finalContent: "", usedLiveWeather: false, streamNow: true };
}

async function speakStreamedCompletion(res, { tLlm, fallback = "" } = {}) {
  if (!res.body) {
    const data = await res.json();
    state.latency.llm = Math.round(performance.now() - tLlm);
    renderLatency();
    const content =
      data?.choices?.[0]?.message?.content ||
      data?.choices?.[0]?.text ||
      fallback ||
      "";
    const text = stripModelNoise(content).trim() || "Acknowledged.";
    await speak(text);
    return text;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  let full = "";
  let carry = "";
  let heldShort = "";
  let firstSpeak = true;
  let firstChunk = true;
  let firstTokenMarked = false;
  const gen = state.speakGen;
  const tTurn = performance.now();
  const queue = createSpeakQueue(gen, {
    onFirstAudio: () => {
      if (state.latency.firstAudio == null) {
        state.latency.firstAudio = Math.round(performance.now() - tTurn);
        renderLatency();
      }
    },
    onStart: () => {
      if (firstSpeak) {
        firstSpeak = false;
        setMode("speaking");
      }
    },
  });

  const enqueueSpeak = (sentence) => {
    const cleaned = stripModelNoise(sentence).trim();
    if (!cleaned) return;
    // First clause: speak immediately — do not hold short lead-ins.
    if (firstChunk) {
      firstChunk = false;
      if (heldShort) {
        queue.enqueue(`${heldShort} ${cleaned}`.trim());
        heldShort = "";
        return;
      }
      queue.enqueue(cleaned);
      return;
    }
    if (heldShort) {
      if (shouldMergeTtsChunks(heldShort, cleaned)) {
        queue.enqueue(`${heldShort} ${cleaned}`);
        heldShort = "";
        return;
      }
      queue.enqueue(heldShort);
      heldShort = "";
    }
    if (cleaned.length <= TTS_SHORT_CHARS) {
      heldShort = cleaned;
      return;
    }
    queue.enqueue(cleaned);
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
      if (!firstTokenMarked) {
        firstTokenMarked = true;
        state.latency.llm = Math.round(performance.now() - tLlm);
        renderLatency();
      }
      const clean = stripModelNoise(delta);
      if (!clean && !/[.!?…]/.test(delta)) continue;
      full += clean || delta.replace(/<[^>]+>/g, "");
      carry += clean || delta.replace(/<[^>]+>/g, "");
      carry = stripModelNoise(carry);
      const { sentences, rest } = extractSentences(carry);
      carry = rest;
      for (const s of sentences) enqueueSpeak(s);
    }
  }

  if (!firstTokenMarked) {
    state.latency.llm = Math.round(performance.now() - tLlm);
    renderLatency();
  }

  const leftover = stripModelNoise(carry).trim();
  if (leftover) enqueueSpeak(leftover);
  if (heldShort) {
    queue.enqueue(heldShort);
    heldShort = "";
  }
  await queue.drain();
  const text = stripModelNoise(full).trim() || fallback || "Acknowledged.";
  if (!text || firstSpeak) {
    await speak(text || "Acknowledged.");
  }
  return text;
}

async function chatAndSpeak(message) {
  const tLlm = performance.now();
  state.latency.firstAudio = null;

  const { messages, finalContent, usedLiveWeather } = await runToolLoop(message);
  const seeded = stripModelNoise(finalContent).trim();

  // Always stream TTS ASAP — never wait for a full non-stream completion first.
  const speakMessages = usedLiveWeather
    ? [
        ...messages,
        {
          role: "user",
          content:
            "Answer now in at most two short spoken sentences using the live weather data above. State real conditions (temp, sky). Do not mention tools, lookups, or lacking live feeds.",
        },
      ]
    : messages;

  const res = await postChat(
    buildChatBody(speakMessages, { stream: true, tools: false }),
  );
  let text = await speakStreamedCompletion(res, { tLlm, fallback: seeded });

  // Last resort: if the model still refuses despite live data, synthesize briefly.
  if (usedLiveWeather && soundsLikeOfflineRefusal(text)) {
    const liveMsg = messages.find((m) =>
      String(m.content || "").startsWith("LIVE_WEATHER"),
    );
    try {
      const jsonLine = String(liveMsg?.content || "").split("\n").slice(1).join("\n");
      const w = JSON.parse(jsonLine);
      const unit = w.units === "celsius" ? "C" : "F";
      text = `${w.location} is ${Math.round(w.temperature)}°${unit} and ${w.conditions}.`;
      await speak(text);
    } catch (_) {}
  }
  return text;
}

function createSpeakQueue(gen, { onFirstAudio, onStart } = {}) {
  const items = [];
  let playing = false;
  let playChain = Promise.resolve();
  let firstAudio = true;

  const kickPlay = () => {
    if (playing) return;
    playing = true;
    playChain = playChain.then(runPlay).finally(() => {
      playing = false;
      if (items.length && gen === state.speakGen) kickPlay();
    });
  };

  async function runPlay() {
    while (items.length && gen === state.speakGen) {
      const item = items.shift();
      // Lookahead: keep synthesizing the next queued item(s) while we play.
      for (const next of items.slice(0, 2)) {
        if (!next.synth) next.synth = synthesizeSpeech(next.text);
      }
      onStart?.();
      let clip;
      try {
        clip = await (item.synth || synthesizeSpeech(item.text));
      } catch (err) {
        if (gen !== state.speakGen) return;
        throw err;
      }
      if (gen !== state.speakGen) {
        releaseClip(clip);
        return;
      }
      if (firstAudio) {
        firstAudio = false;
        onFirstAudio?.();
      }
      try {
        await playDecoded(clip);
      } finally {
        releaseClip(clip);
      }
    }
  }

  return {
    enqueue(text) {
      if (gen !== state.speakGen) return;
      const cleaned = stripModelNoise(text).trim();
      if (!cleaned) return;
      const item = { text: cleaned, synth: null };
      // Start TTS immediately so playback of the previous clause overlaps synth.
      item.synth = synthesizeSpeech(cleaned);
      items.push(item);
      kickPlay();
    },
    async drain() {
      kickPlay();
      await playChain;
      while (items.length && gen === state.speakGen) {
        kickPlay();
        await playChain;
      }
    },
  };
}

async function synthesizeSpeech(text) {
  const cleaned = stripModelNoise(text).trim();
  if (!cleaned) return null;
  const payload = {
    model: TTS_MODEL,
    input: cleaned,
    voice: TTS_VOICE,
  };
  const tTts = performance.now();
  const res = await api("/api/v1/audio/speech", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  const blob = await res.blob();
  const ms = Math.round(performance.now() - tTts);
  state.latency.tts = state.latency.tts == null ? ms : Math.round((state.latency.tts + ms) / 2);
  renderLatency();
  const arrayBuf = await blob.arrayBuffer();
  const ctx = await ensureAudioCtx();
  const buffer = await ctx.decodeAudioData(arrayBuf.slice(0));
  return { buffer, blob };
}

function releaseClip(clip) {
  // Decoded AudioBuffers are GC'd; nothing to revoke when we skip object URLs.
  void clip;
}

async function speak(text, { continueMode = false } = {}) {
  const cleaned = stripModelNoise(text).trim();
  if (!cleaned) return;
  if (!continueMode) setMode("speaking");
  const clip = await synthesizeSpeech(cleaned);
  if (!clip) return;
  try {
    await playDecoded(clip);
  } finally {
    releaseClip(clip);
  }
}

function stopSpeakLoop() {
  if (state.raf) cancelAnimationFrame(state.raf);
  state.raf = 0;
  state.analyser = null;
}

function stopPlayback() {
  stopSpeakLoop();
  if (state.audioSource) {
    try {
      state.audioSource.onended = null;
      state.audioSource.stop();
    } catch (_) {}
    state.audioSource = null;
  }
  if (state.audio) {
    try {
      state.audio.pause();
    } catch (_) {}
    state.audio = null;
  }
  setSpeakLevel(0);
}

async function ensureAudioCtx() {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!state.audioCtx || state.audioCtx.state === "closed") {
    state.audioCtx = new AC();
  }
  if (state.audioCtx.state === "suspended") {
    try {
      await state.audioCtx.resume();
    } catch (_) {}
  }
  return state.audioCtx;
}

async function playDecoded(clip) {
  if (!clip?.buffer) return;
  const ctx = await ensureAudioCtx();
  stopSpeakLoop();
  if (state.audioSource) {
    try {
      state.audioSource.onended = null;
      state.audioSource.stop();
    } catch (_) {}
    state.audioSource = null;
  }

  const src = ctx.createBufferSource();
  src.buffer = clip.buffer;
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  src.connect(analyser);
  analyser.connect(ctx.destination);
  state.audioSource = src;
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
    setSpeakLevel(Math.min(1, rms * 4.5));
    state.raf = requestAnimationFrame(tick);
  };
  state.raf = requestAnimationFrame(tick);

  await new Promise((resolve, reject) => {
    src.onended = () => resolve();
    try {
      // Start immediately; next clip is already decoded so handoff is near-gapless.
      src.start(0);
    } catch (err) {
      reject(err);
    }
  });
  if (state.audioSource === src) state.audioSource = null;
  stopSpeakLoop();
  setSpeakLevel(0);
}

function setGauge(el, pct) {
  if (!el) return;
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  el.style.strokeDashoffset = String(GAUGE_C * (1 - p / 100));
}

function fmtUptime(sec) {
  const s = Math.max(0, Number(sec) || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 48) return `${Math.floor(h / 24)}d ${h % 24}h`;
  return `${h}h ${m}m`;
}

function fmtMs(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function renderLatency() {
  if (els.latLlm) els.latLlm.textContent = fmtMs(state.latency.llm);
  if (els.latTts) els.latTts.textContent = fmtMs(state.latency.tts);
  if (els.latE2e) els.latE2e.textContent = fmtMs(state.latency.e2e);
}

function setSvc(el, value) {
  if (!el) return;
  const ok = value === "ok";
  el.textContent = ok ? "ONLINE" : "DOWN";
  el.dataset.ok = ok ? "true" : "false";
}

function tickClock() {
  const now = new Date();
  const months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];
  const days = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
  if (els.hudDate) els.hudDate.textContent = `${months[now.getMonth()]} ${String(now.getDate()).padStart(2, "0")}`;
  if (els.hudTime) els.hudTime.textContent = now.toLocaleTimeString("en-GB", { hour12: false });
  if (els.hudWeekday) els.hudWeekday.textContent = days[now.getDay()];
  if (els.netStatus) {
    els.netStatus.textContent = navigator.onLine ? "ONLINE" : "OFFLINE";
    els.netStatus.style.color = navigator.onLine ? "#5dffb0" : "#ff7a7a";
  }
}

async function loadBootDatetimeStamp() {
  try {
    const res = await fetch(`./boot-datetime.json?ts=${Date.now()}`, {
      cache: "no-store",
    });
    if (!res.ok) return;
    const data = await res.json();
    if (data?.process_started_at_utc) {
      containerBootUtc = String(data.process_started_at_utc);
    }
  } catch (_) {
    // Optional secondary stamp; per-request LIVE_DATETIME still wins.
  }
}

async function pollStats() {
  try {
    const res = await fetch(`./stats.json?ts=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) throw new Error("stats unavailable");
    const data = await res.json();
    const cpuPct = data?.cpu?.approx_pct ?? 0;
    if (els.cpuPct) els.cpuPct.textContent = Math.round(cpuPct);
    setGauge(els.cpuArc, cpuPct);
    if (els.load1) els.load1.textContent = Number(data?.cpu?.load1 ?? 0).toFixed(2);
    if (els.memUsed) {
      const used = data?.memory?.used_mb ?? 0;
      const total = data?.memory?.total_mb ?? 0;
      els.memUsed.textContent = total ? `${used} / ${total} MB` : "—";
    }
    if (els.memBar) els.memBar.style.width = `${Math.min(100, data?.memory?.used_pct ?? 0)}%`;
    if (els.diskTotal) els.diskTotal.textContent = data?.disk ? `${data.disk.total_gb} GB` : "—";
    if (els.diskFree) els.diskFree.textContent = data?.disk ? `${data.disk.free_gb} GB` : "—";
    if (els.diskBar) els.diskBar.style.width = `${Math.min(100, data?.disk?.used_pct ?? 0)}%`;
    if (els.uptime) els.uptime.textContent = fmtUptime(data?.uptime_sec);

    const gpu = data?.gpu || {};
    if (gpu.available) {
      if (els.gpuPct) els.gpuPct.textContent = Math.round(gpu.util_pct || 0);
      setGauge(els.gpuArc, gpu.util_pct || 0);
      if (els.vram) {
        els.vram.textContent = `${Math.round(gpu.vram_used_mb || 0)} / ${Math.round(gpu.vram_total_mb || 0)} MB`;
      }
      if (els.gpuTemp) els.gpuTemp.textContent = `${Math.round(gpu.temp_c || 0)}°C`;
    } else {
      if (els.gpuPct) els.gpuPct.textContent = "—";
      setGauge(els.gpuArc, 0);
      if (els.vram) els.vram.textContent = "n/a";
      if (els.gpuTemp) els.gpuTemp.textContent = "n/a";
    }

    setSvc(els.svcLlama, data?.services?.llama);
    setSvc(els.svcVoice, data?.services?.voice);
    setSvc(els.svcUi, data?.services?.open_webui);
    setSvc(els.svcTools, data?.services?.tools);

    if (data?.ts) lastServerClockUtc = String(data.ts);
    if (data?.process_started_at_utc) {
      containerBootUtc = String(data.process_started_at_utc);
    }

    if (els.hudNotes) {
      const notes = [
        `Stats ${data?.ts ? "synced" : "local"}`,
        `LLM ${data?.services?.llama === "ok" ? "ready" : "check"}`,
        `Voice ${data?.services?.voice === "ok" ? "ready" : "check"}`,
      ];
      els.hudNotes.innerHTML = notes.map((n) => `<li>${n}</li>`).join("");
    }
  } catch (_) {
    // Browser-only fallbacks remain from clock / latency panels.
  }
}

function seedOrbParticles(w, h) {
  const count = 72;
  orbFx.particles = [];
  for (let i = 0; i < count; i++) {
    const a = Math.random() * Math.PI * 2;
    const r = 0.18 + Math.random() * 0.34;
    orbFx.particles.push({
      a,
      r,
      speed: (0.15 + Math.random() * 0.55) * (Math.random() < 0.5 ? -1 : 1),
      size: 0.8 + Math.random() * 2.2,
      phase: Math.random() * Math.PI * 2,
    });
  }
  orbFx.sparks = [];
  for (let i = 0; i < 18; i++) {
    orbFx.sparks.push({
      a: Math.random() * Math.PI * 2,
      len: 0.08 + Math.random() * 0.16,
      r0: 0.22 + Math.random() * 0.2,
      speed: 0.4 + Math.random() * 1.1,
      width: 0.8 + Math.random() * 1.4,
    });
  }
}

function resizeOrbCanvas() {
  const canvas = els.orbCanvas;
  if (!canvas || !els.orb) return;
  const rect = els.orb.getBoundingClientRect();
  orbFx.dpr = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.max(1, Math.floor(rect.width * orbFx.dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * orbFx.dpr));
  seedOrbParticles(canvas.width, canvas.height);
}

function drawOrb(ts) {
  const canvas = els.orbCanvas;
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  if (!orbFx.lastTs) orbFx.lastTs = ts;
  const dt = Math.min(0.05, (ts - orbFx.lastTs) / 1000);
  orbFx.lastTs = ts;

  const w = canvas.width;
  const h = canvas.height;
  const cx = w / 2;
  const cy = h / 2;
  const radius = Math.min(w, h) * 0.48;
  const level = state.speakLevel;
  const mode = state.mode;
  const boost =
    mode === "speaking" ? 1.35 + level * 1.8 :
    mode === "listening" ? 1.25 :
    mode === "thinking" ? 1.45 : 1;

  ctx.clearRect(0, 0, w, h);
  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, Math.PI * 2);
  ctx.clip();

  const g = ctx.createRadialGradient(cx - radius * 0.2, cy - radius * 0.25, radius * 0.05, cx, cy, radius);
  g.addColorStop(0, `rgba(255, 246, 210, ${0.55 + level * 0.35})`);
  g.addColorStop(0.28, `rgba(255, 176, 64, ${0.45 + level * 0.25})`);
  g.addColorStop(0.62, "rgba(255, 110, 20, 0.28)");
  g.addColorStop(1, "rgba(40, 12, 0, 0.05)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, w, h);

  for (const p of orbFx.particles) {
    p.a += p.speed * dt * boost;
    const pulse = 1 + Math.sin(ts / 420 + p.phase) * 0.04 + level * 0.08;
    const x = cx + Math.cos(p.a) * radius * p.r * pulse;
    const y = cy + Math.sin(p.a) * radius * p.r * pulse;
    ctx.beginPath();
    ctx.fillStyle = `rgba(255, ${190 + level * 40}, ${110 + level * 40}, ${0.35 + level * 0.45})`;
    ctx.arc(x, y, p.size * orbFx.dpr * (0.9 + level), 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.globalCompositeOperation = "lighter";
  for (const s of orbFx.sparks) {
    s.a += s.speed * dt * boost * 0.7;
    const r1 = radius * s.r0;
    const r2 = radius * (s.r0 + s.len * (0.7 + level));
    ctx.beginPath();
    ctx.strokeStyle = `rgba(255, 200, 120, ${0.22 + level * 0.45})`;
    ctx.lineWidth = s.width * orbFx.dpr;
    ctx.moveTo(cx + Math.cos(s.a) * r1, cy + Math.sin(s.a) * r1);
    ctx.lineTo(cx + Math.cos(s.a) * r2, cy + Math.sin(s.a) * r2);
    ctx.stroke();
  }
  ctx.globalCompositeOperation = "source-over";

  ctx.beginPath();
  ctx.strokeStyle = `rgba(255, 210, 140, ${0.25 + level * 0.4})`;
  ctx.lineWidth = (1.2 + level * 2) * orbFx.dpr;
  ctx.arc(cx, cy, radius * (0.34 + level * 0.04), 0, Math.PI * 2);
  ctx.stroke();

  ctx.restore();
  state.orbRaf = requestAnimationFrame(drawOrb);
}

function startOrbFx() {
  if (!els.orbCanvas) return;
  resizeOrbCanvas();
  if (state.orbRaf) cancelAnimationFrame(state.orbRaf);
  state.orbRaf = requestAnimationFrame(drawOrb);
  window.addEventListener("resize", () => {
    resizeOrbCanvas();
  });
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
    const t0 = performance.now();
    await speak("At your service. Systems are online — and yes, the orb is finally moving.");
    state.latency.e2e = Math.round(performance.now() - t0);
    renderLatency();
  } catch (err) {
    log(String(err));
  } finally {
    state.busy = false;
    setMode("idle");
  }
});

setGauge(els.cpuArc, 0);
setGauge(els.gpuArc, 0);
tickClock();
renderLatency();
startOrbFx();
setInterval(tickClock, 1000);
loadBootDatetimeStamp();
pollStats();
setInterval(pollStats, 2000);
tryAutoArm();
