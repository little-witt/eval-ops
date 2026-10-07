import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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
    CatxPayloadBindingAdapter,
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
    def test_default_events_limit_uses_maximum_snapshot_size(self):
        value = profile_payload()
        value.pop("events_limit")
        loaded = CatxAgentProfile.from_mapping(value)

        self.assertEqual(1000, loaded.events_limit)

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
            {
                "repository": {
                    "url": "ssh://git@git.sankuai.com/org/repo.git",
                    "authorization_token_env": "REPO_TOKEN",
                },
                "repositories": [
                    {
                        "url": "ssh://git@git.sankuai.com/org/other.git",
                        "authorization_token_env": "REPO_TOKEN",
                    }
                ],
            },
            {
                "repositories": [
                    {
                        "url": "ssh://git@git.sankuai.com/org/one.git",
                        "authorization_token_env": "REPO_TOKEN",
                        "mount_path": "/workspace/repo",
                    },
                    {
                        "url": "ssh://git@git.sankuai.com/org/two.git",
                        "authorization_token_env": "REPO_TOKEN",
                        "mount_path": "/workspace/repo",
                    },
                ]
            },
        )
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(CatxProfileError):
                profile(**override)


class CatxAgentClientTest(unittest.TestCase):
    def test_default_profile_uses_documented_catx_session_endpoint(self):
        value = profile_payload()
        value.pop("base_url")
        transport = FakeTransport(
            [response({"id": "session_default"}), response({"ok": True})]
        )
        client = CatxAgentClient(
            CatxAgentProfile.from_mapping(value),
            transport=transport,
            environment=ENVIRONMENT,
        )
        client.start_session({"prompt": "review"})
        self.assertEqual(
            "https://api.catx.sankuai.com/api/v1/sessions",
            transport.calls[0]["url"],
        )

    def test_repository_profiles_expand_create_session_payload_with_resources(self):
        transport = FakeTransport(
            [response({"id": "session_multi"}), response({"ok": True})]
        )
        environment = dict(ENVIRONMENT, CATX_REPOSITORY_TOKEN="pat_secret")
        client = CatxAgentClient(
            profile(
                repositories=[
                    {
                        "url": "ssh://git@git.sankuai.com/org/skill.git",
                        "authorization_token_env": "CATX_REPOSITORY_TOKEN",
                        "mount_path": "/workspace/skills/reviewer",
                    },
                    {
                        "url": "ssh://git@git.sankuai.com/org/fixture.git",
                        "authorization_token_env": "CATX_REPOSITORY_TOKEN",
                        "mount_path": "/workspace/repo",
                    },
                ]
            ),
            transport=transport,
            environment=environment,
        )

        client.start_session({"prompt": "review"})

        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(2, len(payload["resources"]))
        self.assertEqual("/workspace/skills/reviewer", payload["resources"][0]["mount_path"])
        self.assertEqual("/workspace/repo", payload["resources"][1]["mount_path"])
        self.assertIn("pat_secret", transport.calls[0]["body"].decode("utf-8"))

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

    def test_repository_profile_is_local_provenance_without_leaking_token(self):
        transport = FakeTransport(
            [response({"id": "session_repo"}), response({"ok": True})]
        )
        environment = dict(ENVIRONMENT, CATX_REPOSITORY_TOKEN="pat_secret")
        client = CatxAgentClient(
            profile(
                base_url="https://api.catx.sankuai.com/api/v1",
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
        self.assertEqual("/workspace/repo", payload["resources"][0]["mount_path"])
        self.assertIn("pat_secret", transport.calls[0]["body"].decode("utf-8"))
        self.assertNotIn("pat_secret", repr(client.profile))

    def test_repository_profile_requires_token_for_session_creation(self):
        transport = FakeTransport(
            [response({"id": "session_repo"}), response({"ok": True})]
        )
        client = CatxAgentClient(
            profile(
                repository={
                    "url": "ssh://git@git.sankuai.com/org/repo.git",
                    "authorization_token_env": "CATX_REPOSITORY_TOKEN",
                }
            ),
            transport=transport,
            environment=ENVIRONMENT,
        )
        with self.assertRaisesRegex(CompanyApiRequestError, "repository authorization token"):
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

    def test_event_binding_rejects_free_text_that_only_mentions_mount_and_commit(self):
        revision = "2" * 40
        binding = CatxExecutionBinding(
            api_version=CATX_EXECUTION_BINDING_API_VERSION,
            subject_hash="sha256:" + "a" * 64,
            skill_ref="catx-repository://skill-fixture",
            repository_ref="catx-repository://repo-fixture",
            repository_hash="sha256:" + "b" * 64,
            base_commit=revision,
            head_commit=revision,
            metadata={"expected_mounts": [{"mount_path": "/workspace/repo", "revision": revision}]},
        )
        client = SimpleNamespace(
            profile=SimpleNamespace(
                repository_resources=(SimpleNamespace(url=binding.repository_ref, mount_path="/workspace/repo"),)
            )
        )
        events = [
            {
                "type": "agent.message",
                "content": [{"type": "text", "text": "已执行 git -C /workspace/repo rev-parse HEAD，结果为 " + revision}],
            }
        ]
        evidence = CatxPayloadBindingAdapter().verify_event_log(client, "session-text-only", binding, events)
        self.assertFalse(evidence.verified)
        self.assertFalse(evidence.details["checks"][0]["mount_observed"])

    def test_event_binding_rejects_a_structured_echo_command(self):
        revision = "2" * 40
        binding = CatxExecutionBinding(
            api_version=CATX_EXECUTION_BINDING_API_VERSION,
            subject_hash="sha256:" + "a" * 64,
            skill_ref="catx-repository://skill-fixture",
            repository_ref="catx-repository://repo-fixture",
            repository_hash="sha256:" + "b" * 64,
            base_commit=revision,
            head_commit=revision,
            metadata={"expected_mounts": [{"mount_path": "/workspace/repo", "revision": revision}]},
        )
        client = SimpleNamespace(profile=SimpleNamespace(repository_resources=()))
        events = [
            {"type": "agent.tool_use", "id": "call-echo", "name": "process_exec", "input": {"command": "echo git -C /workspace/repo rev-parse HEAD"}},
            {"type": "agent.tool_result", "tool_use_id": "call-echo", "is_error": False, "content": [{"type": "text", "text": revision}]},
        ]
        evidence = CatxPayloadBindingAdapter().verify_event_log(client, "session-echo", binding, events)
        self.assertFalse(evidence.verified)

    def test_event_binding_requires_structured_paired_tool_call_and_successful_result(self):
        revision = "2" * 40
        binding = CatxExecutionBinding(
            api_version=CATX_EXECUTION_BINDING_API_VERSION,
            subject_hash="sha256:" + "a" * 64,
            skill_ref="catx-repository://skill-fixture",
            repository_ref="catx-repository://repo-fixture",
            repository_hash="sha256:" + "b" * 64,
            base_commit=revision,
            head_commit=revision,
            metadata={"expected_mounts": [{"mount_path": "/workspace/repo", "revision": revision}]},
        )
        client = SimpleNamespace(profile=SimpleNamespace(repository_resources=()))
        events = [
            {"type": "user.message", "content": [{"type": "text", "text": "review"}]},
            {
                "type": "agent.tool_use",
                "id": "call-1",
                "name": "process_exec",
                "input": {"command": "git -C /workspace/repo rev-parse HEAD"},
            },
            {
                "type": "agent.tool_result",
                "tool_use_id": "call-1",
                "is_error": False,
                "content": [{"type": "text", "text": revision + "\n"}],
            },
            {"type": "agent.message", "content": [{"type": "text", "text": "done"}]},
        ]
        evidence = CatxPayloadBindingAdapter().verify_event_log(client, "session-structured", binding, events)
        self.assertTrue(evidence.verified)
        self.assertEqual("call-1", evidence.details["checks"][0]["call_id"])

    def test_event_binding_ignores_unrelated_non_command_tool_calls(self):
        revision = "2" * 40
        binding = CatxExecutionBinding(
            api_version=CATX_EXECUTION_BINDING_API_VERSION,
            subject_hash="sha256:" + "a" * 64,
            skill_ref="catx-repository://skill-fixture",
            repository_ref="catx-repository://repo-fixture",
            repository_hash="sha256:" + "b" * 64,
            base_commit=revision,
            head_commit=revision,
            metadata={"expected_mounts": [{"mount_path": "/workspace/repo", "revision": revision}]},
        )
        client = SimpleNamespace(profile=SimpleNamespace(repository_resources=()))
        events = [
            {"type": "user.message", "content": [{"type": "text", "text": "review"}]},
            {
                "type": "agent.tool_use",
                "id": "read-1",
                "name": "read_file",
                "input": {"path": "SKILL.md"},
            },
            {
                "type": "agent.tool_result",
                "tool_use_id": "read-1",
                "is_error": False,
                "content": [{"type": "text", "text": "skill contents"}],
            },
            {
                "type": "agent.tool_use",
                "id": "exec-1",
                "name": "process_exec",
                "input": {"command": "git -C /workspace/repo rev-parse HEAD"},
            },
            {
                "type": "agent.tool_result",
                "tool_use_id": "exec-1",
                "is_error": False,
                "content": [{"type": "text", "text": revision}],
            },
            {"type": "agent.message", "content": [{"type": "text", "text": "done"}]},
        ]
        evidence = CatxPayloadBindingAdapter().verify_event_log(client, "session-unrelated-tool", binding, events)
        self.assertTrue(evidence.verified)
        self.assertEqual(1, evidence.details["tool_call_count"])
        self.assertEqual(2, evidence.details["tool_result_count"])

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
        self.assertEqual("files-api-2025-04-14", create["headers"]["anthropic-beta"])
        self.assertEqual("application/json", create["headers"]["Content-Type"])

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

    def test_http_error_detail_is_redacted(self):
        client = CatxAgentClient(
            profile(),
            transport=FakeTransport(
                [
                    response(
                        {"message": "clone failed for token catx-secret"},
                        status=400,
                    )
                ]
            ),
            environment=ENVIRONMENT,
        )
        with self.assertRaises(CompanyApiRequestError) as captured:
            client.get_status("session_error")
        message = str(captured.exception)
        self.assertIn("clone failed", message)
        self.assertNotIn("catx-secret", message)

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

    def test_idle_session_with_error_maps_to_failed(self):
        transport = FakeTransport(
            [
                response({"status": "idle", "usage": {"output_tokens": 8}}),
                response(
                    {
                        "data": [
                            {
                                "type": "session.error",
                                "error": {
                                    "type": "model_rate_limited_error",
                                    "message": "模型请求频率限制",
                                },
                            },
                            {
                                "type": "session.status_idle",
                                "stop_reason": {"type": "retries_exhausted"},
                            },
                        ]
                    }
                ),
            ]
        )
        client = CatxAgentClient(profile(), transport=transport, environment=ENVIRONMENT)

        result = client.poll_session("session_failed")

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("模型请求频率限制", result["error"])

    def test_idle_with_unprocessed_user_message_remains_running(self):
        transport = FakeTransport(
            [
                response({"status": "idle", "usage": {}}),
                response(
                    {
                        "data": [
                            {
                                "type": "user.message",
                                "content": [{"type": "text", "text": "review"}],
                            }
                        ]
                    }
                ),
            ]
        )
        client = CatxAgentClient(profile(), transport=transport, environment=ENVIRONMENT)

        result = client.poll_session("session_pending")

        self.assertEqual("RUNNING", result["status"])

    def test_fetches_complete_event_types_into_imported_bundle(self):
        events = [
            {"type": "session.status_running"},
            {"type": "user.message", "content": [{"type": "text", "text": "review"}]},
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
        self.assertEqual("tool_call", bundle.trace[2].kind)
        self.assertEqual("read_file", bundle.trace[2].tool)
        self.assertEqual("tool_result", bundle.trace[3].kind)
        self.assertEqual("read_file", bundle.trace[3].tool)
        self.assertTrue(bundle.trace[3].payload["ok"])
        self.assertEqual("2026-08-20T18:00:01+08:00", bundle.trace[3].timestamp)
        self.assertEqual(9, bundle.usage["output_tokens"])
        self.assertEqual(7, bundle.usage["cache_creation_input_tokens"])
        self.assertEqual(24, bundle.usage["total_tokens"])
        self.assertTrue(bundle.completeness.complete)
        self.assertEqual("catx_session_api", bundle.source)

    def test_fetch_session_paginates_events_and_seals_one_complete_log(self):
        first_page = [
            {"event_id": "e1", "seq": 1, "type": "user.message", "content": [{"type": "text", "text": "review"}]},
            {"event_id": "e2", "seq": 2, "type": "agent.tool_use", "id": "tool-1", "name": "read_file", "input": {"path": "SKILL.md"}},
        ]
        second_page = [
            {"event_id": "e3", "seq": 3, "type": "agent.tool_result", "tool_use_id": "tool-1", "content": [{"type": "text", "text": "ok"}]},
            {"event_id": "e4", "seq": 4, "type": "agent.message", "content": [{"type": "text", "text": "done"}]},
        ]
        transport = FakeTransport(
            [
                response({"status": "idle", "usage": {}}),
                response({"data": first_page, "pagination": {"has_more": True, "next_cursor": "page-2", "total": 4}}),
                response({"data": second_page, "pagination": {"has_more": False, "total": 4}}),
            ]
        )
        client = CatxAgentClient(profile(events_limit=2), transport=transport, environment=ENVIRONMENT)

        bundle = client.fetch_session("session_paged")
        receipt = bundle.observation.metadata["event_log_integrity"]

        self.assertTrue(bundle.completeness.complete)
        self.assertTrue(receipt["complete"])
        self.assertEqual(2, receipt["page_count"])
        self.assertEqual(4, receipt["event_count"])
        self.assertEqual("complete", receipt["sequence_status"])
        self.assertIn("cursor=page-2", transport.calls[2]["url"])
        self.assertEqual(64, len(receipt["event_log_sha256"]))

    def test_full_page_is_complete_when_declared_total_matches(self):
        events = [
            {
                "event_id": "e1",
                "seq": 1,
                "type": "user.message",
                "content": [{"type": "text", "text": "review"}],
            },
            {
                "event_id": "e2",
                "seq": 2,
                "type": "agent.message",
                "content": [{"type": "text", "text": "done"}],
            },
        ]
        client = CatxAgentClient(
            profile(events_limit=2),
            transport=FakeTransport(
                [
                    response({"status": "idle", "usage": {}}),
                    response({"data": events, "total": 2}),
                ]
            ),
            environment=ENVIRONMENT,
        )

        bundle = client.fetch_session("session_total_proves_complete")
        receipt = bundle.observation.metadata["event_log_integrity"]

        self.assertTrue(receipt["complete"])
        self.assertEqual(2, receipt["declared_total"])
        self.assertTrue(bundle.completeness.trace)

    def test_sequence_gap_and_unpaired_tool_call_make_trace_not_evaluable(self):
        events = [
            {"event_id": "e1", "seq": 1, "type": "user.message", "content": [{"type": "text", "text": "review"}]},
            {"event_id": "e2", "seq": 3, "type": "agent.tool_use", "id": "tool-1", "name": "read_file"},
            {"event_id": "e3", "seq": 4, "type": "agent.message", "content": [{"type": "text", "text": "done"}]},
        ]
        client = CatxAgentClient(
            profile(),
            transport=FakeTransport(
                [response({"status": "idle", "usage": {}}), response({"data": events})]
            ),
            environment=ENVIRONMENT,
        )

        bundle = client.fetch_session("session_incomplete")
        receipt = bundle.observation.metadata["event_log_integrity"]

        self.assertFalse(bundle.completeness.trace)
        self.assertFalse(receipt["complete"])
        self.assertIn("sequence_gap", receipt["reason_codes"])
        self.assertIn("tool_result_missing", receipt["reason_codes"])

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
