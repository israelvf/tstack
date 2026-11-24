import { View, div } from "gpui-kit";
import { v_flex, h_flex, TextView } from "gpui-base";
import { props, theme, home, readTextFile, exec, run, terminals, agents } from "herdr";

const LIMIT_BYTES = "1000000";
const LIMIT_MESSAGES = 400;
const COLLAPSE_AT = 600;
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
  const messages = [], deliveries = new Map(), reads = new Map();
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
  return { messages: messages.slice(-LIMIT_MESSAGES), deliveries, reads };
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
  const body = text.replace(/^\s*(?:Subject|Assunto):[^\n]*\n+/i, ""); // the title is already in the header
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

// ---- Colors --------------------------------------------------------------------

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
    const preview = task.last.text.replace(/\s+/g, " ").slice(0, 90);
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
    const pretty = prettify(message.text, task);
    const long = pretty.length > COLLAPSE_AT;
    const body = long && !open ? `${pretty.slice(0, COLLAPSE_AT).trimEnd()}…` : pretty;
    const tickColor = tick.state === "read" ? "#34b7f1" : tick.state === "blocked" ? "#ef4444" : t.dim;
    const openSource = () => run("terminal.peek", { terminal: message.source_session });
    const card = v_flex().max_w(760).min_w(0).gap(6).px(12).py(9).rounded(12)
      .bg(mix(accent, t.background, 0.22)).border(1).border_color(mix(accent, t.background, 0.55))
      .child(div().w_full().truncate().font_semibold().text_size(12).text_color(accent).child(displayName(message.sender)))
      .child(TextView.markdown(`review-${message.event_id}`, body).selectable(true).on_link_click((url) => this.openLink(url)))
      .when(long, (el) => el.child(div().text_size(11).text_color(t.accent).cursor_pointer()
        .on_click((_event, cx) => {
          if (open) this.expanded.delete(message.event_id); else this.expanded.add(message.event_id);
          cx.notify();
        }).child(open ? "Show less" : "Show more")))
      .child(h_flex().w_full().gap(8).items_center().justify_end()
        .when(available, (el) => el.child(div().text_size(10.5).text_color(t.dim).cursor_pointer().on_click(openSource).child("Open session")))
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
    const live = new Set(terminals().map((terminal) => terminal.address));
    const task = this.selected ? tasks(this.messages).find((item) => item.id === this.selected) : null;
    return v_flex().id("tstack-herdr-review").size_full().min_w(0).min_h(0).bg(t.background).text_color(t.foreground)
      .child(h_flex().w_full().px(16).py(12).gap(12).items_center().border_b(1).border_color(t.border)
        .child(div().flex_1().min_w(0).truncate().font_semibold().text_size(14).child("Reviews"))
        .child(div().px(10).py(5).rounded(7).cursor_pointer().bg(t.raised).text_size(11.5)
          .on_click((_event, cx) => cx.spawn((cx) => this.refresh(cx))).child("Refresh")))
      .child(task ? this.chatView(task, t, live) : this.listView(t));
  }
}

export { parseLog, tasks, receipt, prettify, dated };
