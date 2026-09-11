"""One-shot bootstrap run after migrations: create the attachment bucket if missing.

Usage: python -m app.bootstrap
"""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.logging import configure_logging
from app.storage import AttachmentStorage

log = logging.getLogger("app.bootstrap")


async def main(attempts: int = 30) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    storage = AttachmentStorage(settings)
    for attempt in range(1, attempts + 1):
        try:
            await storage.ensure_bucket()
            log.info("bucket ready", extra={"bucket": settings.s3_bucket})
            return
        except Exception as exc:
            log.warning("storage not ready", extra={"attempt": attempt, "error": repr(exc)})
            await asyncio.sleep(2)
    raise SystemExit("storage bootstrap failed")


if __name__ == "__main__":
    asyncio.run(main())
