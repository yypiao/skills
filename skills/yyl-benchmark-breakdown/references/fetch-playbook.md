# 取数手册:平台本地 API → TikHub → Jina → 粘贴

目标:**任意链接都能拿到可拆解的内容,且短视频必须有口播。** 优先零成本本地方案,免费额度类 API 作为兜底。**永不空手而归。**

---

## 0. 一次性 setup

跑根目录的 `bash scripts/check-deps.sh` 一键自检。缺什么按提示装,详细说明见 `INSTALL.md`。
如果本地 Douyin / XHS 下载 API 不可达,且 Docker 已安装并启动,先运行:

```bash
bash scripts/bootstrap-local-apis.sh
bash scripts/check-deps.sh
```

**铁律:永远只从环境变量读 key(`TIKHUB_API_KEY` / `JINA_API_KEY`),绝不打印、绝不写进任何文件、绝不进命令历史。** Cookie 也一样:如果 XHS-Downloader 需要 Cookie,写进它自己的 `Volume/settings.json`,不要贴进报告或 skill 文档。

优先使用自动证据包流程:
```bash
bash scripts/prepare-assets.sh "<url>"
```

输出目录默认在 `${BENCHMARK_WORKDIR:-/tmp/yyl-benchmark}/<时间戳>/`,核心产物:
- `manifest.txt` — 本次抓取产物清单,开拆前先读。
- `metadata.json` — 平台接口响应/网页元数据。
- `video.mp4`、`audio.txt`、`sheet_*.jpg` — 视频笔记证据。
- `xhs_images/`、`xhs_sheet_*.jpg` — 小红书图文笔记证据。

---

## 1. 平台 + 粒度识别(看 URL)

| 平台 | 域名 | 单条 | 账号主页 |
|---|---|---|---|
| 抖音 | `douyin.com`/`v.douyin.com` | `/video/`、`?modal_id=`、`/note/` | `/user/` |
| 快手 | `kuaishou.com` | `/short-video/` | `/profile/` |
| B站 | `bilibili.com`/`b23.tv` | `/video/BV...` | `space.bilibili.com` |
| TikTok | `tiktok.com` | `/@x/video/` | `/@x` |
| 小红书 | `xiaohongshu.com`/`xhslink.com` | `/explore/`、`/discovery/item/` | `/user/profile/` |
| YouTube | `youtube.com`/`youtu.be` | `/watch`、`/shorts/` | `/@` |
| 公众号 | `mp.weixin.qq.com` | `/s/` | 无 |

---

## 2. 取数路由(逐级回退)

### ①-A Douyin_TikTok_Download_API(主力)—— 抖音 / TikTok / Bilibili
官方支持三平台(README 写明)。**响应快、字段全、零费用、能拿下载地址用于转写。**

```bash
# 通用解析(自动判平台)
curl -s "http://localhost:80/api/hybrid/video_data?url=<urlencoded>&minimal=false"
# 或具体端点:抖音单视频
curl -s "http://localhost:80/api/douyin/web/fetch_one_video?aweme_id=<id>"
# 账号作品流(找规律用)
curl -s "http://localhost:80/api/douyin/web/fetch_user_post_videos?sec_user_id=<sid>"
# 评论
curl -s "http://localhost:80/api/douyin/web/fetch_video_comments?aweme_id=<id>"
```

关键字段位置:
- `data.desc` — 标题/文案完整版
- `data.video.download_addr.url_list[0]` — 视频下载地址(**转写源**)
- `data.video.play_addr.url_list[0]` — 备用下载地址
- `data.music.play_url.url_list[0]` — ⚠️ **背景音乐,不是口播**
- `data.statistics.{digg/comment/share/collect/play}_count` — 数据
- `data.author.{nickname/signature/follower_count/aweme_count}` — 作者
- `data.cha_list[].cha_name`、`data.text_extra[].hashtag_name` — 话题标签

### ①-B XHS-Downloader API(主力)—— 小红书单条笔记
推荐开源部署约定:
- API:`${XHS_API_BASE:-http://127.0.0.1:5556}`
- 下载目录:`${XHS_DOWNLOAD_DIR:-$HOME/.xhs-downloader/Volume/Download}`
- 容器名:`${XHS_CONTAINER_NAME:-xhs-downloader-api}`

自检:
```bash
curl -sSf "${XHS_API_BASE:-http://127.0.0.1:5556}/docs" >/dev/null && echo "XHS API OK"
docker ps --filter name="${XHS_CONTAINER_NAME:-xhs-downloader-api}" --format '{{.Names}} {{.Status}} {{.Ports}}'
```

