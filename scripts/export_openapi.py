"""Write docs/openapi.json from the FastAPI app (no server needed)."""

from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.main import create_app

OUT = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def main() -> None:
    app = create_app(Settings(auth_mode="hs256", demo_enabled=True))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
