-- Copy permanent application preferences and registries into typed documents.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
DO $$ BEGIN
    IF (SELECT max(version) FROM msu_hub_private.schema_migrations) IS DISTINCT FROM 5 THEN
        RAISE EXCEPTION 'Application documents require schema revision 5';
    END IF;
END $$;

-- Writers remain paused until revision 7 verifies and retires their old storage.
LOCK TABLE msu_hub_private.chats, msu_hub_private.chat_settings, msu_hub_private.directory,
    msu_hub_private.vk_subscriptions, msu_hub_private.feature_records IN SHARE ROW EXCLUSIVE MODE;

CREATE FUNCTION msu_hub_private.application_documents_source()
RETURNS TABLE(feature text, collection text, key text, payload jsonb, created_at timestamptz, updated_at timestamptz)
LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT 'settings', 'chats', s.chat_id::text,
        '{"auto_speech_recognition":true,"auto_video_links":true,"with_nsfw":false}'::jsonb || s.settings,
        c.created, s.updated_at
    FROM msu_hub_private.chat_settings s JOIN msu_hub_private.chats c USING(chat_id)
    UNION ALL
    SELECT 'ecosystem', 'chats', d.chat_id::text, to_jsonb(d), d.created, d.created
    FROM msu_hub_private.directory d
    UNION ALL
    SELECT 'vk', 'subscriptions', v.owner_id::text || ':' || v.chat_id::text, to_jsonb(v), v.created, v.created
    FROM msu_hub_private.vk_subscriptions v;
$$;

DO $$ BEGIN
    IF EXISTS (
        SELECT FROM msu_hub_private.feature_records WHERE owner_id = 0 AND scope_key = 'global'
        AND (feature, collection) IN (('settings','chats'),('ecosystem','chats'),('vk','subscriptions'))
    ) THEN
        RAISE EXCEPTION 'Application document destination is not empty';
    END IF;
    IF EXISTS (
        SELECT FROM msu_hub_private.chat_settings s,
            unnest(ARRAY['auto_speech_recognition','auto_video_links','with_nsfw']) AS field
        WHERE s.settings ? field AND jsonb_typeof(s.settings->field) IS DISTINCT FROM 'boolean'
    ) THEN
        RAISE EXCEPTION 'Application preferences contain invalid boolean values';
    END IF;
END $$;

INSERT INTO msu_hub_private.feature_records
    (owner_id, feature, scope_key, collection, key, payload_version, payload, created_at, updated_at)
SELECT 0, feature, 'global', collection, key, 1, payload, created_at, updated_at
FROM msu_hub_private.application_documents_source();

CREATE FUNCTION msu_hub_private.verify_application_documents() RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = '' AS $$
BEGIN
    IF EXISTS (
        SELECT FROM msu_hub_private.application_documents_source() expected FULL JOIN (
            SELECT * FROM msu_hub_private.feature_records WHERE owner_id = 0 AND scope_key = 'global'
            AND (feature, collection) IN (('settings','chats'),('ecosystem','chats'),('vk','subscriptions'))
        ) actual USING(feature, collection, key)
        WHERE expected.key IS NULL OR actual.key IS NULL
            OR actual.payload IS DISTINCT FROM expected.payload OR actual.payload_version <> 1
            OR actual.created_at IS DISTINCT FROM expected.created_at OR actual.updated_at IS DISTINCT FROM expected.updated_at
            OR actual.parent IS NOT NULL OR actual.status IS NOT NULL OR actual.expires_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'Application document parity check failed';
    END IF;
    RETURN jsonb_build_object(
        'settings', (SELECT count(*) FROM msu_hub_private.chat_settings),
        'directory', (SELECT count(*) FROM msu_hub_private.directory),
        'vk_subscriptions', (SELECT count(*) FROM msu_hub_private.vk_subscriptions));
END $$;

ALTER FUNCTION msu_hub_private.application_documents_source() OWNER TO msu_hub_owner;
ALTER FUNCTION msu_hub_private.verify_application_documents() OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_private.application_documents_source(),
    msu_hub_private.verify_application_documents() FROM PUBLIC, anon, authenticated;
DO $$ BEGIN PERFORM msu_hub_private.verify_application_documents(); END $$;

INSERT INTO msu_hub_private.schema_migrations(version) VALUES(6);
NOTIFY pgrst, 'reload schema';
COMMIT;
