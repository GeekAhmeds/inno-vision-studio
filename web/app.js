// Inno Vision Studio — first UI (P0). Plain JS, no build step.
// All data goes into the page through textContent, never innerHTML.

const view = document.getElementById("view");
let META = null;

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) el.append(c.nodeType ? c : String(c));
  return el;
}

// replaceChildren, but accepting arrays and skipping null/false.
function mount(el, ...kids) {
  el.replaceChildren(...kids.flat(2).filter((k) => k != null && k !== false));
}

async function api(path, opts = {}) {
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers = { "Content-Type": "application/json" };
  }
  const res = await fetch("/api" + path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "حدث خطأ غير متوقع");
  return data;
}

function toast(text, bad = false) {
  const t = document.getElementById("toast");
  t.textContent = text; t.className = bad ? "bad" : ""; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), bad ? 8000 : 3500);
}

// Run an action from a button: disable while busy, show errors in plain words.
function act(fn) {
  return async (ev) => {
    const btn = ev.currentTarget; btn.disabled = true;
    try { await fn(ev); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  };
}

function field(label, input) { return h("label", {}, label, input); }
function explain(...lines) { return h("div", { class: "card explain" }, lines.map((l) => h("p", {}, l))); }
function when(ts) { return ts ? new Date(ts).toLocaleString("ar-EG") : "—"; }

// ---------------------------------------------------------------- home

async function home() {
  const list = await api("/projects");
  const name = h("input", { placeholder: "مثال: عيش مخبز خبزة" });
  const product = h("input", { placeholder: "مثال: عيش، معلبات…" });
  const template = h("select", {}, Object.entries(META.templates).map(([k, v]) => h("option", { value: k }, v.ar)));
  const importer = h("input", { type: "file", accept: ".zip", hidden: true, onchange: async () => {
    if (!importer.files.length) return;
    const fd = new FormData(); fd.append("file", importer.files[0]);
    try { const p = await api("/projects/import", { method: "POST", body: fd }); location.hash = "#/p/" + p.slug; }
    catch (e) { toast(e.message, true); }
  } });

  mount(view, 
    h("h1", {}, "المشاريع"),
    h("p", { class: "muted" }, "كل مشروع له اسمه وبياناته وموديلاته، محفوظة في فولدر مستقل."),
    explain(
      "ابدأ بمشروع لكل منتج تريد تدريب موديل عليه. بعدها تضيف له كاميرات أو فيديوهات أو صورًا.",
      "لا شيء يُدرَّب قبل أن تراجع البيانات بنفسك، وكل خطوة تُسجَّل وتُشرح في سجل المشروع."),
    h("div", { class: "card" },
      h("h2", {}, "مشروع جديد"),
      h("div", { class: "row" },
        field("اسم المشروع", name), field("المنتج", product), field("نوع المهمة", template),
        h("button", { class: "primary", onclick: act(async () => {
          const p = await api("/projects", { method: "POST", json: { name: name.value, product: product.value, template: template.value } });
          location.hash = "#/p/" + p.slug;
        }) }, "إنشاء"),
        h("button", { onclick: () => importer.click() }, "استيراد مشروع"), importer)),
    h("div", { class: "grid" }, list.map((p) =>
      h("a", { class: "card project", href: "#/p/" + p.slug },
        h("h2", {}, p.name, p.archived ? h("span", { class: "tag warn" }, "مؤرشف") : null),
        h("div", { class: "muted" }, [p.product, META.templates[p.task_type]?.ar].filter(Boolean).join(" · ")),
        h("div", { class: "stats" },
          h("span", {}, "فيديو ", h("b", {}, p.stats.videos), " (", h("b", {}, p.stats.video_hours), " س)"),
          h("span", {}, "صور ", h("b", {}, p.stats.images)),
          h("span", {}, "تسميات مراجَعة ", h("b", {}, p.stats.labels_human)),
          h("span", {}, "موديلات ", h("b", {}, p.stats.models))),
        h("div", { class: "muted" }, "آخر نشاط: ", when(p.stats.last_activity))))),
    list.length ? null : h("p", { class: "muted" }, "لا توجد مشاريع بعد."));
}

// ---------------------------------------------------------------- project

const TABS = { sources: "المصادر", data: "البيانات", classes: "الأنواع", counting: "خط العدّ", truth: "المسطرة", harness: "القياس", log: "ماذا حدث؟" };

async function project(slug, tab = "sources") {
  const p = await api("/projects/" + slug);
  const body = h("div");
  mount(view, 
    h("h1", {}, p.name),
    h("div", { class: "muted" }, [p.product, META.templates[p.task_type]?.ar, "المعرّف: " + p.slug].filter(Boolean).join(" · ")),
    h("div", { class: "stats" },
      h("span", {}, "فيديو ", h("b", {}, p.stats.videos), " (", h("b", {}, p.stats.video_hours), " ساعة)"),
      h("span", {}, "صور ", h("b", {}, p.stats.images)),
      h("span", {}, "تسميات راجعها إنسان ", h("b", {}, p.stats.labels_human)),
      h("span", {}, "تسميات غير مراجَعة ", h("b", {}, p.stats.labels_unreviewed)),
      h("span", {}, "موديلات ", h("b", {}, p.stats.models))),
    h("div", { class: "tabs" },
      Object.entries(TABS).map(([k, v]) => h("button", { class: k === tab ? "on" : "", onclick: () => (location.hash = `#/p/${slug}/${k}`) }, v)),
      h("button", { onclick: () => (location.href = `/api/projects/${slug}/export`) }, "تصدير"),
      h("button", { onclick: act(async () => {
        const name = prompt("الاسم الجديد للمشروع", p.name);
        if (name) { await api("/projects/" + slug, { method: "PATCH", json: { name } }); project(slug, tab); }
      }) }, "إعادة تسمية"),
      h("button", { onclick: act(async () => {
        const name = prompt("اسم النسخة", p.name + " (نسخة)");
        if (name) { const c = await api(`/projects/${slug}/duplicate`, { method: "POST", json: { name } }); location.hash = "#/p/" + c.slug; }
      }) }, "نسخ"),
      h("button", { onclick: act(async () => {
        await api("/projects/" + slug, { method: "PATCH", json: { archived: !p.archived } }); project(slug, tab);
      }) }, p.archived ? "إلغاء الأرشفة" : "أرشفة"),
      h("button", { class: "danger", onclick: act(async () => {
        const typed = prompt(`الحذف نهائي ويمسح بيانات وموديلات المشروع. للتأكيد اكتب المعرّف: ${slug}`);
        if (typed === null) return;
        await api(`/projects/${slug}?confirm=${encodeURIComponent(typed)}`, { method: "DELETE" }); location.hash = "#/";
      }) }, "حذف")),
    body);
  const reload = () => project(slug, tab);
  const tabs = { sources: sourcesTab, data: dataTab, classes: classesTab, counting: countingTab, truth: truthTab, harness: harnessTab, log: logTab };
  await (tabs[tab] || sourcesTab)(slug, p, body, reload);
}

async function sourcesTab(slug, p, body, reload) {
  const kind = h("select", {}, Object.entries(META.source_kinds).map(([k, v]) => h("option", { value: k }, v.ar)));
  const name = h("input", { placeholder: "مثال: كاميرا السير 1" });
  const url = h("input", { dir: "ltr", placeholder: "rtsp://192.168.1.50:554/stream1" });
  const user = h("input", { dir: "ltr", placeholder: "admin" });
  const pass = h("input", { dir: "ltr", type: "password" });
  const device = h("select", {}, h("option", { value: "" }, "اضغط «بحث عن كاميرات»"));
  const path = h("input", { dir: "ltr", placeholder: "/home/me/videos/clip.mp4 أو C:\\videos\\clip.mp4" });
  const net = h("div", { class: "row" }, field("عنوان البث", url), field("اسم المستخدم", user), field("كلمة السر", pass));
  const usb = h("div", { class: "row" }, field("الكاميرا", device),
    h("button", { onclick: act(async () => {
      const cams = await api("/usb-cameras");
      mount(device, ...(cams.length ? cams.map((c) => h("option", { value: c.device }, `كاميرا ${c.device} — ${c.width}×${c.height}`))
        : [h("option", { value: "" }, "لم توجد كاميرا")]));
    }) }, "بحث عن كاميرات"));
  const file = h("div", { class: "row" }, field("مسار الملف على هذا الجهاز", path));
  const show = () => { net.hidden = !["ip", "nvr"].includes(kind.value); usb.hidden = kind.value !== "usb"; file.hidden = kind.value !== "video"; };
  kind.onchange = show; show();

  const add = act(async () => {
    const json = { kind: kind.value, name: name.value };
    if (["ip", "nvr"].includes(kind.value)) {
      let u = url.value.trim();
      if (user.value && !/\/\/[^/]*@/.test(u)) u = u.replace("://", "://" + encodeURIComponent(user.value) + "@");
      json.url = u; json.password = pass.value;
    } else if (kind.value === "usb") json.device = device.value === "" ? null : Number(device.value);
    else json.path = path.value.trim();
    await api(`/projects/${slug}/sources`, { method: "POST", json }); reload();
  });

  mount(body, 
    explain(
      "المصدر هو ما يرى به البرنامج: كاميرا IP، قناة على جهاز تسجيل، كاميرا USB، أو ملف فيديو.",
      "بعد الإضافة اضغط «اختبار الاتصال» لترى صورة فعلية والدقة والسرعة المقاسة. كلمة السر تُحفظ في خزنة النظام وليست في ملفات المشروع.",
      "الكاميرا الحية تُسجَّل أولًا ثم نعمل على التسجيل، حتى يمكن إعادة أي نتيجة."),
    h("div", { class: "card" }, h("h2", {}, "إضافة مصدر"),
      h("div", { class: "row" }, field("النوع", kind), field("الاسم", name)), net, usb, file,
      h("div", { class: "row" }, h("button", { class: "primary", onclick: add }, "إضافة"))),
    p.sources.length ? p.sources.map((s) => sourceCard(slug, s, reload)) : h("p", { class: "muted" }, "لا توجد مصادر بعد."));
}

function sourceCard(slug, s, reload) {
  const out = h("div");
  const seconds = h("input", { type: "number", value: 60, min: 1, max: 3600, style: "max-width:110px" });
  return h("div", { class: "card" },
    h("h2", {}, s.name), h("div", { class: "muted", dir: "ltr" }, META.source_kinds[s.kind].en, " · ", s.params.url || s.params.path || `device ${s.params.device}`,
      s.has_password ? " · 🔒" : ""),
    h("div", { class: "row", style: "margin-top:10px" },
      h("button", { class: "primary", onclick: act(async () => {
        mount(out, "جارٍ الاختبار…");
        const r = await api(`/projects/${slug}/sources/${s.id}/test`, { method: "POST" });
        mount(out, r.ok
          ? h("div", {}, h("div", { class: "ok" }, `يعمل: ${r.width}×${r.height} بمعدل ${r.fps} صورة/ث`),
              h("img", { class: "preview", src: `/api/projects/${slug}/file/${r.preview}?t=${Date.now()}`, alt: "معاينة" }))
          : h("div", { class: "bad" }, r.error_ar));
      }) }, "اختبار الاتصال"),
      h("label", { style: "flex:0 0 auto" }, "مدة التسجيل (ث)", seconds),
      h("button", { onclick: act(async () => {
        mount(out, "جارٍ التسجيل… لا تغلق الصفحة.");
        const m = await api(`/projects/${slug}/sources/${s.id}/record`, { method: "POST", json: { seconds: Number(seconds.value) } });
        mount(out, h("div", { class: "ok" }, `سُجّل مقطع ${m.width}×${m.height}، ${m.frames} صورة. تجده في تبويب البيانات.`));
      }) }, "تسجيل مقطع"),
      h("button", { class: "danger", onclick: act(async () => {
        if (confirm("حذف هذا المصدر؟ التسجيلات السابقة تبقى.")) { await api(`/projects/${slug}/sources/${s.id}`, { method: "DELETE" }); reload(); }
      }) }, "حذف")),
    out);
}

async function dataTab(slug, p, body, reload) {
  const media = await api(`/projects/${slug}/media`);
  const names = Object.fromEntries(p.classes.map((c) => [c.id, c.display_name]));
  const upload = (endpoint, files, named) => act(async () => {
    if (!files().length) return toast("اختر ملفات أولًا", true);
    const fd = new FormData();
    for (const f of files()) named ? fd.append("files", f, f.webkitRelativePath || f.name) : fd.append(endpoint.endsWith("video") ? "file" : "files", f);
    const r = await api(`/projects/${slug}/media/${endpoint}`, { method: "POST", body: fd });
    toast(r.classes ? `أُضيف ${Object.keys(r.classes).length} منتج` : r.added != null ? `أُضيفت ${r.added} صورة` : "أُضيف الفيديو"); reload();
  });
  const video = h("input", { type: "file", accept: "video/*,.mkv,.ts,.dav" });
  const images = h("input", { type: "file", accept: "image/*", multiple: true });
  const folders = h("input", { type: "file", webkitdirectory: true, multiple: true });
  const link = h("input", { dir: "ltr", placeholder: "/path/to/video.mp4" });
  const angle = h("input", { dir: "ltr", value: "angle-1", style: "max-width:140px" });

  mount(body, 
    explain(
      "هنا تدخل البيانات: فيديو، صور متفرقة، أو فولدر فيه فولدر لكل منتج.",
      "الفيديو الكبير الموجود على هذا الجهاز يُربط في مكانه دون نسخ. لكل ملف بصمة، فلا يُسجَّل مرتين.",
      "تنبيه: صور المنتج النظيفة تعرّف الأنواع، لكنها لا تغني عن تصوير من الكاميرا الحقيقية التي سيعمل عليها الموديل."),
    h("div", { class: "card" }, h("h2", {}, "فيديو"),
      h("div", { class: "row" }, field("ربط فيديو موجود على الجهاز (بدون نسخ)", link), field("زاوية الكاميرا", angle),
        h("button", { class: "primary", onclick: act(async () => {
          await api(`/projects/${slug}/media/video-link`, { method: "POST", json: { path: link.value.trim(), angle_id: angle.value.trim() } }); reload();
        }) }, "ربط")),
      h("div", { class: "row", style: "margin-top:10px" }, field("أو ارفع ملف فيديو", video),
        h("button", { onclick: act(async () => {
          if (!video.files.length) return toast("اختر ملفًا أولًا", true);
          const fd = new FormData(); fd.append("file", video.files[0]); fd.append("angle_id", angle.value.trim());
          await api(`/projects/${slug}/media/video`, { method: "POST", body: fd }); reload();
        }) }, "رفع"))),
    h("div", { class: "card" }, h("h2", {}, "فولدر لكل منتج"),
      h("p", { class: "muted" }, "اختر فولدرًا واحدًا بداخله فولدر لكل منتج، وفي كل واحد صوره. اسم الفولدر يصبح اسم النوع."),
      h("div", { class: "row" }, field("الفولدر الرئيسي", folders),
        h("button", { class: "primary", onclick: upload("product-folders", () => [...folders.files], true) }, "إضافة"))),
    h("div", { class: "card" }, h("h2", {}, "صور متفرقة (بدون تسمية)"),
      h("div", { class: "row" }, field("الصور", images), h("button", { onclick: upload("images", () => [...images.files]) }, "رفع"))),
    h("div", { class: "card" }, h("h2", {}, `المسجَّل في المشروع (${media.length})`),
      media.length ? h("table", {},
        h("tr", {}, ["#", "النوع", "الاسم", "الدقة", "المدة", "الزاوية", "التسمية", "الحالة"].map((t) => h("th", {}, t))),
        media.map((m) => h("tr", {},
          h("td", { class: "num" }, m.id), h("td", {}, m.kind === "video" ? "فيديو" : "صورة"), h("td", { dir: "ltr" }, m.origin_name),
          h("td", { class: "num" }, `${m.width}×${m.height}`),
          h("td", { class: "num" }, m.duration_s ? (m.duration_s / 60).toFixed(1) + " د" : "—"),
          h("td", { dir: "ltr" }, m.angle_id || "—"), h("td", {}, m.classes ? m.classes.split(",").map((c) => names[c] || c).join("، ") : "—"),
          h("td", {}, m.missing ? h("span", { class: "tag warn" }, "الملف غير موجود في مكانه") : m.linked_path ? "مربوط" : "داخل المشروع"))))
        : h("p", { class: "muted" }, "لا توجد بيانات بعد.")));
}

async function classesTab(slug, p, body, reload) {
  const name = h("input", { placeholder: "مثال: سياحي كبير" });
  mount(body, 
    explain(
      "النوع (class) هو ما تريد أن يميّزه الموديل. له اسم تراه أنت بأي لغة، ومعرّف ثابت بحروف إنجليزية يستخدمه البرنامج.",
      "المعرّف لا يتغير بعد استخدامه في أي نسخة بيانات، حتى لو غيّرت الاسم المعروض."),
    h("div", { class: "card" }, h("div", { class: "row" }, field("اسم النوع", name),
      h("button", { class: "primary", onclick: act(async () => {
        await api(`/projects/${slug}/classes`, { method: "POST", json: { display_name: name.value } }); reload();
      }) }, "إضافة نوع"))),
    h("div", { class: "card" }, p.classes.length ? h("table", {},
      h("tr", {}, h("th", {}, "الاسم"), h("th", {}, "المعرّف")),
      p.classes.map((c) => h("tr", {}, h("td", {}, h("span", { class: "swatch", style: `background:${c.colour}` }), c.display_name), h("td", { dir: "ltr" }, c.id))))
      : h("p", { class: "muted" }, "لا توجد أنواع بعد. تُضاف تلقائيًا عند رفع «فولدر لكل منتج».")));
}

async function logTab(slug, p, body) {
  const entries = await api(`/projects/${slug}/activity`);
  mount(body, 
    explain("هذا سجل كل ما حدث في المشروع: ماذا تم، ولماذا، وبأي أرقام. الأحدث أولًا.",
      "كل رقم في أي تقرير لاحق يُؤخذ من هذا السجل."),
    h("div", { class: "card" }, h("ul", { class: "timeline" }, entries.map((e) => h("li", {},
      h("time", {}, when(e.ts)), " · ", h("span", { class: "muted" }, e.user),
      h("div", {}, e.ar), e.why_ar ? h("div", { class: "why" }, "لماذا؟ ", e.why_ar) : null,
      Object.keys(e.numbers || {}).length ? h("div", { class: "why", dir: "ltr" },
        Object.entries(e.numbers).map(([k, v]) => `${k}: ${v}`).join(" · ")) : null)))));
}


// ---------------------------------------------------------------- counting line

const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

// Arrow from the middle of the line toward the side where objects are counted.
function drawLine(layer, line, direction, w, h) {
  mount(layer);
  layer.setAttribute("viewBox", `0 0 ${w} ${h}`);
  if (!line) return;
  const [x1, y1, x2, y2] = line;
  const stroke = Math.max(2, w / 300);
  layer.append(svg("line", { x1, y1, x2, y2, stroke: "#f59e0b", "stroke-width": stroke * 1.5 }));
  const mx = (x1 + x2) / 2, my = (y1 + y2) / 2, len = Math.hypot(x2 - x1, y2 - y1) || 1;
  // "after" side = positive signed distance: normal (dy, -dx) in image coordinates.
  const nx = (y2 - y1) / len, ny = -(x2 - x1) / len, a = Math.min(w, h) / 8;
  const arrow = (sx) => {
    const ex = mx + sx * nx * a, ey = my + sx * ny * a;
    layer.append(svg("line", { x1: mx, y1: my, x2: ex, y2: ey, stroke: "#0f766e", "stroke-width": stroke * 1.5 }));
    layer.append(svg("circle", { cx: ex, cy: ey, r: stroke * 3, fill: "#0f766e" }));
  };
  if (direction === "both") { arrow(1); arrow(-1); } else arrow(direction === "negative" ? -1 : 1);
}

async function countingTab(slug, p, body, reload) {
  const data = await api(`/projects/${slug}/counting`);
  const cfg = data.config;
  let line = cfg.line, pending = null;
  if (!data.angles.length) {
    mount(body, explain("أضف فيديو أولًا (تبويب البيانات). أول فيديو لكل زاوية كاميرا يحفظ صورة مرجعية نرسم عليها خط العدّ."));
    return;
  }
  const angle = data.angles.find((a) => a.angle_id === cfg.angle_id) || data.angles[0];
  const img = h("img", { src: `/api/projects/${slug}/file/${angle.reference}`, style: "width:100%;display:block;border-radius:8px" });
  const layer = svg("svg", { style: "position:absolute;inset:0;width:100%;height:100%;pointer-events:none" });
  const direction = h("select", {}, h("option", { value: "positive" }, "في اتجاه السهم فقط"), h("option", { value: "both" }, "في الاتجاهين"));
  direction.value = cfg.direction === "both" ? "both" : "positive";
  const hyst = h("input", { type: "number", value: cfg.hysteresis_px, min: 0, step: 1, style: "max-width:100px" });
  const redraw = () => drawLine(layer, line, direction.value, angle.width, angle.height);
  direction.onchange = redraw;
  const stage = h("div", { style: "position:relative;cursor:crosshair;user-select:none" }, img, layer);
  stage.addEventListener("click", (ev) => {
    const r = img.getBoundingClientRect();
    const x = Math.round((ev.clientX - r.left) * angle.width / r.width), y = Math.round((ev.clientY - r.top) * angle.height / r.height);
    if (!pending) { pending = [x, y]; line = null; mount(layer); layer.append(svg("circle", { cx: x, cy: y, r: angle.width / 150, fill: "#f59e0b" })); }
    else { line = [...pending, x, y]; pending = null; redraw(); }
  });
  img.onload = redraw;
  mount(body,
    explain("اضغط نقطتين على الصورة لرسم خط العدّ عبر السير. كل رغيف يُعدّ مرة واحدة عند عبوره الخط في اتجاه السهم.",
      "ضع الخط في منطقة يكون فيها الرغيف كاملًا ومنفصلًا عن غيره، مع مسافة كافية قبله لجمع صور الرغيف للتصنيف.",
      "إذا كان السهم عكس حركة السير اضغط «اعكس الاتجاه»."),
    h("div", { class: "card" },
      h("div", { class: "row" },
        field("زاوية الكاميرا", h("select", { disabled: data.angles.length < 2 }, data.angles.map((a) => h("option", { value: a.angle_id }, `${a.angle_id} (${a.width}×${a.height})`)))),
        field("العدّ", direction), field("هامش بعد الخط (بكسل)", hyst),
        h("button", { onclick: () => { if (line) { line = [line[2], line[3], line[0], line[1]]; redraw(); } } }, "اعكس الاتجاه"),
        h("button", { class: "primary", onclick: act(async () => {
          if (!line) return toast("ارسم الخط أولًا بنقطتين", true);
          await api(`/projects/${slug}/counting`, { method: "PUT", json: { line, direction: direction.value, angle_id: angle.angle_id, hysteresis_px: Number(hyst.value) } });
          toast("حُفظ خط العدّ");
        }) }, "حفظ")),
      h("div", { style: "margin-top:12px" }, stage)));
}

// ---------------------------------------------------------------- truth

const STATUS_AR = { needs_count_1: "يحتاج العدّة الأولى", needs_count_2: "يحتاج العدّة الثانية", needs_reconcile: "يحتاج مطابقة العدّتين", final: "نهائي", locked: "نهائي ومقفول" };

function fmtTime(s) { const m = Math.floor(s / 60); return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`; }
function parseTime(v) { v = String(v).trim(); if (v.includes(":")) { const [m, s] = v.split(":"); return Number(m) * 60 + Number(s); } return Number(v); }

async function truthTab(slug, p, body, reload) {
  const [data, videos] = await Promise.all([api(`/projects/${slug}/truth`), api(`/projects/${slug}/media?kind=video`)]);
  const media = h("select", {}, videos.map((v) => h("option", { value: v.id }, `#${v.id} ${v.origin_name} (${((v.duration_s || 0) / 60).toFixed(1)} د)`)));
  const start = h("input", { dir: "ltr", value: "0:00", style: "max-width:110px" });
  const end = h("input", { dir: "ltr", value: "3:00", style: "max-width:110px" });
  const cond = h("select", {}, Object.entries(data.conditions).map(([k, v]) => h("option", { value: k }, v)));
  const note = h("input", { placeholder: "اختياري" });
  const perCond = {};
  for (const c of data.clips) if (c.final_taps) perCond[c.condition] = (perCond[c.condition] || 0) + c.final_taps.length;

  mount(body,
    explain("المسطرة هي عدّ بشري لمقاطع قصيرة، وهي المقياس الوحيد لأي موديل. اختر مقاطع من ظروف مختلفة: زحمة، تراكم، أيدي، ليل، سير فاضي.",
      "كل مقطع يُعدّ مرتين (شخصان، أو نفس الشخص في يومين). البرنامج يطابق العدّتين ويعرض لك الاختلافات لتحسمها.",
      `الحكم على أي حالة يحتاج ${data.min_per_condition} عبور على الأقل فيها، لأن فرق رغيفين في عدد صغير ضوضاء.`,
      "هذه الأجزاء من الفيديو لن تُستخدم في التدريب أبدًا."),
    videos.length ? h("div", { class: "card" }, h("h2", {}, "مقطع مسطرة جديد"),
      h("div", { class: "row" }, field("الفيديو", media), field("من (د:ث)", start), field("إلى (د:ث)", end), field("الحالة", cond), field("ملاحظة", note),
        h("button", { class: "primary", onclick: act(async () => {
          await api(`/projects/${slug}/truth`, { method: "POST", json: { media_id: Number(media.value), start_s: parseTime(start.value), end_s: parseTime(end.value), condition: cond.value, note: note.value } });
          reload();
        }) }, "إنشاء")))
      : explain("أضف فيديو أولًا من تبويب البيانات."),
    Object.keys(data.conditions).some((c) => perCond[c]) ? h("div", { class: "card" }, h("h2", {}, "ما جُمع حتى الآن (عبور نهائي لكل حالة)"),
      h("div", { class: "stats" }, Object.entries(data.conditions).filter(([k]) => perCond[k]).map(([k, v]) =>
        h("span", {}, v, " ", h("b", { class: perCond[k] >= data.min_per_condition ? "ok" : "" }, perCond[k]), ` / ${data.min_per_condition}`)))) : null,
    h("div", { class: "card" }, data.clips.length ? h("table", {},
      h("tr", {}, ["#", "الفيديو", "الوقت", "الحالة", "العدّات", "المسطرة", "الوضع", ""].map((t) => h("th", {}, t))),
      data.clips.map((c) => h("tr", {},
        h("td", { class: "num" }, c.id), h("td", { dir: "ltr" }, c.media.origin_name),
        h("td", { class: "num" }, `${fmtTime(c.start_s)}–${fmtTime(c.end_s)}`), h("td", {}, data.conditions[c.condition] || c.condition),
        h("td", {}, c.counts.map((n) => `${n.counter}: ${n.n}${n.finished ? "" : " (لم تنتهِ)"}`).join("، ") || "—"),
        h("td", { class: "num" }, c.final_taps ? c.final_taps.length : "—"),
        h("td", {}, STATUS_AR[c.status] || c.status),
        h("td", {}, h("div", { class: "row" },
          c.final_taps ? null : h("button", { class: "primary", onclick: act(async () => {
            const open = c.counts.find((n) => !n.finished && n.counter === META.user);
            const cnt = open || await api(`/projects/${slug}/truth/${c.id}/counts`, { method: "POST" });
            location.hash = `#/p/${slug}/truth/${c.id}/count/${cnt.id}`;
          }) }, c.counts.some((n) => !n.finished) ? "أكمل العدّ" : "عُدّ"),
          c.counts.filter((n) => n.finished).length >= 2 || c.final_taps ? h("button", { onclick: () => (location.hash = `#/p/${slug}/truth/${c.id}`) }, c.final_taps ? "عرض" : "طابق العدّتين") : null,
          c.locked ? null : h("button", { class: "danger", onclick: act(async () => {
            if (confirm("حذف مقطع المسطرة وعدّاته؟")) { await api(`/projects/${slug}/truth/${c.id}`, { method: "DELETE" }); reload(); }
          }) }, "حذف"))))))
      : h("p", { class: "muted" }, "لا توجد مقاطع مسطرة بعد.")));
}

async function countPage(slug, clipId, countId) {
  const p = await api("/projects/" + slug);
  let clip = await api(`/projects/${slug}/truth/${clipId}`);
  const count = clip.counts.find((c) => c.id === countId);
  if (!count) throw new Error("العدّة غير موجودة");
  const counting = (await api(`/projects/${slug}/counting`)).config;
  const classes = p.classes.length ? p.classes : [{ id: "unknown", display_name: "رغيف / عنصر", colour: "#2563eb" }];
  let taps = count.taps.slice(), current = classes[0].id, stamps = null, dirty = false;
  const back = h("a", { href: `#/p/${slug}/truth` }, "← المسطرة");

  // Wait for the browser copy of the clip.
  const holder = h("div", { class: "card" }, "جارٍ تجهيز نسخة المتصفح من المقطع…");
  mount(view, back, h("h1", {}, `عدّ المقطع #${clipId}`), holder);
  while (clip.proxy.state === "building") {
    mount(holder, `جارٍ تجهيز نسخة المتصفح من المقطع… ${Math.round((clip.proxy.progress || 0) * 100)}%`);
    await new Promise((r) => setTimeout(r, 1500));
    clip = await api(`/projects/${slug}/truth/${clipId}`);
  }
  const ready = clip.proxy.state === "ready";
  if (ready) stamps = await api(`/projects/${slug}/truth/${clipId}/stamps`);
  const scale = ready ? clip.proxy.scale : 1;
  const W = clip.media.width, H = clip.media.height;

  // The player: the WebM copy, or single frames from the server if it could not be made.
  let player, now, seek, playPause;
  if (ready) {
    player = h("video", { src: `/api/projects/${slug}/truth/${clipId}/proxy.webm`, preload: "auto", style: "width:100%;display:block;border-radius:8px", muted: true });
    now = () => stamps[Math.min(stamps.length - 1, Math.max(0, Math.round(player.currentTime * clip.proxy.fps)))];
    seek = (d) => (player.currentTime = Math.max(0, player.currentTime + d));
    playPause = () => (player.paused ? player.play() : player.pause());
  } else {
    let t = clip.start_s, timer = null;
    player = h("img", { style: "width:100%;display:block;border-radius:8px" });
    const show = () => (player.src = `/api/projects/${slug}/media/${clip.media_id}/frame?t=${t.toFixed(2)}&width=960`);
    now = () => t;
    seek = (d) => { t = Math.min(clip.end_s, Math.max(clip.start_s, t + d)); show(); };
    playPause = () => { if (timer) { clearInterval(timer); timer = null; } else timer = setInterval(() => seek(0.25), 250); };
    show();
  }
  const layer = svg("svg", { style: "position:absolute;inset:0;width:100%;height:100%;pointer-events:none" });
  drawLine(layer, counting.line, counting.direction, W, H);
  const stage = h("div", { style: "position:relative;cursor:crosshair" }, player, layer);
  const list = h("ol", { class: "timeline", style: "max-height:420px;overflow:auto" });
  const counter = h("b", {}, "0");
  const clsButtons = h("div", { class: "row" });
  const status = h("span", { class: "muted" });

  const renderTaps = () => {
    mount(counter, String(taps.length));
    for (const n of layer.querySelectorAll(".tap")) n.remove();
    const t = now();
    for (const tap of taps) if (tap.x != null && Math.abs(tap.t - t) < 0.6)
      layer.append(svg("circle", { class: "tap", cx: tap.x, cy: tap.y, r: W / 60, fill: "none", stroke: "#db2777", "stroke-width": W / 400 }));
    mount(list, taps.slice().reverse().slice(0, 200).map((tap) => h("li", {}, h("span", { class: "num" }, fmtTime(tap.t)), " · ",
      (classes.find((c) => c.id === tap.class_id) || { display_name: tap.class_id }).display_name)));
  };
  const renderClasses = () => mount(clsButtons, classes.map((c, i) => h("button", { class: c.id === current ? "primary" : "", onclick: () => { current = c.id; renderClasses(); } },
    `${i + 1} · ${c.display_name}`)));
  const save = async (finished = false) => {
    await api(`/projects/${slug}/truth-counts/${countId}`, { method: "PUT", json: { taps, finished } });
    dirty = false; mount(status, "حُفظ " + new Date().toLocaleTimeString("ar-EG"));
  };
  const autosave = setInterval(() => { if (dirty) save().catch((e) => toast(e.message, true)); }, 5000);
  window.addEventListener("hashchange", () => clearInterval(autosave), { once: true });

  stage.addEventListener("click", (ev) => {
    const r = player.getBoundingClientRect();
    const x = Math.round((ev.clientX - r.left) * W / r.width), y = Math.round((ev.clientY - r.top) * H / r.height);
    taps.push({ t: now(), class_id: current, x, y }); taps.sort((a, b) => a.t - b.t); dirty = true; renderTaps();
  });
  const onKey = (ev) => {
    if (!document.body.contains(stage)) return document.removeEventListener("keydown", onKey);
    if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") return;
    if (ev.code === "Space") { ev.preventDefault(); playPause(); }
    else if (ev.key === "ArrowRight") seek(-1);   // RTL page: right arrow goes back in time
    else if (ev.key === "ArrowLeft") seek(1);
    else if (ev.key === "z" || ev.key === "Z" || ev.key === "ي") { if (taps.length) { const t = now(); let k = 0; taps.forEach((tap, i) => { if (Math.abs(tap.t - t) < Math.abs(taps[k].t - t)) k = i; }); taps.splice(k, 1); dirty = true; renderTaps(); } }
    else if (/^[1-9]$/.test(ev.key) && classes[+ev.key - 1]) { current = classes[+ev.key - 1].id; renderClasses(); }
  };
  document.addEventListener("keydown", onKey);
  if (ready) { player.addEventListener("timeupdate", renderTaps); player.addEventListener("seeked", renderTaps); }
  else player.addEventListener("load", renderTaps);
  const speed = h("select", { onchange: () => { if (ready) player.playbackRate = Number(speed.value); } },
    [0.25, 0.5, 0.75, 1].map((v) => h("option", { value: v, selected: v === 0.5 }, `×${v}`)));
  if (ready) player.playbackRate = 0.5;

  renderClasses(); renderTaps();
  mount(view, back,
    h("h1", {}, `عدّ المقطع #${clipId}`),
    h("div", { class: "muted" }, `${clip.media.origin_name} · ${fmtTime(clip.start_s)}–${fmtTime(clip.end_s)} · العادّ: ${count.counter}`),
    explain("شغّل المقطع، وكلما عبر رغيف خط العدّ اضغط عليه بالماوس. الضغط على الرغيف نفسه يسجّل مكانه، وهذا يفرّق بين العدّ المزدوج والرغيف الفائت عند التراكم.",
      "اختر النوع قبل الضغط بالأزرار أو بالأرقام 1–9. المسافة: تشغيل/إيقاف. الأسهم: ثانية للأمام أو للخلف. حرف Z: يحذف أقرب ضغطة للحظة الحالية.",
      "السرعة البطيئة (×0.5) أدق. الحفظ تلقائي كل 5 ثوانٍ، واضغط «انتهيت» في الآخر.",
      ready ? "" : "تعذّر تجهيز نسخة فيديو للمتصفح، فيُعرض المقطع صورة بصورة."),
    h("div", { style: "display:grid;grid-template-columns:minmax(0,3fr) minmax(220px,1fr);gap:16px;align-items:start" },
      h("div", { class: "card" }, stage,
        h("div", { class: "row", style: "margin-top:10px" },
          h("button", { onclick: playPause }, "تشغيل / إيقاف"), h("button", { onclick: () => seek(-2) }, "−2 ث"), h("button", { onclick: () => seek(2) }, "+2 ث"),
          h("label", { style: "flex:0 0 auto" }, "السرعة", speed)),
        h("div", { style: "margin-top:10px" }, clsButtons)),
      h("div", { class: "card" }, h("h2", {}, "العبور: ", counter), status,
        h("div", { class: "row", style: "margin:10px 0" },
          h("button", { onclick: act(() => save(false)) }, "حفظ"),
          h("button", { class: "primary", onclick: act(async () => {
            if (!confirm(`إنهاء العدّة بـ ${taps.length} عبور؟ لن يمكن تعديلها بعد ذلك.`)) return;
            await save(true); location.hash = `#/p/${slug}/truth`;
          }) }, "انتهيت")),
        list)));
}

async function reconcilePage(slug, clipId) {
  const p = await api("/projects/" + slug);
  const clip = await api(`/projects/${slug}/truth/${clipId}`);
  const names = Object.fromEntries(p.classes.map((c) => [c.id, c.display_name])); names.unknown = "غير محدد";
  const back = h("a", { href: `#/p/${slug}/truth` }, "← المسطرة");
  if (clip.final_taps) {
    mount(view, back, h("h1", {}, `المقطع #${clipId}: ${clip.final_taps.length} عبور نهائي`),
      h("div", { class: "card" }, h("p", {}, clip.locked ? "المسطرة مقفولة ولن تتغير." : "المسطرة نهائية. اقفلها حتى لا تتغير بعد الآن."),
        clip.locked ? null : h("button", { class: "primary", onclick: act(async () => { await api(`/projects/${slug}/truth/${clipId}/lock`, { method: "POST" }); reconcilePage(slug, clipId); }) }, "اقفل المسطرة")));
    return;
  }
  const rec = await api(`/projects/${slug}/truth/${clipId}/reconcile`);
  const keepA = new Set(), keepB = new Set(), cls = {};
  const thumb = (t, x, y) => h("img", { src: `/api/projects/${slug}/media/${clip.media_id}/frame?t=${t}&width=480` + (x != null ? `&mark_x=${x}&mark_y=${y}` : ""), style: "width:320px;border-radius:6px;display:block" });
  const item = (tap, set, who) => h("div", { class: "card", style: "display:flex;gap:12px;align-items:center" }, thumb(tap.t, tap.x, tap.y),
    h("div", {}, h("div", {}, `${fmtTime(tap.t)} · ${names[tap.class_id] || tap.class_id}`), h("div", { class: "muted" }, `عدّه ${who} فقط`),
      h("label", { style: "flex-direction:row;display:flex;gap:6px;align-items:center" },
        h("input", { type: "checkbox", style: "width:auto", onchange: (ev) => (ev.target.checked ? set.add(tap.index) : set.delete(tap.index)) }), "عبور حقيقي، أبقِه")));
  mount(view, back, h("h1", {}, `مطابقة عدّتي المقطع #${clipId}`),
    explain(`العدّة الأولى (${rec.count_a.counter}): ${rec.count_a.n} · الثانية (${rec.count_b.counter}): ${rec.count_b.n} · اتفقتا على ${rec.pairs}.`,
      rec.status === "agreed" ? "العدّتان متفقتان تمامًا." : "راجع كل اختلاف: الصورة من لحظة الضغط. ما لا تُبقيه يُحذف من المسطرة."),
    rec.only_in_a.length ? h("h2", {}, "في العدّة الأولى فقط") : null, rec.only_in_a.map((t) => item(t, keepA, rec.count_a.counter)),
    rec.only_in_b.length ? h("h2", {}, "في العدّة الثانية فقط") : null, rec.only_in_b.map((t) => item(t, keepB, rec.count_b.counter)),
    rec.class_conflicts.length ? h("h2", {}, "نفس العبور بنوعين مختلفين") : null,
    rec.class_conflicts.map((c) => h("div", { class: "card", style: "display:flex;gap:12px;align-items:center" }, thumb(c.t),
      h("div", {}, h("div", {}, fmtTime(c.t)), h("select", { onchange: (ev) => (cls[c.a] = ev.target.value) },
        h("option", { value: "" }, "اختر النوع"), [c.class_a, c.class_b].map((k) => h("option", { value: k }, names[k] || k)))))),
    h("div", { class: "card" }, h("button", { class: "primary", onclick: act(async () => {
      await api(`/projects/${slug}/truth/${clipId}/resolve`, { method: "POST", json: { decisions: { keep_a: [...keepA], keep_b: [...keepB], class: cls } } });
      reconcilePage(slug, clipId);
    }) }, "اعتمد المسطرة النهائية")));
}

// ---------------------------------------------------------------- harness

async function harnessTab(slug, p, body, reload) {
  const reports = await api(`/projects/${slug}/reports`);
  const model = h("input", { dir: "ltr", placeholder: "/home/me/models/bread_r2.onnx أو C:\\models\\bread_r2.onnx" });
  const fps = h("input", { dir: "ltr", value: "3, 5, 10, 20", style: "max-width:160px" });
  const tracker = h("select", {}, h("option", { value: "bytetrack" }, "ByteTrack (الجديد)"), h("option", { value: "greedy" }, "Greedy (القديم، للمقارنة)"));
  const latency = h("input", { type: "number", value: 0, min: 0, step: 10, style: "max-width:110px" });
  const out = h("div");
  mount(body,
    explain("القياس يشغّل الموديل على كل مقاطع المسطرة النهائية، بنفس كود العدّ الذي يعمل في المخبز، ويطابق كل عدّة مع عدّ الإنسان.",
      "جرّب أكثر من سرعة معالجة (صورة/ث) لتعرف ما يحتاجه جهاز المخبز. «زمن الجهاز» يحاكي جهازًا أبطأ: الصور التي تصل وهو مشغول تُفقد كما في البث الحي.",
      "النتيجة: الفائت والزائد منفصلان لكل حالة، مع تقرير فيه صور حقيقية من الأخطاء."),
    h("div", { class: "card" }, h("div", { class: "row" },
      field("ملف الموديل (.onnx) على هذا الجهاز", model), field("سرعات المعالجة", fps), field("المتتبّع", tracker), field("زمن الجهاز لكل صورة (ms)", latency),
      h("button", { class: "primary", onclick: act(async () => {
        mount(out, h("p", {}, "جارٍ القياس… قد يستغرق دقائق."));
        const r = await api(`/projects/${slug}/harness`, { method: "POST", json: {
          model_path: model.value.trim(), fps_list: fps.value.split(/[,،\s]+/).filter(Boolean).map(Number), tracker: tracker.value, latency_s: Number(latency.value) / 1000 } });
        mount(out, h("table", {}, h("tr", {}, ["صورة/ث", "المسطرة", "النظام", "فائت", "زائد", "دقة النوع"].map((t) => h("th", {}, t))),
          r.runs.map((x) => h("tr", {}, h("td", { class: "num" }, x.fps), h("td", { class: "num" }, x.overall.truth), h("td", { class: "num" }, x.overall.events),
            h("td", { class: "num" }, `${x.overall.misses} (${(100 * x.overall.miss_rate).toFixed(1)}%)`),
            h("td", { class: "num" }, `${x.overall.extras} (${(100 * x.overall.extra_rate).toFixed(1)}%)`),
            h("td", { class: "num" }, x.overall.type_accuracy == null ? "—" : (100 * x.overall.type_accuracy).toFixed(1) + "%")))),
          h("p", {}, h("a", { href: `/api/projects/${slug}/reports/${r.report_dir.split("/").pop()}/report.html`, target: "_blank" }, "افتح التقرير الكامل")));
      }) }, "قِس")), out),
    h("div", { class: "card" }, h("h2", {}, "تقارير سابقة"), reports.length ? h("table", {},
      h("tr", {}, ["الوقت", "الموديل", "المتتبّع", "صورة/ث", "المسطرة", "فائت", "زائد", ""].map((t) => h("th", {}, t))),
      reports.map((r) => h("tr", {}, h("td", {}, when(r.created_at)), h("td", { dir: "ltr" }, r.model_sha || "—"), h("td", {}, r.tracker),
        h("td", { class: "num" }, (r.fps || []).join("، ")), h("td", { class: "num" }, r.truth), h("td", { class: "num" }, r.misses), h("td", { class: "num" }, r.extras),
        h("td", {}, h("a", { href: `/api/projects/${slug}/reports/${r.name}/report.html`, target: "_blank" }, "التقرير")))))
      : h("p", { class: "muted" }, "لا توجد تقارير بعد.")));
}

// ---------------------------------------------------------------- router

async function route() {
  try {
    META = META || await api("/meta");
    document.getElementById("workspace").textContent = "مكان الحفظ: " + META.workspace;
    const parts = location.hash.replace(/^#\//, "").split("/").filter(Boolean);
    if (parts[0] === "p" && parts[1] && parts[2] === "truth" && parts[3] && parts[4] === "count") await countPage(parts[1], +parts[3], +parts[5]);
    else if (parts[0] === "p" && parts[1] && parts[2] === "truth" && parts[3]) await reconcilePage(parts[1], +parts[3]);
    else if (parts[0] === "p" && parts[1]) await project(parts[1], parts[2]);
    else await home();
  } catch (e) { mount(view, h("div", { class: "card bad" }, e.message)); }
}
window.addEventListener("hashchange", route);
route();
