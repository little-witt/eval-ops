import asyncio
import unittest

from aceval.catx_bindings import (
    CATX_EXECUTION_BINDING_API_VERSION,
    CatxBindingError,
    CatxBindingEvidence,
    CatxExecutionBinding,
)
from aceval.catx_runtime import CatxRuntimeAdapter
from aceval.contracts import (
    PreparedScenario,
    RunContext,
    RuntimeResult,
    SubjectSnapshot,
)


SUBJECT_DIGEST = "a" * 64


def binding():
    return CatxExecutionBinding(
        api_version=CATX_EXECUTION_BINDING_API_VERSION,
        subject_hash="sha256:" + SUBJECT_DIGEST,
        skill_ref="skillhub://review@1",
        repository_ref="catx-repository://fixture",
        repository_hash="sha256:" + "b" * 64,
        base_commit="1" * 40,
        head_commit="2" * 40,
    )


class Bundle:
    def to_runtime_result(self):
        return RuntimeResult(final_output='{"findings": []}', metadata={"source": "catx"})


class FakeClient:
    def __init__(self, match=True):
        self.match = match
        self.bound = None

    def start_session(self, request, *, binding=None):
        self.bound = binding
        return "session-1"

    def poll_session(self, session_id):
        return {"status": "COMPLETED"}

    def verify_binding(self, session_id, requested):
        return CatxBindingEvidence(
            verified=True,
            requested_binding_hash=requested.binding_hash,
            actual_subject_hash=(requested.subject_hash if self.match else "sha256:" + "c" * 64),
            actual_repository_hash=requested.repository_hash,
            actual_base_commit=requested.base_commit,
            actual_head_commit=requested.head_commit,
        )

    def fetch_session(self, session_id):
        return Bundle()


class CatxRuntimeAdapterTests(unittest.TestCase):
    def execute(self, client):
        runtime = CatxRuntimeAdapter(
            client,
            lambda prepared, subject, context: binding(),
            poll_interval_seconds=0.001,
        )
        return asyncio.run(
            runtime.execute(
                PreparedScenario(prompt="review", metadata={"scenario_id": "case-1"}),
                SubjectSnapshot(
                    kind="skill",
                    uri="skill://review",
                    content_hash=SUBJECT_DIGEST,
                ),
                RunContext(run_id="run-1"),
            )
        )

    def test_runtime_records_verified_binding_evidence(self):
        result = self.execute(FakeClient())
        self.assertTrue(result.metadata["catx_binding_verified"])
        self.assertEqual(binding().binding_hash, result.metadata["catx_binding_hash"])

    def test_runtime_rejects_observed_binding_drift(self):
        with self.assertRaisesRegex(CatxBindingError, "subject_hash"):
            self.execute(FakeClient(match=False))


if __name__ == "__main__":
    unittest.main()
