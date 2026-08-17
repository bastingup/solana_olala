"""Live execution path: sign → send → confirm on chain → reconstruct the
ACTUAL fill from the landed transaction → record a receipt. Nothing is
ever booked from the quote alone."""

import base64

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from olala.domain.models import Receipt, ReceiptStatus, TradeSide
from olala.domain.wallet import LiveSolanaWallet
from olala.trading.executor import ExecutionError, LiveJupiterExecutor

from conftest import make_token
from fakes import FakeProvider, make_swap_tx

MINT = "MintX111"


def unsigned_swap_tx(payer: Keypair) -> str:
    """A structurally real, unsigned VersionedTransaction, as Jupiter's
    swap builder would return it (base64)."""
    instruction = transfer(TransferParams(
        from_pubkey=payer.pubkey(), to_pubkey=Keypair().pubkey(),
        lamports=1))
    message = MessageV0.try_compile(payer.pubkey(), [instruction], [],
                                    Hash.default())
    unsigned = VersionedTransaction.populate(message, [Signature.default()])
    return base64.b64encode(bytes(unsigned)).decode()


class FakeJupiterSwap:
    def __init__(self, tx_b64: str, out_amount: int) -> None:
        self.tx_b64 = tx_b64
        self.out_amount = out_amount

    def get_quote(self, input_mint, output_mint, amount):
        return {"outAmount": str(self.out_amount), "inAmount": str(amount)}

    def build_swap_transaction(self, quote, address):
        return self.tx_b64


class FakeKeystore:
    def __init__(self, keypair: Keypair) -> None:
        self._keypair = keypair

    def get_signer(self, address):
        return self._keypair


def live_world(confirm=True, err=None, landed_tx="swap", timeout_sec=5.0):
    payer = Keypair()
    provider = FakeProvider()
    wallet = LiveSolanaWallet("lw1", "Vault", str(payer.pubkey()), provider,
                              armed=True)
    signature = "fake-sig-1"
    if confirm:
        provider.signature_status[signature] = {
            "confirmationStatus": "confirmed", "err": err, "slot": 1234}
    if landed_tx == "swap":
        # 1.0 SOL traded (fee excluded) for 100 tokens, per the chain.
        provider.transactions[signature] = make_swap_tx(
            str(payer.pubkey()), -1_000_005_000, MINT, 100.0)
    receipts: list[Receipt] = []
    executor = LiveJupiterExecutor(
        FakeJupiterSwap(unsigned_swap_tx(payer), out_amount=95_000_000),
        provider, FakeKeystore(payer), on_receipt=receipts.append,
        confirm_timeout_sec=timeout_sec, confirm_poll_sec=0.01)
    return executor, wallet, provider, receipts


def test_confirmed_buy_books_chain_actuals_not_quote():
    executor, wallet, provider, receipts = live_world()
    token = make_token(mint=MINT)

    fill = executor.buy(wallet, token, 1.0)

    # The quote promised 95 tokens; the chain delivered 100 — the fill
    # and the receipt must both speak chain truth.
    assert fill.quantity == pytest.approx(100.0)
    assert fill.sol_amount == pytest.approx(1.0)
    assert fill.signature == "fake-sig-1"
    assert fill.fee_sol == pytest.approx(5000 / 1e9)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.status is ReceiptStatus.CONFIRMED
    assert receipt.quoted_tokens == pytest.approx(95.0)
    assert receipt.actual_tokens == pytest.approx(100.0)
    assert receipt.actual_sol == pytest.approx(1.0)
    assert receipt.slot == 1234
    assert receipt.wallet_id == "lw1"


def test_signing_uses_the_wallet_keypair():
    executor, wallet, provider, _ = live_world()
    executor.buy(wallet, make_token(mint=MINT), 1.0)
    # The transaction that went out is a genuinely signed one.
    sent = VersionedTransaction.from_bytes(
        base64.b64decode(provider.sent_transactions[0]))
    assert sent.signatures[0] != Signature.default()


