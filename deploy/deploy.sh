#!/usr/bin/env bash
# Deploys the working tree (tracked and new files, nothing ignored) to the portfolio VM over IAP.
set -euo pipefail
cd "$(dirname "$0")/.."
VM=portfolio-web
GCLOUD=(--project portfolio-pavelpyt --zone europe-west3-b --tunnel-through-iap --quiet)

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
ARCHIVE=$WORK/research-agents.tgz
git ls-files -c -o --exclude-standard -z | COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs --null -T - -czf "$ARCHIVE"
gcloud compute scp "${GCLOUD[@]}" "$ARCHIVE" deploy/install-server.sh "$VM:/tmp/"
gcloud compute ssh "${GCLOUD[@]}" "$VM" --command \
  "sudo bash /tmp/install-server.sh /tmp/$(basename "$ARCHIVE"); rm -f /tmp/$(basename "$ARCHIVE") /tmp/install-server.sh"
