"""Reproduce transformer loading and isolate BE control effects without editing cases."""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys

import pandapower as pp
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.convert_bpa_to_pandapower import parse_dat


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measure(net, index: int) -> dict:
    row = net.res_trafo.loc[index]
    return {
        "converged": bool(net.converged),
        "loading_percent": float(row.loading_percent),
        "p_hv_mw": float(row.p_hv_mw),
        "q_hv_mvar": float(row.q_hv_mvar),
        "vm_lv_pu": float(row.vm_lv_pu),
        "overloaded_transformers": int((net.res_trafo.loading_percent > net.trafo.max_loading_percent).sum()),
    }


def diagnose(label: str, index: int) -> dict:
    case_path = ROOT / f"data/demo_area/converted/demo_area_{label}_pandapower.json"
    dat_path = ROOT / f"data/demo_area/raw/{label}点sim2合并anony.dat"
    net = pp.from_json(str(case_path))
    trafo = net.trafo.loc[index]
    hv, lv = int(trafo.hv_bus), int(trafo.lv_bus)
    _, buses, branches, _, _ = parse_dat(dat_path)
    raw_bus = next(b for b in buses if b.name == net.bus.at[lv, "bpa_name"] and b.kv == net.bus.at[lv, "vn_kv"])
    endpoints = {(net.bus.at[b, "bpa_name"], float(net.bus.at[b, "vn_kv"])) for b in (hv, lv)}
    matches = [b for b in branches if b.kind == "T" and {b.from_key, b.to_key} == endpoints]
    if len(matches) != 1:
        raise ValueError("Expected exactly one matching raw transformer card")
    raw_trafo = matches[0]
    controllers = net.gen.index[(net.gen.bus == lv) & net.gen.in_service]
    if raw_bus.subtype != "E" or len(controllers) != 1:
        raise ValueError("This diagnostic expects one BE voltage controller on the LV bus")
    controller = int(controllers[0])
    modes = {}
    for mode in ("nr", "iwamoto_nr", "nr_existing_q_limits", "diagnostic_controller_q_zero"):
        trial = copy.deepcopy(net)
        if mode == "diagnostic_controller_q_zero":
            # Counterfactual only: this is NOT a corrected engineering case.
            trial.gen.loc[controllers, ["min_q_mvar", "max_q_mvar"]] = 0.0
        with contextlib.redirect_stdout(io.StringIO()):
            pp.runpp(
                trial, algorithm="iwamoto_nr" if mode == "iwamoto_nr" else "nr",
                init="flat", calculate_voltage_angles=True, trafo_model="t",
                tolerance_mva=1e-8, max_iteration=80,
                enforce_q_lims=mode in {"nr_existing_q_limits", "diagnostic_controller_q_zero"},
            )
        modes[mode] = {
            **measure(trial, index),
            "controller_q_mvar": float(trial.res_gen.at[controller, "q_mvar"]),
            "iterations": int(trial._ppc["iterations"]),
        }
    saved = measure(net, index)
    loadings = [modes[m]["loading_percent"] for m in ("nr", "iwamoto_nr", "nr_existing_q_limits")]
    checks = {
        "capacity_matches_raw": abs(float(trafo.sn_mva) - raw_trafo.rate) < 1e-9,
        "saved_result_reproduced": abs(saved["loading_percent"] - loadings[0]) < 1e-4,
        "solver_loading_agrees": max(loadings) - min(loadings) < 1e-4,
    }
    if not all(checks.values()):
        raise ValueError(f"Unexpected diagnostic comparison: {checks}")
    return {
        "scenario": label, "trafo_index": index, "display_name": f"TRAFO-{index + 1:03d}",
        "hv_bus": hv, "lv_bus": lv, "lv_bus_name": raw_bus.name,
        "sn_mva": float(trafo.sn_mva), "raw_rating_mva": raw_trafo.rate,
        "raw_bus_card": raw_bus.raw, "raw_trafo_card": raw_trafo.raw,
        "raw_voltage_target_pu": raw_bus.vm,
        "raw_qmax_mvar": raw_bus.qfield, "raw_qmin_mvar": raw_bus.qmin,
        "converted_qmin_mvar": float(net.gen.at[controller, "min_q_mvar"]),
        "converted_qmax_mvar": float(net.gen.at[controller, "max_q_mvar"]),
        "sources": {str(p.relative_to(ROOT)): digest(p) for p in (case_path, dat_path)},
        "saved": saved, "experiments": modes, "checks": checks,
    }


