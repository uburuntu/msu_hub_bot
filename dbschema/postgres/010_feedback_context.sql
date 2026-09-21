-- Feedback reads only bounded message excerpts from the existing expiring archive.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
DO $$ BEGIN
    IF (SELECT max(version) FROM msu_hub_private.schema_migrations) IS DISTINCT FROM 9 THEN
        RAISE EXCEPTION 'Feedback context requires schema revision 9';
    END IF;
END $$;

CREATE FUNCTION msu_hub_api.recent_feedback_messages_v1(
    p_chat_id bigint, p_thread_id bigint, p_before timestamptz, p_before_message_id bigint
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE bot bigint := msu_hub_private.require_principal(); result jsonb;
BEGIN
    IF p_chat_id IS NULL OR p_chat_id = 0 OR p_thread_id <= 0
       OR p_before IS NULL OR NOT isfinite(p_before)
       OR p_before_message_id IS NULL OR p_before_message_id <= 0 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feedback context scope';
    END IF;
    WITH recent AS (
        SELECT m.*,
            CASE WHEN m.data->'is_topic_message' = 'true'::jsonb THEN m.thread_id END AS normalized_thread,
            CASE WHEN jsonb_typeof(m.data->'text') = 'string' THEN m.data->>'text'
                WHEN jsonb_typeof(m.data->'caption') = 'string' THEN m.data->>'caption' ELSE '' END AS excerpt
        FROM msu_hub_private.messages m
        WHERE m.bot_id = bot AND m.chat_id = p_chat_id AND m.business_connection_id = ''
            AND COALESCE(m.data->>'business_connection_id','') = ''
            AND (m.data->'direct_messages_topic') IS NOT DISTINCT FROM NULL
            AND (CASE WHEN m.data->'is_topic_message' = 'true'::jsonb THEN m.thread_id END)
                IS NOT DISTINCT FROM p_thread_id
            AND m.message_id < p_before_message_id
            AND m.sent_at > now() - interval '30 days'
            AND m.sent_at <= LEAST(p_before,now()) AND m.observed_at <= p_before
        ORDER BY m.sent_at DESC,m.message_id DESC LIMIT 5
    ), projection AS (
        SELECT r.sent_at,r.message_id,jsonb_build_object(
            'chat_id',r.chat_id,'message_id',r.message_id,'sent_at',r.sent_at,'thread_id',r.normalized_thread,
            'author_id',COALESCE(r.sender_chat_id,r.sender_user_id),
            'author_kind',CASE WHEN r.sender_chat_id IS NOT NULL THEN 'chat'
                WHEN r.sender_user_id IS NOT NULL THEN 'user' ELSE 'unknown' END,
            'author_name',left(COALESCE(NULLIF(CASE WHEN r.sender_chat_id IS NOT NULL
                THEN CASE WHEN jsonb_typeof(r.data#>'{sender_chat,title}') = 'string' THEN r.data#>>'{sender_chat,title}' END
                ELSE concat_ws(' ',
                    CASE WHEN jsonb_typeof(r.data#>'{from,first_name}') = 'string' THEN r.data#>>'{from,first_name}' END,
                    CASE WHEN jsonb_typeof(r.data#>'{from,last_name}') = 'string' THEN r.data#>>'{from,last_name}' END)
                END,''),'Unknown'),128),
            'text',left(r.excerpt,800),'truncated',length(r.excerpt)>800,
            'media_kind',(SELECT kind FROM unnest(ARRAY['rich_message','animation','photo','video','audio','voice','video_note',
                'sticker','document','contact','venue','location','poll','dice']) WITH ORDINALITY AS media(kind,ordinal)
                WHERE r.data->kind IS NOT NULL AND r.data->kind <> 'null'::jsonb ORDER BY ordinal LIMIT 1)
        ) AS item FROM recent r
    ) SELECT COALESCE(jsonb_agg(item ORDER BY sent_at,message_id),'[]'::jsonb) INTO result FROM projection;
    RETURN result;
END $$;
ALTER FUNCTION msu_hub_api.recent_feedback_messages_v1(bigint,bigint,timestamptz,bigint) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_api.recent_feedback_messages_v1(bigint,bigint,timestamptz,bigint) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION msu_hub_api.recent_feedback_messages_v1(bigint,bigint,timestamptz,bigint) TO authenticated;

INSERT INTO msu_hub_private.schema_migrations(version) VALUES(10);
NOTIFY pgrst, 'reload schema';
COMMIT;
