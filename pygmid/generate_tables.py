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

def generate_netlist(dev, L, vgs0, vgs1, vgs_step, vds, W=DEFAULT_W, lib=MODEL_LIB):
    model = "nmos" if dev=="nmos" else "pmos"
    p = Path(lib)
    if not p.is_absolute():
        c = Path(__file__).parent.parent / lib
        p = c.resolve() if c.exists() else p.resolve()
    vlist = np.arange(vgs0, vgs1+vgs_step/2, vgs_step)
    vstr = " ".join(f"{v:.4f}" for v in vlist)
    if dev == "pmos":
        vdd, vs, vd = 1.2, 1.2, vds  # V(d) = vds, current flows from s→d→VDS_dummy→0
        # PMOS needs negative VGS: V(g) < V(s) to turn on
        vstr_neg = " ".join(f"{-v:.4f}" for v in vlist)
        return "\n".join([
            f"* techsweep pmos L={L*1e9:.0f}nm |VDS|={vds}V",
            f".include {p}", ".temp 27", "",
            f"M1 d g s s {model} W={W} L={L}",
            f"Vsup s 0 DC {vs}",
            f"VGS_sweep g s DC 0",
            f"VDS_dummy d 0 DC {vd}",
            "",
            ".control",
            f"  foreach vgs_val {vstr_neg}",
            "    alter VGS_sweep dc = $vgs_val", "    op",
            "    print v(s,g) i(VDS_dummy) @m1[gm] @m1[gds] @m1[vth] @m1[vdsat] @m1[cgg] @m1[cgs] @m1[cgd]",
            "  end", ".endc", "", ".end",
        ])
    else:
        return "\n".join([
            f"* techsweep nmos L={L*1e9:.0f}nm VDS={vds}V",
            f".include {p}", ".temp 27", "",
            f"M1 d g 0 0 {model} W={W} L={L}",
            f"VGS g 0 DC 0", f"VDS d 0 DC {vds}", "",
            ".control",
            f"  foreach vgs_val {vstr}",
            "    alter VGS dc = $vgs_val", "    op",
            "    print v(g) i(vds) @m1[gm] @m1[gds] @m1[vth] @m1[vdsat] @m1[cgg] @m1[cgs] @m1[cgd]",
            "  end", ".endc", "", ".end",
        ])

def run_sim(netlist, wdir, timeout=120):
    os.makedirs(wdir, exist_ok=True)
    cp = os.path.join(wdir, "techsweep.cir")
    with open(cp,"w") as f: f.write(netlist)
    try:
        p = subprocess.run([NGSPICE_BIN,"-b",os.path.abspath(cp)], capture_output=True, text=True, timeout=timeout, cwd=wdir)
        return (p.returncode==0, p.stdout)
    except subprocess.TimeoutExpired: return (False,"")
    except FileNotFoundError: return (False,"")

