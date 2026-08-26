# ACEval PoC-0 Research Assets

This directory contains pre-execution assets for validating the research question:

> Does multi-path trajectory divergence provide useful evidence for identifying and safely repairing Agent Skill defects?

## Contents

- `subjects.lock.json`: immutable public source provenance for the selected Skills.
- `sources/`: shallow local clones at the commits named in the lock file. These are research inputs, not generated benchmark artifacts.
- `evalpack-designs/`: design briefs for 20-case EvalPacks. They are intentionally not frozen EvalPacks yet.
- `MULTIPATH_COLLECTION_PROTOCOL.md`: required procedure before launching CATX sessions.

## Current status

The three sources are locked, but no real CATX execution, EvalPack generation, grading, optimization, or publication has occurred. All results remain unreported until the EvalPacks are calibrated, frozen and executed under the collection protocol.

## Reproducibility rule

An execution may only be included in PoC analysis if its subject commit, Skill entrypoint hash, license hash, EvalPack hash, runtime/model profile hash, seed, run context and trace provenance are recorded. A divergence report is observational evidence only; it does not independently authorize a Skill patch.
