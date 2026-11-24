import { View, div } from "gpui-kit";
import { v_flex, h_flex, TextView } from "gpui-base";
import { props, theme, home, readTextFile, exec, run, terminals, agents } from "herdr";

const LIMIT_BYTES = "1000000";
const LIMIT_MESSAGES = 400;
const SUMMARY_MAX = 160;
const EMPTY_LOG = "No review messages yet. Use sol-loop or council to start a logged review.";
const DEFAULT_LOG = () => `${home()}/.local/state/tstack-herdr/messages.jsonl`;
const ROLES_PATH = () => `${home()}/.config/tstack-herdr/roles.json`;

// Override any of these in roles.json under "colors": { "sol": "#10a37f", ... }.
const DEFAULT_COLORS = {
  implementer: "#2563eb",
  sol: "#10a37f",
  opus: "#d97757",
  gemini: "#8b5cf6",
  other: "#64748b",
};

function parseLog(raw) {
  const messages = [], deliveries = new Map(), reads = new Map(), ends = new Map();
  for (const line of String(raw ?? "").split("\n")) {
    if (!line.trim()) continue;
    try {
      const item = JSON.parse(line);
      if (item.kind === "delivery" && item.related_event_id) deliveries.set(item.related_event_id, item);
      // Optional explicit receipt: {kind:"read", related_event_id, reader:"<session>"}.
      if (item.kind === "read" && item.related_event_id && typeof item.reader === "string") {
        if (!reads.has(item.related_event_id)) reads.set(item.related_event_id, new Set());
        reads.get(item.related_event_id).add(item.reader);
      }
      // A dedicated reviewer session that was ended: how to resume it, read-only, from its snapshot.
      if (item.kind === "session_end" && typeof item.conversation_id === "string" && typeof item.session === "string"
        && typeof item.cwd === "string" && Array.isArray(item.resume_argv)
        && ["claude", "codex"].includes(item.resume_argv[0])) ends.set(`${item.conversation_id}:${item.session}`, item);
      if (item.kind === "message" && ["incoming", "outgoing"].includes(item.direction)
        && typeof item.text === "string" && item.text.trim()
        && typeof item.conversation_id === "string" && Number.isInteger(item.round)
        && typeof item.sender === "string" && typeof item.recipient === "string"
        && typeof item.source_session === "string" && typeof item.created_at === "string"
        && typeof item.repo === "string" && typeof item.worktree === "string"
        && /^[0-9a-f]{40}$/.test(item.base_sha) && /^[0-9a-f]{40}$/.test(item.head_sha)
        && Array.isArray(item.criteria) && Array.isArray(item.evidence)) messages.push(item);
    } catch { /* Ignore a truncated trailing line or a malformed record. */ }
  }
  return { messages: messages.slice(-LIMIT_MESSAGES), deliveries, reads, ends };
}

