"use strict";

// One card at a time. Swipe up = next card (no going back). Swipe down = open the topic list.
// Swipe left = open the chat (swipe right there to come back).
const stage = document.getElementById("stage");
const sheet = document.getElementById("topics");
const sheetBody = document.getElementById("sheetBody");
const toastEl = document.getElementById("toast");
const hintEl = document.getElementById("hint");

const SWIPE = 70;           // px of finger travel that counts as a swipe
const FLICK = 0.5;          // px/ms; a fast short flick also counts
const EDGE = 24;            // px from the screen edge where sideways swipes are left to the system
const seen = new Set();     // card ids already fetched (sent as exclude)
let queue = [];             // fetched cards not shown yet
let current = null;         // { el, card, since, notice }
let busy = false;
let loading = null;
let retryTimer = null;

// ---------- helpers ----------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) el.append(c.nodeType ? c : document.createTextNode(c));
  return el;
}

function toast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => toastEl.classList.remove("show"), 2400);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { return null; }
}

// ---------- cards ----------

const HEART = "M12 21s-7.5-4.6-9.6-9.1C.9 8.6 3 5 6.6 5c2 0 3.5 1.1 4.4 2.5C11.9 6.1 13.4 5 15.4 5 19 5 21.1 8.6 19.6 11.9 17.5 16.4 12 21 12 21z";

function heart(cls) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("class", cls);
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = `<path fill="currentColor" d="${HEART}"/>`;
  return s;
}

// The card as plain text, for pasting into a message or a note.
function cardText(c) {
  return [c.title, c.points.map(p => `• ${p}`).join("\n"), c.takeaway ? `💡 ${c.takeaway}` : ""]
    .filter(Boolean).join("\n\n");
}

async function copyCard(c) {
  const text = cardText(c);
  try {
    await navigator.clipboard.writeText(text);
  } catch {   // older browsers, or the clipboard API refused: copy through a hidden text box
    const ta = h("textarea", { readonly: "" });
    ta.value = text;
    ta.style.cssText = "position:fixed;opacity:0;top:0";
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    if (!ok) { toast("复制失败"); return; }
  }
  toast("已复制卡片文字");
}

function copyButton(c) {
  const b = h("button", { class: "copy", type: "button", "aria-label": "复制卡片文字" }, "复制");
  b.addEventListener("click", () => copyCard(c));   // mouse; taps on the card stage are handled in touchend
  return b;
}

function cardSlide(c) {
  const label = [c.review ? "❤ 收藏回顾" : "", c.theme, c.topic].filter(Boolean).join(" · ");
  return h("section", { class: "slide" },
    h("article", { class: "card" + (c.reaction === 1 ? " faved" : "") },
      heart("fav-mark"),
      label ? h("span", { class: "chip" }, label) : null,
      c.image ? h("img", { class: "card-img", src: c.image, alt: "" }) : null,
      h("h1", {}, c.title),
      h("ul", { class: "points" }, c.points.map(p => h("li", {}, p))),
      c.takeaway ? h("p", { class: "takeaway" }, "💡 ", c.takeaway) : null,
      h("div", { class: "card-foot" }, copyButton(c))));
}

function noticeSlide(title, text, spinning) {
  return h("section", { class: "slide" },
    h("div", { class: "notice" }, spinning ? h("div", { class: "spinner" }) : null, h("h2", {}, title), h("p", {}, text)));
}

async function emptySlide() {
  let st = null;
  try { st = await api("/api/status"); } catch {
    return noticeSlide("连不上服务器", "检查一下网络，上滑重试。", false);
  }
  if (st.has_key) return noticeSlide("正在准备更多内容", "AI 正在读新文章、写卡片，一会儿就好。", true);
  const queued = (st.stats.ready || 0) + (st.stats.new || 0);
  return noticeSlide("新卡片都刷完了", queued
    ? `已经攒了 ${queued} 篇新文章。和 Claude 说一声「更新推流」，就会把它们写成卡片。`
    : "和 Claude 说一声「更新推流」，就会去找新内容写成卡片。", false);
}