def parse_output(stdout):
    vgs_l,id_l,gm_l,gds_l,vth_l,vsat_l,cgg_l,cgs_l,cgd_l = [],[],[],[],[],[],[],[],[]
    cur = {}
    for line in stdout.split("\n"):
        s = line.strip()
        if not s:
            if len(cur) >= 4:
                try:
                    vgs_l.append(cur.get("v(g)", cur.get("v(s,g)", 0)))
                    id_l.append(cur.get("i(vds)", cur.get("i(VDS_dummy)", cur.get("i(vds_dummy)", 0))))
                    gm_l.append(cur["@m1[gm]"]); gds_l.append(cur["@m1[gds]"])
                    vth_l.append(cur.get("@m1[vth]",0)); vsat_l.append(cur.get("@m1[vdsat]",0))
                    cgg_l.append(cur.get("@m1[cgg]",0)); cgs_l.append(cur.get("@m1[cgs]",0))
                    cgd_l.append(cur.get("@m1[cgd]",0))
                except KeyError: pass
            cur = {}; continue
        m = re.match(r'^(.+?)\s*=\s*([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)', s)
        if m: cur[m.group(1).strip()] = float(m.group(2))
    if len(cur) >= 4:
        try:
            vgs_l.append(cur.get("v(g)",cur.get("v(s,g)",0)))
            id_l.append(cur.get("i(vds)",cur.get("i(VDS_dummy)",cur.get("i(vds_dummy)",0))))
            gm_l.append(cur["@m1[gm]"]); gds_l.append(cur["@m1[gds]"])
            vth_l.append(cur.get("@m1[vth]",0)); vsat_l.append(cur.get("@m1[vdsat]",0))
            cgg_l.append(cur.get("@m1[cgg]",0)); cgs_l.append(cur.get("@m1[cgs]",0))
            cgd_l.append(cur.get("@m1[cgd]",0))
        except KeyError: pass
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
        ids = np.divide(gw, iw, where=iw>1e-18)
        ggs = np.divide(gw, gdw, where=gdw>1e-18)
        gcs = np.divide(gw, cgw, where=cgw>1e-18)
        ft  = np.divide(gw, 2*math.pi*cgw, where=cgw>1e-18)
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
                   output_dir="tables", verbose=True, keep_tmp=False):
    if L_sweep is None: L_sweep = DEFAULT_L_SWEEP
    vgs_grid = np.arange(vgs0, vgs1+vgs_step/2, vgs_step)
    if verbose: print(f"  VGS grid: {len(vgs_grid)} pts ({vgs0:.2f}->{vgs1:.2f}V)")
    all_data = []; tmp_dir = os.path.join(output_dir, f"tmp_{dev}")
    for i, L in enumerate(L_sweep):
        if verbose: print(f"  [{i+1}/{len(L_sweep)}] L={L*1e9:.0f}nm ...", end=" ", flush=True)
        nl = generate_netlist(dev, L, vgs0, vgs1, vgs_step, vds)
        ok, out = run_sim(nl, tmp_dir, 120)
        if not ok: print("FAILED"); continue
        d = parse_output(out)
        if d is None: print("FAILED (parse)"); continue
        all_data.append(d)
        ids = np.divide(np.abs(d["GM"]), np.abs(d["ID"]), where=np.abs(d["ID"])>1e-18)
        if verbose:
            vv = ids[np.abs(d["ID"])>1e-18]
            rng = (float(np.min(vv)),float(np.max(vv))) if len(vv)>=2 else (0,0)
            print(f"OK (GM_ID: {rng[0]:.1f}-{rng[1]:.1f})")
    if not all_data: print("  No valid data"); return None
    tbl = build_2d(L_sweep, vgs_grid, all_data, vds, vsb, DEFAULT_W)
    os.makedirs(output_dir, exist_ok=True)
    fn = os.path.join(output_dir, f"ptm130_{dev}.npz")
    np.savez_compressed(fn, **tbl)
    if verbose: print(f"  Saved: {fn}")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from pygmid.lookup import LookupTable
    t = LookupTable(fn)
    if verbose: print(f"  {t.summary()}")
    if not keep_tmp and os.path.isdir(tmp_dir): shutil.rmtree(tmp_dir, ignore_errors=True)
    return fn

def main():
    ap = argparse.ArgumentParser(description="Generate gm/ID lookup tables for PTM 130nm BSIM4")
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
    args = ap.parse_args()
    L_grid = list(np.logspace(math.log10(args.L_min), math.log10(args.L_max), args.L_points))
    print(f"PTM 130nm gm/ID Lookup Table Generator V2 (BSIM4 params)")
    print(f"  VDS={args.vds}V  L: {[f'{l*1e9:.0f}nm' for l in L_grid]}")
    if args.device in ("nmos","both"):
        print("\n-- NMOS --")
        generate_table("nmos", L_sweep=L_grid, vds=args.vds, vgs0=args.vgs_start,
                       vgs1=args.vgs_stop, vgs_step=args.vgs_step, output_dir=args.output,
                       keep_tmp=args.keep_tmp)
    if args.device in ("pmos","both"):
        print("\n-- PMOS --")
        generate_table("pmos", L_sweep=L_grid, vds=args.vds, vgs0=args.vgs_start,
                       vgs1=args.vgs_stop, vgs_step=args.vgs_step, output_dir=args.output,
                       keep_tmp=args.keep_tmp)
    print("\nDone.")

if __name__ == "__main__":
    main()
