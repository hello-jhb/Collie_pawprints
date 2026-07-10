#!/usr/bin/env bash
# Reliable Cloud Run deploy. Deploys the COMMITTED HEAD from a clean export, which
# avoids the gcloud `--source` zip bug ("ZIP timestamps before 1980" / FETCH_SOURCE_FAILED)
# triggered by .git and other cruft in the working tree. Commit first, then run this.
set -euo pipefail
D="$(mktemp -d)"
git archive --format=tar HEAD | tar -x -C "$D"
cp .gcloudignore "$D/" 2>/dev/null || true
( cd "$D" && gcloud run deploy paw-prints-api --source . --region us-west1 )
rm -rf "$D"
