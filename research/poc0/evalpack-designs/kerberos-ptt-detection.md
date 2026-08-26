# EvalPack Design Brief: Kerberos Pass-the-Ticket Detection

**Subject**: `kerberos-ptt-detection` in `../subjects.lock.json`  
**Driver**: `artifact_workspace`  
**Output contract**: `report.json`  
**Safety boundary**: Synthetic, sanitized Windows event records only. The benchmark does not execute attacks, access domains, contact SIEMs, or handle credentials.

## Deterministic output contract

`report.json` must contain:

```json
{
  "indicators": [
    {
      "id": "string",
      "type": "rc4_downgrade|cross_host_reuse|ticket_volume_anomaly|preauth_failure",
      "principal": "string",
      "evidence_event_ids": [4768, 4769, 4771],
      "severity": "low|medium|high"
    }
  ],
  "mitre_techniques": ["T1550.003"]
}
```

Primary deterministic graders: JSON schema, exact indicator-ID set, per-indicator evidence-event set, MITRE set, workspace-diff policy. A valid case never requires an LLM judge.

## Case matrix

| ID | Split | Fixture condition | Oracle target |
|---|---|---|---|
| `rc4-single-dev` | dev | One 4769 RC4 ticket request | `rc4_downgrade` finding |
| `rc4-multiple-users-dev` | dev | RC4 requests for two principals | Two distinct findings |
| `cross-host-reuse-dev` | dev | One ticket reused from two hosts | `cross_host_reuse` finding |
| `high-volume-dev` | dev | TGS volume above supplied baseline | `ticket_volume_anomaly` finding |
| `preauth-burst-dev` | dev | Repeated 4771 failures | `preauth_failure` finding |
| `mixed-indicators-dev` | dev | RC4 plus cross-host reuse | Two findings, no duplicates |
| `benign-aes-dev` | dev | Normal AES ticket events | Empty finding set |
| `missing-host-dev` | dev | Incomplete host field | No fabricated cross-host finding |
| `duplicate-events-dev` | dev | Repeated same event record | Deduplicated finding evidence |
| `out-of-order-dev` | dev | Events intentionally unordered | Same result after sorting by timestamp |
| `unrelated-event-dev` | dev | Non-Kerberos event mixed in | Ignore unrelated event |
| `malformed-record-dev` | dev | One invalid JSON line | Explicit ignored-record field or no crash |
| `rc4-threshold-validation` | validation | Boundary RC4 condition | Exact threshold behavior |
| `reuse-boundary-validation` | validation | Same user, same host | Empty reuse finding |
| `volume-boundary-validation` | validation | Exactly baseline threshold | Oracle-defined non-alert |
| `mixed-benign-validation` | validation | Benign AES plus one malformed record | Empty finding set, no crash |
| `cross-host-holdout` | holdout | New source-IP distribution | Reuse finding |
| `encryption-holdout` | holdout | RC4 encoded in alternate field representation | RC4 finding |
| `correlation-holdout` | holdout | Three concurrent principals | Correct per-principal grouping |
| `noise-holdout` | holdout | High unrelated-event ratio | Correct finding set |

## Trace checkpoints

1. Read the input event fixture.
2. Parse/normalize records.
3. Correlate event records by principal/ticket/host.
4. Write only `report.json`.

Forbidden traces: network calls, shell commands that invoke offensive tooling, writes outside `report.json`.

## Defect variants

- Omit deduplication instruction.
- Make the RC4 condition ambiguous.
- State that missing host values imply distinct hosts.
- Omit output schema requirement.
- Fixture fault: corrupt JSON fixture (must become not-evaluable or environment/fixture failure).
- Oracle fault: intentionally stale expected indicator (must not be treated as a Skill defect after calibration).

## Freeze checklist

- [ ] Synthetic fixture provenance recorded.
- [ ] Indicator IDs and threshold definitions independently reviewed.
- [ ] Holdout author/source differs from dev source.
- [ ] All fixture and oracle hashes recorded in EvalPack manifest.
- [ ] No fixture contains credentials, real hosts, users, domains, or incident logs.
