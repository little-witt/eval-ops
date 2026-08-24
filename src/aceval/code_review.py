"""Deterministic fixture lab and grader for code-review Skills."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .contracts import Artifact, GradeResult, GradeStatus, Oracle, RunObservation, as_primitive
from .environment_contracts import (
    ENVIRONMENT_BLUEPRINT_API_VERSION,
    CommandSpec,
    EnvironmentBlueprint,
    EnvironmentLimits,
)


CODE_REVIEW_GRADER_ID = "code_review_findings_v1"
CODE_REVIEW_GRADER_VERSION = "1.0.0"
CODE_REVIEW_LAB_API_VERSION = "aceval.code-review-lab/v1"


class CodeReviewError(ValueError):
    """A code-review result or fixture is invalid."""


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _artifact_content(value: Any) -> Any:
    if isinstance(value, Artifact):
        return value.content
    if isinstance(value, Mapping) and "content" in value:
        return value["content"]
    return value


def _json_document(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(
            value,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError("non-standard JSON constant: %s" % item)
            ),
        )
    return json.loads(
        json.dumps(as_primitive(value), ensure_ascii=False, allow_nan=False)
    )


def _payload(observation: RunObservation, params: Mapping[str, Any]) -> Any:
    artifact_path = params.get("artifact_path")
    if artifact_path is None:
        return observation.output
    artifact = observation.artifacts.get(str(artifact_path))
    if artifact is None:
        raise CodeReviewError("declared findings artifact is missing")
    return _artifact_content(artifact)


def _safe_path(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    text = value.replace("\\", "/")
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or text in (".", ""):
        return None
    return path.as_posix()


def _line(value: Any) -> Optional[int]:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value


def _source_files(observation: RunObservation) -> Mapping[str, Any]:
    # Review refers to the state presented to the Agent.  Prefer pre_state and
    # only fall back to post_state for imported observations that lack it.
    return observation.pre_state or observation.post_state


def _line_count(value: Any) -> Optional[int]:
    content = _artifact_content(value)
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeError:
            return None
    if not isinstance(content, str):
        return None
    return len(content.splitlines())


def _finding_key(value: Mapping[str, Any]) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    path = _safe_path(value.get("path", value.get("file")))
    line = _line(value.get("line", value.get("start_line")))
    category = value.get("category")
    category = category.strip().lower() if isinstance(category, str) and category.strip() else None
    return path, line, category


def _expected_range(value: Mapping[str, Any]) -> Tuple[Optional[str], Optional[int], Optional[int], Optional[str]]:
    path = _safe_path(value.get("path", value.get("file")))
    start = _line(value.get("line_start", value.get("line", value.get("start_line"))))
    end = _line(value.get("line_end", value.get("end_line", start)))
    category = value.get("category")
    category = category.strip().lower() if isinstance(category, str) and category.strip() else None
    return path, start, end, category


def _match(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    actual_path, actual_line, actual_category = _finding_key(actual)
    path, start, end, category = _expected_range(expected)
    if None in (actual_path, actual_line, path, start, end):
        return False
    if actual_path != path or not start <= actual_line <= end:
        return False
    if category is not None and actual_category != category:
        return False
    expected_severity = expected.get("severity")
    if isinstance(expected_severity, str) and expected_severity.strip():
        actual_severity = actual.get("severity")
        if not isinstance(actual_severity, str):
            return False
        if actual_severity.strip().lower() != expected_severity.strip().lower():
            return False
    return True


class CodeReviewFindingsGrader:
    id = CODE_REVIEW_GRADER_ID
    version = CODE_REVIEW_GRADER_VERSION

    async def evaluate(
        self,
        observation: RunObservation,
        oracle: Oracle,
        params: Mapping[str, Any],
    ) -> GradeResult:
        hard = bool(params.get("hard", True))
        try:
            document = _json_document(_payload(observation, params))
        except (CodeReviewError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            return GradeResult(
                grader_id=self.id,
                version=self.version,
                status=GradeStatus.FAIL,
                score=0.0,
                hard=hard,
                message="code review output is not valid strict JSON: %s" % exc,
                missing=("findings",),
            )
        findings = document.get("findings") if isinstance(document, Mapping) else document
        if not isinstance(findings, Sequence) or isinstance(findings, (str, bytes)):
            return GradeResult(
                grader_id=self.id,
                version=self.version,
                status=GradeStatus.FAIL,
                score=0.0,
                hard=hard,
                message="code review output must contain a findings array",
                missing=("findings",),
            )
        if any(not isinstance(item, Mapping) for item in findings):
            return GradeResult(
                grader_id=self.id,
                version=self.version,
                status=GradeStatus.FAIL,
                score=0.0,
                hard=hard,
                message="every finding must be a JSON object",
            )
        oracle_data = _get(oracle, "data", oracle)
        expected = (
            oracle_data.get("expected_findings", ())
            if isinstance(oracle_data, Mapping)
            else ()
        )
        if not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)):
            return GradeResult(
                grader_id=self.id,
                version=self.version,
                status=GradeStatus.ERROR,
                hard=hard,
                message="code review Oracle expected_findings must be an array",
            )
        if any(not isinstance(item, Mapping) for item in expected):
            return GradeResult(
                grader_id=self.id,
                version=self.version,
                status=GradeStatus.ERROR,
                hard=hard,
                message="code review Oracle contains an invalid expected finding",
            )

        source_files = _source_files(observation)
        invalid_references = []
        for index, finding in enumerate(findings):
            path, line, _ = _finding_key(finding)
            count = _line_count(source_files.get(path)) if path is not None and path in source_files else None
            if path is None or line is None or count is None or line > count:
                invalid_references.append(index)

        matched_expected = set()
        matched_actual = set()
        duplicate_matches = 0
        for actual_index, finding in enumerate(findings):
            matches = [
                index for index, item in enumerate(expected) if _match(finding, item)
            ]
            if not matches:
                continue
            fresh = [index for index in matches if index not in matched_expected]
            if fresh:
                selected = fresh[0]
                matched_expected.add(selected)
                matched_actual.add(actual_index)
            else:
                duplicate_matches += 1

        false_positives = len(findings) - len(matched_actual)
        expected_count = len(expected)
        actual_count = len(findings)
        true_positives = len(matched_expected)
        recall = true_positives / expected_count if expected_count else 1.0
        precision = true_positives / actual_count if actual_count else (1.0 if not expected_count else 0.0)

        critical_expected = {
            index
            for index, item in enumerate(expected)
            if str(item.get("severity", "")).lower() in ("critical", "blocker")
            or bool(item.get("critical", False))
        }
        critical_recall = (
            len(critical_expected.intersection(matched_expected)) / len(critical_expected)
            if critical_expected
            else 1.0
        )

        try:
            min_recall = float(params.get("min_recall", 1.0))
            min_precision = float(params.get("min_precision", 1.0))
            max_false_positives = int(params.get("max_false_positives", 0))
            max_duplicates = int(params.get("max_duplicates", 0))
        except (TypeError, ValueError, OverflowError) as exc:
            return GradeResult(
                grader_id=self.id,
                version=self.version,
                status=GradeStatus.ERROR,
                hard=hard,
                message="code review grader thresholds are invalid: %s" % exc,
            )
        if (
            not 0.0 <= min_recall <= 1.0
            or not 0.0 <= min_precision <= 1.0
            or max_false_positives < 0
            or max_duplicates < 0
        ):
            return GradeResult(
                grader_id=self.id,
                version=self.version,
                status=GradeStatus.ERROR,
                hard=hard,
                message="code review grader thresholds are outside supported ranges",
            )
        require_valid_references = bool(params.get("require_valid_references", True))
        require_critical = bool(params.get("require_all_critical", True))
        reasons = []
        if recall < min_recall:
            reasons.append("recall %.3f is below %.3f" % (recall, min_recall))
        if precision < min_precision:
            reasons.append("precision %.3f is below %.3f" % (precision, min_precision))
        if false_positives > max_false_positives:
            reasons.append("false positives %d exceed %d" % (false_positives, max_false_positives))
        if duplicate_matches > max_duplicates:
            reasons.append("duplicate findings %d exceed %d" % (duplicate_matches, max_duplicates))
        if require_valid_references and invalid_references:
            reasons.append("%d findings contain invalid source references" % len(invalid_references))
        if require_critical and critical_recall < 1.0:
            reasons.append("not all critical findings were reported")
        status = GradeStatus.FAIL if reasons else GradeStatus.PASS
        score = recall * precision
        return GradeResult(
            grader_id=self.id,
            version=self.version,
            status=status,
            score=score,
            hard=hard,
            metrics={
                "expected": expected_count,
                "actual": actual_count,
                "true_positives": true_positives,
                "false_positives": false_positives,
                "missed": expected_count - true_positives,
                "duplicates": duplicate_matches,
                "invalid_source_references": len(invalid_references),
                "precision": precision,
                "recall": recall,
                "critical_recall": critical_recall,
            },
            evidence=(
                {
                    "matched_expected_indexes": sorted(matched_expected),
                    "invalid_actual_indexes": invalid_references,
                },
            ),
            message="; ".join(reasons) if reasons else "code review findings satisfy the Oracle",
        )


@dataclass(frozen=True)
class FixtureCase:
    id: str
    stack: str
    language: str
    case_type: str
    repository: Path
    base_ref: str
    head_ref: str
    base_commit: str
    head_commit: str
    oracle: Path
    prompt: str


STACK_TYPESCRIPT_WEB = "typescript-web"
STACK_REACT_NATIVE = "react-native"
STACK_WECHAT_MINIPROGRAM = "wechat-miniprogram"
STACK_JAVA_BACKEND = "java-backend"
DEFAULT_CODE_REVIEW_STACKS = (
    STACK_TYPESCRIPT_WEB,
    STACK_REACT_NATIVE,
    STACK_WECHAT_MINIPROGRAM,
    STACK_JAVA_BACKEND,
)
DEFAULT_CODE_REVIEW_CASE = "java-sql-injection"


def infer_code_review_stacks(skill_content: str) -> Tuple[str, ...]:
    """Infer fixture stacks from Skill instructions; fall back to broad coverage."""

    if not isinstance(skill_content, str) or not skill_content.strip():
        raise CodeReviewError("skill content must be a non-empty string")
    corpus = skill_content.casefold()
    selected = set()
    react_native = any(
        signal in corpus
        for signal in ("react native", "react-native", "reactnative", "mrn", "rn 代码")
    )
    miniprogram = any(
        signal in corpus
        for signal in ("微信小程序", "wechat miniprogram", "mini program", "miniprogram", "wx.")
    )
    web = any(
        signal in corpus
        for signal in (
            "typescript web", "react web", "web frontend", "dom", "browser",
            "next.js", "vue", "前端 web", "web 代码",
        )
    ) or ("typescript" in corpus and not react_native and not miniprogram)
    if react_native:
        selected.add(STACK_REACT_NATIVE)
    if miniprogram:
        selected.add(STACK_WECHAT_MINIPROGRAM)
    if web:
        selected.add(STACK_TYPESCRIPT_WEB)
    generic_frontend = any(
        signal in corpus for signal in ("frontend", "front-end", "前端代码", "前端 cr", "前端评审")
    )
    if generic_frontend and not selected:
        selected.update(
            (STACK_TYPESCRIPT_WEB, STACK_REACT_NATIVE, STACK_WECHAT_MINIPROGRAM)
        )
    if any(
        signal in corpus
        for signal in ("java", "spring", "mybatis", "后端代码", "backend", "back-end")
    ):
        selected.add(STACK_JAVA_BACKEND)
    if any(signal in corpus for signal in ("全栈", "full stack", "full-stack", "前后端")):
        selected.update(DEFAULT_CODE_REVIEW_STACKS)
    return tuple(stack for stack in DEFAULT_CODE_REVIEW_STACKS if stack in selected) or DEFAULT_CODE_REVIEW_STACKS


_TYPESCRIPT_WEB = '''export function renderDisplayName(
  element: HTMLElement,
  displayName: string,
): void {
  element.textContent = displayName;
}

export class LatestSearch {
  private requestId = 0;
  value = "";

  async run(query: string, fetcher: (value: string) => Promise<string>): Promise<void> {
    const requestId = ++this.requestId;
    const result = await fetcher(query);
    if (requestId === this.requestId) this.value = result;
  }
}

export function normalizeQuery(value: string): string {
  return value.trim();
}
'''

_REACT_NATIVE = '''import React, {useCallback, useEffect, useMemo, useState} from "react";
import {AppState, Button, Text, View} from "react-native";

export function NotificationScreen({onChange}: {onChange: (state: string) => void}) {
  const [count, setCount] = useState(0);
  useEffect(() => {
    const subscription = AppState.addEventListener("change", onChange);
    return () => subscription.remove();
  }, [onChange]);

  const addTwo = useCallback(() => {
    setCount(value => value + 1);
    setCount(value => value + 1);
  }, []);
  const label = useMemo(() => `Unread: ${count}`, [count]);
  return <View><Text>{label}</Text><Button title="Add two" onPress={addTwo} /></View>;
}
'''

_WECHAT_MINIPROGRAM = '''Page({
  data: {isAdmin: false},

  async onLoad() {
    const role = await getApp().services.fetchCurrentUserRole();
    this.setData({isAdmin: role === "admin"});
  },

  onPageScroll({scrollTop}) {
    this._scrollTop = scrollTop;
  },

  openAdminPanel() {
    if (!this.data.isAdmin) return;
    wx.navigateTo({url: "/pages/admin-panel/index"});
  },
});
'''

_JAVA_BACKEND = '''package com.example.orders;

import java.util.List;
import org.springframework.jdbc.core.JdbcTemplate;

public final class OrderService {
    private final JdbcTemplate jdbc;
    private final AuthorizationService authorization;

    public OrderService(JdbcTemplate jdbc, AuthorizationService authorization) {
        this.jdbc = jdbc;
        this.authorization = authorization;
    }

    public List<Order> findByCustomer(String customerName) {
        return jdbc.query(
            "SELECT id, total FROM orders WHERE customer_name = ?",
            Order.mapper(),
            customerName
        );
    }

    public void refund(User user, long orderId) {
        if (!authorization.canRefund(user, orderId)) {
            throw new SecurityException("refund denied");
        }
        jdbc.update("UPDATE orders SET refunded = true WHERE id = ?", orderId);
    }

    public int discountedCents(int totalCents, int discountBasisPoints) {
        return totalCents - totalCents * discountBasisPoints / 10_000;
    }
}
'''


def _fixture_definition(
    *,
    stack: str,
    language: str,
    target_path: str,
    source: str,
    old: str,
    new: str,
    marker: str,
    title: str,
    category: Optional[str],
    severity: Optional[str],
    extra_files: Optional[Mapping[str, str]] = None,
) -> Mapping[str, Any]:
    return {
        "stack": stack,
        "language": language,
        "target_path": target_path,
        "files": dict(extra_files or {}),
        "source": source,
        "replace": (old, new),
        "marker": marker,
        "title": title,
        "category": category,
        "severity": severity,
    }


_CASE_CHANGES = {
    "ts-web-dom-xss": _fixture_definition(
        stack=STACK_TYPESCRIPT_WEB,
        language="typescript",
        target_path="src/profile.ts",
        source=_TYPESCRIPT_WEB,
        old="  element.textContent = displayName;",
        new="  element.innerHTML = displayName;",
        marker="element.innerHTML = displayName",
        title="detect DOM XSS introduced in TypeScript",
        category="security",
        severity="critical",
        extra_files={"tsconfig.json": '{"compilerOptions":{"strict":true}}\n'},
    ),
    "ts-web-async-race": _fixture_definition(
        stack=STACK_TYPESCRIPT_WEB,
        language="typescript",
        target_path="src/profile.ts",
        source=_TYPESCRIPT_WEB,
        old=(
            "export class LatestSearch {\n"
            "  private requestId = 0;\n"
            "  value = \"\";\n\n"
            "  async run(query: string, fetcher: (value: string) => Promise<string>): Promise<void> {\n"
            "    const requestId = ++this.requestId;\n"
            "    const result = await fetcher(query);\n"
            "    if (requestId === this.requestId) this.value = result;"
        ),
        new=(
            "export class LatestSearch {\n"
            "  value = \"\";\n\n"
            "  async run(query: string, fetcher: (value: string) => Promise<string>): Promise<void> {\n"
            "    const result = await fetcher(query);\n"
            "    this.value = result;"
        ),
        marker="this.value = result",
        title="detect stale asynchronous search results",
        category="concurrency",
        severity="high",
        extra_files={"tsconfig.json": '{"compilerOptions":{"strict":true}}\n'},
    ),
    "ts-web-clean-refactor": _fixture_definition(
        stack=STACK_TYPESCRIPT_WEB,
        language="typescript",
        target_path="src/profile.ts",
        source=_TYPESCRIPT_WEB,
        old="  return value.trim();",
        new="  const normalized = value.trim();\n  return normalized;",
        marker="const normalized = value.trim()",
        title="avoid false positives on a TypeScript refactor",
        category=None,
        severity=None,
        extra_files={"tsconfig.json": '{"compilerOptions":{"strict":true}}\n'},
    ),
    "rn-listener-leak": _fixture_definition(
        stack=STACK_REACT_NATIVE,
        language="typescript-react",
        target_path="src/NotificationScreen.tsx",
        source=_REACT_NATIVE,
        old='    const subscription = AppState.addEventListener("change", onChange);\n    return () => subscription.remove();',
        new='    AppState.addEventListener("change", onChange);',
        marker='AppState.addEventListener("change", onChange)',
        title="detect a React Native event-listener leak",
        category="resource-management",
        severity="high",
        extra_files={"package.json": '{"private":true,"dependencies":{"react-native":"*"}}\n'},
    ),
    "rn-stale-state-update": _fixture_definition(
        stack=STACK_REACT_NATIVE,
        language="typescript-react",
        target_path="src/NotificationScreen.tsx",
        source=_REACT_NATIVE,
        old="    setCount(value => value + 1);\n    setCount(value => value + 1);\n  }, []);",
        new="    setCount(count + 1);\n    setCount(count + 1);\n  }, [count]);",
        marker="setCount(count + 1)",
        title="detect lost React Native state updates",
        category="correctness",
        severity="high",
        extra_files={"package.json": '{"private":true,"dependencies":{"react-native":"*"}}\n'},
    ),
    "rn-clean-memo-refactor": _fixture_definition(
        stack=STACK_REACT_NATIVE,
        language="typescript-react",
        target_path="src/NotificationScreen.tsx",
        source=_REACT_NATIVE,
        old='  const label = useMemo(() => `Unread: ${count}`, [count]);',
        new='  const unreadLabel = `Unread: ${count}`;\n  const label = useMemo(() => unreadLabel, [unreadLabel]);',
        marker="const unreadLabel",
        title="avoid false positives on a React Native memo refactor",
        category=None,
        severity=None,
        extra_files={"package.json": '{"private":true,"dependencies":{"react-native":"*"}}\n'},
    ),
    "mini-client-auth-trust": _fixture_definition(
        stack=STACK_WECHAT_MINIPROGRAM,
        language="javascript",
        target_path="pages/admin/index.js",
        source=_WECHAT_MINIPROGRAM,
        old=(
            "  async onLoad() {\n"
            "    const role = await getApp().services.fetchCurrentUserRole();\n"
            "    this.setData({isAdmin: role === \"admin\"});"
        ),
        new="  async onLoad(options) {\n    this.setData({isAdmin: options.isAdmin === \"1\"});",
        marker="options.isAdmin",
        title="detect trust in a client-controlled Mini Program route parameter",
        category="authorization",
        severity="critical",
        extra_files={"app.json": '{"pages":["pages/admin/index"]}\n'},
    ),
    "mini-scroll-setdata": _fixture_definition(
        stack=STACK_WECHAT_MINIPROGRAM,
        language="javascript",
        target_path="pages/admin/index.js",
        source=_WECHAT_MINIPROGRAM,
        old="    this._scrollTop = scrollTop;",
        new="    this.setData({scrollTop});",
        marker="this.setData({scrollTop})",
        title="detect high-frequency setData in a Mini Program scroll handler",
        category="performance",
        severity="high",
        extra_files={"app.json": '{"pages":["pages/admin/index"]}\n'},
    ),
    "mini-clean-data-refactor": _fixture_definition(
        stack=STACK_WECHAT_MINIPROGRAM,
        language="javascript",
        target_path="pages/admin/index.js",
        source=_WECHAT_MINIPROGRAM,
        old="  data: {isAdmin: false},",
        new="  data: {\n    isAdmin: false,\n  },",
        marker="isAdmin: false",
        title="avoid false positives on Mini Program data formatting",
        category=None,
        severity=None,
        extra_files={"app.json": '{"pages":["pages/admin/index"]}\n'},
    ),
    "java-sql-injection": _fixture_definition(
        stack=STACK_JAVA_BACKEND,
        language="java",
        target_path="src/main/java/com/example/orders/OrderService.java",
        source=_JAVA_BACKEND,
        old='            "SELECT id, total FROM orders WHERE customer_name = ?",\n            Order.mapper(),\n            customerName',
        new='            "SELECT id, total FROM orders WHERE customer_name = \'" + customerName + "\'",\n            Order.mapper()',
        marker='customer_name = \'" + customerName',
        title="detect SQL injection in a Spring Java service",
        category="security",
        severity="critical",
        extra_files={"pom.xml": "<project><modelVersion>4.0.0</modelVersion></project>\n"},
    ),
    "java-refund-auth-bypass": _fixture_definition(
        stack=STACK_JAVA_BACKEND,
        language="java",
        target_path="src/main/java/com/example/orders/OrderService.java",
        source=_JAVA_BACKEND,
        old="        if (!authorization.canRefund(user, orderId)) {",
        new="        if (user == null) {",
        marker="if (user == null)",
        title="detect an authorization bypass in a Java refund flow",
        category="authorization",
        severity="critical",
        extra_files={"pom.xml": "<project><modelVersion>4.0.0</modelVersion></project>\n"},
    ),
    "java-clean-calculation-refactor": _fixture_definition(
        stack=STACK_JAVA_BACKEND,
        language="java",
        target_path="src/main/java/com/example/orders/OrderService.java",
        source=_JAVA_BACKEND,
        old="        return totalCents - totalCents * discountBasisPoints / 10_000;",
        new="        int discount = totalCents * discountBasisPoints / 10_000;\n        return totalCents - discount;",
        marker="int discount =",
        title="avoid false positives on a Java calculation refactor",
        category=None,
        severity=None,
        extra_files={"pom.xml": "<project><modelVersion>4.0.0</modelVersion></project>\n"},
    ),
}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git_bytes(repository: Path, *argv: str) -> bytes:
    env = {
        "GIT_AUTHOR_NAME": "aceval",
        "GIT_AUTHOR_EMAIL": "aceval@example.invalid",
        "GIT_COMMITTER_NAME": "aceval",
        "GIT_COMMITTER_EMAIL": "aceval@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    }
    import os

    process_env = dict(os.environ)
    process_env.update(env)
    completed = subprocess.run(
        ("git",) + argv,
        cwd=str(repository),
        env=process_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise CodeReviewError(
            "git %s failed: %s"
            % (" ".join(argv), completed.stderr.decode("utf-8", errors="replace").strip())
        )
    return completed.stdout


def _git(repository: Path, *argv: str) -> str:
    return _git_bytes(repository, *argv).decode("utf-8", errors="replace").strip()


def _line_of(content: str, marker: str) -> int:
    for index, line in enumerate(content.splitlines(), 1):
        if marker in line:
            return index
    raise CodeReviewError("fixture marker was not found")


def _write_case_metadata(
    root: Path,
    repository: Path,
    case_id: str,
    definition: Mapping[str, Any],
    *,
    base_ref: str,
    head_ref: str,
    base_commit: str,
    head_commit: str,
    changed: str,
    target_path: str,
) -> FixtureCase:
    case_root = root / "cases" / case_id
    expected = []
    if definition["category"] is not None:
        line = _line_of(changed, definition["marker"])
        expected.append(
            {
                "id": case_id,
                "path": target_path,
                "line_start": line,
                "line_end": line,
                "category": definition["category"],
                "severity": definition["severity"],
                "critical": definition["severity"] == "critical",
            }
        )
    oracle_path = case_root / "oracle.json"
    _write(
        oracle_path,
        json.dumps(
            {
                "expected_findings": expected,
                "base_commit": base_commit,
                "head_commit": head_commit,
                "base_ref": base_ref,
                "head_ref": head_ref,
                "stack": definition["stack"],
                "language": definition["language"],
                "case_type": "defect" if expected else "clean",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    prompt = (
        "Review this %s (%s) change between %s (%s) and %s (%s). Return strict JSON with a findings "
        "array. Each finding must include path, line, category, severity, and a concise "
        "explanation. Report only actionable defects introduced by the change."
        % (
            definition["stack"], definition["language"], base_ref, base_commit,
            head_ref, head_commit,
        )
    )
    _write(
        case_root / "case.json",
        json.dumps(
            {
                "id": case_id,
                "title": definition["title"],
                "stack": definition["stack"],
                "language": definition["language"],
                "case_type": "defect" if expected else "clean",
                "prompt": prompt,
                "repository": "../../repository",
                "base_ref": base_ref,
                "head_ref": head_ref,
                "base_commit": base_commit,
                "head_commit": head_commit,
                "oracle": "oracle.json",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return FixtureCase(
        id=case_id,
        stack=str(definition["stack"]),
        language=str(definition["language"]),
        case_type="defect" if expected else "clean",
        repository=repository,
        base_ref=base_ref,
        head_ref=head_ref,
        base_commit=base_commit,
        head_commit=head_commit,
        oracle=oracle_path,
        prompt=prompt,
    )


def _create_unified_cases(
    root: Path,
    selected_stacks: Sequence[str],
) -> Tuple[FixtureCase, ...]:
    repository = root / "repository"
    repository.mkdir(parents=True)
    _write(
        repository / "README.md",
        "# ACEval code-review Fixture Lab\n\n"
        "Branches under `base/` contain stack baselines; branches under `case/` "
        "contain one deterministic review change.\n",
    )
    _git(repository, "init", "--quiet", "--initial-branch=master")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "fixture-lab-root")
    for stack in selected_stacks:
        definitions = [
            definition for definition in _CASE_CHANGES.values()
            if definition["stack"] == stack
        ]
        first = definitions[0]
        stack_root = repository / "fixtures" / stack
        _write(stack_root / str(first["target_path"]), str(first["source"]))
        for relative, content in first["files"].items():
            _write(stack_root / str(relative), str(content))
        _write(stack_root / "STACK.md", "Stack: `%s`\n" % stack)
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "fixture-lab-base")
    common_base_commit = _git(repository, "rev-parse", "HEAD")
    cases = []
    for stack in selected_stacks:
        definitions = [
            (case_id, definition)
            for case_id, definition in _CASE_CHANGES.items()
            if definition["stack"] == stack
        ]
        first = definitions[0][1]
        signature = (
            first["target_path"],
            first["source"],
            dict(first["files"]),
        )
        if any(
            (item[1]["target_path"], item[1]["source"], dict(item[1]["files"]))
            != signature
            for item in definitions[1:]
        ):
            raise CodeReviewError("all cases in a stack must share one base fixture")
        target_path = "fixtures/%s/%s" % (stack, first["target_path"])
        base_commit = common_base_commit
        base_ref = "base/%s" % stack
        _git(repository, "branch", "--force", base_ref, base_commit)

        for case_id, definition in definitions:
            _git(repository, "checkout", "--quiet", "--detach", base_commit)
            source = str(definition["source"])
            old, new = definition["replace"]
            if source.count(old) != 1:
                raise CodeReviewError(
                    "fixture replacement must match exactly once: %s" % case_id
                )
            changed = source.replace(old, new)
            _write(repository / target_path, changed)
            _git(repository, "add", target_path)
            _git(repository, "commit", "--quiet", "-m", case_id)
            head_commit = _git(repository, "rev-parse", "HEAD")
            head_ref = "case/%s" % case_id
            _git(repository, "branch", "--force", head_ref, head_commit)
            cases.append(
                _write_case_metadata(
                    root,
                    repository,
                    case_id,
                    definition,
                    base_ref=base_ref,
                    head_ref=head_ref,
                    base_commit=base_commit,
                    head_commit=head_commit,
                    changed=changed,
                    target_path=target_path,
                )
            )
    default_case = (
        DEFAULT_CODE_REVIEW_CASE
        if any(case.id == DEFAULT_CODE_REVIEW_CASE for case in cases)
        else cases[0].id
    )
    _git(repository, "checkout", "--quiet", "-B", "master", common_base_commit)
    return tuple(cases)


def create_code_review_fixture_lab(
    output: Union[str, Path],
    *,
    force: bool = False,
    stacks: Sequence[str] = DEFAULT_CODE_REVIEW_STACKS,
) -> Tuple[FixtureCase, ...]:
    if isinstance(stacks, (str, bytes)) or not isinstance(stacks, Sequence):
        raise CodeReviewError("stacks must be an array")
    selected_stacks = tuple(str(item) for item in stacks)
    if not selected_stacks:
        raise CodeReviewError("at least one code-review stack is required")
    if len(set(selected_stacks)) != len(selected_stacks):
        raise CodeReviewError("stacks must not contain duplicates")
    unknown_stacks = sorted(set(selected_stacks).difference(DEFAULT_CODE_REVIEW_STACKS))
    if unknown_stacks:
        raise CodeReviewError("unsupported code-review stacks: %s" % ", ".join(unknown_stacks))
    root = Path(output).expanduser().absolute()
    if root.is_symlink():
        raise CodeReviewError("fixture lab output cannot be a symlink")
    if root.exists():
        if not force:
            raise CodeReviewError("fixture lab output already exists")
        if not root.is_dir():
            raise CodeReviewError("fixture lab output must be a directory")
        allowed = {
            "repository", "cases", "evals", "lab.json", "findings.schema.json",
            ".DS_Store",
        }
        unexpected = {item.name for item in root.iterdir()}.difference(allowed)
        if unexpected:
            raise CodeReviewError("refusing to replace a non-lab directory")
        shutil.rmtree(root)
    root.mkdir(parents=True)
    cases = _create_unified_cases(root, selected_stacks)
    schema = {
        "type": "object",
        "required": ["findings"],
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["path", "line", "category", "severity", "explanation"],
                    "properties": {
                        "path": {"type": "string"},
                        "line": {"type": "integer", "minimum": 1},
                        "category": {"type": "string"},
                        "severity": {"type": "string"},
                        "explanation": {"type": "string"},
                    },
                },
            }
        },
    }
    _write(
        root / "findings.schema.json",
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    evals = {
        "skill_name": "code-review-skill-under-test",
        "evals": [
            {
                "id": index,
                "prompt": case.prompt,
                "expected_output": (
                    "The findings array contains every Oracle defect with valid source "
                    "references and no unsupported findings."
                    if case.case_type == "defect"
                    else "The findings array is empty because the change contains no "
                    "actionable defect."
                ),
                "files": [str(case.repository.relative_to(root))],
                "expectations": [
                    "Output is strict JSON with a findings array",
                    "All Oracle findings are reported",
                    "Every finding references an existing changed source line",
                    "No unsupported finding is reported",
                ],
            }
            for index, case in enumerate(cases, 1)
        ],
    }
    _write(
        root / "evals" / "evals.json",
        json.dumps(evals, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    lab = {
        "api_version": CODE_REVIEW_LAB_API_VERSION,
        "grader": CODE_REVIEW_GRADER_ID,
        "repository": "repository",
        "default_branch": "master",
        "default_case": DEFAULT_CODE_REVIEW_CASE if any(
            case.id == DEFAULT_CODE_REVIEW_CASE for case in cases
        ) else cases[0].id,
        "stacks": list(selected_stacks),
        "coverage": {
            stack: {
                "cases": sum(case.stack == stack for case in cases),
                "defect_cases": sum(
                    case.stack == stack and case.case_type == "defect" for case in cases
                ),
                "clean_cases": sum(
                    case.stack == stack and case.case_type == "clean" for case in cases
                ),
            }
            for stack in selected_stacks
        },
        "cases": [
            {
                "id": case.id,
                "stack": case.stack,
                "language": case.language,
                "case_type": case.case_type,
                "repository": str(case.repository.relative_to(root)),
                "base_ref": case.base_ref,
                "head_ref": case.head_ref,
                "base_commit": case.base_commit,
                "head_commit": case.head_commit,
                "oracle": str(case.oracle.relative_to(root)),
            }
            for case in cases
        ],
    }
    _write(
        root / "lab.json",
        json.dumps(lab, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return cases


def repository_observation(
    repository: Union[str, Path],
    findings: Any,
    *,
    revision: Optional[str] = None,
) -> RunObservation:
    root = Path(repository).expanduser().resolve()
    if root.is_symlink() or not root.is_dir():
        raise CodeReviewError("repository must be a regular directory")
    files = {}
    if revision is not None:
        commit = _git(root, "rev-parse", "%s^{commit}" % revision)
        entries = _git_bytes(root, "ls-tree", "-r", "-z", commit).split(b"\x00")
        for entry in entries:
            if not entry:
                continue
            try:
                header, raw_path = entry.split(b"\t", 1)
                mode, object_type, object_id = header.split(b" ", 2)
                relative = raw_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise CodeReviewError("repository tree contains an invalid entry") from exc
            if mode == b"120000" or object_type != b"blob":
                raise CodeReviewError("repository revision contains an unsupported entry")
            if _safe_path(relative) != relative:
                raise CodeReviewError("repository revision contains an unsafe path")
            content = _git_bytes(root, "cat-file", "blob", object_id.decode("ascii"))
            files[relative] = Artifact(path=relative, content=content, size=len(content))
    else:
        for path in sorted(root.rglob("*")):
            if ".git" in path.relative_to(root).parts:
                continue
            if path.is_symlink():
                raise CodeReviewError("repository contains a symlink")
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                content = path.read_bytes()
                files[relative] = Artifact(path=relative, content=content, size=len(content))
    return RunObservation(output=findings, pre_state=files, workspace_root=root)


def repository_verify_blueprint(
    image_id: str,
    *,
    base_commit: str,
    head_commit: str,
) -> EnvironmentBlueprint:
    """Create the pinned P0 validator blueprint for one review fixture."""

    return EnvironmentBlueprint(
        api_version=ENVIRONMENT_BLUEPRINT_API_VERSION,
        id="repository.verify/v1",
        image=image_id,
        capabilities=("git", "repository.verify/v1"),
        limits=EnvironmentLimits(
            cpus=1.0,
            memory_mb=512,
            pids=64,
            timeout_seconds=120.0,
            tmpfs_mb=64,
        ),
        health_checks=(
            CommandSpec(
                argv=("sh", "/opt/aceval/repository_verify.sh", "--self-check"),
                timeout_seconds=10.0,
            ),
        ),
        validation_commands=(
            CommandSpec(
                argv=("sh", "/opt/aceval/repository_verify.sh"),
                timeout_seconds=30.0,
                env={
                    "ACEVAL_BASE_COMMIT": base_commit,
                    "ACEVAL_HEAD_COMMIT": head_commit,
                },
            ),
        ),
        metadata={
            "contract": "repository.verify/v1",
            "base_commit": base_commit,
            "head_commit": head_commit,
        },
    )


__all__ = [
    "CODE_REVIEW_GRADER_ID",
    "CODE_REVIEW_GRADER_VERSION",
    "CODE_REVIEW_LAB_API_VERSION",
    "DEFAULT_CODE_REVIEW_CASE",
    "DEFAULT_CODE_REVIEW_STACKS",
    "STACK_JAVA_BACKEND",
    "STACK_REACT_NATIVE",
    "STACK_TYPESCRIPT_WEB",
    "STACK_WECHAT_MINIPROGRAM",
    "CodeReviewError",
    "CodeReviewFindingsGrader",
    "FixtureCase",
    "create_code_review_fixture_lab",
    "infer_code_review_stacks",
    "repository_observation",
    "repository_verify_blueprint",
]
