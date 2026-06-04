"""Public API contract helpers for GMSS callers."""

from typing import Any, Dict

from .core import SERVICE_SCHEMA_VERSION
from .service import make_api_request


def example_compact_request() -> Dict[str, Any]:
    return {
        "task_id": "task_001",
        "model_id": "demo_area_pandapower",
        "dataset_id": "demo_area",
        "scenario_id": "13点",
        "time_point": "13:00",
        "use_feeder_profiles": True,
        "enable_microgrid": True,
        "demo_mg_count": 6,
        "mg_bus_count": 33,
        "max_iter": 10,
        "t_solver": "pandapower_direct",
        "t_partition_mode": "none",
        "d_solver": "partitioned",
        "d_partition_mode": "metis",
        "d_n_parts": 0,
        "d_local_parallel": True,
        "d_target_buses_per_part": 150,
        "mg_parallel": True,
        "mg_coupling_iterations": 2,
        "alpha": 0.6,
        "beta": 0.6,
    }


def describe_public_contract() -> Dict[str, Any]:
    normalized = make_api_request(example_compact_request())
    return {
        "schema_version": SERVICE_SCHEMA_VERSION,
        "entry_points": {
            "make_api_request(payload)": "normalize compact external payload without running calculation",
            "run_api_request(payload)": "run calculation and return compact response",
            "run_gmss_service(request)": "run internal request with full dataset_index and gmss_result",
        },
        "compact_request_example": example_compact_request(),
        "normalized_request_example": normalized,
        "response_keys": [
            "schema_version",
            "status",
            "task_id",
            "model_id",
            "scenario_id",
            "summary",
            "result",
            "artifacts",
        ],
    }


__all__ = ["example_compact_request", "describe_public_contract"]
