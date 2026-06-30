"""
whatif.py — deterministic "what-if" return math on the VALIDATED cash-flow streams.

The chat agent calls this instead of hand-waving ("IRR will decrease; you'd need to
re-run the model"). The engine already has the streams whose XIRR reproduces the
model's stated IRR (`find_spine().matched[leg]["flows"]`); a what-if is just a
perturbation of those flows + a recompute. Universal — no per-model tuning; works on
any model with a validated spine.

V1 scope: an UPFRONT capex / investment change funded by equity (the common
"$500k overrun comes out of equity" question). It raises the initial outflow on
BOTH legs — total project cost (unlevered) and the equity contribution (levered) —
holding every other cash flow at plan, then recomputes XIRR and the equity multiple.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from cashflow_spine import find_spine, xirr


def _em(flows: list[tuple]) -> float | None:
    """Equity multiple = total inflows / |total outflows| on a stream."""
    infl = sum(v for _, v in flows if v > 0)
    outf = sum(v for _, v in flows if v < 0)
    return (infl / abs(outf)) if outf else None


def _apply_upfront(flows: list[tuple], delta: float) -> list[tuple]:
    """Add `delta` (signed; negative = extra outflow) to the FIRST period — the
    acquisition / initial-equity outflow. Every other period is held at plan."""
    flows = list(flows)
    if not flows:
        return flows
    d0, v0 = flows[0]
    flows[0] = (d0, v0 + delta)
    return flows


def what_if_capex(model_path: str | Path, amount: float,
                  funded_by: str = "equity") -> dict[str, Any]:
    """Recompute returns under an UPFRONT capex / investment change of `amount`
    (positive = more spend), holding all other cash flows at plan.

    funded_by='equity' (default): the extra outflow hits the equity contribution
    (levered) and total project cost (unlevered) at t0. Returns per-leg old-vs-new
    IRR and equity multiple, plus the bps/multiple deltas.
    """
    sp = find_spine(Path(model_path))
    if not sp.ok:
        return {"ok": False, "reason": (sp.diagnostics or {}).get(
            "reason", "no validated cash-flow engine — cannot recompute returns")}

    amt = abs(float(amount))
    legs: dict[str, Any] = {}
    for leg in ("levered", "unlevered"):
        m = sp.matched.get(leg)
        if not m:
            continue
        flows = m["flows"]
        new = _apply_upfront(flows, -amt)            # extra upfront outflow
        old_irr = m.get("recomputed_irr")
        new_irr = xirr(new)
        old_em = m.get("recomputed_em") or _em(flows)
        new_em = _em(new)
        legs[leg] = {
            "old_irr": old_irr, "new_irr": new_irr,
            "irr_delta_bps": (round((new_irr - old_irr) * 10000) if
                              isinstance(old_irr, (int, float)) and isinstance(new_irr, (int, float)) else None),
            "old_em": old_em, "new_em": new_em,
            "em_delta": (round(new_em - old_em, 3) if
                         isinstance(old_em, (int, float)) and isinstance(new_em, (int, float)) else None),
        }
    if not legs:
        return {"ok": False, "reason": "no levered/unlevered stream matched on this model"}
    return {"ok": True, "amount": amt, "funded_by": funded_by, "timing": "upfront", "legs": legs}


if __name__ == "__main__":
    import sys
    r = what_if_capex(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 500000)
    if not r.get("ok"):
        print("blocked:", r.get("reason")); raise SystemExit(1)
    print(f"+${r['amount']:,.0f} upfront ({r['funded_by']}-funded):")
    for leg, d in r["legs"].items():
        print(f"  {leg:<10} IRR {d['old_irr']*100:.2f}% -> {d['new_irr']*100:.2f}% "
              f"({d['irr_delta_bps']:+d} bps)   EM {d['old_em']:.2f}x -> {d['new_em']:.2f}x "
              f"({d['em_delta']:+.3f})")
