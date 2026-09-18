-- Remove duplicated application storage only after an exact stopped-writer comparison.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
DO $$ BEGIN
    IF (SELECT max(version) FROM msu_hub_private.schema_migrations) IS DISTINCT FROM 6 THEN
        RAISE EXCEPTION 'Application table retirement requires schema revision 6';
    END IF;
END $$;

LOCK TABLE msu_hub_private.chats, msu_hub_private.chat_settings, msu_hub_private.directory,
    msu_hub_private.vk_subscriptions, msu_hub_private.feature_records IN SHARE ROW EXCLUSIVE MODE;
DO $$ BEGIN PERFORM msu_hub_private.verify_application_documents(); END $$;

-- Old callback snapshots may establish a missing chat without replacing newer observations.
DROP FUNCTION msu_hub_api.ensure_chat_v1(jsonb) RESTRICT;
CREATE FUNCTION msu_hub_api.ensure_chat_v1(p_chat jsonb, p_refresh boolean DEFAULT true) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM msu_hub_private.require_principal();
    IF p_refresh IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid chat refresh policy';
    END IF;
    RETURN to_jsonb(msu_hub_private.observe_chat(p_chat, p_refresh));
END $$;
ALTER FUNCTION msu_hub_api.ensure_chat_v1(jsonb,boolean) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_api.ensure_chat_v1(jsonb,boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION msu_hub_api.ensure_chat_v1(jsonb,boolean) TO authenticated;

DROP FUNCTION msu_hub_api.load_settings_v1(jsonb), msu_hub_api.patch_settings_v1(bigint,jsonb),
    msu_hub_api.list_directory_v1(), msu_hub_api.get_directory_v1(bigint), msu_hub_api.create_directory_v1(jsonb),
    msu_hub_api.patch_directory_v1(bigint,jsonb), msu_hub_api.delete_directory_v1(bigint),
    msu_hub_api.list_vk_subscriptions_v1(), msu_hub_api.upsert_vk_subscription_v1(bigint,bigint,jsonb),
    msu_hub_api.advance_vk_cursor_v1(bigint,bigint,bigint) RESTRICT;
DROP FUNCTION msu_hub_private.verify_application_documents(), msu_hub_private.application_documents_source() RESTRICT;
-- The journal and its user/chat hooks are independent recovery data and remain intact.
DROP TABLE msu_hub_private.chat_settings, msu_hub_private.directory, msu_hub_private.vk_subscriptions RESTRICT;

CREATE OR REPLACE FUNCTION msu_hub_api.health_v1() RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE bot bigint := msu_hub_private.require_principal();
BEGIN
    RETURN jsonb_build_object('schema_version',1,'bot_id',bot,'application_documents',1);
END $$;
ALTER FUNCTION msu_hub_api.health_v1() OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_api.health_v1() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION msu_hub_api.health_v1() TO authenticated;

INSERT INTO msu_hub_private.schema_migrations(version) VALUES(7);
NOTIFY pgrst, 'reload schema';
COMMIT;
