"""Build auditable BPA control-model variants without changing the source cases.

The default hypothesis treats zero-P BE nodes with zero declared Q fields as PQ
nodes, with no undocumented reactive source. BE can represent actual SVCs, so this
is an explicit modelling hypothesis, not a verified correction of field data.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import sys

import networkx as nx
import numpy as np
import pandapower as pp
import pandapower.topology as pptopo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.convert_bpa_to_pandapower import parse_dat


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scheduled_generation(net):
    power = {}
    for table in (net.gen, net.sgen):
        for _, row in table.loc[table.in_service].iterrows():
            bus = int(row.bus)
            power[bus] = power.get(bus, 0.0) + float(row.p_mw * row.scaling)
    return {b: round(p, 10) for b, p in power.items() if abs(p) > 1e-10}


def finite_number(value, name):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be an explicit finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def apply_controls(net, raw_buses, overrides=None):
    """Apply the conservative hypothesis plus explicit per-bus overrides."""
    overrides = overrides or {}
    cards = {b.name: b for b in raw_buses if b.subtype == "E"}
    unknown = set(overrides) - set(cards)
    if unknown:
        raise ValueError(f"Control overrides do not match BE bus names: {sorted(unknown)}")
    changes = []
    unresolved = []
    for name, card in cards.items():
        buses = net.bus.index[(net.bus.bpa_name == name) & np.isclose(net.bus.vn_kv, card.kv)]
        if len(buses) != 1:
            raise ValueError(f"Cannot uniquely match raw bus {name}")
        bus = int(buses[0])
        ids = net.gen.index[(net.gen.bus == bus) & net.gen.in_service]
        if len(ids) != 1:
            raise ValueError(f"Expected one active BE controller at {name}")
        gen_id = int(ids[0])
        gen = net.gen.loc[gen_id].copy()
        rule = overrides.get(name)
        if rule is None:
            if any(abs(value) > 1e-10 for value in (card.pg, card.pmax, card.qfield, card.qmin)):
                unresolved.append({"bus": bus, "name": name, "p_mw": float(gen.p_mw), "reason": "Actual reactive capability/control settings required; original controller retained"})
                continue
            rule = {"mode": "fixed_q", "q_mvar": 0.0, "evidence": "Hypothesis only: no undocumented reactive source at a zero-P, zero-declared-Q BE node"}
        if not str(rule.get("evidence", "")).strip():
            raise ValueError(f"Override for {name} needs an evidence/assumption description")
        mode = rule.get("mode")
        if mode == "fixed_q":
            q = finite_number(rule.get("q_mvar"), f"{name}.q_mvar")
            p = float(gen.p_mw * gen.scaling)
            net.gen.at[gen_id, "in_service"] = False
            if abs(p) > 1e-10 or abs(q) > 1e-10:
                pp.create_sgen(net, bus, p_mw=p, q_mvar=q, name=f"BPA_FIXED_PQ_{name}", controllable=False)
            changes.append({"bus": bus, "name": name, "gen_id": gen_id, "mode": mode, "p_mw": p, "q_mvar": q, "old_voltage_target_pu": float(gen.vm_pu), "evidence": rule["evidence"]})
        elif mode == "limited_pv":
            qmin = finite_number(rule.get("min_q_mvar"), f"{name}.min_q_mvar")
            qmax = finite_number(rule.get("max_q_mvar"), f"{name}.max_q_mvar")
            target = finite_number(rule.get("vm_pu", gen.vm_pu), f"{name}.vm_pu")
            if qmin > qmax or target <= 0:
                raise ValueError(f"Invalid reactive bounds or voltage target at {name}")
            if not np.isclose(gen.scaling, 1.0):
                raise ValueError("Limited-PV overrides currently require generator scaling=1")
            net.gen.loc[gen_id, ["min_q_mvar", "max_q_mvar", "vm_pu"]] = [qmin, qmax, target]
            changes.append({"bus": bus, "name": name, "gen_id": gen_id, "mode": mode, "min_q_mvar": qmin, "max_q_mvar": qmax, "vm_pu": target, "evidence": rule["evidence"]})
        else:
            raise ValueError(f"Unsupported control mode at {name}: {mode!r}")
    pp.set_user_pf_options(net, algorithm="nr", enforce_q_lims=True, trafo_model="t", calculate_voltage_angles=True)
    net["model_repair"] = {"status": "hypothesis_requires_field_validation", "changes": changes, "unresolved_controls": unresolved}
    return changes, unresolved


def solve(net):
    pp.runpp(net, algorithm="nr", init="flat", max_iteration=100, tolerance_mva=1e-8,
             enforce_q_lims=True, calculate_voltage_angles=True, trafo_model="t")
    if not net.converged or not np.isfinite(net.res_bus[["vm_pu", "va_degree"]].to_numpy()).all():
        raise RuntimeError("Repaired case has missing, non-finite or unconverged bus results")


def summary(net):
    def overloads(table, result, name, columns):
        rows = []
        for idx, row in table.loc[table.in_service].iterrows():
            loading = float(result.at[idx, "loading_percent"])
            limit = float(row.get("max_loading_percent", 100.0))
            if not math.isfinite(loading) or not math.isfinite(limit):
                raise RuntimeError(f"Non-finite {name} loading or limit at {idx}")
            if loading > limit + 1e-6:
                rows.append({"index": int(idx), "name": f"{name}-{idx + 1:03d}", "loading_percent": loading, "limit_percent": limit,
                             **{key: float(result.at[idx, key]) for key in columns}})
        return sorted(rows, key=lambda row: -row["loading_percent"])

    trafos = overloads(net.trafo, net.res_trafo, "TRAFO", ["p_hv_mw", "q_hv_mvar"])
    lines = overloads(net.line, net.res_line, "LINE", ["p_from_mw", "q_from_mvar"])
    active = net.bus.index[net.bus.in_service]
    vm = net.res_bus.loc[active, "vm_pu"]
    low = int((vm < net.bus.loc[active, "min_vm_pu"] - 1e-8).sum())
    high = int((vm > net.bus.loc[active, "max_vm_pu"] + 1e-8).sum())
    internal = net._ppc["internal"]
    mismatch = internal["V"] * np.conj(internal["Ybus"] @ internal["V"]) - internal["Sbus"]
    residual = np.r_[mismatch[np.r_[internal["pv"], internal["pq"]]].real, mismatch[internal["pq"]].imag]
    target = net.res_trafo.loc[7]
    return {
        "operating_status": "violations_remaining" if trafos or lines or low or high else "within_supplied_limits",
        "converged": bool(net.converged), "overloaded_trafos": trafos, "overloaded_lines": lines,
        "vm_pu_min": float(vm.min()), "vm_pu_max": float(vm.max()),
        "voltage_violations_supplied_limits": low + high,
        "voltage_screening_095_105": int(((vm < 0.95) | (vm > 1.05)).sum()),
        "power_mismatch_max_mva": float(np.max(np.abs(residual)) * net.sn_mva),
        "trafo_008": {"loading_percent": float(target.loading_percent), "p_hv_mw": float(target.p_hv_mw), "q_hv_mvar": float(target.q_hv_mvar), "vm_lv_pu": float(target.vm_lv_pu)},
    }


def active_transfer_bottlenecks(net, overloaded_lines):
    """Necessary active-power bounds at radial cuts, allowing the supplied Vmax."""
    graph = pptopo.create_nxgraph(net, respect_switches=True)
    references = set(int(b) for b in net.ext_grid.loc[net.ext_grid.in_service, "bus"])
    output = []
    for item in overloaded_lines:
        idx = item["index"]
        row = net.line.loc[idx]
        u, v = int(row.from_bus), int(row.to_bus)
        if graph.number_of_edges(u, v) != 1:
            continue
        cut = graph.copy()
        cut.remove_edge(u, v)
        if nx.has_path(cut, u, v):
            continue
        downstream = set(nx.node_connected_component(cut, v))
        if downstream & references:
            downstream = set(nx.node_connected_component(cut, u))
        if downstream & references:
            continue
        demand = 0.0
        for table, sign in ((net.load, 1), (net.gen, -1), (net.sgen, -1)):
            rows = table.loc[table.bus.isin(downstream) & table.in_service]
            demand += sign * float((rows.p_mw * rows.scaling).sum())
        vmax = max(float(net.bus.at[b, "max_vm_pu"]) for b in (u, v))
        capacity = math.sqrt(3) * max(float(net.bus.at[b, "vn_kv"]) for b in (u, v)) * vmax * float(row.max_i_ka * row.parallel * row.df)
        capacity *= float(row.get("max_loading_percent", 100.0)) / 100
        if demand > capacity + 1e-6:
            output.append({"line_index": idx, "from_name": str(net.bus.at[u, "name"]), "to_name": str(net.bus.at[v, "name"]),
                           "downstream_bus_count": len(downstream), "downstream_net_demand_mw": demand,
                           "active_transfer_upper_bound_mw": capacity, "voltage_upper_bound_pu": vmax,
                           "support_or_transfer_reduction_lower_bound_mw": demand - capacity})
    return output


def repair_case(label, output_dir, overrides):
    source = ROOT / f"data/demo_area/converted/demo_area_{label}_pandapower.json"
    raw = ROOT / f"data/demo_area/raw/{label}点sim2合并anony.dat"
    target = output_dir / f"demo_area_{label}_pandapower.json"
    if target.resolve() == source.resolve():
        raise ValueError("The output directory must not overwrite the original converted case")
    hashes = {str(path.relative_to(ROOT)): sha256(path) for path in (source, raw)}
    baseline = pp.from_json(str(source))
    pp.runpp(baseline, algorithm="nr", init="flat", max_iteration=100, tolerance_mva=1e-8,
             enforce_q_lims=False, calculate_voltage_angles=True, trafo_model="t")
    before = summary(baseline)
    fixed = copy.deepcopy(baseline)
    _, cards, _, _, _ = parse_dat(raw)
    changes, unresolved = apply_controls(fixed, cards, overrides)
    for table in ("load", "line", "trafo", "impedance", "ext_grid"):
        if not fixed[table].equals(baseline[table]):
            raise RuntimeError(f"Unexpected change to {table}")
    if scheduled_generation(fixed) != scheduled_generation(baseline):
        raise RuntimeError("Scheduled active generation must not change")
    solve(fixed)
    after = summary(fixed)
    if after["power_mismatch_max_mva"] > 1e-5:
        raise RuntimeError("Power-flow equation residual exceeds validation tolerance")
    report = {"scenario": label, "field_validated": False, "input_sha256": hashes,
              "changes": changes, "unresolved_controls": unresolved, "before": before, "after": after,
              "active_transfer_bottlenecks": active_transfer_bottlenecks(fixed, after["overloaded_lines"]),
              "preserved": ["source files", "all load P/Q", "scheduled active generation at each bus", "topology", "line and transformer ratings", "impedances", "transformer taps", "external-grid voltage"]}
    fixed["model_repair"]["operating_status"] = after["operating_status"]
    pp.to_json(fixed, str(target))
    for path, expected in hashes.items():
        if sha256(ROOT / path) != expected:
            raise RuntimeError(f"Source file changed unexpectedly: {path}")
    return report


def markdown(reports):
    lines = ["# 主配微示范区控制模型校正记录", "",
             "本目录为明确标注假设的校正算例，不是已核实的现场数据。原始算例未覆盖。",
             "默认假设：原始 P、Pmax、Qmax、Qmin 均为零的 BE 节点不连接未记载的额外无功设备，按 PQ 节点求解。零有功并不能证明没有 SVC/补偿设备，仍需核实。",
             "有实际无功设备时，可通过 settings.json 提供有限 PV 控制或固定 Q 设置重新生成；原卡 Q 字段不被擅自解释为 PQ 无功。", "",
             "| 断面 | TRAFO-008 原负载率 | 校正后负载率 | 剩余过载变压器 | 剩余过载线路 | 电压范围 pu | 保留待定的 BE 控制器 |",
             "| --- | ---: | ---: | ---: | ---: | --- | ---: |"]
    for report in reports:
        a, b = report["before"], report["after"]
        lines.append(f"| {report['scenario']} 点 | {a['trafo_008']['loading_percent']:.2f}% | {b['trafo_008']['loading_percent']:.2f}% | {len(b['overloaded_trafos'])} | {len(b['overloaded_lines'])} | {b['vm_pu_min']:.5f}–{b['vm_pu_max']:.5f} | {len(report['unresolved_controls'])} |")
    lines += ["", "## 为什么不能把全网强行改成正常", "",
              "线路额定电流、变压器容量、拓扑、分接头、负荷与有功出力均保持原值。部分径向支路即使只传有功、忽略损耗，并允许达到原模型电压上限，容量仍不够。",
              "下列数值是必要条件下的缺口下界；实际整改可能需要更多裕度，不等于建议切负荷量。", ""]
    for report in reports:
        for b in report["active_transfer_bottlenecks"]:
            lines.append(f"- {report['scenario']} 点 {b['from_name']} → {b['to_name']}：下游净有功 {b['downstream_net_demand_mw']:.3f} MW；在 {b['voltage_upper_bound_pu']:.2f} pu 下最大可传有功上界 {b['active_transfer_upper_bound_mw']:.3f} MW；至少需 {b['support_or_transfer_reduction_lower_bound_mw']:.3f} MW 的本地支撑/转供调整，或核实并整改容量参数。")
    lines += ["", "## 使用与验证", "",
              "- 两个断面均重新求解全网 AC 潮流；summary.json 保存功率方程残差、全部剩余过载、电压筛查、每个节点的改动依据和输入 SHA-256。",
              "- 电压按原模型 min_vm_pu/max_vm_pu 判限；另附 0.95–1.05 pu 筛查数量，不把这一筛查区间当作统一现场标准。",
              "- `pp.from_json(...)` 读取本目录 JSON 后直接 `pp.runpp(net)` 会使用保存的 NR 与无功限值设置。",
              "- 本结果未宣称全网恢复正常；仍有过载或未知设备能力时，operating_status 为 violations_remaining。",
              "- 获得实际参数后，在 settings.json 的 `13` / `20` 中按母线名称填写 `fixed_q` 的 `q_mvar`，或 `limited_pv` 的 `min_q_mvar`、`max_q_mvar`、`vm_pu`，并记录 evidence。所有值必须是有限数。", "",
              "复现：`/home/y/miniconda3/envs/powerflow/bin/python tools/repair_demo_model.py`", "",
              "带实际参数：`/home/y/miniconda3/envs/powerflow/bin/python tools/repair_demo_model.py --settings path/to/settings.json`", "",
              "[pandapower OPF 约束说明](https://pandapower.readthedocs.io/en/stable/opf/formulation.html)：实际无功边界和设备约束需要明确给定。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/demo_area/corrected")
    args = parser.parse_args()
    settings = json.loads(args.settings.read_text(encoding="utf-8")) if args.settings else {}
    if set(settings) - {"13", "20"}:
        raise ValueError("Settings must be keyed by scenario '13' and/or '20'")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    reports = [repair_case(label, args.output_dir, settings.get(label, {})) for label in ("13", "20")]
    (args.output_dir / "summary.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (args.output_dir / "README.md").write_text(markdown(reports), encoding="utf-8")
    template = {r["scenario"]: {c["name"]: {"mode": "limited_pv", "min_q_mvar": None, "max_q_mvar": None, "vm_pu": None, "evidence": "请填入实际设备参数依据；未填写前不可运行"} for c in r["unresolved_controls"]} for r in reports}
    (args.output_dir / "settings_template.json").write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output_dir / "README.md")


if __name__ == "__main__":
    main()
