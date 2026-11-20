"""Receipt-backed fixed-point standing: INERT diagnostics for the SZL estate.

2026-11-20 three-brain study consensus (Kimi K3.1 Pro, GLM-5.3, Mistral Large 4
reviews of the Wikipedia PageRank material): this engine is a diagnostic aid
only. It is NOT qualification, NOT a reliability estimate, NOT
trust-as-entitlement, and it has NO effect on routing, spend, arming, or any
gate. Qualification in this estate is Boolean, evidence-gated, and fail-closed
elsewhere. Callers must already hold a receipt for every edge they pass in,
and every edge must prove exactly what its receipt asserts: capability,
co-use, and handoff are NOT endorsement.

Sybil doctrine: the prior is a FIXED budget over vetted nodes (the study's
"no fresh teleport mass for new identities" rule). Nodes outside the prior
receive standing ONLY through receipt-backed inflow. Dangling mass returns to
the prior, never to the uniform distribution.

Standing values are relative to the supplied node set, prior, and window.
Never publish them as probabilities of reliability or as credentials; expose
ordering plus the disclosure fields.

Pure stdlib. Deterministic. No network, no keys, no secrets. This module is
sovereign: False by construction: it claims no hardware and no energy source,
and it spends nothing.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple


def edge_digest(nodes: Sequence[str], edges: Sequence[Tuple[str, str, float]]) -> str:
    """Stable sha256 digest (first 16 hex chars) of the receipt-validated graph.

    Pinned to a canonical byte representation so a published standing table
    can name exactly which graph (and which alleged receipts) produced it.
    """
    h = hashlib.sha256()
    for n in sorted(nodes):
        h.update(n.encode("utf-8") + b"\x1f")
    for s, d, w in sorted(edges, key=lambda e: (e[0], e[1], float(e[2]))):
        h.update(s.encode("utf-8") + b"\x1f" + d.encode("utf-8") + b"\x1f"
                 + repr(float(w)).encode("ascii") + b"\x1e")
    return h.hexdigest()[:16]


def rank(
    nodes: Sequence[str],
    edges: Sequence[Tuple[str, str, float]],
    prior: Optional[Dict[str, float]] = None,
    damping: float = 0.85,
    tol: float = 1e-12,
    max_rounds: int = 10000,
) -> Dict[str, Any]:
    """Fixed-point standing over a receipt-validated, weighted edge graph.

    nodes: stable identifiers (no duplicates).
    edges: (source, destination, weight>0) tuples the CALLER has already
        receipt-validated. This function cannot and does not validate the
        underlying claims; it is arithmetic on trusted input.
    prior: fixed probability budget over vetted nodes, summing to 1.
        Defaults to the uniform prior OVER THE SUPPLIED NODE SET (a documented
        diagnostic default; trust contexts MUST pass an explicit vetted prior
        per the sybil rule above).
    damping: 0 < damping < 1. The standing leak that prevents closed cycles
        from becoming self-certifying. NOT a calibrated trust discount.

    Returns a disclosure dict: "standing" (id -> mass share), "converged"
    (False means the output MUST NOT be consumed), "rounds", "delta_l1"
    (last successive-iterate L1 gap), "l1_error_bound" (damping/(1-damping) *
    delta_l1; a guaranteed bound on remaining L1 error, NOT a claim of machine
    precision), "damping", "tol", "prior" (echoed), "edge_digest", "note".
    """
    node_tuple = tuple(nodes)
    n = len(node_tuple)
    if n == 0 or len(set(node_tuple)) != n:
        raise ValueError("nodes must be a non-empty sequence of unique ids")
    if not (0.0 < damping < 1.0):
        raise ValueError("damping must be in (0,1)")
    if tol <= 0.0 or max_rounds < 1:
        raise ValueError("bad tol or max_rounds")

    if prior is None:
        p = {x: 1.0 / n for x in node_tuple}
    else:
        p = {x: float(prior.get(x, 0.0)) for x in node_tuple}
    for x in node_tuple:
        if not math.isfinite(p[x]) or p[x] < 0.0:
            raise ValueError("bad prior mass for node: %s" % x)
    if abs(sum(p.values()) - 1.0) > 1e-12:
        raise ValueError("prior must sum to 1")

    out: Dict[str, List[Tuple[str, float]]] = {x: [] for x in node_tuple}
    outdeg: Dict[str, float] = {x: 0.0 for x in node_tuple}
    for s, d, w in edges:
        w = float(w)
        if s not in p or d not in p:
            raise ValueError("edge references unknown node: %s->%s" % (s, d))
        if not math.isfinite(w) or w <= 0.0:
            raise ValueError("edge weight must be finite and > 0: %s->%s" % (s, d))
        out[s].append((d, w))
        outdeg[s] += w

    r = dict(p)
    rounds = 0
    delta = float("inf")
    converged = False
    for _ in range(max_rounds):
        rounds += 1
        dangling = sum(r[s] for s in node_tuple if outdeg[s] == 0.0)
        base = 1.0 - damping + damping * dangling
        nxt = {x: base * p[x] for x in node_tuple}
        for s in node_tuple:
            if outdeg[s]:
                share = damping * r[s] / outdeg[s]
                for d, w in out[s]:
                    nxt[d] += share * w
        delta = sum(abs(nxt[x] - r[x]) for x in node_tuple)
        r = nxt
        if delta <= tol:
            converged = True
            break

    return {
        "standing": {x: r[x] for x in node_tuple},
        "converged": converged,
        "rounds": rounds,
        "delta_l1": delta,
        "l1_error_bound": damping * delta / (1.0 - damping),
        "damping": damping,
        "tol": tol,
        "prior": dict(p),
        "edge_digest": edge_digest(node_tuple, edges),
        "note": "relative standing only; not qualification, not reliability, "
                "not entitlement; consume only if converged is True",
    }


def reorder(chain: Sequence[str], standing: Dict[str, float],
            max_moves: int = 2) -> List[str]:
    """Fail-closed, clamped permutation of an ALREADY-QUALIFIED chain.

    Standing may only tie-break or nudge ordering inside a tier that earned
    its qualification elsewhere. All guards fail closed (the original order
    is returned unchanged): any chain id missing from standing; any standing
    id absent from the chain (a partial graph is refused); negative clamps;
    and more than max_moves displaced positions (churn clamp). Ties keep the
    current order (stable sort).
    """
    result = list(chain)
    if max_moves < 0:
        return result
    if len(set(standing)) != len(standing):
        return result
    if any(x not in standing for x in result):
        return result
    if any(k not in set(result) for k in standing):
        return result
    ordered = sorted(result, key=lambda x: (-float(standing[x]), result.index(x)))
    moves = sum(1 for a, b in zip(ordered, result) if a != b)
    if moves > max_moves:
        return result
    return ordered