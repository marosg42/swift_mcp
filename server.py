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
import secrets
import socket
import socketserver
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
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

MAX_OBJECT_SIZE = 512 * 1024 * 1024  # 512 MB
CONTAINER_NAME = "solutions-qa"  # Hardcoded container to serve

# ---------------------------------------------------------------------------
# Staging HTTP server — serves downloaded objects as raw binary files so the
# agent can curl them without any base64 / JSON overhead in the MCP context.
# ---------------------------------------------------------------------------

_STAGE_DIR = tempfile.mkdtemp(prefix="swift_mcp_stage_")
_STAGE_MAP: dict[str, str] = {}  # token -> local filesystem path
_STAGE_TIME: dict[str, float] = {}  # token -> creation timestamp
_FILE_PORT = int(os.environ.get("MCP_FILE_PORT", "8001"))
_STAGE_TTL = 24 * 60 * 60  # 24 hours


def _reap_staged_files() -> None:
    """Delete staged files older than 24 hours. Called on each new staging request."""
    cutoff = time.monotonic() - _STAGE_TTL
    for token in list(_STAGE_TIME):
        if _STAGE_TIME.get(token, float("inf")) < cutoff:
            local_path = _STAGE_MAP.pop(token, None)
            _STAGE_TIME.pop(token, None)
            if local_path and os.path.exists(local_path):
                try:
                    os.unlink(local_path)
                except OSError:
                    pass


class _StagingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        token = self.path.lstrip("/")
        local_path = _STAGE_MAP.get(token)
        if not local_path or not os.path.exists(local_path):
            self.send_error(404, "Not found")
            return
        size = os.path.getsize(local_path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{os.path.basename(local_path)}"',
        )
        self.end_headers()
        with open(local_path, "rb") as f:
            while chunk := f.read(65536):
                self.wfile.write(chunk)

    def log_message(self, *args):  # suppress access logs
        pass


_staging_server = socketserver.TCPServer(("0.0.0.0", _FILE_PORT), _StagingHandler)
_staging_server.allow_reuse_address = True
threading.Thread(target=_staging_server.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------
# Startup: authenticate with Keystone and build boto3 client
# ---------------------------------------------------------------------------


def _require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set in openrc.sh"
        )
    return val


def _build_s3_client():
    auth_url = _require_env("OS_AUTH_URL")
    username = _require_env("OS_USERNAME")
    password = _require_env("OS_PASSWORD")
    project = _require_env("OS_PROJECT_NAME")
    user_domain = os.environ.get("OS_USER_DOMAIN_NAME", "Default")
    proj_domain = os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default")
    region = os.environ.get("OS_REGION_NAME") or None
    interface = os.environ.get("OS_INTERFACE", "public")

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


