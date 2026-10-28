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
// trades context depth against payload size; three is enough to resolve almost
// all pronouns without carrying the whole session.
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
  readingBack: document.getElementById("reading-back"),
  readingDocBack: document.getElementById("reading-doc-back"),
  readingClose: document.getElementById("reading-close"),
  readingEdit: document.getElementById("reading-edit"),
  readingShare: document.getElementById("reading-share"),
  shareSheet: document.getElementById("share-sheet"),
  shareCopy: document.getElementById("share-copy"),
  shareDownload: document.getElementById("share-download"),
  shareToast: document.getElementById("share-toast"),
  readerView: document.getElementById("reader-view"),
  readerTitle: document.getElementById("reader-title"),
  readerBody: document.getElementById("reader-body"),
  readerState: document.getElementById("reader-state"),
  chatPane: document.getElementById("chat-pane"),
  searchPane: document.getElementById("search-pane"),
  searchForm: document.getElementById("search-form"),
  searchInput: document.getElementById("search-input"),
  searchGo: document.getElementById("search-go"),
  searchResults: document.getElementById("search-results"),
  searchStatus: document.getElementById("search-status"),
  modeTabs: document.getElementById("mode-tabs"),
  modeTabChat: document.getElementById("mode-tab-chat"),
  modeTabSearch: document.getElementById("mode-tab-search"),
  latestView: document.getElementById("latest-view"),
  tabChat: document.getElementById("tab-chat"),
  tabArchive: document.getElementById("tab-archive"),
};

let busy = false;
// Aborts the run in flight when the reader hits stop.
let abortController = null;
const mobileLayout = window.matchMedia("(max-width: 900px)");
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
    // A citation inside single backticks renders as code, so the raw
    // [[title]](url) is what the reader sees. Drop that pair.
    .replace(/`(\[\[[^`]+?\]\](?:\([^)`\n]*\))?)`/g, "$1")
    .replace(BOLD_CITATION_RE, "$2")
    .replace(/^([ \t]*)(\d{1,9}[.)])(?=[^\s\d])/gm, "$1$2 ")
    .replace(/^([ \t]*)([*+-])(?=[^\s*+-])/gm, "$1$2 ");
}

// Citations arrive as ordinary markdown links — `[[标准化标题]](/transcripts/<URI>.md)` —
// so markdown-it turns them into <a> elements on its own; styles.css picks them
// out by href.
function renderMarkdown(md, raw) {
  return md.render(normalizeMarkdown(raw));
}

// Mirror of the server's _repair_citations, applied to the partial answer while
// it streams. The model copies a `Cite:` token — `[[<URI>]]` or
// `[[<URI>#<anchor>]]` — which is not what a reader should see, and the server
// only rewrites it once generation has finished, too late for someone watching
// the text appear. The "citations" event carries a name -> {title, url} map
// keyed by URI, 标准化标题, and `URI#anchor` when the chunk has one, so the same
// substitution can run per render tick. A half-streamed `[[ShuiQianXiaoXi/0501-`
// has no closing brackets yet, so it simply doesn't match and is upgraded on a
// later tick. The `#` stays inside the token; an anchor this retrieval did not
// return falls back to the whole document.
const CITATION_RE = /(?:\[\[([^[\]]+?)\]\]|《([^《》]+?)》)(\([^)]*\))?/g;

// One or more citation links separated only by whitespace. Mirror of the
// server's _dedupe_citation_runs: each distinct link stays once per run.
const CITATION_LINK = String.raw`\[\[[^\[\]]+?\]\]\([^()\s]*\)`;
const CITATION_RUN_RE = new RegExp(String.raw`${CITATION_LINK}(?:\s*${CITATION_LINK})+`, "g");
const CITATION_LINK_RE = new RegExp(CITATION_LINK, "g");

function dedupeCitationRuns(text) {
  return text.replace(CITATION_RUN_RE, (run) =>
    [...new Set(run.match(CITATION_LINK_RE))].join(" "),
  );
}

// Where a citation run belongs: inside the sentence it supports, in full-width
// parentheses, directly before the sentence's final punctuation. Mirror of the
// server's _place_citation_runs, so streaming and the final answer agree.
//   ...很激烈。[[a]] [[b]] 下一句   ->   ...很激烈（[[a]] [[b]]）。 下一句
const SENTENCE_END = "。！？；!?;…";
const PLACE_RUN_RE = new RegExp(String.raw`${CITATION_LINK}(?:[ \t]*${CITATION_LINK})*`, "g");
const LIST_PREFIX_RE = /^\s*(?:[-*+]|\d+[.)])?\s*$/;

function insideParenthetical(paragraph) {
  return (
    Math.max(paragraph.lastIndexOf("（"), paragraph.lastIndexOf("(")) >
    Math.max(paragraph.lastIndexOf("）"), paragraph.lastIndexOf(")"))
  );
}

// Remove text[start:end] and the spaces that would be left around it.
function cutCitationRun(text, start, end) {
  let after = end;
  while (after < text.length && (text[after] === " " || text[after] === "\t")) after++;
  const following = after < text.length ? text[after] : "\n";
  if (following === "\n" || SENTENCE_END.includes(following)) {
    let before = start;
    while (before > 0 && (text[before - 1] === " " || text[before - 1] === "\t")) before--;
    return text.slice(0, before) + text.slice(after);
  }
  return text.slice(0, start) + text.slice(after);
}

// Within a paragraph, cite a source once for a run of sentences, at the last:
// the same citation run repeated with no different citation in between is kept
// only at its last occurrence. Mirror of the server's _collapse_repeated_citations.
function collapseRepeatedCitations(text) {
  const drop = [];
  let previous = null;
  for (const match of text.matchAll(PLACE_RUN_RE)) {
    const start = match.index;
    const end = start + match[0].length;
    const lineStart = text.lastIndexOf("\n", start - 1) + 1;
    if (LIST_PREFIX_RE.test(text.slice(lineStart, start))) {
      previous = null;
      continue;
    }
    const paragraphBreak = text.lastIndexOf("\n\n", start - 1);
    const paragraph = text.slice(paragraphBreak === -1 ? 0 : paragraphBreak + 2, start);
    if (insideParenthetical(paragraph)) continue;
    const links = match[0].match(CITATION_LINK_RE).join("\u0000");
    if (previous && previous.links === links && previous.paragraph === paragraphBreak) {
      drop.push(previous.span);
    }
    previous = { links, paragraph: paragraphBreak, span: [start, end] };
  }
  let out = text;
  for (const [start, end] of drop.reverse()) out = cutCitationRun(out, start, end);
  return out;
}

function placeCitationRuns(text) {
  let out = "";
  let cursor = 0;
  for (const match of text.matchAll(PLACE_RUN_RE)) {
    const start = match.index;
    const end = start + match[0].length;
    const lineStart = text.lastIndexOf("\n", start - 1) + 1;
    // A line that is only a list marker plus citations: a source-list item.
    if (LIST_PREFIX_RE.test(text.slice(lineStart, start))) continue;
    const paragraphBreak = text.lastIndexOf("\n\n", start - 1);
    const paragraph = text.slice(paragraphBreak === -1 ? 0 : paragraphBreak + 2, start);
    // Already inside a parenthetical: leave it, so running twice changes nothing.
    if (insideParenthetical(paragraph)) continue;
    const run = match[0].match(CITATION_LINK_RE).join(" ");
    const head = text.slice(cursor, start);
    const stripped = head.replace(/[ \t]+$/, "");
    let split = stripped.length;
    while (split > 0 && SENTENCE_END.includes(stripped[split - 1])) split--;
    out += `${head.slice(0, split)}（${run}）${stripped.slice(split)}`;
    cursor = end;
  }
  return out + text.slice(cursor);
}

