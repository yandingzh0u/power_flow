"""Parallel distribution-domain Newton with an exact AC Schur interface.

Every Newton step eliminates distribution interiors independently, sums their
interface equations, solves the coordinator, then back-substitutes locally.
There is no whole-network Newton warm start and no fixed solved boundary.
Numba executes independent domains on CPU threads outside the Python GIL.
"""
from __future__ import annotations

from collections import OrderedDict
import os
import threading
import time

import numpy as np
# Prefer the bundled OpenMP runtime over an unrelated, older system TBB.
os.environ.setdefault('NUMBA_THREADING_LAYER', 'omp')
from numba import get_num_threads, get_thread_id, njit, prange, set_num_threads
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, reverse_cuthill_mckee
from scipy.sparse.linalg import splu, spsolve

from .partition import available_cpus


_structure_cache = OrderedDict()
_structure_cache_lock = threading.Lock()
_STRUCTURE_CACHE_BYTES = 32 * 1024 * 1024
_STRUCTURE_CACHE_ENTRIES = 4


def clear_structure_cache():
    """Drop structural templates only; used for uncached benchmarks/tests."""
    with _structure_cache_lock:
        _structure_cache.clear()


@njit(cache=True, parallel=True)
def _eliminate(values, rhs, blocks, pivots, lowers, uppers, updates, failed, worker_ids, durations, schedule):
    for task in prange(len(schedule)):
        part = schedule[task]
        worker_ids[part] = get_thread_id()
        # Numba's monotonic wall clock is supplied by a tiny intrinsic below.
        started = _wall_time()
        for k in range(blocks[part], blocks[part + 1]):
            # pivot: diagonal cell, RHS row, lower range, upper range, update offset
            diagonal, row, ls, le, us, ue, target = pivots[k]
            d = values[diagonal]
            if not np.isfinite(d) or abs(d) < 1e-12:
                failed[part] = 1
                break
            for l in range(ls, le):
                cell, other = lowers[l]
                factor = values[cell] / d
                rhs[other] -= factor * rhs[row]
                for u in range(us, ue):
                    values[updates[target]] -= factor * values[uppers[u, 0]]
                    target += 1
        durations[part] = (_wall_time() - started) * 1000


@njit(cache=True, parallel=True)
def _back_substitute(values, rhs, blocks, pivots, uppers, solution, failed, schedule):
    for task in prange(len(schedule)):
        part = schedule[task]
        if failed[part]:
            continue
        for k in range(blocks[part + 1] - 1, blocks[part] - 1, -1):
            diagonal, row, ls, le, us, ue, target = pivots[k]
            value = rhs[row]
            for u in range(us, ue):
                cell, other = uppers[u]
                value -= values[cell] * solution[other]
            solution[row] = value / values[diagonal]


# clock_gettime is available on the supported Ubuntu platform. Keep the clock
# outside object mode, so timing does not serialize worker threads through GIL.
from numba.extending import intrinsic
from llvmlite import ir


@intrinsic
def _wall_time(typingctx):
    from numba import types
    signature = types.float64()
    def codegen(context, builder, sig, args):
        timespec = ir.LiteralStructType([ir.IntType(64), ir.IntType(64)])
        pointer = builder.alloca(timespec)
        function_type = ir.FunctionType(ir.IntType(32), [ir.IntType(32), timespec.as_pointer()])
        function = builder.module.globals.get('clock_gettime')
        if function is None:
            function = ir.Function(builder.module, function_type, name='clock_gettime')
        builder.call(function, [ir.Constant(ir.IntType(32), 1), pointer])
        stamp = builder.load(pointer)
        seconds = builder.sitofp(builder.extract_value(stamp, 0), ir.DoubleType())
        nanos = builder.sitofp(builder.extract_value(stamp, 1), ir.DoubleType())
        return builder.fadd(seconds, builder.fmul(nanos, ir.Constant(ir.DoubleType(), 1e-9)))
    return signature, codegen


