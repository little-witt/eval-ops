from pathlib import Path
from urllib.parse import unquote

ROOT = Path("/srv/files")

def read_document(name):
    return (ROOT / unquote(name)).read_text()