function fetchMore() {
  if (!loading) {
    loading = api(`/api/feed?n=8&exclude=${[...seen].join(",")}`)
      .then(data => {
        for (const c of data.cards) if (!seen.has(c.id)) { seen.add(c.id); queue.push(c); }
      })
      .catch(() => {})
      .finally(() => { loading = null; });
  }
  return loading;
}

function sendView() {
  if (!current || current.notice || current.sent) return;
  current.sent = true;
  const body = JSON.stringify({ card_id: current.card.id, type: "view", dwell_ms: Math.round(performance.now() - current.since) });
  if (navigator.sendBeacon) navigator.sendBeacon("/api/event", new Blob([body], { type: "application/json" }));
  else fetch("/api/event", { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true });
}

function postEvent(card_id, type) {
  return fetch("/api/event", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ card_id, type }), keepalive: true });
}

// Double tap: favourite the card on screen; a second double tap takes it back.
function toggleFavorite(x, y) {
  if (!current || current.notice) return;
  const c = current.card;
  const on = c.reaction !== 1;
  c.reaction = on ? 1 : 0;
  current.el.querySelector(".card").classList.toggle("faved", on);
  postEvent(c.id, on ? "like" : "clear");
  const burst = heart("burst" + (on ? "" : " off"));
  burst.style.left = `${x}px`;
  burst.style.top = `${y}px`;
  document.body.append(burst);
  setTimeout(() => burst.remove(), 800);
  toast(on ? "已收藏，下滑可以在「收藏」里找到" : "已取消收藏");
}

function setTransform(el, y, animate) {
  el.classList.toggle("animate", !!animate);
  el.style.transform = y ? `translateY(${y})` : "";
}

// Bring in the next card from below; the current one leaves upwards.
async function next() {
  if (busy) return;
  busy = true;
  try {
    if (!queue.length) await fetchMore();
    const card = queue.shift();
    if (!card && current && current.notice) {   // still nothing new: bounce back
      setTransform(current.el, "0", true);
      scheduleRetry();
      return;
    }
    const el = card ? cardSlide(card) : await emptySlide();
    sendView();
    const old = current;
    setTransform(el, "100%", false);
    stage.append(el);
    el.getBoundingClientRect();                 // commit the start position before animating
    setTransform(el, "0", true);
    if (old) {
      setTransform(old.el, "-100%", true);
      setTimeout(() => old.el.remove(), 320);
    }
    current = { el, card, since: performance.now(), notice: !card };
    if (card) clearTimeout(retryTimer); else scheduleRetry();
    if (queue.length < 4) fetchMore();
    await new Promise(r => setTimeout(r, 300));
  } finally {
    busy = false;
  }
}

function scheduleRetry() {
  clearTimeout(retryTimer);
  retryTimer = setTimeout(async () => {
    await fetchMore();
    if (current && current.notice && queue.length) next(); else scheduleRetry();
  }, 20000);
}

function dismissHint() {
  if (!hintEl.hidden) { hintEl.hidden = true; store("tuiliu-hint", "1"); }
}

// ---------- gestures on the card stage ----------

let touch = null;
let lastTap = null;   // { t, x, y } of the previous quick tap, for double-tap detection
let lastTouchEnd = -1e9;

stage.addEventListener("touchstart", e => {
  if (busy || e.touches.length > 1) return;
  const { clientX: x, clientY: y } = e.touches[0];
  touch = { x0: x, y0: y, last: y, t0: performance.now(), dy: 0, dx: 0, moved: 0, axis: null,
            card: current && current.el.querySelector(".card") };
}, { passive: true });

