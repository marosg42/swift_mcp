#!/usr/bin/env python3
"""
Swift MCP Server
Read-only access to OpenStack Swift via S3-compatible API.

Credentials are loaded from an openrc.sh with standard Keystone variables:
  OS_AUTH_URL, OS_USERNAME, OS_PASSWORD, OS_PROJECT_NAME,
  OS_REGION_NAME, OS_USER_DOMAIN_NAME, OS_PROJECT_DOMAIN_NAME

On startup the server authenticates with Keystone to:
  1. Discover the S3/object-store endpoint from the service catalog.
  2. Fetch (or create) EC2 credentials for use with boto3.
"""

import base64
import json
import logging
import os
import sys
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from keystoneauth1 import loading, session as ks_session
from keystoneclient.v3 import client as keystone_client
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logging.getLogger("botocore.endpoint").setLevel(logging.DEBUG)
logging.getLogger("urllib3").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)

MAX_OBJECT_SIZE = 10 * 1024 * 1024  # 10 MB


# ---------------------------------------------------------------------------
# Startup: authenticate with Keystone and build boto3 client
# ---------------------------------------------------------------------------

def _require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Required environment variable '{key}' is not set in openrc.sh")
    return val


def _build_s3_client():
    auth_url    = _require_env("OS_AUTH_URL")
    username    = _require_env("OS_USERNAME")
    password    = _require_env("OS_PASSWORD")
    project     = _require_env("OS_PROJECT_NAME")
    user_domain = os.environ.get("OS_USER_DOMAIN_NAME", "Default")
    proj_domain = os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default")
    region      = os.environ.get("OS_REGION_NAME") or None
    interface   = os.environ.get("OS_INTERFACE", "public")

    loader = loading.get_plugin_loader("password")
    auth = loader.load_from_options(
        auth_url=auth_url,
        username=username,
        password=password,
        project_name=project,
        user_domain_name=user_domain,
        project_domain_name=proj_domain,
    )
    sess = ks_session.Session(auth=auth)

    # -- S3 endpoint from service catalog ------------------------------------
    endpoint: Optional[str] = None
    for svc_type in ("s3", "object-store"):
        try:
            ep = sess.get_endpoint(
                service_type=svc_type,
                interface=interface,
                region_name=region,
            )
            if ep:
                endpoint = ep
                logger.info("S3 endpoint (%s): %s", svc_type, endpoint)
                break
        except Exception:
            continue

    if not endpoint:
        raise RuntimeError(
            "Could not find an 's3' or 'object-store' endpoint in the Keystone "
            "service catalog. Check OS_REGION_NAME and that Swift is registered."
        )

    # -- EC2 credentials (get existing or create one) -----------------------
    ks = keystone_client.Client(session=sess)
    user_id = sess.get_user_id()
    ec2_creds = ks.ec2.list(user_id)
    if ec2_creds:
        cred = ec2_creds[0]
        logger.info("Using existing EC2 credential (access=%s)", cred.access)
    else:
        project_id = sess.get_project_id()
        cred = ks.ec2.create(user_id, project_id)
        logger.info("Created EC2 credential (access=%s)", cred.access)

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=cred.access,
        aws_secret_access_key=cred.secret,
        region_name=region or "regionOne",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


try:
    s3 = _build_s3_client()
except Exception as _err:
    print(f"ERROR: {_err}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "swift-mcp",
    instructions=(
        "Read-only access to OpenStack Swift object storage. "
        "IMPORTANT: This MCP server is the ONLY way to access Swift — no credentials "
        "or openrc.sh are available to shell commands or external scripts. "
        "Always use these tools instead of running boto3/Swift CLI directly.\n\n"
        "Typical workflows:\n"
        "- Explore: list_containers → list_objects\n"
        "- Inspect without downloading: head_object\n"
        "- Download files to local disk: list_objects to enumerate keys, then for each "
        "key call get_object and write the returned content to the local filesystem "
        "(text files: write 'content' field directly; binary files: base64-decode "
        "'content_base64' before writing). Files larger than 10 MB cannot be fetched "
        "and will return an error."
    ),
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8000")),
)


@mcp.tool()
def list_containers() -> str:
    """List all Swift containers (S3 buckets) visible to the configured credentials."""
    try:
        response = s3.list_buckets()
        buckets = response.get("Buckets", [])
        if not buckets:
            return "No containers found."
        return json.dumps(
            [{"name": b["Name"], "created": b["CreationDate"].isoformat()} for b in buckets],
            indent=2,
        )
    except ClientError as exc:
        err = exc.response.get("Error", {})
        return f"Error: {err.get('Message') or err.get('Code') or str(exc)}"