function linkifyCitations(text, urls) {
  if (!urls) return placeCitationRuns(collapseRepeatedCitations(dedupeCitationRuns(text)));
  return placeCitationRuns(collapseRepeatedCitations(dedupeCitationRuns(text.replace(CITATION_RE, (whole, bracketName, cjkName, trailing) => {
    const name = bracketName || cjkName;
    // answer_final is already [[标题]](/transcripts/<URI>#<anchor>). The bare
    // title in the map is the whole episode. Replacing it would drop the section.
    if (trailing?.includes("#")) return whole;
    // The model often drops the `.md` the index keeps on a URI.
    const withMd = (n) => (n.includes("/") && !n.endsWith(".md") ? `${n}.md` : n);
    const hash = name.indexOf("#");
    const base = hash === -1 ? name : name.slice(0, hash);
    const rest = hash === -1 ? "" : name.slice(hash);
    const entry =
      urls[name] ||
      urls[withMd(base) + rest] ||
      (hash === -1 ? null : urls[base] || urls[withMd(base)]);
    // Names we have no entry for are left exactly as written — that is what
    // keeps a genuine 《书名》 in the prose from being turned into a link.
    return entry ? `[[${entry.label || entry.title}]](${entry.url})` : whole;
  }))));
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
    // A section name cut to fit ends in "…"; the heading is the link's anchor
    // (spaces were turned into "-"), so show it in full on hover.
    if (link.textContent.endsWith("…") && link.hash.length > 1) {
      try {
        link.title = decodeURIComponent(link.hash.slice(1)).replaceAll("-", " ");
      } catch {
        /* malformed escape: no tooltip */
      }
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
// here, and the whole set stays above the fold. 换一批 cycles through the rest.
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
let searchStarters = [];
let searchPlaceholderTimer = null;

function renderSampleQuestions() {
  els.grid.replaceChildren();
  const mobile = mobileLayout.matches;
  for (const cat of sampleCategories) {
    const topics = shuffle(cat.topics || []).slice(0, PER_CATEGORY);
    if (!topics.length) continue;

    const group = document.createElement("section");
    group.className = "sample-group";

    // Desktop keeps category names as structure; mobile drops them for density.
    if (!mobile) {
      const label = document.createElement("h2");
      label.className = "sample-label";
      label.textContent = cat.name;
      group.appendChild(label);
    }

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

  // Desktop only: same destination set as the landing desk's footer. Mobile
  // uses the bottom「文稿」tab instead, so this block would be redundant there.
  if (!mobile) {
    const end = document.createElement("div");
    end.className = "latest-end browse-archive";
    const label = document.createElement("p");
    label.className = "latest-end-label";
    label.textContent = "浏览全部文稿";
    end.appendChild(label);
    const chips = document.createElement("div");
    chips.className = "latest-end-chips";
    for (const channel of CHANNEL_ORDER) {
      const chip = document.createElement("a");
      chip.className = "latest-end-chip";
      chip.href = `/transcripts?channel=${encodeURIComponent(channel)}`;
      chip.dataset.route = "";
      chip.textContent = channelLabel(channel);
      chips.appendChild(chip);
    }
    end.appendChild(chips);
    els.grid.appendChild(end);
  }
}

async function loadSampleQuestions() {
  try {
    const res = await fetch("/api/starters");
    const data = await res.json();
    sampleCategories = data.categories || [];
    searchStarters = data.search || [];
    renderSampleQuestions();
    renderSearchStarters();
    startSearchPlaceholderRotation();
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

// Channel bar order is fixed left-to-right (2026-09-21): 睡前消息、参考信息、
// 讲点黑话、高见、产经破壁机. Unknown channels sort after these, alphabetically.
const CHANNEL_ORDER = [
  "ShuiQianXiaoXi",
  "CanKaoXinXi",
  "JiangDianHeiHua",
  "GaoJian",
  "ChanJingPoBiJi",
];

let transcriptItems = null;
let transcriptIndexPromise = null;
let currentArticle = null;
// What the reading pane holds: null (closed) | "archive" | "reader".
let panelLevel = null;
// Channel scope of the list, and of the article on screen while reading.
let currentChannel = null;
let routeGeneration = 0;

// Doc-to-doc hop trail: pushed when an article's own body links to another
// article while the pane is already showing one (a cross-reference jump, not
// a fresh entry from the archive/chat). Lets "back" retrace hops instead of
// always dropping to the channel list. Popped, not replayed — no redo stack.
let docHistory = [];
// Set right before navigate()-ing to a popped docHistory entry, so showReader
// treats that transition as a pop instead of a new hop to push.
let suppressDocHistoryPush = false;

function docBackTarget() {
  return docHistory.length ? docHistory.at(-1) : null;
}

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

// Channels follow the fixed CHANNEL_ORDER; any channel not listed there sorts
// after it alphabetically.
function renderChannelBar(items) {
  const counts = new Map();
  for (const item of items) {
    counts.set(item.channel, (counts.get(item.channel) || 0) + 1);
  }
  const ordered = [...counts.keys()].sort((a, b) => {
    const ia = CHANNEL_ORDER.indexOf(a);
    const ib = CHANNEL_ORDER.indexOf(b);
    if (ia !== -1 && ib !== -1) return ia - ib;
    if (ia !== -1) return -1;
    if (ib !== -1) return 1;
    return a.localeCompare(b);
  });

  const chips = ordered.map((channel) => {
    const chip = document.createElement("a");
    chip.href = `/transcripts?channel=${encodeURIComponent(channel)}`;
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
  // Large in-page channel title (e.g. 睡前消息); the reading bar carries no title.
  els.archiveTitle.hidden = false;
  els.archiveTitle.textContent = channelLabel(currentChannel);
}

function archiveSortKey(item) {
  // ISO dates sort lexicographically; missing dates sink below dated ones.
  const date = item.publication_date || "";
  const hasDate = /^\d{4}-\d{2}-\d{2}/.test(date) ? "1" : "0";
  return `${hasDate}\0${date}\0${item.doc_id || ""}`;
}

function renderArchive(items = transcriptItems || []) {
  const scoped = currentChannel
    ? items.filter((item) => item.channel === currentChannel)
    : [];
  const query = els.archiveSearch.value.trim().toLocaleLowerCase("zh-CN");
  const filtered = query
    ? scoped.filter((item) => itemSearchText(item).includes(query))
    : scoped.slice();
  // Newest publication_date first; undated last. Defensive if the API order changes.
  const visible = filtered.sort((a, b) => {
    const ka = archiveSortKey(a);
    const kb = archiveSortKey(b);
    if (ka < kb) return 1;
    if (ka > kb) return -1;
    return 0;
  });
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
    // Keep the full episode title (often includes 睡前消息N). Do not add a
    // second line that repeats the canonical name.
    label.textContent = item.source_title || item.canonical_title || item.doc_id;
    link.append(label);
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

/* ------------------------------------------------------------------- latest */

// The desk itself is server-rendered into the home page (agent's snapshot-
// keyed cache); only the reader-side anchor helpers below are client code.

// Client-side mirror of indexer/src/anchors.py: the anchor of a topic heading
// is its slug. Newer snapshots ship the ids on the topic h2s; legacy ones do
// not, so the reader body gets the missing ids here and hash links (citations,
// the landing desk's heading chips) jump to the paragraph, not the doc top.
function headingSlug(text) {
  let value = text.trim();
  const mdLink = /\[([^\[\]]*)\]\([^)]*\)/g;
  let previous = null;
  while (previous !== value) {
    previous = value;
    value = value.replace(mdLink, "$1");
  }
  value = value.replace(/\[\^[^\]]*\]/g, "").trim();
  for (const wrapper of ["**", "__", "`"]) {
    const size = wrapper.length;
    if (
      value.length >= size * 2 + 1 &&
      value.startsWith(wrapper) &&
      value.endsWith(wrapper) &&
      !value.slice(size, -size).includes(wrapper)
    ) {
      value = value.slice(size, -size);
    }
  }
  return value.replace(/\s+/g, "-").replace(/^-+|-+$/g, "");
}

// Assign ids to a rendered body's topic h2s where missing. Document-order
// dedupe (-2, -3, ...) mirrors assign_heading_anchors. 正文/附录 carry no
// anchor — they are structure, not topics.
function ensureHeadingAnchors(container) {
  if (!container) return;
  const used = new Set();
  for (const heading of container.querySelectorAll("h2")) {
    const text = heading.textContent.trim();
    if (!text || text === "正文" || text === "附录") continue;
    let id = heading.id;
    if (!id) {
      const base = headingSlug(text);
      if (!base) continue;
      let suffix = 2;
      id = base;
      while (used.has(id)) id = `${base}-${suffix++}`;
      heading.id = id;
    }
    used.add(id);
  }
}

// The landing desk itself (最新文稿) needs no client code: the home page
// server-renders it from the agent's snapshot-keyed cache. showChat() only
// opens the pane over that markup.

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
  // Closed / off-mode panes stay out of the tab order and a11y tree.
  els.readingPane.toggleAttribute("inert", !browse);
  if (mobileLayout.matches) {
    // Mobile: full-screen modes — the inactive pane is fully inert.
    els.chatPane.toggleAttribute("inert", browse);
  } else {
    els.chatPane.toggleAttribute("inert", false);
  }
  if (!browse && els.readingPane.contains(document.activeElement)) {
    els.appShell.focus();
  }
  if (browse && mobileLayout.matches && els.chatPane.contains(document.activeElement)) {
    els.readingPane.focus?.();
  }
  syncChatPanelAccessibility();
  updateMobileTabs();
  keepChatAtBottom(wasAtBottom);
}

function updateMobileTabs() {
  const browse = document.body.dataset.view === "browse";
  if (els.tabChat) {
    els.tabChat.setAttribute("aria-current", browse ? "false" : "page");
  }
  if (els.tabArchive) {
    els.tabArchive.setAttribute("aria-current", browse ? "page" : "false");
  }
}

// Top mode tabs: AI对话 vs 搜索. Pure client state — the server always
// renders the AI tab, so every load and every refresh starts there, and the
// tabs change nothing else: the reading pane (view=browse) stays open and the
// URL keeps pointing at the transcript. The pane swap itself is CSS on
// body[data-mode]; here we only keep the attributes in step (hidden for the
// a11y tree, roving tabindex + aria-selected for tabs).
function setMode(mode) {
  document.body.dataset.mode = mode;
  const search = mode === "search";
  els.searchPane.toggleAttribute("hidden", !search);
  for (const tab of [els.modeTabChat, els.modeTabSearch]) {
    const selected = tab.dataset.mode === mode;
    tab.setAttribute("aria-selected", selected ? "true" : "false");
    tab.tabIndex = selected ? 0 : -1;
  }
  syncChatPanelAccessibility();
}

/* ------------------------------------------------------------------ search */

// 搜索 state. Results stay in the pane when the user flips back to
// AI对话; a new search replaces them. The cursor returned by the backend is
// bound to the analyzed terms and page size, so it is only used verbatim.
let searchState = null;
let searchSeq = 0;

function searchSnippetNode(uri, snippet) {
  const row = document.createElement("a");
  row.className = "search-snippet";
  row.href =
    transcriptPath(uri) +
    (snippet.heading_anchor ? `#${encodeURIComponent(snippet.heading_anchor)}` : "");
  row.dataset.route = "";
  if (snippet.heading) {
    const heading = document.createElement("span");
    heading.className = "search-snippet-heading";
    heading.textContent = snippet.heading;
    row.appendChild(heading);
  }
  const text = document.createElement("p");
  text.className = "search-snippet-text";
  for (const part of snippet.parts) {
    const span = document.createElement("span");
    if (part.highlight) span.className = "search-hit";
    span.textContent = part.text;
    text.appendChild(span);
  }
  row.appendChild(text);
  return row;
}

function searchResultNode(result) {
  const li = document.createElement("article");
  li.className = "search-result";
  const head = document.createElement("a");
  head.className = "search-result-link";
  head.href = transcriptPath(result.doc_id);
  head.dataset.route = "";
  const title = document.createElement("span");
  title.className = "search-result-title";
  title.textContent = result.source_title || result.title || result.doc_id;
  head.appendChild(title);
  if (result.publication_date) {
    const meta = document.createElement("span");
    meta.className = "search-result-meta";
    meta.textContent = result.publication_date;
    head.appendChild(meta);
  }
  li.appendChild(head);
  for (const snippet of result.snippets) {
    li.appendChild(searchSnippetNode(result.doc_id, snippet));
  }
  return li;
}

function renderSearchPage(data, { append }) {
  const more = els.searchResults.querySelector(".search-more");
  more?.remove();
  if (append) {
    els.searchResults.append(
      ...data.results.map((result) => searchResultNode(result)),
    );
  } else {
    els.searchResults.replaceChildren(
      ...data.results.map((result) => searchResultNode(result)),
    );
  }
  if (data.next_cursor) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "search-more";
    button.textContent = "加载更多";
    button.addEventListener("click", () => void loadMoreSearch());
    els.searchResults.appendChild(button);
  }
}

function setSearchStatus(text, { busy = false } = {}) {
  els.searchStatus.textContent = text;
  els.searchStatus.classList.toggle("is-busy", busy);
}

async function fetchSearchPage(query, cursor) {
  const params = new URLSearchParams({ q: query, limit: "20" });
  if (cursor) params.set("cursor", cursor);
  const response = await fetch(`/api/search?${params}`);
  if (!response.ok) {
    // 410: the paged snapshot was retired mid-flip; 422: the query itself
    // holds no searchable words; everything else reads as "service down".
    if (response.status === 410) throw new Error("stale");
    if (response.status === 422) throw new Error("query");
    throw new Error("service");
  }
  return response.json();
}

// The 搜索 tab has its own address: /search, and /search?q=… once a query
// has run. On desktop the address otherwise belongs to the reading pane, so
// this is the path to restore when the reader returns to AI对话.
let readingRoutePath = "/";

function searchPath(query) {
  return query ? `/search?q=${encodeURIComponent(query)}` : "/search";
}

function isSearchPath(pathname) {
  return pathname === "/search" || pathname === "/search/";
}

// Records the search in the address bar without re-rendering the route:
// whatever the reading pane shows stays put. While a transcript is open beside
// it (desktop), the address keeps pointing at that transcript instead.
function pushSearchUrl(query) {
  if (!mobileLayout.matches && panelLevel === "reader") return;
  const next = searchPath(query);
  const current = `${location.pathname}${location.search}`;
  if (next === current) return;
  history.pushState({}, "", next);
  document.title = query
    ? `${query} - 搜索 · 睡前消息知识库`
    : "搜索 · 睡前消息知识库";
}

// A submitted query: from the form, a starter chip, or the address bar.
function submitSearch(query) {
  els.searchInput.value = query;
  pushSearchUrl(query);
  void runSearch(query);
}

function resetSearch() {
  searchSeq++;
  searchState = null;
  els.searchPane.classList.add("is-idle");
  els.searchResults.classList.remove("is-busy");
  els.searchResults.replaceChildren();
  els.searchInput.value = "";
  setSearchStatus("");
  els.searchGo.disabled = false;
}

async function runSearch(query) {
  const seq = ++searchSeq;
  // First search docks the box to the top row so results read under it.
  els.searchPane.classList.remove("is-idle");
  searchState = { query, cursor: null, total: 0, busy: true };
  // The previous results stay visible (dimmed, inert) until the new page
  // arrives — editing the query and re-searching never blanks the pane.
  els.searchResults.classList.add("is-busy");
  setSearchStatus("正在搜索…", { busy: true });
  els.searchGo.disabled = true;
  try {
    const data = await fetchSearchPage(query, null);
    if (seq !== searchSeq) return;
    searchState = {
      query,
      cursor: data.next_cursor,
      total: data.total,
      busy: false,
    };
    els.searchResults.classList.remove("is-busy");
    renderSearchPage(data, { append: false });
    els.searchResults.scrollTop = 0;
    setSearchStatus(
      data.total > 0 ? `找到 ${data.total} 篇相关文稿` : "没有匹配的文稿，换个说法试试。",
    );
  } catch (error) {
    if (seq !== searchSeq) return;
    searchState.busy = false;
    els.searchResults.classList.remove("is-busy");
    els.searchResults.replaceChildren();
    if (error.message === "stale") setSearchStatus("搜索已过期，请重新搜索。");
    else if (error.message === "query")
      setSearchStatus("请输入有效的搜索词，例如：贵州 债务。");
    else setSearchStatus("搜索服务暂时不可用，请稍后重试。");
  } finally {
    if (seq === searchSeq) els.searchGo.disabled = false;
  }
}

async function loadMoreSearch() {
  if (!searchState?.cursor || searchState.busy) return;
  const seq = searchSeq;
  searchState.busy = true;
  setSearchStatus("正在加载更多…", { busy: true });
  els.searchGo.disabled = true;
  try {
    const data = await fetchSearchPage(searchState.query, searchState.cursor);
    if (seq !== searchSeq) return;
    searchState.cursor = data.next_cursor;
    searchState.total = data.total;
    renderSearchPage(data, { append: true });
    setSearchStatus(`找到 ${searchState.total} 篇相关文稿`);
  } catch (error) {
    if (seq !== searchSeq) return;
    if (error.message === "stale") setSearchStatus("搜索已过期，请重新搜索。");
    else setSearchStatus("加载失败，请稍后重试。");
  } finally {
    if (seq === searchSeq) {
      searchState.busy = false;
      els.searchGo.disabled = false;
    }
  }
}

// The input placeholder rotates through the whole starter library every 5s;
// the chips below the box are a random subset of six per page load. With an
// empty input, submitting searches the starter currently on display.
function currentSearchPlaceholder() {
  return els.searchInput.getAttribute("placeholder") || "";
}

function startSearchPlaceholderRotation() {
  if (!searchStarters.length || searchPlaceholderTimer) return;
  const rotate = () => {
    let next = searchStarters[Math.floor(Math.random() * searchStarters.length)];
    while (searchStarters.length > 1 && next === currentSearchPlaceholder()) {
      next = searchStarters[Math.floor(Math.random() * searchStarters.length)];
    }
    els.searchInput.placeholder = next;
  };
  rotate();
  searchPlaceholderTimer = setInterval(rotate, 5000);
}

function renderSearchStarters() {
  const holder = document.querySelector(".search-starters");
  if (!holder || !searchStarters.length) return;
  const pool = searchStarters.slice();
  const picked = [];
  while (picked.length < 10 && pool.length) {
    picked.push(pool.splice(Math.floor(Math.random() * pool.length), 1)[0]);
  }
  holder.replaceChildren(
    ...picked.map((query) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "search-starter";
      chip.textContent = query;
      chip.addEventListener("click", () => {
        submitSearch(query);
        els.searchInput.focus();
      });
      return chip;
    }),
  );
}

function preferredArchiveChannel() {
  try {
    const saved = sessionStorage.getItem("archive-channel");
    if (saved) return saved;
  } catch {
    /* ignore */
  }
  if (currentChannel) return currentChannel;
  if (currentArticle?.channel) return currentArticle.channel;
  if (transcriptItems?.length) {
    const counts = new Map();
    for (const item of transcriptItems) {
      counts.set(item.channel, (counts.get(item.channel) || 0) + 1);
    }
    return [...counts.keys()].sort(
      (a, b) => counts.get(b) - counts.get(a) || a.localeCompare(b),
    )[0];
  }
  return null;
}

async function openArchiveTab() {
  const channel = preferredArchiveChannel();
  if (channel) {
    try {
      sessionStorage.setItem("archive-channel", channel);
    } catch {
      /* ignore */
    }
    navigate(`/transcripts?channel=${encodeURIComponent(channel)}`);
    return;
  }
  try {
    await loadTranscriptIndex();
  } catch {
    /* fall through */
  }
  const fallback = preferredArchiveChannel();
  if (fallback) {
    navigate(`/transcripts?channel=${encodeURIComponent(fallback)}`);
  }
}

function setPanelLevel(level) {
  panelLevel = level;
  // Mirrored on <body> for CSS: mobile hides the reading bar entirely on the
  // archive level (the bottom tabs are the way back) and only shows it —
  // back + close — inside an article.
  document.body.dataset.panel = level ?? "";
  els.archiveView.hidden = level !== "archive";
  els.readerView.hidden = level !== "reader";
  els.latestView.hidden = level !== "latest";
  updateReadingBar();
  void updateShareButton();
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

const TRANSCRIPTS_EDIT_BASE =
  "https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts/edit/main/contents/";

function githubEditUrl(docId) {
  if (!docId) return null;
  // doc_id is already path-like: ShuiQianXiaoXi/1001-1100/1075.md
  return (
    TRANSCRIPTS_EDIT_BASE +
    String(docId)
      .split("/")
      .map(encodeURIComponent)
      .join("/")
  );
}

// Mirrors _short_id_for() / _base58_encode() in server.py exactly: same
// alphabet, same byte count, same left-pad. Computed client-side (rather
// than read off the article payload) so it never depends on a network
// round trip beyond the article fetch the reader already makes.
const BASE58_ALPHABET =
  "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
const SHORT_ID_LENGTH = 8;

function base58Encode(bytes) {
  let n = 0n;
  for (const byte of bytes) n = (n << 8n) | BigInt(byte);
  if (n === 0n) return BASE58_ALPHABET[0];
  let out = "";
  while (n > 0n) {
    const remainder = Number(n % 58n);
    out = BASE58_ALPHABET[remainder] + out;
    n /= 58n;
  }
  return out;
}

// Web Crypto is missing on HTTP LAN (a phone hitting the machine IP). This
// fallback is FIPS 180-4 SHA-256 and must match crypto.subtle.digest.
function sha256Bytes(message) {
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));
  const ml = message.length;
  const paddedLen = Math.ceil((ml + 9) / 64) * 64;
  const m = new Uint8Array(paddedLen);
  m.set(message);
  m[ml] = 0x80;
  const view = new DataView(m.buffer);
  view.setUint32(paddedLen - 4, (ml * 8) >>> 0);
  let h0 = 0x6a09e667;
  let h1 = 0xbb67ae85;
  let h2 = 0x3c6ef372;
  let h3 = 0xa54ff53a;
  let h4 = 0x510e527f;
  let h5 = 0x9b05688c;
  let h6 = 0x1f83d9ab;
  let h7 = 0x5be0cd19;
  const w = new Uint32Array(64);
  for (let i = 0; i < paddedLen; i += 64) {
    for (let t = 0; t < 16; t++) w[t] = view.getUint32(i + t * 4);
    for (let t = 16; t < 64; t++) {
      const s0 = rotr(w[t - 15], 7) ^ rotr(w[t - 15], 18) ^ (w[t - 15] >>> 3);
      const s1 = rotr(w[t - 2], 17) ^ rotr(w[t - 2], 19) ^ (w[t - 2] >>> 10);
      w[t] = (w[t - 16] + s0 + w[t - 7] + s1) >>> 0;
    }
    let a = h0;
    let b = h1;
    let c = h2;
    let d = h3;
    let e = h4;
    let f = h5;
    let g = h6;
    let h = h7;
    for (let t = 0; t < 64; t++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + S1 + ch + K[t] + w[t]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (S0 + maj) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
    h5 = (h5 + f) >>> 0;
    h6 = (h6 + g) >>> 0;
    h7 = (h7 + h) >>> 0;
  }
  const out = new Uint8Array(32);
  const ov = new DataView(out.buffer);
  ov.setUint32(0, h0);
  ov.setUint32(4, h1);
  ov.setUint32(8, h2);
  ov.setUint32(12, h3);
  ov.setUint32(16, h4);
  ov.setUint32(20, h5);
  ov.setUint32(24, h6);
  ov.setUint32(28, h7);
  return out;
}

async function sha256Prefix6(text) {
  const encoded = new TextEncoder().encode(text);
  if (globalThis.crypto?.subtle) {
    const digestBuffer = await crypto.subtle.digest("SHA-256", encoded);
    return new Uint8Array(digestBuffer).slice(0, 6);
  }
  return sha256Bytes(encoded).slice(0, 6);
}

async function shortIdFor(docId) {
  const code = base58Encode(await sha256Prefix6(docId));
  const padded = BASE58_ALPHABET[0].repeat(SHORT_ID_LENGTH) + code;
  return padded.slice(-SHORT_ID_LENGTH);
}

async function shareTextFor(article) {
  const title = article.source_title || article.canonical_title || "";
  const shortId = await shortIdFor(article.doc_id);
  return `${title} ${location.origin}/s/${shortId}`;
}

let shareToastTimer = null;

function showShareToast(message) {
  if (!els.shareToast) return;
  els.shareToast.textContent = message;
  els.shareToast.hidden = false;
  // Reflow so re-triggering the class while it is still visible restarts
  // the fade rather than being a no-op.
  els.shareToast.classList.remove("is-visible");
  els.shareToast.getBoundingClientRect();
  els.shareToast.classList.add("is-visible");
  if (shareToastTimer) clearTimeout(shareToastTimer);
  shareToastTimer = setTimeout(() => {
    els.shareToast.classList.remove("is-visible");
    shareToastTimer = setTimeout(() => {
      els.shareToast.hidden = true;
    }, 200);
  }, 1600);
}

function closeShareSheet() {
  if (els.shareSheet?.open) els.shareSheet.close();
  els.readingShare?.setAttribute("aria-expanded", "false");
}

async function ensureShareText() {
  if (els.readingShare?.dataset.shareText) return els.readingShare.dataset.shareText;
  if (!currentArticle?.doc_id) return "";
  const article = currentArticle;
  const text = await shareTextFor(article);
  if (currentArticle !== article || !els.readingShare) return "";
  els.readingShare.dataset.shareText = text;
  return text;
}

async function openShareSheet() {
  if (!els.shareSheet || els.shareSheet.open) return;
  if (!(await ensureShareText())) return;
  els.shareSheet.showModal();
  els.readingShare.setAttribute("aria-expanded", "true");
}

function hideShareButton() {
  if (!els.readingShare) return;
  closeShareSheet();
  els.readingShare.hidden = false;
  els.readingShare.classList.add("is-slot-hidden");
  els.readingShare.setAttribute("aria-hidden", "true");
  els.readingShare.setAttribute("aria-expanded", "false");
  els.readingShare.tabIndex = -1;
  delete els.readingShare.dataset.shareText;
}

function revealShareButton() {
  if (!els.readingShare) return;
  els.readingShare.hidden = false;
  els.readingShare.classList.remove("is-slot-hidden");
  els.readingShare.removeAttribute("aria-hidden");
  els.readingShare.tabIndex = 0;
}

async function updateShareButton() {
  if (!els.readingShare) return;
  if (panelLevel !== "reader" || !currentArticle?.doc_id) {
    hideShareButton();
    return;
  }
  const article = currentArticle;
  revealShareButton();
  try {
    const text = await shareTextFor(article);
    // The reader may have navigated away while the hash was computing.
    if (currentArticle !== article) return;
    els.readingShare.dataset.shareText = text;
  } catch (error) {
    console.error("Share text failed.", error);
  }
}

// Fallback for browsers (notably in-app webviews) where the Clipboard API is
// missing or rejects the write. execCommand is deprecated but still the only
// option there, so it stays.
function copyTextFallback(text) {
  const input = document.createElement("textarea");
  input.value = text;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.left = "-9999px";
  document.body.appendChild(input);
  input.select();
  // Called through a narrow type: lib.dom marks execCommand deprecated, which is
  // known and accepted here (see the comment above), and the flag would otherwise
  // sit on this line forever.
  const legacyDocument = /** @type {{ execCommand: (command: string) => boolean }} */ (
    document
  );
  const ok = legacyDocument.execCommand("copy");
  input.remove();
  if (!ok) throw new Error("execCommand copy failed");
}

// writeText is invoked before the first await so a click's user-activation
// still covers it. A rejected Clipboard API falls through to the textarea path.
async function writeClipboard(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (error) {
      console.error("Clipboard write failed; trying the fallback.", error);
    }
  }
  copyTextFallback(text);
}

async function copyShareLink() {
  const text = await ensureShareText();
  if (!text) return;
  closeShareSheet();
  try {
    await writeClipboard(text);
    showShareToast("已复制");
  } catch (error) {
    console.error("Clipboard write failed.", error);
    showShareToast("复制失败，请手动复制");
  }
}

function markdownFilename(article) {
  const title = article.source_title || article.canonical_title || "transcript";
  const stem = title.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "").trim() || "transcript";
  return stem.toLowerCase().endsWith(".md") ? stem : `${stem}.md`;
}

function downloadMarkdown() {
  const docId = currentArticle?.doc_id;
  if (!docId) {
    showShareToast("文稿源暂不可用");
    return;
  }
  closeShareSheet();
  const path = `/api/transcripts/${docId.split("/").map(encodeURIComponent).join("/")}?download=markdown`;
  const link = document.createElement("a");
  link.href = path;
  link.download = markdownFilename(currentArticle);
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function updateReadingBar() {
  // Titles live in the pane (archive H1 / reader H1). The bar keeps fixed
  // slots so its border does not jump when back/edit appear.
  const canGoBack = Boolean(readingBackTarget());
  els.readingBack.hidden = false;
  els.readingBack.classList.toggle("is-slot-hidden", !canGoBack);
  els.readingBack.tabIndex = canGoBack ? 0 : -1;
  els.readingBack.setAttribute("aria-hidden", canGoBack ? "false" : "true");

  // The close button only closes the desk from the archive/reader levels; on
  // the landing desk it would just re-render the latest list, so it stays out.
  const canClose = panelLevel !== "latest";
  els.readingClose.classList.toggle("is-slot-hidden", !canClose);
  els.readingClose.tabIndex = canClose ? 0 : -1;
  els.readingClose.setAttribute("aria-hidden", canClose ? "false" : "true");

  const docBackTargetUri = panelLevel === "reader" ? docBackTarget() : null;
  if (els.readingDocBack) {
    els.readingDocBack.hidden = false;
    els.readingDocBack.classList.toggle("is-slot-hidden", !docBackTargetUri);
    els.readingDocBack.tabIndex = docBackTargetUri ? 0 : -1;
    els.readingDocBack.setAttribute("aria-hidden", docBackTargetUri ? "false" : "true");
  }

  const editUrl =
    panelLevel === "reader" ? githubEditUrl(currentArticle?.doc_id) : null;
  if (els.readingEdit) {
    els.readingEdit.hidden = false;
    if (editUrl) {
      els.readingEdit.classList.remove("is-slot-hidden");
      els.readingEdit.href = editUrl;
      els.readingEdit.removeAttribute("aria-hidden");
      els.readingEdit.tabIndex = 0;
    } else {
      els.readingEdit.classList.add("is-slot-hidden");
      els.readingEdit.removeAttribute("href");
      els.readingEdit.setAttribute("aria-hidden", "true");
      els.readingEdit.tabIndex = -1;
    }
  }
}

async function showArchive(generation, channel) {
  currentArticle = null;
  currentChannel = channel;
  try {
    sessionStorage.setItem("archive-channel", channel);
  } catch {
    /* ignore */
  }
  setView("browse");
  setPanelLevel("archive");
  renderArchiveHead();
  els.readingScroll.scrollTo({ top: 0 });
  document.title = `${channelLabel(channel)}文稿 · 睡前消息知识库`;
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
  const preservesServerRender =
    document.body.dataset.ssrDocId === uri && Boolean(els.readerBody.innerHTML.trim());
  currentArticle = null;
  if (!preservesServerRender) {
    els.readerState.textContent = "正在接收文稿…";
    els.readerTitle.textContent = "";
    els.readerBody.replaceChildren();
  } else {
    els.readerState.textContent = "";
  }
  if (els.readingEdit) {
    els.readingEdit.hidden = false;
    els.readingEdit.classList.add("is-slot-hidden");
    els.readingEdit.removeAttribute("href");
    els.readingEdit.setAttribute("aria-hidden", "true");
    els.readingEdit.tabIndex = -1;
  }
  hideShareButton();
  // Re-trigger the page-in. The element is reused between articles, so the
  // animation has to be dropped and reflowed back on to run again.
  els.readerView.classList.remove("is-entering");
  els.readerView.getBoundingClientRect();
  els.readerView.classList.add("is-entering");
}

function updateDocHistory(uri) {
  const previousLevel = panelLevel;
  const previousDocId = currentArticle?.doc_id;
  if (suppressDocHistoryPush) {
    suppressDocHistoryPush = false;
  } else if (previousLevel === "reader" && previousDocId && previousDocId !== uri) {
    // Already reading something else: this is a cross-reference hop, not a
    // fresh entry — remember where we came from.
    docHistory.push(previousDocId);
  } else if (previousLevel !== "reader") {
    // Entering the reader from the archive/chat starts a new trail.
    docHistory = [];
  }
}

function prepareTranscriptLinks(body) {
  for (const link of body.querySelectorAll("a")) {
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
}

async function showReader(uri, generation) {
  updateDocHistory(uri);
  currentChannel = null;
  setView("browse");
  setPanelLevel("reader");
  prepareReader(uri);
  els.readingScroll.scrollTo({ top: 0 });
  // SSR already placed body_html. Scroll before the refresh fetch returns.
  if (window.location.hash && document.body.dataset.ssrDocId === uri) {
    scrollToLocationHash();
  }
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
    void updateShareButton();
    updateChannelBar();
    // Same rule as the server's _display_title, so the tab title doesn't change on hydration.
    const displayTitle =
      article.source_title || article.canonical_title || article.doc_id || "文稿";
    document.title = `${displayTitle} · 睡前消息知识库`;
    els.readerTitle.textContent = displayTitle;
    els.readerBody.innerHTML = article.body_html;
    // Legacy snapshots ship h2s without ids; assign the shared slugs so hash
    // links (citations, the landing desk's heading chips) reach the paragraph.
    ensureHeadingAnchors(els.readerBody);
    delete document.body.dataset.ssrDocId;
    prepareTranscriptLinks(els.readerBody);
    els.readerState.textContent = "";
    scrollToLocationHash();
  } catch (error) {
    if (generation !== routeGeneration) return;
    console.error("Transcript unavailable.", error);
    els.readerState.textContent = "这篇文稿暂时不可用，或已从上游删除。";
    document.title = "文稿不可用 · 睡前消息知识库";
  }
}

/* -------------------------------------------------------------- the router */

// The landing surface: the conversation centred, no channel chosen. Also the
// destination for a route that has nothing to show. Desktop keeps the reading
// desk open beside it with the newest transcripts, so the pane never sits
// empty; mobile stays a full-screen chat (bottom tabs switch the modes).
function showChat() {
  currentArticle = null;
  currentChannel = null;
  if (mobileLayout.matches) {
    setView("chat");
    setPanelLevel(null);
  } else {
    setView("browse");
    // The desk markup is already in the page (server-rendered); opening the
    // pane over it needs no fetch.
    setPanelLevel("latest");
  }
  document.title = "睡前消息知识库";
}

// /latest: the desk on its own address. Desktop looks the same as the home
// page; mobile opens the list full-screen instead of the chat.
function showLatest() {
  currentArticle = null;
  currentChannel = null;
  setView("browse");
  setPanelLevel("latest");
  document.title = "最新文稿 · 睡前消息知识库";
}

async function renderRoute() {
  const generation = ++routeGeneration;
  const { pathname, search } = window.location;
  const channel = new URLSearchParams(search).get("channel") || null;

  if (isSearchPath(pathname)) {
    showSearchRoute(new URLSearchParams(search).get("q")?.trim() ?? "");
    return;
  }
  readingRoutePath = `${pathname}${search}`;

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
      // Same transcript, hash only: the article is already in the pane.
      if (
        currentArticle?.doc_id === uri &&
        panelLevel === "reader" &&
        els.readerBody?.innerHTML.trim()
      ) {
        scrollToLocationHash();
        return;
      }
      await showReader(uri, generation);
      return;
    }
    history.replaceState({}, "", "/");
    showChat();
    return;
  }

  if (pathname === "/latest" || pathname === "/latest/") {
    setMode("chat");
    showLatest();
    return;
  }

  if (pathname !== "/" && pathname !== "") {
    history.replaceState({}, "", "/");
    readingRoutePath = "/";
  }
  setMode("chat");
  showChat();
}

