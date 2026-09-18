"""Shared disposable PostgreSQL fixtures for storage contract suites."""

import os
import shutil
from urllib.parse import urlsplit
from uuid import uuid4

import pytest


@pytest.fixture(scope="session")
def postgres():
    from test_postgres_storage import Database, SCHEMAS, exercise_namespace_migration, exercise_reaction_migration
    from test_feature_postgres import exercise_feature_migration

    dsn = os.environ.get("HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set HUB_TEST_POSTGRES_DSN for the real PostgreSQL contract suite")
    if not shutil.which("psql"):
        pytest.fail("PostgreSQL contract tests require psql")
    db = Database(dsn)
    name = db.run("SELECT current_database();").stdout.strip()
    if not name.startswith("hub_test_"):
        pytest.fail("Refusing to initialize a database outside the hub_test_ namespace")
    if (
        db.run(
            "SELECT count(*) FROM pg_namespace WHERE nspname IN ('hub_private','hub_api','msu_hub_private','msu_hub_api','auth');"
        ).stdout.strip()
        != "0"
    ):
        pytest.fail("PostgreSQL contract tests require an empty disposable database")
    db.run("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon; END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated; END IF;
        END $$;
        CREATE SCHEMA auth;
        CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
        $$ SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
    """)
    for schema in SCHEMAS:
        if int(schema.name[:3]) > 5:
            break
        if schema.name == "003_application_namespaces.sql":
            db.namespace_upgrade = exercise_namespace_migration(db, schema.read_text())
        elif schema.name == "004_reactions.sql":
            db.reaction_upgrade = exercise_reaction_migration(db, schema.read_text())
        elif schema.name == "005_feature_storage.sql":
            db.feature_upgrade = exercise_feature_migration(db, schema.read_text())
        else:
            db.run(schema.read_text())
    return db


@pytest.fixture
def db(postgres):
    # The session fixture refuses existing schemas before installing the test schema.
    reset_database(postgres, legacy=True)
    return postgres


def reset_database(database, *, legacy):
    retired = "msu_hub_private.chat_settings,msu_hub_private.directory,msu_hub_private.vk_subscriptions," if legacy else ""
    database.run(f"""
        TRUNCATE {retired} msu_hub_private.chat_users,msu_hub_private.chat_topics,
            msu_hub_private.reaction_actors,msu_hub_private.reaction_counts,
            msu_hub_private.feature_records,msu_hub_private.feature_jobs,msu_hub_private.feature_operations,
            msu_hub_private.messages,msu_hub_private.updates,msu_hub_private.users,msu_hub_private.chats,
            msu_hub_private.mutation_journal,msu_hub_private.principals;
        INSERT INTO msu_hub_private.principals(auth_user_id,bot_id) VALUES ('00000000-0000-0000-0000-000000000001',999);
    """)


@pytest.fixture(scope="session")
def application_postgres(postgres):
    """Keep the historical RPC suite and complete schema independently executable."""
    from test_application_postgres import exercise_application_migrations
    from test_postgres_storage import Database, SCHEMAS

    parsed = urlsplit(postgres.dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        pytest.fail("PostgreSQL contracts require a PostgreSQL URI for the isolated upgrade database")
    original = postgres.run("SELECT current_database();").stdout.strip()
    name = "hub_test_documents_" + uuid4().hex[:12]
    prefix = f"{parsed.scheme}://{parsed.netloc}/"
    suffix = "?" + parsed.query if parsed.query else ""
    admin = Database(prefix + "postgres" + suffix)
    upgraded = Database(prefix + name + suffix)
    quoted_original = '"' + original.replace('"', '""') + '"'
    admin.run(f'CREATE DATABASE "{name}" TEMPLATE {quoted_original};')
    try:
        reset_database(upgraded, legacy=True)
        upgrades = [schema for schema in SCHEMAS if int(schema.name[:3]) > 5]
        upgraded.application_upgrade = exercise_application_migrations(upgraded, upgrades)
        yield upgraded
    finally:
        admin.run(f'DROP DATABASE "{name}";')


@pytest.fixture
def application_db(application_postgres):
    reset_database(application_postgres, legacy=False)
    return application_postgres
