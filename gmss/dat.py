"""Fixed-column DAT parsing with explicit impedance-base interpretation."""
from __future__ import annotations
import re
import warnings
import numpy as np
from scipy.sparse import bmat, coo_matrix, diags
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import MatrixRankWarning, spsolve

def impedance_policy(base, transformer_base_mva):
    """Describe a chosen T-card interpretation, separate from the DAT base."""
    assumed = transformer_base_mva is not None
    transformer_base = float(transformer_base_mva) if assumed else base
    if not np.isfinite(transformer_base) or transformer_base <= 0:
        raise ValueError("Transformer impedance base must be finite and positive")
    label = (f"T 卡阻抗按 {transformer_base:g} MVA 假设解释；L 卡按系统 {base:g} MVA；"
             "该假设尚未经 PSD-BPA 结果验证") if assumed else f"L / T 卡阻抗均按 DAT 系统 {base:g} MVA 解释"
    return {"mode": "assumed_transformer_base" if assumed else "system_base",
            "system_base_mva": base, "line_base_mva": base,
            "transformer_base_mva": transformer_base,
            "transformer_scale": base / transformer_base,
            "is_assumption": assumed, "label": label}


def read_dat(path, transformer_base_mva=None):
    text = path.read_text(encoding="gbk")
    match = re.search(r"/MVA_BASE\s*=\s*([0-9.]+)", text)
    if not match:
        raise ValueError("System MVA base must be explicitly supplied")
    base = float(match.group(1))
    policy = impedance_policy(base, transformer_base_mva)
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
                   "load_p": num(line, 21, 25), "gen_p": num(line, 43, 47), "load_q": num(line, 26, 30), "gs": num(line, 31, 34), "bs": num(line, 35, 38),
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
            input_r, input_x = num(line, 39, 44, 5), num(line, 45, 50, 5)
            scale = policy['transformer_scale'] if kind == "T" else 1.0
            r, x = input_r * scale, input_x * scale
            regularized = kind == "L" and r == 0 and x == 0
            if regularized:
                x = 1e-6
            g, b = num(line, 51, 56, 5), num(line, 57, 62, 5)
            # All transformer shunt fields in these supplied cases are zero.
            # Refuse to silently assume a T/pi split for future nonzero fields.
            if kind == "T" and (g != 0 or b != 0):
                raise ValueError("Nonzero transformer shunt needs a documented model")
            branches.append({"kind": kind, "from": f, "to": t, "r": r, "x": x,
                             "input_r_pu": input_r, "input_x_pu": input_x,
                             "impedance_base_mva": policy['transformer_base_mva'] if kind == "T" else base,
                             "impedance_scale": scale,
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

def newton(base, buses, ybus, observer=None, damped=False, max_iterations=30, tolerance=1e-8, check_islands=True):
    if check_islands:
        graph = ybus.copy()
        graph.setdiag(0)
        graph.eliminate_zeros()
        graph.data = np.ones(len(graph.data))
        count, labels = connected_components(graph, directed=False)
        for island in range(count):
            if sum(b['subtype'] == 'S' for i, b in enumerate(buses) if labels[i] == island) != 1:
                raise ValueError('Each AC island must have exactly one BS reference')
    pq = np.array([i for i, b in enumerate(buses) if b["subtype"] == " "], dtype=int)
    non_slack = np.array([i for i, b in enumerate(buses) if b["subtype"] != "S"], dtype=int)
    vm = np.array([1.0 if b["subtype"] == " " else b["vm"] for b in buses])
    va = np.array([np.deg2rad(b["va"]) for b in buses])
    specified = np.array([complex(b["p"], b["q"]) / base for b in buses])
    history = []
    for iteration in range(max_iterations + 1):
        voltage = vm * np.exp(1j * va)
        current = ybus @ voltage
        mismatch = voltage * np.conj(current) - specified
        residual = np.r_[mismatch[non_slack].real, mismatch[pq].imag]
        norm = float(np.max(np.abs(residual), initial=0))
        history.append(norm)
        if observer:
            observer({'stage': 'newton', 'iteration': iteration, 'max_mismatch_pu': norm}, voltage.copy())
        if norm < tolerance:
            return voltage, history
        if iteration == max_iterations:
            break
        dv, di = diags(voltage), diags(current)
        unit = diags(voltage / vm)
        da = 1j * dv @ (di - ybus @ dv).conj()
        dm = dv @ (ybus @ unit).conj() + di.conj() @ unit
        jacobian = bmat([[da[non_slack][:, non_slack].real, dm[non_slack][:, pq].real],
                         [da[pq][:, non_slack].imag, dm[pq][:, pq].imag]], format="csc")
        with warnings.catch_warnings():
            warnings.simplefilter('error', MatrixRankWarning)
            step = spsolve(jacobian, -residual)
        damping = 1.0
        if damped:
            for _ in range(20):
                trial_va, trial_vm = va.copy(), vm.copy()
                trial_va[non_slack] += damping * step[:len(non_slack)]
                trial_vm[pq] += damping * step[len(non_slack):]
                trial_v = trial_vm * np.exp(1j * trial_va)
                trial_m = trial_v * np.conj(ybus @ trial_v) - specified
                trial_norm = np.max(np.abs(np.r_[trial_m[non_slack].real, trial_m[pq].imag]), initial=0)
                if np.isfinite(trial_norm) and (trial_vm > 0).all() and trial_norm < norm:
                    break
                damping *= .5
            else:
                raise RuntimeError('Damped Newton could not find a decreasing step')
        va[non_slack] += damping * step[:len(non_slack)]
        vm[pq] += damping * step[len(non_slack):]
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
        "input_r_pu": branch.get("input_r_pu", branch["r"]),
        "input_x_pu": branch.get("input_x_pu", branch["x"]),
        "impedance_base_mva": branch.get("impedance_base_mva", base),
        "impedance_scale": branch.get("impedance_scale", 1.0),
        "tap_ratio_pu": branch["tap"],
        "p_from_mw": float(s_from.real), "q_from_mvar": float(s_from.imag),
        "p_to_mw": float(s_to.real), "q_to_mvar": float(s_to.imag),
        "current_loading_percent": float(max(abs(i_from), abs(i_to)) * base / rate * 100) if rate else None,
        "apparent_power_loading_percent": float(max(abs(s_from), abs(s_to)) / rate * 100) if rate else None,
        "active_power_ratio_percent": float(max(abs(s_from.real), abs(s_to.real)) / rate * 100) if rate else None,
    }
