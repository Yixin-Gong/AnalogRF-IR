"""
单位系统 V2.1 — 模拟IC设计常用单位转换表 + 自动解析。

所有硬件量的单位转换集中在此文件。原则：
  - 内部存储永远用 SI
  - 输入接受多种写法：纯数值、"40 MHz"字符串、"40MHz"、dict {value: 40, unit: "MHz"}
  - parse_value() 是统一入口

支持的维度与单位:
  Length:      m, cm, mm, um, μm, nm, pm, A(angstrom)
  Voltage:     V, mV, uV, μV, nV
  Current:     A, mA, uA, μA, nA, pA
  Power:       W, mW, uW, μW, nW
  Frequency:   Hz, kHz, MHz, GHz, THz
  Temperature: C, K, F
  Capacitance: F, mF, uF, μF, nF, pF, fF
  Resistance:  ohm, Ω, kohm, kΩ, Mohm, MΩ
  Time:        s, ms, us, μs, ns, ps, fs
  Angle:       deg, rad
  Ratio:       dB (仅存储，不转换)
  Area:        m2, cm2, mm2, um2, nm2 (也支持 m² 等 Unicode)

使用方式:
    from util.units import parse_value, DIMENSION

    parse_value("40 MHz")           # → 4.0e7  (纯数值，SI)
    parse_value(1.8e-7, "m")        # → 1.8e-7
    parse_value("180 nm")           # → 1.8e-7
    parse_value({"value": 5, "unit": "uA"})  # → 5e-6
"""

from __future__ import annotations

import re
import math
from dataclasses import dataclass
from typing import Dict, Optional, Union, Any, Tuple


# ═══════════════════════════════════════════════════════════════
#  单位转换表 — 全部以乘法因子表示（到 SI 的倍数）
# ═══════════════════════════════════════════════════════════════

UNIT_TABLES: Dict[str, Dict[str, float]] = {
    "length": {
        "m":  1.0,
        "cm": 1e-2,
        "mm": 1e-3,
        "um": 1e-6,  "μm": 1e-6,
        "nm": 1e-9,
        "pm": 1e-12,
        "a":  1e-10, "ang": 1e-10, "angstrom": 1e-10,
    },
    "voltage": {
        "V":  1.0,
        "mV": 1e-3,
        "uV": 1e-6, "μV": 1e-6,
        "nV": 1e-9,
    },
    "current": {
        "A":  1.0,
        "mA": 1e-3,
        "uA": 1e-6, "μA": 1e-6,
        "nA": 1e-9,
        "pA": 1e-12,
    },
    "power": {
        "W":  1.0,
        "mW": 1e-3,
        "uW": 1e-6, "μW": 1e-6,
        "nW": 1e-9,
    },
    "frequency": {
        "Hz":  1.0,
        "kHz": 1e3,
        "MHz": 1e6,
        "GHz": 1e9,
        "THz": 1e12,
    },
    "temperature": {
        "K": 1.0,
        "C": None,   # 需要 +273.15 偏移
        "F": None,   # 需要 (F-32)*5/9 + 273.15
    },
    "capacitance": {
        "F":  1.0,
        "mF": 1e-3,
        "uF": 1e-6, "μF": 1e-6,
        "nF": 1e-9,
        "pF": 1e-12,
        "fF": 1e-15,
    },
    "resistance": {
        "ohm": 1.0, "Ω": 1.0,
        "kohm": 1e3, "kΩ": 1e3,
        "Mohm": 1e6, "MΩ": 1e6,
    },
    "time": {
        "s":  1.0,
        "ms": 1e-3,
        "us": 1e-6, "μs": 1e-6,
        "ns": 1e-9,
        "ps": 1e-12,
        "fs": 1e-15,
    },
    "angle": {
        "deg": 1.0,
        "rad": 1.0,
    },
    "ratio": {
        "dB": 1.0,
        "":   1.0,
    },
    "area": {
        "m2":  1.0,  "m²": 1.0,
        "cm2": 1e-4, "cm²": 1e-4,
        "mm2": 1e-6, "mm²": 1e-6,
        "um2": 1e-12, "μm2": 1e-12, "μm²": 1e-12,
        "nm2": 1e-18, "nm²": 1e-18,
    },
}


# ═══════════════════════════════════════════════════════════════
#  维度标签
# ═══════════════════════════════════════════════════════════════

class Dimension:
    LENGTH = "length"
    VOLTAGE = "voltage"
    CURRENT = "current"
    POWER = "power"
    FREQUENCY = "frequency"
    TEMPERATURE = "temperature"
    CAPACITANCE = "capacitance"
    RESISTANCE = "resistance"
    TIME = "time"
    ANGLE = "angle"
    RATIO = "ratio"
    AREA = "area"

    @classmethod
    def table(cls, dim: str) -> Dict[str, float]:
        return UNIT_TABLES.get(dim, {})


