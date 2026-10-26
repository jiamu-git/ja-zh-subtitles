# 快速字幕执行手册

## 环境

需要 Python 3.11+、`ffmpeg`、`ffprobe`、`whisper-cli`、whisper.cpp 的 `ggml-large-v3.bin`，以及支持简体中文字形的 Noto Sans CJK 字体。按仓库 [README](../README.md#安装) 安装技能与依赖。以下检查命令从仓库根目录运行。模型与字体分别通过 `--model`、`--font` 指定，也可设置 `JA_ZH_SUBTITLES_MODEL`、`JA_ZH_SUBTITLES_FONT`。

```sh
python3 -m pip install -r requirements.txt
command -v ffmpeg ffprobe whisper-cli
```

用户指出具体时间点后，局部诊断可选用 Silero VAD 模型和 Demucs；它们不参与默认流程：

`whisper-cli --vad --vad-model <Silero 模型路径>`；人声分离可用 `uvx --with numpy --from demucs`。

脚本不上传媒体；翻译时，日文转写及必要上下文会进入所使用的 AI 服务。不要把用户媒体、转写、工作目录或模型文件提交到 GitHub。工作目录默认为：

```text
~/.ja-zh-subtitles-jobs/<视频名>-<源文件哈希前12位>/
```

## 初始化与单轮识别

```sh
SUBS_SKILL="/path/to/ja-zh-subtitles"
SUBS_VIDEO="/path/to/video.mp4"
SUBS_JOB="$(python3 "$SUBS_SKILL/scripts/fast_subtitles.py" init "$SUBS_VIDEO")"
python3 "$SUBS_SKILL/scripts/fast_subtitles.py" transcribe "$SUBS_JOB" --model "/path/to/ggml-large-v3.bin"
```

`init` 在任何计算前检查同目录 `.zh.ass`。已存在时停止。它记录源文件 SHA-256、大小、媒体流、音轨和片长。

`transcribe` 使用容器 PTS 提取 16 kHz 单声道 WAV，并用单遍动态模式 `loudnorm=I=-16:LRA=11:TP=-1.5` 做轻度响度归一化。随后按每块最长 300 秒、起点步进 292 秒顺序运行 large-v3 日语识别；命令不得包含 `--vad`、VAD 模型或阈值参数，`transcript-receipt.json` 必须记录 `vad: false`。这样可避免耳语、气声和轻声对白在送入 Whisper 前被过滤。

“一轮全片识别”指每个预先确定的固定块恰好识别一次；中断后允许复用 receipt 完全匹配的已完成块。禁止根据识别文本、置信度或播放结果挑选内容做第二遍 targeted、focused 或补漏识别。

重叠区以中点分界：每个非首段忽略前 4 秒中点之前的结果，每个非末段忽略末 4 秒中点之后的结果，再映射回全片绝对时间。不要做模糊文本去重，也不要因低置信度、长跨度或非语言声自动删除仍含语言的 cue；这些项目交给翻译阶段判为 `direct`、`inferred` 或 `unclear`。短分块限制持续音乐、音效等非语言声触发长时间重复 token，同时让边界对白至少在一个片段中保留完整上下文。

结果保留为：

- `chunks/chunk-*.json`、`chunks/chunk-*.srt`：各分块的 whisper.cpp 原始结果；
- `chunks/chunk-*.receipt.json`：逐块设置和结果哈希，可在中断后继续；
- `transcript.raw-index.json`：分块位置、接受区间和原始结果索引；
- `transcript.json`：规范化的 ID、时间、日文和 ASR 置信信息；
- `transcript-receipt.json`：源、音频、模型、引擎和结果哈希。

恢复时只复用与 receipt 完全匹配的结果，不静默换模型或重跑第二轮。

## 上下文翻译

把 `transcript.json` 作为一个完整的翻译任务。先依据用户提供的信息与该文件的转写确认题材；不要从文件名或类型标签猜测具体剧情。翻译前形成短上下文：

```text
题材：电影／电视剧单集／动漫单集／成人影片（仅在有依据时填写）
剧情：按当前文件的实际转写概括
人物与称呼：只记录转写或用户提供的资料支持的信息
风格：根据人物、时代和题材选择自然的中文语域
约束：保留时间；不把非语言声臆造成对白；不新增人物、动作、因果或专名
```

不同题材的翻译重点：

| 类型 | 处理重点 |
|---|---|
| 电影 | 保留人物关系、时代语气与场景间的称呼变化；不要凭类型猜剧情。 |
| 电视剧 | 按单集处理，保持本集已证实的人名与称呼；跨集设定只采用用户提供的资料。 |
| 动漫 | 保留角色口吻、敬称和已证实的专有名词；歌曲若被识别成可确认的歌词，也按对白决定规则处理。画面文字不在音频识别范围内。 |
| 成人影片 | 成人用语可自然、直白地翻译；喘息等非语言声不臆造成台词。 |

以上类别可重叠，例如成人题材电视剧或动漫。以具体对白为准，不因题材自动推断台词或省略内容。

若完整输入或预期输出超过上下文窗口，在实现层按连续 ID 顺序分块：

- 每块约 400–800 条，并携带前后 10 条作为只读上下文；
- 每块都附带同一剧情梗概、称呼表和术语表；
- 顺序翻译，并把上一块的新称呼或剧情状态滚入下一块；
- 重叠上下文只辅助理解，不得在输出中产生重复 cue；
- 每个分块都必须为主区间内的每个 ID 输出一次决定；上下文 ID 不输出；
- 最终按原 ID 和绝对时间合并。

不要为了并行速度牺牲人物称呼和语气的一致性。合并结果必须形成完整决定账本：`transcript.json` 中的每个 ID 恰好出现一次，不能因模型漏回某行而静默丢失。

### `translation.json`

```json
{
  "context": {
    "content_type": "Japanese TV episode",
    "plot": "基于转写得到的简短剧情",
    "terms": {"日文称呼": "统一中文译法"}
  },
  "cues": [
    {
      "id": "c0001",
      "start": 12.5,
      "end": 14.2,
      "ja": "原始日文",
      "zh": "简体中文",
      "resolution": "direct",
      "note": ""
    },
    {
      "id": "c0002",
      "start": 15.0,
      "end": 16.4,
      "ja": "不稳定的识别文本",
      "zh": "结合前后请求语气得到的保守译文",
      "resolution": "inferred",
      "note": "前后两句均在表达同一请求"
    }
  ],
  "omissions": [
    {
      "id": "c0003",
      "start": 17.0,
      "end": 18.2,
      "ja": "ああ",
      "resolution": "omit",
      "reason": "non_speech",
      "note": "non_speech: 只有非语言呼声，没有可确认的词语"
    }
  ],
  "coverage": {
    "transcript_candidates": 3,
    "decisions": 3,
    "direct": 1,
    "inferred": 1,
    "unclear": 0,
    "omitted": 1,
    "pending": 0,
    "missing_ids": [],
    "omit_reasons": {"non_speech": 1}
  }
}
```

`resolution` 只能是：

| 值 | 使用条件 |
|---|---|
| `direct` | 日文含义足够清楚，可以直接翻译 |
| `inferred` | 确认存在语言，且前后对白对大意有连续支持；`note` 必填 |
| `unclear` | 只能确认有人说话；使用事实性提示，`note` 必填 |

所有原 ID 都必须有决定。确认不应显示字幕的项目放入 `omissions`，`resolution` 为 `omit`，`zh` 为空，并让 `note` 以以下机器可检验的原因开头：

| 原因 | 使用条件 |
|---|---|
| `non_speech` | 只有呼吸、呻吟、音乐、撞击等，无法确认存在语言 |
| `asr_hallucination` | 可由上下文或重复模式确认是识别幻觉 |
| `duplicate_event` | 同一发声已由另一条保留，且时间实质重复 |
| `unintelligible` | 无法确认任何可显示的信息；只要能确认有人说话，应优先使用 `unclear` |

格式示例：`non_speech: 只有音效，没有可确认的语言`。轻声、短句、低置信度或剧情上“不重要”不得作为省略理由。不要让影片类型本身成为具体推断的证据。

为减少 LLM 输出中的重复字段，也可以先写四列制表符文件，每行依次为 `id`、`resolution`、`zh`、`note`，再与原转写确定性合并：

```sh
python3 "$SUBS_SKILL/scripts/fast_subtitles.py" assemble "$SUBS_JOB" translations.tsv --context context.json
```

空 `note` 仍须保留第四列。`direct`、`inferred`、`unclear` 的第三列必须是中文；`omit` 的第三列必须为空，第四列必须以允许的原因开头。`assemble` 拒绝未知、重复或缺少的 ID，并从 `transcript.json` 回填日文与时间。不要手工绕过 `assemble`：`build` 会重新读取原转写并核对覆盖率，任何未决定 ID 都会硬失败。

## 构建与发布

```sh
python3 "$SUBS_SKILL/scripts/fast_subtitles.py" build "$SUBS_JOB" --font "/path/to/NotoSansCJKsc-Regular.otf"
python3 "$SUBS_SKILL/scripts/fast_subtitles.py" publish "$SUBS_JOB"
```

`build` 先以 `transcript.json` 为基准检查完整决定账本：总决定数必须等于识别候选数，`pending` 必须为 0，且每个省略项都有受限原因。然后检查唯一 ID、时间范围、排序、非空中文、推断依据、重叠、长时长和阅读速度，按本片实际中文字幕字符裁剪所指定的字体并写入 ASS 的 `[Fonts]` 段，生成工作目录内的 `subtitle.zh.ass` 与 `validation.json`。`validation.json` 会记录覆盖计数、各类省略数以及转写哈希。字体附件必须往返解码一致，且不得缺少字幕用字。若 whisper 在连续非语言声中给单句分配超过 8 秒的异常跨度，构建时保留原始 `translation.json`，但把显示窗口收紧到该跨度末尾的 1.5–6 秒，并在 `timing_adjusted` 中列出 ID，避免字幕长时间挂屏。注意收紧只保证跨度**末尾**有字幕，跨度**头部**会留空：若该跨度其实是被合并的多句对白，观看时就是一段明显空洞。这种情况按下面「交付后按时间点复核」切分修正，不要放弃整段。

`publish` 重新校验源文件、翻译和构建哈希，并用独占创建写入同目录 `.zh.ass`。目标已存在或中途出现时停止，不覆盖。

默认不逐条渲染、不做播放器集成验证、不宣称已经人工逐秒实听。需要播放器确认时，只做片头、中段、片尾和推断密集位置的抽样播放。

## 交付

报告：

- 最终字幕绝对路径和字幕条数；
- `direct`、`inferred`、`unclear`、`omit` 数量，以及必须为 0 的 `pending`；
- 源 SHA-256 是否在发布时匹配；
- 单轮识别的分块数、源音频秒数、含重叠识别秒数；
- receipt 中的 `recognition_wall_seconds`（实际识别运行耗时；旧缓存无此值时如实说明）；
- 是否做过实际播放抽检，以及仍未实听的范围；
- 若本次含用户触发的修正：修正的 ID、时间与依据、`inferred`/`direct` 归属、备份文件路径，以及仍未复核的范围（审计发现的其余收紧空洞只是统计，不要在未获要求时逐个处理）。

## 交付后按时间点复核（用户触发）

用户指出具体缺失或错句时，只复核该时间点；不得据此挑选其它片段重跑识别，也不得做全片补漏。先跑只读审计（不写盘、不重新识别）：

```sh
python3 "$SUBS_SKILL/scripts/coverage_audit.py" "$SUBS_JOB"
```

它输出两类待复核信号：**可疑句段**（跨度正好 30 秒可能是整窗幻觉；起点正好落在 30 秒整数倍可能是伪起点；跨度 ≥ 8 秒可能合并了多句）与**覆盖空洞**（收紧未覆盖 vs 该处无 cue）。无 cue 的空白不能单凭审计结果视为有意省略。

### 故障 A：整窗幻觉与伪起点

Whisper 以 30 秒为窗口解码，本流程按约定使用 `-nf`（关闭温度回退）。窗口被数字静音（片头标题卡等）或 BGM 占据时，整窗会被压成一条幻觉句段（常见套话：`ご視聴ありがとうございました`、`おやすみなさい`、`お疲れ様でした`、`私はあなたを愛しています`），紧接着的下一窗口，其首段起点会被写成窗口边界而不是真实起点。窗口内短而轻的对白可能**完全不产生句段**——于是没有 ID、没有决定项、也没有字幕。

复核步骤：

1. **避开 30 秒边界**：把目标点放进不对齐 30 秒的窗口再解码（`ffmpeg -ss <起点> -t <时长>`，或 `whisper-cli -f <wav> -ot <毫秒> -d <毫秒>`）。直接在边界上解码会复现同一个幻觉。
2. **VAD 只作诊断**：`--vad --vad-model <Silero 模型路径>` 用来判断该处到底有没有语音、起止在哪。绝不要把它引入主流程。
3. **人声分离判真假**：`uvx --with numpy --from demucs demucs --two-stems=vocals -n htdemucs -o <目录> <wav>`（必须显式带 `numpy`）。逐 0.25–1 秒比较 `vocals` 与 `no_vocals` 电平：真实人声高于伴奏轨；只有串音时低于伴奏轨 10–20 dB。
4. **量级参照**：把该处 300–3400 Hz 带内 RMS 与已知对白比较。低 10–20 dB 且人声轨不占优，通常就是 BGM，没有可显示的语言。
5. **多路解码收敛**：改变输入（原始混音／人声轨／提升增益／`-mc 0` 与 `-mc -1`）反复解码，看哪些词反复出现。若各变体给出互不相同的结果，就是**无法可靠还原**，不要挑一个当结论。
6. **不要用场景描述当 `--prompt`**：会被复述成字幕内容。要提示只给相邻的真实对白。

### 故障 B：长跨度收紧留下头部空白

`build` 的收紧只保证跨度末尾有字幕。当该跨度其实是 whisper 合并的多句对白时，会留下“对白还在继续、字幕却空白”的空洞。正确做法不是放弃整段，而是按真实语音边界（同上的避边界解码 + VAD）**切分**成多条各自 < 8 秒的 cue，从而不再触发收紧；切分后各段显示窗口即真实位置。

### 修正如何落地

- 只新增、切分或改时；日文一律取原识别文本的**子串**，或经多路解码重建并显式标注，不得凭题材新造台词。
- 重建内容若有连续上下文支持，标 `inferred`，note 写明诊断依据和不确定程度；只能确认有人说话时标 `unclear`。须经用户实听确认，才可将无法独立验证的内容改为 `direct`。
- 在 `transcript.json` 插入条目时带 `provenance`／`reconstructed`／`resegmented` 标记，**数组必须保持按 `start` 递增**（否则 `validate_cues` 因排序硬失败，ASS 事件顺序也会乱）。
- 重新生成四列 TSV（新 ID 必须有决定），依次 `assemble`、`build`、`publish`。
- `publish` 是独占创建，不会覆盖。用户明确要求修正已有字幕时，先把原目标移到备份名保留，再发布，并说明 `transcript.json` 的哈希已不等于 `transcript-receipt.json`（首轮结果仍完整保留在 `chunks/`）。
- 把证据、修正范围与 `not_done`（未做全片漏句审计、未据此挑选其它片段重跑）写进工作目录的 `corrections.json`。
- 复核临时文件放在工作目录下的独立子目录（如 `recheck-*/`），不要污染 `chunks/` 与 receipt。
