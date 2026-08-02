-- Modelo 145 declarations. Requires employee-service V14.
BEGIN;
DELETE FROM employee_tax_profile WHERE tenant_id IN ('acme','test');

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
    DATE '2026-01-01',
    DATE '2026-01-01',
    now(), now()
FROM employees e
CROSS JOIN LATERAL (
    SELECT EXTRACT(YEAR FROM age(DATE '2026-01-01', e.birth_date))::int AS v
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


UPDATE employee_tax_profile
   SET spouse_national_id = lpad(((abs(hashtext(employee_id::text || 'spouse')) % 89999999) + 10000000)::text, 8, '0')
                            || substr('TRWAGMYFPDXBNJZSQVHLCKE',
                                      (((abs(hashtext(employee_id::text || 'spouse')) % 89999999) + 10000000) % 23) + 1, 1)
 WHERE tenant_id = 'acme' AND family_situation = 2;

INSERT INTO employee_tax_profile (id, employee_id, tenant_id, family_situation, spouse_national_id, children_count, children_disabled_count, children_shared_custody, ascendants_count, ascendants_disabled_count, disability_percentage, reduced_mobility, geographic_mobility, spousal_support_annual, child_support_annual, housing_loan, valid_from, declared_at, created_at, updated_at) VALUES ('6c3a50e8-ce5a-45b9-81e9-7f66ded77f2b', 'df27b866-02a4-4e69-bef1-91db6d66d340', 'test', 3, NULL, 0, 0, false, 0, 0, 0, false, false, 0, 0, false, DATE '2026-01-01', DATE '2026-01-01', now(), now());
INSERT INTO employee_tax_profile (id, employee_id, tenant_id, family_situation, spouse_national_id, children_count, children_disabled_count, children_shared_custody, ascendants_count, ascendants_disabled_count, disability_percentage, reduced_mobility, geographic_mobility, spousal_support_annual, child_support_annual, housing_loan, valid_from, declared_at, created_at, updated_at) VALUES ('70fcf2ad-006e-43f5-85e1-8688e9653766', '4e9c572d-6f9a-478c-99cf-384bbb4a30b0', 'test', 1, NULL, 2, 0, false, 0, 0, 0, false, false, 0, 0, true, DATE '2026-01-01', DATE '2026-01-01', now(), now());
COMMIT;
