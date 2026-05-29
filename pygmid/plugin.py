from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.environment import existing_project_path, resolve_project_path
from pygmid.adapter import PygmidAdapter, create_pygmid_adapter


@dataclass
class GmIdPluginConfig:
    tables_dir: str = "tables"
    nmos_table: str | None = None
    pmos_table: str | None = None
    auto_generate_tables: bool = False
    output_prefix: str = "ptm130"


class GmIdPlugin:
    """Lookup-table gm/ID service used by optimizers.

    The flow depends on this interface, not on the concrete Murmann lookup
    implementation. Table generation is opt-in because it can be slow and
    requires ngspice/PDK availability.
    """

    def __init__(self, config: GmIdPluginConfig):
        self.config = config
        self.adapter: PygmidAdapter | None = None

    @classmethod
    def from_environment(cls, env: dict[str, Any]) -> "GmIdPlugin":
        tools = env.get("tools", {}) or {}
        process = env.get("process", {}) or {}
        config = GmIdPluginConfig(
            tables_dir=str(tools.get("pygmid_tables_dir", "tables")),
            nmos_table=tools.get("nmos_table"),
            pmos_table=tools.get("pmos_table"),
            auto_generate_tables=bool(tools.get("auto_generate_tables", False)),
            output_prefix=str(tools.get("table_output_prefix") or _default_prefix(process)),
        )
        return cls(config)

    def load(self, env: dict[str, Any] | None = None) -> PygmidAdapter:
        if self.adapter is not None:
            return self.adapter
        if env and self.config.auto_generate_tables:
            self.ensure_tables(env)
        explicit_tables = bool(self.config.nmos_table or self.config.pmos_table)
        self.adapter = create_pygmid_adapter(
            nmos_path=existing_project_path(self.config.nmos_table),
            pmos_path=existing_project_path(self.config.pmos_table),
            tables_dir=None if explicit_tables else str(resolve_project_path(self.config.tables_dir)),
        )
        return self.adapter

    def ensure_tables(self, env: dict[str, Any]) -> None:
        nmos_path = existing_project_path(self.config.nmos_table)
        pmos_path = existing_project_path(self.config.pmos_table)
        if nmos_path and pmos_path:
            return
        process = env.get("process", {}) or {}
        output_dir = resolve_project_path(self.config.tables_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        from pygmid.generate_tables import generate_table

        common = {
            "output_dir": str(output_dir),
            "model_lib": process.get("model_lib", "ptm_130.lib"),
            "model_corner": process.get("model_corner", ""),
            "nmos_model": process.get("nmos_model", "nmos"),
            "pmos_model": process.get("pmos_model", "pmos"),
            "device_style": process.get("device_style", "mos"),
            "osdi_libs": process.get("osdi_libs", []) or [],
            "output_prefix": self.config.output_prefix,
            "verbose": True,
        }
        if not nmos_path:
            generate_table("nmos", **common)
        if not pmos_path:
            generate_table("pmos", **common)

    def summary(self) -> str:
        adapter = self.load()
        return adapter.summary()


def _default_prefix(process: dict[str, Any]) -> str:
    name = str(process.get("process_name") or "ptm130").lower()
    return "".join(ch if ch.isalnum() else "_" for ch in name).strip("_") or "ptm130"
