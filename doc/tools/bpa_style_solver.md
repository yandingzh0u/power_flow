# 原始 DAT 的 BPA 风格潮流流程

本实现只覆盖当前两个原始 DAT 中实际存在的 B、BE、BS、L、固定变比 T 卡。
它依据公开 BPA/IPF 的说明和计算结构实现，不调用潮流软件，也不声称复刻
PSD-BPA 闭源内核或已经与其输出完成验证。

流程为：固定列 DAT 读取（含隐含小数）→ 原电压/容量基准及变比 → 零阻抗
L 卡理想连接合并 → Ybus → P–θ、Q–V 分解启动 → 完整交流牛顿迭代 →
恢复理想连接潮流 → 原节点功率守恒 → BE 无功分配及两侧支路负载率。

两份原文件都有 `>SOL_ITER,DECOUPLED=2,NEWTON=15<`。这里 2 表示依次求一次
P 和一次 Q；15 是随后完整牛顿迭代的上限。本实现选用 `1e-8 pu` 功率残差
容差，该容差是显式实现设置，不冒称读取自未填写的 PSD-BPA 默认值。

分解启动的固定矩阵为 A=零相角时的 ∂P/∂θ，以及 C=-Im(Ybus)[PQ,PQ]。
完整牛顿阶段保留 P/Q 与幅值/相角的全部交叉导数。分解启动采用公开 IPF
中的平滑步长截断形式；另外增加基于完整交流残差的启动方向阻尼，以及
牛顿回溯。它们是本 Python 实现的收敛措施，不能据此推断 PSD-BPA 的具体
阻尼策略。数值启动失败会明确记录回退；不修改注入、额定值或 BE 电压。

BE 持续维持指定 P、V，不能因为 QMAX/QMIN 填零就自动转为 PQ。当前 BE
无可调并联电纳，故将所需总无功分成原填写范围内的发电无功与范围外的
“未安排无功”。后者是维持电压的模型要求，不是实有补偿容量。13 点有 9
张 BE 卡的 QMAX 为负而 QMIN 留空；其输出分配标为未确定，未擅自猜测
特定软件版本会如何归一化。上述 BE 的固定 P、V 求解不依赖这种输出分配。
未安排无功保留完整精度，不照搬公开 IPF 输出时的 0.5 Mvar 显示抑制。

BQ 的限无功切换、BG 的远方调压、R 卡自动分接、X 卡投切等未在当前文件
出现，尚未实现；遇到这些卡片或不支持的计算指令会报错。当前所有 T 卡
励磁参数为零；非零字段由原始解析器拒绝。零阻抗连接存在环或多个控压
节点时也拒绝猜测支路功率或无功分配。

电流负载率为 `100 max(|S1|/|V1pu|, |S2|/|V2pu|)/额定MVA`；另输出两端
最大视在功率/额定容量，以及超过额定值的百分比。负载率 489% 对应超额
389%，二者字段分开。未将有功/容量代替完整电气负载率。

运行：

```bash
/home/y/miniconda3/envs/powerflow/bin/python tools/run_raw_dat_powerflow.py --algorithm bpa-style
/home/y/miniconda3/envs/powerflow/bin/python -m unittest discover -s tests -p test_bpa_style_solver.py -v
```

结果写入 `runtime/exports/`：`results.json` 含逐步残差、
所用设置、启动阻尼、原始输入哈希、纯牛顿对照和 BE 分配；CSV 保留全部
原节点及全部原支路。原始 DAT、既有转换模型和网页均不由此命令改写。

2026-10-18 实测：两个断面都执行一次 P、一次 Q，启动方向阻尼均为 0.25，
没有回退至原初值，随后完整牛顿均 4 步收敛。GXVS2→GXVY2 电流负载率
分别为 397.895716% 和 489.259635%。与独立纯牛顿路径的最大复电压差均
小于 4e-10 pu，普通支路两端复功率最大差小于 5e-6 MVA。两个实现仍使用
同一套 DAT 建模解释，以上不能证明与 PSD-BPA 的全部语义一致。无阻尼的
分解启动会使本算例完整交流残差增大，详细试探值与接受值都写入 trace。

参考资料（公开 IPF 与 PSD-BPA 不是同一版本的程序）：

- [IPF 迭代顺序及控制选项](https://bpa-ipf.readthedocs.io/en/doc-updates/basic/x_window_gui.html#solve)
- [BE、BQ、BS 卡语义](https://bpa-ipf.readthedocs.io/en/latest/basic/record_formats.html#ac-bus-data-be)
- [公开计算流程 solton.f](https://github.com/mbheinen/bpa-ipf-tsp/blob/master/ipf/solton.f)
- [分解矩阵 jacbfc.f](https://github.com/mbheinen/bpa-ipf-tsp/blob/master/ipf/jacbfc.f)
- [分解步长 baksln.f](https://github.com/mbheinen/bpa-ipf-tsp/blob/master/ipf/baksln.f)
- [无功分配 allocq.f](https://github.com/mbheinen/bpa-ipf-tsp/blob/master/ipf/allocq.f)
- [支路统计 pqparl.f](https://github.com/mbheinen/bpa-ipf-tsp/blob/master/ipf/pqparl.f)
