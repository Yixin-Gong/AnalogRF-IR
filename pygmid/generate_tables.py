#!/usr/bin/env python3
"""pygmid/generate_tables.py V2 — ngspice techsweep with full BSIM4 parameter extraction."""
from __future__ import annotations
import argparse, subprocess, os, sys, time, math, re, shutil
from pathlib import Path
from typing import List, Dict, Tuple, Optional
import numpy as np

MODEL_LIB = "ptm_130.lib"
NGSPICE_BIN = "ngspice"
DEFAULT_L_SWEEP = [130e-9,150e-9,180e-9,220e-9,270e-9,350e-9,500e-9,750e-9,1.0e-6,1.5e-6,2.0e-6]
DEFAULT_VGS_START, DEFAULT_VGS_STOP, DEFAULT_VGS_STEP = 0.0, 1.2, 0.05
DEFAULT_VDS, DEFAULT_VSB, DEFAULT_W = 0.6, 0.0, 1e-6

def _resolve_model_path(lib: str) -> Path:
    p = Path(lib)
    if not p.is_absolute():
        c = Path(__file__).parent.parent / lib
        p = c.resolve() if c.exists() else p.resolve()
    return p

def _library_lines(lib: str, corner: str = "") -> List[str]:
    p = _resolve_model_path(lib)
    if corner:
        return [f'.lib "{p}" {corner}', ".temp 27", ""]
    return [f'.include "{p}"', ".temp 27", ""]

def _device_line(style: str, dev: str, model: str, W: float, L: float) -> str:
    if style == "subckt":
        return f"X1 d g s b {model} W={W} L={L}"
    prefix = "M1"
    return f"{prefix} d g s b {model} W={W} L={L}"

def _op_exprs(style: str, model: str) -> str:
    if style == "subckt":
        inner = f"n.x1.n{model.lower()}"
        params = ("gm", "gds", "vdss", "cgg", "cgs", "cgd")
        return " ".join(f"@{inner}[{param}]" for param in params)
    return "@m1[gm] @m1[gds] @m1[vth] @m1[vdsat] @m1[cgg] @m1[cgs] @m1[cgd]"

def generate_netlist(dev, L, vgs0, vgs1, vgs_step, vds, W=DEFAULT_W, lib=MODEL_LIB,
                     corner: str = "", nmos_model: str = "nmos", pmos_model: str = "pmos",
                     device_style: str = "mos"):
    model = nmos_model if dev=="nmos" else pmos_model
    vlist = np.arange(vgs0, vgs1+vgs_step/2, vgs_step)
    vstr = " ".join(f"{v:.4f}" for v in vlist)
    libs = _library_lines(lib, corner)
    op_exprs = _op_exprs(device_style, model)
    if dev == "pmos":
        vdd, vs, vd = 1.2, 1.2, vds  # V(d) = vds, current flows from s→d→VDS_dummy→0
        # PMOS needs negative VGS: V(g) < V(s) to turn on
        vstr_neg = " ".join(f"{-v:.4f}" for v in vlist)
        return "\n".join([
            f"* techsweep pmos L={L*1e9:.0f}nm |VDS|={vds}V",
            *libs,
            _device_line(device_style, dev, model, W, L).replace(" s b ", " s s "),
            f"Vsup s 0 DC {vs}",
            f"VGS_sweep g s DC 0",
            f"VDS_dummy d 0 DC {vd}",
            "",
            ".control",
            f"  foreach vgs_val {vstr_neg}",
            "    alter VGS_sweep dc = $vgs_val", "    op",
            f"    print v(s,g) i(VDS_dummy) {op_exprs}",
            "  end", ".endc", "", ".end",
        ])
    else:
        return "\n".join([
            f"* techsweep nmos L={L*1e9:.0f}nm VDS={vds}V",
            *libs,
            _device_line(device_style, dev, model, W, L).replace(" s b ", " 0 0 "),
            f"VGS g 0 DC 0", f"VDS d 0 DC {vds}", "",
            ".control",
            f"  foreach vgs_val {vstr}",
            "    alter VGS dc = $vgs_val", "    op",
            f"    print v(g) i(vds) {op_exprs}",
            "  end", ".endc", "", ".end",
        ])