stage.addEventListener("touchmove", e => {
  if (!touch) return;
  e.preventDefault();
  const { clientX: x, clientY: y } = e.touches[0];
  // Decide once per touch whether it is a sideways swipe (chat) or an up/down one (cards, topics).
  if (!touch.axis && Math.max(Math.abs(x - touch.x0), Math.abs(y - touch.y0)) > 10)
    touch.axis = Math.abs(x - touch.x0) > 1.2 * Math.abs(y - touch.y0) ? "x" : "y";
  if (touch.axis === "x") {
    touch.dx = x - touch.x0;
    touch.moved = Math.max(touch.moved, Math.abs(touch.dx));
    if (touch.dx < 0 && current) current.el.style.transform = `translateX(${touch.dx * 0.25}px)`;
    return;
  }
  const step = touch.last - y;
  touch.last = y;
  touch.moved = Math.max(touch.moved, Math.abs(y - touch.y0), Math.abs(e.touches[0].clientX - touch.x0));
  const card = touch.card;
  // A long card scrolls inside itself first; the swipe starts once it reaches its edge.
  if (card && card.scrollHeight > card.clientHeight + 2 && touch.dy === 0) {
    const atTop = card.scrollTop <= 0, atBottom = card.scrollTop + card.clientHeight >= card.scrollHeight - 1;
    if ((step > 0 && !atBottom) || (step < 0 && !atTop)) {
      card.scrollTop += step;
      touch.y0 = y;
      return;
    }
  }
  touch.dy = y - touch.y0;
  if (touch.dy < 0 && current) setTransform(current.el, `${touch.dy}px`, false);
  else if (touch.dy > 0) setSheet(`translateY(calc(-100% + ${touch.dy}px))`, false);
}, { passive: false });

stage.addEventListener("touchend", e => {
  if (!touch) return;
  const { dy, dx, t0, moved, axis, x0 } = touch;
  touch = null;
  if (axis === "x") {
    if (current) setTransform(current.el, "0", true);
    // Ignore swipes that start at the screen edge: that is the phone's own "back" gesture.
    if (-dx > SWIPE && x0 > EDGE && x0 < innerWidth - EDGE) { dismissHint(); openChat(); }
    return;
  }
  const now = performance.now();
  lastTouchEnd = now;
  if (moved < 12 && now - t0 < 300) {             // a quick tap; two in a row is a double tap
    e.preventDefault();                           // no emulated mouse clicks / dblclick afterwards
    if (e.target.closest && e.target.closest(".copy") && current && !current.notice) {
      copyCard(current.card);
      lastTap = null;
      return;
    }
    const { clientX: x, clientY: y } = e.changedTouches[0];
    if (lastTap && now - lastTap.t < 350 && Math.hypot(x - lastTap.x, y - lastTap.y) < 50) {
      lastTap = null;
      toggleFavorite(x, y);
    } else {
      lastTap = { t: now, x, y };
    }
    if (current) setTransform(current.el, "0", true);
    return;
  }
  const fast = Math.abs(dy) / (performance.now() - t0) > FLICK && Math.abs(dy) > 20;
  if (dy < 0) {
    if (-dy > SWIPE || fast) { dismissHint(); next(); }
    else if (current) setTransform(current.el, "0", true);
  } else if (dy > 0) {
    if (dy > SWIPE || fast) { dismissHint(); openSheet(); } else closeSheet();
  }
}, { passive: false });

// Mouse double click (computers). Ignored right after a touch, which has its own double-tap handling.
stage.addEventListener("dblclick", e => {
  if (performance.now() - lastTouchEnd > 800) toggleFavorite(e.clientX, e.clientY);
});

// Mouse wheel and keys, for testing on a computer.
let wheelLock = 0;
stage.addEventListener("wheel", e => {
  const card = current && current.el.querySelector(".card");
  if (card && card.scrollHeight > card.clientHeight + 2) {
    const atTop = card.scrollTop <= 0, atBottom = card.scrollTop + card.clientHeight >= card.scrollHeight - 1;
    if ((e.deltaY > 0 && !atBottom) || (e.deltaY < 0 && !atTop)) return;   // let the card scroll
  }
  e.preventDefault();
  if (Date.now() < wheelLock || Math.abs(e.deltaY) < 30) return;
  wheelLock = Date.now() + 700;
  if (e.deltaY > 0) next(); else openSheet();
}, { passive: false });

