"""Replay truth clips through inno_count exactly as the agent runs it (prompt §6 module 9).

Same core, same settings from the project, a fixed processing rate, and optionally the latency of
the target hardware. Every event is matched to the final human taps; misses and extras are
reported separately per condition, with 95 % intervals, and a condition with fewer crossings than
the minimum is marked as not yet enough to judge.
"""

from __future__ import annotations

import base64
import html
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

import inno_count
from inno_count.bytetrack import ByteTrack
from inno_count.counter import CounterConfig, LineCounter
from inno_count.detector import ModelLoadError, OnnxDetector
from inno_count.engine import Engine, frame_at, read_video
from inno_count.geometry import Line, PlaneCalibration
from inno_count.geometry import Area
from inno_count.legacy import GreedyCentroidTracker
from inno_count.types import ClassificationEvent, CountEvent, EntryEvent

from .. import db
from ..explain import activity
from ..projects import get_counting, get_project, project_dir
from ..truth import CONDITIONS, get_clip
from .match import match, wilson

TRACKERS = {"bytetrack": ByteTrack, "greedy": GreedyCentroidTracker}
MIN_CROSSINGS_PER_CONDITION = 300
MATCH_WINDOW_S = 1.5


class HarnessError(Exception):
    """A problem the owner can understand and fix."""


def media_path(slug: str, media_id: int) -> Path:
    folder = project_dir(slug)
    with db.connect(folder) as con:
        row = con.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    if row is None:
        raise HarnessError(f"Media #{media_id} not found.")
    path = folder / row["rel_path"] if row["rel_path"] else Path(row["linked_path"])
    if not path.is_file():
        raise HarnessError(f"The video is not at its path any more: {path}")
    return path


def load_detector(model_path: str | Path) -> OnnxDetector:
    try:
        return OnnxDetector(model_path)
    except ModelLoadError as exc:
        raise HarnessError(str(exc)) from exc


def build_engine(slug: str, detector: Any, tracker: str = "bytetrack", fps: float | None = 10.0,
                 latency_s: float = 0.0, model_sha: str = "", class_names: list[str] | None = None) -> Engine:
    counting = get_counting(slug)
    if not counting["line"]:
        raise HarnessError("Draw the counting line first (Counting tab).")
    if tracker not in TRACKERS:
        raise HarnessError(f"Unknown tracker: {tracker}")
    cal = PlaneCalibration.from_dict(counting["calibration"]) if counting["calibration"] else None
    cfg = CounterConfig(
        line=Line(*counting["line"]), direction=counting["direction"], hysteresis_px=counting["hysteresis_px"],
        roi=tuple(counting["roi"]) if counting["roi"] else None,
        area=Area(counting["area"]) if counting.get("area") else None, edge_margin_px=counting["edge_margin_px"],
        k_crops=counting["k_crops"], classify_timeout_s=counting["classify_timeout_s"], calibration=cal,
        class_names=class_names, source_id=slug, model_sha=model_sha)
    entry = None
    if counting.get("entry_line"):
        entry = LineCounter(CounterConfig(
            line=Line(*counting["entry_line"]), direction=counting["direction"],
            hysteresis_px=counting["hysteresis_px"], area=cfg.area, source_id=slug, role="entry"))
    return Engine(detector, TRACKERS[tracker](), LineCounter(cfg), target_fps=fps, simulated_latency_s=latency_s,
                  entry_counter=entry)


