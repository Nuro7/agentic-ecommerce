"""Create product_affinity table for co-occurrence data"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'product_affinity',
        sa.Column('id', UUID(as_uuid=False), primary_key=True),
        sa.Column('tenant_id', sa.String(), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('product_id_a', sa.String(255), nullable=False),
        sa.Column('product_id_b', sa.String(255), nullable=False),
        sa.Column('co_count', sa.Integer(), nullable=False, default=0),
        sa.Column('pair_support', sa.Integer(), nullable=False, default=0),
        sa.Column('last_computed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('tenant_id', 'product_id_a', 'product_id_b', name='uq_affinity_tenant_pair'),
    )
    op.create_index('ix_affinity_tenant_a', 'product_affinity', ['tenant_id', 'product_id_a'])
    op.create_index('ix_affinity_tenant_b', 'product_affinity', ['tenant_id', 'product_id_b'])
    
    # Enable RLS using correct GUC: app.tenant_id (not app.current_tenant)
    op.execute("ALTER TABLE product_affinity ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON product_affinity
        USING (tenant_id = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true))
    """)

def downgrade():
    op.drop_table('product_affinity')