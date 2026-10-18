from __future__ import annotations

import copy
import json
import math
import multiprocessing as mp
import shutil
import statistics
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandapower as pp
from matplotlib.collections import LineCollection
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from gmss import core


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "课题1-推进会-任务2-0302(1)_补充算例版.pptx"
BACKUP = ROOT / "课题1-推进会-任务2-0302(1)_算例重构前备份.pptx"
OUTPUT = ROOT / "课题1-推进会-任务2-0302(1)_算例重构版.pptx"
ASSET_DIR = ROOT / "_case_study_assets_v2"

NETWORK_JSON = ROOT / "data/demo_area/converted/demo_area_20_pandapower.json"
FEEDER_JSON = ROOT / "data/demo_area/converted/demo_area_feeders.json"

TEAL = RGBColor(0x00, 0x78, 0x73)
DEEP_TEAL = RGBColor(0x00, 0x66, 0x66)
BLUE = RGBColor(0x1F, 0x66, 0xB1)
GREEN = RGBColor(0x4F, 0x91, 0x35)
ORANGE = RGBColor(0xE9, 0x70, 0x0A)
BLACK = RGBColor(0x16, 0x20, 0x24)
GRAY = RGBColor(0x5F, 0x69, 0x70)
LIGHT_LINE = RGBColor(0xC9, 0xD5, 0xD9)
PALE = RGBColor(0xF4, 0xF7, 0xF7)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

MPL_BLUE = "#1F66B1"
MPL_TEAL = "#007873"
MPL_GREEN = "#4F9135"
MPL_ORANGE = "#E9700A"
MPL_PURPLE = "#6D49A8"
MPL_RED = "#D92D2D"
MPL_BLACK = "#162024"
MPL_GRAY = "#667278"
MPL_LIGHT = "#D6E0E3"
CJK_FONT_PATH = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")


def configure_plot_fonts():
    if CJK_FONT_PATH.exists():
        font_manager.fontManager.addfont(str(CJK_FONT_PATH))
        family = font_manager.FontProperties(fname=str(CJK_FONT_PATH)).get_name()
    else:
        family = "DejaVu Sans"
    plt.rcParams.update(
        {
            "font.family": family,
            "font.sans-serif": [family, "DejaVu Sans"],
            "axes.unicode_minus": False,
            "mathtext.fontset": "dejavusans",
        }
    )


def timed_partition_worker(args):
    start = time.perf_counter()
    part_id, result = core._solve_demo_d_part_worker(*args)
    return int(part_id), result, float(time.perf_counter() - start)


def partition_worker_args(net, part):
    return (
        net,
        int(part["id"]),
        [int(x) for x in part["part_buses"]],
        {int(bus): tuple(value) for bus, value in part["boundary_fixed"].items()},
        [int(x) for x in part["internal_buses"]],
    )


def run_partition_once(net, parts, workers):
    started = time.perf_counter()
    args = [partition_worker_args(net, part) for part in parts]
    if workers == 1:
        raw = [timed_partition_worker(item) for item in args]
    else:
        with ProcessPoolExecutor(
            max_workers=int(workers), mp_context=mp.get_context("fork")
        ) as pool:
            raw = list(pool.map(timed_partition_worker, args))
    elapsed = float(time.perf_counter() - started)
    results = {int(part_id): result for part_id, result, _ in raw}
    task_times = [float(task_time) for _, _, task_time in raw]
    return elapsed, results, task_times


def benchmark_partition_solver(net, parts):
    worker_counts = [1, 2, 4, 8]
    medians = []
    all_runs = {}
    serial_results = None
    reference_task_times = None
    comparison_results = None
    for workers in worker_counts:
        samples = []
        for repeat in range(3):
            elapsed, results, task_times = run_partition_once(net, parts, workers)
            samples.append(elapsed)
            if workers == 1 and repeat == 0:
                serial_results = results
                reference_task_times = task_times
            if workers == 4 and repeat == 0:
                comparison_results = results
        all_runs[str(workers)] = samples
        medians.append(float(statistics.median(samples)))

    speedups = [float(medians[0] / value) for value in medians]
    max_parallel_delta = 0.0
    if serial_results and comparison_results:
        for part_id, serial_map in serial_results.items():
            parallel_map = comparison_results.get(part_id, {})
            for bus, serial_value in serial_map.items():
                if bus not in parallel_map:
                    continue
                max_parallel_delta = max(
                    max_parallel_delta,
                    abs(float(serial_value[0]) - float(parallel_map[bus][0])),
                    abs(float(serial_value[1]) - float(parallel_map[bus][1])),
                )

    summaries = []
    for part in parts:
        result = (serial_results or {}).get(int(part["id"]))
        summaries.append(core._summarize_demo_distribution_partition_result(net, part, result))
    max_vm_error = max(
        float(item["max_vm_delta_vs_full_pu"] or 0.0) for item in summaries
    )
    max_va_error = max(
        float(item["max_va_delta_vs_full_deg"] or 0.0) for item in summaries
    )

    return {
        "workers": worker_counts,
        "median_seconds": medians,
        "speedups": speedups,
        "runs": all_runs,
        "task_seconds": reference_task_times or [],
        "all_converged": all(bool(item.get("converged")) for item in summaries),
        "max_vm_error_pu": max_vm_error,
        "max_va_error_deg": max_va_error,
        "max_serial_parallel_delta": float(max_parallel_delta),
    }


