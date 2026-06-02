#!/usr/bin/env python3
"""Convert a fixed-width PSD-BPA power-flow DAT file to pandapower JSON."""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandapower as pp
from pandapower.converter.pypower.from_ppc import from_ppc


REQUIRED_SECTIONS = ("(POWERFLOW", "/NETWORK_DATA", "/SOLUTION", "(END)")


@dataclass
class BusCard:
    name: str
    kv: float
    zone: str
    subtype: str
    pd: float
    qd: float
    gs: float
    bs: float
    pmax: float
    pg: float
    qfield: float
    qmin: float
    vm: float
    va: float
    raw: str


@dataclass
class BranchCard:
    kind: str
    from_key: tuple[str, float]
    to_key: tuple[str, float]
    r: float
    x: float
    b: float
    rate: float
    ratio: float
    angle: float
    raw: str


def fixed(line: str, start: int, end: int) -> str:
    """Return 1-based inclusive fixed-width slice."""
    return line[start - 1 : end]


def fixed_name(line: str, start: int, end: int) -> str:
    return fixed(line, start, end).strip()


def fixed_float(line: str, start: int, end: int, default: float = 0.0) -> float:
    text = fixed(line, start, end).strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(
            f"Cannot parse float in columns {start}-{end}: {text!r} from {line.rstrip()!r}"
        ) from exc


def bus_key(name: str, kv: float) -> tuple[str, float]:
    return (name, round(float(kv), 6))


def check_dat_structure(dat_path: Path) -> dict[str, Any]:
    text = dat_path.read_text(encoding="gbk", errors="replace")
    present = {section: (section in text) for section in REQUIRED_SECTIONS}
    missing = [section for section, ok in present.items() if not ok]
    if missing:
        raise ValueError(f"DAT file is missing required PSD-BPA sections: {', '.join(missing)}")
    return {"status": "ok", "sections": present}


def parse_dat(dat_path: Path) -> tuple[float, list[BusCard], list[BranchCard], dict[str, int], int]:
    base_mva = 100.0
    buses: list[BusCard] = []
    branches: list[BranchCard] = []
    card_counts: dict[str, int] = {}
    zero_impedance_adjustments = 0

    for raw in dat_path.read_text(encoding="gbk", errors="replace").splitlines():
        line = raw.rstrip("\r\n")
        base_match = re.match(r"\s*/MVA_BASE\s*=\s*([0-9.]+)", line)
        if base_match:
            base_mva = float(base_match.group(1))

        if line.startswith("B") and line[:2] not in {"BD", "BM"}:
            card_counts[line[:2]] = card_counts.get(line[:2], 0) + 1
            padded = line.ljust(90)
            buses.append(
                BusCard(
                    name=fixed_name(padded, 7, 14),
                    kv=fixed_float(padded, 15, 18),
                    zone=fixed_name(padded, 19, 20) or "1",
                    subtype=padded[1],
                    pd=fixed_float(padded, 21, 25),
                    qd=fixed_float(padded, 26, 30),
                    gs=fixed_float(padded, 31, 34),
                    bs=fixed_float(padded, 35, 38),
                    pmax=fixed_float(padded, 39, 42),
                    pg=fixed_float(padded, 43, 47),
                    qfield=fixed_float(padded, 48, 52),
                    qmin=fixed_float(padded, 53, 57),
                    vm=fixed_float(padded, 58, 61),
                    va=fixed_float(padded, 62, 65),
                    raw=line,
                )
            )
        elif line.startswith("L "):
            card_counts["L "] = card_counts.get("L ", 0) + 1
            padded = line.ljust(130)
            name1 = fixed_name(padded, 7, 14)
            kv1 = fixed_float(padded, 15, 18)
            name2 = fixed_name(padded, 20, 27)
            kv2 = fixed_float(padded, 28, 31)
            current_a = fixed_float(padded, 34, 37)
            r = fixed_float(padded, 39, 44)
            x = fixed_float(padded, 45, 50)
            b_half = fixed_float(padded, 57, 62)
            if abs(r) < 1e-12 and abs(x) < 1e-12:
                x = 1e-6
                zero_impedance_adjustments += 1
            rate_mva = math.sqrt(3.0) * kv1 * current_a / 1000.0 if current_a else 0.0
            branches.append(
                BranchCard(
                    kind="L",
                    from_key=bus_key(name1, kv1),
                    to_key=bus_key(name2, kv2),
                    r=r,
                    x=x,
                    b=2.0 * b_half,
                    rate=rate_mva,
                    ratio=0.0,
                    angle=0.0,
                    raw=line,
                )
            )
        elif line.startswith("T "):
            card_counts["T "] = card_counts.get("T ", 0) + 1
            padded = line.ljust(100)
            name1 = fixed_name(padded, 7, 14)
            kv1 = fixed_float(padded, 15, 18)
            name2 = fixed_name(padded, 20, 27)
            kv2 = fixed_float(padded, 28, 31)
            tap1 = fixed_float(padded, 63, 67)
            tap2 = fixed_float(padded, 68, 72)
            ratio = 0.0
            if tap1 and tap2 and kv1 and kv2:
                ratio_value = (tap1 / kv1) / (tap2 / kv2)
                if abs(ratio_value - 1.0) > 1e-9:
                    ratio = ratio_value
            branches.append(
                BranchCard(
                    kind="T",
                    from_key=bus_key(name1, kv1),
                    to_key=bus_key(name2, kv2),
                    r=fixed_float(padded, 39, 44),
                    x=fixed_float(padded, 45, 50),
                    b=fixed_float(padded, 57, 62),
                    rate=fixed_float(padded, 34, 37),
                    ratio=ratio,
                    angle=0.0,
                    raw=line,
                )
            )

    return base_mva, buses, branches, card_counts, zero_impedance_adjustments


