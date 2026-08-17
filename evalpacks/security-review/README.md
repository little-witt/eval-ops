# security-review EvalPack

This pack evaluates a Skill that reviews one small Python fixture and returns a JSON object with a `findings` array.

- `dev`: 6 scenarios, including five vulnerable files and one safe parameterized-query control.
- `validation`: 2 scenarios using an unseen vulnerable form and an over-reporting control.
- `holdout`: 2 scenarios used once after candidate selection.
- `baseline`: intentionally recognizes only obvious SQL concatenation and over-reports safe SQL or process usage.
- `candidate`: covers the declared data-flow patterns while requiring evidence and avoiding the safe controls.

Scenario `metadata.fake_runtime.variants` makes the baseline/candidate comparison deterministic without exposing Oracle files to the prepared workspace. A real Reference Runtime ignores this metadata and executes the same fixtures against the selected Skill snapshot.
