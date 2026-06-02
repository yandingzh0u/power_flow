"""Command-line entry point for the GMSS workflow."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gmss.core import MultiConfig, run_gmss_multi, synthesize_dataset_multi
from gmss.memory import plot_memory_curve, start_memory_monitor, stop_memory_monitor


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GMSS synthesis and iterative power flow.")
    parser.add_argument("--t-case-file", default="case_ACTIVSg25k.m")
    parser.add_argument("--nD", type=int, default=500)
    parser.add_argument("--mg-per-D", type=int, default=3)
    parser.add_argument("--mg-bus-count", type=int, default=46)
    parser.add_argument("--target-total-nodes", type=int, default=100000)
    parser.add_argument("--synthesize-only", action="store_true")
    parser.add_argument("--max-iter", type=int, default=10)
    parser.add_argument("--t-solver", default="partitioned", choices=["partitioned", "monolithic"])
    parser.add_argument("--t-partition-mode", default="hard", choices=["hard", "soft"])
    parser.add_argument("--t-n-parts", type=int, default=4)
    parser.add_argument("--t-admm-max-iter", type=int, default=8)
    parser.add_argument("--t-admm-tol-vm", type=float, default=2e-3)
    parser.add_argument("--t-admm-tol-va-deg", type=float, default=0.2)
    parser.add_argument("--warm-start", action="store_true")
    parser.add_argument("--alpha", type=float, default=0.6)
    parser.add_argument("--beta", type=float, default=0.6)
    parser.add_argument("--no-memory-monitor", action="store_true")
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args()

    cfg = MultiConfig(
        t_case_file=args.t_case_file,
        nD=args.nD,
        mg_per_D=[args.mg_per_D] * args.nD,
        mg_bus_count=args.mg_bus_count,
        target_total_nodes=args.target_total_nodes,
    )
    index = synthesize_dataset_multi(cfg)
    print(json.dumps(index.get("node_summary", {}), ensure_ascii=False, indent=2))

    if args.synthesize_only:
        return

    th = None
    if not args.no_memory_monitor:
        th = start_memory_monitor()
    try:
        run_gmss_multi(
            max_iter=args.max_iter,
            t_solver=args.t_solver,
            t_partition_mode=args.t_partition_mode,
            t_n_parts=args.t_n_parts,
            t_use_monolithic_warm_start=args.warm_start,
            t_admm_max_iter=args.t_admm_max_iter,
            t_admm_tol_vm=args.t_admm_tol_vm,
            t_admm_tol_va_deg=args.t_admm_tol_va_deg,
            alpha=args.alpha,
            beta=args.beta,
        )
    finally:
        if th is not None:
            stop_memory_monitor(th)

    if th is not None and not args.no_plot:
        plot_memory_curve("长时序连续运行内存监控结果（主流程）")


if __name__ == "__main__":
    main()
