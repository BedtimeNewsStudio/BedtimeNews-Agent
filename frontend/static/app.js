/* ============================================================================
   睡前消息知识库 — frontend client
   Renders the sample-question index, drives the composer + theme toggle, and
   parses the agent's SSE stream into the signal-acquisition log and the
   streamed answer (rendered with markdown-it).
   ============================================================================ */

const STAGE_LABELS = {
  condense: "理解",
  route: "路由",
  rewrite: "优化",
  retrieve: "检索",
  grade: "评分",
  generate: "生成",
};
// Pipeline order — used to mark earlier stages "done" once a later one starts.
const STAGE_ORDER = ["condense", "route", "rewrite", "retrieve", "grade", "generate"];

// How many prior turns to replay. Every turn is re-sent on each request, so this
// trades context depth against payload size; two is enough to resolve almost all
// pronouns without carrying the whole session.
const HISTORY_TURNS = 3;
// Answers are truncated before being sent back: resolving "那它呢" needs the
// subject of the previous turn, not its full text.
const HISTORY_ANSWER_CHARS = 300;

// Must match FOLLOWUPS_DELIMITER in agent/src/graph.py. The raw token stream
// still contains it, so the client hides everything from it onward.
const FOLLOWUPS_DELIMITER = "<<<FOLLOWUPS>>>";

// Completed turns, replayed to the backend so follow-ups have context. Lives in
// memory only: refreshing clears the conversation, as the composer note says.
const conversation = [];

const els = {
  hero: document.getElementById("hero"),
  log: document.getElementById("log"),
  grid: document.getElementById("sample-grid"),
  form: document.getElementById("composer-form"),
  input: document.getElementById("composer-input"),
  send: document.getElementById("composer-send"),
  status: document.getElementById("stream-status"),
  reshuffle: document.getElementById("sample-reshuffle"),
  version: document.getElementById("app-version"),
};

let busy = false;
// Aborts the run in flight when the reader hits stop.
let abortController = null;

/* ---------------------------------------------------------------- helpers */

// Announce progress to screen readers. The live region deliberately sits
// outside the conversation log: marking the log itself live would make every
// streamed token re-announce the whole growing answer.
function announce(message) {
  els.status.textContent = message;
}

// Strip a leading [TAG] marker the backend sometimes prefixes to step text.
function cleanStep(text) {
  return text.replace(/^\[[A-Z_]+\]\s*/, "").trim();
}

// Standard CommonMark rendering via markdown-it (vendored, no CDN at runtime).
// The bundle is ~124KB and nothing needs it until an answer starts arriving, so
// it is fetched on demand rather than blocking the page load. askQuestion()
// starts the fetch as soon as a query is sent, which gives it the whole
// retrieval pipeline to arrive in — by the time the first token lands it is
// already in memory.
//
// `mdReady` holds the renderer once it resolves, so the streaming loop can ask
// for it synchronously; until then the stream falls back to plain text.
let mdPromise = null;
let mdReady = null;

function loadMarkdown() {
  if (!mdPromise) {
    mdPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/markdown-it.min.js";
      script.onload = () => {
        mdReady = createRenderer();
        resolve(mdReady);
      };
      script.onerror = () => {
        mdPromise = null; // let a later question retry
        reject(new Error("markdown-it 加载失败"));
      };
      document.head.appendChild(script);
    });
  }
  return mdPromise;
}

// html:false keeps raw HTML escaped (XSS-safe); markdown-it also filters unsafe
// link protocols by default.
function createRenderer() {
  const md = window.markdownit({ html: false, linkify: true, breaks: true });

  // Open links in a new tab.
  const defaultLinkOpen =
    md.renderer.rules.link_open ||
    function (tokens, idx, options, env, self) {
      return self.renderToken(tokens, idx, options);
    };
  md.renderer.rules.link_open = function (tokens, idx, options, env, self) {
    tokens[idx].attrSet("target", "_blank");
    tokens[idx].attrSet("rel", "noopener noreferrer");
    return defaultLinkOpen(tokens, idx, options, env, self);
  };

  return md;
}

