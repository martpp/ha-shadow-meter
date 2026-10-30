"""Joint robust step regression for all lights.

Lights in homes rarely switch alone (scenes, automations), so the step in the whole-home power at a
switch event is usually the sum of several lights. All switch events are grouped into clusters
(events less than GAP apart). For each cluster and phase p:
    Δphase_p = Σ_lights w_{l,p} · s_l + noise
s_l ∈ {-1, 0, +1} is the net state change of light l within the cluster. Only "clean" clusters are used
(stable windows before/after, no metered device changed). Solved per phase with a Huber loss, which is
robust against large devices that happen to switch at the same time.
Lights that (almost) always switch together cannot be separated; they are merged into groups.

Metered lights (config: metered_lights) are hidden from the model and used to validate the estimates.
Usage: joint.py [--max-spread 25] [--out out/lights.csv]
"""
import argparse

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.optimize import least_squares

from common import RULE, phases, power, transitions
from config import DATA, LIGHTS_EXCLUDE, METERED_LIGHTS, METERS, OUT

STEP = pd.Timedelta(RULE)
GAP = pd.Timedelta(seconds=45)
PRE, POST = (40, 10), (10, 40)  # seconds before the first / after the last switch event of a cluster


def clusters(lights):
    ev = pd.concat([transitions(l).assign(dev=l) for l in lights]).sort_values("time").reset_index(drop=True)
    ev["cid"] = (ev.time.diff() > GAP).cumsum()
    g = ev.groupby("cid")
    cl = pd.DataFrame({"t0": g.time.min(), "t1": g.time.max()})
    # net change per device: final state - initial state (initial = opposite of the first change)
    f = ev.drop_duplicates(["cid", "dev"], keep="first").set_index(["cid", "dev"]).on
    l = ev.drop_duplicates(["cid", "dev"], keep="last").set_index(["cid", "dev"]).on
    net = (l.astype(int) - (~f).astype(int)).rename("s")
    S = net[net != 0].unstack(fill_value=0).reindex(index=cl.index, columns=lights, fill_value=0)
    return cl, S


