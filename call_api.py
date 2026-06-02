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
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Call the local GMSS API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--normalize-only", action="store_true", help="only normalize request; do not run calculation")
    parser.add_argument("--full", action="store_true", help="print full API response")
    parser.add_argument("--timeout", type=float, default=7200.0)
    parser.add_argument("--max-iter", type=int, default=10)
    args = parser.parse_args()

    payload = {
        "task_id": "demo_task_001",
        "model_id": "case_ACTIVSg25k_plus_500D",
        "scenario_id": "default_snapshot",
        "t_case_file": "case_ACTIVSg25k.m",
        "nD": 500,
        "mg_per_D": 3,
        "mg_bus_count": 46,
        "target_total_nodes": 100000,
        "n_parts": 4,
        "max_iter": args.max_iter,
        "t_solver": "partitioned",
        "t_partition_mode": "hard",
        "alpha": 0.6,
        "beta": 0.6,
    }

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
                "t_case_file": dataset.get("t_case_file"),
                "nD": dataset.get("nD"),
                "mg_per_D": "3 each" if len(set(dataset.get("mg_per_D", []))) == 1 else dataset.get("mg_per_D", [])[:10],
                "mg_bus_count": dataset.get("mg_bus_count"),
                "target_total_nodes": dataset.get("target_total_nodes"),
            },
            "solver_config": {
                "t_solver": solver.get("t_solver"),
                "t_partition_mode": solver.get("t_partition_mode"),
                "t_n_parts": solver.get("t_n_parts"),
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