def run_clip(slug: str, clip_id: int, detector: Any, tracker: str = "bytetrack", fps: float | None = 10.0,
             latency_s: float = 0.0, model_sha: str = "", class_names: list[str] | None = None) -> dict[str, Any]:
    clip = get_clip(slug, clip_id)
    if clip["final_taps"] is None:
        raise HarnessError(f"Truth clip #{clip_id} has no final truth yet (it needs two counts and reconciliation).")
    path = media_path(slug, clip["media_id"])
    engine = build_engine(slug, detector, tracker, fps, latency_s, model_sha, class_names)
    started = time.perf_counter()
    det_time, events = 0.0, []
    original_detect = engine.detector.detect

    def timed_detect(frame, ts=None):
        nonlocal det_time
        t0 = time.perf_counter()
        out = original_detect(frame, ts)
        det_time += time.perf_counter() - t0
        return out

    engine.detector.detect = timed_detect
    try:
        for ts, frame in read_video(path, clip["start_s"], clip["end_s"]):
            events += engine.process(ts, frame)
        events += engine.finish()
    finally:
        engine.detector.detect = original_detect
    counts = [e for e in events if isinstance(e, CountEvent)]
    entries = [e for e in events if isinstance(e, EntryEvent)] if engine.entry_counter is not None else None
    types = {e.uid: e for e in events if isinstance(e, ClassificationEvent)}
    taps = clip["final_taps"]
    xy = taps and all(t["x"] is not None for t in taps)
    m = match([t["t"] for t in taps], [e.ts for e in counts], window_s=MATCH_WINDOW_S,
              truth_cls=[t["class_id"] for t in taps],
              event_cls=[types[e.uid].cls if e.uid in types else "unknown" for e in counts],
              truth_xy=[(t["x"], t["y"]) for t in taps] if xy else None,
              event_xy=[(e.x, e.y) for e in counts] if xy else None)
    k = engine.counter.cfg.k_crops
    linked = sum(1 for e in counts if e.uid in types and types[e.uid].n_crops >= k)
    return {
        "clip_id": clip_id,
        "condition": clip["condition"],
        "media_id": clip["media_id"],
        "start_s": clip["start_s"],
        "end_s": clip["end_s"],
        "tracker": tracker,
        "fps": fps,
        "latency_s": latency_s,
        "frames_seen": engine.frames_seen,
        "frames_processed": engine.frames_processed,
        "detector_ms_per_frame": round(1000 * det_time / max(engine.frames_processed, 1), 2),
        "wall_s": round(time.perf_counter() - started, 2),
        "link_rate": round(linked / len(counts), 4) if counts else None,
        "summary": m.summary(),
        "events": [e.to_dict() for e in counts],
        "classifications": [e.to_dict() for e in types.values()],
        "misses": [taps[i] for i in m.misses],
        "miss_ids": sorted(int(i) for i in m.misses),   # indices into the clip's final taps: lets the gate pair two models
        "extras": [counts[i].to_dict() for i in m.extras],
        "used_position": bool(xy),
        # With an entry line: how many entered, and entered minus counted = taken off between the lines.
        "entries": len(entries) if entries is not None else None,
        "taken_between": len(entries) - len(counts) if entries is not None else None,
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    truth = sum(r["summary"]["truth"] for r in results)
    ev = sum(r["summary"]["events"] for r in results)
    misses = sum(r["summary"]["misses"] for r in results)
    extras = sum(r["summary"]["extras"] for r in results)
    confusion: dict[str, dict[str, int]] = {}
    for r in results:
        for t, row in r["summary"]["confusion"].items():
            for e, n in row.items():
                confusion.setdefault(t, {}).setdefault(e, 0)
                confusion[t][e] += n
    typed = sum(n for row in confusion.values() for e, n in row.items() if e != "unknown")
    right = sum(row.get(t, 0) for t, row in confusion.items())
    links = [r["link_rate"] for r in results if r["link_rate"] is not None]
    with_entry = [r for r in results if r.get("entries") is not None]
    return {
        "clips": len(results), "truth": truth, "events": ev, "misses": misses, "extras": extras,
        "miss_rate": round(misses / truth, 4) if truth else 0.0,
        "miss_rate_ci95": [round(v, 4) for v in wilson(misses, truth)],
        "extra_rate": round(extras / ev, 4) if ev else 0.0,
        "extra_rate_ci95": [round(v, 4) for v in wilson(extras, ev)],
        "type_accuracy": round(right / typed, 4) if typed else None,
        "confusion": confusion,
        "link_rate": round(float(np.mean(links)), 4) if links else None,
        "enough": truth >= MIN_CROSSINGS_PER_CONDITION,
        "entries": sum(r["entries"] for r in with_entry) if with_entry else None,
        "taken_between": sum(r["taken_between"] for r in with_entry) if with_entry else None,
    }


def run_harness(slug: str, model_path: str | Path | None = None, detector: Any = None, clip_ids: list[int] | None = None,
                fps_list: list[float | None] = (10.0,), tracker: str = "bytetrack", latency_s: float = 0.0,
                write_report: bool = True) -> dict[str, Any]:
    folder = project_dir(slug)
    if detector is None:
        if model_path is None:
            raise HarnessError("Choose a model file (.onnx) to measure.")
        detector = load_detector(model_path)
    model_sha = getattr(detector, "sha256", "") or ""
    class_names = getattr(detector, "names", None) or None
    with db.connect(folder) as con:
        all_ids = [r["id"] for r in con.execute("SELECT id FROM truth_clips WHERE final_taps IS NOT NULL ORDER BY id")]
    ids = clip_ids or all_ids
    if not ids:
        raise HarnessError("There is no truth clip with final truth yet.")
    started = time.perf_counter()
    runs = []
    for fps in fps_list:
        results = [run_clip(slug, cid, detector, tracker, fps, latency_s, model_sha, class_names) for cid in ids]
        by_condition: dict[str, list[dict[str, Any]]] = {}
        for r in results:
            by_condition.setdefault(r["condition"], []).append(r)
        runs.append({
            "fps": fps,
            "overall": _aggregate(results),
            "conditions": {c: _aggregate(rs) for c, rs in by_condition.items()},
            "clips": results,
        })
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": slug,
        "model_path": str(model_path) if model_path else None,
        "model_sha": model_sha,
        "counting_core_version": inno_count.__version__,
        "tracker": tracker,
        "latency_s": latency_s,
        "counting": get_counting(slug),
        "match_window_s": MATCH_WINDOW_S,
        "min_crossings_per_condition": MIN_CROSSINGS_PER_CONDITION,
        "runs": runs,
        "wall_s": round(time.perf_counter() - started, 2),
    }
    if write_report:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = folder / "reports" / f"harness_{stamp}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "report.html").write_text(render_html(slug, report), encoding="utf-8")
        report["report_dir"] = out.relative_to(folder).as_posix()
    best = runs[0]["overall"]
    activity.log(
        folder, "harness.run",
        f"قِيس الموديل على {len(ids)} مقطع مسطرة: فائت {best['misses']} من {best['truth']}، "
        f"عدّ زائد {best['extras']} من {best['events']} (بمعدل {runs[0]['fps']} صورة/ث).",
        f"Model measured on {len(ids)} truth clips: missed {best['misses']} of {best['truth']}, "
        f"extra {best['extras']} of {best['events']} (at {runs[0]['fps']} fps).",
        why_ar="الفائت والزائد يُعدّان منفصلين، لأن الإجمالي قد يخفي أخطاء تلغي بعضها.",
        why_en="Misses and extras are counted separately, because a total can hide errors that cancel out.",
        inputs={"model_sha": model_sha[:12], "tracker": tracker, "fps": [r["fps"] for r in runs], "clips": ids},
        numbers={k: best[k] for k in ("truth", "events", "misses", "extras", "miss_rate", "extra_rate")},
        outputs={"report": report.get("report_dir")}, duration_s=report["wall_s"])
    return report