单条笔记详情 + 下载:
```bash
mkdir -p /tmp/bm
curl -sS -X POST "${XHS_API_BASE:-http://127.0.0.1:5556}/xhs/detail" \
  -H "Content-Type: application/json" \
  -d '{"url":"<小红书链接>","download":true,"skip":false}' \
  -o /tmp/bm/xhs_detail.json

# 看响应摘要,确认没有风控/空数据
head -c 800 /tmp/bm/xhs_detail.json
```

图文只下载指定图片序号(按需):
```bash
curl -sS -X POST "${XHS_API_BASE:-http://127.0.0.1:5556}/xhs/detail" \
  -H "Content-Type: application/json" \
  -d '{"url":"<小红书链接>","download":true,"index":[1,2,3],"skip":false}' \
  -o /tmp/bm/xhs_detail.json
```

关键判断:
- 响应 `data` 是作品详情与下载地址的主要证据;先保存 JSON,再读字段。优先用 `download:false` 拿详情和下载地址,再由脚本自己下载素材;只有详情模式失败或需要验证下载器落盘时,再尝试 `download:true`。
- `download:true` 会把 mp4 / 图片落到 `Volume/Download/`;用最新修改时间或响应里的文件名定位素材。但它比详情模式更慢,也会受下载记录影响,不适合作为第一步取数。
- 小红书视频笔记:找到下载目录里的 `.mp4`,进入第 3 节的转写 + 抽帧流程。
- 小红书图文笔记:找到下载目录里的图片,按文件名/序号拼 contact sheet,把图序当作视觉脚本拆。
- 如果返回风控、空数据、旧链接失效:先自动走 HTML `noteDetailMap` 兜底解析;仍失败再让用户在小红书 App 里重新复制最新分享链接。
- Cookie 不要放在请求体里传给 `/xhs/detail`:API 响应会回显 `params.cookie`,容易把 Cookie 写进 `metadata.json`。需要 Cookie 时,写入 XHS-Downloader 自己的 `Volume/settings.json` 后重启容器。
- 官方文档说明 Cookie 是可选项,但功能异常时建议配置或更新 Cookie;未设置 Cookie 时视频只能下载低分辨率文件。Skill 检测到 API 空数据且 cookie 为空时,应提示用户配置 Cookie,但仍继续走 HTML 兜底。

下载后定位素材:
```bash
XHS_DOWNLOAD_DIR="${XHS_DOWNLOAD_DIR:-$HOME/.xhs-downloader/Volume/Download}"

# 最近 20 个下载文件/文件夹,优先看时间最新且标题匹配的项
find "$XHS_DOWNLOAD_DIR" -maxdepth 2 \( -type f -o -type d \) \
  -print0 | xargs -0 stat -f "%m %N" | sort -nr | head -20

# 如果是视频,取最新 mp4
find "$XHS_DOWNLOAD_DIR" -type f -iname "*.mp4" \
  -print0 | xargs -0 stat -f "%m %N" | sort -nr | head -1

# 如果是图文,取最新图片文件
find "$XHS_DOWNLOAD_DIR" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.webp" -o -iname "*.heic" \) \
  -print0 | xargs -0 stat -f "%m %N" | sort -nr | head -20
```

安全/体验:
- 不要把 Cookie 写进命令行、报告或 benchmarks。
- 自动滚动/账号批量采集不要用 XHS-Downloader 硬扫;账号级采样后续走 MediaCrawler 或让用户给 5-15 条代表链接。

### ② TikHub(备用)—— YouTube / 快手 / 海外平台 / 小红书本地 API 失败
```bash
# 小红书图文/视频笔记(⚠️ 小红书全系端点需付费额度,免费额度不接)
curl -s -G "https://api.tikhub.io/api/v1/xiaohongshu/web_v3/fetch_note_detail" \
  -H "Authorization: Bearer $TIKHUB_API_KEY" \
  --data-urlencode "note_id=<id>" --data-urlencode "xsec_token=<token>"
```
端点目录:https://api.tikhub.io/ ;`tikhub user info` 看额度。

### ③ Jina Reader(免费兜底)—— 公众号、网页、图文长文
```bash
curl -s "https://r.jina.ai/<完整url>"
```
对小红书图文:Jina 通常只能拿到标题/标签/时间,**拿不到正文与完整图片**(被客户端渲染挡),只作为弱兜底。

### ④ 引导粘贴(托底)
前三级都拿不到 → 让用户发文案/字幕/截图,**绝不只甩"失败"**。

