"""Core canary-gate logic: hashing, rollout state, sliding-window evaluation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def parse_iso(value: str) -> str:
    """Validate an ISO-8601 timestamp and return it normalized to UTC."""
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Stable cohort assignment
# --------------------------------------------------------------------------- #
def cohort_bucket(service_name: str, user_id: str) -> int:
    """Deterministic bucket in [0, 99], stable across instances and restarts.

    Hashing the pair (service, user) means the same user is always routed to
    the same cohort for a given service, and different services are
    independent. Percentages split the 0..99 space monotonically so promoting
    a release never moves a user back to the old version.
    """
    digest = hashlib.sha256(f"{service_name}:{user_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 100


def user_is_in_cohort(service_name: str, user_id: str, percentage: int) -> bool:
    return cohort_bucket(service_name, user_id) < percentage


# --------------------------------------------------------------------------- #
# Tenant allow/deny lists
# --------------------------------------------------------------------------- #
def tenant_lists(release: sqlite3.Row) -> tuple[set[str], set[str]]:
    """Parse a release's (allowlist, denylist) from their JSON columns.

    Rows created before the tenant-targeting migration have no such keys and
    behave as if both lists were empty.
    """
    def _load(column: str) -> set[str]:
        try:
            return set(json.loads(release[column]))
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return set()

    return _load("tenant_allowlist"), _load("tenant_denylist")


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
def get_service(db: sqlite3.Connection, service_name: str) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM services WHERE name = ?", (service_name,)).fetchone()


def get_release(db: sqlite3.Connection, release_id: int) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()


def get_progressing_or_paused(db: sqlite3.Connection, service_name: str) -> Optional[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM releases WHERE service_name = ? AND status IN ('progressing', 'paused') "
        "ORDER BY id DESC LIMIT 1",
        (service_name,),
    ).fetchone()


def get_stable_release(db: sqlite3.Connection, service_name: str) -> Optional[sqlite3.Row]:
    """The most recent fully rolled-out release (the fallback / current stable)."""
    return db.execute(
        "SELECT * FROM releases WHERE service_name = ? AND status = 'complete' "
        "ORDER BY id DESC LIMIT 1",
        (service_name,),
    ).fetchone()


def previous_complete(db: sqlite3.Connection, service_name: str, release_id: int) -> Optional[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM releases WHERE service_name = ? AND status = 'complete' AND id < ? "
        "ORDER BY id DESC LIMIT 1",
        (service_name, release_id),
    ).fetchone()


def write_audit(db: sqlite3.Connection, service_name: str, action: str, detail: dict,
                version: Optional[str] = None, actor: str = "api") -> None:
    db.execute(
        "INSERT INTO audit_log (service_name, action, version, actor, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (service_name, action, version, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now_iso()),
    )


# --------------------------------------------------------------------------- #
# Sliding-window error rate
# --------------------------------------------------------------------------- #
def window_stats(db: sqlite3.Connection, release: sqlite3.Row) -> dict:
    cutoff = (utcnow() - timedelta(seconds=release["window_seconds"])).isoformat(timespec="seconds")
    row = db.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures "
        "FROM telemetry WHERE release_id = ? AND occurred_at >= ?",
        (release["id"], cutoff),
    ).fetchone()
    total = row["total"] or 0
    failures = row["failures"] or 0
    successes = total - failures
    error_rate = (failures / total) if total else 0.0
    return {
        "total": total,
        "successes": successes,
        "failures": failures,
        "error_rate": round(error_rate, 6),
        "window_seconds": release["window_seconds"],
        "window_start": cutoff,
        "min_requests": release["min_requests"],
        "error_threshold": release["error_threshold"],
        "breached": total >= release["min_requests"] and error_rate > release["error_threshold"],
    }


def evaluate_gate(db: sqlite3.Connection, release: sqlite3.Row) -> dict:
    """Pause a progressing canary automatically if its error rate breaches."""
    stats = window_stats(db, release)
    stats["release_id"] = release["id"]
    stats["version"] = release["version"]
    if release["status"] == "progressing" and stats["breached"]:
        db.execute(
            "UPDATE releases SET status = 'paused', updated_at = ? WHERE id = ? AND status = 'progressing'",
            (now_iso(), release["id"]),
        )
        write_audit(
            db,
            release["service_name"],
            "auto_paused",
            {
                "version": release["version"],
                "percentage": release["percentage"],
                **stats,
                "reason": "error rate exceeded threshold within sliding window",
            },
            version=release["version"],
            actor="system",
        )
        stats["auto_paused"] = True
    else:
        stats["auto_paused"] = False
    return stats


# --------------------------------------------------------------------------- #
# Routing decision
# --------------------------------------------------------------------------- #
# Rules recorded on /route responses and in route_checked audit entries:
#   tenant_denied        - tenant matched the canary denylist, pinned to stable
#   tenant_allowed       - tenant matched the canary allowlist, pinned to canary
#   canary_cohort        - stable-hash bucket fell inside the canary percentage
#   stable_cohort        - stable-hash bucket fell outside the canary percentage
#   canary_full          - canary at 100% (serves everyone unless denied)
#   canary_paused        - canary paused, all traffic stays on stable
#   stable_only          - no active canary, traffic served by stable
#   none_available       - no fully rolled-out version available
def route_request(db: sqlite3.Connection, service_name: str, user_id: str,
                  tenant_id: Optional[str] = None) -> dict:
    result = _route_decision(db, service_name, user_id, tenant_id)
    if tenant_id is None:
        # Keep the legacy response shape exactly: no tenant metadata is exposed
        # when the caller does not identify a tenant.
        result.pop("tenant_id", None)
        result.pop("rule", None)
    return result


def _route_decision(db: sqlite3.Connection, service_name: str, user_id: str,
                    tenant_id: Optional[str]) -> dict:
    canary = get_progressing_or_paused(db, service_name)
    stable = get_stable_release(db, service_name)
    bucket = cohort_bucket(service_name, user_id)

    allow, deny = (set(), set())
    if canary is not None:
        allow, deny = tenant_lists(canary)

    # Tenant targeting takes precedence over hash-based percentage split,
    # in the order: denylist -> allowlist -> stable-hash cohort.
    if canary is not None and tenant_id:
        if tenant_id in deny:
            return _stable_or_none(service_name, user_id, tenant_id, bucket,
                                   canary, stable, "tenant_denied")
        if tenant_id in allow and canary["status"] != "paused":
            return _route_hit(canary, user_id, bucket, stable, stable is not None,
                              tenant_id=tenant_id, rule="tenant_allowed")

    # A 100% canary is effectively the active version until it is marked complete.
    if canary is not None and canary["percentage"] >= 100 and canary["status"] != "paused":
        return _route_hit(canary, user_id, 100, stable, stable is not None,
                          tenant_id=tenant_id, rule="canary_full" if tenant_id else None)

    if canary is not None and canary["status"] == "progressing":
        if bucket < canary["percentage"]:
            return _route_hit(canary, user_id, bucket, stable, True,
                              tenant_id=tenant_id, rule="canary_cohort" if tenant_id else None)
        if stable is not None:
            return {
                "service": service_name, "user_id": user_id, "bucket": bucket,
                "tenant_id": tenant_id, "rule": "stable_cohort" if tenant_id else None,
                "cohort": "stable", "version": stable["version"], "release_id": stable["id"],
                "canary_version": canary["version"], "canary_percentage": canary["percentage"],
            }
        # No stable version yet: everyone outside the cohort has nothing.
        return {
            "service": service_name, "user_id": user_id, "bucket": bucket,
            "tenant_id": tenant_id, "rule": "none_available" if tenant_id else None,
            "cohort": "none", "version": None, "release_id": None,
            "canary_version": canary["version"], "canary_percentage": canary["percentage"],
        }

    # No active (progressing) canary: paused releases and absent releases both
    # leave all traffic on the stable version.
    if stable is not None:
        if canary is not None and canary["status"] == "paused":
            rule = "canary_paused" if tenant_id else None
        else:
            rule = "stable_only" if tenant_id else None
        return {
            "service": service_name, "user_id": user_id, "bucket": bucket,
            "tenant_id": tenant_id, "rule": rule,
            "cohort": "stable", "version": stable["version"], "release_id": stable["id"],
            "canary_version": None, "canary_percentage": None,
        }

    return {
        "service": service_name, "user_id": user_id,
        "bucket": bucket, "tenant_id": tenant_id,
        "rule": "none_available" if tenant_id else None,
        "cohort": "none", "version": None, "release_id": None,
        "canary_version": None, "canary_percentage": None,
    }


def _stable_or_none(service_name: str, user_id: str, tenant_id: str, bucket: int,
                    canary: sqlite3.Row, stable: Optional[sqlite3.Row], rule: str) -> dict:
    """Denylist outcome: serve stable when one exists, otherwise no version."""
    if stable is not None:
        return {
            "service": service_name, "user_id": user_id, "bucket": bucket,
            "tenant_id": tenant_id, "rule": rule,
            "cohort": "stable", "version": stable["version"], "release_id": stable["id"],
            "canary_version": canary["version"], "canary_percentage": canary["percentage"],
        }
    return {
        "service": service_name, "user_id": user_id, "bucket": bucket,
        "tenant_id": tenant_id, "rule": "none_available",
        "cohort": "none", "version": None, "release_id": None,
        "canary_version": canary["version"], "canary_percentage": canary["percentage"],
    }


def _route_hit(canary: sqlite3.Row, user_id: str, bucket: int,
               stable: Optional[sqlite3.Row], has_fallback: bool,
               tenant_id: Optional[str] = None, rule: Optional[str] = None) -> dict:
    return {
        "service": canary["service_name"], "user_id": user_id, "bucket": bucket,
        "tenant_id": tenant_id, "rule": rule,
        "cohort": "canary", "version": canary["version"], "release_id": canary["id"],
        "canary_version": canary["version"], "canary_percentage": canary["percentage"],
        "fallback_version": stable["version"] if has_fallback else None,
    }
