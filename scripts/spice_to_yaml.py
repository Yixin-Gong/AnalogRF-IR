#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from frontends.spice_parser import parse_spice_file, write_yaml


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Convert a SPICE netlist into AnalogRF-IR YAML")
    parser.add_argument("spice", help="Input .cir/.sp/.spice file")
    parser.add_argument("--out", default="", help="Output YAML path")
    parser.add_argument("--name", default="", help="Override design_name")
    args = parser.parse_args(argv)

    spice = Path(args.spice)
    out = Path(args.out) if args.out else spice.with_suffix(".yaml")
    data = parse_spice_file(spice, design_name=args.name or None)
    write_yaml(data, out)
    print(f"Wrote YAML: {out}")
    print(f"Devices: {len(data.get('topology', {}).get('devices', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
