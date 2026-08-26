import unittest

from aceval.kernel_v2 import (
    KernelV2Error,
    build_attempt_verdict,
    build_case_aggregates,
    compare_candidates,
    compile_diagnosis_graph,
    convergence_state,
)


def evidence(case_id="case-1", *, exact=True, status="completed", path_status="pass"):
    return {
        "case_id": case_id,
        "run_status": status,
        "error": None,
        "artifact": "/tmp/%s.json" % case_id,
        "exact_expected_match": exact,
        "path_conformance": {
            "status": path_status,
            "coverage": 1.0 if path_status == "pass" else 0.25,
            "reason": "path verdict",
        },
    }


class KernelV2Tests(unittest.TestCase):
    def test_hard_path_failure_overrides_an_output_pass(self):
        verdict = build_attempt_verdict(
            {"id": "case-1"},
            evidence(path_status="fail"),
            {"case_id": "case-1", "status": "pass", "reason": "output matched"},
            attempt_id="primary",
            run_context_hash="sha256:test",
        )
        self.assertEqual("fail", verdict.status)
        self.assertFalse(verdict.hard_pass)
        self.assertEqual("fail", next(item for item in verdict.dimensions if item.dimension == "procedure").status)

    def test_attempt_exposes_grounding_runtime_and_optional_efficiency_dimensions(self):
        case = {"id": "budgeted", "metadata": {"max_total_tokens": 100}}
        result = {"case_id": "budgeted", "status": "pass", "reason": "grounded", "evidence_refs": ["artifact.json"]}
        item = evidence("budgeted", exact=None)
        item["usage"] = {"total_tokens": 120}
        item["trace"] = {"errors": []}
        verdict = build_attempt_verdict(case, item, result, attempt_id="a1", run_context_hash="ctx")
        dimensions = {dimension.dimension: dimension for dimension in verdict.dimensions}
        self.assertEqual("pass", dimensions["grounding"].status)
        self.assertEqual("pass", dimensions["runtime"].status)
        self.assertEqual("fail", dimensions["efficiency"].status)

    def test_missing_artifact_is_not_evaluable(self):
        item = evidence()
        item["artifact"] = None
        verdict = build_attempt_verdict(
            {"id": "case-1"}, item,
            {"case_id": "case-1", "status": "pass", "reason": "claimed pass"},
            attempt_id="primary", run_context_hash="sha256:test",
        )
        self.assertEqual("not_evaluable", verdict.status)
        self.assertIn("attempt.artifact_missing", verdict.evidence_validity.reason_codes)

    def test_stable_pass_requires_all_k_attempts(self):
        case = {"id": "case-1", "expected_output": "ok"}
        result = {"case_id": "case-1", "status": "pass", "reason": "oracle", "evidence_refs": []}
        first = build_case_aggregates([case], [result], [evidence()], required_k=2)
        self.assertFalse(first[0].stable_pass)
        verified = build_case_aggregates(
            [case], [result], [evidence()], [evidence()],
            verification_results={"case-1": result}, required_k=2,
        )
        self.assertTrue(verified[0].stable_pass)
        self.assertEqual(1.0, verified[0].pass_power_k)

    def test_semantic_verification_is_never_inferred_from_completed_transport(self):
        aggregate = build_case_aggregates(
            [{"id": "semantic"}],
            [{"case_id": "semantic", "status": "pass", "reason": "semantic judge"}],
            [evidence("semantic", exact=None)],
            [evidence("semantic", exact=None)],
            required_k=2,
        )[0]
        self.assertEqual("not_evaluable", aggregate.attempts[1].status)
        self.assertFalse(aggregate.stable_pass)

    def test_comparison_rejects_any_stable_pass_regression(self):
        previous = [{"case_id": "a", "stable_pass": True, "pass_rate": 1.0}]
        current = [{"case_id": "a", "stable_pass": False, "pass_rate": 0.5}]
        comparison = compare_candidates(
            current, previous,
            champion_id="a" * 40,
            challenger_id="b" * 40,
            minimum_effect=0.01,
        )
        self.assertFalse(comparison["accepted"])
        self.assertEqual(["a"], comparison["hard_regression_case_ids"])

    def test_diagnosis_graph_is_stable_and_distinguishes_facts_from_hypotheses(self):
        args = (
            [{"case_id": "a", "status": "fail", "reason": "expected output missing", "evidence_refs": ["run.json"]}],
            [{"id": "outcome", "case_ids": ["a"], "root_cause": "instruction ambiguity", "skill_change_authorized": True}],
            [{"target": "SKILL.md", "change": "clarify output", "why": "oracle failed", "case_ids": ["a"]}],
            [],
        )
        first = compile_diagnosis_graph(*args)
        second = compile_diagnosis_graph(*args)
        self.assertEqual(first["graph_hash"], second["graph_hash"])
        self.assertEqual("fact", first["clusters"][0]["facts"][0]["source"])
        self.assertEqual("inference", first["clusters"][0]["hypothesis_source"])

    def test_convergence_requires_target_or_bounded_stop_condition(self):
        open_state = convergence_state(
            case_aggregates=[{"stable_pass": False}], failed_case_ids=["a"], comparison=None,
            round_number=2, max_rounds=5, recent_graph_hashes=["one", "two"], patience=2,
        )
        self.assertFalse(open_state["converged"])
        cycle = convergence_state(
            case_aggregates=[{"stable_pass": False}], failed_case_ids=["a"], comparison=None,
            round_number=3, max_rounds=5, recent_graph_hashes=["same", "same"], patience=2,
        )
        self.assertTrue(cycle["converged"])
        self.assertEqual("cycle_detected", cycle["reason"])

    def test_non_json_values_are_rejected(self):
        with self.assertRaises(KernelV2Error):
            build_case_aggregates(
                [{"id": "case-1"}],
                [{"case_id": "case-1", "status": "pass", "reason": "ok"}],
                [evidence()],
                run_context={"bad": float("nan")},
            )


if __name__ == "__main__":
    unittest.main()
