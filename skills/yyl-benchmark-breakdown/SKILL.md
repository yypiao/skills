---
name: yyl-benchmark-breakdown
description: 拆解对标账号 / 竞品内容——丢一个链接或本地视频进来,自动识别平台(抖音/小红书/B站/YouTube/公众号)和粒度(单条 or 整个账号),抓取内容后输出三件套:可复用爆款公式、画面+口播逐段拆解、人设与内容定位,并自动存档到 benchmarks/。短视频自动下载/读取 + 转写口播 + 抽视觉帧(contact sheet);图片输入不可用时自动用 macOS Vision OCR + PIL 画面量化回退。当用户说「拆解这个账号/视频/笔记」「分析对标/竞品」「这条为什么火」「给链接拆内容」「teardown」「看看别人怎么做的」时使用。抓不到内容会引导粘贴文案/字幕,绝不直接报错。
---

# YYL 对标账号拆解 Skill

目标:**给一个链接或本地视频,把内容拆到能直接复用的程度。**

固定产出三件套(格式见 `references/output-template.md`):
1. **可复用爆款公式** —— 选题套路、结构模板、视觉公式。
2. **画面 + 口播逐段拆解** —— 时间轴对齐,标注画面、口播、钩子、节奏、转折、CTA 与协同方式。
3. **人设与内容定位** —— 人设标签、视觉符号、内容矩阵、差异化打法。

> 这是分析型 skill,产出 Markdown,不产图。封面用 `yyl-video-thumbnail`;档案/纸质拼贴风格视频用对应视频创作 skill。

## 0. 前置自检:区分 URL 和本地视频

先判断输入是不是本地文件:
- 本地文件存在 → 跑:`bash scripts/check-deps.sh --local-video`
- URL → 跑:`bash scripts/check-deps.sh`

不要用 URL 的完整依赖清单阻塞本地视频拆解:

| 输入 | 必需依赖 | 可跳过 |
| --- | --- | --- |
| 本地视频 | ffmpeg、ffprobe、openai-whisper、可用 Python;macOS 回退建议有 Swift/PIL | Docker、平台本地 API |
| 抖音/TikTok/B站 URL | ffmpeg、openai-whisper、Docker、Douyin_TikTok_Download_API | TikHub、Jina |
| 小红书 URL | ffmpeg、openai-whisper、Docker、XHS-Downloader API | TikHub、Jina |
| YouTube/快手/海外 URL | ffmpeg、openai-whisper,TikHub 或其他可用抓取方式 | 本地平台 API |

自检失败时先补环境,并明确告诉用户缺什么、为什么需要、下一步命令是什么:
- Docker 未启动:引导启动 Docker Desktop;Colima 用户确认 `docker info` 可用。
- 本地 API 不可达且 Docker 可用:运行 `bash scripts/bootstrap-local-apis.sh`,再重跑自检。
- ffmpeg / whisper 缺失:按 `INSTALL.md` 安装。
- 依赖全部通过后不要反复确认,直接进入证据包准备。

**重要版本约定**:Douyin_TikTok_Download_API 默认使用已验证兼容旧接口的 `evil0ctal/douyin_tiktok_download_api:V4.1.2`;不要默认拉 `latest`,因为新版已改 PostgreSQL/微服务,可能不兼容本 skill 的旧端点。

## 1. 识别输入

- URL:按 `references/fetch-playbook.md` 判断平台和粒度;拿不准粒度先按单条处理。
- 本地路径:只要文件存在且 ffmpeg 能读,就作为 `local-file` 视频处理。
- 本地视频默认拿不到平台账号、标题、点赞、评论、发布时间;这些字段标为「未获取」,不要编造。若用户同时给了来源 URL,再用 URL 流程补元数据。

## 2. 统一准备证据包

对 URL 和本地视频都运行同一个入口:

```bash
bash scripts/prepare-assets.sh "<url-or-local-video>"
```

如需固定产物目录:

```bash
bash scripts/prepare-assets.sh "<url-or-local-video>" "/tmp/yyl-benchmark/<run-name>"
```

脚本会自动生成:
- `metadata.json` / `ffprobe.json` —— 元数据或本地视频技术信息。
- `video.mp4` —— 下载或复制的视频。
- `audio.wav`、`audio.srt`、`audio.txt` —— 音轨、带时间轴字幕、纯文本。
- `frames/s_*.jpg` —— 统一采样帧。
- `frames/cut_*.jpg`、`cuts.log` —— 场景切点与剪辑节奏证据。
- `sheet_*.jpg` —— contact sheet。
- `ocr_raw.txt`、`ocr.md` —— 屏幕文字/OCR 证据。
- `visual_metrics.json`、`visual_metrics.md` —— 亮度、饱和度、对比度、主色与画面跳变。
- `video_stats.txt` —— 转写模型、语言、切点等运行信息。
- `manifest.txt` —— 本次抓到了什么、缺什么、文件清单。

脚本最后会自动运行:

```bash
python3 scripts/verify_evidence.py "<run_dir>"
```

若证据校验失败,先修证据包,不要直接写分析结论。