// /search and /search?q=…: the 搜索 tab, running the query in the address
// unless it is already the one on screen (back/forward between searches).
function showSearchRoute(query) {
  // Mobile shows the search pane full-screen (the left half); desktop keeps
  // whatever the reading pane already has, or opens the desk on first load.
  if (mobileLayout.matches) {
    setView("chat");
    setPanelLevel(null);
  } else if (!panelLevel) {
    setView("browse");
    setPanelLevel("latest");
  }
  setMode("search");
  document.title = query ? `${query} - 搜索 · 睡前消息知识库` : "搜索 · 睡前消息知识库";
  if (!query) {
    if (searchState) resetSearch();
    return;
  }
  if (searchState?.query === query) return;
  els.searchInput.value = query;
  void runSearch(query);
}

// Every transcript link in the page — channel chips, archive rows, citations in
// an answer, links inside an article — lands here. When the pane is open the
// article is swapped in place and the conversation is left exactly as it was,
// which is what makes the pane feel like a window rather than a navigation.
function navigate(href) {
  const url = new URL(href, window.location.origin);
  if (url.origin !== window.location.origin) return;
  const nextPath = `${url.pathname}${url.search}`;
  const currentPath = `${window.location.pathname}${window.location.search}`;
  const next = `${nextPath}${url.hash}`;
  const current = `${currentPath}${window.location.hash}`;
  // Same link again, after the reader has scrolled away: the hash did not
  // change, so jump back to the section without a new history entry.
  if (next === current) {
    if (url.hash && nextPath === currentPath) scrollToLocationHash();
    return;
  }
  history.pushState({}, "", next);
  // A hash-only change must not refetch. The reading pane scrolls to the id.
  if (nextPath === currentPath) {
    scrollToLocationHash();
    return;
  }
  void renderRoute();
}

