#!/usr/bin/env bash
# Mutation check for the v2.14.2 tripwires (the address of a host in the
# shipped configuration).
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties:
#
#   * the deployable configuration names no host by its IPv4 address;
#   * the published ports take the address from the operator;
#   * the default bind address is the loopback;
#   * the scan still looks at the compose file.
set -u
cd "$(dirname "$0")/.."

PY="${PY:-}"
if [ -z "$PY" ]; then
    for candidate in /workspace/.venv-owbs/bin/python3 python3; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c "import pytest" >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done
fi
if [ -z "$PY" ]; then
    echo "no python with pytest found — set PY=/path/to/python" >&2
    exit 2
fi

PASS=0
FAIL=0

# A stale bytecode cache can hide a mutation: Python keys a .pyc by the
# source's mtime AND its size, so two mutations of the same length in the
# same second leave the second one invisible.
export PYTHONDONTWRITEBYTECODE=1

mutate() {
    # mutate <file> <old> <new> <test-selector> <label>
    local file="$1" old="$2" new="$3" selector="$4" label="$5"
    local backup
    backup="$(mktemp -p /tmp mut.XXXXXX)"
    cp "$file" "$backup"
    python3 - "$file" "$old" "$new" <<'PY'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()
if old not in text:
    sys.exit(f"PATTERN NOT FOUND in {path}: {old!r}")
open(path, "w", encoding="utf-8").write(text.replace(old, new, 1))
PY
    if [ $? -ne 0 ]; then
        echo "SKIP  $label (pattern not found — mutation did not apply)"
        cp "$backup" "$file"
        rm -f "$backup"
        FAIL=$((FAIL+1))
        return
    fi
    find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    if $PY -m pytest "$selector" -q >/tmp/mut.out 2>&1; then
        echo "FAIL  $label — the test stayed GREEN under the mutation"
        cp "$backup" "$file"
        rm -f "$backup"
        FAIL=$((FAIL+1))
    else
        echo "OK    $label — the test went red"
        cp "$backup" "$file"
        rm -f "$backup"
        PASS=$((PASS+1))
    fi
}

RULE="tests/test_no_foreign_addresses_v2142.py"

echo "== the address of a host is back in the compose file =="
mutate docker-compose.yml \
    '"${SANDBOX_BIND_HOST:-127.0.0.1}:8090:8090"' \
    '"192.168.10.7:8090:8090"' \
    "$RULE::TestTheConfigurationNamesNoHost::test_no_file_names_a_host_by_its_address" \
    "the compose file names no host"

echo "== the address of a host is back in the documentation =="
mutate docs/CONFIGURATION.md \
    '| `127.0.0.1:3001`   | open-webui     | Web UI                                |' \
    '| `192.168.10.7:3001` | open-webui     | Web UI                                |' \
    "$RULE::TestTheConfigurationNamesNoHost::test_no_file_names_a_host_by_its_address" \
    "the documentation names no host"

echo "== the address stops being the operator's choice =="
mutate docker-compose.yml \
    '"${SANDBOX_BIND_HOST:-127.0.0.1}:8090:8090"' \
    '"127.0.0.1:8090:8090"' \
    "$RULE::TestTheConfigurationNamesNoHost::test_the_published_ports_take_the_address_from_the_operator" \
    "the published ports take the address from the operator"

echo "== the default publishes on every interface =="
mutate docker-compose.yml \
    '${SANDBOX_BIND_HOST:-127.0.0.1}:8090:8090' \
    '${SANDBOX_BIND_HOST:-0.0.0.0}:8090:8090' \
    "$RULE::TestTheConfigurationNamesNoHost::test_the_default_bind_address_is_the_loopback" \
    "the default bind address is the loopback"

echo "== the scan stops looking at the compose file =="
mutate tests/test_no_foreign_addresses_v2142.py \
    '    "docker-compose*.yml",' \
    '    "docker-compose*.yaml",' \
    "$RULE::TestTheConfigurationNamesNoHost::test_the_scan_covers_something" \
    "the scan still looks at the compose file"

echo
echo "mutations: $PASS went red as expected, $FAIL did not"
[ "$FAIL" -eq 0 ] || exit 1
