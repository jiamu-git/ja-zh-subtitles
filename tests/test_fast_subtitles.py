import hashlib
import importlib.util
import io
import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "fast_subtitles.py"
SPEC = importlib.util.spec_from_file_location("fast_subtitles", SCRIPT)
fast_subtitles = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(fast_subtitles)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class FastSubtitlesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.video = self.root / "movie.mp4"
        self.video.write_bytes(b"unchanged-video")
        self.job = self.root / "job"
        self.job.mkdir()
        self.font = self.root / "fixture.ttf"
        self.font.write_bytes(b"test-font")
        self.font_setting = mock.patch.dict(os.environ, {"JA_ZH_SUBTITLES_FONT": str(self.font)})
        self.font_setting.start()
        self.addCleanup(self.font_setting.stop)
        self.font_subset = mock.patch.object(
            fast_subtitles, "subset_font", return_value=("Fixture CJK", b"test-font")
        )
        self.font_subset.start()
        self.addCleanup(self.font_subset.stop)
        self.manifest = {
            "source": {
                "path": str(self.video),
                "size": self.video.stat().st_size,
                "sha256": sha256(self.video),
            },
            "duration": 20.0,
            "target": str(self.root / "movie.zh.ass"),
        }
        (self.job / "manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def write_translation(self, cues, omissions=None):
        omissions = omissions or []
        transcript = [
            {
                "id": decision["id"],
                "start": decision["start"],
                "end": decision["end"],
                "ja": decision["ja"],
            }
            for decision in [*cues, *omissions]
        ]
        transcript.sort(key=lambda row: (row["start"], row["end"], row["id"]))
        (self.job / "transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
        )
        (self.job / "translation.json").write_text(
            json.dumps({"cues": cues, "omissions": omissions}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_build_accepts_direct_inferred_and_unclear_cues(self):
        self.write_translation(
            [
                {
                    "id": "c0001",
                    "start": 1.0,
                    "end": 2.5,
                    "ja": "こんにちは",
                    "zh": "你好。",
                    "resolution": "direct",
                    "note": "",
                },
                {
                    "id": "c0002",
                    "start": 3.0,
                    "end": 4.5,
                    "ja": "…",
                    "zh": "别停。",
                    "resolution": "inferred",
                    "note": "由前后连续请求语气保守推断",
                },
                {
                    "id": "c0003",
                    "start": 5.0,
                    "end": 6.0,
                    "ja": "",
                    "zh": "（低声说话，内容不清）",
                    "resolution": "unclear",
                    "note": "只能确认存在语言",
                },
            ]
        )

        output = fast_subtitles.build(self.job)

        text = output.read_text(encoding="utf-8")
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:02.50,Chinese", text)
        self.assertIn("你好。", text)
        self.assertEqual(self.video.read_bytes(), b"unchanged-video")
        report = json.loads((self.job / "validation.json").read_text())
        self.assertEqual(report["events"], 3)
        self.assertEqual(report["inferred"], 1)
        self.assertEqual(report["unclear"], 1)

    def test_build_embeds_a_font_subset_covering_the_chinese_text(self):
        if importlib.util.find_spec("fontTools") is None:
            self.skipTest("fontTools is required for the font integration test")
        from fontTools.fontBuilder import FontBuilder
        from fontTools.pens.ttGlyphPen import TTGlyphPen
        from fontTools.ttLib import TTFont

        self.font_subset.stop()
        chars = "你好，别停。＼｛｝"
        glyph_names = {ord(char): f"uni{ord(char):04X}" for char in chars}
        glyph_order = [".notdef", *glyph_names.values()]
        builder = FontBuilder(1000, isTTF=True)
        builder.setupGlyphOrder(glyph_order)
        builder.setupCharacterMap(glyph_names)
        glyphs = {}
        for name in glyph_order:
            pen = TTGlyphPen(None)
            if name != ".notdef":
                pen.moveTo((50, 0))
                pen.lineTo((550, 0))
                pen.lineTo((550, 700))
                pen.lineTo((50, 700))
                pen.closePath()
            glyphs[name] = pen.glyph()
        builder.setupGlyf(glyphs)
        builder.setupHorizontalMetrics({name: (600, 0) for name in glyph_order})
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupNameTable({"familyName": "Fixture CJK", "styleName": "Regular"})
        builder.setupOS2(sTypoAscender=800, sTypoDescender=-200,
                         usWinAscent=800, usWinDescent=200)
        builder.setupPost()
        builder.setupMaxp()
        builder.save(self.font)

        self.write_translation(
            [
                {
                    "id": "c0001",
                    "start": 1.0,
                    "end": 2.5,
                    "ja": "こんにちは",
                    "zh": "你好，别停。\\{}",
                    "resolution": "direct",
                    "note": "",
                }
            ]
        )

        output = fast_subtitles.build(self.job)

        text = output.read_text(encoding="utf-8")
        self.assertIn("Style: Chinese,Fixture CJK,", text)
        self.assertIn("\n[Fonts]\nfontname: subtitle-subset.otf\n", text)
        payload = text.split("fontname: subtitle-subset.otf\n", 1)[1].split(
            "\n\n[Events]", 1
        )[0]
        font_bytes = fast_subtitles.decode_font(payload)
        with TTFont(io.BytesIO(font_bytes)) as font:
            cmap = font.getBestCmap()
        self.assertTrue(all(ord(character) in cmap for character in chars))
        report = json.loads((self.job / "validation.json").read_text())
        self.assertTrue(report["font_attachment_roundtrip"])
        self.assertEqual(report["missing_font_glyphs"], 0)

    def test_assemble_joins_compact_translation_rows_to_the_transcript(self):
        transcript = [
            {"id": "c0001", "start": 1.0, "end": 2.0, "ja": "はい", "asr_confidence": 0.9},
            {"id": "c0002", "start": 2.0, "end": 3.0, "ja": "…", "asr_confidence": 0.2},
        ]
        (self.job / "transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
        )
        rows = self.root / "translations.tsv"
        rows.write_text(
            "c0001\tdirect\t好。\t\n"
            "c0002\tunclear\t（低声说话，内容不清）\t只能确认存在语言\n",
            encoding="utf-8",
        )
        context = self.root / "context.json"
        context.write_text(
            json.dumps({"content_type": "Japanese TV episode", "plot": "测试剧情"}),
            encoding="utf-8",
        )

        output = fast_subtitles.assemble(self.job, rows, context)

        value = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(value["context"]["plot"], "测试剧情")
        self.assertEqual(value["cues"][0]["ja"], "はい")
        self.assertEqual(value["cues"][0]["zh"], "好。")
        self.assertEqual(value["cues"][1]["resolution"], "unclear")

    def test_assemble_rejects_a_missing_transcript_decision(self):
        transcript = [
            {"id": "c0001", "start": 1.0, "end": 2.0, "ja": "はい"},
            {"id": "c0002", "start": 2.0, "end": 3.0, "ja": "待って"},
        ]
        (self.job / "transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
        )
        rows = self.root / "translations.tsv"
        rows.write_text("c0001\tdirect\t好。\t\n", encoding="utf-8")
        context = self.root / "context.json"
        context.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(
            ValueError, "translation decisions incomplete.*c0002"
        ):
            fast_subtitles.assemble(self.job, rows, context)

    def test_assemble_records_an_explicit_omission_and_complete_coverage(self):
        transcript = [
            {"id": "c0001", "start": 1.0, "end": 2.0, "ja": "はい"},
            {"id": "c0002", "start": 2.0, "end": 3.0, "ja": "ああ"},
        ]
        (self.job / "transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
        )
        rows = self.root / "translations.tsv"
        rows.write_text(
            "c0001\tdirect\t好。\t\n"
            "c0002\tomit\t\tnon_speech: 只有喘息，没有可确认的语言\n",
            encoding="utf-8",
        )
        context = self.root / "context.json"
        context.write_text("{}", encoding="utf-8")

        output = fast_subtitles.assemble(self.job, rows, context)

        value = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual([cue["id"] for cue in value["cues"]], ["c0001"])
        self.assertEqual(value["omissions"][0]["id"], "c0002")
        self.assertEqual(value["omissions"][0]["reason"], "non_speech")
        self.assertEqual(
            value["coverage"],
            {
                "transcript_candidates": 2,
                "decisions": 2,
                "direct": 1,
                "inferred": 0,
                "unclear": 0,
                "omitted": 1,
                "pending": 0,
                "missing_ids": [],
                "omit_reasons": {"non_speech": 1},
            },
        )

    def test_assemble_rejects_an_omit_without_a_known_reason(self):
        transcript = [
            {"id": "c0001", "start": 1.0, "end": 2.0, "ja": "ああ"},
        ]
        (self.job / "transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
        )
        rows = self.root / "translations.tsv"
        rows.write_text("c0001\tomit\t\t跳过\n", encoding="utf-8")
        context = self.root / "context.json"
        context.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "unknown omit reason"):
            fast_subtitles.assemble(self.job, rows, context)

    def test_build_rejects_translation_json_that_bypasses_coverage(self):
        transcript = [
            {"id": "c0001", "start": 1.0, "end": 2.0, "ja": "はい"},
            {"id": "c0002", "start": 2.0, "end": 3.0, "ja": "待って"},
        ]
        (self.job / "transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
        )
        (self.job / "translation.json").write_text(
            json.dumps(
                {
                    "cues": [
                        {
                            "id": "c0001",
                            "start": 1.0,
                            "end": 2.0,
                            "ja": "はい",
                            "zh": "好。",
                            "resolution": "direct",
                            "note": "",
                        }
                    ],
                    "omissions": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValueError, "translation decisions incomplete.*c0002"
        ):
            fast_subtitles.build(self.job)

    def test_build_reports_complete_decision_coverage(self):
        cue = {
            "id": "c0001",
            "start": 1.0,
            "end": 2.0,
            "ja": "はい",
            "zh": "好。",
            "resolution": "direct",
            "note": "",
        }
        omission = {
            "id": "c0002",
            "start": 2.0,
            "end": 3.0,
            "ja": "ああ",
            "resolution": "omit",
            "reason": "non_speech",
            "note": "non_speech: 只有喘息，没有可确认的语言",
        }
        self.write_translation([cue], [omission])

        fast_subtitles.build(self.job)

        report = json.loads((self.job / "validation.json").read_text())
        self.assertEqual(report["transcript_candidates"], 2)
        self.assertEqual(report["decisions"], 2)
        self.assertEqual(report["direct"], 1)
        self.assertEqual(report["omitted"], 1)
        self.assertEqual(report["pending"], 0)
        self.assertEqual(report["missing_ids"], [])
        self.assertEqual(report["omit_reasons"], {"non_speech": 1})

    def test_inferred_cue_requires_an_explanation(self):
        self.write_translation(
            [
                {
                    "id": "c0001",
                    "start": 1.0,
                    "end": 2.0,
                    "ja": "…",
                    "zh": "继续。",
                    "resolution": "inferred",
                    "note": "",
                }
            ]
        )

        with self.assertRaisesRegex(ValueError, "inferred cue requires a note"):
            fast_subtitles.build(self.job)

    def test_build_clamps_a_pathologically_long_segment_to_its_end(self):
        self.write_translation(
            [
                {
                    "id": "c0001",
                    "start": 1.0,
                    "end": 19.0,
                    "ja": "こんにちは",
                    "zh": "你好。",
                    "resolution": "direct",
                    "note": "",
                }
            ]
        )

        output = fast_subtitles.build(self.job)

        dialogue = next(
            line for line in output.read_text(encoding="utf-8").splitlines() if line.startswith("Dialogue:")
        )
        self.assertNotIn("0:00:01.00,0:00:19.00", dialogue)
        self.assertIn("0:00:19.00", dialogue)
        report = json.loads((self.job / "validation.json").read_text())
        self.assertEqual(report["timing_adjusted"], ["c0001"])

    def test_invalid_or_duplicate_cues_are_rejected(self):
        cue = {
            "id": "c0001",
            "start": 3.0,
            "end": 2.0,
            "ja": "はい",
            "zh": "好。",
            "resolution": "direct",
            "note": "",
        }
        self.write_translation([cue, dict(cue)])

        with self.assertRaises(ValueError):
            fast_subtitles.build(self.job)

    def test_publish_is_exclusive_and_rechecks_source_hash(self):
        self.write_translation(
            [
                {
                    "id": "c0001",
                    "start": 1.0,
                    "end": 2.0,
                    "ja": "はい",
                    "zh": "好。",
                    "resolution": "direct",
                    "note": "",
                }
            ]
        )
        fast_subtitles.build(self.job)
        target = fast_subtitles.publish(self.job)
        self.assertTrue(target.exists())

        with self.assertRaisesRegex(FileExistsError, "target already exists"):
            fast_subtitles.publish(self.job)

        target.unlink()
        self.video.write_bytes(b"changed-video")
        with self.assertRaisesRegex(ValueError, "source media changed"):
            fast_subtitles.publish(self.job)

    def test_publish_does_not_remove_an_existing_target_when_open_fails(self):
        self.write_translation(
            [{"id": "c0001", "start": 1.0, "end": 2.0, "ja": "はい",
              "zh": "好。", "resolution": "direct", "note": ""}]
        )
        fast_subtitles.build(self.job)
        target = self.root / "movie.zh.ass"
        target.write_bytes(b"existing subtitle")
        with mock.patch.object(fast_subtitles.os, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                fast_subtitles.publish(self.job)
        self.assertEqual(target.read_bytes(), b"existing subtitle")

    def test_target_name_is_same_stem_with_zh_ass_suffix(self):
        self.assertEqual(
            fast_subtitles.target_for_source(Path("Example.Name.mkv")),
            Path("Example.Name.zh.ass"),
        )

    def test_model_and_font_paths_require_explicit_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "JA_ZH_SUBTITLES_MODEL"):
                fast_subtitles.configured_file(None, "JA_ZH_SUBTITLES_MODEL", "large-v3 model")
            self.assertEqual(
                fast_subtitles.configured_file(self.font, "JA_ZH_SUBTITLES_FONT", "CJK font"),
                self.font.resolve(),
            )

    def test_initialize_records_source_and_refuses_an_existing_target(self):
        jobs = self.root / "jobs"
        probe = {
            "format": {"duration": "20.0"},
            "streams": [
                {"index": 0, "codec_type": "video"},
                {"index": 1, "codec_type": "audio", "codec_name": "aac"},
            ],
        }

        job = fast_subtitles.initialize(self.video, jobs, probe=probe)
        manifest = json.loads((job / "manifest.json").read_text())
        self.assertEqual(manifest["source"]["sha256"], sha256(self.video))
        self.assertEqual(manifest["duration"], 20.0)
        self.assertEqual(manifest["audio_stream"], 0)
        self.assertEqual(manifest["target"], str(self.video.resolve().with_name("movie.zh.ass")))

        (self.root / "movie.zh.ass").write_text("existing", encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "target already exists"):
            fast_subtitles.initialize(self.video, jobs, probe=probe)

    def test_normalize_whisper_preserves_timing_and_confidence(self):
        raw = {
            "transcription": [
                {
                    "text": " こんにちは ",
                    "offsets": {"from": 1250, "to": 2500},
                    "tokens": [
                        {"id": 100, "p": 0.81},
                        {"id": 101, "p": 0.49},
                        {"id": 50365, "p": 1.0},
                    ],
                },
                {"text": " ", "offsets": {"from": 3000, "to": 3500}},
            ]
        }

        normalized = fast_subtitles.normalize_whisper(raw, duration=20.0)

        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["id"], "c0001")
        self.assertEqual(normalized[0]["start"], 1.25)
        self.assertEqual(normalized[0]["end"], 2.5)
        self.assertEqual(normalized[0]["ja"], "こんにちは")
        self.assertAlmostEqual(normalized[0]["asr_confidence"], 0.63, places=2)

    def test_extraction_filter_normalizes_quiet_speech_before_recognition(self):
        audio_filter = getattr(fast_subtitles, "extraction_filter", lambda: "")()

        self.assertIn("loudnorm=I=-16:LRA=11:TP=-1.5", audio_filter)
        self.assertIn("aresample=16000", audio_filter)

    def test_chunk_plan_uses_five_minutes_with_eight_second_overlap(self):
        plan_chunks = getattr(fast_subtitles, "plan_chunks", lambda _duration: [])

        chunks = plan_chunks(605.0)

        self.assertEqual(
            chunks,
            [
                {
                    "index": 0,
                    "start": 0.0,
                    "duration": 300.0,
                    "accept_start": 0.0,
                    "accept_end": 296.0,
                },
                {
                    "index": 1,
                    "start": 292.0,
                    "duration": 300.0,
                    "accept_start": 296.0,
                    "accept_end": 588.0,
                },
                {
                    "index": 2,
                    "start": 584.0,
                    "duration": 21.0,
                    "accept_start": 588.0,
                    "accept_end": 605.0,
                },
            ],
        )

    def test_normalize_whisper_shifts_chunk_time_and_trims_overlap(self):
        raw = {
            "transcription": [
                {"text": "重复", "offsets": {"from": 0, "to": 3000}},
                {"text": "保留", "offsets": {"from": 3000, "to": 7000}},
            ]
        }

        parameters = inspect.signature(fast_subtitles.normalize_whisper).parameters
        self.assertIn("offset", parameters)
        normalized = fast_subtitles.normalize_whisper(
            raw,
            duration=312.0,
            offset=292.0,
            accept_start=296.0,
            accept_end=312.0,
        )

        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["start"], 295.0)
        self.assertEqual(normalized[0]["end"], 299.0)
        self.assertEqual(normalized[0]["ja"], "保留")

    def test_normalize_whisper_allows_a_silent_chunk(self):
        parameters = inspect.signature(fast_subtitles.normalize_whisper).parameters
        self.assertIn("allow_empty", parameters)

        normalized = fast_subtitles.normalize_whisper(
            {"transcription": []},
            duration=300.0,
            allow_empty=True,
        )

        self.assertEqual(normalized, [])

    def test_normalize_whisper_clips_a_final_segment_to_the_source_timeline(self):
        raw = {
            "transcription": [
                {"text": "片尾对白", "offsets": {"from": 207420, "to": 237400}},
            ]
        }

        normalized = fast_subtitles.normalize_whisper(
            raw,
            duration=6647.659,
            offset=6424.0,
            accept_start=6428.0,
            accept_end=6647.659,
            allow_empty=True,
        )

        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["start"], 6631.42)
        self.assertEqual(normalized[0]["end"], 6647.659)

    def test_normalize_whisper_discards_a_segment_entirely_after_the_source(self):
        raw = {
            "transcription": [
                {"text": "静音幻觉", "offsets": {"from": 224000, "to": 237400}},
            ]
        }

        normalized = fast_subtitles.normalize_whisper(
            raw,
            duration=6647.659,
            offset=6424.0,
            accept_start=6428.0,
            accept_end=6647.659,
            allow_empty=True,
        )

        self.assertEqual(normalized, [])

    def test_whisper_json_replaces_invalid_utf8_and_reports_it(self):
        raw_path = self.root / "whisper.json"
        raw_path.write_bytes(
            b'{"transcription":[{"text":"\xff","offsets":{"from":0,"to":1000}}]}'
        )
        reader = getattr(fast_subtitles, "read_whisper_json", None)
        self.assertIsNotNone(reader)

        value, replacements = reader(raw_path)

        self.assertEqual(replacements, 1)
        self.assertEqual(value["transcription"][0]["text"], "\ufffd")

    def test_recognition_command_processes_one_chunk_without_vad(self):
        command = fast_subtitles.recognition_command(
            Path("/model/large-v3.bin"), Path("/job/chunk-0000.wav"), Path("/job/chunk-0000")
        )

        self.assertEqual(command.count("/job/chunk-0000.wav"), 1)
        self.assertIn("-ojf", command)
        self.assertIn("-osrt", command)
        self.assertIn("-nf", command)
        self.assertNotIn("--vad", command)
        self.assertIn("-mc", command)
        self.assertIn("0", command)

    def test_recognition_command_can_force_cpu_fallback(self):
        parameters = inspect.signature(fast_subtitles.recognition_command).parameters
        self.assertIn("no_gpu", parameters)

        command = fast_subtitles.recognition_command(
            Path("/model/large-v3.bin"),
            Path("/job/chunk-0000.wav"),
            Path("/job/chunk-0000"),
            no_gpu=True,
        )

        self.assertIn("-ng", command)
        self.assertNotIn("--vad", command)


if __name__ == "__main__":
    unittest.main()