def run_coordination(net, parts):
    cfg = core.MultiConfig(
        dataset_id="demo_area",
        scenario_id="current_snapshot",
        enable_microgrid=True,
        demo_mg_count=6,
        mg_bus_count=33,
        randomize_mg=True,
        random_seed=42,
    )
    specs = core._select_demo_microgrid_specs(net, parts, cfg)
    solver_cfg = {
        "q_v_slope_mvar_per_pu": 0.10,
        "q_cap_mvar": 0.10,
        "mg_parallel": True,
        "max_iter": 50,
    }
    alpha = 0.6
    eps_s = 1e-4
    eps_v = 1e-6
    eps_theta = 1e-3
    injections = [(0.0, 0.0) for _ in specs]
    previous_vm = net.res_bus.vm_pu.copy()
    previous_va = net.res_bus.va_degree.copy()
    residual_s = []
    residual_v = []
    residual_theta = []
    iteration_seconds = []

    for _ in range(12):
        started = time.perf_counter()
        pcc_voltages = [
            float(net.res_bus.at[int(spec["pcc_bus"]), "vm_pu"]) for spec in specs
        ]
        raw = core._solve_demo_microgrid_specs(specs, pcc_voltages, solver_cfg)
        updated = [
            (
                (1.0 - alpha) * old_p + alpha * new_p,
                (1.0 - alpha) * old_q + alpha * new_q,
            )
            for (old_p, old_q), (new_p, new_q) in zip(injections, raw)
        ]
        residual_s.append(
            max(
                math.hypot(new_p - old_p, new_q - old_q)
                for (old_p, old_q), (new_p, new_q) in zip(injections, updated)
            )
        )
        core._apply_demo_microgrid_equivalents(net, specs, updated)
        pp.runpp(
            net,
            algorithm="nr",
            init="results",
            max_iteration=50,
            tolerance_mva=1e-8,
            enforce_q_lims=False,
            calculate_voltage_angles=True,
            trafo_model="t",
        )
        residual_v.append(float((net.res_bus.vm_pu - previous_vm).abs().max()))
        residual_theta.append(float((net.res_bus.va_degree - previous_va).abs().max()))
        iteration_seconds.append(float(time.perf_counter() - started))
        injections = updated
        previous_vm = net.res_bus.vm_pu.copy()
        previous_va = net.res_bus.va_degree.copy()

    joint_iteration = None
    for index, (res_s, res_v, res_theta) in enumerate(
        zip(residual_s, residual_v, residual_theta), start=1
    ):
        if res_s <= eps_s and res_v <= eps_v and res_theta <= eps_theta:
            joint_iteration = int(index)
            break

    return {
        "alpha": alpha,
        "eps_s_mw": eps_s,
        "eps_v_pu": eps_v,
        "eps_theta_deg": eps_theta,
        "residual_s_mw": residual_s,
        "residual_v_pu": residual_v,
        "residual_theta_deg": residual_theta,
        "iteration_seconds": iteration_seconds,
        "joint_iteration": joint_iteration,
        "pcc_buses": [int(spec["pcc_bus"]) for spec in specs],
    }


def feeder_profiles():
    data = json.loads(FEEDER_JSON.read_text(encoding="utf-8"))
    profiles = []
    for feeder in data["feeders"]:
        rows = [np.asarray(row["p_mw"], dtype=float) for row in feeder["rows"]]
        profiles.append(np.sum(rows, axis=0))
    return np.asarray(profiles, dtype=float), list(data["time_labels"])


