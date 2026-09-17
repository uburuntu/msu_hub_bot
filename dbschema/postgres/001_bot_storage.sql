-- Application tables and authenticated RPCs are isolated from other applications.
BEGIN;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_owner') THEN
        CREATE ROLE hub_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    ELSIF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'hub_owner'
        AND (rolcanlogin OR rolinherit OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT 1 FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname = 'hub_owner')
    ) THEN
        RAISE EXCEPTION 'Existing hub_owner role has incompatible privileges';
    END IF;
END;
$$;
CREATE SCHEMA hub_private;
CREATE SCHEMA hub_api;
REVOKE ALL ON SCHEMA hub_private, hub_api FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA hub_api TO authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA hub_private, hub_api REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE hub_private.schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO hub_private.schema_migrations(version) VALUES (1);

-- A project administrator provisions identities; callers cannot choose their bot scope.
CREATE TABLE hub_private.principals (
    auth_user_id uuid PRIMARY KEY,
    bot_id bigint NOT NULL,
    enabled boolean NOT NULL DEFAULT true
);

CREATE TABLE hub_private.users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created timestamptz NOT NULL DEFAULT now(),
    user_id bigint UNIQUE NOT NULL,
    is_bot boolean NOT NULL,
    first_name text NOT NULL,
    last_name text,
    username text,
    language_code text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    profile jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(profile) = 'object'),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX users_created ON hub_private.users(created);

CREATE TABLE hub_private.chats (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created timestamptz NOT NULL DEFAULT now(),
    chat_id bigint UNIQUE NOT NULL,
    type text NOT NULL,
    title text,
    username text,
    first_name text,
    last_name text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    profile jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(profile) = 'object'),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chats_created ON hub_private.chats(created);

-- Original metadata is retained verbatim; this object owns current preferences.
CREATE TABLE hub_private.chat_settings (
    chat_id bigint PRIMARY KEY REFERENCES hub_private.chats(chat_id) ON DELETE RESTRICT,
    settings jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(settings) = 'object'),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE hub_private.chat_users (
    chat_id bigint REFERENCES hub_private.chats(chat_id) ON DELETE RESTRICT,
    user_id bigint REFERENCES hub_private.users(user_id) ON DELETE RESTRICT,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    status text,
    permissions jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(permissions) = 'object'),
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE hub_private.chat_topics (
    chat_id bigint REFERENCES hub_private.chats(chat_id) ON DELETE RESTRICT,
    thread_id bigint,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    title text,
    is_closed boolean,
    profile jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(profile) = 'object'),
    PRIMARY KEY (chat_id, thread_id)
);

CREATE TABLE hub_private.updates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created timestamptz NOT NULL DEFAULT now(),
    data jsonb NOT NULL,
    handled boolean NOT NULL DEFAULT false,
    bot_id bigint NOT NULL,
    update_id bigint,
    kind text,
    is_legacy boolean NOT NULL DEFAULT false,
    CHECK (is_legacy OR update_id IS NOT NULL)
);
CREATE UNIQUE INDEX updates_new_receipt ON hub_private.updates(bot_id, update_id) WHERE NOT is_legacy;
CREATE INDEX updates_created ON hub_private.updates(created);
CREATE INDEX updates_statistics ON hub_private.updates(bot_id, created) INCLUDE (handled);

CREATE TABLE hub_private.messages (
    bot_id bigint,
    chat_id bigint,
    message_id bigint,
    business_connection_id text NOT NULL DEFAULT '',
    sent_at timestamptz NOT NULL,
    edited_at timestamptz,
    observed_at timestamptz NOT NULL,
    sender_user_id bigint,
    sender_chat_id bigint,
    thread_id bigint,
    reply_to_message_id bigint,
    -- These references may outlive the retained source/reply payloads.
    source_update_id uuid,
    data jsonb NOT NULL CHECK (jsonb_typeof(data) = 'object'),
    PRIMARY KEY (bot_id, chat_id, message_id, business_connection_id)
);
CREATE INDEX messages_sent_at ON hub_private.messages(sent_at);
CREATE INDEX messages_chat_date ON hub_private.messages(chat_id, sent_at);

