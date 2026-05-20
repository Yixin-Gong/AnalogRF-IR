"""AnalogRF-IR internal documentation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any

# Internal implementation note.

_rule_registry: Dict[str, dict] = {}


def register_rule(name: str, layer: int = 4, description: str = "") -> Callable:
    """AnalogRF-IR internal documentation."""
    def decorator(fn: Callable) -> Callable:
        _rule_registry[name] = {
            "fn": fn,
            "layer": layer,
            "description": description or fn.__doc__ or "",
        }
        return fn
    return decorator


def get_rule(name: str) -> Optional[Callable]:
    entry = _rule_registry.get(name)
    return entry["fn"] if entry else None


def list_rules(layer: Optional[int] = None) -> List[dict]:
    rules = []
    for name, entry in _rule_registry.items():
        if layer is None or entry["layer"] == layer:
            rules.append({"name": name, "layer": entry["layer"],
                          "description": entry["description"]})
    return sorted(rules, key=lambda r: (r["layer"], r["name"]))


def run_registered_rules(state, layer: Optional[int] = None) -> "ValidationReport":
    report = ValidationReport()
    for name, entry in _rule_registry.items():
        if layer is not None and entry["layer"] != layer:
            continue
        try:
            result = entry["fn"](state)
            if isinstance(result, list):
                for r in result:
                    report.add(r)
            elif isinstance(result, ValidationReport):
                report.results.extend(result.results)
        except Exception as e:
            report.add(DiagnosisResult(
                check_name=f"custom:{name}",
                passed=False, severity="error",
                message=f"Rule '{name}' raised exception: {e}",
            ))
    return report


# Internal implementation note.

@dataclass
class DiagnosisResult:
    check_name: str
    passed: bool
    severity: str       # info | warning | error
    message: str
    layer: int = 0
    device: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    schema_valid: bool = True
    results: List[DiagnosisResult] = field(default_factory=list)

    def add(self, result: DiagnosisResult) -> None:
        self.results.append(result)
        if result.severity == "error":
            self.schema_valid = False

    def errors(self) -> List[DiagnosisResult]:
        return [r for r in self.results if r.severity == "error"]

    def warnings(self) -> List[DiagnosisResult]:
        return [r for r in self.results if r.severity == "warning"]

    def info(self) -> List[DiagnosisResult]:
        return [r for r in self.results if r.severity == "info"]

    def summary(self, by_layer: bool = False) -> str:
        lines = [f"Validation Report - {'PASSED' if self.schema_valid else 'FAILED'} "
                 f"({len(self.errors())} errors, {len(self.warnings())} warnings)"]
        if by_layer:
            layers = sorted(set(r.layer for r in self.results))
            layer_names = {1: "Syntax", 2: "Semantic", 3: "Value", 4: "Physical"}
            for lyr in layers:
                lyr_results = [r for r in self.results if r.layer == lyr]
                lines.append(f"\n-- Layer {lyr} ({layer_names.get(lyr, '?')}) --")
                for r in lyr_results:
                    icon = "OK" if r.passed else "FAIL"
                    dev = f" [{r.device}]" if r.device else ""
                    lines.append(f"  [{icon}] [{r.severity}] {r.check_name}{dev}: {r.message}")
        else:
            for r in self.results:
                icon = "OK" if r.passed else "FAIL"
                dev = f" [{r.device}]" if r.device else ""
                lines.append(f"  [{icon}] [{r.severity}][L{r.layer}] {r.check_name}{dev}: {r.message}")
        return "\n".join(lines)
