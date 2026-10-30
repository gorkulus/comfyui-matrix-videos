# Manual mode: selected videos, not parameter axes

Use `comfy-matrix-video manual` for a curated ordered list of clips. This is a distinct mode, not a sweep manifest with made-up row/column parameters. Existing flag-only sweep commands still work unchanged; `comfy-matrix-video sweep ...` is an optional explicit spelling.

## Quick start

Python 3.10+, Pillow and local FFmpeg/ffprobe with libx264 and FFV1 are required. No ComfyUI server, GPU, ML package, database or network service is used. Font rendering is local with Pillow; FFmpeg drawtext is not required.

Generate disposable synthetic clips **from a repository checkout** (the destination must not exist). The sample generator is not included in the copyable skill bundle. Skill-only users should supply their own clips using `templates/manual_sources.example.json` and `templates/manual_design.example.json`, then run `python3 scripts/comfy_matrix_video.py manual --sources <sources.json> --design <design.json> --output-prefix <output-prefix>` from the skill directory.

```bash
python3 examples/create_manual_sample.py --directory ./sample-manual
comfy-matrix-video manual --sources sample-manual/sources.json \
  --design sample-manual/design.json --dry-run
comfy-matrix-video manual --sources sample-manual/sources.json \
  --design sample-manual/design.json --output-prefix outputs/selected
```

This writes `outputs/selected-1x.mp4`, `outputs/selected-3x.mp4`, `outputs/selected.preview.png` and `outputs/selected.verification.json`. The templates are silent by default. Set `audio_source_index` to `1` to use the synthetic reference tone. Use a new output prefix for revisions; existing artifacts are refused unless `--overwrite` is explicit. Sources, source/design JSON and the configured font are protected from output-path collisions (including symbolic and hard-link aliases) even with `--overwrite`. Videos, preview and verification JSON are staged and verified before publication, then each is copied to a temporary file in its destination directory and atomically replaced; existing shared inodes are never truncated. Publication is atomic per file, not a transaction across the entire set: a disk/permission failure during publication may leave a partially updated set.

No-install invocation (Pillow must still be available):

```bash
python3 scripts/comfy_matrix_video.py manual --help
python3 scripts/manual_matrix_video.py --help
PYTHONPATH=src python3 -m comfyui_matrix_videos.cli manual --help
```

## Sources: order is authoritative

Copy `templates/manual_sources.example.json` and edit it:

```json
{
  "expected_count": 2,
  "sources": [
    {"path": "media/reference.mp4", "label": "Reference"},
    {"path": "media/variation.mp4", "label": "Variation"}
  ]
}
```

Sources fill **row-major**: left to right, then top to bottom. No sorting, directory globbing, deduplication or parameter-axis inference takes place. Duplicate paths are permitted as separate cells. Move the entire object to move its label too. The exact count must equal `rows * columns`; incomplete grids are rejected, not padded with invented cells. `expected_count` is an optional additional assertion. Labels default to empty strings.

Relative source paths resolve **only against the sources JSON directory**, never against the current working directory. Relative explicit font paths resolve against the design JSON directory. CLI paths (`--output-prefix`, `--cache-root`) resolve from the working directory. This differs deliberately from sweep mode's historical CWD-first fallback.

## Design and timing

Required fields: `rows`, `columns`, `cell_width`, `cell_height`, `fps`, `frames`. Dimensions, frame count and FPS are **assertions**, not scaling/retiming instructions. All clips must already have the same native geometry, constant frame cadence and frame count (unless explicitly trimmed below). Frame zero is reset to time zero; no interpolation, automatic shortest-clip policy, last-frame hold or individual-cell loops.

One grid cycle lasts exactly `frames / fps` seconds. `fps` accepts integer or rational strings, e.g. `"24"` or `"30000/1001"`; supported range is greater than zero through 240. `loops` defaults to `[1]` and contains distinct positive integers. Each value creates one MP4 repeating the **entire composed grid**. Audio-enabled durations must correspond to whole samples at the selected sample rate; incompatible frame/rate/sample combinations are rejected.

| Field | Default | Meaning |
|---|---|---|
| `gutter` | `8` | Horizontal and vertical gap between tile blocks |
| `margin` | `0` | Fallback outer margin on all sides |
| `margins` | absent | Per-side left/right/top/bottom overrides |
| `caption_height` | `32` | Reserved strip below every video, even unlabeled ones |
| `label_position` | `"caption"` | `caption`, `top_header` or `none` |
| `label_align` | `"center"` | `left`, `center` or `right` |
| `font_size` | `18` | Caption size in pixels |
| `font` | empty | Explicit local font file; otherwise local DejaVuSans.ttf |
| `background` / `text_color` | `#141416` / `#eeeeee` | Six-digit hex colors |
| `square` | `false` | Center grid in a square, incompatible with explicit margins |
| `crf` / `preset` | `18` / `medium` | Final H.264 quality/speed |
| `audio_source_index` | `null` | Silence, or one-based source index |
| `audio_sample_rate` / `audio_channels` | `48000` / `2` | PCM/AAC target format |
| `loops` | `[1]` | Whole-grid repeat counts |

For `C` columns, `R` rows, native tile `W × H`, gutter `G` and caption height `K`, grid width is `C*W + (C-1)*G`; grid height is `R*(H+K) + (R-1)*G`. Add outer margins for the canvas. Caption strips therefore add to **video-to-video vertical distance**. Set `caption_height: 0` for equal horizontal/vertical video gutters. Cell dimensions, gutter, margins and caption height must be even for 4:2:0. Square centering aligns offsets to even pixels, assigning any remaining two pixels to right/bottom.