def markdown(report: dict) -> str:
    lines = [
        "# TRAFO-008 负载率诊断", "",
        "结论：常规全网潮流可复现保存结果；异常与 BE 恒压节点的等效无功强相关。",
        "该证据不能证明真实变压器处于同等过载，也不能证明禁用 BE 控制就是正确工程模型。",
        "原始 DAT 和转换后的算例均未修改。", "",
        f"运行环境：pandapower {report['pandapower_version']}。",
        "两种求解算法均从 flat 初值开始，电压相角开启、T 型变压器模型、容差 1e-8 MVA。", "",
        "## 数值对照", "",
        "| 断面 | 方法 | 负载率 % | 高压侧 Q / Mvar | 低压侧电压 pu | 控制节点 Q / Mvar |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    names = {"nr": "牛顿法", "iwamoto_nr": "Iwamoto 牛顿法", "nr_existing_q_limits": "启用现有无功限制", "diagnostic_controller_q_zero": "仅诊断：该 BE 节点 Q=0"}
    for case in report["cases"]:
        for key, value in case["experiments"].items():
            lines.append(f"| {case['scenario']} 点 | {names[key]} | {value['loading_percent']:.6f} | {value['q_hv_mvar']:.6f} | {value['vm_lv_pu']:.6f} | {value['controller_q_mvar']:.6f} |")
    lines += [
        "", "## 原因与边界", "",
        "原始 GXVY2 是 BE 节点（不是普通 PQ 负荷）。BPA 手册规定 BE 可通过未安排无功维持电压；",
        "转换器用恒压 gen 等效，并将其求解范围设为 ±9999 Mvar。启用 enforce_q_lims 后仍使用这些宽限值，因而结果不变。",
        "原卡 Qmax/Qmin 均为 0，不表示 BE 求解时会禁止额外补偿；不能直接把所有 BE 节点改为 PQ 或限为零并称为修复。",
        "20 点时目标电压 0.988 pu，该等效控制吸收约 47.09 Mvar，变压器高压侧 Q 约 48.07 Mvar；额定容量 10 MVA 与原卡一致。",
        "仅在诊断副本中令该节点 Q=0 后，负载率约 55.16%，电压变为约 1.03361 pu。此时原来的恒压要求已改变。",
        "这不是分区算法独有的数值异常；现有证据尚不能代替 BPA 原软件结果对照和现场参数核验。", "",
        "## 正式修复需要核对", "",
        "- BPA 原始 PFO 结果中的未安排无功，以及 BE 恒压设置是否代表实际设备。",
        "- 现场补偿设备的容量、投切状态、无功上下限和电压目标。",
        "- 原始 1000 MVA 基准、变压器阻抗、分接头数据；本次容量校验通过，但这些物理参数仍需工程核实。",
        "- 若该节点应是普通负荷，按数据提供方确认更正节点类型；若确有调压设备，按真实能力建模并重算全网、核查电压和所有过载。", "",
        "## 依据", "",
        "- 本地 `data/demo_area/raw/潮流手册.pdf`：PDF 第 74–75 页（BE、BQ 与未安排无功）；第 95 页（BE 收敛后的处理）。",
        "- [pandapower 潮流参数说明](https://pandapower.readthedocs.io/en/stable/powerflow/ac.html)：负载率的电流口径、enforce_q_lims。",
        "- 同目录 JSON 包含原始卡片、文件 SHA-256、全部实验数值与一致性检查。", "",
        "复现：`/home/y/miniconda3/envs/powerflow/bin/python tools/diagnose_demo_transformer.py`", "",
    ]
    if report.get("partition_checks"):
        lines += [
            "## 分区算法审查", "",
            "当前示范区先调用全网 NR，再用全网 res_bus 的幅值/相角固定各分区边界，局部结果用于误差验证；",
            "负载率来自全网 res_trafo，分区流程不回写全网结果。这不是独立的主配微分区协调求解。", "",
            "| 断面 | 当前环境分区数 | 收敛分区 | 最大电压误差 pu | 最大相角误差 ° | 全网方程残差 MVA |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for check in report["partition_checks"]:
            lines.append(f"| {check['scenario']} 点 | {check['n_parts']} | {check['converged_partitions']} | {check['max_vm_delta_vs_full_pu']:.3e} | {check['max_va_delta_vs_full_deg']:.3e} | {check['power_mismatch_max_mva']:.3e} |")
        lines += [
            "", "本次修正：局部数值重试保持参考计算的无功约束策略；需要 Q 限制或没有 PQ 节点时使用 NR，避免 Iwamoto 的不支持/退化情况；",
            "分区缺失或非有限电压不再被统计为零误差成功。返回结果已明确标记参考边界复算角色。",
            "尚未实现独立的边界电压—功率协调迭代；不能把上述误差作为该能力已实现的证据。", "",
        ]
    return "\n".join(lines)


def check_partitions(label: str) -> dict:
    from gmss.core import _run_demo_distribution_partitions

    net = pp.from_json(str(ROOT / f"data/demo_area/converted/demo_area_{label}_pandapower.json"))
    pp.runpp(net, algorithm="nr", tolerance_mva=1e-8, enforce_q_lims=False, calculate_voltage_angles=True, trafo_model="t")
    saved_bus, saved_trafo = net.res_bus.copy(), net.res_trafo.copy()
    internal = net._ppc["internal"]
    mismatch = internal["V"] * np.conj(internal["Ybus"] @ internal["V"]) - internal["Sbus"]
    residual = np.r_[mismatch[np.r_[internal["pv"], internal["pq"]]].real, mismatch[internal["pq"]].imag]
    result = _run_demo_distribution_partitions(
        net, ROOT / "data/demo_area/converted/demo_area_feeders.json",
        partition_mode="metis", target_buses_per_part=150, local_parallel=False,
    )
    return {
        "scenario": label,
        "n_parts": result["n_parts"],
        "converged_partitions": result["converged_partitions"],
        "failed_partitions": result["failed_partitions"],
        "max_vm_delta_vs_full_pu": result["max_vm_delta_vs_full_pu"],
        "max_va_delta_vs_full_deg": result["max_va_delta_vs_full_deg"],
        "power_mismatch_max_mva": float(np.max(np.abs(residual)) * net.sn_mva),
        "full_network_results_unchanged": saved_bus.equals(net.res_bus) and saved_trafo.equals(net.res_trafo),
        "solution_role": result["solution_role"],
        "independent_partition_solve": result["independent_partition_solve"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/diagnostics")
    parser.add_argument("--check-partitions", action="store_true", help="Also validate actual local partition solves against the full-network result")
    args = parser.parse_args()
    report = {
        "pandapower_version": pp.__version__,
        "counterfactual_is_production_fix": False,
        "cases": [diagnose(label, 7) for label in ("13", "20")],
    }
    if args.check_partitions:
        report["partition_checks"] = []
        for label in ("13", "20"):
            print(f"Checking {label}:00 local partitions...", flush=True)
            report["partition_checks"].append(check_partitions(label))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "trafo_008.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    path = args.output_dir / "trafo_008.md"
    path.write_text(markdown(report), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
