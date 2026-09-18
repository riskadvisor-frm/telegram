import hashlib
import json
import os
import random
import time
from datetime import datetime

import requests


def md5_sign(data: dict, key: str) -> str:
    """
    Generate MD5 signature for AllPay API.
    Sorts parameters alphabetically and builds query string with key.
    Excludes 'sign' and 'sign_type' parameters from signature calculation.
    """
    # Create a copy and remove sign-related parameters
    sign_data = data.copy()
    sign_data.pop("sign", None)
    sign_data.pop("sign_type", None)

    # Sort the dictionary by keys alphabetically
    sorted_items = sorted(sign_data.items())

    # Build query string manually, excluding empty values
    query_parts = []
    for k, v in sorted_items:
        # Skip empty parameters as per documentation
        if v is not None and str(v).strip() != "":
            query_parts.append(f"{k}={v}")

    # Join with & and append the key
    query_string = "&".join(query_parts)
    string_to_sign = f"{query_string}&key={key}"

    # Generate MD5 hash and return lowercase (as shown in example)
    return hashlib.md5(string_to_sign.encode("utf-8")).hexdigest()


def allpay_100518738(amount: int | None = None):
    """
    AllPay payment gateway integration.
    Creates a payment order and returns the payment URL or payInfo.
    """
    GATEWAY_NAME = "ALLPAY"
    ALLPAY_API = "https://payment.allapay.com/pay/web"

    # Test merchant credentials (replace with actual values)
    MCH_ID = "100518738"  # From documentation example
    SECRET_KEY = "5146eed00721406c9668b803ce936346"  # Replace with actual key from merchant backend

    # Use provided amount or generate random amount
    if amount is not None:
        AMOUNT = str(int(amount))
    else:
        min_amount = 150
        max_amount = 200
        AMOUNT = str(random.randint(min_amount, max_amount))
    NOTIFY_URL = "https://royal888.bet/payment/notify/allpay"
    PAGE_URL = "https://royal888.bet/payment/success"
    PAY_TYPE = "104"  # Consult merchant backend for channel coding
    GOODS_NAME = "test"
    MCH_RETURN_MSG = "test"

    HTTP_TIMEOUT_GATEWAY = float(os.getenv("HTTP_TIMEOUT_GATEWAY", "20"))

    # Generate unique order ID with timestamp
    order_id = f"allpay{int(time.time())}{int(time.time() * 1000) % 1000:03d}"

    # Get current timestamp in required format
    order_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build the data payload (without signature first)
    # Note: version=1.0 is required for JSON response
    data = {
        "version": "1.0",
        "mch_id": MCH_ID,
        "notify_url": NOTIFY_URL,
        "page_url": PAGE_URL,
        "mch_order_no": order_id,
        "pay_type": PAY_TYPE,
        "trade_amount": AMOUNT,
        "order_date": order_date,
        "goods_name": GOODS_NAME,
        "mch_return_msg": MCH_RETURN_MSG,
        "sign_type": "MD5",
    }

    # Add optional parameters if needed
    # data["bank_code"] = ""  # Only for online banking channels
    # data["payer_name"] = ""  # Required for Argentina
    # data["payer_card"] = ""  # Required for Thailand scanning channel
    # data["payer_phone"] = ""  # Required for Kenya collection (with international code)

    # Generate signature BEFORE adding it to data
    signature = md5_sign(data, SECRET_KEY)
    data["sign"] = signature

    # Prepare headers for form-encoded data
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        # Make the API request
        resp = requests.post(
            ALLPAY_API, data=data, headers=headers, timeout=HTTP_TIMEOUT_GATEWAY
        )
        resp.raise_for_status()

        try:
            response_data = resp.json()
        except json.JSONDecodeError:
            raise RuntimeError(
                f"Non-JSON gateway: {GATEWAY_NAME} response: {resp.text[:300]}"
            )

        print(response_data)

        # Check if the response indicates success
        if response_data.get("respCode") == "SUCCESS":
            trade_result = response_data.get("tradeResult")
            if trade_result == "1":  # Order successful
                # Return the payment URL from payInfo
                pay_info = response_data.get("payInfo")
                if pay_info:
                    return pay_info
                else:
                    raise RuntimeError(
                        f"Gateway: {GATEWAY_NAME} success but no payInfo: {json.dumps(response_data, ensure_ascii=False)}"
                    )
            else:
                raise RuntimeError(
                    f"Gateway: {GATEWAY_NAME} order failed, tradeResult: {trade_result}, msg: {response_data.get('tradeMsg', 'Unknown error')}"
                )
        else:
            # Handle failure response
            resp_code = response_data.get("respCode", "UNKNOWN")
            trade_msg = response_data.get("tradeMsg", "Unknown error")
            raise RuntimeError(
                f"Gateway: {GATEWAY_NAME} error - respCode: {resp_code}, msg: {trade_msg}"
            )

    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Gateway: {GATEWAY_NAME} request failed: {str(e)}")
    except Exception as e:
        raise RuntimeError(f"Gateway: {GATEWAY_NAME} unexpected error: {str(e)}")


def get_pause_duration() -> float:
    """Returns pause duration (seconds) when duplicate UPI detected."""
    rand = random.random()
    if rand < 0.05:
        return random.uniform(960, 1080)  # 16-18 minutes (5%)
    elif rand < 0.15:
        return random.uniform(300, 480)  # 5-8 minutes (10%)
    else:
        return random.uniform(120, 180)  # 2-3 minutes (85%)

if __name__ == "__main__":
    print(allpay_100518738(amount=100))
