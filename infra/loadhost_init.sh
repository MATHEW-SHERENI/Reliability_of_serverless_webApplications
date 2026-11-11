#!/bin/bash
# First-boot setup of the load host: install k6 ${k6_version} from the official
# GitHub release and refuse the file if its SHA-256 differs from the pinned one.
set -euo pipefail
cd /tmp
curl -fsSL -o k6.tar.gz "https://github.com/grafana/k6/releases/download/v${k6_version}/k6-v${k6_version}-linux-amd64.tar.gz"
echo "${k6_sha256}  k6.tar.gz" | sha256sum --check --strict -
tar -xzf k6.tar.gz
install -m 0755 "k6-v${k6_version}-linux-amd64/k6" /usr/local/bin/k6
/usr/local/bin/k6 version > /var/log/k6-install.log