def _get_host_ip() -> str:
    """Return the primary outbound IP address of this host."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


mcp = FastMCP(
    "swift-mcp",
    instructions=(
        "Read-only access to OpenStack Swift object storage. "
        "IMPORTANT: This MCP server is the ONLY way to access Swift — no credentials "
        "or openrc.sh are available to shell commands or external scripts. "
        "Always use these tools instead of running boto3/Swift CLI directly.\n\n"
        "Typical workflows:\n"
        "- Explore: list_objects (serves only 'solutions-qa' container)\n"
        "- Inspect without downloading: head_object\n"
        "- Read small text files inline: get_object (returns content in JSON)\n"
        "- Download binary or large files: stage_object → curl the returned URL to disk\n"
        '  e.g.: stage_object(...) returns {"url": "http://host:8001/<token>"}, then\n'
        "  run: curl -fsSL <url> -o /local/path\n"
        "- Download all files for a UUID as a single archive: stage_uuid_bundle → curl the returned URL\n"
        '  e.g.: stage_uuid_bundle(uuid=...) returns {"url": "..."}, then\n'
        "  run: curl -fsSL <url> -o <uuid>.tgz && tar xzf <uuid>.tgz"
    ),
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8000")),
)


@mcp.tool()
def list_containers() -> str:
    """Return information about the single hardcoded container 'solutions-qa'."""
    try:
        # Check if the container exists by attempting to list it
        s3.head_bucket(Bucket=CONTAINER_NAME)
        return json.dumps(
            {
                "name": CONTAINER_NAME,
                "note": "This MCP server only serves the solutions-qa container",
            },
            indent=2,
        )
    except ClientError as exc:
        err = exc.response.get("Error", {})
        return f"Error: {err.get('Message') or err.get('Code') or str(exc)}"


@mcp.tool()
def list_objects(
    prefix: str = "",
    delimiter: str = "",
    max_keys: int = 1000,
) -> str:
    """
    List objects inside the 'solutions-qa' container.

    Args:
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
        kwargs: dict = {"Bucket": CONTAINER_NAME, "MaxKeys": 1000}
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
                "container": CONTAINER_NAME,
                "prefix": prefix,
                "truncated": len(objects) == max_keys
                and response.get("IsTruncated", False),
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
def get_object(key: str, encoding: str = "utf-8") -> str:
    """
    Read the content of an object from the 'solutions-qa' container.

    Objects larger than 512 MB are refused — use head_object to check size first.
    Text content is returned as a string in the "content" field (binary=false).
    Binary content is returned as base64 in the "content_base64" field (binary=true).

    To save a file locally: call this tool, then write the content to disk —
    decode base64 first for binary files (e.g. .tar.gz, .gz, images).
    For files larger than ~10 MB, call this tool from a subagent so the large
    response does not bloat the main agent context.

    Args:
        key:       Object key (full path, e.g. "logs/2024/app.log").
        encoding:  Text encoding to attempt (default "utf-8").
    """
    try:
        head = s3.head_object(Bucket=CONTAINER_NAME, Key=key)
        size = head.get("ContentLength", 0)

        if size > MAX_OBJECT_SIZE:
            return (
                f"Error: object is {size:,} bytes — exceeds the 512 MB read limit. "
                "Use head_object to inspect its metadata."
            )

        response = s3.get_object(Bucket=CONTAINER_NAME, Key=key)
        body = response["Body"].read()
        content_type = response.get("ContentType", "")

        text_types = ("text/", "json", "xml", "yaml", "javascript", "csv")
        if (
            any(t in content_type for t in text_types)
            or not content_type
            or content_type == "binary/octet-stream"
        ):
            try:
                return json.dumps(
                    {
                        "container": CONTAINER_NAME,
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
                "container": CONTAINER_NAME,
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
def stage_object(key: str) -> str:
    """
    Download an object from the 'solutions-qa' container to a local staging area
    and return an HTTP URL that the agent can use to fetch the file as raw binary —
    no base64, no JSON overhead, keeping the MCP context small.

    After calling this tool, download the file with:
        curl -fsSL <url> -o /local/destination/path

    The staged file is available until the MCP server restarts.
    Objects larger than 512 MB are refused — use head_object to check size first.

    Args:
        key:       Object key (full path, e.g. "backups/db.tar.gz").
    """
    try:
        _reap_staged_files()
        head = s3.head_object(Bucket=CONTAINER_NAME, Key=key)
        size = head.get("ContentLength", 0)

        if size > MAX_OBJECT_SIZE:
            return (
                f"Error: object is {size:,} bytes — exceeds the 512 MB limit. "
                "Use head_object to inspect its metadata."
            )

        token = secrets.token_hex(16)
        filename = os.path.basename(key.rstrip("/")) or "object"
        local_path = os.path.join(_STAGE_DIR, f"{token}_{filename}")

        if size == 0:
            open(
                local_path, "wb"
            ).close()  # 0-byte object; boto3 download_file fails on empty objects
        else:
            s3.download_file(CONTAINER_NAME, key, local_path)
        _STAGE_MAP[token] = local_path
        _STAGE_TIME[token] = time.monotonic()

        host_addr = os.environ.get("MCP_HOST_ADDR") or _get_host_ip()
        url = f"http://{host_addr}:{_FILE_PORT}/{token}"

        return json.dumps(
            {
                "url": url,
                "filename": filename,
                "size_bytes": size,
                "content_type": head.get("ContentType"),
                "local_path": local_path,
            },
            indent=2,
        )
    except ClientError as exc:
        err = exc.response.get("Error", {})
        return f"Error: {err.get('Message') or err.get('Code') or str(exc)}"


@mcp.tool()
def stage_uuid_bundle(uuid: str) -> str:
    """
    Download all objects whose key starts with the given UUID, pack them into
    a tar.gz archive named '<uuid>.tgz', stage it, and return an HTTP URL for
    download — identical transport to stage_object.

    The archive preserves the original key structure under a top-level directory
    named after the UUID, so:
        tar xzf <uuid>.tgz
    extracts into ./<uuid>/...

    After calling this tool, download the archive with:
        curl -fsSL <url> -o <uuid>.tgz

    Args:
        uuid:  Prefix to match (e.g. "a1b2c3d4-..."). All objects whose key
               starts with this string are included.
    """
    try:
        _reap_staged_files()

        # Collect all matching keys with their sizes
        objects = []
        kwargs: dict = {"Bucket": CONTAINER_NAME, "Prefix": uuid}
        while True:
            response = s3.list_objects_v2(**kwargs)
            objects.extend(
                {"key": obj["Key"], "size": obj["Size"]}
                for obj in response.get("Contents", [])
                if not obj["Key"].endswith(".img")
            )
            if not response.get("IsTruncated"):
                break
            kwargs["ContinuationToken"] = response["NextContinuationToken"]

        if not objects:
            return f"Error: no objects found with prefix '{uuid}'"

        # Download into a temp directory, preserving key paths under uuid/
        with tempfile.TemporaryDirectory(prefix="swift_mcp_bundle_") as work_dir:
            uuid_dir = os.path.join(work_dir, uuid)
            for obj in objects:
                key, size = obj["key"], obj["size"]
                # Strip the uuid prefix so paths inside the archive are relative
                rel = key[len(uuid) :].lstrip("/")
                dest = (
                    os.path.join(uuid_dir, rel)
                    if rel
                    else os.path.join(uuid_dir, os.path.basename(key))
                )
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if size == 0:
                    open(
                        dest, "wb"
                    ).close()  # boto3 download_file fails on empty objects
                else:
                    s3.download_file(CONTAINER_NAME, key, dest)

            # Build the tgz in the staging directory
            tgz_name = f"{uuid}.tgz"
            tgz_path = os.path.join(_STAGE_DIR, tgz_name)
            with tarfile.open(tgz_path, "w:gz") as tar:
                tar.add(uuid_dir, arcname=uuid)

        token = secrets.token_hex(16)
        _STAGE_MAP[token] = tgz_path
        _STAGE_TIME[token] = time.monotonic()

        host_addr = os.environ.get("MCP_HOST_ADDR") or _get_host_ip()
        url = f"http://{host_addr}:{_FILE_PORT}/{token}"
        size = os.path.getsize(tgz_path)

        return json.dumps(
            {
                "url": url,
                "filename": tgz_name,
                "size_bytes": size,
                "object_count": len(objects),
            },
            indent=2,
        )
    except ClientError as exc:
        err = exc.response.get("Error", {})
        return f"Error: {err.get('Message') or err.get('Code') or str(exc)}"


@mcp.tool()
def head_object(key: str) -> str:
    """
    Return metadata for an object in the 'solutions-qa' container without downloading its content.

    Args:
        key:       Object key (full path).
    """
    try:
        r = s3.head_object(Bucket=CONTAINER_NAME, Key=key)
        return json.dumps(
            {
                "container": CONTAINER_NAME,
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