def test_onchain_failure_raises_and_records_failed_receipt():
    executor, wallet, provider, receipts = live_world(
        err={"InstructionError": [2, "Custom"]}, landed_tx=None)

    with pytest.raises(ExecutionError, match="failed on chain"):
        executor.buy(wallet, make_token(mint=MINT), 1.0)

    assert len(receipts) == 1
    assert receipts[0].status is ReceiptStatus.FAILED
    assert "failed on chain" in receipts[0].detail


def test_unconfirmed_order_times_out_and_records_receipt():
    executor, wallet, provider, receipts = live_world(
        confirm=False, landed_tx=None, timeout_sec=0.05)

    with pytest.raises(ExecutionError, match="not confirmed"):
        executor.buy(wallet, make_token(mint=MINT), 1.0)

    assert len(receipts) == 1
    assert receipts[0].status is ReceiptStatus.TIMEOUT
    assert "blockhash expired" in receipts[0].detail


def test_unreconstructable_tx_falls_back_to_quote_and_flags_it():
    executor, wallet, provider, receipts = live_world(landed_tx=None)
    provider.transactions["fake-sig-1"] = {"meta": {"fee": 5000},
                                           "blockTime": 1_755_000_000}

    fill = executor.buy(wallet, make_token(mint=MINT), 1.0)

    assert fill.quantity == pytest.approx(95.0)  # quote fallback
    assert receipts[0].status is ReceiptStatus.CONFIRMED
    assert "quote" in receipts[0].detail


def test_confirmed_sell_books_chain_actuals():
    executor, wallet, provider, receipts = live_world(landed_tx=None)
    # Trader receives 2.0 SOL (+fee already inside the delta) for 100 tokens.
    provider.transactions["fake-sig-1"] = make_swap_tx(
        wallet.address, +1_999_995_000, MINT, -100.0)

    fill = executor.sell(wallet, make_token(mint=MINT), 100.0)

    assert fill.side is TradeSide.SELL
    assert fill.sol_amount == pytest.approx(2.0)
    assert fill.quantity == pytest.approx(100.0)
    assert receipts[0].status is ReceiptStatus.CONFIRMED
    assert receipts[0].actual_sol == pytest.approx(2.0)


# -- persistence and API surface ------------------------------------------

def sample_receipt():
    return Receipt(signature="SigAAA", order_id="o1", wallet_id="lw1",
                   side=TradeSide.BUY, mint=MINT,
                   status=ReceiptStatus.CONFIRMED, quoted_sol=1.0,
                   quoted_tokens=95.0, actual_sol=1.0, actual_tokens=100.0,
                   fee_sol=0.000005, slot=1234, block_time=1_755_000_000.0)


def test_receipt_roundtrips_through_database(db):
    db.save_receipt(sample_receipt())
    rows = db.load_receipts()
    assert len(rows) == 1
    row = rows[0]
    assert row["signature"] == "SigAAA"
    assert row["status"] == "confirmed"
    assert row["actual_tokens"] == pytest.approx(100.0)


def test_receipts_reach_rest_and_snapshot(tmp_path, config_store):
    from olala.api.server import AppContext, build_app
    from olala.persistence.database import Database
    from olala.security.keystore import EncryptedKeystore
    from fakes import FakeMarketData

    ctx = AppContext(
        config_store=config_store,
        database=Database(path=tmp_path / "r.db"),
        keystore=EncryptedKeystore(path=tmp_path / "r.enc"),
        provider=FakeProvider(), market_data=FakeMarketData())
    ctx.record_receipt(sample_receipt())

    app = build_app(ctx)
    app.testing = True
    client = app.test_client()
    listed = client.get("/api/receipts").get_json()
    assert listed and listed[0]["signature"] == "SigAAA"
    snapshot = client.get("/api/state").get_json()
    assert snapshot["receipts"][0]["signature"] == "SigAAA"
