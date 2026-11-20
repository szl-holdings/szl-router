"""Offline regressions for the inert trust-rank diagnostic engine.

Hermetic: pure stdlib, no network, no keys, no environment. All reference
values below come from the 2026-11-20 three-way verified study of the
Wikipedia 11-page PageRank example (independently recomputed by Kimi
K3.1 Pro, GLM-5.3, and the estate's own stdlib derivation).
"""
import unittest

from szl_router import trust_rank

PAGES = list("ABCDEFGHIJK")
LINKS = ["B->C", "C->B", "D->A", "D->B", "E->B", "E->D", "E->F", "F->B",
         "F->E", "G->B", "G->E", "H->B", "H->E", "I->B", "I->E", "J->E", "K->E"]


def wiki_graph():
    edges = []
    for l in LINKS:
        s, d = l.split("->")
        edges.append((s, d, 1.0))
    return PAGES, edges


class TrustRankReferenceTests(unittest.TestCase):
    """The engine reproduces the three-way verified Wikipedia reference."""

    def test_wikipedia_fixed_point_values(self):
        nodes, edges = wiki_graph()
        out = trust_rank.rank(nodes, edges)
        st = out["standing"]
        self.assertTrue(out["converged"])
        self.assertAlmostEqual(st["B"], 0.384401, places=5)
        self.assertAlmostEqual(st["C"], 0.342910, places=5)
        self.assertAlmostEqual(st["E"], 0.080886, places=5)
        self.assertAlmostEqual(st["D"], 0.039087, places=5)
        self.assertAlmostEqual(st["F"], st["D"], places=10)
        self.assertAlmostEqual(st["A"], 0.032781, places=5)
        # No-inlink floor: teleport 1.3636% + recycled dangling A 0.2533%.
        for x in "GHIJK":
            self.assertAlmostEqual(st[x], 0.016169, places=5)
        self.assertAlmostEqual(sum(st.values()), 1.0, places=9)

    def test_disclosure_fields_and_bounds(self):
        nodes, edges = wiki_graph()
        out = trust_rank.rank(nodes, edges, tol=1e-13)
        for key in ("standing", "converged", "rounds", "delta_l1",
                    "l1_error_bound", "damping", "tol", "prior",
                    "edge_digest", "note"):
            self.assertIn(key, out)
        self.assertLessEqual(out["delta_l1"], out["tol"])
        # Guaranteed remaining-error bound: damping/(1-damping) * delta.
        self.assertAlmostEqual(out["l1_error_bound"],
                               0.85 * out["delta_l1"] / 0.15, places=20)
        # Same graph in -> same digest out (published tables are attributable).
        again = trust_rank.rank(nodes, edges)
        self.assertEqual(out["edge_digest"], again["edge_digest"])
        self.assertTrue(50 < out["rounds"] < 500)  # ~0.85/round decay

    def test_no_edges_returns_prior_exactly(self):
        # Nothing dangles off a prior of 1: with no edges the fixed point IS
        # the prior (dangling mass all returns to the prior).
        prior = {"X": 0.5, "Y": 0.3, "Z": 0.2}
        out = trust_rank.rank(["X", "Y", "Z"], [], prior=prior)
        self.assertTrue(out["converged"])
        for k, v in prior.items():
            self.assertAlmostEqual(out["standing"][k], v, places=12)


class SybilBudgetTests(unittest.TestCase):
    """FIXED prior budget: no fresh standing for unvetted identities."""

    def test_vetted_prior_only_no_inflow_means_zero_standing(self):
        # pi(S) = 0 (S1, S2 absent from the prior) and no receipt-backed
        # inflow into {S1, S2}: the study's sybil bound collapses to
        # r(S) = 0, no matter how many internal edges S mints.
        nodes = ["rootA", "rootB", "S1", "S2"]
        edges = [("S1", "S2", 1.0), ("S2", "S1", 1.0),
                 ("S1", "rootA", 5.0)]  # attack edges OUT of S: still no inflow.
        prior = {"rootA": 0.6, "rootB": 0.4}
        out = trust_rank.rank(nodes, edges, prior=prior)
        self.assertTrue(out["converged"])
        self.assertAlmostEqual(out["standing"]["S1"], 0.0, places=12)
        self.assertAlmostEqual(out["standing"]["S2"], 0.0, places=12)
        self.assertAlmostEqual(sum(out["standing"].values()), 1.0, places=9)

    def test_inflow_is_damped_and_capped_by_sender_standing(self):
        # A single vetted node vouching for one sybil leaks only
        # damping * r(sender) distributed by edge weight: no amplification.
        nodes = ["rootA", "rootB", "S1"]
        edges = [("rootA", "S1", 1.0)]
        prior = {"rootA": 0.5, "rootB": 0.5}
        out = trust_rank.rank(nodes, edges, prior=prior)
        rA, rS1 = out["standing"]["rootA"], out["standing"]["S1"]
        self.assertLess(rS1, 0.85 * prior["rootA"])  # strictly below the leak cap


class ReorderGuardTests(unittest.TestCase):
    """reorder() is fail-closed: original order on any anomaly."""

    def test_within_clamp_reorders_only_qualified_chain(self):
        standing = {"a": 0.2, "b": 0.5, "c": 0.3}
        out = trust_rank.reorder(["a", "b", "c"], standing, max_moves=3)
        self.assertEqual(out, ["b", "c", "a"])

    def test_excess_churn_returns_original(self):
        standing = {"a": 0.1, "b": 0.6, "c": 0.3}
        self.assertEqual(trust_rank.reorder(["a", "b", "c"], standing, max_moves=1),
                         ["a", "b", "c"])

    def test_missing_or_unknown_ids_return_original(self):
        self.assertEqual(trust_rank.reorder(["a", "b"], {"a": 1.0}, max_moves=2),
                         ["a", "b"])
        self.assertEqual(trust_rank.reorder(["a"], {"a": 0.5, "z": 0.5}, max_moves=2),
                         ["a"])

    def test_ties_keep_current_order(self):
        standing = {"a": 0.5, "b": 0.5, "c": 0.6}
        self.assertEqual(trust_rank.reorder(["a", "b", "c"], standing, max_moves=3),
                         ["c", "a", "b"])
        self.assertEqual(trust_rank.reorder(["b", "a", "c"], standing, max_moves=3),
                         ["c", "b", "a"])


class InputValidationTests(unittest.TestCase):
    def test_bad_inputs_raise(self):
        with self.assertRaises(ValueError):
            trust_rank.rank([], [])
        with self.assertRaises(ValueError):
            trust_rank.rank(["a", "a"], [])
        with self.assertRaises(ValueError):
            trust_rank.rank(["a"], [("a", "b", 1.0)])
        with self.assertRaises(ValueError):
            trust_rank.rank(["a"], [], prior={"a": 0.5})
        with self.assertRaises(ValueError):
            trust_rank.rank(["a"], [("b", "a", 0.0)], prior={"a": 1.0, "b": 0.0})
        with self.assertRaises(ValueError):
            trust_rank.rank(["a"], [], prior={"a": 1.0}, damping=1.0)


if __name__ == "__main__":
    unittest.main()