# Inno Vision Studio — execution prompt (v2)

> **مقدمة بالعربية.** هذا برنامج مستقل عن الـ ERP، وظيفته تدريب نماذج الرؤية لأي منتج (العيش أولًا،
> ثم معلبات أو أي شيء آخر) بطريقة صحيحة. تفتح فيه **مشروعًا** باسمه، توصّل له كاميرا (IP أو USB أو
> غيرها) أو ترفع فيديو أو صورًا أو فولدرًا لكل منتج، وتوصّل له موديلات تساعدك في التسمية. البرنامج يصنع
> بيانات يراجعها إنسان (ويساعده Claude في التدقيق)، ويدرّب النموذج، ويقيسه على «مسطرة» عدّها إنسان،
> ولا ينشر إلا نموذجًا غلب الحالي في القياس. كل مشروع محفوظ ببياناته وموديلاته، وكل خطوة مشروحة بلغة
> بسيطة: ماذا حدث، ولماذا، وبأي أرقام. يعمل على لينكس وويندوز.
>
> The rest of this prompt is in English because it is written for the engineer or agent building it.

Version 2, written 2026-11-24. It replaces v1 of the same date. What changed from v1 is listed in
Appendix A. Repository: `inno-vision-studio`, a NEW repo, **not** inside the ERP monorepo. The ERP is
touched only through its existing model-upload API and the shared counting package (§7).

---

## 1. Why: what the 2026-11-24 audit found

Items 1, 4 and 5 were measured or seen directly. Items 2, 3 and 6 come from a code audit, and the
file:line references were spot-checked; the processing-rate figures are reported, not measured
(measuring them is part of STEP 0).

1. **The training data teaches the errors.** `HARDWARE-AGENT/training/datasets/bread_day_cam1`
   holds 318 frames (247 train / 71 val, 1,717 boxes). Its `productline.json` records the labeller as
   `model:bread_night_gpu80.onnx@0.25` (a model from another camera), with `auto_approved`:
   "no human review yet". Rendering 40 training images shows:
   - boxes on the empty steel belt;
   - single boxes spanning two loaves;
   - half-loaf boxes at the crop edge;
   - visible loaves left unlabelled.

   Both published models (r1, r2) trained on it.
2. **The reported accuracy is not real.** mAP50 0.968 was measured on a random-frame val split
   from the same clip, labelled the same way. "113 of 115" comes from one 5-minute daytime clip,
   run through code that differs from the agent's (frame rate, detector class, resize, tracker
   warm-up).
3. **The live counting core has faults:**
   - it stretches the image instead of letterboxing (`agent/drivers/camera.py:329`);
   - a track first seen near the line is never counted (`:151-155`);
   - a greedy centroid tracker;
   - thresholds counted in frames, while the processing rate changes about 7× depending on whether
     someone watches the live view;
   - a silent fallback to motion counting;
   - no check for a moved camera.
4. **Size from the raw box size is wrong.** On the 2026-11-09 footage, the same loaf measures about
   110 px at the top of the belt and about 155 px at the bottom (perspective). Edge-cut boxes and
   false boxes on the belt pollute any size histogram.
5. **The footage.** Folder `/home/ahmeds/Work/Work/0x01/khabza vid`: 16 MP4 files, 1280×720,
   20 fps, about 25 h continuous (2026-11-08 23:18 → 2026-11-10 00:33, one 7.5-min hole at 19:49).
   The camera angle differs from the October training clip. The clock is burned in at
   x 0–420, y 22–62.
6. **The ERP side** (separate task, not this project): `production_line` counts only one product
   per period. A crossing of another type goes to an undifferentiated `foreign_count` that never
   becomes stock. Offline replays land in the currently open period; uid dedupe has no unique
   constraint.

---

## 2. Goal

A cross-platform **studio application** in which the owner opens a named project for any product,
feeds it cameras, videos or pictures, and is led through a repeatable pipeline:

**sources → reviewed dataset → trained model → measured on human-counted truth → release gate →
shadow run → published.**

- **Product-agnostic.** Bread is the first project, not a special case. Tomorrow it is canned goods
  or anything else. No class name, size, colour or camera is hard-coded; everything product-specific
  lives in the project.