// A citation link wrapped in bold, e.g. `在**[[讲点黑话16]](url)**中`.
//
// CommonMark decides whether `**` may open emphasis from the characters around
// it: followed by punctuation — `[` here — it must also be preceded by
// whitespace or punctuation. A Chinese character is neither, and Chinese has no
// spaces, so mid-sentence the run never opens and the reader gets literal
// asterisks either side of the chip, bold only when the sentence happened to
// put punctuation before it. Bolding a citation adds nothing the chip styling
// does not already say, so the markers are dropped rather than repaired —
// which also makes every citation look the same. Plain `**文字**` after a
// Chinese character is unaffected: that opens fine, since it is not followed
// by punctuation.
const BOLD_CITATION_RE = /(\*\*|__)(\[\[[^[\]]+?\]\]\([^()\s]*\))\1/g;

// LLM output is often sloppy: list markers written without a trailing space
// ("1.资源", "-一二三"). Add the space so they parse as real lists. Conservative:
// the ordered rule ignores decimals like "3.5", the bullet rule ignores "---".
function normalizeMarkdown(raw) {
  return raw
    .replace(BOLD_CITATION_RE, "$2")
    .replace(/^([ \t]*)(\d{1,9}[.)])(?=[^\s\d])/gm, "$1$2 ")
    .replace(/^([ \t]*)([*+-])(?=[^\s*+-])/gm, "$1$2 ");
}

// Citations arrive as ordinary markdown links — `[[标准化标题]](https://…​.html)` —
// so markdown-it turns them into <a> elements on its own; styles.css picks them
// out by href.
function renderMarkdown(md, raw) {
  return md.render(normalizeMarkdown(raw));
}

// Mirror of the server's _repair_citations, applied to the partial answer while
// it streams. The model cites a document by its URI — `[[ShuiQianXiaoXi/0501-
// 0600/0588.md]]` — which is not what a reader should see, and the server only
// rewrites it once generation has finished, too late for someone watching the
// text appear. The "citations" event carries a name -> {title, url} map keyed by
// both URI and 标准化标题, so the same substitution can run per render tick: each
// citation turns into a titled link the moment it finishes arriving. A
// half-streamed `[[ShuiQianXiaoXi/0501-` has no closing brackets yet, so it
// simply doesn't match and is upgraded on a later tick.
const CITATION_RE = /(?:\[\[([^[\]]+?)\]\]|《([^《》]+?)》)(\([^)]*\))?/g;

function linkifyCitations(text, urls) {
  if (!urls) return text;
  return text.replace(CITATION_RE, (whole, bracketName, cjkName) => {
    const name = bracketName || cjkName;
    const entry = urls[name];
    // Names we have no entry for are left exactly as written — that is what
    // keeps a genuine 《书名》 in the prose from being turned into a link.
    return entry ? `[[${entry.title}]](${entry.url})` : whole;
  });
}

// The follow-up block rides in the same completion as the answer, so the raw
// stream contains the delimiter. Hide it — including a partially-arrived one at
// the tail, or the marker flickers into view a character at a time as it lands.
function stripFollowupBlock(text) {
  const at = text.indexOf(FOLLOWUPS_DELIMITER);
  if (at !== -1) return text.slice(0, at);
  for (let n = FOLLOWUPS_DELIMITER.length - 1; n > 0; n--) {
    if (text.endsWith(FOLLOWUPS_DELIMITER.slice(0, n))) return text.slice(0, -n);
  }
  return text;
}

// The doubled brackets in `[[名称]](url)` exist so the backend's citation repair
// can find citations unambiguously in the model's output. They are punctuation
// for that parser, not for the reader, so drop them once the link exists.
function unwrapCitationLabels(root) {
  const links = root.querySelectorAll(
    'a[href*="bedtimenewsstudio.github.io/BedtimeNews-Transcripts"]',
  );
  for (const link of links) {
    const label = link.textContent;
    if (label.length > 2 && label.startsWith("[") && label.endsWith("]")) {
      link.textContent = label.slice(1, -1);
    }
  }
}

