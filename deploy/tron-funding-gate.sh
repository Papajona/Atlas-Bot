#!/usr/bin/env bash
# Shared deployment guard: paper mode is not a funds/custody isolation boundary.
# Sourcing this file does not change anything until atlas_configure_tron_funding is called.
atlas_configure_tron_funding() {
  USDT_TRON_ENABLED="${USDT_TRON_ENABLED:-false}"
  USDT_TRON_NETWORK="${USDT_TRON_NETWORK:-mainnet}"

  if [[ "$USDT_TRON_ENABLED" != "true" && "$USDT_TRON_ENABLED" != "false" ]]; then
    echo "ERROR: USDT_TRON_ENABLED must be exactly true or false." >&2
    return 2
  fi

  if [[ "$USDT_TRON_ENABLED" == "true" ]]; then
    if [[ "${ALLOW_TRON_FUNDING:-}" != "YES" ]]; then
      echo "ERROR: TRON funding is disabled by default; set ALLOW_TRON_FUNDING=YES only after funding-path approval." >&2
      return 2
    fi
    if [[ "$USDT_TRON_NETWORK" == "mainnet" && "${ALLOW_MAINNET_TRON_FUNDING:-}" != "YES" ]]; then
      echo "ERROR: mainnet TRON funding requires ALLOW_MAINNET_TRON_FUNDING=YES after custody/withdrawal review." >&2
      return 2
    fi
  fi

  export USDT_TRON_ENABLED USDT_TRON_NETWORK
}
