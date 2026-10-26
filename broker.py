"""Kotak Neo broker adapter for the local option-chain and order terminal."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from getpass import getpass
from typing import Any

import pyotp
from neo_api_client import NeoAPI

try:
    import credentials as cx
except ModuleNotFoundError as exc:
    if exc.name != "credentials":
        raise
    cx = None


INSTRUMENTS: dict[str, dict[str, str]] = {
    "NIFTY": {
        "exchange": "nse_fo",
        "underlying": "NIFTY",
        "spot_segment": "nse_cm",
        "spot_token": "Nifty 50",
    },
    "SENSEX": {
        "exchange": "bse_fo",
        "underlying": "SENSEX",
        "spot_segment": "bse_cm",
        "spot_token": "SENSEX",
    },
}
STRIKES_PER_SIDE_OPTIONS = (10, 20, 30, 40)


class BrokerResponseError(RuntimeError):
    """Raised when Kotak returns a response the adapter cannot use."""


def _successful(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    data = response.get("data", {})
    return isinstance(data, dict) and data.get("status") == "success"


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _contract(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize the SDK's observed response and the alternate documented shape."""
    instrument = row.get("inst") or row.get("instrument") or {}
    quote = row.get("quote") or {}
    oi = row.get("oi") or row.get("openInterest") or {}

    return {
        "symbol": _first(instrument, "symbol"),
        "neo_symbol": _first(instrument, "neoSymbol"),
        "option_type": _first(instrument, "optType", "optionType"),
        "strike": _first(instrument, "strkPrc", "strikePrice"),
        "expiry": _first(instrument, "exp", "expiryDt"),
        "moneyness": _first(instrument, "moneyness"),
        "ltp": _first(quote, "ltp"),
        "open": _first(quote, "o", "open"),
        "high": _first(quote, "h", "high"),
        "low": _first(quote, "l", "low"),
        "previous_close": _first(quote, "pc", "prevClose"),
        "volume": _first(quote, "vol", "volume"),
        "oi": _first(oi, "cur", "current"),
        "oi_previous": _first(oi, "prev", "previous"),
        "oi_change": _first(oi, "chg", "change"),
        "oi_change_pct": _first(oi, "chgPct", "changePct"),
    }


def _market_analysis(contract: dict[str, Any]) -> str:
    """Simple price/OI quadrant label; informational, not a trading signal."""
    try:
        price = Decimal(str(contract.get("ltp")))
        previous_close = Decimal(str(contract.get("previous_close")))
        oi = Decimal(str(contract.get("oi")))
        oi_previous = Decimal(str(contract.get("oi_previous")))
        if previous_close <= 0:
            return "—"
        price_delta = price - previous_close
        oi_delta = oi - oi_previous
    except (InvalidOperation, TypeError, ValueError):
        return "—"

    if price_delta > 0 and oi_delta > 0:
        return "Long Build-up"
    if price_delta < 0 and oi_delta > 0:
        return "Short Build-up"
    if price_delta > 0 and oi_delta < 0:
        return "Short Cover"
    if price_delta < 0 and oi_delta < 0:
        return "Long Unwinding"
    return "—"


