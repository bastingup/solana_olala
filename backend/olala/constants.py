"""Constants shared across the whole system.

These were duplicated across three, two and six modules respectively.
``SOL_MINT`` in particular carried two names (``SOL_MINT`` and
``WSOL_MINT``) for the same 44-character literal, which is exactly the
kind of thing that goes wrong silently when one copy is edited.
"""

from __future__ import annotations

# Wrapped SOL. Solana has no native-mint address, so every DEX quotes
# against this; "SOL" and "WSOL" are the same mint at the protocol level.
SOL_MINT = "So11111111111111111111111111111111111111112"

LAMPORTS_PER_SOL = 1_000_000_000

SECONDS_PER_DAY = 86_400.0

# Assets a swap is commonly denominated in besides SOL. A trader who
# exits into one of these has still exited — recognising that is what
# stops us holding a bag they already sold.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

#: Mints treated as the "money" side of a swap, mapped to whether they
#: are US-dollar pegged.
QUOTE_MINTS: dict[str, bool] = {
    SOL_MINT: False,
    USDC_MINT: True,
    USDT_MINT: True,
}
