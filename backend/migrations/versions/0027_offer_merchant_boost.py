"""Add merchant_boost to product_offers"""
from alembic import op
import sqlalchemy as sa

revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('product_offers',
        sa.Column('merchant_boost', sa.Float(), nullable=False, server_default='1.0'))

def downgrade():
    op.drop_column('product_offers', 'merchant_boost')