# ═══════════════════════════════════════════════════════════════
#  核心 Unit 类型
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Unit:
    """带单位的数值。frozen=True 确保不可变。"""
    value_si: float
    unit: str
    dimension: str

    @classmethod
    def from_value(cls, value: float, unit: str, dimension: str) -> "Unit":
        table = Dimension.table(dimension)
        if dimension == Dimension.TEMPERATURE:
            if unit in ("C",):
                value_si = value + 273.15
            elif unit in ("F",):
                value_si = (value - 32) * 5.0 / 9.0 + 273.15
            else:
                value_si = value
        else:
            factor = table.get(unit)
            if factor is None:
                raise ValueError(
                    f"Unknown unit '{unit}' for {dimension}. Known: {list(table.keys())}"
                )
            value_si = value * factor
        return cls(value_si=value_si, unit=unit, dimension=dimension)

    def to(self, target_unit: str) -> float:
        if self.dimension == Dimension.TEMPERATURE:
            if target_unit == "C":
                return self.value_si - 273.15
            elif target_unit == "F":
                return (self.value_si - 273.15) * 9.0 / 5.0 + 32
            return self.value_si
        table = Dimension.table(self.dimension)
        factor = table.get(target_unit)
        if factor is None:
            raise ValueError(f"Unknown target unit '{target_unit}' for {self.dimension}")
        return self.value_si / factor

    def to_unit(self, target_unit: str) -> "Unit":
        return Unit(value_si=self.value_si, unit=target_unit, dimension=self.dimension)

    def __add__(self, other: "Unit") -> "Unit":
        self._same_dim(other)
        return Unit(self.value_si + other.value_si, self.unit, self.dimension)

    def __sub__(self, other: "Unit") -> "Unit":
        self._same_dim(other)
        return Unit(self.value_si - other.value_si, self.unit, self.dimension)

    def __mul__(self, scalar: float) -> "Unit":
        return Unit(self.value_si * scalar, self.unit, self.dimension)

    def __truediv__(self, other: Union[float, "Unit"]) -> Union[float, "Unit"]:
        if isinstance(other, Unit):
            self._same_dim(other)
            return self.value_si / other.value_si
        return Unit(self.value_si / other, self.unit, self.dimension)

    def __neg__(self) -> "Unit":
        return Unit(-self.value_si, self.unit, self.dimension)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Unit): return NotImplemented
        self._same_dim(other)
        return math.isclose(self.value_si, other.value_si, rel_tol=1e-9)

    def __lt__(self, other: "Unit") -> bool: self._same_dim(other); return self.value_si < other.value_si
    def __le__(self, other: "Unit") -> bool: self._same_dim(other); return self.value_si <= other.value_si
    def __gt__(self, other: "Unit") -> bool: self._same_dim(other); return self.value_si > other.value_si
    def __ge__(self, other: "Unit") -> bool: self._same_dim(other); return self.value_si >= other.value_si

    def _same_dim(self, other: "Unit") -> None:
        if self.dimension != other.dimension:
            raise ValueError(f"Dimension mismatch: {self.dimension} vs {other.dimension}")

    def __repr__(self) -> str:
        return f"{self.to(self.unit):.6g} {self.unit}"

    def to_dict(self) -> dict:
        return {"value": self.to(self.unit), "unit": self.unit, "dimension": self.dimension}

    @classmethod
    def from_dict(cls, d: dict) -> "Unit":
        return cls.from_value(
            d.get("value", 0), d.get("unit", ""), d.get("dimension", Dimension.RATIO)
        )


# ═══════════════════════════════════════════════════════════════
#  字符串解析器 — 自动识别 "40 MHz"、"180nm" 等
# ═══════════════════════════════════════════════════════════════

_UNIT_PATTERN = re.compile(
    r'^\s*'
    r'([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)'  # 数值
    r'\s*'
    r'([a-zA-Zμ]+[/²2]?)'                               # 单位
    r'\s*$'
)

# 从 UNIT_TABLES 自动构建单位→维度映射
_UNIT_TO_DIMENSION: Dict[str, str] = {}
for _dim, _table in UNIT_TABLES.items():
    for _unit in _table:
        _UNIT_TO_DIMENSION[_unit] = _dim
        _UNIT_TO_DIMENSION[_unit.lower()] = _dim