- **Task types.** v1 builds and gates two: *count objects crossing a line* and *count per type*.
  The project structure must also fit *classify*, *detect a defect*, *measure size* and *count in a
  still image* (e.g. cans on a shelf), which arrive later as new task types, not as a redesign.
- **Bread project, first.** Classes the owner named: **سياحي كبير**, **سياحي صغير**, **حبة كاملة**
  (a darker loaf). Stable ids are ASCII (`siyahi_large`, `siyahi_small`, `whole_grain`); the Arabic
  names are display names in the project's class map (§5.5).

**Non-goals for this repo:** ERP period logic and review screens (§8), prompt-to-watcher
(Appendix B), cloud hosting, multi-user accounts.

---

## 3. Hard rules

1. **One counting core.** The tracker, line/zone logic, class vote and size-at-line live in ONE
   Python package (`inno_count`). The ERP's Hardware Agent imports it, and so does the studio's
   replay harness. Never copy it. Pin the version the agent ships with and record it in every
   evaluation report.
2. **No model trains on its own unreviewed output.** Every label carries provenance:
   `auto:<model>`, `claude:<model-id>@<prompt-version>`, `imported:<source>` or `human:<user>`.
   Only `human`-confirmed labels enter a training set. Models propose; a human confirms.
3. **Three separate sets, split by time block, never by random frames.** *Train*, *tune* (for
   choosing confidence, fps, thresholds) and *truth* (the locked scoreboard). Nothing is ever tuned
   on truth. Hold out whole hours including day and night, with a 10-minute buffer around each
   held-out block. Where a project has several recording days, at least one whole day is truth-only.
4. **Negatives are mandatory:** the empty scene, hands, containers, steam, glare, people passing.
   A negative is an image with no boxes, not a class.
5. **Measure misses and double counts separately**, per class, per condition, by matching events
   one to one (§6 module 9). Never report a net figure alone.
6. **Nothing is published unless it beats the current model** on the same truth set, under the
   same counting core, at the frame rate the target hardware actually achieves, by more than the
   noise margin (§6 module 11).
7. **Spending is gated.** Any step that calls a paid API first estimates the cost
   (`messages.count_tokens` on a real sample for Claude), prints it, and waits for the operator's
   yes. Each project has a budget cap and a ledger of actual spend.
8. **Use existing tools** where they exist (Ultralytics or RF-DETR for training, ByteTrack for
   tracking, ONNX Runtime for inference, ffmpeg for video). Build only the glue and what is ours.
9. **Licences are recorded and respected.** Every third-party model and library has its licence
   stored in the registry (§5.3). `inno_count` and anything shipped to a client machine must carry
   no AGPL dependency unless the owner has bought the commercial licence (decision D6).
10. **Everything is explained.** No step runs silently: each one writes what it did, why, and with
    what numbers, to the project's activity log (§5.6).
11. **Linux and Windows are equal targets.** Every feature works on both; CI runs on both (§5.7).

---

## 4. Architecture and repository layout

A **local web application**: a Python backend that owns the pipeline and the GPU, and a browser UI
served from the same machine (`http://localhost:<port>`). One codebase gives Linux and Windows
without a native GUI toolkit, and lets a second PC on the LAN open the UI later.

```
inno-vision-studio/
  packages/inno_count/   shared counting core; pip-installable; own version; permissive deps only
  studio/                backend (FastAPI): one sub-package per module of §6, plus
                         projects/ sources/ providers/ explain/
  web/                   UI (Vue/Nuxt, built to static files the backend serves)
  docs/                  user guide (Arabic), architecture, decisions
  tests/                 unit, acceptance (§10), sample clips small enough for git
  label-app/             the first labelling screen from v1, to be folded into web/
```

- Python 3.11+, managed with `uv`. `pathlib` everywhere; no shell-only scripts; no absolute paths
  in stored data.
- Long jobs (ingest, pre-label, train, harness) run in a job queue with progress, pause, resume
  and cancel, and survive a UI refresh.
- Video, images, datasets and weights never go into git. They live in the workspace (§5.1).

