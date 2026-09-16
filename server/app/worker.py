"""
Background evaluation loop: alert rules, fleet budgets, retention purges.

Runs as its own container (``python -m app.worker``) so exactly one process evaluates alerts, however many
API workers are serving traffic. ``main.py`` can also run it in-process with ENABLE_ALERT_WORKER=1 for local
development.
"""

from __future__ import annotations

import asyncio
import logging
import os

from app.database import SessionLocal

logger = logging.getLogger("agentkit.cloud.worker")

CYCLE_SECONDS = 60.0


async def run_cycle() -> None:
    """One pass: evaluate alert rules, budgets, and retention, then commit."""
    from app.alerting.evaluator import evaluate_all_rules
    from app.budgets import evaluate_all_budgets
    from app.compliance.retention import purge_expired

    async with SessionLocal() as db:
        await evaluate_all_rules(db)
        await evaluate_all_budgets(db)
        await purge_expired(db)
        await db.commit()


async def run_forever(cycle_seconds: float = CYCLE_SECONDS) -> None:
    """Run a cycle every ``cycle_seconds``. A failed cycle is logged; the loop continues."""
    while True:
        await asyncio.sleep(cycle_seconds)
        try:
            await run_cycle()
            logger.debug("alert worker cycle complete")
        except Exception as exc:
            logger.warning("Alert worker error: %s", exc)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("agent-kit Cloud worker started (%.0fs cadence)", CYCLE_SECONDS)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
