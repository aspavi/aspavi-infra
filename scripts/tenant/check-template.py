#!/usr/bin/env python3
"""Comprueba que realm-template.json sigue teniendo la forma de
apps/keycloak/realm-import-acme.yaml (ASP-21). Si cambias uno, cambia el otro.

  python3 scripts/tenant/check-template.py

Renderiza la plantilla para acme ("Acme Corporation") y la compara con el
spec.realm del import, sin el client rrhh-employee-service, que la plantilla no
lleva: no lo usa nada en el clúster. Solo lee ficheros. Necesita yq.
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from onboard import REPO, render_realm  # noqa: E402

IMPORT = REPO / "apps" / "keycloak" / "realm-import-acme.yaml"


def main():
    try:
        out = subprocess.run(["yq", "-o", "json", ".spec.realm", str(IMPORT)],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"ERROR: no puedo leer {IMPORT} con yq: {e}")
    expected = json.loads(out)
    expected["clients"] = [c for c in expected["clients"] if c["clientId"] != "rrhh-employee-service"]
    got = render_realm("acme", "Acme Corporation")
    if got == expected:
        print(f"OK: realm-template.json tiene la forma de {IMPORT.relative_to(REPO)}")
        return
    for key in sorted(set(got) | set(expected)):
        if got.get(key) != expected.get(key):
            print(f"  difiere: {key}")
    sys.exit("ERROR: la plantilla y el import de acme no coinciden")


if __name__ == "__main__":
    main()
