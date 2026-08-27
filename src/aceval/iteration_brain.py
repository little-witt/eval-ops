"""FORGE's local, staged analysis brain.

Evidence and the final control-plane decision are deterministic.  The model
is used only for three small, schema checked jobs: semantic grading,
attribution, and proposal writing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from .agent_runtime import ModelClient
from .evidence_analysis import _heuristic_case_result, _optimization_case_ready, compact_case_evidence
from .kernel_contracts import ANALYSIS_DECISION_API_VERSION
from .kernel_v2 import build_case_aggregates, compile_diagnosis_graph

EVIDENCE_BUNDLE_API_VERSION = "aceval.evidence-bundle/v1"
SEMANTIC_VERDICT_API_VERSION = "aceval.semantic-verdict/v1"
ATTRIBUTION_REPORT_API_VERSION = "aceval.attribution-report/v1"
OPTIMIZATION_PLAN_API_VERSION = "aceval.optimization-plan/v1"
AGENT_CALL_RECEIPT_API_VERSION = "aceval.agent-call-receipt/v1"
PROMPT_TEMPLATE_VERSION = "local-forge-staged-analysis/v2"


def _hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class EvidenceQueryError(ValueError):
    pass


@dataclass
class LocalEvidenceQueryPort:
    """Bounded, read-only access; it has no shell, git, or write operation."""
    task_root: Path
    max_bytes: int = 256_000
    max_queries: int = 32
    _queries: int = 0

    def _artifact(self, reference: str) -> Path:
        root = self.task_root.expanduser().resolve()
        path = Path(reference).expanduser().resolve()
        if root not in path.parents or not path.is_file() or path.is_symlink():
            raise EvidenceQueryError("artifact is outside the task evidence directory")
        return path

    def read_log_window(self, artifact: str, start: int = 0, limit: int = 16_000) -> Mapping[str, Any]:
        if self._queries >= self.max_queries:
            raise EvidenceQueryError("evidence query limit exceeded")
        # Count attempted queries as well as successful reads.  Otherwise a
        # caller could probe an unbounded number of paths after the quota was
        # reached.
        self._queries += 1
        if start < 0 or limit <= 0 or limit > self.max_bytes:
            raise EvidenceQueryError("invalid bounded log window")
        path = self._artifact(artifact)
        with path.open("rb") as stream:
            stream.seek(start)
            data = stream.read(limit)
        return {"artifact": str(path), "start": start, "bytes": len(data), "text": data.decode("utf-8", errors="replace")}

    def get_case_summary(self, case_id: str, evidence: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        for item in evidence:
            if str(item.get("case_id")) == case_id:
                return dict(item)
        raise EvidenceQueryError("case is not present in the frozen evidence bundle")


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("%s must be a JSON object" % label)
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("%s is not strict JSON" % label) from exc


def _json(content: str, label: str) -> Mapping[str, Any]:
    try:
        return _object(json.loads(content), label)
    except json.JSONDecodeError as exc:
        raise ValueError("%s returned invalid JSON" % label) from exc


def _case_rows(value: Any, ids: Sequence[str], label: str, *, verification: bool = True) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(x, Mapping) for x in value):
        raise ValueError("%s.case_results must be an array of objects" % label)
    rows = [dict(x) for x in value]
    if {str(x.get("case_id")) for x in rows} != set(ids):
        raise ValueError("%s.case_results do not cover the selected cases" % label)
    for row in rows:
        if row.get("status") not in ("pass", "fail", "not_evaluable"):
            raise ValueError("%s has an invalid case status" % label)
        if verification and row.get("verification_status", "not_run") not in ("pass", "fail", "not_evaluable", "not_run"):
            raise ValueError("%s has an invalid verification status" % label)
        if not isinstance(row.get("reason", ""), str) or not isinstance(row.get("evidence_refs", []), list):
            raise ValueError("%s case row has invalid reason/evidence_refs" % label)
    return rows


def _parse_semantic(value: Mapping[str, Any], ids: Sequence[str], baseline_ids: Sequence[str]) -> Mapping[str, Any]:
    # Accept the legacy envelope as an input compatibility bridge, while only
    # copying the semantic fields into the frozen artifact.
    if set(value).difference({"api_version", "case_results", "without_skill_baseline_case_results", "failure_clusters", "conflicts", "proposed_changes", "target_scope"}):
        raise ValueError("semantic verdict contains unsupported fields")
    rows = _case_rows(value.get("case_results"), ids, "semantic verdict")
    baseline = []
    if baseline_ids:
        baseline = _case_rows(value.get("without_skill_baseline_case_results"), baseline_ids, "semantic baseline", verification=False)
    elif value.get("without_skill_baseline_case_results"):
        raise ValueError("semantic baseline was returned without requested cases")
    return {"case_results": rows, "without_skill_baseline_case_results": baseline}


def _parse_attribution(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if set(value).difference({"api_version", "failure_clusters", "conflicts", "case_results", "proposed_changes", "target_scope", "without_skill_baseline_case_results"}):
        raise ValueError("attribution report contains unsupported fields")
    for key in ("failure_clusters", "conflicts"):
        if not isinstance(value.get(key), list) or any(not isinstance(x, Mapping) for x in value[key]):
            raise ValueError("attribution %s must be an array of objects" % key)
    return {"failure_clusters": [dict(x) for x in value["failure_clusters"]], "conflicts": [dict(x) for x in value["conflicts"]]}


def _parse_proposal(value: Mapping[str, Any], inventory: Sequence[str]) -> Mapping[str, Any]:
    if set(value).difference({"api_version", "proposed_changes", "target_scope", "case_results", "failure_clusters", "conflicts", "without_skill_baseline_case_results"}):
        raise ValueError("optimization plan contains unsupported fields")
    changes, scope = value.get("proposed_changes"), value.get("target_scope")
    if not isinstance(changes, list) or any(not isinstance(x, Mapping) for x in changes):
        raise ValueError("optimization proposed_changes must be an array of objects")
    if not isinstance(scope, list) or any(not isinstance(x, str) or not x for x in scope):
        raise ValueError("optimization target_scope must be an array of paths")
    allowed = set(str(x) for x in inventory)
    if any(x not in allowed for x in scope):
        raise ValueError("optimization target_scope contains a non-editable path")
    return {"proposed_changes": [dict(x) for x in changes], "target_scope": list(scope)}


class IterationBrain:
    def __init__(self, model: Optional[ModelClient], artifact_root: Path, *, provider: str = "local-forge", profile: str = "default", model_id: Optional[str] = None, max_retries: int = 1, max_evidence_chars_per_case: int = 4000, max_prompt_chars: int = 48_000, stage_callback: Optional[Callable[[str], None]] = None) -> None:
        self.model, self.artifact_root = model, Path(artifact_root)
        self.provider = provider
        model_profile = getattr(model, "profile", None)
        if callable(model_profile):
            model_profile = model_profile()
        self.profile = dict(model_profile) if isinstance(model_profile, Mapping) else str(model_profile or profile)
        self.model_id = str(getattr(model, "model_id", None) or model_id or "unconfigured")
        self.max_retries = max(0, max_retries)
        self.max_evidence_chars_per_case, self.max_prompt_chars = max_evidence_chars_per_case, max_prompt_chars
        self.receipts: list[Mapping[str, Any]] = []
        self.query_receipts: list[Mapping[str, Any]] = []
        self.current_receipts: list[Mapping[str, Any]] = []
        self.current_query_receipts: list[Mapping[str, Any]] = []
        self.stage_callback = stage_callback
        self._load_receipt_history()
        self._prompt_chars = 0
        self._attempt_counter = self._next_attempt()

    def _load_receipt_history(self) -> None:
        """Load immutable root receipts so a restarted Brain never reuses names."""
        def load_many(pattern: str) -> list[Mapping[str, Any]]:
            values = []
            for path in sorted(self.artifact_root.glob(pattern)):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, Mapping): values.append(value)
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
            return values
        if self.artifact_root.is_dir():
            self.receipts.extend(load_many("agent-call-receipt-*.json"))
            self.query_receipts.extend(load_many("evidence-query-receipt-*.json"))

    def _next_attempt(self) -> int:
        values = []
        if self.artifact_root.is_dir():
            for p in self.artifact_root.glob("attempt-*"):
                try: values.append(int(p.name.split("-", 1)[1]))
                except (IndexError, ValueError): pass
        return max(values, default=0)

    def _record_receipt(self, receipt: Mapping[str, Any], stage_dir: Optional[Path] = None) -> None:
        value = dict(receipt); self.receipts.append(value); self.current_receipts.append(value)
        index = max([int(p.stem.rsplit("-", 1)[1]) for p in self.artifact_root.glob("agent-call-receipt-*.json") if p.stem.rsplit("-", 1)[-1].isdigit()] or [0]) + 1
        _write(self.artifact_root / ("agent-call-receipt-%03d.json" % index), value)
        if stage_dir: _write(stage_dir / "agent-call-receipt.json", value)

    def _record_query(self, stage: str, case_id: str, artifact: Optional[str], result: Optional[Mapping[str, Any]], error: Optional[str]) -> None:
        value = {"api_version": "aceval.evidence-query-receipt/v1", "stage": stage, "case_id": case_id, "artifact": artifact, "status": "failed" if error else "succeeded", "query": {"start": 0, "limit": 4096}}
        if result: value.update({"output_hash": _hash(result), "bytes": result.get("bytes", 0)})
        if error: value["error"] = error[:500]
        self.query_receipts.append(value); self.current_query_receipts.append(value)
        index = max([int(p.stem.rsplit("-", 1)[1]) for p in self.artifact_root.glob("evidence-query-receipt-*.json") if p.stem.rsplit("-", 1)[-1].isdigit()] or [0]) + 1
        _write(self.artifact_root / ("evidence-query-receipt-%03d.json" % index), value)

    def _load_stage(self, stage: str, input_hash: str, parser: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
        for manifest_path in sorted(self.artifact_root.glob("attempt-*/%s/manifest.json" % stage)):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("status") != "validated" or manifest.get("input_hash") != input_hash: continue
                artifact_path = manifest_path.parent / "artifact.json"
                value = _object(json.loads(artifact_path.read_text(encoding="utf-8")), stage)
                parsed = parser(value)
                if manifest.get("output_hash") == _hash(parsed): return parsed
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return None

    def _save_stage(self, stage: str, input_hash: str, value: Mapping[str, Any], attempt: int) -> Mapping[str, Any]:
        parsed = _object(value, stage); stage_dir = self.artifact_root / ("attempt-%03d" % attempt) / stage
        _write(stage_dir / "artifact.json", parsed)
        _write(stage_dir / "manifest.json", {"api_version": "aceval.stage-manifest/v1", "stage": stage, "attempt": attempt, "input_hash": input_hash, "output_hash": _hash(parsed), "status": "validated", "artifact_validated": True, "artifact": str(stage_dir / "artifact.json")})
        aliases = {"evidence_compilation": "evidence-bundle.json", "semantic_grading": "semantic-verdict.json", "attribution": "attribution-report.json", "proposal": "optimization-plan.json"}
        if stage in aliases: _write(self.artifact_root / aliases[stage], parsed)
        return parsed

    def _failed_stage(self, stage: str, input_hash: str, attempt: int, exc: Exception) -> None:
        stage_dir = self.artifact_root / ("attempt-%03d" % attempt) / stage
        _write(stage_dir / "manifest.json", {"api_version": "aceval.stage-manifest/v1", "stage": stage, "attempt": attempt, "input_hash": input_hash, "status": "failed", "artifact_validated": False, "error": str(exc)[:500]})

    def _model_stage(self, stage: str, input_value: Mapping[str, Any], parser: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> Mapping[str, Any]:
        input_hash = _hash(input_value); reused = self._load_stage(stage, input_hash, parser)
        if reused is not None: return reused
        if self.model is None: raise ValueError("local-forge model is not configured")
        if self.stage_callback is not None:
            self.stage_callback(stage)
        self._attempt_counter += 1; attempt = self._attempt_counter; stage_dir = self.artifact_root / ("attempt-%03d" % attempt) / stage
        request_text = json.dumps(input_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        started = time.monotonic()
        receipt = {"api_version": AGENT_CALL_RECEIPT_API_VERSION, "provider": self.provider, "profile": self.profile, "model": self.model_id, "prompt_template_version": PROMPT_TEMPLATE_VERSION, "stage": stage, "attempt": attempt, "input_hash": _hash(request_text), "status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
        try:
            if len(request_text) > self.max_prompt_chars: raise ValueError("staged request exceeds Token budget")
            self._prompt_chars += len(request_text)
            reply = self.model.complete([{"role": "system", "content": "Return strict JSON only for the %s stage." % stage}, {"role": "user", "content": request_text}], ())
            receipt.update({"usage": dict(reply.usage or {}), "duration_ms": int((time.monotonic() - started) * 1000), "output_hash": _hash(reply.content)})
            parsed = parser(_json(reply.content, stage))
            receipt.update({"status": "succeeded", "artifact_validated": True}); self._record_receipt(receipt, stage_dir)
            return self._save_stage(stage, input_hash, parsed, attempt)
        except Exception as exc:
            receipt.update({"status": "failed", "artifact_validated": False, "error": str(exc)[:500], "duration_ms": int((time.monotonic() - started) * 1000)})
            self._record_receipt(receipt, stage_dir); self._failed_stage(stage, input_hash, attempt, exc); raise

    def _evidence_root(self, artifacts: Sequence[str]) -> Path:
        try: return Path(os.path.commonpath([str(self.artifact_root.resolve())] + [str(Path(x).expanduser().resolve()) for x in artifacts if x]))
        except ValueError: return self.artifact_root.parent.resolve()

    def _compile_evidence(self, kwargs: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]):
        primary, verification_batch, baseline_batch = kwargs.get("primary_batch", {}), kwargs.get("verification_batch"), kwargs.get("comparison_baseline_batch")
        paths = kwargs.get("path_specs") or {}
        def rows(batch): return {str(x.get("case_id")): x for x in (batch.get("cases", ()) if isinstance(batch, Mapping) else ()) if isinstance(x, Mapping)}
        primary_rows, verification_rows, baseline_rows = rows(primary), rows(verification_batch), rows(baseline_batch)
        evidence, verification, baseline, artifacts = [], [], [], []
        for case in cases:
            cid = str(case.get("id")); item = compact_case_evidence(case, primary_rows.get(cid, {}), max_chars=self.max_evidence_chars_per_case, path_spec=paths.get(cid)); evidence.append(item)
            if item.get("artifact"): artifacts.append(str(item["artifact"]))
            if verification_batch is not None: verification.append(compact_case_evidence(case, verification_rows.get(cid, {}), max_chars=self.max_evidence_chars_per_case, path_spec=paths.get(cid)))
            if baseline_batch is not None: baseline.append(compact_case_evidence(case, baseline_rows.get(cid, {}), max_chars=self.max_evidence_chars_per_case, path_spec=None))
        self.evidence_query_port = LocalEvidenceQueryPort(self._evidence_root(artifacts))
        def heur(items): return {str(c.get("id")): v for c, i in zip(cases, items) if (v := _heuristic_case_result(c, i)) is not None}
        bundle = {"api_version": EVIDENCE_BUNDLE_API_VERSION, "task_id": kwargs.get("task_id"), "iteration": kwargs.get("iteration"), "environment_contract_hash": primary.get("environment_contract_hash"), "cases": evidence, "verification_evidence": verification, "without_skill_baseline_evidence": baseline, "context_compiler": "bounded-indexed-window/v2"}
        return bundle, heur(evidence), heur(verification), heur(baseline)

    def analyze(self, **kwargs: Any) -> Mapping[str, Any]:
        cases = tuple(x for x in kwargs.get("cases", ()) if isinstance(x, Mapping)); bundle, heuristic, verification_heuristic, baseline_heuristic = self._compile_evidence(kwargs, cases)
        verification_batch, baseline_batch = kwargs.get("verification_batch"), kwargs.get("comparison_baseline_batch")
        unresolved = [str(c.get("id")) for c in cases if str(c.get("id")) not in heuristic]
        baseline_unresolved = [str(c.get("id")) for c in cases if baseline_batch is not None and str(c.get("id")) not in baseline_heuristic]
        verification_unresolved = [str(c.get("id")) for c in cases if verification_batch is not None and str(c.get("id")) not in verification_heuristic]
        selected = set(unresolved) | set(verification_unresolved) | set(baseline_unresolved); windows = []
        for item in bundle["cases"]:
            cid = str(item.get("case_id"))
            if cid not in selected or not item.get("artifact"): continue
            try:
                window = self.evidence_query_port.read_log_window(str(item["artifact"]), 0, min(4096, self.evidence_query_port.max_bytes)); self._record_query("semantic_grading", cid, str(item["artifact"]), window, None); windows.append({"case_id": cid, "window": window})
            except EvidenceQueryError as exc: self._record_query("semantic_grading", cid, str(item.get("artifact")), None, str(exc))
        # Receipt history is append-only provenance and must not alter the
        # content identity used for stage reuse after a process restart.
        bundle["on_demand_windows"], bundle["query_receipts"] = windows, list(self.current_query_receipts); bundle["input_hash"] = _hash(bundle)
        evidence_input = {"task_id": bundle["task_id"], "iteration": bundle["iteration"], "bundle": bundle}; evidence_hash = _hash(evidence_input)
        evidence = self._load_stage("evidence_compilation", evidence_hash, lambda x: x)
        if evidence is None:
            self._attempt_counter += 1; evidence = self._save_stage("evidence_compilation", evidence_hash, bundle, self._attempt_counter)
        else: bundle = evidence
        _write(self.artifact_root / "analysis-state.json", {"stage": "evidence_ready", "evidence_hash": bundle["input_hash"]})
        semantic_ids = sorted(selected)
        semantic_input = {"api_version": SEMANTIC_VERDICT_API_VERSION, "cases": [dict(c) for c in cases if str(c.get("id")) in semantic_ids], "evidence": [x for x in bundle["cases"] if str(x.get("case_id")) in semantic_ids], "verification_evidence": [x for x in bundle["verification_evidence"] if str(x.get("case_id")) in semantic_ids], "without_skill_baseline_evidence": [x for x in bundle["without_skill_baseline_evidence"] if str(x.get("case_id")) in semantic_ids], "windows": windows, "requested_case_ids": semantic_ids, "requested_baseline_case_ids": baseline_unresolved}
        if semantic_ids and self.model is not None:
            semantic = self._model_stage("semantic_grading", semantic_input, lambda x: _parse_semantic(x, semantic_ids, baseline_unresolved))
            heuristic.update({str(x["case_id"]): dict(x) for x in semantic["case_results"]}); baseline_heuristic.update({str(x["case_id"]): dict(x) for x in semantic["without_skill_baseline_case_results"]})
        elif semantic_ids:
            # A missing local model is a deterministic inability to grade, not
            # a reason to manufacture a Skill failure or to block resume.
            semantic_rows = [{"case_id": cid, "status": "not_evaluable", "verification_status": "not_run", "reason": "local-forge model is not configured", "evidence_refs": []} for cid in semantic_ids]
            baseline_rows = [{"case_id": cid, "status": "not_evaluable", "reason": "local-forge model is not configured", "evidence_refs": []} for cid in baseline_unresolved]
            semantic = {"case_results": semantic_rows, "without_skill_baseline_case_results": baseline_rows}
            heuristic.update({str(x["case_id"]): dict(x) for x in semantic_rows}); baseline_heuristic.update({str(x["case_id"]): dict(x) for x in baseline_rows})
            self._attempt_counter += 1; self._save_stage("semantic_grading", _hash(semantic_input), semantic, self._attempt_counter)
        else:
            semantic = {"case_results": [], "without_skill_baseline_case_results": []}; self._save_stage("semantic_grading", _hash(semantic_input), semantic, self._attempt_counter)
        results_by_id = {str(c.get("id")): heuristic[str(c.get("id"))] for c in cases}
        # Re-apply frozen deterministic facts after semantic grading.
        for cid, value in self._hard_results(cases, bundle["cases"]).items(): results_by_id[cid] = value
        inventory = list(kwargs.get("editable_resource_inventory") or ("SKILL.md",)); failures = [cid for cid, x in results_by_id.items() if x.get("status") == "fail"]
        attribution_input = {"api_version": ATTRIBUTION_REPORT_API_VERSION, "evidence_hash": bundle["input_hash"], "semantic_verdict": semantic, "case_results": list(results_by_id.values()), "cases": [dict(c) for c in cases], "verification_evidence": list(bundle["verification_evidence"]), "editable_resource_inventory": inventory}
        if self.model is not None and (failures or semantic_ids): attribution = self._model_stage("attribution", attribution_input, _parse_attribution)
        else:
            attribution = {"failure_clusters": ([{"id": "observed-failures", "case_ids": failures, "root_cause": "requires cross-case Skill repair analysis", "skill_change_authorized": bool(failures)}] if failures else []), "conflicts": []}; self._save_stage("attribution", _hash(attribution_input), attribution, self._attempt_counter)
        proposal_input = {"api_version": OPTIMIZATION_PLAN_API_VERSION, "evidence_hash": bundle["input_hash"], "semantic_verdict": semantic, "attribution_report": attribution, "cases": [dict(c) for c in cases], "editable_resource_inventory": inventory}
        if self.model is not None and (failures or semantic_ids): proposal = self._model_stage("proposal", proposal_input, lambda x: _parse_proposal(x, inventory))
        else:
            proposal = {"proposed_changes": ([{"target": "SKILL.md", "change": "repair the shared cause supported by failed case evidence", "why": "one or more frozen expectations failed", "case_ids": failures}] if failures else []), "target_scope": ["SKILL.md"] if failures else []}; self._save_stage("proposal", _hash(proposal_input), proposal, self._attempt_counter)
        _write(self.artifact_root / "analysis-state.json", {"stage": "proposal", "evidence_hash": bundle["input_hash"], "completed": True})
        return self._assemble(kwargs, cases, bundle, results_by_id, verification_heuristic, baseline_heuristic, attribution, proposal)

    @staticmethod
    def _hard_results(cases, evidence):
        out = {}
        for case, item in zip(cases, evidence):
            value = _heuristic_case_result(case, item)
            if value is not None: out[str(case.get("id"))] = value
        return out

    def _assemble(self, kwargs, cases, bundle, results_by_id, verification_heuristic, baseline_heuristic, attribution, proposal):
        task_id, iteration = str(kwargs.get("task_id")), int(kwargs.get("iteration", 0)); evidence = list(bundle["cases"]); results = [dict(results_by_id[str(c.get("id"))]) for c in cases]; eligible = {str(c.get("id")) for c in cases if _optimization_case_ready(c)}; inventory = set(str(x) for x in (kwargs.get("editable_resource_inventory") or ("SKILL.md",)))
        clusters = []
        for cluster in attribution.get("failure_clusters", []):
            value = dict(cluster); original = [str(x) for x in value.get("case_ids", [])]; ids = [x for x in original if x in eligible]; value["case_ids"], value["excluded_unready_case_ids"] = ids, [x for x in original if x not in eligible]; value["skill_change_authorized"] = bool(value.get("skill_change_authorized") is True and ids); clusters.append(value)
        changes = []
        for change in proposal.get("proposed_changes", []):
            value = dict(change); original = [str(x) for x in value.get("case_ids", [])]; ids = [x for x in original if x in eligible]
            if ids: value["case_ids"], value["excluded_unready_case_ids"] = ids, [x for x in original if x not in eligible]; changes.append(value)
        scope = [x for x in proposal.get("target_scope", []) if x in inventory]; primary_passes = [x["case_id"] for x in results if x["status"] == "pass"]; stable, flaky = [], []
        if kwargs.get("verification_batch") is not None:
            for cid in primary_passes:
                verify = verification_heuristic.get(cid)
                if verify and verify.get("status") == "pass": stable.append(cid)
                elif verify and verify.get("status") == "fail": flaky.append(cid)
                elif results_by_id.get(cid, {}).get("verification_status") == "pass": stable.append(cid)
                else: flaky.append(cid)
        failed = [x["case_id"] for x in results if x["status"] == "fail"] + flaky
        if flaky:
            clusters.append({"id": "unstable-pass-verification", "case_ids": list(flaky), "root_cause": "a primary pass did not reproduce in verification", "skill_change_authorized": True}); changes.append({"target": "SKILL.md", "change": "make the affected workflow deterministic", "why": "primary pass was not stable", "case_ids": list(flaky)}); scope = list(scope) + ([] if "SKILL.md" in scope else ["SKILL.md"])
        not_eval = [x["case_id"] for x in results if x["status"] == "not_evaluable"]
        if primary_passes and kwargs.get("verification_batch") is None: next_action = "verify_passes"
        elif not_eval: next_action = "needs_evidence"
        elif failed: next_action = "await_user_confirmation"
        else: next_action = "converged"
        blocker = None
        if next_action == "await_user_confirmation":
            if not any(x.get("skill_change_authorized") is True for x in clusters): next_action, blocker = "needs_evidence", "no failure cluster authorizes a Skill change"
            elif any(x not in inventory for x in scope): next_action, blocker = "needs_evidence", "target scope is not an editable existing Skill resource: %s" % ", ".join(x for x in scope if x not in inventory)
        verification_results = {str(c.get("id")): verification_heuristic[str(c.get("id"))] for c in cases if str(c.get("id")) in verification_heuristic}; aggregates = build_case_aggregates(cases, results, evidence, list(bundle.get("verification_evidence", [])), verification_results=verification_results, required_k=2, run_context={"task_id": task_id, "iteration": iteration, "primary_purpose": kwargs.get("primary_batch", {}).get("purpose", "evaluation"), "primary_created_at": kwargs.get("primary_batch", {}).get("created_at"), "verification_created_at": kwargs.get("verification_batch", {}).get("created_at") if kwargs.get("verification_batch") else None}); diagnosis = compile_diagnosis_graph(results, clusters, changes, list(attribution.get("conflicts", []))); model_calls = sum(1 for x in self.receipts if x.get("status") == "succeeded")
        usage = {}
        for receipt in self.receipts:
            for key, value in (receipt.get("usage") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool): usage[key] = usage.get(key, 0) + value
        receipt_paths = sorted(str(path) for path in self.artifact_root.glob("attempt-*/**/agent-call-receipt.json"))
        root_receipt_paths = sorted(str(path) for path in self.artifact_root.glob("agent-call-receipt-*.json"))
        query_paths = sorted(str(path) for path in self.artifact_root.glob("evidence-query-receipt-*.json"))
        return {"api_version": ANALYSIS_DECISION_API_VERSION, "task_id": task_id, "iteration": iteration, "brain_stage": "proposal", "case_results": results, "stable_pass_case_ids": stable, "verification_required_case_ids": primary_passes if kwargs.get("verification_batch") is None else [], "flaky_case_ids": flaky, "failed_case_ids": failed, "not_evaluable_case_ids": not_eval, "without_skill_baseline_case_results": [baseline_heuristic.get(str(c.get("id")), {"case_id": str(c.get("id")), "status": "not_evaluable", "reason": "baseline requires semantic analysis", "evidence_refs": []}) for c in cases] if kwargs.get("comparison_baseline_batch") is not None else [], "incremental_value_case_ids": [x["case_id"] for x in results if x["status"] == "pass" and baseline_heuristic.get(x["case_id"], {}).get("status") == "fail"], "failure_clusters": clusters, "conflicts": list(attribution.get("conflicts", [])), "proposed_changes": changes, "target_scope": scope if changes else [], "optimization_eligible_case_ids": sorted(eligible), "next_action": next_action, "intervention_blocker": blocker, "requires_user_confirmation": next_action == "await_user_confirmation", "analysis_usage": usage, "attempt_verdicts": [a.to_dict() for ag in aggregates for a in ag.attempts], "case_aggregates": [a.to_dict() for a in aggregates], "diagnosis_graph": diagnosis, "evidence_health": {"valid_attempts": sum(a.evidence_validity.status == "valid" for ag in aggregates for a in ag.attempts), "invalid_attempts": sum(a.evidence_validity.status == "invalid" for ag in aggregates for a in ag.attempts), "not_evaluable_case_ids": [a.case_id for a in aggregates if a.status == "not_evaluable"]}, "token_economy": {"model_calls": model_calls, "full_logs_sent": False, "evidence_artifacts": [x.get("artifact") for x in evidence], "serialized_prompt_chars": self._prompt_chars, "estimated_prompt_tokens": (self._prompt_chars + 3) // 4}, "analysis_artifacts": {"evidence_bundle": str(self.artifact_root / "evidence-bundle.json"), "semantic_verdict": str(self.artifact_root / "semantic-verdict.json"), "attribution_report": str(self.artifact_root / "attribution-report.json"), "optimization_plan": str(self.artifact_root / "optimization-plan.json"), "agent_call_receipts": receipt_paths, "agent_call_receipt_history": root_receipt_paths, "evidence_query_receipts": query_paths}, "agent_call_receipts": list(self.receipts), "evidence_query_receipts": list(self.query_receipts)}


__all__ = ["IterationBrain", "LocalEvidenceQueryPort", "EvidenceQueryError"]