// Both of these read or write scroll geometry, which forces the browser to lay
// the page out. During a stream they are called from the throttled render, not
// per token, so that cost is paid ~12x a second instead of once per chunk.

// Is the reader following along at the bottom, rather than having scrolled up
// to re-read something? Must be sampled *before* new text is appended: growing
// the document moves the bottom away and would answer false every time.
function isNearBottom() {
  return window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
}

// Instant (not smooth) scrolling: on iOS Safari a perpetual smooth-scroll
// animation starves requestAnimationFrame callbacks, which would freeze the
// streamed answer mid-flight.
function scrollToEnd() {
  window.scrollTo({ top: document.body.scrollHeight });
}

/* ---------------------------------------------------------- sample questions */

// One question per category, every category shown. Which subjects the archive
// covers is the thing a first-time visitor has no way to guess, and it is real
// structure from starters.py rather than decoration — so breadth wins over depth
// here, and the whole set stays above the fold. 换一批 cycles the other ~60.
const PER_CATEGORY = 1;
const UINT32_RANGE = 2 ** 32;

function secureRandomIndex(upperBound) {
  const rejectionLimit = UINT32_RANGE - (UINT32_RANGE % upperBound);
  const value = new Uint32Array(1);
  do {
    crypto.getRandomValues(value);
  } while (value[0] >= rejectionLimit);
  return value[0] % upperBound;
}

// Fisher–Yates shuffle (returns a new array).
function shuffle(items) {
  const a = items.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = secureRandomIndex(i + 1);
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

let sampleCategories = [];

function renderSampleQuestions() {
  els.grid.replaceChildren();
  for (const cat of sampleCategories) {
    const topics = shuffle(cat.topics || []).slice(0, PER_CATEGORY);
    if (!topics.length) continue;

    const group = document.createElement("section");
    group.className = "sample-group";

    const label = document.createElement("h2");
    label.className = "sample-label";
    label.textContent = cat.name;
    group.appendChild(label);

    for (const topic of topics) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "topic";
      btn.textContent = topic.question;
      btn.addEventListener("click", () => askQuestion(topic.question));
      group.appendChild(btn);
    }
    els.grid.appendChild(group);
  }
}

async function loadSampleQuestions() {
  try {
    const res = await fetch("/api/starters");
    const data = await res.json();
    sampleCategories = data.categories || [];
    renderSampleQuestions();
    els.reshuffle.hidden = sampleCategories.length === 0;
  } catch (err) {
    console.error("Failed to load sample questions.", err);
    els.grid.innerHTML =
      '<p class="sample-error">示例加载失败，可直接在下方输入问题。</p>';
  }
}

/* -------------------------------------------------------------- conversation */

function addQueryTurn(question) {
  const node = document.getElementById("tpl-query").content.cloneNode(true);
  node.querySelector(".query-text").textContent = question;
  els.log.appendChild(node);
}

function addTransmissionTurn() {
  const frag = document.getElementById("tpl-transmission").content.cloneNode(true);
  els.log.appendChild(frag);
  const turn = els.log.lastElementChild;

  const signal = turn.querySelector(".signal");
  const head = turn.querySelector(".signal-head");
  head.addEventListener("click", () => {
    const collapsed = signal.dataset.collapsed === "true";
    signal.dataset.collapsed = String(!collapsed);
    head.setAttribute("aria-expanded", String(collapsed));
  });

  return {
    turn,
    signal,
    statusEl: turn.querySelector(".signal-status"),
    stages: turn.querySelectorAll(".stage-item"),
    answer: turn.querySelector(".answer"),
    answerBody: turn.querySelector(".answer-body"),
    counts: { retrieved: null, relevant: null },
  };
}

// The retrieve and grade steps carry the only numbers that say anything about
// how well grounded an answer is. Pull them out so the collapsed signal can keep
// showing them after the trace itself is folded away.
function captureCounts(ctx, stepType, content) {
  if (stepType === "retrieve") {
    const m = content.match(/(\d+)/);
    if (m) ctx.counts.retrieved = m[1];
  } else if (stepType === "grade") {
    const m = content.match(/\b(\d{1,9})[ \t]*(?:relevant\b|个?相关)/i);
    if (m) ctx.counts.relevant = m[1];
  }
}

