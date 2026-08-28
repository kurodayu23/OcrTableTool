# OCR 回归基准集边界

`scripts/prepare_benchmark_corpus.ps1` 从经过校验的官方归档中确定性抽取 120 张本地回归图。原始归档、抽取图片、真值、OCR 输出和性能日志默认位于项目根目录下已忽略的：

```text
benchmark-data\
```

它们不属于产品运行时，也不进入平板包。正式安装包不得包含 `benchmarks`、`ai-workspace`、下载归档、测试图片或运行报告。

120 张按来源分层覆盖：

- 30 张 FUNSD 噪声表单；
- 20 张 XFUND 中文键值表单；
- 25 张 CORD 票据；
- 20 张 DocLayNet 纯文字、列表和混排页面；
- 25 张 cTDaR 现代/历史表格页面。

其中约三分之二为 `development`，其余为 `holdout`。只允许根据 development 集调节通用算法；禁止查看某个 holdout 样本后写文件名、固定行列数或模板特例。

这些图片用于评测，不用于训练模型。需要真正训练或微调时，必须另建获得商业授权的训练集，不能把验收集混入训练集。

每次回归至少保存：原始 OCR JSON、结构化 JSON、CSV、XLSX、总耗时、峰值工作集、模式选择和错误信息。导出后必须回读 CSV/XLSX，比较行列、文本和数值精确值。
