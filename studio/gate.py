"""Release gate (P5, prompt §6 module 11, rule 6).

The candidate and the current model run on the same final truth clips, with the same counting core,
tracker and frame rate (the rate the bakery's machine actually reaches). The comparison is paired:

- **Misses:** each truth crossing is missed or not by each model. Only the crossings where the two
  disagree say anything: b = missed by the candidate only, c = missed by the current only. The
  candidate is worse when b − c is at least 1 and larger than z·√(b + c) (McNemar's test).
- **Extras (double/false counts):** not tied to a truth crossing, so the counts are compared:
  worse when E_cand − E_cur is at least 1 and larger than z·√(E_cand + E_cur).
- A condition (night, piled, …) is used only when it has at least `min_crossings` human-counted
  crossings; below that a difference of two loaves is noise.

The candidate passes when it is worse in **no** used condition, on misses or on extras, and its type
accuracy meets the owner's threshold (if set). A difference inside the noise margin does not fail
it. Whether it is also clearly better is reported, so the owner can decide if a swap is worth it.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import db, registry
from .explain import activity
from .harness.run import HarnessError, load_detector, run_clip
from .projects import get_project, project_dir, save_project
from .truth.store import CONDITIONS

GATE_DEFAULTS: dict[str, Any] = {
    "fps": 4.0,                  # what the bakery machine reaches; measure it, then set it here
    "tracker": "bytetrack",
    "latency_s": 0.0,
    "min_crossings": 300,        # per condition, prompt §6 module 8
    "z": 1.96,                   # 95 %
    "min_type_accuracy": None,   # e.g. 0.95 when there are several product types
}


class GateError(Exception):
    """A problem the owner can understand and fix."""


def get_settings(slug: str) -> dict[str, Any]:
    return {**GATE_DEFAULTS, **(get_project(slug).get("gate") or {})}


def set_settings(slug: str, changes: dict[str, Any]) -> dict[str, Any]:
    meta = get_project(slug)
    cfg = {**GATE_DEFAULTS, **(meta.get("gate") or {})}
    for k, v in changes.items():
        if k not in GATE_DEFAULTS:
            raise GateError(f"Unknown gate setting: {k}")
        cfg[k] = v
    cfg["fps"] = float(cfg["fps"])
    cfg["min_crossings"] = int(cfg["min_crossings"])
    cfg["z"] = float(cfg["z"])
    cfg["latency_s"] = float(cfg["latency_s"])
    if cfg["fps"] <= 0 or cfg["min_crossings"] < 1 or not 0 < cfg["z"] <= 5 or cfg["latency_s"] < 0:
        raise GateError("fps > 0, min crossings ≥ 1, 0 < z ≤ 5, latency ≥ 0.")
    if cfg["min_type_accuracy"] is not None:
        cfg["min_type_accuracy"] = float(cfg["min_type_accuracy"])
        if not 0 < cfg["min_type_accuracy"] <= 1:
            raise GateError("The type accuracy threshold is a fraction between 0 and 1 (e.g. 0.95).")
    meta["gate"] = cfg
    save_project(meta)
    return cfg


# ------------------------------------------------------------------ the comparison (pure)

def _worse(diff: float, spread: float, z: float) -> bool:
    return diff >= 1 and diff > z * math.sqrt(spread)


def _side(results: list[dict[str, Any]]) -> dict[str, Any]:
    truth = sum(r["summary"]["truth"] for r in results)
    events = sum(r["summary"]["events"] for r in results)
    misses = sum(r["summary"]["misses"] for r in results)
    extras = sum(r["summary"]["extras"] for r in results)
    typed = right = 0
    for r in results:
        for t, row in r["summary"]["confusion"].items():
            for e, n in row.items():
                if e != "unknown":
                    typed += n
                    right += n if e == t else 0
    return {"truth": truth, "events": events, "misses": misses, "extras": extras,
            "type_accuracy": round(right / typed, 4) if typed else None}


def compare(cand: list[dict[str, Any]], cur: list[dict[str, Any]] | None, settings: dict[str, Any]) -> dict[str, Any]:
    """cand / cur: run_clip results on the same clips, in the same order. cur None = first model."""
    z, need = settings["z"], settings["min_crossings"]
    if cur is not None and [r["clip_id"] for r in cand] != [r["clip_id"] for r in cur]:
        raise GateError("The two models were not run on the same clips.")
    groups: dict[str, list[int]] = {}
    for i, r in enumerate(cand):
        groups.setdefault(r["condition"], []).append(i)
    rows, reasons = {}, []
    used_idx: list[int] = []
    for cond, idx in sorted(groups.items()):
        c = _side([cand[i] for i in idx])
        row: dict[str, Any] = {"candidate": c, "used": c["truth"] >= need}
        if cur is not None:
            u = _side([cur[i] for i in idx])
            b = sum(len(set(cand[i]["miss_ids"]) - set(cur[i]["miss_ids"])) for i in idx)
            k = sum(len(set(cur[i]["miss_ids"]) - set(cand[i]["miss_ids"])) for i in idx)
            row.update(current=u, missed_by_candidate_only=b, missed_by_current_only=k,
                       misses_worse=_worse(b - k, b + k, z), misses_better=_worse(k - b, b + k, z),
                       extras_worse=_worse(c["extras"] - u["extras"], c["extras"] + u["extras"], z),
                       extras_better=_worse(u["extras"] - c["extras"], c["extras"] + u["extras"], z),
                       miss_margin=round(z * math.sqrt(b + k), 2),
                       extra_margin=round(z * math.sqrt(c["extras"] + u["extras"]), 2))
            if row["used"]:
                if row["misses_worse"]:
                    reasons.append(f"{cond}: more misses ({b} missed only by the candidate vs {k} only by the current)")
                if row["extras_worse"]:
                    reasons.append(f"{cond}: more extra counts ({c['extras']} vs {u['extras']})")
        if row["used"]:
            used_idx += idx
            th = settings.get("min_type_accuracy")
            if th is not None and c["type_accuracy"] is not None and c["type_accuracy"] < th:
                reasons.append(f"{cond}: type accuracy {c['type_accuracy']:.1%} below {th:.0%}")
        rows[cond] = row
    if not used_idx:
        verdict = "insufficient"
        reasons = [f"No condition has {need} human-counted crossings yet."] + reasons
    else:
        verdict = "fail" if reasons else "pass"
    overall_c = _side([cand[i] for i in used_idx]) if used_idx else _side(cand)
    overall: dict[str, Any] = {"candidate": overall_c}
    better = False
    if cur is not None and used_idx:
        overall_u = _side([cur[i] for i in used_idx])
        b = sum(len(set(cand[i]["miss_ids"]) - set(cur[i]["miss_ids"])) for i in used_idx)
        k = sum(len(set(cur[i]["miss_ids"]) - set(cand[i]["miss_ids"])) for i in used_idx)
        better = (_worse(k - b, b + k, z) or _worse(overall_u["extras"] - overall_c["extras"],
                                                 overall_c["extras"] + overall_u["extras"], z))
        overall.update(current=overall_u, missed_by_candidate_only=b, missed_by_current_only=k)
    return {"verdict": verdict, "clearly_better": bool(better and verdict == "pass"), "reasons": reasons,
            "conditions": rows, "overall": overall, "first_model": cur is None}


# ------------------------------------------------------------------ running it

def _clip_ids(slug: str) -> list[int]:
    with db.connect(project_dir(slug)) as con:
        return [r["id"] for r in con.execute("SELECT id FROM truth_clips WHERE final_taps IS NOT NULL ORDER BY id")]


def run_gate(slug: str, candidate_id: str, current_id: str | None = None,
             detectors: dict[str, Any] | None = None, progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    """Run both models and write the gate report. `detectors` replaces loading (tests only)."""
    settings = get_settings(slug)
    cand_entry = registry.require_shippable(candidate_id)
    if current_id is None:
        current_id = ((get_project(slug).get("deployment") or {}).get("current") or {}).get("model_id")
    cur_entry = registry.get_model(current_id) if current_id else None
    if cur_entry and cur_entry["sha256"] == cand_entry["sha256"]:
        raise GateError("The candidate is the model already in use.")
    ids = _clip_ids(slug)
    if not ids:
        raise GateError("There is no truth clip with final truth yet (Truth tab).")

    def det(role: str, entry: dict[str, Any]) -> Any:
        if detectors and role in detectors:
            return detectors[role]
        try:
            return load_detector(entry["path"])
        except HarnessError as exc:
            raise GateError(f"{entry['name']}: {exc}") from exc

    sides = [("candidate", cand_entry)] + ([("current", cur_entry)] if cur_entry else [])
    results: dict[str, list[dict[str, Any]]] = {}
    total = len(ids) * len(sides)
    step = 0
    for role, entry in sides:
        d = det(role, entry)
        names = getattr(d, "names", None) or None
        out = []
        for cid in ids:
            out.append(run_clip(slug, cid, d, settings["tracker"], settings["fps"], settings["latency_s"],
                                entry["sha256"], names))
            step += 1
            if progress:
                progress(step / total, f"{'المرشّح' if role == 'candidate' else 'الحالي'}: مقطع {len(out)}/{len(ids)}")
        results[role] = out
    verdict = compare(results["candidate"], results.get("current"), settings)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": slug, "settings": settings, "clips": ids,
        "candidate": {k: cand_entry[k] for k in ("id", "name", "sha256", "licence", "note")},
        "current": {k: cur_entry[k] for k in ("id", "name", "sha256", "licence", "note")} if cur_entry else None,
        **verdict,
        "per_clip": {role: [{k: r[k] for k in ("clip_id", "condition", "summary", "miss_ids", "frames_processed",
                                                "detector_ms_per_frame")} for r in rs] for role, rs in results.items()},
    }
    folder = project_dir(slug)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = folder / "reports" / f"gate_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    data = json.dumps(report, ensure_ascii=False, indent=1).encode("utf-8")
    (out_dir / "report.json").write_bytes(data)
    report["report_sha"] = hashlib.sha256(data).hexdigest()
    report["report_dir"] = out_dir.relative_to(folder).as_posix()
    (out_dir / "report.html").write_text(render_html(slug, report), encoding="utf-8")
    word = {"pass": "نجح", "fail": "رُفض", "insufficient": "المسطرة لسه مش كفاية"}[report["verdict"]]
    activity.log(folder, "gate.run",
                 f"البوابة: «{cand_entry['name']}» {word}" + (f" مقابل «{cur_entry['name']}»" if cur_entry else " (أول موديل)") + ".",
                 f"Gate: {cand_entry['name']} {report['verdict']}.",
                 why_ar="الموديل الجديد ما ينزلش إلا لو مش أسوأ من الحالي في أي حالة (ليل، زحمة، …)، "
                        "لا في الفائت ولا في الزائد، بفرق أكبر من الضوضاء.",
                 why_en="A candidate ships only if it is not worse than the current model in any condition.",
                 outputs={"verdict": report["verdict"], "report": report["report_dir"], "reasons": report["reasons"]})
    return report


def list_reports(slug: str) -> list[dict[str, Any]]:
    folder = project_dir(slug) / "reports"
    out = []
    for d in sorted(folder.glob("gate_*"), reverse=True):
        f = d / "report.json"
        if not f.exists():
            continue
        raw = f.read_bytes()
        r = json.loads(raw)
        out.append({"name": d.name, "created_at": r["created_at"], "verdict": r["verdict"],
                    "clearly_better": r["clearly_better"], "candidate": r["candidate"], "current": r["current"],
                    "reasons": r["reasons"], "report_sha": hashlib.sha256(raw).hexdigest()})
    return out


def load_report(slug: str, name: str) -> dict[str, Any]:
    if not name.startswith("gate_") or "/" in name or "\\" in name or ".." in name:
        raise GateError("Unknown gate report.")
    f = project_dir(slug) / "reports" / name / "report.json"
    if not f.exists():
        raise GateError(f"Gate report {name} not found.")
    raw = f.read_bytes()
    return {**json.loads(raw), "name": name, "report_sha": hashlib.sha256(raw).hexdigest()}


# ------------------------------------------------------------------ HTML

def _pct(v: float | None) -> str:
    return "—" if v is None else f"{100 * v:.1f}%"


def render_html(slug: str, report: dict[str, Any]) -> str:
    esc = html.escape
    meta = get_project(slug)
    has_cur = report["current"] is not None
    verdict_ar = {"pass": "نجح ✔", "fail": "مرفوض ✘", "insufficient": "المسطرة مش كفاية للحكم"}[report["verdict"]]
    colour = {"pass": "#0f766e", "fail": "#b91c1c", "insufficient": "#b45309"}[report["verdict"]]
    rows = []
    for cond, r in report["conditions"].items():
        c = r["candidate"]
        mark = lambda worse, better: ("<b style=color:#b91c1c>أسوأ</b>" if worse else "<b style=color:#0f766e>أحسن</b>"
                                      if better else "نفس المستوى")
        cur_cells = ""
        if has_cur:
            u = r["current"]
            cur_cells = (f"<td class=n>{u['misses']}</td><td class=n>{r['missed_by_candidate_only']} / {r['missed_by_current_only']}"
                         f" (هامش {r['miss_margin']})</td><td>{mark(r['misses_worse'], r['misses_better'])}</td>"
                         f"<td class=n>{u['extras']}</td><td>{mark(r['extras_worse'], r['extras_better'])} (هامش {r['extra_margin']})</td>")
        rows.append(f"<tr><td>{esc(CONDITIONS.get(cond, cond))}</td><td class=n>{c['truth']}</td>"
                    f"<td>{'يُستخدم' if r['used'] else '<span class=warn>أقل من الحد</span>'}</td>"
                    f"<td class=n>{c['misses']}</td>{cur_cells if has_cur else ''}<td class=n>{c['extras']}</td>"
                    f"<td class=n>{_pct(c['type_accuracy'])}</td></tr>")
    head = ("<th>الحالة</th><th>المسطرة</th><th>تُستخدم؟</th><th>فائت المرشّح</th>"
            + ("<th>فائت الحالي</th><th>فاته هو بس / فات الحالي بس</th><th>الفائت</th><th>زائد الحالي</th><th>الزائد</th>" if has_cur else "")
            + "<th>زائد المرشّح</th><th>دقة النوع</th>")
    s = report["settings"]
    reasons = "".join(f"<li>{esc(x)}</li>" for x in report["reasons"]) or "<li>لا شيء</li>"
    type_th = "" if s.get("min_type_accuracy") is None else " · حد دقة النوع " + _pct(s["min_type_accuracy"])
    cand, cur = report["candidate"], report["current"]
    cand_html = f"<b>{esc(cand['name'])}</b> <span class=muted dir=ltr>{cand['sha256'][:12]} · {esc(cand['licence'])}</span>"
    cur_html = (f"<b>{esc(cur['name'])}</b> <span class=muted dir=ltr>{cur['sha256'][:12]}</span>" if cur
                else "مفيش (ده أول موديل)")
    if report["clearly_better"]:
        better = "<b>أحسن بوضوح من الحالي.</b>"
    elif report["verdict"] == "pass" and has_cur:
        better = "مش أسوأ، بس كمان مش أحسن بفرق أكبر من الضوضاء."
    else:
        better = ""
    return f"""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><title>البوابة — {esc(meta['name'])}</title>
