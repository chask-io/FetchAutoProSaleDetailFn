#!/usr/bin/env python3
"""Run one read-only AutoPro detail fetch with local headless Chrome."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.append(str(REPO_ROOT / "layers" / "browserbase_layer" / "python"))

from backend.autopro_detail_client import (  # noqa: E402
    LOCAL_CHROME_MODE,
    AutoProSaleDetailClient,
    normalize_folio,
)


AUTOPRO_USERNAME_SECRET_UUID = "0d123d55-45fc-4211-99f0-6d7082e0b15e"
AUTOPRO_PASSWORD_SECRET_UUID = "b79b9aba-c64e-49ad-b211-27bc1a4be5c8"
BRANCH = "698"
TENANT_ID = "daniel-achondo"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folio", help="Exactly one numeric AutoPro folio")
    parser.add_argument(
        "--profile",
        default="daniel-achondo",
        help="Chask profile name used only for in-process SDK secret retrieval",
    )
    parser.add_argument(
        "--profile-file",
        type=Path,
        default=Path.home() / ".chask" / "profiles.json",
        help="Path to the local Chask profile metadata file",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "poc-artifacts",
        help="Directory for sanitized artifact and evidence log",
    )
    return parser.parse_args()


def load_sdk_client(profile_file: Path, profile_name: str):
    from chask_sdk import ChaskClient

    metadata = json.loads(profile_file.read_text(encoding="utf-8"))
    profile = metadata["profiles"][profile_name]
    api_url = str(profile["apiUrl"]).rstrip("/")
    if api_url.endswith("/api/v2"):
        api_url = api_url[: -len("/api/v2")]
    return ChaskClient(
        token=profile["apiKey"],
        base_url=api_url,
        cache_ttl=0,
        timeout=30,
    )


def resolve_credentials(client) -> tuple[str, str]:
    """Resolve values in memory. Do not log or persist either value."""
    username = client.get_secret(AUTOPRO_USERNAME_SECRET_UUID).reveal()
    password = client.get_secret(AUTOPRO_PASSWORD_SECRET_UUID).reveal()
    return str(username).strip(), str(password).strip()


def build_artifact(detail) -> dict[str, Any]:
    record = {
        "status": "success",
        "tenant_id": TENANT_ID,
        "folio": detail.folio,
        "folio_venta": detail.folio,
        "branch": detail.branch,
        "browserbase_session_id": detail.browserbase_session_id,
        "detalle_raw": detail.detalle_raw,
        **detail.promoted,
    }
    collection = {
        "schema_version": "autopro_sale_detail_batch.v1",
        "tenant_id": TENANT_ID,
        "branch": BRANCH,
        "source_file_uuid": None,
        "requested_folios": [detail.folio],
        "counts": {"requested": 1, "success": 1, "failed": 0},
        "failures": [],
        "results": [record],
    }
    return collection


def artifact_metadata(*, artifact_path: Path, artifact: dict[str, Any], artifact_bytes: bytes) -> dict[str, Any]:
    return {
        "artifact_path": str(artifact_path),
        "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "counts": artifact["counts"],
        "schema_version": artifact["schema_version"],
        "folio": artifact["requested_folios"][0],
    }


def main() -> int:
    args = parse_args()
    folio = normalize_folio(args.folio)
    if args.profile != "daniel-achondo":
        raise SystemExit("This POC is restricted to the daniel-achondo profile.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = args.output_dir / f"autopro_sale_detail_batch.v1_{folio}.json"
    evidence_path = args.output_dir / f"autopro_local_chrome_{folio}.log.json"
    evidence: dict[str, Any] = {
        "artifact_path": str(artifact_path),
        "artifact_sha256": None,
        "counts": {"requested": 1, "success": 0, "failed": 1},
        "schema_version": "autopro_sale_detail_batch.v1",
        "folio": folio,
    }
    try:
        client = load_sdk_client(args.profile_file, args.profile)
        username, password = resolve_credentials(client)
        detail_client = AutoProSaleDetailClient(
            username=username,
            password=password,
            folio=folio,
            branch=BRANCH,
            browser_mode=LOCAL_CHROME_MODE,
        )
        detail = detail_client.fetch_detail()
        artifact = build_artifact(detail)
        artifact_bytes = (json.dumps(artifact, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        artifact_path.write_bytes(artifact_bytes)
        evidence = artifact_metadata(artifact_path=artifact_path, artifact=artifact, artifact_bytes=artifact_bytes)
        return_code = 0
    except Exception:
        return_code = 1
    finally:
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, ensure_ascii=False))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
