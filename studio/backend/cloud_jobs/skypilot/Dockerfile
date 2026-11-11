# SkyPilot API server for local development (SkyPilot does not run on Windows).
# Credentials are mounted at run time: gcloud config (~/.config/gcloud), ~/.aws, ~/.kube.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl rsync openssh-client git ca-certificates gnupg socat netcat-openbsd \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
       > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update && apt-get install -y --no-install-recommends google-cloud-cli \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv \
    && uv pip install --system --no-cache "skypilot[gcp,aws,kubernetes]"
# Use the helektron AWS profile unless overridden at docker run.
ENV AWS_PROFILE=helektron
EXPOSE 46580
CMD ["sky", "api", "start", "--deploy", "--host", "0.0.0.0", "--foreground"]