def run_sim(netlist, wdir, timeout=120, osdi_libs: Optional[List[str]] = None):
    os.makedirs(wdir, exist_ok=True)
    cp = os.path.join(wdir, "techsweep.cir")
    with open(cp,"w") as f: f.write(netlist)
    if osdi_libs:
        with open(os.path.join(wdir, ".spiceinit"), "w") as f:
            for osdi in osdi_libs:
                f.write(f"osdi {_resolve_model_path(osdi)}\n")
    try:
        p = subprocess.run([NGSPICE_BIN,"-b",os.path.abspath(cp)], capture_output=True, text=True, timeout=timeout, cwd=wdir)
        return (p.returncode==0, p.stdout)
    except subprocess.TimeoutExpired: return (False,"")
    except FileNotFoundError: return (False,"")

def parse_output(stdout):
    vgs_l,id_l,gm_l,gds_l,vth_l,vsat_l,cgg_l,cgs_l,cgd_l = [],[],[],[],[],[],[],[],[]
    cur = {}
    def get_param(values: Dict[str, float], name: str, default: float = 0.0) -> float:
        for key in (f"@m1[{name}]", f"@m1[{name.lower()}]"):
            if key in values:
                return values[key]
        suffix = f"[{name.lower()}]"
        for key, value in values.items():
            if key.lower().endswith(suffix):
                return value
        return default

    def flush(values: Dict[str, float]) -> None:
        if len(values) < 4:
            return
        vgs_l.append(values.get("v(g)", values.get("v(s,g)", 0)))
        id_l.append(values.get("i(vds)", values.get("i(VDS_dummy)", values.get("i(vds_dummy)", 0))))
        gm_l.append(get_param(values, "gm"))
        gds_l.append(get_param(values, "gds"))
        vth_l.append(get_param(values, "vth"))
        vsat_l.append(get_param(values, "vdsat", get_param(values, "vdss")))
        cgg_l.append(get_param(values, "cgg"))
        cgs_l.append(get_param(values, "cgs"))
        cgd_l.append(get_param(values, "cgd"))

    for line in stdout.split("\n"):
        s = line.strip()
        if not s:
            flush(cur)
            cur = {}; continue
        m = re.match(r'^(.+?)\s*=\s*([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)', s)
        if m: cur[m.group(1).strip()] = float(m.group(2))
    flush(cur)
    if len(vgs_l) < 3: return None
    return {"VGS":np.array(vgs_l),"ID":np.abs(np.array(id_l)),"GM":np.abs(np.array(gm_l)),
            "GDS":np.abs(np.array(gds_l)),"VTH":np.array(vth_l),"VDSAT":np.array(vsat_l),
            "CGG":np.abs(np.array(cgg_l)),"CGS":np.abs(np.array(cgs_l)),"CGD":np.abs(np.array(cgd_l))}

