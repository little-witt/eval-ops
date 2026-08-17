---
name: review-python-security
description: Review small Python source files for command injection, path traversal, SQL injection, weak password hashing, and sensitive-data logging, returning grounded JSON findings.
---

# Review Python security

Read every provided Python file before answering. Trace caller-controlled values into security-sensitive operations and inspect nearby validation or containment guards.

Check these issue classes:

- Report `command-injection` when untrusted text reaches a shell string, `shell=True`, `os.system`, or `os.popen`. Do not report fixed argument lists when the variable is allowlisted and no shell parses it.
- Report `path-traversal` when an untrusted path is read or written without canonicalization and a root-containment check. Do not report a resolved path that is proven beneath the intended root.
- Report `sql-injection` for string formatting or concatenation into SQL. Do not report bound parameters.
- Report `weak-password-hash` when passwords use MD5 or SHA-1 instead of a password KDF.
- Report `sensitive-data-log` when credentials, tokens, or passwords are logged verbatim.

Ground every finding in the actual filename and the exact vulnerable line. Prefer no finding over a keyword-only guess.

Return only JSON with this shape:

```json
{"findings":[{"rule_id":"command-injection","severity":"high","file":"app.py","line":10,"evidence":"Concrete source-to-sink evidence.","recommendation":"A specific safe alternative."}]}
```

Use an empty `findings` array when no issue is found.
