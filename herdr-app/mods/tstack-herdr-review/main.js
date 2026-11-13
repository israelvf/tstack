import { View, div } from "gpui-kit";
import { v_flex, h_flex, TextView } from "gpui-base";
import { props, theme, home, readTextFile, exec, run, terminals } from "herdr";

const LIMIT_BYTES = "1000000";
const LIMIT_MESSAGES = 180;
const EMPTY_LOG = "No review messages yet. Use sol-loop or council to start a logged review.";
const DEFAULT_LOG = () => `${home()}/.local/state/tstack-herdr/messages.jsonl`;

function parseLog(raw) {
  const messages = [], deliveries = new Map();
  for (const line of String(raw ?? "").split("\n")) {
    if (!line.trim()) continue;
    try {
      const item = JSON.parse(line);
      if (item.kind === "delivery" && item.related_event_id) deliveries.set(item.related_event_id, item);
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
  return { messages: messages.slice(-LIMIT_MESSAGES), deliveries };
}

const stamp = (value) => {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
};

function grouped(messages) {
  const groups = new Map();
  for (const message of messages) {
    const key = `${message.conversation_id}:${message.round}`;
    if (!groups.has(key)) groups.set(key, { id: key, conversation: message.conversation_id, round: message.round, repo: message.repo, worktree: message.worktree, head: message.head_sha, messages: [] });
    groups.get(key).messages.push(message);
  }
  return [...groups.values()];
}

export default class TStackReviews extends View {
  init(_props, cx) {
    this.path = props()?.logPath || DEFAULT_LOG();
    this.messages = [];
    this.deliveries = new Map();
    this.status = "Loading local review log…";
    this.loading = false;
    cx.spawn((cx) => this.refresh(cx));
    cx.timer.every(3000, (cx) => this.refresh(cx));
  }

  async refresh(cx) {
    if (this.loading) return;
    this.loading = true;
    try {
      const read = await exec("tail", ["-c", LIMIT_BYTES, this.path], { timeout: 5000 });
      if (read.code !== 0) {
        const fallback = await readTextFile(this.path);
        if (fallback == null) {
          this.messages = [];
          this.deliveries = new Map();
          this.status = EMPTY_LOG;
        } else {
          const parsed = parseLog(fallback);
          this.messages = parsed.messages;
          this.deliveries = parsed.deliveries;
          this.status = EMPTY_LOG;
        }
      } else {
        const parsed = parseLog(read.stdout);
        this.messages = parsed.messages;
        this.deliveries = parsed.deliveries;
        this.status = EMPTY_LOG;
      }
    } catch (error) {
      this.status = `Could not read the local log: ${String(error?.message ?? error)}`;
    } finally {
      this.loading = false;
      cx.notify();
    }
  }

  bubble(message, t, available) {
    const outgoing = message.direction === "outgoing";
    const delivery = this.deliveries.get(message.event_id);
    const author = message.sender;
    const destination = message.recipient;
    const open = () => run("terminal.peek", { terminal: message.source_session });
    const side = h_flex().w_full().when(outgoing, (el) => el.justify_end());
    const card = v_flex().max_w(760).min_w(0).gap(8).px(14).py(12).rounded(12)
      .bg(outgoing ? t.raised : t.chrome).border(1).border_color(t.border)
      .child(h_flex().w_full().gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().font_semibold().text_size(12.5).text_color(t.foreground).child(author))
        .child(div().max_w(120).truncate().text_size(11.5).text_color(t.dim).child(`to ${destination}`)))
      .child(div().text_size(11).text_color(t.dim).child(stamp(message.created_at)))
      .child(TextView.markdown(`review-${message.event_id}`, message.text).selectable(true))
      .child(h_flex().w_full().gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().text_size(10.5).text_color(t.dim)
          .child(delivery ? `Delivery: ${delivery.status}${delivery.receipt?.turn ? ` · turn ${delivery.receipt.turn}` : ""}` : outgoing ? "Delivery: pending" : `Session: ${message.source_session}`))
        .when(available, (el) => el.child(div().px(9).py(4).rounded(7).cursor_pointer().bg(t.surface).text_size(11).text_color(t.accent)
          .on_click(open).child("Open source session")))
        .when(!available, (el) => el.child(div().text_size(11).text_color(t.dim).child("Source session not live"))));
    return side.child(card);
  }

  groupView(group, t, live) {
    return v_flex().w_full().gap(10).pb(16)
      .child(v_flex().w_full().gap(4).pb(8).border_b(1).border_color(t.border)
        .child(div().w_full().truncate().font_semibold().text_size(13).text_color(t.foreground).child(`Task ${group.conversation}`))
        .child(div().text_size(11).text_color(t.muted).child(`Round ${group.round} · ${group.messages.length} ${group.messages.length === 1 ? "message" : "messages"}`))
        .child(div().w_full().truncate().text_size(11).text_color(t.muted).child(`${group.repo} · ${group.worktree}`))
        .child(div().truncate().text_size(10.5).text_color(t.dim).child(`Reviewed head ${group.head}`)))
      .children(group.messages.map((message) => this.bubble(message, t, live.has(message.source_session))));
  }

  render() {
    const t = theme();
    const live = new Set(terminals().map((terminal) => terminal.address));
    const groups = grouped(this.messages);
    return v_flex().id("tstack-herdr-review").size_full().min_w(0).min_h(0).bg(t.background).text_color(t.foreground)
      .child(h_flex().w_full().px(16).py(12).gap(12).items_center().border_b(1).border_color(t.border)
        .child(div().flex_1().min_w(0).truncate().font_semibold().text_size(14).child("TStack Reviews"))
        .child(div().px(10).py(5).rounded(7).cursor_pointer().bg(t.raised).text_size(11.5)
          .on_click((_event, cx) => cx.spawn((cx) => this.refresh(cx))).child("Refresh")))
      .child(v_flex().flex_1().min_h(0).min_w(0).overflow_y_scroll().gap(18).px(12).py(14)
        .when(groups.length > 0, (el) => el.children(groups.map((group) => this.groupView(group, t, live))))
        .when(groups.length === 0, (el) => el.justify_center().child(div().w_full().text_size(13).text_color(t.muted).child(this.status))));
  }
}

export { parseLog, grouped };
