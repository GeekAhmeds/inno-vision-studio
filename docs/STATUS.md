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

## Known gaps

- **No real camera, no real model, no real footage has been through the studio yet.**
- **Windows:** the automated tests pass on Windows in CI, but nobody has opened the UI on a real
  Windows PC. The browser copy of truth clips needs VP8 in OpenCV's FFmpeg; if it is missing, the
  counting page falls back to single frames.
- The harness and recording run inside the HTTP request and block the page until they end. The job
  queue with progress is not built.
- ONVIF discovery, USB camera names, scheduled recording, disk-space guard.
- Timeline building, gap detection, burned-in-clock OCR (module 1).
- The UI is plain JS. The Vue/Nuxt build named in the specification can replace it later.
- No login. The server binds to localhost; do not expose it with `--host 0.0.0.0` on an untrusted network.
- P2 onward: sampling, model registry, pre-labelling, review screens, Claude QA, datasets, training,
  gate, shadow run, publish.

## Next

- **On the owner's machine:** connect a real camera; draw the line on the bread footage; make the
  first truth clips (two counts each); run the harness on the current r2 model.
- **Decision D6 (training stack licence)** before P4 training. P1 needed no decision: the tracker is
  our own code, and inference uses ONNX Runtime (MIT).
- **Access to the ERP repository** for `inno_count` v0 and the agent's code path.
