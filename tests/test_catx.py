import json
import os
import tempfile
import unittest
from pathlib import Path

from aceval.catx import (
    CATX_PROFILE_API_VERSION,
    CatxAgentClient,
    CatxAgentProfile,
    CatxProfileError,
    CatxRepositoryResourceProfile,
    CatxStreamError,
    iter_sse_json_events,
    last_effective_agent_message,
)
from aceval.connections import CompanyApiRequestError, HTTPResponse
from aceval.catx_bindings import (
    CATX_EXECUTION_BINDING_API_VERSION,
    CatxBindingEvidence,
    CatxExecutionBinding,
)


def profile_payload(**overrides):
    value = {
        "api_version": CATX_PROFILE_API_VERSION,
        "name": "catx-online",
        "base_url": "https://api.catx.test/api/v1",
        "api_key_env": "CATX_API_KEY",
        "user_mis_id_env": "USER_MIS_ID",
        "agent_id_env": "CATX_AGENT_ID",
        "environment_id_env": "CATX_ENV_ID",
        "vault_ids": ["vlt_test"],
        "default_title": "aceval test",
        "timeout_seconds": 12,
        "max_response_bytes": 4096,
        "events_limit": 200,
        "stream": {
            "base_url": "https://project.supabase.test",
            "api_key_env": "SUPABASE_ANON_KEY",
            "bearer_env": "SUPABASE_ACCESS_TOKEN",
        },
    }
    value.update(overrides)
    return value


def profile(**overrides):
    return CatxAgentProfile.from_mapping(profile_payload(**overrides))


def response(payload, status=200):
    return HTTPResponse(
        status_code=status,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected transport call")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSSETransport:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.chunks)


ENVIRONMENT = {
    "CATX_API_KEY": "catx-secret",
    "USER_MIS_ID": "tester",
    "CATX_AGENT_ID": "agent_test",
    "CATX_ENV_ID": "env_test",
    "SUPABASE_ANON_KEY": "anon-secret",
    "SUPABASE_ACCESS_TOKEN": "access-secret",
}


def execution_binding():
    return CatxExecutionBinding(
        api_version=CATX_EXECUTION_BINDING_API_VERSION,
        subject_hash="sha256:" + "a" * 64,
        skill_ref="skillhub://frontend-code-reviewer@1.2.3",
        repository_ref="catx-repository://repo-fixture",
        repository_hash="sha256:" + "b" * 64,
        base_commit="1" * 40,
        head_commit="2" * 40,
    )


class BindingAdapter:
    def __init__(self, mutate=False):
        self.mutate = mutate

    def augment_create_payload(self, base_payload, binding):
        value = dict(base_payload)
        value["skill_binding"] = {"ref": binding.skill_ref, "hash": binding.subject_hash}
        value["repository_mount"] = {"ref": binding.repository_ref}
        if self.mutate:
            value["agent"] = "different-agent"
        return value

    def verify_session(self, client, session_id, binding):
        return CatxBindingEvidence(
            verified=True,
            requested_binding_hash=binding.binding_hash,
            actual_subject_hash=binding.subject_hash,
            actual_repository_hash=binding.repository_hash,
            actual_base_commit=binding.base_commit,
            actual_head_commit=binding.head_commit,
            details={"session_id": session_id},
        )


class CatxProfileTest(unittest.TestCase):
    def test_loads_strict_profile_without_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "catx.json")
            path.write_text(json.dumps(profile_payload()), encoding="utf-8")
            loaded = CatxAgentProfile.load(path)

        self.assertEqual("CATX_API_KEY", loaded.api_key_env)
        self.assertEqual(("vlt_test",), loaded.vault_ids)
        self.assertEqual("SUPABASE_ACCESS_TOKEN", loaded.stream.bearer_env)
        self.assertNotIn("catx-secret", repr(loaded))

    def test_credentials_file_is_resolved_relative_to_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credentials = root / "credentials.json"
            credentials.write_text(
                json.dumps({"CATX_API_KEY": "file-secret"}), encoding="utf-8"
            )
            credentials.chmod(0o600)
            path = root / "catx.json"
            path.write_text(
                json.dumps(profile_payload(credentials_file="credentials.json")),
                encoding="utf-8",
            )
            loaded = CatxAgentProfile.load(path)

        self.assertEqual(str(credentials.resolve()), loaded.credentials_file)
        self.assertNotIn("file-secret", repr(loaded))

    def test_repository_resource_profile_keeps_only_token_environment_name(self):
        repository = CatxRepositoryResourceProfile.from_mapping(
            {
                "url": "ssh://git@git.sankuai.com/org/repo.git",
                "authorization_token_env": "CATX_REPOSITORY_TOKEN",
                "mount_path": "/workspace/repo",
            }
        )
        self.assertEqual("/workspace/repo", repository.mount_path)
        self.assertNotIn("pat_secret", repr(repository))

    def test_rejects_inline_secrets_unknown_fields_and_unsafe_urls(self):
        invalid = (
            {"api_key": "secret"},
            {"base_url": "https://secret@api.catx.test"},
            {"events_limit": 1001},
            {"vault_ids": []},
            {"stream": {"base_url": "file:///tmp/sse", "api_key_env": "KEY"}},
            {
                "repository": {
                    "url": "file:///tmp/repo",
                    "authorization_token_env": "REPO_TOKEN",
                }
            },
            {
                "repository": {
                    "url": "ssh://git@git.sankuai.com/org/repo.git",
                    "authorization_token_env": "REPO_TOKEN",
                    "mount_path": "../repo",
                }
            },
        )
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(CatxProfileError):
                profile(**override)


