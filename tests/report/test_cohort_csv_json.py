"""Tests for cohort CSV and JSON renderers."""

from __future__ import annotations

import json

from codevigil.analysis.cohort import CohortCell, CohortSlice
from codevigil.report.renderer import render_group_by_csv, render_group_by_json


def _slice() -> CohortSlice:
    return CohortSlice(
        dimension="week",
        cells=[
            CohortCell(
                dimension_value="2026-W14",
                metric_name="read_edit_ratio",
                mean=0.42,
                stdev=0.12,
                n=37,
                min_value=0.05,
                max_value=0.91,
            ),
            CohortCell(
                dimension_value="2026-W15",
                metric_name="read_edit_ratio",
                mean=0.55,
                stdev=0.18,
                n=44,
                min_value=0.10,
                max_value=1.20,
            ),
        ],
        session_count=81,
        excluded_null_count=0,
    )


def test_csv_header_and_rows() -> None:
    out = render_group_by_csv(_slice())
    lines = out.strip().splitlines()
    assert lines[0] == "week,metric_name,mean,stdev,n,min,max"
    assert "2026-W14,read_edit_ratio,0.420000,0.120000,37,0.050000,0.910000" in lines
    assert "2026-W15,read_edit_ratio,0.550000,0.180000,44,0.100000,1.200000" in lines


def test_json_payload_shape() -> None:
    payload = json.loads(render_group_by_json(_slice()))
    assert payload["schema_version"] == 1
    assert payload["dimension"] == "week"
    assert payload["session_count"] == 81
    assert len(payload["cells"]) == 2
    cell = payload["cells"][0]
    assert set(cell.keys()) == {
        "dimension_value",
        "metric_name",
        "mean",
        "stdev",
        "n",
        "min",
        "max",
    }
