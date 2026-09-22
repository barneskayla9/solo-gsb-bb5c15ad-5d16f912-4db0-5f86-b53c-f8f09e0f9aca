"""End-to-end checks for tenant-targeted gray rollout.

Run with: python3 scripts/verify_tenant_routing.py
Uses an isolated temp DB; does not touch ./data.
"""
from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["CANARY_DB_PATH"] = os.path.join(_tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.logic import cohort_bucket  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)

failures: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def pick_outside_cohort_user(service: str, percentage: int) -> str:
    """Find a user whose stable-hash bucket is outside the canary cohort."""
    for i in range(10000):
        uid = f"user-{i}"
        if cohort_bucket(service, uid) >= percentage:
            return uid
    raise RuntimeError("no outside-cohort user found")


def pick_inside_cohort_user(service: str, percentage: int) -> str:
    for i in range(10000):
        uid = f"user-{i}"
        if cohort_bucket(service, uid) < percentage:
            return uid
    raise RuntimeError("no inside-cohort user found")


# --------------------------------------------------------------------------- #
# 1. Setup: service + baseline v1 + canary v2 at 10%
# --------------------------------------------------------------------------- #
r = client.post("/services", json={"name": "checkout", "default_rollout": [10, 50, 100]})
check("register service", r.status_code == 201, r.text)

r = client.post("/services/checkout/releases", json={"version": "v1.0.0"})
check("baseline release", r.status_code == 201, r.text)
check("baseline lists default empty",
      r.json()["tenant_allowlist"] == [] and r.json()["tenant_denylist"] == [])

# 2. List overlap -> 422
r = client.post("/services/checkout/releases", json={
    "version": "v2.0.0",
    "tenant_allowlist": ["tenant-a", "  tenant-b  "],
    "tenant_denylist": ["tenant-b", "tenant-c"],
})
check("overlapping lists rejected with 422", r.status_code == 422, f"{r.status_code} {r.text}")

# Duplicate entries within one list are de-duplicated, not rejected.
r = client.post("/services/checkout/releases", json={
    "version": "v2.0.0",
    "tenant_allowlist": ["tenant-a", "tenant-a", "  tenant-b  "],
    "tenant_denylist": ["tenant-c", "tenant-c"],
})
check("canary with lists created", r.status_code == 201, r.text)
body = r.json()
check("allowlist trimmed/deduped", body["tenant_allowlist"] == ["tenant-a", "tenant-b"], str(body))
check("denylist deduped", body["tenant_denylist"] == ["tenant-c"], str(body))
canary_id = body["id"]

# 3. No tenant_id -> legacy behavior, legacy shape (no tenant_id/rule keys)
outside_user = pick_outside_cohort_user("checkout", 10)
inside_user = pick_inside_cohort_user("checkout", 10)
print(f"    outside-cohort user={outside_user} bucket={cohort_bucket('checkout', outside_user)}")
print(f"    inside-cohort  user={inside_user} bucket={cohort_bucket('checkout', inside_user)}")

r = client.get("/services/checkout/route", params={"user_id": outside_user})
check("legacy outside -> stable", r.status_code == 200 and r.json()["cohort"] == "stable", r.text)
check("legacy response has no tenant_id key", "tenant_id" not in r.json())
check("legacy response has no rule key", "rule" not in r.json())

r = client.get("/services/checkout/route", params={"user_id": inside_user})
check("legacy inside -> canary", r.json()["cohort"] == "canary")

# 4. Allowlist: an outside-cohort user from an allowed tenant is pinned to canary
r = client.get("/services/checkout/route",
               params={"user_id": outside_user, "tenant_id": "tenant-a"})
j = r.json()
check("allowlist tenant -> canary despite bucket",
      j["cohort"] == "canary" and j["version"] == "v2.0.0", str(j))
check("allowlist rule reported", j.get("rule") == "tenant_allowed", str(j))
check("tenant_id echoed", j.get("tenant_id") == "tenant-a", str(j))

# 5. Denylist takes precedence: an inside-cohort user from a denied tenant -> stable
r = client.get("/services/checkout/route",
               params={"user_id": inside_user, "tenant_id": "tenant-c"})
j = r.json()
check("denylist tenant -> stable despite bucket",
      j["cohort"] == "stable" and j["version"] == "v1.0.0", str(j))
check("denylist rule reported", j.get("rule") == "tenant_denied", str(j))