-- Legacy directory and subscription IDs need not reference observed Telegram chats.
CREATE TABLE hub_private.directory (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created timestamptz NOT NULL DEFAULT now(),
    chat_id bigint UNIQUE NOT NULL,
    name text NOT NULL,
    section text NOT NULL,
    is_hidden boolean NOT NULL DEFAULT false,
    username_alias text DEFAULT '',
    members bigint,
    pinned_message_id bigint
);
CREATE INDEX directory_created ON hub_private.directory(created);

CREATE TABLE hub_private.vk_subscriptions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created timestamptz NOT NULL DEFAULT now(),
    owner_id bigint NOT NULL,
    chat_id bigint NOT NULL,
    last_post_id bigint NOT NULL DEFAULT 0,
    with_reposts boolean NOT NULL DEFAULT false,
    with_header boolean NOT NULL DEFAULT true,
    is_suspended boolean NOT NULL DEFAULT false,
    description text,
    UNIQUE(owner_id, chat_id)
);
CREATE INDEX vk_subscriptions_created ON hub_private.vk_subscriptions(created);

-- Only durable source-equivalent records are journaled, never message payloads.
CREATE TABLE hub_private.mutation_journal (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    changed_at timestamptz NOT NULL DEFAULT now(),
    relation_name text NOT NULL,
    operation text NOT NULL CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
    row_data jsonb NOT NULL
);
CREATE FUNCTION hub_private.journal_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE payload jsonb;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
    payload := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    IF TG_OP <> 'DELETE' THEN
        SELECT jsonb_object_agg(key,value) INTO payload FROM jsonb_each(payload)
        WHERE key IN ('id','user_id','chat_id','owner_id');
    END IF;
    INSERT INTO hub_private.mutation_journal(relation_name, operation, row_data)
    VALUES (TG_TABLE_NAME, TG_OP, payload);
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

DO $$
DECLARE relation text;
BEGIN
    FOREACH relation IN ARRAY ARRAY['users','chats','chat_settings','directory','vk_subscriptions'] LOOP
        EXECUTE format('CREATE TRIGGER journal AFTER INSERT OR UPDATE OR DELETE ON hub_private.%I FOR EACH ROW EXECUTE FUNCTION hub_private.journal_mutation()', relation);
    END LOOP;
    FOREACH relation IN ARRAY ARRAY['schema_migrations','principals','users','chats','chat_settings','chat_users','chat_topics','updates','messages','directory','vk_subscriptions','mutation_journal'] LOOP
        EXECUTE format('ALTER TABLE hub_private.%I ENABLE ROW LEVEL SECURITY', relation);
    END LOOP;
END;
$$;

CREATE FUNCTION hub_private.require_principal() RETURNS bigint
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE scoped_bot bigint;
BEGIN
    SELECT bot_id INTO scoped_bot FROM hub_private.principals
    WHERE auth_user_id = auth.uid() AND enabled;
    IF scoped_bot IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Bot principal is not authorized';
    END IF;
    RETURN scoped_bot;
END;
$$;

CREATE FUNCTION hub_private.check_object(value jsonb, allowed text[], required text[] DEFAULT '{}') RETURNS void
LANGUAGE plpgsql IMMUTABLE SET search_path = '' AS $$
BEGIN
    IF value IS NULL OR jsonb_typeof(value) <> 'object'
       OR EXISTS (SELECT 1 FROM jsonb_object_keys(value) AS k WHERE NOT k = ANY(allowed))
       OR EXISTS (SELECT 1 FROM unnest(required) AS k WHERE NOT value ? k OR value->k = 'null'::jsonb)
    THEN RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid storage request'; END IF;
END;
$$;