/* --------------------------------------------------------- chat pane state */

function syncChatPanelAccessibility() {
  // Mobile uses full-screen modes (tabs), not a side drawer. 搜索 is one
  // of those modes: whichever of the two left panes is showing owns focus.
  if (mobileLayout.matches) {
    const browse = document.body.dataset.view === "browse";
    const searching =
      document.body.dataset.mode === "search" && !browse;
    els.chatPane.toggleAttribute("inert", browse || searching);
    els.chatPane.toggleAttribute("aria-hidden", browse || searching);
    els.searchPane.toggleAttribute("inert", browse || !searching);
    return;
  }
  // Desktop never hides chat; the search pane toggles by display.
  els.chatPane.toggleAttribute("inert", false);
  els.chatPane.removeAttribute("aria-hidden");
  els.searchPane.toggleAttribute("inert", false);
}

document.getElementById("masthead-latest").addEventListener("click", (event) => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  navigate("/latest");
  // Clicking again on the same route still returns the list to its start.
  els.readingScroll.scrollTo({ top: 0 });
});

els.archiveSearch.addEventListener("input", () => renderArchive());
els.channelBar.addEventListener("click", (event) => {
  const chip = event.target.closest(".channel-chip");
  if (!chip) return;
  event.preventDefault();
  const channel = chip.dataset.channel;
  try {
    sessionStorage.setItem("archive-channel", channel);
  } catch {
    /* ignore */
  }
  navigate(`/transcripts?channel=${encodeURIComponent(channel)}`);
});
// Close steps back one level: an article returns to its channel's list, a
// channel list returns to the landing desk.
els.readingClose.addEventListener("click", () => navigate(readingBackTarget() ?? "/"));
els.readingShare?.addEventListener("click", () => {
  if (els.shareSheet?.open) closeShareSheet();
  else void openShareSheet();
});
els.shareCopy?.addEventListener("click", () => {
  void copyShareLink();
});
els.shareDownload?.addEventListener("click", () => {
  downloadMarkdown();
});
els.shareSheet?.addEventListener("click", (event) => {
  if (event.target === els.shareSheet) closeShareSheet();
});
els.shareSheet?.addEventListener("close", () => {
  els.readingShare?.setAttribute("aria-expanded", "false");
});

