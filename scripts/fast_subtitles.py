#!/usr/bin/env python3
"""Build and exclusively publish a lightweight Simplified Chinese ASS file."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import wave


ALLOWED_RESOLUTIONS = {"direct", "inferred", "unclear"}
ALLOWED_DECISIONS = ALLOWED_RESOLUTIONS | {"omit"}
OMIT_REASONS = {
    "non_speech",
    "asr_hallucination",
    "duplicate_event",
    "unintelligible",
}
CHUNK_SECONDS = 300.0
CHUNK_OVERLAP_SECONDS = 8.0


def configured_file(value: Path | None, variable: str, description: str) -> Path:
    """Resolve an explicit path or its environment fallback without machine-specific defaults."""
    candidate = value if value is not None else os.environ.get(variable)
    if not candidate:
        raise ValueError(f"{description} is required; pass its CLI option or set {variable}")
    path = Path(candidate).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"{description} must be a file: {path}")
    return path


def read_json(path: Path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def read_whisper_json(path: Path) -> tuple[dict, int]:
    payload = path.read_bytes()
    try:
        text = payload.decode("utf-8")
        replacements = 0
    except UnicodeDecodeError:
        text = payload.decode("utf-8", errors="replace")
        replacements = text.count("\ufffd")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("whisper output must be a JSON object")
    return value, replacements


def write_json(path: Path, value) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    atomic_write(path, payload.encode("utf-8"))


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def target_for_source(source: Path) -> Path:
    return source.with_name(f"{source.stem}.zh.ass")


def tool(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise ValueError(f"missing dependency: {name}")
    return executable


def probe_media(video: Path) -> dict:
    result = subprocess.run(
        [
            tool("ffprobe"),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def initialize(
    video: Path,
    jobs_root: Path | None = None,
    *,
    audio_stream: int = 0,
    probe: dict | None = None,
) -> Path:
    video = Path(video).expanduser().resolve(strict=True)
    if video.suffix.lower() not in {".mp4", ".mkv"}:
        raise ValueError("source must be an MP4 or MKV file")
    target = target_for_source(video)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"target already exists: {target}")
    digest = sha256(video)
    jobs_root = (
        Path(jobs_root).expanduser()
        if jobs_root is not None
        else Path.home() / ".ja-zh-subtitles-jobs"
    )
    job = jobs_root / f"{video.stem}-{digest[:12]}"
    manifest_path = job / "manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        check_source(manifest)
        if manifest.get("target") != str(target) or manifest.get("audio_stream") != audio_stream:
            raise ValueError("existing job settings differ; choose a fresh job root")
        return job
    if job.exists() and any(job.iterdir()):
        raise ValueError("job directory is nonempty and has no manifest")

    probe = probe if probe is not None else probe_media(video)
    try:
        duration = float(probe["format"]["duration"])
        audio = [stream for stream in probe["streams"] if stream.get("codec_type") == "audio"]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("ffprobe returned invalid media information") from error
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("media duration must be positive")
    if audio_stream < 0 or audio_stream >= len(audio):
        raise ValueError("selected audio stream does not exist")

    job.mkdir(parents=True, exist_ok=True)
    write_json(
        manifest_path,
        {
            "version": 1,
            "source": {
                "path": str(video),
                "size": video.stat().st_size,
                "sha256": digest,
            },
            "duration": duration,
            "audio_stream": audio_stream,
            "streams": probe["streams"],
            "target": str(target),
        },
    )
    return job


def check_source(manifest: dict) -> Path:
    try:
        source = Path(manifest["source"]["path"])
        expected_size = manifest["source"]["size"]
        expected_hash = manifest["source"]["sha256"]
    except (KeyError, TypeError) as error:
        raise ValueError("invalid manifest source identity") from error
    if not source.is_file():
        raise ValueError("source media is missing")
    if source.stat().st_size != expected_size or sha256(source) != expected_hash:
        raise ValueError("source media changed")
    return source


def run(command: list[str], log: Path) -> None:
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise ValueError(f"command failed ({result.returncode}); inspect {log}")


def extraction_filter() -> str:
    return "loudnorm=I=-16:LRA=11:TP=-1.5,aresample=16000:async=1:first_pts=0,apad"


def extract_audio(job: Path, manifest: dict) -> Path:
    audio = job / "audio.wav"
    receipt_path = job / "audio-receipt.json"
    expected = {
        "source_sha256": manifest["source"]["sha256"],
        "audio_stream": manifest["audio_stream"],
        "ffmpeg_sha256": sha256(Path(tool("ffmpeg"))),
        "audio_filter": extraction_filter(),
    }
    if audio.exists() and receipt_path.exists():
        receipt = read_json(receipt_path)
        if receipt.get("settings") == expected and receipt.get("audio_sha256") == sha256(audio):
            return audio
        raise ValueError("cached audio does not match the source or settings")
    if audio.exists():
        raise ValueError("unreceipted cached audio exists; preserve it and use a fresh job")

    run(
        [
            tool("ffmpeg"),
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-copyts",
            "-start_at_zero",
            "-i",
            manifest["source"]["path"],
            "-map",
            f"0:a:{manifest['audio_stream']}",
            "-vn",
            "-ac",
            "1",
            "-af",
            extraction_filter(),
            "-t",
            str(manifest["duration"]),
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio),
        ],
        job / "extract.log",
    )
    with wave.open(str(audio), "rb") as stream:
        if stream.getnchannels() != 1 or stream.getframerate() != 16000 or stream.getnframes() == 0:
            raise ValueError("extracted audio has an invalid format")
    write_json(
        receipt_path,
        {"settings": expected, "audio_sha256": sha256(audio)},
    )
    return audio


def plan_chunks(
    duration: float,
    chunk_seconds: float = CHUNK_SECONDS,
    overlap_seconds: float = CHUNK_OVERLAP_SECONDS,
) -> list[dict]:
    if not all(math.isfinite(value) and value > 0 for value in (duration, chunk_seconds)):
        raise ValueError("duration and chunk length must be positive")
    if not math.isfinite(overlap_seconds) or not 0 <= overlap_seconds < chunk_seconds:
        raise ValueError("chunk overlap must be nonnegative and shorter than a chunk")
    step = chunk_seconds - overlap_seconds
    chunks: list[dict] = []
    start = 0.0
    while start < duration:
        chunk_duration = min(chunk_seconds, duration - start)
        final = start + chunk_duration >= duration - 1e-6
        chunks.append(
            {
                "index": len(chunks),
                "start": round(start, 3),
                "duration": round(chunk_duration, 3),
                "accept_start": round(start if not chunks else start + overlap_seconds / 2, 3),
                "accept_end": round(
                    duration if final else start + chunk_duration - overlap_seconds / 2,
                    3,
                ),
            }
        )
        if final:
            break
        start += step
    return chunks


def extract_chunk(job: Path, audio: Path, chunk: dict) -> Path:
    directory = job / "chunks"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"chunk-{chunk['index']:04d}.wav"
    if output.exists():
        try:
            with wave.open(str(output), "rb") as stream:
                valid = (
                    stream.getnchannels() == 1
                    and stream.getframerate() == 16000
                    and stream.getnframes() > 0
                )
        except (EOFError, wave.Error):
            valid = False
        if not valid:
            raise ValueError(f"cached chunk audio is invalid: {output}")
        return output
    run(
        [
            tool("ffmpeg"),
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-ss",
            str(chunk["start"]),
            "-t",
            str(chunk["duration"]),
            "-i",
            str(audio),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        directory / f"chunk-{chunk['index']:04d}.extract.log",
    )
    return output


def recognition_command(
    model: Path,
    audio: Path,
    output_prefix: Path,
    *,
    no_gpu: bool = False,
) -> list[str]:
    command = [
        tool("whisper-cli"),
    ]
    if no_gpu:
        command.append("-ng")
    command.extend(
        [
        "-m",
        str(model),
        "-l",
        "ja",
        "-t",
        "4",
        "-bs",
        "5",
        "-bo",
        "1",
        "-mc",
        "0",
        "-sns",
        "-ml",
        "50",
        "-nf",
        "-ojf",
        "-osrt",
        "-np",
        "-of",
        str(output_prefix),
        str(audio),
        ]
    )
    return command


def normalize_whisper(
    raw: dict,
    duration: float,
    *,
    offset: float = 0.0,
    accept_start: float | None = None,
    accept_end: float | None = None,
    allow_empty: bool = False,
) -> list[dict]:
    transcription = raw.get("transcription")
    if not isinstance(transcription, list):
        raise ValueError("whisper output is missing the transcription array")
    normalized: list[dict] = []
    for segment in transcription:
        try:
            text = segment["text"].strip()
            start = segment["offsets"]["from"] / 1000 + offset
            end = segment["offsets"]["to"] / 1000 + offset
        except (AttributeError, KeyError, TypeError) as error:
            raise ValueError("invalid whisper segment") from error
        midpoint = (start + end) / 2
        if not text or end <= start:
            continue
        if accept_start is not None and midpoint < accept_start:
            continue
        if accept_end is not None and midpoint >= accept_end:
            continue
        if not all(math.isfinite(value) for value in (start, end)) or not 0 <= start < end:
            raise ValueError("whisper segment is outside the source timeline")
        if end > duration:
            # accept_end is rounded to milliseconds, so allow a sub-millisecond
            # tolerance; otherwise a duration that rounds down (e.g. ...5575 ->
            # ...557) makes the clip-to-duration path unreachable.
            if accept_end is None or accept_end + 1e-3 < duration:
                raise ValueError("whisper segment is outside the source timeline")
            end = duration
        probabilities = [
            max(1e-9, token["p"])
            for token in segment.get("tokens", [])
            if token.get("id", 50257) < 50257 and isinstance(token.get("p"), (int, float))
        ]
        confidence = (
            math.exp(sum(math.log(value) for value in probabilities) / len(probabilities))
            if probabilities
            else 0.0
        )
        normalized.append(
            {
                "id": f"c{len(normalized) + 1:04d}",
                "start": round(start, 3),
                "end": round(end, 3),
                "ja": text,
                "asr_confidence": round(confidence, 4),
            }
        )
    if not normalized and not allow_empty:
        raise ValueError("recognition produced no timed text")
    return normalized


def transcribe(
    job: Path,
    model: Path | None = None,
    *,
    no_gpu: bool = False,
) -> Path:
    job = Path(job).expanduser().resolve(strict=True)
    manifest = read_json(job / "manifest.json")
    check_source(manifest)
    target = Path(manifest["target"])
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"target already exists: {target}")
    model = configured_file(model, "JA_ZH_SUBTITLES_MODEL", "large-v3 model")
    audio = extract_audio(job, manifest)
    transcript = job / "transcript.json"
    receipt_path = job / "transcript-receipt.json"
    chunks = plan_chunks(manifest["duration"])
    engine = Path(tool("whisper-cli"))
    settings = {
        "source_sha256": manifest["source"]["sha256"],
        "audio_sha256": sha256(audio),
        "model_sha256": sha256(model),
        "engine_sha256": sha256(engine),
        "audio_filter": extraction_filter(),
        "chunk_seconds": CHUNK_SECONDS,
        "overlap_seconds": CHUNK_OVERLAP_SECONDS,
        "vad": False,
        "prefer_gpu": not no_gpu,
    }
    if transcript.exists() and receipt_path.exists():
        receipt = read_json(receipt_path)
        if (
            receipt.get("settings") == settings
            and receipt.get("transcript_sha256") == sha256(transcript)
        ):
            return transcript
        raise ValueError("cached transcript does not match the source or recognition settings")
    if transcript.exists() or receipt_path.exists():
        raise ValueError("unreceipted recognition output exists; preserve it and use a fresh job")

    normalized: list[dict] = []
    chunk_reports: list[dict] = []
    chunks_directory = job / "chunks"
    chunks_directory.mkdir(parents=True, exist_ok=True)
    for chunk in chunks:
        prefix = chunks_directory / f"chunk-{chunk['index']:04d}"
        raw_json = prefix.with_suffix(".json")
        chunk_receipt_path = prefix.with_suffix(".receipt.json")
        command_settings = {
            **settings,
            "chunk": chunk,
        }
        if raw_json.exists() and chunk_receipt_path.exists():
            chunk_receipt = read_json(chunk_receipt_path)
            if (
                chunk_receipt.get("settings") != command_settings
                or chunk_receipt.get("raw_sha256") != sha256(raw_json)
            ):
                raise ValueError(f"cached recognition chunk does not match settings: {chunk['index']}")
        else:
            if raw_json.exists() or chunk_receipt_path.exists():
                raise ValueError(
                    f"unreceipted recognition chunk exists; preserve it and use a fresh job: {chunk['index']}"
                )
            chunk_audio = extract_chunk(job, audio, chunk)
            command = recognition_command(model, chunk_audio, prefix, no_gpu=no_gpu)
            started = time.monotonic()
            try:
                run(command, prefix.with_suffix(".recognize.log"))
            except ValueError as error:
                if no_gpu or "command failed (-6)" not in str(error):
                    raise
                command = recognition_command(model, chunk_audio, prefix, no_gpu=True)
                run(command, prefix.with_suffix(".recognize-cpu.log"))
            write_json(
                chunk_receipt_path,
                {
                    "settings": command_settings,
                    "command": command,
                    "raw_sha256": sha256(raw_json),
                    "recognition_wall_seconds": round(time.monotonic() - started, 3),
                },
            )
            chunk_receipt = read_json(chunk_receipt_path)
        raw_value, replacements = read_whisper_json(raw_json)
        if chunk_receipt.get("utf8_replacements") != replacements:
            chunk_receipt["utf8_replacements"] = replacements
            write_json(chunk_receipt_path, chunk_receipt)
        chunk_segments = normalize_whisper(
            raw_value,
            manifest["duration"],
            offset=chunk["start"],
            accept_start=chunk["accept_start"],
            accept_end=chunk["accept_end"],
            allow_empty=True,
        )
        normalized.extend(chunk_segments)
        chunk_reports.append(
            {
                **chunk,
                "raw": str(raw_json),
                "raw_sha256": chunk_receipt["raw_sha256"],
                "utf8_replacements": replacements,
                "recognition_wall_seconds": chunk_receipt.get("recognition_wall_seconds"),
                "accepted_segments": len(chunk_segments),
            }
        )

    normalized.sort(key=lambda row: (row["start"], row["end"]))
    if not normalized:
        raise ValueError("recognition produced no timed text")
    for index, segment in enumerate(normalized, 1):
        segment["id"] = f"c{index:04d}"
    write_json(transcript, normalized)
    write_json(job / "transcript.raw-index.json", {"chunks": chunk_reports})
    write_json(
        receipt_path,
        {
            "settings": settings,
            "transcript_sha256": sha256(transcript),
            "segments": len(normalized),
            "source_audio_seconds": manifest["duration"],
            "recognized_audio_seconds": round(sum(chunk["duration"] for chunk in chunks), 3),
            "overlap_audio_seconds": round(
                sum(chunk["duration"] for chunk in chunks) - manifest["duration"], 3
            ),
            "chunks": len(chunks),
            "recognition_wall_seconds": round(
                sum(report["recognition_wall_seconds"] for report in chunk_reports), 3
            ) if all(isinstance(report["recognition_wall_seconds"], (int, float)) for report in chunk_reports) else None,
        },
    )
    return transcript


def timestamp(seconds: float) -> str:
    centiseconds = round(seconds * 100)
    hour, rest = divmod(centiseconds, 360000)
    minute, rest = divmod(rest, 6000)
    second, fraction = divmod(rest, 100)
    return f"{hour}:{minute:02d}:{second:02d}.{fraction:02d}"


def display_text(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")


def ass_text(text: str) -> str:
    safe = display_text(text)
    lines = [line.strip() for line in safe.splitlines() if line.strip()]
    return r"\N".join(lines)


def check_glyphs(text: str, cmap: dict) -> None:
    missing = sorted({ord(character) for character in text if not character.isspace() and ord(character) not in cmap})
    if missing:
        raise ValueError(
            "font missing glyph: " + ", ".join(f"U+{codepoint:04X}" for codepoint in missing)
        )


def encode_font(data: bytes) -> str:
    encoded: list[str] = []
    for offset in range(0, len(data), 3):
        group = data[offset : offset + 3]
        first, second, third = group.ljust(3, b"\0")
        values = [
            first >> 2,
            ((first & 3) << 4) | (second >> 4),
            ((second & 15) << 2) | (third >> 6),
            third & 63,
        ]
        encoded.extend(chr(value + 33) for value in values[: len(group) + 1])
    payload = "".join(encoded)
    return "\n".join(payload[index : index + 80] for index in range(0, len(payload), 80))


def decode_font(encoded: str) -> bytes:
    payload = "".join(encoded.splitlines())
    if len(payload) % 4 == 1 or any(not 33 <= ord(character) <= 96 for character in payload):
        raise ValueError("invalid ASS font attachment")
    result = bytearray()
    for offset in range(0, len(payload), 4):
        values = [ord(character) - 33 for character in payload[offset : offset + 4]]
        if len(values) > 1:
            result.append((values[0] << 2) | (values[1] >> 4))
        if len(values) > 2:
            result.append(((values[1] & 15) << 4) | (values[2] >> 2))
        if len(values) > 3:
            result.append(((values[2] & 3) << 6) | values[3])
    return bytes(result)


def subset_font(path: Path, text: str) -> tuple[str, bytes]:
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
    except ImportError as error:
        raise ValueError("fontTools missing; install this repository's Python dependency") from error
    with TTFont(path, recalcTimestamp=False) as font:
        check_glyphs(text, font.getBestCmap())
        family = font["name"].getDebugName(16) or font["name"].getDebugName(1)
        if not family or any(character in family for character in ",\r\n"):
            raise ValueError("font family is not safe for an ASS style")
        options = subset.Options()
        options.layout_features = ["*"]
        options.name_IDs = ["*"]
        options.name_legacy = True
        options.glyph_names = True
        options.notdef_glyph = True
        options.notdef_outline = True
        options.recommended_glyphs = True
        worker = subset.Subsetter(options=options)
        worker.populate(text=text)
        worker.subset(font)
        check_glyphs(text, font.getBestCmap())
        buffer = io.BytesIO()
        font.save(buffer)
    return family, buffer.getvalue()


def validate_cues(cues, duration: float) -> tuple[list[dict], dict]:
    if not isinstance(cues, list) or not cues:
        raise ValueError("translation must contain a nonempty cues array")
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("manifest duration must be positive")

    seen: set[str] = set()
    previous_start = -1.0
    overlaps: list[str] = []
    long_or_fast: list[str] = []
    inferred = 0
    unclear = 0
    previous_end = 0.0

    for cue in cues:
        if not isinstance(cue, dict):
            raise ValueError("each cue must be an object")
        cue_id = cue.get("id")
        if not isinstance(cue_id, str) or not cue_id.strip() or cue_id in seen:
            raise ValueError("cue ids must be nonempty and unique")
        seen.add(cue_id)

        start, end = cue.get("start"), cue.get("end")
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (start, end)):
            raise ValueError(f"invalid cue interval: {cue_id}")
        if not 0 <= start < end <= duration:
            raise ValueError(f"cue interval is outside the source timeline: {cue_id}")
        if start < previous_start:
            raise ValueError("cues must be ordered by start time")
        if start < previous_end:
            overlaps.append(cue_id)
        previous_start, previous_end = start, max(previous_end, end)

        if not isinstance(cue.get("ja"), str):
            raise ValueError(f"cue ja must be a string: {cue_id}")
        zh = cue.get("zh")
        if not isinstance(zh, str) or not zh.strip():
            raise ValueError(f"cue zh must be nonempty: {cue_id}")
        resolution = cue.get("resolution")
        if resolution not in ALLOWED_RESOLUTIONS:
            raise ValueError(f"unknown cue resolution: {cue_id}")
        note = cue.get("note", "")
        if not isinstance(note, str):
            raise ValueError(f"cue note must be a string: {cue_id}")
        if resolution == "inferred":
            inferred += 1
            if not note.strip():
                raise ValueError(f"inferred cue requires a note: {cue_id}")
        if resolution == "unclear":
            unclear += 1
            if not note.strip():
                raise ValueError(f"unclear cue requires a note: {cue_id}")

        span = end - start
        if span > 8 or len(zh.strip()) / span > 20:
            long_or_fast.append(cue_id)

    return cues, {
        "events": len(cues),
        "inferred": inferred,
        "unclear": unclear,
        "overlaps": overlaps,
        "long_or_fast": long_or_fast,
    }


def transcript_by_id(transcript) -> dict[str, dict]:
    if not isinstance(transcript, list) or not transcript:
        raise ValueError("transcript must be a nonempty array")
    source: dict[str, dict] = {}
    for row in transcript:
        if not isinstance(row, dict):
            raise ValueError("each transcript row must be an object")
        cue_id = row.get("id")
        if not isinstance(cue_id, str) or not cue_id.strip() or cue_id in source:
            raise ValueError("transcript ids must be unique and nonempty")
        source[cue_id] = row
    return source


def summarize_ids(ids: list[str], limit: int = 20) -> str:
    shown = ", ".join(ids[:limit])
    if len(ids) > limit:
        shown += f", ... ({len(ids)} total)"
    return shown


def omit_reason(note: str, cue_id: str) -> str:
    if not isinstance(note, str) or not note.strip():
        raise ValueError(f"omit decision requires a reason: {cue_id}")
    reason = note.split(":", 1)[0].strip()
    if reason not in OMIT_REASONS:
        allowed = ", ".join(sorted(OMIT_REASONS))
        raise ValueError(f"unknown omit reason for {cue_id}: {reason}; use one of {allowed}")
    return reason


def validate_decision_coverage(transcript, translation) -> dict:
    source = transcript_by_id(transcript)
    if not isinstance(translation, dict):
        raise ValueError("translation must be an object")
    cues = translation.get("cues")
    omissions = translation.get("omissions", [])
    if not isinstance(cues, list):
        raise ValueError("translation cues must be an array")
    if not isinstance(omissions, list):
        raise ValueError("translation omissions must be an array")

    seen: set[str] = set()
    counts = {"direct": 0, "inferred": 0, "unclear": 0}
    reason_counts: dict[str, int] = {}
    for decision in [*cues, *omissions]:
        if not isinstance(decision, dict):
            raise ValueError("each translation decision must be an object")
        cue_id = decision.get("id")
        if not isinstance(cue_id, str) or not cue_id.strip():
            raise ValueError("translation decision id must be nonempty")
        if cue_id in seen:
            raise ValueError(f"duplicate translation decision id: {cue_id}")
        if cue_id not in source:
            raise ValueError(f"unknown transcript id in translation decisions: {cue_id}")
        seen.add(cue_id)
        original = source[cue_id]
        for field in ("start", "end", "ja"):
            if decision.get(field) != original.get(field):
                raise ValueError(f"translation decision changed transcript {field}: {cue_id}")

    for cue in cues:
        resolution = cue.get("resolution")
        if resolution not in ALLOWED_RESOLUTIONS:
            raise ValueError(f"unknown cue resolution: {cue.get('id')}")
        counts[resolution] += 1

    for omission in omissions:
        cue_id = omission.get("id")
        if omission.get("resolution") != "omit":
            raise ValueError(f"omission resolution must be omit: {cue_id}")
        if isinstance(omission.get("zh"), str) and omission["zh"].strip():
            raise ValueError(f"omit decision must not contain Chinese text: {cue_id}")
        reason = omission.get("reason")
        parsed_reason = omit_reason(omission.get("note", ""), cue_id)
        if reason != parsed_reason:
            raise ValueError(f"omit reason does not match note prefix: {cue_id}")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    missing = [cue_id for cue_id in source if cue_id not in seen]
    if missing:
        raise ValueError(
            "translation decisions incomplete; missing ids: " + summarize_ids(missing)
        )
    return {
        "transcript_candidates": len(source),
        "decisions": len(seen),
        "direct": counts["direct"],
        "inferred": counts["inferred"],
        "unclear": counts["unclear"],
        "omitted": len(omissions),
        "pending": 0,
        "missing_ids": [],
        "omit_reasons": reason_counts,
    }


def adjust_long_timings(cues: list[dict]) -> tuple[list[dict], list[str]]:
    adjusted: list[dict] = []
    changed: list[str] = []
    for cue in cues:
        rendered = dict(cue)
        span = cue["end"] - cue["start"]
        if span > 8:
            display = min(6.0, max(1.5, 1.2 + len(cue["zh"].strip()) * 0.25))
            rendered["start"] = round(cue["end"] - display, 3)
            changed.append(cue["id"])
        adjusted.append(rendered)
    return adjusted, changed


def assemble(job: Path, rows_path: Path, context_path: Path) -> Path:
    job = Path(job).expanduser().resolve(strict=True)
    transcript = read_json(job / "transcript.json")
    source = transcript_by_id(transcript)

    translations: dict[str, dict] = {}
    for number, line in enumerate(Path(rows_path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        columns = line.split("\t")
        if len(columns) != 4:
            raise ValueError(f"translation row {number} must have four tab-separated columns")
        cue_id, resolution, zh, note = columns
        if cue_id not in source:
            raise ValueError(f"unknown transcript id in translation row {number}: {cue_id}")
        if cue_id in translations:
            raise ValueError(f"duplicate transcript id in translation rows: {cue_id}")
        if resolution not in ALLOWED_DECISIONS:
            raise ValueError(f"unknown translation decision in row {number}: {resolution}")
        if resolution == "omit":
            if zh.strip():
                raise ValueError(f"omit decision must have empty zh in row {number}: {cue_id}")
            reason = omit_reason(note, cue_id)
            translations[cue_id] = {
                "resolution": resolution,
                "reason": reason,
                "note": note,
            }
        else:
            if not zh.strip():
                raise ValueError(f"translation zh must be nonempty in row {number}: {cue_id}")
            if resolution in {"inferred", "unclear"} and not note.strip():
                raise ValueError(f"{resolution} decision requires a note: {cue_id}")
            translations[cue_id] = {"resolution": resolution, "zh": zh, "note": note}
    if not translations:
        raise ValueError("translation rows are empty")

    missing = [cue_id for cue_id in source if cue_id not in translations]
    if missing:
        raise ValueError(
            "translation decisions incomplete; missing ids: " + summarize_ids(missing)
        )

    cues = []
    omissions = []
    for original in transcript:
        translated = translations.get(original["id"])
        decision = {
            "id": original["id"],
            "start": original["start"],
            "end": original["end"],
            "ja": original["ja"],
            **translated,
        }
        if translated["resolution"] == "omit":
            omissions.append(decision)
        else:
            cues.append(decision)
    context = read_json(Path(context_path))
    if not isinstance(context, dict):
        raise ValueError("translation context must be an object")
    translation = {"context": context, "cues": cues, "omissions": omissions}
    translation["coverage"] = validate_decision_coverage(transcript, translation)
    output = job / "translation.json"
    write_json(output, translation)
    return output


def render_ass(title: str, cues: list[dict], font_family: str, font_payload: str) -> str:
    header = f"""[Script Info]
