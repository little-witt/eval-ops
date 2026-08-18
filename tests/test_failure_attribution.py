import unittest

from aceval.contracts import GradeResult, GradeStatus, RunObservation, TraceEvent
from aceval.failure_attribution import (
    EvidenceStrength,
    FailureAttributor,
    PatchDecision,
)


class FailureAttributionTest(unittest.TestCase):
    def setUp(self):
        self.attributor = FailureAttributor()

    def hard_fail(self, message="expected output did not match"):
        return GradeResult(
            grader_id="expected-output",
            status=GradeStatus.FAIL,
            hard=True,
            message=message,
            evidence=({"path": "$.answer"},),
        )

    def test_clean_hard_failure_allows_constrained_skill_intervention(self):
        report = self.attributor.attribute(
            "case-1",
            observation=RunObservation(output={"answer": 1}),
            grades=(self.hard_fail(),),
        )

        self.assertEqual(PatchDecision.ALLOW_SKILL_INTERVENTION, report.patch_decision)
        self.assertTrue(report.skill_patch_allowed)
        self.assertEqual(1, len(report.eligible_skill_failures))
        card = report.failure_cards[0]
        self.assertEqual("agent_behavior", card.observed_component)
        self.assertEqual("skill_instruction", card.remediation_surface)
        self.assertEqual(EvidenceStrength.INFERRED, card.confidence)

    def test_runtime_error_denies_skill_intervention(self):
        report = self.attributor.attribute(
            "case-runtime",
            observation=RunObservation(error="runtime_error: model bridge timed out"),
            error="runtime_error: model bridge timed out",
            grades=(
                GradeResult(
                    grader_id="schema",
                    status=GradeStatus.NOT_EVALUABLE,
                    missing=("runtime_observation",),
                ),
            ),
        )

        self.assertEqual(PatchDecision.DENY_SKILL_INTERVENTION, report.patch_decision)
        self.assertFalse(report.skill_patch_allowed)
        self.assertFalse(report.evaluable)
        self.assertIn("runtime.failure", report.blocked_reasons)

    def test_cli_command_not_found_is_direct_external_failure(self):
        observation = RunObservation(
            trace=(
                TraceEvent(
                    kind="tool_result",
                    seq=3,
                    tool="process_exec",
                    payload={
                        "ok": False,
                        "exit_code": 127,
                        "stderr_excerpt": "command not found",
                    },
                ),
            )
        )
        report = self.attributor.attribute("case-cli", observation=observation)

        self.assertEqual(PatchDecision.DENY_SKILL_INTERVENTION, report.patch_decision)
        card = report.failure_cards[0]
        self.assertEqual("cli.binary_not_found", card.reason_code)
        self.assertEqual("cli", card.observed_component)
        self.assertEqual(EvidenceStrength.DIRECT, card.confidence)

    def test_bad_tool_argument_requires_more_evidence(self):
        observation = RunObservation(
            trace=(
                TraceEvent(
                    kind="tool_result",
                    seq=2,
                    tool="read_file",
                    payload={"ok": False, "error": "path argument must be a string"},
                ),
            )
        )
        report = self.attributor.attribute("case-arg", observation=observation)

        self.assertEqual(PatchDecision.NEEDS_MORE_EVIDENCE, report.patch_decision)
        self.assertEqual("agent.bad_argument", report.failure_cards[0].reason_code)

    def test_expected_fault_does_not_block_recovery_failure(self):
        observation = RunObservation(
            trace=(
                TraceEvent(
                    kind="tool_result",
                    seq=4,
                    tool="process_exec",
                    payload={
                        "ok": False,
                        "error_type": "permission_denied",
                        "exit_code": 126,
                    },
                ),
            )
        )
        report = self.attributor.attribute(
            "case-recovery",
            observation=observation,
            grades=(self.hard_fail("partial artifact was left behind"),),
            metadata={
                "attribution": {
                    "expected_signals": [
                        {"code": "permission.denied", "tool": "process_exec"}
                    ]
                }
            },
        )

        self.assertEqual(PatchDecision.ALLOW_SKILL_INTERVENTION, report.patch_decision)
        self.assertEqual("recovery_failure", report.failure_cards[0].category)
        self.assertTrue(report.failure_cards[0].expected_fault)

    def test_missing_observation_completeness_denies_patch(self):
        report = self.attributor.attribute(
            "session-1",
            observation=RunObservation(output="done"),
            metadata={"missing_observation_fields": ("trace", "usage")},
        )

        self.assertEqual(PatchDecision.DENY_SKILL_INTERVENTION, report.patch_decision)
        self.assertEqual("evidence.missing", report.failure_cards[0].reason_code)

    def test_passing_scenario_has_no_failure_cards(self):
        report = self.attributor.attribute(
            "case-pass",
            observation=RunObservation(output={"ok": True}),
            grades=(
                GradeResult(
                    grader_id="schema",
                    status=GradeStatus.PASS,
                    hard=True,
                ),
            ),
        )
        self.assertEqual(PatchDecision.NONE, report.patch_decision)
        self.assertEqual((), report.failure_cards)
        self.assertTrue(report.evaluable)

    def test_failure_ids_are_stable(self):
        first = self.attributor.attribute(
            "case-stable",
            observation=RunObservation(output={}),
            grades=(self.hard_fail(),),
        )
        second = self.attributor.attribute(
            "case-stable",
            observation=RunObservation(output={}),
            grades=(self.hard_fail(),),
        )
        self.assertEqual(
            first.failure_cards[0].failure_id,
            second.failure_cards[0].failure_id,
        )


if __name__ == "__main__":
    unittest.main()
