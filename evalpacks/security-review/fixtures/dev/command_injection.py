import subprocess


def healthcheck(host):
    return subprocess.run(f"ping -c 1 {host}", shell=True, check=False)
