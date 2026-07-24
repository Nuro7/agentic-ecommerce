-- RLS Role Fix: Create a NOSUPERUSER NOBYPASSRLS app role
--
-- The current app role 'agentic' has rolsuper=True / rolbypassrls=True,
-- which bypasses Row-Level Security entirely (migration 0013 is inert).
--
-- Run this as a PostgreSQL superuser (e.g. 'postgres') to create a safe
-- app role, then update DATABASE_URL in .env to use the new credentials.
--
-- Usage:
--   psql -U postgres -d your_db -f fix_rls_role.sql
--
-- Then update .env:
--   DATABASE_URL=postgresql+asyncpg://rls_app:<PASSWORD>@<HOST>:5432/<DB>
--   # Remove or set to false:
--   SPEAKO_ALLOW_RLS_BYPASS=false

DO $$
DECLARE
    rls_password TEXT := 'generate-a-strong-password-here';
BEGIN
    -- 1. Create the restricted app role (safe: cannot bypass RLS)
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rls_app') THEN
        CREATE ROLE rls_app LOGIN PASSWORD rls_password NOSUPERUSER NOBYPASSRLS;
    ELSE
        RAISE NOTICE 'Role rls_app already exists — skipping creation.';
    END IF;

    -- 2. Grant schema and table permissions
    GRANT USAGE ON SCHEMA public TO rls_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rls_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rls_app;

    -- 3. Ensure future tables are also accessible
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rls_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO rls_app;

    RAISE NOTICE 'Role rls_app created/updated successfully.';
    RAISE NOTICE 'Update your DATABASE_URL to use: rls_app / %', rls_password;
    RAISE NOTICE 'Then set SPEAKO_ALLOW_RLS_BYPASS=false in .env';
END $$;

-- 4. Verify the role is safe
SELECT
    rolname,
    rolsuper,
    rolbypassrls,
    CASE
        WHEN rolsuper OR rolbypassrls THEN 'UNSAFE — bypasses RLS'
        ELSE 'SAFE — RLS will be enforced'
    END AS rls_status
FROM pg_roles
WHERE rolname = 'rls_app';
