"""Real PostgreSQL regression for concurrent first-use withdrawal-destination creation."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_postgres_destination_first_use_race_reuses_the_committed_winner():
    url = os.getenv("ATLAS_POSTGRES_URL", "").strip()
    if not url:
        pytest.skip("requires the mandatory PostgreSQL service (ATLAS_POSTGRES_URL)")

    from app.db import WithdrawalDestination
    from app.transaction_policy import register_or_check_destination
    from app.withdrawal_security import destination_fingerprint

    run_token = uuid.uuid4().hex
    customer_id = 1_000_000_000 + int(run_token[:7], 16)
    destination = f"TRON-RACE-{run_token}"
    currency, network = "USDT", "TRON"
    fingerprint = destination_fingerprint(destination, currency, network)
    now = datetime.now(timezone.utc)

    engine = create_async_engine(url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    class _InsertWinnerAfterEmptyRead:
        """Return the real empty SELECT, then commit the competing row before INSERT."""

        def __init__(self, session):
            self.session = session
            self.injected = False

        def __getattr__(self, name):
            return getattr(self.session, name)

        async def execute(self, statement):
            result = await self.session.execute(statement)
            if not self.injected:
                self.injected = True
                async with sessions() as winner_db:
                    winner_db.add(
                        WithdrawalDestination(
                            customer_id=customer_id,
                            fingerprint=fingerprint,
                            currency=currency,
                            network=network,
                            first_seen_at=now,
                            status="PENDING",
                        )
                    )
                    await winner_db.commit()
            return result

    try:
        async with sessions() as db:
            wrapped = _InsertWinnerAfterEmptyRead(db)
            result = await register_or_check_destination(
                wrapped,
                customer_id=customer_id,
                destination=destination,
                currency=currency,
                network=network,
                now=now,
            )
            await db.commit()

        assert result["allowed"] is False
        assert result["reason"] == "new_destination_cooling_off"
        assert result["fingerprint"] == fingerprint

        async with sessions() as verify_db:
            row = (
                await verify_db.execute(
                    select(WithdrawalDestination).where(
                        WithdrawalDestination.customer_id == customer_id,
                        WithdrawalDestination.fingerprint == fingerprint,
                    )
                )
            ).scalar_one()
            assert row.status == "PENDING"
            assert row.last_used_at == now
    finally:
        async with sessions() as cleanup_db:
            await cleanup_db.execute(
                delete(WithdrawalDestination).where(
                    WithdrawalDestination.customer_id == customer_id,
                    WithdrawalDestination.fingerprint == fingerprint,
                )
            )
            await cleanup_db.commit()
        await engine.dispose()
