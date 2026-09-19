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
  appShell: document.getElementById("app-shell"),
  stage: document.getElementById("stage"),
  channelBar: document.getElementById("channel-bar"),
  archiveView: document.getElementById("archive-view"),
  archiveTitle: document.getElementById("archive-title"),
  archiveGroups: document.getElementById("archive-groups"),
  archiveSearch: document.getElementById("archive-search"),
  archiveState: document.getElementById("archive-state"),
  readingPane: document.getElementById("reading-pane"),
  readingScroll: document.getElementById("reading-scroll"),
  readingBarLabel: document.getElementById("reading-bar-label"),
  readingBack: document.getElementById("reading-back"),
  readingClose: document.getElementById("reading-close"),
  readerView: document.getElementById("reader-view"),
  readerTitle: document.getElementById("reader-title"),
  readerBody: document.getElementById("reader-body"),
  readerState: document.getElementById("reader-state"),
  chatPane: document.getElementById("chat-pane"),
  chatFab: document.getElementById("chat-fab"),
  chatClose: document.getElementById("chat-close"),
};

let busy = false;
// Aborts the run in flight when the reader hits stop.
let abortController = null;
const mobileDrawer = window.matchMedia("(max-width: 900px)");
// Must match the .reading-pane flex-basis transition in styles.css.
const PANE_SLIDE_MS = 460;

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

  // Keep same-origin transcript citations inside this SPA; only genuinely
  // external links open a new tab.
  const defaultLinkOpen =
    md.renderer.rules.link_open ||
    function (tokens, idx, options, env, self) {
      return self.renderToken(tokens, idx, options);
    };
  md.renderer.rules.link_open = function (tokens, idx, options, env, self) {
    const href = String(tokens[idx].attrGet("href") || "");
    if (href.startsWith("/transcripts/")) {
      tokens[idx].attrSet("data-transcript-link", "true");
    } else {
      tokens[idx].attrSet("target", "_blank");
      tokens[idx].attrSet("rel", "noopener noreferrer");
    }
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
  const links = root.querySelectorAll("a[data-transcript-link]");
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

// The conversation scrolls inside .stage — the page itself never scrolls, so
// the chat keeps its place through every squeeze and release of the reading
// pane instead of being re-anchored to a moving window.

// Is the reader following along at the bottom, rather than having scrolled up
// to re-read something? Must be sampled *before* new text is appended: growing
// the document moves the bottom away and would answer false every time.
function isNearBottom() {
  return (
    els.stage.clientHeight + els.stage.scrollTop >= els.stage.scrollHeight - 160
  );
}

// Instant (not smooth) scrolling: on iOS Safari a perpetual smooth-scroll
// animation starves requestAnimationFrame callbacks, which would freeze the
// streamed answer mid-flight.
function scrollToEnd() {
  els.stage.scrollTo({ top: els.stage.scrollHeight });
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

/* ------------------------------------------------------- channel + reader */

const CHANNEL_LABELS = {
  ShuiQianXiaoXi: "睡前消息",
  CanKaoXinXi: "参考信息",
  GaoJian: "高见",
  JiangDianHeiHua: "讲点黑话",
  ChanJingPoBiJi: "产经破壁机",
};

let transcriptItems = null;
let transcriptIndexPromise = null;
let currentArticle = null;
// What the reading pane holds: null (closed) | "archive" | "reader".
let panelLevel = null;
// Channel scope of the list, and of the article on screen while reading.
let currentChannel = null;
let routeGeneration = 0;

function transcriptPath(uri) {
  return `/transcripts/${uri.split("/").map(encodeURIComponent).join("/")}`;
}

function channelLabel(channel) {
  return CHANNEL_LABELS[channel] || channel;
}

// One fetch serves the channel bar, the archive and the reader's prev/next
// pair. Callers await the same promise; only a failure clears it, so a later
// route can retry.
async function loadTranscriptIndex() {
  if (transcriptItems) return transcriptItems;
  if (!transcriptIndexPromise) {
    transcriptIndexPromise = fetch("/api/transcripts")
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        transcriptItems = data.items || [];
        renderChannelBar(transcriptItems);
        return transcriptItems;
      })
      .catch((error) => {
        transcriptIndexPromise = null;
        throw error;
      });
  }
  return transcriptIndexPromise;
}

