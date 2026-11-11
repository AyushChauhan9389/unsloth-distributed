#!/bin/sh
# Keeps gcloud's active account in sync with the service-account key saved from the Studio UI
# (/creds/gcp/key.json): SkyPilot's GCP check reads gcloud's active account and project.
key=/creds/gcp/key.json
last=""
while true; do
  if [ -f "$key" ]; then
    m=$(stat -c %Y "$key")
    if [ "$m" != "$last" ]; then
      project=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["project_id"])' "$key")
      if gcloud auth activate-service-account --key-file="$key" >/dev/null 2>&1 \
         && gcloud config set project "$project" >/dev/null 2>&1; then
        last="$m"
        echo "creds-watch: gcloud now uses the saved service account (project $project)"
      fi
    fi
  elif [ -n "$last" ]; then
    gcloud auth revoke --all >/dev/null 2>&1
    last=""
    echo "creds-watch: Google Cloud disconnected"
  fi
  sleep 5
done
