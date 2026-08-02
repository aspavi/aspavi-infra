#!/usr/bin/env python3
"""
Demo-data generator for the aspavi cluster databases.

Emits one SQL file per database beside this script. Deterministic (seeded
RNG) so a re-run produces identical statements. Every script is transactional and
deletes only what it itself generates (scoped to tenants acme/test on tables
that were verified empty, or to fixed ids it owns).
"""
import random
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

rng = random.Random(2026)
OUT = Path(__file__).parent

NOW = "2026-07-30 17:00:00+00"
TODAY = date(2026, 7, 30)


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


def make_dni(seedn: int) -> str:
    n = 10000000 + (seedn * 4797) % 89999999
    return f"{n}{dni_letter(n)}"


def make_nie(seedn: int) -> str:
    n = 1000000 + (seedn * 4797) % 8999999
    return f"Y{n}{dni_letter(int('1' + str(n)))}"  # Y maps to 1 for the checksum


def make_iban(seedn: int) -> str:
    r = random.Random(seedn)
    bank = r.choice(['2100', '0049', '0182', '1465', '2085'])
    branch = f"{r.randrange(10000):04d}"
    dc = f"{r.randrange(100):02d}"
    acct = f"{r.randrange(10**10):010d}"
    bban = bank + branch + dc + acct
    # IBAN check digits: mod 97 over BBAN + 'ES00' translated.
    check = 98 - int(bban + '142800') % 97  # E=14, S=28
    return f"ES{check:02d}{bban}"


# ═══════════════════════ Reference ids (from the live DB) ═══════════════════

AG_OFI = '11111111-1111-1111-1111-111111111111'   # Oficinas y Despachos VLC, 23 días laborables, 14 pagas
AG_SAN = '22222222-2222-2222-2222-222222222222'   # Sanidad Privada, 30 días naturales, 14 pagas

# Full-time week each agreement fixes, straight from collective_agreement.
# A contract may not exceed it, and the FTE is measured against it — so the
# part-timers below are written as a fraction of this, never as loose hours.
FULL_WEEK = {AG_OFI: 38.5, AG_SAN: 37.0}

CAT = {  # category id, annual base, group
    'recepcionista': ('11111111-0001-0000-0000-000000000001', 16800, 'IV'),
    'aux_admin':     ('11111111-0001-0000-0000-000000000002', 17500, 'IV'),
    'admin':         ('11111111-0001-0000-0000-000000000003', 19800, 'III'),
    'oficial':       ('11111111-0001-0000-0000-000000000004', 22400, 'III'),
    'contable':      ('11111111-0001-0000-0000-000000000005', 26500, 'II'),
    'jefe_admin':    ('11111111-0001-0000-0000-000000000006', 34000, 'I'),
    'celador':       ('22222222-0001-0000-0000-000000000001', 17200, 'V'),
    'tcae':          ('22222222-0001-0000-0000-000000000002', 19400, 'IV'),
    'due':           ('22222222-0001-0000-0000-000000000005', 28500, 'II'),
    'fisio':         ('22222222-0001-0000-0000-000000000004', 26900, 'II'),
    'supervisor':    ('22222222-0001-0000-0000-000000000006', 33200, 'I'),
}

POS = {
    'recepcionista': 'b0000000-0000-0000-0000-000000000001',
    'administrativo': 'b0000000-0000-0000-0000-000000000002',
    'contable': 'b0000000-0000-0000-0000-000000000003',
    'resp_admin': 'b0000000-0000-0000-0000-000000000004',
    'cuidador': 'b0000000-0000-0000-0000-000000000005',
    'aux_enfermeria': 'b0000000-0000-0000-0000-000000000006',
    'fisio': 'b0000000-0000-0000-0000-000000000007',
    'enfermero': 'b0000000-0000-0000-0000-000000000008',
    'director': 'b0000000-0000-0000-0000-000000000009',
    'monitor': 'b0000000-0000-0000-0000-000000000010',
}

CENTER = {
    'sede_vlc': 'c0000000-0000-0000-0000-000000000001',
    'la_marina': 'c0000000-0000-0000-0000-000000000002',
    'residencia': 'c0000000-0000-0000-0000-000000000003',
    'turia': 'c0000000-0000-0000-0000-000000000004',
    'castello': 'c0000000-0000-0000-0000-000000000005',
    'madrid': '20000001-0000-0000-0000-000000000001',
    'barcelona': '20000001-0000-0000-0000-000000000002',
    'sevilla': '20000001-0000-0000-0000-000000000003',
}

LOCALITY = {
    'vlc': 'c5b418f3-fc89-4273-839c-e50956b4377e',
    'gandia': '4e46cb99-a700-4f0a-8182-024ff5ebf7f1',
    'paterna': '766805b7-bf6b-4262-a230-dd43b9cee322',
    'castello': '2b8b0262-bb14-4ba4-b2f2-af5a2b0c699a',
}

# Fixed ids for the calendars this script owns (safe to re-run).
CAL = {
    'vlc_ofi': 'ca1e0000-0000-4000-8000-000000000001',
    'vlc_san': 'ca1e0000-0000-4000-8000-000000000002',
    'gandia': 'ca1e0000-0000-4000-8000-000000000003',
    'paterna': 'ca1e0000-0000-4000-8000-000000000004',
    'castello': 'ca1e0000-0000-4000-8000-000000000005',
    'madrid': '7e0aa899-b57e-4390-8b9d-8be92bc1b408',
    'barcelona': '9d297d65-19b9-41a5-9029-7c027925b4bd',
    'sevilla': '36839ac7-e695-4c7e-80c5-e8a1cd9ad45a',
}

