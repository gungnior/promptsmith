# 质量与画质 tag 基础

质量词放在 `q` 字段，用来告诉模型"这是一张完成度很高的图"。

## 常用质量词

- `masterpiece` — 通用最高质量标记
- `best quality` — 与 masterpiece 成对使用
- `highres` — 高分辨率
- `ultra detailed` — 细节丰富（慎用，容易过拟合）
- `official art` — 官方画风感
- `amazing quality` — 强化版质量标记

## 评分前缀

部分模型支持 `score_N` 形式的质量评分，例如 `score_7`、`score_9`。这是 Anima 系模型的写法，SDXL 不需要。

## 注意事项

1. 质量词堆太多不会更好，反而挤占内容词的位置
2. `q` 字段里**只放质量词**，不要混入画面内容
3. 负面质量词（`low quality`、`worst quality`）属于 `n` 字段，不要写在 `q`
