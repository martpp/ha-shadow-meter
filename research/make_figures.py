"""Figures for the README / a blog post, from the results in out/ (run the pipeline first).

Usage:
  make_figures.py fridge-seasonal [--until 2025-09]
  make_figures.py validation
  make_figures.py night <phase> "<start>" "<end>"                 # raw phase power, e.g. fridge cycles
  make_figures.py gap-artefact <phase> <meter> "<start>" "<end>"  # residual with short vs. long carry-forward
  make_figures.py stove-hours
Times are local (config: timezone). Figures go to out/figures/.
"""
import argparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from common import power
from config import CFG, DATA, OUT, PHASES, TZ

FIG = OUT / "figures"
FIG.mkdir(exist_ok=True)
NAMES = CFG.get("display_names", {})
# reference palette (light mode): series 1 blue, series 2 orange; recessive text and grid
BLUE, ORANGE = "#2a78d6", "#eb6834"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True, "font.size": 11,
    "axes.titlesize": 13, "axes.titleweight": "bold", "axes.titlelocation": "left", "legend.frameon": False,
})


def save(fig, name):
    fig.tight_layout()
    fig.savefig(FIG / name, dpi=150)
    print("saved", FIG / name)


def fridge_seasonal(a):
    m = pd.read_csv(OUT / "fridge_monthly.csv", index_col=0)
    m.index = pd.PeriodIndex(m.index, freq="M")
    if a.until:
        m = m[m.index <= pd.Period(a.until, "M")]
    fig, ax = plt.subplots(figsize=(10, 4))
    x = m.index.to_timestamp()
    ax.bar(x, m.duty_pct, width=24, color=BLUE)
    ax.set_ylabel("compressor running (% of time)")
    ax.set_title("Fridge duty cycle per month, detected from the whole-home meter")
    ax.grid(axis="x", visible=False)
    kwh = (m.kwh_per_day.mean() * 365).round()
    ax.text(0.01, 0.96, f"≈ {m.p_run_w.median():.0f} W while running · ≈ {kwh:.0f} kWh/year",
            transform=ax.transAxes, va="top", color=INK2)
    save(fig, "fridge_seasonal.png")


def validation(a):
    v = pd.read_csv(OUT / "validation.csv")
    v["label"] = [NAMES.get(l, l.removeprefix("light.")) for l in v.light]
    v = v.iloc[::-1].reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(9, 0.6 * len(v) + 2.0))
    y = np.arange(len(v))
    ax.errorbar(v.estimated_w, y + 0.12, xerr=1.96 * v.se_w, fmt="o", color=ORANGE, ms=8, lw=2, capsize=0,
                label="estimated from whole-home meter (95% CI)")
    ax.plot(v.measured_w, y - 0.12, "o", color=BLUE, ms=8, label="measured by its own plug")
    ax.set_yticks(y, v.label)
    ax.axvline(0, color=INK2, lw=0.8)
    ax.set_xlabel("power (W)")
    ax.set_title("Validation: metered lights, hidden from the model")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.4, -0.2), ncol=2, fontsize=9)
    save(fig, "validation.png")


def local_range(start, end):
    return pd.Timestamp(start, tz=TZ).tz_convert("UTC"), pd.Timestamp(end, tz=TZ).tz_convert("UTC")


def night(a):
    s, e = local_range(a.start, a.end)
    raw = pd.read_parquet(DATA / "W" / f"{PHASES[a.phase]}.parquet",
                          filters=[("time", ">=", s), ("time", "<", e)]).set_index("time")["value"].sort_index()
    fig, ax = plt.subplots(figsize=(10, 3.6))
    ax.step(raw.index.tz_convert(TZ), raw.values, where="post", color=BLUE, lw=1.5)
    ax.set_ylabel("W")
    ax.set_ylim(0, np.percentile(raw.values, 99.5) * 1.15)
    ax.set_title(f"One night on phase {a.phase.upper()} (raw): the fridge compressor switching on and off")
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M", tz=TZ))
    save(fig, f"night_phase_{a.phase}.png")


def gap_artefact(a):
    s, e = local_range(a.start, a.end)
    raw = pd.read_parquet(DATA / "W" / f"{PHASES[a.phase]}.parquet",
                          filters=[("time", ">=", s), ("time", "<", e)]).set_index("time")["value"].sort_index()
    raw = raw.resample("5s").last().ffill()
    short = power(a.meter, limit="2min", start=s, end=e).reindex(raw.index).fillna(0)
    long = power(a.meter, limit="6h", start=s, end=e).reindex(raw.index).fillna(0)
    fig, ax = plt.subplots(2, 1, figsize=(10, 5.2), sharex=True, sharey=True)
    for x, sub, title in ((ax[0], short, "value carried forward for 2 min, then treated as 0  →  fake 'device'"),
                          (ax[1], long, "value carried forward until the next report  →  flat, as it should be")):
        r = raw - sub
        x.plot(r.index.tz_convert(TZ), r.values, color=ORANGE if x is ax[0] else BLUE, lw=1)
        x.set_title(title, fontsize=11, fontweight="normal")
        x.set_ylabel("residual (W)")
    ax[1].xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M", tz=TZ))
    fig.suptitle("Pitfall: Home Assistant only records power when it changes", x=0.01, ha="left",
                 fontweight="bold", fontsize=13)
    save(fig, "gap_artefact.png")


def stove_hours(a):
    sess = pd.read_parquet(OUT / "stove_sessions.parquet")
    h = sess["min"].dt.tz_convert(TZ).dt.hour.value_counts().reindex(range(24), fill_value=0)
    fig, ax = plt.subplots(figsize=(10, 3.6))
    ax.bar(h.index, h / h.sum() * 100, width=0.8, color=BLUE)
    ax.set_xticks(range(0, 24, 2))
    ax.set_xlabel("hour of day (start of cooking session)")
    ax.set_ylabel("% of sessions")
    ax.set_title(f"Stove: {len(sess):,} detected cooking sessions by hour of day")
    ax.grid(axis="x", visible=False)
    save(fig, "stove_hours.png")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("fridge-seasonal").add_argument("--until")
    sp.add_parser("validation")
    p = sp.add_parser("night")
    p.add_argument("phase"), p.add_argument("start"), p.add_argument("end")
    p = sp.add_parser("gap-artefact")
    p.add_argument("phase"), p.add_argument("meter"), p.add_argument("start"), p.add_argument("end")
    sp.add_parser("stove-hours")
    a = ap.parse_args()
    {"fridge-seasonal": fridge_seasonal, "validation": validation, "night": night,
     "gap-artefact": gap_artefact, "stove-hours": stove_hours}[a.cmd](a)
