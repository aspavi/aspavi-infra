#!/bin/sh
# Copias por tenant (ASP-43): baja age, la versión y el sha256 fijados aquí, y
# lo deja en /tools para el paso upload. Lo ejecuta el initContainer `tools`.
# Si el sha256 no coincide, para: nada se cifra con un binario que no es el
# esperado. Para cambiar de versión, cambia las dos líneas (el sha256 es el del
# .tar.gz de linux-amd64 de https://github.com/FiloSottile/age/releases).
set -eu
AGE_VERSION=v1.3.2
AGE_SHA256=cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10
: "${TOOLS_DIR:=/tools}"

curl -fsSL --retry 3 --max-time 120 -o /tmp/age.tar.gz \
  "https://github.com/FiloSottile/age/releases/download/$AGE_VERSION/age-$AGE_VERSION-linux-amd64.tar.gz"
echo "$AGE_SHA256  /tmp/age.tar.gz" | sha256sum -c -
tar -xzf /tmp/age.tar.gz -C "$TOOLS_DIR" --strip-components 1 age/age
rm -f /tmp/age.tar.gz
echo "age $("$TOOLS_DIR/age" --version)"
