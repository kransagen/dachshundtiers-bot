"""Transaction boundary: one session, one transaction, commit-or-rollback.

The caller supplies a session factory; the fresh session is the ONLY thing the
body may use, so a failure in the body can never leave a half-committed state
behind. Uses ``session.begin()`` — commit is automatic on clean exit and the
`async with` exit does the rollback on exception.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@asynccontextmanager
async def transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        async with session.begin():
            yield session