els.tabChat?.addEventListener("click", (event) => {
  event.preventDefault();
  navigate("/");
});
els.tabArchive?.addEventListener("click", (event) => {
  event.preventDefault();
  void openArchiveTab();
});
mobileLayout.addEventListener("change", () => {
  syncChatPanelAccessibility();
  updateMobileTabs();
  // Category labels vs flat list depends on viewport.
  if (sampleCategories.length) renderSampleQuestions();
});

// Keep the composer above the mobile keyboard when the visual viewport shrinks.
function syncComposerToKeyboard() {
  if (!window.visualViewport) return;
  const vv = window.visualViewport;
  const inset = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
  document.documentElement.style.setProperty(
    "--keyboard-inset",
    `${inset}px`,
  );
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", syncComposerToKeyboard);
  window.visualViewport.addEventListener("scroll", syncComposerToKeyboard);
  syncComposerToKeyboard();
}

// Fragment links (footnote ref <-> appendix) must move only #reading-scroll.
// scrollIntoView() and bare hash navigation also scroll ancestor/viewport
// containers; with a 100dvh locked shell that shoves the whole desk upward and
// leaves a blank band of page background.
// The hash never reaches the server. After body_html is in the pane, scroll
// that pane — not the 100dvh page shell — to the decoded id. A missing id or
// an empty hash stays at the top.
function readingPaneIsSettled() {
  const pane = els.readingPane;
  if (!pane || document.body.dataset.view !== "browse") return false;
  if (typeof pane.getAnimations !== "function") return true;
  // flex-basis is still sliding from 0% to half the desk. Measuring a heading
  // during that slide uses the narrow column, then the wider reflow leaves
  // the scroll position past the heading.
  return !pane.getAnimations().some((anim) => {
    if (anim.playState !== "running") return false;
    const prop = anim.transitionProperty;
    return !prop || prop === "flex-basis" || prop === "border-left-width";
  });
}

