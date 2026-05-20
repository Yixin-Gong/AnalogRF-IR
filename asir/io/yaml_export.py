from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def graph_bundle_to_dict(design: Any) -> dict[str, Any]:
    return design.to_dict()


def export_design_yaml(design: Any, path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            graph_bundle_to_dict(design),
            handle,
            sort_keys=False,
            allow_unicode=True,
            width=120,
        )
    return output
