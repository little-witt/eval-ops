"""Manual/CI smoke for the real Node + Chrome D2C worker."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys

from aceval.d2c import D2CBrowserProfile, D2CValidationRequest, validate_d2c


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: run_real_d2c_smoke.py URL OUTPUT")
    url = sys.argv[1]
    output = Path(sys.argv[2]).expanduser().resolve()
    node = shutil.which("node")
    chrome = next((item for item in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
    ) if item and Path(item).is_file()), None)
    if not node or not chrome:
        raise SystemExit("real D2C smoke requires Node.js and Chrome/Chromium")
    profile = D2CBrowserProfile.from_mapping({
        "api_version": "aceval.d2c-browser-profile/v1",
        "name": "real-local-chrome-smoke",
        "node_executable": node,
        "chrome_executable": chrome,
        "viewport_width": 1440,
        "viewport_height": 900,
        "device_scale_factor": 1.0,
        "locale": "zh-CN",
        "timezone": "Asia/Shanghai",
        "color_scheme": "light",
        "stability_wait_ms": 500,
        "timeout_seconds": 90,
    })
    base = {
        "api_version": "aceval.d2c-validation-request/v1",
        "case_id": "real-browser-smoke",
        "url": url,
        "candidate_commit": "c" * 40,
        "actions": [{"type": "click", "selector": "#verify", "timeout_ms": 5000}],
        "expected_title": "FORGE D2C Fixture",
        "metadata": {"purpose": "product-release-smoke"},
    }
    reference_receipt = validate_d2c(profile, D2CValidationRequest.from_mapping(base), output / "reference")
    reference = output / "reference" / "screenshot.png"
    digest = "sha256:" + hashlib.sha256(reference.read_bytes()).hexdigest()
    compared = dict(base)
    compared["visual_oracle"] = {"path": str(reference), "sha256": digest, "max_diff_ratio": 0.0, "pixel_threshold": 0}
    comparison_receipt = validate_d2c(profile, D2CValidationRequest.from_mapping(compared), output / "comparison")
    value = {"reference": reference_receipt, "comparison": comparison_receipt}
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if reference_receipt["status"] == "succeeded" and comparison_receipt["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
