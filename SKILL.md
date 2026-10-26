---
name: ja-zh-subtitles
description: Create Japanese-to-Simplified-Chinese ASS subtitles for one selected local MP4/MKV film, TV episode, anime episode, or adult video using whisper.cpp large-v3 and contextual translation. Use for a quick viewing copy rather than a line-by-line coverage audit.
license: MIT
---

# 快速生成日语视频的简体中文字幕

用本地 whisper.cpp large-v3 对一部明确指定的日语 MP4/MKV 做一轮全片识别，再结合上下文翻译为简体中文 ASS。适用于电影、电视剧单集、动漫单集与成人影片。目标是尽快得到可观看的对白字幕；不承诺逐秒覆盖或逐句取证。

开始处理前阅读 [执行手册](references/workflow.md)，确认依赖和命令参数。安装说明见仓库 [README](README.md)。

## 范围与数据

- 仅处理用户明确指定的一个本地视频文件。多个匹配项先请用户选定；不扫描媒体库。电视剧与动漫按单集处理；跨集资料只使用用户明确提供的内容。
- 输出固定为视频同目录同名 `.zh.ass`。已有目标时停止；用户明确要求修正已有字幕时，先备份原目标再发布修正版。
- 不修改原视频。脚本只在本机处理媒体；翻译阶段会把日文转写和必要上下文交给当前使用的 AI 服务。处理前告知用户这一数据流。
- 只识别日语音轨中的声音；画面文字、路牌和硬字幕不在默认流程内。
- 如用户要求逐句审计、漏句取证、全片实播或播放器验证，先明确这些工作超出本技能的快速流程，再按该要求另行处理。

## 执行

1. `init` 固定源文件哈希、时长、音轨与目标路径。
2. `transcribe` 从原 PTS 音轨提取音频并做轻度响度归一化；按最长 300 秒、步进 292 秒的固定分块顺序运行 large-v3，使用日语和 beam 5，不启用 VAD。receipt 中的 `vad` 必须为 `false`。按绝对时间合并重叠区；默认不做第二轮选段识别。
3. 翻译前依据用户提供的信息和完整转写判断内容类型，建立简短剧情梗概、人物称呼和术语表。按电影、电视剧、动漫或成人影片的实际对白调整语域；长视频可按连续 ID 顺序分块翻译，每块沿用同一上下文，并参考相邻对白。
4. 对 `transcript.json` 的每个 ID 恰好作一次决定：清楚内容为 `direct`；有连续上下文支撑的保守推断为 `inferred`，写明依据；只能确认有人说话为 `unclear`；确认不应显示为 `omit`，写明受限原因。不要凭题材臆造人物、动作、因果或台词。
5. 用 `assemble` 生成完整 `translation.json`，再用 `build` 检查决定账本、时间、阅读速度、字体字形并生成 ASS，最后用 `publish` 独占创建目标文件。
6. 交付后用户指出具体时间点时，先运行只读 `coverage_audit.py`，再按执行手册的「按时间点复核」处理该点。

## 质量与交付

- 保留人物语气、称呼和作品术语；成人内容可自然、直白地翻译，不因题材自行弱化。低置信度、轻声或短句本身不构成省略理由。
- `omit` 仅接受 `non_speech`、`asr_hallucination`、`duplicate_event`、`unintelligible`，必须记录理由；不要通过漏掉 ID 表示省略。
- 不使用 VAD 过滤主识别音轨；耳语和气声可能被误判。用户触发的局部复核可使用 VAD 作诊断。
- 报告字幕路径、`direct`/`inferred`/`unclear`/`omit` 数量、必须为 0 的 `pending`、识别耗时，以及未实听或未实播范围。
