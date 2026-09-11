"""Write docs/openapi.json from the FastAPI app (no server needed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

OUT = REPO_ROOT / "docs" / "openapi.json"


def main() -> None:
    app = create_app(Settings(auth_mode="hs256", demo_enabled=True))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
