#!/usr/bin/env python3
"""Convert demo-area feeder XLS load profiles to JSON."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = (
    PROJECT_ROOT
    / "data"
    / "demo_area"
    / "raw"
    / "\u540410kV\u9988\u7ebf"
)
DEFAULT_OUTPUT_JSON = (
    PROJECT_ROOT / "data" / "demo_area" / "converted" / "demo_area_feeders.json"
)

FEEDER_CODE_OVERRIDES = {
    "ZF\u4e1c\u7ebf": "ZF",
    "ZF\u7ebf": "ZFA",
    "JK2\u7ebf": "JKA",
    "NV\u96621\u7ebf": "NVA",
    "GA\u7199\u6e56\u7ebf": "GAA",
}


def compact_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def feeder_code_from_filename(path: Path) -> str:
    key = compact_text(path.stem).upper()
    if key in FEEDER_CODE_OVERRIDES:
        return FEEDER_CODE_OVERRIDES[key]
    match = re.match(r"[A-Z]+", key)
    if not match:
        raise ValueError(f"Cannot derive feeder code from file name: {path.name}")
    return match.group(0)


def float_or_zero(value: Any) -> float:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def node_to_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def node_to_bus_name(feeder_code: str, value: Any) -> str:
    node = node_to_int(value)
    if node is None or node <= 0:
        return ""
    return f"{feeder_code}{node}"


def bus_aliases(feeder_code: str, value: Any) -> list[str]:
    primary = node_to_bus_name(feeder_code, value)
    if not primary:
        return []
    aliases = [primary]
    node = node_to_int(value)
    if feeder_code == "GAA" and node is not None:
        aliases.insert(0, f"GA  B{node}")
    deduped: list[str] = []
    for alias in aliases:
        if alias not in deduped:
            deduped.append(alias)
    return deduped


def normalize_time_label(value: Any, idx: int) -> str:
    text = str(value or "").strip().replace("\uff1a", ":")
    match = re.match(r"^(\d{1,2})(?::(\d{1,2}))?", text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or "0")
    else:
        total_minutes = idx * 15
        hour = total_minutes // 60
        minute = total_minutes % 60
    return f"{hour:02d}:{minute:02d}"


def read_xls_as_csv_rows(xls_path: Path) -> list[list[str]]:
    with tempfile.TemporaryDirectory(prefix="gmss_feeder_") as tmp_dir_raw:
        tmp_dir = Path(tmp_dir_raw)
        tmp_xls = tmp_dir / "feeder.xls"
        shutil.copy2(xls_path, tmp_xls)
        proc = subprocess.run(
            [
                "libreoffice",
                "--headless",
                "--convert-to",
                "csv",
                "--outdir",
                str(tmp_dir),
                str(tmp_xls),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"LibreOffice failed to convert {xls_path}: {proc.stderr or proc.stdout}"
            )
        csv_path = tmp_dir / "feeder.csv"
        if not csv_path.exists():
            raise FileNotFoundError(f"LibreOffice did not create CSV for {xls_path}")
        with csv_path.open("r", encoding="latin1", newline="") as fh:
            return list(csv.reader(fh))


def parse_feeder_xls(xls_path: Path, source_dir: Path) -> tuple[dict[str, Any], list[str]]:
    rows = read_xls_as_csv_rows(xls_path)
    if not rows:
        raise ValueError(f"Empty feeder XLS: {xls_path}")

    header = rows[0]
    time_labels = [
        normalize_time_label(header[col], idx)
        for idx, col in enumerate(range(10, len(header), 2))
    ]
    feeder_code = feeder_code_from_filename(xls_path)
    rel_path = xls_path.relative_to(source_dir)
    parsed_rows: list[dict[str, Any]] = []
    for row in rows[1:]:
        padded = row + [""] * max(0, len(header) - len(row))
        from_node = node_to_int(padded[0])
        to_node = node_to_int(padded[1])
        p_mw = []
        q_mvar = []
        for idx in range(len(time_labels)):
            p_col = 10 + idx * 2
            q_col = p_col + 1
            p_mw.append(float_or_zero(padded[p_col] if p_col < len(padded) else ""))
            q_mvar.append(float_or_zero(padded[q_col] if q_col < len(padded) else ""))
        parsed_rows.append(
            {
                "from_node": from_node,
                "to_node": to_node,
                "bus_name": node_to_bus_name(feeder_code, to_node),
                "bus_aliases": bus_aliases(feeder_code, to_node),
                "line_type": str(padded[2]).strip(),
                "length_km": float_or_zero(padded[3]),
                "transformer_kva": float_or_zero(padded[4]),
                "base_p_mw": float_or_zero(padded[5]),
                "base_q_mvar": float_or_zero(padded[6]),
                "p_mw": p_mw,
                "q_mvar": q_mvar,
            }
        )

    feeder = {
        "feeder_id": str(rel_path),
        "display_name": xls_path.stem,
        "feeder_code": feeder_code,
        "source_file": str(xls_path),
        "row_count": len(parsed_rows),
        "rows": parsed_rows,
    }
    return feeder, time_labels


def convert_feeders(source_dir: Path, output_json: Path) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    output_json = output_json.resolve()
    files = sorted(source_dir.rglob("*.xls"))
    if not files:
        raise FileNotFoundError(f"No feeder XLS files found under {source_dir}")

    feeders: list[dict[str, Any]] = []
    expected_time_labels: list[str] | None = None
    for xls_path in files:
        feeder, time_labels = parse_feeder_xls(xls_path, source_dir)
        if expected_time_labels is None:
            expected_time_labels = time_labels
        elif time_labels != expected_time_labels:
            raise ValueError(f"Time labels differ in {xls_path}")
        feeders.append(feeder)

    row_count = sum(int(feeder["row_count"]) for feeder in feeders)
    mapped_rows = sum(
        1
        for feeder in feeders
        for row in feeder["rows"]
        if str(row.get("bus_name") or "")
    )
    data = {
        "schema_version": "demo-area-feeders-v1",
        "source_dir": str(source_dir),
        "time_labels": expected_time_labels or [],
        "feeders": feeders,
        "summary": {
            "feeder_count": len(feeders),
            "row_count": row_count,
            "mapped_row_count": mapped_rows,
            "profile_point_count": len(expected_time_labels or []),
        },
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert demo feeder XLS profiles to JSON.")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--json", type=Path, default=DEFAULT_OUTPUT_JSON)
    args = parser.parse_args()

    data = convert_feeders(args.source_dir, args.json)
    print(json.dumps(data["summary"], ensure_ascii=False, indent=2))
    print(f"feeder_json={args.json}")


if __name__ == "__main__":
    main()
