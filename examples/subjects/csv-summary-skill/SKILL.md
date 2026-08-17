---
name: summarize-sales-csv
description: Summarize a sales input.csv file into summary.json with totals and region groups when asked to process sales CSV data.
---

# Summarize sales CSV

Read `input.csv`. Convert `amount` with the language's normal floating-point conversion. Skip rows whose region is blank or whose amount cannot be converted directly.

Write `summary.json` with this shape:

```json
{"currency":"USD","total_revenue":0,"orders":0,"by_region":{}}
```

Count only included rows. Group included amounts by region. Do not modify `input.csv`.