CENTER_CAL = {
    'sede_vlc': 'vlc_ofi', 'la_marina': 'vlc_san', 'residencia': 'gandia',
    'turia': 'paterna', 'castello': 'castello', 'madrid': 'madrid',
    'barcelona': 'barcelona', 'sevilla': 'sevilla',
}

ABS = {  # acme absence type ids
    'VACACIONES': '750e8061-6c47-482d-983d-4c5e356fab78',
    'ASUNTOS_PROPIOS': '35185d08-dc44-4826-856d-c8f9f49ca549',
    'BAJA_MEDICA': '8b8deb27-9970-45e0-8a10-ae995584a7b0',
    'MATRIMONIO': 'f1901520-baf4-4d53-8111-5d7f5625de81',
    'FALLECIMIENTO': 'eab4ea34-c85f-4683-b758-1edbfa52aea4',
    'MUDANZA': 'de00d45a-6982-4bfd-97f2-48145b9af4d5',
    'FORMACION': '2e88a4ba-1299-4fc3-8113-9222c0f3903c',
}

# CV holidays 2026 for the new calendars (national set + autonómicos).
HOLIDAYS_CV = [
    ('2026-01-01', 'Año Nuevo', 'NACIONAL'),
    ('2026-01-06', 'Epifanía del Señor', 'NACIONAL'),
    ('2026-03-19', 'San José', 'AUTONOMICO'),
    ('2026-04-03', 'Viernes Santo', 'NACIONAL'),
    ('2026-04-06', 'Lunes de Pascua', 'AUTONOMICO'),
    ('2026-05-01', 'Fiesta del Trabajo', 'NACIONAL'),
    ('2026-08-15', 'Asunción de la Virgen', 'NACIONAL'),
    ('2026-10-09', 'Día de la Comunitat Valenciana', 'AUTONOMICO'),
    ('2026-10-12', 'Fiesta Nacional de España', 'NACIONAL'),
    ('2026-11-01', 'Todos los Santos', 'NACIONAL'),
    ('2026-12-06', 'Día de la Constitución Española', 'NACIONAL'),
    ('2026-12-08', 'Inmaculada Concepción', 'NACIONAL'),
    ('2026-12-25', 'Natividad del Señor', 'NACIONAL'),
]
CV_HOLIDAY_DATES = {h[0] for h in HOLIDAYS_CV}


def laborables(start: date, end: date) -> int:
    """Working days in range, excluding weekends and CV holidays."""
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in CV_HOLIDAY_DATES:
            n += 1
        d += timedelta(days=1)
    return n


def naturales(start: date, end: date) -> int:
    return (end - start).days + 1


# ═══════════════════════ The 28 acme employees ═══════════════════════
# (id, first, last, gender, center, position, category, convenio, weekly_hours,
#  contract_type, plus_codes, variable, marital, hire)
E = 'e0000001-0000-4000-8000-0000000000'


def emp(idsuf, first, last, gender, center, pos, cat, conv, fte, ctype,
        pluses, variable, marital, hire, *, preferred=None, nationality='Española',
        nie=False, seniority=None, dept=None):
    """`fte` is the fraction of the agreement's full-time week, not hours:
    hours follow from it, so nobody can end up contracted above the cap."""
    return dict(
        id=(E + idsuf) if len(idsuf) == 2 else idsuf,
        first=first, last=last, gender=gender, center=center, pos=pos, cat=cat,
        conv=conv, fte=round(fte, 2), weekly=round(FULL_WEEK[conv] * fte, 2),
        ctype=ctype, pluses=pluses, variable=variable,
        marital=marital, hire=date.fromisoformat(hire), preferred=preferred,
        nationality=nationality, nie=nie,
        seniority=date.fromisoformat(seniority) if seniority else date.fromisoformat(hire),
        dept=dept,
    )


