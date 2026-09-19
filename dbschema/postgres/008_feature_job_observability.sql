-- Read-only queue summaries contain no document bodies or conversation identities.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
DO $$ BEGIN
    IF (SELECT max(version) FROM msu_hub_private.schema_migrations) IS DISTINCT FROM 7 THEN
        RAISE EXCEPTION 'Feature job observability requires schema revision 7';
    END IF;
END $$;

CREATE FUNCTION msu_hub_api.feature_job_overview_v1(p_request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE bot bigint := msu_hub_private.require_principal(); item jsonb; result jsonb;
BEGIN
    PERFORM msu_hub_private.check_object(p_request,ARRAY['handlers'],ARRAY['handlers']);
    IF octet_length(p_request::text) > 16384 OR jsonb_typeof(p_request->'handlers') <> 'array'
       OR jsonb_array_length(p_request->'handlers') NOT BETWEEN 1 AND 64 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature job overview';
    END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(p_request->'handlers') LOOP
        PERFORM msu_hub_private.check_object(item,ARRAY['feature','kind'],ARRAY['feature','kind']);
        PERFORM msu_hub_private.feature_text(item->'feature',64,true);
        PERFORM msu_hub_private.feature_text(item->'kind',64,true);
    END LOOP;
    WITH handlers AS (
        SELECT DISTINCT h.value->>'feature' AS feature,h.value->>'kind' AS kind
        FROM jsonb_array_elements(p_request->'handlers') h(value)
    ), counts AS (
        SELECT h.feature,h.kind,
            count(j.key) FILTER (WHERE j.state='pending') AS pending,
            count(j.key) FILTER (WHERE j.state='held') AS held,
            count(j.key) FILTER (WHERE j.state='pending' AND j.lease_until > now()) AS leased,
            count(j.key) FILTER (WHERE j.state='pending' AND j.run_at <= now()
                AND (j.lease_until IS NULL OR j.lease_until <= now())) AS overdue,
            COALESCE(max(GREATEST(0,extract(epoch FROM now()-j.run_at))) FILTER
                (WHERE j.state='pending' AND j.run_at <= now()
                AND (j.lease_until IS NULL OR j.lease_until <= now())),0) AS oldest_due_seconds
        FROM handlers h LEFT JOIN msu_hub_private.feature_jobs j ON j.feature=h.feature AND j.kind=h.kind
            AND j.owner_id IN (0,bot) AND j.terminal_at IS NULL
        GROUP BY h.feature,h.kind
    ) SELECT COALESCE(jsonb_agg(to_jsonb(counts) ORDER BY feature,kind),'[]'::jsonb) INTO result FROM counts;
    RETURN result;
END $$;
ALTER FUNCTION msu_hub_api.feature_job_overview_v1(jsonb) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_api.feature_job_overview_v1(jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION msu_hub_api.feature_job_overview_v1(jsonb) TO authenticated;

INSERT INTO msu_hub_private.schema_migrations(version) VALUES(8);
NOTIFY pgrst, 'reload schema';
COMMIT;
