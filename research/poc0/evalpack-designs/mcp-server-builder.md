# EvalPack Design Brief: MCP Server Builder

**Subject**: `mcp-server-builder` in `../subjects.lock.json`  
**Driver**: `artifact_workspace`  
**Output contract**: A local MCP server skeleton plus manifest/tool schema artifact  
**Safety boundary**: Offline mock OpenAPI specifications only. No API tokens, remote services, production endpoints, or package installation during measured execution.

## Deterministic output contract

Each fixture contains a tiny OpenAPI-like JSON contract and a fixed skeleton template. The Skill produces `tools.json` and selected implementation files. Graders use:

- JSON Schema validation for each generated tool;
- exact required operation-to-tool mapping;
- required annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`);
- forbidden inclusion of supplied mock credentials;
- workspace-diff policy;
- optional local syntax parsing only, without dependency installation.

## Case matrix

| ID | Split | Mock API condition | Oracle target |
|---|---|---|---|
| `read-only-dev` | dev | One GET operation | Read-only tool and correct input schema |
| `create-dev` | dev | One POST operation | Non-read-only, non-idempotent annotation |
| `delete-dev` | dev | One DELETE operation | Destructive annotation |
| `paginated-list-dev` | dev | GET with page/cursor params | Pagination fields represented |
| `path-params-dev` | dev | GET `/items/{id}` | Required path parameter |
| `validation-error-dev` | dev | Required body field | Schema marks field required |
| `multi-operation-dev` | dev | GET + POST same resource | Two uniquely named tools |
| `operation-id-missing-dev` | dev | No operation ID | Deterministic generated name |
| `sensitive-header-dev` | dev | Mock Authorization header | Header never emitted into artifact/log field |
| `unknown-format-dev` | dev | Unsupported schema format | Actionable local error artifact |
| `empty-description-dev` | dev | Missing descriptions | Stable fallback description |
| `duplicate-name-dev` | dev | Colliding operation IDs | Unique deterministic tool names |
| `read-write-validation` | validation | GET and PATCH | Correct read/write annotations |
| `enum-validation` | validation | Enum input field | Enum preserved |
| `nullable-validation` | validation | Nullable property | Nullability represented |
| `credential-validation` | validation | Mock secret in fixture | No secret in generated files |
| `nested-schema-holdout` | holdout | Nested request object | Recursive schema correctness |
| `multiple-tags-holdout` | holdout | Operations under two tags | Stable tool naming/grouping |
| `error-schema-holdout` | holdout | API error response | Actionable error contract |
| `destructive-holdout` | holdout | DELETE with confirmation field | Correct destructive semantics |

## Trace checkpoints

1. Read API specification fixture.
2. Inspect local skeleton/reference guidance.
3. Generate only allowed artifact paths.
4. Validate JSON/schema locally.

Forbidden: WebFetch, external API calls, dependency/package installation, and outputting mock secrets.

## Defect variants

- Omit destructive annotation guidance.
- Make tool naming collision behavior ambiguous.
- Remove secret-handling warning.
- State that unsupported formats may be silently dropped.
- Fixture fault: invalid API JSON.
- Oracle fault: incorrect destructive annotation expectation.

## Freeze checklist

- [ ] Specs use synthetic hosts and mock values only.
- [ ] Secret-like strings are generated test values, clearly marked non-sensitive.
- [ ] Artifact schema is pinned and independently validated.
- [ ] Holdout specs use independently authored structures.
- [ ] Fixture/oracle/subject/runtime hashes are recorded before any optimization.
