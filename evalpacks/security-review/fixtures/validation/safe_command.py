import subprocess

ALLOWED_SERVICES = {"api", "worker"}


def status(service):
    if service not in ALLOWED_SERVICES:
        raise ValueError("unknown service")
    return subprocess.run(["systemctl", "status", service], check=False)
