#!/usr/bin/env bash
# Preview deploy for the M1 portfolio branch — a SEPARATE Cloud Run service
# (paw-prints-m1) with its own URL, so the live v1 service (paw-prints-api)
# is never touched. Same clean-export trick as deploy.sh: deploys COMMITTED
# HEAD only, so commit (and be on the branch you mean to ship) first.
#
# Notes:
#   * First deploy: answer "Y" to "allow unauthenticated" so the URL is
#     reachable in a browser.
#   * OPENAI_API_KEY must be set on the service once (Cloud Run console →
#     paw-prints-m1 → Edit & deploy new revision → Variables), or by
#     uncommenting the --update-env-vars line below for one run. Never
#     commit the key.
#   * Cloud Run disk is ephemeral: portfolio state (assets/*.json) resets
#     on restart. For portfolio demos, upload all workbooks in one sitting.
set -euo pipefail
echo "Deploying branch: $(git branch --show-current) @ $(git rev-parse --short HEAD)"
D="$(mktemp -d)"
git archive --format=tar HEAD | tar -x -C "$D"
cp .gcloudignore "$D/" 2>/dev/null || true
( cd "$D" && gcloud run deploy paw-prints-m1 --source . --region us-west1 \
    --labels "commit=$(git rev-parse --short HEAD)" )
#   --update-env-vars OPENAI_API_KEY="$OPENAI_API_KEY"   # one-time; then remove
rm -rf "$D"
