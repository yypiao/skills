# video-script-analysic

把商品售卖/商品展示参考视频转换为可直接投喂 Seedance 的本地生成稿。

## 特点

- 支持真人口播、手部演示、开箱、教程、测评、穿戴展示、无人商品特写等多种商品视频；
- 输出纯提示词 `*-prompt.txt` 和证据详情 `*.md`；
- 使用本地 Whisper、macOS Vision、PIL、ffmpeg/ffprobe 和确定性 Python；
- 不调用云端 LLM/VLM 做分析或改写；
- 商品由用户提供参考图和资料，原品牌/价格/配件不直接复制；
- 支持 `observations.json` 人工补充人物、服装、场景、逐镜动作和字幕。

## 快速使用

```bash
PRODUCT_NAME="用户商品名" \
PRODUCT_IMAGE="/absolute/path/product.png" \
OBSERVATIONS_FILE="/absolute/path/observations.json" \
bash scripts/local-to-seedance.sh "/absolute/path/reference.mp4" \
  "/absolute/path/output.md"
```

输出：

```text
output.md
output-prompt.txt
```

人工补充模板：

```text
references/observations-template.json
```

更多规则见 [SKILL.md](SKILL.md)。