function markStage(ctx, stepType, content) {
  const idx = STAGE_ORDER.indexOf(stepType);
  if (idx < 0) return;
  captureCounts(ctx, stepType, content);
  ctx.stages.forEach((item) => {
    const stage = item.dataset.stage;
    const stageIdx = STAGE_ORDER.indexOf(stage);
    if (stageIdx < idx) {
      item.dataset.status = "done";
    } else if (stageIdx === idx) {
      item.dataset.status = "active";
      const line = item.querySelector(".stage-line");
      if (content) line.textContent = content;
    } else {
      // Grading can send the pipeline back to query_rewrite for another pass.
      // Clear everything downstream of the stage we just re-entered, or the
      // previous attempt's 检索/评分 lines stay lit while 优化 runs again.
      delete item.dataset.status;
      item.querySelector(".stage-line").textContent = "";
    }
  });
  announce(`${STAGE_LABELS[stepType] || stepType}${content ? "：" + content : ""}`);
}

function lockSignal(ctx) {
  ctx.signal.dataset.state = "locked";
  ctx.signal.dataset.collapsed = "true";
  ctx.signal.querySelector(".signal-head").setAttribute("aria-expanded", "false");
  // Collapsing the trace used to leave nothing behind but "信号已锁定", which
  // says only that the pipeline finished. The counts are the part worth keeping:
  // they are what makes the answer look grounded rather than asserted.
  const { retrieved, relevant } = ctx.counts;
  ctx.statusEl.textContent = relevant
    ? `检索 ${retrieved || "—"} · 相关 ${relevant}`
    : "信号已锁定";
  ctx.stages.forEach((item) => {
    if (item.dataset.status === "active") {
      item.dataset.status = "done";
    }
  });
}

// Suggestions belong to the turn that produced them; once a new question is
// asked they are stale, so the previous turn's buttons are cleared rather than
// left around inviting a click that no longer follows from anything.
function clearFollowups() {
  for (const block of els.log.querySelectorAll(".followups")) block.remove();
}

// Keep a turn for the backend to replay. Truncated here rather than server-side
// so the payload stays small on the way up.
function recordTurn(question, answer, grounded) {
  if (!answer) return;
  conversation.push({
    question,
    answer: answer.slice(0, HISTORY_ANSWER_CHARS),
    grounded,
  });
}

function renderFollowups(ctx, items) {
  if (!items?.length) return;
  const wrap = document.createElement("div");
  wrap.className = "followups";

  const label = document.createElement("span");
  label.className = "followups-label";
  label.textContent = "继续追问";
  wrap.appendChild(label);

  for (const question of items) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "followup";
    btn.textContent = question;
    btn.addEventListener("click", () => askQuestion(question));
    wrap.appendChild(btn);
  }
  ctx.answer.appendChild(wrap);
}

/* ------------------------------------------------------------- SSE handling */

const RENDER_INTERVAL_MS = 80;

function beginQuestion(question) {
  clearFollowups();
  busy = true;
  abortController = new AbortController();
  setComposerBusy(true);
  els.hero.classList.add("is-hidden");

  addQueryTurn(question);
  const ctx = addTransmissionTurn();
  scrollToEnd();

  // Warm the Markdown renderer while the pipeline runs. Failures are handled at
  // finalize time, where there is an answer to fall back to.
  loadMarkdown().catch((err) => {
    console.warn("Markdown renderer warmup failed; plain text remains available.", err);
  });
  return ctx;
}

function createAnswerState(ctx) {
  return {
    ctx,
    answerText: "",
    finalText: "",
    citationUrls: null,
    followups: [],
    // Reported by the server rather than guessed from whether citations arrived.
    grounded: false,
    streaming: false,
    lastRender: 0,
    renderTimer: null,
  };
}

