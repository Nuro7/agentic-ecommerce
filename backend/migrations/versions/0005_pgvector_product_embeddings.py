"""Enable pgvector extension and add embedding column to product_cache.

Also adds:
  • tsvector search column for BM25 full-text search
  • ivfflat index for cosine similarity (vector search)
  • GIN index on tsvector for BM25

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pgvector extension (requires PostgreSQL 11+ with pgvector installed)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Add embedding column (1536 dims = OpenAI text-embedding-3-small)
    op.add_column(
        "product_cache",
        sa.Column(
            "embedding",
            sa.Text(),          # stored as text "[ 0.1, 0.2, ...]" then cast to vector
            nullable=True,
        ),
    )

    # Add tags column for filtering (comma-separated or JSON array as text)
    op.add_column(
        "product_cache",
        sa.Column("tags", sa.Text(), nullable=True),
    )

    # Add category_slug for category filter
    op.add_column(
        "product_cache",
        sa.Column("category_slug", sa.String(255), nullable=True),
    )

    # Add tsvector column for BM25 full-text search
    # Populated by a trigger or explicit UPDATE during product sync
    op.add_column(
        "product_cache",
        sa.Column("search_vector", sa.Text(), nullable=True),
    )

    # Convert embedding text column to actual vector type (NULL-safe)
    op.execute(
        "ALTER TABLE product_cache "
        "ALTER COLUMN embedding TYPE vector(1536) "
        "USING CASE WHEN embedding IS NOT NULL THEN embedding::vector ELSE NULL END"
    )

    # Convert search_vector to tsvector type (NULL-safe)
    op.execute(
        "ALTER TABLE product_cache "
        "ALTER COLUMN search_vector TYPE tsvector "
        "USING CASE WHEN search_vector IS NOT NULL THEN search_vector::tsvector ELSE NULL END"
    )

    # ivfflat index for approximate cosine similarity (vector search)
    # lists=100 is suitable for up to ~1M rows per tenant
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_product_cache_embedding "
        "ON product_cache USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )

    # GIN index for full-text search (BM25 via tsvector)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_product_cache_search_vector "
        "ON product_cache USING GIN (search_vector)"
    )

    # Trigger to auto-update search_vector whenever name/description changes
    op.execute("""
        CREATE OR REPLACE FUNCTION product_cache_search_vector_update()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.name, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.description, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(NEW.tags, '')), 'C');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        CREATE TRIGGER product_cache_search_vector_trigger
        BEFORE INSERT OR UPDATE ON product_cache
        FOR EACH ROW EXECUTE FUNCTION product_cache_search_vector_update();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS product_cache_search_vector_trigger ON product_cache")
    op.execute("DROP FUNCTION IF EXISTS product_cache_search_vector_update()")
    op.execute("DROP INDEX IF EXISTS ix_product_cache_search_vector")
    op.execute("DROP INDEX IF EXISTS ix_product_cache_embedding")
    op.drop_column("product_cache", "search_vector")
    op.drop_column("product_cache", "category_slug")
    op.drop_column("product_cache", "tags")
    op.drop_column("product_cache", "embedding")