def window_delta(s, t0, t1, with_spread=False):
    """median(after) - median(before) on the 5 s grid, vectorised. NaN if a window is incomplete."""
    v = s.to_numpy()
    start = s.index[0]
    p0 = ((t0 - start) // STEP).to_numpy()
    p1 = ((t1 - start) // STEP).to_numpy()
    pre = p0[:, None] + np.arange(-PRE[0] // 5, -PRE[1] // 5)
    post = p1[:, None] + np.arange(POST[0] // 5 + 1, POST[1] // 5 + 1)
    ok = (pre.min(1) >= 0) & (post.max(1) < len(v))
    out = np.full(len(t0), np.nan, dtype=np.float32)
    spread = np.full(len(t0), np.nan, dtype=np.float32)
    if ok.any():
        a, b = v[pre[ok]], v[post[ok]]
        with np.errstate(all="ignore"):
            out[ok] = np.median(b, 1) - np.median(a, 1)  # NaN inside a window -> NaN
            spread[ok] = np.maximum(np.ptp(a, 1), np.ptp(b, 1))
    return (out, spread) if with_spread else out


def huber(X, y, scale=8.0):
    X = sparse.csr_matrix(X)
    r = least_squares(lambda w: X @ w - y, np.zeros(X.shape[1]), jac=lambda w: X, loss="huber",
                      f_scale=scale, method="trf", tr_solver="lsmr")
    # rough standard errors from the robustly weighted normal equations
    res = X @ r.x - y
    wt = np.minimum(1, scale / np.maximum(np.abs(res), 1e-9))
    XtWX = (X.T @ sparse.diags(wt) @ X).toarray()
    sig2 = np.sum(wt * res**2) / max(1, len(y) - X.shape[1])
    return r.x, np.sqrt(np.diag(np.linalg.pinv(XtWX)) * sig2)


def features():
    """Computed once, then cached: clusters, switch matrix, phase steps, meter steps."""
    cache = DATA / "joint_features.npz"
    if cache.exists():
        z = np.load(cache, allow_pickle=True)
        return {k: z[k] for k in z.files}
    lights = sorted(f.stem for f in (DATA / "state").glob("light.*.parquet"))
    lights = [l for l in lights if l not in LIGHTS_EXCLUDE and len(transitions(l)) >= 20]
    cl, S = clusters(lights)
    ph = phases()
    YS = [window_delta(ph[p], cl.t0, cl.t1, True) for p in "abc"]
    meters = [m for m in METERS if (DATA / "W" / f"{m}.parquet").exists()]
    M = np.column_stack([window_delta(power(m), cl.t0, cl.t1) for m in meters])
    f = dict(lights=np.array(lights), meters=np.array(meters), S=S.to_numpy(np.int8), t0=cl.t0.to_numpy(),
             Y=np.column_stack([y for y, _ in YS]), spread=np.column_stack([s for _, s in YS]), M=M)
    np.savez(cache, **f)
    return f


def group_collinear(X, lights, thr):
    """Merge lights whose switch vectors correlate above thr (union-find)."""
    C = np.abs(np.nan_to_num(np.corrcoef(X.T)))
    parent = list(range(len(lights)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for i in range(len(lights)):
        for j in range(i + 1, len(lights)):
            if C[i, j] > thr:
                parent[root(j)] = root(i)
    groups = {}
    for i in range(len(lights)):
        groups.setdefault(root(i), []).append(i)
    glist = list(groups.values())
    Xg = np.column_stack([X[:, g].mean(1) for g in glist])  # group regressor: mean net change of its members
    names = [" + ".join(lights[i].removeprefix("light.") for i in g) if len(g) > 1 else lights[g[0]]
             for g in glist]
    return Xg, names


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-spread", type=float, default=25, help="max. range (W) within a window, per phase")
    ap.add_argument("--max-meter", type=float, default=2, help="max. |step| (W) of any other meter in the cluster")
    ap.add_argument("--scale", type=float, default=8, help="Huber scale (W)")
    ap.add_argument("--group-corr", type=float, default=0.9)
    ap.add_argument("--out", default=str(OUT / "lights.csv"))
    a = ap.parse_args()
    f = features()
    lights, meters = list(f["lights"]), list(f["meters"])
    S, Y, M, spread = f["S"].astype(np.float32), f["Y"], f["M"], f["spread"]
    hidden = [m for m in METERED_LIGHTS if m in meters]
    Mk = np.nan_to_num(M[:, [i for i, m in enumerate(meters) if m not in hidden]])
    ok = np.isfinite(Y).all(1) & (np.nan_to_num(spread, nan=1e9) <= a.max_spread).all(1) \
        & (np.abs(Mk) <= a.max_meter).all(1)
    print(f"{len(lights)} lights, {len(S)} clusters, clean: {ok.sum()}"
          f" (range <= {a.max_spread} W, other meters <= {a.max_meter} W)")
    X, names = group_collinear(S[ok], lights, a.group_corr)
    res = {}
    for i, p in enumerate("abc"):
        res[p], res[p + "_se"] = huber(X, Y[ok, i], a.scale)
    R = pd.DataFrame(res, index=names)
    R["total"] = R[["a", "b", "c"]].sum(axis=1)
    R["se"] = np.sqrt((R[["a_se", "b_se", "c_se"]] ** 2).sum(axis=1))
    R["phase"] = R[["a", "b", "c"]].abs().idxmax(axis=1)
    R["n_events"] = (X != 0).sum(0)
    C = np.corrcoef(X.T)
    np.fill_diagonal(C, 0)
    C = np.abs(np.nan_to_num(C))
    R["max_corr"], R["corr_with"] = C.max(1), [names[j] for j in C.argmax(1)]
    pd.set_option("display.width", 220, "display.max_rows", 200)
    print(R[["total", "se", "a", "b", "c", "phase", "n_events", "max_corr", "corr_with"]].round(2)
          .sort_values("total", ascending=False).to_string())
    print("\n== Validation against hidden meters")
    val = []
    for m, l in METERED_LIGHTS.items():
        if m not in meters:
            continue
        if l not in names:
            print(f"{l}: part of a group, cannot be checked on its own")
            continue
        d, s = M[ok, meters.index(m)], X[:, names.index(l)]
        sel = (s != 0) & np.isfinite(d)
        if sel.sum() < 10:
            print(f"{l}: too few clean events ({sel.sum()})")
            continue
        true = np.median(d[sel] * s[sel])
        print(f"{l:28s} measured {true:6.1f} W  estimated {R.loc[l, 'total']:6.1f} ± {R.loc[l, 'se']:.1f} W"
              f"  (n={sel.sum()})")
        val.append(dict(light=l, measured_w=true, estimated_w=R.loc[l, "total"], se_w=R.loc[l, "se"], n=sel.sum()))
    R.to_csv(a.out)
    pd.DataFrame(val).to_csv(OUT / "validation.csv", index=False)