def compute_layout(graph):
    cache = ASSET_DIR / "actual_network_layout.json"
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))
        if len(raw) == len(graph.nodes):
            return {int(key): np.asarray(value, dtype=float) for key, value in raw.items()}
    positions = nx.spring_layout(
        graph,
        seed=22,
        iterations=90,
        k=0.045,
        method="energy",
        gravity=0.8,
    )
    cache.write_text(
        json.dumps(
            {str(int(node)): [float(value[0]), float(value[1])] for node, value in positions.items()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return positions


def panel_style(ax, title, color=MPL_BLUE):
    ax.set_title(
        title,
        loc="left",
        fontsize=12.5,
        fontweight="bold",
        color=color,
        pad=9,
    )
    for spine in ax.spines.values():
        spine.set_color(MPL_LIGHT)
        spine.set_linewidth(0.9)
    ax.set_facecolor("white")


def figure_border(fig):
    fig.add_artist(
        FancyBboxPatch(
            (0.004, 0.006),
            0.992,
            0.988,
            transform=fig.transFigure,
            boxstyle="round,pad=0.003,rounding_size=0.008",
            facecolor="white",
            edgecolor="#C9D5D9",
            linewidth=1.0,
            zorder=-10,
        )
    )


def draw_graph(ax, graph, positions, node_colors, node_sizes, pcc_buses):
    segments = []
    for edge in graph.edges():
        left, right = edge[0], edge[1]
        segments.append([positions[int(left)], positions[int(right)]])
    ax.add_collection(
        LineCollection(segments, colors="#AEB8BD", linewidths=0.34, alpha=0.56, zorder=1)
    )
    nodes = list(graph.nodes)
    coords = np.asarray([positions[int(node)] for node in nodes])
    ax.scatter(
        coords[:, 0],
        coords[:, 1],
        s=node_sizes,
        c=node_colors,
        linewidths=0,
        alpha=0.96,
        zorder=2,
    )
    pcc = [bus for bus in pcc_buses if bus in positions]
    if pcc:
        pcc_xy = np.asarray([positions[int(bus)] for bus in pcc])
        ax.scatter(
            pcc_xy[:, 0],
            pcc_xy[:, 1],
            s=58,
            marker="*",
            facecolor=MPL_RED,
            edgecolor="white",
            linewidth=0.8,
            zorder=5,
        )
    ax.autoscale()
    ax.margins(0.035)
    ax.set_xticks([])
    ax.set_yticks([])


def make_case_setting_figure(net, graph, positions, parts, profiles, coordination, benchmark):
    configure_plot_fonts()
    path = ASSET_DIR / "case_setting_and_partition.png"
    fig = plt.figure(figsize=(15.6, 5.25), dpi=220, facecolor="white")
    figure_border(fig)
    grid = fig.add_gridspec(
        2,
        3,
        left=0.032,
        right=0.985,
        bottom=0.09,
        top=0.93,
        height_ratios=[3.25, 1.25],
        width_ratios=[3.05, 4.50, 4.50],
        hspace=0.42,
        wspace=0.23,
    )
    ax_profile = fig.add_subplot(grid[0, 0])
    ax_topology = fig.add_subplot(grid[0, 1])
    ax_partition = fig.add_subplot(grid[0, 2])
    ax_balance = fig.add_subplot(grid[1, 0:2])
    ax_status = fig.add_subplot(grid[1, 2])

    # Actual feeder profiles, aggregated over the day.
    hours = np.arange(profiles.shape[1], dtype=float) / 4.0
    aggregate = profiles.sum(axis=0)
    panel_style(ax_profile, "① 运行断面与源荷输入", MPL_BLUE)
    for profile in profiles:
        ax_profile.plot(hours, profile, color="#91AAB8", linewidth=0.45, alpha=0.30)
    ax_profile.fill_between(hours, aggregate, color=MPL_BLUE, alpha=0.12)
    ax_profile.plot(hours, aggregate, color=MPL_BLUE, linewidth=2.0, label="馈线聚合有功")
    ax_profile.axhline(0.0, color="#9AA7AD", linewidth=0.7)
    ax_profile.set_xlim(0, 23.75)
    ax_profile.set_xticks([0, 6, 12, 18, 24])
    ax_profile.set_xticklabels(["00:00", "06:00", "12:00", "18:00", "24:00"], fontsize=8.5)
    ax_profile.set_ylabel("有功功率 / MW", fontsize=9.2)
    ax_profile.tick_params(axis="y", labelsize=8.3)
    ax_profile.grid(True, axis="y", color="#DDE5E8", linewidth=0.65)
    ax_profile.legend(loc="upper left", frameon=False, fontsize=8.7)
    ax_profile.text(
        0.02,
        0.04,
        "实际主配网模型  →  馈线时序注入  →  PCC微网等值",
        transform=ax_profile.transAxes,
        fontsize=8.7,
        color=MPL_TEAL,
        fontweight="bold",
        bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": "#B9CFD0", "alpha": 0.92},
    )

    nodes = list(graph.nodes)
    voltage_colors = []
    voltage_sizes = []
    for node in nodes:
        voltage = float(net.bus.at[int(node), "vn_kv"])
        if voltage >= 115.0:
            voltage_colors.append(MPL_BLUE)
            voltage_sizes.append(15.0)
        elif voltage >= 30.0:
            voltage_colors.append(MPL_TEAL)
            voltage_sizes.append(8.0)
        elif voltage >= 5.0:
            voltage_colors.append(MPL_GREEN)
            voltage_sizes.append(5.0)
        else:
            voltage_colors.append(MPL_ORANGE)
            voltage_sizes.append(5.0)
    panel_style(ax_topology, "② 实际网络连接与主配接口识别", MPL_TEAL)
    draw_graph(
        ax_topology,
        graph,
        positions,
        voltage_colors,
        voltage_sizes,
        coordination["pcc_buses"],
    )
    legend = [
        Line2D([0], [0], marker="o", color="none", markerfacecolor=MPL_BLUE, markersize=5.5, label="高压主网"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor=MPL_TEAL, markersize=5.5, label="中压网络"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor=MPL_GREEN, markersize=5.5, label="配电网络"),
        Line2D([0], [0], marker="*", color="none", markerfacecolor=MPL_RED, markeredgecolor="white", markersize=8, label="微网PCC"),
    ]
    ax_topology.legend(handles=legend, loc="lower left", frameon=True, fontsize=7.6, ncol=2, borderpad=0.35)

    partition_lookup = {}
    for part in parts:
        for bus in part["internal_buses"]:
            partition_lookup[int(bus)] = int(part["id"])
    max_partition = max(partition_lookup.values())
    cmap = plt.get_cmap("turbo")
    partition_colors = []
    partition_sizes = []
    for node in nodes:
        part_id = partition_lookup.get(int(node))
        if part_id is None:
            partition_colors.append("#34506A")
            partition_sizes.append(7.0)
        else:
            partition_colors.append(cmap((part_id - 1) / max(1, max_partition - 1)))
            partition_sizes.append(5.2)
    panel_style(ax_partition, "③ 加权多级图分区结果", MPL_ORANGE)
    draw_graph(
        ax_partition,
        graph,
        positions,
        partition_colors,
        partition_sizes,
        coordination["pcc_buses"],
    )
    ax_partition.text(
        0.98,
        0.04,
        "同色节点构成局部潮流子区\n红色星标为微网等值接入位置",
        transform=ax_partition.transAxes,
        ha="right",
        va="bottom",
        fontsize=8.3,
        color=MPL_BLACK,
        bbox={"boxstyle": "round,pad=0.28", "fc": "white", "ec": "#D4DEE1", "alpha": 0.92},
    )

    boundary_counts = np.sort(
        np.asarray([len(part["boundary_buses"]) for part in parts], dtype=float)
    )
    cut_counts = np.sort(
        np.asarray([part["inter_partition_cut_edge_count"] for part in parts], dtype=float)
    )
    percentiles = np.linspace(0, 100, len(boundary_counts))
    panel_style(ax_balance, "④ 分区边界弱耦合性校核", MPL_GREEN)
    ax_balance.step(
        percentiles,
        boundary_counts,
        where="mid",
        color=MPL_TEAL,
        linewidth=1.9,
        label="边界节点数",
    )
    ax_balance.fill_between(
        percentiles,
        boundary_counts,
        step="mid",
        color=MPL_TEAL,
        alpha=0.10,
    )
    ax_balance.step(
        percentiles,
        cut_counts,
        where="mid",
        color=MPL_ORANGE,
        linewidth=1.9,
        label="跨区切边数",
    )
    ax_balance.set_xlim(-1, 101)
    ax_balance.set_ylim(0, max(float(boundary_counts.max()), float(cut_counts.max())) + 0.8)
    ax_balance.set_xlabel("分区样本（分别按指标排序） / 百分位", fontsize=8.6)
    ax_balance.set_ylabel("边界连接数量", fontsize=8.6)
    ax_balance.tick_params(labelsize=8.0)
    ax_balance.grid(True, axis="y", color="#DDE5E8", linewidth=0.6)
    ax_balance.legend(frameon=False, fontsize=8.0, loc="upper left", ncol=2)
    ax_balance.text(
        0.98,
        0.78,
        f"跨区切边：中位数 {np.median(cut_counts):.0f}，P90 {np.percentile(cut_counts, 90):.0f}\n"
        f"边界节点：中位数 {np.median(boundary_counts):.0f}，P90 {np.percentile(boundary_counts, 90):.0f}",
        transform=ax_balance.transAxes,
        ha="right",
        va="top",
        fontsize=8.2,
        color=MPL_BLACK,
        bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": "#C9D5D9", "alpha": 0.92},
    )

    panel_style(ax_status, "⑤ 分区结果质量校核", MPL_PURPLE)
    ax_status.set_xticks([])
    ax_status.set_yticks([])
    statuses = [
        ("边界锚点", "保留", MPL_BLUE),
        ("拓扑连通", "通过", MPL_TEAL),
        ("局部潮流", "全部收敛", MPL_GREEN),
        (
            "最大幅值偏差",
            rf"${benchmark['max_vm_error_pu'] / 10 ** math.floor(math.log10(benchmark['max_vm_error_pu'])):.2f}"
            rf"\times10^{{{math.floor(math.log10(benchmark['max_vm_error_pu']))}}}$ p.u.",
            MPL_ORANGE,
        ),
    ]
    for index, (label, value, color) in enumerate(statuses):
        y = 0.78 - index * 0.22
        ax_status.text(0.05, y, label, transform=ax_status.transAxes, fontsize=9.0, color=MPL_GRAY, va="center")
        ax_status.text(
            0.95,
            y,
            value,
            transform=ax_status.transAxes,
            fontsize=10.2,
            fontweight="bold",
            color=color,
            ha="right",
            va="center",
        )
        if index < len(statuses) - 1:
            ax_status.plot([0.05, 0.95], [y - 0.105, y - 0.105], transform=ax_status.transAxes, color="#E0E6E8", lw=0.7)

    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def make_convergence_figure(coordination, benchmark):
    configure_plot_fonts()
    path = ASSET_DIR / "coordination_and_parallel.png"
    fig = plt.figure(figsize=(15.6, 5.25), dpi=220, facecolor="white")
    figure_border(fig)
    grid = fig.add_gridspec(
        2,
        2,
        left=0.045,
        right=0.985,
        bottom=0.11,
        top=0.91,
        width_ratios=[7.25, 4.75],
        height_ratios=[2.65, 1.85],
        hspace=0.48,
        wspace=0.28,
    )
    ax_residual = fig.add_subplot(grid[:, 0])
    ax_parallel = fig.add_subplot(grid[0, 1])
    ax_tasks = fig.add_subplot(grid[1, 1])

    iterations = np.arange(1, len(coordination["residual_s_mw"]) + 1)
    norm_s = np.asarray(coordination["residual_s_mw"]) / coordination["eps_s_mw"]
    norm_v = np.asarray(coordination["residual_v_pu"]) / coordination["eps_v_pu"]
    norm_theta = np.asarray(coordination["residual_theta_deg"]) / coordination["eps_theta_deg"]
    panel_style(ax_residual, "① 主配微边界联合残差（相对收敛阈值）", MPL_BLUE)
    ax_residual.semilogy(iterations, norm_s, "o-", color=MPL_BLUE, linewidth=2.0, markersize=4.5, label=r"$r_S/\varepsilon_S$")
    ax_residual.semilogy(iterations, norm_v, "s-", color=MPL_TEAL, linewidth=2.0, markersize=4.2, label=r"$r_V/\varepsilon_V$")
    ax_residual.semilogy(iterations, norm_theta, "^-", color=MPL_ORANGE, linewidth=2.0, markersize=4.5, label=r"$r_\theta/\varepsilon_\theta$")
    ax_residual.axhline(1.0, color=MPL_GREEN, linestyle="--", linewidth=1.45, label="联合收敛阈值")
    joint = coordination["joint_iteration"]
    if joint is not None:
        ax_residual.axvline(joint, color=MPL_PURPLE, linestyle=":", linewidth=1.35)
        ax_residual.annotate(
            f"第 {joint} 轮满足联合判据",
            xy=(joint, 1.0),
            xytext=(joint - 3.7, 0.08),
            arrowprops={"arrowstyle": "->", "color": MPL_PURPLE, "lw": 1.1},
            fontsize=9.4,
            color=MPL_PURPLE,
            fontweight="bold",
        )
    ax_residual.set_xticks(iterations)
    ax_residual.set_xlabel("协调迭代轮次", fontsize=9.8)
    ax_residual.set_ylabel("归一化残差", fontsize=9.8)
    ax_residual.tick_params(labelsize=8.7)
    ax_residual.grid(True, which="both", color="#DDE5E8", linewidth=0.65)
    ax_residual.legend(loc="upper right", frameon=False, fontsize=8.8, ncol=2)
    ax_residual.text(
        0.02,
        0.97,
        r"$r_S^{(k)}=\max_m\|S_m^{(k)}-S_m^{(k-1)}\|_2$" + "\n"
        + r"$r_V^{(k)}=\max_i|V_i^{(k)}-V_i^{(k-1)}|,\quad "
        + r"r_\theta^{(k)}=\max_i|\theta_i^{(k)}-\theta_i^{(k-1)}|$",
        transform=ax_residual.transAxes,
        va="top",
        fontsize=9.0,
        color=MPL_BLACK,
        bbox={"boxstyle": "round,pad=0.3", "fc": "white", "ec": "#C9D5D9", "alpha": 0.92},
    )

    workers = np.asarray(benchmark["workers"], dtype=int)
    seconds = np.asarray(benchmark["median_seconds"], dtype=float)
    speedups = np.asarray(benchmark["speedups"], dtype=float)
    panel_style(ax_parallel, "② 局部潮流任务并行实测", MPL_ORANGE)
    bars = ax_parallel.bar(np.arange(len(workers)), seconds, color=[MPL_BLUE, MPL_TEAL, MPL_GREEN, MPL_ORANGE], width=0.62)
    ax_parallel.set_xticks(np.arange(len(workers)), [str(value) for value in workers])
    ax_parallel.set_xlabel("工作进程数", fontsize=8.8)
    ax_parallel.set_ylabel("中位耗时 / s", fontsize=8.8)
    ax_parallel.tick_params(labelsize=8.2)
    ax_parallel.grid(True, axis="y", color="#DDE5E8", linewidth=0.6)
    for bar, value in zip(bars, seconds):
        ax_parallel.text(bar.get_x() + bar.get_width() / 2, value + seconds.max() * 0.025, f"{value:.2f}", ha="center", fontsize=8.0, color=MPL_BLACK)
    speed_axis = ax_parallel.twinx()
    speed_axis.plot(np.arange(len(workers)), speedups, color=MPL_PURPLE, marker="o", linewidth=1.8, markersize=4.5)
    speed_axis.set_ylabel("加速比", fontsize=8.8, color=MPL_PURPLE)
    speed_axis.tick_params(axis="y", labelsize=8.2, colors=MPL_PURPLE)
    speed_axis.set_ylim(0.8, max(2.0, float(speedups.max()) * 1.18))
    for x, value in enumerate(speedups):
        speed_axis.text(x, value + 0.07, f"{value:.2f}×", ha="center", fontsize=8.0, color=MPL_PURPLE)
    ax_parallel.text(
        0.02,
        0.95,
        "同一断面、同一容差，重复 3 次取中位数",
        transform=ax_parallel.transAxes,
        va="top",
        fontsize=7.9,
        color=MPL_GRAY,
    )

    task_times = np.sort(np.asarray(benchmark["task_seconds"], dtype=float))
    task_percentile = np.linspace(0, 100, len(task_times))
    panel_style(ax_tasks, "③ 子区任务粒度与调度开销", MPL_GREEN)
    ax_tasks.plot(task_percentile, task_times * 1000.0, color=MPL_GREEN, linewidth=1.9)
    ax_tasks.fill_between(task_percentile, task_times * 1000.0, color=MPL_GREEN, alpha=0.14)
    ax_tasks.set_xlabel("局部任务耗时排序 / 百分位", fontsize=8.6)
    ax_tasks.set_ylabel("单任务耗时 / ms", fontsize=8.6)
    ax_tasks.tick_params(labelsize=8.0)
    ax_tasks.grid(True, color="#DDE5E8", linewidth=0.6)
    p50 = float(np.percentile(task_times, 50) * 1000.0)
    p90 = float(np.percentile(task_times, 90) * 1000.0)
    ax_tasks.text(
        0.98,
        0.10,
        f"P50 = {p50:.1f} ms\nP90 = {p90:.1f} ms\n并行度提高后，单位进程效率逐步下降",
        transform=ax_tasks.transAxes,
        ha="right",
        va="bottom",
        fontsize=8.2,
        color=MPL_BLACK,
        bbox={"boxstyle": "round,pad=0.28", "fc": "white", "ec": "#C9D5D9", "alpha": 0.92},
    )

    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def set_east_asian_font(run, typeface="微软雅黑"):
    run.font.name = typeface
    r_pr = run._r.get_or_add_rPr()
    ea = r_pr.find(qn("a:ea"))
    if ea is None:
        from lxml import etree

        ea = etree.SubElement(r_pr, qn("a:ea"))
    ea.set("typeface", typeface)


def set_shape_text(
    shape,
    text,
    *,
    size,
    bold=False,
    color=BLACK,
    align=PP_ALIGN.LEFT,
    valign=MSO_ANCHOR.MIDDLE,
    margins=(0.05, 0.05, 0.03, 0.03),
    font="微软雅黑",
):
    tf = shape.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.vertical_anchor = valign
    tf.margin_left = Inches(margins[0])
    tf.margin_right = Inches(margins[1])
    tf.margin_top = Inches(margins[2])
    tf.margin_bottom = Inches(margins[3])
    for index, line in enumerate(text.split("\n")):
        paragraph = tf.paragraphs[0] if index == 0 else tf.add_paragraph()
        paragraph.alignment = align
        paragraph.space_before = Pt(0)
        paragraph.space_after = Pt(0)
        run = paragraph.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
        set_east_asian_font(run, font)


def remove_shape(shape):
    element = shape._element
    element.getparent().remove(element)


def clear_slide(slide):
    for shape in list(slide.shapes):
        remove_shape(shape)


def add_box(slide, x, y, w, h, *, fill, line, rounded=False, line_width=0.9):
    shape_type = MSO_AUTO_SHAPE_TYPE.ROUNDED_RECTANGLE if rounded else MSO_AUTO_SHAPE_TYPE.RECTANGLE
    shape = slide.shapes.add_shape(shape_type, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.line.color.rgb = line
    shape.line.width = Pt(line_width)
    return shape


def add_text(slide, x, y, w, h, text, **kwargs):
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    set_shape_text(shape, text, **kwargs)
    return shape


def add_line(slide, x1, y1, x2, y2, color, width=1.2):
    shape = slide.shapes.add_connector(1, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    shape.line.color.rgb = color
    shape.line.width = Pt(width)
    return shape


def add_header(slide, page_number):
    marker = add_box(slide, 0, 0, 0.16, 0.76, fill=TEAL, line=TEAL)
    marker.line.fill.background()
    add_line(slide, 0, 0.76, 12.42, 0.76, TEAL, 1.45)
    add_text(
        slide,
        0.35,
        0.13,
        5.9,
        0.46,
        "二 、任务进展情况",
        size=26,
        bold=True,
        color=RGBColor(0, 0, 0),
        font="黑体",
    )
    add_text(
        slide,
        12.75,
        7.19,
        0.36,
        0.18,
        str(page_number),
        size=9.5,
        color=RGBColor(0x88, 0x88, 0x88),
        align=PP_ALIGN.RIGHT,
        margins=(0, 0, 0, 0),
    )


def add_title(slide, title):
    add_text(
        slide,
        0.35,
        0.94,
        12.55,
        0.44,
        title,
        size=22.5,
        bold=True,
        color=DEEP_TEAL,
        margins=(0, 0, 0, 0),
    )


def add_summary(slide, label, text):
    left = add_box(slide, 0.35, 1.49, 1.12, 0.62, fill=TEAL, line=TEAL)
    set_shape_text(left, label, size=15.5, bold=True, color=WHITE, align=PP_ALIGN.CENTER)
    right = add_box(slide, 1.51, 1.49, 11.42, 0.62, fill=PALE, line=LIGHT_LINE)
    set_shape_text(right, text, size=14.8, color=BLACK, margins=(0.15, 0.10, 0.03, 0.03))


def add_picture_contain(slide, path, x, y, w, h):
    from PIL import Image

    with Image.open(path) as image:
        ratio = image.width / image.height
    box_ratio = w / h
    if ratio > box_ratio:
        target_w = w
        target_h = w / ratio
        target_x = x
        target_y = y + (h - target_h) / 2
    else:
        target_h = h
        target_w = h * ratio
        target_x = x + (w - target_w) / 2
        target_y = y
    slide.shapes.add_picture(
        str(path),
        Inches(target_x),
        Inches(target_y),
        width=Inches(target_w),
        height=Inches(target_h),
    )


def add_conclusion(slide, text):
    box = add_box(
        slide,
        0.35,
        6.72,
        12.58,
        0.41,
        fill=RGBColor(0xF0, 0xF7, 0xF6),
        line=TEAL,
        rounded=True,
        line_width=0.95,
    )
    set_shape_text(
        box,
        text,
        size=13.2,
        bold=True,
        color=DEEP_TEAL,
        align=PP_ALIGN.CENTER,
        margins=(0.04, 0.04, 0.02, 0.02),
    )


def rebuild_slides(case_figure, convergence_figure, coordination, benchmark):
    prs = Presentation(SOURCE)
    if len(prs.slides) != 22:
        raise RuntimeError(f"Expected 22 slides, found {len(prs.slides)}")

    slide_18 = prs.slides[17]
    clear_slide(slide_18)
    add_header(slide_18, 18)
    add_title(slide_18, "算例验证1：石家庄主配微电网算例设置与分区结果")
    add_summary(
        slide_18,
        "算例设置",
        "基于石家庄实际主配网运行断面，叠加馈线时序注入与PCC微网等值模型，完成主配接口识别、加权多级图分区及局部潮流结果校核。",
    )
    add_picture_contain(slide_18, case_figure, 0.35, 2.20, 12.58, 4.37)
    add_conclusion(
        slide_18,
        "实际运行断面  →  主配接口识别  →  微网PCC等值接入  →  加权多级分区  →  拓扑完整的局部潮流子问题",
    )

    slide_19 = prs.slides[18]
    clear_slide(slide_19)
    add_header(slide_19, 19)
    add_title(slide_19, "算例验证2：协调收敛特性与并行求解效果")
    add_summary(
        slide_19,
        "验证方法",
        "在同一运行断面与求解容差下记录边界功率、电压和相角残差，并对局部潮流任务采用不同工作进程数重复计时，以检验协调收敛与并行扩展特性。",
    )
    add_picture_contain(slide_19, convergence_figure, 0.35, 2.20, 12.58, 4.37)
    joint = coordination["joint_iteration"]
    best_index = int(np.argmax(np.asarray(benchmark["speedups"])))
    best_workers = benchmark["workers"][best_index]
    best_speedup = benchmark["speedups"][best_index]
    add_conclusion(
        slide_19,
        f"联合残差第 {joint} 轮满足阈值；{best_workers} 个工作进程取得 {best_speedup:.2f}× 实测加速，但单位进程效率随并行度提高而下降，进一步扩展受任务粒度与调度开销约束。",
    )

    prs.save(OUTPUT)


def restore_untouched_slide_parts(source, output):
    from lxml import etree

    def parts_by_page(zip_file):
        presentation = etree.fromstring(zip_file.read("ppt/presentation.xml"))
        relationships = etree.fromstring(zip_file.read("ppt/_rels/presentation.xml.rels"))
        relation_map = {item.get("Id"): item.get("Target") for item in relationships}
        namespaces = {
            "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }
        parts = []
        for slide_id in presentation.xpath("//p:sldIdLst/p:sldId", namespaces=namespaces):
            relation_id = slide_id.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            parts.append("ppt/" + relation_map[relation_id])
        return parts

    replacement = output.with_suffix(".restored.pptx")
    with ZipFile(source) as source_zip, ZipFile(output) as output_zip, ZipFile(
        replacement, "w", ZIP_DEFLATED
    ) as replacement_zip:
        source_parts = parts_by_page(source_zip)
        output_parts = parts_by_page(output_zip)
        replacements = {}
        for page in range(1, 23):
            if page in {18, 19}:
                continue
            source_part = source_parts[page - 1]
            output_part = output_parts[page - 1]
            replacements[output_part] = source_zip.read(source_part)
            source_rels = source_part.replace(
                "ppt/slides/", "ppt/slides/_rels/"
            ) + ".rels"
            output_rels = output_part.replace(
                "ppt/slides/", "ppt/slides/_rels/"
            ) + ".rels"
            if source_rels in source_zip.namelist():
                replacements[output_rels] = source_zip.read(source_rels)
        for item in output_zip.infolist():
            data = output_zip.read(item.filename)
            if item.filename in replacements:
                data = replacements[item.filename]
            replacement_zip.writestr(item, data)
    replacement.replace(output)


def verify_output():
    forbidden = (
        "总节点",
        "节点总数",
        "母线总数",
        "脱敏",
        "ACTIVSg",
        "25k",
        "2121",
        "2122",
    )
    prs = Presentation(OUTPUT)
    expected_titles = {
        18: "算例验证1：石家庄主配微电网算例设置与分区结果",
        19: "算例验证2：协调收敛特性与并行求解效果",
    }
    for page, title in expected_titles.items():
        text = "\n".join(
            shape.text for shape in prs.slides[page - 1].shapes if hasattr(shape, "text")
        )
        if title not in text:
            raise RuntimeError(f"Slide {page} is missing the requested title")
        found = [token for token in forbidden if token in text]
        if found:
            raise RuntimeError(f"Slide {page} contains forbidden wording: {found}")


def main():
    for path in (SOURCE, NETWORK_JSON, FEEDER_JSON):
        if not path.exists():
            raise FileNotFoundError(path)
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    if not BACKUP.exists():
        shutil.copy2(SOURCE, BACKUP)

    net = pp.from_json(str(NETWORK_JSON))
    pp.runpp(
        net,
        algorithm="nr",
        init="auto",
        max_iteration=50,
        tolerance_mva=1e-8,
        enforce_q_lims=False,
        calculate_voltage_angles=True,
        trafo_model="t",
    )
    parts = core._build_demo_distribution_partitions(
        net,
        FEEDER_JSON,
        "metis",
        n_parts=0,
        target_buses_per_part=150,
    )
    graph = core._build_pp_graph(net)
    positions = compute_layout(graph)
    profiles, _ = feeder_profiles()

    record_path = ASSET_DIR / "measured_results.json"
    if record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        coordination = record["coordination"]
        benchmark = record["parallel_benchmark"]
    else:
        coordination = run_coordination(copy.deepcopy(net), parts)
        benchmark = benchmark_partition_solver(net, parts)
        record = {
            "coordination": coordination,
            "parallel_benchmark": benchmark,
            "data_source": "Shijiazhuang actual main-distribution network snapshot and feeder profiles",
        }
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    case_figure = make_case_setting_figure(
        net,
        graph,
        positions,
        parts,
        profiles,
        coordination,
        benchmark,
    )
    convergence_figure = make_convergence_figure(coordination, benchmark)
    rebuild_slides(case_figure, convergence_figure, coordination, benchmark)
    restore_untouched_slide_parts(SOURCE, OUTPUT)
    verify_output()
    print(OUTPUT)


if __name__ == "__main__":
    main()