/* ------------------------------------------------------------- channel bar */

// Loudest signal first: the channel carrying the most transcripts leads, which
// reads as a frequency index instead of an alphabetical list nobody scans.
function renderChannelBar(items) {
  const counts = new Map();
  for (const item of items) {
    counts.set(item.channel, (counts.get(item.channel) || 0) + 1);
  }
  const ordered = [...counts.keys()].sort(
    (a, b) => counts.get(b) - counts.get(a) || a.localeCompare(b),
  );

  const chips = ordered.map((channel) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "channel-chip";
    chip.dataset.channel = channel;
    chip.textContent = channelLabel(channel);
    chip.title = `${channel} · ${counts.get(channel)} 篇`;
    return chip;
  });

  els.channelBar.replaceChildren(...chips);
  els.channelBar.hidden = chips.length === 0;
  updateChannelBar();
}

// The chip for whatever the pane is currently showing stays lit; with the pane
// closed nothing is current, since the conversation is not inside a channel.
function updateChannelBar() {
  const active = panelLevel ? currentChannel : null;
  for (const chip of els.channelBar.children) {
    chip.setAttribute("aria-current", String(chip.dataset.channel === active));
  }
}

/* ------------------------------------------------------------------ archive */

function itemSearchText(item) {
  // Title and episode only — not URI path or publication date.
  return [item.canonical_title, item.source_title]
    .filter(Boolean)
    .join(" ")
    .toLocaleLowerCase("zh-CN");
}

function renderArchiveHead() {
  els.archiveTitle.textContent = channelLabel(currentChannel);
}

// "睡前消息588" under "睡前消息588" is the same words twice. When the source
// title wraps the canonical one in brackets, that is what it always looks like:
// the brackets hold the episode's own subject line, which is the useful half.
function archiveRowTitle(item) {
  const canonical = item.canonical_title || "";
  const source = item.source_title || item.doc_id;
  const wrapped = canonical && `【${canonical}】`;
  if (wrapped && source.startsWith(wrapped)) {
    return source.slice(wrapped.length).trim() || source;
  }
  return source;
}

function renderArchive(items = transcriptItems || []) {
  const scoped = currentChannel
    ? items.filter((item) => item.channel === currentChannel)
    : [];
  const query = els.archiveSearch.value.trim().toLocaleLowerCase("zh-CN");
  const visible = query
    ? scoped.filter((item) => itemSearchText(item).includes(query))
    : scoped;
  els.archiveGroups.replaceChildren();

  // The vertical frequency line still marks the block as one channel's signal,
  // now that no heading sits above it to do that job.
  const section = document.createElement("section");
  section.className = "archive-group";
  const list = document.createElement("ol");
  list.className = "archive-list";
  for (const item of visible) {
    const row = document.createElement("li");
    const link = document.createElement("a");
    link.href = transcriptPath(item.doc_id);
    link.dataset.route = "";
    const label = document.createElement("span");
    label.className = "archive-item-title";
    label.textContent = item.canonical_title || item.source_title || item.doc_id;
    const source = document.createElement("span");
    source.className = "archive-item-source";
    source.textContent = archiveRowTitle(item);
    link.append(label, source);
    if (item.publication_date) {
      const date = document.createElement("time");
      date.dateTime = item.publication_date;
      date.textContent = item.publication_date;
      link.append(date);
    }
    row.appendChild(link);
    list.appendChild(row);
  }
  section.appendChild(list);
  els.archiveGroups.appendChild(section);

  els.archiveState.hidden = visible.length > 0;
  if (!visible.length) {
    els.archiveState.textContent = query
      ? "没有匹配的文稿。可尝试标题或期号。"
      : "此栏目暂无文稿。";
  }
}

