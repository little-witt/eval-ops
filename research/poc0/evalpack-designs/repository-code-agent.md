# EvalPack Design Brief: Repository Code Agent

**Subject**: `repository-code-agent` in `../subjects.lock.json`  
**Driver**: `repository_workspace`  
**Output contract**: Workspace diff and test command result  
**Safety boundary**: Tiny synthetic repositories only. Network egress, package installation, project scaffolding, git push and writes outside the temporary workspace are disabled.

## Deterministic output contract

Each fixture includes a baseline repository, a requested change and an expected patch. Graders use:

- required/forbidden file paths;
- normalized `git diff` or file-content assertions;
- language parser/AST assertions when available;
- a fixed local test command;
- workspace-diff policy;
- execution-path checkpoints.

Do not use subjective code-review rubrics in the main PoC outcome metric.

## Case matrix

| ID | Split | Requested change | Oracle target |
|---|---|---|---|
| `rename-python-dev` | dev | Rename `foo` to `bar` in one module | Exact call-site and definition update |
| `rename-multi-file-dev` | dev | Rename across two imports | All imports updated, tests pass |
| `fix-off-by-one-dev` | dev | Correct bounded loop | Fixed test output |
| `json-validation-dev` | dev | Add missing input validation | Expected exception and message |
| `config-default-dev` | dev | Add a default config key | JSON/YAML structure assertion |
| `remove-dead-code-dev` | dev | Remove unused helper | File-content/AST absence |
| `preserve-public-api-dev` | dev | Refactor internal helper | Public API snapshot unchanged |
| `failing-test-first-dev` | dev | Repair one named test | Fixed test passes; unrelated test remains |
| `comment-only-dev` | dev | Clarify misleading comment | Exact content assertion |
| `read-before-edit-dev` | dev | Change one function | Required trace sequence |
| `no-scaffold-dev` | dev | Fix existing repository | Existing root retained; no new project tree |
| `forbidden-network-dev` | dev | Local fix | No network tool/event |
| `import-cycle-validation` | validation | Eliminate simple cycle | Import graph assertion |
| `type-boundary-validation` | validation | Handle `None` input | Tests and AST assertion |
| `minimal-diff-validation` | validation | Correct one constant | Only permitted file modified |
| `test-command-validation` | validation | Repair test failure | Fixed local test command |
| `rename-holdout` | holdout | New language/structure rename | AST assertion and tests |
| `error-message-holdout` | holdout | Improve validation error | Exact raised message |
| `state-mutation-holdout` | holdout | Prevent shared mutable default | Deterministic behavior test |
| `policy-holdout` | holdout | Local code fix | No outside-workspace changes |

## Trace checkpoints

1. Inspect the repository tree.
2. Read the target file(s).
3. Edit only permitted path(s).
4. Run the stated local validation command.

Forbidden: `git push`, package installation, network egress, new project scaffolding where a repository already exists.

## Defect variants

- Remove the “read before edit” rule.
- Make existing-versus-new-project boundary ambiguous.
- Remove validation-command requirement.
- Permit an over-broad edit path.
- Environment fault: validation executable absent.
- Grader fault: stale patch expectation.

## Freeze checklist

- [ ] Every fixture is self-contained and uses a pinned local interpreter/runtime.
- [ ] All tests finish within the configured timeout without network access.
- [ ] Expected diff is normalized for line endings.
- [ ] Holdout requests are authored separately from dev cases.
- [ ] No fixture includes proprietary code or secrets.
