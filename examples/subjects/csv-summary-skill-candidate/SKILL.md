---
name: summarize-sales-csv
description: Summarize sales input.csv files into deterministic summary.json artifacts with currency parsing, blank-region handling, refunds, and decimal-safe rounding.
---

# Summarize sales CSV

Read every data row in `input.csv` and preserve the source file.

For each row:

- Trim surrounding whitespace.
- Treat a blank `region` as `Unknown`.
- Remove one leading `$` from `amount`, then parse it as a base-10 decimal.
- Include positive and negative amounts; negative values are refunds.

Sum all rows and group amounts by region. Round the final total and each regional total to two decimal places using decimal `ROUND_HALF_UP`, never binary floating point. Count every data row as one order. Sort region keys lexicographically for deterministic output.

Write only `summary.json` with exactly these fields:

```json
{"currency":"USD","total_revenue":0.00,"orders":0,"by_region":{}}
```

Do not create or modify any other file. After writing the artifact, return the same JSON object as the final response.
