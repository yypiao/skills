---
name: video-script-analysic
description: 输入任意商品售卖或商品展示主题的视频/链接/脚本，输出可直接投喂 Seedance 的简化生成稿和证据详情：自动识别原片类型、比例、人物/无人、场景、镜头动作、商品展示顺序、屏幕文字和声音；不预设固定模板。允许普通联网取数/平台 API，但分析和改稿不调用云端 LLM/VLM。商品通常由用户提供相似产品或参考图。当用户说「把这个商品视频转成 Seedance」「照着做类似带货视频」「提取可生成脚本」时使用。
---

# Video Script Analysic · 商品视频转 Seedance

目标：把任意以**商品售卖 / 商品展示 / 商品种草**为主题的参考视频，整理成两份结果：

1. **简版投喂稿 `*-prompt.txt`**：只放可直接复制给 Seedance 的提示词，包含全局、逐镜、参考帧路径和声音后期说明；
2. **详情证据稿 `*.md`**：保留 OCR、口播、Vision 粗标签、参考帧、原品牌/原价格证据，方便人工复核，但不建议整份投喂生成模型。

默认复刻参考视频的**构图、人物关系、场景、动作调度、镜头节奏、商品展示顺序和售卖逻辑**，再把主体商品替换为用户提供的商品图/商品资料。

## 核心边界

1. 可以使用普通 API 做视频下载、平台取数、网页元数据、商品资料读取。
2. 禁止调用 GPT、Claude、Gemini、云端 VLM 或其他在线 LLM/VLM 做分析、改写、总结或补全。
3. 分析与组稿只使用本地 Whisper、macOS Vision、PIL、ffmpeg/ffprobe 和确定性 Python。
4. 商品名称、外观、logo、参数、价格、折扣、配件、功效和库存以用户资料为准；未提供就占位或删除。
5. 不套固定模板：先识别真人口播、手部演示、开箱、教程、测评、穿戴、无人商品轮播等类型，再决定输出。
6. 原片无人不新增人物；原片无声不新增旁白；原片品牌、包装和价格不直接复制。

## 一键用法

### 本地视频，自动生成商品占位

```bash
bash scripts/local-to-seedance.sh "/absolute/path/to/reference.mp4"
```

### 带用户商品资料

```bash
PRODUCT_NAME="用户商品名" \
PRODUCT_IMAGE="/absolute/path/to/product.png" \
PRODUCT_NOTES="只写用户已确认的外观、配件、材质或卖点" \
bash scripts/local-to-seedance.sh "/absolute/path/to/reference.mp4" \
  "/absolute/path/to/output.md"
```

同时生成：

```text
output.md                 # 详情证据稿 + 可复制提示词
output-prompt.txt         # 纯 Seedance 提示词，默认与 md 同目录
```

### 使用人工 observations 补足本地 Vision 看不清的信息

```bash
PRODUCT_NAME="用户商品名" \
PRODUCT_IMAGE="/absolute/path/to/product.png" \
OBSERVATIONS_FILE="/absolute/path/to/observations.json" \
bash scripts/local-to-seedance.sh "/absolute/path/to/reference.mp4"
```

模板：

```text
references/observations-template.json
```

`observations.json` 用来补充本地视觉模型不稳定的信息：人物年龄感、发型、唇钉、纹身、服装图案、墙面装饰、逐镜动作、成片字幕、商品出场时间、最终 CTA 等。它是**人工提供的本地结构化资料**，不是云端识别结果。

可选环境变量：

```bash
PRODUCT_NAME="..."
PRODUCT_IMAGE="/absolute/path/product.png"
PRODUCT_NOTES="..."
OBSERVATIONS_FILE="/absolute/path/observations.json"
BRAND_ALIASES="原品牌,原商品名"
SIMPLE_PROMPT="/absolute/path/seedance-prompt.txt"
RUN_DIR="/absolute/path/evidence-dir"
```

## 本地处理流程

1. `check-deps.sh --local-video`：本地视频不做外网依赖探测。
2. `prepare-assets.sh`：复制视频、ffprobe 元数据、抽音频、抽帧、切点检测。
3. Whisper：只使用本机已缓存模型；auto 模式不自动联网下载。
4. macOS Vision：本地 OCR、人脸/人体、粗图像标签。
5. PIL：亮度、饱和度、对比度、主色等画面量化。
6. `build_seedance_prompt.py`：按证据确定性组稿，不调用云端模型。

