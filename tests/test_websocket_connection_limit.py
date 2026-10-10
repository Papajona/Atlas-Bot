import asyncio


def test_websocket_reservations_enforce_limit_and_cleanup():
    import app.main as main_module

    async def exercise():
        user_id = "test-risk-officer"
        main_module._ws_user_connections.clear()
        limit = main_module._WS_MAX_CONNECTIONS_PER_USER
        reservations = list(range(1, limit + 1))
        results = [
            await main_module._reserve_ws_connection(user_id, connection_id)
            for connection_id in reservations
        ]
        assert all(results)
        assert await main_module._reserve_ws_connection(user_id, limit + 1) is False
        assert len(main_module._ws_user_connections[user_id]) == limit

        await main_module._release_ws_connection(user_id, reservations[0])
        assert await main_module._reserve_ws_connection(user_id, limit + 1) is True
        for connection_id in reservations[1:] + [limit + 1]:
            await main_module._release_ws_connection(user_id, connection_id)
        assert user_id not in main_module._ws_user_connections

    asyncio.run(exercise())


def test_websocket_reservation_cleanup_is_idempotent():
    import app.main as main_module

    async def exercise():
        user_id = "cleanup-user"
        main_module._ws_user_connections.clear()
        assert await main_module._reserve_ws_connection(user_id, 42)
        await main_module._release_ws_connection(user_id, 42)
        await main_module._release_ws_connection(user_id, 42)
        assert user_id not in main_module._ws_user_connections

    asyncio.run(exercise())
