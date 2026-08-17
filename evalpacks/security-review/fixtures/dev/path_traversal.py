from pathlib import Path


def download(name):
    return Path("/srv/files", name).read_text()
