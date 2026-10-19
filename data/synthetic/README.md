# 万节点 / 十万节点研究输入

由 `python tools/build_expanded_case.py` 生成。原始文件在 `raw_dataset/extracted/`，不在此处覆盖或调整。

- `sjz_13_x5_ring.dat`：五份 13 点网络，10,605 母线、10,640 支路。
- `sjz_20_x5_ring.dat`：五份 20 点网络，10,610 母线、10,650 支路。
- `sjz_13_x50_ring.dat`：五十份 13 点网络，106,050 母线、106,400 支路。
- `sjz_20_x50_ring.dat`：五十份 20 点网络，106,100 母线、106,500 支路。
- 同名 `.manifest.json`：源文件与生成文件哈希、改为 BE 的原 BS、联络线参数来源及名称映射。

这些是复制区域互联研究假设，非实测的石家庄大网架。构造规则、控制含义、试算结果见[万节点说明](../../doc/万节点扩展算例说明.md)和[十万节点说明](../../doc/十万节点扩展算例说明.md)。

`python tools/build_expanded_case.py` 重建 ×5；`python tools/build_expanded_case.py --copies 50` 重建 ×50。生成器不允许向原始数据目录写入。
