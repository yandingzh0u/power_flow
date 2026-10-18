# 主配微示范区控制模型校正记录

本目录为明确标注假设的校正算例，不是已核实的现场数据。原始算例未覆盖。
默认假设：原始 P、Pmax、Qmax、Qmin 均为零的 BE 节点不连接未记载的额外无功设备，按 PQ 节点求解。零有功并不能证明没有 SVC/补偿设备，仍需核实。
有实际无功设备时，可通过 settings.json 提供有限 PV 控制或固定 Q 设置重新生成；原卡 Q 字段不被擅自解释为 PQ 无功。

| 断面 | TRAFO-008 原负载率 | 校正后负载率 | 剩余过载变压器 | 剩余过载线路 | 电压范围 pu | 保留待定的 BE 控制器 |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| 13 点 | 397.90% | 55.40% | 18 | 21 | 0.93709–1.02787 | 9 |
| 20 点 | 489.26% | 53.53% | 4 | 8 | 0.92410–1.06510 | 0 |

## 为什么不能把全网强行改成正常

线路额定电流、变压器容量、拓扑、分接头、负荷与有功出力均保持原值。部分径向支路即使只传有功、忽略损耗，并允许达到原模型电压上限，容量仍不够。
下列数值是必要条件下的缺口下界；实际整改可能需要更多裕度，不等于建议切负荷量。

- 13 点 FUNS2 → AYWS1：下游净有功 43.189 MW；在 1.10 pu 下最大可传有功上界 34.824 MW；至少需 8.365 MW 的本地支撑/转供调整，或核实并整改容量参数。
- 20 点 DD1 → DD2：下游净有功 4.374 MW；在 1.10 pu 下最大可传有功上界 3.301 MW；至少需 1.073 MW 的本地支撑/转供调整，或核实并整改容量参数。
- 20 点 CW16 → CW30：下游净有功 2.752 MW；在 1.10 pu 下最大可传有功上界 2.501 MW；至少需 0.251 MW 的本地支撑/转供调整，或核实并整改容量参数。

## 使用与验证

- 两个断面均重新求解全网 AC 潮流；summary.json 保存功率方程残差、全部剩余过载、电压筛查、每个节点的改动依据和输入 SHA-256。
- 电压按原模型 min_vm_pu/max_vm_pu 判限；另附 0.95–1.05 pu 筛查数量，不把这一筛查区间当作统一现场标准。
- `pp.from_json(...)` 读取本目录 JSON 后直接 `pp.runpp(net)` 会使用保存的 NR 与无功限值设置。
- 本结果未宣称全网恢复正常；仍有过载或未知设备能力时，operating_status 为 violations_remaining。
- 获得实际参数后，在 settings.json 的 `13` / `20` 中按母线名称填写 `fixed_q` 的 `q_mvar`，或 `limited_pv` 的 `min_q_mvar`、`max_q_mvar`、`vm_pu`，并记录 evidence。所有值必须是有限数。

复现：`/home/y/miniconda3/envs/powerflow/bin/python tools/repair_demo_model.py`

带实际参数：`/home/y/miniconda3/envs/powerflow/bin/python tools/repair_demo_model.py --settings path/to/settings.json`

[pandapower OPF 约束说明](https://pandapower.readthedocs.io/en/stable/opf/formulation.html)：实际无功边界和设备约束需要明确给定。