---

## 3. 短视频:读取/下载 + 自动语言转写 + 抽视觉帧

**铁律:抖音 / TikTok / B站 / YouTube / 小红书视频笔记必须有口播 + 画面证据。**只看 caption、互动数据或只看口播都不够。URL 和本地视频都统一走:

```bash
bash scripts/prepare-assets.sh "<url-or-local-video>"
```

### 本地视频轻量自检

```bash
bash scripts/check-deps.sh --local-video
```

本地视频不需要 Docker / Douyin API / XHS API;只需要 ffmpeg、ffprobe、openai-whisper。本地文件默认拿不到账号、标题、点赞、评论、发布时间,报告中标「未获取」。

### Whisper 语言策略

- 默认 `WHISPER_LANGUAGE=auto`,不传 `--language`。
- 已知中文原生口播才可显式:`WHISPER_LANGUAGE=Chinese`。
- 搬运、AI 译制、TikTok 英文原声、中英混合、语言不确定:必须 auto。
- 不要根据用户语言或文件所在平台强制设为中文。

回归案例已验证:49 秒英文 Philips OneBlade 视频自动检测为 English 后转写准确;强制 Chinese 会误识别,并在片尾静音段产生幻觉。

### Whisper 模型策略

默认 `WHISPER_MODEL=auto`:
1. 完整 medium 缓存 → 使用 medium。
2. 无完整 medium、有完整 small 缓存 → 使用 small,并在 manifest 标记低质量风险。
3. 都没有 → 默认准备下载 medium。

提前预取:

```bash
bash scripts/prefetch-model.sh medium      # 常规重要对标
bash scripts/prefetch-model.sh large-v3    # 专业术语/口音重
```

预取脚本支持续传 `.part`、SHA256 校验、通过后原子替换。默认自动探测 Clash `127.0.0.1:7897`,显式 `HTTPS_PROXY` 优先。

默认不设置 `--initial_prompt`。只有用户提供可靠术语表且明确音频语言时才设置 `WHISPER_INITIAL_PROMPT`;混合语言或机器译制视频乱加 prompt 可能更差。

### 产物

`prepare-assets.sh` 自动产出:

- `video.mp4` —— 下载或复制的视频。
- `audio.wav`、`audio.srt`、`audio.txt` —— 音轨、带时间轴字幕、纯文本。
- `frames/s_*.jpg` —— 统一采样帧,供 contact sheet、OCR 和画面量化使用。
- `frames/cut_*.jpg`、`cuts.log` —— 场景切点和剪辑节奏。
- `sheet_*.jpg` —— contact sheet。
- `ocr_raw.txt`、`ocr.md` —— 屏幕标题、UI、产品名、价格、烧录文字。
- `visual_metrics.json`、`visual_metrics.md` —— 亮度、饱和度、对比度、主色、画面跳变。
- `video_stats.txt`、`manifest.txt`。

脚本末尾自动执行证据校验:

```bash
python3 scripts/verify_evidence.py "<run_dir>"
```

### 无图片输入时的视觉回退

优先读 `sheet_*.jpg`;如果当前环境不支持图片输入,不要停止,使用:

1. `ocr.md` —— 屏幕文字证据。
2. `visual_metrics.md` —— 画面风格和节奏的量化证据。
3. `cuts.log` —— 切点时间、剪辑密度。

OCR 只证明屏幕出现过的文字,不能把 OCR 噪声当口播;无音频则标「无口播」。

### 抽帧密度

| 视频时长 | 采样密度 |
| --- | --- |
| <=3 分钟 | 每秒 1 帧 |
| 3–15 分钟 | 每 3 秒 1 帧 |
| >15 分钟 | 每 5 秒 1 帧,必要时切片并行转写 |

---

## 4. 账号级 / 评论 / 进阶数据
- 账号作品流:本地 API `/api/douyin/web/fetch_user_post_videos`(参数 `sec_user_id`,从单视频响应里能拿到)
- 评论:`/api/douyin/web/fetch_video_comments`(参数 `aweme_id`)
- 抓最近 5–15 条标题+数据找规律,**够找规律就停**,别无限抓。挑 1–2 条代表作用上面的「下载+转写」流程深拆。

---

## 5. 抓到后必做

回显一句「**我抓到了什么**」:
- 平台 / 粒度 / 标题
- 拿到哪些字段(数据、标签、文案)
- **口播是否已转写**(没转写就明说,做不了脚本层)
- 缺什么、要不要补

再喂给 `breakdown-framework.md` 开始拆。
