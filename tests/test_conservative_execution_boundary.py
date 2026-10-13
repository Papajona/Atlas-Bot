import ast
from pathlib import Path

import pytest

from app.execution import RiskBlocked, _require_conservative_execution_ready


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("live", [False, True])
def test_conservative_execution_fails_closed_until_trusted_metadata_is_wired(live):
    with pytest.raises(RiskBlocked, match="not yet wired|not certified"):
        _require_conservative_execution_ready(live=live)


def test_conservative_profile_does_not_trust_client_supplied_metadata():
    source = (ROOT / "app/execution.py").read_text()
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "execute_signal")
    block = ast.get_source_segment(source, function) or ""
    assert 'if strategy == "atlas_conservative_v1":' in block
    assert "_require_conservative_execution_ready(live=live)" in block
    assert 'signal.get("instrument_metadata")' not in block
