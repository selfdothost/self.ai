#!/usr/bin/env python3
"""Cancel + delete eval jobs by model_id and/or benchmark filter.

Requires SELF_AI_API_KEY in the environment, and SELF_AI_URL pointing at the
target self.ai deployment (defaults to http://localhost:8080). Useful for cleaning up after a
gauntlet run gone wrong, or clearing test jobs before a real one — GET /jobs
has no pagination yet (self.ai#42), so a large stale job count degrades that
endpoint badly; this exists to recover from that quickly.

Usage:
    python3 cleanup_eval_jobs.py --model-id <id> [--benchmark <name>]
    python3 cleanup_eval_jobs.py --job-ids-file <path>   # one UUID per line
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("SELF_AI_URL", "http://localhost:8080").rstrip("/") + "/api/v1/evaluations/jobs"
API_KEY = os.environ["SELF_AI_API_KEY"]
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def call(method, url, body=None, retries=3, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else True
        except urllib.error.HTTPError as e:
            return {"_error": e.code, "_body": e.read().decode()}
        except Exception as e:
            if attempt == retries - 1:
                return {"_error": "exception", "_body": str(e)}
            time.sleep(2)


def resolve_ids(args):
    if args.job_ids_file:
        with open(args.job_ids_file) as f:
            return [line.strip() for line in f if line.strip()]

    jobs = call("GET", BASE)
    if isinstance(jobs, dict) and "_error" in jobs:
        raise RuntimeError(f"failed to list jobs: {jobs}")
    ids = []
    for j in jobs:
        if args.model_id and j["model_id"] != args.model_id:
            continue
        if args.benchmark and j["benchmark"] != args.benchmark:
            continue
        ids.append(j["id"])
    return ids


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model-id")
    p.add_argument("--benchmark")
    p.add_argument("--job-ids-file")
    args = p.parse_args()

    if not (args.model_id or args.benchmark or args.job_ids_file):
        p.error("need --model-id, --benchmark, or --job-ids-file")

    ids = resolve_ids(args)
    print(f"{len(ids)} jobs to clean up", flush=True)

    cancelled = 0
    deleted = 0
    errors = []

    for i, job_id in enumerate(ids):
        cresp = call("POST", f"{BASE}/{job_id}/cancel", {})
        if not (isinstance(cresp, dict) and "_error" in cresp):
            cancelled += 1
        dresp = call("DELETE", f"{BASE}/{job_id}/delete")
        if isinstance(dresp, dict) and "_error" in dresp:
            errors.append((job_id, dresp))
        else:
            deleted += 1
        if (i + 1) % 25 == 0:
            print(f"progress: {i+1}/{len(ids)} cancelled={cancelled} deleted={deleted}", flush=True)

    print(f"\nFINAL: cancelled={cancelled} deleted={deleted} errors={len(errors)}", flush=True)
    for job_id, err in errors[:10]:
        print(f"  {job_id}: {err}", flush=True)


if __name__ == "__main__":
    main()
