#!/usr/bin/env python3
"""Render labeled matrix videos from ComfyUI batch manifests."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
DEFAULT_VIDEO_KEYS = (
    "result.outputs[].fullpath",
    "result.outputs[].file",
    "result.outputs[].path",
    "outputs[].file",
    "outputs[].fullpath",
    "outputs[].path",
    "video",
    "file",
    "path",
)
DEFAULT_STYLE = {
    "canvas_max": 2048,
    "background": "#000000",
    "grid_line": "#2d2d32",
    "text_color": "#ffc5d3",
    "muted_text_color": "#ffc5d3",
    "font_regular": "DejaVuSansMono.ttf",
    "font_bold": "DejaVuSansMono-Bold.ttf",
    "title_font_size": 36,
    "subtitle_font_size": 22,
    "label_font_size": 20,
    "row_label_font_size": 20,
    "legend_width": 260,
    "legend_height": 240,
    "grid_line_width": 1,
    "title_y": 22,
    "subtitle_y": 82,
    "header_grid_y": 150,
    "column_label_bottom_padding": 18,
    "row_label_left_padding": 14,
    "crf": 18,
    "fps": 30,
    "work_root": "",
}


@dataclass(frozen=True)
class MatrixPlan:
    records: list[dict[str, Any]]
    rows: list[Any]
    cols: list[Any]
    row_labels: dict[Any, str]
    col_labels: dict[Any, str]
    cell_records: dict[tuple[Any, Any], dict[str, Any]]
    missing: list[tuple[Any, Any]]


class MatrixError(RuntimeError):
    """Raised for user-facing CLI errors."""


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        raise MatrixError(f"Invalid hex color: {value!r}")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def load_font(spec: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if spec:
        expanded = Path(spec).expanduser()
        candidates = [expanded] if expanded.exists() else []
        common_dirs = [
            Path("/usr/share/fonts/truetype/dejavu"),
            Path("/usr/share/fonts/TTF"),
            Path("/usr/share/fonts/truetype/liberation2"),
            Path("/Library/Fonts"),
            Path("/System/Library/Fonts"),
        ]
        candidates.extend(d / spec for d in common_dirs)
        for candidate in candidates:
            try:
                if candidate.exists():
                    return ImageFont.truetype(str(candidate), size)
            except Exception:
                pass
        try:
            return ImageFont.truetype(spec, size)
        except Exception:
            pass
    return ImageFont.load_default()


def wrap_label(text: Any, max_chars: int, max_lines: int = 4) -> str:
    words = str(text).replace("_", " ").split()
    if not words:
        return ""
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word[:max_chars]
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and len(" ".join(words)) > len(" ".join(lines)):
        lines[-1] = lines[-1].rstrip("…")[: max(1, max_chars - 1)] + "…"
    return "\n".join(lines)


def natural_key(value: Any) -> tuple[Any, ...]:
    text = str(value)
    parts = re.split(r"(\d+)", text)
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


def parse_json_or_jsonl(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return []
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("results", "records", "items"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
        return [data]
    raise MatrixError(f"Unsupported manifest JSON root in {path}: {type(data).__name__}")


def load_manifest(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise MatrixError(f"Manifest not found: {path}")
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    return parse_json_or_jsonl(path)


def _select_part(values: list[Any], part: str) -> list[Any]:
    expand = part.endswith("[]")
    key = part[:-2] if expand else part
    out: list[Any] = []
    for value in values:
        if value is None:
            continue
        if key == "":
            selected = value
        elif isinstance(value, dict):
            selected = value.get(key)
        elif isinstance(value, list) and key.isdigit():
            idx = int(key)
            selected = value[idx] if 0 <= idx < len(value) else None
        else:
            selected = None
        if selected is None:
            continue
        if expand:
            if isinstance(selected, list):
                out.extend(selected)
            else:
                out.append(selected)
        else:
            out.append(selected)
    return out


def select_values(record: dict[str, Any], expr: str | None) -> list[Any]:
    if not expr:
        return []
    values: list[Any] = [record]
    for part in expr.split("."):
        values = _select_part(values, part)
        if not values:
            break
    return values


def get_first(record: dict[str, Any], expr: str | None, default: Any = None) -> Any:
    values = select_values(record, expr)
    return values[0] if values else default


def value_to_label(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if "/" in text or "\\" in text:
        p = Path(text)
        if p.suffix:
            return p.stem
    return text


def first_video_path(record: dict[str, Any], video_key: str | None = None) -> str | None:
    keys = (video_key,) if video_key else DEFAULT_VIDEO_KEYS
    for key in keys:
        for value in select_values(record, key):
            if isinstance(value, str) and (Path(value).suffix.lower() in VIDEO_EXTS or not video_key):
                if not video_key and Path(value).suffix.lower() not in VIDEO_EXTS:
                    continue
                return value
    # Last resort: search shallow string values for a video-looking path.
    stack: list[Any] = [record]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str) and Path(item).suffix.lower() in VIDEO_EXTS:
            return item
    return None


def infer_axis_key(records: list[dict[str, Any]], preferred: str, fallbacks: tuple[str, ...]) -> str:
    if preferred != "auto":
        return preferred
    for key in fallbacks:
        if any(select_values(r, key) for r in records):
            return key
    raise MatrixError("Could not infer axis key. Pass --row-key and --col-key explicitly.")


def parse_filter(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise MatrixError(f"Filter must be KEY=VALUE, got {spec!r}")
    key, value = spec.split("=", 1)
    return key.strip(), value.strip()


def apply_filters(records: list[dict[str, Any]], filters: list[tuple[str, str]]) -> list[dict[str, Any]]:
    if not filters:
        return records
    kept = []
    for record in records:
        ok = True
        for key, expected in filters:
            values = [str(v) for v in select_values(record, key)]
            if expected not in values:
                ok = False
                break
        if ok:
            kept.append(record)
    return kept


def successful_records(records: list[dict[str, Any]], include_failed: bool = False) -> list[dict[str, Any]]:
    if include_failed:
        return records
    return [r for r in records if str(r.get("status", "success")).lower() == "success"]


def order_values(values: Iterable[Any], explicit: str | None = None) -> list[Any]:
    unique = list(dict.fromkeys(values))
    if explicit:
        wanted = explicit.split(",")
        ordered: list[Any] = []
        by_text = {str(v): v for v in unique}
        for item in wanted:
            item = item.strip()
            if item in by_text:
                ordered.append(by_text[item])
        ordered.extend(v for v in unique if v not in ordered)
        return ordered
    return sorted(unique, key=natural_key)


def build_plan(
    records: list[dict[str, Any]],
    *,
    row_key: str,
    col_key: str,
    row_label_key: str | None = None,
    col_label_key: str | None = None,
    row_order: str | None = None,
    col_order: str | None = None,
) -> MatrixPlan:
    if not records:
        raise MatrixError("No records left after filters.")
    rows = order_values((get_first(r, row_key) for r in records), row_order)
    cols = order_values((get_first(r, col_key) for r in records), col_order)
    rows = [r for r in rows if r is not None]
    cols = [c for c in cols if c is not None]
    if not rows or not cols:
        raise MatrixError("Row/column keys produced no values.")

    cell_records: dict[tuple[Any, Any], dict[str, Any]] = {}
    row_labels: dict[Any, str] = {}
    col_labels: dict[Any, str] = {}
    for record in records:
        row = get_first(record, row_key)
        col = get_first(record, col_key)
        if row is None or col is None:
            continue
        cell_records.setdefault((row, col), record)
        if row not in row_labels:
            label_value = get_first(record, row_label_key) if row_label_key else row
            row_labels[row] = value_to_label(label_value)
        if col not in col_labels:
            label_value = get_first(record, col_label_key) if col_label_key else col
            col_labels[col] = value_to_label(label_value)
    for row in rows:
        row_labels.setdefault(row, value_to_label(row))
    for col in cols:
        col_labels.setdefault(col, value_to_label(col))
    missing = [(row, col) for row in rows for col in cols if (row, col) not in cell_records]
    return MatrixPlan(records, rows, cols, row_labels, col_labels, cell_records, missing)


def load_style(path: str | None) -> dict[str, Any]:
    style = dict(DEFAULT_STYLE)
    if path:
        style.update(json.loads(Path(path).expanduser().read_text(encoding="utf-8")))
    return style


def run_ffmpeg_extract(src: str, dest: Path, *, cell: int, fps: int, frames: int) -> None:
    vf = f"scale={cell}:{cell}:force_original_aspect_ratio=decrease,pad={cell}:{cell}:(ow-iw)/2:(oh-ih)/2:black,fps={fps}"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            src,
            "-vf",
            vf,
            "-frames:v",
            str(frames),
            str(dest / "%03d.png"),
        ],
        check=True,
    )


def render_matrix(
    plan: MatrixPlan,
    *,
    output: Path,
    manifest: Path,
    video_key: str | None,
    style: dict[str, Any],
    title: str | None,
    subtitle: str | None,
    frames: int,
    keep_work: bool,
    preview_frame: Path | None,
    allow_missing_videos: bool,
) -> dict[str, Any]:
    if shutil.which("ffmpeg") is None:
        raise MatrixError("ffmpeg is required but was not found on PATH.")
    output.parent.mkdir(parents=True, exist_ok=True)

    nrows, ncols = len(plan.rows), len(plan.cols)
    canvas = int(style["canvas_max"])
    legend_w = int(style["legend_width"])
    legend_h = int(style["legend_height"])
    cell = min((canvas - legend_w) // ncols, (canvas - legend_h) // nrows)
    if cell <= 0:
        raise MatrixError(f"Grid {nrows}x{ncols} is too large for canvas_max={canvas}.")
    width = legend_w + cell * ncols
    height = legend_h + cell * nrows
    width += width % 2
    height += height % 2
    fps = int(style.get("fps", 30))

    work_root = Path(str(style.get("work_root") or tempfile.gettempdir())).expanduser()
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{output.stem}_", dir=work_root))
    frames_dir = work / "matrix_frames"
    frames_dir.mkdir()
    cell_dirs: dict[tuple[int, int], Path] = {}

    try:
        for ri, row in enumerate(plan.rows):
            for ci, col in enumerate(plan.cols):
                record = plan.cell_records.get((row, col))
                if record is None:
                    continue
                src = first_video_path(record, video_key)
                if not src:
                    if allow_missing_videos:
                        continue
                    raise MatrixError(f"No video path for cell row={row!r}, col={col!r}")
                src_path = Path(src).expanduser()
                if not src_path.is_absolute() and not src_path.exists():
                    manifest_relative = manifest.parent / src_path
                    if manifest_relative.exists():
                        src_path = manifest_relative
                if not src_path.exists():
                    if allow_missing_videos:
                        continue
                    raise MatrixError(f"Video file not found for row={row!r}, col={col!r}: {src}")
                d = work / f"r{ri:03d}_c{ci:03d}"
                d.mkdir()
                run_ffmpeg_extract(str(src_path), d, cell=cell, fps=fps, frames=frames)
                cell_dirs[(ri, ci)] = d

        bg = hex_to_rgb(str(style["background"]))
        text = hex_to_rgb(str(style["text_color"]))
        muted = hex_to_rgb(str(style.get("muted_text_color", style["text_color"])))
        grid = hex_to_rgb(str(style["grid_line"]))
        title_font = load_font(style.get("font_bold"), int(style["title_font_size"]))
        subtitle_font = load_font(style.get("font_regular"), int(style["subtitle_font_size"]))
        label_font = load_font(style.get("font_regular"), int(style["label_font_size"]))
        row_font = load_font(style.get("font_regular"), int(style["row_label_font_size"]))
        line_w = int(style.get("grid_line_width", 1))
        header_grid_y = int(style.get("header_grid_y", max(120, legend_h - 92)))
        title_text = title or f"{manifest.stem} matrix"
        subtitle_text = subtitle or f"Rows: {len(plan.rows)} values   Columns: {len(plan.cols)} values"

        for fi in range(1, frames + 1):
            image = Image.new("RGB", (width, height), bg)
            draw = ImageDraw.Draw(image)
            bbox = draw.textbbox((0, 0), title_text, font=title_font)
            draw.text(((width - (bbox[2] - bbox[0])) // 2, int(style["title_y"])), title_text, fill=text, font=title_font)
            bbox = draw.textbbox((0, 0), subtitle_text, font=subtitle_font)
            draw.text(((width - (bbox[2] - bbox[0])) // 2, int(style["subtitle_y"])), subtitle_text, fill=muted, font=subtitle_font)

            for ci, col in enumerate(plan.cols):
                x0 = legend_w + ci * cell
                label = wrap_label(plan.col_labels[col], max(8, cell // 12))
                lines = label.split("\n")
                line_h = int(style["label_font_size"]) + 3
                y = legend_h - int(style["column_label_bottom_padding"]) - line_h * len(lines)
                for li, line in enumerate(lines):
                    bb = draw.textbbox((0, 0), line, font=label_font)
                    draw.text((x0 + (cell - (bb[2] - bb[0])) // 2, y + li * line_h), line, fill=text, font=label_font)

            for ri, row in enumerate(plan.rows):
                y0 = legend_h + ri * cell
                label = wrap_label(plan.row_labels[row], max(8, (legend_w - 20) // 9))
                lines = label.split("\n")
                line_h = int(style["row_label_font_size"]) + 4
                y = y0 + (cell - line_h * len(lines)) // 2
                for li, line in enumerate(lines):
                    draw.text((int(style.get("row_label_left_padding", 14)), y + li * line_h), line, fill=text, font=row_font)
                for ci, _col in enumerate(plan.cols):
                    fp = cell_dirs.get((ri, ci), work / "missing") / f"{fi:03d}.png"
                    if fp.exists():
                        tile = Image.open(fp).convert("RGB")
                    else:
                        tile = Image.new("RGB", (cell, cell), (20, 0, 0))
                    image.paste(tile, (legend_w + ci * cell, y0))

            xs = [0, legend_w] + [legend_w + i * cell for i in range(1, ncols + 1)]
            ys = [0, header_grid_y, legend_h] + [legend_h + i * cell for i in range(1, nrows + 1)]
            draw.line([(0, 0), (width, 0)], fill=grid, width=line_w)
            draw.line([(0, 0), (0, height)], fill=grid, width=line_w)
            draw.line([(width - 1, 0), (width - 1, height)], fill=grid, width=line_w)
            draw.line([(0, height - 1), (width, height - 1)], fill=grid, width=line_w)
            for x in xs[1:]:
                draw.line([(x, header_grid_y), (x, height)], fill=grid, width=line_w)
            for y in ys[1:]:
                x_start = legend_w if y == header_grid_y else 0
                draw.line([(x_start, y), (width, y)], fill=grid, width=line_w)
            frame_path = frames_dir / f"{fi:03d}.png"
            image.save(frame_path, quality=95)
            if fi == 1 and preview_frame:
                preview_frame.parent.mkdir(parents=True, exist_ok=True)
                image.save(preview_frame)

        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-framerate",
                str(fps),
                "-i",
                str(frames_dir / "%03d.png"),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                str(style.get("crf", 18)),
                str(output),
            ],
            check=True,
        )
        return {
            "output": str(output),
            "rows": nrows,
            "cols": ncols,
            "cell_px": cell,
            "canvas": [width, height],
            "frames": frames,
            "fps": fps,
            "missing_cells": len(plan.missing),
            "preview_frame": str(preview_frame) if preview_frame else None,
            "work_dir": str(work) if keep_work else None,
        }
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)


def make_summary(plan: MatrixPlan, *, row_key: str, col_key: str, video_key: str | None) -> dict[str, Any]:
    return {
        "records": len(plan.records),
        "row_key": row_key,
        "col_key": col_key,
        "video_key": video_key or "auto",
        "rows": [str(r) for r in plan.rows],
        "cols": [str(c) for c in plan.cols],
        "matrix_cells": len(plan.rows) * len(plan.cols),
        "filled_cells": len(plan.cell_records),
        "missing_cells": len(plan.missing),
        "missing": [[str(r), str(c)] for r, c in plan.missing[:50]],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a labeled matrix video from a ComfyUI batch manifest.")
    parser.add_argument("--manifest", required=True, help="JSON, JSONL, or CSV manifest")
    parser.add_argument("--output", help="Output MP4 path. Required unless --dry-run is set")
    parser.add_argument("--row-key", default="auto", help="Dotted path for row axis, e.g. args.steps")
    parser.add_argument("--col-key", default="auto", help="Dotted path for column axis, e.g. args.seed")
    parser.add_argument("--row-label-key", help="Optional dotted path for row labels")
    parser.add_argument("--col-label-key", help="Optional dotted path for column labels")
    parser.add_argument("--video-key", help="Dotted path for video path, e.g. outputs[].file")
    parser.add_argument("--phase", help="Shortcut filter for params.phase=<value>")
    parser.add_argument("--filter", action="append", default=[], help="Exact-match filter KEY=VALUE; repeatable")
    parser.add_argument("--include-failed", action="store_true", help="Do not drop records whose status is not success")
    parser.add_argument("--row-order", help="Comma-separated explicit row order")
    parser.add_argument("--col-order", help="Comma-separated explicit column order")
    parser.add_argument("--style", help="Style JSON path")
    parser.add_argument("--title", help="Matrix title")
    parser.add_argument("--subtitle", help="Matrix subtitle")
    parser.add_argument("--frames", type=int, default=25, help="Frames to sample/render from each clip")
    parser.add_argument("--preview-frame", help="Optional PNG path for frame 1 preview")
    parser.add_argument("--dry-run", action="store_true", help="Print inferred matrix plan without rendering")
    parser.add_argument("--keep-work", action="store_true", help="Keep extracted/intermediate frames")
    parser.add_argument("--allow-missing-videos", action="store_true", help="Render missing videos as dark red cells instead of failing")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manifest = Path(args.manifest).expanduser()
        records = load_manifest(manifest)
        records = successful_records(records, include_failed=args.include_failed)
        filters = [parse_filter(f) for f in args.filter]
        if args.phase:
            filters.append(("params.phase", args.phase))
        records = apply_filters(records, filters)
        row_key = infer_axis_key(records, args.row_key, ("params.input_index", "input_index", "args.row", "row", "index"))
        col_key = infer_axis_key(records, args.col_key, ("params.profile", "profile", "args.seed", "seed", "args.col", "col", "index"))
        plan = build_plan(
            records,
            row_key=row_key,
            col_key=col_key,
            row_label_key=args.row_label_key,
            col_label_key=args.col_label_key,
            row_order=args.row_order,
            col_order=args.col_order,
        )
        if args.dry_run:
            print(json.dumps(make_summary(plan, row_key=row_key, col_key=col_key, video_key=args.video_key), indent=2))
            return 0
        if not args.output:
            raise MatrixError("--output is required unless --dry-run is set.")
        style = load_style(args.style)
        result = render_matrix(
            plan,
            output=Path(args.output).expanduser(),
            manifest=manifest,
            video_key=args.video_key,
            style=style,
            title=args.title,
            subtitle=args.subtitle,
            frames=args.frames,
            keep_work=args.keep_work,
            preview_frame=Path(args.preview_frame).expanduser() if args.preview_frame else None,
            allow_missing_videos=args.allow_missing_videos,
        )
        print(json.dumps(result, indent=2))
        return 0
    except (MatrixError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