def _symbolic_sparse(rows, cols, size, interior):
    """Symbolic local LU; boundary rows stay uneliminated. No numeric cache."""
    graph = [set() for _ in range(size)]
    for i, j in zip(rows, cols):
        graph[int(i)].add(int(j))
        graph[int(j)].add(int(i))
    cells = {(i, i) for i in range(size)}
    cells.update(zip(map(int, rows), map(int, cols)))
    instructions = []
    for k in range(interior):
        neighbors = sorted(n for n in graph[k] if n > k)
        cells.update((i, j) for i in neighbors for j in neighbors)
        for i in neighbors:
            graph[i].update(neighbors)
        instructions.append(neighbors)
    cells = sorted(cells)
    lookup = {ij: i for i, ij in enumerate(cells)}
    pivots, lower, upper, updates = [], [], [], []
    for k, neighbors in enumerate(instructions):
        ls, us, target = len(lower), len(upper), len(updates)
        lower.extend((lookup[i, k], i) for i in neighbors)
        upper.extend((lookup[k, i], i) for i in neighbors)
        updates.extend(lookup[i, j] for i in neighbors for j in neighbors)
        pivots.append((lookup[k, k], k, ls, len(lower), us, len(upper), target))
    boundary_cells = [(cell, i - interior, j - interior)
                      for cell, (i, j) in enumerate(cells) if i >= interior and j >= interior]
    return (lookup, np.asarray(pivots, dtype=np.int64).reshape(-1, 7),
            np.asarray(lower, dtype=np.int64).reshape(-1, 2),
            np.asarray(upper, dtype=np.int64).reshape(-1, 2),
            np.asarray(updates, dtype=np.int64), boundary_cells)


@njit(cache=True)
def _symbolic_dense(rows, cols, size, interior):
    pattern = np.eye(size, dtype=np.bool_)
    for i, j in zip(rows, cols):
        pattern[i, j] = True
        pattern[j, i] = True
    counts = np.zeros(interior, dtype=np.int64)
    for k in range(interior):
        adjacent = np.flatnonzero(pattern[k, k + 1:]) + k + 1
        counts[k] = len(adjacent)
        for i in adjacent:
            for j in adjacent:
                pattern[i, j] = True
    slots = np.full((size, size), -1, dtype=np.int64)
    cell = 0
    boundary_count = 0
    for i in range(size):
        for j in range(size):
            if pattern[i, j]:
                slots[i, j] = cell
                cell += 1
                if i >= interior and j >= interior:
                    boundary_count += 1
    pivots = np.empty((interior, 7), dtype=np.int64)
    lower = np.empty((counts.sum(), 2), dtype=np.int64)
    upper = np.empty_like(lower)
    updates = np.empty((counts * counts).sum(), dtype=np.int64)
    boundary_cells = np.empty((boundary_count, 3), dtype=np.int64)
    offset = update = 0
    for k in range(interior):
        adjacent = np.flatnonzero(pattern[k, k + 1:]) + k + 1
        end = offset + len(adjacent)
        pivots[k] = (slots[k, k], k, offset, end, offset, end, update)
        for n in adjacent:
            lower[offset] = (slots[n, k], n)
            upper[offset] = (slots[k, n], n)
            offset += 1
            for m in adjacent:
                updates[update] = slots[n, m]
                update += 1
    cell = 0
    for i in range(interior, size):
        for j in range(interior, size):
            if pattern[i, j]:
                boundary_cells[cell] = (slots[i, j], i - interior, j - interior)
                cell += 1
    return slots, pivots, lower, upper, updates, boundary_cells


def _symbolic(rows, cols, size, interior):
    """Native symbolic fill for small domains; bounded dense scratch only.

    The table stores integer offsets, never admittances or LU factors. Large
    domains retain the sparse path instead of allocating quadratic scratch.
    """
    if size > 512:
        return _symbolic_sparse(rows, cols, size, interior)
    return _symbolic_dense(np.asarray(rows, dtype=np.int64), np.asarray(cols, dtype=np.int64), size, interior)


