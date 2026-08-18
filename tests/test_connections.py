import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from aceval.connections import (
    PROFILE_API_VERSION,
    CompanyApiProfile,
    CompanyApiProfileError,
    CompanyApiRequestError,
    EnvironmentAuth,
    ExecuteEndpointMapping,
    HTTPResponse,
    HTTPSessionLogProvider,
    JsonPathError,
    ObservationCompleteness,
    SessionLogEndpointMapping,
    SessionLogImportError,
    SessionLogProvider,
    extract_json_path,
    import_session_log,
    load_imported_run_bundle,
    load_session_log,
)
from aceval.contracts import RuntimeResult


def profile_payload(**overrides):
    payload = {
        "api_version": PROFILE_API_VERSION,
        "name": "company-agent",
        "base_url": "https://agent.company.test/v1/",
        "auth": {"type": "bearer_env", "env": "COMPANY_AGENT_TOKEN"},
        "execute": {
            "path": "/runs",
            "session_id_path": "$.data.session_id",
        },
        "session_log": {
            "path_template": "/sessions/{session_id}",
            "output_path": "$.data.output",
            "trace_path": "$.data.events",
            "usage_path": "$.data.usage",
            "error_path": "$.data.error",
            "metadata_path": "$.data.metadata",
        },
        "timeout_seconds": 12,
        "max_response_bytes": 4096,
    }
    payload.update(overrides)
    return payload


def profile(**overrides):
    return CompanyApiProfile.from_mapping(profile_payload(**overrides))


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected transport call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(payload, status=200):
    return HTTPResponse(
        status_code=status,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


class CompanyApiProfileTest(unittest.TestCase):
    def test_loads_strict_profile_without_storing_a_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "company-profile.json")
            path.write_text(json.dumps(profile_payload()), encoding="utf-8")

            loaded = CompanyApiProfile.load(path)

        self.assertEqual("company-agent", loaded.name)
        self.assertEqual("https://agent.company.test/v1", loaded.base_url)
        self.assertEqual("COMPANY_AGENT_TOKEN", loaded.auth.env)
        self.assertFalse(hasattr(loaded.auth, "token"))
        self.assertEqual("/runs", loaded.execute.path)
        self.assertEqual("$.data.events", loaded.session_log.trace_path)
        self.assertEqual(12.0, loaded.timeout_seconds)
        self.assertNotIn("actual-secret", repr(loaded))
        with self.assertRaises(FrozenInstanceError):
            loaded.name = "changed"

    def test_supports_header_auth_and_session_only_profiles(self):
        payload = profile_payload(
            auth={
                "type": "header_env",
                "env": "COMPANY_API_KEY",
                "header": "X-API-Key",
            }
        )
        payload.pop("execute")

        loaded = CompanyApiProfile.from_mapping(payload)

        self.assertIsNone(loaded.execute)
        self.assertEqual(
            {"X-API-Key": "key-value"},
            loaded.auth.request_headers({"COMPANY_API_KEY": "key-value"}),
        )

    def test_rejects_inline_secrets_unknown_fields_and_duplicate_json_keys(self):
        invalid = profile_payload(
            auth={
                "type": "bearer_env",
                "env": "COMPANY_AGENT_TOKEN",
                "token": "must-not-be-stored",
            }
        )
        with self.assertRaisesRegex(CompanyApiProfileError, "unsupported fields"):
            CompanyApiProfile.from_mapping(invalid)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "duplicate.json")
            path.write_text(
                '{"api_version":"%s","api_version":"%s"}'
                % (PROFILE_API_VERSION, PROFILE_API_VERSION),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CompanyApiProfileError, "strict JSON"):
                CompanyApiProfile.load(path)

    def test_rejects_unsafe_or_ambiguous_configuration(self):
        invalid_values = (
            {"api_version": "aceval.company-profile/v2"},
            {"base_url": "https://token@agent.company.test"},
            {"base_url": "file:///tmp/agent"},
            {"timeout_seconds": float("nan")},
            {"max_response_bytes": True},
            {"auth": {"type": "bearer_env", "env": "NOT VALID"}},
            {
                "auth": {
                    "type": "bearer_env",
                    "env": "TOKEN",
                    "header": "Authorization",
                }
            },
            {"session_log": {"path_template": "/sessions/no-placeholder"}},
            {
                "session_log": {
                    "path_template": "/sessions/{session_id}?full=true"
                }
            },
            {
                "session_log": {
                    "path_template": "/sessions/{session_id}",
                    "output_path": "$..output",
                }
            },
            {"execute": {"path": "https://other.test/runs"}},
        )
        for override in invalid_values:
            with self.subTest(override=override):
                payload = profile_payload(**override)
                with self.assertRaises(CompanyApiProfileError):
                    CompanyApiProfile.from_mapping(payload)


