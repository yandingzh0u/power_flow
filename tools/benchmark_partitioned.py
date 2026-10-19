"""Reproducible, alternating NR/parallel-domain benchmark on unchanged DAT.

No local post-validation is added to NR. Every timed call reads the DAT again,
starts from flat voltage, and checks all original bus equations. Cold first
request is recorded separately, never silently dropped from the report.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gmss.raw_solver import solve
from gmss.partition import available_cpus


def measure(source, algorithm, pipeline='raw'):
    started = time.perf_counter()
    if pipeline == 'page':
        from gmss.core import run_case
        events = []
        result = run_case(source, algorithm, emit=lambda k, d: events.append((k, d)))
        run, scene = result['run'], result['scenario']
        detail = run.get('solver_details') or {}
        voltage = np.array([b['vm'] * np.exp(1j * np.deg2rad(b['va'])) for b in scene['buses']])
        power = np.array([[b[k] for k in ('p', 'q', 'p_to', 'q_to')]
                          for b in scene['lines'] + scene['trafos']])
        row = {'algorithm': algorithm, 'solver_ms': run['solver_ms'],
               'residual_mva': run['residual_mva'], 'iterations': run['iterations'],
               'bus_count': scene['bus_count'], 'branch_count': len(power), 'details': detail,
               'partitioning': run.get('partitioning'), 'event_count': len(events)}
    else:
        result = solve(source, algorithm=algorithm, include_results=True)
        detail = result.get('solver_details') or {}
        voltage = np.array([b['vm_pu'] * np.exp(1j * np.deg2rad(b['va_degree'])) for b in result['bus_results']])
        power = np.array([[b['p_from_mw'], b['q_from_mvar'], b['p_to_mw'], b['q_to_mvar']] for b in result['branch_results']])
        row = {'algorithm': algorithm, 'solver_ms': result['numerical_solve_ms'],
               'residual_mva': result['original_bus_equation_residual_mva'], 'iterations': result['iterations'],
               'bus_count': result['bus_count'], 'branch_count': result['branch_count'], 'details': detail,
               'partitioning': result.get('partitioning')}
    wall_ms = (time.perf_counter() - started) * 1000
    row.update(wall_ms=wall_ms, setup_ms=detail.get('setup_ms', 0),
               iteration_ms=detail.get('iteration_ms', row['solver_ms']),
               structure_cache_hit=detail.get('structure_cache_hit', False))
    return row, voltage, power


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', nargs='+', choices=('13', '20'), default=['13', '20'])
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--folder', type=Path, default=ROOT / 'raw_dataset/extracted')
    parser.add_argument('--pipeline', choices=('raw', 'page'), default='raw')
    parser.add_argument('--uncached-repeats', type=int, default=3,
                        help='Clear both structural caches before each call; compiled kernels remain loaded')
    parser.add_argument('--workers', type=int, default=available_cpus())
    parser.add_argument('--worker-sweep', nargs='*', type=int, default=[])
    parser.add_argument('--target-sweep', nargs='*', type=int, default=[])
    parser.add_argument('--compare-serial', action='store_true')
    parser.add_argument('--assert-speedup', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/benchmarks/weighted_original.json')
    args = parser.parse_args()
    if args.repeats < 3 or args.uncached_repeats < 0 or args.workers < 1 or any(w<1 for w in args.worker_sweep) or any(t<2 for t in args.target_sweep):
        parser.error('Use at least 3 repetitions and a positive worker count')
    os.environ['GMSS_WORKERS'] = str(args.workers)
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'python': sys.version,
              'platform': platform.platform(), 'cpu_affinity': sorted(os.sched_getaffinity(0)),
              'workers': args.workers, 'tolerance_pu': 1e-8,
              'impedance_assumption': 'T cards interpreted on 100 MVA; L cards on declared system base; source bytes unchanged',
              'pipeline': args.pipeline,
              'scope': 'fresh DAT/model, original KCL and all branch results; page additionally includes real iteration telemetry and topology, excludes HTTP/persistence/rendering',
              'cache_policy': 'structural indices only; first request, repeated runs, and cleared structural caches reported separately; every call starts flat and reassembles numeric matrices',
              'initialization': 'identical flat PQ; raw controlled voltage; no cached solution or numerical factorization', 'cases': {}}
    failures = []
    for hour in args.hours:
        source = args.folder / f'{hour}点sim2合并anony.dat'
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        first, _, _ = measure(source, 'partitioned-nr', args.pipeline)
        print(hour, 'first request', round(first['solver_ms'], 2), 'ms', flush=True)
        rows, reference_v, reference_s = [], None, None
        max_v = max_s = 0.
        for repeat in range(args.repeats):
            methods = ('nr', 'partitioned-nr') if repeat % 2 == 0 else ('partitioned-nr', 'nr')
            for method in methods:
                row, voltage, power = measure(source, method, args.pipeline)
                if reference_v is None:
                    reference_v, reference_s = voltage, power
                max_v = max(max_v, float(np.max(abs(voltage-reference_v))))
                max_s = max(max_s, float(np.max(abs(power-reference_s))))
                rows.append(row)
                print(hour, repeat + 1, method, round(row['solver_ms'], 2), 'ms; wall', round(row['wall_ms'], 2), 'ms', flush=True)
        medians = {m: {field: statistics.median(r[field] for r in rows if r['algorithm'] == m)
                       for field in ('solver_ms', 'wall_ms')} for m in ('nr', 'partitioned-nr')}
        means = {m: {field: statistics.mean(r[field] for r in rows if r['algorithm'] == m)
                     for field in ('solver_ms', 'wall_ms')} for m in ('nr', 'partitioned-nr')}
        speedup = {field: medians['nr'][field] / medians['partitioned-nr'][field] for field in ('solver_ms', 'wall_ms')}
        serial = []
        if args.compare_serial:
            os.environ['GMSS_WORKERS'] = '1'
            for _ in range(3):
                row, voltage, power = measure(source, 'partitioned-nr', args.pipeline)
                np.testing.assert_allclose(voltage, reference_v, atol=1e-8, rtol=0)
                serial.append(row)
            os.environ['GMSS_WORKERS'] = str(args.workers)
        sweep = {}
        for worker_count in args.worker_sweep:
            os.environ['GMSS_WORKERS'] = str(worker_count)
            samples = []
            for repeat in range(3):
                row, voltage, power = measure(source, 'partitioned-nr', args.pipeline)
                np.testing.assert_allclose(voltage, reference_v, atol=1e-8, rtol=0)
                np.testing.assert_allclose(power, reference_s, atol=1e-5, rtol=0)
                samples.append(row)
                print(hour,'worker sweep',worker_count,repeat+1,round(row['solver_ms'],2),'ms',flush=True)
            sweep[str(worker_count)] = {'samples': samples,
                'median_solver_ms': statistics.median(r['solver_ms'] for r in samples),
                'median_wall_ms': statistics.median(r['wall_ms'] for r in samples)}
        os.environ['GMSS_WORKERS'] = str(args.workers)
        target_sweep = {}
        for target in args.target_sweep:
            os.environ['GMSS_PARTITION_TARGET'] = str(target)
            samples=[]
            for repeat in range(3):
                row, voltage, power = measure(source,'partitioned-nr', args.pipeline)
                np.testing.assert_allclose(voltage,reference_v,atol=1e-8,rtol=0)
                np.testing.assert_allclose(power,reference_s,atol=1e-5,rtol=0)
                samples.append(row)
                print(hour,'target sweep',target,repeat+1,round(row['solver_ms'],2),'ms',flush=True)
            target_sweep[str(target)]={'samples':samples,
                'median_solver_ms':statistics.median(r['solver_ms'] for r in samples),
                'median_wall_ms':statistics.median(r['wall_ms'] for r in samples)}
        os.environ.pop('GMSS_PARTITION_TARGET',None)
        uncached = []
        from gmss.coupled import clear_structure_cache
        from gmss.partition import clear_partition_cache
        for repeat in range(args.uncached_repeats):
            for method in (('nr', 'partitioned-nr') if repeat % 2 == 0 else ('partitioned-nr', 'nr')):
                clear_structure_cache(); clear_partition_cache()
                row, voltage, power = measure(source, method, args.pipeline)
                np.testing.assert_allclose(voltage, reference_v, atol=1e-8, rtol=0)
                np.testing.assert_allclose(power, reference_s, atol=1e-5, rtol=0)
                uncached.append(row)
        uncached_medians = {m: {field: statistics.median(r[field] for r in uncached if r['algorithm'] == m)
                               for field in ('solver_ms', 'wall_ms', 'setup_ms', 'iteration_ms')}
                           for m in ('nr', 'partitioned-nr')} if uncached else {}
        assert max_v < 1e-6 and max_s < 1e-3, (max_v, max_s)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        report['cases'][hour] = {'source': str(source), 'sha256': digest,
                                'first_request': first, 'samples': rows, 'medians': medians, 'speedup': speedup,
                                'means': means,
                                'max_complex_voltage_difference_pu': max_v,
                                'max_branch_power_component_difference_mva': max_s, 'serial_partition_samples': serial,
                                'worker_sweep': sweep,'target_sweep':target_sweep,
                                'uncached_samples': uncached, 'uncached_medians': uncached_medians}
        if speedup['solver_ms'] <= 1 or speedup['wall_ms'] <= 1:
            failures.append(hour)
        print(hour, 'medians', medians, 'speedup', speedup, flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
    if args.assert_speedup and failures:
        raise SystemExit(f'No measured speedup for cases {failures}; see {args.output}')


if __name__ == '__main__':
    main()