document.addEventListener("keydown", e => {
  if (e.target.closest && e.target.closest("textarea, input")) return;
  if (chatEl.classList.contains("open")) { if (e.key === "Escape") closeChat(); return; }
  if (e.key === "ArrowLeft") { e.preventDefault(); openChat(); return; }
  if (sheet.classList.contains("open")) { if (e.key === "Escape" || e.key === "ArrowDown") closeSheet(); return; }
  if (["ArrowDown", " ", "j", "PageDown"].includes(e.key)) { e.preventDefault(); next(); }
  if (["ArrowUp", "k", "PageUp"].includes(e.key)) { e.preventDefault(); openSheet(); }
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) sendView();
  else if (current && current.sent) { current.sent = false; current.since = performance.now(); }
});

// ---------- topic list ----------

function setSheet(y, animate) {
  sheet.classList.toggle("animate", !!animate);
  sheet.style.transform = y || "";
}

function openSheet() {
  setSheet("", true);
  sheet.classList.add("open");
  sheet.setAttribute("aria-hidden", "false");
  sheetBody.scrollTop = 0;
  renderPanel();
}

function renderPanel() {
  const favs = !document.getElementById("favPanel").hidden;
  (favs ? renderFavorites() : renderTopics()).catch(e => toast(e.message));
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    for (const t of document.querySelectorAll(".tab")) {
      t.classList.toggle("on", t === tab);
      document.getElementById(t.dataset.panel).hidden = t !== tab;
    }
    sheetBody.scrollTop = 0;
    renderPanel();
  });
}

function closeSheet() {
  setSheet("", true);
  sheet.classList.remove("open");
  sheet.setAttribute("aria-hidden", "true");
  document.activeElement && document.activeElement.blur();
}

document.getElementById("closeTopics").addEventListener("click", closeSheet);

// Swipe up on the bottom bar, or on the list once it is scrolled to the end, closes the sheet.
let sheetTouch = null;
sheet.addEventListener("touchstart", e => {
  const onFoot = !!e.target.closest(".sheet-foot");
  const atBottom = sheetBody.scrollTop + sheetBody.clientHeight >= sheetBody.scrollHeight - 2;
  sheetTouch = (onFoot || atBottom) && !e.target.closest("textarea") ? { y0: e.touches[0].clientY, onFoot } : null;
}, { passive: true });
sheet.addEventListener("touchmove", e => {
  if (!sheetTouch) return;
  const dy = e.touches[0].clientY - sheetTouch.y0;
  if (dy < 0 && sheetTouch.onFoot) { e.preventDefault(); setSheet(`translateY(${dy}px)`, false); }
}, { passive: false });
sheet.addEventListener("touchend", e => {
  if (!sheetTouch) return;
  const dy = e.changedTouches[0].clientY - sheetTouch.y0;
  sheetTouch = null;
  if (dy < -SWIPE) closeSheet(); else setSheet("", true);
});

