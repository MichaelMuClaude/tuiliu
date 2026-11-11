"use strict";

// One card at a time. Swipe up = next card (no going back). Swipe down = open the topic list.
// Swipe left or right = this card goes away for good (the same in every app: a word is known, an idea is deleted,
// a news card never comes back), with 撤销 in the toast. Swipe up = next; borrowed cards come round again later.
// This page's version (app.js?v=N); the feed says the server's, and an old page reloads itself.
const APP_VERSION = (document.currentScript && new URL(document.currentScript.src).searchParams.get("v")) || "";
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

function toast(msg, action) {   // action = [label, fn]: a button in the toast, e.g. 撤销
  toastEl.replaceChildren(msg);
  if (action) {
    const b = h("button", { type: "button" }, action[0]);
    b.addEventListener("click", () => { toastEl.classList.remove("show"); action[1](); });
    toastEl.append(b);
  }
  toastEl.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => toastEl.classList.remove("show"), action ? 4000 : 2400);
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

// 📌: put the card on 探索清单. The server writes the line and picks the day it comes up; it can take a few seconds.
async function exploreCard(c, b) {
  if (b.disabled) return;
  b.disabled = true;
  b.textContent = "加入中…";
  try {
    const d = new Date(), today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const r = await api(`/api/cards/${c.id}/explore`, { method: "POST", body: JSON.stringify({ today }) });
    const when = r.due ? `，${+r.due.slice(5, 7)}月${+r.due.slice(8)}日出现` : "";
    toast(r.already ? `这张已经在探索清单里了${when}` : `已加入探索清单${when}`);
    b.textContent = "📌 已加入";
  } catch (e) {
    toast(e.message);
    b.textContent = "📌 加入探索清单";
    b.disabled = false;
  }
}

function exploreButton(c) {
  const b = h("button", { class: "copy explore", type: "button" }, "📌 加入探索清单");
  b.addEventListener("click", () => exploreCard(c, b));   // mouse; taps are handled in touchend
  return b;
}

// A word card from GlanceCards: its picture already carries the word, phonetics and Chinese.
function wordSlide(c) {
  const known = h("button", { class: "copy known", type: "button" }, "✓ 认识了");
  known.addEventListener("click", () => knownWord(c, known));   // mouse; taps are handled in touchend
  const unknown = h("button", { class: "copy unknown", type: "button" }, c.studying ? "✗ 在不认识里" : "✗ 不认识");
  if (c.studying) unknown.disabled = true;
  unknown.addEventListener("click", () => studyWord(c, unknown));
  return h("section", { class: "slide" },
    h("article", { class: "card word-card" },
      h("span", { class: "chip" }, "词卡"),
      h("img", { class: "card-img word", src: c.image, alt: c.title }),
      h("div", { class: "card-foot" },
        unknown, known,
        c.lookup ? h("a", { class: "copy", href: c.lookup, target: "_blank", rel: "noopener" }, "查词典") : null)));
}

async function knownWord(c, b) {
  if (b.disabled) return;
  b.disabled = true;
  try {
    await api("/api/word/known", { method: "POST", body: JSON.stringify({ file: c.file }) });
    b.textContent = "✓ 已放进认识的词";
    toast("这个词以后不会再出现了");
  } catch {
    b.disabled = false;
    toast("没记上，词卡服务器没响应");
  }
}

async function studyWord(c, b) {
  if (b.disabled) return;
  b.disabled = true;
  try {
    await api("/api/word/study", { method: "POST", body: JSON.stringify({ file: c.file }) });
    c.studying = true;
    b.textContent = "✗ 在不认识里";
    toast("记下了，这个词以后会多出现几次");
  } catch {
    b.disabled = false;
    toast("没记上，词卡服务器没响应");
  }
}