function clearScheduledRender(state) {
  if (!state.renderTimer) return;
  clearTimeout(state.renderTimer);
  state.renderTimer = null;
}

function renderAnswerMarkdown(answerBody, md, text, citationUrls) {
  answerBody.style.whiteSpace = "";
  answerBody.innerHTML = renderMarkdown(md, linkifyCitations(text, citationUrls));
  unwrapCitationLabels(answerBody);
}

function renderStreamingText(state) {
  clearScheduledRender(state);
  state.lastRender = Date.now();
  const stick = isNearBottom();
  const visibleText = stripFollowupBlock(state.answerText);
  if (mdReady) {
    renderAnswerMarkdown(state.ctx.answerBody, mdReady, visibleText, state.citationUrls);
  } else {
    // Renderer still in flight: pre-wrap plain text keeps the answer readable
    // until it lands, and the next tick upgrades it.
    state.ctx.answerBody.style.whiteSpace = "pre-wrap";
    state.ctx.answerBody.textContent = state.answerText;
  }
  if (stick) scrollToEnd();
}

function scheduleRender(state) {
  if (state.renderTimer) return;
  const elapsed = Date.now() - state.lastRender;
  if (elapsed >= RENDER_INTERVAL_MS) {
    renderStreamingText(state);
    return;
  }
  state.renderTimer = setTimeout(
    () => renderStreamingText(state),
    RENDER_INTERVAL_MS - elapsed
  );
}

// Final pass. This re-renders even though the stream was already rendering,
// because answer_final differs from the accumulated chunks: the chunks are raw
// model output, while answer_final has been through citation repair, so this
// is where broken 《名称》 references become real links. If the renderer never
// arrived, the pre-wrap plain text on screen is a legible answer.
async function finalizeAnswer(state) {
  clearScheduledRender(state);
  // answer_final already has the follow-up block removed server-side; the strip
  // matters for the fallback where only the raw chunks arrived.
  const text = stripFollowupBlock(state.finalText || state.answerText);
  if (!text) return;
  try {
    const md = await loadMarkdown();
    renderAnswerMarkdown(state.ctx.answerBody, md, text, state.citationUrls);
  } catch (err) {
    console.warn("Markdown rendering failed; keeping the plain-text answer.", err);
    state.ctx.answerBody.textContent = text;
  }
}

function parseSseEvent(block) {
  const dataLine = block.split("\n").find((line) => line.startsWith("data: "));
  if (!dataLine) return null;
  const payload = dataLine.slice(6);
  if (payload === "[DONE]") return null;
  try {
    return JSON.parse(payload);
  } catch (err) {
    console.warn("Ignoring a malformed stream event.", err);
    return null;
  }
}

function startStreamingAnswer(state) {
  if (state.streaming) return;
  state.streaming = true;
  lockSignal(state.ctx);
  state.ctx.answer.hidden = false;
  state.ctx.answer.classList.add("is-streaming");
}

function handleStreamEvent(state, event) {
  switch (event.type) {
    case "step":
      markStage(state.ctx, event.step, cleanStep(event.content || ""));
      break;
    case "answer_chunk": {
      const chunk = event.content || "";
      if (!chunk) return;
      startStreamingAnswer(state);
      state.answerText += chunk;
      scheduleRender(state);
      break;
    }
    case "citations":
      state.citationUrls = event.urls || null;
      break;
    case "followups":
      state.followups = event.items || [];
      break;
    case "answer_final":
      state.finalText = event.content || "";
      state.grounded = !!event.grounded;
      break;
    case "answer_meta":
      // Sent when the streamed text needs no replacement; carries only whether
      // this turn was answered from retrieved documents.
      state.grounded = !!event.grounded;
      break;
    case "error":
      throw new Error(event.content || "服务内部错误");
  }
}

async function consumeEventStream(body, state) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });

    // SSE events are separated by a blank line.
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const block of events) {
      const event = parseSseEvent(block);
      if (event) handleStreamEvent(state, event);
    }
  }
}