---

## 5. Studio foundations

### 5.1 Projects

A **workspace** folder (default `~/InnoVisionStudio`, configurable) holds one folder per project:

```
projects/<slug>/
  project.yaml       name, product, task type, class map, counting line/zone, owner decisions
  studio.db          SQLite: media index, labels with provenance, tracks, jobs, spend ledger
  sources/           source definitions (no secrets), reference frame per camera angle
  media/             recordings, uploaded videos, uploaded images
  datasets/<ver>/    immutable snapshots: manifest, hashes, splits
  truth/             truth clips and human counts (locked)
  models/<run>/      training runs: config, metrics, weights, exported ONNX + sidecar
  reports/           HTML reports, gate reports, project story
  activity.jsonl     the activity log (§5.6)
```

- Create, rename, duplicate, archive, delete (with confirmation), **export to one archive** and
  **import** on another machine, Linux ↔ Windows. Paths inside a project are relative.
- A project can link large media in place instead of copying it (the 25 h of bread footage stays
  where it is); linked media is hashed so a moved or changed file is detected.
- **Templates:** "count on a line", "count per type", "classify", each pre-filling the wizard.
- The home screen lists projects with their state: data collected, labels confirmed, models built,
  best gate result, last activity.

### 5.2 Sources

One `Source` interface (open, read frame with timestamp, health, close) with these kinds:

| Kind | Notes |
|---|---|
| IP camera | RTSP (main and sub-stream), ONVIF discovery on the LAN, HTTP MJPEG as fallback. |
| NVR/DVR channel | RTSP per channel; recorded-footage export where the device allows it. |
| USB / built-in camera | V4L2 on Linux, Media Foundation / DirectShow on Windows; enumerate devices, resolutions and frame rates. |
| Video file | Upload or link in place; any container ffmpeg reads. |
| Image set | Upload files, a folder, or a zip. |
| Folder per product | A folder whose sub-folders are products (§5.4). |
| Other | The interface is a plug-in point: a phone used as an IP camera, a screen region, an industrial camera SDK can be added without touching the pipeline. |

Rules for every source:
- A **Test connection** button shows a live preview, the measured resolution and fps, and plain
  errors ("wrong password", "camera unreachable", "stream codec not supported").
- Credentials go to the OS keyring (Secret Service on Linux, Credential Manager on Windows), never
  into `project.yaml`, the database, logs or an exported archive.
- **Live cameras are recorded first.** Training and truth always work from recorded clips, so
  every result can be replayed. The studio records to disk on a schedule or on a button, with a
  disk-space guard.
- Timestamps come from the stream (PTS for files, capture time for live), never from a frame
  index. Frame drops and variable frame rate are detected and stored.
- Each camera angle has an id and a stored reference frame. Media from a different angle is
  refused until a new angle id is created.

### 5.3 Helper models and providers

A **model registry** per workspace, usable by any project. Each entry has a role, a licence, and
where it runs.

| Role | Examples |
|---|---|
| Pre-labeller (draws boxes or masks) | a model from an earlier project, any ONNX or `.pt` detector, open-vocabulary detectors (YOLO-World, Grounding DINO, OWLv2), SAM-family masks seeded from boxes |
| Checker / classifier (judges a crop) | Claude through the API (§6 module 4); a local classifier |
| Embedder (groups similar things) | DINOv2, CLIP-family |
| Starting weights for training | pretrained detector or classifier backbones |

- Add a model by file upload, local path, or a named download; API providers by a key stored in
  the keyring. Each entry shows its licence and whether it may ship to a client machine (rule 9).
- A helper model's output is always `auto:` or `claude:` provenance. Connecting a stronger model
  never bypasses human confirmation (rule 2).
- **Existing datasets can be imported** (YOLO, COCO, CVAT, Label Studio exports) as
  `imported:<source>`. They are treated as unreviewed unless the owner declares them human-reviewed,
  and that declaration is logged with his name.

### 5.4 Folder per product

The owner can make groups or folders, one per product, and upload each product's pictures into it.

- The folder name becomes the class (he can rename or merge). Because he sorted them himself, the
  **class label** of each picture is `human:<user>`.
