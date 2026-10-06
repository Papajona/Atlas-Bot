            if last_entry is not None:
                if last_entry.tzinfo is None:
                    last_entry = last_entry.replace(tzinfo=timezone.utc)
                elapsed = (now - last_entry).total_seconds()
                if elapsed < settings.discipline_entry_cooldown_seconds:
                    raise RiskBlocked("Trading-discipline entry cooldown is active")

        if settings.edge_decay_halt_enabled and not reducing:
            # Stop NEW entries when realized net returns are statistically negative (z <= -edge_decay_z_halt vs a zero-edge null).
            # Reducing/closing orders always pass. Resumes automatically once the rolling window recovers; a human decides about live.
            qout = select(StrategyOutcome.net_return_bps).order_by(StrategyOutcome.created_at.desc()).limit(int(settings.edge_decay_window_trades))
            qout = qout.where(StrategyOutcome.customer_id == customer_id) if customer_id is not None else qout.where(StrategyOutcome.customer_id.is_(None))
            realized = [float(v) / 10_000.0 for v in (await db.execute(qout)).scalars().all() if v is not None]
            decay = edge_decay_check(realized, expected_mean=0.0, min_obs=int(settings.edge_decay_min_trades), z_halt=float(settings.edge_decay_z_halt))
            if decay["status"] == "HALT_NEW_ENTRIES":
                await open_incident(key=f"EDGE_DECAY:{customer_id or 'platform'}", severity="HIGH", category="STRATEGY_EDGE",
                                    summary="Realized net returns are statistically negative; new entries halted",
                                    detail={"customer_id": customer_id, "z": decay["z"], "realized_mean": decay["realized_mean"],
                                            "observations": decay["observations"]}, customer_id=customer_id)
                raise RiskBlocked("Edge-decay halt: recent realized net returns are statistically negative")

        dd = 1 - (equity / peak) if peak else 0.0
        daily_loss = 1 - (equity / daily_start) if daily_start else 0.0
        if dd >= settings.max_drawdown or daily_loss >= settings.daily_loss_limit:
            already_halted_exit = account is not None and account.status == "HALTED" and account_reduce_only
            if account is not None:
                account.status = "HALTED"
            else:
                s.kill_switch = True
                s.live_enabled = False
                s.mode = "HALTED"
            if not already_halted_exit:
                await db.commit()
                raise RiskBlocked("Customer/account risk loss limit reached" if account is not None else "Maximum drawdown or daily loss limit reached")
            # already_halted_exit: this order only closes/reduces an existing position on an
            # account already marked HALTED by a prior breach -- let it through so the customer
            # can exit, rather than re-raising and trapping them in the position again.

        notional = price * quantity
        pilot_max_notional = None
        pilot_max_exposure = None
        pilot_max_positions = None
        pilot_max_leverage = None
        pilot_daily_loss = None
        if live and customer_id is not None:
            if str(account.pilot_status or "NONE").upper() != "APPROVED":
                raise RiskBlocked("Customer is not approved for the live pilot")
            pilot_expires_at = account.pilot_expires_at
            if pilot_expires_at is None:
                raise RiskBlocked("Customer pilot expiry is not configured")
            if pilot_expires_at.tzinfo is None:
                pilot_expires_at = pilot_expires_at.replace(tzinfo=timezone.utc)
            if pilot_expires_at <= datetime.now(timezone.utc):
                account.pilot_status = "EXPIRED"
                await db.commit()
                raise RiskBlocked("Customer live pilot has expired")
            pilot_max_notional = float(account.pilot_max_position_notional_usd or 0)
            pilot_max_exposure = float(account.pilot_max_total_exposure_usd or 0)
            pilot_max_positions = int(account.pilot_max_open_positions or 0)
            pilot_max_leverage = float(account.pilot_max_leverage or 0)
            pilot_daily_loss = float(account.pilot_daily_loss_limit or 0)
            if min(pilot_max_notional, pilot_max_exposure, pilot_max_leverage, pilot_daily_loss) <= 0 or pilot_max_positions <= 0:
                raise RiskBlocked("Customer pilot risk caps are not configured")
            if not reducing:
                pilot_daily_loss_actual = 1 - (equity / daily_start) if daily_start else 0.0
                if pilot_daily_loss_actual >= pilot_daily_loss:
                    account.status = "HALTED"
                    await db.commit()
                    raise RiskBlocked("Customer pilot daily loss limit reached")

        if settings.discipline_enabled and settings.discipline_enforce_risk_per_trade and not reducing:
            if stop_loss_price is None or stop_loss_price <= 0:
                raise RiskBlocked("Trading discipline requires a protective stop for new exposure")
            risk_cash = abs(price - float(stop_loss_price)) * quantity
            allowed_risk = equity * settings.risk_per_trade * (1.0 + settings.discipline_risk_tolerance)
            if not (risk_cash <= allowed_risk + 1e-9):
                raise RiskBlocked("Per-trade risk exceeds the configured discipline limit")
        max_leverage = min(settings.max_leverage, pilot_max_leverage) if pilot_max_leverage is not None else settings.max_leverage
        max_account_notional = equity * max_leverage
        max_notional_limit = min(settings.max_notional_usd, settings.max_position_notional_usd, max_account_notional)
        if pilot_max_notional is not None:
            max_notional_limit = min(max_notional_limit, pilot_max_notional)
        if notional > max_notional_limit:
            raise RiskBlocked("Order notional exceeds configured account/position limit")
        projected_exposure = await _projected_exposure(db, symbol, price, quantity, side, customer_id)
        max_exposure_limit = min(settings.max_total_exposure_usd, max_account_notional)
        if pilot_max_exposure is not None:
            max_exposure_limit = min(max_exposure_limit, pilot_max_exposure)
        if projected_exposure > max_exposure_limit:
            raise RiskBlocked("Total portfolio exposure limit exceeded")
        q = select(Position).where(Position.quantity != 0)
        if customer_id is not None:
            q = q.where(Position.customer_id == customer_id)
        open_positions = (await db.execute(q)).scalars().all()
        max_open_position_limit = min(settings.max_open_positions, pilot_max_positions) if pilot_max_positions is not None else settings.max_open_positions
        if symbol not in {p.symbol for p in open_positions} and len(open_positions) >= max_open_position_limit:
            raise RiskBlocked("Maximum open position count reached")
        if live and signal_timestamp:
            try:
                ts = datetime.fromisoformat(signal_timestamp.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                if age > settings.max_signal_age_seconds:
                    raise RiskBlocked("Signal is stale")
                if age < -60:
                    raise RiskBlocked("Signal timestamp is too far in the future")
            except ValueError:
                raise RiskBlocked("Invalid signal timestamp")
        await db.commit()

