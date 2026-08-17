# csv-summary-smoke EvalPack

This pack proves that a file-artifact task can reuse the same Kernel and Skill optimizer as the security-review pack.

- Two dev scenarios cover currency symbols, blank regions, and refunds.
- One validation scenario covers decimal rounding without adding a holdout claim.
- The Skill must preserve `input.csv` and create a schema-valid `summary.json`.
- Oracle values stay outside the prepared workspace and feed generic JSON-path graders.

Scenario `metadata.fake_runtime.variants` provides deterministic baseline and candidate artifacts. A real Reference Runtime reads `input.csv`, follows the selected Skill, and writes `summary.json` in the isolated workspace.
