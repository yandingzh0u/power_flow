"""AC solve following the published BPA/IPF decoupled-start -> Newton workflow.

This is a Python implementation for B / BE / BS, L and fixed-tap T cards in the
supplied DAT cases. It is neither the PSD-BPA executable nor a reproduction of
its closed-source kernel. See bpa_style_solver.md for sources and scope.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import warnings

import numpy as np
from scipy.sparse import bmat, diags
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import MatrixRankWarning, splu, spsolve


@dataclass(frozen=True)
class SolutionOptions:
    decoupled_half_steps: int = 2
    max_newton_iterations: int = 15
    tolerance_pu: float = 1e-8

    def __post_init__(self):
        if not 0 <= self.decoupled_half_steps <= 10:
            raise ValueError("DECOUPLED must be between 0 and 10")
        if not 0 <= self.max_newton_iterations <= 50:
            raise ValueError("NEWTON must be between 0 and 50")
        if not np.isfinite(self.tolerance_pu) or self.tolerance_pu <= 0:
            raise ValueError("A finite positive mismatch tolerance is required")

    @classmethod
    def from_instructions(cls, instructions):
        options = {}
        for instruction in instructions:
            match = re.fullmatch(r">SOL_ITER\s*,\s*(.*?)\s*<", instruction.strip(), re.I)
            if not match:
                raise ValueError(f"Unsupported solution instruction: {instruction}")
            for field in match[1].split(","):
                pair = re.fullmatch(r"\s*(DECOUPLED|NEWTON)\s*=\s*(\d+)\s*", field, re.I)
                if not pair:
                    raise ValueError(f"Unsupported SOL_ITER setting: {field}")
                key = {"DECOUPLED": "decoupled_half_steps", "NEWTON": "max_newton_iterations"}[pair[1].upper()]
                if key in options:
                    raise ValueError(f"Duplicate SOL_ITER setting: {pair[1]}")
                options[key] = int(pair[2])
        return cls(**options)


def solve_bpa_style(base, buses, ybus, options, observer=None):
    """Keep BE voltage fixed, alternate P/theta and Q/V, then solve full AC NR.

    Startup uses fixed A = H at zero angle and C = -Im(Y)[PQ,PQ], following
    the public IPF jacbfc/dcfact structure. Its convergence path and the Newton
    backtracking below are not claimed to match a particular PSD-BPA version.
    """
    if not np.isfinite(base) or base <= 0 or not buses:
        raise ValueError("A positive MVA base and at least one bus are required")
    if ybus.shape != (len(buses), len(buses)) or not np.isfinite(ybus.data).all():
        raise ValueError("Invalid nodal admittance matrix")
    if any(b["subtype"] not in (" ", "E", "S") for b in buses):
        raise ValueError("Only B, BE and BS bus controls are implemented")
    pq = np.array([i for i, b in enumerate(buses) if b["subtype"] == " "], dtype=int)
    ns = np.array([i for i, b in enumerate(buses) if b["subtype"] != "S"], dtype=int)
    graph = ybus.copy()
    graph.data = np.ones(len(graph.data))
    graph.setdiag(0)
    graph.eliminate_zeros()
    count, components = connected_components(graph, directed=False)
    for component in range(count):
        slack_count = sum(b["subtype"] == "S" for i, b in enumerate(buses) if components[i] == component)
        if slack_count != 1:
            raise ValueError("Each AC island must have exactly one BS for this implementation")
    vm = np.array([1.0 if b["subtype"] == " " else b["vm"] for b in buses])
    va = np.array([np.deg2rad(b["va"]) for b in buses])
    specified = np.array([complex(b["p"], b["q"]) / base for b in buses])
    if not np.isfinite(specified).all() or not np.isfinite(vm).all() or not np.isfinite(va).all() or (vm <= 0).any():
        raise ValueError("Nonfinite injection or invalid initial voltage")

    def state(magnitude, angle):
        voltage = magnitude * np.exp(1j * angle)
        mismatch = voltage * np.conj(ybus @ voltage) - specified
        residual = np.r_[mismatch[ns].real, mismatch[pq].imag]
        norm = float(np.max(np.abs(residual), initial=0))
        return voltage, mismatch, residual, norm

    trace = []

    def record(stage, iteration, before, delta_angle=None, delta_vm=None, damping=1.0):
        _, mismatch, _, norm = state(vm, va)
        trace.append({"stage": stage, "iteration": iteration,
                      "max_p_mismatch_pu": float(np.max(np.abs(mismatch[ns].real), initial=0)),
                      "max_q_mismatch_pu": float(np.max(np.abs(mismatch[pq].imag), initial=0)),
                      "max_mismatch_pu": norm, "before_max_mismatch_pu": before,
                      "max_angle_step_rad": float(np.max(np.abs(delta_angle), initial=0)) if delta_angle is not None else 0.0,
                      "max_vm_step_pu": float(np.max(np.abs(delta_vm), initial=0)) if delta_vm is not None else 0.0,
                      "damping": damping})

        if observer:
            observer(trace[-1].copy(), vm * np.exp(1j * va))

    record("initial", 0, None)
    startup_reverted = False
    startup_damping = 1.0
    if options.decoupled_half_steps and trace[-1]["max_mismatch_pu"] >= options.tolerance_pu:
        saved_vm, saved_va = vm.copy(), va.copy()
        saved_norm = trace[-1]["max_mismatch_pu"]
        # A's diagonal is the sum of V_i V_j B_ij over off-diagonal entries;
        # tap and shunt effects remain in the actual AC mismatch throughout.
        susceptance = ybus.imag.copy()
        susceptance.setdiag(0)
        susceptance.eliminate_zeros()
        weighted = diags(vm) @ susceptance @ diags(vm)
        a = diags(np.asarray(weighted.sum(axis=1)).ravel()) - weighted
        c = -ybus.imag
        try:
            p_factor = splu(a[ns][:, ns].tocsc()) if len(ns) else None
            q_factor = splu(c[pq][:, pq].tocsc()) if len(pq) and options.decoupled_half_steps >= 2 else None
            for half_step in range(options.decoupled_half_steps):
                _, mismatch, _, norm = state(vm, va)
                if half_step % 2 == 0:
                    delta = -p_factor.solve(mismatch[ns].real) if p_factor else np.array([])
                    # Smooth truncation used in public IPF baksln.f.
                    large = np.abs(delta) >= 1.5
                    delta[large] = np.sign(delta[large]) * (3.0 - 2.25 / np.abs(delta[large]))
                    va[ns] += delta
                    record("decoupled_P", half_step + 1, norm, delta_angle=delta)
                else:
                    delta = -q_factor.solve(mismatch[pq].imag / vm[pq]) if q_factor else np.array([])
                    large = np.abs(delta) >= 0.2
                    delta[large] = np.sign(delta[large]) * (0.4 - 0.04 / np.abs(delta[large]))
                    vm[pq] += delta
                    record("decoupled_Q", half_step + 1, norm, delta_vm=delta)
                if not np.isfinite(vm).all() or not np.isfinite(va).all() or (vm <= 0).any():
                    raise RuntimeError("Invalid voltage during decoupled startup")
            if not np.isfinite(trace[-1]["max_mismatch_pu"]):
                raise RuntimeError("Nonfinite AC mismatch after decoupled startup")
            # A/C decoupling is a weak approximation on resistive feeders and
            # negative-X three-winding equivalents. Globalize the startup as a
            # whole, with unchanged P/Q equations and fixed BE/BS targets.
            if trace[-1]["max_mismatch_pu"] > saved_norm:
                trial_vm, trial_va = vm.copy(), va.copy()
                for _ in range(16):
                    startup_damping *= 0.5
                    vm = saved_vm + startup_damping * (trial_vm - saved_vm)
                    va = saved_va + startup_damping * (trial_va - saved_va)
                    _, _, _, norm = state(vm, va)
                    if np.isfinite(norm) and norm < saved_norm:
                        record("startup_damped", 0, saved_norm, va - saved_va, vm - saved_vm, startup_damping)
                        break
                else:
                    raise RuntimeError("No decreasing step along the decoupled startup direction")
        except RuntimeError as error:
            # Failed numerical initialization does not change the electrical model.
            vm, va = saved_vm, saved_va
            startup_reverted = True
            record("startup_reverted", 0, saved_norm)
            trace[-1]["reason"] = str(error)

    newton_iterations = 0
    for iteration in range(options.max_newton_iterations + 1):
        voltage, _, residual, norm = state(vm, va)
        if norm < options.tolerance_pu:
            return voltage, {"solver": "Python BPA-style decoupled startup + full AC Newton",
                             "PSD_BPA_executed": False, "options": asdict(options),
                             "startup_reverted": startup_reverted,
                             "startup_damping": startup_damping,
                             "newton_iterations": newton_iterations, "trace": trace}
        if iteration == options.max_newton_iterations:
            break
        current = ybus @ voltage
        dv, di, unit = diags(voltage), diags(current), diags(voltage / vm)
        da = 1j * dv @ (di - ybus @ dv).conj()
        dm = dv @ (ybus @ unit).conj() + di.conj() @ unit
        jacobian = bmat([[da[ns][:, ns].real, dm[ns][:, pq].real],
                         [da[pq][:, ns].imag, dm[pq][:, pq].imag]], format="csc")
        with warnings.catch_warnings():
            warnings.simplefilter("error", MatrixRankWarning)
            delta = spsolve(jacobian, -residual)
        if not np.isfinite(delta).all():
            raise RuntimeError("Nonfinite Newton step")
        damping = 1.0
        for _ in range(16):
            next_va, next_vm = va.copy(), vm.copy()
            next_va[ns] += damping * delta[:len(ns)]
            next_vm[pq] += damping * delta[len(ns):]
            if (next_vm > 0).all():
                _, _, _, next_norm = state(next_vm, next_va)
                if np.isfinite(next_norm) and next_norm < norm:
                    break
            damping *= 0.5
        else:
            raise RuntimeError(f"Newton backtracking failed at mismatch {norm} pu")
        vm, va = next_vm, next_va
        newton_iterations += 1
        record("newton", newton_iterations, norm, damping * delta[:len(ns)], damping * delta[len(ns):], damping)
    raise RuntimeError(f"AC solve did not converge in NEWTON={options.max_newton_iterations}: {norm} pu")


def allocate_be_reactive(required_mvar, qmax, qmin):
    """Separate available generation from unlimited voltage-support requirement.

    Valid only with zero adjustable shunt. Reversed raw ranges remain unresolved
    rather than guessing a particular PSD-BPA version's input normalization.
    """
    if qmax < qmin:
        return {"allocated_q_generation_mvar": None, "unscheduled_q_mvar": None,
                "q_allocation_status": "QMAX < QMIN: input normalization not assumed"}
    assigned = float(np.clip(required_mvar, qmin, qmax))
    return {"allocated_q_generation_mvar": assigned,
            "unscheduled_q_mvar": float(required_mvar - assigned),
            "q_allocation_status": "raw Q range; no adjustable shunt; no display rounding"}