async function requestAnswerStream(question, signal) {
  const response = await fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question,
      history: conversation.slice(-HISTORY_TURNS),
      stream: true,
    }),
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`HTTP ${response.status}`);
  }
  return response.body;
}

async function completeQuestion(question, state) {
  // answer_final can arrive without any chunks if the model never streamed.
  if (!state.streaming && state.finalText) {
    lockSignal(state.ctx);
    state.ctx.answer.hidden = false;
  }
  await finalizeAnswer(state);
  if (!state.streaming && !state.finalText) {
    // No answer arrived — surface a graceful fallback.
    lockSignal(state.ctx);
    state.ctx.answer.hidden = false;
    state.ctx.answerBody.innerHTML =
      '<p class="answer-empty">未能生成回答，请换个问法再试。</p>';
  }
  state.ctx.answer.classList.remove("is-streaming");
  recordTurn(
    question,
    stripFollowupBlock(state.finalText || state.answerText),
    state.grounded
  );
  renderFollowups(state.ctx, state.followups);
  announce("回答完成");
}

async function handleQuestionError(err, state) {
  const stopped = err.name === "AbortError";
  lockSignal(state.ctx);
  state.ctx.answer.hidden = false;
  state.ctx.answer.classList.remove("is-streaming");
  // A stop is a choice, not a failure: keep whatever arrived, rendered, and
  // say so plainly instead of dressing it up as an error.
  await finalizeAnswer(state);
  if (stopped) {
    state.ctx.statusEl.textContent = "已停止";
    if (!state.answerText && !state.finalText) {
      state.ctx.answerBody.innerHTML = '<p class="answer-empty">已停止生成。</p>';
    }
    announce("已停止生成");
    return;
  }

  state.ctx.statusEl.textContent = "信号中断";
  const msg = document.createElement("p");
  msg.className = "answer-error";
  msg.textContent = `信号中断：${err.message}。请稍后重试。`;
  state.ctx.answerBody.appendChild(msg);
  announce(`信号中断：${err.message}`);
}

function finishQuestion() {
  busy = false;
  abortController = null;
  setComposerBusy(false);
  // Refocusing raises the on-screen keyboard, which on a phone covers the
  // answer the reader was waiting for. Only worth doing where focus is free.
  if (!window.matchMedia("(pointer: coarse)").matches) els.input.focus();
  scrollToEnd();
}

async function askQuestion(rawQuestion) {
  const question = (rawQuestion ?? "").trim();
  if (!question || busy) return;

  const ctx = beginQuestion(question);
  const state = createAnswerState(ctx);
  // Markdown is rendered as the answer arrives, so headings, lists and citation
  // links appear while the reader is already reading rather than snapping into
  // place at the end. Each pass re-parses the whole accumulated answer, so it is
  // throttled by wall clock — never per chunk, which is what made this O(n^2)
  // and froze the stream on slower mobile CPUs. Wall clock rather than rAF
  // because iOS Safari pauses rAF callbacks while scrolling.
  try {
    const body = await requestAnswerStream(question, abortController.signal);
    await consumeEventStream(body, state);
    await completeQuestion(question, state);
  } catch (err) {
    await handleQuestionError(err, state);
  } finally {
    finishQuestion();
  }
}

/* ----------------------------------------------------------------- composer */

// While an answer is streaming the field stays typable — a reader who already
// knows their follow-up should be able to write it instead of waiting — and the
// send button becomes the stop control for the run in flight.
function setComposerBusy(isBusy) {
  els.send.classList.toggle("is-stop", isBusy);
  els.send.textContent = isBusy ? "停止" : "发送";
  els.send.setAttribute("aria-label", isBusy ? "停止生成" : "发送问题");
  if (!isBusy) {
    const arrow = document.createElement("span");
    arrow.className = "send-arrow";
    arrow.setAttribute("aria-hidden", "true");
    arrow.textContent = "▸";
    els.send.appendChild(arrow);
  }
}

function stopStreaming() {
  if (abortController) abortController.abort();
}

