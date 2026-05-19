"""
NSGA-II 优化器 V1.1 — 内层循环的自动搜索引擎。

在抽象的 (gm_id, L) 空间中进行多目标优化搜索。
使用 NSGA-II (Non-dominated Sorting Genetic Algorithm II) 算法。

核心流程：
1. 从 design_variables 解码决策变量
2. 通过 pygmid 查表翻译为物理尺寸
3. 调用仿真器（mock 或 ngspice）获取性能指标
4. 计算 loss_terms 定义的加权总 loss
5. 非支配排序 + 拥挤距离选择下一代

遵循"认知-执行分离"原则：优化器不直接修改 Schema，
只返回最优解的决策变量值。
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Tuple, Any
import numpy as np

from core.regions import compact_operating_region, inversion_region_from_gm_id, normalize_spice_region
from schemas.design_state import DesignState, DesignVariable, Range, LossTerm
from pygmid.adapter import PygmidAdapter, create_pygmid_adapter


# ── 个体定义 ──────────────────────────────────────────────────

@dataclass
class Individual:
    """NSGA-II 种群中的一个个体（候选设计点）。"""
    x: np.ndarray                          # 决策变量向量 [gm_id_M1, L_M1, gm_id_M2, ...]
    objectives: np.ndarray = field(default_factory=lambda: np.array([]))  # 目标值（多目标）
    constraints_violation: float = 0.0     # 约束违反总量
    rank: int = 0                          # 非支配排序层级
    crowding_distance: float = 0.0         # 拥挤距离
    feasible: bool = True                  # 是否满足所有约束
    # 记录翻译后的物理参数（用于调试）
    meta: Dict[str, Any] = field(default_factory=dict)


# ── NSGA-II 配置 ──────────────────────────────────────────────

@dataclass
class NSGA2Config:
    """NSGA-II 算法参数。"""
    pop_size: int = 100              # 种群大小
    n_generations: int = 50          # 最大代数
    # 交叉参数
    crossover_prob: float = 0.9      # 交叉概率
    crossover_eta: float = 15.0      # SBX 分布指数（越大越接近父代）
    # 变异参数
    mutation_prob: float = 0.1       # 变异概率（每个变量）
    mutation_eta: float = 20.0       # 多项式变异分布指数
    # 终止条件
    patience: int = 10               # 无改进等待代数
    tol: float = 1e-6                # 改进阈值
    # 随机种子
    seed: Optional[int] = None
    # 日志
    verbose: bool = True


# ── 电路评估器（替代 ngspice 的模拟仿真）──────────────────────

class CircuitEvaluator:
    """
    电路评估器 — 内层循环中的"仿真"步骤。

    在 Phase 1 中，使用简化手工模型估算电路性能。
    Phase 2 中将集成 ngspice 调用。

    评估流程：
      决策变量 → pygmid 翻译 → 手工电路公式 → 性能指标 → Loss 计算
    """

    def __init__(self, schema: DesignState, pygmid_adapter=None):
        self.schema = schema
        self.pygmid = pygmid_adapter or create_pygmid_adapter()
        # 缓存设计变量索引映射 (device → {variable → index})
        self._var_map = {}
        self._build_var_map()

    def _build_var_map(self) -> None:
        """建立决策变量到向量索引的映射 — 器件变量 + 全局变量。

        器件变量: _var_map[device][variable] = index
        全局变量: _var_map["__global__"][variable] = index
        """
        for i, dv in enumerate(self.schema.design_variables):
            key = dv.device if dv.device else "__global__"
            if key not in self._var_map:
                self._var_map[key] = {}
            self._var_map[key][dv.variable] = i

    def decode_x(self, x: np.ndarray) -> Dict[str, Dict[str, float]]:
        """
        将决策变量向量解码为 {device: {variable: val}} 字典。
        全局变量 (device="") 放在 "__global__" 键下。
        """
        result = {}
        for key, var_dict in self._var_map.items():
            result[key] = {}
            for var_name, idx in var_dict.items():
                result[key][var_name] = float(x[idx])
        return result

    def evaluate(self, x: np.ndarray) -> Tuple[np.ndarray, float, Dict[str, Any]]:
        """
        评估一个设计点的性能。

        Returns:
            objectives: 目标值向量（目前为总 loss 的标量，多目标备用）
            total_loss: 加权总损失
            meta: 详细评估数据（性能指标、晶体管参数等）
        """
        decoded = self.decode_x(x)

        # Step 1: pygmid 翻译 — (gm_id, L) → (W, VGS, gm, vdsat 等)
        transistor_params = self._translate_to_physics(decoded)

        # Step 2: 手工电路模型 — 估算顶层性能指标
        performance = self._estimate_performance(transistor_params, decoded.get("__global__", {}))

        # Step 3: 计算 Loss
        total_loss, loss_breakdown = self._compute_loss(performance, transistor_params)

        # 检查约束
        violation = self._check_constraints(x, decoded)

        # 如果有约束违反，给一个巨大惩罚
        if violation > 0:
            total_loss += 1e6 * violation

        meta = {
            "decoded": decoded,
            "transistor_params": transistor_params,
            "performance": performance,
            "loss_breakdown": loss_breakdown,
            "total_loss": total_loss,
            "constraint_violation": violation,
        }

        return np.array([total_loss]), violation, meta

    def _translate_to_physics(self, decoded: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
        """
        物理翻译层 — 将抽象 (gm_id, L) 翻译为物理参数 (W, VGS, gm...)。

        对称性处理（架构要求）：同 role 的器件组成对称组，
        组内只翻译第一个（主导管），其余从属管直接复制结果。
        优化器无需处理对称 — 翻译层自动消除冗余维度。
        """
        result = {}
        proc = self.schema.process

        # ── 构建对称组：同 role → 同组，组内首器件为主导管 ──
        sym_groups: Dict[str, List[str]] = {}
        for dev in self.schema.topology.devices:
            sym_groups.setdefault(dev.role, []).append(dev.id)
        # 确定每个器件属于哪个组、组内主导管是谁
        dominant_of: Dict[str, str] = {}
        for role, dev_ids in sym_groups.items():
            if len(dev_ids) >= 1:
                dom = dev_ids[0]
                for did in dev_ids:
                    dominant_of[did] = dom

        # ── 电源与电流估算 ──
        # Bias currents come from global design variables when present.
        vdd = self.schema.simulation.supply.get("vdd", 1.8)
        vss = self.schema.simulation.supply.get("vss", 0.0)
        globals_decoded = decoded.get("__global__", {})
        i_tail_est = globals_decoded.get("I_tail", None)
        if i_tail_est is None:
            power_max = 1e-3
            if "power" in self.schema.targets:
                power_max = self.schema.targets["power"].max or 1e-3
            i_tail_est = power_max / (vdd - vss) / 3
        i_stage2_est = globals_decoded.get("I_stage2", globals_decoded.get("I_out", i_tail_est))

        has_tail_mirror = any(dev.role == "tail_bias_mirror" for dev in self.schema.topology.devices)
        has_output_mirror = any(dev.role == "output_bias_mirror" for dev in self.schema.topology.devices)

        def _role_current(role: str) -> float:
            if role in ("tail_current_source", "tail_bias_mirror"):
                return i_tail_est
            if role == "input_pair":
                return i_tail_est / 2
            if "current_mirror" in role:
                return i_tail_est / 2
            if role in ("second_stage_gain", "second_stage_load", "output_current_source", "output_bias_mirror"):
                return i_stage2_est
            return i_tail_est / 2

        def _mirror_copy_factor(role: str, vds: float, phys: dict) -> float:
            if role == "tail_current_source" and not has_tail_mirror:
                return 1.0
            if role == "output_current_source" and not has_output_mirror:
                return 1.0
            if role not in ("tail_current_source", "output_current_source"):
                return 1.0
            vref = max(phys.get("vgs", 0.0), phys.get("vdsat", 0.0), 1e-3)
            ratio = max(vds, 1e-6) / vref
            return max(0.05, min(1.0, ratio ** 0.35))

        # ── 先翻译主导管，再复制给从属管 ──
        translated: Dict[str, dict] = {}

        for device_id in decoded:
            if device_id.startswith("__"):
                continue  # 跳过内部键 (如 __i_tail)
            dom = dominant_of.get(device_id, device_id)
            if dom != device_id:
                continue  # 从属管跳过，等主导管翻译完再复制

            gm_id = decoded[device_id].get("gm_id", 10)
            L = decoded[device_id].get("L", 1e-6)
            dev_def = self.schema.get_device_def(device_id)
            dev_type = dev_def.type if dev_def else "nmos"
            role = dev_def.role if dev_def else ""
            id_val = _role_current(role)

            phys = None
            if role == "second_stage_gain" and dev_type == "pmos":
                load_refs = [
                    p for p in translated.values()
                    if p.get("role") == "current_mirror_load" and p.get("type") == "pmos"
                ]
                if load_refs:
                    # M6 gate is driven by the first-stage output. Its VSG is
                    # therefore set mostly by the PMOS mirror load, not by an
                    # independent M6 bias. Size it at that imposed VSG.
                    phys = self.pygmid.forward_vgs(load_refs[0].get("vgs", 0.6), L, id_val, dev_type)
            if phys is None:
                phys = self.pygmid.forward(gm_id, L, id_val, dev_type)
            # numpy 标量 → Python 原生类型
            phys = {k: float(v) if hasattr(v, 'dtype') else v for k, v in phys.items()}
            # 网格对齐
            phys["W"] = _snap_to_grid(phys["W"], proc.W_precision or 1e-9)
            phys["W"] = max(phys["W"], proc.min_W)
            L = _snap_to_grid(L, proc.L_precision or 1e-9)
            width_factor = 1.0
            if role == "second_stage_gain" and dev_type == "pmos" and has_output_mirror:
                # The second-stage PMOS gate is imposed by the first-stage
                # diode load; IHP ngspice consistently needs a wider device
                # than the isolated lookup-table inversion predicts.
                width_factor = 4.0
                phys["W"] = min(
                    _snap_to_grid(phys["W"] * width_factor, proc.W_precision or 1e-9),
                    proc.max_W,
                )

            if role in ("tail_bias_mirror", "output_bias_mirror"):
                vds = max(phys.get("vgs", 0.45), 0.02)
            elif role == "tail_current_source":
                input_refs = [
                    p for p in translated.values()
                    if p.get("role") == "input_pair"
                ]
                if input_refs:
                    vcm = 0.5 * (vdd + vss)
                    vds = max(vcm - input_refs[0].get("vgs", 0.45), 0.02)
                else:
                    vds = 0.25
            elif role == "input_pair":
                vds = 0.45
            elif role in ("second_stage_gain", "second_stage_load", "output_current_source"):
                vds = max((vdd - vss) * 0.5, 0.2)
            else:
                vds = max(vdd - 0.5, 0.2)
            mirror_factor = _mirror_copy_factor(role, vds, phys)

            # Keep SPICE operating region and gm/ID inversion level separate.
            gm_id_eff = phys.get("gm_id", gm_id)
            region = compact_operating_region(vds, phys.get("vdsat", 0.0), id_val)
            inversion_region = inversion_region_from_gm_id(gm_id_eff)

            # 透传 pygmid 查表所得的全部物理参数 (含精确的 GDS_W → gds)
            # 不覆盖 gds — pygmid.forward() 已从 BSIM4 查表给出真实输出导纳
            translated[device_id] = {
                **phys, "L": L, "vds": vds,
                "id": id_val,
                "id_effective": id_val * mirror_factor,
                "mirror_copy_factor": mirror_factor,
                "model_width_factor": width_factor,
                "region": region,
                "inversion_region": inversion_region,
                "role": role, "type": dev_type,
            }

        # ── 复制主导管结果给同组其他器件 ──
        for device_id in decoded:
            if device_id.startswith("__"):
                continue  # 跳过内部键
            if device_id in translated:
                continue
            dom = dominant_of.get(device_id, device_id)
            if dom in translated:
                result[device_id] = dict(translated[dom])
                # 更新器件自身的上下文
                dev_def = self.schema.get_device_def(device_id)
                if dev_def:
                    result[device_id]["role"] = dev_def.role
                    result[device_id]["type"] = dev_def.type
            else:
                result[device_id] = {}

        # 把主导管的结果也写入
        for did, phys in translated.items():
            result[did] = phys

        return result

    def _estimate_performance(
        self,
        tp: Dict[str, Dict[str, float]],
        global_vars: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        if self._is_two_stage():
            return self._estimate_two_stage_performance(tp, global_vars or {})
        return self._estimate_five_transistor_performance(tp)

    def _is_two_stage(self) -> bool:
        arch = (self.schema.topology.architecture or "").lower()
        if "two" in arch or "2" in arch:
            return True
        return any(dev.role.startswith("second_stage") for dev in self.schema.topology.devices)

    def _global_value(self, name: str, default: float, global_vars: Dict[str, float]) -> float:
        if name in global_vars:
            return float(global_vars[name])
        for dv in self.schema.design_variables:
            if not dv.device and dv.variable == name:
                if dv.initial is not None:
                    return float(dv.initial)
                return 0.5 * (dv.range.min + dv.range.max)
        return default

    def _estimate_five_transistor_performance(self, tp: Dict[str, Dict[str, float]]) -> Dict[str, float]:
        """
        5T OTA 改进小信号模型 -- 三极点 + 双零点。

        极点:
          w_p1 = 1/(Rout x C_out)    输出节点 (主极点)
          w_p2 = 1/(R_fold x C_fold) 折叠节点 net1
          w_p3 = gm5 / C_tail        尾电流源节点

        零点:
          w_z1 = +gm1 / Cgd1         RHP (Miller 前馈, 减相位)
          w_z2 = +2*gm3 / (Cgs3+Cgs4) LHP (电流镜, 加相位)

        相位裕度:
          PM = 90 - Sum atan(GBW/w_pi) + Sum atan(GBW/w_zj)
        """
        # 识别器件角色
        input_devs = []
        load_devs = []
        tail_dev_id = None
        for dev in self.schema.topology.devices:
            if dev.role == "input_pair":
                input_devs.append(dev.id)
            elif "current_mirror" in dev.role:
                load_devs.append(dev.id)
            elif dev.role == "tail_current_source":
                tail_dev_id = dev.id

        if not input_devs or not load_devs:
            return {"dc_gain": 0, "unity_gain_bandwidth": 0, "phase_margin": 0, "power": 0}

        M1 = tp[input_devs[0]]
        M3 = tp[load_devs[0]]
        M5 = tp.get(tail_dev_id, {}) if tail_dev_id else {}

        gm_corr = self.schema.corrections.gm_factor if hasattr(self.schema, "corrections") else 1.0
        gds_corr = self.schema.corrections.gds_factor if hasattr(self.schema, "corrections") else 1.0

        gm_in   = M1.get("gm", 0) * gm_corr
        gds_in  = M1.get("gds", 1e-6) * gds_corr
        gds_ld  = M3.get("gds", 1e-6) * gds_corr
        gm_ld   = M3.get("gm", 0) * gm_corr
        gm_tail = M5.get("gm", 0) * gm_corr

        # -- 寄生电容 (来自 LookupTable) --
        c_corr = self.schema.corrections.c_factor if hasattr(self.schema, "corrections") else 1.0
        cgs_in  = M1.get("cgs", 1e-14) * c_corr
        cgd_in  = (M1.get("cgd", 0) or 1e-16) * c_corr
        cgs_ld  = M3.get("cgs", 1e-14) * c_corr
        cgd_ld  = (M3.get("cgd", 0) or 1e-16) * c_corr
        cgs_tail = M5.get("cgs", 1e-14)
        cgd_tail = M5.get("cgd", 0) or 1e-16

        # -- 漏极结电容估计 (Cdd ~ W_um x Cj_per_um) --
        W_in   = M1.get("W", 1e-6)
        W_ld   = M3.get("W", 1e-6)
        W_tail = M5.get("W", 1e-6)
        cj_per_um = 0.5e-15
        cdd_in   = W_in * 1e6 * cj_per_um * c_corr
        cdd_ld   = W_ld * 1e6 * cj_per_um * c_corr
        cdd_tail = W_tail * 1e6 * cj_per_um * c_corr

        # -- 输出阻抗 --
        ro_in   = 1.0 / max(gds_in, 1e-15)
        ro_load = 1.0 / max(gds_ld, 1e-15)
        rout = (ro_in * ro_load) / (ro_in + ro_load)

        # -- 直流增益 --
        dc_gain = gm_in * rout
        dc_gain_db = 20.0 * math.log10(max(dc_gain, 1e-15))

        # -- C_out (输出节点: vout = M2_drain + M4_drain + Cload) --
        cload = self.schema.simulation.cload or 1e-12
        c_out = cload + cgd_in + cgd_ld + cdd_in + cdd_ld
        wp1 = 1.0 / (rout * c_out) if rout * c_out > 0 else 0

        # -- GBW --
        gbw_rad = gm_in / c_out if c_out > 0 else 0
        gbw = gbw_rad / (2.0 * math.pi)

        # -- 折叠节点 (net1) --
        # R_fold = 1/(gm3 + gmb3 + gds1 + gds3) ~ 1/(gm3*1.25 + gds1)
        gmb_ld = gm_ld * 0.25
        r_fold = 1.0 / max(gm_ld + gmb_ld + gds_in + gds_ld, 1e-12)
        # C_fold = Cgs3 + Cgs4 + Cgd1 + Cdb1 + Cdb3
        c_fold = 2.0 * cgs_ld + cgd_in + cdd_in + cdd_ld
        wp2 = 1.0 / (r_fold * c_fold) if r_fold * c_fold > 0 else 1e12

        # -- 尾电流节点 (tail) --
        # R_tail ~ 1/gm5 (source degeneration)
        r_tail = 1.0 / max(gm_tail, 1e-12) if gm_tail > 0 else 1e6
        # C_tail = Cgs1 + Cgs2 + Cdb5 + Cgd5
        c_tail = 2.0 * cgs_in + cdd_tail + cgd_tail
        wp3 = 1.0 / (r_tail * c_tail) if r_tail * c_tail > 0 else 1e12

        # -- RHP 零点 (Miller 前馈) --
        wz1 = gm_in / max(cgd_in, 1e-18)

        # -- LHP 零点 (电流镜 pole-zero doublet) --
        # w_z2 ~ 2*gm3 / (Cgs3 + Cgs4)
        wz2 = 2.0 * gm_ld / max(2.0 * cgs_ld, 1e-18)

        # -- 相位裕度 (三极点 + 双零点) --
        if wp2 > 0 and gbw_rad > 0:
            pm_rad = math.pi / 2.0
            pm_rad -= math.atan(gbw_rad / wp2)       # w_p2
            if wp3 > 0 and wp3 < gbw_rad * 100:
                pm_rad -= math.atan(gbw_rad / wp3)   # w_p3 (仅当接近GBW时)
            pm_rad -= math.atan(gbw_rad / wz1)        # RHP zero
            pm_rad += math.atan(gbw_rad / wz2)        # LHP zero
            pm = max(0.0, pm_rad * 180.0 / math.pi)
        else:
            pm = 45.0

        # -- 功耗 --
        vdd = self.schema.simulation.supply.get("vdd", 1.2)
        vss = self.schema.simulation.supply.get("vss", 0.0)
        vdd_current = 0.0
        for dev in self.schema.topology.devices:
            p = tp.get(dev.id, {})
            if dev.type == "pmos" and dev.connections.get("source") == "vdd":
                vdd_current += p.get("id", 0)
        if vdd_current == 0 and tail_dev_id and tail_dev_id in tp:
            vdd_current = tp[tail_dev_id].get("id", 0)
        power = (vdd - vss) * vdd_current

        return {
            "dc_gain": dc_gain_db,
            "unity_gain_bandwidth": gbw,
            "phase_margin": pm,
            "power": power,
        }

    def _estimate_two_stage_performance(
        self,
        tp: Dict[str, Dict[str, float]],
        global_vars: Dict[str, float],
    ) -> Dict[str, float]:
        """Compact Miller-compensated two-stage OTA model.

        The model is intentionally conservative around PM because ngspice is the
        source of truth after sizing. It is still monotonic in the useful knobs:
        larger Cc lowers UGB and improves pole separation, and Rz near 1/gm6
        removes the RHP zero.
        """
        input_devs = [d.id for d in self.schema.topology.devices if d.role == "input_pair"]
        load_devs = [d.id for d in self.schema.topology.devices if "current_mirror" in d.role]
        gain_devs = [d.id for d in self.schema.topology.devices if d.role == "second_stage_gain"]
        out_loads = [
            d.id for d in self.schema.topology.devices
            if d.role in ("second_stage_load", "output_current_source")
        ]
        if not input_devs or not load_devs or not gain_devs:
            return {"dc_gain": 0, "unity_gain_bandwidth": 0, "phase_margin": 0, "power": 0}

        M1 = tp[input_devs[0]]
        M3 = tp[load_devs[0]]
        M6 = tp[gain_devs[0]]
        M7 = tp[out_loads[0]] if out_loads else {}
        tail_sources = [p for p in tp.values() if p.get("role") == "tail_current_source"]
        bias_refs = [p for p in tp.values() if p.get("role") in ("tail_bias_mirror", "output_bias_mirror")]
        tail_factor = min([p.get("mirror_copy_factor", 1.0) for p in tail_sources] or [1.0])
        stage2_mirror_factor = M7.get("mirror_copy_factor", 1.0)

        def _wl_ratio(dev: Dict[str, float]) -> float:
            return dev.get("W", 0.0) / max(dev.get("L", 0.0), 1e-30)

        # The second-stage PMOS gate is tied to the first-stage PMOS diode load.
        # Its available current is therefore mirror-like, not independently
        # biasable by I_stage2. Derate the ideal W/L copy ratio so the compact
        # model stays conservative against IHP ngspice operating points.
        first_stage_load_current = M3.get("id", 0.0) * tail_factor
        m6_copy_ratio = _wl_ratio(M6) / max(_wl_ratio(M3), 1e-30)
        stage2_current_capacity = first_stage_load_current * m6_copy_ratio * 0.30
        stage2_current_demand = max(
            M6.get("id", 0.0) * stage2_mirror_factor,
            M7.get("id_effective", M7.get("id", 0.0)),
            1e-15,
        )
        stage2_balance_factor = max(
            0.02,
            min(1.0, stage2_current_capacity / stage2_current_demand),
        )
        stage2_factor = stage2_mirror_factor * stage2_balance_factor
        stage2_current_effective = min(stage2_current_demand, stage2_current_capacity)
        collapse_deficit = max(0.0, 0.85 - stage2_balance_factor) / 0.85

        corr = self.schema.corrections
        gm_corr = corr.gm_factor
        gds_corr = corr.gds_factor
        c_corr = corr.c_factor

        gm1 = M1.get("gm", 0) * gm_corr * tail_factor
        gds1 = max(M1.get("gds", 0) * gds_corr * max(tail_factor, 0.35), 1e-15)
        gds3 = max(M3.get("gds", 0) * gds_corr * max(tail_factor, 0.35), 1e-15)
        gm6 = M6.get("gm", 0) * gm_corr * stage2_factor
        gds6 = max(M6.get("gds", 0) * gds_corr * max(stage2_factor, 0.35), 1e-15)
        gds7 = max(
            M7.get("gds", 0)
            * gds_corr
            * max(stage2_factor, 0.35)
            * (1.0 + 30.0 * collapse_deficit ** 2),
            1e-15,
        )

        r1 = 1.0 / max(gds1 + gds3, 1e-15)
        r2 = 1.0 / max(gds6 + gds7, 1e-15)
        a1 = gm1 * r1
        a2 = gm6 * r2
        dc_gain_db = 20.0 * math.log10(max(abs(a1 * a2), 1e-15))

        cc = self._global_value("Cc", 5e-13, global_vars)
        rz = self._global_value("Rz", 0.0, global_vars)
        cload = self.schema.simulation.cload or 2e-13

        load_gate_caps = sum(
            tp.get(did, {}).get("cgs", 0.0) + tp.get(did, {}).get("cgd", 0.0)
            for did in load_devs
        )
        c1_par = (
            M1.get("cgd", 0) + M6.get("cgs", 0) + load_gate_caps
            + (M1.get("W", 0) + M3.get("W", 0)) * 1e6 * 0.5e-15
        ) * c_corr
        c2_par = (
            M6.get("cgd", 0) + M7.get("cgd", 0)
            + (M6.get("W", 0) + M7.get("W", 0)) * 1e6 * 0.5e-15
        ) * c_corr

        wu = gm1 / max(cc, 1e-18)
        ugbw = wu / (2.0 * math.pi)

        # Dominant pole is split by Miller multiplication; non-dominant pole is
        # output-stage gm over the load capacitance seen at vout.
        p2 = gm6 / max(cload + c2_par + cc * 0.25, 1e-18)
        p3 = 1.0 / max(r1 * (c1_par + cc / max(abs(a2), 1.0)), 1e-30)

        inv_gm6 = 1.0 / max(gm6, 1e-12)
        zero_den = cc * max(abs(inv_gm6 - rz), 1e-3)
        wz = 1.0 / max(zero_den, 1e-18)
        zero_is_lhp = rz > inv_gm6

        pm_rad = math.pi / 2.0
        pm_rad -= math.atan(wu / max(p2, 1e-12))
        if p3 < wu * 20.0:
            pm_rad -= math.atan(wu / max(p3, 1e-12))
        if zero_is_lhp:
            pm_rad += math.atan(wu / max(wz, 1e-12))
        else:
            pm_rad -= math.atan(wu / max(wz, 1e-12))
        pm = max(0.0, min(120.0, pm_rad * 180.0 / math.pi - 15.0))

        vdd = self.schema.simulation.supply.get("vdd", 1.2)
        vss = self.schema.simulation.supply.get("vss", 0.0)
        i_tail = max([p.get("id_effective", p.get("id", 0.0)) for p in tail_sources] or [0.0])
        i_bias_ref = sum(p.get("id", 0.0) for p in bias_refs)
        i_stage2 = stage2_current_effective
        power = (vdd - vss) * (i_tail + i_stage2 + i_bias_ref)

        return {
            "dc_gain": dc_gain_db,
            "unity_gain_bandwidth": ugbw,
            "phase_margin": pm,
            "power": power,
            "Cc": cc,
            "Rz": rz,
            "zero_target_rz": inv_gm6,
            "tail_mirror_factor": tail_factor,
            "stage2_mirror_factor_raw": stage2_mirror_factor,
            "stage2_mirror_factor": stage2_factor,
            "stage2_balance_factor": stage2_balance_factor,
            "stage2_current_capacity": stage2_current_capacity,
            "stage2_current_demand": stage2_current_demand,
            "stage2_current_effective": stage2_current_effective,
            "load_gate_cap": load_gate_caps,
        }
    def _compute_loss(self, perf: Dict[str, float],
                       tp: Dict[str, Dict[str, float]]) -> Tuple[float, Dict[str, float]]:
        """
        根据 Schema 的 loss_terms + 自动软约束惩罚 计算加权总损失。

        软约束（架构要求）：对派生物理量采用惩罚函数，
        而非硬性报错。包括 W/L 范围、工作区、电压。
        """
        total = 0.0
        breakdown = {}

        # ── 用户定义的 loss 项 ──
        for lt in self.schema.loss_terms:
            value = _safe_eval_loss_formula(lt.formula, perf, self.schema.targets, tp)
            weighted = value * lt.weight
            total += weighted
            breakdown[lt.id] = weighted

        # ── 自动软约束惩罚 ──
        proc = self.schema.process
        PENALTY_BIG = 1e6
        PENALTY_WARN = 1e3
        roles_present = {p.get("role", "") for p in tp.values()}

        def _is_mirrored_copy(role: str) -> bool:
            return (
                (role == "tail_current_source" and "tail_bias_mirror" in roles_present)
                or (role == "output_current_source" and "output_bias_mirror" in roles_present)
            )

        for did, p in tp.items():
            W = p.get("W", 0)
            L_val = p.get("L", 0)
            vds = p.get("vds", 0)
            vgs = p.get("vgs", 0)
            role = p.get("role", "")
            dev_type = p.get("type", "nmos")

            # W/L 硬范围（工艺极限）
            if W > 0 and W < proc.min_W * 0.99:
                total += PENALTY_BIG * (proc.min_W - W)
                breakdown[f"hard:W_min_{did}"] = PENALTY_BIG * (proc.min_W - W)
            if W > proc.max_W:
                total += PENALTY_WARN * (W - proc.max_W)
                breakdown[f"soft:W_max_{did}"] = PENALTY_WARN * (W - proc.max_W)
            if L_val > 0 and L_val < proc.min_L * 0.99:
                total += PENALTY_BIG * (proc.min_L - L_val)
                breakdown[f"hard:L_min_{did}"] = PENALTY_BIG * (proc.min_L - L_val)
            if L_val > proc.max_L:
                total += PENALTY_WARN * (L_val - proc.max_L)
                breakdown[f"soft:L_max_{did}"] = PENALTY_WARN * (L_val - proc.max_L)

            # 工作区软约束（diode-connected 豁免）
            if role not in ("current_mirror_load", "tail_bias_mirror", "output_bias_mirror") and vds > 0 and "vdsat" in p:
                vdsat = p.get("vdsat", 0)
                if vdsat > 0 and vds < vdsat * proc.VDSAT_headroom_factor:
                    shortage = vdsat * proc.VDSAT_headroom_factor - vds
                    penalty_scale = PENALTY_WARN * (0.1 if _is_mirrored_copy(role) else 1.0)
                    total += penalty_scale * shortage
                    breakdown[f"soft:saturation_{did}"] = penalty_scale * shortage

            # VGS 安全范围
            VTH = proc.VTH_n if dev_type == "nmos" else proc.VTH_p
            if vgs > 0 and vgs - VTH < 0.05:
                total += PENALTY_WARN * (0.05 - (vgs - VTH))
                breakdown[f"soft:deep_subth_{did}"] = PENALTY_WARN * (0.05 - (vgs - VTH))

        mirror_pairs = [
            ("tail_current_source", "tail_bias_mirror", "tail_bias_mirror_ratio"),
            ("output_current_source", "output_bias_mirror", "output_bias_mirror_ratio"),
        ]
        for copy_role, ref_role, label in mirror_pairs:
            copy_dev = next((p for p in tp.values() if p.get("role") == copy_role), None)
            ref_dev = next((p for p in tp.values() if p.get("role") == ref_role), None)
            if not copy_dev or not ref_dev:
                continue
            copy_ratio = copy_dev.get("W", 0.0) / max(copy_dev.get("L", 0.0), 1e-30)
            ref_ratio = ref_dev.get("W", 0.0) / max(ref_dev.get("L", 0.0), 1e-30)
            if copy_ratio > 0 and ref_ratio > 0:
                mismatch = abs(math.log(copy_ratio / ref_ratio))
                if mismatch > 1e-3:
                    total += PENALTY_WARN * mismatch
                    breakdown[f"soft:{label}"] = PENALTY_WARN * mismatch

        return total, breakdown

    def _check_constraints(self, x: np.ndarray,
                            decoded: Dict[str, Dict[str, float]]) -> float:
        """检查边界约束，返回违约总量 — 支持器件变量与全局变量。"""
        violation = 0.0
        for key, vars_dict in decoded.items():
            for var_name, val in vars_dict.items():
                # 全局变量: device="" → key="__global__"
                dv = self._get_design_var(key, var_name)
                if dv is None:
                    continue
                if val < dv.range.min:
                    violation += (dv.range.min - val)
                elif val > dv.range.max:
                    violation += (val - dv.range.max)
        return violation

    def _get_design_var(self, device_key: str, variable: str) -> Optional[DesignVariable]:
        """查找匹配的 DesignVariable — device_key 为器件ID或 \"__global__\"。"""
        for dv in self.schema.design_variables:
            dv_key = dv.device if dv.device else "__global__"
            if dv_key == device_key and dv.variable == variable:
                return dv
        return None

    @property
    def n_vars(self) -> int:
        return len(self.schema.design_variables)

    @property
    def bounds(self) -> List[Tuple[float, float]]:
        if not self.schema.design_variables:
            return []
        return [(dv.range.min, dv.range.max) for dv in self.schema.design_variables]


