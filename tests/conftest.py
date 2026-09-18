"""Shared disposable PostgreSQL fixtures for storage contract suites."""

import os
import shutil

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
    postgres.run("""
        TRUNCATE msu_hub_private.chat_users,msu_hub_private.chat_topics,msu_hub_private.chat_settings,
            msu_hub_private.reaction_actors,msu_hub_private.reaction_counts,
            msu_hub_private.feature_records,msu_hub_private.feature_jobs,msu_hub_private.feature_operations,
            msu_hub_private.messages,msu_hub_private.updates,msu_hub_private.users,msu_hub_private.chats,
            msu_hub_private.directory,msu_hub_private.vk_subscriptions,msu_hub_private.mutation_journal,msu_hub_private.principals;
        INSERT INTO msu_hub_private.principals(auth_user_id,bot_id) VALUES ('00000000-0000-0000-0000-000000000001',999);
    """)
    return postgres
