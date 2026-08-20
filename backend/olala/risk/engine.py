"""Risk engine: every entry and re-size passes through here.

Hard rules, in evaluation order:
1. Token must pass the safety screen (contract + market shape).
2. Capital added never exceeds ``max_liquidity_fraction`` (1%) of the
   token's existing pool liquidity.
3. New entries never touch the SOL reserve; only re-sizes of an existing
   position (a followed trader buying a dip) may draw on it.
4. Per-wallet position-count and per-position exposure ceilings.

The engine sizes trades; it never originates them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import AppConfig, RiskConfig
from ..domain.models import RiskVerdict, TokenInfo



def target_size_sol(risk: RiskConfig, market_cap_usd: float) -> float:
    """What a normal entry in a token of this size is worth, in SOL.

    A flat share of equity cannot serve the whole market. At 1% of a 10
    SOL wallet, 0.1 SOL is a rounding error in a $1B token and several
    percent of a $2k pump.fun pool — so one setting either refuses to
    trade small tokens or bulldozes them.

    Market cap spans six orders of magnitude, so the ladder is
    LOGARITHMIC. Linear interpolation would leave everything below
    $100M sitting on the floor, which is most of what these traders
    actually trade.

    This is only the TARGET. The liquidity ceiling, the cash reserve and
    the per-position cap all still apply on top, and any of them may cut
    it down.
    """
    floor, ceiling = risk.min_trade_sol, risk.max_trade_sol
    if ceiling <= floor:
        return max(floor, 0.0)
    low = max(risk.size_mcap_floor_usd, 1.0)
    high = max(risk.size_mcap_ceiling_usd, low * 10.0)
    if market_cap_usd <= low:
        # Unknown or tiny: the smallest order we are willing to place.
        return floor
    if market_cap_usd >= high:
        return ceiling
    span = math.log10(high) - math.log10(low)
    position = (math.log10(market_cap_usd) - math.log10(low)) / span
    return floor + position * (ceiling - floor)


@dataclass
class WalletExposure:
    """Snapshot of one wallet handed to the engine by the portfolio.

    ``invested_in_mint_sol`` is this wallet's stake in the token (bounds
    the per-position ceiling); ``fleet_invested_in_mint_sol`` is the stake
    across ALL wallets (bounds the 1%-of-liquidity rule, which is a
    property of the pool, not of a wallet).
    """

    wallet_id: str
    cash_sol: float
    equity_sol: float
    open_positions: int
    invested_in_mint_sol: float
    fleet_invested_in_mint_sol: float = 0.0
    wallet_is_paper: bool = True
    live_wallets_holding_mint: int = 0


class RiskEngine:
    def evaluate_entry(self, config: AppConfig, token: TokenInfo,
                       exposure: WalletExposure,
                       is_resize: bool) -> RiskVerdict:
        risk = config.risk
        if token.price_sol <= 0 or token.price_usd <= 0:
            return RiskVerdict(False, "no usable price for token")
        if not is_resize and exposure.open_positions >= risk.max_positions_per_wallet:
            return RiskVerdict(
                False, f"wallet at max positions ({risk.max_positions_per_wallet})")

        # Correlation gate: many followed traders piling into one token
        # must not put every live wallet into the same position. A resize
        # is exempt (this wallet already holds); paper wallets are exempt.
        if (not exposure.wallet_is_paper and not is_resize
                and exposure.live_wallets_holding_mint
                >= risk.max_live_wallets_per_token):
            return RiskVerdict(
                False, f"token already held by "
                       f"{exposure.live_wallets_holding_mint} live wallets "
                       f"(max {risk.max_live_wallets_per_token})")

        sol_usd = token.price_usd / token.price_sol

        # Rule: our capital must never exceed 1% of existing liquidity —
        # counting what EVERY wallet has already added to the same pool.
        fleet_invested = max(exposure.fleet_invested_in_mint_sol,
                             exposure.invested_in_mint_sol)
        liquidity_cap_sol = (token.liquidity_usd * risk.max_liquidity_fraction
                             ) / sol_usd - fleet_invested

        # Reserve: a fraction of equity is held back so re-sizes can follow
        # a trader into dips. New entries only spend above the reserve.
        reserve_sol = exposure.equity_sol * risk.reserve_fraction
        if is_resize:
            available_sol = exposure.cash_sol
        else:
            available_sol = exposure.cash_sol - reserve_sol

        # Size follows the TOKEN, not the wallet: see `target_size_sol`.
        target_sol = target_size_sol(risk, token.market_cap_usd)

        # Per-position ceiling keeps one trade from dominating the
        # wallet, measured against what a normal entry in THIS token
        # would be.
        position_cap_sol = (target_sol * risk.max_position_equity_multiple
                            ) - exposure.invested_in_mint_sol

        size = min(target_sol, liquidity_cap_sol, available_sol,
                   position_cap_sol)

        if size < risk.min_order_sol:
            constraint = min(
                (liquidity_cap_sol, "liquidity ceiling (1% of pool)"),
                (available_sol, "reserve/cash constraint"),
                (position_cap_sol, "per-position exposure ceiling"),
                key=lambda pair: pair[0])
            return RiskVerdict(
                False, f"size {max(size, 0):.4f} SOL below minimum "
                       f"— binding constraint: {constraint[1]}")
        return RiskVerdict(True, "approved", size_sol=round(size, 6))
