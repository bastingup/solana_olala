import json

import pytest
from solders.keypair import Keypair

from olala.security.keystore import (EncryptedKeystore, KeystoreError,
                                     KeystoreLocked)


@pytest.fixture
def keystore(tmp_path):
    ks = EncryptedKeystore(path=tmp_path / "keystore.enc")
    ks.unlock("correct horse battery staple")
    return ks


def test_locked_by_default(tmp_path):
    ks = EncryptedKeystore(path=tmp_path / "keystore.enc")
    assert ks.is_locked
    with pytest.raises(KeystoreLocked):
        ks.addresses()


def test_empty_passphrase_rejected(tmp_path):
    ks = EncryptedKeystore(path=tmp_path / "keystore.enc")
    with pytest.raises(KeystoreError):
        ks.unlock("")


def test_add_base58_key_roundtrip(keystore):
    keypair = Keypair()
    address = keystore.add_key("main", str(keypair))
    assert address == str(keypair.pubkey())
    signer = keystore.get_signer(address)
    assert str(signer.pubkey()) == address
    assert keystore.addresses() == [{"address": address, "label": "main"}]


def test_add_json_array_key(keystore):
    keypair = Keypair()
    secret = json.dumps(list(bytes(keypair)))
    address = keystore.add_key("json", secret)
    assert address == str(keypair.pubkey())


def test_invalid_secret_rejected(keystore):
    with pytest.raises(KeystoreError):
        keystore.add_key("bad", "not-a-key")
    with pytest.raises(KeystoreError):
        keystore.add_key("bad", "[1, 2, 3]")


def test_wrong_passphrase_rejected(tmp_path):
    path = tmp_path / "keystore.enc"
    ks = EncryptedKeystore(path=path)
    ks.unlock("first-passphrase")
    ks.add_key("w", str(Keypair()))

    reopened = EncryptedKeystore(path=path)
    with pytest.raises(KeystoreError):
        reopened.unlock("wrong-passphrase")
    assert reopened.is_locked


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "keystore.enc"
    keypair = Keypair()
    ks = EncryptedKeystore(path=path)
    ks.unlock("pass")
    address = ks.add_key("w", str(keypair))

    reopened = EncryptedKeystore(path=path)
    reopened.unlock("pass")
    assert str(reopened.get_signer(address).pubkey()) == address


def test_key_material_not_plaintext_on_disk(tmp_path):
    path = tmp_path / "keystore.enc"
    keypair = Keypair()
    ks = EncryptedKeystore(path=path)
    ks.unlock("pass")
    ks.add_key("w", str(keypair))
    raw = path.read_bytes()
    assert str(keypair).encode() not in raw
    assert bytes(keypair) not in raw
