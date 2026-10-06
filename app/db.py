        UniqueConstraint("customer_id", name="uq_trading_account_customer"),
        UniqueConstraint("id", "customer_id", name="uq_trading_account_id_customer"),
        Index("ix_trading_account_status", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(20), default="USDT")
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")
    cash_equity: Mapped[float] = mapped_column(FinancialNumeric, default=0.0)
    equity: Mapped[float] = mapped_column(FinancialNumeric, default=0.0)
    peak_equity: Mapped[float] = mapped_column(FinancialNumeric, default=0.0)
    daily_start_equity: Mapped[float] = mapped_column(FinancialNumeric, default=0.0)
    daily_start_date: Mapped[date] = mapped_column(default=_utc_date)
    realized_pnl: Mapped[float] = mapped_column(FinancialNumeric, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(FinancialNumeric, default=0.0)
    reserved_margin: Mapped[float] = mapped_column(FinancialNumeric, default=0.0)
    pilot_status: Mapped[str] = mapped_column(String(20), default="NONE")
    pilot_stage: Mapped[int] = mapped_column(Integer, default=0)
    pilot_requested_by: Mapped[str] = mapped_column(String(160), default="")
    pilot_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pilot_approved_by: Mapped[str] = mapped_column(String(160), default="")
    pilot_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pilot_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pilot_max_position_notional_usd: Mapped[float] = mapped_column(FinancialNumeric, default=0.0)
    pilot_max_total_exposure_usd: Mapped[float] = mapped_column(FinancialNumeric, default=0.0)
    pilot_max_open_positions: Mapped[int] = mapped_column(Integer, default=0)
    pilot_max_leverage: Mapped[float] = mapped_column(Float, default=0.0)
    pilot_daily_loss_limit: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)



class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_trade_client_order_id"),
        UniqueConstraint("signal_id", name="uq_trade_signal_id"),
        UniqueConstraint("id", "customer_id", name="uq_trade_id_customer"),
        ForeignKeyConstraint(["trading_account_id", "customer_id"], ["trading_accounts.id", "trading_accounts.customer_id"], name="fk_trade_account_customer"),
        Index("ix_trade_status_symbol", "status", "symbol"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer_profiles.id"), nullable=True, index=True)
    trading_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    signal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(120), nullable=False)
    broker_order_id: Mapped[str] = mapped_column(String(120), default="")
    exchange: Mapped[str] = mapped_column(String(50), default="")