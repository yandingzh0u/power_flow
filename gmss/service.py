"""External API-style request adapter and compact response helpers."""

from typing import Any, Dict, Optional

from .core import (
    SERVICE_SCHEMA_VERSION,
    default_run_gmss_solver_config,
    describe_gmss_service_contract,
    make_gmss_service_request,
    normalize_run_gmss_solver_config,
    run_gmss_service,
    run_gmss_service_from_configs,
)

DATASET_KEYS = (
    "dataset_id",
    "scenario_id",
    "time_point",
    "use_feeder_profiles",
    "enable_microgrid",
    "demo_mg_count",
    "t_case_file",
    "nD",
    "mg_per_D",
    "mg_bus_count",
    "target_total_nodes",
    "mv_level_kv",
    "pcc_bus",
    "randomize_mg",
    "random_seed",
    "d_bus_count_hint",
    "prefer_balanced_mix",
    "reuse_if_match",
    "force_rebuild",
)

SOLVER_KEYS = (
    "t_solver",
    "t_partition_mode",
    "t_admm_max_iter",
    "t_admm_tol_vm",
    "t_admm_tol_va_deg",
    "t_use_monolithic_warm_start",
    "tol_dS_MW",
    "tol_dV_pu",
    "alpha",
    "beta",
    "parallel",
    "mg_parallel",
    "mg_coupling_iterations",
    "t_local_parallel",
    "q_v_slope_mvar_per_pu",
    "q_cap_mvar",
    "target_buses_per_part",
    "d_solver",
    "d_partition_mode",
    "d_n_parts",
    "d_local_parallel",
    "d_target_buses_per_part",
)


def make_api_request(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalize a compact external payload into the internal GMSS request."""
    payload = dict(payload or {})
    dataset_config = dict(payload.get("dataset_config") or {})
    solver_config = dict(payload.get("solver_config") or {})

    for key in DATASET_KEYS:
        if key in payload:
            dataset_config[key] = payload[key]

    if "n_parts" in payload:
        solver_config["t_n_parts"] = payload["n_parts"]
    if "t_n_parts" in payload:
        solver_config["t_n_parts"] = payload["t_n_parts"]
    if "max_iter" in payload:
        solver_config["max_iter"] = payload["max_iter"]
    for key in SOLVER_KEYS:
        if key in payload:
            solver_config[key] = payload[key]

    # Stable API defaults. Computational functions remain available for explicit soft mode.
    solver_config.setdefault("t_solver", "partitioned")
    solver_config.setdefault("t_partition_mode", "hard")

    request = make_gmss_service_request(dataset_config=dataset_config, solver_config=solver_config)
    request["task_id"] = str(payload.get("task_id", ""))
    request["model_id"] = str(payload.get("model_id", ""))
    request["scenario_id"] = str(payload.get("scenario_id", ""))
    return request


def simplify_response(response: Dict[str, Any], api_request: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the compact fields expected by an external caller."""
    request = response.get("request", {})
    api_request = api_request or {}
    gmss_result = response.get("gmss_result", {})
    node_summary = gmss_result.get("node_summary", {})
    solver_config = gmss_result.get("solver_config", {})
    return {
        "schema_version": SERVICE_SCHEMA_VERSION,
        "status": response.get("status", "unknown"),
        "task_id": api_request.get("task_id", request.get("task_id", "")),
        "model_id": api_request.get("model_id", request.get("model_id", "")),
        "scenario_id": api_request.get("scenario_id", request.get("scenario_id", "")),
        "summary": {
            "dataset_id": node_summary.get("dataset_id"),
            "scenario_id": node_summary.get("scenario_id"),
            "bus_count": node_summary.get("bus_count"),
            "line_count": node_summary.get("line_count"),
            "trafo_count": node_summary.get("trafo_count"),
            "main_bus_count": node_summary.get("main_bus_count"),
            "distribution_bus_count": node_summary.get("distribution_bus_count"),
            "td_transformer_count": node_summary.get("td_transformer_count"),
            "td_boundary_bus_count": node_summary.get("td_boundary_bus_count"),
            "distribution_component_count": node_summary.get("distribution_component_count"),
            "nD": node_summary.get("nD"),
            "total_mg": node_summary.get("total_mg"),
            "total_nodes": node_summary.get("total_nodes"),
            "microgrid_enabled": node_summary.get("microgrid_enabled"),
            "microgrid_model_type": node_summary.get("microgrid_model_type"),
            "microgrid_bus_count": node_summary.get("microgrid_bus_count"),
            "microgrid_nodes_total": node_summary.get("microgrid_nodes_total"),
            "microgrid_p_mw_total": node_summary.get("microgrid_p_mw_total"),
            "microgrid_q_mvar_total": node_summary.get("microgrid_q_mvar_total"),
            "microgrid_coupling_iterations": node_summary.get("microgrid_coupling_iterations"),
            "microgrid_parallel": node_summary.get("microgrid_parallel"),
            "time_point": node_summary.get("time_point"),
            "feeder_profile_enabled": node_summary.get("feeder_profile_enabled"),
            "feeder_profile_matched_rows": node_summary.get("feeder_profile_matched_rows"),
            "feeder_profile_updated_buses": node_summary.get("feeder_profile_updated_buses"),
            "d_partition_enabled": node_summary.get("d_partition_enabled"),
            "d_partition_mode": node_summary.get("d_partition_mode"),
            "d_n_parts": node_summary.get("d_n_parts"),
            "d_partition_converged": node_summary.get("d_partition_converged"),
            "d_partition_failed": node_summary.get("d_partition_failed"),
            "d_graph_algorithm": node_summary.get("d_graph_algorithm"),
            "d_metis_partition_count": node_summary.get("d_metis_partition_count"),
            "d_metis_split_component_count": node_summary.get("d_metis_split_component_count"),
            "d_metis_total_edge_cuts": node_summary.get("d_metis_total_edge_cuts"),
            "d_metis_connectivity_repairs": node_summary.get("d_metis_connectivity_repairs"),
            "d_metis_tiny_group_merges": node_summary.get("d_metis_tiny_group_merges"),
            "t_solver": solver_config.get("t_solver"),
            "t_partition_mode": solver_config.get("t_partition_mode"),
            "t_n_parts": solver_config.get("t_n_parts"),
            "d_solver": solver_config.get("d_solver"),
            "d_local_parallel": solver_config.get("d_local_parallel"),
            "d_target_buses_per_part": solver_config.get("d_target_buses_per_part"),
        },
        "result": gmss_result.get("last_state", {}),
        "artifacts": response.get("artifacts", {}),
    }


def run_api_request(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    request = make_api_request(payload)
    return simplify_response(run_gmss_service(request), request)


__all__ = [
    "SERVICE_SCHEMA_VERSION",
    "DATASET_KEYS",
    "SOLVER_KEYS",
    "default_run_gmss_solver_config",
    "normalize_run_gmss_solver_config",
    "make_gmss_service_request",
    "describe_gmss_service_contract",
    "run_gmss_service",
    "run_gmss_service_from_configs",
    "make_api_request",
    "simplify_response",
    "run_api_request",
]