CREATE FUNCTION hub_private.observe_user(value jsonb) RETURNS void
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE stamp timestamptz := COALESCE((value->>'observed_at')::timestamptz, now());
BEGIN
    PERFORM hub_private.check_object(value, ARRAY['user_id','is_bot','first_name','last_name','username','language_code','observed_at','profile'], ARRAY['user_id','is_bot','first_name']);
    INSERT INTO hub_private.users AS existing(user_id,is_bot,first_name,last_name,username,language_code,profile,first_seen_at,last_seen_at)
    VALUES ((value->>'user_id')::bigint,(value->>'is_bot')::boolean,value->>'first_name',value->>'last_name',value->>'username',value->>'language_code',COALESCE(value->'profile','{}'::jsonb),stamp,stamp)
    ON CONFLICT(user_id) DO UPDATE SET
        first_seen_at = least(existing.first_seen_at, stamp),
        last_seen_at = greatest(existing.last_seen_at, stamp),
        is_bot = CASE WHEN stamp >= existing.last_seen_at THEN EXCLUDED.is_bot ELSE existing.is_bot END,
        first_name = CASE WHEN stamp >= existing.last_seen_at THEN EXCLUDED.first_name ELSE existing.first_name END,
        last_name = CASE WHEN stamp >= existing.last_seen_at AND value ? 'last_name' THEN EXCLUDED.last_name ELSE existing.last_name END,
        username = CASE WHEN stamp >= existing.last_seen_at AND value ? 'username' THEN EXCLUDED.username ELSE existing.username END,
        language_code = CASE WHEN stamp >= existing.last_seen_at AND value ? 'language_code' THEN EXCLUDED.language_code ELSE existing.language_code END,
        profile = CASE WHEN stamp >= existing.last_seen_at THEN existing.profile || EXCLUDED.profile ELSE existing.profile END;
END;
$$;

CREATE FUNCTION hub_private.observe_chat(value jsonb) RETURNS hub_private.chats
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE stamp timestamptz := COALESCE((value->>'observed_at')::timestamptz, now()); result hub_private.chats;
BEGIN
    PERFORM hub_private.check_object(value, ARRAY['chat_id','type','title','username','first_name','last_name','observed_at','profile'], ARRAY['chat_id','type']);
    INSERT INTO hub_private.chats AS existing(chat_id,type,title,username,first_name,last_name,profile,first_seen_at,last_seen_at)
    VALUES ((value->>'chat_id')::bigint,value->>'type',value->>'title',value->>'username',value->>'first_name',value->>'last_name',COALESCE(value->'profile','{}'::jsonb),stamp,stamp)
    ON CONFLICT(chat_id) DO UPDATE SET
        first_seen_at = least(existing.first_seen_at, stamp),
        last_seen_at = greatest(existing.last_seen_at, stamp),
        type = CASE WHEN stamp >= existing.last_seen_at THEN EXCLUDED.type ELSE existing.type END,
        title = CASE WHEN stamp >= existing.last_seen_at AND value ? 'title' THEN EXCLUDED.title ELSE existing.title END,
        username = CASE WHEN stamp >= existing.last_seen_at AND value ? 'username' THEN EXCLUDED.username ELSE existing.username END,
        first_name = CASE WHEN stamp >= existing.last_seen_at AND value ? 'first_name' THEN EXCLUDED.first_name ELSE existing.first_name END,
        last_name = CASE WHEN stamp >= existing.last_seen_at AND value ? 'last_name' THEN EXCLUDED.last_name ELSE existing.last_name END,
        profile = CASE WHEN stamp >= existing.last_seen_at THEN existing.profile || EXCLUDED.profile ELSE existing.profile END
    RETURNING * INTO result;
    RETURN result;
END;
$$;

CREATE FUNCTION hub_api.health_v1() RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE bot bigint := hub_private.require_principal();
BEGIN
    RETURN jsonb_build_object('schema_version',(SELECT max(version) FROM hub_private.schema_migrations),'bot_id',bot);
END;
$$;

