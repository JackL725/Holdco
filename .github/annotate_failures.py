"""Turn unittest failures into GitHub Actions annotations, so a failing run shows the tracebacks on its summary page.

    python -m unittest discover -s tests -t . -v 2>&1 | tee test-output.txt
    python .github/annotate_failures.py test-output.txt      # on failure
"""
from __future__ import annotations

import re
import sys


def encode(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(path: str) -> None:
    text = open(path, encoding="utf-8", errors="replace").read()
    blocks = re.split(r"\n={70}\n", text)
    found = 0
    for block in blocks[1:]:
        head = block.strip().splitlines()[0] if block.strip() else ""
        if not head.startswith(("FAIL:", "ERROR:")):
            continue
        body = block.split("\n" + "-" * 70 + "\n", 1)[-1]
        body = body.split("\n" + "-" * 70, 1)[0].strip()
        if len(body) > 3800:
            body = "…" + body[-3800:]
        print(f"::error title={encode(head[:120])}::{encode(body)}")
        found += 1
        if found >= 9:
            break
    tail = "\n".join(text.strip().splitlines()[-4:])
    print(f"::error title=unittest summary::{encode(tail)}")


if __name__ == "__main__":
    main(sys.argv[1])