# 6. Non-listed tenant -> hash percentage split
r = client.get("/services/checkout/route",
               params={"user_id": outside_user, "tenant_id": "tenant-other"})
j = r.json()
check("unlisted outside bucket -> stable_cohort",
      j["cohort"] == "stable" and j.get("rule") == "stable_cohort", str(j))

r = client.get("/services/checkout/route",
               params={"user_id": inside_user, "tenant_id": "tenant-other"})
j = r.json()
check("unlisted inside bucket -> canary_cohort",
      j["cohort"] == "canary" and j.get("rule") == "canary_cohort", str(j))

# 7. Paused canary: allowlist does NOT bypass the pause; denylist still reports deny
client.post(f"/services/checkout/releases/{canary_id}/pause")
r = client.get("/services/checkout/route",
               params={"user_id": outside_user, "tenant_id": "tenant-a"})
j = r.json()
check("paused: allowlist cannot bypass pause",
      j["cohort"] == "stable" and j.get("rule") == "canary_paused", str(j))

r = client.get("/services/checkout/route",
               params={"user_id": inside_user, "tenant_id": "tenant-c"})
j = r.json()
check("paused: denylist still wins",
      j["cohort"] == "stable" and j.get("rule") == "tenant_denied", str(j))
client.post(f"/services/checkout/releases/{canary_id}/resume")

# 8. Audit: only tenant-identified route checks are recorded
r = client.get("/services/checkout/audit", params={"action": "route_checked"})
entries = r.json()["entries"]
check("route_checked audit entries exist", len(entries) >= 5, f"got {len(entries)}")
for e in entries:
    d = e["detail"]
    check(f"audit row has rule ({d.get('rule')})",
          "rule" in d and "tenant_id" in d and "cohort" in d, str(d))
    check("audit tenant-identified only", d["tenant_id"] is not None, str(d))
deny_entries = [e for e in entries if e["detail"]["rule"] == "tenant_denied"]
check("audit captured tenant_denied hits", len(deny_entries) >= 2, str(deny_entries))

# creation audit carries lists
r = client.get("/services/checkout/audit", params={"action": "release_created"})
d = r.json()["entries"][0]["detail"]
check("release_created audit includes lists",
      d["tenant_allowlist"] == ["tenant-a", "tenant-b"] and d["tenant_denylist"] == ["tenant-c"],
      str(d))

# 9. Blank tenant_id query value -> 422 (min_length)
r = client.get("/services/checkout/route", params={"user_id": "u1", "tenant_id": ""})
check("blank tenant_id -> 422", r.status_code == 422, str(r.status_code))

# 10. A canary at 100% that is still 'progressing' (single-stage rollout):
#     deny still pins to stable; everyone else hits canary_full.
client.post(f"/services/checkout/releases/{canary_id}/rollback")
r = client.post("/services/checkout/releases", json={
    "version": "v2.1.0",
    "rollout": [100],
    "tenant_allowlist": ["tenant-a"],
    "tenant_denylist": ["tenant-c"],
})
check("100% single-stage canary created progressing",
      r.status_code == 201 and r.json()["status"] == "progressing"
      and r.json()["percentage"] == 100, r.text)
r = client.get("/services/checkout/route",
               params={"user_id": outside_user, "tenant_id": "tenant-c"})
j = r.json()
check("100% canary: denied tenant stays on stable",
      j["cohort"] == "stable" and j["version"] == "v1.0.0" and j.get("rule") == "tenant_denied",
      str(j))
r = client.get("/services/checkout/route",
               params={"user_id": outside_user, "tenant_id": "tenant-other"})
j = r.json()
check("100% canary: others rule=canary_full",
      j["cohort"] == "canary" and j.get("rule") == "canary_full", str(j))
r = client.get("/services/checkout/route", params={"user_id": outside_user})
check("100% canary: legacy shape unchanged", "rule" not in r.json() and "tenant_id" not in r.json())

# 11. Second service with no lists, tenant query -> hash rules
client.post("/services", json={"name": "payments"})
client.post("/services/payments/releases", json={"version": "p1"})
client.post("/services/payments/releases", json={"version": "p2"})
r = client.get("/services/payments/route", params={"user_id": "u", "tenant_id": "t"})
j = r.json()
check("service without lists: tenant falls to hash rules",
      j.get("rule") in ("canary_cohort", "stable_cohort"), str(j))

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED: {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