`top_header` requires zero caption height, labels only on first-row sources, and enough top margin for font size plus 8 px. `none` hides labels but does **not** remove a configured caption strip. Caption mode needs font size plus 4 px minimum strip height. Labels are not automatically truncated or wrapped; explicit newlines work. Pixel-measured overflow is rejected rather than drawing into another tile. Inspect the preview even when the fit check passes.

## Audio: one source, never a mix

Default output is silent. A non-null `audio_source_index` is **one-based in the current order**; reset it explicitly after reordering. Only its first audio stream is used. Other clips may have no audio. Choosing a silent source fails clearly.

Preparation decodes available audio to float PCM, resamples/channel-converts to the configured format (32000, 44100 or 48000 Hz; mono or stereo), truncates to the video cycle or pads short audio with silence. By default audio and video each start at zero independently; original stream-start offsets are not compensated unless explicitly requested. No fade is applied by default. Whole-cycle PCM is repeated before **one continuous AAC encode** per output, rather than concatenating independently encoded AAC segments. MP4 audio presentation duration is verified; raw AAC decoders may expose packet tail padding.

## Optional source preparation, never project defaults

All sources pass through a reusable lossless video normalization cache. With absent/null/empty `preparation`, the source must already have the requested frame count; nothing is trimmed and no fade is applied. Already-prepared media should use this default.

To explicitly keep only the first 11 of 12 frames and apply a 10 ms end-only linear audio fade, set the design to `frames: 11` and opt in on **each applicable source**:

```json
{
  "path": "media/reference.mp4",
  "label": "Reference",
  "preparation": {
    "expected_source_frames": 12,
    "end_fade_ms": 10,
    "align_audio_to_video_start": true
  }
}
```

`expected_source_frames` asserts the original count and must be at least design `frames`. No arbitrary start trim or fade-in is supported. Fade length is rounded to an integer sample count. `align_audio_to_video_start` skips audio before the video's first timestamp; negative offsets (audio starting later) are rejected rather than guessed. Preparation video is FFV1 in NUT, using the source pixel format. Decoded retained-frame hashes must match the source; unsupported lossless pixel formats fail rather than silently changing pixels. Final composition is H.264/yuv420p and may be lossy, including color/chroma conversion. Originals are only read, never rewritten.

## Stages and caches

```bash
comfy-matrix-video manual --sources sources.json --design design.json \
  --cache-root ./scratch-cache --stage prepare
comfy-matrix-video manual --sources sources.json --design design.json \
  --cache-root ./scratch-cache --stage compose --output-prefix outputs/revision
```

- `all` (default): prepare missing sources, then compose and mux.
- `prepare`: create/verify per-source cached video and PCM only; no output prefix required. JSON stdout includes ordered records and cache paths.
- `compose`: require all preparation entries; changing source content or preparation requires `prepare`/`all` first. Missing composition caches are built normally.
- `--dry-run`: validate configuration, paths and geometry only; no media probing, encoding or writes. It is not a media-validity check.

Default cache root is `comfy-matrix-cache-<uid>` under the system temp directory. Temp directories can be purged by the OS; choose an explicit persistent **unwatched** cache root when needed. Keep caches/scratch outside indexed/synced media folders. Preparation keys include source SHA256, prep policy, asserted native cell width/height, target cadence/audio format, algorithm version and FFmpeg version—not labels/order or decorative layout. Changing cell dimensions invalidates preparation: `compose` reports a cache miss, and `prepare`/`all` revalidate native geometry rather than padding/cropping to fit. Entries created before native dimensions were included in the key also require preparing again. Composition keys include ordered prepared video hashes, actual rendered caption pixels, layout/encoding settings and FFmpeg version. Audio choice and loop counts do not invalidate composition. Cached files are content-hash checked; corruption fails with an entry path to remove/rebuild. Run only one process per cache root/output prefix at a time; caches are not concurrency-locked. Cache entries are never automatically evicted.

Processing streams through FFmpeg instead of extracting frame piles. CPU-only, encoder/decoder limits at most two threads per codec (composition input decoders use one), filters one thread, and nice +10 where available. This is not a whole-process two-core cap: a grid has multiple decoder contexts. Verification decodes sources and outputs several times; lossless intermediates and repeated PCM need disk space. Intended for short review clips, not unbounded archival videos or huge grids. Scratch is cleaned on success/failure; interrupted/incomplete cache entries may remain and are rebuilt when no completion record exists.

## Verify and limits

The verification sidecar records source order/paths/hashes, prepared paths/hashes, per-cell rectangles, resolved design, composition key, output hashes/durations and preview path. Source and retained decoded pixel hashes are checked; outputs are probed for geometry, cadence, frame count, duration and strict decode errors. Every repeated output frame is compared with the base grid's decoded hashes. These checks prove repetition, **not perceptual continuity** at the original end/start seam. Watch/listen to the result before calling it seamless.

```bash
ffprobe -v error -show_streams -show_format -of json outputs/selected-3x.mp4
```

Inspect `outputs/selected.preview.png` for ordered cell placement, readable labels and gutters. Sidecars include local resolved paths for provenance: review/sanitize them before publishing. No provenance is uploaded automatically.

Limits: uniform constant-frame-rate, square-pixel, non-rotated video; even native dimensions; first video/audio stream only; no scaling, cropping, mixed-size layouts, blank cells, audio mixing or crossfades. Large numbers of open inputs may hit OS/FFmpeg limits. Normalize copies externally if needed. Font availability affects caption rendering; vendor a suitable licensed font for repeatability. Unknown design/source/preparation fields fail early to catch typos. This public schema is deliberately smaller than any one-off project renderer; migrate only documented keys.