# ── Loss 公式安全求值器 ──────────────────────────────────────

def _snap_to_grid(value: float, precision: float) -> float:
    """将 value 对齐到 precision 的整数倍（四舍五入），消除浮点误差。"""
    if precision <= 0:
        return value
    n = int(round(value / precision))
    result = n * precision
    n_dec = max(0, int(-math.floor(math.log10(precision)))) if precision > 0 else 0
    return round(result, n_dec)


def _safe_eval_loss_formula(formula: str, perf: Dict[str, float],
                              targets: Dict[str, Any],
                              tp: Dict[str, Dict[str, float]]) -> float:
    """
    安全地求值 loss 公式字符串。

    支持的函数: relu, abs, max, min, sqrt, pow, log10, penalty_if
    支持的变量: realized.*, targets.*, device.Mx.*
    """
    # 构建安全的局部命名空间
    namespace = {
        "relu": lambda x: max(0.0, x),
        "abs": abs,
        "max": max,
        "min": min,
        "sqrt": math.sqrt,
        "pow": pow,
        "log10": math.log10,
        "log": math.log,
        "exp": math.exp,
        "penalty_if": lambda cond, penalty, base: penalty if cond else base,
        "realized": type("Realized", (), perf)(),
        "targets": _targets_namespace(targets),
        "math": math,
    }

    # 添加设备级变量
    for dev_id, params in tp.items():
        namespace[dev_id] = type("Dev", (), params)()

    try:
        result = eval(formula, {"__builtins__": {}}, namespace)
        return float(result)
    except Exception:
        return 1e6  # 公式求值失败，返回大惩罚