class CatxAgentClientTest(unittest.TestCase):
    def test_credentials_file_supplies_values_and_environment_overrides_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = Path(temp_dir, "credentials.json")
            values = dict(ENVIRONMENT)
            values["CATX_API_KEY"] = "file-secret"
            credentials.write_text(json.dumps(values), encoding="utf-8")
            credentials.chmod(0o600)
            transport = FakeTransport(
                [response({"id": "session_file"}), response({"ok": True})]
            )
            client = CatxAgentClient(
                profile(credentials_file=str(credentials)),
                transport=transport,
                environment={"CATX_API_KEY": "override-secret"},
            )
            client.start_session({"prompt": "review"})

        self.assertEqual("override-secret", transport.calls[0]["headers"]["X-Api-Key"])
        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual("agent_test", payload["agent"])

    def test_credentials_file_rejects_open_permissions_and_unreferenced_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = Path(temp_dir, "credentials.json")
            credentials.write_text(
                json.dumps({"UNREFERENCED_SECRET": "secret"}), encoding="utf-8"
            )
            credentials.chmod(0o600)
            with self.assertRaisesRegex(CatxProfileError, "unreferenced keys"):
                CatxAgentClient(
                    profile(credentials_file=str(credentials)), environment={}
                )

            credentials.write_text(
                json.dumps({"CATX_API_KEY": "secret"}), encoding="utf-8"
            )
            if os.name == "posix":
                credentials.chmod(0o644)
                with self.assertRaisesRegex(CatxProfileError, "permissions"):
                    CatxAgentClient(
                        profile(credentials_file=str(credentials)), environment={}
                    )

    def test_repository_resource_is_mounted_from_profile_without_leaking_token(self):
        transport = FakeTransport(
            [response({"id": "session_repo"}), response({"ok": True})]
        )
        environment = dict(ENVIRONMENT, CATX_REPOSITORY_TOKEN="pat_secret")
        client = CatxAgentClient(
            profile(
                base_url="https://api.catpaw.sankuai.com/v1",
                repository={
                    "url": "ssh://git@git.sankuai.com/org/repo.git",
                    "authorization_token_env": "CATX_REPOSITORY_TOKEN",
                    "mount_path": "/workspace/repo",
                },
            ),
            transport=transport,
            environment=environment,
        )

        client.start_session({"prompt": "review"})

        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(
            [
                {
                    "type": "repository",
                    "url": "ssh://git@git.sankuai.com/org/repo.git",
                    "authorization_token": "pat_secret",
                    "mount_path": "/workspace/repo",
                }
            ],
            payload["resources"],
        )
        self.assertNotIn("pat_secret", repr(client.profile))

    def test_repository_resource_requires_configured_token_environment(self):
        client = CatxAgentClient(
            profile(
                repository={
                    "url": "ssh://git@git.sankuai.com/org/repo.git",
                    "authorization_token_env": "CATX_REPOSITORY_TOKEN",
                }
            ),
            transport=FakeTransport([]),
            environment=ENVIRONMENT,
        )
        with self.assertRaisesRegex(CompanyApiRequestError, "repository authorization"):
            client.start_session({"prompt": "review"})

    def test_exact_binding_is_augmented_and_can_be_verified(self):
        transport = FakeTransport(
            [response({"id": "session_bound"}), response({"ok": True})]
        )
        adapter = BindingAdapter()
        client = CatxAgentClient(
            profile(),
            transport=transport,
            environment=ENVIRONMENT,
            binding_adapter=adapter,
        )
        binding = execution_binding()
        session = client.start_session({"prompt": "review"}, binding=binding)
        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(binding.subject_hash, payload["skill_binding"]["hash"])
        self.assertEqual(binding.repository_ref, payload["repository_mount"]["ref"])
        evidence = client.verify_binding(session, binding)
        evidence.assert_matches(binding)

    def test_binding_requires_adapter_and_cannot_change_profile_owned_fields(self):
        client = CatxAgentClient(
            profile(), transport=FakeTransport([]), environment=ENVIRONMENT
        )
        with self.assertRaisesRegex(CompanyApiRequestError, "requires a binding adapter"):
            client.start_session({"prompt": "review"}, binding=execution_binding())
        client = CatxAgentClient(
            profile(),
            transport=FakeTransport([]),
            environment=ENVIRONMENT,
            binding_adapter=BindingAdapter(mutate=True),
        )
        with self.assertRaisesRegex(CompanyApiRequestError, "protected fields"):
            client.start_session({"prompt": "review"}, binding=execution_binding())

    def test_binding_repository_must_match_profile_repository(self):
        environment = dict(ENVIRONMENT, CATX_REPOSITORY_TOKEN="pat_secret")
        client = CatxAgentClient(
            profile(
                repository={
                    "url": "ssh://git@git.sankuai.com/org/repo.git",
                    "authorization_token_env": "CATX_REPOSITORY_TOKEN",
                }
            ),
            transport=FakeTransport([]),
            environment=environment,
            binding_adapter=BindingAdapter(),
        )
        with self.assertRaisesRegex(CompanyApiRequestError, "repository_ref"):
            client.start_session({"prompt": "review"}, binding=execution_binding())

    def test_starts_session_then_appends_user_message(self):
        transport = FakeTransport(
            [response({"id": "session_1", "status": "idle"}), response({"ok": True})]
        )
        client = CatxAgentClient(profile(), transport=transport, environment=ENVIRONMENT)

        session_id = client.start_session({"prompt": "检查告警", "title": "根因分析"})

        self.assertEqual("session_1", session_id)
        self.assertEqual(2, len(transport.calls))
        create, message = transport.calls
        self.assertEqual("https://api.catx.test/api/v1/sessions", create["url"])
        self.assertEqual(
            {
                "title": "根因分析",
                "agent": "agent_test",
                "environment_id": "env_test",
                "vault_ids": ["vlt_test"],
            },
            json.loads(create["body"]),
        )
        self.assertEqual(
            {
                "events": [
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": "检查告警"}],
                    }
                ]
            },
            json.loads(message["body"]),
        )
        self.assertEqual("catx-secret", create["headers"]["X-Api-Key"])
        self.assertEqual("tester", create["headers"]["user-mis-id"])
        self.assertEqual("2023-06-01", create["headers"]["anthropic-version"])

    def test_reports_partial_creation_and_rejects_missing_configuration(self):
        client = CatxAgentClient(
            profile(),
            transport=FakeTransport(
                [response({"id": "session_partial"}), response({}, status=503)]
            ),
            environment=ENVIRONMENT,
        )
        with self.assertRaisesRegex(CompanyApiRequestError, "session_partial was created"):
            client.start_session({"prompt": "hello"})

        missing_api_key = dict(ENVIRONMENT)
        missing_api_key.pop("CATX_API_KEY")
        client = CatxAgentClient(
            profile(), transport=FakeTransport([]), environment=missing_api_key
        )
        with self.assertRaisesRegex(CompanyApiRequestError, "CATX API key"):
            client.start_session({"prompt": "hello"})

    def test_polls_status_and_uses_last_effective_message(self):
        events = [
            {"type": "agent.message", "content": [{"type": "text", "text": "draft"}]},
            {"type": "agent.message", "content": [{"type": "text", "text": "(no content)"}]},
            {"type": "agent.message", "content": [{"type": "text", "text": "final"}]},
        ]
        transport = FakeTransport(
            [response({"status": "idle", "usage": {"output_tokens": 8}}), response({"data": events})]
        )
        client = CatxAgentClient(profile(), transport=transport, environment=ENVIRONMENT)

        result = client.poll_session("session/1")

        self.assertEqual("COMPLETED", result["status"])
        self.assertEqual("final", result["message"])
        self.assertIn("session%2F1/events", transport.calls[1]["url"])
        self.assertEqual("final", last_effective_agent_message(events))

    def test_fetches_complete_event_types_into_imported_bundle(self):
        events = [
            {"type": "session.status_running"},
            {
                "type": "agent.tool_use",
                "id": "tool-1",
                "name": "read_file",
                "processed_at": "2026-08-20T18:00:00+08:00",
                "input": {"path": "x.py"},
            },
            {
                "type": "agent.tool_result",
                "tool_use_id": "tool-1",
                "is_error": False,
                "processed_at": "2026-08-20T18:00:01+08:00",
                "content": [{"type": "text", "text": "ok"}],
            },
            {"type": "agent.message", "content": [{"type": "text", "text": "done"}]},
        ]
        client = CatxAgentClient(
            profile(),
            transport=FakeTransport(
                [
                    response(
                        {
                            "status": "idle",
                            "usage": {
                                "input_tokens": 3,
                                "output_tokens": 9,
                                "cache_read_input_tokens": 5,
                                "cache_creation": {
                                    "ephemeral_1h_input_tokens": 0,
                                    "ephemeral_5m_input_tokens": 7,
                                },
                            },
                        }
                    ),
                    response({"data": events}),
                ]
            ),
            environment=ENVIRONMENT,
        )

        bundle = client.fetch_session("session_2")

        self.assertEqual("done", bundle.output)
        self.assertEqual("tool_call", bundle.trace[1].kind)
        self.assertEqual("read_file", bundle.trace[1].tool)
        self.assertEqual("tool_result", bundle.trace[2].kind)
        self.assertEqual("read_file", bundle.trace[2].tool)
        self.assertTrue(bundle.trace[2].payload["ok"])
        self.assertEqual("2026-08-20T18:00:01+08:00", bundle.trace[2].timestamp)
        self.assertEqual(9, bundle.usage["output_tokens"])
        self.assertEqual(7, bundle.usage["cache_creation_input_tokens"])
        self.assertEqual(24, bundle.usage["total_tokens"])
        self.assertTrue(bundle.completeness.complete)
        self.assertEqual("catx_session_api", bundle.source)

    def test_fetch_redacts_repository_token_and_url_userinfo_from_log(self):
        environment = dict(ENVIRONMENT, CATX_REPOSITORY_TOKEN="pat_secret")
        events = [
            {
                "type": "agent.tool_result",
                "content": [
                    {
                        "type": "text",
                        "text": "http://oauth2:pat_secret@git.test/repo.git",
                    }
                ],
            },
            {
                "type": "agent.message",
                "content": [
                    {
                        "type": "text",
                        "text": "remote=https://user:unknown-token@git.test/repo.git",
                    }
                ],
            },
        ]
        client = CatxAgentClient(
            profile(
                repository={
                    "url": "ssh://git@git.test/repo.git",
                    "authorization_token_env": "CATX_REPOSITORY_TOKEN",
                }
            ),
            transport=FakeTransport(
                [response({"status": "idle", "usage": {}}), response({"data": events})]
            ),
            environment=environment,
        )

        bundle = client.fetch_session("session_redacted")
        serialized = repr(
            {"output": bundle.output, "trace": [event.payload for event in bundle.trace]}
        )

        self.assertNotIn("pat_secret", serialized)
        self.assertNotIn("unknown-token", serialized)
        self.assertIn("http://***@git.test/repo.git", serialized)

    def test_sse_waits_for_idle_then_fetches_authoritative_log(self):
        sse = FakeSSETransport(
            [
                b": keep-alive\n\ndata: {\"type\":\"session.status_running\"}\n\n",
                b"data: {\"type\":\"agent.message.chunk\",\"content\":[{\"type\":\"text\",\"text\":\"do",
                b"ne\"}]}\n\ndata: {\"type\":\"session.status_idle\"}\n\n",
            ]
        )
        transport = FakeTransport(
            [
                response({"status": "idle", "usage": {"output_tokens": 2}}),
                response({"data": [{"type": "agent.message", "content": [{"type": "text", "text": "done"}]}]}),
            ]
        )
        observed = []
        client = CatxAgentClient(
            profile(), transport=transport, sse_transport=sse, environment=ENVIRONMENT
        )

        bundle = client.listen_session("session 3", observed.append)

        self.assertEqual("done", bundle.output)
        self.assertEqual("session.status_idle", observed[-1]["type"])
        call = sse.calls[0]
        self.assertIn("action=getMessage", call["url"])
        self.assertIn("session_id=session+3", call["url"])
        self.assertEqual("Bearer access-secret", call["headers"]["Authorization"])
        self.assertEqual("anon-secret", call["headers"]["apikey"])

    def test_sse_rejects_non_terminal_stream(self):
        client = CatxAgentClient(
            profile(),
            transport=FakeTransport([]),
            sse_transport=FakeSSETransport(
                [b'data: {"type":"session.status_running"}\n\n']
            ),
            environment=ENVIRONMENT,
        )
        with self.assertRaisesRegex(CatxStreamError, "before a terminal"):
            client.listen_session("session_4")

    def test_sse_error_still_fetches_diagnostic_log(self):
        client = CatxAgentClient(
            profile(),
            transport=FakeTransport(
                [response({"status": "terminated", "usage": {}}), response({"data": []})]
            ),
            sse_transport=FakeSSETransport(
                [b'data: {"type":"session.error","error":{"message":"boom"}}\n\n']
            ),
            environment=ENVIRONMENT,
        )

        bundle = client.listen_session("session_4")

        self.assertEqual("boom", bundle.observation.error)


class SSEParserTest(unittest.TestCase):
    def test_parses_multiline_data_and_rejects_invalid_json(self):
        events = list(
            iter_sse_json_events(
                [b'data: {"type":"agent.message",\n', b'data: "content":[]}\n\n'],
                1024,
            )
        )
        self.assertEqual("agent.message", events[0]["type"])
        with self.assertRaises(CatxStreamError):
            list(iter_sse_json_events([b"data: not-json\n\n"], 1024))


if __name__ == "__main__":
    unittest.main()