function whenReadingPaneSettled(callback) {
  if (readingPaneIsSettled()) {
    callback();
    return;
  }
  const pane = els.readingPane;
  if (!pane) return;
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    pane.removeEventListener("transitionend", onEnd);
    requestAnimationFrame(() => requestAnimationFrame(callback));
  };
  const onEnd = (event) => {
    if (event.target !== pane || event.propertyName !== "flex-basis") return;
    finish();
  };
  pane.addEventListener("transitionend", onEnd);
  setTimeout(finish, PANE_SLIDE_MS + 40);
}

function scrollToLocationHash() {
  const scroller = els.readingScroll;
  if (!scroller) return;
  const raw = window.location.hash;
  if (!raw || raw === "#") {
    scroller.scrollTo({ top: 0 });
    return;
  }
  let id = raw.slice(1);
  try {
    id = decodeURIComponent(id);
  } catch {
    /* keep the raw fragment */
  }
  // Resolve the node after the pane settles. The element found before the
  // wait can be replaced when the fetched article HTML arrives.
  whenReadingPaneSettled(() => {
    const target =
      els.readerBody?.querySelector(`#${CSS.escape(id)}`) ||
      scroller.querySelector(`#${CSS.escape(id)}`);
    if (!target) {
      scroller.scrollTo({ top: 0 });
      return;
    }
    scrollReadingPaneTo(target);
  });
}

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
els.readingDocBack?.addEventListener("click", () => {
  const target = docBackTarget();
  if (!target) return;
  docHistory.pop();
  suppressDocHistoryPush = true;
  navigate(transcriptPath(target));
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  // Close the share sheet ourselves so aria-expanded stays in sync, and so
  // Escape does not also fall through to leaving the article.
  if (els.shareSheet?.open) {
    event.preventDefault();
    closeShareSheet();
    return;
  }
  // Escape backs out one level, same as the close button: article to its
  // channel's list, list to the landing desk.
  if (panelLevel) navigate(readingBackTarget() ?? "/");
});
document.addEventListener("click", (event) => {
  if (event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest('a[data-route], a[href^="/transcripts/"]');
  if (!link) return;
  event.preventDefault();
  navigate(link.href);
});

if ("scrollRestoration" in history) history.scrollRestoration = "manual";
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

  const answerCopy = turn.querySelector(".answer-copy");
  answerCopy.addEventListener("click", () => {
    void copyAnswerMarkdown(answerCopy);
  });

  return {
    turn,
    signal,
    statusEl: turn.querySelector(".signal-status"),
    stages: turn.querySelectorAll(".stage-item"),
    answer: turn.querySelector(".answer"),
    answerBody: turn.querySelector(".answer-body"),
    answerActions: turn.querySelector(".answer-actions"),
    answerCopy,
    counts: { retrieved: null, relevant: null },
  };
}

