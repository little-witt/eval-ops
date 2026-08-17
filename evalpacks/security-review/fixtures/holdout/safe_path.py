from pathlib import Path

ROOT = Path("/srv/files").resolve()

def read_document(name):
    candidate = (ROOT / name).resolve()
    candidate.relative_to(ROOT)
    return candidate.read_text()
