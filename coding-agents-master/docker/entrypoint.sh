#!/bin/bash
# Container entrypoint: install the egress firewall as root, then drop to the
# unprivileged agent user and exec the agent command (passed as docker CMD).
# Exit 97 = sandbox setup failure (the runner classifies it as infra_failure).
set -uo pipefail

if ! /usr/local/bin/init-firewall.sh; then
    echo "entrypoint: firewall setup FAILED — aborting attempt (fail-safe)" >&2
    exit 97
fi

if [ "$#" -eq 0 ]; then
    echo "entrypoint: no agent command given" >&2
    exit 97
fi

chown agent:agent /workspace 2>/dev/null || true
exec runuser -u agent -- "$@"
