"""Solve supplied DAT cases directly, contracting ideal zero-impedance L cards.

No converted network is read and no source data is changed. BE cards retain their
unlimited-PV interpretation. This is an AC Newton solve, not PSD-BPA execution.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.sparse import coo_matrix

from verify_bpa_newton_comparison import read_dat, stamp, newton, branch_flow

ROOT = Path(__file__).resolve().parents[1]


def save_csv(path, records):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def solve(path, output):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    base, buses, branches, instructions = read_dat(path)
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
    v_reduced, history = newton(base, reduced, y_reduced)
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
                      "p_from_mw": float(power.real), "q_from_mvar": float(power.imag),
                      "p_to_mw": float(-power.real), "q_to_mvar": float(-power.imag),
                      "current_loading_percent": float(current_equiv / rate * 100) if rate else None,
                      "apparent_power_loading_percent": float(abs(power) / rate * 100) if rate else None,
                      "active_power_ratio_percent": float(abs(power.real) / rate * 100) if rate else None})
    flows.sort(key=lambda r: r["source_line"])
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
    summary = {"source": str(path.relative_to(ROOT)), "source_sha256": digest,
               "computed_at_utc": datetime.now(timezone.utc).isoformat(),
               "solver": "standalone polar Newton-Raphson", "PSD_BPA_executed": False,
               "base_mva": base, "bus_count": len(buses), "branch_count": len(branches),
               "ideal_zero_impedance_L_cards_contracted": len(ideal), "reduced_bus_count": len(reduced),
               "BE_control": "unlimited PV at raw voltage target", "raw_solution_instruction": instructions,
               "converged": True, "iterations": len(history) - 1, "residual_history_pu": history,
               "original_bus_equation_residual_mva": float(equation_residual),
               "voltage_min": min(bus_rows, key=lambda r: r["vm_pu"]),
               "voltage_max": max(bus_rows, key=lambda r: r["vm_pu"]),
               "slack_buses": [r for r in bus_rows if r["type"] == "S"],
               "target_GXVS2_GXVY2": next(r for r in flows if r["from_name"] == "GXVS2" and r["to_name"] == "GXVY2")}
    for kind in ("T", "L"):
        rated = [r for r in flows if r["kind"] == kind and r["current_loading_percent"] is not None]
        summary[kind + "_loading"] = {
            "count": sum(r["kind"] == kind for r in flows), "rated_count": len(rated),
            "above_100_percent": sum(r["current_loading_percent"] > 100 for r in rated),
            "above_200_percent": sum(r["current_loading_percent"] > 200 for r in rated),
            "top_5": sorted(rated, key=lambda r: r["current_loading_percent"], reverse=True)[:5]}
    name = path.name.split("点")[0]
    save_csv(output / f"raw_{name}_bus.csv", bus_rows)
    save_csv(output / f"raw_{name}_branch.csv", flows)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "raw_dataset/extracted")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/diagnostics/raw_dat_powerflow")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = [solve(args.raw_dir.resolve() / f"{hour}点sim2合并anony.dat", output) for hour in ("13", "20")]
    (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
