"""The guardian-family block is the same bytes in all four guardian READMEs. Edit it in one repo and
this test fails until the new hash is carried to the other three."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START = "<!-- guardian-family:start -->"
END = "<!-- guardian-family:end -->"
FAMILY_SHA256 = "0cd2ecb7fd53801c8aacae8e4ed48d9712dd97aba8503aca9ffcf0e48d7f8430"


def test_family_block_is_the_shared_bytes():
    text = (ROOT / "README.md").read_bytes().decode("utf-8").replace("\r\n", "\n")
    lines = text.split("\n")
    assert lines.count(START) == 1 and lines.count(END) == 1, "one start and one end marker line"
    first, last = lines.index(START), lines.index(END)
    assert first < last
    block = "\n".join(lines[first:last + 1]).rstrip("\n")
    assert hashlib.sha256(block.encode("utf-8")).hexdigest() == FAMILY_SHA256
