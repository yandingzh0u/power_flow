"""Independently stamp DAT equations and solve polar Newton power flow.

This is a comparison of the equations currently inferred from the supplied DAT,
NOT a run of PSD-BPA. No source case, control setting or rated capacity changes.
The standalone sparse Newton iteration does not use pandapower's PF/converter.
Pandapower is used only afterwards as the separately labelled reference run.
"""

from __future__ import annotations

import hashlib
import json
import argparse
import csv
from pathlib import Path
import re

import numpy as np
from scipy.sparse import bmat, coo_matrix, diags
from scipy.sparse.linalg import spsolve

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/diagnostics/bpa_crosscheck"


def read_dat(path):
    text = path.read_text(encoding="gbk")
    match = re.search(r"/MVA_BASE\s*=\s*([0-9.]+)", text)
    if not match:
        raise ValueError("System MVA base must be explicitly supplied")
    base = float(match.group(1))
    buses, branches, instructions = [], [], []
    def part(line, start, end):
        return line.ljust(end)[start - 1:end].strip()
    def num(line, start, end, decimals=0):
        value = part(line, start, end)
        if not value:
            return 0.0
        # Follow the documented Fortran fixed-format implicit decimal convention.
        return float(value) if "." in value or "e" in value.lower() else float(value) / 10 ** decimals
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.startswith(">SOL_ITER"):
            instructions.append(line)
        if line[:2] in ("B ", "BE", "BS"):
            subtype = line[1]
            bus = {"key": (part(line, 7, 14), num(line, 15, 18)), "subtype": subtype,
                   "p": num(line, 43, 47) - num(line, 21, 25),
                   "q": (num(line, 48, 52) if subtype == " " else 0.0) - num(line, 26, 30),
                   "load_q": num(line, 26, 30), "gs": num(line, 31, 34), "bs": num(line, 35, 38),
                   "vm": num(line, 58, 61, 3) or 1.0, "va": num(line, 62, 65, 1) if subtype == "S" else 0.0,
                   "source_line": line_number}
            buses.append(bus)
        elif line[:2] in ("L ", "T "):
            kind = line[0]
            f = (part(line, 7, 14), num(line, 15, 18))
            t = (part(line, 20, 27), num(line, 28, 31))
            tap = 1.0
            if kind == "T":
                tap1, tap2 = num(line, 63, 67, 2), num(line, 68, 72, 2)
                if tap1 and tap2:
                    tap = (tap1 / f[1]) / (tap2 / t[1])
                rate = num(line, 34, 37)
            else:
                rate = np.sqrt(3) * f[1] * num(line, 34, 37) / 1000
            r, x = num(line, 39, 44, 5), num(line, 45, 50, 5)
            regularized = kind == "L" and r == 0 and x == 0
            if regularized:
                x = 1e-6
            g, b = num(line, 51, 56, 5), num(line, 57, 62, 5)
            # All transformer shunt fields in these supplied cases are zero.
            # Refuse to silently assume a T/pi split for future nonzero fields.
            if kind == "T" and (g != 0 or b != 0):
                raise ValueError("Nonzero transformer shunt needs a documented model")
            branches.append({"kind": kind, "from": f, "to": t, "r": r, "x": x,
                             "half_y": complex(g, b), "tap": tap, "rate_mva": rate,
                             "regularized": regularized, "source_line": line_number})
    if any(line.startswith(("R", "X", "BQ", "BG", "BX", "E ", "L+")) for line in text.splitlines()):
        raise ValueError("Additional control/branch cards require explicit support")
    return base, buses, branches, instructions


def stamp(base, buses, branches):
    lookup = {bus["key"]: index for index, bus in enumerate(buses)}
    rr, cc, vv = [], [], []
    for branch in branches:
        f, t = lookup[branch["from"]], lookup[branch["to"]]
        y = 1 / complex(branch["r"], branch["x"])
        half = branch["half_y"]
        tap = branch["tap"]
        yff, yft, ytf, ytt = (y + half) / tap ** 2, -y / tap, -y / tap, y + half
        rr += [f, f, t, t]
        cc += [f, t, f, t]
        vv += [yff, yft, ytf, ytt]
        branch.update({"f": f, "t": t, "yff": yff, "yft": yft, "ytf": ytf, "ytt": ytt})
    for i, bus in enumerate(buses):
        rr.append(i)
        cc.append(i)
        vv.append(complex(bus["gs"], bus["bs"]) / base)
    return coo_matrix((vv, (rr, cc)), shape=(len(buses), len(buses))).tocsr()


