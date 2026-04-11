# catalog/utils.py (new helper)
import re

_ST_RE = re.compile(r"\s+")

def clean_station(name: str) -> str:
    """
    Normalize kitchen station names:
    - trim spaces
    - replace spaces with underscores
    - uppercase
    - remove illegal chars
    - max 80 chars
    """
    s = _ST_RE.sub(" ", (name or "MAIN").strip()).upper()
    return re.sub(r"[^0-9A-Z._-]", "_", s)[:80]
