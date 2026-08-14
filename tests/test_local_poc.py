import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


POC_PATH = Path(__file__).parents[1] / "poc" / "run_local_chrome.py"
SPEC = importlib.util.spec_from_file_location("run_local_chrome", POC_PATH)
POC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(POC)


def test_local_artifact_preserves_detail_and_metadata_is_sanitized(tmp_path):
    detail = SimpleNamespace(
        folio="7954",
        branch="698",
        browserbase_session_id="local-chrome",
        detalle_raw={"customer_name": "Customer detail stays in artifact"},
        promoted={"comentario": "detail comment"},
    )
    artifact = POC.build_artifact(detail)
    artifact_bytes = (json.dumps(artifact, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    artifact_path = tmp_path / "autopro_sale_detail_batch.v1_7954.json"
    artifact_path.write_bytes(artifact_bytes)

    metadata = POC.artifact_metadata(
        artifact_path=artifact_path,
        artifact=artifact,
        artifact_bytes=artifact_bytes,
    )

    assert artifact["schema_version"] == "autopro_sale_detail_batch.v1"
    assert artifact["counts"] == {"requested": 1, "success": 1, "failed": 0}
    assert artifact["results"][0]["detalle_raw"]["customer_name"] == "Customer detail stays in artifact"
    assert artifact["results"][0]["comentario"] == "detail comment"
    assert metadata == {
        "artifact_path": str(artifact_path),
        "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "counts": {"requested": 1, "success": 1, "failed": 0},
        "schema_version": "autopro_sale_detail_batch.v1",
        "folio": "7954",
    }
    assert "customer_name" not in metadata
