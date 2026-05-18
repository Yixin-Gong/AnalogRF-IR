"""
pygmid — Boris Murmann gm/ID LookupTable 适配层。

导出：
    LookupTable      — 查找表类（npz 加载 + 双线性插值）
    PygmidAdapter    — 适配器（forward/backward/lookup 接口）
    create_pygmid_adapter — 工厂函数
    load_lookup_table      — 便捷加载
    create_lookup_pair     — NMOS/PMOS 配对加载
"""

from pygmid.lookup import LookupTable, load_lookup_table, create_lookup_pair
from pygmid.adapter import PygmidAdapter, create_pygmid_adapter

__all__ = [
    "LookupTable",
    "PygmidAdapter",
    "create_pygmid_adapter",
    "load_lookup_table",
    "create_lookup_pair",
]