/* --------------------------------------------------------------- panel view */

// Squeezing the column rewraps the conversation. A reader who was following the
// bottom of an answer would otherwise be left a few lines behind it, so they are
// re-pinned once the column has settled.
let paneSettleTimer = null;

function keepChatAtBottom(wasAtBottom) {
  clearTimeout(paneSettleTimer);
  if (!wasAtBottom) return;
  paneSettleTimer = setTimeout(() => {
    paneSettleTimer = null;
    scrollToEnd();
  }, PANE_SLIDE_MS + 40);
}

function setView(view) {
  const wasAtBottom = isNearBottom();
  document.body.dataset.view = view;
  const browse = view === "browse";
  if (!browse) {
    document.body.classList.remove("chat-open");
    els.chatFab.setAttribute("aria-expanded", "false");
  }
  // A closed pane is clipped to zero width but still in the document, so it has
  // to be taken out of the tab order and the accessibility tree by hand.
  els.readingPane.toggleAttribute("inert", !browse);
  if (!browse && els.readingPane.contains(document.activeElement)) {
    els.appShell.focus();
  }
  syncChatPanelAccessibility();
  keepChatAtBottom(wasAtBottom);
}

function setPanelLevel(level) {
  panelLevel = level;
  els.archiveView.hidden = level !== "archive";
  els.readerView.hidden = level !== "reader";
  updateReadingBar();
  updateChannelBar();
}

// Back steps down one level: article -> its own channel's list -> every
// channel. The top level has nothing above it, so the control is hidden there.
function readingBackTarget() {
  if (panelLevel !== "reader") return null;
  const channel = currentArticle?.channel;
  return channel
    ? `/transcripts?channel=${encodeURIComponent(channel)}`
    : "/";
}

function updateReadingBar() {
  els.readingBack.hidden = !readingBackTarget();
  if (panelLevel === "reader") {
    els.readingBarLabel.textContent =
      currentArticle?.canonical_title || "正在载入…";
  } else if (panelLevel === "archive") {
    els.readingBarLabel.textContent = channelLabel(currentChannel);
  } else {
    els.readingBarLabel.textContent = "";
  }
}

async function showArchive(generation, channel) {
  currentArticle = null;
  currentChannel = channel;
  setView("browse");
  setPanelLevel("archive");
  renderArchiveHead();
  els.readingScroll.scrollTo({ top: 0 });
  document.title = `${channelLabel(channel)} · 睡前消息知识库`;
  els.archiveState.hidden = false;
  els.archiveState.textContent = "正在接收文稿目录…";
  try {
    const items = await loadTranscriptIndex();
    if (generation !== routeGeneration) return;
    renderArchive(items);
  } catch (error) {
    if (generation !== routeGeneration) return;
    console.error("Transcript index unavailable.", error);
    els.archiveGroups.replaceChildren();
    els.archiveState.hidden = false;
    els.archiveState.textContent = "文稿目录暂时不可用，请稍后重试。";
  }
}

function prepareReader(uri) {
  currentArticle = null;
  els.readerState.textContent = "正在接收文稿…";
  els.readerTitle.textContent = "";
  els.readerBody.replaceChildren();
  // Re-trigger the page-in. The element is reused between articles, so the
  // animation has to be dropped and reflowed back on to run again.
  els.readerView.classList.remove("is-entering");
  void els.readerView.offsetWidth;
  els.readerView.classList.add("is-entering");
}

