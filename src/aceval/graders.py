"""Domain-neutral deterministic graders shipped with EvalOps Kernel v1."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    Artifact,
    GradeResult,
    GradeStatus,
    RunObservation,
    as_primitive,
    canonical_trace_kind,
)


_MISSING = object()


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _oracle_data(oracle: Any) -> Any:
    return _get(oracle, "data", oracle)


def _oracle_metadata(oracle: Any) -> Mapping[str, Any]:
    value = _get(oracle, "metadata", {})
    return value if isinstance(value, Mapping) else {}


def _grade(
    grader_id: str,
    status: GradeStatus,
    message: str,
    version: str,
    *,
    score: Optional[float] = None,
    hard: bool = True,
    metrics: Optional[Mapping[str, Any]] = None,
    evidence: Iterable[Any] = (),
    missing: Iterable[str] = (),
) -> GradeResult:
    if score is None:
        if status == GradeStatus.PASS:
            score = 1.0
        elif status == GradeStatus.FAIL:
            score = 0.0
    return GradeResult(
        grader_id=grader_id,
        status=status,
        version=version,
        score=score,
        hard=hard,
        metrics=dict(metrics or {}),
        evidence=tuple(evidence),
        missing=tuple(missing),
        message=message,
    )


def _hard(params: Mapping[str, Any]) -> bool:
    return bool(params.get("hard", True))


def _artifact_content(value: Any) -> Any:
    if isinstance(value, Artifact):
        return value.content
    if isinstance(value, Mapping) and "content" in value:
        return value["content"]
    return value


def _payload(observation: RunObservation, params: Mapping[str, Any]) -> Any:
    artifact_path = params.get("artifact_path")
    if artifact_path is None:
        return observation.output
    artifact = observation.artifacts.get(str(artifact_path), _MISSING)
    if artifact is _MISSING:
        return _MISSING
    return _artifact_content(artifact)


def _json_document(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value, parse_constant=_reject_json_constant)
    encoded = json.dumps(
        as_primitive(value), allow_nan=False, separators=(",", ":")
    )
    return json.loads(encoded, parse_constant=_reject_json_constant)


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant: %s" % value)


def _json_equal(left: Any, right: Any) -> bool:
    """Compare values using JSON equality, keeping booleans distinct from numbers."""
    left_number = isinstance(left, (int, float)) and not isinstance(left, bool)
    right_number = isinstance(right, (int, float)) and not isinstance(right, bool)
    if left_number or right_number:
        return left_number and right_number and left == right
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        return set(left) == set(right) and all(
            _json_equal(left[key], right[key]) for key in left
        )
    left_array = isinstance(left, Sequence) and not isinstance(left, (str, bytes))
    right_array = isinstance(right, Sequence) and not isinstance(right, (str, bytes))
    if left_array or right_array:
        return (
            left_array
            and right_array
            and len(left) == len(right)
            and all(_json_equal(a, b) for a, b in zip(left, right))
        )
    return type(left) is type(right) and left == right


_PATH_PART = re.compile(
    r"(?:\.([A-Za-z_][A-Za-z0-9_-]*))"
    r"|(?:\[['\"]([^'\"]+)['\"]\])"
    r"|(?:\[(\d+)\])"
    r"|(?:\[\*\])"
)


def _parse_json_path(path: str) -> Tuple[Any, ...]:
    if not isinstance(path, str) or not path:
        raise ValueError("JSONPath must be a non-empty string")
    if not path.startswith("$"):
        path = "$." + path
    if path == "$":
        return ()
    position = 1
    parts = []
    while position < len(path):
        match = _PATH_PART.match(path, position)
        if match is None:
            raise ValueError("unsupported JSONPath syntax at offset %s" % position)
        if match.group(1) is not None:
            parts.append(match.group(1))
        elif match.group(2) is not None:
            parts.append(match.group(2))
        elif match.group(3) is not None:
            parts.append(int(match.group(3)))
        else:
            parts.append("*")
        position = match.end()
    return tuple(parts)


def _json_path(document: Any, path: str) -> List[Any]:
    values = [document]
    for part in _parse_json_path(path):
        next_values = []
        for value in values:
            if part == "*":
                if isinstance(value, Mapping):
                    next_values.extend(value.values())
                elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    next_values.extend(value)
            elif isinstance(part, int):
                if (
                    isinstance(value, Sequence)
                    and not isinstance(value, (str, bytes))
                    and 0 <= part < len(value)
                ):
                    next_values.append(value[part])
            elif isinstance(value, Mapping) and part in value:
                next_values.append(value[part])
        values = next_values
    return values


def _oracle_value(oracle: Any, path: str) -> Any:
    values = _json_path(_oracle_data(oracle), path)
    if not values:
        return _MISSING
    return values[0] if len(values) == 1 else values


class _SchemaDefinitionError(ValueError):
    pass


class _UnsupportedSchema(_SchemaDefinitionError):
    pass


_SCHEMA_ANNOTATIONS = {
    "$schema",
    "$id",
    "$anchor",
    "title",
    "description",
    "default",
    "examples",
    "deprecated",
    "readOnly",
    "writeOnly",
    "$comment",
    "$defs",
    "definitions",
}

_SCHEMA_ASSERTIONS = {
    "$ref",
    "type",
    "enum",
    "const",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minProperties",
    "maxProperties",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
}


def _schema_type(value: Any, expected: str) -> bool:
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "object": isinstance(value, Mapping),
        "array": isinstance(value, Sequence) and not isinstance(value, (str, bytes)),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "string": isinstance(value, str),
    }.get(expected, False)


def _resolve_schema_ref(root: Any, reference: str) -> Any:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise _UnsupportedSchema("only local JSON Schema $ref values are supported")
    current = root
    for raw in reference[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or key not in current:
            raise _SchemaDefinitionError("JSON Schema $ref does not exist: %s" % reference)
        current = current[key]
    return current


def _validate_schema_definition(
    schema: Any, root: Any, active: Optional[set] = None
) -> None:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, Mapping):
        raise _SchemaDefinitionError("JSON Schema nodes must be objects or booleans")
    unsupported = set(schema) - _SCHEMA_ANNOTATIONS - _SCHEMA_ASSERTIONS
    if unsupported:
        raise _UnsupportedSchema(
            "unsupported JSON Schema keywords: %s" % ", ".join(sorted(unsupported))
        )
    active = active if active is not None else set()
    marker = id(schema)
    if marker in active:
        raise _UnsupportedSchema("cyclic JSON Schema references are not supported")
    active.add(marker)
    try:
        if "$ref" in schema:
            _validate_schema_definition(
                _resolve_schema_ref(root, schema["$ref"]), root, active
            )

        expected_types = schema.get("type")
        if expected_types is not None:
            if isinstance(expected_types, str):
                expected_types = (expected_types,)
            if (
                not isinstance(expected_types, Sequence)
                or isinstance(expected_types, (str, bytes))
                or not expected_types
            ):
                raise _SchemaDefinitionError("type must be a string or non-empty array")
            known_types = {
                "null",
                "boolean",
                "object",
                "array",
                "number",
                "integer",
                "string",
            }
            if any(not isinstance(item, str) or item not in known_types for item in expected_types):
                raise _SchemaDefinitionError("JSON Schema contains an unknown type")

        if "enum" in schema and (
            not isinstance(schema["enum"], Sequence)
            or isinstance(schema["enum"], (str, bytes))
            or not schema["enum"]
        ):
            raise _SchemaDefinitionError("enum must be a non-empty array")

        for keyword in ("allOf", "anyOf", "oneOf"):
            if keyword not in schema:
                continue
            options = schema[keyword]
            if (
                not isinstance(options, Sequence)
                or isinstance(options, (str, bytes))
                or not options
            ):
                raise _SchemaDefinitionError("%s must be a non-empty array" % keyword)
            for option in options:
                _validate_schema_definition(option, root, active)
        if "not" in schema:
            _validate_schema_definition(schema["not"], root, active)

        for definitions_key in ("$defs", "definitions"):
            if definitions_key not in schema:
                continue
            definitions = schema[definitions_key]
            if not isinstance(definitions, Mapping):
                raise _SchemaDefinitionError("%s must be an object" % definitions_key)
            for child in definitions.values():
                _validate_schema_definition(child, root, active)

        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise _SchemaDefinitionError("properties must be an object")
        for child in properties.values():
            _validate_schema_definition(child, root, active)

        additional = schema.get("additionalProperties", True)
        if not isinstance(additional, (Mapping, bool)):
            raise _SchemaDefinitionError("additionalProperties must be boolean or schema")
        if isinstance(additional, Mapping):
            _validate_schema_definition(additional, root, active)

        if "items" in schema:
            _validate_schema_definition(schema["items"], root, active)

        required = schema.get("required", ())
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
            raise _SchemaDefinitionError("required must be an array")
        if any(not isinstance(item, str) for item in required):
            raise _SchemaDefinitionError("required entries must be strings")

        for keyword in (
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "minProperties",
            "maxProperties",
        ):
            if keyword in schema and (
                not isinstance(schema[keyword], int)
                or isinstance(schema[keyword], bool)
                or schema[keyword] < 0
            ):
                raise _SchemaDefinitionError("%s must be a non-negative integer" % keyword)
        if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
            raise _SchemaDefinitionError("uniqueItems must be boolean")

        if "pattern" in schema:
            if not isinstance(schema["pattern"], str):
                raise _SchemaDefinitionError("pattern must be a string")
            try:
                re.compile(schema["pattern"])
            except re.error as exc:
                raise _SchemaDefinitionError("invalid Schema pattern") from exc
        if "format" in schema and schema["format"] not in ("email",):
            raise _UnsupportedSchema(
                "unsupported JSON Schema format: %s" % schema["format"]
            )

        for keyword in (
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
        ):
            if keyword in schema and (
                not isinstance(schema[keyword], (int, float))
                or isinstance(schema[keyword], bool)
                or not math.isfinite(float(schema[keyword]))
            ):
                raise _SchemaDefinitionError("%s must be a finite number" % keyword)
        if "multipleOf" in schema:
            divisor = schema["multipleOf"]
            if (
                not isinstance(divisor, (int, float))
                or isinstance(divisor, bool)
                or not math.isfinite(float(divisor))
                or divisor <= 0
            ):
                raise _SchemaDefinitionError("multipleOf must be a positive finite number")
    finally:
        active.remove(marker)


def _validate_schema(instance: Any, schema: Any, root: Any, path: str = "$") -> List[str]:
    if schema is True:
        return []
    if schema is False:
        return ["%s is rejected by the boolean schema" % path]
    if not isinstance(schema, Mapping):
        raise _SchemaDefinitionError("JSON Schema nodes must be objects or booleans")
    unsupported = set(schema) - _SCHEMA_ANNOTATIONS - _SCHEMA_ASSERTIONS
    if unsupported:
        raise _UnsupportedSchema(
            "unsupported JSON Schema keywords: %s" % ", ".join(sorted(unsupported))
        )
    errors = []  # type: List[str]
    if "$ref" in schema:
        errors.extend(
            _validate_schema(
                instance, _resolve_schema_ref(root, schema["$ref"]), root, path
            )
        )

    expected_types = schema.get("type")
    if expected_types is not None:
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if not isinstance(expected_types, Sequence) or not expected_types:
            raise _SchemaDefinitionError("type must be a string or non-empty array")
        known_types = {"null", "boolean", "object", "array", "number", "integer", "string"}
        if any(item not in known_types for item in expected_types):
            raise _SchemaDefinitionError("JSON Schema contains an unknown type")
        if not any(_schema_type(instance, item) for item in expected_types):
            return ["%s has the wrong type; expected %s" % (path, expected_types)]

    if "enum" in schema:
        if not isinstance(schema["enum"], Sequence) or isinstance(schema["enum"], (str, bytes)):
            raise _SchemaDefinitionError("enum must be an array")
        if not any(_json_equal(instance, option) for option in schema["enum"]):
            errors.append("%s is not one of the allowed enum values" % path)
    if "const" in schema and not _json_equal(instance, schema["const"]):
        errors.append("%s does not equal const" % path)

    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword not in schema:
            continue
        options = schema[keyword]
        if not isinstance(options, Sequence) or isinstance(options, (str, bytes)) or not options:
            raise _SchemaDefinitionError("%s must be a non-empty array" % keyword)
        option_errors = [_validate_schema(instance, item, root, path) for item in options]
        passing = sum(not item for item in option_errors)
        if keyword == "allOf":
            for item in option_errors:
                errors.extend(item)
        elif keyword == "anyOf" and passing == 0:
            errors.append("%s does not match any anyOf branch" % path)
        elif keyword == "oneOf" and passing != 1:
            errors.append("%s must match exactly one oneOf branch" % path)
    if "not" in schema and not _validate_schema(instance, schema["not"], root, path):
        errors.append("%s matches the forbidden not schema" % path)

    if isinstance(instance, Mapping):
        required = schema.get("required", ())
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
            raise _SchemaDefinitionError("required must be an array")
        for key in required:
            if not isinstance(key, str):
                raise _SchemaDefinitionError("required entries must be strings")
            if key not in instance:
                errors.append("%s.%s is required" % (path, key))
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise _SchemaDefinitionError("properties must be an object")
        for key, child_schema in properties.items():
            if key in instance:
                errors.extend(
                    _validate_schema(instance[key], child_schema, root, "%s.%s" % (path, key))
                )
        additional = schema.get("additionalProperties", True)
        extras = set(instance) - set(properties)
        if additional is False:
            for key in sorted(extras):
                errors.append("%s.%s is an additional property" % (path, key))
        elif isinstance(additional, Mapping) or isinstance(additional, bool):
            if additional is not True:
                for key in extras:
                    errors.extend(
                        _validate_schema(instance[key], additional, root, "%s.%s" % (path, key))
                    )
        else:
            raise _SchemaDefinitionError("additionalProperties must be boolean or schema")
        for keyword, comparator in (("minProperties", lambda size, bound: size < bound),
                                    ("maxProperties", lambda size, bound: size > bound)):
            if keyword in schema and comparator(len(instance), schema[keyword]):
                errors.append("%s violates %s" % (path, keyword))

    if isinstance(instance, Sequence) and not isinstance(instance, (str, bytes)):
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(_validate_schema(item, schema["items"], root, "%s[%s]" % (path, index)))
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append("%s has fewer than minItems" % path)
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append("%s has more than maxItems" % path)
        if schema.get("uniqueItems"):
            if any(
                _json_equal(item, other)
                for index, item in enumerate(instance)
                for other in instance[index + 1 :]
            ):
                errors.append("%s items are not unique" % path)

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append("%s is shorter than minLength" % path)
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append("%s is longer than maxLength" % path)
        if "pattern" in schema:
            try:
                matched = re.search(schema["pattern"], instance)
            except (TypeError, re.error) as exc:
                raise _SchemaDefinitionError("invalid Schema pattern") from exc
            if matched is None:
                errors.append("%s does not match pattern" % path)
        if "format" in schema:
            format_name = schema["format"]
            if format_name == "email" and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", instance):
                errors.append("%s is not an email" % path)
            elif format_name not in ("email",):
                raise _UnsupportedSchema("unsupported JSON Schema format: %s" % format_name)

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        comparisons = (
            ("minimum", lambda bound: instance < bound),
            ("maximum", lambda bound: instance > bound),
            ("exclusiveMinimum", lambda bound: instance <= bound),
            ("exclusiveMaximum", lambda bound: instance >= bound),
        )
        for keyword, comparator in comparisons:
            if keyword in schema:
                try:
                    invalid = comparator(schema[keyword])
                except TypeError as exc:
                    raise _SchemaDefinitionError("%s must be numeric" % keyword) from exc
                if invalid:
                    errors.append("%s violates %s" % (path, keyword))
        if "multipleOf" in schema:
            divisor = schema["multipleOf"]
            if not isinstance(divisor, (int, float)) or isinstance(divisor, bool) or divisor <= 0:
                raise _SchemaDefinitionError("multipleOf must be a positive number")
            quotient = instance / divisor
            if not math.isclose(quotient, round(quotient), abs_tol=1e-9):
                errors.append("%s is not a multipleOf %s" % (path, divisor))
    return errors


def _safe_resource(ref: str, oracle: Any, params: Mapping[str, Any]) -> Any:
    resources = params.get("resources") or _oracle_metadata(oracle).get("resources", {})
    if isinstance(resources, Mapping) and ref in resources:
        return resources[ref]
    root_value = params.get("pack_root") or _oracle_metadata(oracle).get("pack_root")
    if root_value is None:
        return _MISSING
    root = Path(str(root_value)).resolve()
    relative = Path(ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise _SchemaDefinitionError("schema_ref must remain under pack_root")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise _SchemaDefinitionError("schema_ref escapes pack_root") from exc
    if not path.is_file() or path.is_symlink():
        return _MISSING
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise _SchemaDefinitionError("schema_ref is not valid JSON") from exc


class JsonSchemaGrader:
    id = "json_schema"
    version = "1.0.0"

    async def evaluate(self, observation: RunObservation, oracle: Any, params: Mapping[str, Any]) -> GradeResult:
        hard = _hard(params)
        value = _payload(observation, params)
        if value is _MISSING:
            return _grade(self.id, GradeStatus.NOT_EVALUABLE, "artifact was not observed", self.version,
                          hard=hard, missing=("artifact:%s" % params.get("artifact_path"),))
        if value is None:
            return _grade(self.id, GradeStatus.NOT_EVALUABLE, "final output was not observed", self.version,
                          hard=hard, missing=("final_output",))
        schema = params.get("schema", _MISSING)
        if schema is _MISSING and params.get("schema_ref"):
            try:
                schema = _safe_resource(str(params["schema_ref"]), oracle, params)
            except _SchemaDefinitionError as exc:
                return _grade(self.id, GradeStatus.ERROR, str(exc), self.version, hard=hard)
        if schema is _MISSING:
            data = _oracle_data(oracle)
            schema = data.get("schema", _MISSING) if isinstance(data, Mapping) else _MISSING
        if schema is _MISSING:
            return _grade(self.id, GradeStatus.NOT_EVALUABLE, "no JSON Schema was supplied", self.version,
                          hard=hard, missing=("schema",))
        try:
            _validate_schema_definition(schema, schema)
        except _SchemaDefinitionError as exc:
            return _grade(self.id, GradeStatus.ERROR, "invalid or unsupported schema: %s" % exc,
                          self.version, hard=hard)
        try:
            document = _json_document(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return _grade(self.id, GradeStatus.FAIL, "output is not valid JSON: %s" % exc, self.version,
                          hard=hard, evidence=({"kind": "parse_error", "detail": str(exc)},))
        try:
            errors = _validate_schema(document, schema, schema)
        except _SchemaDefinitionError as exc:
            return _grade(self.id, GradeStatus.ERROR, "invalid or unsupported schema: %s" % exc,
                          self.version, hard=hard)
        if errors:
            return _grade(self.id, GradeStatus.FAIL, "JSON output does not match the schema", self.version,
                          hard=hard, metrics={"error_count": len(errors)},
                          evidence=({"kind": "schema_error", "detail": item} for item in errors[:20]))
        return _grade(self.id, GradeStatus.PASS, "JSON output matches the schema", self.version,
                      hard=hard, metrics={"error_count": 0})


def _check_json_value(value: Any, check: Mapping[str, Any], expected: Any) -> List[str]:
    failures = []
    if "equals" in check and not _json_equal(value, check["equals"]):
        failures.append("does not equal the expected value")
    if expected is not _MISSING and not _json_equal(value, expected):
        failures.append("does not equal the oracle value")
    if "not_equals" in check and _json_equal(value, check["not_equals"]):
        failures.append("equals a forbidden value")
    if "one_of" in check and not any(
        _json_equal(value, option) for option in check["one_of"]
    ):
        failures.append("is not in one_of")
    if "forbidden_values" in check and any(
        _json_equal(value, option) for option in check["forbidden_values"]
    ):
        failures.append("is in forbidden_values")
    for keyword, comparator in (
        ("min", lambda actual, bound: actual < bound),
        ("max", lambda actual, bound: actual > bound),
        ("min_length", lambda actual, bound: len(actual) < bound),
        ("max_length", lambda actual, bound: len(actual) > bound),
    ):
        if keyword in check:
            try:
                if comparator(value, check[keyword]):
                    failures.append("violates %s" % keyword)
            except (TypeError, ValueError):
                failures.append("cannot be compared using %s" % keyword)
    if "regex" in check:
        if not isinstance(value, str) or re.search(str(check["regex"]), value) is None:
            failures.append("does not match regex")
    return failures


class JsonPathGrader:
    id = "json_path"
    version = "1.0.0"

    async def evaluate(self, observation: RunObservation, oracle: Any, params: Mapping[str, Any]) -> GradeResult:
        hard = _hard(params)
        payload = _payload(observation, params)
        if payload is _MISSING:
            return _grade(self.id, GradeStatus.NOT_EVALUABLE, "artifact was not observed", self.version,
                          hard=hard, missing=("artifact",))
        if payload is None:
            return _grade(self.id, GradeStatus.NOT_EVALUABLE, "final output was not observed", self.version,
                          hard=hard, missing=("final_output",))
        try:
            document = _json_document(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return _grade(self.id, GradeStatus.FAIL, "output is not valid JSON: %s" % exc, self.version, hard=hard)
        checks = params.get("checks")
        if checks is None:
            checks = (params,)
        if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes)) or not checks:
            return _grade(self.id, GradeStatus.ERROR, "checks must be a non-empty list", self.version, hard=hard)
        failures = []
        evaluated = 0
        try:
            for check in checks:
                if not isinstance(check, Mapping) or not check.get("path"):
                    raise ValueError("each JSONPath check requires path")
                path = str(check["path"])
                values = _json_path(document, path)
                should_exist = bool(check.get("exists", True))
                if not values:
                    if should_exist:
                        failures.append({"path": path, "detail": "path does not exist"})
                    evaluated += 1
                    continue
                if not should_exist:
                    failures.append({"path": path, "detail": "forbidden path exists"})
                    evaluated += 1
                    continue
                oracle_path = check.get("equals_from_oracle") or check.get("oracle_key")
                expected = _oracle_value(oracle, str(oracle_path)) if oracle_path else _MISSING
                if oracle_path and expected is _MISSING:
                    return _grade(self.id, GradeStatus.NOT_EVALUABLE,
                                  "oracle value does not exist: %s" % oracle_path, self.version,
                                  hard=hard, missing=("oracle:%s" % oracle_path,))
                comparison_values = (
                    [values]
                    if len(values) > 1
                    and isinstance(expected, Sequence)
                    and not isinstance(expected, (str, bytes))
                    else values
                )
                for value in comparison_values:
                    for detail in _check_json_value(value, check, expected):
                        failures.append({"path": path, "detail": detail, "actual": value})
                evaluated += 1
        except (TypeError, ValueError, re.error) as exc:
            return _grade(self.id, GradeStatus.ERROR, "invalid JSONPath grader params: %s" % exc,
                          self.version, hard=hard)
        if failures:
            return _grade(self.id, GradeStatus.FAIL, "one or more JSONPath checks failed", self.version,
                          hard=hard, score=max(0.0, 1.0 - len(failures) / max(1, evaluated)),
                          metrics={"checks": evaluated, "failures": len(failures)}, evidence=failures)
        return _grade(self.id, GradeStatus.PASS, "all JSONPath checks passed", self.version,
                      hard=hard, metrics={"checks": evaluated, "failures": 0})


def _collection(document: Any, path: str) -> List[Any]:
    values = _json_path(document, path)
    if len(values) == 1 and isinstance(values[0], Sequence) and not isinstance(values[0], (str, bytes)):
        return list(values[0])
    return values


def _record_matches(actual: Any, expected: Any, fields: Sequence[str], exact: bool) -> bool:
    if fields:
        if not isinstance(actual, Mapping) or not isinstance(expected, Mapping):
            return False
        return all(
            field in actual
            and field in expected
            and _json_equal(actual[field], expected[field])
            for field in fields
        )
    if not isinstance(actual, Mapping) or not isinstance(expected, Mapping) or exact:
        return _json_equal(actual, expected)
    return all(
        key in actual and _json_equal(actual[key], value)
        for key, value in expected.items()
    )


class RecordMatchGrader:
    id = "record_match"
    version = "1.0.0"

    async def evaluate(self, observation: RunObservation, oracle: Any, params: Mapping[str, Any]) -> GradeResult:
        hard = _hard(params)
        payload = _payload(observation, params)
        if payload is _MISSING or payload is None:
            missing = "artifact" if payload is _MISSING else "final_output"
            return _grade(self.id, GradeStatus.NOT_EVALUABLE, "%s was not observed" % missing,
                          self.version, hard=hard, missing=(missing,))
        try:
            document = _json_document(payload)
            actual = _collection(document, str(params.get("collection_path", "$")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            return _grade(self.id, GradeStatus.FAIL, "record collection cannot be read: %s" % exc,
                          self.version, hard=hard)
        oracle_data = _oracle_data(oracle)
        expected_key = str(params.get("expected_key", "expected_records"))
        forbidden_key = str(params.get("forbidden_key", "forbidden_records"))
        expected = params.get("expected_records", _MISSING)
        forbidden = params.get("forbidden_records", _MISSING)
        if expected is _MISSING and isinstance(oracle_data, Mapping):
            expected = _oracle_value(oracle, expected_key)
        if forbidden is _MISSING and isinstance(oracle_data, Mapping):
            forbidden = _oracle_value(oracle, forbidden_key)
        if expected is _MISSING:
            expected = []
        if forbidden is _MISSING:
            forbidden = []
        if not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)):
            return _grade(self.id, GradeStatus.ERROR, "expected records must be an array", self.version, hard=hard)
        if not isinstance(forbidden, Sequence) or isinstance(forbidden, (str, bytes)):
            return _grade(self.id, GradeStatus.ERROR, "forbidden records must be an array", self.version, hard=hard)
        if not expected and not forbidden:
            return _grade(self.id, GradeStatus.NOT_EVALUABLE, "oracle has no record assertions", self.version,
                          hard=hard, missing=("expected_records|forbidden_records",))
        fields = params.get("match_fields", ())
        if isinstance(fields, str):
            fields = (fields,)
        exact = bool(params.get("exact", False))
        unmatched = []
        consumed = set()
        for record in expected:
            match_index = next(
                (index for index, item in enumerate(actual)
                 if index not in consumed and _record_matches(item, record, fields, exact)),
                None,
            )
            if match_index is None:
                unmatched.append(record)
            else:
                consumed.add(match_index)
        forbidden_hits = [
            {"expected": record, "actual": item}
            for record in forbidden
            for item in actual
            if _record_matches(item, record, fields, exact)
        ]
        extra = []
        if params.get("allow_extra", True) is False:
            extra = [item for index, item in enumerate(actual) if index not in consumed]
        failures = len(unmatched) + len(forbidden_hits) + len(extra)
        metrics = {
            "actual_records": len(actual),
            "expected_records": len(expected),
            "matched_records": len(expected) - len(unmatched),
            "forbidden_hits": len(forbidden_hits),
            "extra_records": len(extra),
        }
        if failures:
            evidence = []
            evidence.extend({"kind": "missing_record", "record": item} for item in unmatched)
            evidence.extend({"kind": "forbidden_record", **item} for item in forbidden_hits)
            evidence.extend({"kind": "extra_record", "record": item} for item in extra)
            denominator = max(1, len(expected) + len(forbidden))
            return _grade(self.id, GradeStatus.FAIL, "record assertions failed", self.version, hard=hard,
                          score=max(0.0, 1.0 - failures / denominator), metrics=metrics, evidence=evidence)
        return _grade(self.id, GradeStatus.PASS, "record assertions passed", self.version,
                      hard=hard, metrics=metrics)


def _artifact_metadata(value: Any, path: str) -> Mapping[str, Any]:
    content = _artifact_content(value)
    if isinstance(content, str):
        encoded = content.encode("utf-8")
    elif isinstance(content, bytes):
        encoded = content
    elif content is None:
        encoded = None
    else:
        encoded = json.dumps(
            as_primitive(content), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
    size = _get(value, "size", len(encoded) if encoded is not None else None)
    digest = _get(value, "content_hash", hashlib.sha256(encoded).hexdigest() if encoded is not None else None)
    mime = _get(value, "mime_type")
    return {"path": path, "size": size, "sha256": digest, "mime": mime, "content": content}


class ArtifactExistsGrader:
    id = "artifact_exists"
    version = "1.0.0"

    async def evaluate(self, observation: RunObservation, oracle: Any, params: Mapping[str, Any]) -> GradeResult:
        del oracle
        hard = _hard(params)
        paths = params.get("paths", params.get("path", ()))
        if isinstance(paths, str):
            paths = (paths,)
        if not isinstance(paths, Sequence) or not paths:
            return _grade(self.id, GradeStatus.ERROR, "path or paths is required", self.version, hard=hard)
        failures = []
        checked = []
        for raw_path in paths:
            path = str(raw_path)
            relative = Path(path)
            if relative.is_absolute() or ".." in relative.parts or not path:
                return _grade(self.id, GradeStatus.ERROR, "artifact paths must be relative", self.version, hard=hard)
            value = observation.artifacts.get(relative.as_posix(), _MISSING)
            if value is _MISSING:
                failures.append({"path": path, "detail": "artifact is missing"})
                continue
            metadata = _artifact_metadata(value, path)
            checked.append(metadata)
            for minimum_key in ("min_bytes", "min_size"):
                if minimum_key in params and (metadata["size"] is None or metadata["size"] < params[minimum_key]):
                    failures.append({"path": path, "detail": "artifact is too small"})
            for maximum_key in ("max_bytes", "max_size"):
                if maximum_key in params and (metadata["size"] is None or metadata["size"] > params[maximum_key]):
                    failures.append({"path": path, "detail": "artifact is too large"})
            expected_mime = params.get("mime") or params.get("mime_type")
            if isinstance(expected_mime, Mapping):
                expected_mime = expected_mime.get(path)
            if expected_mime and metadata["mime"] != expected_mime:
                failures.append({"path": path, "detail": "MIME type does not match"})
            expected_hash = params.get("sha256")
            if isinstance(expected_hash, Mapping):
                expected_hash = expected_hash.get(path)
            if expected_hash and metadata["sha256"] != expected_hash:
                failures.append({"path": path, "detail": "SHA-256 does not match"})
        metrics = {"required": len(paths), "observed": len(checked), "failures": len(failures)}
        if failures:
            return _grade(self.id, GradeStatus.FAIL, "artifact assertions failed", self.version,
                          hard=hard, metrics=metrics, evidence=failures)
        return _grade(self.id, GradeStatus.PASS, "all required artifacts exist", self.version,
                      hard=hard, metrics=metrics, evidence=checked)


def _state_content(value: Any) -> Optional[bytes]:
    content = _artifact_content(value)
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, bytes):
        return content
    return None


def _record_field(record: Any, path: str) -> Any:
    values = _json_path(record, path)
    return values[0] if values else _MISSING


class SourceReferenceGrader:
    id = "source_reference"
    version = "1.0.0"

    async def evaluate(self, observation: RunObservation, oracle: Any, params: Mapping[str, Any]) -> GradeResult:
        del oracle
        hard = _hard(params)
        payload = _payload(observation, params)
        if payload is _MISSING or payload is None:
            missing = "artifact" if payload is _MISSING else "final_output"
            return _grade(self.id, GradeStatus.NOT_EVALUABLE, "%s was not observed" % missing,
                          self.version, hard=hard, missing=(missing,))
        if not observation.pre_state and not observation.post_state:
            return _grade(self.id, GradeStatus.NOT_EVALUABLE, "workspace state was not observed", self.version,
                          hard=hard, missing=("workspace_state",))
        try:
            document = _json_document(payload)
            if params.get("file_path") and params.get("line_path"):
                files = _json_path(document, str(params["file_path"]))
                lines = _json_path(document, str(params["line_path"]))
                if len(files) != len(lines):
                    return _grade(self.id, GradeStatus.FAIL, "file and line reference counts differ",
                                  self.version, hard=hard)
                references = list(zip(files, lines))
            else:
                records = _collection(document, str(params.get("collection_path", "$")))
                file_field = str(params.get("file_field", "file"))
                line_field = str(params.get("line_field", "line"))
                references = [(_record_field(item, file_field), _record_field(item, line_field)) for item in records]
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            return _grade(self.id, GradeStatus.FAIL, "source references cannot be read: %s" % exc,
                          self.version, hard=hard)
        if not references:
            if params.get("allow_empty", False):
                return _grade(self.id, GradeStatus.PASS, "no source references were required", self.version, hard=hard)
            return _grade(self.id, GradeStatus.FAIL, "no source references were emitted", self.version, hard=hard)
        state = dict(observation.pre_state)
        state.update(observation.post_state)
        failures = []
        unverifiable = []
        for file_value, line_value in references:
            if file_value is _MISSING or line_value is _MISSING:
                failures.append({"file": file_value, "line": line_value, "detail": "reference fields are missing"})
                continue
            path = Path(str(file_value))
            if path.is_absolute() or ".." in path.parts or not str(path):
                failures.append({"file": str(file_value), "line": line_value, "detail": "file path is unsafe"})
                continue
            key = path.as_posix()
            source = state.get(key, _MISSING)
            if source is _MISSING:
                failures.append({"file": key, "line": line_value, "detail": "file does not exist"})
                continue
            try:
                line = int(line_value)
            except (TypeError, ValueError):
                failures.append({"file": key, "line": line_value, "detail": "line is not an integer"})
                continue
            content = _state_content(source)
            if content is None:
                unverifiable.append("workspace_state.content:%s" % key)
                continue
            line_count = len(content.decode("utf-8", errors="replace").splitlines())
            if line < 1 or line > line_count:
                failures.append({"file": key, "line": line, "line_count": line_count,
                                 "detail": "line is outside the file"})
        if unverifiable:
            return _grade(self.id, GradeStatus.NOT_EVALUABLE, "source contents were not retained",
                          self.version, hard=hard, missing=unverifiable, evidence=failures)
        if failures:
            return _grade(self.id, GradeStatus.FAIL, "one or more source references are invalid",
                          self.version, hard=hard,
                          metrics={"references": len(references), "failures": len(failures)}, evidence=failures)
        return _grade(self.id, GradeStatus.PASS, "all source references are grounded", self.version,
                      hard=hard, metrics={"references": len(references), "failures": 0})


def _trace_tool(event: Any) -> Optional[str]:
    if canonical_trace_kind(event) != "tool_call":
        return None
    tool = _get(event, "tool") or _get(event, "name")
    payload = _get(event, "payload", {})
    if not tool and isinstance(payload, Mapping):
        tool = payload.get("name") or payload.get("tool")
    return str(tool) if tool else None


class TraceAssertGrader:
    id = "trace_assert"
    version = "1.0.0"

    async def evaluate(self, observation: RunObservation, oracle: Any, params: Mapping[str, Any]) -> GradeResult:
        del oracle
        hard = _hard(params)
        completeness = observation.metadata.get("completeness", {})
        if isinstance(completeness, Mapping) and completeness.get("trace") is False:
            return _grade(self.id, GradeStatus.NOT_EVALUABLE, "trace was not captured", self.version,
                          hard=hard, missing=("trace",))
        tools = [tool for tool in (_trace_tool(event) for event in observation.trace) if tool]
        counts = {tool: tools.count(tool) for tool in sorted(set(tools))}
        required = params.get("required_tools", ())
        forbidden = params.get("forbidden_tools", ())
        ordered = params.get("ordered_tools", params.get("ordered_subsequence", ()))
        if isinstance(required, str):
            required = (required,)
        if isinstance(forbidden, str):
            forbidden = (forbidden,)
        if isinstance(ordered, str):
            ordered = (ordered,)
        failures = []
        for tool in required:
            if counts.get(str(tool), 0) == 0:
                failures.append({"tool": str(tool), "detail": "required tool was not called"})
        for tool in forbidden:
            if counts.get(str(tool), 0) > 0:
                failures.append({"tool": str(tool), "detail": "forbidden tool was called"})
        minimum = params.get("min_calls")
        maximum = params.get("max_calls")
        if isinstance(minimum, Mapping):
            for tool, bound in minimum.items():
                if counts.get(str(tool), 0) < bound:
                    failures.append({"tool": str(tool), "detail": "fewer than min_calls"})
        elif minimum is not None and len(tools) < minimum:
            failures.append({"detail": "total tool calls are below min_calls"})
        if isinstance(maximum, Mapping):
            for tool, bound in maximum.items():
                if counts.get(str(tool), 0) > bound:
                    failures.append({"tool": str(tool), "detail": "more than max_calls"})
        elif maximum is not None and len(tools) > maximum:
            failures.append({"detail": "total tool calls exceed max_calls"})
        if ordered:
            iterator = iter(tools)
            if not all(any(actual == str(expected) for actual in iterator) for expected in ordered):
                failures.append({"expected": list(ordered), "actual": tools,
                                 "detail": "ordered tool subsequence was not observed"})
        if params.get("no_errors"):
            error_events = []
            for event in observation.trace:
                payload = _get(event, "payload", {})
                payload_error = isinstance(payload, Mapping) and (
                    payload.get("ok") is False or bool(payload.get("error"))
                )
                if _get(event, "error") or payload_error:
                    error_events.append(event)
            if error_events:
                failures.append({"detail": "trace contains errors", "count": len(error_events)})
        metrics = {"tool_calls": len(tools), "tool_counts": counts, "failures": len(failures)}
        if failures:
            return _grade(self.id, GradeStatus.FAIL, "trace assertions failed", self.version,
                          hard=hard, metrics=metrics, evidence=failures)
        return _grade(self.id, GradeStatus.PASS, "trace assertions passed", self.version,
                      hard=hard, metrics=metrics)


def _state_fingerprint(value: Any) -> Any:
    digest = _get(value, "content_hash")
    if digest is not None:
        return ("hash", digest)
    content = _artifact_content(value)
    if isinstance(content, bytes):
        return ("content", hashlib.sha256(content).hexdigest())
    try:
        return (
            "json",
            json.dumps(as_primitive(content), sort_keys=True, default=str),
        )
    except TypeError:
        return ("repr", repr(content))


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


class WorkspaceDiffGrader:
    id = "workspace_diff"
    version = "1.0.0"

    async def evaluate(self, observation: RunObservation, oracle: Any, params: Mapping[str, Any]) -> GradeResult:
        del oracle
        hard = _hard(params)
        before = observation.pre_state
        after = observation.post_state
        if before is None or after is None:
            return _grade(self.id, GradeStatus.NOT_EVALUABLE, "workspace snapshots were not captured",
                          self.version, hard=hard, missing=("pre_state", "post_state"))
        created = sorted(set(after) - set(before))
        deleted = sorted(set(before) - set(after))
        modified = sorted(
            path for path in set(before).intersection(after)
            if _state_fingerprint(before[path]) != _state_fingerprint(after[path])
        )
        changed = sorted(set(created + deleted + modified))
        failures = []
        allowed = params.get("allowed_paths")
        forbidden = params.get("forbidden_paths", ())
        required = params.get("required_paths", ())
        if isinstance(allowed, str):
            allowed = (allowed,)
        if isinstance(forbidden, str):
            forbidden = (forbidden,)
        if isinstance(required, str):
            required = (required,)
        if params.get("no_changes") and changed:
            failures.append({"detail": "workspace changed", "paths": changed})
        if allowed is not None:
            outside = [path for path in changed if not _matches(path, allowed)]
            if outside:
                failures.append({"detail": "paths changed outside allowed_paths", "paths": outside})
        forbidden_hits = [path for path in changed if _matches(path, forbidden)]
        if forbidden_hits:
            failures.append({"detail": "forbidden paths changed", "paths": forbidden_hits})
        missing_required = [pattern for pattern in required if not _matches_any(changed, pattern)]
        if missing_required:
            failures.append({"detail": "required paths did not change", "patterns": missing_required})
        for category, values in (("created", created), ("deleted", deleted), ("modified", modified)):
            expected = params.get("required_%s" % category, ())
            if isinstance(expected, str):
                expected = (expected,)
            missing = [pattern for pattern in expected if not _matches_any(values, pattern)]
            if missing:
                failures.append({"detail": "required %s paths missing" % category, "patterns": missing})
        metrics = {"created": created, "deleted": deleted, "modified": modified,
                   "changed": len(changed), "failures": len(failures)}
        if failures:
            return _grade(self.id, GradeStatus.FAIL, "workspace diff assertions failed", self.version,
                          hard=hard, metrics=metrics, evidence=failures)
        return _grade(self.id, GradeStatus.PASS, "workspace diff assertions passed", self.version,
                      hard=hard, metrics=metrics)


def _matches_any(paths: Sequence[str], pattern: str) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for path in paths)


def builtin_graders() -> Mapping[str, Any]:
    graders = (
        JsonSchemaGrader(),
        JsonPathGrader(),
        RecordMatchGrader(),
        ArtifactExistsGrader(),
        SourceReferenceGrader(),
        TraceAssertGrader(),
        WorkspaceDiffGrader(),
    )
    return {grader.id: grader for grader in graders}


def register_builtin_graders(registry: Any) -> None:
    for component_id, grader in builtin_graders().items():
        registry.register_grader(component_id, grader)


__all__ = [
    "ArtifactExistsGrader",
    "JsonPathGrader",
    "JsonSchemaGrader",
    "RecordMatchGrader",
    "SourceReferenceGrader",
    "TraceAssertGrader",
    "WorkspaceDiffGrader",
    "builtin_graders",
    "register_builtin_graders",
]
