"""Solve supplied DAT cases directly, contracting ideal zero-impedance L cards.

No source data is changed. The project defaults to the user-selected 100 MVA
T-card interpretation; None retains the declared system base for all cards.
BE cards retain unlimited-PV control. This is not PSD-BPA execution.
"""

from __future__ import annotations

from collections import defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import time
from pathlib import Path

import numpy as np
from scipy.sparse import coo_matrix

from .dat import read_dat, stamp, newton, branch_flow, impedance_policy
from .bpa import SolutionOptions, solve_bpa_style, allocate_be_reactive

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRANSFORMER_BASE_MVA = 100.0


def save_csv(path, records):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def validate_bpa_scope(path, buses):
    """Reject unsupported controls instead of silently omitting network cards."""
    lines = path.read_text(encoding="gbk").splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("."):
            continue
        if line.startswith(("(POWERFLOW", "(END)")):
            continue
        if line.startswith("/"):
            command = line.split(",")[0].split("=")[0].rstrip("\\ \t")
            if command not in {"/P_INPUT_LIST", "/P_OUTPUT_LIST", "/MVA_BASE", "/P_ANALYSIS_RPT",
                               "/RPT_SORT", "/NEW_BASE", "/PF_MAP", "/NETWORK_DATA", "/CHECK", "/SOLUTION"}:
                raise ValueError(f"Unsupported DAT command at line {number}: {line}")
            continue
        if line.startswith(">SOL_ITER"):
            continue
        if line[:2] not in ("B ", "BE", "BS", "L ", "T "):
            raise ValueError(f"Unsupported DAT record at line {number}: {line[:20]}")
        if line[2:3].strip():
            raise ValueError(f"Change cards are not implemented (line {number})")
    for bus in buses:
        line = lines[bus["source_line"] - 1]
        bus["raw_qmax_mvar"] = float(line[47:52].strip() or 0)
        bus["raw_qmin_mvar"] = float(line[52:57].strip() or 0)
        if bus["subtype"] in ("E", "S"):
            if not line[57:61].strip():
                raise ValueError("BE/BS requires an explicit voltage target in this implementation")
            if bus["bs"] or bus["gs"]:
                raise ValueError("Controlled-bus shunt allocation requires explicit support")
        elif bus["raw_qmin_mvar"]:
            raise ValueError("Nonzero QMIN on B-blank requires an explicit scheduling rule")


