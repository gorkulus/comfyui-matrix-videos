---
name: comfyui-matrix-videos
description: Use when making video grids from sweeps or selected clips. Render and verify labeled MP4s without a database.
version: 1.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [comfyui, video, matrix, batch-review, visualization]
    related_skills: [comfyui]
---

# ComfyUI Matrix Videos

## Overview

Use this skill to convert a ComfyUI batch manifest into a compact labeled matrix video. Rows and columns are batch parameters such as steps, seeds, prompts, checkpoints, ControlNet recipes, motion LoRAs, or any other fields present in the manifest.

The companion CLI is `comfy-matrix-video` from the `comfyui-matrix-videos` repository. This skill also ships the same standalone script at `scripts/comfy_matrix_video.py`, plus a style template under `templates/`. The tool reads JSON, JSONL, or CSV, extracts source video paths, builds a grid with FFmpeg and Pillow, and writes an H.264 MP4 plus an optional PNG preview frame.

This skill is intentionally standalone. It does not require a media manager, database, tag system, or file metadata integration.

## Choose the mode first

- **Sweep** (legacy default): use actual row/column parameter fields; all existing flag-only commands below remain valid. Optional explicit prefix: `comfy-matrix-video sweep ...`.
- **Manual**: use an explicit ordered list of paths/labels, not fabricated parameter axes. Read `references/manual-mode.md` before rendering. Copy `templates/manual_sources.example.json` and `templates/manual_design.example.json`; edit paths and native dimensions/FPS/frame count. Source paths resolve relative to the source manifest. Fill row-major; reset the one-based audio index after reordering.

```bash
python3 scripts/comfy_matrix_video.py manual --sources sources.json --design design.json --dry-run
python3 scripts/manual_matrix_video.py --sources sources.json --design design.json \
  --stage prepare --cache-root ./unwatched-cache
python3 scripts/comfy_matrix_video.py manual --sources sources.json --design design.json \
  --stage compose --cache-root ./unwatched-cache --output-prefix outputs/review
```

Manual defaults: silence, one whole-grid cycle, no source trim or fade, native equal-sized cells. `all` (default) prepares missing entries and composes; `compose` cannot prepare missing entries. Opt into trim/fade only when the user requests them; remove preparation settings when inputs are already prepared. Keep lossless caches outside watched/indexed folders. Use a new output prefix for revisions; never overwrite original media.

Inspect the resulting `.preview.png`, probe the MP4, read the `.verification.json`, and watch/listen to repeat seams before claiming perceptual continuity. Verify exact source order, caption fit, geometry, cadence, duration, chosen audio and loop count. Dry-run validates configuration, not media. Hash-verified repetition is not proof that source endpoints are seamless.

## When to Use

- The user ran Hermes' ComfyUI `run_batch.py` and wants a visual review grid.
- The user has a custom JSONL/CSV batch manifest with generated MP4 paths.
- The user wants rows and columns mapped to sweep parameters.
- The user wants a quick preview/contact sheet frame for a generated matrix video.

Do not use this for:

- Running ComfyUI workflows; use the `comfyui` skill first.
- Very large grids where cells would be unreadable; split by filters or phases instead.
- Long videos where extracting many frames would be too slow; reduce `--frames`.

## Install

From the repository root:

```bash
python3 -m pip install -e .
```

Without installing the package, run the bundled script from this skill/repo:

```bash
python3 scripts/comfy_matrix_video.py --help
```

Requirements:

- Python 3.10+
- FFmpeg on `PATH`; manual mode also requires ffprobe, libx264 and FFV1
- Pillow, installed by the package

## Basic Recipe

1. Run a ComfyUI batch and save JSON output:

```bash
python3 /path/to/comfyui-skill/scripts/run_batch.py \
  --workflow workflow_api.json \
  --args '{"prompt":"abstract luminous shape"}' \
  --sweep '{"steps":[8,16],"seed":[101,202,303]}' \
  --output-dir ./outputs/sweep \
  > batch.json
```

2. Dry-run the inferred grid:

```bash
comfy-matrix-video \
  --manifest batch.json \
  --row-key args.steps \
  --col-key args.seed \
  --video-key outputs[].file \
  --dry-run
```

3. Render the matrix:

```bash
comfy-matrix-video \
  --manifest batch.json \
  --row-key args.steps \
  --col-key args.seed \
  --video-key outputs[].file \
  --title "ComfyUI sweep" \
  --subtitle "Rows: steps   Columns: seed" \
  --output ./outputs/steps_by_seed_matrix.mp4 \
  --preview-frame ./outputs/steps_by_seed_matrix.png
```

Equivalent no-install form from the repository or skill directory:

```bash
python3 scripts/comfy_matrix_video.py \
  --manifest batch.json \
  --row-key args.steps \
  --col-key args.seed \
  --video-key outputs[].file \
  --output ./outputs/steps_by_seed_matrix.mp4
```

## Manifest Paths

Use dotted paths to select fields:

- `args.steps`
- `args.seed`
- `params.profile`
- `params.input_index`
- `outputs[].file`
- `result.outputs[].fullpath`

`[]` expands a list and chooses the first matching value. If the video key is omitted, the CLI tries common locations automatically, but explicit `--video-key` is more reliable.

## Filtering

Use `--phase` for manifests with `params.phase`, or repeat `--filter KEY=VALUE` for arbitrary fields:

```bash
comfy-matrix-video \
  --manifest batch.jsonl \
  --phase motion \
  --row-key params.input_index \
  --col-key params.profile \
  --video-key result.outputs[].fullpath \
  --output motion_matrix.mp4
```

## Style

The bundled default style is intended to be public-facing, not just utilitarian: black canvas, white text, subtle white grid lines, generous header spacing, pixel-aware row-label wrapping, and a top legend band with no horizontal line directly above the column labels.

Use `--style templates/matrix_style.example.json` to customize:

- canvas size, optional fixed cell size, and legend dimensions
- colors and grid-line alpha
- font family, explicit font paths, and variable-font weights
- title/subtitle/label sizes and positions
- row-label left/right padding
- top legend padding and separator positions
- FPS and CRF
- temp work root

Font handling is local-only. A style may name a family such as `Raleway`; the renderer asks the local system/fontconfig for that font and falls back to `font_fallback` when unavailable. It does not stream Google Fonts or download fonts at render time. For repeatable output, install or vendor the `.ttf` locally and set `font_regular` / `title_font` to that path.

Keep the temp work root outside directories that other apps are watching. Sweep mode creates temporary PNG frames; manual mode streams video and retains separate preparation/composition caches.

## Common Pitfalls

1. **Wrong video path key.** Run `--dry-run`, inspect one manifest record, then pass the explicit `--video-key`.
2. **Grid too dense.** A 12x12 grid may render, but labels and cells can become unreadable. Split by phase/filter.
3. **Missing FFmpeg.** The CLI shells out to `ffmpeg`. Install FFmpeg first.
4. **Output directory used as temp directory.** Do not put temp frames in synced/watched media folders. Use the default system temp directory or set a safe `work_root` in style JSON.
5. **Non-video outputs.** Hermes ComfyUI batches may include images, audio, and videos. Point `--video-key` at the MP4 output field.

## Verification Checklist

- [ ] `comfy-matrix-video --dry-run ...` reports expected rows/columns.
- [ ] Filled cell count matches expected batch size.
- [ ] Output MP4 exists.
- [ ] Preview frame exists if `--preview-frame` was requested.
- [ ] Visual check shows no missing dark-red cells unless intentionally allowed.
- [ ] Labels are readable enough for review; if not, increase `legend_width` or split the grid.
