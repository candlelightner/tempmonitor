#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
exec 9>.git/tempmonitor-update.lock
flock -n 9 || exit 0
git fetch origin
git merge --ff-only '@{upstream}'
revision=$(git rev-parse HEAD)
running=$(sudo -n docker compose ps --status running --quiet app)
# A deployed but stopped app is redeployed: Docker does not retry a container whose devices
# were missing at boot (P40 enumerated late), and without host telemetry the fans fail safe to 100 %.
if [[ -n $running ]] && [[ -f .git/tempmonitor-deployed ]] && [[ $(cat .git/tempmonitor-deployed) == "$revision" ]]; then
    exit 0
fi
# Local edits are preserved; Git refuses updates that would overwrite them.
sudo -n docker compose up -d --build --no-deps --wait --wait-timeout 90 app
printf '%s\n' "$revision" > .git/tempmonitor-deployed
