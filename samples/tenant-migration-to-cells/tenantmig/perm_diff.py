"""Permissions diff harness: same (user, resource, action) questions, old model vs new model.

The old decision is evaluated independently in Python straight from V1 auth (not via the
transform's SQL), so a bug in the transform cannot hide itself. Every disagreement is listed.
"""
import itertools

import psycopg

from tenantmig import settings


def _old_decision(perms: list[tuple[str, str, str]], resource: str, action: str) -> bool:
    matching = [eff for (res, act, eff) in perms if res in (resource, "*") and act in (action, "*")]
    return "allow" in matching and "deny" not in matching


def diff_org(org_id: str) -> dict:
    with psycopg.connect(settings.V1_AUTH_DSN) as auth:
        users = [r[0] for r in auth.execute("SELECT id FROM users WHERE org_id = %s ORDER BY id", (org_id,))]
        rows = auth.execute("""
            SELECT ur.user_id, rp.resource, rp.action, rp.effect
            FROM user_roles ur JOIN roles r ON r.id = ur.role_id AND r.org_id = %s
            JOIN role_permissions rp ON rp.role_id = r.id""", (org_id,)).fetchall()
    perms: dict[int, list] = {}
    for uid, res, act, eff in rows:
        perms.setdefault(uid, []).append((res, act, eff))
    resources = sorted({r[1] for r in rows if r[1] != "*"})
    actions = sorted({r[2] for r in rows if r[2] != "*"})

    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as mgmt:
        granted = set(mgmt.execute("""
            SELECT g.principal_id, g.resource, g.action FROM grants g
            JOIN principals p ON p.id = g.principal_id WHERE p.org_id = %s""", (org_id,)).fetchall())

    disagreements = []
    checked = 0
    for uid, res, act in itertools.product(users, resources, actions):
        checked += 1
        old = _old_decision(perms.get(uid, []), res, act)
        new = (uid, res, act) in granted
        if old != new:
            disagreements.append({"user_id": uid, "resource": res, "action": act,
                                  "v1": "allow" if old else "deny", "v2": "allow" if new else "deny"})
    return {"org_id": org_id, "decisions_checked": checked, "disagreements": disagreements}
