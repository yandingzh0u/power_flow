"""One-click GMSS API caller."""

import argparse
import json
import sys
from urllib import request as urlrequest
from urllib.error import HTTPError


def post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {exc.reason}", file=sys.stderr)
        if body:
            print(body, file=sys.stderr)
        raise SystemExit(1) from exc


def preview_value(value):
    if isinstance(value, list):
        return value[:3]
    if isinstance(value, dict):
        preview = dict(value)
        partitions = preview.get("partitions")
        if isinstance(partitions, list):
            preview["partition_count"] = len(partitions)
            preview["partitions_preview"] = partitions[:3]
            preview.pop("partitions", None)
        return preview
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Call the local GMSS API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--dataset", default="demo_area", help="dataset id: demo_area or synthetic")
    parser.add_argument("--scenario", default="13点", help="demo_area supports 13点 or 20点")
    parser.add_argument("--normalize-only", action="store_true", help="only normalize request; do not run calculation")
    parser.add_argument("--full", action="store_true", help="print full API response")
    parser.add_argument("--timeout", type=float, default=7200.0)
    parser.add_argument("--max-iter", type=int, default=10)
    parser.add_argument("--time-point", default="", help="demo_area feeder profile time, e.g. 13:00 or 14:15")
    parser.add_argument("--no-feeder-profiles", action="store_true", help="disable demo_area feeder XLS profiles")
    args = parser.parse_args()

    payload = {
        "task_id": "demo_task_001",
        "dataset_id": args.dataset,
        "scenario_id": args.scenario,
        "max_iter": args.max_iter,
        "alpha": 0.6,
        "beta": 0.6,
    }
    if args.dataset.strip().lower() in {"demo_area", "demo", "示范区"}:
        payload.update({
            "model_id": "demo_area_pandapower",
            "t_solver": "pandapower_direct",
            "t_partition_mode": "none",
            "d_solver": "partitioned",
            "d_partition_mode": "physical_component",
            "d_n_parts": 0,
            "d_local_parallel": True,
            "d_target_buses_per_part": 400,
            "time_point": args.time_point,
            "use_feeder_profiles": not args.no_feeder_profiles,
        })
    else:
        payload.update({
            "model_id": "case_ACTIVSg25k_plus_500D",
            "t_case_file": "case_ACTIVSg25k.m",
            "nD": 500,
            "mg_per_D": 3,
            "mg_bus_count": 46,
            "target_total_nodes": 100000,
            "n_parts": 4,
            "t_solver": "partitioned",
            "t_partition_mode": "hard",
        })

    endpoint = "/api/v1/gmss/normalize" if args.normalize_only else "/api/v1/gmss/run"
    result = post_json(args.base_url.rstrip("/") + endpoint, payload, timeout=args.timeout)
    if args.full:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.normalize_only:
        req = result["request"]
        dataset = req["dataset_config"]
        solver = req["solver_config"]
        compact = {
            "status": result.get("status"),
            "task_id": req.get("task_id"),
            "model_id": req.get("model_id"),
            "scenario_id": req.get("scenario_id"),
            "dataset_config": {
                "dataset_id": dataset.get("dataset_id"),
                "scenario_id": dataset.get("scenario_id"),
                "t_case_file": dataset.get("t_case_file"),
                "nD": dataset.get("nD"),
                "mg_per_D": "3 each" if len(set(dataset.get("mg_per_D", []))) == 1 else dataset.get("mg_per_D", [])[:10],
                "mg_bus_count": dataset.get("mg_bus_count"),
                "target_total_nodes": dataset.get("target_total_nodes"),
                "time_point": dataset.get("time_point"),
                "use_feeder_profiles": dataset.get("use_feeder_profiles"),
            },
            "solver_config": {
                "t_solver": solver.get("t_solver"),
                "t_partition_mode": solver.get("t_partition_mode"),
                "t_n_parts": solver.get("t_n_parts"),
                "d_solver": solver.get("d_solver"),
                "d_partition_mode": solver.get("d_partition_mode"),
                "d_n_parts": solver.get("d_n_parts"),
                "d_local_parallel": solver.get("d_local_parallel"),
                "d_target_buses_per_part": solver.get("d_target_buses_per_part"),
                "max_iter": solver.get("max_iter"),
                "alpha": solver.get("alpha"),
                "beta": solver.get("beta"),
            },
        }
        print(json.dumps(compact, ensure_ascii=False, indent=2))
        return

    compact = {
        "status": result.get("status"),
        "task_id": result.get("task_id"),
        "model_id": result.get("model_id"),
        "scenario_id": result.get("scenario_id"),
        "summary": result.get("summary", {}),
        "result_preview": {
            key: preview_value(value)
            for key, value in result.get("result", {}).items()
        },
        "artifacts": result.get("artifacts", {}),
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