CREATE FUNCTION hub_api.ensure_chat_v1(p_chat jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM hub_private.require_principal();
    RETURN to_jsonb(hub_private.observe_chat(p_chat));
END;
$$;

CREATE FUNCTION hub_api.get_chat_v1(p_chat_id bigint) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM hub_private.require_principal();
    RETURN (SELECT to_jsonb(c) FROM hub_private.chats c WHERE chat_id = p_chat_id);
END;
$$;

CREATE FUNCTION hub_api.load_settings_v1(p_chat jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE observed hub_private.chats;
BEGIN
    PERFORM hub_private.require_principal();
    observed := hub_private.observe_chat(p_chat);
    INSERT INTO hub_private.chat_settings(chat_id,settings)
    VALUES (observed.chat_id, CASE WHEN jsonb_typeof(observed.metadata->'settings') = 'object' THEN observed.metadata->'settings' ELSE '{}'::jsonb END)
    ON CONFLICT(chat_id) DO NOTHING;
    RETURN (SELECT settings FROM hub_private.chat_settings WHERE chat_id = observed.chat_id);
END;
$$;

CREATE FUNCTION hub_api.patch_settings_v1(p_chat_id bigint, p_changes jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE result jsonb;
BEGIN
    PERFORM hub_private.require_principal();
    IF p_changes IS NULL OR jsonb_typeof(p_changes) <> 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid settings patch';
    END IF;
    INSERT INTO hub_private.chat_settings(chat_id,settings)
    SELECT chat_id, CASE WHEN jsonb_typeof(metadata->'settings') = 'object' THEN metadata->'settings' ELSE '{}'::jsonb END
    FROM hub_private.chats WHERE chat_id = p_chat_id ON CONFLICT(chat_id) DO NOTHING;
    UPDATE hub_private.chat_settings SET settings = settings || p_changes, updated_at = now()
    WHERE chat_id = p_chat_id RETURNING settings INTO result;
    IF result IS NULL THEN RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'Chat is not observed'; END IF;
    RETURN result;
END;
$$;

CREATE FUNCTION hub_api.statistics_v1(p_since timestamptz) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE bot bigint := hub_private.require_principal();
BEGIN
    RETURN jsonb_build_object(
        'users',(SELECT count(*) FROM hub_private.users),
        'chats',(SELECT count(*) FROM hub_private.chats),
        'updates',(SELECT count(*) FROM hub_private.updates WHERE bot_id = bot AND created > p_since),
        'handled_updates',(SELECT count(*) FROM hub_private.updates WHERE bot_id = bot AND created > p_since AND handled)
    );
END;
$$;

CREATE FUNCTION hub_api.list_directory_v1() RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM hub_private.require_principal();
    RETURN (SELECT COALESCE(jsonb_agg(to_jsonb(d) ORDER BY chat_id),'[]'::jsonb) FROM hub_private.directory d);
END;
$$;

CREATE FUNCTION hub_api.get_directory_v1(p_chat_id bigint) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM hub_private.require_principal();
    RETURN (SELECT to_jsonb(d) FROM hub_private.directory d WHERE chat_id = p_chat_id);
END;
$$;

CREATE FUNCTION hub_api.create_directory_v1(p_entry jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM hub_private.require_principal();
    PERFORM hub_private.check_object(p_entry, ARRAY['chat_id','name','section','is_hidden','username_alias','members','pinned_message_id'], ARRAY['chat_id','name']);
    IF p_entry->'section' = 'null'::jsonb OR p_entry->'is_hidden' = 'null'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Required directory fields cannot be cleared';
    END IF;
    INSERT INTO hub_private.directory(chat_id,name,section,is_hidden,username_alias,members,pinned_message_id)
    VALUES ((p_entry->>'chat_id')::bigint,p_entry->>'name',COALESCE(p_entry->>'section','other'),COALESCE((p_entry->>'is_hidden')::boolean,false),
        CASE WHEN p_entry ? 'username_alias' THEN p_entry->>'username_alias' ELSE '' END,(p_entry->>'members')::bigint,(p_entry->>'pinned_message_id')::bigint)
    ON CONFLICT(chat_id) DO NOTHING;
    RETURN (SELECT to_jsonb(d) FROM hub_private.directory d WHERE chat_id = (p_entry->>'chat_id')::bigint);
END;
$$;

CREATE FUNCTION hub_api.patch_directory_v1(p_chat_id bigint, p_changes jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE result hub_private.directory;
BEGIN
    PERFORM hub_private.require_principal();
    PERFORM hub_private.check_object(p_changes, ARRAY['name','section','is_hidden','username_alias','members','pinned_message_id']);
    UPDATE hub_private.directory SET
        name = CASE WHEN p_changes ? 'name' THEN p_changes->>'name' ELSE name END,
        section = CASE WHEN p_changes ? 'section' THEN p_changes->>'section' ELSE section END,
        is_hidden = CASE WHEN p_changes ? 'is_hidden' THEN (p_changes->>'is_hidden')::boolean ELSE is_hidden END,
        username_alias = CASE WHEN p_changes ? 'username_alias' THEN p_changes->>'username_alias' ELSE username_alias END,
        members = CASE WHEN p_changes ? 'members' THEN (p_changes->>'members')::bigint ELSE members END,
        pinned_message_id = CASE WHEN p_changes ? 'pinned_message_id' THEN (p_changes->>'pinned_message_id')::bigint ELSE pinned_message_id END
    WHERE chat_id = p_chat_id RETURNING * INTO result;
    RETURN CASE WHEN result.id IS NULL THEN NULL ELSE to_jsonb(result) END;
END;
$$;

CREATE FUNCTION hub_api.delete_directory_v1(p_chat_id bigint) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM hub_private.require_principal();
    DELETE FROM hub_private.directory WHERE chat_id = p_chat_id;
    RETURN FOUND;
END;
$$;

CREATE FUNCTION hub_api.list_vk_subscriptions_v1() RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM hub_private.require_principal();
    RETURN (SELECT COALESCE(jsonb_agg(to_jsonb(v) ORDER BY owner_id,chat_id),'[]'::jsonb) FROM hub_private.vk_subscriptions v);
END;
$$;

CREATE FUNCTION hub_api.upsert_vk_subscription_v1(p_owner_id bigint, p_chat_id bigint, p_changes jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE result hub_private.vk_subscriptions;
BEGIN
    PERFORM hub_private.require_principal();
    PERFORM hub_private.check_object(p_changes, ARRAY['last_post_id','with_reposts','with_header','is_suspended','description']);
    IF EXISTS (SELECT 1 FROM unnest(ARRAY['last_post_id','with_reposts','with_header','is_suspended']) AS k WHERE p_changes->k = 'null'::jsonb) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Required subscription fields cannot be cleared';
    END IF;
    INSERT INTO hub_private.vk_subscriptions AS existing(owner_id,chat_id,last_post_id,with_reposts,with_header,is_suspended,description)
    VALUES (p_owner_id,p_chat_id,COALESCE((p_changes->>'last_post_id')::bigint,0),COALESCE((p_changes->>'with_reposts')::boolean,false),
        COALESCE((p_changes->>'with_header')::boolean,true),COALESCE((p_changes->>'is_suspended')::boolean,false),p_changes->>'description')
    ON CONFLICT(owner_id,chat_id) DO UPDATE SET
        last_post_id = CASE WHEN p_changes ? 'last_post_id' THEN (p_changes->>'last_post_id')::bigint ELSE existing.last_post_id END,
        with_reposts = CASE WHEN p_changes ? 'with_reposts' THEN (p_changes->>'with_reposts')::boolean ELSE existing.with_reposts END,
        with_header = CASE WHEN p_changes ? 'with_header' THEN (p_changes->>'with_header')::boolean ELSE existing.with_header END,
        is_suspended = CASE WHEN p_changes ? 'is_suspended' THEN (p_changes->>'is_suspended')::boolean ELSE existing.is_suspended END,
        description = CASE WHEN p_changes ? 'description' THEN p_changes->>'description' ELSE existing.description END
    RETURNING * INTO result;
    RETURN to_jsonb(result);
END;
$$;

CREATE FUNCTION hub_api.advance_vk_cursor_v1(p_owner_id bigint, p_chat_id bigint, p_last_post_id bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM hub_private.require_principal();
    UPDATE hub_private.vk_subscriptions SET last_post_id = greatest(last_post_id,p_last_post_id)
    WHERE owner_id = p_owner_id AND chat_id = p_chat_id;
END;
$$;

CREATE FUNCTION hub_api.archive_update_v1(p_update jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    bot bigint := hub_private.require_principal();
    receipt uuid;
    item jsonb;
    stamp timestamptz;
    received timestamptz := COALESCE((p_update->>'received_at')::timestamptz,now());
BEGIN
    PERFORM hub_private.check_object(p_update, ARRAY['id','update_id','received_at','kind','handled','data','users','chats','memberships','topics','messages'], ARRAY['id','update_id','kind','handled','data']);
    INSERT INTO hub_private.updates(id,created,data,handled,bot_id,update_id,kind)
    VALUES ((p_update->>'id')::uuid,received,p_update->'data',(p_update->>'handled')::boolean,bot,(p_update->>'update_id')::bigint,p_update->>'kind')
    ON CONFLICT(bot_id,update_id) WHERE NOT is_legacy DO NOTHING RETURNING id INTO receipt;
    IF receipt IS NULL THEN RETURN; END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'users','[]'::jsonb)) LOOP
        PERFORM hub_private.observe_user(item);
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'chats','[]'::jsonb)) LOOP
        PERFORM hub_private.observe_chat(item);
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'memberships','[]'::jsonb)) LOOP
        PERFORM hub_private.check_object(item, ARRAY['chat_id','user_id','observed_at','status','permissions'], ARRAY['chat_id','user_id']);
        stamp := COALESCE((item->>'observed_at')::timestamptz,received);
        INSERT INTO hub_private.chat_users AS existing(chat_id,user_id,first_seen_at,last_seen_at,status,permissions)
        VALUES ((item->>'chat_id')::bigint,(item->>'user_id')::bigint,stamp,stamp,item->>'status',COALESCE(item->'permissions','{}'::jsonb))
        ON CONFLICT(chat_id,user_id) DO UPDATE SET
            first_seen_at = least(existing.first_seen_at,stamp), last_seen_at = greatest(existing.last_seen_at,stamp),
            status = CASE WHEN stamp >= existing.last_seen_at AND item ? 'status' THEN EXCLUDED.status ELSE existing.status END,
            permissions = CASE WHEN stamp >= existing.last_seen_at AND item ? 'permissions' THEN EXCLUDED.permissions ELSE existing.permissions END;
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'topics','[]'::jsonb)) LOOP
        PERFORM hub_private.check_object(item, ARRAY['chat_id','thread_id','observed_at','title','is_closed','profile'], ARRAY['chat_id','thread_id']);
        stamp := COALESCE((item->>'observed_at')::timestamptz,received);
        INSERT INTO hub_private.chat_topics AS existing(chat_id,thread_id,first_seen_at,last_seen_at,title,is_closed,profile)
        VALUES ((item->>'chat_id')::bigint,(item->>'thread_id')::bigint,stamp,stamp,item->>'title',(item->>'is_closed')::boolean,COALESCE(item->'profile','{}'::jsonb))
        ON CONFLICT(chat_id,thread_id) DO UPDATE SET
            first_seen_at = least(existing.first_seen_at,stamp), last_seen_at = greatest(existing.last_seen_at,stamp),
            title = CASE WHEN stamp >= existing.last_seen_at AND item ? 'title' THEN EXCLUDED.title ELSE existing.title END,
            is_closed = CASE WHEN stamp >= existing.last_seen_at AND item ? 'is_closed' THEN EXCLUDED.is_closed ELSE existing.is_closed END,
            profile = CASE WHEN stamp >= existing.last_seen_at THEN existing.profile || EXCLUDED.profile ELSE existing.profile END;
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'messages','[]'::jsonb)) LOOP
        PERFORM hub_private.check_object(item, ARRAY['chat_id','message_id','sent_at','edited_at','observed_at','business_connection_id','sender_user_id','sender_chat_id','thread_id','reply_to_message_id','data'], ARRAY['chat_id','message_id','sent_at','data']);
        IF (item->>'sent_at')::timestamptz <= now() - interval '30 days' THEN CONTINUE; END IF;
        INSERT INTO hub_private.messages AS existing(bot_id,chat_id,message_id,business_connection_id,sent_at,edited_at,observed_at,sender_user_id,sender_chat_id,thread_id,reply_to_message_id,source_update_id,data)
        VALUES (bot,(item->>'chat_id')::bigint,(item->>'message_id')::bigint,COALESCE(item->>'business_connection_id',''),(item->>'sent_at')::timestamptz,
            (item->>'edited_at')::timestamptz,COALESCE((item->>'observed_at')::timestamptz,received),(item->>'sender_user_id')::bigint,(item->>'sender_chat_id')::bigint,
            (item->>'thread_id')::bigint,(item->>'reply_to_message_id')::bigint,receipt,item->'data')
        ON CONFLICT(bot_id,chat_id,message_id,business_connection_id) DO UPDATE SET
            sent_at = least(existing.sent_at,EXCLUDED.sent_at), edited_at = EXCLUDED.edited_at, observed_at = EXCLUDED.observed_at,
            sender_user_id = EXCLUDED.sender_user_id, sender_chat_id = EXCLUDED.sender_chat_id, thread_id = EXCLUDED.thread_id,
            reply_to_message_id = EXCLUDED.reply_to_message_id, source_update_id = EXCLUDED.source_update_id, data = EXCLUDED.data
        WHERE (COALESCE(EXCLUDED.edited_at,EXCLUDED.sent_at), EXCLUDED.observed_at)
            >= (COALESCE(existing.edited_at,existing.sent_at), existing.observed_at);
    END LOOP;