def build_2d(L_grid, vgs_grid, all_data, vds, vsb, W):
    N, M = len(vgs_grid), len(L_grid)
    keys = ["ID_W","GM_W","GDS_W","VTH","VDSAT","CGG_W","CGS_W","CGD_W","FT","GM_ID","GM_GDS","GM_CGG"]
    arrs = {k: np.full((M,N), np.nan) for k in keys}
    for i, d in enumerate(all_data):
        v = d.get("VGS"); 
        if v is None or len(v)<3: continue
        if v[0] > v[-1]:
            for k in list(d.keys()): d[k] = d[k][::-1]
            v = d["VGS"]
        iw = np.abs(d["ID"])/W; gw = np.abs(d["GM"])/W; gdw = np.abs(d["GDS"])/W
        cgw = np.abs(d.get("CGG",np.zeros_like(iw)))/W
        csw = np.abs(d.get("CGS",np.zeros_like(iw)))/W
        cdw = np.abs(d.get("CGD",np.zeros_like(iw)))/W
        ids = np.divide(gw, iw, out=np.zeros_like(gw), where=iw>1e-18)
        ggs = np.divide(gw, gdw, out=np.zeros_like(gw), where=gdw>1e-18)
        gcs = np.divide(gw, cgw, out=np.zeros_like(gw), where=cgw>1e-18)
        ft  = np.divide(gw, 2*math.pi*cgw, out=np.zeros_like(gw), where=cgw>1e-18)
        for k, src in [("ID_W",iw),("GM_W",gw),("GDS_W",gdw),("VTH",d.get("VTH",np.zeros_like(iw))),
                        ("VDSAT",d.get("VDSAT",np.zeros_like(iw))),("CGG_W",cgw),("CGS_W",csw),
                        ("CGD_W",cdw),("FT",ft),("GM_ID",ids),("GM_GDS",ggs),("GM_CGG",gcs)]:
            arrs[k][i,:] = np.interp(vgs_grid, v, src, left=np.nan, right=np.nan)
    # fill NaN
    for k in keys:
        for i in range(M):
            r = arrs[k][i,:]; nan = np.isnan(r)
            if np.all(nan): continue
            last = np.nan
            for j in range(N):
                if not np.isnan(r[j]): last=r[j]
                elif not np.isnan(last): r[j]=last
            last = np.nan
            for j in range(N-1,-1,-1):
                if not np.isnan(r[j]): last=r[j]
                elif not np.isnan(last): r[j]=last
            arrs[k][i,:] = r
    return {"L_grid":np.array(L_grid,dtype=np.float64),"VGS_grid":vgs_grid.astype(np.float64),
            "VDS":np.float64(vds),"VSB":np.float64(vsb),
            **{k:v.astype(np.float64) for k,v in arrs.items()}}

def generate_table(dev, L_sweep=None, vgs0=DEFAULT_VGS_START, vgs1=DEFAULT_VGS_STOP,
                   vgs_step=DEFAULT_VGS_STEP, vds=DEFAULT_VDS, vsb=DEFAULT_VSB,
                   output_dir="tables", verbose=True, keep_tmp=False, model_lib=MODEL_LIB,
                   model_corner: str = "", nmos_model: str = "nmos", pmos_model: str = "pmos",
                   device_style: str = "mos", osdi_libs: Optional[List[str]] = None,
                   output_prefix: str = "ptm130"):
    if L_sweep is None: L_sweep = DEFAULT_L_SWEEP
    vgs_grid = np.arange(vgs0, vgs1+vgs_step/2, vgs_step)
    if verbose: print(f"  VGS grid: {len(vgs_grid)} pts ({vgs0:.2f}->{vgs1:.2f}V)")
    all_data = []; tmp_dir = os.path.join(output_dir, f"tmp_{dev}")
    for i, L in enumerate(L_sweep):
        if verbose: print(f"  [{i+1}/{len(L_sweep)}] L={L*1e9:.0f}nm ...", end=" ", flush=True)
        nl = generate_netlist(dev, L, vgs0, vgs1, vgs_step, vds, lib=model_lib,
                              corner=model_corner, nmos_model=nmos_model,
                              pmos_model=pmos_model, device_style=device_style)
        ok, out = run_sim(nl, tmp_dir, 120, osdi_libs=osdi_libs)
        if not ok: print("FAILED"); continue
        d = parse_output(out)
        if d is None: print("FAILED (parse)"); continue
        all_data.append(d)
        gm_abs = np.abs(d["GM"])
        id_abs = np.abs(d["ID"])
        ids = np.divide(gm_abs, id_abs, out=np.zeros_like(gm_abs), where=id_abs>1e-18)
        if verbose:
            vv = ids[np.abs(d["ID"])>1e-18]
            rng = (float(np.min(vv)),float(np.max(vv))) if len(vv)>=2 else (0,0)
            print(f"OK (GM_ID: {rng[0]:.1f}-{rng[1]:.1f})")
    if not all_data: print("  No valid data"); return None
    tbl = build_2d(L_sweep, vgs_grid, all_data, vds, vsb, DEFAULT_W)
    os.makedirs(output_dir, exist_ok=True)
    fn = os.path.join(output_dir, f"{output_prefix}_{dev}.npz")
    np.savez_compressed(fn, **tbl)
    if verbose: print(f"  Saved: {fn}")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from pygmid.lookup import LookupTable
    t = LookupTable(fn)
    if verbose: print(f"  {t.summary()}")
    if not keep_tmp and os.path.isdir(tmp_dir): shutil.rmtree(tmp_dir, ignore_errors=True)
    return fn

