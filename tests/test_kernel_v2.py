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

    def test_incomplete_log_without_reason_is_not_evaluable(self):
        item = evidence("incomplete-log", exact=True)
        item["log_completeness"] = {"complete": False}
        verdict = build_attempt_verdict(
            {"id": "incomplete-log"},
            item,
            {
                "case_id": "incomplete-log",
                "status": "pass",
                "reason": "claimed pass",
                "evidence_refs": [item["artifact"]],
            },
            attempt_id="primary",
            run_context_hash="sha256:test",
        )
        self.assertEqual("not_evaluable", verdict.status)
        self.assertIn("attempt.log_incomplete", verdict.evidence_validity.reason_codes)

    def test_complete_trace_after_remote_terminal_error_remains_analyzable(self):
        item = evidence("completed-trace-error", exact=None, status="failed")
        item.update({
            "error": "agent process exited after writing the review",
            "trace_complete": True,
            "output_chars": 128,
            "trace": {"event_count": 6, "errors": []},
        })
        verdict = build_attempt_verdict(
            {"id": "completed-trace-error"},
            item,
            {
                "case_id": "completed-trace-error",
                "status": "fail",
                "reason": "review output was produced before the terminal error",
                "evidence_refs": [item["artifact"]],
            },
            attempt_id="primary",
            run_context_hash="sha256:test",
        )
        self.assertEqual("fail", verdict.status)
        self.assertTrue(verdict.evidence_validity.evaluable)
        self.assertEqual("partial", verdict.evidence_validity.status)
        self.assertIn("attempt.runtime_error_after_trace", verdict.evidence_validity.reason_codes)

    def test_stable_pass_requires_all_k_attempts(self):
        case = {"id": "case-1", "expected_output": "ok"}
        result = {"case_id": "case-1", "status": "pass", "reason": "oracle", "evidence_refs": []}
        first = build_case_aggregates([case], [result], [evidence()], required_k=2)
        self.assertEqual("pass", first[0].status)
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

    def test_semantic_pass_without_evidence_reference_is_not_evaluable(self):
        verdict = build_attempt_verdict(
            {
                "id": "semantic",
                "metadata": {
                    "expectation_mode": "semantic",
                    "aceval_test": {
                        "oracle_ready": True,
                        "oracle_trust": "human_confirmed",
                    },
                },
            },
            evidence("semantic", exact=None),
            {
                "case_id": "semantic",
                "status": "pass",
                "reason": "model claimed success",
                "evidence_refs": [],
            },
            attempt_id="primary",
            run_context_hash="sha256:test",
        )
        self.assertEqual("not_evaluable", verdict.status)
        grounding = next(item for item in verdict.dimensions if item.dimension == "grounding")
        self.assertTrue(grounding.hard)
        self.assertEqual("not_evaluable", grounding.status)

    def test_model_proposed_semantic_pass_without_reference_stays_exploratory(self):
        verdict = build_attempt_verdict(
            {
                "id": "exploratory",
                "metadata": {
                    "expectation_mode": "model_proposed",
                    "aceval_test": {
                        "oracle_ready": False,
                        "oracle_trust": "model_proposed",
                    },
                },
            },
            evidence("exploratory", exact=None),
            {
                "case_id": "exploratory",
                "status": "pass",
                "reason": "model suggestion",
                "evidence_refs": [],
            },
            attempt_id="primary",
            run_context_hash="sha256:test",
        )
        self.assertEqual("pass", verdict.status)
        grounding = next(item for item in verdict.dimensions if item.dimension == "grounding")
        self.assertFalse(grounding.hard)
        self.assertEqual("not_evaluable", grounding.status)

    def test_malformed_formal_grading_cannot_fall_back_to_a_passing_result(self):
        case = {
            "id": "formal-malformed",
            "metadata": {
                "expectation_mode": "evalpack_graders",
                "evalpack_grader_ids": ["output-schema"],
            },
        }
        for formal in (
            {"status": "pass", "grades": ["not an object"]},
            {"status": "pass", "grades": [{"grader_id": "schema", "status": "pass", "score": 2.0}]},
            "pass",
        ):
            with self.subTest(formal=formal):
                item = evidence("formal-malformed", exact=True)
                item["formal_grading"] = formal
                verdict = build_attempt_verdict(
                    case,
                    item,
                    {"case_id": "formal-malformed", "status": "pass", "reason": "claimed pass"},
                    attempt_id="malformed",
                    run_context_hash="sha256:test",
                )
                self.assertEqual("not_evaluable", verdict.status)
                contract = next(
                    item for item in verdict.dimensions
                    if item.dimension == "outcome:formal_grading_contract"
                )
                self.assertTrue(contract.hard)
                self.assertEqual("not_evaluable", contract.status)

    def test_attempt_without_hard_dimensions_is_not_a_hard_pass(self):
        verdict = build_attempt_verdict(
            {
                "id": "soft-only",
                "metadata": {
                    "expectation_mode": "model_proposed",
                    "aceval_test": {
                        "oracle_ready": False,
                        "oracle_trust": "model_proposed",
                    },
                },
            },
            evidence("soft-only", exact=None),
            {
                "case_id": "soft-only",
                "status": "pass",
                "reason": "model suggestion",
                "evidence_refs": ["/tmp/soft-only.json"],
            },
            attempt_id="primary",
            run_context_hash="sha256:test",
        )
        self.assertEqual("pass", verdict.status)
        self.assertFalse(verdict.hard_pass)

    def test_human_confirmed_semantic_reference_must_anchor_attempt_artifact(self):
        case = {
            "id": "semantic",
            "metadata": {
                "expectation_mode": "semantic",
                "aceval_test": {"oracle_ready": True, "oracle_trust": "human_confirmed"},
            },
        }
        item = evidence("semantic", exact=None)
        invalid = build_attempt_verdict(
            case,
            item,
            {"case_id": "semantic", "status": "pass", "reason": "claimed", "evidence_refs": ["other.json"]},
            attempt_id="invalid-ref",
            run_context_hash="sha256:test",
        )
        self.assertEqual("not_evaluable", invalid.status)
        valid = build_attempt_verdict(
            case,
            item,
            {
                "case_id": "semantic",
                "status": "pass",
                "reason": "anchored",
                "evidence_refs": ["/tmp/semantic.json#trace[0]"],
            },
            attempt_id="valid-ref",
            run_context_hash="sha256:test",
        )
        self.assertEqual("pass", valid.status)
        self.assertEqual(
            "pass",
            next(item for item in valid.dimensions if item.dimension == "grounding").status,
        )
        basename = build_attempt_verdict(
            case,
            item,
            {
                "case_id": "semantic",
                "status": "pass",
                "reason": "anchored by compact artifact name",
                "evidence_refs": ["semantic.json#terminal.message"],
            },
            attempt_id="basename-ref",
            run_context_hash="sha256:test",
        )
        self.assertEqual("pass", basename.status)

    def test_malformed_formal_grading_cannot_be_overridden_by_claimed_pass(self):
        item = evidence("malformed-formal", exact=None)
        item["formal_grading"] = {
            "status": "pass",
            # A producer must not be able to turn an absent/malformed grader
            # receipt into a trusted semantic pass.
            "grades": [None],
        }
        verdict = build_attempt_verdict(
            {"id": "malformed-formal"},
            item,
            {
                "case_id": "malformed-formal",
                "status": "pass",
                "reason": "untrusted claimed pass",
                "evidence_refs": [item["artifact"]],
            },
            attempt_id="primary",
            run_context_hash="sha256:test",
        )
        self.assertEqual("not_evaluable", verdict.status)
        contract = next(
            dimension
            for dimension in verdict.dimensions
            if dimension.dimension == "outcome:formal_grading_contract"
        )
        self.assertTrue(contract.hard)
        self.assertEqual("not_evaluable", contract.status)

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

    def test_comparison_requires_valid_evidence_on_both_sides(self):
        previous = [
            {
                "case_id": "a",
                "status": "pass",
                "stable_pass": True,
                "pass_rate": 1.0,
                "attempts": [
                    {"evidence_validity": {"status": "invalid"}, "dimensions": []}
                ],
            }
        ]
        current = [
            {
                "case_id": "a",
                "status": "pass",
                "stable_pass": True,
                "pass_rate": 1.0,
                "attempts": [
                    {"evidence_validity": {"status": "valid"}, "dimensions": []}
                ],
            }
        ]

        comparison = compare_candidates(
            current,
            previous,
            champion_id="a" * 40,
            challenger_id="b" * 40,
            minimum_effect=0.0,
        )

        self.assertFalse(comparison["accepted"])
        self.assertEqual(["a"], comparison["champion_evidence_blocked_case_ids"])
        self.assertEqual([], comparison["challenger_evidence_blocked_case_ids"])
        self.assertIn("insufficient_evidence", comparison["reasons"])

    def test_comparison_rejects_forged_hard_pass_without_hard_dimension(self):
        # A producer must not be able to mark an aggregate as stable merely by
        # setting ``attempt.hard_pass``.  The flag is derived from at least one
        # valid hard dimension and must be checked at the comparison boundary.
        def aggregate():
            return {
                "case_id": "forged",
                "required_k": 1,
                "status": "pass",
                "stable_pass": True,
                "pass_rate": 1.0,
                "attempt_count": 1,
                "evaluable_attempt_count": 1,
                "successes": 1,
                "attempts": [
                    {
                        "case_id": "forged",
                        "status": "pass",
                        "hard_pass": True,
                        "evidence_validity": {"status": "valid"},
                        "dimensions": [
                            {
                                "dimension": "outcome",
                                "status": "pass",
                                "score": 1.0,
                                "hard": False,
                                "grader_id": "soft",
                            }
                        ],
                    }
                ],
            }

        comparison = compare_candidates(
            [aggregate()],
            [aggregate()],
            champion_id="a" * 40,
            challenger_id="b" * 40,
            minimum_effect=0.0,
        )
        self.assertFalse(comparison["accepted"])
        self.assertIn("malformed_aggregate", comparison["reasons"])
        self.assertEqual(["forged"], comparison["malformed_aggregate_case_ids"])

    def test_comparison_rejects_duplicate_attempt_ids(self):
        def aggregate():
            return {
                "case_id": "duplicate-attempt",
                "required_k": 2,
                "status": "pass",
                "stable_pass": True,
                "pass_rate": 1.0,
                "attempt_count": 2,
                "evaluable_attempt_count": 2,
                "successes": 2,
                "attempts": [
                    {
                        "case_id": "duplicate-attempt",
                        "attempt_id": "same-id",
                        "status": "pass",
                        "hard_pass": True,
                        "evidence_validity": {"status": "valid"},
                        "dimensions": [{
                            "dimension": "outcome",
                            "status": "pass",
                            "hard": True,
                            "score": 1.0,
                        }],
                    },
                    {
                        "case_id": "duplicate-attempt",
                        "attempt_id": "same-id",
                        "status": "pass",
                        "hard_pass": True,
                        "evidence_validity": {"status": "valid"},
                        "dimensions": [{
                            "dimension": "outcome",
                            "status": "pass",
                            "hard": True,
                            "score": 1.0,
                        }],
                    },
                ],
            }

        result = compare_candidates(
            [aggregate()],
            [aggregate()],
            champion_id="old",
            challenger_id="new",
            minimum_effect=0.0,
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(["duplicate-attempt"], result["malformed_aggregate_case_ids"])
        self.assertIn("malformed_aggregate", result["reasons"])

    def test_comparison_rejects_different_case_sets(self):
        previous = [{"case_id": "a", "stable_pass": True, "pass_rate": 1.0}]
        current = [
            {"case_id": "a", "stable_pass": True, "pass_rate": 1.0},
            {"case_id": "b", "stable_pass": True, "pass_rate": 1.0},
        ]

        comparison = compare_candidates(
            current,
            previous,
            champion_id="a" * 40,
            challenger_id="b" * 40,
            minimum_effect=0.0,
        )

        self.assertFalse(comparison["accepted"])
        self.assertFalse(comparison["case_set_comparable"])
        self.assertEqual(["b"], comparison["challenger_only_case_ids"])
        self.assertIn("case_set_mismatch", comparison["reasons"])

    def test_comparison_minimum_effect_cannot_be_bypassed_by_one_improved_case(self):
        previous = [
            {"case_id": "improved", "stable_pass": False, "pass_rate": 0.0},
            {"case_id": "degraded", "stable_pass": True, "pass_rate": 1.0},
        ]
        current = [
            {"case_id": "improved", "stable_pass": True, "pass_rate": 0.2},
            {"case_id": "degraded", "stable_pass": True, "pass_rate": 0.0},
        ]
        comparison = compare_candidates(
            current,
            previous,
            champion_id="a" * 40,
            challenger_id="b" * 40,
            minimum_effect=0.05,
        )
        self.assertFalse(comparison["accepted"])
        self.assertLess(comparison["paired_mean_delta"], 0.05)
        self.assertIn("minimum_effect_not_met", comparison["reasons"])

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

    def test_candidate_comparison_rejects_mismatched_case_manifest(self):
        def aggregate(revision, grader="outcome"):
            return {
                "case_id": "case-1",
                "case_revision": revision,
                "required_k": 2,
                "stable_pass": True,
                "pass_rate": 1.0,
                "status": "pass",
                "attempts": [{
                    "status": "pass",
                    "evidence_validity": {"status": "valid"},
                    "dimensions": [{"dimension": "outcome", "grader_id": grader, "hard": True, "status": "pass", "score": 1.0}],
                }],
            }

        result = compare_candidates(
            [aggregate("sha256:new", grader="different")],
            [aggregate("sha256:old")],
            champion_id="old",
            challenger_id="new",
            minimum_effect=0.0,
        )
        self.assertFalse(result["accepted"])
        self.assertFalse(result["case_set_comparable"])
        self.assertEqual(["case-1"], result["attempt_manifest_mismatch_case_ids"])
        self.assertIn("attempt_manifest_mismatch", result["reasons"])

    def test_candidate_comparison_keeps_malformed_rows_visible_without_authorizing_them(self):
        def aggregate(*, status="pass", case_id="case-1"):
            return {
                "case_id": case_id,
                "status": status,
                "stable_pass": True,
                "pass_rate": 1.0,
                "required_k": 1,
                "attempts": [{
                    "case_id": case_id,
                    "status": "pass",
                    "evidence_validity": {"status": "valid"},
                    "dimensions": [{
                        "dimension": "outcome",
                        "status": "pass",
                        "hard": True,
                        "score": 1.0,
                    }],
                }],
            }

        result = compare_candidates(
            [aggregate(status="corrupted")],
            [aggregate()],
            champion_id="old",
            challenger_id="new",
            minimum_effect=0.0,
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(["case-1"], result["comparable_case_ids"])
        self.assertEqual([], result["challenger_only_case_ids"])
        self.assertEqual([], result["champion_only_case_ids"])
        self.assertEqual(["case-1"], result["malformed_aggregate_case_ids"])
        self.assertIn("malformed_aggregate", result["reasons"])

    def test_candidate_comparison_reports_case_set_and_malformed_diagnostics_together(self):
        valid = {
            "case_id": "case-1",
            "status": "pass",
            "stable_pass": True,
            "pass_rate": 1.0,
        }
        malformed_extra = {"case_id": "case-2", "stable_pass": "yes"}
        result = compare_candidates(
            [valid, malformed_extra],
            [valid],
            champion_id="old",
            challenger_id="new",
            minimum_effect=0.0,
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(["case-1"], result["comparable_case_ids"])
        self.assertEqual(["case-2"], result["challenger_only_case_ids"])
        self.assertEqual(["case-2"], result["malformed_aggregate_case_ids"])
        self.assertIn("case_set_mismatch", result["reasons"])
        self.assertIn("malformed_aggregate", result["reasons"])

    def test_candidate_comparison_reports_null_nested_receipts_without_crashing(self):
        malformed = {
            "case_id": "case-1",
            "status": "pass",
            "stable_pass": True,
            "pass_rate": 1.0,
            "attempts": None,
        }
        result = compare_candidates(
            [malformed],
            [malformed],
            champion_id="old",
            challenger_id="new",
            minimum_effect=0.0,
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(["case-1"], result["comparable_case_ids"])
        self.assertEqual(["case-1"], result["malformed_aggregate_case_ids"])
        self.assertIn("malformed_aggregate", result["reasons"])

    def test_candidate_comparison_rejects_forged_pass_rate(self):
        """The paired delta must come from the attempt receipts, not a free-form summary."""

        def aggregate(rate):
            return {
                "case_id": "case-1",
                "status": "pass",
                "stable_pass": True,
                "pass_rate": rate,
                "required_k": 1,
                "attempts": [{
                    "case_id": "case-1",
                    "attempt_id": "attempt-1",
                    "run_context_hash": "sha256:context",
                    "status": "pass",
                    "hard_pass": True,
                    "evidence_validity": {"status": "valid"},
                    "dimensions": [{
                        "dimension": "outcome",
                        "status": "pass",
                        "hard": True,
                        "score": 1.0,
                    }],
                }],
            }

        result = compare_candidates(
            [aggregate(1.0)],
            [aggregate(0.0)],
            champion_id="old",
            challenger_id="new",
            minimum_effect=0.1,
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(["case-1"], result["malformed_aggregate_case_ids"])
        self.assertIn("malformed_aggregate", result["reasons"])

    def test_candidate_comparison_requires_stable_challenger(self):
        """A candidate that has not met its repeat budget cannot be promoted."""

        def aggregate(stable, rate):
            return {
                "case_id": "case-1",
                "status": "fail" if not stable else "pass",
                "stable_pass": stable,
                "pass_rate": rate,
                "required_k": 2,
                "attempts": [{
                    "case_id": "case-1",
                    "attempt_id": "attempt-1",
                    "run_context_hash": "sha256:context",
                    "status": "pass",
                    "hard_pass": True,
                    "evidence_validity": {"status": "valid"},
                    "dimensions": [{
                        "dimension": "outcome",
                        "status": "pass",
                        "hard": True,
                        "score": 1.0,
                    }],
                }],
            }

        result = compare_candidates(
            [aggregate(False, 1.0)],
            [aggregate(False, 0.0)],
            champion_id="old",
            challenger_id="new",
            minimum_effect=0.1,
        )
        self.assertFalse(result["accepted"])
        self.assertIn("insufficient_stability", result["reasons"])


if __name__ == "__main__":
    unittest.main()