def newton(base, buses, ybus):
    pq = np.array([i for i, b in enumerate(buses) if b["subtype"] == " "], dtype=int)
    non_slack = np.array([i for i, b in enumerate(buses) if b["subtype"] != "S"], dtype=int)
    vm = np.array([1.0 if b["subtype"] == " " else b["vm"] for b in buses])
    va = np.array([np.deg2rad(b["va"]) for b in buses])
    specified = np.array([complex(b["p"], b["q"]) / base for b in buses])
    history = []
    for iteration in range(30):
        voltage = vm * np.exp(1j * va)
        current = ybus @ voltage
        mismatch = voltage * np.conj(current) - specified
        residual = np.r_[mismatch[non_slack].real, mismatch[pq].imag]
        norm = float(np.max(np.abs(residual)))
        history.append(norm)
        if norm < 1e-8:
            return voltage, history
        dv, di = diags(voltage), diags(current)
        unit = diags(voltage / vm)
        da = 1j * dv @ (di - ybus @ dv).conj()
        dm = dv @ (ybus @ unit).conj() + di.conj() @ unit
        jacobian = bmat([[da[non_slack][:, non_slack].real, dm[non_slack][:, pq].real],
                         [da[pq][:, non_slack].imag, dm[pq][:, pq].imag]], format="csc")
        step = spsolve(jacobian, -residual)
        va[non_slack] += step[:len(non_slack)]
        vm[pq] += step[len(non_slack):]
        if not np.isfinite(step).all() or (vm <= 0).any():
            raise RuntimeError("Newton iteration left the admissible voltage range")
    raise RuntimeError(f"Standalone NR did not converge: {history}")


def branch_flow(branch, voltage, base):
    f, t = branch["f"], branch["t"]
    i_from = branch["yff"] * voltage[f] + branch["yft"] * voltage[t]
    i_to = branch["ytf"] * voltage[f] + branch["ytt"] * voltage[t]
    s_from = voltage[f] * np.conj(i_from) * base
    s_to = voltage[t] * np.conj(i_to) * base
    rate = branch["rate_mva"]
    return {
        "kind": branch["kind"], "source_line": branch["source_line"],
        "from_name": branch["from"][0], "from_kv": branch["from"][1],
        "to_name": branch["to"][0], "to_kv": branch["to"][1],
        "rate_mva": rate, "r_pu": branch["r"], "x_pu": branch["x"],
        "tap_ratio_pu": branch["tap"],
        "p_from_mw": float(s_from.real), "q_from_mvar": float(s_from.imag),
        "p_to_mw": float(s_to.real), "q_to_mvar": float(s_to.imag),
        "current_loading_percent": float(max(abs(i_from), abs(i_to)) * base / rate * 100) if rate else None,
        "apparent_power_loading_percent": float(max(abs(s_from), abs(s_to)) / rate * 100) if rate else None,
        "active_power_ratio_percent": float(max(abs(s_from.real), abs(s_to.real)) / rate * 100) if rate else None,
    }


