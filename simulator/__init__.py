"""
ngspice 仿真器模块 V2.0

提供 ngspice 仿真接口：运行仿真、解析 .measure 结果、提取晶体管工作点。
"""
from .ngspice import NgspiceSimulator, SimulationResult