## 3. Whisper 策略:语言自动、模型分级、不要乱加术语 prompt

### 语言

默认 `WHISPER_LANGUAGE=auto`,不传 `--language`,让 Whisper 根据前 30 秒自动检测。

- 已知中文原生视频:可显式 `WHISPER_LANGUAGE=Chinese`。
- 译制、搬运、TikTok/英文原声、中英混合、语言不确定:必须用 `auto`。
- 不要因为文件来自中文平台或用户用中文说话,就把视频语言强制设成中文。

本 skill 的回归视频虽然出现在中文环境,但 Whisper 自动检测为 English;强制 Chinese 已验证会把剃须/刀头等内容误识别成发型/鞋子等,并在片尾静音段产生幻觉。

### 模型

默认 `WHISPER_MODEL=auto`,只使用完整缓存:
1. 完整 `medium` 缓存 → 用 medium。
2. 没有完整 medium 但有完整 small 缓存 → 用 small,并在 `manifest.txt` 标记低质量风险。
3. 都没有 → 默认准备下载 medium。

重要中文对标优先提前预取模型:

```bash
bash scripts/prefetch-model.sh medium      # 平衡速度与准确率
bash scripts/prefetch-model.sh large-v3    # 专业术语/口音重时再用
```

### Prompt

- 默认不设置 `--initial_prompt`。
- 只有用户已提供可靠术语表,且明确知道音频语言时才设置 `WHISPER_INITIAL_PROMPT`。
- 对 AI 配音、机器译制、混合语言视频,强行塞术语 prompt 可能让结果更差。

转写命令始终带:
- `--condition_on_previous_text False`:减少错误上下文滚雪球。
- `--fp16 False`:CPU 环境稳定。

## 4. URL 抓取回退

按顺序尝试,不在第一级失败就停止:

1. **平台本地 API**
   - 抖音 / TikTok / Bilibili:`Douyin_TikTok_Download_API`。
   - 小红书:XHS-Downloader API。
2. **TikHub**:YouTube、快手、海外平台或本地 API 失败时用;需要 `TIKHUB_API_KEY`。
3. **Jina Reader**:公众号、普通网页、图文长文兜底。
4. **用户粘贴**:让用户补文案、字幕、截图或录屏;不要只报“失败”。

具体端点和命令见 `references/fetch-playbook.md`。

## 5. 视觉证据:能读图就读,不能读图用回退

优先级:
1. 如果环境支持图片输入,读取 `sheet_*.jpg` 和必要的关键帧。
2. 如果图片输入不可用,不要停;读取并使用脚本自动生成的:
   - `ocr.md` —— 标题、UI、烧录字、产品名、价格等。
   - `visual_metrics.md` —— 画面亮度、饱和度、对比度、主色和明显跳变。
   - `cuts.log` —— 切点时间与剪辑密度。
3. OCR 只能证明屏幕上出现过什么文字,不能把 OCR 噪声当口播。
4. 无音频视频明确标「无口播」;无字幕/无屏幕文字也不要硬编。

短视频采样密度:
- `<=3 分钟`:每秒 1 帧。
- `3–15 分钟`:每 3 秒 1 帧。
- `>15 分钟`:每 5 秒 1 帧,必要时切片并行转写。

## 6. 拆解与存档

证据包完整后,结合:
- `references/breakdown-framework.md`
- `references/visual-framework.md`
- `references/output-template.md`

输出报告并自动存档到:

```text
benchmarks/<平台>-<账号名或未知账号>/<日期>-<标题slug>.md
```

短视频报告末尾必须包含 `## 完整文字转写稿`,完整粘贴 `audio.txt`;图文/网页附完整正文。抓不到的数据写「未获取」,不要编造。

## 铁律

- **本地视频不被 Docker 阻塞**:本地文件走轻量依赖检查。
- **语言不确定就自动检测**:不要无脑 `--language Chinese`。
- **短视频必须同时有口播和画面证据**:caption 和互动数据不能替代。
- **图片输入不可用也要完成视觉分析**:使用 OCR、PIL 指标和切点日志。
- **逐级回退,永不空手而归**:真抓不到就引导用户粘贴文案/字幕/截图。
- **结论必须可复用**:不是复述内容,而是说明为什么有效、怎么套用。
- **始终存档**:为长期对标库沉淀样本。

## 文件

- `INSTALL.md` —— 依赖安装与常见问题。
- `scripts/check-deps.sh` —— URL/本地视频依赖自检。
- `scripts/bootstrap-local-apis.sh` —— 启动本地下载 API;Douyin API 固定 V4.1.2。
- `scripts/prepare-assets.sh` —— 统一证据包流程。
- `scripts/prefetch-model.sh` —— 预取 Whisper 模型。
- `scripts/verify_evidence.py` / `verify-evidence.sh` —— 校验证据包完整性。
- `scripts/ocr.swift` —— macOS Vision OCR 源码。
- `scripts/build_ocr_report.py` —— OCR 原始结果整理。
- `scripts/analyze_frames.py` —— PIL 画面量化。
- `references/*.md` —— 取数、拆解、视觉和输出模板。
