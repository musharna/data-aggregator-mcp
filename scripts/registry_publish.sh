#!/usr/bin/env bash
# Publish server.json to the MCP registry, retrying while the registry cannot yet see
# the PyPI release. The registry fetches pypi.org/pypi/<pkg>/<version>/json itself and
# can be answered 404 for minutes after the same URL answers 200 to this runner
# (v0.58.0, 2026-10-04: two 404s ~4 min apart, while the runner saw 200), so a
# runner-side wait cannot tell when the registry will see it: only its answer can.
# Retries only the answers the registry calls transient; any other failure is final.
# Logs in before every try: a registry token lasts 5 minutes (internal/auth/jwt.go).
#
# usage: registry_publish.sh <mcp-publisher> <login method...>   (env: TRIES=20, WAIT=30 s)
set -uo pipefail
publisher=${1:?usage: registry_publish.sh <mcp-publisher> <login method...>}
shift
[ $# -gt 0 ] || {
	echo "usage: registry_publish.sh <mcp-publisher> <login method...>" >&2
	exit 2
}
tries=${TRIES:-20}
wait=${WAIT:-30}
for i in $(seq 1 "$tries"); do
	"$publisher" login "$@" || exit
	out=$("$publisher" publish 2>&1)
	rc=$?
	printf '%s\n' "$out"
	if [ "$rc" -eq 0 ]; then exit 0; fi
	case "$out" in
	*"A newly published release can take a moment to appear on PyPI"* | *"Likely transient, retry later"*) ;;
	*)
		echo "registry publish failed (exit $rc), not a propagation delay: not retrying" >&2
		exit "$rc"
		;;
	esac
	if [ "$i" -lt "$tries" ]; then
		echo "the registry cannot see the PyPI release yet: try $((i + 1))/$tries in ${wait}s"
		sleep "$wait"
	fi
done
echo "the registry still could not see the PyPI release after $tries tries" >&2
exit 1