- That is enough to train a classifier. For a detector the pictures still need boxes: the studio
  proposes them (one object per picture is the easy case) and he confirms in bulk on the grid.
- The studio must say this plainly when it applies: **catalogue-style pictures do not replace
  footage from the real camera.** A model trained only on clean product shots usually fails on the
  line. Such pictures seed the classes and the pre-labeller; the gate still runs on truth clips
  from the real source.

### 5.5 Classes

- Each class has a stable ASCII id, a display name (Arabic or any language), a colour and an
  example crop. Ids never change once a dataset version uses them; display names may.
- Structural labels exist in every counting project: `truncated` (an attribute of a box cut by the
  frame or ROI edge), and the classifier outputs `partial`, `two_objects`, `not_product`, `unknown`.

### 5.6 Explanation layer ("what happened and why")

The owner asked for an explanation of everything that happens. Three levels:

1. **While it runs.** Every screen has a plain-language panel: what this step is doing now, why it
   is needed, what it will produce, and what he should do next.
2. **Activity log.** Every step appends a structured entry to `activity.jsonl`: time, step, inputs
   (dataset version, model, settings), outputs, key numbers, duration, cost, who confirmed. The UI
   shows it as a timeline in Arabic or English.
3. **Project story.** At the end of each phase, and on demand, the studio writes an HTML report
   that tells the project from sources to published model: what data came in, what was rejected and
   why, how many labels a human confirmed, what each trained model scored, why the gate passed or
   refused, with real crops and a short glossary (miss, double count, truth set, confidence).

Numbers in an explanation always come from the log, never from a language model. Claude may be
used (cost-gated) to turn log entries into readable prose; the figures are inserted from the data
and the report links each one to its log entry.

`docs/` in the repo additionally holds an Arabic user guide and an architecture note, updated in
the same change as the feature they describe.

### 5.7 Cross-platform

- Supported: Ubuntu 22.04+ and Windows 10/11, x86-64. NVIDIA GPU through CUDA on both; a CPU
  fallback that works (slowly) with a clear warning.
- Inference through ONNX Runtime with the CUDA, OpenVINO and (Windows) DirectML providers, chosen
  per hardware profile and recorded in every report.
- ffmpeg is bundled or fetched at install; the studio never assumes it is on PATH.
- One-command install and start on each OS (`uv sync`, then `studio start`), which opens the
  browser. A packaged installer is a later nicety, not a v1 requirement.
- Before any GPU job: check the GPU is present (`nvidia-smi` or the NVML library); the local
  RTX 4060 has dropped off the bus before. With 8 GB of VRAM, heavy helper models run one at a time.
- CI runs the unit and acceptance tests on both `ubuntu-latest` and `windows-latest`.
- The built-in labelling screens are the default review tool, because CVAT needs Docker and is
  heavy on Windows (decision D5).

---

## 6. Pipeline (modules)