async function renderTopics() {
  const [{ themes }, st] = await Promise.all([api("/api/topics"), api("/api/status").catch(() => null)]);
  const list = document.getElementById("themeList");
  // Only news topics are listed: they are the ones searched for again and again. Series and the weather stay in the feed.
  for (const th of themes) th.topics = th.topics.filter(t => t.kind === "news");
  list.replaceChildren(...themes.filter(th => th.topics.length).map(th =>
    h("div", { class: "theme" },
      h("h3", {}, th.name),
      th.topics.map(t => {
        const pending = !t.keywords.length;
        const about = `搜索：${t.keywords.join("、")}`;
        const note = pending ? "等下次更新推流时整理" : `${about} · ${t.cards} 张卡片`;
        const del = h("button", { class: "del" }, "删除");
        del.addEventListener("click", async () => {
          if (!del.classList.contains("confirm")) {   // first tap arms, second tap deletes
            del.classList.add("confirm");
            del.textContent = "确认删除";
            setTimeout(() => { del.classList.remove("confirm"); del.textContent = "删除"; }, 3000);
            return;
          }
          await api(`/api/topics/${t.id}`, { method: "DELETE" });
          toast(`已删除「${t.name}」`);
          renderTopics();
        });
        return h("div", { class: "topic" }, h("div", { class: "name" }, h("b", {}, t.name), h("small", {}, note)), del);
      }))));
  if (st) {
    const s = st.stats;
    document.getElementById("statusLine").textContent =
      `还有 ${s.unseen_cards || 0} 张没看过的卡片 · ${(s.ready || 0) + (s.new || 0)} 篇文章等着写成卡片` +
      (st.google_blocked ? "\n⚠ Google 要求验证：用远程桌面连上 Surface，在 AI 浏览器的 Chrome 窗口里点一下验证码。点好之前暂停采集新闻。" : "");
  }
}

