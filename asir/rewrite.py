from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from asir.semantic import SemanticPrimitiveGraph


@dataclass
class RewriteReport:
    source: str
    target: str
    preserved_primitives: dict[str, int]
    added_primitives: dict[str, int]
    removed_primitives: dict[str, int]
    preserved_phase_signatures: list[tuple[str, tuple[str, ...]]]
    changed_phase_signatures: list[tuple[str, tuple[str, ...]]]
    conclusion: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["preserved_phase_signatures"] = [
            {"primitive_type": item[0], "active_phases": list(item[1])}
            for item in self.preserved_phase_signatures
        ]
        data["changed_phase_signatures"] = [
            {"primitive_type": item[0], "active_phases": list(item[1])}
            for item in self.changed_phase_signatures
        ]
        return data


class RewriteReasoner:
    """Small semantic equivalence checker for topology rewrite experiments."""

    def compare(self, source: SemanticPrimitiveGraph, target: SemanticPrimitiveGraph) -> RewriteReport:
        source_counts = source.primitive_type_counts()
        target_counts = target.primitive_type_counts()
        keys = sorted(set(source_counts) | set(target_counts))

        preserved = {key: min(source_counts.get(key, 0), target_counts.get(key, 0)) for key in keys}
        added = {
            key: target_counts.get(key, 0) - source_counts.get(key, 0)
            for key in keys
            if target_counts.get(key, 0) > source_counts.get(key, 0)
        }
        removed = {
            key: source_counts.get(key, 0) - target_counts.get(key, 0)
            for key in keys
            if source_counts.get(key, 0) > target_counts.get(key, 0)
        }

        source_signatures = {primitive.signature() for primitive in source.primitives()}
        target_signatures = {primitive.signature() for primitive in target.primitives()}
        preserved_signatures = sorted(source_signatures & target_signatures)
        changed_signatures = sorted(source_signatures ^ target_signatures)

        if not removed and {"differential_pair", "cross_coupled_latch"}.issubset(target_counts):
            conclusion = "rewrite preserves comparator decision semantics at primitive level"
        elif "cross_coupled_latch" not in target_counts:
            conclusion = "rewrite breaks regeneration semantics"
        else:
            conclusion = "rewrite changes semantic primitive coverage"

        return RewriteReport(
            source=source.name,
            target=target.name,
            preserved_primitives={k: v for k, v in preserved.items() if v},
            added_primitives=added,
            removed_primitives=removed,
            preserved_phase_signatures=preserved_signatures,
            changed_phase_signatures=changed_signatures,
            conclusion=conclusion,
        )
