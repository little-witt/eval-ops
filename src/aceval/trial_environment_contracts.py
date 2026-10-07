"""Compatibility import for the frozen trial-environment contract API."""

from .trial_environment import (
    TRIAL_ENVIRONMENT_CONTRACT_API_VERSION,
    TrialEnvironmentContract,
    TrialEnvironmentContractError,
    build_trial_environment_contract,
    git_revision,
    verify_contract_hash,
)

__all__ = [
    "TRIAL_ENVIRONMENT_CONTRACT_API_VERSION",
    "TrialEnvironmentContract",
    "TrialEnvironmentContractError",
    "build_trial_environment_contract",
    "git_revision",
    "verify_contract_hash",
]
