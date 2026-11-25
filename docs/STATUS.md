# Status

Phases refer to §11 of `VISION_TRAINING_STUDIO_PROMPT.md`.

## P0 — done (2026-11-24)

| Part | State |
|---|---|
| Repo skeleton, `uv` workspace, CI matrix (Ubuntu + Windows) | built; tests pass on both |
| Projects: create, rename, duplicate, archive, delete, export, import | built and tested |
| Activity log ("what happened and why"), Arabic + English | built and tested |
| Sources: IP/NVR (RTSP, HTTP), USB, video file; keyring for passwords | built; **tested with video files only** |
| Test connection with preview and plain-language errors | built; real cameras not yet tried |
| Record a clip from a source, with per-frame timestamps | built; tested with a file standing in for a camera |
| Ingest: video (link or copy), loose pictures, folder per product | built and tested |
| Camera angle id + reference frame; refuse mixed resolutions | built and tested |
| API + first UI (Arabic, RTL) | built; exercised in a headless browser |

## P1 — built (2026-11-25), not yet run on real footage

| Part | State |
|---|---|
| `inno_count` 0.1.0: letterbox, our own ByteTrack (time-based), line counter, per-track crops and vote, size at the line (px, or cm with a belt calibration), classification event after the count, health per minute | built, unit-tested |
| Fix: an object first seen near the line still counts | tested |
| Thresholds in seconds: 20 / 10 / 5 fps give the same count on a clean belt | tested (simulator) |
| ONNX detector: letterbox, vectorised decode (YOLOv8/11 and end-to-end layouts), NMS, sidecar, refusal with a reason | tested with a toy ONNX model, **not with a real model** |
| Camera-moved check (edges + phase correlation, masked clock, latched) | tested on synthetic frames (acceptance 9) |
| Engine at a fixed rate; viewers cannot change the count; simulated hardware latency | tested (acceptance 11) |
| Simulator (`inno_count.sim`): belt, perspective, piles, merged boxes, occlusion | for tests and demos only |
| Truth: clips, two counts, tap with position, reconciliation, final, lock | built, tested (acceptance 12) |
| Counting page: browser copy (VP8 WebM) built in the background, frame-by-frame fallback | built, exercised in a browser |
| Harness: one-to-one matching on time **and place**, misses and extras separately, Wilson 95 % intervals, per condition, "not enough" below 300 crossings, link rate, fps sweep, HTML report with real crops | built, tested end to end on the simulator |
| UI tabs: counting line, truth, measure | built, exercised in a browser |

### What P1 found about measuring

- **Time alone cannot separate a double count from a miss in a pile.** Two loaves cross within half a
  second; a duplicate of one pairs with the other's tap and both errors disappear. The counting page
  therefore records WHERE the human tapped, and the harness matches on time and place.
- **One track counts once.** On the simulator, errors in piles are almost all misses: when the
  detector draws one box over two touching loaves, both follow one track. That is the "two_loaves"
  case the Classification Engine must flag.

### Deviations to know about

- **Acceptance test 4 is adapted.** The prompt says the old greedy tracker must show *more doubles* on
  piled clips. On the simulator it shows many more *misses* at low frame rates (where cheap hardware
  runs) and no doubles, because the old agent's doubles came from its own counting code, which is
  not available here. The test asserts "greedy is clearly worse on piles at 5 fps". It must be re-run
  on real piled truth clips with `inno_count` v0.
- **`inno_count` v0 (the agent's legacy code, verbatim) is not built**: it needs the ERP repository,
  which this session cannot reach. Acceptance test 3 (reproduce r2's numbers) waits for it.
- ByteTrack lets a low-score box start a track (the paper does not), so a loaf half under another for
  its whole trip still counts. A new track needs two hits.
- The belt calibration (four points + two distances) exists in the core and the API, not yet in the UI.

## P2 — built (2026-11-25), not yet run on real footage

| Part | State |
|---|---|
| Background jobs: one at a time (8 GB GPU), progress, cancel, history kept in the project | built, tested |
| Harness now runs as a job (the page no longer blocks) | built |
| Helper-model registry (workspace-wide): ONNX detectors, role, licence, hash, "may ship" | built, tested |
| Licence policy: copyleft models help with labelling but cannot ship while policy is "permissive only" (acceptance 20) | built, tested |
| Frame sampling: scan, motion, difference hash, time slices, farthest-point diversity, quiet frames as likely negatives, dedupe against earlier runs, no frame from a truth clip or within 10 min of it | built, tested on the simulator |
| Pre-labelling with a registry detector: proposals stored as `auto:<name>@<sha12>` with score and truncation | built, tested with toy models |
| Review screen: draw / select / delete boxes, class keys, "truncated", empty frame = negative, queue with unsure frames first, saved as `human:<user>` | built, exercised in a browser |
| 5 % blind review (no proposals shown), recorded per frame, agreement compared | built |
| Bake-off: precision, recall, F1 at IoU 0.5 against human-confirmed frames, false boxes on empty frames, "provisional" under 100 frames | built, tested |

### P2 limits

- **Only ONNX detectors in YOLO format** can be registered. Open-vocabulary detectors (YOLO-World,
  OWLv2, Grounding DINO), SAM masks and embedders need PyTorch and model downloads; not built.
  "It looks first / groups similar things" (prompt §6.1 step 3–4) waits for an embedder.
- **GPU:** the installed `onnxruntime` is the CPU build. For the RTX 4060 install `onnxruntime-gpu`
  (CUDA 12) in its place; the studio then picks CUDA by itself. Remote providers are never used.
- Sampling has no global timeline: the 10-minute truth buffer applies within the same video file only.
- The review screen has no box resize handles: delete and redraw.
- Box classes are optional: the Counting Engine's detector is single-class; "object" is accepted.

## Known gaps

- **No real camera, no real model, no real footage has been through the studio yet.**
- **Windows:** the automated tests pass on Windows in CI, but nobody has opened the UI on a real
  Windows PC. The browser copy of truth clips needs VP8 in OpenCV's FFmpeg; if it is missing, the
  counting page falls back to single frames.
- Recording a clip still runs inside the HTTP request (the harness, sampling and pre-labelling are jobs).
- ONVIF discovery, USB camera names, scheduled recording, disk-space guard.
- Timeline building, gap detection, burned-in-clock OCR (module 1).
- The UI is plain JS. The Vue/Nuxt build named in the specification can replace it later.
- No login. The server binds to localhost; do not expose it with `--host 0.0.0.0` on an untrusted network.
- P3 onward: Claude QA, datasets, training, gate, shadow run, publish.

## Next

- **On the owner's machine:** connect a real camera; draw the line on the bread footage; make the
  first truth clips (two counts each); run the harness on the current r2 model.
- **Decision D6 (training stack licence)** before P4 training. P1 needed no decision: the tracker is
  our own code, and inference uses ONNX Runtime (MIT).
- **Access to the ERP repository** for `inno_count` v0 and the agent's code path.
