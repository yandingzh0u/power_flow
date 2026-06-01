"""扫描 RHO_SOFT 找到 n_parts=4 的可用区间和数值边界"""
import json, sys, time, nbformat, numpy as np
from pathlib import Path
ROOT=Path("/home/y/Desktop/power_flow"); sys.path.insert(0,str(ROOT))
nb=nbformat.read(ROOT/"power_flow_fixed.ipynb",as_version=4)
ns={"__name__":"__main__"}
exec(compile("".join(nb.cells[0].source),"<cell0>","exec"),ns)
idx=json.loads((ROOT/"INDEX_synth_multi.json").read_text())
t_json=Path(idx["paths"]["SYN_T_plus_multiD_json"])
mv_idx=[int(x["mv_bus_idx"]) for x in idx["td_meta"]["attach_list"]]
S_bdry=[(-3.07,-1.73)]*len(mv_idx)
V_mono,Va_mono,_=ns["solve_T_monolithic_and_get_Vs"](t_json=t_json,mv_bus_indices=mv_idx,S_bdry_list_MW_Mvar=S_bdry)

print(f"\n{'RHO':>10} {'x_pu':>10} {'Z@138kV':>10} {'pri_vm@1':>10} {'|ΔV|_max':>10} {'status'}")
print("-"*72)
for rho in [100.0, 500.0, 1000.0, 5000.0, 10000.0, 50000.0, 100000.0, 1000000.0]:
    ns["RHO_SOFT"]=rho
    x_pu=1.0/rho; Zohm=x_pu*138**2/100
    t0=time.time()
    try:
        V_p,Va_p,_=ns["solve_T_multi_and_get_Vs_ADMM"](
            t_json=t_json,mv_bus_indices=mv_idx,S_bdry_list_MW_Mvar=S_bdry,
            n_parts=4, local_parallel=True,
            admm_cfg=ns["ADMMConfig"](max_iter=8,tol_vm=2e-3,tol_va_deg=0.2,over_relax=0.7,verbose=False),
            warm_start_z=None, use_monolithic_warm_start=True, use_soft_boundary=True)
        dv=np.abs(np.array(V_p)-np.array(V_mono)).max()
        print(f"{rho:>10.0f} {x_pu:>10.1e} {Zohm:>9.3f}Ω {'-':>10} {dv:>10.2e}  OK ({time.time()-t0:.1f}s)")
    except Exception as e:
        msg=str(e)[:60]
        print(f"{rho:>10.0f} {x_pu:>10.1e} {Zohm:>9.3f}Ω {'-':>10} {'-':>10}  FAIL: {msg}")