// The retrieve and grade steps carry the only numbers that say anything about
// how well grounded an answer is. Pull them out so the collapsed signal can keep
// showing them after the trace itself is folded away.
function captureCounts(ctx, stepType, content) {
  if (stepType === "retrieve") {
    // "Retrieved 90 chunks (30 unique), kept top 15 by similarity." — the kept
    // count is what grading actually sees; the raw total counts duplicates.
    const m = content.match(/kept top (\d+)/i) || content.match(/(\d+)/);
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

// The string the renderer parsed: citation links repaired, follow-up block
// removed, list markers made valid. That is the Markdown of this one answer.
// The question and the suggested follow-ups are not part of it.
function answerMarkdownSource(state) {
  const text = stripFollowupBlock(state.finalText || state.answerText).trim();
  if (!text) return "";
  return absolutizeSiteLinks(
    normalizeMarkdown(linkifyCitations(text, state.citationUrls)),
  ).trim();
}

// A citation link is a path on this site (`/transcripts/...`), which works in the
// page but means nothing once the Markdown is pasted elsewhere. Only the copied
// text gets the origin; the rendered answer keeps relative links so they navigate
// inside the app.
function absolutizeSiteLinks(markdown) {
  const origin = publicBaseUrl();
  return markdown.replace(
    /\]\((\/(?!\/)[^)\s]*)\)/g,
    (_match, path) => `](${origin}${path})`,
  );
}