@mcp.tool()
def list_objects(
    container: str,
    prefix: str = "",
    delimiter: str = "",
    max_keys: int = 1000,
) -> str:
    """
    List objects inside a Swift container.

    Args:
        container: Container (bucket) name.
        prefix:    Return only objects whose key starts with this string,
                   e.g. "logs/2024/" to navigate into a subdirectory.
        delimiter: Group keys by this character. Use "/" for a directory-style
                   listing where subdirectories appear in common_prefixes.
        max_keys:  Maximum results to return (1–10000, default 1000).
    """
    max_keys = max(1, min(max_keys, 10000))
    try:
        objects = []
        common_prefixes = []
        kwargs: dict = {"Bucket": container, "MaxKeys": 1000}
        if prefix:
            kwargs["Prefix"] = prefix
        if delimiter:
            kwargs["Delimiter"] = delimiter

        while len(objects) < max_keys:
            response = s3.list_objects_v2(**kwargs)
            objects.extend(response.get("Contents", []))
            for cp in response.get("CommonPrefixes", []):
                if cp["Prefix"] not in common_prefixes:
                    common_prefixes.append(cp["Prefix"])
            if not response.get("IsTruncated") or len(objects) >= max_keys:
                break
            kwargs["ContinuationToken"] = response["NextContinuationToken"]

        objects = objects[:max_keys]
        return json.dumps(
            {
                "container": container,
                "prefix": prefix,
                "truncated": len(objects) == max_keys and response.get("IsTruncated", False),
                "count": len(objects),
                "objects": [
                    {
                        "key": obj["Key"],
                        "size_bytes": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                        "etag": obj.get("ETag", "").strip('"'),
                    }
                    for obj in objects
                ],
                "common_prefixes": common_prefixes,
            },
            indent=2,
        )
    except ClientError as exc:
        err = exc.response.get("Error", {})
        return f"Error: {err.get('Message') or err.get('Code') or str(exc)}"


@mcp.tool()
def get_object(container: str, key: str, encoding: str = "utf-8") -> str:
    """
    Read the content of an object from Swift.

    Objects larger than 10 MB are refused — use head_object to check size first.
    Text content is returned as a string in the "content" field (binary=false).
    Binary content is returned as base64 in the "content_base64" field (binary=true).

    To save a file locally: call this tool, then write the content to disk —
    decode base64 first for binary files (e.g. .tar.gz, .gz, images).

    Args:
        container: Container (bucket) name.
        key:       Object key (full path, e.g. "logs/2024/app.log").
        encoding:  Text encoding to attempt (default "utf-8").
    """
    try:
        head = s3.head_object(Bucket=container, Key=key)
        size = head.get("ContentLength", 0)

        if size > MAX_OBJECT_SIZE:
            return (
                f"Error: object is {size:,} bytes — exceeds the 10 MB read limit. "
                "Use head_object to inspect its metadata."
            )

        response = s3.get_object(Bucket=container, Key=key)
        body = response["Body"].read()
        content_type = response.get("ContentType", "")

        text_types = ("text/", "json", "xml", "yaml", "javascript", "csv")
        if any(t in content_type for t in text_types) or not content_type or content_type == "binary/octet-stream":
            try:
                return json.dumps(
                    {
                        "container": container,
                        "key": key,
                        "size_bytes": size,
                        "content_type": content_type,
                        "encoding": encoding,
                        "binary": False,
                        "content": body.decode(encoding),
                    },
                    indent=2,
                )
            except UnicodeDecodeError:
                pass  # fall through to base64

        return json.dumps(
            {
                "container": container,
                "key": key,
                "size_bytes": size,
                "content_type": content_type,
                "binary": True,
                "content_base64": base64.b64encode(body).decode("ascii"),
            },
            indent=2,
        )
    except ClientError as exc:
        err = exc.response.get("Error", {})
        return f"Error: {err.get('Message') or err.get('Code') or str(exc)}"


@mcp.tool()
def head_object(container: str, key: str) -> str:
    """
    Return metadata for an object without downloading its content.

    Args:
        container: Container (bucket) name.
        key:       Object key (full path).
    """
    try:
        r = s3.head_object(Bucket=container, Key=key)
        return json.dumps(
            {
                "container": container,
                "key": key,
                "size_bytes": r.get("ContentLength"),
                "content_type": r.get("ContentType"),
                "last_modified": (
                    r["LastModified"].isoformat() if r.get("LastModified") else None
                ),
                "etag": r.get("ETag", "").strip('"'),
                "user_metadata": r.get("Metadata", {}),
            },
            indent=2,
        )
    except ClientError as exc:
        err = exc.response.get("Error", {})
        return f"Error: {err.get('Message') or err.get('Code') or str(exc)}"


if __name__ == "__main__":
    mcp.run(transport="sse")
