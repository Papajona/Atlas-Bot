from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from .config import settings



@contextmanager
def model_artifact_lock(model_path: str | Path):
    """Serialize model pointer reads and mutations with inter-process locking."""
    model_file = Path(model_path)
    lock_path = model_file.parent / ".model-artifacts.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_signature(digest: str) -> str:
    """Sign a model digest with Ed25519; legacy HMAC is migration-only."""
    private_pem = str(settings.model_signing_private_key or "")
    if private_pem:
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            key = load_pem_private_key(private_pem.encode(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                return ""
            return base64.urlsafe_b64encode(key.sign(digest.encode())).decode().rstrip("=")
        except Exception:
            return ""
    key = str(settings.model_signing_key or "")
    if not key:
        return ""
    return hmac.new(key.encode(), digest.encode(), hashlib.sha256).hexdigest()


def read_model_manifest(model_path: str | Path) -> dict:
    """Read the atomic model pointer, or the legacy .meta.json sidecar."""
    model_file = Path(model_path)
    try:
        with model_file.open("rb") as handle:
            looks_like_pointer = handle.read(1) == b"{"
        pointer = (
            json.loads(model_file.read_text(encoding="utf-8"))
            if looks_like_pointer else None
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pointer = None
    if isinstance(pointer, dict) and pointer.get("artifact_pointer_schema") == 1:
        return pointer

    sidecar = model_file.with_suffix(model_file.suffix + ".meta.json")
    if not sidecar.is_file():
        return {}
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve_model_artifact(model_path: str | Path, manifest: dict) -> Path:
    """Resolve a versioned artifact pointer without allowing paths outside its bundle dir."""
    model_file = Path(model_path)
    artifact_ref = manifest.get("artifact_file")
    if not artifact_ref:
        return model_file

    relative = Path(str(artifact_ref))
    expected_directory = f"{model_file.name}.versions"
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != expected_directory
        or relative.parts[1] in {"", ".", ".."}
    ):
        raise ValueError("model manifest contains an invalid artifact pointer")

    versions_dir = model_file.parent / expected_directory
    resolved_versions_dir = versions_dir.resolve()
    if resolved_versions_dir.parent != model_file.parent.resolve():
        raise ValueError("model version directory escapes the model directory")
    artifact = versions_dir / relative.parts[1]
    if artifact.resolve().parent != resolved_versions_dir:
        raise ValueError("model artifact pointer escapes its version directory")
    return artifact


def verify_model_file(
    model_path: str | Path,
    expected_sha256: str,
    expected_signature: str = "",
) -> bool:
    model_file = Path(model_path)
    if not model_file.is_file() or not expected_sha256:
        return False
    try:
        with model_artifact_lock(model_file):
            manifest = read_model_manifest(model_file)
            artifact_file = resolve_model_artifact(model_file, manifest)
            if not artifact_file.is_file():
                return False
            digest = sha256_file(artifact_file).lower()
        if digest != str(expected_sha256).lower():
            return False
        required = str(expected_signature or manifest.get("manifest_signature") or "")
        if required:
            return verify_artifact_signature(digest, required)
        # Production live models require an independent manifest signature.
        if settings.environment in {"staging", "production"}:
            return False
        return True
    except (OSError, TypeError, ValueError):
        return False


def backup_champion(model_path: str | Path) -> Path | None:
    p = Path(model_path)
    with model_artifact_lock(p):
        if not p.exists():
            return None
        backup = p.with_name(p.name + ".previous")
        shutil.copy2(p, backup)
        meta = p.with_suffix(p.suffix + ".meta.json")
        if meta.exists():
            shutil.copy2(meta, Path(str(backup) + ".meta.json"))
        return backup


def rollback_champion(model_path: str | Path) -> bool:
    p = Path(model_path)
    with model_artifact_lock(p):
        backup = p.with_name(p.name + ".previous")
        if not backup.exists():
            return False
        # For versioned models this single rename switches the complete model/manifest pointer.
        os.replace(backup, p)
        backup_meta = Path(str(backup) + ".meta.json")
        meta = p.with_suffix(p.suffix + ".meta.json")
        if backup_meta.exists():
            os.replace(backup_meta, meta)
        else:
            meta.unlink(missing_ok=True)
        return True


def verify_artifact_signature(digest: str, signature: str) -> bool:
    signature = str(signature or "")
    public_pem = str(settings.model_signing_public_key or "")
    if public_pem and signature:
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            key = load_pem_public_key(public_pem.encode())
            if not isinstance(key, Ed25519PublicKey):
                return False
            padded = signature + "=" * (-len(signature) % 4)
            key.verify(base64.urlsafe_b64decode(padded.encode()), digest.encode())
            return True
        except Exception:
            return False
    # Legacy HMAC verification is kept only so already-deployed 3.10.44 artifacts can be
    # migrated/retrained; staging/production config now requires the Ed25519 public key.
    legacy = str(settings.model_signing_key or "")
    return bool(legacy and signature) and hmac.compare_digest(
        hmac.new(legacy.encode(), digest.encode(), hashlib.sha256).hexdigest(), signature
    )