def main():
    ap = argparse.ArgumentParser(description="Generate gm/ID lookup tables using ngspice operating points")
    ap.add_argument("--device", choices=["nmos","pmos","both"], default="both")
    ap.add_argument("--vds", type=float, default=DEFAULT_VDS)
    ap.add_argument("--vgs-start", type=float, default=DEFAULT_VGS_START)
    ap.add_argument("--vgs-stop", type=float, default=DEFAULT_VGS_STOP)
    ap.add_argument("--vgs-step", type=float, default=DEFAULT_VGS_STEP)
    ap.add_argument("--output", type=str, default="tables")
    ap.add_argument("--L-min", type=float, default=130e-9)
    ap.add_argument("--L-max", type=float, default=2e-6)
    ap.add_argument("--L-points", type=int, default=11)
    ap.add_argument("--keep-tmp", action="store_true")
    ap.add_argument("--model-lib", type=str, default=MODEL_LIB)
    ap.add_argument("--model-corner", type=str, default="")
    ap.add_argument("--nmos-model", type=str, default="nmos")
    ap.add_argument("--pmos-model", type=str, default="pmos")
    ap.add_argument("--device-style", choices=["mos", "subckt"], default="mos")
    ap.add_argument("--osdi", action="append", default=[])
    ap.add_argument("--output-prefix", type=str, default="ptm130")
    args = ap.parse_args()
    L_grid = list(np.logspace(math.log10(args.L_min), math.log10(args.L_max), args.L_points))
    print(f"gm/ID Lookup Table Generator V2")
    print(f"  VDS={args.vds}V  L: {[f'{l*1e9:.0f}nm' for l in L_grid]}")
    if args.device in ("nmos","both"):
        print("\n-- NMOS --")
        generate_table("nmos", L_sweep=L_grid, vds=args.vds, vgs0=args.vgs_start,
                       vgs1=args.vgs_stop, vgs_step=args.vgs_step, output_dir=args.output,
                       keep_tmp=args.keep_tmp, model_lib=args.model_lib,
                       model_corner=args.model_corner, nmos_model=args.nmos_model,
                       pmos_model=args.pmos_model, device_style=args.device_style,
                       osdi_libs=args.osdi, output_prefix=args.output_prefix)
    if args.device in ("pmos","both"):
        print("\n-- PMOS --")
        generate_table("pmos", L_sweep=L_grid, vds=args.vds, vgs0=args.vgs_start,
                       vgs1=args.vgs_stop, vgs_step=args.vgs_step, output_dir=args.output,
                       keep_tmp=args.keep_tmp, model_lib=args.model_lib,
                       model_corner=args.model_corner, nmos_model=args.nmos_model,
                       pmos_model=args.pmos_model, device_style=args.device_style,
                       osdi_libs=args.osdi, output_prefix=args.output_prefix)
    print("\nDone.")

if __name__ == "__main__":
    main()
