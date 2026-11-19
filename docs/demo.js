/* ============================================================
 *  在线演示用的假接口层
 *
 *  只在 GitHub Pages 的静态预览里生效：拦截所有 /api/ 请求，
 *  返回与真实后端结构一致的数据，让界面可以正常点、正常走流程。
 *
 *  不会剪视频，也不会读写本地文件。
 *  本地运行时这个文件根本不会被加载（Flask 只认 web/ 目录）。
 * ============================================================ */
(function () {
  "use strict";

  // ---------------- 演示素材 ----------------
  var VIDEO = {
    name: "示例素材_城市漫步.mp4",
    duration: 46.8,
    width: 1920,
    height: 1080,
    fps: 30,
    size_mb: 128.4
  };

  var SCENES = [
    { start: 4.2, score: 0.94 }, { start: 9.8, score: 0.88 },
    { start: 15.6, score: 0.91 }, { start: 22.1, score: 0.76 },
    { start: 28.7, score: 0.96 }, { start: 34.9, score: 0.83 },
    { start: 41.2, score: 0.79 }
  ];

  var SILENT = [
    { start: 5.4, end: 7.1 }, { start: 12.3, end: 13.2 },
    { start: 19.8, end: 23.4 }, { start: 30.2, end: 31.0 },
    { start: 38.6, end: 41.5 }
  ];

  var REDUNDANT = [
    { start: 24.6, end: 27.2, similar_to: 3.1, similarity: 0.94 },
    { start: 33.5, end: 35.8, similar_to: 8.4, similarity: 0.91 }
  ];

  var STATIC = [{ start: 16.1, end: 18.2 }, { start: 42.3, end: 44.0 }];
  var BLANK = [{ start: 0.0, end: 0.6 }];

  var ANALYSIS = {
    scenes: SCENES,
    silent: SILENT,
    redundant: REDUNDANT,
    static: STATIC,
    blank: BLANK,
    has_audio: true,
    suggestions: [],
    duration: VIDEO.duration,
    fps: VIDEO.fps
  };

  var DECISIONS = [
    { type: "blank", start: 0.0, end: 0.6, saved: 0.6, reason: "开头黑场 0.6 秒，整段剪掉" },
    { type: "silence", start: 5.4, end: 7.1, saved: 1.2, reason: "静音 1.7 秒，两端各留 0.25 秒气口" },
    { type: "silence", start: 12.3, end: 13.2, saved: 0.6, reason: "静音 0.9 秒，两端留气口" },
    { type: "static", start: 16.1, end: 18.2, saved: 1.5, reason: "画面几乎不动 2.1 秒，保留开头 0.6 秒" },
    { type: "silence", start: 19.8, end: 23.4, saved: 2.8, reason: "静音 3.6 秒，长停顿只留两端气口" },
    { type: "dup", start: 24.6, end: 27.2, saved: 2.6, reason: "与 3.1s 处重复（相似度 94%），保留后一遍" },
    { type: "silence", start: 30.2, end: 31.0, saved: 0.5, reason: "静音 0.8 秒，剪掉中间" },
    { type: "dup", start: 33.5, end: 35.8, saved: 2.3, reason: "与 8.4s 处重复（相似度 91%），保留后一遍" },
    { type: "silence", start: 38.6, end: 41.5, saved: 2.1, reason: "静音 2.9 秒，收尾保留 0.7 秒" }
  ];

  var SAVED = 14.2;
  var KEPT = 32.6;

  var OUTPUT = {
    output: "示例素材_城市漫步_成片.mp4",
    name: "示例素材_城市漫步_成片.mp4",
    size_mb: 92.4,
    kept: KEPT,
    removed: SAVED,
    no_cut: false
  };

  var CUES = [
    { start: 0.8, end: 3.4, text: "早上七点的城市还没醒。" },
    { start: 3.9, end: 7.8, text: "我沿着河走，风比想象中冷。" },
    { start: 8.2, end: 12.1, text: "拐过第三个路口，才看到那家店。" },
    { start: 12.6, end: 17.4, text: "老板说，这条街二十年前全是梧桐。" },
    { start: 18.0, end: 22.8, text: "现在只剩两棵，被围在停车场中间。" },
    { start: 23.4, end: 28.6, text: "我拍了很久，也没拍到什么特别的东西。" },
    { start: 29.1, end: 33.2, text: "但风穿过树叶的声音，还是录下来了。" }
  ];

  var MUSIC = [
    { name: "清晨街道.mp3", path: "演示音乐库/清晨街道.mp3", size_mb: 4.2, duration: 132 },
    { name: "午后散步.mp3", path: "演示音乐库/午后散步.mp3", size_mb: 3.8, duration: 118 },
    { name: "黄昏收尾.mp3", path: "演示音乐库/黄昏收尾.mp3", size_mb: 5.1, duration: 156 }
  ];

  var PRESETS = {
    soft: { label: "很轻（口播）", volume: 0.14 },
    normal: { label: "适中（日常）", volume: 0.24 },
    strong: { label: "明显（混剪）", volume: 0.40 }
  };

  var MODEL_STATUS = {
    sizes: {
      tiny: { label: "极速", mb: 75, note: "最快，中文错字多，只适合试水", ready: true, loaded: false },
      base: { label: "轻量", mb: 145, note: "快，口播尚可，长句容易漏字", ready: true, loaded: false },
      small: { label: "推荐", mb: 484, note: "中文识别准，CPU 上速度可接受", ready: true, loaded: true },
      medium: { label: "高准", mb: 1500, note: "更准，慢 3～4 倍，长视频慎用", ready: false, loaded: false }
    },
    default: "small",
    dir: "models/whisper（演示）"
  };

  // ---------------- 胶片条 ----------------
  // 用 canvas 画一组随时间变化的缩略图，让时间轴看起来像真的素材
  function makeStrip(count) {
    var frames = [];
    var i;
    for (i = 0; i < count; i++) {
      var c = document.createElement("canvas");
      c.width = 160;
      c.height = 90;
      var g = c.getContext("2d");
      if (!g) {
        frames.push(null);
        continue;
      }
      var t = i / count;

      var bg = g.createLinearGradient(0, 0, 160, 90);
      bg.addColorStop(0, "hsl(" + (206 + t * 26) + ",34%," + (24 + t * 10) + "%)");
      bg.addColorStop(1, "hsl(" + (188 + t * 22) + ",38%," + (44 + t * 8) + "%)");
      g.fillStyle = bg;
      g.fillRect(0, 0, 160, 90);

      g.fillStyle = "rgba(8,14,26,0.55)";
      g.beginPath();
      g.moveTo(0, 62 + Math.sin(t * 6) * 8);
      g.lineTo(40, 50 + Math.cos(t * 5) * 10);
      g.lineTo(88, 58 + Math.sin(t * 7) * 9);
      g.lineTo(160, 46 + Math.cos(t * 4) * 8);
      g.lineTo(160, 90);
      g.lineTo(0, 90);
      g.closePath();
      g.fill();

      g.fillStyle = "rgba(255,255,255,0.86)";
      g.beginPath();
      g.arc(118 - t * 60, 20 + t * 6, 5.5, 0, Math.PI * 2);
      g.fill();

      frames.push(c.toDataURL("image/jpeg", 0.72));
    }
    return frames;
  }

  var STRIP = null;
  function strip() {
    if (!STRIP) STRIP = makeStrip(14);
    return STRIP;
  }

  // ---------------- 任务模拟 ----------------
  var jobs = Object.create(null);
  var jobSeq = 0;

  function makeJob(kind, script, result) {
    var id = "demo-" + (++jobSeq);
    jobs[id] = { kind: kind, t0: Date.now(), script: script, result: result, cancelled: false };
    return id;
  }

  function readJob(id) {
    var j = jobs[id];
    if (!j) return { ok: false, error: "任务不存在" };
    if (j.cancelled) {
      return { ok: true, job: { kind: j.kind, status: "cancelled", progress: 0, message: "已取消" } };
    }
    var el = Date.now() - j.t0;
    var last = j.script[j.script.length - 1];
    if (el >= last.t) {
      return {
        ok: true,
        job: { kind: j.kind, status: "done", progress: 100, message: "完成", result: j.result }
      };
    }
    var cur = j.script[0];
    var i;
    for (i = 0; i < j.script.length; i++) {
      if (el >= j.script[i].t) cur = j.script[i];
    }
    return {
      ok: true,
      job: { kind: j.kind, status: "running", progress: cur.p, message: cur.m }
    };
  }

  var S_ANALYZE = [
    { t: 0, p: 6, m: "正在读取素材…" },
    { t: 700, p: 26, m: "检测镜头切换…" },
    { t: 1500, p: 52, m: "分析音轨与静音段…" },
    { t: 2300, p: 74, m: "比对重复画面…" },
    { t: 3000, p: 92, m: "整理剪辑建议…" },
    { t: 3600, p: 100, m: "分析完成" }
  ];

  var S_AUTOCUT = [
    { t: 0, p: 4, m: "AI 正在看片…" },
    { t: 900, p: 18, m: "检测镜头与静音段…" },
    { t: 2000, p: 34, m: "比对重复画面…" },
    { t: 3200, p: 48, m: "转写语音，找出口头禅…" },
    { t: 4600, p: 62, m: "找出说错重来的段落…" },
    { t: 5800, p: 76, m: "按标准力度排剪辑点…" },
    { t: 7000, p: 88, m: "渲染成片…" },
    { t: 8200, p: 96, m: "写入输出文件…" },
    { t: 9000, p: 100, m: "粗剪完成" }
  ];

  var S_RENDER = [
    { t: 0, p: 5, m: "准备渲染…" },
    { t: 800, p: 24, m: "按时间线切分片段…" },
    { t: 1800, p: 46, m: "拼接画面…" },
    { t: 2900, p: 68, m: "处理音轨…" },
    { t: 3900, p: 86, m: "封装输出…" },
    { t: 4600, p: 100, m: "成片已生成" }
  ];

  var S_KIT = [
    { t: 0, p: 8, m: "正在打包精调包…" },
    { t: 900, p: 42, m: "导出分段素材…" },
    { t: 1900, p: 72, m: "生成时间线与清单…" },
    { t: 2700, p: 100, m: "打包完成" }
  ];

  var S_SUB = [
    { t: 0, p: 5, m: "加载语音模型…" },
    { t: 1200, p: 24, m: "正在听写…" },
    { t: 2600, p: 52, m: "正在听写…" },
    { t: 4000, p: 78, m: "断句与时间轴对齐…" },
    { t: 5200, p: 100, m: "转写完成" }
  ];

  var S_DIRECT = [
    { t: 0, p: 8, m: "正在理解需求…" },
    { t: 900, p: 30, m: "排剪辑方案…" },
    { t: 1900, p: 58, m: "渲染成片…" },
    { t: 3000, p: 84, m: "收尾…" },
    { t: 3800, p: 100, m: "完成" }
  ];

  var KIT_RESULT = {
    dir: "演示精调包",
    clips: [
      { name: "clip_01.mp4", start: 0.0, end: 5.4 },
      { name: "clip_02.mp4", start: 7.1, end: 12.3 },
      { name: "clip_03.mp4", start: 13.2, end: 16.1 },
      { name: "clip_04.mp4", start: 18.2, end: 19.8 },
      { name: "clip_05.mp4", start: 23.4, end: 24.6 },
      { name: "clip_06.mp4", start: 27.2, end: 30.2 }
    ],
    edl: "演示精调包/timeline.edl",
    csv: "演示精调包/剪辑清单.csv",
    srt: "演示精调包/字幕.srt"
  };

  // ---------------- 路由 ----------------
  function route(path) {
    var m;

    if (path === "/api/pick") {
      return { ok: true, path: VIDEO.name, paths: [VIDEO.name] };
    }
    if (path === "/api/info") {
      return { ok: true, info: VIDEO };
    }
    if (path === "/api/filmstrip") {
      return { ok: true, frames: strip(), fps: VIDEO.fps, duration: VIDEO.duration };
    }

    if (path === "/api/analyze") {
      return { ok: true, job: makeJob("analyze", S_ANALYZE, ANALYSIS) };
    }
    if (path === "/api/autocut") {
      var result = {
        analysis: ANALYSIS,
        plan: { decisions: DECISIONS, strength_label: "标准", saved: SAVED },
        output: OUTPUT.output,
        name: OUTPUT.name,
        size_mb: OUTPUT.size_mb,
        kept: OUTPUT.kept,
        removed: OUTPUT.removed,
        no_cut: false,
        cues: CUES,
        fx_applied: { subtitle: CUES.length }
      };
      return { ok: true, job: makeJob("autocut", S_AUTOCUT, result) };
    }
    if (path === "/api/render") {
      return { ok: true, job: makeJob("render", S_RENDER, OUTPUT) };
    }
    if (path === "/api/export-kit") {
      return { ok: true, job: makeJob("kit", S_KIT, KIT_RESULT) };
    }
    if (path === "/api/direct") {
      var directResult = {
        reply: "听懂了：**剪成 30 秒左右的版本**，开头 3 秒不要，去掉停顿和重复。\n已经按这个方案出片了，剪掉 " + SAVED + " 秒。",
        output: OUTPUT.output,
        name: OUTPUT.name,
        size_mb: OUTPUT.size_mb,
        kept: OUTPUT.kept,
        removed: OUTPUT.removed,
        no_cut: false
      };
      return { ok: true, job: makeJob("direct", S_DIRECT, directResult) };
    }

    if (path === "/api/subtitle/status") {
      var status = { ok: true };
      var k;
      for (k in MODEL_STATUS) {
        if (Object.prototype.hasOwnProperty.call(MODEL_STATUS, k)) status[k] = MODEL_STATUS[k];
      }
      return status;
    }
    if (path === "/api/subtitle/transcribe") {
      return { ok: true, job: makeJob("subtitle", S_SUB, { cues: CUES }) };
    }
    if (path === "/api/subtitle/parse") {
      return { ok: true, count: CUES.length, cues: CUES };
    }
    if (path === "/api/subtitle/save") {
      return { ok: true, path: "演示字幕.srt" };
    }

    if (path === "/api/bgm/list") {
      return { ok: true, files: MUSIC, dir: "演示音乐库", presets: PRESETS };
    }

    if (path === "/api/settings") {
      return { ok: true, llm: { base_url: "", model: "", api_key: "", enabled: true, ready: false } };
    }
    if (path === "/api/llm-test") {
      return { ok: true, reply: "在线演示没有接入大模型，这里只是走个过场。", model: "demo" };
    }

    if (path === "/api/ping") {
      return { ok: true };
    }

    if (path === "/api/open") {
      return { ok: true };
    }
    if (path === "/api/save-json") {
      return { ok: true, path: "演示时间线.json" };
    }

    // 任务轮询
    m = path.match(/^\/api\/job\/([^/]+)\/cancel$/);
    if (m) {
      if (jobs[m[1]]) jobs[m[1]].cancelled = true;
      return { ok: true };
    }
    m = path.match(/^\/api\/job\/([^/]+)$/);
    if (m) {
      return readJob(m[1]);
    }

    // 其余接口一律当作成功，避免演示页冒出红色报错
    return { ok: true };
  }

  // ---------------- 拦截 fetch ----------------
  var realFetch = window.fetch ? window.fetch.bind(window) : null;

  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    var cut = url.indexOf("?");
    var path = (cut >= 0 ? url.slice(0, cut) : url).replace(/^https?:\/\/[^/]+/, "");

    if (path.indexOf("/api/") !== 0) {
      return realFetch ? realFetch(input, init) : Promise.reject(new Error("fetch unavailable"));
    }

    var data;
    try {
      data = route(path);
    } catch (e) {
      data = { ok: true };
    }

    return Promise.resolve(
      new Response(JSON.stringify(data), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
  };

  // ---------------- 让播放器有画面 ----------------
  // 真实的 src 是 /api/video?path=...，演示里换成自带的示例片段
  function attachVideo() {
    var v = document.getElementById("video");
    if (!v) return;

    var fix = function () {
      var src = v.getAttribute("src") || "";
      if (src.indexOf("/api/video") === 0) {
        v.setAttribute("src", "assets/sample.mp4");
        v.load();
      }
    };

    if (window.MutationObserver) {
      new MutationObserver(fix).observe(v, { attributes: true, attributeFilter: ["src"] });
    }
    fix();
  }

  // 打开就用示例素材填满界面，读者不用先猜要点哪里
  function autoLoad() {
    var btn = document.getElementById("btnPick");
    if (!btn) return;
    setTimeout(function () {
      if (!btn.disabled) btn.click();
    }, 600);
  }

  function boot() {
    attachVideo();
    autoLoad();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