Title: {title} Simplified Chinese
; Fast local Japanese transcription with contextual AI translation.
; Inferred dialogue is recorded in the job translation JSON.
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Chinese,{font_family},52,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,3,1,2,60,60,54,1

[Fonts]
fontname: subtitle-subset.otf
{font_payload}

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    events = "".join(
        "Dialogue: 0,"
        f"{timestamp(cue['start'])},{timestamp(cue['end'])},"
        f"Chinese,,0,0,0,,{ass_text(cue['zh'].strip())}\n"
        for cue in cues
    )
    return header + events


def build(job: Path, font: Path | None = None) -> Path:
    job = Path(job).expanduser().resolve(strict=True)
    manifest_path = job / "manifest.json"
    translation_path = job / "translation.json"
    manifest = read_json(manifest_path)
    source = check_source(manifest)
    translation = read_json(translation_path)
    transcript_path = job / "transcript.json"
    transcript = read_json(transcript_path)
    coverage = validate_decision_coverage(transcript, translation)
    cues, report = validate_cues(translation.get("cues"), manifest.get("duration"))
    rendered_cues, timing_adjusted = adjust_long_timings(cues)
    font = configured_file(font, "JA_ZH_SUBTITLES_FONT", "CJK font")
    font_family, font_bytes = subset_font(
        font, "".join(display_text(cue["zh"]) for cue in rendered_cues)
    )
    font_payload = encode_font(font_bytes)
    if decode_font(font_payload) != font_bytes:
        raise ValueError("font attachment roundtrip mismatch")
    output = job / "subtitle.zh.ass"
    atomic_write(
        output,
        render_ass(source.stem, rendered_cues, font_family, font_payload).encode("utf-8"),
    )
    report.update(coverage)
    report["timing_adjusted"] = timing_adjusted
    report.update(
        {
            "missing_font_glyphs": 0,
            "font_attachment_roundtrip": True,
            "font_attachment_bytes": len(font_bytes),
            "font_family": font_family,
            "font_sha256": sha256(font),
            "source_sha256": manifest["source"]["sha256"],
            "transcript_sha256": sha256(transcript_path),
            "translation_sha256": sha256(translation_path),
            "subtitle_sha256": sha256(output),
        }
    )
    write_json(job / "validation.json", report)
    return output