END;
$$;

-- Administrative maintenance only. No entities, preferences or journal rows expire here.
CREATE FUNCTION hub_private.retain_messages(p_batch integer DEFAULT 1000, p_now timestamptz DEFAULT now()) RETURNS jsonb
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE message_count integer; update_count integer; cutoff timestamptz := p_now - interval '30 days';
BEGIN
    IF p_batch IS NULL OR p_batch < 1 OR p_batch > 10000 OR p_now IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid retention batch';
    END IF;
    WITH expired AS (SELECT ctid FROM hub_private.messages WHERE sent_at <= cutoff ORDER BY sent_at LIMIT p_batch FOR UPDATE SKIP LOCKED)
    DELETE FROM hub_private.messages WHERE ctid IN (SELECT ctid FROM expired);
    GET DIAGNOSTICS message_count = ROW_COUNT;
    WITH expired AS (SELECT id FROM hub_private.updates WHERE created <= cutoff ORDER BY created LIMIT p_batch FOR UPDATE SKIP LOCKED)
    DELETE FROM hub_private.updates WHERE id IN (SELECT id FROM expired);
    GET DIAGNOSTICS update_count = ROW_COUNT;
    RETURN jsonb_build_object('messages',message_count,'updates',update_count,'cutoff',cutoff);
