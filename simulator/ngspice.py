"""AnalogRF-IR internal documentation."""
from __future__ import annotations

import subprocess
import re
import tempfile
import os
import time
import math
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple


@dataclass
class SimulationResult:
    """AnalogRF-IR internal documentation."""

    success: bool = False
    return_code: int = -1
    elapsed_sec: float = 0.0

    # Internal implementation note.
    measurements: Dict[str, float] = field(default_factory=dict)

    # Internal implementation note.
    operating_points: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # Internal implementation note.
    raw_stdout: str = ""
    raw_stderr: str = ""

    # Per-analysis execution summary. This is intentionally compact so
    # downstream artifacts can distinguish an AC/DC success from a missing
    # transient export.
    pass_status: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class NgspiceSimulator:
    """AnalogRF-IR internal documentation."""

    def __init__(self, ngspice_bin: str = "ngspice", timeout_sec: Optional[float] = None):
        self.ngspice_bin = ngspice_bin
        self.timeout_sec = timeout_sec

    def check_available(self) -> bool:
        try:
            result = subprocess.run(
                [self.ngspice_bin, "--version"],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def run(
        self,
        netlist: str,
        work_dir: Optional[str] = None,
        include_transient: Optional[bool] = None,
    ) -> SimulationResult:
        """AnalogRF-IR internal documentation."""
        t0 = time.time()

        result_ac = self._run_ac_pass(netlist, work_dir)
        result_dc = self._run_dc_pass(netlist, work_dir)
        result_icmr = self._run_icmr_pass(netlist, work_dir)
        if include_transient is None:
            include_transient = self._has_tran_request(netlist)
        result_tran = self._run_tran_pass(netlist, work_dir) if include_transient else SimulationResult()

        merged = SimulationResult()
        merged.elapsed_sec = time.time() - t0

        # Internal implementation note.
        merged.measurements = {}
        merged.measurements.update(result_dc.measurements)  # DC: total_power
        merged.measurements.update(result_icmr.measurements)  # ICMR: common-mode OP sweep
        merged.measurements.update(result_ac.measurements)  # AC: dc_gain_db, ugbw, pm
        merged.measurements.update(result_tran.measurements)  # TRAN: slew_rate

        # Internal implementation note.
        merged.operating_points = result_dc.operating_points

        merged.raw_stdout = (
            result_ac.raw_stdout
            + "\n"
            + result_dc.raw_stdout
            + "\n"
            + result_icmr.raw_stdout
            + "\n"
            + result_tran.raw_stdout
        )
        merged.raw_stderr = (
            result_ac.raw_stderr
            + "\n"
            + result_dc.raw_stderr
            + "\n"
            + result_icmr.raw_stderr
            + "\n"
            + result_tran.raw_stderr
        )

        executed_passes = [
            ("ac", result_ac, ("dc_gain_db", "unity_gain_bandwidth", "phase_margin"), False),
            ("dc", result_dc, ("total_power", "output_swing", "saturation_margin"), False),
            ("icmr", result_icmr, (), False),
        ]
        if include_transient:
            executed_passes.append(("tran", result_tran, ("slew_rate",), True))
        merged.pass_status = {
            name: self._summarize_pass(
                result,
                expected_measurements=expected,
                required_for_run=required_for_run,
            )
            for name, result, expected, required_for_run in executed_passes
        }

        pass_codes = [
            result.return_code
            for _name, result, _expected, _required_for_run in executed_passes
            if result.return_code is not None
        ]
        missing_required = any(
            bool(summary.get("missing_measurements"))
            for summary in merged.pass_status.values()
            if summary.get("required_for_run")
        )
        if pass_codes:
            merged.return_code = 0 if all(code == 0 for code in pass_codes) and not missing_required else 1

        executed_ok = all(
            bool(result.success) and result.return_code == 0
            for _name, result, _expected, _required_for_run in executed_passes
        )
        merged.success = bool(merged.measurements) and executed_ok and not missing_required

        return merged

    def _summarize_pass(
        self,
        result: SimulationResult,
        *,
        expected_measurements: tuple[str, ...] = (),
        required_for_run: bool = False,
    ) -> Dict[str, Any]:
        measurements = result.measurements or {}
        missing = [key for key in expected_measurements if key not in measurements]
        summary: Dict[str, Any] = {
            "success": bool(result.success),
            "return_code": result.return_code,
            "elapsed_sec": round(float(result.elapsed_sec or 0.0), 6),
            "measurement_count": len(measurements),
            "measurements": sorted(measurements),
            "missing_measurements": missing,
            "required_for_run": bool(required_for_run),
        }
        if result.raw_stderr:
            summary["stderr_tail"] = result.raw_stderr[-500:]
        return summary

    def _has_tran_request(self, netlist: str) -> bool:
        low = netlist.lower()
        return ".tran" in low or ".meas tran" in low or "slew_rate" in low

    # Pass 1: AC

    def _run_ac_pass(self, netlist: str, work_dir: Optional[str]) -> SimulationResult:
        """AnalogRF-IR internal documentation."""
        # Internal implementation note.
        cleaned = self._strip_dc_control(netlist)
        if ".ac" not in cleaned.lower():
            return SimulationResult()
        probe = self._infer_ac_probe(cleaned)
        cleaned = self._add_ac_curve_export(cleaned, probe)
        return self._exec_ngspice(cleaned, work_dir, suffix="ac")

    def _infer_ac_probe(self, netlist: str) -> str:
        for pattern in (r"vdb\(([^)]+)\)", r"vp\(([^)]+)\)"):
            m = re.search(pattern, netlist, flags=re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return "vout"

    def _add_ac_curve_export(self, netlist: str, probe: str) -> str:
        lines = netlist.splitlines()
        control = [
            "",
            ".control",
            "  set filetype=ascii",
            "  set wr_singlescale",
            "  set wr_vecnames",
            "  run",
            f"  wrdata ac_sweep.dat vdb({probe}) vp({probe})",
            ".endc",
        ]
        for idx in range(len(lines) - 1, -1, -1):
            if lines[idx].strip().lower() == ".end":
                return "\n".join(lines[:idx] + control + lines[idx:])
        return "\n".join(lines + control + [".end"])

    def _strip_dc_control(self, netlist: str) -> str:
        """AnalogRF-IR internal documentation."""
        lines = netlist.split("\n")
        out = []
        skip_control = False
        for line in lines:
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith(".control"):
                skip_control = True
                continue
            if skip_control:
                if low.startswith(".endc"):
                    skip_control = False
                continue
            # Internal implementation note.
            if low.startswith(".dc ") or low.startswith(".dc\t"):
                continue
            if low.startswith(".tran ") or low.startswith(".tran\t"):
                continue
            if ".meas dc" in low or ".meas tran" in low:
                continue
            out.append(line)
        return "\n".join(out)

    # Pass 2: DC

    def _run_dc_pass(self, netlist: str, work_dir: Optional[str]) -> SimulationResult:
        """AnalogRF-IR internal documentation."""
        lines = netlist.split("\n")
        out = []
        for line in lines:
            stripped = line.strip()
            low = stripped.lower()
            # Internal implementation note.
            if low.startswith(".ac ") or low.startswith(".ac\t"):
                out.append(f"* {line}")
            elif low.startswith(".tran ") or low.startswith(".tran\t"):
                out.append(f"* {line}")
            # Internal implementation note.
            elif ".meas ac" in low or ".meas tran" in low:
                out.append(f"* {line}")
            else:
                out.append(line)

        # Internal implementation note.
        has_dc = any(l.strip().startswith(".dc ") for l in out)

        # Internal implementation note.
        out.append("")
        out.append(".control")
        out.append("  set ngbehavior = hsa")
        out.append("  run")
        out.append("  print all")
        for print_line in self._device_operating_point_prints(netlist):
            out.append(f"  {print_line}")
        out.append(".endc")

        dc_netlist = "\n".join(out)
        return self._exec_ngspice(dc_netlist, work_dir, suffix="dc")

    # Pass 2b: ICMR common-mode operating-point sweep

    def _run_icmr_pass(self, netlist: str, work_dir: Optional[str]) -> SimulationResult:
        """Measure ICMR by sweeping the input common-mode voltage with ngspice OP."""
        result = SimulationResult()
        if not self._requires_icmr_sweep(netlist):
            return result

        source_names = self._icmr_input_source_names(netlist)
        devices = self._parse_mos_devices(netlist)
        if not source_names or not devices:
            return result

        vdd = self._infer_vdd(netlist)
        vss = self._infer_vss(netlist)
        factor = self._infer_vdsat_factor(netlist)
        grid = self._icmr_sweep_grid(vdd, vss)
        sample_dir = os.path.join(work_dir, "icmr_sweep") if work_dir else None

        samples: List[Tuple[float, bool, float]] = []
        raw_stdout: List[str] = []
        raw_stderr: List[str] = []
        return_codes: List[int] = []
        executed = 0
        for idx, vcm in enumerate(grid):
            best_margin: Optional[float] = None
            variants = self._icmr_source_variants(netlist, source_names, vcm)
            for variant_idx, variant_netlist in enumerate(variants):
                sample_netlist = self._strip_for_op(variant_netlist)
                sample_netlist = self._add_op_control(sample_netlist)
                suffix = f"icmr_{idx:02d}" if len(variants) == 1 else f"icmr_{idx:02d}_{variant_idx}"
                sample = self._exec_ngspice(sample_netlist, sample_dir, suffix=suffix)
                if sample.return_code >= 0:
                    return_codes.append(sample.return_code)
                raw_stdout.append(sample.raw_stdout)
                raw_stderr.append(sample.raw_stderr)
                if not sample.operating_points:
                    continue
                margin = self._icmr_headroom_margin(devices, sample.operating_points, factor)
                if margin is None or not math.isfinite(margin):
                    continue
                if best_margin is None or margin > best_margin:
                    best_margin = margin
            if best_margin is None:
                continue
            executed += 1
            samples.append((vcm, best_margin >= -1e-4, best_margin))

        if return_codes:
            result.return_code = 0 if any(code == 0 for code in return_codes) else max(return_codes)
        result.success = executed > 0
        result.raw_stdout = "\n".join(raw_stdout)
        result.raw_stderr = "\n".join(raw_stderr)

        if not samples:
            result.measurements = {
                "icmr_sweep_points": float(len(grid)),
                "icmr_valid_points": 0.0,
            }
            return result

        segments = self._valid_icmr_segments(samples)
        if not segments:
            lo = min(vdd, vss)
            hi = max(vdd, vss)
            result.measurements = {
                "icmr": 0.0,
                "icmr_min": hi,
                "icmr_max": lo,
                "icmr_sweep_points": float(len(samples)),
                "icmr_valid_points": 0.0,
                "icmr_headroom_margin_min": min(item[2] for item in samples),
            }
            return result

        start, end = max(
            segments,
            key=lambda item: (samples[item[1]][0] - samples[item[0]][0], item[1] - item[0]),
        )
        low = samples[start][0]
        high = samples[end][0]
        valid_margins = [margin for _vcm, valid, margin in samples[start : end + 1] if valid]
        result.measurements = {
            "icmr": max(0.0, high - low),
            "icmr_min": low,
            "icmr_max": high,
            "icmr_sweep_points": float(len(samples)),
            "icmr_valid_points": float(sum(1 for _vcm, valid, _margin in samples if valid)),
            "icmr_headroom_margin_min": min(valid_margins) if valid_margins else 0.0,
            "icmr_sweep_step": abs(grid[1] - grid[0]) if len(grid) > 1 else 0.0,
        }
        return result

    def _requires_icmr_sweep(self, netlist: str) -> bool:
        return bool(re.search(r"\bicmr(?:_min|_max)?\b|input_common_mode", netlist, flags=re.IGNORECASE))

    def _icmr_input_source_names(self, netlist: str) -> List[str]:
        candidates: List[Tuple[str, str]] = []
        preferred_nodes = {"vinp", "vinn", "vin", "inp", "inn", "in_p", "in_n"}
        for line in netlist.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("*"):
                continue
            m = re.match(r"^(V\S+)\s+(\S+)\s+(\S+)\s+", stripped, flags=re.IGNORECASE)
            if not m:
                continue
            name = m.group(1)
            node = m.group(2)
            name_low = name.lower()
            node_low = node.lower()
            if name_low in {"vinp", "vinn", "vin"} or node_low in preferred_nodes:
                candidates.append((name, node_low))

        differential = [name for name, node in candidates if name.lower() in {"vinp", "vinn"} or node in {"vinp", "vinn"}]
        if len(differential) >= 2:
            return list(dict.fromkeys(differential))
        return list(dict.fromkeys(name for name, _node in candidates))

    def _icmr_sweep_grid(self, vdd: float, vss: float) -> List[float]:
        span = max(abs(vdd - vss), 1e-9)
        low = min(vss, vdd) + 0.02 * span
        high = max(vss, vdd) - 0.02 * span
        if high <= low:
            return [0.5 * (vdd + vss)]
        points = 25
        step = (high - low) / float(points - 1)
        return [low + step * idx for idx in range(points)]

    def _set_common_mode_sources(self, netlist: str, source_names: List[str], vcm: float) -> str:
        source_set = {name.lower() for name in source_names}
        out: List[str] = []
        value = f"{vcm:.8g}"
        for line in netlist.splitlines():
            m = re.match(r"^(\s*)(V\S+)(\s+\S+\s+\S+\s+)(.*)$", line, flags=re.IGNORECASE)
            if not m or m.group(2).lower() not in source_set:
                out.append(line)
                continue
            prefix = f"{m.group(1)}{m.group(2)}{m.group(3)}"
            tail = m.group(4).strip()
            if re.search(r"\bDC\s+\S+", tail, flags=re.IGNORECASE):
                tail = re.sub(r"\bDC\s+\S+", f"DC {value}", tail, count=1, flags=re.IGNORECASE)
            elif tail:
                parts = tail.split(None, 1)
                suffix = f" {parts[1]}" if len(parts) > 1 else ""
                tail = f"DC {value}{suffix}"
            else:
                tail = f"DC {value}"
            out.append(prefix + tail)
        return "\n".join(out)

    def _icmr_source_variants(self, netlist: str, source_names: List[str], vcm: float) -> List[str]:
        output_node = self._infer_output_node(netlist)
        records = self._voltage_source_records(netlist, source_names)
        vinp = next((item for item in records if item[0].lower() == "vinp" or item[1].lower() == "vinp"), None)
        vinn = next((item for item in records if item[0].lower() == "vinn" or item[1].lower() == "vinn"), None)
        if output_node and vinp and vinn:
            return [
                self._set_feedback_icmr_sources(netlist, vinp[0], vinn[0], vinn[1], output_node, vcm),
                self._set_feedback_icmr_sources(netlist, vinn[0], vinp[0], vinp[1], output_node, vcm),
                self._set_common_mode_sources(netlist, source_names, vcm),
            ]
        return [self._set_common_mode_sources(netlist, source_names, vcm)]

    def _voltage_source_records(self, netlist: str, source_names: List[str]) -> List[Tuple[str, str, str]]:
        source_set = {name.lower() for name in source_names}
        records: List[Tuple[str, str, str]] = []
        for line in netlist.splitlines():
            m = re.match(r"^\s*(V\S+)\s+(\S+)\s+(\S+)\s+", line, flags=re.IGNORECASE)
            if not m or m.group(1).lower() not in source_set:
                continue
            records.append((m.group(1), m.group(2), m.group(3)))
        return records

    def _set_feedback_icmr_sources(
        self,
        netlist: str,
        driven_source: str,
        feedback_source: str,
        feedback_node: str,
        output_node: str,
        vcm: float,
    ) -> str:
        driven = driven_source.lower()
        feedback = feedback_source.lower()
        value = f"{vcm:.8g}"
        out: List[str] = []
        for line in netlist.splitlines():
            m = re.match(r"^(\s*)(V\S+)\s+(\S+)\s+(\S+)\s+(.*)$", line, flags=re.IGNORECASE)
            if not m:
                out.append(line)
                continue
            name = m.group(2)
            name_low = name.lower()
            if name_low == driven:
                out.append(f"{m.group(1)}{name} {m.group(3)} 0 DC {value}")
            elif name_low == feedback:
                out.append(f"{m.group(1)}{name} {feedback_node} {output_node} DC 0")
            else:
                out.append(line)
        return "\n".join(out)

    def _strip_for_op(self, netlist: str) -> str:
        lines = netlist.split("\n")
        out: List[str] = []
        skip_control = False
        for line in lines:
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith(".control"):
                skip_control = True
                continue
            if skip_control:
                if low.startswith(".endc"):
                    skip_control = False
                continue
            if low.startswith((".ac ", ".ac\t", ".dc ", ".dc\t", ".tran ", ".tran\t")):
                continue
            if low.startswith(".meas "):
                continue
            out.append(line)
        return "\n".join(out)

    def _add_op_control(self, netlist: str) -> str:
        lines = netlist.splitlines()
        control = [
            "",
            ".control",
            "  set ngbehavior = hsa",
            "  op",
            "  print all",
        ]
        for print_line in self._device_operating_point_prints(netlist):
            control.append(f"  {print_line}")
        control.append(".endc")
        for idx in range(len(lines) - 1, -1, -1):
            if lines[idx].strip().lower() == ".end":
                return "\n".join(lines[: idx + 1] + control + lines[idx + 1 :])
        return "\n".join(lines + [".end"] + control)

    def _icmr_headroom_margin(
        self,
        devices: List[Dict[str, str]],
        op: Dict[str, Dict[str, float]],
        factor: float,
    ) -> Optional[float]:
        margins: List[float] = []
        for dev in devices:
            gm = self._op_abs_optional(op, dev["id"], "gm")
            vds = self._op_abs_optional(op, dev["id"], "vds")
            vdsat = self._op_abs_optional(op, dev["id"], "vdsat")
            if gm is None or vds is None or vdsat is None:
                return None
            if not all(math.isfinite(value) for value in (gm, vds, vdsat)):
                return None
            if gm <= 1e-12 or vdsat <= 1e-6:
                margins.append(float("-inf"))
                continue
            margins.append(vds - factor * vdsat)
        return min(margins) if margins else None

    def _valid_icmr_segments(self, samples: List[Tuple[float, bool, float]]) -> List[Tuple[int, int]]:
        segments: List[Tuple[int, int]] = []
        start: Optional[int] = None
        for idx, (_vcm, valid, _margin) in enumerate(samples):
            if valid and start is None:
                start = idx
            elif not valid and start is not None:
                segments.append((start, idx - 1))
                start = None
        if start is not None:
            segments.append((start, len(samples) - 1))
        return segments

    def _device_operating_point_prints(self, netlist: str) -> List[str]:
        devices = []
        for line in netlist.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("*"):
                continue
            m = re.match(r"^(M\S+)\s+", stripped, re.IGNORECASE)
            if m:
                devices.append(("mos", m.group(1), ""))
                continue
            x = re.match(r"^(X\S+)\s+\S+\s+\S+\s+\S+\s+\S+\s+(\S+)", stripped, re.IGNORECASE)
            if x:
                devices.append(("subckt", x.group(1), x.group(2)))
        params = ("gm", "gds", "vgs", "vds", "vdsat", "id", "cgs", "cgd", "cgg")
        lines = []
        for style, dev, model in devices:
            if style == "subckt":
                inner = f"n.{dev.lower()}.n{model.lower()}"
                sub_params = ("gm", "gds", "vgs", "vds", "vdss", "ids", "cgs", "cgd", "cgg")
                exprs = " ".join(f"@{inner}[{param}]" for param in sub_params)
            else:
                exprs = " ".join(f"@{dev}[{param}]" for param in params)
            lines.append(f"print {exprs}")
        return lines

    # Pass 3: transient slew-rate

    def _run_tran_pass(self, netlist: str, work_dir: Optional[str]) -> SimulationResult:
        """Pass 3: transient step response + Python slew-rate extraction."""
        cleaned = self._strip_for_tran(netlist)
        probe = self._infer_tran_probe(cleaned)
        cleaned = self._inject_slew_stimulus(cleaned)
        cleaned = self._ensure_tran_analysis(cleaned)
        cleaned = self._add_tran_curve_export(cleaned, probe)
        return self._exec_ngspice(cleaned, work_dir, suffix="tran")

    def _strip_for_tran(self, netlist: str) -> str:
        lines = netlist.split("\n")
        out = []
        skip_control = False
        for line in lines:
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith(".control"):
                skip_control = True
                continue
            if skip_control:
                if low.startswith(".endc"):
                    skip_control = False
                continue
            if low.startswith(".ac ") or low.startswith(".ac\t"):
                continue
            if low.startswith(".dc ") or low.startswith(".dc\t"):
                continue
            if low.startswith(".meas "):
                continue
            out.append(line)
        return "\n".join(out)

    def _infer_tran_probe(self, netlist: str) -> str:
        for pattern in (r"^Cload\s+(\S+)\s+", r"vdb\(([^)]+)\)", r"vp\(([^)]+)\)"):
            m = re.search(pattern, netlist, flags=re.IGNORECASE | re.MULTILINE)
            if m:
                return m.group(1).strip()
        return "vout"

    def _inject_slew_stimulus(self, netlist: str) -> str:
        if "comparator" in netlist.lower() or re.search(r"^\s*Vclk\s+", netlist, flags=re.IGNORECASE | re.MULTILINE):
            return netlist
        lines = netlist.splitlines()
        tstop = self._tran_stop_time(netlist)
        vdd = self._infer_vdd(netlist)
        vcm = 0.5 * vdd
        half_step = min(0.10, 0.10 * vdd)
        td = 0.15 * tstop
        tr = max(min(tstop / 2000.0, 50e-12), 1e-12)
        tf = tr
        pw = 0.35 * tstop
        per = 2.0 * tstop

        def fmt(value: float) -> str:
            return f"{value:.6g}"

        def replacement(name: str, node: str, invert: bool) -> str:
            low = vcm - half_step
            high = vcm + half_step
            v1, v2 = (high, low) if invert else (low, high)
            ac = "-0.5" if invert else "0.5"
            return (
                f"{name} {node} 0 DC {fmt(vcm)} AC {ac} "
                f"PULSE({fmt(v1)} {fmt(v2)} {fmt(td)} {fmt(tr)} {fmt(tf)} {fmt(pw)} {fmt(per)})"
            )

        out = []
        changed_single = False
        for line in lines:
            stripped = line.strip()
            m = re.match(r"^(V\S+)\s+(\S+)\s+0\s+", stripped, flags=re.IGNORECASE)
            if not m:
                out.append(line)
                continue
            name, node = m.group(1), m.group(2)
            node_low = node.lower()
            name_low = name.lower()
            if node_low == "vinp" or name_low == "vinp":
                out.append(replacement(name, node, invert=False))
            elif node_low == "vinn" or name_low == "vinn":
                out.append(replacement(name, node, invert=True))
            elif node_low in {"vin", "in", "inp"} and not changed_single:
                out.append(replacement(name, node, invert=False))
                changed_single = True
            else:
                out.append(line)
        return "\n".join(out)

    def _ensure_tran_analysis(self, netlist: str) -> str:
        if re.search(r"^\s*\.tran\s+", netlist, flags=re.IGNORECASE | re.MULTILINE):
            return netlist
        tstop = 2.0e-7
        tstep = 2.0e-10
        tmax = 1.0e-10
        lines = netlist.splitlines()
        for idx in range(len(lines) - 1, -1, -1):
            if lines[idx].strip().lower() == ".end":
                return "\n".join(lines[:idx] + [f".tran {tstep:.6g} {tstop:.6g} 0 {tmax:.6g}"] + lines[idx:])
        return "\n".join(lines + [f".tran {tstep:.6g} {tstop:.6g} 0 {tmax:.6g}", ".end"])

    def _add_tran_curve_export(self, netlist: str, probe: str) -> str:
        lines = netlist.splitlines()
        probes = [probe]
        if "comparator" in netlist.lower() or re.search(r"^\s*Vclk\s+", netlist, flags=re.IGNORECASE | re.MULTILINE):
            load_nodes = re.findall(r"^\s*Cload(?:_\S+)?\s+(\S+)\s+0\s+", netlist, flags=re.IGNORECASE | re.MULTILINE)
            probes = list(dict.fromkeys(load_nodes or [probe]))
        probe_expr = " ".join(f"v({item})" for item in probes)
        control = [
            "",
            ".control",
            "  set filetype=ascii",
            "  set wr_singlescale",
            "  set wr_vecnames",
            "  run",
            f"  wrdata tran_sweep.dat {probe_expr}",
            ".endc",
        ]
        for idx in range(len(lines) - 1, -1, -1):
            if lines[idx].strip().lower() == ".end":
                return "\n".join(lines[:idx] + control + lines[idx:])
        return "\n".join(lines + control + [".end"])

    def _infer_vdd(self, netlist: str) -> float:
        m = re.search(r"^\s*Vdd\s+\S+\s+\S+\s+DC\s+(\S+)", netlist, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            try:
                return abs(self._spice_number(m.group(1)))
            except ValueError:
                pass
        return 1.2

    def _tran_stop_time(self, netlist: str) -> float:
        m = re.search(r"^\s*\.tran\s+(\S+)\s+(\S+)", netlist, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            try:
                return max(self._spice_number(m.group(2)), 1e-12)
            except ValueError:
                pass
        return 2.0e-7

    def _spice_number(self, token: str) -> float:
        token = token.strip().strip("'\"")
        try:
            return float(token)
        except ValueError:
            pass
        m = re.match(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]+)$", token)
        if not m:
            raise ValueError(token)
        value = float(m.group(1))
        suffix = m.group(2).lower()
        scale = {
            "t": 1e12,
            "g": 1e9,
            "meg": 1e6,
            "k": 1e3,
            "m": 1e-3,
            "u": 1e-6,
            "n": 1e-9,
            "p": 1e-12,
            "f": 1e-15,
        }.get(suffix)
        if scale is None:
            raise ValueError(token)
        return value * scale

    # Internal implementation note.

    def _exec_ngspice(self, netlist: str, work_dir: Optional[str], suffix: str) -> SimulationResult:
        t0 = time.time()
        result = SimulationResult()

        if work_dir:
            os.makedirs(work_dir, exist_ok=True)
            cir_path = os.path.abspath(os.path.join(work_dir, f"sim_{suffix}.cir"))
        else:
            fd, cir_path = tempfile.mkstemp(suffix=f"_{suffix}.cir", text=True)
            os.close(fd)
            cir_path = os.path.abspath(cir_path)

        with open(cir_path, "w", encoding="utf-8") as f:
            f.write(netlist)

        self._write_spiceinit_if_needed(netlist, Path(os.path.dirname(cir_path)))

        if suffix == "ac":
            sweep_path = Path(os.path.dirname(cir_path)) / "ac_sweep.dat"
            try:
                sweep_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if suffix == "tran":
            sweep_path = Path(os.path.dirname(cir_path)) / "tran_sweep.dat"
            try:
                sweep_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

        try:
            timeout = self.timeout_sec if self.timeout_sec and self.timeout_sec > 0 else None
            proc = subprocess.run(
                [self.ngspice_bin, "-b", cir_path],
                capture_output=True, text=True,
                timeout=timeout,
                cwd=os.path.dirname(cir_path),
            )
            result.return_code = proc.returncode
            result.raw_stdout = proc.stdout
            result.raw_stderr = proc.stderr
        except subprocess.TimeoutExpired:
            result.raw_stderr = f"Timeout after {self.timeout_sec}s"
            result.elapsed_sec = time.time() - t0
            return result
        except FileNotFoundError:
            result.raw_stderr = f"'{self.ngspice_bin}' not found"
            result.elapsed_sec = time.time() - t0
            return result
        finally:
            if not work_dir:
                try:
                    os.unlink(cir_path)
                except OSError:
                    pass

        result.elapsed_sec = time.time() - t0

        combined = proc.stdout + "\n" + proc.stderr
        executed = "simulation executed" in combined.lower()
        has_error = "error" in combined.lower() and "Note:" not in combined
        result.success = executed and not has_error

        # Internal implementation note.
        result.measurements = self._parse_measures(combined)
        result.operating_points = self._parse_op(combined)

        if suffix == "ac":
            ac_path = Path(os.path.dirname(cir_path)) / "ac_sweep.dat"
            curve_perf = self._extract_ac_curve_performance(ac_path)
            if curve_perf:
                if "phase_margin" in result.measurements:
                    result.measurements["phase_at_unity_meas"] = result.measurements["phase_margin"]
                result.measurements.update(curve_perf)

        if suffix == "tran":
            result.measurements = {
                k: v for k, v in result.measurements.items()
                if k.startswith("slew_rate") or k.startswith("tran_")
            }
            tran_path = Path(os.path.dirname(cir_path)) / "tran_sweep.dat"
            result.measurements.update(self._extract_tran_curve_performance(tran_path, netlist))

        # Internal implementation note.
        if suffix == "dc":
            # Internal implementation note.
            result.measurements = {
                k: v for k, v in result.measurements.items()
                if k in ("total_power", "i_vdd") or k.endswith("_power")
            }
            dc_perf = self._extract_dc_performance(combined, result.operating_points)
            result.measurements.update(dc_perf)
            result.measurements.update(self._extract_headroom_performance(netlist, result.operating_points))

        return result

    def _write_spiceinit_if_needed(self, netlist: str, work_path: Path) -> None:
        osdi_paths = []
        for line in netlist.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("* .osdi "):
                osdi_paths.append(stripped.split(None, 2)[2])
        if not osdi_paths:
            return
        lines = ["* Auto-generated by AnalogRF-IR v0.1 for OSDI-backed PDK models"]
        for path in osdi_paths:
            lines.append(f"osdi {path}")
        try:
            (work_path / ".spiceinit").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    # Internal implementation note.

    def _parse_measures(self, stdout: str) -> Dict[str, float]:
        """AnalogRF-IR internal documentation."""
        measures = {}
        pat = re.compile(
            r"^\s*([\w_]+)\s*(?:at\s+\S+\s+)?=\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)",
            re.MULTILINE
        )
        for m in pat.finditer(stdout):
            name = m.group(1).strip().lower().replace(" ", "_")
            try:
                measures[name] = float(m.group(2))
            except ValueError:
                continue
        return measures

    def _extract_ac_curve_performance(self, path: Path) -> Dict[str, float]:
        rows = self._read_numeric_table(path)
        if len(rows) < 3:
            return {}

        columns = self._select_ac_columns(rows)
        if columns is None:
            return {}
        f_col, gain_col, phase_col = columns

        freqs = [row[f_col] for row in rows if len(row) > max(f_col, gain_col, phase_col)]
        gains = [row[gain_col] for row in rows if len(row) > max(f_col, gain_col, phase_col)]
        phases = [row[phase_col] for row in rows if len(row) > max(f_col, gain_col, phase_col)]
        samples = [
            (f, g, p) for f, g, p in zip(freqs, gains, phases)
            if f > 0 and math.isfinite(f) and math.isfinite(g) and math.isfinite(p)
        ]
        if len(samples) < 3:
            return {}
        samples.sort(key=lambda item: item[0])
        freqs = [item[0] for item in samples]
        gains = [item[1] for item in samples]
        phases = [item[2] for item in samples]

        if max(abs(p) for p in phases) <= 2.0 * math.pi + 0.1:
            phases = [p * 180.0 / math.pi for p in phases]
        phases_unwrapped = self._unwrap_degrees(phases)

        crossings = self._unity_crossings(freqs, gains, phases_unwrapped)
        perf: Dict[str, float] = {
            "dc_gain_db": max(gains),
            "ac_curve_points": float(len(samples)),
        }
        if not crossings:
            return perf

        base_phase = phases_unwrapped[0]
        evaluated = []
        for freq, phase in crossings:
            rel_phase = phase - base_phase
            while rel_phase > 0.0:
                rel_phase -= 360.0
            pm = 180.0 + rel_phase
            evaluated.append((pm, freq, phase, rel_phase))

        pm, freq, phase, rel_phase = min(evaluated, key=lambda item: item[0])
        perf.update({
            "unity_gain_bandwidth": freq,
            "phase_margin": pm,
            "phase_at_unity_unwrapped": phase,
            "phase_lag_at_unity": -rel_phase,
            "unity_gain_crossings": float(len(crossings)),
            "phase_margin_from_curve": 1.0,
        })
        return perf

    def _read_numeric_table(self, path: Path) -> List[List[float]]:
        if not path.exists():
            return []
        rows: List[List[float]] = []
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return rows
        for line in text.splitlines():
            vals: List[float] = []
            for token in line.replace(",", " ").split():
                try:
                    vals.append(float(token))
                except ValueError:
                    vals = []
                    break
            if len(vals) >= 2:
                rows.append(vals)
        return rows

    def _select_ac_columns(self, rows: List[List[float]]) -> Optional[Tuple[int, int, int]]:
        width = min(len(row) for row in rows)
        layouts = [(0, 1, 2)]
        if width >= 4:
            layouts.append((0, 1, 3))
        if width >= 5:
            layouts.append((0, 2, 4))
        if width >= 6:
            layouts.append((0, 3, 5))

        best: Optional[Tuple[int, int, int]] = None
        best_score = float("-inf")
        for f_col, gain_col, phase_col in layouts:
            if max(f_col, gain_col, phase_col) >= width:
                continue
            freqs = [row[f_col] for row in rows]
            gains = [row[gain_col] for row in rows]
            phases = [row[phase_col] for row in rows]
            if any(f <= 0 or not math.isfinite(f) for f in freqs):
                continue
            monotonic = all(b >= a for a, b in zip(freqs, freqs[1:]))
            if not monotonic:
                continue
            gain_span = max(gains) - min(gains)
            crossings = sum(
                1 for a, b in zip(gains, gains[1:])
                if (a >= 0.0 >= b) or (a <= 0.0 <= b)
            )
            phase_abs = max(abs(p) for p in phases)
            score = 10.0
            score += min(gain_span, 120.0) / 20.0
            score += 5.0 if crossings else 0.0
            score += 2.0 if phase_abs < 1000.0 else -10.0
            if score > best_score:
                best_score = score
                best = (f_col, gain_col, phase_col)
        return best

    def _unwrap_degrees(self, phases: List[float]) -> List[float]:
        if not phases:
            return []
        out = [phases[0]]
        offset = 0.0
        prev = phases[0]
        for phase in phases[1:]:
            candidate = phase + offset
            delta = candidate - prev
            while delta > 180.0:
                offset -= 360.0
                candidate = phase + offset
                delta = candidate - prev
            while delta < -180.0:
                offset += 360.0
                candidate = phase + offset
                delta = candidate - prev
            out.append(candidate)
            prev = candidate
        return out

    def _unity_crossings(
        self,
        freqs: List[float],
        gains: List[float],
        phases: List[float],
    ) -> List[Tuple[float, float]]:
        crossings: List[Tuple[float, float]] = []
        for i in range(1, len(freqs)):
            g0 = gains[i - 1]
            g1 = gains[i]
            if not ((g0 >= 0.0 >= g1) or (g0 <= 0.0 <= g1)):
                continue
            if g0 == g1:
                frac = 0.0
            else:
                frac = (0.0 - g0) / (g1 - g0)
            frac = min(max(frac, 0.0), 1.0)
            log_f0 = math.log10(freqs[i - 1])
            log_f1 = math.log10(freqs[i])
            freq = 10.0 ** (log_f0 + frac * (log_f1 - log_f0))
            phase = phases[i - 1] + frac * (phases[i] - phases[i - 1])
            if crossings and abs(freq - crossings[-1][0]) <= max(freq, 1.0) * 1e-9:
                continue
            crossings.append((freq, phase))
        return crossings

    def _extract_tran_curve_performance(self, path: Path, netlist: str = "") -> Dict[str, float]:
        rows = self._read_numeric_table(path)
        if len(rows) < 4:
            return {}
        columns = self._select_tran_columns(rows)
        if columns is None:
            return {}
        t_col, v_col = columns
        samples = [
            (row[t_col], row[v_col])
            for row in rows
            if len(row) > max(t_col, v_col)
            and math.isfinite(row[t_col])
            and math.isfinite(row[v_col])
        ]
        if len(samples) < 4:
            return {}
        samples.sort(key=lambda item: item[0])
        deduped: List[Tuple[float, float]] = []
        for t, v in samples:
            if deduped and abs(t - deduped[-1][0]) <= max(abs(t), 1.0) * 1e-15:
                deduped[-1] = (t, v)
            else:
                deduped.append((t, v))
        if len(deduped) < 4:
            return {}
        times = [item[0] for item in deduped]
        volts = [item[1] for item in deduped]
        span = max(volts) - min(volts)
        if span <= 1e-6:
            return {
                "slew_rate": 0.0,
                "slew_rate_pos": 0.0,
                "slew_rate_neg": 0.0,
                "tran_curve_points": float(len(deduped)),
                "tran_output_span": span,
            }

        window = max(1, min(25, len(deduped) // 200))
        pos_slopes: list[float] = []
        neg_slopes: list[float] = []
        for i in range(0, len(deduped) - window):
            dt = times[i + window] - times[i]
            if dt <= 0:
                continue
            slope = (volts[i + window] - volts[i]) / dt
            if slope > 0.0:
                pos_slopes.append(slope)
            if slope < 0.0:
                neg_slopes.append(-slope)
        pos = self._robust_high_slope(pos_slopes)
        neg = self._robust_high_slope(neg_slopes)
        metrics = {
            "slew_rate": min(pos, neg),
            "slew_rate_pos": pos,
            "slew_rate_neg": neg,
            "tran_curve_points": float(len(deduped)),
            "tran_output_span": span,
            "tran_t_start": times[0],
            "tran_t_stop": times[-1],
        }
        comparator_like = "comparator" in netlist.lower() or re.search(
            r"^\s*Vclk\s+", netlist, flags=re.IGNORECASE | re.MULTILINE
        )
        if comparator_like:
            delay_metrics = self._extract_comparator_timing(times, volts, netlist)
            metrics.update(delay_metrics)
        return metrics

    @staticmethod
    def _robust_high_slope(values: list[float], quantile: float = 0.98) -> float:
        clean = sorted(float(value) for value in values if math.isfinite(float(value)) and value > 0.0)
        if not clean:
            return 0.0
        index = int(round((len(clean) - 1) * min(max(float(quantile), 0.0), 1.0)))
        return clean[index]

    def _extract_comparator_timing(
        self,
        times: List[float],
        volts: List[float],
        netlist: str,
    ) -> Dict[str, float]:
        span = max(volts) - min(volts)
        required_span = 0.25 * max(self._infer_vdd(netlist), 1e-9)
        if span <= max(required_span, 1e-6):
            return {}
        v0 = volts[0]
        vf = volts[-1]
        rising = vf >= v0
        v_min = min(volts)
        v_max = max(volts)
        mid = v_min + 0.5 * span
        lo = v_min + 0.1 * span
        hi = v_min + 0.9 * span

        def crossing(threshold: float) -> Optional[float]:
            for idx in range(1, len(times)):
                a = volts[idx - 1]
                b = volts[idx]
                if rising:
                    hit = a <= threshold <= b
                else:
                    hit = a >= threshold >= b
                if not hit or a == b:
                    continue
                frac = (threshold - a) / (b - a)
                return times[idx - 1] + frac * (times[idx] - times[idx - 1])
            return None

        t_mid = crossing(mid)
        if t_mid is None:
            return {}
        reference = self._infer_clock_edge(netlist)
        out: Dict[str, float] = {"delay": max(t_mid - reference, 0.0)}
        t_lo = crossing(lo)
        t_hi = crossing(hi)
        if t_lo is not None and t_hi is not None:
            out["regeneration_time"] = abs(t_hi - t_lo)
        return out

    def _infer_clock_edge(self, netlist: str) -> float:
        m = re.search(
            r"^\s*Vclk\s+\S+\s+\S+\s+(?:DC\s+\S+\s+)?PULSE\(([^)]*)\)",
            netlist,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if not m:
            return 0.0
        parts = m.group(1).replace(",", " ").split()
        if len(parts) < 3:
            return 0.0
        try:
            return self._spice_number(parts[2])
        except ValueError:
            return 0.0

    def _select_tran_columns(self, rows: List[List[float]]) -> Optional[Tuple[int, int]]:
        width = min(len(row) for row in rows)
        best: Optional[Tuple[int, int]] = None
        best_score = float("-inf")
        for t_col in range(width):
            times = [row[t_col] for row in rows]
            if any(not math.isfinite(t) for t in times):
                continue
            monotonic = all(b >= a for a, b in zip(times, times[1:]))
            if not monotonic or max(times) <= min(times):
                continue
            for v_col in range(width):
                if v_col == t_col:
                    continue
                values = [row[v_col] for row in rows]
                if any(not math.isfinite(v) for v in values):
                    continue
                span = max(values) - min(values)
                if span <= 1e-9:
                    continue
                score = span
                if 0.0 <= min(values) <= max(values) <= 2.5:
                    score += 10.0
                if t_col == 0:
                    score += 1.0
                if score > best_score:
                    best_score = score
                    best = (t_col, v_col)
        return best

    def _parse_op(self, stdout: str) -> Dict[str, Dict[str, float]]:
        """AnalogRF-IR internal documentation."""
        op = {}
        patterns = [
            re.compile(r"([mM]+\d[\w]*)#(\w+)\s*=\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)", re.MULTILINE),
            re.compile(r"@([^\s\[]+)\[(\w+)\]\s*=\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)", re.MULTILINE),
        ]
        for pat in patterns:
            for m in pat.finditer(stdout):
                dev = self._canonical_op_device_name(m.group(1))
                param = self._canonical_op_param(m.group(2).lower())
                try:
                    val = float(m.group(3))
                except ValueError:
                    continue
                op.setdefault(dev, {})[param] = val
        return op

    def _extract_headroom_performance(
        self,
        netlist: str,
        op: Dict[str, Dict[str, float]],
    ) -> Dict[str, float]:
        devices = self._parse_mos_devices(netlist)
        if not devices:
            return {}
        vdd = self._infer_vdd(netlist)
        vss = self._infer_vss(netlist)
        factor = self._infer_vdsat_factor(netlist)
        output_node = self._infer_output_node(netlist)

        perf: Dict[str, float] = {}
        if output_node:
            output = self._output_swing_from_op(devices, op, output_node, vdd, vss, factor)
            perf.update(output)
        icmr = self._icmr_from_op(devices, op, vdd, vss, factor)
        perf.update(icmr)
        perf.update(self._saturation_margin_from_op(devices, op, factor=factor, required_margin=0.01))
        return perf

    def _saturation_margin_from_op(
        self,
        devices: List[Dict[str, str]],
        op: Dict[str, Dict[str, float]],
        *,
        factor: float,
        required_margin: float,
    ) -> Dict[str, float]:
        margins: List[float] = []
        for dev in devices:
            vds = self._op_abs(op, dev["id"], "vds")
            vdsat = self._op_abs(op, dev["id"], "vdsat")
            if vds <= 0.0 or vdsat <= 0.0:
                continue
            margins.append(vds - factor * vdsat)
        if not margins:
            return {}
        margin = min(margins)
        return {
            "saturation_margin": margin,
            "saturation_required_gap": margin - required_margin,
        }

    def _parse_mos_devices(self, netlist: str) -> List[Dict[str, str]]:
        devices: List[Dict[str, str]] = []
        for line in netlist.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("*"):
                continue
            parts = stripped.split()
            if len(parts) < 6:
                continue
            name = parts[0]
            if name[0].upper() not in {"M", "X"}:
                continue
            model = parts[5]
            device_type = self._mos_type_from_model(model)
            if not device_type:
                continue
            canonical = name.upper()
            if canonical.startswith("X"):
                canonical = canonical[1:]
            devices.append(
                {
                    "id": canonical,
                    "raw_id": name.upper(),
                    "drain": parts[1].lower(),
                    "gate": parts[2].lower(),
                    "source": parts[3].lower(),
                    "body": parts[4].lower(),
                    "model": model,
                    "type": device_type,
                }
            )
        return devices

    def _mos_type_from_model(self, model: str) -> Optional[str]:
        low = model.lower()
        if "pmos" in low or "pch" in low or low.startswith("p"):
            return "pmos"
        if "nmos" in low or "nch" in low or low.startswith("n"):
            return "nmos"
        return None

    def _infer_output_node(self, netlist: str) -> str:
        m = re.search(r"^\s*Cload\s+(\S+)\s+", netlist, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).lower()
        return "vout"

    def _infer_vss(self, netlist: str) -> float:
        m = re.search(r"^\s*Vss\s+\S+\s+\S+\s+DC\s+(\S+)", netlist, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            try:
                return -abs(self._spice_number(m.group(1)))
            except ValueError:
                pass
        return 0.0

    def _infer_vdsat_factor(self, netlist: str) -> float:
        m = re.search(r"^\s*\*\s*VDSAT_headroom_factor:\s*(\S+)", netlist, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            try:
                return max(0.0, self._spice_number(m.group(1)))
            except ValueError:
                pass
        return 1.0

    def _output_swing_from_op(
        self,
        devices: List[Dict[str, str]],
        op: Dict[str, Dict[str, float]],
        output_node: str,
        vdd: float,
        vss: float,
        factor: float,
    ) -> Dict[str, float]:
        pullups = [
            dev for dev in devices
            if dev["drain"] == output_node and dev["type"] == "pmos"
        ]
        pulldowns = [
            dev for dev in devices
            if dev["drain"] == output_node and dev["type"] == "nmos"
        ]
        if not pullups or not pulldowns:
            return {}
        p_vdsat = max(self._op_abs(op, dev["id"], "vdsat") for dev in pullups)
        n_vdsat = max(self._op_abs(op, dev["id"], "vdsat") for dev in pulldowns)
        if p_vdsat <= 0.0 or n_vdsat <= 0.0:
            return {}
        low = vss + factor * n_vdsat
        high = vdd - factor * p_vdsat
        return {
            "output_swing": max(0.0, high - low),
            "output_swing_low": low,
            "output_swing_high": high,
        }

    def _icmr_from_op(
        self,
        devices: List[Dict[str, str]],
        op: Dict[str, Dict[str, float]],
        vdd: float,
        vss: float,
        factor: float,
    ) -> Dict[str, float]:
        input_nodes = self._input_nodes_from_sources(devices)
        input_devs = [
            dev for dev in devices
            if dev["gate"] in input_nodes
        ]
        n_inputs = [dev for dev in input_devs if dev["type"] == "nmos"]
        if len(n_inputs) < 1:
            return {}
        inp = n_inputs[0]
        source_node = inp["source"]
        tail_candidates = [
            dev for dev in devices
            if dev["type"] == "nmos" and dev["drain"] == source_node and dev["id"] != inp["id"]
        ]
        load_candidates = [
            dev for dev in devices
            if dev["type"] == "pmos" and dev["drain"] == inp["drain"]
        ]
        if not tail_candidates or not load_candidates:
            return {}
        tail = tail_candidates[0]
        load = load_candidates[0]
        vgs_in = self._op_abs(op, inp["id"], "vgs")
        vdsat_in = self._op_abs(op, inp["id"], "vdsat")
        vdsat_tail = self._op_abs(op, tail["id"], "vdsat")
        vdsat_load = self._op_abs(op, load["id"], "vdsat")
        if min(vgs_in, vdsat_in, vdsat_tail, vdsat_load) <= 0.0:
            return {}
        icmr_min = vss + vgs_in + factor * vdsat_tail
        drain_limit = vdd - factor * vdsat_load
        icmr_max = drain_limit - factor * vdsat_in + vgs_in
        if icmr_max < icmr_min:
            icmr_max = icmr_min
        return {
            "icmr": max(0.0, icmr_max - icmr_min),
            "icmr_min": icmr_min,
            "icmr_max": icmr_max,
        }

    def _input_nodes_from_sources(self, devices: List[Dict[str, str]]) -> set[str]:
        nodes = {"vin", "vinp", "vinn", "inp", "inn", "in_p", "in_n"}
        return nodes | {dev["gate"] for dev in devices if dev["gate"].startswith("vin")}

    def _op_abs(self, op: Dict[str, Dict[str, float]], device_id: str, param: str) -> float:
        value = self._op_abs_optional(op, device_id, param)
        return value if value is not None else 0.0

    def _op_abs_optional(
        self,
        op: Dict[str, Dict[str, float]],
        device_id: str,
        param: str,
    ) -> Optional[float]:
        for name in (device_id.upper(), f"M{device_id}".upper()):
            if name in op and param in op[name]:
                return abs(float(op[name][param]))
        return None

    def _canonical_op_device_name(self, raw: str) -> str:
        name = raw.upper()
        m = re.search(r"\.X(M+\d[\w]*)\.", name)
        if m:
            return m.group(1)
        return name

    def _canonical_op_param(self, param: str) -> str:
        if param == "ids":
            return "id"
        if param == "vdss":
            return "vdsat"
        return param

    def _extract_dc_performance(
        self, stdout: str, op: Dict[str, Dict[str, float]]
    ) -> Dict[str, float]:
        """AnalogRF-IR internal documentation."""
        perf = {}
        for line in stdout.split("\n"):
            if "vdd" in line and "branch" in line and "=" in line:
                m = re.search(r"=\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)", line)
                if m:
                    i_vdd = abs(float(m.group(1)))
                    perf["total_power"] = 1.2 * i_vdd
                    perf["i_vdd"] = i_vdd
                    break
        if "total_power" not in perf:
            for did in ("MM5", "M5"):
                m5_id = op.get(did, {}).get("id", 0)
                if m5_id > 0:
                    perf["total_power"] = 1.2 * m5_id
                    perf["i_vdd"] = m5_id
                    break
        return perf


def run_simulation(
    netlist: str,
    ngspice_bin: str = "ngspice",
    work_dir: Optional[str] = None,
) -> SimulationResult:
    sim = NgspiceSimulator(ngspice_bin=ngspice_bin)
    return sim.run(netlist, work_dir)
