"""summary reports: summary_reports, report_download_tokens, query_executions.report_id (ADR 0015)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('summary_reports',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.Enum('building', 'ready', 'failed', name='report_status', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('report_date', sa.Date(), nullable=False),
    sa.Column('models', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('headline', sa.String(length=300), nullable=True),
    sa.Column('suggestion_counts', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('json_path', sa.String(length=512), nullable=True),
    sa.Column('pdf_path', sa.String(length=512), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('building', 'ready', 'failed')", name=op.f('ck_summary_reports_report_status')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_summary_reports_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_summary_reports'))
    )
    op.create_index(op.f('ix_summary_reports_created_at'), 'summary_reports', ['created_at'], unique=False)
    op.create_index('ix_summary_reports_user_created', 'summary_reports', ['user_id', 'created_at'], unique=False)
    op.create_index('uq_summary_reports_one_building', 'summary_reports', ['user_id'], unique=True, postgresql_where=sa.text("status = 'building'"))

    op.create_table('report_download_tokens',
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('report_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['summary_reports.id'], name=op.f('fk_report_download_tokens_report_id_summary_reports'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_report_download_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('token_hash', name=op.f('pk_report_download_tokens'))
    )
    op.create_index(op.f('ix_report_download_tokens_expires_at'), 'report_download_tokens', ['expires_at'], unique=False)
    op.create_index(op.f('ix_report_download_tokens_report_id'), 'report_download_tokens', ['report_id'], unique=False)

    # A query execution now belongs to a chat session or to a report, never both.
    op.alter_column('query_executions', 'session_id', existing_type=sa.UUID(), nullable=True)
    op.add_column('query_executions', sa.Column('report_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_query_executions_report_id'), 'query_executions', ['report_id'], unique=False)
    op.create_foreign_key(op.f('fk_query_executions_report_id_summary_reports'), 'query_executions', 'summary_reports', ['report_id'], ['id'], ondelete='CASCADE')
    op.create_check_constraint(op.f('ck_query_executions_one_owner'), 'query_executions', '(session_id IS NULL) <> (report_id IS NULL)')


def downgrade() -> None:
    """Downgrade schema."""
    # Report queries have no chat session; they go with the reports.
    op.execute('DELETE FROM query_executions WHERE session_id IS NULL')
    op.drop_constraint(op.f('ck_query_executions_one_owner'), 'query_executions', type_='check')
    op.drop_constraint(op.f('fk_query_executions_report_id_summary_reports'), 'query_executions', type_='foreignkey')
    op.drop_index(op.f('ix_query_executions_report_id'), table_name='query_executions')
    op.drop_column('query_executions', 'report_id')
    op.alter_column('query_executions', 'session_id', existing_type=sa.UUID(), nullable=False)

    op.drop_index(op.f('ix_report_download_tokens_report_id'), table_name='report_download_tokens')
    op.drop_index(op.f('ix_report_download_tokens_expires_at'), table_name='report_download_tokens')
    op.drop_table('report_download_tokens')
    op.drop_index('uq_summary_reports_one_building', table_name='summary_reports', postgresql_where=sa.text("status = 'building'"))
    op.drop_index('ix_summary_reports_user_created', table_name='summary_reports')
    op.drop_index(op.f('ix_summary_reports_created_at'), table_name='summary_reports')
    op.drop_table('summary_reports')