<style>body{{font:15px/1.7 system-ui,'Noto Sans Arabic',sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1c1b18}}
table{{border-collapse:collapse;width:100%;margin:12px 0}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:start}}
.n{{direction:ltr;text-align:end;font-variant-numeric:tabular-nums}}.warn{{color:#b45309}}.muted{{color:#666;font-size:13px}}
.box{{border-inline-start:4px solid #0f766e;padding:8px 14px;background:#f4f8f7;margin:12px 0}}</style>
<h1>البوابة: <span style="color:{colour}">{verdict_ar}</span></h1>
<p>المرشّح: {cand_html}<br>الحالي: {cur_html}</p>
<p class=muted>{esc(report['created_at'])} · {s['fps']} صورة/ث · tracker {esc(s['tracker'])} · حد كل حالة {s['min_crossings']} عبور · z = {s['z']}
{type_th}</p>
<p>{better}</p>
<h2>الأسباب</h2><ul>{reasons}</ul>
<div class=box><p><b>إزاي البوابة بتحكم:</b> الموديلين اتشغّلوا على نفس مقاطع المسطرة بنفس كود العدّ ونفس السرعة.
في الفائت بنبص بس على العبورات اللي واحد فاتها والتاني لأ، والمرشّح أسوأ لو الفرق أكبر من الهامش (z × جذر مجموعهم).
الزائد بيتقارن بنفس الطريقة. أي حالة فيها أقل من {s['min_crossings']} عبور ما بتتحسبش.</p></div>
<table><tr>{head}</tr>{''.join(rows)}</table>
<p class=muted>بصمة التقرير (sha256): <span dir=ltr>{report.get('report_sha', '')}</span></p>"""