END;
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA hub_private FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA hub_private FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA hub_private, hub_api FROM PUBLIC, anon, authenticated;
-- Definer execution has application ownership, not a platform administrator's privileges.
ALTER SCHEMA hub_private OWNER TO hub_owner;
ALTER SCHEMA hub_api OWNER TO hub_owner;
DO $$
DECLARE item record;
BEGIN
    FOR item IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'hub_private' AND c.relkind = 'r'
    LOOP
        EXECUTE format('ALTER TABLE hub_private.%I OWNER TO hub_owner', item.relname);
    END LOOP;
    FOR item IN SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid) AS arguments
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname IN ('hub_private','hub_api')
    LOOP
        EXECUTE format('ALTER FUNCTION %I.%I(%s) OWNER TO hub_owner', item.nspname, item.proname, item.arguments);
    END LOOP;
END;
$$;
GRANT USAGE ON SCHEMA auth TO hub_owner;
GRANT EXECUTE ON FUNCTION auth.uid() TO hub_owner;
ALTER DEFAULT PRIVILEGES FOR ROLE hub_owner IN SCHEMA hub_private, hub_api REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hub_api.health_v1(), hub_api.ensure_chat_v1(jsonb), hub_api.get_chat_v1(bigint),
    hub_api.load_settings_v1(jsonb), hub_api.patch_settings_v1(bigint,jsonb), hub_api.statistics_v1(timestamptz),
    hub_api.list_directory_v1(), hub_api.get_directory_v1(bigint), hub_api.create_directory_v1(jsonb),
    hub_api.patch_directory_v1(bigint,jsonb), hub_api.delete_directory_v1(bigint), hub_api.list_vk_subscriptions_v1(),
    hub_api.upsert_vk_subscription_v1(bigint,bigint,jsonb), hub_api.advance_vk_cursor_v1(bigint,bigint,bigint),
    hub_api.archive_update_v1(jsonb) TO authenticated;
NOTIFY pgrst, 'reload schema';
COMMIT;
