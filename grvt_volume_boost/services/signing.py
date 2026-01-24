from __future__ import annotations

from eth_account import Account as EthAccount
from eth_account.messages import encode_typed_data

from grvt_volume_boost.config import AccountConfig
from grvt_volume_boost.settings import CHAIN_ID

EIP712_ORDER_TYPE = {
    "Order": [
        {"name": "subAccountID", "type": "uint64"},
        {"name": "isMarket", "type": "bool"},
        {"name": "timeInForce", "type": "uint8"},
        {"name": "postOnly", "type": "bool"},
        {"name": "reduceOnly", "type": "bool"},
        {"name": "legs", "type": "OrderLeg[]"},
        {"name": "nonce", "type": "uint32"},
        {"name": "expiration", "type": "int64"},
    ],
    "OrderLeg": [
        {"name": "assetID", "type": "uint256"},
        {"name": "contractSize", "type": "uint64"},
        {"name": "limitPrice", "type": "uint64"},
        {"name": "isBuyingContract", "type": "bool"},
    ],
}


def sign_order(acc: AccountConfig, message_data: dict) -> tuple[str, dict]:
    """Return (signer_address, signature_fields_dict) for message_data."""
    account = EthAccount.from_key(acc.session_private_key)
    signed = account.sign_message(
        encode_typed_data(
            {"name": "GRVT Exchange", "version": "0", "chainId": CHAIN_ID},
            EIP712_ORDER_TYPE,
            message_data,
        )
    )
    sig = {
        "s": account.address,
        "r": "0x" + hex(signed.r)[2:].zfill(64),
        "s1": "0x" + hex(signed.s)[2:].zfill(64),
        "v": int(signed.v),
        "e": str(message_data["expiration"]),
        "n": message_data["nonce"],
    }
    return account.address, sig