def _strike_sort_key(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("Infinity")


class KotakBroker:
    def __init__(self, client: NeoAPI):
        self.client = client

    @classmethod
    def login_interactive(cls) -> "KotakBroker":
        """Log in at startup using local credentials.py or secure terminal prompts."""
        if cx is not None:
            try:
                consumer_key = str(cx.CONSUMER_KEY).strip()
                mobile_number = str(cx.MOBILE_NUMBER).strip()
                ucc = str(cx.UCC).strip()
                totp_secret = str(cx.TOTP).replace(" ", "").strip()
                mpin = str(cx.MPIN).strip()
                if any(not value or value.lower() == "none" for value in (
                    consumer_key, mobile_number, ucc, totp_secret, mpin
                )):
                    raise RuntimeError("Required credentials.py fields must be non-empty.")
            except AttributeError as exc:
                missing = getattr(exc, "name", None) or "a required field"
                raise RuntimeError(
                    f"credentials.py is missing {missing}; required fields are CONSUMER_KEY, MOBILE_NUMBER, UCC, TOTP, and MPIN."
                ) from None
            # PASSWORD is not part of this SDK's TOTP login/MPIN validation flow.
        else:
            consumer_key = getpass("Kotak consumer key: ").strip()
            mobile_number = input("Registered mobile (+91...): ").strip()
            ucc = getpass("UCC/client code: ").strip()
            totp_secret = getpass("TOTP secret: ").replace(" ", "").strip()
            mpin = getpass("MPIN: ").strip()

        client = NeoAPI(
            consumer_key=consumer_key,
            environment="prod",
            timeout=15,
        )

        login_response = client.totp_login(
            mobile_number=mobile_number,
            ucc=ucc,
            totp=pyotp.TOTP(totp_secret).now(),
        )
        if not _successful(login_response):
            raise BrokerResponseError("Kotak TOTP login did not return success.")

        validate_response = client.totp_validate(mpin=mpin)
        if not _successful(validate_response):
            raise BrokerResponseError("Kotak MPIN validation did not return success.")

        # Do not retain the plain MPIN or TOTP secret on this object.
        del mpin, totp_secret, consumer_key, ucc, mobile_number
        return cls(client)

    def get_expiries(self, instrument_name: str) -> list[str]:
        product = INSTRUMENTS.get(instrument_name)
        if product is None:
            raise ValueError("Unsupported underlying.")

        response = self.client.expiries(
            exchange=product["exchange"],
            underlying=product["underlying"],
        )
        expiries = response.get("expiries", []) if isinstance(response, dict) else []
        if not isinstance(expiries, list):
            raise BrokerResponseError("Unexpected expiry response shape.")
        return [str(expiry) for expiry in expiries]

    def get_option_chain(
        self,
        instrument_name: str,
        expiry: str,
        strikes_per_side: int = 10,
    ) -> dict[str, Any]:
        product = INSTRUMENTS.get(instrument_name)
        if product is None:
            raise ValueError("Unsupported underlying.")
        if strikes_per_side not in STRIKES_PER_SIDE_OPTIONS:
            raise ValueError("Choose 10, 20, 30, or 40 strikes per side.")

        # Kotak's count is the total number of strikes (calls and puts are
        # returned for each strike). Double the UI's per-side choice to request
        # a similarly sized chain around ATM; supported counts are multiples of 10.
        api_strike_count = strikes_per_side * 2
        response = self.client.option_chain(
            exchange=product["exchange"],
            underlying=product["underlying"],
            expiry=expiry,
            instrument_type="option",
            count=api_strike_count,
        )
        if not isinstance(response, dict):
            raise BrokerResponseError("Option-chain response was not an object.")

        # SDK 3.0.7 returns the chain fields at top level. Accommodate the
        # nested {"data": ...} shape shown in some API documentation too.
        payload = response.get("data")
        if not isinstance(payload, dict):
            payload = response

        common = payload.get("common_data") or {}
        calls = payload.get("call") or []
        puts = payload.get("put") or []
        if not isinstance(common, dict) or not isinstance(calls, list) or not isinstance(puts, list):
            raise BrokerResponseError("Option-chain response has unexpected fields.")
        if not common and not calls and not puts:
            raise BrokerResponseError("Kotak returned an empty option chain.")

        by_strike: dict[str, dict[str, Any]] = {}
        for side, contracts in (("call", calls), ("put", puts)):
            for raw_contract in contracts:
                if not isinstance(raw_contract, dict):
                    continue
                contract = _contract(raw_contract)
                contract["analysis"] = _market_analysis(contract)
                strike = contract.get("strike")
                if strike is None:
                    continue
                strike_key = str(strike)
                by_strike.setdefault(strike_key, {"strike": strike_key})[side] = contract

        spot = payload.get("spot") or {}
        spot_price = None
        try:
            if spot.get("ltp") is not None:
                spot_price = Decimal(str(spot["ltp"]))
        except (InvalidOperation, TypeError, ValueError):
            spot_price = None

        if spot_price is not None:
            # Preserve normal low-to-high strike order; a symmetric chain naturally
            # places its nearest-to-spot (ATM) strike around the vertical center.
            rows = sorted(by_strike.values(), key=lambda item: _strike_sort_key(item["strike"]))
            atm_strike = min(
                (_strike_sort_key(row["strike"]) for row in rows),
                key=lambda strike: abs(strike - spot_price),
                default=None,
            )
            for row in rows:
                strike_value = _strike_sort_key(row["strike"])
                for side in ("call", "put"):
                    contract = row.get(side)
                    if not contract:
                        continue
                    if strike_value == atm_strike:
                        zone = "ATM"
                    elif side == "call":
                        zone = "ITM" if strike_value < spot_price else "OTM"
                    else:
                        zone = "ITM" if strike_value > spot_price else "OTM"
                    contract["moneyness_group"] = zone
        else:
            rows = sorted(by_strike.values(), key=lambda item: _strike_sort_key(item["strike"]))
            for row in rows:
                for side in ("call", "put"):
                    contract = row.get(side)
                    if contract:
                        contract["moneyness_group"] = str(contract.get("moneyness") or "OTM").upper()

        return {
            "instrument": instrument_name,
            "exchange": product["exchange"],
            "expiry": expiry,
            "market_lot": common.get("mktLot"),
            "strikes_per_side": strikes_per_side,
            "api_strike_count": api_strike_count,
            "spot": spot,
            "spot_neo_symbol": f"{product['spot_segment']}|{product['spot_token']}",
            "future": payload.get("future") or {},
            "rows": rows,
            "snapshot_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def place_live_order(self, broker_fields: dict[str, Any], tag: str) -> Any:
        """Send exactly one broker order request; callers must handle ambiguity."""
        return self.client.place_order(
            exchange_segment=str(broker_fields["es"]),
            product=str(broker_fields["pc"]),
            price=str(broker_fields["pr"]),
            order_type=str(broker_fields["pt"]),
            quantity=str(broker_fields["qt"]),
            validity=str(broker_fields["rt"]),
            trading_symbol=str(broker_fields["ts"]),
            transaction_type=str(broker_fields["tt"]),
            amo="NO",
            disclosed_quantity="0",
            trigger_price=str(broker_fields.get("tp", "0")),
            tag=tag,
        )

    def modify_live_order(self, order_id: str, fields: dict[str, Any]) -> Any:
        """Send one validated modify request; never retry an ambiguous result."""
        return self.client.modify_order(
            order_id=str(order_id),
            price=str(fields["price"]),
            order_type=str(fields["order_type"]),
            quantity=str(fields["quantity"]),
            validity=str(fields["validity"]),
            trigger_price=str(fields.get("trigger_price", "0")),
            disclosed_quantity="0",
            amo="NO",
        )

    def cancel_live_order(self, order_id: str) -> Any:
        """Send one cancel request for an order ID."""
        return self.client.cancel_order(order_id=str(order_id))

    def get_order_report(self) -> Any:
        return self.client.order_report()

    def get_positions(self) -> Any:
        return self.client.positions()
