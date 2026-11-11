"""Cloud accounts connected from the UI: AWS access keys (optionally assuming an IAM role) and a GCP service-account key.

Secrets live only in files under <studio>/cloud_credentials (never returned by the API). The layout is what the AWS
SDK/CLI and gcloud read, so the same folder is mounted into the SkyPilot API server container:
  aws/credentials, aws/config      -> AWS_SHARED_CREDENTIALS_FILE / AWS_CONFIG_FILE (profile "default")
  gcp/key.json                     -> GOOGLE_APPLICATION_CREDENTIALS
  meta.json                        -> what the UI shows: identity, masked key id, project, saved time
"""
import configparser
import json
import os
import shutil
import time
from pathlib import Path

from utils.paths import studio_root

AWS_BASE_PROFILE = "studio-base"  # holds the keys when an IAM role is assumed from them


def root() -> Path:
    return studio_root() / "cloud_credentials"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents = True, exist_ok = True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding = "utf-8")
    os.chmod(tmp, 0o600)  # best effort on Windows
    os.replace(tmp, path)


def _meta() -> dict:
    p = root() / "meta.json"
    return json.loads(p.read_text(encoding = "utf-8")) if p.exists() else {}


def _save_meta(cloud: str, info: dict | None) -> None:
    meta = _meta()
    if info is None:
        meta.pop(cloud, None)
    else:
        meta[cloud] = {**info, "saved_at": int(time.time() * 1000)}
    _write_private(root() / "meta.json", json.dumps(meta, indent = 2))


def status() -> dict:
    """Public view: never contains secrets."""
    return {"aws": _meta().get("aws"), "gcp": _meta().get("gcp")}


# ---- AWS
def _ini(sections: dict[str, dict[str, str]]) -> str:
    cp = configparser.ConfigParser()
    for name, values in sections.items():
        cp[name] = values
    import io
    buf = io.StringIO()
    cp.write(buf)
    return buf.getvalue()


def aws_session(region: str | None = None):
    """boto3 session from the stored keys, assuming the stored role when there is one."""
    import boto3

    d = root() / "aws"
    if not (d / "credentials").exists():
        raise LookupError("AWS is not connected")
    return boto3.Session(profile_name = "default", region_name = region or _meta().get("aws", {}).get("region"),
                         botocore_session = _botocore_session(d))


def _botocore_session(d: Path):
    import botocore.session

    s = botocore.session.Session()
    s.set_config_variable("credentials_file", str(d / "credentials"))
    s.set_config_variable("config_file", str(d / "config"))
    return s


def connect_aws(access_key_id: str, secret_access_key: str, region: str, role_arn: str | None) -> dict:
    """Validates the keys (and role) with STS before saving anything. Returns the public identity."""
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    access_key_id, secret_access_key, role_arn = access_key_id.strip(), secret_access_key.strip(), (role_arn or "").strip()
    try:
        base = boto3.Session(aws_access_key_id = access_key_id, aws_secret_access_key = secret_access_key,
                             region_name = region)
        sts = base.client("sts")
        ident = sts.get_caller_identity()
        if role_arn:
            creds = sts.assume_role(RoleArn = role_arn, RoleSessionName = "unsloth-studio-check")["Credentials"]
            ident = boto3.client("sts", aws_access_key_id = creds["AccessKeyId"], region_name = region,
                                 aws_secret_access_key = creds["SecretAccessKey"],
                                 aws_session_token = creds["SessionToken"]).get_caller_identity()
    except (ClientError, BotoCoreError) as e:
        raise ValueError(f"AWS rejected these credentials: {e}") from e

    keys = {"aws_access_key_id": access_key_id, "aws_secret_access_key": secret_access_key}
    d = root() / "aws"
    if role_arn:
        _write_private(d / "credentials", _ini({AWS_BASE_PROFILE: keys}))
        _write_private(d / "config", _ini({"default": {"region": region, "role_arn": role_arn,
                                                       "source_profile": AWS_BASE_PROFILE},
                                           f"profile {AWS_BASE_PROFILE}": {"region": region}}))
    else:
        _write_private(d / "credentials", _ini({"default": keys}))
        _write_private(d / "config", _ini({"default": {"region": region}}))
    info = {"account": ident["Account"], "arn": ident["Arn"], "region": region, "role_arn": role_arn or None,
            "access_key_id": f"{access_key_id[:4]}…{access_key_id[-4:]}"}
    _save_meta("aws", info)
    return info


# ---- GCP
def gcp_credentials():
    """(credentials, project_id) from the stored service-account key."""
    from google.oauth2 import service_account

    key = root() / "gcp" / "key.json"
    if not key.exists():
        raise LookupError("Google Cloud is not connected")
    creds = service_account.Credentials.from_service_account_file(
        str(key), scopes = ["https://www.googleapis.com/auth/cloud-platform"])
    return creds, json.loads(key.read_text(encoding = "utf-8"))["project_id"]


def connect_gcp(key_json: str) -> dict:
    """Validates the service-account key against Google before saving it. Returns the public identity."""
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    try:
        info = json.loads(key_json)
    except json.JSONDecodeError as e:
        raise ValueError("That is not a JSON key file") from e
    if info.get("type") != "service_account" or not info.get("project_id") or not info.get("private_key"):
        raise ValueError("Upload a service-account key (JSON with type 'service_account', project_id, private_key)")
    try:
        creds = service_account.Credentials.from_service_account_info(
            info, scopes = ["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(Request())
        build("compute", "v1", credentials = creds, cache_discovery = False).projects().get(
            project = info["project_id"]).execute()
    except (GoogleAuthError, HttpError) as e:
        raise ValueError(f"Google rejected this key or it cannot read project {info['project_id']}: {e}") from e

    _write_private(root() / "gcp" / "key.json", json.dumps(info))
    public = {"client_email": info["client_email"], "project_id": info["project_id"]}
    _save_meta("gcp", public)
    return public


def disconnect(cloud: str) -> None:
    shutil.rmtree(root() / cloud, ignore_errors = True)
    _save_meta(cloud, None)
