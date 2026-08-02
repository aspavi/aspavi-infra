#!/usr/bin/env python3
"""
Modelo 145 declarations for the acme and test workforce.

Emits 8_tax_profile.sql. Requires V14 of employee-service (the
employee_tax_profile table).

The declarations are derived from data already in the database — marital
status and age — so they never contradict the rest of the file: nobody
married in the employees table is filing as single-parent, and only people
old enough have children of a plausible age. Deterministic, like the rest of
the dataset.
"""
import random
import uuid
from pathlib import Path

rng = random.Random(145)
OUT = Path(__file__).parent / '8_tax_profile.sql'
NOW = "2026-01-01"


def u() -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def q(v) -> str:
    if v is None:
        return 'NULL'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def dni_letter(n: int) -> str:
    return "TRWAGMYFPDXBNJZSQVHLCKE"[n % 23]


def spouse_dni(seed: int) -> str:
    n = 10000000 + (seed * 7919) % 89999999
    return f"{n}{dni_letter(n)}"


sql = [
    "-- Modelo 145 declarations. Requires employee-service V14.",
    "BEGIN;",
    "DELETE FROM employee_tax_profile WHERE tenant_id IN ('acme','test');",
]

# Pull marital status and age straight from the employees table so the
# declaration cannot disagree with the file it belongs to.
rows = """
    SELECT id, marital_status, EXTRACT(YEAR FROM age(DATE '2026-01-01', birth_date))::int AS age
    FROM employees WHERE tenant_id = 'acme' ORDER BY id
"""

# Rather than hard-code each employee, generate one INSERT ... SELECT that
# decides the situation from marital_status. The counts below are the only
# invented part, and they are seeded per employee id so they stay stable.
sql.append(f"""
INSERT INTO employee_tax_profile (
    id, employee_id, tenant_id, family_situation, spouse_national_id,
    children_count, children_disabled_count, children_shared_custody,
    ascendants_count, ascendants_disabled_count,
    disability_percentage, reduced_mobility, geographic_mobility,
    spousal_support_annual, child_support_annual, housing_loan,
    valid_from, declared_at, created_at, updated_at)
SELECT
    gen_random_uuid(),
    e.id,
    'acme',
    -- Situación 2 requires a spouse without income; we only assume it for a
    -- minority of the married, since two-income households are the norm.
    CASE
        WHEN e.marital_status = 'MARRIED' AND (hashtext(e.id::text) % 5) = 0 THEN 2
        WHEN e.marital_status IN ('DIVORCED','WIDOWED','SINGLE') AND kids.n > 0 THEN 1
        ELSE 3
    END,
    NULL,
    kids.n,
    0,
    -- Shared custody only makes sense for a separated parent.
    (e.marital_status = 'DIVORCED' AND kids.n > 0 AND (hashtext(e.id::text) % 2) = 0),
    CASE WHEN age.v >= 48 AND (hashtext(e.id::text) % 7) = 0 THEN 1 ELSE 0 END,
    0,
    CASE WHEN (hashtext(e.id::text) % 23) = 0 THEN 33 ELSE 0 END,
    false,
    false,
    0, 0,
    -- Pre-2013 mortgage: only plausible for people who were already adults then.
    (age.v >= 40 AND (hashtext(e.id::text) % 4) = 0),
    DATE '{NOW}',
    DATE '{NOW}',
    now(), now()
FROM employees e
CROSS JOIN LATERAL (
    SELECT EXTRACT(YEAR FROM age(DATE '{NOW}', e.birth_date))::int AS v
) age
CROSS JOIN LATERAL (
    SELECT CASE
        -- Nobody under 28 has children here, and family size tracks both age
        -- and marital status rather than being uniformly random.
        WHEN age.v < 28 THEN 0
        WHEN e.marital_status IN ('MARRIED','PARTNER') THEN LEAST(3, (abs(hashtext(e.id::text)) % 3) + (CASE WHEN age.v >= 40 THEN 1 ELSE 0 END))
        WHEN e.marital_status IN ('DIVORCED','WIDOWED') THEN (abs(hashtext(e.id::text)) % 2) + 1
        ELSE CASE WHEN (abs(hashtext(e.id::text)) % 4) = 0 THEN 1 ELSE 0 END
    END AS n
) kids
WHERE e.tenant_id = 'acme';
""")

# Situación 2 needs the spouse's NIF — that is what identifies it, and the
# CHECK constraint only permits it there.
sql.append("""
UPDATE employee_tax_profile
   SET spouse_national_id = lpad(((abs(hashtext(employee_id::text || 'spouse')) % 89999999) + 10000000)::text, 8, '0')
                            || substr('TRWAGMYFPDXBNJZSQVHLCKE',
                                      (((abs(hashtext(employee_id::text || 'spouse')) % 89999999) + 10000000) % 23) + 1, 1)
 WHERE tenant_id = 'acme' AND family_situation = 2;
""")

# The two test-tenant employees, declared explicitly.
JUAN = 'df27b866-02a4-4e69-bef1-91db6d66d340'
PACO = '4e9c572d-6f9a-478c-99cf-384bbb4a30b0'
for emp, situation, kids, spouse, loan in (
    (JUAN, 3, 0, None, False),
    (PACO, 1, 2, None, True),
):
    sql.append(
        "INSERT INTO employee_tax_profile (id, employee_id, tenant_id, family_situation, "
        "spouse_national_id, children_count, children_disabled_count, children_shared_custody, "
        "ascendants_count, ascendants_disabled_count, disability_percentage, reduced_mobility, "
        "geographic_mobility, spousal_support_annual, child_support_annual, housing_loan, "
        "valid_from, declared_at, created_at, updated_at) VALUES ("
        f"{q(u())}, {q(emp)}, 'test', {situation}, {q(spouse)}, {kids}, 0, false, 0, 0, 0, false, "
        f"false, 0, 0, {q(loan)}, DATE '{NOW}', DATE '{NOW}', now(), now());")

sql.append("COMMIT;")
OUT.write_text("\n".join(sql) + "\n")
print(f"{OUT.name}: {OUT.stat().st_size:,} bytes")