class DomainPlan:
    """Structural assembly plan plus private, per-solve numeric workspace."""
    def __init__(self, buses, ybus, parts):
        started = time.perf_counter()
        n = len(buses)
        self.pq = np.flatnonzero([b['subtype'] == ' ' for b in buses])
        self.ns = np.flatnonzero([b['subtype'] != 'S' for b in buses])
        theta = np.full(n, -1, dtype=np.int64)
        magnitude = theta.copy()
        theta[self.ns] = np.arange(len(self.ns))
        magnitude[self.pq] = len(self.ns) + np.arange(len(self.pq))
        self.unknowns = len(self.ns) + len(self.pq)
        coo = ybus.tocoo()
        self.yrow, self.ycol, self.ydata = coo.row, coo.col, coo.data
        self.diagonal = coo.row == coo.col
        row, col = [], []
        self.masks = []
        for a, b in ((theta, theta), (theta, magnitude), (magnitude, theta), (magnitude, magnitude)):
            mask = (a[coo.row] >= 0) & (b[coo.col] >= 0)
            self.masks.append(mask)
            row.append(a[coo.row[mask]])
            col.append(b[coo.col[mask]])
        self.row, self.col = np.concatenate(row), np.concatenate(col)
        labels = np.full(n, -1, dtype=np.int64)
        for i, part in enumerate(parts):
            labels[part['internal']] = i
        # BOTH endpoints of every inter-domain edge become coordinator buses.
        # This preserves distribution-to-distribution cuts, loops and main ties.
        boundary = labels < 0
        cut = labels[coo.row] != labels[coo.col]
        boundary[coo.row[cut]] = True
        boundary[coo.col[cut]] = True
        unknown_bus = np.r_[self.ns, self.pq]
        self.interface = np.flatnonzero(boundary[unknown_bus])
        self.interface_buses = np.flatnonzero(boundary)
        self.interface_theta_mask = boundary[self.ns]
        self.interface_magnitude_mask = boundary[self.pq]
        central = np.full(self.unknowns, -1, dtype=np.int64)
        central[self.interface] = np.arange(len(self.interface))
        owner = labels[unknown_bus].copy()
        owner[boundary[unknown_bus]] = -1
        self.central_entries = (owner[self.row] < 0) & (owner[self.col] < 0)
        self.central_rows = central[self.row[self.central_entries]]
        self.central_cols = central[self.col[self.central_entries]]
        entry_owner = np.maximum(owner[self.row], owner[self.col])
        if np.any((owner[self.row] >= 0) & (owner[self.col] >= 0) & (owner[self.row] != owner[self.col])):
            raise ValueError('A cross-partition edge was not promoted to the interface')
        indices = np.argsort(entry_owner, kind='stable')
        sorted_owners = entry_owner[indices]
        offsets = np.searchsorted(sorted_owners, np.arange(len(parts) + 1))
        # The interior graph is block diagonal after promoting cut endpoints.
        # One RCM pass orders all disconnected interiors independently, avoiding
        # one SciPy matrix construction/order call for every tiny feeder.
        interior_mask = (owner[self.row] >= 0) & (owner[self.col] >= 0)
        adjacency = coo_matrix((np.ones(np.count_nonzero(interior_mask)),
                                (self.row[interior_mask], self.col[interior_mask])),
                               shape=(self.unknowns, self.unknowns)).tocsr()
        ordered = reverse_cuthill_mckee(adjacency, symmetric_mode=True)
        ordered = ordered[owner[ordered] >= 0]
        ordered = ordered[np.argsort(owner[ordered], kind='stable')]
        inside_offsets = np.searchsorted(owner[ordered], np.arange(len(parts) + 1))
        self.domains = []
        self.domain_part_ids = []
        domain_costs = []
        self.symbolic_cache_hits = 0
        symbolic_cache = {}
        values_count = rows_count = lower_count = upper_count = update_count = 0
        locator = np.empty(self.unknowns, dtype=np.int64)
        vp, lo, up, ud = [], [], [], []
        blocks = [0]
        gather_entries, gather_cells, initial_rhs, rhs_rows = [], [], [], []
        schur_cells, schur_rows, schur_cols = [], [], []
        interface_rhs, interface_rhs_rows = [], []
        boundary_solution_rows, boundary_solution_cols = [], []
        interior_solution_rows, interior_solution_cols = [], []
        for part_id, part in enumerate(parts):
            entries = indices[offsets[part_id]:offsets[part_id + 1]]
            inside = ordered[inside_offsets[part_id]:inside_offsets[part_id + 1]]
            if not len(inside):
                continue
            neighbors = np.unique(np.concatenate((self.row[entries], self.col[entries])))
            neighbors = neighbors[central[neighbors] >= 0]
            # RCM reduces fill within each small domain, never mixes domains.
            local_ids = np.concatenate((inside, neighbors))
            locator[local_ids] = np.arange(len(local_ids))
            rr, cc = locator[self.row[entries]], locator[self.col[entries]]
            count = len(inside)
            key = (count, len(local_ids), rr.tobytes(), cc.tobytes())
            if key not in symbolic_cache:
                symbolic = _symbolic(rr, cc, len(local_ids), count)
                table = symbolic[0]
                cells = table[rr, cc] if isinstance(table, np.ndarray) else np.fromiter(
                    (table[int(a), int(b)] for a, b in zip(rr, cc)), dtype=np.int64)
                symbolic_cache[key] = symbolic, cells
            else:
                self.symbolic_cache_hits += 1
            symbolic, cells = symbolic_cache[key]
            lookup, pivots, lower, upper, updates, boundary_cells = symbolic
            value_count = int(lookup.max(initial=-1)) + 1 if isinstance(lookup, np.ndarray) else len(lookup)
            gather_entries.append(entries)
            gather_cells.append(cells + values_count)
            p = pivots.copy()
            p[:, 0] += values_count
            p[:, 1] += rows_count
            p[:, 2:4] += lower_count
            p[:, 4:6] += upper_count
            p[:, 6] += update_count
            vp.append(p)
            l, u = lower.copy(), upper.copy()
            l[:, 0] += values_count; l[:, 1] += rows_count
            u[:, 0] += values_count; u[:, 1] += rows_count
            lo.append(l); up.append(u); ud.append(updates + values_count)
            lower_count += len(l); upper_count += len(u); update_count += len(updates)
            blocks.append(blocks[-1] + count)
            initial_rhs.append(local_ids[:count]); rhs_rows.append(rows_count + np.arange(count))
            ci = central[local_ids[count:]]
            boundary_cells = np.asarray(boundary_cells, dtype=np.int64).reshape(-1, 3)
            schur_cells.append(boundary_cells[:, 0] + values_count)
            schur_rows.append(ci[boundary_cells[:, 1]]); schur_cols.append(ci[boundary_cells[:, 2]])
            interface_rhs.append(ci); interface_rhs_rows.append(rows_count + np.arange(count, len(local_ids)))
            boundary_solution_rows.append(rows_count + np.arange(count, len(local_ids)))
            boundary_solution_cols.append(ci)
            interior_solution_rows.append(local_ids[:count]); interior_solution_cols.append(rows_count + np.arange(count))
            self.domains.append((entries, rr, cc, count, local_ids, values_count, rows_count, boundary_cells))
            self.domain_part_ids.append(part.get('id', part_id + 1))
            domain_costs.append(len(updates) + len(lower) + count)
            values_count += value_count; rows_count += len(local_ids)
        def array(x): return np.asarray(x, dtype=np.int64)
        self.pivots = np.concatenate(vp) if vp else np.empty((0, 7), dtype=np.int64)
        self.lowers = np.concatenate(lo) if lo else np.empty((0, 2), dtype=np.int64)
        self.uppers = np.concatenate(up) if up else np.empty((0, 2), dtype=np.int64)
        self.updates = np.concatenate(ud) if ud else array([])
        self.blocks = array(blocks)
        for name in ('gather_entries', 'gather_cells', 'initial_rhs', 'rhs_rows', 'schur_cells', 'schur_rows', 'schur_cols',
                     'interface_rhs', 'interface_rhs_rows', 'boundary_solution_rows', 'boundary_solution_cols',
                     'interior_solution_rows', 'interior_solution_cols'):
            source = locals()[name]
            setattr(self, name, np.concatenate(source) if source else array([]))
        self.values = np.zeros(values_count)
        self.rhs = np.zeros(rows_count)
        # LPT scheduling: spread expensive domains across contiguous OpenMP
        # chunks. Tiny disconnected feeders remain domains, not OS processes.
        self.domain_costs = np.array(domain_costs)
        self.schedule = np.arange(len(self.domains), dtype=np.int64)
        self.setup_ms = (time.perf_counter() - started) * 1000

    def schedule_workers(self, workers):
        bins = [[] for _ in range(min(workers, max(1, len(self.domains))))]
        loads = np.zeros(len(bins))
        # Equal task counts match the static prange chunks, cost ordering
        # distributes heavy domains instead of clustering the large feeders.
        capacity = [len(self.domains) // len(bins) + (i < len(self.domains) % len(bins)) for i in range(len(bins))]
        for task in np.argsort(-self.domain_costs, kind='stable'):
            choices = [i for i in range(len(bins)) if len(bins[i]) < capacity[i]]
            worker = min(choices, key=lambda i: loads[i])
            bins[worker].append(int(task)); loads[worker] += self.domain_costs[task]
        self.schedule = np.array([task for group in bins for task in group], dtype=np.int64)

    def jacobian_data(self, voltage, vm, current):
        i, j, y = self.yrow, self.ycol, self.ydata
        da = -1j * voltage[i] * np.conj(y * voltage[j])
        dm = voltage[i] * np.conj(y * (voltage[j] / vm[j]))
        diag = self.diagonal
        a = i[diag]
        da[diag] = 1j * voltage[a] * np.conj(current[a] - y[diag] * voltage[a])
        dm[diag] += np.conj(current[a]) * voltage[a] / vm[a]
        return np.r_[da[self.masks[0]].real, dm[self.masks[1]].real,
                     da[self.masks[2]].imag, dm[self.masks[3]].imag]

    def step(self, data, residual, progress=None):
        self.values.fill(0); self.rhs.fill(0)
        self.values[self.gather_cells] = data[self.gather_entries]
        self.rhs[self.rhs_rows] = -residual[self.initial_rhs]
        failed = np.zeros(len(self.domains), dtype=np.int64)
        worker_ids = np.zeros(len(self.domains), dtype=np.int64)
        durations = np.zeros(len(self.domains))
        if progress:
            progress('local-elimination', {})
        started = time.perf_counter()
        _eliminate(self.values, self.rhs, self.blocks, self.pivots, self.lowers, self.uppers, self.updates, failed, worker_ids, durations, self.schedule)
        # Rare unstable scalar pivots use local pivoted LU, still the same domains.
        fallback = []
        for part in np.flatnonzero(failed):
            entries, rr, cc, count, ids, vo, ro, boundary_cells = self.domains[part]
            local = coo_matrix((data[entries], (rr, cc)), shape=(len(ids), len(ids))).tocsc()
            factor = splu(local[:count, :count])
            transfer = factor.solve(local[:count, count:].toarray())
            forcing = factor.solve(-residual[ids[:count]])
            schur = -local[count:, :count] @ transfer
            for cell, i, j in boundary_cells: self.values[vo + cell] = schur[i, j]
            self.rhs[ro + count:ro + len(ids)] = -local[count:, :count] @ forcing
            fallback.append((part, ids, ro, count, forcing, transfer))
        eliminated = time.perf_counter()
        if progress:
            progress('boundary-coordination', {'local_ms': (eliminated - started) * 1000,
                'domain_part_ids': self.domain_part_ids, 'domain_worker_ids': worker_ids.tolist(),
                'domain_local_ms': durations.tolist()})
        coordination_started = time.perf_counter()
        rows = np.r_[self.central_rows, self.schur_rows]
        cols = np.r_[self.central_cols, self.schur_cols]
        values = np.r_[data[self.central_entries], self.values[self.schur_cells]]
        size = len(self.interface)
        schur = coo_matrix((values, (rows, cols)), shape=(size, size)).tocsc()
        rhs = -residual[self.interface].copy()
        np.add.at(rhs, self.interface_rhs, self.rhs[self.interface_rhs_rows])
        interface_step = spsolve(schur, rhs) if size else np.empty(0)
        coordinated = time.perf_counter()
        if progress:
            progress('back-substitution', {'coordinator_ms': (coordinated - coordination_started) * 1000,
                'max_interface_step': float(np.max(abs(interface_step), initial=0))})
        back_started = time.perf_counter()
        local_step = np.zeros(len(self.rhs))
        local_step[self.boundary_solution_rows] = interface_step[self.boundary_solution_cols]
        _back_substitute(self.values, self.rhs, self.blocks, self.pivots, self.uppers, local_step, failed, self.schedule)
        for part, ids, ro, count, forcing, transfer in fallback:
            local_step[ro:ro + count] = forcing - transfer @ local_step[ro + count:ro + len(ids)]
        step = np.empty(self.unknowns)
        step[self.interface] = interface_step
        step[self.interior_solution_rows] = local_step[self.interior_solution_cols]
        finished = time.perf_counter()
        # Verify the original linearized equations, including all cut edges.
        linear_error = np.bincount(self.row, weights=data * step[self.col], minlength=self.unknowns) + residual
        norm = float(np.max(abs(linear_error), initial=0))
        if not np.isfinite(step).all() or norm > max(1e-8, np.max(abs(residual), initial=0) * 1e-7):
            raise RuntimeError(f'Partition Schur linear residual too large: {norm:.3e}')
        return step, {'local_ms': (eliminated - started) * 1000,
                      'coordinator_ms': (coordinated - coordination_started) * 1000,
                      'back_substitution_ms': (finished - back_started) * 1000,
                      'linear_residual_pu': norm, 'pivoted_local_fallbacks': len(fallback),
                      'active_worker_threads': len(np.unique(worker_ids)), 'worker_thread_ids': np.unique(worker_ids).tolist(),
                      'domain_part_ids': self.domain_part_ids, 'domain_worker_ids': worker_ids.tolist(),
                      'domain_local_ms': durations.tolist(),
                      'worker_local_ms': np.bincount(worker_ids, weights=durations).tolist()}


def _domain_plan(buses, ybus, parts):
    """Reuse only an exact sparsity/type/domain match, binding fresh Y values.

    Every request has its own mutable numeric work arrays. Cached arrays are
    read-only, bounded in memory, and contain no voltage, admittance or factor.
    """
    started = time.perf_counter()
    ybus = ybus.tocsr()
    ownership = np.full(len(buses), -1, dtype=np.int64)
    for i, part in enumerate(parts):
        ownership[part['internal']] = i
    key = (ybus.shape, ybus.indptr.tobytes(), ybus.indices.tobytes(),
           tuple(b['subtype'] for b in buses), ownership.tobytes(),
           tuple(p.get('id', i + 1) for i, p in enumerate(parts)))
    with _structure_cache_lock:
        cached = _structure_cache.get(key)
        if cached is not None:
            _structure_cache.move_to_end(key)
    hit = cached is not None
    if not hit:
        plan = DomainPlan(buses, ybus, parts)
        # Freeze views rather than changing flags on a caller-owned CSR array.
        seen = {}
        def freeze(value):
            if isinstance(value, np.ndarray):
                if id(value) not in seen:
                    frozen = value.view()
                    frozen.flags.writeable = False
                    seen[id(value)] = frozen
                return seen[id(value)]
            if isinstance(value, (tuple, list)):
                return tuple(freeze(v) for v in value)
            return value
        excluded = {'values', 'rhs', 'ydata', 'setup_ms', 'schedule'}
        structure = {k: freeze(v) for k, v in vars(plan).items() if k not in excluded}
        size = sum(v.nbytes for v in seen.values()) + sum(len(v) for v in key if isinstance(v, bytes))
        cached = (structure, len(plan.values), len(plan.rhs), plan.setup_ms, size)
        if size <= _STRUCTURE_CACHE_BYTES:
            with _structure_cache_lock:
                _structure_cache[key] = cached
                _structure_cache.move_to_end(key)
                while (len(_structure_cache) > _STRUCTURE_CACHE_ENTRIES or
                       sum(v[4] for v in _structure_cache.values()) > _STRUCTURE_CACHE_BYTES):
                    _structure_cache.popitem(last=False)
    structure, values_count, rows_count, build_ms, size = cached
    plan = object.__new__(DomainPlan)
    vars(plan).update(structure)
    plan.ydata = ybus.tocoo().data
    plan.values = np.zeros(values_count)
    plan.rhs = np.zeros(rows_count)
    plan.schedule = np.arange(len(plan.domains), dtype=np.int64)
    plan.setup_ms = (time.perf_counter() - started) * 1000
    plan.structure_cache_hit = hit
    plan.structure_build_ms = build_ms
    plan.structure_bytes = size
    return plan


def solve_coupled(base, buses, ybus, parts, observer=None, workers=None, tolerance=1e-8, max_iterations=30):
    requested_workers = workers if workers is not None else os.environ.get('GMSS_WORKERS', available_cpus())
    workers = int(requested_workers)
    if workers < 1:
        raise ValueError('GMSS_WORKERS must be positive')
    previous_threads = get_num_threads()
    from numba import config
    set_num_threads(min(workers, available_cpus(), config.NUMBA_NUM_THREADS))
    actual_workers = get_num_threads()
    try:
        graph = ybus.copy(); graph.setdiag(0); graph.eliminate_zeros(); graph.data = np.ones(len(graph.data))
        count, labels = connected_components(graph, directed=False)
        slack = np.flatnonzero([b['subtype'] == 'S' for b in buses])
        if not np.all(np.bincount(labels[slack], minlength=count) == 1):
            raise ValueError('Each AC island must have exactly one BS reference')
        plan = _domain_plan(buses, ybus, parts)
        # Fewer domains than CPUs cannot use the remaining workers.
        set_num_threads(min(actual_workers, max(1, len(plan.domains))))
        actual_workers = get_num_threads()
        plan.schedule_workers(actual_workers)
        if observer:
            observer({'kind': 'setup', 'setup_ms': plan.setup_ms,
                      'structure_cache_hit': plan.structure_cache_hit,
                      'structure_build_ms': plan.structure_build_ms,
                      'structure_bytes': plan.structure_bytes,
                      'workers': actual_workers}, None)
        iteration_started = time.perf_counter()
        vm = np.array([1.0 if b['subtype'] == ' ' else b['vm'] for b in buses])
        va = np.deg2rad([b['va'] for b in buses])
        specified = np.array([complex(b['p'], b['q']) / base for b in buses])
        history, timings = [], []
        for iteration in range(max_iterations + 1):
            voltage = vm * np.exp(1j * va)
            current = ybus @ voltage
            mismatch = voltage * np.conj(current) - specified
            residual = np.r_[mismatch[plan.ns].real, mismatch[plan.pq].imag]
            norm = float(np.max(abs(residual), initial=0))
            history.append(norm)
            if observer:
                observer({'stage': 'partition-schur', 'iteration': iteration, 'max_mismatch_pu': norm,
                          'workers': actual_workers, 'partition_count': len(parts),
                          'available_cpus': available_cpus(),
                          'interface_unknowns': len(plan.interface), 'boundary_exchange': iteration > 0,
                          'coordination': timings[-1] if timings else None}, voltage.copy())
            if norm < tolerance:
                return voltage, {'solver': 'parallel distribution Schur-complement AC Newton',
                                 'newton_iterations': iteration, 'history': history, 'workers': actual_workers,
                                 'partition_count': len(parts), 'local_domains': len(plan.domains),
                                 'interface_bus_count': len(plan.interface_buses), 'interface_unknowns': len(plan.interface),
                                 'setup_ms': plan.setup_ms, 'steps': timings,
                                 'iteration_ms': (time.perf_counter() - iteration_started) * 1000,
                                 'structure_cache_hit': plan.structure_cache_hit,
                                 'structure_build_ms': plan.structure_build_ms,
                                 'structure_bytes': plan.structure_bytes,
                                 'cache_policy': 'exact sparse pattern, bus types and domain ownership; structural indices only; fresh numerical workspace and flat initial voltage',
                                 'available_cpus': available_cpus(), 'requested_workers': int(requested_workers),
                                 'execution': 'Numba CPU threads; independent domain LU, shared AC Schur coordinator',
                                 'initialization': 'flat PQ voltage; raw PV/BS voltage and BS angle; no global warm start'}
            if iteration == max_iterations:
                break
            def progress(stage, detail):
                if observer:
                    observer({'kind': 'phase', 'stage': stage, 'iteration': iteration + 1,
                              'workers': actual_workers, 'partition_count': len(parts),
                              'interface_unknowns': len(plan.interface), **detail}, None)
            step, timing = plan.step(plan.jacobian_data(voltage, vm, current), residual,
                                     progress if observer else None)
            timing['boundary_max_delta_angle_deg'] = float(np.rad2deg(np.max(abs(step[:len(plan.ns)][plan.interface_theta_mask]), initial=0)))
            timing['boundary_max_delta_voltage_pu'] = float(np.max(abs(step[len(plan.ns):][plan.interface_magnitude_mask]), initial=0))
            timings.append(timing)
            va[plan.ns] += step[:len(plan.ns)]
            vm[plan.pq] += step[len(plan.ns):]
            if not np.isfinite(vm).all() or (vm <= 0).any():
                raise RuntimeError('Partition Newton left the admissible voltage range')
        raise RuntimeError(f'Partition Newton did not converge: {history}')
    finally:
        set_num_threads(previous_threads)