EMPLOYEES = [
    # ── Sede Central València · oficinas ──
    emp('11', 'Sofía', 'Álvarez Muñoz', 'FEMALE', 'sede_vlc', 'resp_admin', 'jefe_admin', AG_OFI, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_RESPONSABILIDAD'], 4000, 'MARRIED', '2020-04-01',
        preferred='Sofi', dept='Administración'),
    emp('13', 'Carmen', 'Navarro Domínguez', 'FEMALE', 'sede_vlc', 'administrativo', 'oficial', AG_OFI, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_IDIOMAS'], None, 'MARRIED', '2019-11-04', dept='Administración'),
    emp('15', 'Lucía', 'Domínguez Torres', 'FEMALE', 'sede_vlc', 'contable', 'contable', AG_OFI, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_CONVENIO'], None, 'SINGLE', '2020-02-03', dept='Administración'),
    emp('16', 'Marcos', 'Vázquez Ruiz', 'MALE', 'sede_vlc', 'contable', 'contable', AG_OFI, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_CONVENIO'], None, 'PARTNER', '2020-08-03', dept='Administración'),
    emp('17', 'Isabel', 'Ramos Martín', 'FEMALE', 'sede_vlc', 'administrativo', 'admin', AG_OFI, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE'], None, 'SINGLE', '2023-02-01', preferred='Isa', dept='Administración'),
    emp('14', 'Javier', 'Morales Jiménez', 'MALE', 'sede_vlc', 'administrativo', 'admin', AG_OFI, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE'], None, 'SINGLE', '2022-03-01', dept='Administración'),
    # ── Centro de Día La Marina · sanidad ──
    emp('01', 'María', 'García López', 'FEMALE', 'la_marina', 'director', 'supervisor', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_RESPONSABILIDAD'], 3000, 'MARRIED', '2019-03-01',
        seniority='2018-06-01', dept='Dirección'),
    emp('02', 'Carlos', 'Martínez Ruiz', 'MALE', 'la_marina', 'cuidador', 'celador', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_TURNICIDAD'], None, 'MARRIED', '2019-06-01',
        seniority='2019-01-07', dept='Atención directa'),
    emp('03', 'Elena', 'Rodríguez Fernández', 'FEMALE', 'la_marina', 'aux_enfermeria', 'tcae', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_TURNICIDAD'], None, 'DIVORCED', '2020-01-15', dept='Atención directa'),
    emp('04', 'Ana', 'González Pérez', 'FEMALE', 'la_marina', 'monitor', 'celador', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE'], None, 'SINGLE', '2020-09-01', dept='Atención directa'),
    emp('05', 'David', 'López Sánchez', 'MALE', 'la_marina', 'cuidador', 'celador', AG_SAN, 1.0,
        'TEMPORAL', ['PLUS_TRANSPORTE', 'PLUS_TURNICIDAD'], None, 'SINGLE', '2021-03-01', dept='Atención directa'),
    # ── Residencia Mediterráneo Gandia · sanidad ──
    emp('06', 'Francisco', 'Hernández Martínez', 'MALE', 'residencia', 'director', 'supervisor', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_RESPONSABILIDAD'], 3000, 'MARRIED', '2021-01-15', dept='Dirección'),
    emp('07', 'Laura', 'Gómez Díaz', 'FEMALE', 'residencia', 'enfermero', 'due', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_TURNICIDAD', 'PLUS_ESPECIALIDAD'], None, 'PARTNER', '2022-02-01',
        dept='Atención sanitaria'),
    emp('08', 'Sergio', 'Castillo Blanco', 'MALE', 'residencia', 'enfermero', 'due', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_TURNICIDAD', 'PLUS_ESPECIALIDAD'], None, 'SINGLE', '2022-06-01',
        dept='Atención sanitaria'),
    emp('09', 'Beatriz', 'Vargas Romero', 'FEMALE', 'residencia', 'aux_enfermeria', 'tcae', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_TURNICIDAD', 'PLUS_FESTIVOS'], None, 'SINGLE', '2023-09-04',
        preferred='Bea', dept='Atención directa'),
    emp('10', 'Miguel', 'Torres Romero', 'MALE', 'residencia', 'aux_enfermeria', 'tcae', AG_SAN, 1.0,
        'TEMPORAL', ['PLUS_TRANSPORTE', 'PLUS_TURNICIDAD'], None, 'SINGLE', '2023-01-16', dept='Atención directa'),
    emp('22', 'Fernando', 'Ortega García', 'MALE', 'residencia', 'fisio', 'fisio', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_ESPECIALIDAD'], None, 'WIDOWED', '2020-07-01',
        dept='Atención terapéutica'),
    emp('23', 'Raquel', 'Romero Jiménez', 'FEMALE', 'residencia', 'cuidador', 'celador', AG_SAN, 0.8,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_TURNICIDAD'], None, 'MARRIED', '2021-11-02', dept='Atención directa'),
    emp('24', 'Alberto', 'Medina Torres', 'MALE', 'residencia', 'cuidador', 'celador', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_TURNICIDAD', 'PLUS_FESTIVOS'], None, 'PARTNER', '2022-08-01',
        dept='Atención directa'),
    # ── Centro Ocupacional Túria Paterna · sanidad ──
    emp('12', 'Pablo', 'Jiménez Moreno', 'MALE', 'turia', 'director', 'supervisor', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_RESPONSABILIDAD'], 2500, 'MARRIED', '2021-07-01', dept='Dirección'),
    emp('19', 'Roberto', 'Serrano García', 'MALE', 'turia', 'monitor', 'celador', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE'], None, 'SINGLE', '2020-10-01', dept='Atención directa'),
    emp('20', 'Pilar', 'Blanco López', 'FEMALE', 'turia', 'monitor', 'celador', AG_SAN, 0.5,
        'INDEFINIDO', ['PLUS_TRANSPORTE'], None, 'MARRIED', '2021-09-01', dept='Atención directa'),
    emp('21', 'Alejandro', 'Molina Sanz', 'MALE', 'turia', 'fisio', 'fisio', AG_SAN, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE', 'PLUS_ESPECIALIDAD'], None, 'MARRIED', '2023-03-01',
        preferred='Álex', dept='Atención terapéutica'),
    emp('25', 'Natalia', 'Delgado Vega', 'FEMALE', 'turia', 'cuidador', 'celador', AG_SAN, 1.0,
        'TEMPORAL', ['PLUS_TRANSPORTE', 'PLUS_TURNICIDAD'], None, 'SINGLE', '2023-06-05',
        nationality='Argentina', nie=True, dept='Atención directa'),
    # ── Delegación Castelló · oficinas ──
    emp('18', 'Cristina', 'Castro Morales', 'FEMALE', 'castello', 'administrativo', 'admin', AG_OFI, 1.0,
        'INDEFINIDO', ['PLUS_TRANSPORTE'], None, 'MARRIED', '2021-05-03', dept='Administración'),
    emp('6ce85cae-4bac-4e58-9dea-232ebabdfacc', 'Andres', 'Lopez', 'MALE', 'castello', 'recepcionista',
        'recepcionista', AG_OFI, 1.0, 'INDEFINIDO', ['PLUS_TRANSPORTE'], None, 'SINGLE', '2026-03-23',
        dept='Administración'),
    # ── Oficinas Madrid / Barcelona · oficinas ──
    emp('637b9e22-17fa-4433-9329-802ad76652ca', 'Pablo', 'Carrascal', 'MALE', 'madrid', 'administrativo',
        'oficial', AG_OFI, 1.0, 'INDEFINIDO', ['PLUS_TRANSPORTE'], None, 'SINGLE', '2026-03-23',
        dept='Administración'),
    emp('4804acda-e3e3-4279-83fd-82a0ebb663b7', 'Javier', 'Tebas', 'MALE', 'barcelona', 'administrativo',
        'admin', AG_OFI, 1.0, 'INDEFINIDO', ['PLUS_TRANSPORTE'], None, 'PARTNER', '2026-03-23',
        dept='Administración'),
]
assert len(EMPLOYEES) == 28, len(EMPLOYEES)
BY_SUF = {e['id']: e for e in EMPLOYEES}


def eid(suf: str) -> str:
    return E + suf


# Managers: center staff → their director; directors and staff functions → Sofía.
DIRECTOR_OF = {'la_marina': eid('01'), 'residencia': eid('06'), 'turia': eid('12')}
TOP = eid('11')  # Sofía


def manager_of(e) -> str | None:
    if e['id'] == TOP:
        return None
    d = DIRECTOR_OF.get(e['center'])
    if d and d != e['id']:
        return d
    return TOP


CITY = {
    'sede_vlc': ('València', 'València', ['46001', '46004', '46008', '46015', '46021']),
    'la_marina': ('València', 'València', ['46011', '46022', '46023']),
    'residencia': ('Gandia', 'València', ['46701', '46702']),
    'turia': ('Paterna', 'València', ['46980', '46988']),
    'castello': ('Castelló de la Plana', 'Castelló', ['12001', '12004', '12006']),
    'madrid': ('Madrid', 'Madrid', ['28001', '28010', '28045']),
    'barcelona': ('Barcelona', 'Barcelona', ['08001', '08015', '08025']),
}
STREETS = [
    'Calle Colón', 'Avenida del Puerto', 'Calle San Vicente Mártir', 'Gran Vía Marqués del Túria',
    'Calle Quart', 'Avinguda Blasco Ibáñez', 'Carrer de la Pau', 'Calle Xàtiva', 'Avenida Aragón',
    'Calle Sagunto', 'Carrer Major', 'Plaça del Mercat', 'Calle del Mar', 'Avenida Corts Valencianes',
]
EMERGENCY_REL = ['Cónyuge', 'Pareja', 'Madre', 'Padre', 'Hermano', 'Hermana']
EMERGENCY_NAMES = [
    'José Luis Pérez', 'Marta Sanchis', 'Antonio Ferrer', 'Rosa Ibáñez', 'Vicente Gil',
    'Amparo Navarro', 'Jorge Camps', 'Teresa Bou', 'Ismael Ortiz', 'Nuria Soler',
    'Rafael Monzó', 'Silvia Andrés', 'Óscar Peris', 'Eva Llorens',
]

# ═══════════════════════ 1 · collective_agreements_db ═══════════════════════

sql = ["BEGIN;"]
new_cals = [
    ('vlc_ofi', LOCALITY['vlc'], AG_OFI),
    ('vlc_san', LOCALITY['vlc'], AG_SAN),
    ('gandia', LOCALITY['gandia'], AG_SAN),
    ('paterna', LOCALITY['paterna'], AG_SAN),
    ('castello', LOCALITY['castello'], AG_OFI),
]
ids5 = ", ".join(q(CAL[k]) for k, _, _ in new_cals)
sql.append(f"DELETE FROM holiday WHERE work_calendar_id IN ({ids5});")
sql.append(f"DELETE FROM work_calendar WHERE id IN ({ids5});")
for key, loc, ag in new_cals:
    sql.append(
        "INSERT INTO work_calendar (id, tenant_id, year, locality_id, collective_agreement_id, status, created_at, updated_at, created_by) "
        f"VALUES ({q(CAL[key])}, 'acme', 2026, {q(loc)}, {q(ag)}, 'ACTIVE', {q(NOW)}, {q(NOW)}, 'seed');")
    for d, name, scope in HOLIDAYS_CV:
        sql.append(
            "INSERT INTO holiday (id, tenant_id, work_calendar_id, date, name, scope, is_paid, is_recoverable, created_at, updated_at) "
            f"VALUES ({q(u())}, 'acme', {q(CAL[key])}, {q(d)}, {q(name)}, {q(scope)}, true, false, {q(NOW)}, {q(NOW)});")
sql.append("COMMIT;")
(OUT / '1_collective_agreements.sql').write_text("\n".join(sql) + "\n")

# ═══════════════════════ 2 · organization_db ═══════════════════════

RESP = {
    'sede_vlc': TOP, 'la_marina': eid('01'), 'residencia': eid('06'), 'turia': eid('12'),
    'castello': eid('18'), 'madrid': TOP, 'barcelona': TOP, 'sevilla': TOP,
}
sql = ["BEGIN;"]
for ck, cal_key in CENTER_CAL.items():
    sql.append(
        f"UPDATE work_centers SET work_calendar_id = {q(CAL[cal_key])}, responsable_id = {q(RESP[ck])}, "
        f"updated_at = {q(NOW)} WHERE id = {q(CENTER[ck])} AND tenant_id = 'acme';")
sql.append("COMMIT;")
(OUT / '2_organization.sql').write_text("\n".join(sql) + "\n")

# ═══════════════════════ 3 · employee_db ═══════════════════════

sql = ["BEGIN;"]
for i, e in enumerate(EMPLOYEES):
    n = i + 7  # seed offset
    ident = make_nie(n) if e['nie'] else make_dni(n)
    age = rng.randint(42, 56) if e['pos'] in ('director', 'resp_admin') else rng.randint(24, 52)
    birth = date(2026 - age, rng.randint(1, 12), rng.randint(1, 28))
    if birth > e['hire'] - timedelta(days=20 * 365):
        birth = birth.replace(year=e['hire'].year - 21)
    city, prov, postals = CITY[e['center']]
    street = f"{rng.choice(STREETS)} {rng.randint(1, 120)}, {rng.randint(1, 8)}º {rng.choice('ABCD')}"
    slug = (e['first'].split()[0] + '.' + e['last'].split()[0]).lower()
    slug = slug.translate(str.maketrans('áéíóúüñç', 'aeiouunc'))
    updates = {
        'department': e['dept'],
        'work_center_id': CENTER[e['center']],
        'job_position_id': POS[e['pos']],
        'seniority_date': e['seniority'].isoformat(),
        'birth_date': birth.isoformat(),
        'national_id': ident,
        'gender': e['gender'],
        'marital_status': e['marital'],
        'nationality': e['nationality'],
        'preferred_name': e['preferred'],
        'personal_email': f"{slug}{rng.randint(1, 99)}@gmail.com",
        'personal_phone': f"+34 6{rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(100, 999)}",
        'phone': f"+34 6{rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(100, 999)}",
        'address_street': street,
        'address_city': city,
        'address_province': prov,
        'address_postal': rng.choice(postals),
        'address_country': 'España',
        'emergency_name': rng.choice(EMERGENCY_NAMES),
        'emergency_relation': rng.choice(EMERGENCY_REL),
        'emergency_phone': f"+34 6{rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(100, 999)}",
        'work_mode': 'HYBRID' if e['conv'] == AG_OFI and e['pos'] != 'recepcionista' else 'ONSITE',
        'bank_iban': make_iban(n),
        'bank_holder': f"{e['first']} {e['last']}",
        'updated_at': NOW.replace('+00', ''),
    }
    sets = ", ".join(f"{k} = {q(v)}" for k, v in updates.items())
    sql.append(f"UPDATE employees SET {sets} WHERE id = {q(e['id'])} AND tenant_id = 'acme';")

# Reporting tree — regenerate wholesale for acme.
sql.append("DELETE FROM employee_reporting WHERE tenant_id = 'acme';")
for e in EMPLOYEES:
    m = manager_of(e)
    if not m:
        continue
    sql.append(
        "INSERT INTO employee_reporting (id, employee_id, manager_id, kind, is_primary, can_approve_timeoff, started_at, tenant_id, created_at) "
        f"VALUES ({q(u())}, {q(e['id'])}, {q(m)}, 'DIRECT', true, true, {q(e['hire'].isoformat())}, 'acme', {q(NOW)});")

# Calendar assignments — one per employee, their center's calendar.
sql.append("DELETE FROM work_calendar_assignment WHERE tenant_id = 'acme';")
for e in EMPLOYEES:
    cal = CAL[CENTER_CAL[e['center']]]
    sql.append(
        "INSERT INTO work_calendar_assignment (id, employee_id, work_calendar_id, assigned_at, tenant_id) "
        f"VALUES ({q(u())}, {q(e['id'])}, {q(cal)}, {q(NOW)}, 'acme');")

# The two test-tenant employees get full personal data too.
JUAN = 'df27b866-02a4-4e69-bef1-91db6d66d340'
PACO = '4e9c572d-6f9a-478c-99cf-384bbb4a30b0'
for tid, first, last, hire, extra in (
    (JUAN, 'Juan', 'García', '2024-01-15', {}),
    (PACO, 'Paco', 'Porras', '2026-03-23', {}),
):
    n = 100 + (1 if tid == PACO else 0)
    r2 = random.Random(n)
    birth = date(1990 + r2.randint(0, 8), r2.randint(1, 12), r2.randint(1, 28))
    upd = {
        'birth_date': birth.isoformat(), 'national_id': make_dni(n), 'gender': 'MALE',
        'marital_status': 'SINGLE', 'nationality': 'Española',
        'personal_email': f"{first.lower()}.{last.lower()}{n}@gmail.com",
        'personal_phone': f"+34 6{r2.randint(10, 99)} {r2.randint(100, 999)} {r2.randint(100, 999)}",
        'address_street': f"Calle de la Prueba {r2.randint(1, 40)}, {r2.randint(1, 5)}º B",
        'address_city': 'Madrid', 'address_province': 'Madrid',
        'address_postal': '28012', 'address_country': 'España',
        'emergency_name': 'Luisa Porras' if tid == PACO else 'Marina García',
        'emergency_relation': 'Hermana', 'emergency_phone': '+34 699 111 222',
        'work_mode': 'HYBRID', 'bank_iban': make_iban(n), 'bank_holder': f"{first} {last}",
        'seniority_date': hire, 'updated_at': NOW.replace('+00', ''),
    }
    sets = ", ".join(f"{k} = {q(v)}" for k, v in upd.items())
    sql.append(f"UPDATE employees SET {sets} WHERE id = {q(tid)} AND tenant_id = 'test';")
sql.append("COMMIT;")
(OUT / '3_employee.sql').write_text("\n".join(sql) + "\n")

# ═══════════════════════ 4 · contract_db ═══════════════════════

PLUS_AMOUNT = {
    'PLUS_TRANSPORTE': 1080, 'PLUS_CONVENIO': 720, 'PLUS_IDIOMAS': 900,
    'PLUS_TURNICIDAD': 1320, 'PLUS_ESPECIALIDAD': 1500, 'PLUS_FESTIVOS': 900,
    'PLUS_RESPONSABILIDAD': 3600,
}
sql = ["BEGIN;",
       "DELETE FROM contract_salary_concepts WHERE tenant_id IN ('acme','test');",
       "DELETE FROM contracts WHERE tenant_id IN ('acme','test');"]
for e in EMPLOYEES:
    cid = u()
    cat_id, base, grupo = CAT[e['cat']]
    fte = e['fte']
    base_pro = round(base * fte)
    trial_m = 6 if grupo in ('I', 'II') else 2
    end = "'2026-12-31'" if e['ctype'] == 'TEMPORAL' else 'NULL'
    var_amount = e['variable']
    sql.append(
        "INSERT INTO contracts (id, employee_id, job_position_id, collective_agreement_id, professional_category_id, "
        "contract_type_code, start_date, end_date, is_current, weekly_hours, fte, trial_period_unit, trial_period_duration, "
        "base_salary, pay_periods_per_year, variable_amount, variable_description, variable_period_months, notes, tenant_id, created_at, updated_at) "
        f"VALUES ({q(cid)}, {q(e['id'])}, {q(POS[e['pos']])}, {q(e['conv'])}, {q(cat_id)}, {q(e['ctype'])}, "
        f"{q(e['hire'].isoformat())}, {end}, true, {e['weekly']}, {fte}, 'MONTHS', {trial_m}, {base_pro}, 14, "
        f"{q(var_amount)}, {q('Bonus por objetivos del centro' if var_amount else None)}, {q(12 if var_amount else None)}, "
        f"NULL, 'acme', {q(NOW)}, {q(NOW)});")
    for code in e['pluses']:
        amt = round(PLUS_AMOUNT[code] * fte) if code != 'PLUS_TRANSPORTE' else PLUS_AMOUNT[code]
        sql.append(
            "INSERT INTO contract_salary_concepts (id, contract_id, salary_concept_code, amount, is_percentage, is_absorbible, tenant_id, created_at) "
            f"VALUES ({q(u())}, {q(cid)}, {q(code)}, {amt}, false, {q(code == 'PLUS_CONVENIO')}, 'acme', {q(NOW)});")

for tid, hire, base, ctype in ((JUAN, '2024-01-15', 24000, 'INDEFINIDO'), (PACO, '2026-03-23', 21000, 'INDEFINIDO')):
    cid = u()
    sql.append(
        "INSERT INTO contracts (id, employee_id, contract_type_code, start_date, is_current, weekly_hours, fte, "
        "trial_period_unit, trial_period_duration, base_salary, pay_periods_per_year, tenant_id, created_at, updated_at) "
        f"VALUES ({q(cid)}, {q(tid)}, {q(ctype)}, {q(hire)}, true, 40, 1.00, 'MONTHS', 2, {base}, 12, 'test', {q(NOW)}, {q(NOW)});")
    sql.append(
        "INSERT INTO contract_salary_concepts (id, contract_id, salary_concept_code, amount, is_percentage, is_absorbible, tenant_id, created_at) "
        f"VALUES ({q(u())}, {q(cid)}, 'PLUS_TRANSPORTE', 1100, false, false, 'test', {q(NOW)});")
sql.append("COMMIT;")
(OUT / '4_contract.sql').write_text("\n".join(sql) + "\n")

# ═══════════════════════ 5 · absence_db ═══════════════════════

absences = []   # (emp, type, start, end, days, status, reason, approver)


def add_abs(e, typ, start, end, status, reason=None):
    d = naturales(start, end) if e['conv'] == AG_SAN or typ in ('MATRIMONIO',) else laborables(start, end)
    approver = manager_of(e) or DIRECTOR_OF.get('la_marina')
    absences.append((e, typ, start, end, d, status, reason, approver))


# Summer vacations — staggered so no center empties out.
AUG_A = (date(2026, 8, 10), date(2026, 8, 21))   # oficinas: 10 laborables
AUG_SAN_A = (date(2026, 8, 3), date(2026, 8, 16))   # sanidad: 14 naturales
AUG_SAN_B = (date(2026, 8, 17), date(2026, 8, 30))
for i, e in enumerate(EMPLOYEES):
    suf = e['id'][-2:]
    if suf in ('07',):   # Laura took July instead
        add_abs(e, 'VACACIONES', date(2026, 7, 13), date(2026, 7, 26), 'APPROVED', 'Vacaciones de verano')
        continue
    if suf in ('23',):   # Raquel: one week in July
        add_abs(e, 'VACACIONES', date(2026, 7, 6), date(2026, 7, 12), 'APPROVED', 'Semana en familia')
    if e['conv'] == AG_SAN:
        rangeo = AUG_SAN_A if i % 2 == 0 else AUG_SAN_B
    else:
        rangeo = AUG_A
    add_abs(e, 'VACACIONES', rangeo[0], rangeo[1], 'APPROVED', 'Vacaciones de verano')

# Easter for some office staff (Apr 7-10: Apr 6 is Lunes de Pascua).
for suf in ('11', '13', '15', '17', '18'):
    e = BY_SUF[eid(suf)]
    add_abs(e, 'VACACIONES', date(2026, 4, 7), date(2026, 4, 10), 'APPROVED', 'Semana Santa')

# Pending September requests.
for suf, (s, t) in {'08': (date(2026, 9, 14), date(2026, 9, 18)),
                    '17': (date(2026, 9, 21), date(2026, 9, 25))}.items():
    e = BY_SUF[eid(suf)]
    absences.append((e, 'VACACIONES', s, t, laborables(s, t) if e['conv'] == AG_OFI else naturales(s, t),
                     'PENDING', 'Puente de septiembre', None))
absences.append((BY_SUF['4804acda-e3e3-4279-83fd-82a0ebb663b7'], 'VACACIONES',
                 date(2026, 9, 7), date(2026, 9, 11), 5, 'PENDING', None, None))

# One rejected request — month-close clash.
e16 = BY_SUF[eid('16')]
absences.append((e16, 'ASUNTOS_PROPIOS', date(2026, 6, 30), date(2026, 6, 30), 1,
                 'REJECTED', 'Asuntos personales', manager_of(e16)))

# Sick leave: Alberto in February; David current (explains missing punches this week).
add_abs(BY_SUF[eid('24')], 'BAJA_MEDICA', date(2026, 2, 9), date(2026, 2, 13), 'APPROVED', 'Gripe')
add_abs(BY_SUF[eid('05')], 'BAJA_MEDICA', date(2026, 7, 27), date(2026, 7, 31), 'APPROVED', 'Lumbalgia')

# Statutory leaves.
add_abs(BY_SUF[eid('21')], 'MATRIMONIO', date(2026, 5, 18), date(2026, 6, 1), 'APPROVED', 'Matrimonio')
add_abs(BY_SUF[eid('09')], 'MUDANZA', date(2026, 6, 12), date(2026, 6, 12), 'APPROVED', 'Traslado de domicilio')
add_abs(BY_SUF[eid('22')], 'FALLECIMIENTO', date(2026, 3, 4), date(2026, 3, 6), 'APPROVED', None)
add_abs(BY_SUF[eid('13')], 'FORMACION', date(2026, 5, 6), date(2026, 5, 7), 'APPROVED', 'Curso de nóminas')

# Scattered single-day asuntos propios.
AP_DAYS = {'02': date(2026, 3, 13), '04': date(2026, 5, 22), '12': date(2026, 4, 24),
           '15': date(2026, 6, 5), '19': date(2026, 2, 20), '25': date(2026, 6, 19)}
for suf, d in AP_DAYS.items():
    add_abs(BY_SUF[eid(suf)], 'ASUNTOOS' if False else 'ASUNTOS_PROPIOS', d, d, 'APPROVED', None)

sql = ["BEGIN;",
       "DELETE FROM absence_request WHERE tenant_id IN ('acme','test');",
       "DELETE FROM vacation_balance WHERE tenant_id IN ('acme','test');"]

CARRIED = {'11': 3, '07': 2, '22': 4, '13': 2}
for e in EMPLOYEES:
    entitled = 23 if e['conv'] == AG_OFI else 30
    carried = CARRIED.get(e['id'][-2:], 0)
    sql.append(
        "INSERT INTO vacation_balance (id, tenant_id, employee_id, year, entitled_days, carried_days, extra_days, created_at, updated_at) "
        f"VALUES ({q(u())}, 'acme', {q(e['id'])}, 2026, {entitled}, {carried}, 0, {q(NOW).replace('+00', '')}, {q(NOW).replace('+00', '')});")

for e, typ, s, t, d, status, reason, approver in absences:
    requested = s - timedelta(days=rng.randint(12, 30))
    decided = requested + timedelta(days=rng.randint(1, 3))
    has_decision = status in ('APPROVED', 'REJECTED')
    sql.append(
        "INSERT INTO absence_request (id, tenant_id, employee_id, absence_type_id, start_date, end_date, days, "
        "half_day_start, half_day_end, status, reason, approver_id, decided_at, decision_note, requested_at, created_at, updated_at) "
        f"VALUES ({q(u())}, 'acme', {q(e['id'])}, {q(ABS[typ])}, {q(s.isoformat())}, {q(t.isoformat())}, {d}, "
        f"false, false, {q(status)}, {q(reason)}, {q(approver if has_decision else None)}, "
        f"{q(decided.isoformat() + ' 09:30:00' if has_decision else None)}, "
        f"{q('Coincide con el cierre contable' if status == 'REJECTED' else None)}, "
        f"{q(requested.isoformat() + ' 10:00:00')}, {q(requested.isoformat() + ' 10:00:00')}, {q(decided.isoformat() + ' 09:30:00' if has_decision else requested.isoformat() + ' 10:00:00')});")

# test tenant: types have different ids there.
sql.append(
    "INSERT INTO vacation_balance (id, tenant_id, employee_id, year, entitled_days, carried_days, extra_days, created_at, updated_at) "
    f"SELECT {q(u())}, 'test', {q(JUAN)}, 2026, 23, 2, 0, now(), now();")
sql.append(
    "INSERT INTO vacation_balance (id, tenant_id, employee_id, year, entitled_days, carried_days, extra_days, notes, created_at, updated_at) "
    f"SELECT {q(u())}, 'test', {q(PACO)}, 2026, 23, 0, 0, 'Excedencia voluntaria desde junio', now(), now();")
sql.append(
    "INSERT INTO absence_request (id, tenant_id, employee_id, absence_type_id, start_date, end_date, days, half_day_start, half_day_end, status, reason, approver_id, decided_at, requested_at, created_at, updated_at) "
    f"SELECT {q(u())}, 'test', {q(JUAN)}, id, '2026-08-03', '2026-08-14', 10, false, false, 'APPROVED', 'Vacaciones de verano', {q(PACO)}, '2026-07-06 09:00:00', '2026-07-01 10:00:00', '2026-07-01 10:00:00', '2026-07-06 09:00:00' "
    "FROM absence_type WHERE tenant_id='test' AND code='VACACIONES';")
sql.append(
    "INSERT INTO absence_request (id, tenant_id, employee_id, absence_type_id, start_date, end_date, days, half_day_start, half_day_end, status, reason, approver_id, decided_at, requested_at, created_at, updated_at) "
    f"SELECT {q(u())}, 'test', {q(PACO)}, id, '2026-06-01', '2026-09-30', 122, false, false, 'APPROVED', 'Permiso sin sueldo por estudios', {q(JUAN)}, '2026-05-12 12:00:00', '2026-05-04 09:00:00', '2026-05-04 09:00:00', '2026-05-12 12:00:00' "
    "FROM absence_type WHERE tenant_id='test' AND code='SIN_SUELDO';")
sql.append("COMMIT;")
(OUT / '5_absence.sql').write_text("\n".join(sql) + "\n")

# ═══════════════════════ 6 · attendance_db · July 2026 ═══════════════════════

# Days each employee is absent in July (from the approved requests above).
absent_by_emp: dict[str, set] = {}
for e, typ, s, t, d, status, *_ in absences:
    if status != 'APPROVED':
        continue
    dd = s
    while dd <= t:
        if dd.month == 7:
            absent_by_emp.setdefault(e['id'], set()).add(dd)
        dd += timedelta(days=1)

OPEN_TODAY = {eid('16'), eid('17')}   # still at the office right now

sql = ["BEGIN;",
       "DELETE FROM work_day WHERE tenant_id IN ('acme','test');",
       "DELETE FROM time_correction WHERE tenant_id IN ('acme','test');",
       "DELETE FROM time_entry WHERE tenant_id IN ('acme','test');"]

entry_rows, day_rows = [], []


def gen_month(emp_id: str, tenant: str, pattern: str, weekly: float, source: str,
              absent: set, open_today: bool):
    expected = round(weekly / 5 * 60)
    d = date(2026, 7, 1)
    while d <= TODAY:
        if d.weekday() >= 5 or d in absent:
            d += timedelta(days=1)
            continue
        r = random.Random(f"{emp_id}:{d}")
        entries = []
        if pattern == 'continua':          # care centres: no lunch break
            m_in = r.randint(470, 492)     # 07:50-08:12
            m_out = m_in + expected + r.randint(-12, 18)
        elif pattern == 'partial':
            m_in = r.randint(535, 555) if weekly < 25 else r.randint(475, 495)
            m_out = m_in + expected + r.randint(-8, 10)
        else:                              # office day with a lunch break
            m_in = r.randint(518, 550)     # 08:38-09:10
            b_start = r.randint(834, 850)  # ~13:54-14:10
            b_len = r.randint(25, 40)
            m_out = m_in + b_len + expected + r.randint(-10, 22)
            entries.append(('BREAK_START', b_start))
            entries.append(('BREAK_END', b_start + b_len))
        entries.insert(0, ('IN', m_in))
        is_open = open_today and d == TODAY
        if not is_open:
            entries.append(('OUT', m_out))
        entries.sort(key=lambda x: x[1])

        break_min = 0
        worked = 0
        last_in = None
        for kind, m in entries:
            if kind == 'IN' or kind == 'BREAK_END':
                last_in = m
            elif kind in ('OUT', 'BREAK_START') and last_in is not None:
                worked += m - last_in
                last_in = None
            if kind == 'BREAK_END':
                pass
        if pattern == 'office' and not is_open:
            break_min = [m for k, m in entries if k == 'BREAK_END'][0] - [m for k, m in entries if k == 'BREAK_START'][0]
        if is_open and last_in is not None:
            worked += (19 * 60) - last_in   # counted up to ~19:00 local
            if pattern == 'office':
                bs = [m for k, m in entries if k == 'BREAK_START']
                be = [m for k, m in entries if k == 'BREAK_END']
                if bs and be:
                    break_min = be[0] - bs[0]

        first_in = None
        last_out = None
        for kind, m in entries:
            ts = f"2026-07-{d.day:02d} {m // 60:02d}:{m % 60:02d}:{r.randint(0, 59):02d}+02:00"
            if kind == 'IN':
                first_in = ts
            if kind == 'OUT':
                last_out = ts
            entry_rows.append(
                f"({q(u())}, {q(tenant)}, {q(emp_id)}, {q(kind)}, {q(ts)}, {q(d.isoformat())}, {q(source)}, false, {q(NOW)})")
        day_rows.append(
            f"({q(u())}, {q(tenant)}, {q(emp_id)}, {q(d.isoformat())}, {worked}, {break_min}, {expected}, "
            f"{q(None) if is_open else worked - expected}, {q(first_in)}, {q(last_out)}, {q(is_open)}, false, {q(NOW)}, {q(NOW)}, {q(NOW)})")
        d += timedelta(days=1)


for e in EMPLOYEES:
    if e['conv'] == AG_SAN:
        pattern = 'partial' if e['weekly'] < 36 else 'continua'
        source = 'TERMINAL'
    else:
        pattern = 'office'
        source = 'WEB' if e['center'] in ('sede_vlc', 'castello') else 'MOBILE'
    gen_month(e['id'], 'acme', pattern, e['weekly'], source,
              absent_by_emp.get(e['id'], set()), e['id'] in OPEN_TODAY)

gen_month(JUAN, 'test', 'office', 40, 'WEB', set(), False)

for i in range(0, len(entry_rows), 200):
    sql.append(
        "INSERT INTO time_entry (id, tenant_id, employee_id, kind, occurred_at, work_date, source, voided, created_at) VALUES\n"
        + ",\n".join(entry_rows[i:i + 200]) + ";")
for i in range(0, len(day_rows), 200):
    sql.append(
        "INSERT INTO work_day (id, tenant_id, employee_id, work_date, worked_minutes, break_minutes, expected_minutes, "
        "balance_minutes, first_in, last_out, is_open, has_anomaly, computed_at, created_at, updated_at) VALUES\n"
        + ",\n".join(day_rows[i:i + 200]) + ";")
sql.append("COMMIT;")
(OUT / '6_attendance.sql').write_text("\n".join(sql) + "\n")

print(f"employees: {len(EMPLOYEES)}  absences: {len(absences)}  entries: {len(entry_rows)}  work_days: {len(day_rows)}")
for f in sorted(OUT.glob('*.sql')):
    print(f"  {f.name}: {f.stat().st_size:,} bytes")
