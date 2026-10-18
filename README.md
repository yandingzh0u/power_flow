# 石家庄主配微潮流计算

基于提供的石家庄 13 点 / 20 点原始 DAT 计算交流潮流，实时展示迭代日志、节点电压、线路与全部 T 卡结果，并对配网分区进行固定边界 AC 校核。原始数据参数不修改，过载结果不截断。

## 启动

当前已验证的 Python 版本为 3.11：

```bash
python -m pip install -r requirements.txt
python start_api.py
```

本机现有环境可直接运行：

```bash
/home/y/miniconda3/envs/powerflow/bin/python start_api.py
```

打开 <http://127.0.0.1:4173/>。同一个服务同时提供网页、计算接口与实时事件流，不需要额外启动静态 HTTP 服务。

默认显示 13 点已保存结果。选择断面和算法后点击“运行潮流校核”，由 Python 重新计算；完成后拓扑、节点详情、分析表格全部更新为本次结果。接口失败、求解未收敛、分区未通过分别报告。

## 输入与计算含义

- 唯一原始输入目录：`raw_dataset/extracted/`，保留 2 个 DAT 和 19 个配套 XLS；校验值见 `doc/repository_cleanup.json`。
- 全部算法采用同一份原始阻抗、基准容量、P/Q、BE/BS 节点控制、固定分接值、拓扑和设备额定值。BE 按原始恒 P/V 控制求解，不擅自添加无功限额。
- 支持普通牛顿、阻尼牛顿、解耦启动加牛顿。最后一种参考公开 BPA/IPF 流程，不是 PSD-BPA 软件，也不声称复制其闭源内核；来源与范围见 `doc/tools/bpa_style_solver.md`。
- 原始零阻抗 L 卡精确合并，计算后按 KCL 恢复支路潮流；全部 91 条变压器 T 卡均展示。
- 配网节点按低于 35 kV 的模型基准划分，METIS 后检查连通性。各分区使用本次全网边界电压重新求解，并报告与全网结果的误差。这是固定边界局部验证，不是独立的全网分布式协调算法。
- 既有 6 个微网 PCC 保留为等值接口展示；没有额外向原始 DAT 叠加人工微网注入。原数据未提供真实微网内部模型。
- 配套 XLS 原样保留，上传时一并保存，不自动覆盖 DAT 断面负荷。
- 拓扑坐标仅用于电气工程图，不代表实地线路走向。没有对坐标缺失编造地理数据。

## 文件夹导入

点击“导入数据文件夹”，选择含 DAT 的文件夹。支持本算例已实现的 B、BE、BS、L、固定分接 T 卡；不支持的控制卡会明确报错。多个 DAT 作为多个可选断面。上传文件按原字节保存到 `runtime/uploads/`，不会覆盖内置数据。

## 目录

```text
gmss/                  DAT 解析、求解、分区、展示数据、任务与 HTTP 服务
raw_dataset/extracted/ 石家庄唯一原始 DAT / XLS
data/demo_area/         电气图布局坐标
web_sjz/               HTML / CSS / JS、初始结果及生成脚本
tools/                 命令行结果导出
tests/                 解析、算法、物理一致性及接口回归测试
doc/                   原 Word / PDF / WPS、报告附件、清理记录
runtime/               新计算结果、日志、上传数据（不纳入 Git）
```

重建初始快照：`python web_sjz/build_data.py`。使用与现场计算相同的入口，不另设一套计算模型。

导出原始断面的 CSV / JSON：

```bash
python tools/run_raw_dat_powerflow.py --algorithm nr
```

运行测试：`python -m unittest discover -s tests -v`。

## 接口

- `GET /api/datasets`：内置及导入数据、可用算法。
- `POST /api/datasets/import`：上传目录文件，内容为原字节的 Base64。
- `POST /api/runs`：`{"dataset_id":"13","algorithm":"nr"}`，返回任务 ID。
- `GET /api/runs/{id}/events`：SSE 实时日志、迭代电压、分区进度与结束状态。
- `GET /api/runs/{id}/result`：本次完整结果及耗时、残差、源文件哈希。

每个任务在 `runtime/runs/{id}/` 保存结果和事件日志。过载不会被当作数值不收敛，成功也不会被解释为所有设备都满足限额。

## 清理与恢复

无关算例、旧缓存、PPT、ZIP、图片及无用脚本已移入系统回收站，未永久删除。逐项原路径、回收站路径与资料迁移位置见 `doc/repository_cleanup.json`。`.git` 与原有 IDE / 工作区配置保留。

历史报告原文归档到 `doc/`，其中早期参数修正假设不作为当前计算规则；当前规则以本文件与原始 DAT 为准。
