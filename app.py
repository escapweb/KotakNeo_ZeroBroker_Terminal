"""Local Flask option-chain terminal with one-click order placement."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from flask import Flask, current_app, jsonify, render_template, request

from broker import INSTRUMENTS, STRIKES_PER_SIDE_OPTIONS, KotakBroker
from market_feed import MarketFeed
from order_service import OrderPreviewError, build_order_preview


ACTIVE_ORDER_STATUSES = {
    "open",
    "pending",
    "open pending",
    "trigger pending",
    "partially filled",
    "partial filled",
    "part filled",
    "partially executed",
    "partial executed",
    "part executed",
    "validation pending",
}


def _order_status(order: dict) -> str:
    return str(order.get("ordSt") or order.get("stat") or "").strip()


def _is_active_order(order: dict) -> bool:
    status = " ".join(_order_status(order).lower().replace("_", " ").replace("-", " ").split())
    return status in ACTIVE_ORDER_STATUSES or (
        status.startswith("partially ") and ("fill" in status or "execut" in status)
    )


def _integer_value(value, default: int) -> int:
    try:
        number = Decimal(str(value))
        if number.is_finite() and number == number.to_integral_value():
            return int(number)
    except (InvalidOperation, TypeError, ValueError):
        pass
    return default


def _classify_broker_action(response) -> str:
    if not isinstance(response, dict):
        return "UNKNOWN"
    stat = str(response.get("stat", "")).strip().lower().replace("_", " ")
    try:
        status_code = int(response.get("status_code", 0) or 0)
        st_code = int(response.get("stCode", 0) or 0)
    except (TypeError, ValueError):
        status_code = st_code = 0
    if stat in {"ok", "success"} or st_code == 200 or response.get("nOrdNo"):
        return "ACKNOWLEDGED"
    if 400 <= status_code < 500 or st_code == 1021 or stat in {"not ok", "error", "rejected"}:
        return "REJECTED"
    return "UNKNOWN"


def _normalize_option_positions(response) -> list[dict]:
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise RuntimeError("Kotak returned an unexpected positions response.")

    positions = []
    for raw in response["data"]:
        if not isinstance(raw, dict):
            continue
        exchange = str(raw.get("exSeg") or "").strip().lower()
        if exchange not in {"nse_fo", "bse_fo"}:
            continue

        symbol = str(raw.get("sym") or "").strip().upper()
        trading_symbol = str(raw.get("trdSym") or "").strip()
        trading_upper = trading_symbol.upper()
        underlying = symbol if symbol in {"NIFTY", "SENSEX"} else ""
        if not underlying:
            underlying = next((name for name in ("NIFTY", "SENSEX") if trading_upper.startswith(name)), "")
        if not underlying:
            continue

        option_type = str(raw.get("optTp") or "").strip().upper()
        if option_type not in {"CE", "PE"}:
            option_type = next((suffix for suffix in ("CE", "PE") if trading_upper.endswith(suffix)), "")
        if option_type not in {"CE", "PE"}:
            continue

        product = str(raw.get("prod") or "NRML").strip().upper()
        if product not in {"NRML", "MIS"}:
            continue
        token = str(raw.get("tok") or "").strip()
        if not token or not trading_symbol:
            continue

        bought = _integer_value(raw.get("cfBuyQty"), 0) + _integer_value(raw.get("flBuyQty"), 0)
        sold = _integer_value(raw.get("cfSellQty"), 0) + _integer_value(raw.get("flSellQty"), 0)
        net_quantity = bought - sold
        if net_quantity == 0:
            continue

        lot_size = _integer_value(raw.get("lotSz"), _integer_value(raw.get("brdLtQty"), 1))
        if lot_size <= 0:
            lot_size = 1
        expiry = str(raw.get("expDt") or "").strip()
        raw_expiry = raw.get("exp")
        try:
            expiry_number = float(raw_expiry)
            if expiry_number > 0:
                if expiry_number > 1_000_000_000_000:
                    expiry_number /= 1000
                expiry = datetime.fromtimestamp(expiry_number, tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            pass

        ltp = raw.get("ltp")
        positions.append({
            "position_id": f"{exchange}|{token}|{product}",
            "underlying": underlying,
            "exchange": exchange,
            "token": token,
            "neo_symbol": f"{exchange}|{token}",
            "trading_symbol": trading_symbol,
            "option_type": option_type,
            "strike": str(raw.get("stkPrc") or ""),
            "expiry": expiry,
            "product": product,
            "net_quantity": net_quantity,
            "lot_size": lot_size,
            "lots": net_quantity / lot_size,
            "ltp": ltp,
        })
    return positions


def create_app(
    broker: KotakBroker | None = None,
    market_feed: MarketFeed | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["KOTAK_BROKER"] = broker
    app.config["MARKET_FEED"] = market_feed
    app.config["CURRENT_CHAIN"] = None
    app.config["CURRENT_POSITIONS"] = []

    def get_broker() -> KotakBroker:
        active_broker = current_app.config.get("KOTAK_BROKER")
        if active_broker is None:
            raise RuntimeError("Broker is not connected. Restart the app and log in.")
        return active_broker

    def load_option_positions() -> list[dict]:
        positions = _normalize_option_positions(get_broker().get_positions())
        current_app.config["CURRENT_POSITIONS"] = positions
        feed = current_app.config.get("MARKET_FEED")
        if feed is not None and hasattr(feed, "set_positions"):
            feed.set_positions(positions)
        return positions

    def make_order_preview(request_data: dict):
        chain_data = current_app.config.get("CURRENT_CHAIN")
        normalized_request = dict(request_data)
        position_id = str(request_data.get("position_id") or "").strip()
        snapshot_source = "position report LTP" if position_id else "option-chain snapshot LTP"
        if position_id:
            # Refresh before Add More/Exit so a stale ticket cannot exceed the
            # currently open position or trade a contract that has already closed.
            try:
                positions = load_option_positions()
            except Exception:
                raise OrderPreviewError("Could not refresh Kotak positions; no order was sent.") from None
            position = next((item for item in positions if item["position_id"] == position_id), None)
            if position is None:
                raise OrderPreviewError("That option position is no longer open. Refresh Positions.")
            action = str(request_data.get("position_action") or "").strip().upper()
            if action not in {"ADD", "EXIT"}:
                raise OrderPreviewError("Choose Add More or Exit for this position ticket.")
            net_quantity = int(position["net_quantity"])
            expected_side = ("BUY" if net_quantity > 0 else "SELL") if action == "ADD" else ("SELL" if net_quantity > 0 else "BUY")
            if str(request_data.get("side") or "").strip().upper() != expected_side:
                raise OrderPreviewError("The side must match the selected position action.")
            quantity = _integer_value(request_data.get("quantity"), 0)
            lot_size = int(position["lot_size"])
            if action == "EXIT":
                if quantity < 1:
                    raise OrderPreviewError("Exit quantity must be greater than zero.")
                try:
                    reserved_quantity = pending_exit_quantity(position)
                except Exception:
                    raise OrderPreviewError("Could not verify active exit orders; no order was sent.") from None
                available_quantity = max(0, abs(net_quantity) - reserved_quantity)
                if quantity > available_quantity:
                    raise OrderPreviewError(
                        f"Exit quantity exceeds the available position ({available_quantity} units after active exit orders)."
                    )
            if quantity < 1 or quantity % lot_size != 0:
                raise OrderPreviewError(f"Quantity must be a positive multiple of the lot size ({lot_size}).")
            contract = {
                "symbol": position["trading_symbol"],
                "neo_symbol": position["neo_symbol"],
                "option_type": position["option_type"],
                "strike": position["strike"],
                "expiry": position["expiry"],
                "ltp": position.get("ltp"),
            }
            chain_data = {
                "instrument": position["underlying"],
                "exchange": position["exchange"],
                "expiry": position["expiry"],
                "market_lot": lot_size,
                "rows": [{
                    "strike": position["strike"],
                    "call" if position["option_type"] == "CE" else "put": contract,
                }],
            }
            normalized_request.update({
                "neo_symbol": position["neo_symbol"],
                "product": position["product"],
                "side": expected_side,
            })

        live_ltp = None
        feed = current_app.config.get("MARKET_FEED")
        neo_symbol = str(normalized_request.get("neo_symbol", "")).strip()
        if feed is not None and neo_symbol:
            quote = feed.snapshot().get("prices", {}).get(neo_symbol)
            if quote and quote.get("is_live") and quote.get("received_at"):
                try:
                    received_at = datetime.fromisoformat(str(quote["received_at"]))
                    age = datetime.now().astimezone() - received_at
                    if timedelta(seconds=0) <= age <= timedelta(seconds=30):
                        live_ltp = quote.get("ltp")
                except (TypeError, ValueError):
                    live_ltp = None
        return build_order_preview(
            chain_data,
            normalized_request,
            live_ltp=live_ltp,
            snapshot_source=snapshot_source,
        )

    def order_items() -> list[dict]:
        response = get_broker().get_order_report()
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise RuntimeError("Kotak returned an unexpected order-book response.")
        return [item for item in response["data"] if isinstance(item, dict)]

    def pending_exit_quantity(position: dict) -> int:
        expected_side = "S" if int(position["net_quantity"]) > 0 else "B"
        reserved = 0
        for item in order_items():
            if not _is_active_order(item):
                continue
            if str(item.get("trdSym") or "") != str(position["trading_symbol"]):
                continue
            if str(item.get("exSeg") or "").lower() != str(position["exchange"]).lower():
                continue
            if str(item.get("prod") or position["product"]).upper() != str(position["product"]).upper():
                continue
            side = str(item.get("trnsTp") or "").strip().upper()
            if side not in {expected_side, "SELL" if expected_side == "S" else "BUY"}:
                continue
            total = _integer_value(item.get("qty"), 0)
            filled = _integer_value(item.get("fldQty"), 0)
            reserved += max(0, total - filled)
        return reserved

    def find_active_order(order_id: str) -> dict | None:
        for item in order_items():
            if str(item.get("nOrdNo", "")) == order_id and _is_active_order(item):
                return item
        return None

    def action_response(response: dict, action: str, order_id: str) -> dict:
        outcome = _classify_broker_action(response)
        message = (
            f"Kotak acknowledged the {action} request; refresh the open-order list to verify its status."
            if outcome == "ACKNOWLEDGED" else
            str(response.get("emsg") or response.get("errMsg") or "Kotak rejected the request.")
            if outcome == "REJECTED" else
            f"No definitive {action} response was received. Refresh open orders before trying again."
        )
        return {
            "outcome": outcome,
            "order_id": order_id,
            "stat": response.get("stat"),
            "stCode": response.get("stCode"),
            "status_code": response.get("status_code"),
            "message": message,
        }

    @app.get("/")
    def home():
        return render_template("index.html", instruments=INSTRUMENTS.keys())

    @app.get("/api/expiries")
    def expiries():
        instrument = request.args.get("instrument", "", type=str).upper()
        if instrument not in INSTRUMENTS:
            return jsonify({"error": "Choose NIFTY or SENSEX."}), 400
        try:
            values = get_broker().get_expiries(instrument)
            return jsonify({"instrument": instrument, "expiries": values})
        except Exception as exc:
            app.logger.error("Expiry request failed (%s).", type(exc).__name__)
            return jsonify({"error": f"Expiry request failed ({type(exc).__name__}). Check the terminal."}), 502

    @app.get("/api/chain")
    def chain():
        instrument = request.args.get("instrument", "", type=str).upper()
        expiry = request.args.get("expiry", "", type=str)
        raw_size = request.args.get("strikes_per_side", "10")
        if instrument not in INSTRUMENTS:
            return jsonify({"error": "Choose NIFTY or SENSEX."}), 400
        if len(expiry) != 10:
            return jsonify({"error": "Choose an expiry date."}), 400
        try:
            strikes_per_side = int(raw_size)
        except (TypeError, ValueError):
            return jsonify({"error": "Choose 10, 20, 30, or 40 strikes per side."}), 400
        if strikes_per_side not in STRIKES_PER_SIDE_OPTIONS:
            return jsonify({"error": "Choose 10, 20, 30, or 40 strikes per side."}), 400
        try:
            values = get_broker().get_option_chain(instrument, expiry, strikes_per_side)
            current_app.config["CURRENT_CHAIN"] = values
            feed = current_app.config.get("MARKET_FEED")
            if feed is not None:
                feed.set_chain(values)
            return jsonify(values)
        except Exception as exc:
            app.logger.error("Option-chain request failed (%s).", type(exc).__name__)
            return jsonify({"error": f"Option-chain request failed ({type(exc).__name__}). Check the terminal."}), 502

    @app.get("/api/positions")
    def positions():
        try:
            values = load_option_positions()
            return jsonify({"positions": values, "count": len(values)})
        except Exception as exc:
            app.logger.error("Positions request failed (%s).", type(exc).__name__)
            return jsonify({"error": f"Positions request failed ({type(exc).__name__}). Check the terminal."}), 502

    @app.post("/api/order-preview")
    def order_preview():
        request_data = request.get_json(silent=True)
        if not isinstance(request_data, dict):
            return jsonify({"error": "Expected a JSON order-preview request."}), 400
        try:
            preview = make_order_preview(request_data)
            return jsonify(preview)
        except OrderPreviewError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.error("Order preview failed (%s).", type(exc).__name__)
            return jsonify({"error": "Could not build order preview."}), 500

    @app.post("/api/place-order")
    def place_order():
        request_data = request.get_json(silent=True)
        if not isinstance(request_data, dict):
            return jsonify({"outcome": "REJECTED", "message": "Expected a JSON order request."}), 400
        try:
            review = make_order_preview(request_data)
        except OrderPreviewError as exc:
            return jsonify({"outcome": "REJECTED", "message": str(exc)}), 400

        # Tracking tag only; it is not guaranteed to be an idempotency key.
        tag = f"PERSONAL-{uuid.uuid4().hex[:12]}"
        try:
            response = get_broker().place_live_order(review["broker_fields_preview"], tag)
        except Exception as exc:
            # A timeout/connection failure can happen after the broker receives the order.
            app.logger.error("Order request exception (%s); outcome may be unknown.", type(exc).__name__)
            return jsonify({
                "outcome": "UNKNOWN",
                "tag": tag,
                "message": (
                    "No definitive response was received. Do not retry; check the Kotak order book "
                    "for this order/tag first."
                ),
            }), 504

        if not isinstance(response, dict):
            return jsonify({
                "outcome": "UNKNOWN",
                "tag": tag,
                "message": "Unexpected broker response. Check the order book before retrying.",
            })

        stat = str(response.get("stat", "")).lower()
        order_id = response.get("nOrdNo")
        if stat in {"ok", "success"} or order_id:
            outcome = "ACKNOWLEDGED"
        elif stat in {"not_ok", "not ok", "error", "rejected"}:
            outcome = "REJECTED"
        else:
            outcome = "UNKNOWN"

        return jsonify({
            "outcome": outcome,
            "tag": tag,
            "order_id": order_id,
            "stat": response.get("stat"),
            "stCode": response.get("stCode"),
            "status_code": response.get("status_code"),
            "emsg": response.get("emsg") or response.get("errMsg"),
            "message": (
                "Broker acknowledged the request; verify its final status in Kotak."
                if outcome == "ACKNOWLEDGED" else
                "Broker returned a rejection." if outcome == "REJECTED" else
                "Response is ambiguous. Check the order book before retrying."
            ),
        })

    @app.get("/api/order-book")
    def order_book():
        try:
            orders = []
            for item in order_items():
                if not _is_active_order(item):
                    continue
                order_type = str(item.get("prcTp") or item.get("ordTyp") or "").upper()
                validity = str(item.get("vldt") or "DAY").upper()
                orders.append({
                    "order_id": item.get("nOrdNo"),
                    "tag": item.get("GuiOrdId"),
                    "symbol": item.get("trdSym") or item.get("sym"),
                    "exchange": item.get("exSeg"),
                    "side": item.get("trnsTp"),
                    "order_type": order_type,
                    "quantity": item.get("qty"),
                    "filled": item.get("fldQty"),
                    "average_price": item.get("avgPrc"),
                    "price": item.get("prc"),
                    "trigger_price": item.get("trgPrc"),
                    "validity": validity,
                    "status": _order_status(item),
                    "reject_reason": item.get("rejRsn"),
                    "lot_size": item.get("lotSz") or item.get("lLotSize") or 1,
                    "tick_size": item.get("tckSz") or "0.05",
                    "active": True,
                    "can_modify": order_type in {"L", "SL", "SL-M"} and validity in {"DAY", "IOC"},
                    "can_cancel": True,
                })
            return jsonify({"orders": orders})
        except Exception as exc:
            app.logger.error("Order-book request failed (%s).", type(exc).__name__)
            return jsonify({"error": f"Order-book request failed ({type(exc).__name__}). Check the terminal."}), 502

    @app.post("/api/orders/modify")
    def modify_order():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"outcome": "REJECTED", "message": "Expected a JSON modify request."}), 400
        order_id = str(data.get("order_id", "")).strip()
        if not order_id:
            return jsonify({"outcome": "REJECTED", "message": "Order ID is required."}), 400
        try:
            item = find_active_order(order_id)
            if item is None:
                return jsonify({"outcome": "REJECTED", "message": "That order is no longer open. Refresh the order list."}), 409
            order_type = str(item.get("prcTp") or item.get("ordTyp") or "").upper()
            if order_type not in {"L", "SL", "SL-M"}:
                return jsonify({"outcome": "REJECTED", "message": "Only limit and stop orders can be modified here."}), 400
            validity = str(item.get("vldt") or "DAY").upper()
            if validity not in {"DAY", "IOC"}:
                return jsonify({"outcome": "REJECTED", "message": "This order's validity is not supported for modification in the terminal."}), 400
            quantity_text = str(data.get("quantity", "")).strip()
            if not quantity_text.isdigit() or int(quantity_text) < 1:
                return jsonify({"outcome": "REJECTED", "message": "Quantity must be a positive whole number."}), 400
            quantity = int(quantity_text)
            filled = _integer_value(item.get("fldQty", 0) or 0, 0)
            lot_size = max(_integer_value(item.get("lotSz") or item.get("lLotSize") or 1, 1), 1)
            try:
                tick_size = Decimal(str(item.get("tckSz") or "0.05"))
                if not tick_size.is_finite() or tick_size <= 0:
                    tick_size = Decimal("0.05")
            except (InvalidOperation, TypeError, ValueError):
                tick_size = Decimal("0.05")
            if quantity <= filled:
                return jsonify({"outcome": "REJECTED", "message": "New total quantity must be greater than the already-filled quantity."}), 400
            if quantity % lot_size != 0:
                return jsonify({"outcome": "REJECTED", "message": f"Quantity must be a multiple of lot size ({lot_size})."}), 400

            if order_type in {"L", "SL"}:
                try:
                    price_value = Decimal(str(data.get("price", "")).strip())
                except (InvalidOperation, TypeError, ValueError):
                    return jsonify({"outcome": "REJECTED", "message": "Enter a valid limit price."}), 400
                if not price_value.is_finite() or price_value <= 0:
                    return jsonify({"outcome": "REJECTED", "message": "Limit price must be greater than zero."}), 400
                if price_value % tick_size != 0:
                    return jsonify({"outcome": "REJECTED", "message": f"Price must align to the order tick size ({tick_size})."}), 400
                price = format(price_value, "f")
            else:
                price = "0"

            trigger_price = "0"
            if order_type in {"SL", "SL-M"}:
                try:
                    trigger_value = Decimal(str(data.get("trigger_price", "")).strip())
                except (InvalidOperation, TypeError, ValueError):
                    return jsonify({"outcome": "REJECTED", "message": "Enter a valid trigger price."}), 400
                if not trigger_value.is_finite() or trigger_value <= 0:
                    return jsonify({"outcome": "REJECTED", "message": "Trigger price must be greater than zero."}), 400
                if trigger_value % tick_size != 0:
                    return jsonify({"outcome": "REJECTED", "message": f"Trigger price must align to the order tick size ({tick_size})."}), 400
                trigger_price = format(trigger_value, "f")

            response = get_broker().modify_live_order(order_id, {
                "price": price,
                "order_type": order_type,
                "quantity": quantity,
                "validity": validity,
                "trigger_price": trigger_price,
            })
            if not isinstance(response, dict):
                return jsonify({"outcome": "UNKNOWN", "order_id": order_id, "message": "Unexpected response. Refresh open orders to verify."})
            return jsonify(action_response(response, "modify", order_id))
        except Exception as exc:
            app.logger.error("Order modify request failed (%s); outcome may be unknown.", type(exc).__name__)
            return jsonify({"outcome": "UNKNOWN", "order_id": order_id, "message": "No definitive response. Refresh open orders before trying again."}), 504

    @app.post("/api/orders/cancel")
    def cancel_order():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"outcome": "REJECTED", "message": "Expected a JSON cancel request."}), 400
        order_id = str(data.get("order_id", "")).strip()
        if not order_id:
            return jsonify({"outcome": "REJECTED", "message": "Order ID is required."}), 400
        try:
            if find_active_order(order_id) is None:
                return jsonify({"outcome": "REJECTED", "message": "That order is no longer open. Refresh the order list."}), 409
            response = get_broker().cancel_live_order(order_id)
            if not isinstance(response, dict):
                return jsonify({"outcome": "UNKNOWN", "order_id": order_id, "message": "Unexpected response. Refresh open orders to verify."})
            return jsonify(action_response(response, "cancel", order_id))
        except Exception as exc:
            app.logger.error("Order cancel request failed (%s); outcome may be unknown.", type(exc).__name__)
            return jsonify({"outcome": "UNKNOWN", "order_id": order_id, "message": "No definitive response. Refresh open orders before trying again."}), 504

    @app.get("/api/live-quotes")
    def live_quotes():
        feed = current_app.config.get("MARKET_FEED")
        if feed is None:
            return jsonify({"status": "disabled", "prices": {}})
        return jsonify(feed.snapshot())

    return app


def main() -> None:
    print("Starting Kotak Neo option-chain terminal.")
    print("Order requests are sent once from the ticket Buy/Sell button.")
    broker = KotakBroker.login_interactive()

    market_feed = MarketFeed(broker.client)
    market_feed.start()

    app = create_app(broker, market_feed)
    print("Open http://127.0.0.1:5000 in your browser.")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
