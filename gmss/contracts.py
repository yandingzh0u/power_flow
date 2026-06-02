"""Public API contract helpers for GMSS callers."""

from typing import Any, Dict

from .core import SERVICE_SCHEMA_VERSION
from .service import make_api_request


def example_compact_request() -> Dict[str, Any]:
    return {
        "task_id": "task_001",
        "model_id": "case_ACTIVSg25k_plus_500D",
        "scenario_id": "default_snapshot",
        "t_case_file": "case_ACTIVSg25k.m",
        "nD": 500,
        "mg_per_D": 3,
        "mg_bus_count": 46,
        "target_total_nodes": 100000,
        "n_parts": 4,
        "max_iter": 10,
        "t_solver": "partitioned",
        "t_partition_mode": "hard",
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
