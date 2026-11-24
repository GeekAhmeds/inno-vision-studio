# Status

Phases refer to §11 of `VISION_TRAINING_STUDIO_PROMPT.md`.

## P0 — done (2026-11-24)

| Part | State |
|---|---|
| Repo skeleton, `uv` workspace, CI matrix (Ubuntu + Windows) | built; the 32 tests pass on both |
| Projects: create, rename, duplicate, archive, delete, export, import | built and tested |
| Activity log ("what happened and why"), Arabic + English | built and tested |
| Sources: IP/NVR (RTSP, HTTP), USB, video file; keyring for passwords | built; **tested with video files only** |
| Test connection with preview and plain-language errors | built; real cameras not yet tried |
| Record a clip from a source, with per-frame timestamps | built; tested with a file standing in for a camera |
| Ingest: video (link or copy), loose pictures, folder per product | built and tested |
| Camera angle id + reference frame; refuse mixed resolutions | built and tested |
| API + first UI (Arabic, RTL) | built; exercised in a headless browser |

## Known gaps in P0

- **No real camera has been connected.** RTSP and USB paths go through OpenCV and are untested on
  hardware, on either OS. First thing to try on the owner's machine.
- **Windows:** the automated tests pass on Windows in CI, but nobody has opened the UI or
  connected a camera on a real Windows PC yet.
- ONVIF discovery is not built; the camera address is typed in.
- USB cameras are listed by index, not by name.
- Recording runs inside the HTTP request (max 1 hour) and blocks it. The job queue with progress,
  pause and resume is not built yet; neither is scheduled recording or the disk-space guard.
- The angle check compares resolution only. Real "camera moved" detection belongs to `inno_count` (P1).
- Timeline building, gap detection and burned-in-clock OCR/masking (module 1) are not built.
- The UI is plain JS. The Vue/Nuxt build named in the specification can replace it later.
- No login. The server binds to localhost; do not expose it with `--host 0.0.0.0` on an untrusted network.
- `inno_count` is an empty package.

## Next

- **STEP 0 (bread measurements)** needs the owner's machine: the 25 h of footage, the existing
  datasets, the r2 model and the agent code.
- **Decision D6 (training stack licence)** must be settled before P1 pulls in a tracker or trainer.
- **P1:** `inno_count` v0 (legacy, verbatim) → v1, truth page, harness.
