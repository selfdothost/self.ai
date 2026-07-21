#!/usr/bin/env python3
"""Submit one eval job per available code-eval task for one or more models.

Requires SELF_AI_URL pointing at the target self.ai deployment (defaults to
http://localhost:8080) and SELF_AI_API_KEY (a personal API key — jobs are
owned by whichever account the key belongs to, so results show up in that
user's UI) and kubectl access to the self-ai namespace (no public proxy for
self.code-eval's /api/tasks exists yet — fetched via `kubectl exec` against
the self-code-eval pod directly). Fetches the live task catalog rather than
hardcoding it, so it stays current as tasks are added/removed (see self.ai#41
context — benchmarks get rotated as questions leak into training data).

Usage:
    python3 submit_gauntlet_jobs.py <model_id> [<model_id> ...]

This is what the "Gauntlet" eval type (self.ai issue TBD) should eventually
replace with a first-class, RBAC'd, single-job-triggers-everything API call —
see that issue for the full design. Until it lands, this script is the manual
equivalent, tracked here instead of living only as an ad-hoc local file.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("SELF_AI_URL", "http://localhost:8080").rstrip("/") + "/api/v1/evaluations/jobs"
API_KEY = os.environ["SELF_AI_API_KEY"]
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def fetch_tasks():
    pod = subprocess.run(
        ["kubectl", "-n", "self-ai", "get", "pods", "-l", "app=self-code-eval",
         "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    out = subprocess.run(
        ["kubectl", "-n", "self-ai", "exec", pod, "--",
         "curl", "-s", "http://localhost:8094/api/tasks"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [t["name"] for t in json.loads(out)]


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


def main():
    models = sys.argv[1:]
    if not models:
        print("usage: submit_gauntlet_jobs.py <model_id> [<model_id> ...]", file=sys.stderr)
        sys.exit(1)

    tasks = fetch_tasks()
    print(f"{len(tasks)} tasks x {len(models)} models = {len(tasks) * len(models)} jobs", flush=True)

    created = 0
    approved = 0
    failed = []

    for model_id in models:
        for task in tasks:
            resp = call("POST", f"{BASE}/create", {
                "eval_type": "code-eval",
                "benchmark": task,
                "model_id": model_id,
            })
            if isinstance(resp, dict) and "_error" in resp:
                failed.append((model_id, task, "create", resp))
                print(f"CREATE FAILED {model_id} / {task}: {resp}", flush=True)
                continue
            created += 1
            job_id = resp["id"]
            aresp = call("POST", f"{BASE}/{job_id}/approve", {})
            # Admin-created jobs can auto-approve before this call lands —
            # a 400 "only pending/scheduled jobs can be approved" here is
            # benign (job's already queued), not a real failure.
            if isinstance(aresp, dict) and "_error" in aresp and aresp["_error"] != 400:
                failed.append((model_id, task, "approve", aresp))
                print(f"APPROVE FAILED {model_id} / {task}: {aresp}", flush=True)
                continue
            approved += 1

    print(f"\ncreated={created} approved={approved} failed={len(failed)}", flush=True)
    if failed:
        print("failures:", flush=True)
        for m, t, stage, err in failed[:20]:
            print(f"  {m} / {t} ({stage}): {err}", flush=True)


if __name__ == "__main__":
    main()
