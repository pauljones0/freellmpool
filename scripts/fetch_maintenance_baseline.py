"""Fetch bounded public data from a successful, trusted maintenance workflow.

GitHub REST contracts: https://docs.github.com/en/rest/actions/artifacts
Signed artifact redirects receive no GitHub Authorization header.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import stat
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

DEFAULT_REPOSITORY = "pauljones0/freellmpool"
WORKFLOW_FILE = "provider-evidence-review.yml"
ARTIFACT_NAME = "freellmpool-public-baseline"
BASELINE_FILE = "public-baseline.json"
MAX_BYTES = 8_000_000
MAX_RUN_PAGES = 5
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
Validator = Callable[[Any], dict[str, Any]]


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def bounded_json(content: bytes) -> Any:
    if len(content) > MAX_BYTES:
        raise ValueError("Maintenance data exceeds its size bound")
    try:
        value = json.loads(content, object_pairs_hook=_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid number")))
    except (UnicodeDecodeError, RecursionError) as exc:
        raise ValueError("Invalid maintenance JSON") from exc
    return value


class GitHubAPI:
    """Restricted GitHub transport; response bodies and tokens never enter errors."""

    def __init__(self, repository: str, token: str, *, client: Any = None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid GitHub repository")
        if not token or any(char.isspace() for char in token):
            raise ValueError("A GitHub token is required")
        self.repository = repository
        self._token = token
        self._client = client

    def close(self) -> None:
        """Requests close their own responses; injected test transports are caller-owned."""

    def _read(self, method: str, url: str, *, authenticated: bool,
              payload: dict[str, Any] | None = None) -> tuple[int, str | None, bytes]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if authenticated:
            if urlsplit(url).netloc != "api.github.com" or not url.startswith("https://"):
                raise ValueError("GitHub credentials cannot leave the API host")
            headers["Authorization"] = "Bearer " + self._token
        if self._client is None:
            return _stdlib_read(method, url, headers, payload)
        import httpx  # Optional injected transport; static incidents need only the stdlib.
        try:
            with self._client.stream(method, url, headers=headers, json=payload,
                                     follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    return response.status_code, response.headers.get("location"), b""
                if not 200 <= response.status_code < 300:
                    raise ValueError(f"GitHub maintenance request failed (HTTP {response.status_code})")
                length = response.headers.get("content-length")
                if length is not None and (not length.isdigit() or int(length) > MAX_BYTES):
                    raise ValueError("GitHub maintenance response exceeds its size bound")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError("GitHub maintenance response exceeds its size bound")
                    chunks.append(chunk)
                return response.status_code, None, b"".join(chunks)
        except httpx.HTTPError:
            raise ValueError("GitHub maintenance transport failed") from None

    def json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        prefix = "/repos/" + self.repository
        if (method not in {"GET", "POST", "PATCH"}
                or not (path == prefix or path.startswith(prefix + "/"))
                or ".." in path or "\\" in path or "#" in path):
            raise ValueError("GitHub request escaped its repository boundary")
        status, _, body = self._read(method, "https://api.github.com" + path,
                                    authenticated=True, payload=payload)
        if status >= 300:
            raise ValueError("GitHub API redirects are not accepted")
        return bounded_json(body)

    def download_artifact(self, artifact_id: int) -> bytes:
        if type(artifact_id) is not int or artifact_id <= 0:
            raise ValueError("Invalid artifact identifier")
        path = f"/repos/{self.repository}/actions/artifacts/{artifact_id}/zip"
        status, location, body = self._read("GET", "https://api.github.com" + path,
                                           authenticated=True)
        if status == 200:
            return body
        if status != 302 or not location:
            raise ValueError("Unexpected artifact download response")
        parsed = urlsplit(location)
        host = parsed.hostname or ""
        try:
            allowed = (parsed.scheme == "https" and parsed.port in {None, 443}
                       and not parsed.username and not parsed.password and not parsed.fragment
                       and (host.endswith(".blob.core.windows.net")
                            or host.endswith(".actions.githubusercontent.com")
                            or host == "objects.githubusercontent.com"))
        except ValueError:
            allowed = False
        if not allowed:
            raise ValueError("Artifact redirect host is not approved")
        status, _, body = self._read("GET", location, authenticated=False)
        if status != 200:
            raise ValueError("Artifact storage redirects are not accepted")
        return body


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _stdlib_read(method: str, url: str, headers: dict[str, str],
                 payload: dict[str, Any] | None) -> tuple[int, str | None, bytes]:
    data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    if data is not None:
        headers = {**headers, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())
    try:
        with opener.open(request, timeout=30) as response:
            status = int(response.status)
            if status in {301, 302, 303, 307, 308}:
                return status, response.headers.get("Location"), b""
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_BYTES):
                raise ValueError("GitHub maintenance response exceeds its size bound")
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("GitHub maintenance response exceeds its size bound")
            return status, None, bytes(body)
    except urllib.error.HTTPError as error:
        try:
            if error.code in {301, 302, 303, 307, 308}:
                return error.code, error.headers.get("Location"), b""
            raise ValueError(f"GitHub maintenance request failed (HTTP {error.code})") from None
        finally:
            error.close()
    except (urllib.error.URLError, OSError):
        raise ValueError("GitHub maintenance transport failed") from None


def _baseline_validator(value: Any) -> dict[str, Any]:
    from freellmpool.maintenance import validate_public_baseline
    result = validate_public_baseline(value)
    if not isinstance(result, dict):
        raise ValueError("Invalid normalized baseline")
    return dict(result)


def read_baseline_archive(content: bytes, revision: str, *,
                          validator: Validator = _baseline_validator) -> dict[str, Any]:
    if len(content) > MAX_BYTES or not SHA.fullmatch(revision):
        raise ValueError("Invalid public baseline archive")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) != 1:
                raise ValueError("Baseline archive must contain exactly one public file")
            entry = entries[0]
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if (entry.filename != BASELINE_FILE or entry.is_dir() or entry.file_size > MAX_BYTES
                    or kind not in {0, stat.S_IFREG} or entry.flag_bits & 1):
                raise ValueError("Invalid baseline archive member")
            document = validator(bounded_json(archive.read(entry)))
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError):
        raise ValueError("Invalid public baseline archive") from None
    if document.get("source_revision") != revision:
        raise ValueError("Baseline does not match its trusted workflow revision")
    return document


def fetch_baseline(api: GitHubAPI, *, current_run_id: int,
                   validator: Validator = _baseline_validator) -> dict[str, Any] | None:
    prefix = "/repos/" + api.repository
    repository = api.json("GET", prefix)
    branch = repository.get("default_branch") if isinstance(repository, dict) else None
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9_.\-/]{1,200}", branch):
        raise ValueError("Cannot establish the trusted default branch")
    for page in range(1, MAX_RUN_PAGES + 1):
        query = urlencode({"branch": branch, "status": "success", "per_page": 100, "page": page})
        response = api.json("GET", f"{prefix}/actions/workflows/{WORKFLOW_FILE}/runs?{query}")
        runs = response.get("workflow_runs") if isinstance(response, dict) else None
        if not isinstance(runs, list) or len(runs) > 100:
            raise ValueError("Invalid workflow run listing")
        for run in runs:
            if not isinstance(run, dict):
                raise ValueError("Invalid workflow run record")
            run_id = run.get("id")
            revision = run.get("head_sha")
            if (type(run_id) is not int or not 0 < run_id < current_run_id
                    or run.get("event") not in {"schedule", "workflow_dispatch"}
                    or run.get("conclusion") != "success" or run.get("head_branch") != branch
                    or run.get("path") != ".github/workflows/" + WORKFLOW_FILE
                    or run.get("head_repository", {}).get("full_name") != api.repository
                    or not isinstance(revision, str) or not SHA.fullmatch(revision)):
                continue
            response = api.json("GET", f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100")
            artifacts = response.get("artifacts") if isinstance(response, dict) else None
            if not isinstance(artifacts, list) or len(artifacts) >= 100:
                raise ValueError("Invalid or incomplete artifact listing")
            for artifact in artifacts:
                if not isinstance(artifact, dict) or artifact.get("name") != ARTIFACT_NAME:
                    continue
                if artifact.get("expired") is not False:
                    continue
                size = artifact.get("size_in_bytes")
                artifact_id = artifact.get("id")
                origin = artifact.get("workflow_run", {})
                if (type(size) is not int or not 0 < size <= MAX_BYTES
                        or not isinstance(artifact_id, int) or isinstance(artifact_id, bool)
                        or artifact_id <= 0
                        or not isinstance(origin, dict) or origin.get("id") != run_id
                        or origin.get("head_sha") != revision):
                    raise ValueError("Invalid public artifact provenance or size")
                return read_baseline_archive(api.download_artifact(artifact_id), revision,
                                             validator=validator)
        if len(runs) < 100:
            return None
    raise ValueError("Workflow history exceeded its bounded search")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--current-run-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    api: GitHubAPI | None = None
    try:
        api = GitHubAPI(args.repository, os.environ.get("GH_TOKEN", ""))
        document = fetch_baseline(api, current_run_id=args.current_run_id)
        if document is None:
            print("No previous trusted public baseline; this run will establish one.")
            return 0
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(args.output)
        print("Loaded a validated baseline from the trusted public workflow.")
        return 0
    except (ValueError, OSError, KeyError, TypeError, RecursionError):
        print("Public baseline retrieval failed validation; inspect the maintenance workflow.")
        return 1
    finally:
        if api is not None:
            api.close()


if __name__ == "__main__":
    raise SystemExit(main())
