-- Bounded typed documents, atomic decisions and durable feature deadlines.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
DO $$ BEGIN
    IF (SELECT max(version) FROM msu_hub_private.schema_migrations) IS DISTINCT FROM 4 THEN
        RAISE EXCEPTION 'Feature storage requires schema revision 4';
    END IF;
END $$;

CREATE TABLE msu_hub_private.feature_records (
    owner_id bigint NOT NULL,
    feature text NOT NULL,
    scope_key text NOT NULL,
    collection text NOT NULL,
    key text COLLATE "C" NOT NULL,
    etag uuid NOT NULL DEFAULT gen_random_uuid(),
    payload_version integer NOT NULL CHECK (payload_version > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 65536),
    parent text,
    status text,
    expires_at timestamptz CHECK (isfinite(expires_at)),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id,feature,scope_key,collection,key)
);
CREATE INDEX feature_records_parent ON msu_hub_private.feature_records(owner_id,feature,scope_key,collection,parent,key);
CREATE INDEX feature_records_status ON msu_hub_private.feature_records(owner_id,feature,scope_key,collection,status,key);
CREATE INDEX feature_records_expiry ON msu_hub_private.feature_records(expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE msu_hub_private.feature_jobs (
    owner_id bigint NOT NULL,
    feature text NOT NULL,
    scope_key text NOT NULL,
    key text COLLATE "C" NOT NULL,
    kind text NOT NULL,
    record_collection text NOT NULL,
    record_key text NOT NULL,
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    serial_key text,
    sequence bigint GENERATED ALWAYS AS IDENTITY,
    run_at timestamptz NOT NULL CHECK (isfinite(run_at)),
    retry_until timestamptz CHECK (isfinite(retry_until)),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','held','complete','expired','cancelled')),
    lease_token uuid,
    leased_generation bigint,
    lease_until timestamptz CHECK (isfinite(lease_until)),
    terminal_at timestamptz CHECK (isfinite(terminal_at)),
    PRIMARY KEY (owner_id,feature,scope_key,key),
    CHECK ((lease_token IS NULL) = (leased_generation IS NULL) AND (lease_token IS NULL) = (lease_until IS NULL)),
    CHECK ((terminal_at IS NULL) = (state IN ('pending','held')))
);
CREATE INDEX feature_jobs_due ON msu_hub_private.feature_jobs(run_at,sequence) WHERE state = 'pending';
CREATE INDEX feature_jobs_serial ON msu_hub_private.feature_jobs(owner_id,feature,scope_key,serial_key,sequence)
    WHERE terminal_at IS NULL;
CREATE INDEX feature_jobs_records ON msu_hub_private.feature_jobs(owner_id,feature,scope_key,record_collection,record_key)
    WHERE terminal_at IS NULL;
CREATE INDEX feature_jobs_expiry ON msu_hub_private.feature_jobs(terminal_at) WHERE terminal_at IS NOT NULL;

CREATE TABLE msu_hub_private.feature_operations (
    owner_id bigint NOT NULL,
    feature text NOT NULL,
    scope_key text NOT NULL,
    operation_id text NOT NULL,
    request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
    result jsonb NOT NULL,
    expires_at timestamptz NOT NULL CHECK (isfinite(expires_at)),
    PRIMARY KEY (owner_id,feature,scope_key,operation_id)
);
CREATE INDEX feature_operations_expiry ON msu_hub_private.feature_operations(expires_at);

DO $$ DECLARE relation text; BEGIN
    FOREACH relation IN ARRAY ARRAY['feature_records','feature_jobs','feature_operations'] LOOP
        EXECUTE format('ALTER TABLE msu_hub_private.%I OWNER TO msu_hub_owner',relation);
        EXECUTE format('ALTER TABLE msu_hub_private.%I ENABLE ROW LEVEL SECURITY',relation);
        EXECUTE format('REVOKE ALL ON msu_hub_private.%I FROM PUBLIC,anon,authenticated',relation);
    END LOOP;
END $$;
REVOKE ALL ON SEQUENCE msu_hub_private.feature_jobs_sequence_seq FROM PUBLIC,anon,authenticated;

CREATE FUNCTION msu_hub_private.feature_text(p_value jsonb,p_limit integer,p_identifier boolean DEFAULT false,p_nullable boolean DEFAULT false)
RETURNS text LANGUAGE plpgsql IMMUTABLE SET search_path = '' AS $$
DECLARE value text := p_value #>> '{}';
BEGIN
    IF p_nullable AND (p_value IS NULL OR p_value = 'null'::jsonb) THEN RETURN NULL; END IF;
    IF jsonb_typeof(p_value) IS DISTINCT FROM 'string' OR length(value) NOT BETWEEN 1 AND p_limit
       OR value ~ '[[:cntrl:]]' OR (p_identifier AND value !~ '^[a-z][a-z0-9_]*$') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature identifier';
    END IF;
    RETURN value;
END $$;

CREATE FUNCTION msu_hub_private.feature_time(p_value jsonb,p_nullable boolean DEFAULT false)
RETURNS timestamptz LANGUAGE plpgsql IMMUTABLE SET search_path = '' AS $$
DECLARE value timestamptz;
BEGIN
    IF p_nullable AND (p_value IS NULL OR p_value = 'null'::jsonb) THEN RETURN NULL; END IF;
    IF jsonb_typeof(p_value) IS DISTINCT FROM 'string'
       OR (p_value #>> '{}') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature timestamp';
    END IF;
    value := (p_value #>> '{}')::timestamptz;
    IF NOT isfinite(value) OR value < '0001-01-01 UTC'::timestamptz OR value >= '10000-01-01 UTC'::timestamptz THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature timestamp';
    END IF;
    RETURN value;
END $$;

CREATE FUNCTION msu_hub_private.feature_scope(p_request jsonb,p_bot bigint) RETURNS bigint
LANGUAGE plpgsql IMMUTABLE SET search_path = '' AS $$
BEGIN
    IF octet_length(p_request::text) > 262144 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Feature request is too large';
    END IF;
    PERFORM msu_hub_private.feature_text(p_request->'feature',64,true);
    PERFORM msu_hub_private.check_object(p_request->'scope',ARRAY['key','owner'],ARRAY['key','owner']);
    PERFORM msu_hub_private.feature_text(p_request->'scope'->'key',256);
    IF jsonb_typeof(p_request->'scope'->'owner') IS DISTINCT FROM 'string'
       OR p_request->'scope'->>'owner' NOT IN ('bot','application') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature scope';
    END IF;
    IF p_request->'scope'->>'owner' = 'application' THEN RETURN 0; END IF;
    IF p_bot = 0 THEN RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Invalid bot principal'; END IF;
    RETURN p_bot;
END $$;

CREATE FUNCTION msu_hub_private.feature_lock(p_owner bigint,p_feature text,p_scope text) RETURNS bigint
LANGUAGE sql IMMUTABLE SET search_path = '' AS $$
    SELECT hashtextextended(jsonb_build_array('msu_hub_features',p_owner,p_feature,p_scope)::text,0);
$$;

CREATE FUNCTION msu_hub_private.feature_record_json(p_record msu_hub_private.feature_records) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path = '' AS $$
    SELECT (to_jsonb(p_record)-'owner_id'-'scope_key') || jsonb_build_object('scope',jsonb_build_object(
        'key',p_record.scope_key,'owner',CASE WHEN p_record.owner_id = 0 THEN 'application' ELSE 'bot' END));
$$;

CREATE FUNCTION msu_hub_private.feature_job_json(p_job msu_hub_private.feature_jobs) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path = '' AS $$
    SELECT jsonb_build_object('feature',p_job.feature,'scope',jsonb_build_object(
        'key',p_job.scope_key,'owner',CASE WHEN p_job.owner_id = 0 THEN 'application' ELSE 'bot' END),
        'key',p_job.key,'kind',p_job.kind,'record',jsonb_build_object('collection',p_job.record_collection,'key',p_job.record_key),
        'generation',p_job.generation,'lease_token',p_job.lease_token,'run_at',p_job.run_at,
        'attempts',p_job.attempts,'retry_until',p_job.retry_until);
$$;

CREATE FUNCTION msu_hub_api.feature_health_v1(p_request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM msu_hub_private.require_principal();
    PERFORM msu_hub_private.check_object(p_request,ARRAY[]::text[]);
    RETURN '{"version":1}'::jsonb;
END $$;

CREATE FUNCTION msu_hub_api.feature_get_v1(p_request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE bot bigint := msu_hub_private.require_principal(); owner_key bigint; result jsonb;
BEGIN
    PERFORM msu_hub_private.check_object(p_request,ARRAY['feature','scope','collection','key'],ARRAY['feature','scope','collection','key']);
    owner_key := msu_hub_private.feature_scope(p_request,bot);
    PERFORM msu_hub_private.feature_text(p_request->'collection',64,true);
    PERFORM msu_hub_private.feature_text(p_request->'key',256);
    SELECT msu_hub_private.feature_record_json(r) INTO result FROM msu_hub_private.feature_records r
    WHERE owner_id = owner_key AND feature = p_request->>'feature' AND scope_key = p_request->'scope'->>'key'
      AND collection = p_request->>'collection' AND key = p_request->>'key' AND (expires_at IS NULL OR expires_at > now());
    RETURN result;
END $$;

CREATE FUNCTION msu_hub_api.feature_list_v1(p_request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE bot bigint := msu_hub_private.require_principal(); owner_key bigint; page_size integer; result jsonb;
BEGIN
    PERFORM msu_hub_private.check_object(p_request,ARRAY['feature','scope','collection','parent','status','after','limit'],
        ARRAY['feature','scope','collection','limit']);
    owner_key := msu_hub_private.feature_scope(p_request,bot);
    PERFORM msu_hub_private.feature_text(p_request->'collection',64,true);
    PERFORM msu_hub_private.feature_text(p_request->'parent',256,false,true);
    PERFORM msu_hub_private.feature_text(p_request->'status',64,false,true);
    PERFORM msu_hub_private.feature_text(p_request->'after',256,false,true);
    IF jsonb_typeof(p_request->'limit') <> 'number' OR (p_request->>'limit') !~ '^[0-9]+$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature page size';
    END IF;
    page_size := (p_request->>'limit')::integer;
    IF page_size NOT BETWEEN 1 AND 200 THEN RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature page size'; END IF;
    SELECT COALESCE(jsonb_agg(msu_hub_private.feature_record_json(r) ORDER BY r.key),'[]'::jsonb) INTO result FROM (
        SELECT * FROM msu_hub_private.feature_records WHERE owner_id = owner_key AND feature = p_request->>'feature'
          AND scope_key = p_request->'scope'->>'key' AND collection = p_request->>'collection'
          AND (expires_at IS NULL OR expires_at > now()) AND (p_request->>'parent' IS NULL OR parent = p_request->>'parent')
          AND (p_request->>'status' IS NULL OR status = p_request->>'status') AND (p_request->>'after' IS NULL OR key > p_request->>'after')
        ORDER BY key LIMIT page_size
    ) r;
    RETURN result;
END $$;

CREATE FUNCTION msu_hub_api.feature_commit_v1(p_request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    bot bigint := msu_hub_private.require_principal(); owner_key bigint; feature_key text; scope text; stamp timestamptz;
    item jsonb; entry jsonb; value msu_hub_private.feature_records; job msu_hub_private.feature_jobs;
    receipt msu_hub_private.feature_operations; digest bytea; result jsonb; saved_result jsonb; records jsonb := '[]'::jsonb;
    conflict boolean := false; expected uuid;
BEGIN
    PERFORM msu_hub_private.check_object(p_request,
        ARRAY['feature','scope','operation_id','guards','puts','deletes','jobs','cancel_jobs'],
        ARRAY['feature','scope','operation_id','guards','puts','deletes','jobs','cancel_jobs']);
    owner_key := msu_hub_private.feature_scope(p_request,bot);
    feature_key := p_request->>'feature'; scope := p_request->'scope'->>'key';
    PERFORM msu_hub_private.feature_text(p_request->'operation_id',128);
    FOREACH entry IN ARRAY ARRAY[p_request->'guards',p_request->'puts',p_request->'deletes',p_request->'jobs',p_request->'cancel_jobs'] LOOP
        IF jsonb_typeof(entry) <> 'array' OR jsonb_array_length(entry) > 64 THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature transaction size';
        END IF;
    END LOOP;
    IF jsonb_array_length(p_request->'puts') + jsonb_array_length(p_request->'deletes')
       + jsonb_array_length(p_request->'jobs') + jsonb_array_length(p_request->'cancel_jobs') > 64 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature transaction size';
    END IF;
    PERFORM pg_advisory_xact_lock(msu_hub_private.feature_lock(owner_key,feature_key,scope));
    stamp := clock_timestamp(); digest := sha256(convert_to(p_request::text,'UTF8'));
    SELECT * INTO receipt FROM msu_hub_private.feature_operations
    WHERE owner_id = owner_key AND feature = feature_key AND scope_key = scope AND operation_id = p_request->>'operation_id';
    IF FOUND AND receipt.expires_at > stamp THEN
        IF receipt.request_hash <> digest THEN RETURN '{"outcome":"operation_mismatch","records":[]}'::jsonb; END IF;
        IF receipt.result->>'outcome' <> 'committed' THEN RETURN receipt.result; END IF;
        -- The verified retry supplies its original bodies; receipts retain only envelopes.
        SELECT COALESCE(jsonb_agg(r.value || jsonb_build_object('payload',p.value->'payload') ORDER BY r.ordinality),'[]'::jsonb)
            INTO records FROM jsonb_array_elements(receipt.result->'records') WITH ORDINALITY r(value,ordinality)
            JOIN jsonb_array_elements(p_request->'puts') p(value)
              ON p.value->>'collection' = r.value->>'collection' AND p.value->>'key' = r.value->>'key';
        RETURN jsonb_build_object('outcome','replayed','records',records);
    END IF;
    IF EXISTS(SELECT FROM jsonb_array_elements(p_request->'guards') x GROUP BY x->>'collection',x->>'key' HAVING count(*) > 1)
       OR EXISTS(SELECT FROM jsonb_array_elements((p_request->'puts') || (p_request->'deletes')) x
                 GROUP BY x->>'collection',x->>'key' HAVING count(*) > 1)
       OR EXISTS(SELECT FROM (SELECT x->>'key' AS key FROM jsonb_array_elements(p_request->'jobs') x UNION ALL
                 SELECT x #>> '{}' FROM jsonb_array_elements(p_request->'cancel_jobs') x) q GROUP BY key HAVING count(*) > 1) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Duplicate feature transaction key';
    END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(p_request->'guards') LOOP
        PERFORM msu_hub_private.check_object(item,ARRAY['collection','key','etag'],ARRAY['collection','key']);
        PERFORM msu_hub_private.feature_text(item->'collection',64,true);
        PERFORM msu_hub_private.feature_text(item->'key',256);
        IF NOT item ? 'etag' OR (item->'etag' <> 'null'::jsonb AND jsonb_typeof(item->'etag') <> 'string') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature guard';
        END IF;
        expected := (item->>'etag')::uuid;
        SELECT * INTO value FROM msu_hub_private.feature_records WHERE owner_id = owner_key AND feature = feature_key
            AND scope_key = scope AND collection = item->>'collection' AND key = item->>'key'
            AND (expires_at IS NULL OR expires_at > stamp);
        IF value.etag IS DISTINCT FROM expected THEN conflict := true; END IF;
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements((p_request->'puts') || (p_request->'deletes')) LOOP
        PERFORM msu_hub_private.feature_text(item->'collection',64,true);
        PERFORM msu_hub_private.feature_text(item->'key',256);
        IF NOT EXISTS(SELECT FROM jsonb_array_elements(p_request->'guards') g
            WHERE g->>'collection' = item->>'collection' AND g->>'key' = item->>'key') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Feature mutation requires a guard';
        END IF;
    END LOOP;
    -- Invalid mutations fail even when a guard is stale; no partial writes survive.
    FOR item IN SELECT * FROM jsonb_array_elements(p_request->'puts') LOOP
        PERFORM msu_hub_private.check_object(item,ARRAY['collection','key','payload','payload_version','parent','status','expires_at'],
            ARRAY['collection','key','payload','payload_version']);
        PERFORM msu_hub_private.feature_text(item->'parent',256,false,true);
        PERFORM msu_hub_private.feature_text(item->'status',64,false,true);
        PERFORM msu_hub_private.feature_time(item->'expires_at',true);
        IF NOT item ? 'expires_at' OR jsonb_typeof(item->'payload') <> 'object' OR octet_length((item->'payload')::text) > 65536
           OR jsonb_typeof(item->'payload_version') <> 'number' OR (item->>'payload_version') !~ '^[0-9]+$'
           OR (item->>'payload_version')::integer < 1 THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature payload';
        END IF;
        IF EXISTS(SELECT FROM msu_hub_private.feature_records WHERE owner_id = owner_key AND feature = feature_key AND scope_key = scope
            AND collection = item->>'collection' AND key = item->>'key' AND (expires_at IS NULL OR expires_at > stamp)
            AND payload_version > (item->>'payload_version')::integer) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Feature payload version cannot decrease';
        END IF;
        IF EXISTS(SELECT FROM msu_hub_private.feature_records r JOIN msu_hub_private.feature_jobs j
            ON j.owner_id = r.owner_id AND j.feature = r.feature AND j.scope_key = r.scope_key
              AND j.record_collection = r.collection AND j.record_key = r.key AND j.terminal_at IS NULL
            WHERE r.owner_id = owner_key AND r.feature = feature_key AND r.scope_key = scope
              AND r.collection = item->>'collection' AND r.key = item->>'key' AND r.expires_at <= stamp
              AND NOT EXISTS(SELECT FROM jsonb_array_elements(p_request->'jobs') replacement WHERE replacement->>'key' = j.key)
              AND NOT (p_request->'cancel_jobs') ? j.key) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Expired feature record still has unfinished jobs';
        END IF;
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(p_request->'deletes') LOOP
        PERFORM msu_hub_private.check_object(item,ARRAY['collection','key'],ARRAY['collection','key']);
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(p_request->'jobs') LOOP
        PERFORM msu_hub_private.check_object(item,ARRAY['key','kind','record','run_at','serial_key','retry_until'],ARRAY['key','kind','record','run_at']);
        PERFORM msu_hub_private.feature_text(item->'key',256);
        PERFORM msu_hub_private.feature_text(item->'kind',64,true);
        PERFORM msu_hub_private.feature_text(item->'serial_key',256,false,true);
        PERFORM msu_hub_private.feature_time(item->'run_at');
        PERFORM msu_hub_private.feature_time(item->'retry_until',true);
        IF msu_hub_private.feature_time(item->'retry_until',true) < msu_hub_private.feature_time(item->'run_at') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Feature job deadline precedes its schedule';
        END IF;
        PERFORM msu_hub_private.check_object(item->'record',ARRAY['collection','key'],ARRAY['collection','key']);
        PERFORM msu_hub_private.feature_text(item->'record'->'collection',64,true);
        PERFORM msu_hub_private.feature_text(item->'record'->'key',256);
        IF NOT EXISTS(SELECT FROM jsonb_array_elements(p_request->'guards') g
            WHERE g->>'collection' = item->'record'->>'collection' AND g->>'key' = item->'record'->>'key') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Feature job requires a record guard';
        END IF;
        IF EXISTS(SELECT FROM msu_hub_private.feature_jobs WHERE owner_id = owner_key AND feature = feature_key AND scope_key = scope
            AND key = item->>'key' AND serial_key IS DISTINCT FROM item->>'serial_key') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Feature job serial identity cannot change';
        END IF;
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(p_request->'cancel_jobs') LOOP
        PERFORM msu_hub_private.feature_text(item,256);
    END LOOP;
    IF conflict THEN result := '{"outcome":"conflict","records":[]}'::jsonb;
    ELSE
        FOR item IN SELECT * FROM jsonb_array_elements(p_request->'puts') LOOP
            INSERT INTO msu_hub_private.feature_records AS r(owner_id,feature,scope_key,collection,key,payload_version,payload,parent,status,expires_at,created_at,updated_at)
            VALUES(owner_key,feature_key,scope,item->>'collection',item->>'key',(item->>'payload_version')::integer,item->'payload',
                item->>'parent',item->>'status',msu_hub_private.feature_time(item->'expires_at',true),stamp,stamp)
            ON CONFLICT(owner_id,feature,scope_key,collection,key) DO UPDATE SET etag = gen_random_uuid(),payload_version = EXCLUDED.payload_version,
                payload = EXCLUDED.payload,parent = EXCLUDED.parent,status = EXCLUDED.status,expires_at = EXCLUDED.expires_at,updated_at = stamp,
                created_at = CASE WHEN r.expires_at <= stamp THEN stamp ELSE r.created_at END
            RETURNING * INTO value;
            records := records || jsonb_build_array(msu_hub_private.feature_record_json(value));
        END LOOP;
        FOR item IN SELECT * FROM jsonb_array_elements(p_request->'deletes') LOOP
            DELETE FROM msu_hub_private.feature_records WHERE owner_id = owner_key AND feature = feature_key AND scope_key = scope
                AND collection = item->>'collection' AND key = item->>'key';
        END LOOP;
        FOR item IN SELECT * FROM jsonb_array_elements(p_request->'jobs') LOOP
            IF NOT EXISTS(SELECT FROM msu_hub_private.feature_records WHERE owner_id = owner_key AND feature = feature_key AND scope_key = scope
                AND collection = item->'record'->>'collection' AND key = item->'record'->>'key' AND (expires_at IS NULL OR expires_at > stamp)) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Feature job requires a live record';
            END IF;
            INSERT INTO msu_hub_private.feature_jobs AS j(owner_id,feature,scope_key,key,kind,record_collection,record_key,run_at,serial_key,retry_until)
            VALUES(owner_key,feature_key,scope,item->>'key',item->>'kind',item->'record'->>'collection',item->'record'->>'key',
                msu_hub_private.feature_time(item->'run_at'),item->>'serial_key',msu_hub_private.feature_time(item->'retry_until',true))
            ON CONFLICT(owner_id,feature,scope_key,key) DO UPDATE SET generation = j.generation + 1,kind = EXCLUDED.kind,
                record_collection = EXCLUDED.record_collection,record_key = EXCLUDED.record_key,run_at = EXCLUDED.run_at,
                retry_until = EXCLUDED.retry_until,attempts = 0,state = 'pending',terminal_at = NULL;
        END LOOP;
        FOR item IN SELECT * FROM jsonb_array_elements(p_request->'cancel_jobs') LOOP
            UPDATE msu_hub_private.feature_jobs SET generation = generation + 1,state = 'cancelled',terminal_at = stamp
            WHERE owner_id = owner_key AND feature = feature_key AND scope_key = scope AND key = item #>> '{}';
        END LOOP;
        IF EXISTS(SELECT FROM msu_hub_private.feature_jobs j WHERE j.owner_id = owner_key AND j.feature = feature_key AND j.scope_key = scope
            AND j.terminal_at IS NULL AND EXISTS(SELECT FROM jsonb_array_elements(p_request->'deletes') d
                WHERE d->>'collection' = j.record_collection AND d->>'key' = j.record_key)) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Unfinished feature jobs protect their records';
        END IF;
        result := jsonb_build_object('outcome','committed','records',records);
    END IF;
    SELECT jsonb_set(result,'{records}',COALESCE(jsonb_agg(r.value - 'payload' ORDER BY r.ordinality),'[]'::jsonb)) INTO saved_result
        FROM jsonb_array_elements(result->'records') WITH ORDINALITY r(value,ordinality);
    INSERT INTO msu_hub_private.feature_operations(owner_id,feature,scope_key,operation_id,request_hash,result,expires_at)
    VALUES(owner_key,feature_key,scope,p_request->>'operation_id',digest,saved_result,stamp + interval '7 days')
    ON CONFLICT(owner_id,feature,scope_key,operation_id) DO UPDATE SET request_hash = EXCLUDED.request_hash,result = EXCLUDED.result,expires_at = EXCLUDED.expires_at;
    RETURN result;
END $$;

CREATE FUNCTION msu_hub_api.feature_claim_jobs_v1(p_request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    bot bigint := msu_hub_private.require_principal(); batch integer; lease integer; item jsonb;
    candidate msu_hub_private.feature_jobs; job msu_hub_private.feature_jobs; result jsonb := '[]'::jsonb; stamp timestamptz;
BEGIN
    PERFORM msu_hub_private.check_object(p_request,ARRAY['handlers','limit','lease_seconds'],ARRAY['handlers','limit','lease_seconds']);
    IF octet_length(p_request::text) > 262144 OR jsonb_typeof(p_request->'handlers') <> 'array'
       OR jsonb_array_length(p_request->'handlers') NOT BETWEEN 1 AND 64
       OR jsonb_typeof(p_request->'limit') <> 'number' OR (p_request->>'limit') !~ '^[0-9]+$'
       OR jsonb_typeof(p_request->'lease_seconds') <> 'number' OR (p_request->>'lease_seconds') !~ '^[0-9]+$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature job claim';
    END IF;
    batch := (p_request->>'limit')::integer; lease := (p_request->>'lease_seconds')::integer;
    IF batch NOT BETWEEN 1 AND 20 OR lease NOT BETWEEN 5 AND 300 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature job claim';
    END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(p_request->'handlers') LOOP
        PERFORM msu_hub_private.check_object(item,ARRAY['feature','kind'],ARRAY['feature','kind']);
        PERFORM msu_hub_private.feature_text(item->'feature',64,true);
        PERFORM msu_hub_private.feature_text(item->'kind',64,true);
    END LOOP;
    -- Never hold a row lock while waiting for a scope lock held by a commit.
    FOR candidate IN SELECT * FROM msu_hub_private.feature_jobs j
        WHERE j.owner_id IN (0,bot) AND j.state = 'pending' AND j.run_at <= now()
          AND (j.lease_until IS NULL OR j.lease_until <= now())
          AND EXISTS(SELECT FROM jsonb_array_elements(p_request->'handlers') h WHERE h->>'feature' = j.feature AND h->>'kind' = j.kind)
          AND (j.serial_key IS NULL OR NOT EXISTS(SELECT FROM msu_hub_private.feature_jobs earlier
            WHERE earlier.owner_id = j.owner_id AND earlier.feature = j.feature AND earlier.scope_key = j.scope_key
              AND earlier.serial_key = j.serial_key AND earlier.sequence < j.sequence AND earlier.terminal_at IS NULL))
        ORDER BY j.run_at,j.sequence LIMIT 200
    LOOP
        IF NOT pg_try_advisory_xact_lock(msu_hub_private.feature_lock(candidate.owner_id,candidate.feature,candidate.scope_key)) THEN CONTINUE; END IF;
        stamp := clock_timestamp();
        SELECT * INTO job FROM msu_hub_private.feature_jobs WHERE owner_id = candidate.owner_id AND feature = candidate.feature
            AND scope_key = candidate.scope_key AND key = candidate.key FOR UPDATE SKIP LOCKED;
        IF NOT FOUND OR job.state <> 'pending' OR job.run_at > stamp OR job.lease_until > stamp
           OR NOT EXISTS(SELECT FROM jsonb_array_elements(p_request->'handlers') h WHERE h->>'feature' = job.feature AND h->>'kind' = job.kind)
           OR (job.serial_key IS NOT NULL AND EXISTS(SELECT FROM msu_hub_private.feature_jobs earlier
                WHERE earlier.owner_id = job.owner_id AND earlier.feature = job.feature AND earlier.scope_key = job.scope_key
                  AND earlier.serial_key = job.serial_key AND earlier.sequence < job.sequence AND earlier.terminal_at IS NULL)) THEN CONTINUE; END IF;
        UPDATE msu_hub_private.feature_jobs SET lease_token = gen_random_uuid(),leased_generation = generation,
            lease_until = stamp + make_interval(secs => lease),attempts = attempts + 1
        WHERE owner_id = job.owner_id AND feature = job.feature AND scope_key = job.scope_key AND key = job.key RETURNING * INTO job;
        result := result || jsonb_build_array(msu_hub_private.feature_job_json(job));
        EXIT WHEN jsonb_array_length(result) >= batch;
    END LOOP;
    RETURN result;
END $$;

CREATE FUNCTION msu_hub_api.feature_job_status_v1(p_request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    bot bigint := msu_hub_private.require_principal(); owner_key bigint; feature_key text; scope text; action text;
    token uuid; requested_generation bigint; lease integer; stamp timestamptz; job msu_hub_private.feature_jobs;
BEGIN
    PERFORM msu_hub_private.check_object(p_request,ARRAY['feature','scope','key','generation','lease_token','action','lease_seconds','run_at'],
        ARRAY['feature','scope','key','generation','lease_token','action']);
    owner_key := msu_hub_private.feature_scope(p_request,bot); feature_key := p_request->>'feature'; scope := p_request->'scope'->>'key';
    PERFORM msu_hub_private.feature_text(p_request->'key',256);
    token := msu_hub_private.feature_text(p_request->'lease_token',36)::uuid;
    action := msu_hub_private.feature_text(p_request->'action',16);
    IF action NOT IN ('check','renew','complete','retry','hold','expire') OR jsonb_typeof(p_request->'generation') <> 'number'
       OR (p_request->>'generation') !~ '^[0-9]+$' OR (p_request->>'generation')::bigint < 1 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature job status';
    END IF;
    requested_generation := (p_request->>'generation')::bigint;
    IF action = 'renew' THEN
        IF jsonb_typeof(p_request->'lease_seconds') IS DISTINCT FROM 'number' OR (p_request->>'lease_seconds') !~ '^[0-9]+$'
           OR (p_request->>'lease_seconds')::integer NOT BETWEEN 5 AND 300 THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature lease';
        END IF;
        lease := (p_request->>'lease_seconds')::integer;
    END IF;
    IF action = 'retry' THEN PERFORM msu_hub_private.feature_time(p_request->'run_at'); END IF;
    PERFORM pg_advisory_xact_lock(msu_hub_private.feature_lock(owner_key,feature_key,scope)); stamp := clock_timestamp();
    SELECT * INTO job FROM msu_hub_private.feature_jobs WHERE owner_id = owner_key AND feature = feature_key AND scope_key = scope
        AND key = p_request->>'key' FOR UPDATE;
    IF NOT FOUND OR job.lease_token IS DISTINCT FROM token OR job.leased_generation IS DISTINCT FROM requested_generation
       OR job.lease_until <= stamp THEN RETURN '{"current":false}'::jsonb; END IF;
    IF job.generation <> requested_generation OR job.terminal_at IS NOT NULL THEN
        IF action IN ('complete','retry','hold','expire') THEN
            UPDATE msu_hub_private.feature_jobs SET lease_token = NULL,leased_generation = NULL,lease_until = NULL
            WHERE owner_id = owner_key AND feature = feature_key AND scope_key = scope AND key = job.key;
        END IF;
        RETURN '{"current":false}'::jsonb;
    END IF;
    IF action = 'renew' THEN
        UPDATE msu_hub_private.feature_jobs SET lease_until = stamp + make_interval(secs => lease)
        WHERE owner_id = owner_key AND feature = feature_key AND scope_key = scope AND key = job.key;
    ELSIF action <> 'check' THEN
        UPDATE msu_hub_private.feature_jobs SET lease_token = NULL,leased_generation = NULL,lease_until = NULL,
            state = CASE action WHEN 'complete' THEN 'complete' WHEN 'expire' THEN 'expired' WHEN 'hold' THEN 'held' ELSE 'pending' END,
            terminal_at = CASE WHEN action IN ('complete','expire') THEN stamp END,
            run_at = CASE WHEN action = 'retry' THEN msu_hub_private.feature_time(p_request->'run_at') ELSE run_at END
        WHERE owner_id = owner_key AND feature = feature_key AND scope_key = scope AND key = job.key;
    END IF;
    RETURN '{"current":true}'::jsonb;
END $$;

-- Separate maintenance keeps the message-retention contract and its installed guards intact.
CREATE FUNCTION msu_hub_private.retain_features(p_batch integer DEFAULT 1000,p_now timestamptz DEFAULT now()) RETURNS jsonb
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE record_count integer := 0; job_count integer := 0; operation_count integer := 0; affected integer; item record;
BEGIN
    IF p_batch IS NULL OR p_batch NOT BETWEEN 1 AND 10000 OR p_now IS NULL OR NOT isfinite(p_now) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid feature retention batch';
    END IF;
    -- Maintenance follows the same scope-before-row order as application writers.
    FOR item IN SELECT owner_id,feature,scope_key,key FROM msu_hub_private.feature_jobs
        WHERE terminal_at <= p_now - interval '7 days' ORDER BY terminal_at LIMIT p_batch LOOP
        IF NOT pg_try_advisory_xact_lock(msu_hub_private.feature_lock(item.owner_id,item.feature,item.scope_key)) THEN CONTINUE; END IF;
        DELETE FROM msu_hub_private.feature_jobs WHERE owner_id = item.owner_id AND feature = item.feature AND scope_key = item.scope_key
            AND key = item.key AND terminal_at <= p_now - interval '7 days' AND (lease_until IS NULL OR lease_until <= p_now);
        GET DIAGNOSTICS affected = ROW_COUNT; job_count := job_count + affected;
    END LOOP;
    FOR item IN SELECT owner_id,feature,scope_key,collection,key FROM msu_hub_private.feature_records r
        WHERE expires_at <= p_now AND NOT EXISTS(SELECT FROM msu_hub_private.feature_jobs j WHERE j.owner_id = r.owner_id
            AND j.feature = r.feature AND j.scope_key = r.scope_key AND j.record_collection = r.collection AND j.record_key = r.key AND j.terminal_at IS NULL)
        ORDER BY expires_at LIMIT p_batch LOOP
        IF NOT pg_try_advisory_xact_lock(msu_hub_private.feature_lock(item.owner_id,item.feature,item.scope_key)) THEN CONTINUE; END IF;
        DELETE FROM msu_hub_private.feature_records r WHERE owner_id = item.owner_id AND feature = item.feature AND scope_key = item.scope_key
            AND collection = item.collection AND key = item.key AND expires_at <= p_now AND NOT EXISTS(
                SELECT FROM msu_hub_private.feature_jobs j WHERE j.owner_id = r.owner_id AND j.feature = r.feature AND j.scope_key = r.scope_key
                  AND j.record_collection = r.collection AND j.record_key = r.key AND j.terminal_at IS NULL);
        GET DIAGNOSTICS affected = ROW_COUNT; record_count := record_count + affected;
    END LOOP;
    FOR item IN SELECT owner_id,feature,scope_key,operation_id FROM msu_hub_private.feature_operations
        WHERE expires_at <= p_now ORDER BY expires_at LIMIT p_batch LOOP
        IF NOT pg_try_advisory_xact_lock(msu_hub_private.feature_lock(item.owner_id,item.feature,item.scope_key)) THEN CONTINUE; END IF;
        DELETE FROM msu_hub_private.feature_operations WHERE owner_id = item.owner_id AND feature = item.feature AND scope_key = item.scope_key
            AND operation_id = item.operation_id AND expires_at <= p_now;
        GET DIAGNOSTICS affected = ROW_COUNT; operation_count := operation_count + affected;
    END LOOP;
    RETURN jsonb_build_object('records',record_count,'jobs',job_count,'operations',operation_count);
END $$;

DO $$ DECLARE routine record; BEGIN
    FOR routine IN SELECT p.oid::regprocedure AS signature,n.nspname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE (n.nspname = 'msu_hub_private' AND (p.proname LIKE 'feature_%' OR p.proname = 'retain_features'))
           OR (n.nspname = 'msu_hub_api' AND p.proname LIKE 'feature_%') LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO msu_hub_owner',routine.signature);
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',routine.signature);
        IF routine.nspname = 'msu_hub_api' THEN EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO authenticated',routine.signature); END IF;
    END LOOP;
END $$;
INSERT INTO msu_hub_private.schema_migrations(version) VALUES(5);
NOTIFY pgrst, 'reload schema';
COMMIT;
