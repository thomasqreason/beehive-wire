#!/usr/bin/env python3
"""Checks that the browser's chart analysis (templates/markets_analyze.js, used by the quote box) gives the same
answers as the desk's own analyze() in markets.py. Runs both on a few hundred synthetic price histories — bases,
breakouts, pullbacks, fresh IPOs, crashes — and diffs every field the pop-up shows.   python relay/parity_test.py"""
import json, subprocess, sys, tempfile
from pathlib import Path
import numpy as np, pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import markets  # noqa: E402

def synth(seed: int, n: int):
    rng = np.random.default_rng(seed)
    c = [50 + rng.random() * 200]
    regime = 0
    for i in range(1, n):
        if i % rng.integers(15, 60) == 0:
            regime = rng.choice([-1, 0, 0, 1, 2])
        drift = {-1: -0.004, 0: 0.0, 1: 0.003, 2: 0.008}[int(regime)]
        c.append(max(1.0, c[-1] * (1 + drift + rng.normal(0, 0.018))))
    c = np.array(c)
    o = c * (1 + rng.normal(0, 0.006, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.01, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.01, n)))
    v = (rng.lognormal(14, 0.5, n)).astype(int) + 1000
    idx = pd.bdate_range("2024-10-01", periods=n)
    df = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v.astype(float)}, index=idx)
    return df

FIELDS = ["date", "close", "chg", "chg_pct", "vol_ratio", "hi52", "from_hi52", "vs_sma50", "vs_sma200", "status", "from_pivot",
          "days_since_breakout", "new_high", "bounce50", "breakdown50", "ipo_sessions", "angle"]
BASE = ["buy_point", "buy_max", "kind", "weeks", "depth", "rim_date", "low_date", "breakout_date"]

def main() -> int:
    cfg = markets.load_cfg()
    cases, py = [], []
    lengths = [16, 40, 90, 129, 130, 200, 260, 330, 504]
    for seed in range(400):
        n = lengths[seed % len(lengths)]
        df = synth(seed, n)
        f = markets.analyze(f"S{seed}", df, None, cfg)
        if f:
            f["angle"] = markets.angle(f)
        cases.append({"sym": f"S{seed}", "d": [d.date().isoformat() for d in df.index],
                      **{k: df[col].tolist() for k, col in zip("ohlcv", ["Open", "High", "Low", "Close", "Volume"])}})
        py.append(f)
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "cases.json"; inp.write_text(json.dumps(cases))
        js = f"""
        require({json.dumps(str(ROOT / 'templates' / 'markets_analyze.js'))});
        const cases = JSON.parse(require('fs').readFileSync({json.dumps(str(inp))}, 'utf8'));
        const zone = {cfg['desk'].get('buy_zone_pct', 5)}, bars = {cfg['desk'].get('chart_bars', 130)};
        process.stdout.write(JSON.stringify(cases.map(c => globalThis.BWA.analyze(c.sym, c, {{buyZonePct: zone, chartBars: bars}}))));
        """
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    jsres = json.loads(out)
    bad = 0
    statuses = {}
    for p, j in zip(py, jsres):
        diffs = []
        if (p is None) != (j is None):
            diffs.append(("none", p is None, j is None))
        elif p:
            statuses[p["status"]] = statuses.get(p["status"], 0) + 1
            for k in FIELDS:
                if p.get(k) != j.get(k):
                    diffs.append((k, p.get(k), j.get(k)))
            if (p["base"] is None) != (j["base"] is None):
                diffs.append(("base", p["base"], j["base"]))
            elif p["base"]:
                for k in BASE:
                    if p["base"].get(k) != j["base"].get(k):
                        diffs.append(("base." + k, p["base"].get(k), j["base"].get(k)))
            if p["rs_score"] is not None and abs(p["rs_score"] - j["rs_score"]) > 1e-9:
                diffs.append(("rs_score", p["rs_score"], j["rs_score"]))
            for k in ("d", "c", "e21", "s50", "s200", "v", "rim_i", "low_i", "bo_i"):
                if p["series"].get(k) != j["series"].get(k):
                    diffs.append(("series." + k, str(p["series"].get(k))[:60], str(j["series"].get(k))[:60]))
        if diffs:
            bad += 1
            print(p and p["sym"], diffs[:4])
    print(f"{len(py) - bad}/{len(py)} identical; statuses covered: {statuses}")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