| # | Module | What it does |
|---|---|---|
| 1 | `ingest` | Register media per source and angle. Build a continuous timeline from stream timestamps, an OCR of a burned-in clock, or NVR metadata. Detect gaps. Store the reference frame. Regions that change by themselves (a burned-in clock) are stored as a mask and excluded from the detector input when they overlap the ROI, from perceptual hashing and from the camera-moved check. |
| 2 | `sample` | Pick frames for labelling by diversity (time of day, motion, scene change, perceptual-hash dedupe), not uniformly. Always include negatives. Export crops of the counting zone plus the full frame for context. |
| 3 | `prelabel` | Run a **bake-off** of the registered pre-labellers against 100–200 human-labelled frames. Choose by measured recall and precision, never by name. |
| 4 | `claude_qa` | Send each proposed box as a **crop with a 20–30 % margin** (never a frame with workers in it), together with its measured real size (§7.2), to Claude. Ask, with structured output: `whole / partial / two_objects / not_product` and the class id (or `unsure`) with a reason. Class ids and their descriptions come from the project, with a few human-confirmed example crops per class. Use the Message Batches API and prompt caching for bulk runs. Before any bulk run, score the candidate Claude models on the **mark sheet**: 200–300 crops a human labelled blind. Default model `claude-opus-5-5`; a cheaper one if the owner picks it from the measured agreement. Disagreements with the pre-label go to the top of the human queue. Claude does **not** draw boxes. |
| 5 | `review` | The built-in screens: a grid for bulk decisions by cluster and class (multi-select, sort by size / time / confidence), and a simple box editor for corrections. Flagged items first. Confirmations are stored as `human:<user>`. A random 5 % of items are reviewed **blind** (no model suggestion shown) to measure how often bulk confirmation follows a wrong suggestion; that rate is reported per dataset version. Export to and import from Label Studio or CVAT is available for heavy box work. |
| 6 | `dataset` | Immutable, versioned snapshots (manifest + hashes), class map, train / tune / truth separation by time block, a per-class and per-condition count table. A truncated object is labelled as the object class with `truncated=true`, so the detector learns to see objects entering the frame; exclusion from count and size is done geometrically in `inno_count`. |
| 7 | `train` | Trains the two engines of §7.1 separately. **Counting Engine detector:** single class (the product), detect and seg variants (masks give a real area), candidates per decision D6 (Ultralytics YOLO26 / YOLO11, RF-DETR). **Classification Engine:** a crop classifier, trained and scored per **track**, whose input is K crops **plus the measured real size at the line**. A multi-class detector may be kept only as a gate baseline. Refuses to train a class with too few human-confirmed examples and says how many are missing. |
| 8 | `truth` | Short clips (2–5 min) across conditions: busy, piled, hands, glare, night, empty, each type. A built-in page plays the clip; a human taps once per object crossing the line and picks its type; each tap is stored with its video time. Every clip is counted **twice** (two people, or the same person on two days); differences are reconciled on screen. Minimum 300 crossings per condition before that condition is used by the gate (config). Truth is locked and versioned; its time blocks are excluded from train and tune. |
| 9 | `harness` | Replays clips through `inno_count` exactly as the agent runs it: same resize, ROI and timing. **Real-time mode** simulates the target hardware's inference latency and drops frames as a live stream would. Matches system events to truth taps one to one inside a time window (config, default ±1.5 s): an unmatched tap is a miss, an unmatched event is a false or double count. Reports misses, doubles and per-type confusion with confidence intervals, per condition; the link rate (§7.1); latency per hardware profile. Sweeps 3/5/10/20 fps on the **tune** set. Deterministic: the same clip twice gives identical events. |
| 10 | `shadow` | Before activation, the candidate runs on the real agent beside the current model for at least one full production day, counting but not reporting to stock. Its counts are compared with the current model and with the physical check (sacks, crates). |
| 11 | `gate` | Candidate vs current on the harness, paired on the same clips. Pass only if neither misses nor doubles get worse in any condition by more than the **noise margin** (config, derived from the confidence intervals) and per-type accuracy meets the owner's thresholds. Writes an HTML report. |
| 12 | `export_publish` | Export ONNX plus the sidecar JSON. Keep the existing fields `names, imgsz, conf, iou, roi_crop` and add `class_map`, `size_calibration`, `reference_frame_sha`, `counting_core_version`, `dataset_version`, `gate_report_sha`. For ERP projects, publish through the existing endpoint `POST /en/vision/api/v1/counter-app/model/upload/` (JWT from `/en/auth/jwt/create/`, fields `branch`, `activate`; see `HARDWARE-AGENT/training/productline.py cmd_publish`): upload with `activate=false`, run `shadow`, then a human presses activate with the gate report attached. One-click rollback to the previous model sha. Projects not tied to the ERP simply export the model folder. |
| 13 | `feedback` | Pull reviewers' corrections and the crops of held periods back into the review queue (active learning). |

### 6.1 The guided flow (what the owner sees)

1. **New project:** name, product, template.
2. **Add sources** (§5.2) and helper models (§5.3).
3. **It looks first.** It finds objects (open-vocabulary detector), groups similar ones (embedding
   clusters plus measured size) and shows the groups: "I found these N kinds of things; name them,
   merge them, or say which are not what you want."
4. **He decides by groups, not one by one,** then fixes only the items the system is unsure about
   (lowest-confidence and cluster-border items first).