def _targets_namespace(targets: Dict[str, Any]) -> Any:
    """将 targets dict 转为可点访问的命名空间。"""
    attrs = {}
    for k, t in targets.items():
        attrs[k] = type("TargetObj", (), {
            "min": t.min if hasattr(t, "min") else t.get("min"),
            "max": t.max if hasattr(t, "max") else t.get("max"),
            "unit": t.unit if hasattr(t, "unit") else t.get("unit", ""),
            "priority": t.priority if hasattr(t, "priority") else t.get("priority", 1),
        })()
    return type("Targets", (), attrs)()


# ── NSGA-II 核心算法 ─────────────────────────────────────────

class NSGA2Optimizer:
    """
    NSGA-II 优化器。

    在 (gm_id, L) 的抽象空间中搜索满足规格的最优设计点。
    与 Schema 交互只通过 DesignVariable 的 range 和 loss_terms。

    使用方式:
        opt = NSGA2Optimizer(schema, evaluator, config)
        best_x, best_meta = opt.optimize()
    """

    def __init__(self, schema: DesignState, evaluator: CircuitEvaluator,
                 config: Optional[NSGA2Config] = None):
        self.schema = schema
        self.evaluator = evaluator
        self.config = config or NSGA2Config()
        self.rng = np.random.RandomState(self.config.seed)
        self.history = []  # 记录每代最优
        self.last_population: List[Individual] = []
        self.last_fronts: List[List[int]] = []

    def optimize(self) -> Tuple[np.ndarray, Dict[str, Any]]:
        """
        运行 NSGA-II 优化。

        Returns:
            best_x: 最优决策变量向量
            best_meta: 对应的评估元数据
        """
        config = self.config
        n_vars = self.evaluator.n_vars
        bounds = np.array(self.evaluator.bounds)

        if config.verbose:
            print(f"[NSGA-II] Starting: {n_vars} variables, pop_size={config.pop_size}, "
                  f"generations={config.n_generations}")
            t0 = time.time()

        # Step 1: 初始化种群
        population = self._initialize_population(n_vars, bounds)
        self._apply_symmetry_constraints(population)       # 对称锁

        # Step 2: 评估初始种群
        self._evaluate_population(population)

        # Step 3: 非支配排序
        fronts = self._non_dominated_sort(population)
        self._assign_ranks(population, fronts)
        self._compute_crowding_distance(population, fronts)

        # 记录全局最优。二级 OTA 的补偿变量会让局部搜索很抖，
        # 所以收敛判断必须只响应真正变好的候选点。
        best_idx = self._get_best_index(population)
        best_x = population[best_idx].x.copy()
        best_meta = dict(population[best_idx].meta)
        best_loss = float(population[best_idx].objectives[0])

        # 主循环
        no_improve = 0
        for gen in range(config.n_generations):
            # Step 4: 选择、交叉、变异 → 后代
            offspring = self._create_offspring(population, bounds)
            self._apply_symmetry_constraints(offspring)    # 对称锁

            # Step 5: 评估后代
            self._evaluate_population(offspring)

            # Step 6: 合并、排序、选择下一代
            combined = population + offspring
            fronts = self._non_dominated_sort(combined)
            self._assign_ranks(combined, fronts)
            self._compute_crowding_distance(combined, fronts)

            population = self._select_next_generation(combined, config.pop_size)

            # 记录
            current_best_idx = self._get_best_index(population)
            current_loss = float(population[current_best_idx].objectives[0])

            if current_loss < best_loss - config.tol:
                no_improve = 0
                best_loss = current_loss
                best_x = population[current_best_idx].x.copy()
                best_meta = dict(population[current_best_idx].meta)
            else:
                no_improve += 1

            self.history.append(best_loss)

            if config.verbose and gen % 10 == 0:
                print(f"  Gen {gen:3d}: best_loss={best_loss:.6e}, no_improve={no_improve}")

            if no_improve >= config.patience:
                if config.verbose:
                    print(f"  Converged at generation {gen}")
                break

        if config.verbose:
            print(f"[NSGA-II] Completed in {time.time() - t0:.1f}s, best_loss={best_loss:.6e}")

        self.last_population = population
        self.last_fronts = self._non_dominated_sort(population)
        self._assign_ranks(self.last_population, self.last_fronts)
        self._compute_crowding_distance(self.last_population, self.last_fronts)

        return best_x, best_meta

    def _initialize_population(self, n_vars: int, bounds: np.ndarray) -> List[Individual]:
        """
        初始化种群。

        第一个个体使用 Schema 中的 initial_strategy 值（gm_id_strategy, L_strategy），
        其余个体均匀随机采样。确保 Agent 设定的初始猜测被优化器实际使用。
        """
        pop = []

        # ── 个体 0: 从 DesignVariable.initial 取值 ──
        x_seed = np.zeros(n_vars)
        for j, dv in enumerate(self.schema.design_variables):
            if dv.initial is not None:
                x_seed[j] = dv.initial
            elif dv.device and dv.device in self.schema.transistors:
                # legacy: 从 transistor state 读取
                ts = self.schema.transistors[dv.device]
                if dv.variable == "gm_id":
                    x_seed[j] = ts.gm_id_strategy
                elif dv.variable == "L":
                    x_seed[j] = ts.L_strategy
                else:
                    x_seed[j] = (bounds[j, 0] + bounds[j, 1]) / 2.0
            else:
                x_seed[j] = (bounds[j, 0] + bounds[j, 1]) / 2.0
        x_seed = np.clip(x_seed, bounds[:, 0], bounds[:, 1])
        pop.append(Individual(x=x_seed))

        # ── 个体 1..pop_size-1: 均匀随机 ──
        for i in range(1, self.config.pop_size):
            x = np.zeros(n_vars)
            for j in range(n_vars):
                x[j] = self._sample_initial_value(j, bounds[j, 0], bounds[j, 1])
            pop.append(Individual(x=x))
        return pop

    def _sample_initial_value(self, index: int, low: float, high: float) -> float:
        """Sample wide positive physical variables in log space."""
        if low <= 0 or high <= low:
            return self.rng.uniform(low, high)
        dv = self.schema.design_variables[index]
        wide = high / low >= 50.0
        log_vars = {"I_tail", "I_stage2", "I_out", "Cc", "Rz"}
        if wide or dv.variable in log_vars:
            return float(math.exp(self.rng.uniform(math.log(low), math.log(high))))
        return self.rng.uniform(low, high)

    def _apply_symmetry_constraints(self, population: List[Individual]) -> None:
        """
        对称锁定：将同一 (symmetry_label, variable) 的变量的值设为相等。
        只匹配同类变量（gm_id 对 gm_id，L 对 L），不跨变量类型。
        """
        # 收集对称组：按 (symmetry_label, variable) 分组
        sym_groups: Dict[Tuple[str, str], List[int]] = {}
        for i, dv in enumerate(self.schema.design_variables):
            if dv.symmetry_label:
                key = (dv.symmetry_label, dv.variable)
                sym_groups.setdefault(key, []).append(i)

        for (label, var_name), indices in sym_groups.items():
            if len(indices) <= 1:
                continue
            for ind in population:
                # 取该组第一个变量的值，应用到同组其他变量
                val = ind.x[indices[0]]
                for idx in indices[1:]:
                    ind.x[idx] = val

    def _evaluate_population(self, population: List[Individual]) -> None:
        """评估种群中所有个体。"""
        for ind in population:
            objectives, violation, meta = self.evaluator.evaluate(ind.x)
            ind.objectives = objectives
            ind.constraints_violation = violation
            ind.feasible = (violation == 0)
            ind.meta = meta

    def _non_dominated_sort(self, population: List[Individual]) -> List[List[int]]:
        """
        非支配排序（Deb 2002 快速算法）。
        返回 fronts，每个 front 是索引列表。
        """
        N = len(population)
        dominated_count = np.zeros(N, dtype=int)  # n_p: 支配 p 的个体数
        dominates_list = [[] for _ in range(N)]   # S_p: p 支配的个体

        for p in range(N):
            for q in range(N):
                if p == q:
                    continue
                if self._dominates(population[p], population[q]):
                    dominates_list[p].append(q)
                elif self._dominates(population[q], population[p]):
                    dominated_count[p] += 1

        fronts = []
        current_front = [i for i in range(N) if dominated_count[i] == 0]

        while current_front:
            fronts.append(current_front)
            next_front = []
            for p in current_front:
                for q in dominates_list[p]:
                    dominated_count[q] -= 1
                    if dominated_count[q] == 0:
                        next_front.append(q)
            current_front = next_front

        return fronts

    def _dominates(self, a: Individual, b: Individual) -> bool:
        """
        支配关系：a dominates b 当且仅当：
        - 约束处理：feasible > infeasible
        - 都 feasible 时：a 在所有目标上 ≤ b，且至少一个严格 <
        """
        if a.feasible and not b.feasible:
            return True
        if not a.feasible and b.feasible:
            return False
        if not a.feasible and not b.feasible:
            return a.constraints_violation < b.constraints_violation

        # 都 feasible
        return np.all(a.objectives <= b.objectives) and np.any(a.objectives < b.objectives)

    def _assign_ranks(self, population: List[Individual], fronts: List[List[int]]) -> None:
        for rank, front in enumerate(fronts):
            for idx in front:
                population[idx].rank = rank

    def _compute_crowding_distance(self, population: List[Individual],
                                     fronts: List[List[int]]) -> None:
        """计算拥挤距离（Deb 2002）。"""
        for ind in population:
            ind.crowding_distance = 0.0

        for front in fronts:
            if len(front) <= 2:
                for idx in front:
                    population[idx].crowding_distance = float("inf")
                continue

            n_obj = len(population[front[0]].objectives)
            for m in range(n_obj):
                sorted_front = sorted(front, key=lambda i: population[i].objectives[m])
                f_min = population[sorted_front[0]].objectives[m]
                f_max = population[sorted_front[-1]].objectives[m]
                if f_max - f_min < 1e-12:
                    continue
                population[sorted_front[0]].crowding_distance = float("inf")
                population[sorted_front[-1]].crowding_distance = float("inf")
                for k in range(1, len(sorted_front) - 1):
                    population[sorted_front[k]].crowding_distance += (
                        (population[sorted_front[k + 1]].objectives[m] -
                         population[sorted_front[k - 1]].objectives[m]) / (f_max - f_min)
                    )

    def _select_next_generation(self, combined: List[Individual],
                                  pop_size: int) -> List[Individual]:
        """从合并种群中选择下一代。"""
        next_pop = []
        current_rank = 0
        rank_indices = {}

        for i, ind in enumerate(combined):
            rank_indices.setdefault(ind.rank, []).append(i)

        sorted_ranks = sorted(rank_indices.keys())

        for rank in sorted_ranks:
            indices = rank_indices[rank]
            if len(next_pop) + len(indices) <= pop_size:
                for idx in indices:
                    next_pop.append(combined[idx])
            else:
                # 需要从当前 front 按拥挤距离选择
                sorted_by_cd = sorted(indices,
                                       key=lambda i: combined[i].crowding_distance,
                                       reverse=True)
                remaining = pop_size - len(next_pop)
                for idx in sorted_by_cd[:remaining]:
                    next_pop.append(combined[idx])
                break

        return next_pop

    def _create_offspring(self, population: List[Individual],
                           bounds: np.ndarray) -> List[Individual]:
        """通过锦标赛选择、SBX 交叉、多项式变异生成后代。"""
        pop_size = len(population)
        offspring = []

        for _ in range(pop_size):
            # 锦标赛选择
            p1 = self._tournament_select(population)
            p2 = self._tournament_select(population)

            # SBX 交叉
            if self.rng.random() < self.config.crossover_prob:
                c1_x, c2_x = self._sbx_crossover(p1.x, p2.x, bounds)
            else:
                c1_x, c2_x = p1.x.copy(), p2.x.copy()

            # 多项式变异
            c1_x = self._polynomial_mutation(c1_x, bounds)
            c2_x = self._polynomial_mutation(c2_x, bounds)

            # 裁剪到边界
            c1_x = np.clip(c1_x, bounds[:, 0], bounds[:, 1])
            c2_x = np.clip(c2_x, bounds[:, 0], bounds[:, 1])

            offspring.append(Individual(x=c1_x))
            offspring.append(Individual(x=c2_x))

        # 截断到 pop_size
        offspring = offspring[:pop_size]

        return offspring

    def _tournament_select(self, population: List[Individual]) -> Individual:
        """锦标赛选择（基于 rank + 拥挤距离）。"""
        k = 2  # tournament size
        candidates = self.rng.choice(len(population), k, replace=False)
        best = candidates[0]
        for c in candidates[1:]:
            if population[c].rank < population[best].rank:
                best = c
            elif population[c].rank == population[best].rank:
                if population[c].crowding_distance > population[best].crowding_distance:
                    best = c
        return population[best]

    def _sbx_crossover(self, p1: np.ndarray, p2: np.ndarray,
                         bounds: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """模拟二进制交叉 (Simulated Binary Crossover)。"""
        eta = self.config.crossover_eta
        c1, c2 = p1.copy(), p2.copy()
        n = len(p1)
        for i in range(n):
            bound_range = bounds[i, 1] - bounds[i, 0]
            if bound_range < 1e-20:
                continue  # 固定维度（min==max），跳过交叉
            if self.rng.random() < 0.5:
                if abs(p2[i] - p1[i]) > 1e-14:
                    if p1[i] < p2[i]:
                        y1, y2 = p1[i], p2[i]
                    else:
                        y1, y2 = p2[i], p1[i]

                    # 计算 beta_q
                    rand = self.rng.random()
                    beta = 1.0 + 2.0 * (y1 - bounds[i, 0]) / max(y2 - y1, 1e-14)
                    alpha = 2.0 - beta ** -(eta + 1.0)
                    if rand <= 1.0 / alpha:
                        beta_q = (rand * alpha) ** (1.0 / (eta + 1.0))
                    else:
                        beta_q = (1.0 / (2.0 - rand * alpha)) ** (1.0 / (eta + 1.0))

                    c1[i] = 0.5 * ((y1 + y2) - beta_q * (y2 - y1))

                    beta = 1.0 + 2.0 * (bounds[i, 1] - y2) / max(y2 - y1, 1e-14)
                    alpha = 2.0 - beta ** -(eta + 1.0)
                    if rand <= 1.0 / alpha:
                        beta_q = (rand * alpha) ** (1.0 / (eta + 1.0))
                    else:
                        beta_q = (1.0 / (2.0 - rand * alpha)) ** (1.0 / (eta + 1.0))

                    c2[i] = 0.5 * ((y1 + y2) + beta_q * (y2 - y1))

        return np.clip(c1, bounds[:, 0], bounds[:, 1]), np.clip(c2, bounds[:, 0], bounds[:, 1])

    def _polynomial_mutation(self, x: np.ndarray, bounds: np.ndarray) -> np.ndarray:
        """多项式变异。"""
        eta = self.config.mutation_eta
        x_mut = x.copy()
        for i in range(len(x)):
            bound_range = bounds[i, 1] - bounds[i, 0]
            if bound_range < 1e-20:
                continue  # 固定维度（min==max），无法变异
            if self.rng.random() < self.config.mutation_prob:
                delta = (x[i] - bounds[i, 0]) / bound_range
                rand = self.rng.random()
                if rand < 0.5:
                    delta_q = (2.0 * rand) ** (1.0 / (eta + 1.0)) - 1.0
                else:
                    delta_q = 1.0 - (2.0 * (1.0 - rand)) ** (1.0 / (eta + 1.0))
                x_mut[i] = x[i] + delta_q * bound_range
        return x_mut

    def _get_best_index(self, population: List[Individual]) -> int:
        """获取最优个体索引（优先 rank 最低，同 rank 选 objective 最小）。"""
        best = 0
        for i in range(1, len(population)):
            if population[i].rank < population[best].rank:
                best = i
            elif population[i].rank == population[best].rank:
                if np.sum(population[i].objectives) < np.sum(population[best].objectives):
                    best = i
        return best


# ── W/L Rounding 函数 ──────────────────────────────────────────

def round_transistor_params(
    tp: Dict[str, Dict[str, float]],
    proc=None,
) -> Dict[str, Dict[str, float]]:
    """
    将优化器输出的晶体管物理参数 round 到工艺网格。

    PTM 130nm: W→10nm grid, L→1nm grid
    整数法消除浮点累积误差: n = round(W/grid), W = round(n*grid, grid_decimals)
    """
    W_grid = getattr(proc, "W_precision", 10e-9)
    L_grid = getattr(proc, "L_precision", 1e-9)
    W_min  = getattr(proc, "min_W", 150e-9)
    L_min  = getattr(proc, "min_L", 130e-9)
    W_max  = getattr(proc, "max_W", 200e-6)
    W_dec = max(0, int(-math.floor(math.log10(W_grid)))) if W_grid > 0 else 0
    L_dec = max(0, int(-math.floor(math.log10(L_grid)))) if L_grid > 0 else 0

    rounded = {}
    for dev_id, params in tp.items():
        rp = dict(params)

        W = params.get("W", 0)
        L = params.get("L", 0)

        if W > 0:
            W = max(W, W_min)
            W = min(W, W_max)
            n = int(round(W / W_grid))
            rp["W"] = round(n * W_grid, W_dec)

        if L > 0:
            L = max(L, L_min)
            n = int(round(L / L_grid))
            rp["L"] = round(n * L_grid, L_dec)

        rp["W_rounded"] = True
        rounded[dev_id] = rp

    return rounded


def round_and_update_state(
    state,
    decoded: Dict[str, Dict[str, float]],
    tp: Dict[str, Dict[str, float]],
) -> None:
    """
    Round W/L 并写回 DesignState.transistors。

    修改 state 的 transistors[dev_id].parameters.W 和 .L。
    """
    proc = state.process
    rounded = round_transistor_params(tp, proc)

    for dev_id, rp in rounded.items():
        if dev_id in state.transistors:
            ts = state.transistors[dev_id]
            if rp.get("W", 0) > 0:
                ts.parameters.W = rp["W"]
            if rp.get("L", 0) > 0:
                ts.parameters.L = rp["L"]
                ts.L_strategy = rp["L"]
            # 回填全部物理参数（非零值）
            if rp.get("id", 0) > 0:
                ts.parameters.id = rp["id"]
            if rp.get("gm", 0) > 0:
                ts.parameters.gm = rp["gm"]
            if rp.get("gds", 0) > 0:
                ts.parameters.gds = rp["gds"]
            ts.parameters.vgs = rp.get("vgs", ts.parameters.vgs)
            ts.parameters.vds = rp.get("vds", ts.parameters.vds)
            ts.parameters.vdsat = rp.get("vdsat", ts.parameters.vdsat)
            ts.parameters.region = normalize_spice_region(
                rp.get("region", ts.parameters.region),
                vds=ts.parameters.vds,
                vdsat=ts.parameters.vdsat,
                current=ts.parameters.id,
            )

    return rounded
