"""Resolve the Groq settings of revisions currently receiving Cloud Run API traffic."""
from __future__ import annotations

import json
import re
import sys
from decimal import Decimal, InvalidOperation
from typing import Any


_REVISION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}\Z")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
_TOKEN_RE = re.compile(r"[1-9][0-9]{0,4}\Z")


def serving_revisions(service: dict[str, Any]) -> list[str]:
    """Return only observed revisions receiving Cloud Run traffic, not pending targets."""
    status = service.get("status")
    if not isinstance(status, dict):
        raise ValueError("API service response has no observed status")

    # Cloud Run v2 distinguishes desired Service.traffic from output-only
    # status.trafficStatuses. Never use the desired allocation during rollout.
    if "trafficStatuses" in status:
        traffic = status.get("trafficStatuses")
        observed_v2 = True
        if not isinstance(traffic, list) or not traffic:
            raise ValueError("API service has no observed trafficStatuses; refusing worker rollout")
    elif "traffic" in status:
        # Cloud Run v1 reports its observed targets as status.traffic.
        traffic = status.get("traffic")
        observed_v2 = False
        if not isinstance(traffic, list) or not traffic:
            raise ValueError("API service has no observed v1 traffic allocation")
    else:
        raise ValueError("API service has no observed traffic state; refusing requested traffic targets")

    latest_ready = (
        status.get("latestReadyRevisionName")
        or status.get("latestReadyRevision")
    )
    if isinstance(latest_ready, str):
        latest_ready = latest_ready.rsplit("/", 1)[-1]
    active: set[str] = set()
    total = Decimal("0")
    for target in traffic:
        if not isinstance(target, dict):
            raise ValueError("API service has an invalid observed traffic target")
        raw_percent = target.get("percent")
        if raw_percent is None and target.get("tag"):
            # A tagged target without a traffic allocation is not serving
            # production requests and must not determine worker configuration.
            continue
        if raw_percent is None:
            raise ValueError("observed API traffic target has no explicit percent allocation")
        try:
            percent = Decimal(str(raw_percent))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("observed API traffic target has an invalid percent allocation") from exc
        if not percent.is_finite() or percent < 0 or percent > 100:
            raise ValueError("observed API traffic target percent must be finite and between 0 and 100")
        if percent == 0:
            continue

        revision = target.get("revision") or target.get("revisionName")
        if not revision and not observed_v2 and target.get("latestRevision") is True:
            revision = latest_ready
        if not isinstance(revision, str):
            raise ValueError("active observed API traffic target has no resolvable revision name")
        revision = revision.rsplit("/", 1)[-1]
        if not _REVISION_RE.fullmatch(revision):
            raise ValueError("active observed API traffic target has an invalid revision name")
        active.add(revision)
        total += percent

    if not active or total != Decimal("100"):
        raise ValueError(
            f"observed API traffic allocations must resolve to revisions totaling exactly 100 percent; observed {total}"
        )
    return sorted(active)

def _container_env(revision: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read the first container's environment from Cloud Run v1 or v2 revision JSON."""
    spec = revision.get("spec")
    containers = spec.get("containers") if isinstance(spec, dict) else None
    if not isinstance(containers, list):
        containers = revision.get("containers")
    if not isinstance(containers, list) or not containers or not isinstance(containers[0], dict):
        raise ValueError("serving API revision has no first container")
    entries = containers[0].get("env")
    if not isinstance(entries, list):
        raise ValueError("serving API revision has no environment list")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("serving API revision contains an invalid environment entry")
        name = entry["name"]
        if name in result:
            raise ValueError(f"serving API revision contains duplicate environment key {name}")
        result[name] = entry
    return result


def resolve(revision: dict[str, Any], expected_project_id: str) -> tuple[str, str, str, str]:
    """Return validated model, token cap, enablement and pinned optional secret from one revision."""
    env = _container_env(revision)
    values: list[str] = []
    for name in ("GROQ_MODEL", "GROQ_MAX_COMPLETION_TOKENS", "GROQ_RESEARCH_REVIEW_ENABLED"):
        entry = env.get(name)
        value = entry.get("value") if entry else None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"serving API revision is missing a literal {name} value")
        values.append(value.strip())
    model, tokens, enabled = values
    if not _MODEL_RE.fullmatch(model):
        raise ValueError("serving API GROQ_MODEL has an invalid value")
    if not _TOKEN_RE.fullmatch(tokens) or not 1 <= int(tokens) <= 8192:
        raise ValueError("serving API GROQ_MAX_COMPLETION_TOKENS must be between 1 and 8192")
    if enabled.lower() not in {"true", "false"}:
        raise ValueError("serving API GROQ_RESEARCH_REVIEW_ENABLED must be true or false")

    secret_entry = env.get("GROQ_API_KEY")
    secret_ref = "__NONE__"
    if secret_entry is not None:
        value_source = secret_entry.get("valueSource") or secret_entry.get("valueFrom") or {}
        secret = value_source.get("secretKeyRef") if isinstance(value_source, dict) else None
        if not isinstance(secret, dict):
            raise ValueError("serving API GROQ_API_KEY is not backed by a Secret Manager reference")
        raw_secret_name = secret.get("secret") or secret.get("name")
        if not isinstance(raw_secret_name, str) or not raw_secret_name:
            raise ValueError("serving API GROQ_API_KEY has no secret resource name")
        qualified = re.fullmatch(r"projects/([^/]+)/secrets/([^/]+)", raw_secret_name)
        if qualified:
            secret_project, secret_name = qualified.groups()
            if secret_project != expected_project_id:
                raise ValueError(
                    "serving API GROQ_API_KEY project does not match GCP_PROJECT_ID; refusing cross-project substitution"
                )
        elif "/" in raw_secret_name:
            raise ValueError("serving API GROQ_API_KEY has an invalid qualified Secret Manager resource name")
        else:
            # A short Secret Manager name is scoped to the API service project.
            secret_name = raw_secret_name
        version = secret.get("version") or secret.get("key")
        version = str(version) if version is not None else ""
        if secret_name != "atlas-groq-api-key" or not re.fullmatch(r"[1-9][0-9]*", version):
            raise ValueError("serving API GROQ_API_KEY does not use an immutable atlas-groq-api-key version")
        secret_ref = f"{secret_name}:{version}"
    return model, tokens, enabled.lower(), secret_ref


def require_consistent_configs(configs: list[tuple[str, str, str, str]]) -> tuple[str, str, str, str]:
    """Refuse split-traffic API revisions that would give workers ambiguous settings."""
    if not configs:
        raise ValueError("no active API revision configuration was resolved")
    if any(config != configs[0] for config in configs[1:]):
        raise ValueError("API revisions receiving traffic have different Groq configurations")
    return configs[0]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Cloud Run response must be a JSON object")
        if args == ["--traffic-revisions"]:
            print("\n".join(serving_revisions(payload)))
        elif len(args) == 2 and args[0] == "--revision-config":
            expected_project_id = args[1]
            if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", expected_project_id):
                raise ValueError("GCP_PROJECT_ID is not a valid Google Cloud project ID")
            print("\n".join(resolve(payload, expected_project_id)))
        else:
            raise ValueError("Use --traffic-revisions or --revision-config GCP_PROJECT_ID")
    except (AttributeError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