# ------------------------------------------------------------------ HTML report

def _thumb(path: Path, t: float, x: float | None, y: float | None, line: list[float], colour) -> str | None:
    frame = frame_at(path, t)
    if frame is None:
        return None
    cv2.line(frame, (int(line[0]), int(line[1])), (int(line[2]), int(line[3])), (0, 200, 255), 2)
    h, w = frame.shape[:2]
    if x is not None and y is not None:
        cv2.circle(frame, (int(x), int(y)), 28, colour, 3)
        x1, y1 = int(min(max(x - 120, 0), max(w - 240, 0))), int(min(max(y - 90, 0), max(h - 180, 0)))
        frame = frame[y1:y1 + 180, x1:x1 + 240]
    else:
        frame = cv2.resize(frame, (320, int(320 * h / w)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return base64.b64encode(buf.tobytes()).decode() if ok else None


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{100 * v:.1f}%"


def render_html(slug: str, report: dict[str, Any]) -> str:
    esc = html.escape
    meta = get_project(slug)
    names = {c["id"]: c["display_name"] for c in meta["classes"]}
    names["unknown"] = "غير معروف"
    rows = []
    for run in report["runs"]:
        for cond, agg in [("الكل", run["overall"])] + [(CONDITIONS.get(c, c), a) for c, a in run["conditions"].items()]:
            rows.append(
                f"<tr><td>{run['fps']}</td><td>{esc(cond)}</td><td class=n>{agg['clips']}</td><td class=n>{agg['truth']}</td>"
                f"<td class=n>{agg['events']}</td><td class=n><b>{agg['misses']}</b> ({_pct(agg['miss_rate'])}, "
                f"{_pct(agg['miss_rate_ci95'][0])}–{_pct(agg['miss_rate_ci95'][1])})</td>"
                f"<td class=n><b>{agg['extras']}</b> ({_pct(agg['extra_rate'])}, "
                f"{_pct(agg['extra_rate_ci95'][0])}–{_pct(agg['extra_rate_ci95'][1])})</td>"
                f"<td class=n>{_pct(agg['type_accuracy'])}</td><td class=n>{_pct(agg['link_rate'])}</td>"
                f"<td>{'كافٍ' if agg['enough'] else '<span class=warn>غير كافٍ للحكم</span>'}</td></tr>")
    first = report["runs"][0]
    entry_html = ""
    ov = first["overall"]
    if ov.get("entries") is not None:
        entry_html = (f"<h2>خط الدخول</h2><p>دخل <b>{ov['entries']}</b> من خط الدخول، واتعدّ <b>{ov['events']}</b> "
                      f"عند خط العدّ، يعني اتشال بين الخطين حوالي <b>{ov['taken_between']}</b> "
                      f"(بالإيد أو وقع). الرقم ده تقديري: خط الدخول مالوش عدّ بشري يتقاس عليه.</p>")
    conf = first["overall"]["confusion"]
    labels = sorted({k for k in conf} | {e for row in conf.values() for e in row})
    conf_html = ""
    if conf:
        head = "".join(f"<th>{esc(names.get(e, e))}</th>" for e in labels)
        body = "".join(
            f"<tr><th>{esc(names.get(t, t))}</th>" + "".join(f"<td class=n>{conf.get(t, {}).get(e, 0)}</td>" for e in labels) + "</tr>"
            for t in labels if t in conf)
        conf_html = f"<h2>الأنواع (الصف = المسطرة، العمود = الموديل)</h2><table><tr><th></th>{head}</tr>{body}</table>"
    thumbs = []
    line = report["counting"]["line"]
    for kind, colour, label in (("misses", (0, 0, 255), "فائت"), ("extras", (255, 0, 255), "زائد")):
        shown = 0
        for clip in first["clips"]:
            path = media_path(slug, clip["media_id"])
            for item in clip[kind]:
                if shown >= 12:
                    break
                t = item.get("t", item.get("ts"))
                b64 = _thumb(path, t, item.get("x"), item.get("y"), line, colour)
                if b64:
                    thumbs.append(f"<figure><img src='data:image/jpeg;base64,{b64}'><figcaption>{label} · "
                                  f"مقطع #{clip['clip_id']} · {t:.2f} ث</figcaption></figure>")
                    shown += 1
    return f"""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8>
<title>قياس الموديل — {esc(meta['name'])}</title>
<style>body{{font:15px/1.7 system-ui,'Noto Sans Arabic',sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1c1b18}}
table{{border-collapse:collapse;width:100%;margin:12px 0}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:start}}
.n{{direction:ltr;text-align:end;font-variant-numeric:tabular-nums}}.warn{{color:#b45309}}.muted{{color:#666;font-size:13px}}
figure{{display:inline-block;margin:6px}}figure img{{border-radius:6px;border:1px solid #ccc}}figcaption{{font-size:12px;color:#555}}
.box{{border-inline-start:4px solid #0f766e;padding:8px 14px;background:#f4f8f7;margin:12px 0}}</style>
<h1>قياس الموديل على المسطرة</h1>
<p class=muted>{esc(meta['name'])} · {esc(report['created_at'])} · inno_count {esc(report['counting_core_version'])} ·
tracker {esc(report['tracker'])} · model sha {esc(report['model_sha'][:12] or '—')} · نافذة المطابقة ±{report['match_window_s']} ث</p>
<div class=box><p><b>كيف تقرأ هذا:</b> كل عبور عدّه الإنسان يُطابَق مع حدث واحد من النظام في نافذة زمنية
{'وعلى نفس المكان من الخط' if any(c['used_position'] for c in first['clips']) else ''}.
<b>الفائت</b> عبور عدّه الإنسان ولم يعدّه النظام. <b>الزائد</b> حدث من النظام بلا عبور حقيقي (عدّ مزدوج أو خاطئ).
لا يُعرض الصافي وحده لأن الفائت والزائد قد يلغي أحدهما الآخر.</p>
<p>الأرقام بين القوسين: النسبة، ثم مدى الثقة 95%. حالة فيها أقل من {report['min_crossings_per_condition']} عبور
تُعلَّم «غير كافٍ للحكم»: فرق رغيفين فيها ضوضاء.</p>
<p><b>معدل الربط</b>: نسبة الأرغفة المعدودة التي جُمعت لها {report['counting']['k_crops']} صور صالحة للتصنيف.</p></div>
<table><tr><th>صورة/ث</th><th>الحالة</th><th>مقاطع</th><th>المسطرة</th><th>أحداث النظام</th><th>فائت</th><th>زائد</th>
<th>دقة النوع</th><th>معدل الربط</th><th>الحكم</th></tr>{''.join(rows)}</table>
{entry_html}
{conf_html}
<h2>أمثلة حقيقية من الأخطاء</h2><p class=muted>الدائرة الحمراء: فائت. البنفسجية: زائد. الخط البرتقالي: خط العدّ.</p>
{''.join(thumbs) or '<p>لا توجد أخطاء لعرضها.</p>'}
<h2>زمن الكاشف على هذا الجهاز</h2><p class=n>{', '.join(f"{c['detector_ms_per_frame']} ms" for c in first['clips'])}</p>
</html>"""
