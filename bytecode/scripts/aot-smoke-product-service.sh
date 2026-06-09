#!/usr/bin/env bash
# AOT smoke check for product_service.
# Verifies that `--aot` produces a native binary that boots, responds, and exits cleanly.
# Exits non-zero on any failure. Safe to re-run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SVC_DIR="$REPO_ROOT/bytecode/generated-csharp-product_service"

# Detect RID
case "$(uname -s)" in
  Linux*)   RID=linux-x64 ;;
  Darwin*)  RID=osx-arm64 ;;
  MINGW*|MSYS*|CYGWIN*) RID=win-x64 ;;
  *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

echo "[smoke] RID=$RID"

echo "[smoke] regenerating with --aot"
python "$REPO_ROOT/transpiler/transpiler.py" \
    "$REPO_ROOT/bytecode/product_service.sexpr" \
    --target csharp --aot

cd "$SVC_DIR"

if [ "$RID" = "win-x64" ]; then
  # MSBuild's ILC target shells out to vswhere.exe to locate link.exe; if it's not on PATH
  # the AOT publish fails with MSB3073. Prepend the standard install location.
  # Use mingw-style path (/c/...) so the value translates correctly when child cmd.exe
  # processes inherit PATH from this bash shell.
  INSTALLER_DIR="/c/Program Files (x86)/Microsoft Visual Studio/Installer"
  if [ -d "$INSTALLER_DIR" ] && ! command -v vswhere.exe >/dev/null 2>&1; then
    export PATH="$INSTALLER_DIR:$PATH"
    echo "[smoke] prepended VS Installer dir to PATH for vswhere.exe"
  fi
fi

echo "[smoke] dotnet publish (AOT)"
dotnet publish -c Release -r "$RID" --nologo --verbosity quiet

PUB_DIR="$SVC_DIR/bin/Release/net10.0/$RID/publish"
case "$RID" in
  win-x64) BIN="$PUB_DIR/ProductService.exe" ;;
  *) BIN="$PUB_DIR/ProductService" ;;
esac

if [ ! -x "$BIN" ]; then
  echo "[smoke] FAIL: published binary not found at $BIN" >&2
  exit 1
fi

BIN_SIZE_MB=$(du -m "$BIN" | cut -f1)
echo "[smoke] binary: $BIN (${BIN_SIZE_MB} MB)"

echo "[smoke] starting binary on :8080"
"$BIN" >/tmp/aot-smoke.log 2>&1 &
PID=$!
trap "kill $PID 2>/dev/null || true; rm -f /tmp/aot-smoke.log /tmp/aot-smoke-resp" EXIT

# Wait up to 10s for [READY]
for i in $(seq 1 20); do
  if grep -q "\[READY\]" /tmp/aot-smoke.log 2>/dev/null; then
    break
  fi
  sleep 0.5
done
if ! grep -q "\[READY\]" /tmp/aot-smoke.log 2>/dev/null; then
  echo "[smoke] FAIL: binary did not reach [READY] in 10s"
  cat /tmp/aot-smoke.log
  exit 1
fi
echo "[smoke] binary is ready"

# Public GET — list returns []
LIST=$(curl -fsS http://127.0.0.1:8080/products/)
if [ "$LIST" != "[]" ]; then
  echo "[smoke] FAIL: expected GET / → '[]', got '$LIST'"
  exit 1
fi
echo "[smoke] GET /products/ → [] ✓"

# Public GET on missing UUID — 404 with AiLangError shape
HTTP=$(curl -s -o /tmp/aot-smoke-resp -w "%{http_code}" \
    http://127.0.0.1:8080/products/00000000-0000-0000-0000-000000000000)
if [ "$HTTP" != "404" ]; then
  echo "[smoke] FAIL: expected 404 on missing GUID, got $HTTP"
  cat /tmp/aot-smoke-resp; exit 1
fi
grep -q '"type":"NOT_FOUND"' /tmp/aot-smoke-resp \
  || { echo "[smoke] FAIL: 404 body missing NOT_FOUND"; cat /tmp/aot-smoke-resp; exit 1; }
echo "[smoke] GET /products/<missing> → 404 NOT_FOUND ✓"

# Get a token and POST a product
TOKEN=$(curl -fsS -X POST http://127.0.0.1:8080/auth/token | grep -o '"token":"[^"]*"' | cut -d'"' -f4)
if [ -z "$TOKEN" ]; then
  echo "[smoke] FAIL: /auth/token did not return a token"
  exit 1
fi
echo "[smoke] /auth/token → token (len=${#TOKEN}) ✓"

POSTED=$(curl -fsS -X POST http://127.0.0.1:8080/products/ \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"name":"smoke widget","price":1.5,"stock":2}')
echo "$POSTED" | grep -q '"name":"smoke widget"' \
  || { echo "[smoke] FAIL: POST response missing name"; echo "$POSTED"; exit 1; }
echo "[smoke] POST /products/ → product created ✓"

echo "[smoke] PASS"