5. **It asks what he wants:** count only / count per type / detect a defect / measure size. Claude
   turns his plain-language answer into a task spec (classes, counting line, size rule), shown to
   him for confirmation before anything is trained.
6. **It tells him what is missing** ("only 30 examples of حبة كاملة at night; I need about 200")
   and proposes which hours of video to take them from.
7. **Truth, train, gate, shadow, publish,** each with its explanation panel (§5.6).

---

## 7. Counting, classifying and size

### 7.1 Two engines (owner's decision, 2026-11-24)

**1 — Counting Engine:** detects the object, tracks it, and records its crossing of the counting
line **once**. It knows nothing about type.

**2 — Classification Engine:** decides the type from **several images of the same object**
collected along its track. It also outputs `partial`, `two_objects` and `not_product`, so it
doubles as the verifier (there is no third model).

The contract between them:
- **Count never waits on type.** Two messages share one `uid`: a `count` event sent at the
  crossing, stamped with the crossing time, and a `classification` event that follows within a
  bounded time (config, default ≤ 2 s). If the second never arrives, the type is `unknown` and the
  count stands. Both are idempotent on `uid` (decision D7).
- **A verdict never rewrites a count.** `not_product`, `partial` and `two_objects` are fields on
  the event. What they do to stock is ERP policy (hold the period). The harness reports how often
  each verdict is right against truth.
- **Crops:** K crops (e.g. K = 5) taken where the object is separate from others, spread along the
  track, never truncated. The counting line sits far enough into the zone that K crops exist before
  the crossing. The harness reports the **link rate**: the share of counted objects that got K
  usable crops (an ID switch breaks the link).
- **Unit of labelling and scoring:** the track. Splits are by time block, so one object's crops
  never sit in both train and truth.
- **Voting limit:** it reduces pose and occlusion noise only. It does not fix lighting. A class
  told apart by colour needs labelled examples in every lighting the camera produces. If the camera
  switches to infrared at night, colour is gone, and the studio must say that the class cannot be
  told apart then instead of guessing.
- **Size** enters the classifier as a measured number (§7.2), not from the crop pixels.

Limit, state it everywhere: **the verifier catches false counts, and merged pairs it can see. It
does not catch an object the detector never saw.** Those are caught by the physical check (sacks,
crate weight) and the truth set.

### 7.2 Size measurement

- **Calibrate the plane, not the product.** Per camera angle, the owner clicks four points on the
  belt or surface and enters two real distances. The resulting homography gives sizes in
  centimetres for any product. Stored in the sidecar; invalid the moment "camera moved" fires.
- A fit learned from human-confirmed tracks of a known class is kept only as a cross-check of the
  homography.
- Measure each object **once**: the median of its box (or mask) size over the frames around the
  counting line.
- Ignore truncated boxes for size and type.
- Test whether a mask beats a box for separating size classes. Report it in the gate.

### 7.3 The shared counting core `inno_count`

Built in two steps so the baseline is honest:

- **v0 — the legacy behaviour, extracted verbatim** from the agent (stretch resize, greedy tracker,
  frame-count thresholds). Its only purpose is to reproduce today's numbers in the harness.
- **v1 — the fixes, each measured as its own delta against v0:**
  - letterbox resize (matching training), vectorised decode;
  - ByteTrack from a permissively licensed implementation, instead of the greedy tracker;
  - thresholds in **seconds**, on stream timestamps;
  - the "first seen near the line is never counted" fault fixed;
  - per-track class vote, size at the line, truncated detections excluded by geometry;
  - a model that fails to load is a refusal with a reason, never a silent motion fallback;
  - the live frame is compared with the reference frame on static structures only (masked regions
    and moving areas excluded). If the camera moved, report "camera moved" and stop counting as
    trusted;
  - health per minute: frames processed, blind seconds, reconnects, sent with the counts.

