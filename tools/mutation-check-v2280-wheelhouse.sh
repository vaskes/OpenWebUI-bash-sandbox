#!/usr/bin/env bash
# Mutation check for the wave that makes the wheelhouse carry the build
# backends.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The property: the wheelhouse the tree is assembled from carries the
# build backends, because an sdist is built offline against it.
#
# The trigger was measured, not imagined: pypi published SQLAlchemy
# 2.1.7 on 2026-11-03 with no cp314 wheel, and the build died with
# "Could not find a version that satisfies the requirement
# setuptools>=77.0.3".
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

TESTS=tests/test_wheelhouse_v2280.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"

echo "== the wheelhouse does not carry the build backends =="
mutate Dockerfile.sandbox \
  '    && python3 -m pip download --dest /opt/wheelhouse setuptools wheel \
' \
  '' \
  "$TESTS::TestTheWheelhouseCarriesTheBuildBackends::test_the_build_backends_are_downloaded" \
  "the wheelhouse has no build backend for an sdist"

echo "== the install is not offline any more =="
mutate Dockerfile.sandbox \
  '    && python3 -m pip install --no-index --find-links=/opt/wheelhouse \
' \
  '    && python3 -m pip install \
' \
  "$TESTS::TestTheWheelhouseCarriesTheBuildBackends::test_the_install_really_is_offline" \
  "the tree is no longer assembled from the wheelhouse alone"

echo "== the measured failure is not recorded =="
mutate Dockerfile.sandbox \
  'setuptools>=77.0.3' \
  'setuptools' \
  "$TESTS::TestTheWheelhouseCarriesTheBuildBackends::test_the_measured_failure_is_recorded" \
  "the Dockerfile does not name the requirement that could not be found"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
