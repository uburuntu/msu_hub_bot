-- Administrative imports and authenticated receipts share observation merge semantics.
BEGIN;
DO $$ BEGIN
    IF (SELECT max(version) FROM hub_private.schema_migrations) IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION 'Archive observation migration requires schema revision 1';
    END IF;
END $$;

CREATE FUNCTION hub_private.observe_archive(
    p_update jsonb, p_bot bigint, p_receipt uuid, p_received timestamptz,
    p_retention_at timestamptz DEFAULT now()
) RETURNS void
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE item jsonb; stamp timestamptz;
BEGIN
    IF p_retention_at IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Retention timestamp is required';
    END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'users','[]'::jsonb)) LOOP
        PERFORM hub_private.observe_user(item);
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'chats','[]'::jsonb)) LOOP
        PERFORM hub_private.observe_chat(item);
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'memberships','[]'::jsonb)) LOOP
        PERFORM hub_private.check_object(item, ARRAY['chat_id','user_id','observed_at','status','permissions'], ARRAY['chat_id','user_id']);
        stamp := COALESCE((item->>'observed_at')::timestamptz,p_received);
        INSERT INTO hub_private.chat_users AS existing(chat_id,user_id,first_seen_at,last_seen_at,status,permissions)
        VALUES ((item->>'chat_id')::bigint,(item->>'user_id')::bigint,stamp,stamp,item->>'status',COALESCE(item->'permissions','{}'::jsonb))
        ON CONFLICT(chat_id,user_id) DO UPDATE SET
            first_seen_at = least(existing.first_seen_at,stamp), last_seen_at = greatest(existing.last_seen_at,stamp),
            status = CASE WHEN stamp >= existing.last_seen_at AND item ? 'status' THEN EXCLUDED.status ELSE existing.status END,
            permissions = CASE WHEN stamp >= existing.last_seen_at AND item ? 'permissions' THEN EXCLUDED.permissions ELSE existing.permissions END;
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'topics','[]'::jsonb)) LOOP
        PERFORM hub_private.check_object(item, ARRAY['chat_id','thread_id','observed_at','title','is_closed','profile'], ARRAY['chat_id','thread_id']);
        stamp := COALESCE((item->>'observed_at')::timestamptz,p_received);
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
        IF (item->>'sent_at')::timestamptz <= p_retention_at - interval '30 days' THEN CONTINUE; END IF;
        INSERT INTO hub_private.messages AS existing(bot_id,chat_id,message_id,business_connection_id,sent_at,edited_at,observed_at,sender_user_id,sender_chat_id,thread_id,reply_to_message_id,source_update_id,data)
        VALUES (p_bot,(item->>'chat_id')::bigint,(item->>'message_id')::bigint,COALESCE(item->>'business_connection_id',''),(item->>'sent_at')::timestamptz,
            (item->>'edited_at')::timestamptz,COALESCE((item->>'observed_at')::timestamptz,p_received),(item->>'sender_user_id')::bigint,(item->>'sender_chat_id')::bigint,
            (item->>'thread_id')::bigint,(item->>'reply_to_message_id')::bigint,p_receipt,item->'data')
        ON CONFLICT(bot_id,chat_id,message_id,business_connection_id) DO UPDATE SET
            sent_at = least(existing.sent_at,EXCLUDED.sent_at), edited_at = EXCLUDED.edited_at, observed_at = EXCLUDED.observed_at,
            sender_user_id = EXCLUDED.sender_user_id, sender_chat_id = EXCLUDED.sender_chat_id, thread_id = EXCLUDED.thread_id,
            reply_to_message_id = EXCLUDED.reply_to_message_id, source_update_id = EXCLUDED.source_update_id, data = EXCLUDED.data
        WHERE (COALESCE(EXCLUDED.edited_at,EXCLUDED.sent_at), EXCLUDED.observed_at)
            >= (COALESCE(existing.edited_at,existing.sent_at), existing.observed_at);
    END LOOP;
END;
$$;
ALTER FUNCTION hub_private.observe_archive(jsonb,bigint,uuid,timestamptz,timestamptz) OWNER TO hub_owner;
REVOKE ALL ON FUNCTION hub_private.observe_archive(jsonb,bigint,uuid,timestamptz,timestamptz) FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION hub_api.archive_update_v1(p_update jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    bot bigint := hub_private.require_principal();
    receipt uuid;
    received timestamptz := COALESCE((p_update->>'received_at')::timestamptz,now());
BEGIN
    PERFORM hub_private.check_object(p_update, ARRAY['id','update_id','received_at','kind','handled','data','users','chats','memberships','topics','messages'], ARRAY['id','update_id','kind','handled','data']);
    INSERT INTO hub_private.updates(id,created,data,handled,bot_id,update_id,kind)
    VALUES ((p_update->>'id')::uuid,received,p_update->'data',(p_update->>'handled')::boolean,bot,(p_update->>'update_id')::bigint,p_update->>'kind')
    ON CONFLICT(bot_id,update_id) WHERE NOT is_legacy DO NOTHING RETURNING id INTO receipt;
    IF receipt IS NULL THEN RETURN; END IF;
    PERFORM hub_private.observe_archive(p_update,bot,receipt,received);
END;
$$;

-- API compatibility is independent of administrative SQL migration numbering.
CREATE OR REPLACE FUNCTION hub_api.health_v1() RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE bot bigint := hub_private.require_principal();
BEGIN
    RETURN jsonb_build_object('schema_version',1,'bot_id',bot);
END;
$$;
INSERT INTO hub_private.schema_migrations(version) VALUES (2);
NOTIFY pgrst, 'reload schema';
COMMIT;