**Live view must never change the count** (was v1 §11.2):
- Capture and counting run in their own process at a fixed rate. The live view is a **separate
  consumer** (the camera's sub-stream or a re-encode of the same frames), started only when opened.
- The harness proves it: the same clip counted with and without a viewer gives the same events.
- Every reconnect and blind second is recorded and shown on the live page and on the period.
- A wired connection and the camera's lower-bitrate stream for counting; recorded evidence clips
  for held periods. The agent clock is NTP-synced and events carry UTC time.
- Evidence crops have a retention period and a size cap (config).

---

## 8. What the ERP needs from this project (interfaces only)

The ERP-side work (per-type counts per period, the per-period verdict, the review screen) is a
separate ERP task. This project delivers:

- per counted object: `uid, ts (UTC), track_id, model_sha, counting_core_version` at the crossing,
  then `class, class_confidence, verdict, size_at_line, crop jpeg` in the follow-up;
- per minute: `frames, blind_seconds, reconnects, camera_moved`;
- the **Classification Engine** (§7.1), running locally on every counted object.

---

## 9. Owner decisions (defaults in brackets; all stored as config)

- D1 Release thresholds per class and condition [set after the first truth set; default "not worse
  than current in any condition beyond the noise margin"].
- D2 Claude model for QA [`claude-opus-5-5`]; chosen from measured agreement on the mark sheet.
- D3 Architecture: **decided** — two engines (§7.1).
- D4 Hardware: cashier PC CPU / OpenVINO / a GPU box [decided by the fps sweep].
- D5 Labelling tool [built-in screens; Label Studio or CVAT by export/import when wanted].
- D6 Training stack and licence [undecided — **settle before P1**]. Ultralytics is AGPL-3.0: either
  buy its Enterprise licence, or use permissively licensed stacks (RF-DETR, a MIT ByteTrack) for
  everything that ships. Get a legal opinion; this prompt is not one.
- D7 Event contract [two messages per object, §7.1] or one delayed message.
- D8 Workspace location and which large media are linked in place rather than copied.
- D9 Language of the UI and explanations [Arabic, with English available].

---

## 10. Acceptance tests

Pipeline:
1. A training run refuses a dataset containing any label without `human:` provenance.
2. Splits: no frame within 10 minutes of a tune or truth block appears in training; nothing in
   truth appears in tune.
3. `inno_count` v0 in the harness reproduces the current agent's events on the same clip at the
   same frame rate (diffed event by event).
4. Mutation: replace ByteTrack with the greedy tracker → the harness reports more doubles on the
   "piled" clips (the harness can fail).
5. The gate refuses a candidate that improves total count but worsens misses on the night clips;
   and it does **not** refuse one whose difference is inside the noise margin.
6. Size: on human-confirmed tracks, size-class separation is reported with a confusion matrix
   after homography correction. Raw-pixel bands are shown to be worse.
7. `claude_qa` prints a `count_tokens` estimate and stops without a yes. A dry run sends nothing.
   A run that would exceed the project budget cap refuses.
8. Publish attaches the gate report, uploads inactive, and after activation the ERP shows the new
   model's sha on counted events. Rollback restores the previous sha.
9. Camera moved: shifting the reference by 5 % of the frame flags "camera moved" within one
   minute. The same angle at day and at night, and a changing burned-in clock, do **not** flag it.
10. A truncated object at the crop edge is never counted twice and never used for size.
11. Determinism: the same clip replayed twice gives identical events. With and without a viewer
    attached gives identical events.
12. Truth: two counts of the same clip are stored separately and a disagreement blocks the clip
    from the gate until reconciled.

Studio:
13. Sources: an RTSP camera, a USB camera and an uploaded video each pass Test connection, record
    a clip and ingest it, on Linux **and** Windows. (CI uses a simulated RTSP server and a virtual
    camera where a real one is missing.)
14. No credential appears in `project.yaml`, the database, the logs or an exported archive.
15. A project exported on Linux imports on Windows (and back) with identical dataset hashes, and
    its models still load.
16. Folder per product: three folders of pictures become three classes with `human:` class labels;
    the detector still refuses to train until boxes are confirmed.
17. An imported dataset is `imported:` and is refused for training until declared reviewed.
18. Two projects with different products and classes exist side by side; nothing in the code
    names bread.
19. Every pipeline step leaves an activity-log entry; the project story's figures match the log.
20. A registry entry with an AGPL licence cannot be selected for a shipped component while D6 says
    "permissive only".

---

## 11. Order of work

**STEP 0 — measure (bread):**
1. Re-render the existing datasets with their labels and count the label faults.
2. Run r2 on its own October clip with its training ROI, to separate a bad model from wrong
   conditions.
3. Measure the processing rate with and without a viewer (the reported 7×).
4. Survey the 25 h: which types appear and in which hours; whether the camera switches to infrared
   at night. Report what the footage **cannot** support.
5. Ask the owner to record two or three further days, so one whole day can be truth-only.

**Then:**
- **P0** repo skeleton, CI on both OSes, project store (§5.1), sources (§5.2), ingest, activity log.
- **P1** `inno_count` v0 → v1, truth page, harness.
- **P2** sample, model registry, prelabel bake-off, review screens (fold in `label-app/`),
  folder-per-product import.
- **P3** claude_qa with mark sheet, review loop, blind audit.
- **P4** dataset snapshots, train bake-off.
- **P5** gate, shadow, publish, rollback.
- **P6** feedback loop.
- **P7** guided wizard polish, project story, user guide; then the next task types.

At the end of each phase: a short HTML report with real numbers and real crops, the docs updated,
and **stop for the owner's review** before starting the next phase. Stop and ask, do not guess,
when: a decision in §9 is still open and blocks the work; a paid call is needed; something would
touch the ERP repo or a production branch.

---

## Appendix A. What changed from v1

Added at the owner's request: named projects with their own data and models; camera sources of
any kind plus video and image upload; a helper-model registry; folder per product; Linux and
Windows as equal targets; any product, not only bread; an explanation layer and project story.

Changed after review:
- truth needs event-level matching, double counting, minimum sizes, and a noise margin in the gate;
- a separate tune set, so nothing is tuned on truth;
- `inno_count` v0 (legacy, verbatim) before v1, so the baseline is reproducible;
- STEP 0 now checks what the single day of footage can support, and asks for more days;
- size calibration by homography of the surface, instead of "a known full loaf by position";
- truncated objects are labelled and detected, and excluded by geometry;
- the count/type contract is two idempotent messages, and a verdict never rewrites a count;
- licences recorded, with the AGPL question as decision D6;
- a shadow run and rollback before activation;
- ASCII class ids with display names;
- self-changing regions (the burned-in clock) masked; timestamps from the stream;
- a mark sheet and a blind audit for the Claude and human review steps;
- built-in labelling as the default (CVAT needs Docker, heavy on Windows);
- "live view must never change the count" moved into `inno_count` (§7.3);
- prompt-to-watcher moved out to Appendix B as a separate ERP task;
- section order fixed.

## Appendix B. Prompt-to-watcher (separate ERP task, NOT built in this repo)

Kept here so the idea is not lost. It is an ERP vision-module feature that will use this studio's
models and `inno_count` primitives.

The owner writes what a camera should watch: tampering, crowding, a danger zone, a person at a
desk, presence, attendance. The system then:

1. takes a snapshot, sends it with the prompt to Claude, and gets back a **proposed watch recipe**:
   zones/lines drawn on the snapshot, the detector to use (person / pose / object /
   open-vocabulary), the rule (dwell time, count threshold, schedule, direction) and the alert;
2. shows the drawing and the rule in words; the owner corrects and confirms, and nothing runs
   before that;
3. replays the recipe on recorded footage and shows what it would have flagged, before it goes
   live;
4. keeps rules to a **fixed, tested set of primitives** (zone entry, dwell, count in zone, line
   crossing, presence/absence on a schedule, object left/removed). A prompt that needs something
   else is answered "not supported yet", never improvised.

Notes for the builder:
- The ERP already has watchers, behaviour watchers, persons and attendance in `BACKEND/vision`.
  Build on them.
- Monitoring employees (desk presence, attendance by face) needs the staff told and, by country, a
  legal basis. Make it a per-branch setting that is off by default, with a visible notice text.
- "Tampering" has no general detector. It becomes concrete rules per site, for example a hand in
  the crate zone with no counted object, or the camera covered or moved.
