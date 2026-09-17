-- Rename this application's namespaces without replacing its data or API contract.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';
SET LOCAL search_path = '';

DO $rename$
DECLARE
    expected record;
    routine pg_proc;
    definitions jsonb := '[]';
    saved jsonb;
    permissions jsonb;
    owner_id oid := to_regrole('hub_owner');
    private_id oid := to_regnamespace('hub_private');
    api_id oid := to_regnamespace('hub_api');
BEGIN
    IF private_id IS NULL OR api_id IS NULL OR owner_id IS NULL
       OR (SELECT array_agg(version ORDER BY version) FROM hub_private.schema_migrations) IS DISTINCT FROM ARRAY[1,2] THEN
        RAISE EXCEPTION 'Namespace migration requires application schema revision 2';
    END IF;
    IF to_regnamespace('msu_hub_private') IS NOT NULL OR to_regnamespace('msu_hub_api') IS NOT NULL
       OR to_regrole('msu_hub_owner') IS NOT NULL THEN
        RAISE EXCEPTION 'Application namespace rename target already exists';
    END IF;
    IF EXISTS(SELECT FROM pg_namespace WHERE oid IN (private_id,api_id) AND nspowner <> owner_id)
       OR EXISTS(SELECT FROM pg_roles WHERE oid = owner_id
           AND (rolcanlogin OR rolinherit OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))
       OR EXISTS(SELECT FROM pg_auth_members WHERE roleid = owner_id OR member = owner_id)
       OR (SELECT count(*) FROM pg_proc WHERE pronamespace IN (private_id,api_id)) <> 22 THEN
        RAISE EXCEPTION 'Application ownership or routine set differs from the reviewed contract';
    END IF;

    -- Exact bodies from migrations 001/002 bound this rewrite to reviewed code.
    -- Capture definitions before renaming: new names contain the old names as suffixes.
    FOR expected IN SELECT * FROM (VALUES
        ('hub_private.journal_mutation()', '93dedb73c5288da40c4edc039536cc544f89f60f061358e24f87ae90a45b70d5', false, 'v'),
        ('hub_private.require_principal()', '4f6b2df217b5bf37d27488a11911417b5b7c162c8d1a622145608b6645fb2d98', true, 's'),
        ('hub_private.check_object(jsonb, text[], text[])', '38911d48d939f35160bdf2b47b0623b7cf175157bf1a9de85c7487b0aaccf0c5', false, 'i'),
        ('hub_private.observe_user(jsonb)', '8b7436b57ada5cbef7bcc82324a6ea684a39a35c55bffb62dcbdc01f2c85f819', false, 'v'),
        ('hub_private.observe_chat(jsonb, boolean)', 'c760017bd2ddf72e09218316ac008f70e77dddebdd038952dc96a9cc22f474fe', false, 'v'),
        ('hub_api.health_v1()', '42f139b3f1a1d97ddadd3b3ca3c288ad389411f9a554d6825d16fd3eac07cc63', true, 's'),
        ('hub_api.ensure_chat_v1(jsonb)', 'bfc0e9b8f03c5a58588c38caa26ea8ef37e0684571af33f2bc4724fdf5e99717', true, 'v'),
        ('hub_api.get_chat_v1(bigint)', '2aceeb82abd20a9cdcfcc5670e5e9060558bc489fcf917a13c1fb4d68ed15274', true, 's'),
        ('hub_api.load_settings_v1(jsonb)', 'd54df2ae295a5526d807dd89bab747400e078a78b442c60f60b674dc29ba94d5', true, 'v'),
        ('hub_api.patch_settings_v1(bigint, jsonb)', 'b702831c807d1270568de5d115122defb6c7b857cc0468863f69fafc12bf9605', true, 'v'),
        ('hub_api.statistics_v1(timestamptz)', '660577a30fdb46606fc65f700d65b8c8d44e6e678f4a5f0aa10e7a73ab61581e', true, 's'),
        ('hub_api.list_directory_v1()', '17aa64e70772354009b1eba4d43652311265b3cf5506308bbfe313892d9c607a', true, 's'),
        ('hub_api.get_directory_v1(bigint)', '245927ae1a08d6e6cb8e1e1e70f42e0cf23a95e69b235bca32573e5d5fd451f0', true, 's'),
        ('hub_api.create_directory_v1(jsonb)', 'd296ec99d139617efaf34ba8ca9f06ceb7bc2379a80bd2daad4a067a3833ee93', true, 'v'),
        ('hub_api.patch_directory_v1(bigint, jsonb)', 'd57a15ce40afac289ae3f25fd5850466fe88e761f116c048e6331595e764d8ee', true, 'v'),
        ('hub_api.delete_directory_v1(bigint)', 'a4d4c557b9759d06109fb48282d6d737dbac5fdf6c74b27f746e0d54edce9209', true, 'v'),
        ('hub_api.list_vk_subscriptions_v1()', 'c16f8920e6bc09a93b619e685640f76f875580f5b76d7562b8dc20ff029ea0a9', true, 's'),
        ('hub_api.upsert_vk_subscription_v1(bigint, bigint, jsonb)', '5e16d52074cf37a357081a227eb27d23c617dc3e2d2bc5e98265f75f42ba1f63', true, 'v'),
        ('hub_api.advance_vk_cursor_v1(bigint, bigint, bigint)', '0b369d53e88fe0a0703c5f153a7253692fa384c21fa12fa7ee41ddd2c6b14c4e', true, 'v'),
        ('hub_api.archive_update_v1(jsonb)', 'b0d331c5ffd1019cdcd5edeeba90d632d7e9018e00995bb97a43bcf0ad166899', true, 'v'),
        ('hub_private.retain_messages(integer, timestamptz)', '166b41c9a98051d802eeffad9e16d3396108e08bfe020c78807bfcb1120cb469', false, 'v'),
        ('hub_private.observe_archive(jsonb, bigint, uuid, timestamptz, timestamptz)', 'f49929ee5eb7fa3ec6cd4749926abbe37eaa48f869889956fac00dced43b49e8', false, 'v')
    ) AS contract(signature, body_sha256, security_definer, volatility) LOOP
        SELECT * INTO routine FROM pg_proc WHERE oid = to_regprocedure(expected.signature);
        IF NOT FOUND OR routine.proowner <> owner_id
           OR routine.prolang <> (SELECT oid FROM pg_language WHERE lanname = 'plpgsql')
           OR routine.prokind <> 'f' OR routine.prosecdef IS DISTINCT FROM expected.security_definer
           OR routine.provolatile::text <> expected.volatility
           OR routine.proconfig IS DISTINCT FROM ARRAY['search_path=""']
           OR encode(sha256(convert_to(routine.prosrc,'UTF8')),'hex') <> expected.body_sha256 THEN
            RAISE EXCEPTION 'Application routine differs from the reviewed contract';
        END IF;
        SELECT jsonb_agg(to_jsonb(a) ORDER BY a.grantor,a.grantee,a.privilege_type)
        INTO permissions FROM aclexplode(routine.proacl) a;
        definitions := definitions || jsonb_build_array(jsonb_build_object(
            'oid',routine.oid, 'definition',pg_get_functiondef(routine.oid), 'body',routine.prosrc,
            'catalog',to_jsonb(routine) - 'prosrc' - 'proargdefaults' - 'proacl',
            'acl',permissions, 'acl_is_null',routine.proacl IS NULL,
            'defaults',pg_get_expr(routine.proargdefaults,0)));
    END LOOP;

    ALTER SCHEMA hub_private RENAME TO msu_hub_private;
    ALTER SCHEMA hub_api RENAME TO msu_hub_api;
    ALTER ROLE hub_owner RENAME TO msu_hub_owner;

    FOR saved IN SELECT value FROM jsonb_array_elements(definitions) LOOP
        IF strpos(saved->>'body','hub_private.') > 0 THEN
            EXECUTE replace(replace(saved->>'definition',
                'hub_private.','msu_hub_private.'),'hub_api.','msu_hub_api.');
        END IF;
        SELECT * INTO routine FROM pg_proc WHERE oid = (saved->>'oid')::oid;
        IF NOT FOUND THEN RAISE EXCEPTION 'Namespace migration lost a routine identity'; END IF;
        SELECT jsonb_agg(to_jsonb(a) ORDER BY a.grantor,a.grantee,a.privilege_type)
        INTO permissions FROM aclexplode(routine.proacl) a;
        IF to_jsonb(routine) - 'prosrc' - 'proargdefaults' - 'proacl' IS DISTINCT FROM saved->'catalog'
           OR permissions IS DISTINCT FROM nullif(saved->'acl','null'::jsonb)
           OR to_jsonb(routine.proacl IS NULL) IS DISTINCT FROM saved->'acl_is_null'
           OR to_jsonb(pg_get_expr(routine.proargdefaults,0)) IS DISTINCT FROM nullif(saved->'defaults','null'::jsonb)
           OR routine.prosrc IS DISTINCT FROM replace(saved->>'body','hub_private.','msu_hub_private.') THEN
            RAISE EXCEPTION 'Namespace migration changed a routine contract';
        END IF;
    END LOOP;
    IF to_regnamespace('msu_hub_private') IS DISTINCT FROM private_id
       OR to_regnamespace('msu_hub_api') IS DISTINCT FROM api_id
       OR to_regrole('msu_hub_owner') IS DISTINCT FROM owner_id THEN
        RAISE EXCEPTION 'Namespace migration changed object identities';
    END IF;
END;
$rename$;

INSERT INTO msu_hub_private.schema_migrations(version) VALUES (3);
NOTIFY pgrst, 'reload schema';
COMMIT;
