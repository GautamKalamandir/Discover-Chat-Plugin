"""report lenses: semantic_models.report_lenses, admin pins for /sales-summary tables (ADR 0016)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('semantic_models', sa.Column('report_lenses', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('semantic_models', 'report_lenses')