async function showReader(uri, generation) {
  currentChannel = null;
  setView("browse");
  setPanelLevel("reader");
  prepareReader(uri);
  els.readingScroll.scrollTo({ top: 0 });
  document.title = "文稿 · 睡前消息知识库";
  try {
    const response = await fetch(
      `/api/transcripts/${uri.split("/").map(encodeURIComponent).join("/")}`,
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const article = await response.json();
    if (generation !== routeGeneration) return;
    currentArticle = article;
    currentChannel = article.channel;
    updateReadingBar();
    updateChannelBar();
    document.title = `${article.canonical_title || article.source_title} · 睡前消息知识库`;
    els.readerTitle.textContent = article.source_title;
    els.readerBody.innerHTML = article.body_html;
    for (const link of els.readerBody.querySelectorAll("a")) {
      const href = link.getAttribute("href") || "";
      if (href.startsWith("/transcripts/")) {
        link.dataset.route = "";
      } else if (href.startsWith("#")) {
        // Footnote refs / backrefs stay inside the reading pane.
        link.classList.add("transcript-fragment-link");
      } else if (href) {
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      }
    }
    els.readerState.textContent = "";
  } catch (error) {
    if (generation !== routeGeneration) return;
    console.error("Transcript unavailable.", error);
    els.readerState.textContent = "这篇文稿暂时不可用，或已从上游删除。";
    document.title = "文稿不可用 · 睡前消息知识库";
  }
}

/* -------------------------------------------------------------- the router */

// The landing surface: the conversation centred, no channel chosen. Also the
// destination for a route that has nothing to show.
function showChat() {
  currentArticle = null;
  currentChannel = null;
  setView("chat");
  setPanelLevel(null);
  document.title = "睡前消息知识库";
}

async function renderRoute() {
  const generation = ++routeGeneration;
  const { pathname, search } = window.location;
  const channel = new URLSearchParams(search).get("channel") || null;

  // A channel is the whole of this route now; without one there is no page here.
  if (pathname === "/transcripts" || pathname === "/transcripts/") {
    if (channel) {
      await showArchive(generation, channel);
      return;
    }
    history.replaceState({}, "", "/");
    showChat();
    return;
  }

  if (pathname.startsWith("/transcripts/")) {
    const raw = pathname.slice("/transcripts/".length);
    let uri = "";
    try {
      uri = raw.split("/").map(decodeURIComponent).join("/");
    } catch (error) {
      console.error("Malformed transcript route.", error);
    }
    if (uri) {
      await showReader(uri, generation);
      return;
    }
    history.replaceState({}, "", "/");
    showChat();
    return;
  }

  if (pathname !== "/" && pathname !== "") {
    history.replaceState({}, "", "/");
  }
  showChat();
}

// Every transcript link in the page — channel chips, archive rows, citations in
// an answer, links inside an article — lands here. When the pane is open the
// article is swapped in place and the conversation is left exactly as it was,
// which is what makes the pane feel like a window rather than a navigation.
function navigate(href) {
  const url = new URL(href, window.location.origin);
  if (url.origin !== window.location.origin) return;
  const next = `${url.pathname}${url.search}`;
  if (next === `${window.location.pathname}${window.location.search}`) return;
  history.pushState({}, "", next);
  renderRoute();
}

/* --------------------------------------------------------- chat pane state */

function syncChatPanelAccessibility() {
  const drawerClosed =
    mobileDrawer.matches &&
    document.body.dataset.view === "browse" &&
    !document.body.classList.contains("chat-open");
  els.chatPane.toggleAttribute("inert", drawerClosed);
  if (drawerClosed) {
    els.chatPane.setAttribute("aria-hidden", "true");
  } else {
    els.chatPane.removeAttribute("aria-hidden");
  }
}

function openChatPanel() {
  if (document.body.dataset.view === "chat") return;
  document.body.classList.add("chat-open");
  els.chatFab.setAttribute("aria-expanded", "true");
  syncChatPanelAccessibility();
  window.setTimeout(() => els.input.focus(), 0);
}

function closeChatPanel() {
  document.body.classList.remove("chat-open");
  els.chatFab.setAttribute("aria-expanded", "false");
  syncChatPanelAccessibility();
  if (mobileDrawer.matches && document.body.dataset.view === "browse") {
    els.chatFab.focus();
  }
}

els.archiveSearch.addEventListener("input", () => renderArchive());
els.channelBar.addEventListener("click", (event) => {
  const chip = event.target.closest(".channel-chip");
  if (!chip) return;
  const channel = chip.dataset.channel;
  navigate(`/transcripts?channel=${encodeURIComponent(channel)}`);
});
els.readingClose.addEventListener("click", () => navigate("/"));

// Fragment links (footnote ref <-> appendix) must move only #reading-scroll.
// scrollIntoView() and bare hash navigation also scroll ancestor/viewport
// containers; with a 100dvh locked shell that shoves the whole desk upward and
// leaves a blank band of page background.
function scrollReadingPaneTo(target) {
  const scroller = els.readingScroll;
  if (!scroller || !target) return;
  // Measure against the scroller's padding box, then clamp so a short appendix
  // near the end cannot overscroll and leave empty canvas below.
  const offset =
    target.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
  const pad = 16;
  const maxTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  const top = Math.min(maxTop, Math.max(0, scroller.scrollTop + offset - pad));
  const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  scroller.scrollTo({ top, behavior: smooth ? "smooth" : "auto" });
}

els.readerBody.addEventListener("click", (event) => {
  const link = event.target.closest("a.transcript-fragment-link, a[href^='#']");
  if (!link || !els.readerBody.contains(link)) return;
  const href = link.getAttribute("href") || "";
  if (!href.startsWith("#") || href === "#") return;
  let id = href.slice(1);
  try {
    id = decodeURIComponent(id);
  } catch {
    /* keep raw id */
  }
  const target =
    els.readerBody.querySelector(`#${CSS.escape(id)}`) ||
    els.readingScroll.querySelector(`#${CSS.escape(id)}`);
  if (!target) return;
  event.preventDefault();
  event.stopPropagation();
  // Keep :target styles without letting the browser scroll the document.
  const next = `${location.pathname}${location.search}#${encodeURIComponent(id)}`;
  if (`${location.pathname}${location.search}${location.hash}` !== next) {
    history.replaceState(history.state, "", next);
  }
  scrollReadingPaneTo(target);
});
els.readingBack.addEventListener("click", () => {
  const target = readingBackTarget();
  if (target) navigate(target);
});
els.chatFab.addEventListener("click", openChatPanel);
els.chatClose.addEventListener("click", closeChatPanel);
mobileDrawer.addEventListener("change", syncChatPanelAccessibility);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (document.body.classList.contains("chat-open")) {
    closeChatPanel();
    return;
  }
  // Escape also backs out of the reading pane, the way it closes a modal.
  if (panelLevel) navigate("/");
});
document.addEventListener("click", (event) => {
  if (event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest('a[data-route], a[href^="/transcripts/"]');
  if (!link) return;
  event.preventDefault();
  navigate(link.href);
});

window.addEventListener("popstate", renderRoute);

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

const initialDataPromise = Promise.all([
  loadSampleQuestions(),
  loadVersion(),
  // The channel bar is part of every route, including the landing one, so the
  // index is fetched up front. A failure only costs the bar.
  loadTranscriptIndex().catch((error) => {
    console.warn("Transcript index unavailable; the channel bar stays hidden.", error);
  }),
]);
// Not awaited before the focus decision: every route sets data-view
// synchronously, so the landing surface is already known.
const routePromise = renderRoute();

// Same reasoning as after a run: autofocus on a phone opens the keyboard over
// the sample questions before the reader has seen them.
if (
  document.body.dataset.view === "chat" &&
  !window.matchMedia("(pointer: coarse)").matches
) {
  els.input.focus();
}

// Deep link: /?q=... opens straight into a query (shareable links).
const deepLink = new URLSearchParams(location.search).get("q");
if (deepLink && document.body.dataset.view === "chat") {
  await askQuestion(deepLink);
}
await Promise.all([initialDataPromise, routePromise]);
