"""Order-ticket validation and Kotak order-field mapping."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


class OrderPreviewError(ValueError):
    """Raised when the order ticket is invalid."""


def _find_contract(chain: dict[str, Any], neo_symbol: str) -> dict[str, Any] | None:
    for row in chain.get("rows", []):
        for side in ("call", "put"):
            contract = row.get(side) or {}
            if contract.get("neo_symbol") == neo_symbol:
                return contract
    return None


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise OrderPreviewError(f"Enter a valid {field_name}.") from None
    if not number.is_finite() or number <= 0:
        raise OrderPreviewError(f"{field_name.capitalize()} must be greater than zero.")
    return number


def build_order_preview(
    chain: dict[str, Any] | None,
    request_data: dict[str, Any],
    live_ltp: Any | None = None,
    snapshot_source: str = "option-chain snapshot LTP",
) -> dict[str, Any]:
    """Validate the selected chain contract and map user intent to broker fields.

    Kotak's direct market order type is intentionally not used here. A UI
    MARKET choice is translated into a LIMIT order priced at the freshest
    current SFeed LTP, falling back to the REST chain snapshot if no fresh tick
    is available.
    """
    if not chain:
        raise OrderPreviewError("Load an option chain before creating an order.")

    neo_symbol = str(request_data.get("neo_symbol", "")).strip()
    contract = _find_contract(chain, neo_symbol)
    if not contract:
        raise OrderPreviewError("Select a contract from the currently loaded chain.")

    side = str(request_data.get("side", "")).upper()
    if side not in {"BUY", "SELL"}:
        raise OrderPreviewError("Side must be BUY or SELL.")

    user_order_type = str(request_data.get("order_type", "")).upper()
    if user_order_type not in {"L", "MKT", "SL", "SL-M"}:
        raise OrderPreviewError("Choose Limit, Market, Stop-loss Limit, or Stop-loss Market.")

    product = str(request_data.get("product", "")).upper()
    if product not in {"NRML", "MIS"}:
        raise OrderPreviewError("Choose NRML (overnight) or MIS (intraday).")

    validity = str(request_data.get("validity", "DAY")).upper()
    if validity not in {"DAY", "IOC"}:
        raise OrderPreviewError("Choose DAY or IOC validity.")

    quantity_text = str(request_data.get("quantity", "")).strip()
    if not quantity_text.isdigit() or int(quantity_text) < 1:
        raise OrderPreviewError("Quantity must be a positive whole number of units.")
    quantity = int(quantity_text)

    try:
        market_lot = int(str(chain.get("market_lot", "")))
    except (TypeError, ValueError):
        raise OrderPreviewError("Kotak did not return a valid market lot.") from None
    if market_lot <= 0:
        raise OrderPreviewError("Kotak did not return a valid market lot.")
    if quantity % market_lot != 0:
        raise OrderPreviewError(f"Quantity must be a multiple of the lot size ({market_lot}).")
    lots = quantity // market_lot

    price_source = None
    if user_order_type in {"L", "SL"}:
        price = format(_positive_decimal(request_data.get("price"), "price"), "f")
        broker_order_type = user_order_type
    elif user_order_type == "MKT":
        quote_source = live_ltp if live_ltp is not None else contract.get("ltp")
        price = format(_positive_decimal(quote_source, "current LTP"), "f")
        price_source = "current SFeed LTP" if live_ltp is not None else snapshot_source
        broker_order_type = "L"
    else:
        price = "0"
        broker_order_type = "SL-M"

    if user_order_type in {"SL", "SL-M"}:
        trigger_price = format(
            _positive_decimal(request_data.get("trigger_price"), "trigger price"),
            "f",
        )
    else:
        trigger_price = "0"

    amo = "NO"
    broker_fields = {
        "es": chain.get("exchange"),
        "pc": product,
        "pr": price,
        "pt": broker_order_type,
        "qt": str(quantity),
        "rt": validity,
        "ts": contract.get("symbol"),
        "tt": "B" if side == "BUY" else "S",
        "tp": trigger_price,
        "am": amo,
    }

    return {
        "mode": "LIVE ORDER",
        "sent_to_broker": False,
        "instrument": chain.get("instrument"),
        "exchange": chain.get("exchange"),
        "expiry": chain.get("expiry"),
        "symbol": contract.get("symbol"),
        "neo_symbol": contract.get("neo_symbol"),
        "option_type": contract.get("option_type"),
        "strike": contract.get("strike"),
        "side": side,
        "order_type": user_order_type,
        "broker_order_type": broker_order_type,
        "product": product,
        "validity": validity,
        "quantity": quantity,
        "lots": lots,
        "market_lot": market_lot,
        "price": None if price == "0" else price,
        "price_source": price_source,
        "trigger_price": None if trigger_price == "0" else trigger_price,
        "broker_fields_preview": broker_fields,
        "note": f"A MARKET selection is sent as LIMIT at a fresh SFeed LTP or {snapshot_source}; AMO is always NO.",
    }
