"""Inspect private job metadata without exposing payloads or changing queue state."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def query(feature: str, state: str, limit: int) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", feature) is None or state not in {"pending", "held"} or not 1 <= limit <= 200:
        raise ValueError("Invalid queue filter")
    return f"""BEGIN READ ONLY;
SET LOCAL statement_timeout='5s';
SELECT COALESCE(jsonb_agg(to_jsonb(q)),'[]'::jsonb) FROM (
 SELECT j.owner_id,j.feature,j.scope_key,j.key,j.kind,j.generation,j.state,j.attempts,
        j.run_at,j.retry_until,j.lease_until,j.record_collection,j.record_key,
        r.etag AS record_etag,r.payload_version,r.status AS record_status,r.expires_at AS record_expiry
 FROM msu_hub_private.feature_jobs j
 LEFT JOIN msu_hub_private.feature_records r ON
   (r.owner_id,r.feature,r.scope_key,r.collection,r.key)=(j.owner_id,j.feature,j.scope_key,j.record_collection,j.record_key)
 WHERE j.feature='{feature}' AND j.state='{state}'
 ORDER BY j.run_at,j.sequence LIMIT {limit}
) q;
COMMIT;"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", type=Path, required=True, help="Private PostgreSQL URI file; use administrative access")
    parser.add_argument("--feature", required=True)
    parser.add_argument("--state", choices=("pending", "held"), default="held")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    path = args.connection_file
    if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > 8192:
        raise ValueError("Connection file must be private and bounded")
    env = os.environ.copy()
    env["PGDATABASE"] = path.read_text().strip()
    result = subprocess.run(
        ["psql", "-XqAt", "-v", "ON_ERROR_STOP=1"],
        input=query(args.feature, args.state, args.limit),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError("Queue inspection failed; check administrative access")
    rows = json.loads(result.stdout)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise SystemExit("Queue inspection failed; no database details printed") from None
