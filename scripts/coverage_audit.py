#!/usr/bin/env python3
"""Read-only audit of a finished job.

Reports two failure classes that the delivery pass cannot detect by itself:

1. suspicious recognition segments — whole-30s spans (whole-window
   hallucinations) or segments whose start sits exactly on a 30s decode-window
   boundary (pseudo onsets).  Whisper decodes in 30s windows; when a window is
   dominated by digital silence or background music the no-fallback greedy
   decode of this recipe can collapse it into one hallucinated segment and give
   the next window's first segment a window-boundary start.
2. coverage holes in the rendered timeline — places where build's long-span
   tightening (spans over 8s keep only their last 1.5-6s) may hide speech that
   the recognizer merged into one long span. Other gaps have not been verified
   by this read-only audit and must not be assumed to be intentional.

Nothing here re-runs recognition and nothing is written to the job.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

WINDOW_SECONDS = 30.0
SPAN_THRESHOLD = 8.0
GAP_THRESHOLD = 1.5


def read_json(path: Path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def rendered_interval(cue: dict, adjusted: set[str]) -> tuple[float, float]:
    start, end = float(cue["start"]), float(cue["end"])
    if cue["id"] in adjusted:
        display = min(6.0, max(1.5, 1.2 + len(cue.get("zh", "").strip()) * 0.25))
        start = end - display
    return start, end


def merge(intervals: list[tuple[float, float]]) -> list[list[float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def timestamp(seconds: float) -> str:
    centiseconds = round(seconds * 100)
    hours, rest = divmod(centiseconds, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, centis = divmod(rest, 100)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{centis:02d}"


def audit(job: Path, gap_threshold: float) -> int:
    transcript = read_json(job / "transcript.json")
    translation = read_json(job / "translation.json")
    validation_path = job / "validation.json"
    validation = read_json(validation_path) if validation_path.exists() else {}
    adjusted = set(validation.get("timing_adjusted", []))
    source = {cue["id"]: cue for cue in transcript}

    print(f"job: {job}")
    print(f"transcript candidates: {len(transcript)}  cues: {len(translation.get('cues', []))}  "
          f"omissions: {len(translation.get('omissions', []))}  timing_adjusted: {len(adjusted)}")

    print("\n[1] suspicious segments (whole-window span, or start on a 30s window boundary)")
    suspicious = 0
    for cue in transcript:
        span = float(cue["end"]) - float(cue["start"])
        start = float(cue["start"])
        on_boundary = start > 0 and math.isclose(start % WINDOW_SECONDS, 0.0, abs_tol=1e-6)
        whole_window = math.isclose(span, WINDOW_SECONDS, abs_tol=1e-6)
        if not (on_boundary or span >= SPAN_THRESHOLD):
            continue
        suspicious += 1
        decision = next((c for c in translation.get("cues", []) if c["id"] == cue["id"]), None)
        kind = "whole-30s-span" if whole_window else ("window-boundary-start" if on_boundary else "long-span")
        state = "cue" if decision else "omitted"
        print(f"  {cue['id']} {cue['start']:9.2f}-{cue['end']:9.2f} span={span:6.2f} "
              f"{kind:22s} {state:7s} {cue['ja'][:36]}")
    if not suspicious:
        print("  none")

    print(f"\n[2] coverage holes over {gap_threshold:.1f}s in the rendered timeline")
    merged = merge([rendered_interval(cue, adjusted) for cue in translation.get("cues", [])])
    uncovered = 0
    other_gaps = 0
    for index in range(1, len(merged)):
        gap_start, gap_end = merged[index - 1][1], merged[index][0]
        if gap_end - gap_start <= gap_threshold:
            continue
        following = [
            cue for cue in translation.get("cues", [])
            if math.isclose(rendered_interval(cue, adjusted)[0], gap_end, abs_tol=0.01)
        ]
        tightening = bool(
            following
            and float(source[following[0]["id"]]["start"]) < gap_end - 1.0
            and float(source[following[0]["id"]]["end"]) - float(source[following[0]["id"]]["start"]) > SPAN_THRESHOLD
        )
        label = "tightening-hole (review needed)" if tightening else "no-cue (unverified gap)"
        if tightening:
            uncovered += 1
        else:
            other_gaps += 1
        print(f"  {timestamp(gap_start)}-{timestamp(gap_end)} {gap_end - gap_start:7.2f}s  {label:34s} "
              f"{following[0]['id'] if following else '-'}")
    print(f"\ntightening holes: {uncovered}   other unverified gaps: {other_gaps}")
    print("Re-check only the time points the user points at (see workflow.md); never re-run the film.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path)
    parser.add_argument("--gap-threshold", type=float, default=GAP_THRESHOLD)
    options = parser.parse_args()
    raise SystemExit(audit(options.job.expanduser().resolve(strict=True), options.gap_threshold))


if __name__ == "__main__":
    main()
