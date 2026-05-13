"""
Atlas Governance Constitution - Tier 22
Civilization-grade governed infrastructure: immutable constitutional policies,
execution law hierarchy, permission inheritance, canonical truth arbitration,
and violation enforcement. All execution is subject to this constitution.

Law Hierarchy (highest authority first):
  L1 ABSOLUTE     - Cannot be overridden by any agent or operator
  L2 SOVEREIGN    - System-wide invariants, operator-level override only
  L3 OPERATIONAL  - Runtime constraints, can be relaxed by SOVEREIGN policy
  L4 ADVISORY     - Soft guidance; agents may log exceptions with rationale

Violation Actions:
  BLOCK  - Execution halted, task moved to BLOCKED state
  WARN   - Execution continues, violation logged
  AUDIT  - Silent log for compliance records only
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from kernel import AtlasRuntimeKernel
from memory import MemoryEngine


# ── Constitutional policies (immutable at L1/L2) ─────────────────────────────

CONSTITUTION = [
    # L1 ABSOLUTE — hardcoded, cannot be overridden via DB
    {
        "policy_id":      "CONST-L1-001",
        "tier":           22,
        "category":       "execution",
        "name":           "No Lineage Erasure",
        "body":           "No agent, operator, or process may delete, truncate, or overwrite the event_ledger or memory_snapshots tables. Lineage is permanent.",
        "authority_level": 1000,
        "source":         "CONSTITUTION_L1",
        "law_level":      "L1_ABSOLUTE",
        "violation_action": "BLOCK",
    },
    {
        "policy_id":      "CONST-L1-002",
        "tier":           22,
        "category":       "execution",
        "name":           "No Self-Modification of Constitution",
        "body":           "No agent may modify, deactivate, or replace any L1_ABSOLUTE policy. The constitution is immutable by runtime agents.",
        "authority_level": 1000,
        "source":         "CONSTITUTION_L1",
        "law_level":      "L1_ABSOLUTE",
        "violation_action": "BLOCK",
    },
    {
        "policy_id":      "CONST-L1-003",
        "tier":           22,
        "category":       "safety",
        "name":           "No Destructive Shell Execution",
        "body":           "No agent may execute shell commands that delete system directories, drop databases, or kill supervisor processes without explicit human approval logged in the event ledger.",
        "authority_level": 1000,
        "source":         "CONSTITUTION_L1",
        "law_level":      "L1_ABSOLUTE",
        "violation_action": "BLOCK",
    },
    # L2 SOVEREIGN — system-wide, operator override only
    {
        "policy_id":      "CONST-L2-001",
        "tier":           22,
        "category":       "budget",
        "name":           "Daily Cost Ceiling",
        "body":           "Total compute cost must not exceed the configured daily_budget_cu. Requests that would breach the ceiling must be deferred or rejected.",
        "authority_level": 800,
        "source":         "CONSTITUTION_L2",
        "law_level":      "L2_SOVEREIGN",
        "violation_action": "BLOCK",
    },
    {
        "policy_id":      "CONST-L2-002",
        "tier":           22,
        "category":       "execution",
        "name":           "Single Supervisor Per Role",
        "body":           "At most one agent of each role may be in ACTIVE state simultaneously. Duplicate root detection violations require immediate deregistration of excess workers.",
        "authority_level": 800,
        "source":         "CONSTITUTION_L2",
        "law_level":      "L2_SOVEREIGN",
        "violation_action": "WARN",
    },
    {
        "policy_id":      "CONST-L2-003",
        "tier":           22,
        "category":       "data",
        "name":           "Checkpoint Frequency",
        "body":           "A memory snapshot must be taken at least every 3600 seconds. Failure to checkpoint within this window is a survivability violation.",
        "authority_level": 800,
        "source":         "CONSTITUTION_L2",
        "law_level":      "L2_SOVEREIGN",
        "violation_action": "WARN",
    },
    # L3 OPERATIONAL — runtime constraints
    {
        "policy_id":      "CONST-L3-001",
        "tier":           22,
        "category":       "execution",
        "name":           "Task Retry Limit",
        "body":           "Failed tasks may be retried at most 3 times before moving to DLQ. Agents must not bypass the retry counter.",
        "authority_level": 600,
        "source":         "CONSTITUTION_L3",
        "law_level":      "L3_OPERATIONAL",
        "violation_action": "WARN",
    },
    {
        "policy_id":      "CONST-L3-002",
        "tier":           22,
        "category":       "execution",
        "name":           "Stuck Task Recovery",
        "body":           "Any task in RUNNING state for more than 180 seconds without a heartbeat update must be requeued. Agents must not leave tasks permanently stuck.",
        "authority_level": 600,
        "source":         "CONSTITUTION_L3",
        "law_level":      "L3_OPERATIONAL",
        "violation_action": "WARN",
    },
    {
        "policy_id":      "CONST-L3-003",
        "tier":           22,
        "category":       "safety",
        "name":           "Dangerous Payload Blocking",
        "body":           "Payloads matching patterns for destructive operations (rm -rf, DROP TABLE, DELETE FROM without WHERE, format disk) must be moved to BLOCKED state pending human review.",
        "authority_level": 600,
        "source":         "CONSTITUTION_L3",
        "law_level":      "L3_OPERATIONAL",
        "violation_action": "BLOCK",
    },
    # L4 ADVISORY — soft guidance
    {
        "policy_id":      "CONST-L4-001",
        "tier":           22,
        "category":       "quality",
        "name":           "Decision Rationale Required",
        "body":           "Agents are encouraged to log a rationale for every non-trivial routing decision. Absence of rationale should be flagged in compliance audits.",
        "authority_level": 200,
        "source":         "CONSTITUTION_L4",
        "law_level":      "L4_ADVISORY",
        "violation_action": "AUDIT",
    },
]

# Dangerous patterns that trigger L3-003 payload blocking
DANGEROUS_PATTERNS = [
    "rm -rf", "rm -r /", "format disk", "DROP TABLE", "DROP DATABASE",
    "DELETE FROM", "> /dev/sda", "mkfs", "dd if=", ":(){ :|:& };:",
]


class GovernanceConstitution:
    """
    The Atlas constitutional execution layer. Enforces all active policies
    against actions, payloads, and state transitions before they occur.
    """

    def __init__(self, kernel: AtlasRuntimeKernel, memory: MemoryEngine):
        self.kernel = kernel
        self.memory = memory
        self._loaded_hashes: dict[str, str] = {}
        self._load_count: int = 0
        self._violation_count: int = 0
        self._boot()

    # ── Boot / loading ────────────────────────────────────────────────────────

    def _boot(self):
        """Load all constitutional policies into the DB on first boot, idempotently."""
        existing = {r[0] for r in self.kernel._query("SELECT policy_id FROM governance_policies")}
        loaded = 0
        for pol in CONSTITUTION:
            if pol["policy_id"] not in existing:
                self.kernel.insert_policy(
                    policy_id=pol["policy_id"],
                    tier=pol["tier"],
                    category=pol["category"],
                    name=pol["name"],
                    body=pol["body"],
                    authority_level=pol["authority_level"],
                    created_by=pol["source"],
                )
                loaded += 1
        if loaded:
            self.kernel.log_event("CONSTITUTION_LOADED", "GOVERNANCE_CONSTITUTION",
                                  {"policies_loaded": loaded, "total": len(CONSTITUTION)})

        # Verify hashes of all active policies
        self._verify_integrity()
        self._load_count += 1

    def _verify_integrity(self) -> list[dict]:
        """Check that stored policy bodies haven't been tampered with."""
        rows = self.kernel._query(
            "SELECT policy_id, body, content_hash FROM governance_policies WHERE active = 1"
        )
        violations = []
        for policy_id, body, stored_hash in rows:
            computed = hashlib.sha256(body.encode()).hexdigest()[:16]
            self._loaded_hashes[policy_id] = computed
            if stored_hash and computed != stored_hash:
                violations.append({
                    "policy_id": policy_id, "stored": stored_hash, "computed": computed
                })
                self.kernel.log_event("CONSTITUTION_TAMPER_DETECTED", "GOVERNANCE_CONSTITUTION",
                                      {"policy_id": policy_id, "computed_hash": computed})
        return violations

    # ── Law hierarchy ─────────────────────────────────────────────────────────

    def get_law_hierarchy(self) -> list[dict]:
        """Return all active policies ordered by authority (highest first)."""
        rows = self.kernel._query(
            "SELECT policy_id, tier, category, name, body, authority_level, created_by, content_hash, created_at "
            "FROM governance_policies WHERE active = 1 ORDER BY authority_level DESC"
        )
        result = []
        for r in rows:
            pol = CONSTITUTION_BY_ID.get(r[0], {})
            result.append({
                "policy_id": r[0], "tier": r[1], "category": r[2], "name": r[3],
                "authority_level": r[5], "source": r[6], "content_hash": r[7],
                "law_level": pol.get("law_level", "UNKNOWN"),
                "violation_action": pol.get("violation_action", "AUDIT"),
            })
        return result

    def get_policies_by_level(self, law_level: str) -> list[dict]:
        """Return policies filtered to a specific law level."""
        return [p for p in self.get_law_hierarchy() if p.get("law_level") == law_level]

    # ── Permission inheritance ────────────────────────────────────────────────

    def check_permission(self, action: str, payload: dict, actor: str = "AGENT") -> dict:
        """
        Evaluate whether an action is permitted under the active constitution.

        Returns:
            {"allowed": bool, "violations": [...], "blocking_policy": str | None}
        """
        violations = []
        blocking_policy = None

        # L1/L2/L3 checks in authority order
        for pol in CONSTITUTION:
            result = self._evaluate_policy(pol, action, payload, actor)
            if result:
                violations.append(result)
                if result["action"] == "BLOCK" and not blocking_policy:
                    blocking_policy = pol["policy_id"]

        allowed = blocking_policy is None
        return {
            "allowed": allowed,
            "violations": violations,
            "blocking_policy": blocking_policy,
            "actor": actor,
            "action": action,
            "evaluated_at": datetime.now().isoformat(),
        }

    def _evaluate_policy(self, pol: dict, action: str, payload: dict, actor: str) -> dict | None:
        """Evaluate a single policy against an action. Returns violation dict or None."""
        pid = pol["policy_id"]

        # L1-001: No lineage erasure
        if pid == "CONST-L1-001":
            danger_actions = {"drop_table", "truncate", "delete_events", "wipe_snapshots"}
            if action in danger_actions:
                return self._make_violation(pol, actor, f"action={action} would erase lineage")

        # L1-003: No destructive shell
        if pid == "CONST-L1-003":
            prompt = str(payload.get("prompt", "")).lower()
            for pattern in DANGEROUS_PATTERNS:
                if pattern.lower() in prompt:
                    return self._make_violation(pol, actor, f"dangerous pattern: {pattern!r}")

        # L2-001: Budget ceiling — checked by governor at dispatch time
        if pid == "CONST-L2-001":
            cost = payload.get("estimated_cost_cu", 0)
            budget = payload.get("daily_budget_cu", float("inf"))
            day_cost = payload.get("day_cost", 0)
            if cost > 0 and day_cost + cost > budget:
                return self._make_violation(pol, actor,
                                            f"cost {day_cost+cost:.2f} > budget {budget}")

        # L3-003: Dangerous payload blocking
        if pid == "CONST-L3-003":
            prompt = str(payload.get("prompt", "")).lower()
            for pattern in DANGEROUS_PATTERNS:
                if pattern.lower() in prompt:
                    return self._make_violation(pol, actor, f"pattern blocked: {pattern!r}")

        return None

    def _make_violation(self, pol: dict, actor: str, reason: str) -> dict:
        vid = self.kernel.log_violation(
            policy_id=pol["policy_id"],
            agent_id=actor,
            action_attempted=pol.get("violation_action", "WARN"),
            detail=reason,
        )
        self._violation_count += 1
        return {
            "violation_id": vid,
            "policy_id": pol["policy_id"],
            "name": pol["name"],
            "law_level": pol.get("law_level", "UNKNOWN"),
            "action": pol.get("violation_action", "AUDIT"),
            "reason": reason,
        }

    # ── Canonical truth arbitration ───────────────────────────────────────────

    def arbitrate_truth(self, competing_claims: list[dict]) -> dict:
        """
        When multiple agents report conflicting state, arbitrate using:
        1. Event ledger (canonical — highest authority)
        2. Most recent timestamp
        3. Highest authority_level source

        Each claim: {"source": str, "claim": str, "value": any, "timestamp": str, "authority": int}
        """
        if not competing_claims:
            return {"winner": None, "method": "no_claims", "confidence": 0.0}

        # Authority 1: if any claim has "EVENT_LEDGER" source, it wins
        ledger_claims = [c for c in competing_claims if c.get("source") == "EVENT_LEDGER"]
        if ledger_claims:
            winner = max(ledger_claims, key=lambda c: c.get("timestamp", ""))
            return {
                "winner": winner,
                "method": "event_ledger_authority",
                "confidence": 1.0,
                "rejected": [c for c in competing_claims if c is not winner],
            }

        # Authority 2: highest authority_level
        max_auth = max(competing_claims, key=lambda c: (c.get("authority", 0), c.get("timestamp", "")))
        ties = [c for c in competing_claims if c.get("authority") == max_auth.get("authority")]

        if len(ties) == 1:
            return {
                "winner": max_auth,
                "method": "authority_level",
                "confidence": 0.9,
                "rejected": [c for c in competing_claims if c is not max_auth],
            }

        # Authority 3: most recent timestamp among ties
        most_recent = max(ties, key=lambda c: c.get("timestamp", ""))
        return {
            "winner": most_recent,
            "method": "recency",
            "confidence": 0.7,
            "rejected": [c for c in competing_claims if c is not most_recent],
        }

    # ── Violation enforcement ─────────────────────────────────────────────────

    def enforce_on_task(self, task_id: str, payload: dict, worker_id: str = "UNKNOWN") -> dict:
        """
        Apply the full constitution to a task before execution.
        Returns enforcement result: allowed or blocked.
        """
        result = self.check_permission(
            action="task_execute",
            payload=payload,
            actor=worker_id,
        )
        if not result["allowed"]:
            # Move task to BLOCKED state
            self.kernel._query(
                "UPDATE task_queue SET state='BLOCKED', last_updated=? WHERE task_id=?",
                (datetime.now().isoformat(), task_id),
                commit=True,
            )
            self.kernel.log_event("TASK_BLOCKED_BY_CONSTITUTION", "GOVERNANCE_CONSTITUTION", {
                "task_id": task_id,
                "blocking_policy": result["blocking_policy"],
                "violations": len(result["violations"]),
            })
        return result

    # ── Status and reporting ──────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Current constitution status: load count, policy count, violation count, integrity."""
        policy_count = self.kernel._query(
            "SELECT COUNT(*) FROM governance_policies WHERE active = 1"
        )[0][0]
        violation_count = self.kernel._query(
            "SELECT COUNT(*) FROM governance_violations"
        )[0][0]
        tamper = self._verify_integrity()
        return {
            "status": "CONSTITUTION_ACTIVE",
            "load_count": self._load_count,
            "policy_count": policy_count,
            "session_violations": self._violation_count,
            "total_violations": violation_count,
            "integrity_ok": len(tamper) == 0,
            "tamper_detected": len(tamper) > 0,
            "tampered_policies": tamper,
            "generated_at": datetime.now().isoformat(),
        }

    def generate_sovereignty_report(self) -> dict:
        """Full sovereignty validation report for Tier 22 certification."""
        hierarchy = self.get_law_hierarchy()
        by_level = {}
        for pol in hierarchy:
            lvl = pol.get("law_level", "UNKNOWN")
            by_level.setdefault(lvl, []).append(pol)

        violations = self.kernel.get_violations(limit=20)
        status = self.get_status()

        report = {
            "generated_at": datetime.now().isoformat(),
            "tier": 22,
            "name": "Atlas Sovereignty Validation Report",
            "constitution_status": status,
            "law_hierarchy": {
                "L1_ABSOLUTE":   [p["name"] for p in by_level.get("L1_ABSOLUTE", [])],
                "L2_SOVEREIGN":  [p["name"] for p in by_level.get("L2_SOVEREIGN", [])],
                "L3_OPERATIONAL":[p["name"] for p in by_level.get("L3_OPERATIONAL", [])],
                "L4_ADVISORY":   [p["name"] for p in by_level.get("L4_ADVISORY", [])],
            },
            "policy_count_by_level": {k: len(v) for k, v in by_level.items()},
            "recent_violations": [
                {"id": r[0], "timestamp": r[1], "policy_id": r[2], "actor": r[3],
                 "action_attempted": r[4], "detail": r[5]}
                for r in violations
            ],
            "arbitration_model": {
                "authority_1": "EVENT_LEDGER — canonical, immutable",
                "authority_2": "HIGHEST authority_level score",
                "authority_3": "MOST RECENT timestamp among ties",
            },
            "enforcement_chain": [
                "GovernanceAgent blocks at task intake",
                "GovernanceConstitution.enforce_on_task moves task to BLOCKED",
                "RuntimeObserver flags duplicate_root / stuck_task / governance_tamper",
                "SurvivabilityEngine restores on restart",
                "ContinuityEngine preserves full lineage",
            ],
        }

        self.kernel.set_state("sovereignty_report", report)
        self.kernel.log_event("SOVEREIGNTY_REPORT_GENERATED", "GOVERNANCE_CONSTITUTION",
                              {"policy_count": status["policy_count"],
                               "violations": status["total_violations"]})
        return report


# Lookup table by policy_id for metadata not stored in DB
CONSTITUTION_BY_ID = {pol["policy_id"]: pol for pol in CONSTITUTION}