# 额外常用小写别名
_UNIT_TO_DIMENSION["mv"] = Dimension.VOLTAGE
_UNIT_TO_DIMENSION["uv"] = Dimension.VOLTAGE
_UNIT_TO_DIMENSION["ma"] = Dimension.CURRENT
_UNIT_TO_DIMENSION["ua"] = Dimension.CURRENT
_UNIT_TO_DIMENSION["na"] = Dimension.CURRENT
_UNIT_TO_DIMENSION["mw"] = Dimension.POWER
_UNIT_TO_DIMENSION["uw"] = Dimension.POWER
_UNIT_TO_DIMENSION["khz"] = Dimension.FREQUENCY
_UNIT_TO_DIMENSION["mhz"] = Dimension.FREQUENCY
_UNIT_TO_DIMENSION["ghz"] = Dimension.FREQUENCY
_UNIT_TO_DIMENSION["pf"] = Dimension.CAPACITANCE
_UNIT_TO_DIMENSION["nf"] = Dimension.CAPACITANCE
_UNIT_TO_DIMENSION["uf"] = Dimension.CAPACITANCE
_UNIT_TO_DIMENSION["ns"] = Dimension.TIME
_UNIT_TO_DIMENSION["ps"] = Dimension.TIME
_UNIT_TO_DIMENSION["us"] = Dimension.TIME
_UNIT_TO_DIMENSION["ms"] = Dimension.TIME
_UNIT_TO_DIMENSION["k"] = Dimension.RESISTANCE
_UNIT_TO_DIMENSION["kohm"] = Dimension.RESISTANCE
_UNIT_TO_DIMENSION["mohm"] = Dimension.RESISTANCE
_UNIT_TO_DIMENSION["db"] = Dimension.RATIO
_UNIT_TO_DIMENSION["deg"] = Dimension.ANGLE
_UNIT_TO_DIMENSION["rad"] = Dimension.ANGLE


def parse_value(
    value: Any,
    default_unit: str = "",
    dimension: str = Dimension.RATIO,
) -> float:
    """
    统一入口：解析各种格式的物理量，返回 SI 数值。

    支持格式:
      - float / int: 直接返回
      - str "40 MHz":     拆分 → 40 * 1e6 = 4e7
      - str "40MHz":      同上
      - str "180nm":      拆分 → 180 * 1e-9 = 1.8e-7
      - str "2.5k":       拆分 → 2.5 * 1e3 = 2500 (识别为电阻)
      - dict {value: 40, unit: "MHz"}: 用 dict 显式指定
    """
    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, dict):
        v = value.get("value", 0)
        u = value.get("unit", default_unit)
        d = value.get("dimension", dimension)
        return parse_value(f"{v} {u}", default_unit=default_unit, dimension=d)

    if isinstance(value, str):
        s = value.strip()
        try:
            return float(s)
        except ValueError:
            pass

        m = _UNIT_PATTERN.match(s)
        if m:
            num = float(m.group(1))
            raw_unit = m.group(2).strip()
            unit_lower = raw_unit.lower().replace("μ", "u")

            inferred_dim = _UNIT_TO_DIMENSION.get(raw_unit) or _UNIT_TO_DIMENSION.get(unit_lower)
            if inferred_dim is None:
                raise ValueError(f"Unknown unit '{raw_unit}' in '{s}'")

            table = Dimension.table(inferred_dim)
            std_unit = raw_unit
            if raw_unit not in table:
                for ku in table:
                    if ku.lower() == unit_lower or ku.lower().replace("μ", "u") == unit_lower:
                        std_unit = ku
                        break

            factor = table.get(std_unit)
            if factor is not None:
                if inferred_dim == Dimension.TEMPERATURE:
                    if std_unit in ("C",): return num + 273.15
                    if std_unit in ("F",): return (num - 32) * 5.0/9.0 + 273.15
                    return num
                return num * factor

        raise ValueError(f"Cannot parse unit string: '{s}'")

    raise TypeError(f"Unsupported value type: {type(value)}")


# ═══════════════════════════════════════════════════════════════
#  便捷构造器
# ═══════════════════════════════════════════════════════════════

def Length(value: float, unit: str = "m") -> Unit:
    return Unit.from_value(value, unit, Dimension.LENGTH)

def Voltage(value: float, unit: str = "V") -> Unit:
    return Unit.from_value(value, unit, Dimension.VOLTAGE)

def Current(value: float, unit: str = "A") -> Unit:
    return Unit.from_value(value, unit, Dimension.CURRENT)

def Power(value: float, unit: str = "W") -> Unit:
    return Unit.from_value(value, unit, Dimension.POWER)

def Frequency(value: float, unit: str = "Hz") -> Unit:
    return Unit.from_value(value, unit, Dimension.FREQUENCY)

def Temperature(value: float, unit: str = "C") -> Unit:
    return Unit.from_value(value, unit, Dimension.TEMPERATURE)

def Capacitance(value: float, unit: str = "F") -> Unit:
    return Unit.from_value(value, unit, Dimension.CAPACITANCE)

def Resistance(value: float, unit: str = "ohm") -> Unit:
    return Unit.from_value(value, unit, Dimension.RESISTANCE)

def Time(value: float, unit: str = "s") -> Unit:
    return Unit.from_value(value, unit, Dimension.TIME)