// Word cards come from GlanceCards, ideas from 探索清单, photos from 相框 and dishes from 御膳房: those apps keep
// them, so no feed events.
const borrowed = c => !!(c && (c.word || c.idea || c.photo || c.dish));
const localDay = () => { const d = new Date(), p = n => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`; };

// A sideways swipe: the card flies off, the next one comes in, and the card is put away where it belongs.
async function swipeAway(dir) {
  if (busy || !current || current.notice || !current.card) return;
  const c = current.card;
  current.el.classList.add("animate");
  current.el.style.transform = `translateX(${dir * 130}%) rotate(${dir * 12}deg)`;
  current.gone = true;
  next();
  try {
    if (c.word) {
      await api("/api/word/known", { method: "POST", body: JSON.stringify({ file: c.file }) });
      toast(`认识了：${c.title}`, ["撤销", () => undo("/api/word/undo", {}, "这个词还会再出现")]);
    } else if (c.dish) {
      await api("/api/dish/delete", { method: "POST", body: JSON.stringify({ id: c.dish_id }) });
      toast(`从御膳房菜单拿掉了：${c.title}`, ["撤销", () => undo("/api/dish/restore", { id: c.dish_id }, "放回菜单了")]);
    } else if (c.photo) {
      await api("/api/photo/trash", { method: "POST", body: JSON.stringify({ path: c.path }) });
      toast("照片移到回收站了", ["撤销", () => undo("/api/photo/undo", {}, "照片放回去了")]);
    } else if (c.idea) {
      await api("/api/idea/delete", { method: "POST", body: JSON.stringify({ id: c.idea_id, today: localDay() }) });
      toast("从探索清单删掉了", ["撤销", () => undo("/api/idea/restore", { id: c.idea_id }, "放回探索清单了")]);
    } else {
      const before = c.reaction;
      postEvent(c.id, "dislike");
      toast("这张不会再出现", ["撤销", () => { postEvent(c.id, before === 1 ? "like" : "clear"); toast("撤销了"); }]);
    }
  } catch {
    toast(c.word ? "没记上，词卡服务器没响应" : c.photo ? "没删掉，相框没响应" : c.dish ? "没删掉，御膳房没响应" : "没删掉，探索清单没响应");
  }
}

async function undo(path, body, done) {
  try {
    await api(path, { method: "POST", body: JSON.stringify(body) });
    toast("撤销了，" + done);
  } catch {
    toast("没撤销成功");
  }
}

// A dish from 御膳房: photo, name, how to make it and what goes in; double tap = 就吃这个.
function dishSlide(c) {
  return h("section", { class: "slide" },
    h("article", { class: "card dish-card" },
      h("span", { class: "chip" }, ["御膳房", ...c.chips].join(" · ")),
      c.image ? h("img", { class: "card-img dish", src: c.image, alt: "" }) : null,
      h("h1", {}, c.title),
      h("ul", { class: "points" }, c.points.map(p => h("li", {}, p))),
      h("p", { class: "dish-eaten" }, c.eaten, " · 双击 = 就吃这个"),
      h("div", { class: "stamp" }, "就它了")));
}

// A photo from 相框: the picture alone, with its folder.
function photoSlide(c) {
  return h("section", { class: "slide" },
    h("article", { class: "card photo-card" + (c.reaction === 1 ? " faved" : "") },
      heart("fav-mark"),
      h("span", { class: "chip" }, ["相框", c.folder].filter(Boolean).join(" · ")),
      h("img", { class: "card-img photo", src: c.image, alt: "" })));
}

// An idea card from 探索清单: its text (and picture, if it has one) and the day it is for.
function ideaSlide(c) {
  return h("section", { class: "slide" },
    h("article", { class: "card idea-card" + (c.reaction === 1 ? " faved" : "") },
      heart("fav-mark"),
      h("span", { class: "chip" }, ["探索清单", c.when].filter(Boolean).join(" · ")),
      c.image ? h("img", { class: "card-img", src: c.image, alt: "" }) : null,
      h("p", { class: "idea-text" }, c.title)));
}

function cardSlide(c) {
  if (c.word) return wordSlide(c);
  if (c.idea) return ideaSlide(c);
  if (c.photo) return photoSlide(c);
  if (c.dish) return dishSlide(c);
  const label = [c.review ? "❤ 收藏回顾" : "", c.theme, c.topic].filter(Boolean).join(" · ");
  return h("section", { class: "slide" },
    h("article", { class: "card" + (c.reaction === 1 ? " faved" : "") },
      heart("fav-mark"),
      label ? h("span", { class: "chip" }, label) : null,
      c.image ? h("img", { class: "card-img" + (c.liya ? " liya" : ""), src: c.image, alt: "" }) : null,
      h("h1", {}, c.title),
      h("ul", { class: "points" }, c.points.map(p => h("li", {}, p))),
      c.takeaway ? h("p", { class: "takeaway" }, "💡 ", c.takeaway) : null,
      h("div", { class: "card-foot" }, exploreButton(c), copyButton(c))));
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
    loading = api(`/api/feed?n=8&exclude=${[...seen].filter(id => typeof id === "number").join(",")}`)
      .then(data => {
        if (data.app && APP_VERSION && data.app !== APP_VERSION) { location.reload(); return; }   // updated meanwhile
        for (const c of data.cards) if (!seen.has(c.id)) { seen.add(c.id); queue.push(c); }
      })
      .catch(() => {})
      .finally(() => { loading = null; });
  }
  return loading;
}

function sendView() {
  if (!current || current.notice || current.sent || borrowed(current.card)) return;
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
  if (!current || current.notice || current.card.word) return;
  const c = current.card;
  if (c.photo) return likePhoto(c, x, y);
  if (c.dish) return eatDish(c);
  const on = c.reaction !== 1;
  c.reaction = on ? 1 : 0;
  current.el.querySelector(".card").classList.toggle("faved", on);
  if (c.idea) {   // hearted in 探索清单 itself, where it then comes up more often
    api("/api/idea/fav", { method: "POST", body: JSON.stringify({ id: c.idea_id, fav: on }) }).catch(() => toast("没收藏上"));
  } else postEvent(c.id, on ? "like" : "clear");
  const burst = heart("burst" + (on ? "" : " off"));
  burst.style.left = `${x}px`;
  burst.style.top = `${y}px`;
  document.body.append(burst);
  setTimeout(() => burst.remove(), 800);
  toast(c.idea ? (on ? "在探索清单里收藏了，以后多出现" : "已取消收藏") : on ? "已收藏，下滑可以在「收藏」里找到" : "已取消收藏");
}

// Double tap on a dish: 就吃这个, logged in 御膳房 (so it comes up less for a few days); 撤销 puts the old date back.
function eatDish(c) {
  const card = current.el.querySelector(".card");
  card.classList.remove("stamped"); void card.offsetWidth; card.classList.add("stamped");
  const before = c.last_eaten ?? null;
  api("/api/dish/eaten", { method: "POST", body: JSON.stringify({ id: c.dish_id }) })
    .then(() => toast(`好，就吃${c.title}`, ["撤销", () => undo("/api/dish/eaten", { id: c.dish_id, undo_eaten: true, last_eaten: before }, "没记这一顿")]))
    .catch(() => toast("没记上，御膳房没响应"));
}

// Double tap on a photo: 相框 moves it into 收藏 (once; 撤销 takes it back out).
function likePhoto(c, x, y) {
  if (c.reaction === 1) { toast("已经在收藏里了"); return; }
  c.reaction = 1;
  current.el.querySelector(".card").classList.add("faved");
  const burst = heart("burst");
  burst.style.left = `${x}px`;
  burst.style.top = `${y}px`;
  document.body.append(burst);
  setTimeout(() => burst.remove(), 800);
  api("/api/photo/like", { method: "POST", body: JSON.stringify({ path: c.path }) })
    .then(r => { if (r.path) c.path = r.path; })   // it now lives in 收藏
    .then(() => toast("收藏了，移到「收藏」文件夹", ["撤销", () => undo("/api/photo/undo", {}, "照片放回原来的文件夹了")]))
    .catch(() => toast("没收藏上，相框没响应"));
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
      if (!old.gone) setTransform(old.el, "-100%", true);   // a word card swiped sideways has already left
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
  // Decide once per touch whether it is a sideways swipe (word cards) or an up/down one (cards, topics).
  if (!touch.axis && Math.max(Math.abs(x - touch.x0), Math.abs(y - touch.y0)) > 10)
    touch.axis = Math.abs(x - touch.x0) > 1.2 * Math.abs(y - touch.y0) ? "x" : "y";
  if (touch.axis === "x") {
    touch.dx = x - touch.x0;
    touch.moved = Math.max(touch.moved, Math.abs(touch.dx));
    if (current && current.card)
      current.el.style.transform = `translateX(${touch.dx}px) rotate(${touch.dx / 25}deg)`;
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
    // Ignore swipes that start at the screen edge: that is the phone's own "back" gesture.
    if (Math.abs(dx) > SWIPE && x0 > EDGE && x0 < innerWidth - EDGE && current && current.card) {
      dismissHint();
      swipeAway(dx > 0 ? 1 : -1);
    } else if (current) setTransform(current.el, "0", true);
    return;
  }
  const now = performance.now();
  lastTouchEnd = now;
  if (moved < 12 && now - t0 < 300) {             // a quick tap; two in a row is a double tap
    e.preventDefault();                           // no emulated mouse clicks / dblclick afterwards
    const explore = e.target.closest && e.target.closest(".explore");
    if (explore && current && !current.notice) {
      exploreCard(current.card, explore);
      lastTap = null;
      return;
    }
    const unknown = e.target.closest && e.target.closest(".unknown");
    if (unknown && current && current.card.word) {
      studyWord(current.card, unknown);
      lastTap = null;
      return;
    }
    const known = e.target.closest && e.target.closest(".known");
    if (known && current && current.card.word) {
      knownWord(current.card, known);
      lastTap = null;
      return;
    }
    if (e.target.closest && e.target.closest("a.copy")) {   // the dictionary link opens as a normal link
      window.open(e.target.closest("a.copy").href, "_blank", "noopener");
      lastTap = null;
      return;
    }
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
  if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && current && current.card) {
    e.preventDefault(); swipeAway(e.key === "ArrowRight" ? 1 : -1); return;
  }
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
      h("div", { class: "fav-foot" }, exploreButton(c), copyButton(c), remove));
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