## 分镜策略

- 镜头边界优先使用 ffmpeg 场景切点，并参考口播时间。
- 自动合并过短镜头、拆分过长镜头；目标是适合 Seedance 的单段长度，而不是机械照抄所有碎切。
- 每个镜头必须包含：销售任务、可见动作、人物/无人关系、商品阶段、景别/机位、光线、屏幕文字策略、参考帧。
- 每镜自动嵌入 2–3 张参考原帧绝对路径；投喂 Seedance 时需要同时绑定这些帧和用户商品图。
- 口播默认作为后期配音：画面生成自然说话状态，不要求单段精准对口型。
- OCR 区分“叠加字幕”和“商品/包装表面字样”：原商品 logo 只在证据区出现，不进入简版提示词。

## 输出结构

### `*-prompt.txt`

- 全局风格、人物、场景、镜头、光线、商品一致性约束；
- 每个镜头一段可执行提示词；
- 每段包含参考帧路径；
- 声音后期说明；
- 不包含原品牌/原商品名，不包含大段分析。

### `*.md`

- Story 一句话；
- 简版总提示词；
- 分段提示词；
- 参考帧绑定清单；
- 每镜 OCR、Vision 粗标签、原片声音证据；
- 原片配音稿和占位替换稿；
- 反向约束和建议补充素材。

## observations 字段

常用字段：

```json
{
  "global": {
    "story": "",
    "style": "",
    "person_mode": "auto | person | hands | none",
    "character": "",
    "wardrobe": "",
    "scene": "",
    "camera": "",
    "lighting": "",
    "audio": ""
  },
  "product": {
    "name": "",
    "image": "/absolute/path/product.png",
    "notes": "",
    "intro_time": null,
    "brand_aliases": []
  },
  "shots": [
    {
      "start": 0,
      "end": 4,
      "sales_task": "",
      "shot_size": "",
      "visual_action": "",
      "camera": "",
      "lighting": "",
      "screen_text": "",
      "product_phase": ""
    }
  ]
}
```

规则：

- `person_mode` 支持 `auto / person / hands / none`：自动判断、真人出镜、只露手、无人展示；逐镜字段可覆盖全局设置。
- `shot_size` 可选，用来强制指定“人物中近景穿插商品特写”等景别；留空则自动判断。
- `screen_text: ""` 表示本段不加字幕；字符串或数组表示明确添加。
- `intro_time` 不确定就写 `null`，由口播关键词粗略推断。
- 只写画面可见或用户确认的信息；不要让 observations 编造商品参数。

## 铁律

- 不调用云端 LLM/VLM。
- 不把所有视频套成 OneBlade 式真人口播。
- 不臆造品牌、logo、价格、折扣、功效、库存、配件、销量或参数。
- 不改变原片比例、人物数量、场景、声音类型和镜头节奏。
- 不让 AI 商品变形、融化、数量增减、结构错位或被手部遮挡关键外观。
- 本地 Vision 的 `people/adult/art/tool` 只作为粗线索，不能当作精确年龄、族裔、品牌或场景结论。

## 文件

- `scripts/local-to-seedance.sh`：一键入口。
- `scripts/prepare-assets.sh`：本地证据包。
- `scripts/build_seedance_prompt.py`：确定性生成简版和详情稿。
- `scripts/local_semantics.swift`：macOS Vision 本地基础语义。
- `references/observations-template.json`：人工补充模板。
- `references/output-template.md`：输出结构说明。


## 结构化结果（Web/API 使用）

`scripts/build_seedance_prompt.py` 支持：

```bash
python3 scripts/build_seedance_prompt.py "$RUN_DIR"   --output result.md   --simple-output seedance-prompt.txt   --json-output result.json
```

`local-to-seedance.sh` 会通过 `RESULT_JSON` 环境变量自动传入该参数。`result.json` 使用 `video-analysis.result/v1`，包含 meta、story、全局/简版提示词、逐镜动作与证据、OCR/Vision 标签、声音稿和反向约束；Web 服务以 JSON 为主结果，不使用 Markdown 正则解析。