const stamp = (value) => {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
};
const clock = (value) => {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

function conversationMessages(messages) {
  const bubbles = new Map();
  for (const message of messages) {
    // Older broadcasts have one record per recipient and no shared message ID.
    const identity = message.group_message_id
      ?? (message.direction === "outgoing" ? message.text : message.event_id);
    const key = JSON.stringify([
      message.conversation_id, message.round, message.head_sha,
      message.source_session, message.direction, identity,
    ]);
    if (!bubbles.has(key)) bubbles.set(key, { ...message, copies: [] });
    bubbles.get(key).copies.push(message);
  }
  return [...bubbles.values()];
}

// ---- Safe resume commands ----------------------------------------------------
// Mirrors validate_resume_argv() in scripts/herdr_review_log.py. The panel reads the raw log, so it
// must not trust a session_end record: only a read-only resume of a snapshot folder may run.

const RESUME_FLAGS = {
  claude: { "--permission-mode": "one", "--model": "one", "--disallowedTools": "many", "--resume": "one" },
  codex: { "--no-daemon": "zero", "--model": "one", "--sandbox": "one", "--ask-for-approval": "one" },
};

function parseFlags(tokens, allowed) {
  const found = new Map();
  let index = 0;
  while (index < tokens.length) {
    const flag = tokens[index++];
    if (!Object.hasOwn(allowed, flag) || found.has(flag)) return null;
    const arity = allowed[flag], values = [];
    if (arity === "one") values.push(...tokens.slice(index, index + 1)), index += 1;
    else if (arity === "many") while (index < tokens.length && !String(tokens[index]).startsWith("--")) values.push(tokens[index++]);
    if ((arity === "one" && values.length !== 1) || (arity === "many" && !values.length)
      || values.some((value) => String(value).startsWith("--"))) return null;
    found.set(flag, values);
  }
  return found;
}

function safeResume(end, root) {
  const argv = end?.resume_argv;
  if (!Array.isArray(argv) || argv.some((item) => typeof item !== "string" || !item)) return false;
  // A plain folder name directly under the root: rejects ".", "..", "//" and a trailing slash.
  const cut = typeof end.cwd === "string" ? end.cwd.lastIndexOf("/") : -1;
  if (cut < 0 || end.cwd.slice(0, cut) !== root.replace(/\/$/, "")
    || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(end.cwd.slice(cut + 1))) return false;
  let id, flags;
  if (argv[0] === "codex") {
    if (argv.length < 3 || argv[1] !== "resume") return false;
    id = argv[2]; flags = parseFlags(argv.slice(3), RESUME_FLAGS.codex);
    if (!flags || flags.get("--sandbox")?.join() !== "read-only") return false;
    if (flags.has("--ask-for-approval") && flags.get("--ask-for-approval").join() !== "never") return false;
  } else if (argv[0] === "claude") {
    if (argv.length < 3 || argv[argv.length - 2] !== "--resume") return false;
    id = argv[argv.length - 1]; flags = parseFlags(argv.slice(1), RESUME_FLAGS.claude);
    if (!flags || flags.get("--permission-mode")?.join() !== "plan") return false;
  } else return false;
  return /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(id);
}

// ---- Task (conversation) model -------------------------------------------------

function humanize(slug) {
  const text = slug.replace(/[-_]+/g, " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

function taskTitle(messages) {
  for (const message of messages) if (typeof message.title === "string" && message.title.trim()) return message.title.trim();
  for (const message of messages) {
    const found = /^(?:Subject|Assunto):\s*(.+?)(?:\s+reply-to:.*)?$/im.exec(message.text);
    if (found) return humanize(found[1]);
  }
  const first = messages[0];
  const repo = first.repo.split("/").pop();
  return `Revisão de ${repo} · ${first.head_sha.slice(0, 7)}`;
}

function displayName(sender) {
  return sender.replace(/\s*[(/]\s*t\w+\)?\s*$/i, "").trim() || sender;
}

function roleOf(message) {
  const text = `${message.sender} ${message.sender_model ?? ""}`.toLowerCase();
  if (/sol\b/.test(text)) return "sol";
  if (/opus|claude/.test(text)) return "opus";
  if (/gemini|antigravity/.test(text)) return "gemini";
  if (/implementer|opencode|luna/.test(text)) return "implementer";
  return "other";
}

function dated(list) {
  const out = [];
  let day = null;
  for (const task of list) {
    const date = new Date(task.last.created_at);
    const label = Number.isNaN(date.valueOf()) ? "—"
      : `${String(date.getDate()).padStart(2, "0")}/${String(date.getMonth() + 1).padStart(2, "0")}/${String(date.getFullYear()).slice(-2)}`;
    if (label !== day) { day = label; out.push({ day }); }
    out.push({ task });
  }
  return out;
}

function tasks(messages) {
  const byConversation = new Map();
  for (const message of conversationMessages(messages)) {
    if (!byConversation.has(message.conversation_id)) byConversation.set(message.conversation_id, []);
    byConversation.get(message.conversation_id).push(message);
  }
  const list = [];
  for (const [id, items] of byConversation) {
    items.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
    const first = items[0], last = items[items.length - 1];
    const roles = [...new Set(items.map(roleOf))];
    const sessions = [...new Set(items.flatMap((m) => [m.source_session, ...m.copies.map((c) => c.destination_session)]).filter(Boolean))];
    list.push({
      id, title: taskTitle(items), repo: first.repo, worktree: first.worktree, head: last.head_sha,
      rounds: Math.max(...items.map((m) => m.round)), messages: items, last, roles, sessions,
      originSession: first.direction === "outgoing" ? first.source_session : (first.copies[0]?.destination_session ?? first.source_session),
    });
  }
  list.sort((a, b) => String(b.last.created_at).localeCompare(String(a.last.created_at)));
  return list;
}

// Read receipts, WhatsApp style. A participant counts as having read a message when
// an explicit "read" event exists, or when they posted anything later in the task.
function receipt(task, message, deliveries, reads) {
  const sender = message.source_session;
  const outgoing = message.direction === "outgoing";
  const targets = outgoing
    ? [...new Set(message.copies.map((copy) => copy.destination_session).filter(Boolean))]
    : task.sessions.filter((session) => session !== sender);
  if (!targets.length) return { mark: "", state: "none" };
  const explicit = new Set();
  for (const copy of message.copies) for (const reader of reads.get(copy.event_id) ?? []) explicit.add(reader);
  const readBy = (session) => explicit.has(session) || task.messages.some((m) =>
    m.source_session === session && String(m.created_at) > String(message.created_at));
  const delivered = (session) => !outgoing || message.copies.some((copy) =>
    copy.destination_session === session && ["sent", "delivered"].includes(deliveries.get(copy.event_id)?.status));
  if (targets.every(readBy)) return { mark: "✓✓", state: "read" };
  if (targets.every(delivered)) return { mark: "✓✓", state: "delivered" };
  if (targets.some((session) => outgoing && message.copies.some((copy) =>
    copy.destination_session === session && deliveries.get(copy.event_id)?.status === "blocked"))) return { mark: "!", state: "blocked" };
  return { mark: "✓", state: "sent" };
}

// ---- Human-readable message text ---------------------------------------------

const FENCE = /(```[\s\S]*?```)/g;
const HOME_PREFIX = () => home().replace(/\/$/, "");
const FILE_REF = /(?<![\w/.:-])((?:[\w@+.-]+\/)*[\w@+-][\w@+.-]*\.[A-Za-z][\w]{0,7})(?::(\d+)(?:-(\d+))?)?(?![\w/-])/g;
const LINK_BASE = "https://tstack-reviews.invalid/open";

function fileLink(task, ref, line, end) {
  const abs = ref.startsWith("/") ? ref : `${task.worktree}/${ref}`;
  const parts = ref.split("/");
  const label = `${parts.slice(-2).join("/")}${line ? `:${line}${end ? `-${end}` : ""}` : ""}`;
  return `[\`${label}\`](${LINK_BASE}?p=${encodeURIComponent(abs)}${line ? `&l=${line}` : ""})`;
}

function prettify(text, task) {
  const body = text.replace(/^\s*(?:Subject|Assunto):[^\n]*\n+/i, "") // the title is already in the header
    .replace(SUMMARY_LINE, ""); // the summary is already the bubble text
  return body.split(FENCE).map((part, index) => {
    if (index % 2) return part; // fenced code stays as written
    let out = part
      .replace(/\b[0-9a-f]{40}\b/g, (sha) => `\`${sha.slice(0, 7)}\``)
      .replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/g, (id) => `\`${id.slice(0, 8)}\``)
      // Absolute paths: inside the task worktree they become file links; elsewhere a short code span.
      .replace(/(?<![\w`])\/(?:[\w@+.-]+\/)+[\w@+.-]+(?::(\d+))?/g, (match, line) => {
        const path = line ? match.slice(0, match.lastIndexOf(":")) : match;
        if (path.startsWith(`${task.worktree}/`)) return fileLink(task, path.slice(task.worktree.length + 1), line);
        const base = path.split("/").pop();
        return /\.[A-Za-z]\w{0,7}$/.test(base) ? fileLink(task, path, line) : `\`${path.replace(HOME_PREFIX(), "~").split("/").slice(-1)[0] || path}\``;
      })
      .replace(FILE_REF, (match, ref, line, end) => ref.includes("/") || /^[\w-]+\.(js|ts|py|md|json|jsonl|rs|toml|yaml|yml|sh|css|html|patch|txt|svg)$/.test(ref)
        ? fileLink(task, ref, line, end) : match);
    // Single newlines are real line breaks in these messages.
    return out.replace(/([^\n])\n(?!\n)/g, "$1  \n");
  }).join("");
}

// ---- Chat-style summaries ----------------------------------------------------

function firstSentence(text) {
  const clean = text.replace(/^\s*(?:Subject|Assunto):[^\n]*\n+/i, "").replace(/[`*#>]/g, "").replace(/(^|\s)_+|_+(?=\s|$)/g, "$1").replace(/\s+/g, " ").trim();
  const cut = /^(.{20,}?[.!?])(\s|$)/.exec(clean);
  const sentence = cut ? cut[1] : clean;
  return sentence.length > SUMMARY_MAX ? `${sentence.slice(0, SUMMARY_MAX - 1).trimEnd()}…` : sentence;
}

const FINDING = /^\W*([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)\s*[—-]+\s*(Critical|High|Medium|Low|Info|Nit)\b\s*[—-]*\s*(.*)$/i;
const SEVERITY_ORDER = ["critical", "high", "medium", "low", "info", "nit"];
const MAX_FINDING_LINES = 3;

function shorten(text, max) {
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}

// Findings look like "R1-01 — Medium — path:line" followed by their description.
function findingsOf(text) {
  const lines = text.split("\n");
  const found = [];
  for (let i = 0; i < lines.length; i++) {
    const head = FINDING.exec(lines[i]);
    if (!head) continue;
    const body = [];
    let j = i + 1;
    while (j < lines.length && !lines[j].trim()) j++; // a blank line may follow the heading
    for (; j < lines.length && lines[j].trim() && !FINDING.test(lines[j]); j++) body.push(lines[j]);
    const label = /^\s*[-*]?\s*(?:Impact|Impacto|Problem|Problema|Issue|Summary|Resumo)\s*:\s*/i;
    const prose = body.map((line) => line.replace(label, "")).join(" ");
    const sentence = firstSentence(prose) || firstSentence(head[3]);
    found.push({ level: head[2].toLowerCase(), text: sentence });
  }
  return found;
}

// Intent line of a request, e.g. "Intenção do usuário: …" or "Change under review: …".
function intentOf(text) {
  // Prefer the stated intent over a generic "request" line.
  const labels = [
    /^\W*(?:(?:Intent|Intention|Intenção)[^:]{0,30}|(?:Change under review|Mudança em revisão)(?:\s*\([^)]*\))?):\s*(.+)$/i,
    /^\W*(?:Pedido|Request)[^:]{0,30}:\s*(.+)$/i,
  ];
  // Paragraphs are hard-wrapped, so unwrap each one before matching.
  const paragraphs = text.split(/\n\s*\n/).map((paragraph) => paragraph.replace(/\s*\n\s*/g, " "));
  for (const label of labels) {
    for (const paragraph of paragraphs) {
      const found = label.exec(paragraph);
      if (found) return firstSentence(found[1]);
    }
  }
  return "";
}

// The agent's own "summary" field wins. Older or summary-less messages get a rule-based one.
const SUMMARY_LINE = /^\s*Summary:[ \t]*(.+)(?:\n+|$)/i;

function summarize(message) {
  if (typeof message.summary === "string" && message.summary.trim()) return message.summary.trim();
  // A reviewer may open its reply with "Summary: …" when the field was not copied to the log.
  const written = SUMMARY_LINE.exec(message.text);
  if (written && written[1].trim()) return shorten(written[1].trim(), 500);
  const text = message.text.trim();
  const verdict = message.direction === "incoming" ? /\b(APPROVE|CHANGES_REQUESTED|INCONCLUSIVE)\b/.exec(text.slice(0, 300)) : null;
  if (verdict) {
    if (verdict[1] === "APPROVE") return "Approved";
    if (verdict[1] === "INCONCLUSIVE") return "Inconclusive";
    const findings = findingsOf(text);
    if (!findings.length) return "Changes requested";
    const counts = new Map();
    for (const finding of findings) counts.set(finding.level, (counts.get(finding.level) ?? 0) + 1);
    const detail = SEVERITY_ORDER.filter((level) => counts.has(level))
      .map((level) => `${counts.get(level)} ${level[0].toUpperCase()}${level.slice(1)}`).join(", ");
    const top = [...findings].sort((a, b) => SEVERITY_ORDER.indexOf(a.level) - SEVERITY_ORDER.indexOf(b.level))
      .slice(0, MAX_FINDING_LINES)
      .map((finding) => `• ${finding.level[0].toUpperCase()}${finding.level.slice(1)} — ${shorten(finding.text, 110)}`);
    const more = findings.length - top.length;
    return [`Changes requested · ${findings.length} ${findings.length === 1 ? "finding" : "findings"}: ${detail}`,
      ...top, ...(more > 0 ? [`+${more} more`] : [])].join("\n");
  }
  if (message.direction === "outgoing") {
    const intent = intentOf(text);
    const n = message.criteria.length;
    const head = `Review requested · round ${message.round}${n ? ` · ${n} ${n === 1 ? "criterion" : "criteria"}` : ""}`;
    return intent ? `${head}\n${intent}` : head;
  }
  return firstSentence(text) || shorten(text.replace(/\s+/g, " ").trim(), SUMMARY_MAX) || "(empty message)";
}

// ---- Colors --------------------------------------------------------------------

const REVIEW_ROOT = () => `${home()}/.cache/tstack-review`;

const HEX = /^#([0-9a-f]{6})$/i;
function mix(color, base, amount) {
  const a = HEX.exec(color), b = HEX.exec(base ?? "");
  if (!a || !b) return color;
  const channel = (i) => Math.round(parseInt(a[1].slice(i, i + 2), 16) * amount + parseInt(b[1].slice(i, i + 2), 16) * (1 - amount));
  return `#${[0, 2, 4].map((i) => channel(i).toString(16).padStart(2, "0")).join("")}`;
}

export default class TStackReviews extends View {
  init(_props, cx) {
    this.path = props()?.logPath || DEFAULT_LOG();
    this.messages = [];
    this.deliveries = new Map();
    this.reads = new Map();
    this.reviewRoot = props()?.reviewRoot || REVIEW_ROOT();
    this.ends = new Map(); // conversation:session -> session_end record
    this.resumable = new Set(); // keys of ended sessions whose snapshot folder still exists
    this.colors = { ...DEFAULT_COLORS };
    this.context = new Map(); // conversation_id -> { space, tab, spaceName, tabName }
    this.selected = null;
    this.expanded = new Set();
    this.status = "Loading local review log…";
    this.loading = false;
    cx.spawn((cx) => this.refresh(cx));
    cx.timer.every(3000, (cx) => this.refresh(cx));
  }

  async loadColors() {
    try {
      const roles = JSON.parse((await readTextFile(ROLES_PATH())) ?? "{}");
      this.colors = { ...DEFAULT_COLORS, ...(roles.colors && typeof roles.colors === "object" ? roles.colors : {}) };
    } catch { this.colors = { ...DEFAULT_COLORS }; }
  }

  // Remember where a task started: the space/tab of its originating session while it is live.
  // Context written into the log by the protocol (space_id/tab_id/...) survives closed sessions.
  loggedContext(task) {
    const found = task.messages.find((m) => m.tab_id || m.space_id);
    return found ? { space: found.space_id, tab: found.tab_id, spaceName: found.space_name ?? "Space", tabName: found.tab_name ?? "Tab" } : null;
  }

  trackContext() {
    const live = new Map(agents().map((agent) => [agent.address, agent]));
    for (const task of tasks(this.messages)) {
      const logged = this.loggedContext(task);
      if (logged) this.context.set(task.id, logged);
      const sessions = [task.originSession, ...task.sessions];
      for (const session of sessions) {
        const agent = live.get(session);
        if (!logged && agent && session === task.originSession) {
          this.context.set(task.id, { space: agent.space, tab: agent.tab, spaceName: agent.space_name, tabName: agent.tab_name });
          break;
        }
      }
    }
  }

  async refresh(cx) {
    if (this.loading) return;
    this.loading = true;
    try {
      const read = await exec("tail", ["-c", LIMIT_BYTES, this.path], { timeout: 5000 });
      const raw = read.code === 0 ? read.stdout : await readTextFile(this.path);
      const parsed = parseLog(raw);
      this.messages = parsed.messages;
      this.deliveries = parsed.deliveries;
      this.reads = parsed.reads;
      this.ends = parsed.ends;
      const resumable = new Set();
      for (const [key, end] of parsed.ends) if (await this.checkedFolder(end)) resumable.add(key);
      this.resumable = resumable;
      await this.loadColors();
      try { this.trackContext(); } catch { /* Context is best effort. */ }
      this.status = parsed.messages.length ? "" : EMPTY_LOG;
    } catch (error) {
      this.status = `Could not read the local log: ${String(error?.message ?? error)}`;
    } finally {
      this.loading = false;
      cx.notify();
    }
  }

  // The canonical snapshot folder for a resume record, or null. Run at refresh and again at the click,
  // because a folder can be swapped for a symlink or a file in between.
  async checkedFolder(end) {
    if (!safeResume(end, this.reviewRoot)) return null;
    const real = async (path) => {
      const out = await exec("realpath", ["-e", "--", path], { timeout: 3000 }).catch(() => ({ code: 1 }));
      return out.code === 0 ? out.stdout.trim() : null;
    };
    const [root, folder] = [await real(this.reviewRoot), await real(end.cwd)];
    if (!root || !folder || folder.slice(0, folder.lastIndexOf("/")) !== root) return null;
    const isDir = await exec("test", ["-d", folder], { timeout: 3000 }).catch(() => ({ code: 1 }));
    return isDir.code === 0 ? folder : null;
  }

  // Reopen an ended reviewer in its original snapshot, with the read-only flags it was launched with.
  async resume(end) {
    const folder = await this.checkedFolder(end);
    if (!folder) return;
    run("tab.new", { name: "Reviewer (resumed)", folder, argv: end.resume_argv });
  }

  openLink(url) {
    if (!url.startsWith(LINK_BASE)) return run("open.url", { url });
    const query = new URLSearchParams(url.slice(url.indexOf("?") + 1));
    const file = query.get("p");
    if (!file) return;
    const open = (path) => run("tab.new", { mod: "zed-file", props: { file: path, line: Number(query.get("l")) || undefined } });
    exec("test", ["-f", file], { timeout: 3000 }).then(async (result) => {
      if (result.code === 0) return open(file);
      // A bare name such as main.js: look for it inside the task worktree.
      const root = file.slice(0, file.lastIndexOf("/"));
      const found = await exec("find", [root.includes("tstack-") ? root : root, "-name", file.split("/").pop(), "-type", "f", "-not", "-path", "*/node_modules/*"], { timeout: 4000 });
      const first = found.code === 0 ? found.stdout.split("\n").find(Boolean) : null;
      if (first) open(first);
    }).catch(() => {});
  }

  color(role) { return this.colors[role] ?? this.colors.other; }

  // ---- Task list -------------------------------------------------------------

  taskRow(task, t) {
    const where = this.context.get(task.id);
    const preview = summarize(task.last).split("\n")[0].slice(0, 90);
    return v_flex().w_full().gap(4).px(12).py(10).rounded(10).cursor_pointer()
      .bg(t.raised).border(1).border_color(t.border)
      .on_click((_event, cx) => { this.selected = task.id; cx.notify(); })
      .child(h_flex().w_full().gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().font_semibold().text_size(13).text_color(t.foreground).child(task.title))
        .child(div().text_size(10.5).text_color(t.dim).child(clock(task.last.created_at))))
      .child(div().w_full().truncate().text_size(11.5).text_color(t.muted)
        .child(`${displayName(task.last.sender)}: ${preview}`))
      .child(h_flex().w_full().gap(6).items_center()
        .children(task.roles.map((role) => div().w(8).h(8).rounded(4).bg(this.color(role))))
        .child(div().flex_1().min_w(0).truncate().text_size(10.5).text_color(t.dim)
          .child(`${where ? `${where.spaceName} › ${where.tabName} · ` : ""}Round ${task.rounds} · ${task.messages.length} msgs`)));
  }

  listView(t) {
    const list = tasks(this.messages);
    return v_flex().flex_1().min_h(0).min_w(0).overflow_y_scroll().gap(8).px(12).py(12)
      .when(list.length > 0, (el) => el.children(dated(list).flatMap((entry) => entry.day
        ? [h_flex().w_full().gap(8).items_center().py(4)
            .child(div().flex_1().h(1).bg(t.border))
            .child(div().text_size(10.5).text_color(t.dim).child(entry.day))
            .child(div().flex_1().h(1).bg(t.border))]
        : [this.taskRow(entry.task, t)])))
      .when(Boolean(this.status), (el) => el.child(div().w_full().text_size(13).text_color(t.muted).child(this.status)));
  }

  // ---- Conversation ----------------------------------------------------------

  bubble(task, message, t, available) {
    const role = roleOf(message);
    const accent = this.color(role);
    const right = role === "implementer";
    const tick = receipt(task, message, this.deliveries, this.reads);
    const open = this.expanded.has(message.event_id);
    const summary = summarize(message);
    const tickColor = tick.state === "read" ? "#34b7f1" : tick.state === "blocked" ? "#ef4444" : t.dim;
    const openSource = () => run("terminal.peek", { terminal: message.source_session });
    const endKey = `${task.id}:${message.source_session}`;
    const resumeEnd = !available && this.resumable.has(endKey) ? this.ends.get(endKey) : null;
    const card = v_flex().max_w(760).min_w(0).gap(6).px(12).py(9).rounded(12)
      .bg(mix(accent, t.background, 0.22)).border(1).border_color(mix(accent, t.background, 0.55))
      .child(div().w_full().truncate().font_semibold().text_size(12).text_color(accent).child(displayName(message.sender)))
      .child(v_flex().w_full().gap(3).children(summary.split("\n").map((line) => div().w_full().text_size(13.5).text_color(t.foreground).child(line))))
      .when(open, (el) => el.child(v_flex().w_full().gap(6).pt(6).mt(2).border_t(1).border_color(mix(accent, t.background, 0.4))
        .child(TextView.markdown(`review-${message.event_id}`, prettify(message.text, task)).selectable(true).on_link_click((url) => this.openLink(url)))))
      .child(div().text_size(11).text_color(t.accent).cursor_pointer()
        .on_click((_event, cx) => {
          if (open) this.expanded.delete(message.event_id); else this.expanded.add(message.event_id);
          cx.notify();
        }).child(open ? "Hide details" : "Details"))
      .child(h_flex().w_full().gap(8).items_center().justify_end()
        .when(available, (el) => el.child(div().text_size(10.5).text_color(t.dim).cursor_pointer().on_click(openSource).child("Open session")))
        .when(!available && resumeEnd, (el) => el.child(div().text_size(10.5).text_color(t.accent).cursor_pointer().on_click(() => this.resume(resumeEnd)).child("Resume session")))
        .child(div().text_size(10.5).text_color(t.dim).child(clock(message.created_at)))
        .when(Boolean(tick.mark), (el) => el.child(div().text_size(11).font_semibold().text_color(tickColor).child(tick.mark))));
    return h_flex().w_full().when(right, (el) => el.justify_end()).child(card);
  }

  chatView(task, t, live) {
    const where = this.context.get(task.id);
    const rows = [];
    let round = null;
    for (const message of task.messages) {
      if (message.round !== round) {
        round = message.round;
        rows.push(div().w_full().text_size(10.5).text_color(t.dim).child(`— Round ${round} · head ${message.head_sha.slice(0, 7)} —`));
      }
      rows.push(this.bubble(task, message, t, live.has(message.source_session)));
    }
    return v_flex().flex_1().min_h(0).min_w(0).child(
      v_flex().w_full().gap(4).px(14).py(10).border_b(1).border_color(t.border)
        .child(h_flex().w_full().gap(8).items_center()
          .child(div().px(8).py(4).rounded(7).cursor_pointer().bg(t.raised).text_size(11.5)
            .on_click((_event, cx) => { this.selected = null; cx.notify(); }).child("← Tasks"))
          .child(div().flex_1().min_w(0).truncate().font_semibold().text_size(13).child(task.title)))
        .child(h_flex().w_full().gap(6).items_center()
          .children(task.roles.map((role) => h_flex().gap(4).items_center()
            .child(div().w(8).h(8).rounded(4).bg(this.color(role)))
            .child(div().text_size(10.5).text_color(t.muted).child(role))))
          .child(div().flex_1()))
        .child(div().w_full().truncate().text_size(10.5).text_color(t.dim).child(`${task.repo} · ${task.worktree.split("/").pop()} · ${task.head.slice(0, 7)}`))
        .when(Boolean(where), (el) => el.child(div().w_full().truncate().text_size(11).text_color(t.accent).cursor_pointer()
          .on_click(() => run("tab.select", { id: where.tab }))
          .child(`Started in ${where.spaceName} › ${where.tabName} (go to tab)`))))
      .child(v_flex().flex_1().min_h(0).min_w(0).overflow_y_scroll().gap(10).px(12).py(12).children(rows));
  }

  render() {
    const t = theme();
    const live = new Set(terminals().filter((terminal) => (terminal.state ?? "live") === "live").map((terminal) => terminal.address));
    const task = this.selected ? tasks(this.messages).find((item) => item.id === this.selected) : null;
    return v_flex().id("tstack-herdr-review").size_full().min_w(0).min_h(0).bg(t.background).text_color(t.foreground)
      .child(h_flex().w_full().px(16).py(12).gap(12).items_center().border_b(1).border_color(t.border)
        .child(div().flex_1().min_w(0).truncate().font_semibold().text_size(14).child("Reviews"))
        .child(div().px(10).py(5).rounded(7).cursor_pointer().bg(t.raised).text_size(11.5)
          .on_click((_event, cx) => cx.spawn((cx) => this.refresh(cx))).child("Refresh")))
      .child(task ? this.chatView(task, t, live) : this.listView(t));
  }
}

export { parseLog, tasks, receipt, prettify, dated, summarize, safeResume };