def publish(job: Path) -> Path:
    job = Path(job).expanduser().resolve(strict=True)
    manifest = read_json(job / "manifest.json")
    check_source(manifest)
    translation = job / "translation.json"
    subtitle = job / "subtitle.zh.ass"
    validation = read_json(job / "validation.json")
    transcript = job / "transcript.json"
    if validation.get("transcript_sha256") != sha256(transcript):
        raise ValueError("transcript changed after build")
    if validation.get("translation_sha256") != sha256(translation):
        raise ValueError("translation changed after build")
    if validation.get("subtitle_sha256") != sha256(subtitle):
        raise ValueError("built subtitle changed after validation")
    target = Path(manifest.get("target", ""))
    if not target.is_absolute():
        raise ValueError("manifest target must be an absolute path")

    descriptor = None
    created = False
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        created = True
        with subtitle.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
            descriptor = None
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
    except FileExistsError as error:
        raise FileExistsError(f"target already exists: {target}") from error
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            target.unlink()
        raise
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    initialize_command = commands.add_parser("init")
    initialize_command.add_argument("video", type=Path)
    initialize_command.add_argument("--jobs-root", type=Path)
    initialize_command.add_argument("--audio-stream", type=int, default=0)
    transcribe_command = commands.add_parser("transcribe")
    transcribe_command.add_argument("job", type=Path)
    transcribe_command.add_argument("--model", type=Path)
    transcribe_command.add_argument("--no-gpu", action="store_true")
    assemble_command = commands.add_parser("assemble")
    assemble_command.add_argument("job", type=Path)
    assemble_command.add_argument("rows", type=Path)
    assemble_command.add_argument("--context", type=Path, required=True)
    build_command = commands.add_parser("build")
    build_command.add_argument("job", type=Path)
    build_command.add_argument("--font", type=Path)
    publish_command = commands.add_parser("publish")
    publish_command.add_argument("job", type=Path)
    options = parser.parse_args()
    if options.action == "init":
        result = initialize(options.video, options.jobs_root, audio_stream=options.audio_stream)
    elif options.action == "transcribe":
        result = transcribe(options.job, options.model, no_gpu=options.no_gpu)
    elif options.action == "assemble":
        result = assemble(options.job, options.rows, options.context)
    elif options.action == "build":
        result = build(options.job, options.font)
    else:
        result = publish(options.job)
    print(result)


if __name__ == "__main__":
    try:
        main()
    except (FileExistsError, KeyError, OSError, TypeError, ValueError) as error:
        raise SystemExit(str(error)) from error
