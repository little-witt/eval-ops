import unittest

from aceval.iteration_kernel import _comparison_requires_more_evidence


class CandidateComparisonGateTests(unittest.TestCase):
    def test_incomplete_or_incomparable_result_keeps_challenger_pending(self):
        for reason in (
            "no_comparable_cases",
            "case_set_mismatch",
            "comparison_context_mismatch",
            "insufficient_evidence",
        ):
            with self.subTest(reason=reason):
                self.assertTrue(
                    _comparison_requires_more_evidence({"reasons": [reason]})
                )

    def test_hard_failure_remains_decisive_even_with_missing_evidence(self):
        self.assertFalse(
            _comparison_requires_more_evidence(
                {"reasons": ["insufficient_evidence", "hard_gate_failure"]}
            )
        )
        self.assertFalse(
            _comparison_requires_more_evidence(
                {"reasons": ["case_set_mismatch", "critical_regression"]}
            )
        )

    def test_sufficient_but_too_small_gain_is_a_real_rejection(self):
        self.assertFalse(
            _comparison_requires_more_evidence(
                {"reasons": ["minimum_effect_not_met"]}
            )
        )


if __name__ == "__main__":
    unittest.main()
