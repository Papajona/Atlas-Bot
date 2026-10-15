import asyncio

import app.main as main


def test_reconciliation_failure_incident_fires_once_after_three_consecutive_errors(monkeypatch):
    events = []
    monkeypatch.setattr(main, "_reconciliation_failure_state", {})

    async def fake_open_incident(**kwargs):
        events.append(kwargs)
        return len(events)

    monkeypatch.setattr(main, "open_incident", fake_open_incident)

    async def run():
        for _ in range(2):
            assert await main._record_reconciliation_failure("oanda", TimeoutError("provider unavailable")) < 3
        assert events == []
        assert await main._record_reconciliation_failure("oanda", TimeoutError("provider unavailable")) == 3
        assert len(events) == 1
        assert events[0]["severity"] == "CRITICAL"
        assert events[0]["category"] == "RECONCILIATION"
        assert events[0]["detail"]["consecutive_failures"] == 3
        # Continued outage polls do not create a fresh incident every cycle.
        assert await main._record_reconciliation_failure("oanda", TimeoutError("provider unavailable")) == 4
        assert len(events) == 1

    asyncio.run(run())


def test_reconciliation_recovery_resets_failure_streak_and_allows_new_episode_alert(monkeypatch):
    events = []
    audits = []
    monkeypatch.setattr(main, "_reconciliation_failure_state", {})

    async def fake_open_incident(**kwargs):
        events.append(kwargs)
        return len(events)

    async def fake_audit(event, detail):
        audits.append((event, detail))

    monkeypatch.setattr(main, "open_incident", fake_open_incident)
    monkeypatch.setattr(main, "_safe_audit", fake_audit)

    async def run():
        for _ in range(3):
            await main._record_reconciliation_failure("crypto", OSError("exchange down"))
        await main._record_reconciliation_success("crypto")
        assert audits and audits[-1][0] == "RECONCILIATION_PROVIDER_RECOVERED"
        for _ in range(3):
            await main._record_reconciliation_failure("crypto", OSError("exchange down again"))

    asyncio.run(run())
    assert len(events) == 2
    assert events[0]["key"] != events[1]["key"]
    assert events[0]["detail"]["episode"] == 1
    assert events[1]["detail"]["episode"] == 2
