from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "wuying.interfaces.api:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
