"""Read-only dataset checks used by the Shijiazhuang quality report.

Requires the powerflow Python environment and xlrd to inspect original XLS cells.
No source case, model setting or profile is changed.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

import networkx as nx
import numpy as np
import pandapower as pp
import pandapower.topology as topology
import xlrd

ROOT = Path(__file__).resolve().parents[1]


def compact(value):
    return re.sub(r"\s+", "", str(value)).rstrip(".。")


def audit():
    feeder_path = ROOT / "data/demo_area/converted/demo_area_feeders.json"
    profiles = json.loads(feeder_path.read_text())
    rows = [r for f in profiles["feeders"] for r in f["rows"]]
    result = {"scope": "Read-only original-input and converted-model checks; not field validation", "pandapower_version": pp.__version__}
    result["profiles"] = {
        "feeders": len(profiles["feeders"]), "rows": len(rows),
        "time_labels": profiles["time_labels"],
        "label_count": len(profiles["time_labels"]),
        "duplicate_labels": [k for k, n in Counter(profiles["time_labels"]).items() if n > 1],
        "incorrect_profile_lengths": sum(len(r[k]) != 96 for r in rows for k in ("p_mw", "q_mvar")),
        "nonfinite_profile_values": sum(not math.isfinite(v) for r in rows for k in ("p_mw", "q_mvar") for v in r[k]),
        "negative_p_values": sum(v < 0 for r in rows for v in r["p_mw"]),
        "negative_q_values": sum(v < 0 for r in rows for v in r["q_mvar"]),
        "empty_bus_names": sum(not r["bus_name"] for r in rows),
        "missing_line_type_rows": sum(not r["line_type"] for r in rows),
        "nonpositive_length_rows": sum(r["length_km"] <= 0 for r in rows),
        "source_cells": {}, "blank_profile_rows": [],
    }
    original = Counter()
    empty_types = []
    source_numeric_mismatches = []
    for feeder in profiles["feeders"]:
        source = ROOT / "data/demo_area/raw/各10kV馈线" / feeder["feeder_id"]
        sheet = xlrd.open_workbook(str(source)).sheet_by_index(0)
        for i in range(1, sheet.nrows):
            if all(sheet.cell_value(i, j) == "" for j in range(10, 202)):
                result["profiles"]["blank_profile_rows"].append({"source": str(source.relative_to(ROOT)), "excel_row": i + 1, "identity_empty": all(sheet.cell_value(i, j) == "" for j in range(7))})
            if not str(sheet.cell_value(i, 2)).strip():
                empty_types.append({"source": str(source.relative_to(ROOT)), "excel_row": i + 1})
            for j in range(10, 202):
                cell = sheet.cell(i, j)
                if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK) or cell.value == "":
                    original["blank_profile_cells"] += 1
                elif cell.ctype == xlrd.XL_CELL_ERROR:
                    original["excel_error_profile_cells"] += 1
                elif isinstance(cell.value, (int, float)):
                    original["numeric_profile_cells"] += 1
                    r = feeder["rows"][i - 1]
                    key = "p_mw" if j % 2 == 0 else "q_mvar"
                    converted = r[key][(j - 10) // 2]
                    if not math.isclose(float(cell.value), converted, rel_tol=1e-12, abs_tol=1e-12):
                        source_numeric_mismatches.append({"source": str(source.relative_to(ROOT)), "row": i + 1, "column": j + 1})
                else:
                    original["text_profile_cells"] += 1
    result["profiles"]["source_cells"] = dict(original)
    result["profiles"]["numeric_conversion_mismatches"] = source_numeric_mismatches
    result["profiles"]["missing_line_type_samples"] = empty_types[:10]

    directory = ROOT / "data/demo_area/raw/示范区典型10kV馈线目录 - 脱密.xls"
    sheet = xlrd.open_workbook(str(directory)).sheet_by_index(0)
    listed = [str(sheet.cell_value(i, 2)).strip() for i in range(1, sheet.nrows) if str(sheet.cell_value(i, 2)).strip()]
    actual = {compact(f["display_name"]) for f in profiles["feeders"]}
    result["catalogue"] = {
        "listed_rows": len(listed), "unique_names": len({compact(n) for n in listed}),
        "duplicate_names": {k: n for k, n in Counter(compact(n) for n in listed).items() if n > 1},
        "listed_without_same_named_file": sorted({compact(n) for n in listed} - actual),
        "files_without_same_named_catalogue_entry": sorted(actual - {compact(n) for n in listed}),
        "normalization": "Whitespace and trailing full stops removed only; possible renamings are not assumed.",
    }

    result["cases"] = {}
    for label in ("13", "20"):
        source = ROOT / f"data/demo_area/converted/demo_area_{label}_pandapower.json"
        net = pp.from_json(str(source))
        lookup = {}
        for i, b in net.bus.iterrows():
            for key in ("bpa_name", "name"):
                name = str(b.get(key) or "").strip().upper()
                if name:
                    lookup.setdefault(name, int(i))
        matches = []
        missing = []
        per_feeder = []
        for feeder in profiles["feeders"]:
            n_match = 0
            for row_index, row in enumerate(feeder["rows"], start=2):
                candidates = list(row.get("bus_aliases") or [])
                if row["bus_name"] not in candidates:
                    candidates.append(row["bus_name"])
                bus = next((lookup[n.upper()] for n in candidates if n and n.upper() in lookup), None)
                if bus is None:
                    missing.append({"feeder": feeder["display_name"], "source": "data/demo_area/raw/各10kV馈线/" + feeder["feeder_id"], "excel_row": row_index, "candidates": candidates, "empty_bus_name": not row["bus_name"], "p_mw_at_named_hour": row["p_mw"][profiles["time_labels"].index(f"{label}:00")], "q_mvar_at_named_hour": row["q_mvar"][profiles["time_labels"].index(f"{label}:00")]})
                else:
                    matches.append(bus)
                    n_match += 1
            per_feeder.append({"feeder": feeder["display_name"], "matched": n_match, "rows": len(feeder["rows"])})
        graph = topology.create_nxgraph(net, respect_switches=True)
        cc = list(nx.connected_components(graph))
        names = Counter(zip(net.bus.bpa_name, net.bus.vn_kv))
        case = {
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "counts": {k: len(net[k]) for k in ("bus", "line", "trafo", "impedance", "load", "gen", "sgen", "ext_grid", "switch", "shunt", "storage")},
            "connected_components": len(cc),
            "duplicate_bpa_name_voltage_keys": sum(n - 1 for n in names.values() if n > 1),
            "feeder_matching": {"matched_rows": len(matches), "unique_buses": len(set(matches)), "unmatched_rows": missing, "duplicate_target_bus_count": sum(n - 1 for n in Counter(matches).values() if n > 1), "per_feeder": per_feeder},
            "nonpositive_line_ratings": int((net.line.max_i_ka <= 0).sum()),
            "nonpositive_trafo_ratings": int((net.trafo.sn_mva <= 0).sum()),
            "line_length_unique_values": sorted(float(x) for x in net.line.length_km.unique()),
            "negative_reactance_impedance_elements": int((net.impedance.xft_pu < 0).sum()),
            "negative_vk_trafo_elements": int((net.trafo.vk_percent < 0).sum()),
            "voltage_min_limits": sorted(float(x) for x in net.bus.min_vm_pu.unique()),
            "voltage_max_limits": sorted(float(x) for x in net.bus.max_vm_pu.unique()),
            "geo_nonempty_bus_rows": int(net.bus.get("geo", net.bus.name.iloc[:0]).fillna("").astype(str).ne("").sum()) if "geo" in net.bus else 0,
            "geo_nonempty_line_rows": int(net.line.get("geo", net.line.name.iloc[:0]).fillna("").astype(str).ne("").sum()) if "geo" in net.line else 0,
        }
        # Compare the two distinct input sources without overwriting either one.
        differences = []
        time_index = profiles["time_labels"].index(f"{label}:00")
        for feeder in profiles["feeders"]:
            matched_buses = set()
            p_curve = 0.0
            for row in feeder["rows"]:
                candidates = row.get("bus_aliases") or [row["bus_name"]]
                bus = next((lookup[n.upper()] for n in candidates if n and n.upper() in lookup), None)
                if bus is not None:
                    matched_buses.add(bus)
                    p_curve += row["p_mw"][time_index]
            p_model = float((net.load.loc[net.load.bus.isin(matched_buses), "p_mw"] * net.load.loc[net.load.bus.isin(matched_buses), "scaling"]).sum())
            differences.append({"feeder": feeder["display_name"], "dat_model_load_p_mw": p_model, "xls_curve_load_p_mw": p_curve, "difference_mw": p_curve - p_model})
        case["dat_vs_curve_load_totals_at_named_hour"] = differences
        result["cases"][label] = case
    return result


if __name__ == "__main__":
    destination = ROOT / "data/diagnostics/dataset_quality_audit.json"
    result = audit()
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(destination)
    print(json.dumps({"catalogue": result["catalogue"], "profiles": {k: v for k, v in result["profiles"].items() if k != "time_labels"}, "cases": {k: {a: b for a, b in v.items() if a not in ("feeder_matching", "dat_vs_curve_load_totals_at_named_hour")} | {"matched_rows": v["feeder_matching"]["matched_rows"], "unmatched_rows": len(v["feeder_matching"]["unmatched_rows"])} for k, v in result["cases"].items()}}, ensure_ascii=False, indent=2))
