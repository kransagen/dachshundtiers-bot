"""kits.first_to – FT (first to N) HT Fightu podle kitu

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-10-03

Sloupec se seeduje podle provozovatele; kit, který tu není, zůstane NULL a
``/topresult`` ho odmítne, dokud admin nenastaví FT přes ``/setkitft``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None

# Klíč = název kitu bez mezer a interpunkce, malými písmeny.
SEED_FIRST_TO = {
    "uhcmace": 3,
    "molepvp": 4,
    "ironaxe": 5,
    "shieldlesssmp": 2,
    "netheritesword": 10,
    "goldsmp": 3,
    "anchorpvp": 4,
    "randompot": 3,
}


def upgrade() -> None:
    op.add_column("kits", sa.Column("first_to", sa.Integer(), nullable=True))
    for normalized, first_to in SEED_FIRST_TO.items():
        op.execute(
            sa.text(
                "UPDATE kits SET first_to = :ft "
                "WHERE regexp_replace(lower(key), '[^a-z0-9]', '', 'g') = :k"
            ).bindparams(ft=first_to, k=normalized)
        )


def downgrade() -> None:
    op.drop_column("kits", "first_to")
