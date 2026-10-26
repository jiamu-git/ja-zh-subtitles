# ja-zh-subtitles

为**一个明确指定的本地日语 MP4/MKV**快速生成简体中文 `.zh.ass` 字幕的 Codex 技能。适用于电影、电视剧单集、动漫单集和成人影片。脚本使用 [whisper.cpp](https://github.com/ggml-org/whisper.cpp) 的 large-v3 模型完成一轮全片转写；AI 助手按内容类型和整片上下文翻译，并为每条转写记录明确的处理决定。字幕嵌入本片实际用字的字体子集。

这是以观看质量和处理速度为目标的流程，不保证逐秒覆盖或审计级准确率。脚本不会修改视频，也不会覆盖已有字幕。

## 依赖

- Python 3.11+ 与 `fonttools`（见 `requirements.txt`）。
- [FFmpeg](https://ffmpeg.org/download.html) 提供的 `ffmpeg`、`ffprobe`，以及 [whisper.cpp](https://github.com/ggml-org/whisper.cpp) 的 `whisper-cli`，均需在 `PATH` 中。
- 本地 [`ggml-large-v3.bin`](https://github.com/ggml-org/whisper.cpp/blob/master/models/README.md) 模型。
- 一份覆盖字幕中文字形的 [Noto Sans CJK 简体中文字型](https://github.com/notofonts/noto-cjk/tree/main/Sans) OTF/TTF 文件。字体另行下载，本仓库不分发字体或模型。

## 安装

**推荐交给 Codex 安装。**在 Codex 中发送：

> 请从 GitHub 仓库 `https://github.com/jiamu-git/ja-zh-subtitles` 安装 `ja-zh-subtitles` 技能，并按本 README 配置运行依赖、模型和字体。安装后检查 `SKILL.md` 与命令行工具是否可用；如果已有同名技能，先报告，不要覆盖。

手动安装时，把**整个仓库目录**放到 `$HOME/.agents/skills/ja-zh-subtitles/`，确保其中直接包含 `SKILL.md`；Codex 的[官方文档](https://learn.chatgpt.com/docs/build-skills)也列出了项目级安装位置。运行流程见 [执行手册](references/workflow.md)。

## 快速使用

下面的命令从仓库根目录运行。请替换示例路径：

```sh
VIDEO="/absolute/path/to/video.mkv"
JOB="$(python3 scripts/fast_subtitles.py init "$VIDEO")"
python3 scripts/fast_subtitles.py transcribe "$JOB" --model "/absolute/path/to/ggml-large-v3.bin"
```

接着让 AI 助手读取 `$JOB/transcript.json`，结合影片类型按 [翻译规则](references/workflow.md#上下文翻译) 生成四列 TSV：`id`、`resolution`、`zh`、`note`，另写 `context.json`。每个原始 ID 必须恰好出现一次；`omit` 也要占一行并写允许的原因。电视剧和动漫按单集处理；跨集信息仅采用用户提供的资料。

```sh
python3 scripts/fast_subtitles.py assemble "$JOB" translations.tsv --context context.json
python3 scripts/fast_subtitles.py build "$JOB" --font "/absolute/path/to/NotoSansCJKsc-Regular.otf"
python3 scripts/fast_subtitles.py publish "$JOB"
```

`publish` 只在视频目录内独占创建同名 `.zh.ass`；若目标已存在，命令失败并保留原文件。`init --audio-stream N` 可选择第 N 条**音频流**（从 0 开始）；`init --jobs-root DIR` 可指定工作目录；`transcribe --no-gpu` 可强制使用 CPU。模型、字体也可分别通过 `JA_ZH_SUBTITLES_MODEL`、`JA_ZH_SUBTITLES_FONT` 设置。

## 数据与限制

媒体、模型、字体和工作目录都留在本机，脚本不上传它们。**翻译阶段会把日文转写及必要的上下文交给你所使用的 AI 服务**；请先确认这符合你的隐私要求。不要将真实媒体、转写、字幕或工作目录提交到仓库。

识别结果与翻译决定保存在工作目录，以支持中断恢复和用户指出的局部修正。脚本只处理音轨，不识别画面文字或硬字幕；默认流程不进行逐秒人工实听、全片实播或漏句审计。交付时须说明未验证的范围。

## 开发与验证

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts
```

测试使用临时文件和人工构造的字幕，不需要实际影片或模型。GitHub Actions 会安装 Python 依赖并运行完整测试，包括字体子集嵌入测试。

## 许可证

仓库代码与文档采用 [MIT License](LICENSE)。whisper.cpp、FFmpeg、fontTools、模型与字体各自遵循其上游许可证；本仓库不包含这些第三方文件。
