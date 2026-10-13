#!/usr/bin/env python3
"""Checks the page's live screen (templates/markets_live.js) against the desk: for each synthetic history, the
desk's analyze() on ALL bars (today included) vs the live path — save_screen()'s snapshot of the bars BEFORE today,
plus today's price/high/low from a quote. Status, distance from the buy point, days since breakout and the angle
should agree; the rare misses are days when today's bar would make analyze() pick a different base.
    python relay/live_parity_test.py"""
import json, subprocess, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "relay"))
import markets  # noqa: E402
from parity_test import synth  # noqa: E402

def main() -> int:
    cfg = markets.load_cfg()
    import pandas as pd
    prices = {}
    for i in range(400):
        d = synth(i, [60, 130, 260, 504][i % 4])
        d.index = pd.bdate_range(end="2026-10-13", periods=len(d))     # every name trades "today"
        prices[f"S{i}"] = d
    today = max(d.index[-1].date().isoformat() for d in prices.values())
    rs_all = {k: markets.rs_score(v["Close"].to_numpy(dtype=float)) for k, v in prices.items()}
    rs_all = {k: v for k, v in rs_all.items() if v is not None}
    old = markets.DATA
    with tempfile.TemporaryDirectory() as td:
        markets.DATA = Path(td)
        markets.save_screen(prices, None, cfg, rs_all, lambda s: 80, today, datetime.now(timezone.utc), True, cfg.get("movers") or {})
        S = json.loads((Path(td) / "screen.json").read_text())
    markets.DATA = old
    recs = [dict(zip(S["cols"], r)) for r in S["rows"]]
    cases, py = [], {}
    for r in recs:
        d = prices[r["sym"]]
        if d.index[-1].date().isoformat() != today:
            continue
        f = markets.analyze(r["sym"], d, None, cfg); f["angle"] = markets.angle(f)
        py[r["sym"]] = f
        last = d.iloc[-1]
        cases.append({"r": r, "q": {"p": float(last.Close), "pc": float(d.iloc[-2].Close), "hi": float(last.High), "lo": float(last.Low)}})
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "c.json"; inp.write_text(json.dumps(cases))
        js = f"""require({json.dumps(str(ROOT/'templates'/'markets_analyze.js'))}); globalThis.BWA = globalThis.BWA;
        require({json.dumps(str(ROOT/'templates'/'markets_live.js'))});
        const cs = JSON.parse(require('fs').readFileSync({json.dumps(str(inp))},'utf8'));
        process.stdout.write(JSON.stringify(cs.map(c => globalThis.BWLiveFacts(c.r, c.q, 5))));"""
        out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    same, diffs = 0, []
    for c, j in zip(cases, out):
        p = py[c["r"]["sym"]]
        keys = ("status", "from_pivot", "days_since_breakout", "angle", "chg_pct", "new_high", "bounce50", "breakdown50")
        d = [(k, p.get(k), j.get(k)) for k in keys if p.get(k) != j.get(k)]
        if d: diffs.append((c["r"]["sym"], d[:3]))
        else: same += 1
    for x in diffs[:12]: print(x)
    print(f"{same}/{len(cases)} identical")
    return 0 if same >= 0.97 * len(cases) else 1

if __name__ == "__main__":
    sys.exit(main())