// The site's public origin, from the server's PUBLIC_BASE_URL (the one place the
// domain is set; the page carries it in a meta tag). Falls back to the page's own
// origin if the tag is missing or was never filled in.
function publicBaseUrl() {
  const configured = document
    .querySelector('meta[name="public-base-url"]')
    ?.getAttribute("content");
  if (configured && !configured.includes("__")) return configured.replace(/\/+$/, "");
  return window.location.origin;
}

function publishAnswerCopy(state) {
  const markdown = answerMarkdownSource(state);
  state.ctx.answerCopy.markdown = markdown;
  state.ctx.answerActions.hidden = !markdown;
}

let copiedAnswerButton = null;
let copiedAnswerTimer = 0;

function restoreAnswerCopyButton(button) {
  if (!button) return;
  button.classList.remove("is-copied", "is-failed");
  const label = button.querySelector(".answer-copy-label");
  if (label) label.textContent = "复制";
  button.setAttribute("aria-label", "复制 Markdown");
}

function flashAnswerCopy(button, ok) {
  if (copiedAnswerButton && copiedAnswerButton !== button) {
    restoreAnswerCopyButton(copiedAnswerButton);
  }
  if (copiedAnswerTimer) clearTimeout(copiedAnswerTimer);
  copiedAnswerButton = button;
  button.classList.toggle("is-copied", ok);
  button.classList.toggle("is-failed", !ok);
  const label = button.querySelector(".answer-copy-label");
  if (label) label.textContent = ok ? "已复制" : "复制失败";
  button.setAttribute("aria-label", ok ? "已复制 Markdown" : "复制失败");
  const status = button.parentElement?.querySelector(".answer-copy-status");
  if (status) status.textContent = ok ? "已复制" : "复制失败";
  copiedAnswerTimer = setTimeout(() => {
    restoreAnswerCopyButton(button);
    if (copiedAnswerButton === button) copiedAnswerButton = null;
    copiedAnswerTimer = 0;
  }, 1600);
}

async function copyAnswerMarkdown(button) {
  const text = button.markdown;
  if (!text) return;
  try {
    await writeClipboard(text);
    flashAnswerCopy(button, true);
  } catch (error) {
    console.error("Clipboard write failed.", error);
    flashAnswerCopy(button, false);
  }
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
    state.ctx.answerBody.textContent = visibleText;
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
  publishAnswerCopy(state);
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
  publishAnswerCopy(state);
  if (stopped) {
    state.ctx.statusEl.textContent = "已停止";
    if (!state.answerText && !state.finalText) {
      state.ctx.answerBody.innerHTML = '<p class="answer-empty">已停止生成。</p>';
    }
    announce("已停止生成");
    return;
  }

  state.ctx.statusEl.textContent = "信号中断";
  // The frontend server already sends complete sentences ending in 请稍后重试;
  // only bare messages (network, HTTP status, raw agent errors) need framing.
  const text = /请稍后重试。?$/.test(err.message)
    ? err.message
    : `信号中断：${err.message}。请稍后重试。`;
  const msg = document.createElement("p");
  msg.className = "answer-error";
  msg.textContent = text;
  state.ctx.answerBody.appendChild(msg);
  announce(text);
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
  // Only the label span is rewritten — the button also holds the mobile
  // send/stop SVGs, which a blanket textContent assignment would wipe.
  const label = els.send.querySelector(".composer-send-label");
  if (label) label.textContent = isBusy ? "停止" : "发送";
  els.send.setAttribute("aria-label", isBusy ? "停止生成" : "发送问题");
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
  void askQuestion(q);
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

function applyTheme(theme, preference) {
  // Instant swap — no cross-fade class. A transition on every element stalls
  // for the length of a long transcript's style recalc, which read as the
  // article frame lagging (or the toggle "failing") on reader pages.
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.themePreference = preference;
  const meta = document.getElementById("theme-color-meta");
  if (meta) meta.setAttribute("content", theme === "light" ? "#ffffff" : "#212121");
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
    // Standalone in the masthead now — no separator dot, that belonged to the
    // old inline spot in the composer note.
    els.version.textContent = displayVersion;
    els.version.hidden = false;
  } catch (err) {
    console.warn("Version metadata is unavailable; hiding the version label.", err);
  }
}

els.reshuffle.addEventListener("click", renderSampleQuestions);

// A tab switch moves the address between /search and the reading pane's own
// route, without re-rendering the reading pane. The 搜索 tab always opens the
// empty search page, the same as visiting /search.
function switchMode(mode) {
  if (mode === "search") {
    setMode("search");
    resetSearch();
    pushSearchUrl("");
    els.searchInput.focus();
    return;
  }
  if (mode === document.body.dataset.mode) return;
  setMode(mode);
  if (isSearchPath(location.pathname)) {
    history.pushState({}, "", readingRoutePath);
    document.title = "睡前消息知识库";
  }
}

// Top mode tabs: click or Left/Right arrows, per the tabs pattern.
if (els.modeTabs) {
  els.modeTabs.addEventListener("click", (event) => {
    const tab = event.target.closest(".mode-tab");
    if (!tab) return;
    switchMode(tab.dataset.mode);
  });
  els.modeTabs.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const tabs = [els.modeTabChat, els.modeTabSearch];
    const current = tabs.indexOf(document.activeElement);
    if (current === -1) return;
    event.preventDefault();
    const step = event.key === "ArrowRight" ? 1 : tabs.length - 1;
    const next = tabs[(current + step) % tabs.length];
    next.focus();
    switchMode(next.dataset.mode);
  });
}

// 搜索: explicit submit (button or Enter) — no live-as-you-type calls.
els.searchForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  // An empty input submits the starter currently shown as the placeholder.
  const typed = els.searchInput.value.trim();
  const placeholder = currentSearchPlaceholder();
  const query = typed || (searchStarters.includes(placeholder) ? placeholder : "");
  if (query) {
    submitSearch(query);
    // Focus returns to the input so the query can be edited and re-run at once.
    els.searchInput.focus();
  }
});

const initialDataPromise = Promise.all([
  loadSampleQuestions(),
  loadVersion(),
  // The channel bar is part of every route, including the landing one, so the
  // index is fetched up front. A failure only costs the bar.
  loadTranscriptIndex().catch((error) => {
    console.warn("Transcript index unavailable; the channel bar stays hidden.", error);
  }),
]);
// A server-rendered article body (deep link) may come from a legacy snapshot
// without heading ids; anchor them before any hash scroll can run.
ensureHeadingAnchors(els.readerBody);
// Not awaited before the focus decision: every route sets data-view
// synchronously, so the landing surface is already known.
const routePromise = renderRoute();

// Same reasoning as after a run: autofocus on a phone opens the keyboard over
// the sample questions before the reader has seen them. Desktop always lands
// beside the reading desk (view=browse), so its check is layout, not view.
if (
  (!mobileLayout.matches || document.body.dataset.view === "chat") &&
  !window.matchMedia("(pointer: coarse)").matches
) {
  // /search lands in the search box; a results page keeps focus off it.
  if (document.body.dataset.mode !== "search") els.input.focus();
  else if (!searchState) els.searchInput.focus();
}

// Deep link: /?q=... opens straight into a query (shareable links). The query
// streams into the chat pane, so force the AI tab even if 搜索 was chosen.
// The view check must not key off data-view: on desktop the landing route now
// opens the reading desk (view=browse) while the chat pane stays interactive.
const deepLink =
  location.pathname === "/" ? new URLSearchParams(location.search).get("q") : null;
if (deepLink) {
  setMode("chat");
  await askQuestion(deepLink);
}
await Promise.all([initialDataPromise, routePromise]);
