# 石家庄电网潮流分析页

打开页面前先在项目根目录运行：

```bash
python web_sjz/build_data.py
python -m http.server 4173 --directory web_sjz
```

浏览器访问 `http://127.0.0.1:4173/`。

页面读取项目内两个断面：

- `data/demo_area/converted/demo_area_13_pandapower.json`
- `data/demo_area/converted/demo_area_20_pandapower.json`
- 对应的 `*_distribution_partitions.json`

`build_data.py` 将母线、线路、变压器及其潮流结果生成 `data.js`，页面不会在缺值时用随机数补值。点击“运行潮流校核”会尝试调用本地 `POST /api/v1/gmss/run`；接口没有启动时继续显示已保存的快照，并在日志里说明原因。

首页按主配微结构展示四个可开关层：主网母线、主配接口变压器、METIS 配网分区和 6 个微网 PCC。配网分区着色基于重算出的拓扑成员集合，并与保存的 63/64 分区统计对齐；如果当前依赖版本重算出额外微小组，脚本会按拓扑连接合并到保存的目标分区数，避免页面同时出现两个分区总数。

## 位置数据说明

当前页面使用的是电气连接端点和工程图布局，能够准确表达“哪条线路连接哪两个母线”，但不能证明线路在现实地图上的走向。项目中的 `actual_network_layout.json` 是 NetworkX 弹簧布局，不是经纬度；现有线路 `length_km` 字段也全部为 1，是 BPA 阻抗换算参数。

页面的“导入 GeoJSON”可以接入现场 GIS 数据。推荐准备 WGS84 `FeatureCollection`，用 `LineString` 表示线路折线、`Point` 表示母线位置，并在 `properties` 中放置 `bus_id`、`name`、`line_id` 或 `loading` 字段。导入后页面会切换为 GIS 图层，并保留拓扑快照作为对照。

## 变压器过载与 BE 恒压节点

网页会标记计算过载，并展示相邻 BE 恒压节点的等效无功。计算收敛不表示运行安全；BE 的无功可能包含为维持电压而产生的未安排补偿，需与真实设备能力核对。

TRAFO-008 在 20 点断面的 489.26% 可由常规牛顿法和 Iwamoto 牛顿法复现。仅在诊断副本中将其低压侧 BE 控制无功限为 0 后，负载率约为 55.16%，但电压目标也随之失去控制。这是原因隔离实验，不能作为正式修复；原始 DAT 和 Pandapower 算例均保留。

诊断记录见 [diagnostics/trafo_008.md](diagnostics/trafo_008.md)，完整数值、输入文件哈希见同目录 JSON。使用项目环境复现：

```bash
/home/y/miniconda3/envs/powerflow/bin/python tools/diagnose_demo_transformer.py
```

正式更正需确认节点类型、电压目标、无功补偿容量与状态、变压器阻抗及分接头，并对照 BPA 的 PFO 未安排无功结果。
