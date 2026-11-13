/* AI 剪辑工具 - 前端逻辑 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  path: null,
  info: null,
  analysis: null,
  plan: null,        // AI 粗剪的决策结果
  suggestions: [],
  job: null,
  output: null,
  kit: null,         // 已导出的精调包
  busy: false,
  selected: null,   // 当前选中的剪切区间 id
  rows: null,       // id -> 建议行里显示时间的元素，拖拽时局部更新用
  strength: "standard",
  typingEl: null,   // 对话里的「正在想」气泡
  fx: null,         // 效果面板：字幕 / BGM / 变速 / 转场
};

// 记住按钮原始文案（含图标），任务结束后要还原
const LABEL_ANALYZE = "仅分析";
let LABEL_RENDER = "";
let LABEL_AUTO = "";
let LABEL_KIT = "";
const LABEL_CHAT = "按需求剪";

/* ---------------- 基础工具 ---------------- */
function pad(n) { return String(Math.floor(n)).padStart(2, "0"); }

function fmt(sec) {
  if (sec == null || isNaN(sec)) return "--:--";
  sec = Math.max(0, sec);
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${pad(m)}:${pad(s)}`;
}

function fmtPrecise(sec) {
  if (sec == null || isNaN(sec)) return "--";
  return `${fmt(sec)}.${String(Math.floor((sec % 1) * 10))}`;
}

async function api(url, opts = {}) {
  const opts2 = {
    headers: { "Content-Type": "application/json" },
    ...opts,
  };
  let res;
  try {
    res = await fetch(url, opts2);
  } catch (e) {
    // 服务刚重启/正在退出时可能出现瞬时失败，等一下重试一次
    await new Promise((r) => setTimeout(r, 900));
    try {
      res = await fetch(url, opts2);
    } catch (e2) {
      return { ok: false, error: "连不上本地服务，请重新打开应用" };
    }
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = { ok: false, error: "服务返回异常" }; }
  return data;
}

let toastTimer = null;
function toast(msg, kind = "info") {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    Object.assign(el.style, {
      position: "fixed", left: "50%", top: "66px", transform: "translateX(-50%)",
      padding: "9px 16px", borderRadius: "10px", fontSize: "12.5px", zIndex: 99,
      background: "rgba(20,22,30,.92)", color: "#fff", pointerEvents: "none",
      transition: "opacity .2s", opacity: "0", maxWidth: "70vw",
      boxShadow: "0 6px 20px rgba(12,16,28,.22)",
    });
    document.body.appendChild(el);
  }
  el.textContent = msg;
  if (kind === "error") el.style.background = "rgba(190,50,70,.95)";
  else el.style.background = "rgba(20,22,30,.92)";
  el.style.opacity = "1";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.opacity = "0"; }, 2600);
}

function setStatus(text, kind = "") {
  $("#statusText").textContent = text;
  $("#statusDot").className = "status-dot" + (kind ? " " + kind : "");
}

function setProgress(percent) {
  const bar = $("#topProgress");
  const i = bar.querySelector("i");
  if (percent == null) { bar.classList.remove("active"); i.style.width = "0%"; return; }
  bar.classList.remove("active");
  i.style.width = Math.max(0, Math.min(100, percent)) + "%";
}

function setBusy(busy) {
  state.busy = busy;
  const running = state.job ? state.job.kind : null;
  $("#btnAnalyze").disabled = !state.path || (busy && running !== "analyze");
  // 渲染进行中时按钮变成「取消」，要允许点击
  $("#btnRender").disabled = (busy && running !== "render") || !hasCuts();
  $("#btnAuto").disabled = !state.path || (busy && running !== "autocut");
  $("#btnPick").disabled = busy;
  $("#strength").classList.toggle("locked", busy);
  $$("#strength button").forEach((b) => { b.disabled = busy; });
  const prof = $("#btnProfile");
  if (prof) prof.disabled = busy;

  // 对话：任务跑着的时候变成「取消」
  const send = $("#btnChatSend");
  if (send) {
    send.disabled = !state.path || (busy && running !== "direct");
    send.textContent = (busy && running === "direct") ? "取消" : LABEL_CHAT;
  }
}

/* ---------------- 选视频 ---------------- */
async function pickVideo() {
  setStatus("等待选择文件…", "busy");
  const r = await api("/api/pick", { method: "POST" });
  if (!r.ok) {
    if (r.cancelled) setStatus("就绪");
    else { setStatus("打开文件对话框失败", "err"); toast(r.error || "打开对话框失败", "error"); }
    return;
  }
  await loadVideo(r.path);
}

async function loadVideo(path) {
  state.path = path;
  state.analysis = null;
  state.plan = null;
  state.suggestions = [];
  state.output = null;
  state.kit = null;
  $("#outputBox").classList.add("hidden");
  $("#estBox").classList.add("hidden");
  $("#sugCount").classList.add("hidden");
  $("#decideBlock").classList.add("hidden");
  $("#decideList").classList.remove("open");
  $("#decideToggle").classList.remove("open");
  setStatus("读取视频信息…", "busy");

  const q = encodeURIComponent(path);
  const [infoRes, strip] = await Promise.all([
    api(`/api/info?path=${q}`),
    api(`/api/filmstrip?path=${q}&count=14`),
  ]);

  if (!infoRes.ok) {
    setStatus("读取失败", "err");
    toast(infoRes.error || "无法读取该视频", "error");
    return;
  }

  state.info = infoRes.info;

  // 信息展示
  $("#fileName").textContent = infoRes.info.name;
  $("#fileName").classList.remove("hidden");
  if ($("#chatLog") && $("#chatLog").querySelector(".msg")) {
    pushDivider("换成了 " + infoRes.info.name);
  }
  $("#sDuration").textContent = fmt(infoRes.info.duration);
  $("#sRes").textContent = `${infoRes.info.width}×${infoRes.info.height}`;
  $("#sFps").textContent = infoRes.info.fps.toFixed(2);
  $("#sSize").textContent = infoRes.info.size_mb + " MB";
  $("#durTime").textContent = fmt(infoRes.info.duration);

  // 视频源 + 首帧封面
  const video = $("#video");
  if (strip.frames && strip.frames[0]) video.poster = strip.frames[0];
  video.src = `/api/video?path=${q}`;
  video.load();
  $("#playerWrap").classList.add("has-video");
  $("#btnPlay").disabled = false;

  // 胶片条
  const fs = $("#filmstrip");
  fs.innerHTML = "";
  (strip.frames || []).forEach((src) => {
    const img = document.createElement("img");
    if (src) img.src = src;
    else img.className = "placeholder";
    fs.appendChild(img);
  });
  $("#timelineEmpty").classList.add("hidden");

  drawRuler(infoRes.info.duration);
  drawTimeline();

  $("#btnAnalyze").disabled = false;
  $("#btnAuto").disabled = false;
  $("#btnChatSend").disabled = false;
  $("#btnExport").disabled = true;
  $("#btnRender").disabled = true;
  $("#emptyHint").classList.remove("hidden");
  setStatus("视频已载入，说说想怎么剪", "done");
}

/* ---------------- 时间轴 ---------------- */
const MIN_CUT = 0.2;                                   // 最短可剪区间（秒）
const TYPE_CLASS = {
  silence: "silent", dup: "dup", static: "static", blank: "blank",
  manual: "manual", beat: "beat", filler: "filler", retake: "retake",
};
const TYPE_TEXT = {
  silence: "静音", dup: "重复", static: "静止", blank: "黑场",
  manual: "手动", beat: "卡点", filler: "口头禅", retake: "重说",
};
const TAG_CLASS = {
  scene: "tag-scene", silence: "tag-silent", dup: "tag-dup",
  static: "tag-static", blank: "tag-blank", manual: "tag-manual", beat: "tag-beat",
  filler: "tag-filler", retake: "tag-retake",
};

function tagOf(type) {
  return [TAG_CLASS[type] || "tag-manual", TYPE_TEXT[type] || (type === "scene" ? "场景" : "手动")];
}

let drag = null;                                       // 当前拖拽状态
let seq = 0;
const newId = () => "c" + (++seq);

function durationOf() {
  return (state.analysis && state.analysis.duration) ||
    (state.info && state.info.duration) || 0;
}

function round1(t) { return Math.round(t * 10) / 10; }

function sortSuggestions() {
  state.suggestions.sort((a, b) => a.start - b.start);
}

function timeAt(clientX, el) {
  const node = el || $("#timeline");
  const r = node.getBoundingClientRect();
  if (!r.width) return 0;
  const ratio = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
  return ratio * durationOf();
}

function drawRuler(duration) {
  const ruler = $("#ruler");
  ruler.innerHTML = "";
  if (!duration) return;
  const n = 5;
  for (let i = 0; i < n; i++) {
    const ratio = i / (n - 1);
    const span = document.createElement("span");
    span.textContent = fmt(duration * ratio);
    span.style.left = (ratio * 100) + "%";
    ruler.appendChild(span);
  }
}

function drawTimeline() {
  const overlay = $("#overlay");
  overlay.innerHTML = "";
  const dur = durationOf();
  if (!dur) return;

  // 场景切点（只作参考，不可拖）
  if (state.analysis) {
    (state.analysis.scenes || []).forEach((sc) => {
      const m = document.createElement("div");
      m.className = "cutmark";
      m.style.left = (sc.start / dur) * 100 + "%";
      m.title = `场景切点 ${fmtPrecise(sc.start)}`;
      overlay.appendChild(m);
    });
  }

  // 剪切区间（静音 / 重复 / 手动），全部可拖
  state.suggestions
    .filter((s) => s.type !== "scene" && s.end != null && s.end > s.start)
    .forEach((item) => {
      const b = document.createElement("div");
      b.className = "band " + (TYPE_CLASS[item.type] || "manual");
      if (!item.on) b.classList.add("dim");
      if (state.selected === item.id) b.classList.add("sel");
      b.dataset.id = item.id;
      b.style.left = (item.start / dur) * 100 + "%";
      b.style.width = Math.max(0.35, ((item.end - item.start) / dur) * 100) + "%";
      b.title = `${TYPE_TEXT[item.type] || ""} ${fmtPrecise(item.start)} → ${fmtPrecise(item.end)}`;
      const gl = document.createElement("i");
      gl.className = "grip l";
      gl.title = "拖动调整起点";
      const gr = document.createElement("i");
      gr.className = "grip r";
      gr.title = "拖动调整终点";
      b.append(gl, gr);
      overlay.appendChild(b);
    });

  updateEstimate();
}

function itemAt(id) {
  return state.suggestions.find((s) => s.id === id) || null;
}

/* 播放头 / 时间码同步（拖拽时不触发播放） */
function scrubTo(t) {
  const dur = durationOf();
  if (!dur) return;
  t = Math.max(0, Math.min(dur - 0.05, t));
  const v = $("#video");
  if (v.src) { try { v.currentTime = t; } catch (e) { /* 元数据还没就绪 */ } }
  $("#playhead").style.left = (t / dur) * 100 + "%";
  $("#curTime").textContent = fmt(t);
}

function refreshAfterEdit() {
  sortSuggestions();
  renderSuggestions();
  drawTimeline();
  updateEstimate();
  syncToggles();
  $("#btnRender").disabled = state.busy || !hasCuts();
  const n = state.suggestions.filter((s) => s.type !== "scene").length;
  $("#sugCount").textContent = n;
  $("#sugCount").classList.toggle("hidden", n === 0);
  if (n > 0) $("#emptyHint").classList.add("hidden");
  // 区间变了，之前生成的成片就过期了
  if (state.output) {
    state.output = null;
    $("#outputBox").classList.add("hidden");
  }
}

function setHint(text) {
  const el = $("#tlHint");
  if (el) el.innerHTML = text;
}

/* ---- 拖拽交互：薄膜条上拖动 = 新建区间 / 调整区间 ---- */
function bindTimelineDrag() {
  const tl = $("#timeline");
  const ruler = $("#ruler");

  tl.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    if (!durationOf()) return;

    const band = e.target.closest(".band");
    const item = band ? itemAt(band.dataset.id) : null;
    const t = timeAt(e.clientX);

    if (item) {
      const rect = band.getBoundingClientRect();
      const x = e.clientX - rect.left;
      let mode = "move";
      if (e.target.classList.contains("grip")) {
        mode = e.target.classList.contains("l") ? "resize-l" : "resize-r";
      } else if (rect.width > 24 && x <= 9) {
        mode = "resize-l";
      } else if (rect.width > 24 && rect.width - x <= 9) {
        mode = "resize-r";
      }
      state.selected = item.id;
      drag = { mode, item, grabT: t, start0: item.start, end0: item.end, startX: e.clientX, moved: false };
      band.classList.add("dragging");
      drawTimeline();
      e.preventDefault();
      return;
    }

    // 空白处按下：准备拖出新区间
    state.selected = null;
    drag = { mode: "create", anchor: t, startX: e.clientX, moved: false, item: null };
    drawTimeline();
    e.preventDefault();
  });

  // 点标尺 / 拖标尺 = 定位
  ruler.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    if (!durationOf()) return;
    drag = { mode: "scrub", startX: e.clientX, moved: false };
    scrubTo(timeAt(e.clientX, ruler));
    e.preventDefault();
  });

  // 双击区间 = 启用/停用
  tl.addEventListener("dblclick", (e) => {
    const band = e.target.closest(".band");
    if (!band) return;
    const item = itemAt(band.dataset.id);
    if (!item) return;
    item.on = !item.on;
    refreshAfterEdit();
    setStatus(item.on ? "已启用该区间" : "已停用该区间，不会剪掉", "");
  });

  document.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const dur = durationOf();
    if (!dur) return;
    if (Math.abs(e.clientX - drag.startX) > 2) drag.moved = true;

    if (drag.mode === "scrub") {
      scrubTo(timeAt(e.clientX, ruler));
      return;
    }

    const t = Math.max(0, Math.min(dur, timeAt(e.clientX)));

    if (drag.mode === "create") {
      if (!drag.moved) return;
      if (!drag.item) {
        const s = round1(Math.min(drag.anchor, t));
        const en = round1(Math.max(drag.anchor, t));
        const item = {
          id: newId(), type: "manual", start: s, end: Math.max(en, s + MIN_CUT),
          on: true, reason: "手动标出的剪切区间",
        };
        state.suggestions.push(item);
        sortSuggestions();
        drag.item = item;
        drag.mode = t < drag.anchor ? "resize-l" : "resize-r";
        state.selected = item.id;
      }
    }

    const it = drag.item;
    if (!it) return;

    if (drag.mode === "resize-l") {
      it.start = Math.max(0, Math.min(round1(t), it.end - MIN_CUT));
    } else if (drag.mode === "resize-r") {
      it.end = Math.min(dur, Math.max(round1(t), it.start + MIN_CUT));
    } else if (drag.mode === "move") {
      const len = drag.end0 - drag.start0;
      let s = drag.start0 + (t - drag.grabT);
      s = Math.max(0, Math.min(dur - len, s));
      it.start = round1(s);
      it.end = round1(s + len);
    }

    drawTimeline();
    updateEstimate();
    syncRowTime(it);
  });

  document.addEventListener("mouseup", (e) => {
    if (!drag) return;
    const d = drag;
    drag = null;
    const dur = durationOf();

    if (d.mode === "scrub") return;

    // 空白处只是点了一下 = 定位
    if (d.mode === "create" && !d.moved) {
      scrubTo(timeAt(e.clientX));
      return;
    }

    if (d.item) {
      if (d.item.end - d.item.start < MIN_CUT * 1.2) {
        state.suggestions = state.suggestions.filter((s) => s !== d.item);
        state.selected = null;
        toast("区间太短，已取消");
      } else {
        d.item.start = Math.max(0, round1(d.item.start));
        d.item.end = Math.min(dur, round1(d.item.end));
        state.selected = d.item.id;
      }
      refreshAfterEdit();
      const cuts = computeCuts().length;
      setStatus(`已调整剪切区间：${cuts} 处`, "");
    } else {
      refreshAfterEdit();
    }
  });
}

/* 拖拽过程中只更新对应那一行的时间文字，避免整表重绘 */
function syncRowTime(item) {
  const row = state.rows && state.rows.get(item.id);
  if (row) row.textContent = `${fmtPrecise(item.start)} → ${fmtPrecise(item.end)}`;
}

function removeItem(id) {
  const item = itemAt(id);
  if (!item) return;
  state.suggestions = state.suggestions.filter((s) => s !== item);
  state.selected = null;
  refreshAfterEdit();
  setStatus(`已删除「${TYPE_TEXT[item.type] || ""} ${fmtPrecise(item.start)} → ${fmtPrecise(item.end)}」`, "");
}

/* ---------------- AI 粗剪 ---------------- */
async function autocut() {
  if (state.busy || !state.path) return;

  setBusy(true);
  setProgress(0);
  setStatus("AI 正在看片…", "busy");
  $("#btnAuto").innerHTML = "取消";

  const r = await api("/api/autocut", {
    method: "POST",
    body: JSON.stringify({
      path: state.path,
      strength: state.strength,
      // 勾了才转写——这一步比画面分析慢得多
      speech: {
        filler: !!($("#optFiller") || {}).checked,
        retake: !!($("#optRetake") || {}).checked,
      },
      // 效果跟着一起走：字幕要开就会自动转写，一次转写两处用（口头禅 + 字幕）
      effects: fxPayload(),
    }),
  });
  if (!r.ok) {
    finishJob();
    setStatus("启动失败", "err");
    toast(r.error || "AI 粗剪启动失败", "error");
    return;
  }
  state.job = { id: r.job, kind: "autocut" };
  setBusy(true);
  pollJob();
}

/* ---------------- 仅分析 ---------------- */
async function analyze() {
  if (state.busy || !state.path) return;

  setBusy(true);
  setProgress(0);
  setStatus("正在分析视频…", "busy");
  $("#btnAnalyze").textContent = "取消";

  const r = await api("/api/analyze", { method: "POST", body: JSON.stringify({ path: state.path }) });
  if (!r.ok) {
    setBusy(false);
    $("#btnAnalyze").textContent = LABEL_ANALYZE;
    setStatus("分析失败", "err");
    toast(r.error || "分析启动失败", "error");
    return;
  }
  state.job = { id: r.job, kind: "analyze" };
  setBusy(true);   // 拿到 job 后重新算按钮可用性（让「取消」可点）
  pollJob();
}

/* ---------------- 导出精调包 ---------------- */
async function exportKit() {
  if (state.busy || !state.output) return;

  // 永远按「当前时间轴」导出，这样你在界面上改过的区间也会生效
  const keep = computeKeep();
  const plan = {
    keep_ranges: keep,
    decisions: state.suggestions
      .filter((s) => s.end != null)
      .map((s) => ({ type: s.type, start: s.start, end: s.end, saved: s.end - s.start, reason: s.reason })),
  };

  if (!keep.length) { toast("还没有可导出的剪辑结果"); return; }

  setBusy(true);
  setProgress(0);
  setStatus("正在打包精调包…", "busy");

  const r = await api("/api/export-kit", {
    method: "POST",
    body: JSON.stringify({
      path: state.path,
      output: state.output.output,
      analysis: state.analysis || {},
      plan,
      want_clips: true,
      // 字幕按「原片时间」传过去，后端用这次的时间线映射成 字幕.srt
      cues: (state.fx && state.fx.cues) || [],
      speed: (state.fx && state.fx.speed) || 1,
      transition: (state.fx && state.fx.transition && state.fx.transition.on)
        ? ((state.fx.transition.type === "auto") ? 0 : (Number(state.fx.transition.duration) || 0))
        : 0,
    }),
  });
  if (!r.ok) {
    finishJob();
    setStatus("打包失败", "err");
    toast(r.error || "打包失败", "error");
    return;
  }
  state.job = { id: r.job, kind: "kit" };
  setBusy(true);
  pollJob();
}

async function pollJob() {
  if (!state.job) return;
  const r = await api(`/api/job/${state.job.id}`);
  if (!r.ok) { finishJob(); return; }
  const job = r.job;

  setProgress(job.progress || 0);
  if (job.message) setStatus(job.message, "busy");

  if (job.status === "running") {
    setTimeout(pollJob, 400);
    return;
  }

  if (job.status === "done") {
    if (job.kind === "analyze") onAnalysisDone(job.result);
    else if (job.kind === "autocut") onAutocutDone(job.result);
    else if (job.kind === "direct") onDirectDone(job.result);
    else if (job.kind === "kit") onKitDone(job.result);
    else onRenderDone(job.result);
  } else if (job.status === "cancelled") {
    setStatus("已取消", "");
    setProgress(null);
    dropTyping();
    if (job.kind === "direct") pushMsg("ai", "这次先停下了，改好需求再发我一次就行。");
  } else {
    const msg = job.error ? "出错：" + job.error : "失败";
    setStatus(msg, "err");
    setProgress(null);
    toast(job.error || "任务失败", "error");
    if (job.kind === "direct") {
      const r = job.result || {};
      if (r.reply) pushMsg("ai", r.reply);
      dropTyping();
      if (r.traceback) console.error(r.traceback);
    }
    if (job.result && job.result.traceback) console.error(job.result.traceback);
  }
  finishJob();
}

function finishJob() {
  state.job = null;
  dropTyping();
  setBusy(false);
  $("#btnAnalyze").textContent = LABEL_ANALYZE;
  if (LABEL_RENDER) $("#btnRender").innerHTML = LABEL_RENDER;
  if (LABEL_AUTO) $("#btnAuto").innerHTML = LABEL_AUTO;
  setTimeout(() => setProgress(null), 900);
}

async function cancelJob() {
  if (!state.job) return;
  await api(`/api/job/${state.job.id}/cancel`, { method: "POST" });
  setStatus("正在取消…", "busy");
}

/* ---------------- 结果展示 ---------------- */
function fillCounts(an) {
  $("#cScene").textContent = (an.scenes || []).length;
  $("#cSilent").textContent = (an.silent || []).length;
  $("#cDup").textContent = (an.redundant || []).length;
  $("#cStatic").textContent = (an.static || []).length;
  $("#cBlank").textContent = (an.blank || []).length;
}

function renderDecisions(plan) {
  const box = $("#decideList");
  if (!box) return;
  const list = (plan && plan.decisions) || [];
  $("#decideBlock").classList.toggle("hidden", list.length === 0);

  // 用合并后的真实剪掉量，不能把各条 saved 直接相加（区间会重叠）
  const total = Number(plan && plan.saved);
  const real = isNaN(total) ? list.reduce((a, d) => a + Number(d.saved || 0), 0) : total;
  $("#decideSum").textContent = list.length ? `${list.length} 处 · 共 ${real.toFixed(1)}s` : "";

  box.innerHTML = "";

  list.forEach((d) => {
    const row = document.createElement("div");
    row.className = "decide";

    const tag = document.createElement("span");
    const [cls, text] = tagOf(d.type);
    tag.className = "tag " + cls;
    tag.textContent = text;

    const main = document.createElement("div");
    main.className = "decide-main";

    const top = document.createElement("div");
    top.className = "decide-top";
    const time = document.createElement("span");
    time.className = "decide-time";
    time.textContent = `${fmtPrecise(d.start)} → ${fmtPrecise(d.end)}`;
    const saved = document.createElement("span");
    saved.className = "decide-saved";
    saved.textContent = Number(d.saved).toFixed(1) + "s";
    top.append(time, saved);

    const reason = document.createElement("div");
    reason.className = "decide-reason";
    reason.textContent = d.reason;

    main.append(top, reason);
    row.append(tag, main);
    box.appendChild(row);
  });
}

function showOutput(result) {
  state.output = result;
  state.kit = null;
  $("#outName").textContent = result.name;
  $("#outMeta").textContent =
    `保留 ${fmt(result.kept)} · 剪掉 ${Number(result.removed).toFixed(1)} 秒 · ${result.size_mb} MB`;
  if (LABEL_KIT) $("#btnKit").innerHTML = LABEL_KIT;
  $("#estBox").classList.add("hidden");
  $("#outputBox").classList.remove("hidden");
}

function onAutocutDone(result) {
  const an = result.analysis || {};
  const plan = result.plan || {};

  state.analysis = an;
  state.plan = plan;
  fillCounts(an);
  $("#emptyHint").classList.add("hidden");

  // AI 的每条决策都变成时间轴上可拖可删的区间
  state.suggestions = (plan.decisions || []).map((d) => ({
    id: newId(),
    type: d.type,
    start: d.start,
    end: d.end,
    on: true,
    reason: d.reason,
  }));
  state.selected = null;
  state.rows = null;

  renderDecisions(plan);
  refreshAfterEdit();

  // 切到概览页看结果
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === "overview"));
  $("#tab-overview").classList.remove("hidden");
  $("#tab-suggest").classList.add("hidden");

  // 后端顺手转出了字幕：存进面板，下次出片/精调包直接复用，别再听一遍
  if (result.cues && result.cues.length && state.fx) {
    state.fx.cues = result.cues;
    state.fx.cuesFrom = "asr";
    fxSync();
  }

  const subN = (result.fx_applied && result.fx_applied.subtitle) || 0;
  const subTag = subN ? `（含字幕 ${subN} 条）` : "";

  if (result.no_cut) {
    // 没剪点但有效果时，后端其实整条重渲染过（烧字幕/归一/BGM/变速），不能说成「原片」
    const fa = result.fx_applied || {};
    const anyFx = !!(fa.subtitle || fa.normalize || fa.bgm
      || fa.transition || (Number(fa.speed) && Number(fa.speed) !== 1));
    if (anyFx) {
      setStatus("AI 看过了：这段没什么可剪的，已按当前效果出片" + subTag, "done");
      toast("无需剪辑，已按当前效果出片");
    } else {
      setStatus("AI 看过了：这段没什么可剪的，直接输出原片" + subTag, "done");
      toast("AI 判断无需剪辑，已输出原片");
    }
  } else {
    setStatus(
      `AI 粗剪完成（${plan.strength_label || ""}）：剪掉 ${Number(result.removed).toFixed(1)}s，` +
      `保留 ${fmt(result.kept)}${subTag}`,
      "done"
    );
    toast(`剪掉 ${Number(result.removed).toFixed(1)} 秒，成片已生成`);
  }
  if (plan.warning) toast(plan.warning);
  // 降级说明放最后，别被「剪掉 xx 秒」盖住
  if (result.note) toast(result.note);

  showOutput(result);
}

function onKitDone(files) {
  state.kit = files;
  const n = (files.clips || []).length;
  if (LABEL_KIT) $("#btnKit").innerHTML = "打开精调包";
  setStatus(`精调包已生成：${n} 段素材 + 时间线，可直接拖进剪映`, "done");
  toast(`精调包已就绪：${n} 段素材 + timeline.edl`);
  if (files.dir) api("/api/open", { method: "POST", body: JSON.stringify({ path: files.dir, mode: "folder" }) });
}

function onAnalysisDone(result) {
  state.analysis = result;
  state.plan = null;
  const dur = result.duration || (state.info && state.info.duration) || 0;

  fillCounts(result);
  $("#emptyHint").classList.add("hidden");
  $("#decideBlock").classList.add("hidden");

  // 汇总建议：静音 / 重复 / 静止 / 黑场可剪，场景切点仅作参考
  const list = [];
  (result.scenes || []).forEach((s) => {
    list.push({ id: newId(), type: "scene", start: s.start, end: null, reason: `镜头切换点（置信度 ${(s.score * 100).toFixed(0)}%）`, on: false });
  });
  (result.silent || []).forEach((s) => {
    list.push({ id: newId(), type: "silence", start: s.start, end: s.end, reason: `静音 ${(s.end - s.start).toFixed(1)} 秒，建议剪掉`, on: true });
  });
  (result.redundant || []).forEach((s) => {
    list.push({ id: newId(), type: "dup", start: s.start, end: s.end, reason: `与 ${fmtPrecise(s.similar_to)} 处重复（相似度 ${(s.similarity * 100).toFixed(0)}%），建议剪掉`, on: true });
  });
  (result.static || []).forEach((s) => {
    list.push({ id: newId(), type: "static", start: s.start, end: s.end, reason: `画面静止 ${(s.end - s.start).toFixed(1)} 秒`, on: false });
  });
  (result.blank || []).forEach((s) => {
    list.push({ id: newId(), type: "blank", start: s.start, end: s.end, reason: `黑场 / 纯色 ${(s.end - s.start).toFixed(1)} 秒`, on: true });
  });
  list.sort((a, b) => a.start - b.start);
  state.suggestions = list;
  state.selected = null;

  renderSuggestions();
  drawTimeline();
  syncToggles();

  const cuts = computeCuts().length;
  $("#sugCount").textContent = list.filter((x) => x.type !== "scene").length;
  $("#sugCount").classList.remove("hidden");
  $("#btnExport").disabled = false;

  setStatus(`分析完成：${list.length} 条线索，其中 ${cuts} 处可剪`, "done");
  toast(`分析完成，检出 ${cuts} 处可剪内容`);
}

function renderSuggestions() {
  const box = $("#sugList");
  box.innerHTML = "";
  state.rows = new Map();
  if (!state.suggestions.length) {
    box.innerHTML = '<div class="empty-hint">还没有区间。可以先跑分析，或直接在时间轴上拖出要剪掉的部分。</div>';
    return;
  }

  state.suggestions.forEach((item) => {
    const row = document.createElement("label");
    row.className = "sug" + (item.on ? "" : " off") + (state.selected === item.id ? " sel" : "");
    row.dataset.id = item.id;

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = item.on;
    cb.disabled = item.type === "scene";
    cb.addEventListener("change", () => {
      item.on = cb.checked;
      row.classList.toggle("off", !cb.checked);
      drawTimeline();
      updateEstimate();
      $("#btnRender").disabled = state.busy || !hasCuts();
      syncToggles();
    });

    const main = document.createElement("div");
    main.className = "sug-main";

    const top = document.createElement("div");
    top.className = "sug-top";
    const tag = document.createElement("span");
    const [cls, text] = tagOf(item.type);
    tag.className = "tag " + cls;
    tag.textContent = text;
    const time = document.createElement("span");
    time.className = "sug-time";
    time.textContent = item.end ? `${fmtPrecise(item.start)} → ${fmtPrecise(item.end)}` : fmtPrecise(item.start);
    top.append(tag, time);

    const reason = document.createElement("div");
    reason.className = "sug-reason";
    reason.textContent = item.reason;

    main.append(top, reason);
    row.append(cb, main);

    // 手动区间多给一个删除按钮
    if (item.type === "manual") {
      const del = document.createElement("button");
      del.className = "sug-del";
      del.textContent = "×";
      del.title = "删除这个区间";
      del.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        removeItem(item.id);
      });
      row.append(del);
    }

    row.addEventListener("click", (e) => {
      if (e.target === cb || e.target.classList.contains("sug-del")) return;
      state.selected = item.id;
      drawTimeline();
      $$("#sugList .sug").forEach((r) => r.classList.toggle("sel", r.dataset.id === item.id));
      seekTo(item.start);
    });

    state.rows.set(item.id, time);
    box.appendChild(row);
  });
}

function syncToggles() {
  const all = (type) => state.suggestions.filter((s) => s.type === type);
  const set = (sel, type) => {
    const list = all(type);
    const el = $(sel);
    if (el) el.checked = list.length ? list.every((s) => s.on) : true;
  };
  set("#optSilent", "silence");
  set("#optDup", "dup");
  set("#optStatic", "static");
  set("#optBlank", "blank");
  // 口头禅/重说默认不开：开了要先转写，第一次很慢。
  // 只有已经剪出结果时才跟着结果回显，不能像上面那样「空列表默认打勾」。
  [["#optFiller", "filler"], ["#optRetake", "retake"]].forEach(([sel, type]) => {
    const el = $(sel);
    const list = all(type);
    if (el && list.length) el.checked = list.every((s) => s.on);
  });
}

function applyToggle(type, on) {
  state.suggestions.forEach((s) => { if (s.type === type) s.on = on; });
  renderSuggestions();
  drawTimeline();
  updateEstimate();
  $("#btnRender").disabled = state.busy || !hasCuts();
}

/* ---------------- 剪辑计算 ---------------- */
function computeCuts() {
  return state.suggestions
    .filter((s) => s.on && s.type !== "scene" && s.end != null)
    .map((s) => [s.start, s.end]);
}

function mergeRanges(ranges) {
  if (!ranges.length) return [];
  const rs = ranges.slice().sort((a, b) => a[0] - b[0]);
  const out = [rs[0].slice()];
  for (let i = 1; i < rs.length; i++) {
    const [s, e] = rs[i];
    if (s <= out[out.length - 1][1] + 0.02) out[out.length - 1][1] = Math.max(out[out.length - 1][1], e);
    else out.push([s, e]);
  }
  return out;
}

function hasCuts() {
  return computeCuts().length > 0;
}

function round2(t) { return Math.round(t * 100) / 100; }

/* 当前时间轴状态对应的「保留片段」，精调包按它导出 */
function computeKeep() {
  const dur = durationOf();
  if (!dur) return [];
  const cuts = mergeRanges(computeCuts());
  const keep = [];
  let cur = 0;
  cuts.forEach(([s, e]) => {
    if (s - cur > 0.15) keep.push([round2(cur), round2(s)]);
    cur = Math.max(cur, e);
  });
  if (dur - cur > 0.15) keep.push([round2(cur), round2(dur)]);
  return keep;
}

function updateEstimate() {
  const dur = durationOf();
  const box = $("#estBox");
  const hasAny = state.suggestions.some((s) => s.type !== "scene" && s.end != null);
  if (!dur || !hasAny) { box.classList.add("hidden"); $("#btnRender").disabled = true; return; }

  const cuts = mergeRanges(computeCuts());
  const cutTotal = cuts.reduce((a, [s, e]) => a + (e - s), 0);
  const after = Math.max(0, dur - cutTotal);

  // 成片卡片里已经写了保留/剪掉时长，不必再重复一块
  box.classList.toggle("hidden", !!state.output);
  $("#estAfter").textContent = fmt(after);
  $("#estCut").textContent = `${cutTotal.toFixed(1)} 秒`;
  $("#estFill").style.width = Math.min(100, (after / dur) * 100) + "%";

  $("#btnRender").disabled = state.busy || cuts.length === 0 || after < 0.3;
}

/* ---------------- 渲染 ---------------- */
async function render() {
  if (state.busy || !state.path) return;
  const dur = durationOf();
  if (!dur) return;
  const cuts = computeCuts();
  if (!cuts.length) { toast("没有需要剪掉的内容"); return; }

  setBusy(true);
  setProgress(0);
  setStatus("正在生成成片…", "busy");
  $("#btnRender").innerHTML = "取消";

  const r = await api("/api/render", {
    method: "POST",
    body: JSON.stringify({
      path: state.path,
      duration: dur,
      cut_ranges: cuts,
      // 卡点方案要沿用「画面拼接 + 原音轨整条」，别把音乐切断
      audio_mode: (state.plan && state.plan.audio_mode) || "clip",
      effects: fxPayload(),
      cues: (state.fx && state.fx.subtitle.on) ? state.fx.cues : [],
    }),
  });

  if (!r.ok) {
    finishJob();
    setStatus("渲染启动失败", "err");
    toast(r.error || "渲染失败", "error");
    return;
  }
  state.job = { id: r.job, kind: "render" };
  setBusy(true);   // 同上：让「取消」可点
  pollJob();
}

function onRenderDone(result) {
  showOutput(result);
  setStatus(`成片已生成：${result.name}`, "done");
  toast("成片已生成，可在右侧打开");
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === "overview"));
  $("#tab-overview").classList.remove("hidden");
  $("#tab-suggest").classList.add("hidden");
}

/* ---------------- 对话：按需求剪 ---------------- */
function escHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* 只认 **加粗** 和换行，够用了，也不怕注入 */
function mdLite(s) {
  return escHtml(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>");
}

const AI_FACE = '<svg viewBox="0 0 24 24" fill="none"><path d="M12 3l2.2 5.4L20 10.6l-5.8 2.2L12 18l-2.2-5.2L4 10.6l5.8-2.2z" fill="currentColor"/></svg>';

function chatScrollBottom() {
  const log = $("#chatLog");
  if (log) log.scrollTop = log.scrollHeight;
}

function pushMsg(role, text, opts = {}) {
  const log = $("#chatLog");
  if (!log) return null;
  const hello = $("#chatHello");
  if (hello) hello.classList.add("hidden");

  const row = document.createElement("div");
  row.className = "msg " + (role === "me" ? "msg-me" : "msg-ai");

  if (role === "me") {
    const b = document.createElement("div");
    b.className = "bubble";
    b.textContent = text;
    row.appendChild(b);
  } else {
    const face = document.createElement("div");
    face.className = "msg-face";
    face.innerHTML = AI_FACE;
    const b = document.createElement("div");
    b.className = "bubble";

    if (opts.typing) {
      b.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
    } else {
      b.innerHTML = mdLite(text);

      if (opts.chips && opts.chips.length) {
        const box = document.createElement("div");
        box.className = "res-chips";
        opts.chips.forEach((c) => {
          const chip = document.createElement("span");
          chip.className = "chip" + (c.accent ? " chip-accent" : "") + (c.ok ? " chip-ok" : "");
          chip.textContent = c.text;
          box.appendChild(chip);
        });
        b.appendChild(box);
      }

      if (opts.actions && opts.actions.length) {
        const acts = document.createElement("div");
        acts.className = "msg-actions";
        opts.actions.forEach((a) => {
          const btn = document.createElement("button");
          btn.className = "btn " + (a.primary ? "btn-primary" : "btn-ghost") + " btn-sm";
          btn.textContent = a.text;
          btn.addEventListener("click", a.onClick);
          acts.appendChild(btn);
        });
        b.appendChild(acts);
      }
    }
    row.append(face, b);
  }

  log.appendChild(row);
  chatScrollBottom();
  return row;
}

function pushTyping() {
  dropTyping();
  state.typingEl = pushMsg("ai", "", { typing: true });
}

function pushDivider(text) {
  const log = $("#chatLog");
  if (!log) return;
  const d = document.createElement("div");
  d.className = "chat-divider";
  d.innerHTML = "<i></i><span></span><i></i>";
  d.querySelector("span").textContent = text;
  log.appendChild(d);
  chatScrollBottom();
}

function dropTyping() {
  if (state.typingEl && state.typingEl.parentNode) state.typingEl.remove();
  state.typingEl = null;
}

async function sendChat() {
  if (state.busy) return;
  const el = $("#chatText");
  const text = (el.value || "").trim();
  if (!text) { el.focus(); return; }
  if (!state.path) {
    toast("先选一个视频，再告诉我想怎么剪", "error");
    setStatus("先用右上角「选择视频」载入素材", "err");
    return;
  }

  el.value = "";
  pushMsg("me", text);

  setBusy(true);
  setProgress(0);
  setStatus("正在理解你的需求…", "busy");
  pushTyping();

  const r = await api("/api/direct", {
    method: "POST",
    body: JSON.stringify({
      path: state.path,
      text,
      render: $("#optRenderNow").checked,
      effects: fxPayload(),
      cues: (state.fx && state.fx.subtitle.on) ? state.fx.cues : [],
    }),
  });

  if (!r.ok) {
    dropTyping();
    finishJob();
    setStatus("启动失败", "err");
    const msg = r.error || "按需求剪启动失败";
    toast(msg, "error");
    pushMsg("ai", msg);
    return;
  }

  state.job = { id: r.job, kind: "direct" };
  setBusy(true);
  pollJob();
}

function onDirectDone(result) {
  const an = result.analysis || {};
  const plan = result.plan || {};
  const intent = result.intent || {};

  state.analysis = an;
  state.plan = plan;
  fillCounts(an);
  $("#emptyHint").classList.add("hidden");
  $("#btnExport").disabled = false;

  state.suggestions = (plan.decisions || []).map((d) => ({
    id: newId(),
    type: d.type,
    start: d.start,
    end: d.end,
    on: true,
    reason: d.reason,
  }));
  state.selected = null;
  state.rows = null;

  renderDecisions(plan);
  refreshAfterEdit();

  // 对话里说的效果，同步到面板；转写出来的字幕也存下来复用
  fxFromPlan(plan);
  if (result.cues && result.cues.length) {
    state.fx.cues = result.cues;
    state.fx.cuesFrom = "asr";
    fxSync();
  }

  const chips = [];
  if (plan.style_label) chips.push({ text: plan.style_label, accent: true });
  if (plan.strength_label) chips.push({ text: plan.strength_label + "节奏" });
  if (plan.beat && plan.beat.ok && plan.beat.bpm) chips.push({ text: `${plan.beat.bpm} BPM` });
  if (plan.speed && Math.abs(plan.speed - 1) > 1e-6) chips.push({ text: `${plan.speed}× 速度` });
  if (plan.transition) chips.push({ text: "带转场" });
  if (plan.subtitle && plan.subtitle.on) chips.push({ text: "字幕" });
  if (plan.bgm && plan.bgm.on) chips.push({ text: "BGM" });
  if (result.removed != null) chips.push({ text: `剪掉 ${Number(result.removed).toFixed(1)}s` });
  if (plan.after) chips.push({ text: `成片 ${fmt(plan.after)}`, ok: !!result.output });

  const actions = [];
  if (result.output) {
    actions.push({
      text: "播放成片", primary: true,
      onClick: () => api("/api/open", { method: "POST", body: JSON.stringify({ path: result.output, mode: "file" }) }),
    });
    actions.push({
      text: "打开文件夹",
      onClick: () => api("/api/open", { method: "POST", body: JSON.stringify({ path: result.output, mode: "folder" }) }),
    });
  } else if (hasCuts()) {
    actions.push({
      text: "按这个方案出片", primary: true,
      onClick: () => render(),
    });
  }

  dropTyping();
  pushMsg("ai", result.reply || "方案排好了。", { chips, actions });

  if (result.output) {
    showOutput({ ...result, kept: plan.after, removed: plan.saved });
    setStatus(`按需求剪完了：${result.name}`, "done");
    toast("成片已生成");
  } else {
    setStatus("方案排好了，确认时间轴后点「按时间轴出片」", "done");
  }
  if (plan.warning) toast(plan.warning);
}

/* ---- 大模型设置（可选） ---- */
function setLlmStatus(text, kind = "") {
  const el = $("#llmStatus");
  if (!el) return;
  el.textContent = text;
  el.className = "modal-status" + (kind ? " " + kind : "");
}

async function openLlmModal() {
  $("#llmModal").classList.remove("hidden");
  setLlmStatus("读取配置…");
  const r = await api("/api/settings");
  const llm = (r && r.llm) || {};
  $("#llmBase").value = llm.base_url || "https://api.deepseek.com/v1";
  $("#llmModel").value = llm.model || "deepseek-chat";
  $("#llmKey").value = llm.api_key || "";
  $("#llmKey").placeholder = llm.api_key && llm.api_key.startsWith("****")
    ? "已保存（重新输入可覆盖）" : "sk-…";
  setLlmStatus(llm.ready ? "已配置，可以用大模型理解长句了" : "还没配置：会用内置规则理解，也能用",
    llm.ready ? "ok" : "");
}

function closeLlmModal() {
  $("#llmModal").classList.add("hidden");
}

async function saveLlm(silent) {
  const body = {
    llm: {
      base_url: $("#llmBase").value.trim(),
      model: $("#llmModel").value.trim(),
      api_key: $("#llmKey").value.trim(),
      enabled: true,
    },
  };
  const r = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
  if (!r.ok) {
    setLlmStatus(r.error || "保存失败", "err");
    return null;
  }
  const llm = r.llm || {};
  $("#llmKey").value = llm.api_key || "";
  if (!silent) setLlmStatus("已保存", "ok");
  return llm;
}

async function testLlm() {
  const btn = $("#btnLlmTest");
  btn.disabled = true;
  setLlmStatus("正在测试…");
  await saveLlm(true);
  const r = await api("/api/llm-test", { method: "POST" });
  btn.disabled = false;
  if (r.ok) setLlmStatus(`连通了（示例需求理解成：${r.style || "auto"} / ${r.target || "-"}s）`, "ok");
  else setLlmStatus(r.error || "测试失败", "err");
}

/* ---------------- 效果面板：字幕 / BGM / 变速 / 转场 ---------------- */

const FX_DEFAULT = () => ({
  // 字幕：描边色 / 底框不透明度 / 逐字点亮都是口播第三步加的
  subtitle: {
    on: false, size: "normal", position: "bottom",
    outline_color: "#000000",
    box: false, box_alpha: 62,
    highlight: false, highlight_color: "#FFD54A",
  },
  bgm: { on: false, path: "", name: "", volume: 0.24, keep_original: true },
  speed: 1.0,
  // 音量归一：统一到 -16 LUFS，手机外放/平台播放不再忽大忽小
  normalize: false,
  // 口腔杂音抑制（轻度降噪）：压呼吸声/咂嘴，不动人声和 BGM
  denoise: false,
  // 画幅：origin = 原画幅；blur = 竖屏 1080×1920 模糊背景；crop = 竖屏居中裁切
  frame: "origin",
  // auto = 按接缝自己判断：同一镜头剪停顿硬切，换镜头才过渡，再按画面差异挑类型
  // fadeTouched：用户亲手拖过淡入淡出滑块，关掉转场也照样生效（没拖过就跟着转场走）
  transition: {
    on: false, type: "auto", duration: 0.5,
    fade_in: 0.4, fade_out: 0.5, fadeTouched: false,
  },
  cues: [],
  cuesFrom: "",
});

function segSet(sel, val) {
  $$(sel + " button").forEach((b) => {
    b.classList.toggle("active", String(b.dataset.val) === String(val));
  });
}

// 转场：auto 时每个接缝的时长由镜头长短决定，滑块不参与
function fxTransLabel() {
  const t = (state.fx && state.fx.transition) || FX_DEFAULT().transition;
  const kind = !t.type || t.type === "none" ? "auto" : t.type;
  const auto = kind === "auto";
  const d = Number(t.duration) || 0.5;
  $("#fxTransType").value = kind;
  $("#fxTransNote").textContent = auto
    ? "同一镜头里剪掉的停顿直接切，换了镜头才过渡；明暗突变用黑场、换色调用溶解，其余叠化。"
    : "全片每个接缝都用这一种，接缝多的时候容易显得糊，不如自动自然。";
  $("#fxTransDur").value = Math.round(d * 10);
  $("#fxTransDur").disabled = auto;
  $("#fxTransDurVal").textContent = auto ? "自动" : `${d.toFixed(1)}s`;
  $("#fxTransDurRow").style.opacity = auto ? ".5" : "";

  // 首尾淡入淡出独立于转场开关
  const fin = Number(t.fade_in != null ? t.fade_in : 0.4);
  const fout = Number(t.fade_out != null ? t.fade_out : 0.5);
  $("#fxFadeIn").value = Math.round(fin * 10);
  $("#fxFadeOut").value = Math.round(fout * 10);
  $("#fxFadeInVal").textContent = fin > 0.04 ? `${fin.toFixed(1)}s` : "不淡";
  $("#fxFadeOutVal").textContent = fout > 0.04 ? `${fout.toFixed(1)}s` : "不淡";
}

function fxSync() {
  const fx = state.fx;
  if (!fx) return;

  $("#fxSubOn").checked = !!fx.subtitle.on;
  $("#fxSubBody").classList.toggle("hidden", !fx.subtitle.on);
  segSet("#fxSubSize", fx.subtitle.size);
  segSet("#fxSubPos", fx.subtitle.position);
  $("#fxSubOutline").value = fx.subtitle.outline_color || "#000000";
  $("#fxSubBox").checked = !!fx.subtitle.box;
  $("#fxSubBoxAlpha").value = Math.round(fx.subtitle.box_alpha != null ? fx.subtitle.box_alpha : 62);
  $("#fxSubBoxAlphaVal").textContent =
    Math.round(fx.subtitle.box_alpha != null ? fx.subtitle.box_alpha : 62) + "%";
  $("#fxSubBoxAlphaRow").style.opacity = fx.subtitle.box ? "" : ".45";
  $("#fxSubHighlight").checked = !!fx.subtitle.highlight;
  $("#fxSubHlColor").value = fx.subtitle.highlight_color || "#FFD54A";
  // 底框和逐字点亮实测可以同时开（相邻 karaoke 的框会连成一整条），只提示不拦
  $("#fxSubHlColor").disabled = !fx.subtitle.highlight;

  $("#fxBgmOn").checked = !!fx.bgm.on;
  // 音乐库入口一直露着：关了开关也能先导入、试听，选一首就自动打开
  $("#fxBgmBody").classList.toggle("dim", !fx.bgm.on);
  $("#fxBgmVol").value = Math.round(fx.bgm.volume * 100);
  $("#fxBgmVolVal").textContent = Math.round(fx.bgm.volume * 100) + "%";
  $("#fxBgmKeep").checked = !!fx.bgm.keep_original;

  segSet("#fxSpeed", fx.speed);
  const spd = Number(fx.speed) || 1;
  $("#fxSpeedHint").textContent = Math.abs(spd - 1) < 1e-6 ? "1×（原速）" : spd + "×";

  $("#fxNormOn").checked = !!fx.normalize;
  $("#fxDenoiseOn").checked = !!fx.denoise;

  segSet("#fxFrame", fx.frame || "origin");
  $("#fxFrameHint").textContent = FX_FRAME_HINT[fx.frame || "origin"] || "原画幅";
  $("#fxFrameNote").textContent = FX_FRAME_NOTE[fx.frame || "origin"] || FX_FRAME_NOTE.origin;

  $("#fxTransOn").checked = !!fx.transition.on;
  $("#fxTransBody").classList.toggle("hidden", !fx.transition.on);
  fxTransLabel();

  const st = $("#subStatus");
  if (fx.cues.length) {
    const words = fx.cues.some((c) => c && c.words && c.words.length);
    st.textContent = `已有 ${fx.cues.length} 条字幕` +
      (fx.cuesFrom === "srt" ? "（导入的）" : "（自动转写）") +
      (fx.subtitle.highlight
        ? (words ? " · 可逐字点亮" : " · 没有词时间，逐字点亮不生效")
        : "");
    st.className = fx.subtitle.highlight && !words ? "fx-note warn" : "fx-note ok";
  } else if (fx.subtitle.on) {
    st.textContent = "还没字幕。点「自动转写」让工具听一遍音频。";
    st.className = "fx-note warn";
  } else {
    st.textContent = "还没转写。点「自动转写」让工具听一遍音频。";
    st.className = "fx-note";
  }
}

const FX_FRAME_HINT = {
  origin: "原画幅",
  blur: "1080×1920 · 模糊背景",
  crop: "1080×1920 · 居中裁切",
};
const FX_FRAME_NOTE = {
  origin: "保持源视频的分辨率，不重编画面。",
  blur: "竖屏输出 1080×1920：原画面完整放进中间，四周补虚化背景，口播最稳。",
  crop: "竖屏输出 1080×1920：放大后只留中间一条，人不在正中间就别用这个。",
};

function fxPayload() {
  const fx = state.fx || FX_DEFAULT();
  const tr = fx.transition;
  // 转场关着又没亲手拖过首尾淡入淡出 → 什么都不加（保持「纯硬切」的老行为）
  const fadeOn = !!tr.on || !!tr.fadeTouched;
  return {
    subtitle: {
      on: !!fx.subtitle.on, size: fx.subtitle.size, position: fx.subtitle.position,
      // 新样式键：描边色 / 底框 / 逐字点亮
      outline_color: fx.subtitle.outline_color || "#000000",
      box: !!fx.subtitle.box,
      box_alpha: Number(fx.subtitle.box_alpha != null ? fx.subtitle.box_alpha : 62),
      // 底框和逐字点亮可以同时开（实测相邻 karaoke 的框会连成一整条）
      highlight: !!fx.subtitle.highlight,
      highlight_color: fx.subtitle.highlight_color || "#FFD54A",
    },
    bgm: {
      on: !!fx.bgm.on, path: fx.bgm.path || "", volume: Number(fx.bgm.volume) || 0.24,
      keep_original: !!fx.bgm.keep_original,
    },
    speed: Number(fx.speed) || 1,
    normalize: !!fx.normalize,
    denoise: !!fx.denoise,
    // origin → 不传（null），后端按老行为走原画幅
    frame: (fx.frame && fx.frame !== "origin") ? { mode: fx.frame } : null,
    transition: {
      on: !!tr.on,
      type: tr.type || "auto",
      // auto 时具体时长交给后端按镜头判断，0 表示「不强制」
      duration: (tr.type || "auto") === "auto"
        ? 0 : (Number(tr.duration) || 0.5),
      // 首尾淡入淡出：0 表示不淡
      fade_in: fadeOn ? Number(tr.fade_in != null ? tr.fade_in : 0.4) : 0,
      fade_out: fadeOn ? Number(tr.fade_out != null ? tr.fade_out : 0.5) : 0,
    },
  };
}

/* ---- 口播一键预设 ----
   开：标准力度 + 剪口头禅/重说 + 字幕 + 全硬切（不淡入淡出）+ 统一音量
   关：把开之前的样子原样还原（fx 是整份快照，BGM/字号这些都没丢）
   用户中途手动改力度/关字幕不自动退出预设——保持简单，别抢用户的操作 */
let profileSnapshot = null;

function profileOn() {
  return !!profileSnapshot;
}

function setProfile(on) {
  const btn = $("#btnProfile");

  if (on) {
    if (profileOn()) return;
    if (!state.fx) {
      toast("效果面板还没准备好，稍等一下再点「口播」", "error");
      return;
    }

    profileSnapshot = {
      strength: state.strength,
      filler: !!($("#optFiller") || {}).checked,
      retake: !!($("#optRetake") || {}).checked,
      fx: JSON.parse(JSON.stringify(state.fx)),
    };

    state.strength = "standard";
    $$("#strength button").forEach((b) => {
      b.classList.toggle("active", b.dataset.val === "standard");
    });
    if ($("#optFiller")) $("#optFiller").checked = true;
    if ($("#optRetake")) $("#optRetake").checked = true;

    const fx = state.fx;
    fx.subtitle.on = true;
    // 口播里全是同一机位连着说话，叠化只会让话头话尾糊掉：一律硬切
    fx.transition.on = false;
    fx.transition.fade_in = 0;
    fx.transition.fade_out = 0;
    fx.transition.fadeTouched = true;   // 片头片尾也不淡
    fx.normalize = true;

    fxSync();
    if (btn) btn.classList.add("active");
    toast("口播模式：剪停顿+口头禅、上字幕、硬切、统一音量");
    return;
  }

  if (!profileOn()) return;
  const snap = profileSnapshot;
  profileSnapshot = null;

  state.strength = snap.strength || "standard";
  $$("#strength button").forEach((b) => {
    b.classList.toggle("active", b.dataset.val === state.strength);
  });
  if ($("#optFiller")) $("#optFiller").checked = !!snap.filler;
  if ($("#optRetake")) $("#optRetake").checked = !!snap.retake;
  if (snap.fx) {
    // 整个 fx 按快照还原；但字幕是听写几分钟才有的「数据」不是「设置」，
    // 开着预设时新转出来的字幕要留着，别一关就白听了
    const keepCues = (state.fx && state.fx.cues) || [];
    const keepFrom = (state.fx && state.fx.cuesFrom) || "asr";
    state.fx = snap.fx;
    if (keepCues.length && !((state.fx.cues || []).length)) {
      state.fx.cues = keepCues;
      state.fx.cuesFrom = keepFrom;
    }
  }
  fxSync();
  if (btn) btn.classList.remove("active");
  toast("口播模式已关，设置还原成原来的样子");
}

// 对话里说「加字幕」，面板要跟着变，否则两边说的不是一件事
function fxFromPlan(plan) {
  if (!plan || !state.fx) return;
  const fx = state.fx;

  if (plan.subtitle) {
    if (typeof plan.subtitle.on === "boolean") fx.subtitle.on = plan.subtitle.on;
    if (plan.subtitle.size) fx.subtitle.size = plan.subtitle.size;
    if (plan.subtitle.position) fx.subtitle.position = plan.subtitle.position;
  }
  if (plan.bgm) {
    if (typeof plan.bgm.on === "boolean") fx.bgm.on = plan.bgm.on;
    if (plan.bgm.volume) fx.bgm.volume = plan.bgm.volume;
    if (plan.bgm.keep_original === false) fx.bgm.keep_original = false;
  }
  if (plan.speed) fx.speed = plan.speed;
  if (plan.transition) {
    const kind = plan.transition.type || "auto";
    if (kind === "none") {
      // 只要求首尾淡入淡出，接缝仍然硬切：转场开关保持关闭
      fx.transition.on = false;
    } else {
      fx.transition.on = true;
      fx.transition.type = kind;
      if (plan.transition.duration) fx.transition.duration = plan.transition.duration;
    }
    if (plan.transition.fade_in != null) {
      fx.transition.fade_in = plan.transition.fade_in;
      fx.transition.fadeTouched = true;
    }
    if (plan.transition.fade_out != null) {
      fx.transition.fade_out = plan.transition.fade_out;
      fx.transition.fadeTouched = true;
    }
  }
  // 只说了片头淡入/片尾淡出：转场开关不打开，但淡入淡出要落到面板上
  if (plan.fades) {
    if (plan.fades.fade_in != null) {
      fx.transition.fade_in = plan.fades.fade_in;
      fx.transition.fadeTouched = true;
    }
    if (plan.fades.fade_out != null) {
      fx.transition.fade_out = plan.fades.fade_out;
      fx.transition.fadeTouched = true;
    }
  }
  fxSync();
}

async function fxTranscribe() {
  if (!state.path) { toast("先选一个视频", "error"); return; }
  if (state.busy) { toast("有任务在跑，稍等一下", "error"); return; }

  const btn = $("#btnSubTranscribe");
  const st = $("#subStatus");
  btn.disabled = true;
  st.textContent = "准备转写…";
  st.className = "fx-note";

  try {
    const r = await api("/api/subtitle/transcribe", {
      method: "POST",
      body: JSON.stringify({ path: state.path }),
    });
    if (!r.ok) throw new Error(r.error || "启动失败");
    fxPollJob(r.job);
  } catch (e) {
    btn.disabled = false;
    st.textContent = String(e.message || e);
    st.className = "fx-note err";
  }
}

async function fxPollJob(id) {
  const st = $("#subStatus");
  const btn = $("#btnSubTranscribe");
  try {
    const r = await api("/api/job/" + id);
    const job = r.job || {};

    if (job.status === "running") {
      st.textContent = (job.message || "听写中…") + `  ${Math.round(job.progress || 0)}%`;
      st.className = "fx-note";
      setTimeout(() => fxPollJob(id), 900);
      return;
    }

    btn.disabled = false;
    if (job.status === "done") {
      const res = job.result || {};
      state.fx.cues = res.cues || [];
      state.fx.cuesFrom = "asr";
      state.fx.subtitle.on = true;
      fxSync();
      toast(`转写出 ${state.fx.cues.length} 条字幕`, "success");
    } else if (job.status === "cancelled") {
      st.textContent = "已取消";
      st.className = "fx-note";
    } else {
      st.textContent = "转写失败：" + (job.error || "未知错误");
      st.className = "fx-note err";
    }
  } catch (e) {
    btn.disabled = false;
    st.textContent = "查询进度失败";
    st.className = "fx-note err";
  }
}

async function fxImportSrt() {
  const r = await api("/api/pick", { method: "POST", body: JSON.stringify({ kind: "subtitle" }) });
  if (!r.ok || !r.path) return;

  const p = await api("/api/subtitle/parse", {
    method: "POST", body: JSON.stringify({ path: r.path }),
  });
  if (!p.ok) { toast(p.error || "读不了这个字幕文件", "error"); return; }

  state.fx.cues = p.cues || [];
  state.fx.cuesFrom = "srt";
  state.fx.subtitle.on = true;
  fxSync();
  $("#subStatus").textContent = `已导入 ${p.count} 条（${p.name}）`;
  $("#subStatus").className = "fx-note ok";
  toast(`导入 ${p.count} 条字幕`, "success");
}

async function fxLoadMusic() {
  try {
    const r = await api("/api/bgm/list");
    const sel = $("#fxBgmSel");
    const files = r.files || [];
    sel.innerHTML = "";
    const head = document.createElement("option");
    head.value = "";
    head.textContent = files.length ? `音乐库（${files.length} 首）` : "音乐库（空）";
    sel.appendChild(head);
    files.forEach((f) => {
      const o = document.createElement("option");
      o.value = f.path;
      o.textContent = f.name.replace(/\.[^.]+$/, "");
      sel.appendChild(o);
    });
    if (state.fx && state.fx.bgm.path) sel.value = state.fx.bgm.path;
    return files;
  } catch (e) {
    return [];      // 音乐库读不到不影响剪辑
  }
}

/* ---------------- 音乐库窗口 ---------------- */

let musicFiles = [];        // 库里的曲目
let musicPlaying = "";      // 正在试听的曲目名
let previewAudio = null;
let musicArmTimer = null;

function setMusicStatus(text, kind = "") {
  const el = $("#musicStatus");
  if (!el) return;
  el.textContent = text;
  el.className = "modal-status" + (kind ? " " + kind : "");
}

function fmtDur(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if (!s) return "--:--";
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function stopPreview() {
  if (previewAudio) {
    try { previewAudio.pause(); } catch (e) { /* 已经停了 */ }
    previewAudio.onended = null;
    previewAudio.onerror = null;
    previewAudio = null;
  }
  musicPlaying = "";
  updateMusicPlaying();
}

function updateMusicPlaying() {
  $$("#musicList .music-row").forEach((row) => {
    const on = row.dataset.name === musicPlaying;
    row.classList.toggle("playing", on);
    const b = row.querySelector('[data-act="play"]');
    if (b) b.textContent = on ? "❚❚" : "▶";
  });
}

function renderMusicList() {
  const box = $("#musicList");
  if (!box) return;
  const cur = (state.fx && state.fx.bgm.path) || "";
  box.innerHTML = "";
  $("#musicCount").textContent = musicFiles.length ? `${musicFiles.length} 首` : "音乐库是空的";
  const hint = document.querySelector("#musicModal .music-hint");
  if (hint) hint.style.visibility = musicFiles.length ? "" : "hidden";

  if (!musicFiles.length) {
    const empty = document.createElement("div");
    empty.className = "music-empty";
    empty.innerHTML = "还没有音乐。<br>拖几首进来，或者点上面的「从电脑选…」。";
    box.appendChild(empty);
    return;
  }

  musicFiles.forEach((f) => {
    const row = document.createElement("div");
    row.className = "music-row";
    row.dataset.name = f.name;
    row.dataset.path = f.path;
    if (cur === f.path) row.classList.add("in-use");

    const ico = document.createElement("span");
    ico.className = "music-ico";
    ico.textContent = "♪";

    const main = document.createElement("div");
    main.className = "music-main";
    const nm = document.createElement("b");
    nm.className = "music-name";
    nm.textContent = f.name.replace(/\.[^.]+$/, "");
    nm.title = f.name;
    const meta = document.createElement("span");
    meta.className = "music-meta";
    meta.textContent = `${fmtDur(f.duration)} · ${f.size_mb} MB`;
    main.append(nm, meta);
    row.append(ico, main);

    if (cur === f.path) {
      const tag = document.createElement("span");
      tag.className = "music-use";
      tag.textContent = "使用中";
      row.append(tag);
    }

    const play = document.createElement("button");
    play.className = "music-act";
    play.dataset.act = "play";
    play.title = "试听";
    play.textContent = f.name === musicPlaying ? "❚❚" : "▶";

    const del = document.createElement("button");
    del.className = "music-act del";
    del.dataset.act = "del";
    del.title = "移出音乐库（进回收目录，可找回）";
    del.textContent = "×";

    row.append(play, del);
    box.appendChild(row);
  });
  updateMusicPlaying();
}

async function reloadMusicLib() {
  const files = await fxLoadMusic();
  musicFiles = files || [];
  renderMusicList();
}

async function openMusicModal() {
  $("#musicModal").classList.remove("hidden");
  setMusicStatus("");
  renderMusicList();          // 先用缓存顶上，别一片空白
  await reloadMusicLib();
}

function closeMusicModal() {
  stopPreview();
  $("#musicModal").classList.add("hidden");
}

// 选中一首作为 BGM（面板和窗口同步）
async function fxUseMusic(path, name) {
  state.fx.bgm.path = path;
  state.fx.bgm.name = name || path.split(/[\\/]/).pop();
  state.fx.bgm.on = true;
  await fxLoadMusic();
  fxSync();
  const st = $("#bgmStatus");
  st.textContent = "已选：" + state.fx.bgm.name;
  st.className = "fx-note ok";
  renderMusicList();
}

function togglePreview(name) {
  if (musicPlaying === name) { stopPreview(); return; }
  stopPreview();
  const a = new Audio("/api/bgm/audio?name=" + encodeURIComponent(name));
  a.onended = () => stopPreview();
  a.onerror = () => {
    setMusicStatus("这首放不出来（格式浏览器不认，但剪辑时能用）", "err");
    stopPreview();
  };
  previewAudio = a;
  musicPlaying = name;
  updateMusicPlaying();
  a.play().catch(() => {
    setMusicStatus("浏览器挡住了播放，再点一次试试", "err");
    stopPreview();
  });
}

function armDelete(btn) {
  $$("#musicList .music-act.del").forEach((b) => {
    b.classList.remove("arm");
    b.textContent = "×";
  });
  btn.classList.add("arm");
  btn.textContent = "确认移走";
  clearTimeout(musicArmTimer);
  musicArmTimer = setTimeout(() => {
    $$("#musicList .music-act.del").forEach((b) => {
      b.classList.remove("arm");
      b.textContent = "×";
    });
  }, 2600);
}

async function deleteMusic(name) {
  const used = !!(state.fx && state.fx.bgm.path &&
    state.fx.bgm.path.split(/[\\/]/).pop() === name);
  if (musicPlaying === name) stopPreview();   // 先松手，Windows 才让移动文件
  const r = await api("/api/bgm/delete", { method: "POST", body: JSON.stringify({ name }) });
  if (!r.ok) {
    setMusicStatus(r.error || "移走失败", "err");
    return;
  }
  if (used) {
    state.fx.bgm.path = "";
    state.fx.bgm.name = "";
    state.fx.bgm.on = false;
    fxSync();
    const st = $("#bgmStatus");
    st.textContent = "原来那首被移走了，重新挑一首。";
    st.className = "fx-note warn";
  }
  await reloadMusicLib();
  setMusicStatus(`已移走「${name}」，放进了 music/.trash，反悔能拿回来`, "ok");
}

async function uploadMusic(fileList) {
  const list = Array.from(fileList || []).filter(
    (f) => /\.(mp3|wav|m4a|aac|flac|ogg|opus|wma|aiff)$/i.test(f.name));
  if (!list.length) {
    setMusicStatus("这些不是音频文件（要 mp3 / wav / m4a 这类）", "err");
    return;
  }
  setMusicStatus(`正在导入 ${list.length} 个文件…`);

  const fd = new FormData();
  list.forEach((f) => fd.append("files", f));
  try {
    const res = await fetch("/api/bgm/upload", { method: "POST", body: fd });
    const r = await res.json();
    if (r.error) throw new Error(r.error);
    await reloadMusicLib();
    const n = (r.added || []).length;
    const bad = (r.errors || []).length;
    if (n && !bad) {
      setMusicStatus(`导入 ${n} 首，点一首换上`, "ok");
      toast(`导入 ${n} 首音乐`, "success");
    } else if (n && bad) {
      setMusicStatus(`成功 ${n} 首，失败 ${bad} 首：${(r.errors || []).join("；")}`, "err");
    } else {
      setMusicStatus((r.errors || []).join("；") || "一首都没导进去", "err");
    }
  } catch (e) {
    setMusicStatus("导入失败：" + (e.message || e), "err");
  }
}

async function openMusicFolder() {
  const r = await api("/api/bgm/reveal", { method: "POST", body: JSON.stringify({}) });
  if (r.ok) setMusicStatus("已在资源管理器里打开 music 文件夹", "ok");
  else setMusicStatus(r.dir ? `音乐库位置：${r.dir}` : (r.error || "打不开文件夹"), "");
}

function musicBind() {
  $("#btnBgmLib").addEventListener("click", openMusicModal);
  $("#musicClose").addEventListener("click", closeMusicModal);
  $("#musicDone").addEventListener("click", closeMusicModal);
  $("#musicModal").addEventListener("click", (e) => {
    if (e.target.id === "musicModal") closeMusicModal();
  });
  $("#btnMusicFolder").addEventListener("click", openMusicFolder);

  const input = $("#musicFile");
  $("#btnMusicPick").addEventListener("click", () => input.click());
  $("#musicDrop").addEventListener("click", (e) => {
    if (e.target.closest("button")) return;
    input.click();
  });
  input.addEventListener("change", (e) => {
    uploadMusic(e.target.files);
    e.target.value = "";     // 同一个文件再选一次也能触发
  });

  const dz = $("#musicDrop");
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault();
    e.stopPropagation();
    dz.classList.add("over");
  }));
  dz.addEventListener("dragleave", (e) => {
    if (!dz.contains(e.relatedTarget)) dz.classList.remove("over");
  });
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
    dz.classList.remove("over");
    if (e.dataTransfer && e.dataTransfer.files.length) uploadMusic(e.dataTransfer.files);
  });
  // 拖到窗口别的地方，也别让浏览器直接把文件打开
  ["dragover", "drop"].forEach((ev) => $("#musicModal").addEventListener(ev, (e) => e.preventDefault()));

  // 列表：试听 / 选用 / 删除（事件委托，列表重绘后依然有效）
  $("#musicList").addEventListener("click", async (e) => {
    const row = e.target.closest(".music-row");
    if (!row) return;
    const act = e.target.closest(".music-act");
    const name = row.dataset.name;

    if (act && act.dataset.act === "play") {
      togglePreview(name);
      return;
    }
    if (act && act.dataset.act === "del") {
      if (act.classList.contains("arm")) await deleteMusic(name);
      else armDelete(act);
      return;
    }
    await fxUseMusic(row.dataset.path, name);
    setMusicStatus("已换上：" + name.replace(/\.[^.]+$/, ""), "ok");
  });
}

function fxBind() {
  $("#fxSubOn").addEventListener("change", (e) => {
    state.fx.subtitle.on = e.target.checked;
    fxSync();
  });
  $$("#fxSubSize button").forEach((b) => b.addEventListener("click", () => {
    state.fx.subtitle.size = b.dataset.val;
    fxSync();
  }));
  $$("#fxSubPos button").forEach((b) => b.addEventListener("click", () => {
    state.fx.subtitle.position = b.dataset.val;
    fxSync();
  }));
  $("#fxSubOutline").addEventListener("input", (e) => {
    state.fx.subtitle.outline_color = e.target.value;
  });
  $("#fxSubBox").addEventListener("change", (e) => {
    state.fx.subtitle.box = e.target.checked;
    fxSync();
  });
  $("#fxSubBoxAlpha").addEventListener("input", (e) => {
    state.fx.subtitle.box_alpha = Number(e.target.value);
    $("#fxSubBoxAlphaVal").textContent = e.target.value + "%";
  });
  $("#fxSubHighlight").addEventListener("change", (e) => {
    state.fx.subtitle.highlight = e.target.checked;
    if (e.target.checked && !(state.fx.cues || []).some((c) => c && c.words && c.words.length)) {
      toast("这批字幕没有词时间（导入的 SRT），点亮不生效，会整句显示", "error");
    }
    fxSync();
  });
  $("#fxSubHlColor").addEventListener("input", (e) => {
    state.fx.subtitle.highlight_color = e.target.value;
  });
  $("#btnSubTranscribe").addEventListener("click", fxTranscribe);
  $("#btnSubImport").addEventListener("click", fxImportSrt);

  $("#fxBgmOn").addEventListener("change", (e) => {
    state.fx.bgm.on = e.target.checked;
    fxSync();
  });
  $("#fxBgmVol").addEventListener("input", (e) => {
    state.fx.bgm.volume = Number(e.target.value) / 100;
    $("#fxBgmVolVal").textContent = e.target.value + "%";
  });
  $("#fxBgmKeep").addEventListener("change", (e) => {
    state.fx.bgm.keep_original = e.target.checked;
  });
  $("#fxBgmSel").addEventListener("change", (e) => {
    const v = e.target.value;
    if (!v) return;
    state.fx.bgm.path = v;
    state.fx.bgm.name = v.split(/[\\/]/).pop();
    state.fx.bgm.on = true;
    fxSync();
    renderMusicList();
    const st = $("#bgmStatus");
    st.textContent = "已选：" + state.fx.bgm.name;
    st.className = "fx-note ok";
  });
  musicBind();

  $$("#fxSpeed button").forEach((b) => b.addEventListener("click", () => {
    state.fx.speed = Number(b.dataset.val);
    fxSync();
  }));

  $("#fxNormOn").addEventListener("change", (e) => {
    state.fx.normalize = e.target.checked;
    fxSync();
  });

  $("#fxDenoiseOn").addEventListener("change", (e) => {
    state.fx.denoise = e.target.checked;
    fxSync();
  });

  $$("#fxFrame button").forEach((b) => b.addEventListener("click", () => {
    state.fx.frame = b.dataset.val;
    fxSync();
  }));

  $("#fxTransOn").addEventListener("change", (e) => {
    state.fx.transition.on = e.target.checked;
    // 关掉转场 = 连首尾淡入淡出一起关，回到纯硬切（想只留淡入淡出就再拖一下滑块）
    state.fx.transition.fadeTouched = false;
    fxSync();
  });
  $("#fxTransType").addEventListener("change", (e) => {
    state.fx.transition.type = e.target.value;
    fxTransLabel();
  });
  $("#fxTransDur").addEventListener("input", (e) => {
    state.fx.transition.duration = Number(e.target.value) / 10;
    fxTransLabel();
  });
  $("#fxFadeIn").addEventListener("input", (e) => {
    state.fx.transition.fade_in = Number(e.target.value) / 10;
    state.fx.transition.fadeTouched = true;
    fxTransLabel();
  });
  $("#fxFadeOut").addEventListener("input", (e) => {
    state.fx.transition.fade_out = Number(e.target.value) / 10;
    state.fx.transition.fadeTouched = true;
    fxTransLabel();
  });
}

/* ---------------- 交互绑定 ---------------- */
function seekTo(t) {
  const v = $("#video");
  if (!v.src || !state.info) return;
  v.currentTime = Math.min(t + 0.05, (v.duration || state.info.duration) - 0.1);
  if (v.paused) v.play().catch(() => {});
}

function togglePlay() {
  const v = $("#video");
  if (!v.src) return;
  if (v.paused) v.play().catch(() => {}); else v.pause();
}

function bind() {
  $("#btnPick").addEventListener("click", pickVideo);
  $("#btnPickBig").addEventListener("click", pickVideo);

  $("#btnAnalyze").addEventListener("click", () => {
    if (state.job && state.job.kind === "analyze") cancelJob();
    else analyze();
  });

  $("#btnRender").addEventListener("click", () => {
    if (state.job && state.job.kind === "render") cancelJob();
    else render();
  });

  $("#btnAuto").addEventListener("click", () => {
    if (state.job && state.job.kind === "autocut") cancelJob();
    else autocut();
  });

  $("#btnKit").addEventListener("click", () => {
    if (state.kit && state.kit.dir) {
      api("/api/open", { method: "POST", body: JSON.stringify({ path: state.kit.dir, mode: "folder" }) });
    } else {
      exportKit();
    }
  });

  $$("#strength button").forEach((b) => {
    b.addEventListener("click", () => {
      if (state.busy) return;
      state.strength = b.dataset.val;
      $$("#strength button").forEach((x) => x.classList.toggle("active", x === b));
      // 手动改力度不自动退出「口播」预设，按钮状态也不动——只是想微调一下
    });
  });

  const profBtn = $("#btnProfile");
  if (profBtn) profBtn.addEventListener("click", () => setProfile(!profileOn()));

  $("#btnPlay").addEventListener("click", togglePlay);

  const video = $("#video");
  video.addEventListener("timeupdate", () => {
    $("#curTime").textContent = fmt(video.currentTime);
    const d = video.duration || (state.info && state.info.duration) || 0;
    if (d) $("#playhead").style.left = (video.currentTime / d) * 100 + "%";
  });
  video.addEventListener("play", () => { $("#playIcon").innerHTML = '<path d="M8 5h3v14H8zM13 5h3v14h-3z"/>'; });
  video.addEventListener("pause", () => { $("#playIcon").innerHTML = '<path d="M8 5.5v13l11-6.5z"/>'; });
  video.addEventListener("loadedmetadata", () => {
    $("#durTime").textContent = fmt(video.duration);
    drawRuler(video.duration);
  });

  $("#timeline").addEventListener("click", (e) => {
    const d = (state.info && state.info.duration) || (state.analysis && state.analysis.duration);
    if (!d || !$("#playerWrap").classList.contains("has-video")) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const ratio = (e.clientX - rect.left) / rect.width;
    const video = $("#video");
    video.currentTime = Math.max(0, Math.min(video.duration || d, ratio * d));
  });

  $("#optSilent").addEventListener("change", (e) => applyToggle("silence", e.target.checked));
  $("#optDup").addEventListener("change", (e) => applyToggle("dup", e.target.checked));
  $("#optStatic").addEventListener("change", (e) => applyToggle("static", e.target.checked));
  $("#optBlank").addEventListener("change", (e) => applyToggle("blank", e.target.checked));
  // 口头禅/重说：勾上只是「这次粗剪要剪」，得点 AI 粗剪才会真的转写
  [["#optFiller", "filler", "口头禅"], ["#optRetake", "retake", "重说"]].forEach(([sel, type, name]) => {
    $(sel).addEventListener("change", (e) => {
      applyToggle(type, e.target.checked);
      if (e.target.checked && !(state.suggestions || []).some((s) => s.type === type)) {
        toast(`要剪${name}了：点「AI 粗剪」会先听写一遍，第一次会慢一些`, "info");
      }
    });
  });

  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const name = tab.dataset.tab;
      ["chat", "fx", "overview", "suggest"].forEach((k) => {
        const body = $("#tab-" + k);
        if (body) body.classList.toggle("hidden", k !== name);
      });
    });
  });

  // ---- 对话 ----
  $("#btnChatSend").addEventListener("click", () => {
    if (state.job && state.job.kind === "direct") cancelJob();
    else sendChat();
  });

  $("#chatText").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendChat();
    }
  });

  $$("#chatQuick button").forEach((b) => {
    b.addEventListener("click", () => {
      const t = $("#chatText");
      t.value = b.dataset.text || b.textContent.trim();
      t.focus();
    });
  });

  $("#btnLlmCfg").addEventListener("click", openLlmModal);
  $("#llmClose").addEventListener("click", closeLlmModal);
  $("#llmModal").addEventListener("click", (e) => {
    if (e.target.id === "llmModal") closeLlmModal();
  });
  $("#btnLlmSave").addEventListener("click", () => saveLlm(false));
  $("#btnLlmTest").addEventListener("click", testLlm);

  $("#decideToggle").addEventListener("click", () => {
    const open = $("#decideList").classList.toggle("open");
    $("#decideToggle").classList.toggle("open", open);
  });

  $("#btnExport").addEventListener("click", async () => {
    if (!state.analysis) return;
    const r = await api("/api/save-json", {
      method: "POST",
      body: JSON.stringify({ path: state.path, data: { video: state.path, info: state.info, ...state.analysis } }),
    });
    if (r.ok) toast("已保存：" + r.path); else toast(r.error || "保存失败", "error");
  });

  $("#btnOpenFolder").addEventListener("click", () => {
    if (state.output) api("/api/open", { method: "POST", body: JSON.stringify({ path: state.output.output, mode: "folder" }) });
  });
  $("#btnPlayOut").addEventListener("click", () => {
    if (state.output) api("/api/open", { method: "POST", body: JSON.stringify({ path: state.output.output, mode: "file" }) });
  });

  document.addEventListener("keydown", (e) => {
    const el = document.activeElement;
    const typing = el && (el.tagName === "TEXTAREA" ||
      (el.tagName === "INPUT" && !["checkbox", "radio"].includes(el.type)));

    if (e.code === "Space") {
      if (typing || (el && el.tagName === "INPUT")) return;   // 让复选框自己响应空格
      if (!$("#musicModal").classList.contains("hidden")) return;  // 音乐库开着就别控视频
      e.preventDefault();
      togglePlay();
      return;
    }

    if ((e.code === "Delete" || e.code === "Backspace") && state.selected && !typing) {
      e.preventDefault();
      removeItem(state.selected);
      return;
    }

    if (e.code === "Escape") {
      if (!$("#musicModal").classList.contains("hidden")) { closeMusicModal(); return; }
      if (!$("#llmModal").classList.contains("hidden")) { closeLlmModal(); return; }
      if (state.selected) {
        state.selected = null;
        drawTimeline();
        $$("#sugList .sug").forEach((r) => r.classList.remove("sel"));
      }
    }
  });

  bindTimelineDrag();

  // 效果面板
  state.fx = FX_DEFAULT();
  fxBind();
  fxSync();
  fxLoadMusic();

  // 心跳：窗口关掉后服务自动退出
  setInterval(() => fetch("/api/ping").catch(() => {}), 2000);
}

bind();
LABEL_RENDER = $("#btnRender").innerHTML;
LABEL_AUTO = $("#btnAuto").innerHTML;
LABEL_KIT = $("#btnKit").innerHTML;
setStatus("就绪");

// 深链接：?path=<视频路径>&analyze=1 可直接打开某个视频（可用于「用本工具打开」）
(function deepLink() {
  const params = new URLSearchParams(location.search);
  const path = params.get("path");
  if (!path) return;
  loadVideo(path).then(() => {
    if (params.get("analyze") === "1") analyze();
  });
})();