document.getElementById("addTopic").addEventListener("submit", async ev => {
  ev.preventDefault();
  const ta = document.getElementById("topicDesc");
  const btn = ev.target.querySelector("button");
  const description = ta.value.trim();
  if (!description) return;
  btn.disabled = true;
  try {
    const t = await api("/api/topics", { method: "POST", body: JSON.stringify({ description }) });
    ta.value = "";
    ta.blur();
    toast(t.message);
    renderTopics();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
});

async function renderFavorites() {
  const { cards } = await api("/api/favorites");
  const list = document.getElementById("favList");
  if (!cards.length) {
    list.replaceChildren(h("p", { class: "empty" }, "还没有收藏。刷到喜欢的卡片，双击就能收藏。"));
    return;
  }
  list.replaceChildren(...cards.map(c => {
    const remove = h("button", { class: "del" }, "取消收藏");
    const item = h("article", { class: "fav" },
      c.topic ? h("span", { class: "chip" }, c.topic) : null,
      c.image ? h("img", { class: "card-img small", src: c.image, alt: "" }) : null,
      h("h4", {}, c.title),
      h("ul", {}, c.points.map(p => h("li", {}, p))),
      c.takeaway ? h("p", { class: "take" }, "💡 ", c.takeaway) : null,
      h("div", { class: "fav-foot" }, copyButton(c), remove));
    remove.addEventListener("click", async () => {
      await postEvent(c.id, "clear");
      if (current && current.card && current.card.id === c.id) {
        current.card.reaction = 0;
        current.el.querySelector(".card").classList.remove("faved");
      }
      item.remove();
      toast("已取消收藏");
      if (!list.children.length) renderFavorites();
    });
    return item;
  }));
}

// ---------- start ----------

if (!store("tuiliu-hint")) hintEl.hidden = false;
next();

// A faint line at the bottom: when the newest card was written.
async function showLastPush() {
  try {
    const t = (await api("/api/status")).last_card;
    if (!t) return;
    const d = new Date(t * 1000), pad = n => String(n).padStart(2, "0");
    document.getElementById("lastPush").textContent =
      `上次推流 ${d.getMonth() + 1}月${d.getDate()}日 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch {}
}
showLastPush();
setInterval(showLastPush, 10 * 60 * 1000);

// ---------- chat ----------

const chatEl = document.getElementById("chat");
const chatLog = document.getElementById("chatLog");
const chatInput = document.getElementById("chatInput");
const chatCardEl = document.getElementById("chatCard");
let chatLoaded = false;
let chatCard = null;     // the card the chat was opened from; sent along with the next message
let chatBusy = false;

// Replies use a little Markdown: **bold** is shown bold, everything else as plain text (line breaks kept).
function richText(text) {
  return text.split(/\*\*(.+?)\*\*/g).map((part, i) => i % 2 ? h("b", {}, part) : part);
}

function addMsg(role, text, extra = "") {
  const el = h("div", { class: `msg ${role} ${extra}`.trim() }, ...(role === "assistant" ? richText(text) : [text]));
  chatLog.append(el);
  chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}

async function loadChat() {
  const { messages, has_key } = await api("/api/chat");
  chatLog.replaceChildren();
  if (!messages.length) {
    chatLog.append(h("p", { class: "empty" }, has_key
      ? "在这里告诉我你想跟进什么新闻（比如珀斯房价），或者想做什么专题（比如 30 张面试英语卡片）。我们聊过的内容都会留着，下次接着聊。"
      : "聊天需要 API key，现在还没有。"));
  }
  for (const m of messages) addMsg(m.role, m.display);
  chatLoaded = true;
}

function openChat() {
  chatCard = current && !current.notice ? current.card : null;
  chatCardEl.hidden = !chatCard;
  if (chatCard) chatCardEl.querySelector("span").textContent = `关于这张卡片：${chatCard.title}`;
  chatEl.classList.add("open");
  chatEl.setAttribute("aria-hidden", "false");
  (chatLoaded ? Promise.resolve() : loadChat()).catch(e => toast(e.message))
    .then(() => { chatLog.scrollTop = chatLog.scrollHeight; });
}

function closeChat() {
  chatEl.classList.remove("open");
  chatEl.setAttribute("aria-hidden", "true");
  chatInput.blur();
}

document.getElementById("closeChat").addEventListener("click", closeChat);
chatCardEl.querySelector("button").addEventListener("click", () => { chatCard = null; chatCardEl.hidden = true; });

chatInput.addEventListener("input", () => {
  chatInput.style.height = "auto";
  chatInput.style.height = `${chatInput.scrollHeight}px`;
});
// On a computer Enter sends (Shift+Enter for a new line); not while an input method is composing.
chatInput.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !matchMedia("(pointer: coarse)").matches) {
    e.preventDefault();
    document.getElementById("chatForm").requestSubmit();
  }
});

document.getElementById("chatForm").addEventListener("submit", async ev => {
  ev.preventDefault();
  const message = chatInput.value.trim();
  if (!message || chatBusy) return;
  chatBusy = true;
  const btn = ev.target.querySelector("button");
  btn.disabled = true;
  chatLog.querySelector(".empty")?.remove();
  addMsg("user", message);
  chatInput.value = "";
  chatInput.style.height = "auto";
  const pending = addMsg("assistant", "思考中…", "pending");
  const card_id = chatCard ? chatCard.id : null;
  chatCard = null;
  chatCardEl.hidden = true;
  try {
    const { replies } = await api("/api/chat", { method: "POST", body: JSON.stringify({ message, card_id }) });
    pending.remove();
    for (const r of replies) addMsg("assistant", r);
    if (!replies.length) addMsg("error", "没有收到回复，再试一次？");
  } catch (e) {
    pending.remove();
    addMsg("error", e.message);
  } finally {
    chatBusy = false;
    btn.disabled = false;
  }
});

// Swipe right on the chat (not from the screen edge) goes back to the cards.
let chatTouch = null;
chatEl.addEventListener("touchstart", e => {
  const { clientX: x, clientY: y } = e.touches[0];
  chatTouch = x > EDGE ? { x0: x, y0: y } : null;
}, { passive: true });
chatEl.addEventListener("touchend", e => {
  if (!chatTouch) return;
  const dx = e.changedTouches[0].clientX - chatTouch.x0, dy = e.changedTouches[0].clientY - chatTouch.y0;
  chatTouch = null;
  if (dx > SWIPE && dx > 1.5 * Math.abs(dy)) closeChat();
});