function autoGrow() {
  els.input.style.height = "auto";
  els.input.style.height = `${Math.min(els.input.scrollHeight, 144)}px`;
}

// The button is inside the form, so a click while streaming would otherwise try
// to submit. Intercept before that and stop the run instead.
els.send.addEventListener("click", (e) => {
  if (!busy) return;
  e.preventDefault();
  stopStreaming();
});

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (busy) return;
  const q = els.input.value;
  els.input.value = "";
  autoGrow();
  askQuestion(q);
});

els.input.addEventListener("input", autoGrow);
els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    els.form.requestSubmit();
  }
});

/* -------------------------------------------------------------- theme toggle */

const themeToggle = document.getElementById("theme-toggle");
const systemThemeQuery = window.matchMedia("(prefers-color-scheme: dark)");

function currentSystemTheme() {
  return systemThemeQuery.matches ? "dark" : "light";
}

function currentTheme() {
  return document.documentElement.dataset.theme === "light"
    ? "light"
    : "dark";
}

function updateThemeToggle() {
  const nextLabel = currentTheme() === "light" ? "深色" : "浅色";
  const label = `切换到${nextLabel}主题`;
  themeToggle.setAttribute("aria-label", label);
  themeToggle.setAttribute("title", label);
}

// Must match --theme-fade in styles.css, plus a margin: stripping the class
// while the transition is still running cancels it and snaps the last few
// percent, which is exactly the jolt this is meant to remove.
const THEME_FADE_MS = 320 + 120;
const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
let themeFadeTimer = null;

// The cross-fade class lives only for the length of the swap. Leaving it on
// permanently would make every hover and focus change inherit the long theme
// transition, and putting it in the markup would animate the very first paint.
function crossFadeTheme() {
  if (reducedMotionQuery.matches) return;
  const root = document.documentElement;
  root.classList.add("theme-fade");
  clearTimeout(themeFadeTimer);
  themeFadeTimer = setTimeout(() => {
    root.classList.remove("theme-fade");
    themeFadeTimer = null;
  }, THEME_FADE_MS);
}

function applyTheme(theme, preference) {
  if (theme !== currentTheme()) crossFadeTheme();
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.themePreference = preference;
  updateThemeToggle();
}

// sessionStorage rather than localStorage: the choice holds for the rest of
// this visit, including reloads, but every fresh arrival starts from the OS
// preference again instead of inheriting a decision made days ago.
themeToggle.addEventListener("click", () => {
  const next = currentTheme() === "light" ? "dark" : "light";
  applyTheme(next, "user");
  try {
    sessionStorage.setItem("theme", next);
  } catch (e) {
    console.warn("Theme storage is unavailable; the page theme still changed.", e);
  }
});

// Follow OS changes until the reader uses the icon to choose an explicit
// theme. A manual choice then holds for the rest of the session.
systemThemeQuery.addEventListener("change", () => {
  if (document.documentElement.dataset.themePreference === "system") {
    applyTheme(currentSystemTheme(), "system");
  }
});

updateThemeToggle();

// Version label. Fetched rather than templated because index.html is served as
// a static file; failure leaves the element hidden rather than showing a blank.
async function loadVersion() {
  try {
    const res = await fetch("/healthz");
    const { version } = await res.json();
    if (!version) return;
    const displayVersion = version.startsWith("v") ? version : `v${version}`;
    els.version.textContent = `${displayVersion} · `;
    els.version.hidden = false;
  } catch (err) {
    console.warn("Version metadata is unavailable; hiding the version label.", err);
  }
}

els.reshuffle.addEventListener("click", renderSampleQuestions);

const initialDataPromise = Promise.all([loadSampleQuestions(), loadVersion()]);
// Same reasoning as after a run: autofocus on a phone opens the keyboard over
// the sample questions before the reader has seen them.
if (!window.matchMedia("(pointer: coarse)").matches) els.input.focus();

// Deep link: /?q=... opens straight into a query (shareable links).
const deepLink = new URLSearchParams(location.search).get("q");
if (deepLink) {
  await askQuestion(deepLink);
}
await initialDataPromise;
