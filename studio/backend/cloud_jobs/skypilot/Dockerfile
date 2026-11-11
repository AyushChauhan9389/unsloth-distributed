# SkyPilot API server for cloud jobs (SkyPilot does not run on Windows, so it runs in this container).
# Cloud credentials come from Studio's "Cloud accounts" UI: mount <studio>/cloud_credentials at /creds (read-only).
#   docker build -t unsloth-skypilot studio/backend/cloud_jobs/skypilot
#   docker run -d --name skypilot-api -p 127.0.0.1:46580:46580 -v skypilot-state:/root/.sky \
#     -v <studio>/cloud_credentials:/creds:ro [-v ~/.kube:/root/.kube:ro] unsloth-skypilot
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl rsync openssh-client git ca-certificates gnupg socat netcat-openbsd \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
       > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update && apt-get install -y --no-install-recommends google-cloud-cli \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv \
    && uv pip install --system --no-cache "skypilot[gcp,aws,kubernetes]"
# AWS SDK/CLI and SkyPilot read these; a missing file just means that cloud is not connected.
ENV AWS_SHARED_CREDENTIALS_FILE=/creds/aws/credentials \
    AWS_CONFIG_FILE=/creds/aws/config \
    GOOGLE_APPLICATION_CREDENTIALS=/creds/gcp/key.json
COPY creds-watch.sh /usr/local/bin/creds-watch.sh
RUN chmod +x /usr/local/bin/creds-watch.sh
EXPOSE 46580
CMD ["sh", "-c", "creds-watch.sh & exec sky api start --deploy --host 0.0.0.0 --foreground"]
