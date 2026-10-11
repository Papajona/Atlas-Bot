import asyncio


def test_shutdown_waits_for_cancelled_worker_cleanup(monkeypatch):
    import app.main as main_module

    cleanup_finished = []

    async def worker():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleanup_finished.append(True)

    async def exercise():
        task = asyncio.create_task(worker())
        monkeypatch.setattr(main_module, "_worker_tasks", {task})
        monkeypatch.setattr(main_module, "_background_task", None)
        monkeypatch.setattr(main_module, "_usdt_task", None)
        await asyncio.sleep(0)
        await main_module.shutdown()
        assert task.done()
        assert cleanup_finished == [True]
        assert main_module._worker_tasks == set()

    asyncio.run(exercise())
