"""Build browser-friendly Shijiazhuang topology snapshots from the checked-in cases."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PCC_BUSES = [125, 124, 88, 110, 36, 84]


def frame(table: dict) -> list[dict]:
    payload = json.loads(table["_object"])
    return [dict(zip(payload["columns"], row), _index=int(index))
            for index, row in zip(payload["index"], payload["data"])]


def serialise_frame(obj: dict, key: str) -> list[dict]:
    return frame(obj[key]) if key in obj else []


def round_num(value, digits=6):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return value


def merge_partition_overflow(parts: list[dict], lines: list[dict], target: int) -> list[dict]:
    """Keep the displayed membership count aligned with the saved snapshot.

    The METIS implementation can produce a different number of tiny groups
    across dependency versions. When that happens, merge the smallest groups
    into their most connected neighbour instead of showing a contradictory
    count in the UI. The saved error metrics remain the source of truth.
    """
    if len(parts) <= target:
        return parts
    groups = [{"part": p, "buses": set(int(x) for x in p.get("internal_buses", []))} for p in parts]
    while len(groups) > target:
        tiny_idx = min(range(len(groups)), key=lambda i: len(groups[i]["buses"]))
        tiny = groups[tiny_idx]["buses"]
        best_idx, best_score = None, -1
        for j, candidate in enumerate(groups):
            if j == tiny_idx:
                continue
            score = 0
            for line in lines:
                if (line["from_bus"] in tiny and line["to_bus"] in candidate["buses"]) or (line["to_bus"] in tiny and line["from_bus"] in candidate["buses"]):
                    score += 1
            if score > best_score:
                best_idx, best_score = j, score
        if best_idx is None:
            break
        target_group = groups[best_idx]
        target_group["buses"].update(tiny)
        target_group["part"].setdefault("internal_buses", [])
        target_group["part"]["internal_buses"] = sorted(target_group["buses"])
        target_group["part"]["boundary_buses"] = sorted(set(target_group["part"].get("boundary_buses", [])) | set(groups[tiny_idx]["part"].get("boundary_buses", [])))
        target_group["part"]["boundary_main_buses"] = sorted(set(target_group["part"].get("boundary_main_buses", [])) | set(groups[tiny_idx]["part"].get("boundary_main_buses", [])))
        target_group["part"]["boundary_trafo_indices"] = sorted(set(target_group["part"].get("boundary_trafo_indices", [])) | set(groups[tiny_idx]["part"].get("boundary_trafo_indices", [])))
        del groups[tiny_idx]
    return [g["part"] for g in groups]


def build_scenario(label: str, raw: dict | None = None, result: dict | None = None) -> dict:
    raw = raw or json.loads((ROOT / f"data/demo_area/converted/demo_area_{label}_pandapower.json").read_text())[
        "_object"
    ]
    result = result or json.load(
        open(ROOT / f"data/demo_area/converted/demo_area_{label}_distribution_partitions.json")
    )

    buses = serialise_frame(raw, "bus")
    loads = serialise_frame(raw, "load")
    sgens = serialise_frame(raw, "sgen")
    gens = serialise_frame(raw, "gen")
    ext_grids = serialise_frame(raw, "ext_grid")
    lines = serialise_frame(raw, "line")
    trafos = serialise_frame(raw, "trafo")
    res_bus = serialise_frame(raw, "res_bus")
    res_line = serialise_frame(raw, "res_line")
    res_trafo = serialise_frame(raw, "res_trafo")
    res_gen_by_id = {r["_index"]: r for r in serialise_frame(raw, "res_gen")}
    bus_by_id = {b["_index"]: b for b in buses}
    be_controls_by_bus: dict[int, list[dict]] = {}
    for gen in gens:
        bus = bus_by_id[int(gen["bus"])]
        if gen.get("in_service", True) and bus.get("bpa_subtype") == "E":
            be_controls_by_bus.setdefault(int(gen["bus"]), []).append({
                "bus": int(gen["bus"]),
                "name": bus.get("name"),
                "vm_target": round_num(gen.get("vm_pu"), 6),
                "q_mvar": round_num(res_gen_by_id.get(gen["_index"], {}).get("q_mvar"), 4),
            })

    # The persisted result intentionally contains summary-only partition rows.
    # Recompute the membership sets when pandapower is available so the browser
    # can draw the actual main/distribution boundary and METIS zones.
    computed_partitions = None
    try:
        import pandapower as pp  # type: ignore
        from gmss.core import _build_demo_distribution_partitions  # type: ignore

        net = pp.from_json(str(ROOT / f"data/demo_area/converted/demo_area_{label}_pandapower.json"))
        computed_partitions = _build_demo_distribution_partitions(
            net,
            ROOT / "data/demo_area/converted/demo_area_feeders.json",
            partition_mode="metis",
            target_buses_per_part=150,
        )
    except Exception:
        # A plain Python installation can still rebuild the page from the
        # checked-in summaries; the membership arrays are then empty.
        computed_partitions = None

    load_by_bus: dict[int, float] = {}
    for item in loads:
        if item.get("in_service", True):
            load_by_bus[int(item["bus"])] = load_by_bus.get(int(item["bus"]), 0.0) + float(item.get("p_mw") or 0)
    sgen_by_bus: dict[int, float] = {}
    for item in sgens:
        if item.get("in_service", True):
            sgen_by_bus[int(item["bus"])] = sgen_by_bus.get(int(item["bus"]), 0.0) + float(item.get("p_mw") or 0)
    slack_buses = {int(item["bus"]) for item in ext_grids}

    res_bus_by_id = {r["_index"]: r for r in res_bus}
    res_line_by_id = {r["_index"]: r for r in res_line}
    res_trafo_by_id = {r["_index"]: r for r in res_trafo}
    bus_rows = []
    for index, item in enumerate(buses):
        bus_id = item["_index"]
        bus_result = res_bus_by_id.get(bus_id, {})
        vm = bus_result.get("vm_pu")
        va = bus_result.get("va_degree")
        bus_rows.append(
            {
                "id": bus_id,
                "name": item.get("name") or f"BUS-{bus_id:04d}",
                "kv": round_num(item.get("vn_kv"), 3),
                "zone": item.get("zone"),
                "vm_min": item.get("min_vm_pu"),
                "vm_max": item.get("max_vm_pu"),
                "bpa_zone": item.get("bpa_zone"),
                "vm": round_num(vm, 6),
                "va": round_num(va, 5),
                "p": round_num(bus_result.get("p_mw"), 3),
                "q": round_num(bus_result.get("q_mvar"), 3),
                "load_mw": round_num(load_by_bus.get(bus_id, 0.0), 3),
                "sgen_mw": round_num(sgen_by_bus.get(bus_id, 0.0), 3),
                "kind": "slack" if bus_id in slack_buses else "source" if sgen_by_bus.get(bus_id, 0) > 0 else "load" if load_by_bus.get(bus_id, 0) > 0 else "bus",
            }
        )

    partition_source = computed_partitions or result.get("partitions", [])
    if computed_partitions and len(computed_partitions) > int(result.get("n_parts", len(computed_partitions))):
        partition_source = merge_partition_overflow(partition_source, lines, int(result["n_parts"]))
    persisted_by_id = {int(p["id"]): p for p in result.get("partitions", [])}
    partition_by_bus: dict[int, int] = {}
    boundary_by_bus: set[int] = set()
    compact_partitions = []
    for part in partition_source:
        persisted = persisted_by_id.get(int(part["id"]), {})
        internal_ids = [int(x) for x in part.get("internal_buses", [])]
        boundary_ids = [int(x) for x in part.get("boundary_buses", [])]
        boundary_main_ids = [int(x) for x in part.get("boundary_main_buses", [])]
        boundary_trafo_ids = [int(x) for x in part.get("boundary_trafo_indices", [])]
        for bus_id in internal_ids:
            partition_by_bus[bus_id] = int(part["id"])
        boundary_by_bus.update(boundary_ids)
        compact_partitions.append(
            {
                "id": int(part["id"]),
                "display_name": part.get("display_name", f"P-{part['id']}"),
                "supply_area": part.get("supply_area", ""),
                "converged": bool(persisted.get("converged", part.get("converged", True))),
                "internal_bus_count": int(part.get("internal_bus_count", persisted.get("internal_buses", len(internal_ids)))),
                "boundary_bus_count": int(part.get("boundary_bus_count", persisted.get("boundary_buses", len(boundary_ids)))),
                "boundary_transformer_count": int(part.get("boundary_transformer_count", persisted.get("boundary_trafos", len(boundary_trafo_ids)))),
                "inter_partition_cut_edge_count": int(part.get("inter_partition_cut_edge_count", persisted.get("cut_edges", 0))),
                "max_vm_delta_vs_full_pu": part.get("max_vm_delta_vs_full_pu") if part.get("max_vm_delta_vs_full_pu") is not None else persisted.get("max_vm_delta_vs_full_pu", persisted.get("vm_err")),
                "max_va_delta_vs_full_deg": part.get("max_va_delta_vs_full_deg") if part.get("max_va_delta_vs_full_deg") is not None else persisted.get("max_va_delta_vs_full_deg", persisted.get("va_err")),
                "internal_bus_ids": internal_ids,
                "boundary_bus_ids": boundary_ids,
                "boundary_main_bus_ids": boundary_main_ids,
                "boundary_trafo_ids": boundary_trafo_ids,
            }
        )

    main_ids = {b["id"] for b in bus_rows if float(b.get("kv") or 0) >= 35.0}
    distribution_ids = {b["id"] for b in bus_rows if b["id"] not in main_ids}
    for bus in bus_rows:
        bus["layer"] = "main" if bus["id"] in main_ids else "distribution"
        bus["partition_id"] = partition_by_bus.get(bus["id"])
        bus["is_boundary"] = bus["id"] in boundary_by_bus

    line_rows = []
    for index, item in enumerate(lines):
        flow = res_line_by_id.get(item["_index"], {})
        line_rows.append(
            {
                "id": item["_index"],
                "name": item.get("name") or f"LINE-{item['_index'] + 1:04d}",
                "from": int(item["from_bus"]),
                "to": int(item["to_bus"]),
                "length_km": round_num(item.get("length_km"), 3),
                "max_i_ka": round_num(item.get("max_i_ka"), 4),
                "loading": round_num(flow.get("loading_percent"), 2),
                "p": round_num(flow.get("p_from_mw"), 3),
                "q": round_num(flow.get("q_from_mvar"), 3),
                "in_service": bool(item.get("in_service", True)),
                "from_layer": "main" if int(item["from_bus"]) in main_ids else "distribution",
                "to_layer": "main" if int(item["to_bus"]) in main_ids else "distribution",
            }
        )

    trafo_rows = []
    for index, item in enumerate(trafos):
        flow = res_trafo_by_id.get(item["_index"], {})
        be_controls = be_controls_by_bus.get(int(item["hv_bus"]), []) + be_controls_by_bus.get(int(item["lv_bus"]), [])
        loading_limit = item.get("max_loading_percent") or 100.0
        overloaded = flow.get("loading_percent") is not None and flow["loading_percent"] > loading_limit
        trafo_rows.append(
            {
                "id": item["_index"],
                "name": item.get("name") or f"TRAFO-{item['_index'] + 1:03d}",
                "hv": int(item["hv_bus"]),
                "lv": int(item["lv_bus"]),
                "sn_mva": round_num(item.get("sn_mva"), 3),
                "hv_kv": round_num(item.get("vn_hv_kv"), 3),
                "lv_kv": round_num(item.get("vn_lv_kv"), 3),
                "loading": round_num(flow.get("loading_percent"), 2),
                "p_hv": round_num(flow.get("p_hv_mw"), 3),
                "q_hv": round_num(flow.get("q_hv_mvar"), 3),
                "i_hv_ka": round_num(flow.get("i_hv_ka"), 6),
                "i_rated_hv_ka": round_num(float(item["sn_mva"]) * float(item.get("parallel") or 1) * float(item.get("df") or 1) / (math.sqrt(3) * float(item["vn_hv_kv"])), 6),
                "loading_limit": loading_limit,
                "be_controls": be_controls,
                "model_note": "模型计算过载；相邻 BE 恒压节点的等效无功需核验" if overloaded and be_controls else "模型计算过载，需核验参数与运行工况" if overloaded else "",
                "vm_hv": round_num(flow.get("vm_hv_pu"), 6),
                "vm_lv": round_num(flow.get("vm_lv_pu"), 6),
                "hv_layer": "main" if float(item.get("vn_hv_kv") or 0) >= 35.0 else "distribution",
                "lv_layer": "main" if float(item.get("vn_lv_kv") or 0) >= 35.0 else "distribution",
                "is_interface": (int(item["hv_bus"]) in main_ids) != (int(item["lv_bus"]) in main_ids),
            }
        )

    interface_trafo_ids = [t["id"] for t in trafo_rows if t["is_interface"]]
    partition_by_line = {
        l["id"]: (partition_by_bus.get(l["from"]), partition_by_bus.get(l["to"]))
        for l in line_rows
    }
    for line in line_rows:
        left, right = partition_by_line[line["id"]]
        line["partition_from"] = left
        line["partition_to"] = right
        line["is_cut_edge"] = (
            line["from"] in distribution_ids
            and line["to"] in distribution_ids
            and left is not None
            and right is not None
            and left != right
        )

    microgrids = []
    for index, pcc_bus in enumerate(PCC_BUSES, start=1):
        bus = next((b for b in bus_rows if b["id"] == pcc_bus), None)
        if bus is None:
            continue
        microgrids.append(
            {
                "id": index,
                "name": f"MG-{index}",
                "pcc_bus": pcc_bus,
                "pcc_name": bus["name"],
                "partition_id": partition_by_bus.get(pcc_bus),
                "pv_mw": bus["sgen_mw"],
                "load_mw": bus["load_mw"],
            }
        )
        bus["microgrid_id"] = index
        bus["microgrid_name"] = f"MG-{index} PCC"

    bus_ids = {b["id"] for b in bus_rows}
    for edge in line_rows + [{"from": t["hv"], "to": t["lv"]} for t in trafo_rows]:
        if edge["from"] not in bus_ids or edge["to"] not in bus_ids:
            raise ValueError("Branch endpoint missing from bus table")

    return {
        "result_source": "snapshot",
        "model_audit": {
            "be_control_count": sum(len(v) for v in be_controls_by_bus.values()),
            "overloaded_trafos": sum(1 for t in trafo_rows if t["loading"] is not None and t["loading"] > t["loading_limit"]),
            "overloaded_be_trafos": sum(1 for t in trafo_rows if t["loading"] is not None and t["loading"] > t["loading_limit"] and t["be_controls"]),
        },
        "converged": bool(raw.get("converged")),
        "partitions": compact_partitions,
        "label": f"{label}点",
        "bus_count": len(bus_rows),
        "line_count": len(line_rows),
        "trafo_count": len(trafo_rows),
        "load_count": len(loads),
        "load_mw": round(sum(float(x.get("p_mw") or 0) for x in loads), 2),
        "sgen_mw": round(sum(float(x.get("p_mw") or 0) for x in sgens), 2),
        "summary": {
            "parts": result["n_parts"],
            "converged": result["converged_partitions"],
            "failed": result["failed_partitions"],
            "boundary_buses": result["unique_boundary_bus_count"],
            "boundary_trafos": result["total_boundary_transformer_count"],
            "cut_edges": result["total_inter_partition_cut_edge_count"],
            "repairs": result["metis_connectivity_repairs"],
            "max_vm_error": result["max_vm_delta_vs_full_pu"],
            "max_va_error": result["max_va_delta_vs_full_deg"],
            "min_part_buses": result["min_partition_bus_count"],
            "max_part_buses": result["max_partition_bus_count"],
        },
        "layers": {
            "main_bus_count": len(main_ids),
            "distribution_bus_count": len(distribution_ids),
            "interface_trafo_count": len(interface_trafo_ids),
            "cut_edge_count": sum(1 for line in line_rows if line["is_cut_edge"]),
            "main_bus_ids": sorted(main_ids),
            "distribution_bus_ids": sorted(distribution_ids),
            "interface_trafo_ids": sorted(interface_trafo_ids),
        },
        "microgrids": microgrids,
        "buses": bus_rows,
        "lines": line_rows,
        "trafos": trafo_rows,
    }


def main() -> None:
    layout = json.loads((ROOT / "_case_study_assets_v2/actual_network_layout.json").read_text())
    data = {"layout": layout, "scenarios": {"13": build_scenario("13"), "20": build_scenario("20")}}
    target = OUT / "data.js"
    target.write_text("window.SJZ_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print(f"wrote {target} ({target.stat().st_size / 1024:.1f} KiB)")


if __name__ == "__main__":
    main()