class JsonPathTest(unittest.TestCase):
    def test_extracts_root_keys_quoted_keys_and_array_indexes(self):
        payload = {"data": {"odd.key": [{"value": 7}]}}

        self.assertIs(payload, extract_json_path(payload, "$"))
        self.assertEqual(
            7, extract_json_path(payload, '$.data["odd.key"][0].value')
        )

    def test_rejects_missing_and_executable_jsonpath_syntax(self):
        invalid = (
            "data.output",
            "$..output",
            "$.items[*]",
            "$.items[-1]",
            "$.items[?(@.ok)]",
            "$['single-quotes']",
        )
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(JsonPathError):
                extract_json_path({}, path)
        with self.assertRaisesRegex(JsonPathError, "not found"):
            extract_json_path({"data": []}, "$.data[0]")


class SessionLogImportTest(unittest.TestCase):
    def test_imports_neutral_observation_trace_usage_and_metadata(self):
        payload = {
            "data": {
                "output": {"answer": "完成"},
                "events": [
                    {
                        "type": "tool_start",
                        "seq": 4,
                        "timestamp": "2026-08-18T00:00:00Z",
                        "name": "read_file",
                        "arguments": {"path": "input.txt"},
                        "duration_ms": 1.5,
                    },
                    {
                        "kind": "message",
                        "name": "final",
                        "payload": {"role": "assistant"},
                    },
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "cost_usd": 0.02,
                },
                "error": None,
                "metadata": {"model": "company-model"},
            }
        }

        bundle = import_session_log(profile(), "session-1", payload)

        self.assertEqual({"answer": "完成"}, bundle.output)
        self.assertEqual("tool_call", bundle.trace[0].kind)
        self.assertEqual("read_file", bundle.trace[0].tool)
        self.assertEqual(
            {"path": "input.txt"}, bundle.trace[0].payload["arguments"]
        )
        self.assertEqual(1, bundle.trace[1].seq)
        self.assertEqual(15, bundle.usage["total_tokens"])
        self.assertTrue(bundle.completeness.complete)
        self.assertTrue(bundle.completeness.trace)
        self.assertEqual(
            "company-model",
            bundle.observation.metadata["source_metadata"]["model"],
        )
        self.assertTrue(bundle.observation.metadata["completeness"]["trace"])
        runtime_result = bundle.to_runtime_result()
        self.assertIsInstance(runtime_result, RuntimeResult)
        self.assertEqual(bundle.output, runtime_result.final_output)
        with self.assertRaises(TypeError):
            bundle.usage["total_tokens"] = 0

    def test_explicitly_unconfigured_telemetry_is_marked_not_observed(self):
        log_mapping = {
            "path_template": "/sessions/{session_id}",
            "output_path": "$.output",
            "trace_path": None,
            "usage_path": None,
        }
        loaded = profile(session_log=log_mapping)

        bundle = import_session_log(loaded, "session-2", {"output": "ok"})

        self.assertEqual((), bundle.trace)
        self.assertEqual({}, bundle.usage)
        self.assertFalse(bundle.completeness.trace)
        self.assertFalse(bundle.completeness.usage)
        self.assertTrue(bundle.completeness.complete)
        self.assertEqual(frozenset({"output"}), bundle.completeness.expected)
        self.assertFalse(bundle.observation.metadata["completeness"]["trace"])

    def test_missing_configured_channels_fail_closed(self):
        base = {"data": {"output": "ok", "events": [], "usage": {}}}
        for key, message in (
            ("output", "output is missing"),
            ("events", "trace is missing"),
            ("usage", "usage is missing"),
        ):
            payload = json.loads(json.dumps(base))
            del payload["data"][key]
            with self.subTest(key=key), self.assertRaisesRegex(
                SessionLogImportError, message
            ):
                import_session_log(profile(), "session-3", payload)

    def test_malformed_trace_usage_and_optional_values_fail_closed(self):
        good = {
            "data": {
                "output": "ok",
                "events": [],
                "usage": {},
                "error": None,
                "metadata": {},
            }
        }
        mutations = (
            ("events", {}, "trace must be an array"),
            ("events", ["tool"], "trace events must be objects"),
            ("events", [{"name": "read"}], "kind must be"),
            (
                "events",
                [{"kind": "tool_call", "seq": True}],
                "seq must be",
            ),
            (
                "events",
                [{"kind": "tool_call", "duration_ms": float("inf")}],
                "duration_ms",
            ),
            ("usage", [], "usage must be an object"),
            ("usage", {"total_tokens": 1.5}, "token usage values"),
            ("usage", {"cost_usd": -1}, "non-negative finite"),
            ("error", 500, "error must be"),
            ("metadata", [], "metadata must be an object"),
        )
        for key, value, message in mutations:
            with self.subTest(key=key, value=value):
                payload = json.loads(json.dumps(good))
                payload["data"][key] = value
                with self.assertRaisesRegex(SessionLogImportError, message):
                    import_session_log(profile(), "session-4", payload)

    def test_rejects_conflicting_trace_aliases_and_payload_fields(self):
        events = (
            {"kind": "message", "type": "tool_call"},
            {
                "kind": "tool_call",
                "arguments": {"top": True},
                "payload": {"arguments": {"nested": True}},
            },
        )
        for event in events:
            payload = {
                "data": {"output": "ok", "events": [event], "usage": {}}
            }
            with self.subTest(event=event), self.assertRaises(SessionLogImportError):
                import_session_log(profile(), "session-5", payload)

    def test_loads_local_session_json_strictly(self):
        payload = {
            "data": {"output": "ok", "events": [], "usage": {}}
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "session.json")
            path.write_text(json.dumps(payload), encoding="utf-8")
            bundle = load_session_log(profile(), "local-session", path)
            self.assertEqual("ok", bundle.output)

            path.write_text('{"data":{},"data":{}}', encoding="utf-8")
            with self.assertRaisesRegex(SessionLogImportError, "strict JSON"):
                load_session_log(profile(), "local-session", path)

    def test_completeness_contract_rejects_unknown_channels(self):
        with self.assertRaisesRegex(ValueError, "unsupported observation"):
            ObservationCompleteness(observed=frozenset({"screenshots"}))

    def test_loads_normalized_imported_bundle_for_offline_diagnosis(self):
        document = {
            "schema_version": "aceval.imported-session/v1",
            "session_id": "session-offline",
            "profile_name": "company-agent",
            "source": "company_session_log",
            "completeness": {
                "expected": ["output", "trace", "usage"],
                "observed": ["output", "trace", "usage"],
            },
            "observation": {
                "output": "failed",
                "trace": [
                    {
                        "kind": "tool_result",
                        "seq": 1,
                        "tool": "process_exec",
                        "payload": {"ok": False, "exit_code": 127},
                    }
                ],
                "artifacts": {},
                "pre_state": {},
                "post_state": {},
                "error": None,
                "usage": {"total_tokens": 3},
                "metadata": {},
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "imported.json")
            path.write_text(json.dumps(document), encoding="utf-8")
            bundle = load_imported_run_bundle(path)

        self.assertEqual("session-offline", bundle.session_id)
        self.assertEqual(127, bundle.trace[0].payload["exit_code"])
        self.assertTrue(bundle.completeness.complete)


class HTTPSessionLogProviderTest(unittest.TestCase):
    def test_rejects_non_mapping_environment(self):
        with self.assertRaisesRegex(TypeError, "environment must be a mapping"):
            HTTPSessionLogProvider(profile(), environment=[])

    def test_fetches_with_env_auth_escaped_id_and_injected_transport(self):
        payload = {
            "data": {"output": "ok", "events": [], "usage": {"total_tokens": 3}}
        }
        transport = FakeTransport([response(payload)])
        provider = HTTPSessionLogProvider(
            profile(),
            transport=transport,
            environment={"COMPANY_AGENT_TOKEN": "test-secret"},
        )

        bundle = provider.fetch_session("session/with space")

        self.assertIsInstance(provider, SessionLogProvider)
        self.assertEqual("ok", bundle.output)
        call = transport.calls[0]
        self.assertEqual("GET", call["method"])
        self.assertEqual(
            "https://agent.company.test/v1/sessions/session%2Fwith%20space",
            call["url"],
        )
        self.assertEqual("Bearer test-secret", call["headers"]["Authorization"])
        self.assertIsNone(call["body"])
        self.assertEqual(12.0, call["timeout_seconds"])
        self.assertEqual(4096, call["max_response_bytes"])
        self.assertNotIn("test-secret", repr(provider.profile))

    def test_missing_or_invalid_auth_fails_before_transport(self):
        for environment in ({}, {"COMPANY_AGENT_TOKEN": "bad\nvalue"}):
            transport = FakeTransport([])
            provider = HTTPSessionLogProvider(
                profile(), transport=transport, environment=environment
            )
            with self.subTest(environment=environment), self.assertRaises(
                CompanyApiRequestError
            ):
                provider.fetch_session("session-1")
            self.assertEqual([], transport.calls)

    def test_execute_posts_strict_json_and_extracts_session_id(self):
        transport = FakeTransport(
            [response({"data": {"session_id": "started-session"}})]
        )
        provider = HTTPSessionLogProvider(
            profile(),
            transport=transport,
            environment={"COMPANY_AGENT_TOKEN": "secret"},
        )

        session_id = provider.execute({"prompt": "检查代码", "case_id": "case-1"})

        self.assertEqual("started-session", session_id)
        call = transport.calls[0]
        self.assertEqual("POST", call["method"])
        self.assertEqual("https://agent.company.test/v1/runs", call["url"])
        self.assertEqual(
            {"prompt": "检查代码", "case_id": "case-1"},
            json.loads(call["body"].decode("utf-8")),
        )
        self.assertEqual(
            "application/json; charset=utf-8", call["headers"]["Content-Type"]
        )

    def test_execute_requires_mapping_config_and_valid_session_response(self):
        session_only_payload = profile_payload()
        session_only_payload.pop("execute")
        session_only = CompanyApiProfile.from_mapping(session_only_payload)
        provider = HTTPSessionLogProvider(
            session_only,
            transport=FakeTransport([]),
            environment={"COMPANY_AGENT_TOKEN": "secret"},
        )
        with self.assertRaisesRegex(CompanyApiRequestError, "does not configure"):
            provider.execute({})

        provider = HTTPSessionLogProvider(
            profile(),
            transport=FakeTransport([response({"data": {}})]),
            environment={"COMPANY_AGENT_TOKEN": "secret"},
        )
        with self.assertRaisesRegex(CompanyApiRequestError, "missing session_id"):
            provider.execute({})

        provider = HTTPSessionLogProvider(
            profile(),
            transport=FakeTransport([]),
            environment={"COMPANY_AGENT_TOKEN": "secret"},
        )
        with self.assertRaisesRegex(CompanyApiRequestError, "must be an object"):
            provider.execute([])

    def test_http_status_json_shape_size_and_transport_fail_closed(self):
        malformed_responses = (
            HTTPResponse(status_code=503, body=b'{"detail":"internal"}'),
            HTTPResponse(status_code=200, body=b"not-json"),
            HTTPResponse(status_code=200, body=b"[]"),
            HTTPResponse(status_code=200, body=b"x" * 4097),
            object(),
            OSError("network details"),
        )
        for item in malformed_responses:
            provider = HTTPSessionLogProvider(
                profile(),
                transport=FakeTransport([item]),
                environment={"COMPANY_AGENT_TOKEN": "secret"},
            )
            with self.subTest(item=type(item).__name__), self.assertRaises(
                CompanyApiRequestError
            ):
                provider.fetch_session("session-1")


if __name__ == "__main__":
    unittest.main()