def verify(label, raw_dir=None, converted_dir=None, output_dir=None):
    import pandapower as pp
    raw_dir = Path(raw_dir) if raw_dir is not None else ROOT / "data/demo_area/raw"
    converted_dir = Path(converted_dir) if converted_dir is not None else ROOT / "data/demo_area/converted"
    source = raw_dir / f"{label}点sim2合并anony.dat"
    converted = converted_dir / f"demo_area_{label}_pandapower.json"
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, converted)}
    base, buses, branches, instructions = read_dat(source)
    ybus = stamp(base, buses, branches)
    voltage, history = newton(base, buses, ybus)
    all_flows = [branch_flow(b, voltage, base) for b in branches]
    flow_groups = {
        "all_T_cards": [row for row in all_flows if row["kind"] == "T"],
        "T_cards_with_nonunity_tap": [row for row in all_flows if row["kind"] == "T" and not np.isclose(row["tap_ratio_pu"], 1.0, rtol=0, atol=1e-9)],
        "T_cards_with_unity_tap": [row for row in all_flows if row["kind"] == "T" and np.isclose(row["tap_ratio_pu"], 1.0, rtol=0, atol=1e-9)],
        "L_cards": [row for row in all_flows if row["kind"] == "L"],
    }
    branch_summary = {}
    for name, rows in flow_groups.items():
        rated = [row for row in rows if row["current_loading_percent"] is not None]
        ordered = sorted(rated, key=lambda row: row["current_loading_percent"], reverse=True)
        branch_summary[name] = {
            "count": len(rows), "rated_count": len(rated),
            "above_100_percent": sum(row["current_loading_percent"] > 100 for row in rated),
            "above_200_percent": sum(row["current_loading_percent"] > 200 for row in rated),
            "max_current_loading_percent": ordered[0]["current_loading_percent"] if ordered else None,
            "max_apparent_power_loading_percent": max((row["apparent_power_loading_percent"] for row in rated), default=None),
            "top_5": ordered[:5],
        }
    target = next(b for b in branches if b["kind"] == "T" and b["from"][0] == "GXVS2" and b["to"][0] == "GXVY2")
    f, t = target["f"], target["t"]
    i_from = target["yff"] * voltage[f] + target["yft"] * voltage[t]
    i_to = target["ytf"] * voltage[f] + target["ytt"] * voltage[t]
    s_from, s_to = voltage[f] * np.conj(i_from) * base, voltage[t] * np.conj(i_to) * base
    injection = voltage * np.conj(ybus @ voltage) * base
    standalone = {"converged": True, "iterations": len(history) - 1, "residual_history_pu": history,
                  "equation_residual_mva": history[-1] * base,
                  "p_hv_mw": float(s_from.real), "q_hv_mvar": float(s_from.imag),
                  "p_lv_mw": float(s_to.real), "q_lv_mvar": float(s_to.imag),
                  "be_q_mvar": float(injection[t].imag + buses[t]["load_q"]),
                  "current_loading_percent": float(max(abs(i_from), abs(i_to)) * base / target["rate_mva"] * 100),
                  "apparent_power_loading_percent": float(max(abs(s_from), abs(s_to)) / target["rate_mva"] * 100),
                  "active_power_ratio_percent": float(max(abs(s_from.real), abs(s_to.real)) / target["rate_mva"] * 100),
                  "vm_hv_pu": float(abs(voltage[f])), "vm_lv_pu": float(abs(voltage[t]))}
    net = pp.from_json(str(converted))
    pp.runpp(net, algorithm="nr", init="flat", max_iteration=80, tolerance_mva=1e-8,
             enforce_q_lims=False, calculate_voltage_angles=True, trafo_model="t")
    bus_indices = [int(net.bus.index[(net.bus.bpa_name == b["key"][0]) & np.isclose(net.bus.vn_kv, b["key"][1])][0]) for b in buses]
    pp_voltage = net.res_bus.loc[bus_indices, "vm_pu"].to_numpy() * np.exp(1j * np.deg2rad(net.res_bus.loc[bus_indices, "va_degree"].to_numpy()))
    internal_order = net._pd2ppc_lookups["bus"][bus_indices]
    pp_ybus = net._ppc["internal"]["Ybus"][internal_order][:, internal_order]
    delta_y = pp_ybus - ybus
    # The current from_ppc importer represents unity-ratio, different-voltage T
    # cards as impedance elements. Check all of them rather than just res_trafo.
    element_counters = {"line": 0, "trafo": 0, "impedance": 0}
    max_branch_power_difference = 0.0
    for row in all_flows:
        element = "line" if row["kind"] == "L" else ("impedance" if np.isclose(row["tap_ratio_pu"], 1.0, rtol=0, atol=1e-9) else "trafo")
        idx = element_counters[element]
        element_counters[element] += 1
        from_col, to_col = ("hv_bus", "lv_bus") if element == "trafo" else ("from_bus", "to_bus")
        from_bus, to_bus = int(net[element].at[idx, from_col]), int(net[element].at[idx, to_col])
        assert net.bus.at[from_bus, "bpa_name"] == row["from_name"]
        assert net.bus.at[to_bus, "bpa_name"] == row["to_name"]
        suffixes = ("hv", "lv") if element == "trafo" else ("from", "to")
        for side, suffix in zip(("from", "to"), suffixes):
            p_delta = row[f"p_{side}_mw"] - net[f"res_{element}"].at[idx, f"p_{suffix}_mw"]
            q_delta = row[f"q_{side}_mvar"] - net[f"res_{element}"].at[idx, f"q_{suffix}_mvar"]
            max_branch_power_difference = max(max_branch_power_difference, float(abs(complex(p_delta, q_delta))))
        row["pandapower_element_type"] = element
        row["pandapower_element_index"] = idx
    result = {
        "scenario": label, "PSD_BPA_executed": False,
        "source_sha256": hashes, "raw_solution_instruction": instructions,
        "assumptions": ["BE is unlimited PV at raw voltage target", "No extra curve writeback or microgrid overlay", "Both transformer shunt fields are zero", "Zero-impedance L cards use the existing converter's 1e-6 pu regularization"],
        "zero_impedance_regularization_count": sum(b["regularized"] for b in branches),
        "branch_summary": branch_summary,
        "standalone_polar_NR": standalone,
        "pandapower_NR": {"converged": bool(net.converged), "loading_percent": float(net.res_trafo.at[7, "loading_percent"]), "p_hv_mw": float(net.res_trafo.at[7, "p_hv_mw"]), "q_hv_mvar": float(net.res_trafo.at[7, "q_hv_mvar"]),
                          "max_trafo_loading_percent": float(net.res_trafo.loading_percent.max()),
                          "max_line_loading_percent": float(net.res_line.loading_percent.max())},
        "comparisons": {"max_ybus_absolute_difference_pu": float(max(abs(delta_y.data), default=0)),
                        "max_ybus_difference_relative_to_largest_entry": float(max(abs(delta_y.data), default=0) / max(abs(ybus.data))),
                        "max_complex_voltage_difference_pu": float(max(abs(voltage - pp_voltage))),
                        "all_branch_end_power_max_complex_difference_mva": max_branch_power_difference,
                        "verified_branch_element_counts": element_counters,
                        "target_loading_difference_percentage_points": float(abs(standalone["current_loading_percent"] - net.res_trafo.at[7, "loading_percent"]))},
    }
    if result["comparisons"]["max_complex_voltage_difference_pu"] > 1e-6 or result["comparisons"]["target_loading_difference_percentage_points"] > 1e-3:
        raise RuntimeError(f"Standalone and converted equations differ: {result['comparisons']}")
    if max_branch_power_difference > 1e-3:
        raise RuntimeError(f"Branch powers differ by {max_branch_power_difference} MVA")
    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / f"raw_{label}_branch_flows.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(all_flows[0]))
            writer.writeheader()
            writer.writerows(all_flows)
    for name, digest in hashes.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/demo_area/raw")
    parser.add_argument("--converted-dir", type=Path, default=ROOT / "data/demo_area/converted")
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    OUT = args.output_dir.resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    results = [verify(label, args.raw_dir.resolve(), args.converted_dir.resolve(), OUT) for label in ("13", "20")]
    (OUT / "independent_newton.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    lines = ["# BPA软件与牛顿算法的对照核查", "",
             "BPA在本项目指PSD-BPA电力系统分析软件，不是与牛顿法并列的算法。项目原始DAT明确配置先做2次P-Q分解，再进入牛顿法；手册PDF第7、21页说明相应算法选项。", "",
             "本机未找到PSD-BPA可执行程序或原PFO结果，以下未运行BPA，不能据此判断对方BPA结果正确与否。独立实现从原始固定列DAT读取、组装Ybus并进行极坐标牛顿迭代；其后单独用pandapower做参考计算。此验证覆盖当前文件所描述的数学模型，不证明PSD-BPA软件的全部控制语义已经一致。", "",
             "| 断面 | 独立NR电流负载率 % | pandapower NR % | 视在功率/容量 % | 有功/容量 % |", "| --- | ---: | ---: | ---: | ---: |"]
    for r in results:
        s = r["standalone_polar_NR"]
        lines.append(f"| {r['scenario']}点 | {s['current_loading_percent']:.6f} | {r['pandapower_NR']['loading_percent']:.6f} | {s['apparent_power_loading_percent']:.6f} | {s['active_power_ratio_percent']:.6f} |")
    lines += ["", "结论与边界：", "",
              "1. 489.26%可由当前原始DAT解释出的方程独立复现，不依赖分区算法，也不是已保存结果未更新。两个NR实现对同一数学模型一致，不等于与原BPA软件完成交叉验证。",
              "2. 电流与视在功率口径只造成小幅差异，不能解释约500%与正常负载率之间的巨大差别。只看有功/容量会忽略大量无功，不能替代电气负载率。",
              "3. 原MVA_BASE=1000在手册PDF第20页有明确支持；本次未找到足够证据将其改成100或将设备阻抗任意缩放。",
              "4. 应继续核对相同DAT/断面、BPA版本和控制选项，特别是BE的已安排/未安排无功；同时核对单台变压器与整站汇总口径。未经这些对照，不能宣布数据集本身错误或真实设备过载。",
              "", "关键对照对象：GXVS2→GXVY2，原T卡10 MVA。需要双方的P、Q、两侧电压、分接值、未安排无功及负载率定义；对照应保留原BE，不能用改为PQ的假设模型冒充同模型验证。",
              "", "复现：/home/y/miniconda3/envs/powerflow/bin/python tools/verify_bpa_newton_comparison.py。完整矩阵、残差和输入哈希的比较统计见independent_newton.json。", ""]
    (OUT / "BPA与牛顿法核查说明.md").write_text("\n".join(lines))
    print(json.dumps(results, ensure_ascii=False, indent=2))
