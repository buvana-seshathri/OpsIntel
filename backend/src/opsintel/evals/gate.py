"""Compare eval results with thresholds. A threshold is {"min": x} or {"max": y}."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

THRESHOLDS = Path(__file__).resolve().parents[4] / "evals" / "thresholds.json"


def load_thresholds(path: Path = THRESHOLDS) -> dict[str, dict[str, dict[str, float]]]:
    data: dict[str, dict[str, dict[str, float]]] = json.loads(path.read_text())
    return data


def check(metrics: dict[str, Any], limits: dict[str, dict[str, float]]) -> list[str]:
    failures = []
    for name, bound in limits.items():
        if name not in metrics:
            failures.append(f"{name}: missing from results")
            continue
        value = metrics[name]
        if "min" in bound and value < bound["min"]:
            failures.append(f"{name} = {value} < min {bound['min']}")
        if "max" in bound and value > bound["max"]:
            failures.append(f"{name} = {value} > max {bound['max']}")
    return failures
