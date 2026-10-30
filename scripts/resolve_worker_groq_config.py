"""Read validated Groq settings from the active Cloud Run API service revision."""
from __future__ import annotations

import json
import re
import sys
from typing import Any


def _container_env(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Support both Cloud Run v1 and v2 service response shapes."""
    service_spec = service.get("spec")
    template = service_spec.get("template") if isinstance(service_spec, dict) else None
    if not isinstance(template, dict):
        template = service.get("template")
    if not isinstance(template, dict):
        raise ValueError("API service response has no revision template")
    template_spec = template.get("spec")
    containers = template_spec.get("containers") if isinstance(template_spec, dict) else None
    if not isinstance(containers, list):
        containers = template.get("containers")
    if not isinstance(containers, list) or not containers or not isinstance(containers[0], dict):
        raise ValueError("API service response has no first container")
    entries = containers[0].get("env")
    if not isinstance(entries, list):
        raise ValueError("API service response has no environment list")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("API service contains an invalid environment entry")
        name = entry["name"]
        if name in result:
            raise ValueError(f"API service contains duplicate environment key {name}")
        result[name] = entry
    return result


def resolve(service: dict[str, Any]) -> tuple[str, str, str, str]:
    env = _container_env(service)
    values: list[str] = []
    for name in ("GROQ_MODEL", "GROQ_MAX_COMPLETION_TOKENS", "GROQ_RESEARCH_REVIEW_ENABLED"):
        entry = env.get(name)
        value = entry.get("value") if entry else None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"deployed API is missing a literal {name} value")
        values.append(value.strip())
    model, tokens, enabled = values
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
        raise ValueError("deployed API GROQ_MODEL has an invalid value")
    if not re.fullmatch(r"[1-9][0-9]{0,4}", tokens) or int(tokens) > 32768:
        raise ValueError("deployed API GROQ_MAX_COMPLETION_TOKENS has an invalid value")
    if enabled.lower() not in {"true", "false"}:
        raise ValueError("deployed API GROQ_RESEARCH_REVIEW_ENABLED must be true or false")

    secret_entry = env.get("GROQ_API_KEY")
    secret_ref = "__NONE__"
    if secret_entry is not None:
        value_source = secret_entry.get("valueSource") or secret_entry.get("valueFrom") or {}
        secret = value_source.get("secretKeyRef") if isinstance(value_source, dict) else None
        if not isinstance(secret, dict):
            raise ValueError("deployed API GROQ_API_KEY is not backed by a Secret Manager reference")
        secret_name = secret.get("secret") or secret.get("name")
        secret_name = secret_name.rsplit("/", 1)[-1] if isinstance(secret_name, str) else None
        version = secret.get("version") or secret.get("key")
        version = str(version) if version is not None else ""
        if secret_name != "atlas-groq-api-key" or not re.fullmatch(r"[1-9][0-9]*", version):
            raise ValueError("deployed API GROQ_API_KEY does not use an immutable atlas-groq-api-key version")
        secret_ref = f"{secret_name}:{version}"
    return model, tokens, enabled.lower(), secret_ref


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("API service response must be an object")
        print("\n".join(resolve(payload)))
    except (AttributeError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
