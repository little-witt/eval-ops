---
name: review-python-security
description: Review small Python source files for security problems and return structured JSON findings when asked for a security review.
---

# Review Python security

Read every provided Python file before answering.

Look for SQL statements that include request or function parameters. Report `sql-injection` whenever user input and SQL appear in the same function. Do not speculate about other issue classes unless the vulnerability is immediately obvious.

Return only this shape:

```json
{"findings":[{"rule_id":"sql-injection","severity":"high","file":"app.py","line":1,"evidence":"...","recommendation":"..."}]}
```

Use an empty `findings` array when no issue is found.