def build_ppc(base_mva: float, buses: list[BusCard], branches: list[BranchCard]) -> tuple[dict[str, Any], dict[str, int]]:
    zones: dict[str, int] = {}
    for bus in buses:
        if bus.zone not in zones:
            zones[bus.zone] = len(zones) + 1

    bus_index = {bus_key(bus.name, bus.kv): idx + 1 for idx, bus in enumerate(buses)}
    missing = [
        branch.raw
        for branch in branches
        if branch.from_key not in bus_index or branch.to_key not in bus_index
    ]
    if missing:
        sample = "\n".join(missing[:5])
        raise ValueError(f"{len(missing)} branch endpoints are missing from B cards:\n{sample}")

    bus_rows: list[list[float]] = []
    gen_rows: list[list[float]] = []
    for bus in buses:
        idx = bus_index[bus_key(bus.name, bus.kv)]
        bus_type = 3 if bus.subtype == "S" else (2 if bus.subtype in {"E", "Q", "G"} else 1)
        pd = bus.pd
        qd = bus.qd

        # BPA PQ-node scheduled generation is equivalent to negative demand.
        if bus_type == 1 and (abs(bus.pg) > 1e-12 or abs(bus.qfield) > 1e-12):
            pd -= bus.pg
            qd -= bus.qfield

        vm = bus.vm or 1.0
        va = bus.va if bus_type == 3 else 0.0
        bus_rows.append(
            [
                idx,
                bus_type,
                pd,
                qd,
                bus.gs,
                bus.bs,
                1,
                vm,
                va,
                bus.kv,
                zones[bus.zone],
                1.1,
                0.9,
            ]
        )

        if bus_type in {2, 3}:
            pmax = max(abs(bus.pg), abs(bus.pmax), base_mva)
            pmin = -base_mva if bus_type == 3 else 0.0
            gen_rows.append(
                [
                    idx,
                    bus.pg,
                    bus.qfield,
                    9999.0,
                    -9999.0,
                    vm,
                    base_mva,
                    1,
                    pmax,
                    pmin,
                ]
            )

    branch_rows = [
        [
            bus_index[branch.from_key],
            bus_index[branch.to_key],
            branch.r,
            branch.x,
            branch.b,
            branch.rate,
            branch.rate,
            branch.rate,
            branch.ratio,
            branch.angle,
            1,
            -360,
            360,
        ]
        for branch in branches
    ]

    return (
        {
            "version": "2",
            "baseMVA": base_mva,
            "bus": np.array(bus_rows, dtype=float),
            "gen": np.array(gen_rows, dtype=float),
            "branch": np.array(branch_rows, dtype=float),
        },
        zones,
    )


def convert(dat_path: Path) -> tuple[Any, dict[str, Any]]:
    structure_check = check_dat_structure(dat_path)
    base_mva, buses, branches, card_counts, zero_adjustments = parse_dat(dat_path)
    ppc, zones = build_ppc(base_mva, buses, branches)
    net = from_ppc(ppc, f_hz=50.0)
    net.name = dat_path.stem

    summary: dict[str, Any] = {
        "input_file": str(dat_path),
        "structure_check": structure_check,
        "base_mva": base_mva,
        "card_counts": card_counts,
        "zone_count": len(zones),
        "bpa_counts": {
            "buses": len(buses),
            "branches": len(branches),
            "voltage_control_generators": int(len(ppc["gen"])),
        },
        "pandapower_counts": {
            "bus": int(len(net.bus)),
            "load": int(len(net.load)),
            "sgen": int(len(net.sgen)),
            "gen": int(len(net.gen)),
            "ext_grid": int(len(net.ext_grid)),
            "line": int(len(net.line)),
            "trafo": int(len(net.trafo)),
            "impedance": int(len(net.impedance)),
        },
        "zero_impedance_l_cards_adjusted": zero_adjustments,
    }
    return net, summary


def add_powerflow_summary(net: Any, summary: dict[str, Any]) -> None:
    pp.runpp(
        net,
        algorithm="nr",
        init="auto",
        max_iteration=50,
        tolerance_mva=1e-6,
        enforce_q_lims=False,
        calculate_voltage_angles=True,
        trafo_model="t",
    )
    summary["pandapower_powerflow"] = {
        "converged": bool(net.converged),
        "iterations": int(net._ppc.get("iterations", -1)),
        "vm_pu_min": float(net.res_bus.vm_pu.min()),
        "vm_pu_max": float(net.res_bus.vm_pu.max()),
        "ext_grid_p_mw": float(net.res_ext_grid.p_mw.sum()),
        "ext_grid_q_mvar": float(net.res_ext_grid.q_mvar.sum()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert PSD-BPA DAT to pandapower JSON.")
    parser.add_argument("dat", type=Path, help="PSD-BPA DAT file")
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Output pandapower JSON path",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="Output conversion summary JSON path",
    )
    parser.add_argument("--no-runpp", action="store_true", help="Skip pandapower power-flow validation")
    args = parser.parse_args()

    dat_path = args.dat.resolve()
    out_json = args.json or dat_path.with_name(f"{dat_path.stem}_pandapower.json")
    out_summary = args.summary or dat_path.with_name(f"{dat_path.stem}_conversion_summary.json")

    net, summary = convert(dat_path)
    if not args.no_runpp:
        add_powerflow_summary(net, summary)

    pp.to_json(net, str(out_json))
    out_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"pandapower_json={out_json}")
    print(f"summary_json={out_summary}")


if __name__ == "__main__":
    main()
