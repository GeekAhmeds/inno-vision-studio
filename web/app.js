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

const TABS = { sources: "المصادر", data: "البيانات", classes: "الأنواع", log: "ماذا حدث؟" };

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
  await ({ sources: sourcesTab, data: dataTab, classes: classesTab, log: logTab }[tab] || sourcesTab)(slug, p, body, reload);
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

// ---------------------------------------------------------------- router

async function route() {
  try {
    META = META || await api("/meta");
    document.getElementById("workspace").textContent = "مكان الحفظ: " + META.workspace;
    const parts = location.hash.replace(/^#\//, "").split("/").filter(Boolean);
    if (parts[0] === "p" && parts[1]) await project(parts[1], parts[2]);
    else await home();
  } catch (e) { mount(view, h("div", { class: "card bad" }, e.message)); }
}
window.addEventListener("hashchange", route);
route();
