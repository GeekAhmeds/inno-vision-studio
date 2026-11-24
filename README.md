# Inno Vision Studio

استوديو لتدريب نماذج الرؤية لأي منتج (العيش أولًا): من الكاميرا أو الفيديو أو الصور، إلى بيانات يراجعها
إنسان، إلى موديل مقاس على عدّ بشري، ولا يُنشر إلا إذا غلب الموديل الحالي. يعمل على لينكس وويندوز.

A guided studio for building vision models for any counted product: sources → reviewed dataset →
trained model → measured on human-counted truth → release gate → published. Linux and Windows.

- The full specification: [`docs/VISION_TRAINING_STUDIO_PROMPT.md`](docs/VISION_TRAINING_STUDIO_PROMPT.md)
- دليل الاستخدام بالعربية: [`docs/USER_GUIDE.ar.md`](docs/USER_GUIDE.ar.md)
- What is built and what is not: [`docs/STATUS.md`](docs/STATUS.md)

## Run

Needs [uv](https://docs.astral.sh/uv/) (it installs the right Python by itself).

```
uv sync
uv run studio start
```

The studio opens at `http://localhost:8765`. Projects are stored in `~/InnoVisionStudio`
(set `INNO_STUDIO_HOME` to move it). Media, datasets and weights never go into git.

## Test

```
uv run pytest
```

CI runs the same tests on Ubuntu and Windows.

## Layout

```
packages/inno_count/   shared counting core (placeholder until phase P1)
studio/                backend: projects, sources, ingest, explain, api, cli
web/                   UI served by the backend (plain JS for now, no build step)
docs/                  specification, user guide, status
tests/
```
