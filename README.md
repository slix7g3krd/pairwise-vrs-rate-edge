# pairwise-vrs-rate-edge

## 领域

游戏、图形与多媒体

## 主题

可变速率着色

## 说明

边缘速率图、运动掩码与能力降级。

初始状态下 `graphics/vrs_rate_edge.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
