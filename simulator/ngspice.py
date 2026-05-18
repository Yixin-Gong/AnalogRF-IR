"""
ngspice 仿真器接口 V2.0 — ngspice 45 兼容 · 双 pass 架构

策略（ngspice 45.2 限制：.meas ac 不能与 .dc + .control 共存）：
  Pass 1: 纯 AC — .ac + .meas ac → 提取 gain/GBW/PM
  Pass 2: 纯 DC — .dc + .control print all → 提取 power + 工作点

合并结果到 SimulationResult。
"""
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
    """ngspice 仿真结果。"""

    success: bool = False
    return_code: int = -1
    elapsed_sec: float = 0.0

    # 性能指标: {"dc_gain_db": 41.7, "ugbw": 4.82e7, "pm": 85.0, "total_power": 4.05e-5, ...}
    measurements: Dict[str, float] = field(default_factory=dict)

    # 晶体管工作点: {"MM1": {"gate": 0.6, "id": 1.69e-5, ...}, ...}
    operating_points: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # 原始输出（调试）
    raw_stdout: str = ""
    raw_stderr: str = ""


class NgspiceSimulator:
    """ngspice 批量仿真器（双 pass，兼容 ngspice 45.2 + BSIM4）。"""

    def __init__(self, ngspice_bin: str = "ngspice", timeout_sec: float = 60.0):
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

    def run(self, netlist: str, work_dir: Optional[str] = None) -> SimulationResult:
        """双 pass 仿真：AC 测量 + DC 工作点。"""
        t0 = time.time()

        result_ac = self._run_ac_pass(netlist, work_dir)
        result_dc = self._run_dc_pass(netlist, work_dir)

        merged = SimulationResult()
        merged.elapsed_sec = time.time() - t0

        # 合并测量值
        merged.measurements = {}
        merged.measurements.update(result_dc.measurements)  # DC: total_power
        merged.measurements.update(result_ac.measurements)  # AC: dc_gain_db, ugbw, pm

        # 工作点从 DC pass
        merged.operating_points = result_dc.operating_points

        merged.raw_stdout = result_ac.raw_stdout + "\n" + result_dc.raw_stdout
        merged.raw_stderr = result_ac.raw_stderr + "\n" + result_dc.raw_stderr

        merged.success = bool(merged.measurements)  # 有测量值即成功

        return merged

    # ── Pass 1: AC ──

    def _run_ac_pass(self, netlist: str, work_dir: Optional[str]) -> SimulationResult:
        """Pass 1: 纯 AC 分析 + Bode 曲线导出。

        `.meas ac phase_margin find vp(vout) when vdb(vout)=0` only returns the
        principal phase at one crossing. For compensated OTAs that can hide phase
        wrapping or secondary unity-gain crossings, so the final PM is calculated
        from the exported sweep in Python.
        """
        # 去掉 .dc 行和 .control 块
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
        """移除 .dc 和 .control 块，保留 .ac 和 .meas ac。"""
        lines = netlist.split("\n")
        out = []
        skip_control = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(".control"):
                skip_control = True
                continue
            if skip_control:
                if stripped.startswith(".endc"):
                    skip_control = False
                continue
            # 移除 .dc 分析行
            if stripped.startswith(".dc ") or stripped.startswith(".dc\t"):
                continue
            out.append(line)
        return "\n".join(out)

    # ── Pass 2: DC ──

    def _run_dc_pass(self, netlist: str, work_dir: Optional[str]) -> SimulationResult:
        """Pass 2: 纯 DC 分析 + .control print all。"""
        lines = netlist.split("\n")
        out = []
        for line in lines:
            stripped = line.strip()
            # 注释掉 .ac 行
            if stripped.startswith(".ac ") or stripped.startswith(".ac\t"):
                out.append(f"* {line}")
            # 移除 .meas ac 行
            elif ".meas ac" in stripped:
                out.append(f"* {line}")
            else:
                out.append(line)

        # 确保有 .dc（如果没有，添加单点 dc）
        has_dc = any(l.strip().startswith(".dc ") for l in out)

        # 添加 .control 块
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

    # ── 执行 ──

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

        try:
            proc = subprocess.run(
                [self.ngspice_bin, "-b", cir_path],
                capture_output=True, text=True,
                timeout=self.timeout_sec,
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

        # 解析
        result.measurements = self._parse_measures(combined)
        result.operating_points = self._parse_op(combined)

        if suffix == "ac":
            ac_path = Path(os.path.dirname(cir_path)) / "ac_sweep.dat"
            curve_perf = self._extract_ac_curve_performance(ac_path)
            if curve_perf:
                if "phase_margin" in result.measurements:
                    result.measurements["phase_at_unity_meas"] = result.measurements["phase_margin"]
                result.measurements.update(curve_perf)

        # DC pass: 保留 .meas dc 解析 + 补充 vdd#branch fallback
        if suffix == "dc":
            # 过滤掉 _parse_measures 误捕获的节点电压（如 net1=, vout= 等）
            result.measurements = {
                k: v for k, v in result.measurements.items()
                if k in ("total_power", "i_vdd") or k.endswith("_power")
            }
            dc_perf = self._extract_dc_performance(combined, result.operating_points)
            result.measurements.update(dc_perf)

        return result

    def _write_spiceinit_if_needed(self, netlist: str, work_path: Path) -> None:
        osdi_paths = []
        for line in netlist.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("* .osdi "):
                osdi_paths.append(stripped.split(None, 2)[2])
        if not osdi_paths:
            return
        lines = ["* Auto-generated by objective_ir for OSDI-backed PDK models"]
        for path in osdi_paths:
            lines.append(f"osdi {path}")
        try:
            (work_path / ".spiceinit").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    # ── 解析 ──

    def _parse_measures(self, stdout: str) -> Dict[str, float]:
        """解析 .meas 输出行。

        ngspice 对短测量名输出:  dc_gain_db          =  4.807e+01 at=  1.000e+00
        对长测量名(>~20字符):    unity_gain_bandwidth=   7.428e+07
        正则需兼容两种格式: name 后可有0~若干空格, at= 可选。
        """
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
            if len(vals) >= 3:
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

    def _parse_op(self, stdout: str) -> Dict[str, Dict[str, float]]:
        """解析 print all 输出中的晶体管工作点。"""
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
        """从 DC 输出提取 power。"""
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