def solve(path, output=None, algorithm="nr", observer=None, include_results=False,
          transformer_base_mva=DEFAULT_TRANSFORMER_BASE_MVA):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    base, buses, branches, instructions = read_dat(path, transformer_base_mva=transformer_base_mva)
    policy = impedance_policy(base, transformer_base_mva)
    if algorithm not in ("nr", "damped-nr", "bpa-style", "partitioned-nr"):
        raise ValueError(f"Unknown algorithm: {algorithm}")
    validate_bpa_scope(path, buses)
    lookup = {b["key"]: i for i, b in enumerate(buses)}
    if len(lookup) != len(buses):
        raise ValueError("Duplicate bus identifiers")
    parent = list(range(len(buses)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    ideal, ordinary = [], []
    for b in branches:
        if b["regularized"]:
            if b["half_y"]:
                raise ValueError("Ideal L card with shunt needs explicit shunt preservation")
            # read_dat flags original R=X=0 cards; undo its small-X fallback.
            b["x"] = 0.0
            b["f"], b["t"] = lookup[b["from"]], lookup[b["to"]]
            f, t = find(b["f"]), find(b["t"])
            if f == t:
                raise ValueError("Ideal-connection cycle: individual ideal flows are not unique")
            if b["from"][1] != b["to"][1]:
                raise ValueError("Ideal L card joins different voltage bases")
            parent[t] = f
            ideal.append(b)
        else:
            ordinary.append(b)
    groups = defaultdict(list)
    for i in range(len(buses)):
        groups[find(i)].append(i)
    memberships = list(groups.values())
    group_index = np.empty(len(buses), dtype=int)
    reduced = []
    anchors = []
    for j, members in enumerate(memberships):
        group_index[members] = j
        controlled = [i for i in members if buses[i]["subtype"] != " "]
        if len(controlled) > 1:
            raise ValueError("Multiple controlled buses in an ideal group need explicit Q allocation")
        anchor = controlled[0] if controlled else members[0]
        anchors.append(anchor)
        b = dict(buses[anchor])
        b["p"] = sum(buses[i]["p"] for i in members)
        b["q"] = sum(buses[i]["q"] for i in members)
        b["gs"] = sum(buses[i]["gs"] for i in members)
        b["bs"] = sum(buses[i]["bs"] for i in members)
        reduced.append(b)
    # C copies group voltages to original nodes; C.T sums nodal currents.
    c = coo_matrix((np.ones(len(buses)), (np.arange(len(buses)), group_index)),
                   shape=(len(buses), len(reduced))).tocsr()
    y_original = stamp(base, buses, ordinary)
    y_reduced = (c.T @ y_original @ c).tocsr()
    model = {'kind': 'model', 'base': base, 'buses': buses, 'branches': branches,
             'reduced': reduced, 'group_index': group_index, 'y_reduced': y_reduced,
             'impedance_policy': policy}
    if observer:
        observer(model)

    def progress(row, value):
        if observer:
            observer({'kind': 'iteration', **row, 'voltage': value[group_index] if value is not None else None, 'base': base})

    solver_details = None
    reference_voltage = None
    numerical_started = time.perf_counter()
    if algorithm == "partitioned-nr":
        from .coupled import solve_coupled
        from .partition import build_partitions
        parts = model.get('parts')
        if parts is None:
            parts = build_partitions(model)
        v_reduced, solver_details = solve_coupled(base, reduced, y_reduced, parts, progress)
        history = solver_details['history']
    elif algorithm == "bpa-style":
        options = SolutionOptions.from_instructions(instructions)
        v_reduced, solver_details = solve_bpa_style(base, reduced, y_reduced, options, progress)
        history = [row["max_mismatch_pu"] for row in solver_details["trace"]]
        # Same raw model, separate pure-NR path: isolate the effect of startup.
        reference_reduced, _ = newton(base, reduced, y_reduced)
        reference_voltage = reference_reduced[group_index]
    else:
        v_reduced, history = newton(base, reduced, y_reduced, observer=progress, damped=algorithm == "damped-nr")
    numerical_solve_ms = (time.perf_counter() - numerical_started) * 1000
    voltage = v_reduced[group_index]
    network = voltage * np.conj(y_original @ voltage) * base
    specified = np.array([complex(b["p"], b["q"]) for b in buses])
    allocations = specified.copy()
    for members, anchor in zip(memberships, anchors):
        total = sum(network[i] for i in members)
        others = sum(specified[i] for i in members if i != anchor)
        kind = buses[anchor]["subtype"]
        if kind == "S":
            allocations[anchor] = total - others
        elif kind == "E":
            allocations[anchor] = complex(specified[anchor].real, (total - others).imag)
    kcl_remaining = allocations - network
    adjacency = defaultdict(list)
    for edge, b in enumerate(ideal):
        adjacency[b["f"]].append((b["t"], edge))
        adjacency[b["t"]].append((b["f"], edge))
    ideal_s = np.zeros(len(ideal), dtype=complex)
    for members, anchor in zip(memberships, anchors):
        if len(members) == 1:
            continue
        traversal, previous, incoming = [anchor], {anchor: None}, {}
        for node in traversal:
            for neighbor, edge in adjacency[node]:
                if neighbor != previous[node]:
                    previous[neighbor], incoming[neighbor] = node, edge
                    traversal.append(neighbor)
        subtotal = {i: kcl_remaining[i] for i in members}
        for node in reversed(traversal[1:]):
            edge = incoming[node]
            b = ideal[edge]
            ideal_s[edge] = subtotal[node] if node == b["f"] else -subtotal[node]
            subtotal[previous[node]] += subtotal[node]
    recovered = network.copy()
    flows = [branch_flow(b, voltage, base) for b in ordinary]
    for b, power in zip(ideal, ideal_s):
        recovered[b["f"]] += power
        recovered[b["t"]] -= power
        rate = b["rate_mva"]
        current_equiv = max(abs(power) / abs(voltage[b["f"]]), abs(power) / abs(voltage[b["t"]]))
        flows.append({"kind": b["kind"], "source_line": b["source_line"],
                      "from_name": b["from"][0], "from_kv": b["from"][1],
                      "to_name": b["to"][0], "to_kv": b["to"][1],
                      "rate_mva": rate, "r_pu": 0.0, "x_pu": 0.0, "tap_ratio_pu": 1.0,
                      "input_r_pu": b["input_r_pu"], "input_x_pu": b["input_x_pu"],
                      "impedance_base_mva": b["impedance_base_mva"], "impedance_scale": b["impedance_scale"],
                      "p_from_mw": float(power.real), "q_from_mvar": float(power.imag),
                      "p_to_mw": float(-power.real), "q_to_mvar": float(-power.imag),
                      "current_loading_percent": float(current_equiv / rate * 100) if rate else None,
                      "apparent_power_loading_percent": float(abs(power) / rate * 100) if rate else None,
                      "active_power_ratio_percent": float(abs(power.real) / rate * 100) if rate else None})
    flows.sort(key=lambda r: r["source_line"])
    if algorithm == 'bpa-style':
        for row in flows:
            for quantity in ("current", "apparent_power"):
                loading = row[quantity + "_loading_percent"]
                row[quantity + "_overload_excess_percent"] = max(0.0, loading - 100) if loading is not None else None
    mismatch = recovered - allocations
    equation_residual = max(abs(mismatch.real).max(), abs(mismatch.imag).max())
    # Test KCL at every original bus, including recovered ideal-branch flows.
    if equation_residual > 1e-4:
        raise RuntimeError(f"Original-bus KCL residual too large: {equation_residual} MVA")
    bus_rows = [{"name": b["key"][0], "base_kv": b["key"][1], "type": b["subtype"].strip() or "PQ",
                 "source_line": b["source_line"], "vm_pu": float(abs(voltage[i])),
                 "va_degree": float(np.rad2deg(np.angle(voltage[i]))),
                 "net_p_injection_mw": float(recovered[i].real),
                 "net_q_injection_mvar": float(recovered[i].imag),
                 "be_q_mvar": float(recovered[i].imag + b["load_q"]) if b["subtype"] == "E" else None}
                for i, b in enumerate(buses)]
    if algorithm == 'bpa-style':
        for bus, row in zip(buses, bus_rows):
            row.update({"raw_qmax_mvar": bus["raw_qmax_mvar"], "raw_qmin_mvar": bus["raw_qmin_mvar"],
                        "required_voltage_support_q_mvar": None, "allocated_q_generation_mvar": None,
                        "unscheduled_q_mvar": None, "q_allocation_status": "not a BE bus"})
            if bus["subtype"] == "E":
                row["required_voltage_support_q_mvar"] = row["be_q_mvar"]
                row.update(allocate_be_reactive(row["be_q_mvar"], bus["raw_qmax_mvar"], bus["raw_qmin_mvar"]))
    source_label = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    summary = {"numerical_solve_ms": numerical_solve_ms, "source": source_label, "source_sha256": digest,
               "computed_at_utc": datetime.now(timezone.utc).isoformat(),
               "solver": solver_details["solver"] if solver_details else ("damped polar Newton-Raphson" if algorithm == "damped-nr" else "standalone polar Newton-Raphson"), "PSD_BPA_executed": False,
               "base_mva": base, "impedance_policy": policy,
               "bus_count": len(buses), "branch_count": len(branches),
               "ideal_zero_impedance_L_cards_contracted": len(ideal), "reduced_bus_count": len(reduced),
               "BE_control": "unlimited PV at raw voltage target", "raw_solution_instruction": instructions,
               "converged": True,
               "iterations": solver_details["newton_iterations"] if solver_details else len(history) - 1,
               "residual_history_pu": history,
               "original_bus_equation_residual_mva": float(equation_residual),
               "voltage_min": min(bus_rows, key=lambda r: r["vm_pu"]),
               "voltage_max": max(bus_rows, key=lambda r: r["vm_pu"]),
               "slack_buses": [r for r in bus_rows if r["type"] == "S"],
               "target_GXVS2_GXVY2": next((r for r in flows if r["from_name"] == "GXVS2" and r["to_name"] == "GXVY2"), None)}
    for kind in ("T", "L"):
        rated = [r for r in flows if r["kind"] == kind and r["current_loading_percent"] is not None]
        summary[kind + "_loading"] = {
            "count": sum(r["kind"] == kind for r in flows), "rated_count": len(rated),
            "max_current_loading_percent": max((r["current_loading_percent"] for r in rated), default=None),
            "max_apparent_power_loading_percent": max((r["apparent_power_loading_percent"] for r in rated), default=None),
            "above_100_percent": sum(r["current_loading_percent"] > 100 for r in rated),
            "above_200_percent": sum(r["current_loading_percent"] > 200 for r in rated),
            "top_5": sorted(rated, key=lambda r: r["current_loading_percent"], reverse=True)[:5]}
    if reference_voltage is not None:
        comparison = {"max_complex_voltage_difference_pu": float(max(abs(voltage - reference_voltage))),
                      "max_ordinary_branch_end_power_difference_mva": 0.0,
                      "target_loading_difference_percentage_points": 0.0}
        for branch in ordinary:
            current = branch_flow(branch, voltage, base)
            reference = branch_flow(branch, reference_voltage, base)
            for side in ("from", "to"):
                difference = complex(current[f"p_{side}_mw"] - reference[f"p_{side}_mw"],
                                     current[f"q_{side}_mvar"] - reference[f"q_{side}_mvar"])
                comparison["max_ordinary_branch_end_power_difference_mva"] = max(
                    comparison["max_ordinary_branch_end_power_difference_mva"], float(abs(difference)))
            if branch["from"][0] == "GXVS2" and branch["to"][0] == "GXVY2":
                comparison["target_loading_difference_percentage_points"] = abs(
                    current["current_loading_percent"] - reference["current_loading_percent"])
        if comparison["max_complex_voltage_difference_pu"] > 1e-6 or comparison["max_ordinary_branch_end_power_difference_mva"] > 1e-3:
            raise RuntimeError(f"BPA-style and pure Newton reached different results: {comparison}")
        be_rows = [row for row in bus_rows if row["type"] == "E"]
        summary.update({"algorithm": algorithm, "solver_details": solver_details,
                        "decoupled_half_steps_executed": sum(row["stage"] in ("decoupled_P", "decoupled_Q") for row in solver_details["trace"]),
                        "pure_newton_comparison": comparison,
                        "BE_reactive_allocation": {
                            "BE_count": len(be_rows),
                            "unresolved_raw_Q_range_count": sum(row["unscheduled_q_mvar"] is None for row in be_rows),
                            "unscheduled_above_0_5_mvar_count": sum(abs(row["unscheduled_q_mvar"] or 0) > 0.5 for row in be_rows),
                            "target_GXVY2": next((row for row in be_rows if row["name"] == "GXVY2"), None),
                            "interpretation": "Q required to hold V is not proof of installed reactive capability; raw Q ranges are not field validated"},
                        "controls": "B=PQ; BE=fixed P,V with unscheduled Q allowed; BS=fixed V,angle; fixed raw T taps; no other control cards in supplied cases",
                        "loading_definitions": {
                            "current_loading_percent": "100 * max(|S_from|/|V_from_pu|, |S_to|/|V_to_pu|) / rate_MVA",
                            "apparent_power_loading_percent": "100 * max(|S_from|, |S_to|) / rate_MVA",
                            "overload_excess_percent": "max(loading_percent - 100, 0); differs from loading percent"}})
    if solver_details is not None:
        summary['solver_details'] = solver_details
    summary['partitioning'] = model.get('partitioning')
    name = path.name.split("点")[0]
    if output is not None:
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        save_csv(output / f"raw_{name}_bus.csv", bus_rows)
        save_csv(output / f"raw_{name}_branch.csv", flows)
    if include_results:
        summary['bus_results'] = bus_rows
        summary['branch_results'] = flows
    summary['algorithm'] = algorithm
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    return summary

