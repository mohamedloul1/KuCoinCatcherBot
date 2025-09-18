from kucoin.client import Market
import requests
import time
import os
import json
from dotenv import load_dotenv

load_dotenv()

# ── Env check ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_BOT_TOKEN of TELEGRAM_CHAT_ID ontbreekt in .env")

# ── Globals ─────────────────────────────────────────────────────────────────────
user_wants_messages = True          # meldingen aan/uit
scan_enabled = True                 # KuCoin-requests aan/uit
reset_baseline_on_start = False     # bij /start de baseline vernieuwen
last_update_id = None

COINS_FILE = "coins.json"
threshold = 0.0198  # 1.98%

processed_callback_ids = set()
followed_owners = set()

# Eén KuCoin Market-client hergebruiken
market = Market(url='https://api.kucoin.com')

# Optioneel: requests.Session voor connectie-hergebruik
http = requests.Session()


# ── Helpers ─────────────────────────────────────────────────────────────────────
def load_coins():
    try:
        with open(COINS_FILE, 'r') as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_coins(coins):
    with open(COINS_FILE, 'w') as file:
        json.dump(coins, file, indent=4)

def send_telegram_message(text, buttons=None):
    global user_wants_messages
    if not user_wants_messages:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})

    try:
        resp = http.post(url, data=payload, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending message: {e}")

def receive_telegram_commands():
    global user_wants_messages, scan_enabled, reset_baseline_on_start
    global last_update_id, threshold, processed_callback_ids, followed_owners

    coins = load_coins()

    # Long-polling met timeout: zuiniger & minder calls
    params = {"timeout": 25}
    if last_update_id:
        params["offset"] = last_update_id + 1

    try:
        response = http.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params=params,
            timeout=30
        )
    except requests.exceptions.RequestException as e:
        print(f"Failed to receive commands: {e}")
        return

    if response.status_code == 200:
        messages = response.json().get("result", [])
        if messages:
            for message in messages:
                if "message" in message and "text" in message["message"]:
                    text = message["message"]["text"].lower()

                    if text == "/stop":
                        send_telegram_message("You will no longer receive messages.")
                        user_wants_messages = False
                        scan_enabled = False

                    elif text == "/start":
                        user_wants_messages = True
                        scan_enabled = True
                        reset_baseline_on_start = True
                        send_telegram_message("You will now receive messages again.")

                    elif text == "/follow_owners":
                        owners = {coin['owner'] for coin in coins if coin.get('owner')}
                        if not owners:
                            send_telegram_message("Geen owners bekend. Voeg eerst coins toe met /add_coin SYMBOL OWNER.")
                        else:
                            buttons = [[{"text": owner, "callback_data": f"follow_{owner}"}] for owner in owners]
                            buttons.append([{"text": "No Filter", "callback_data": "follow_all"}])
                            send_telegram_message("Select owners to follow:", buttons=buttons)

                    elif text.startswith("/add_coin "):
                        coin_owner_string = text[len("/add_coin "):]
                        parts = coin_owner_string.split(" ", 1)
                        if len(parts) == 2:
                            new_coin, owner = parts
                            new_coin = new_coin.upper()
                            if not any(coin['symbol'] == new_coin for coin in coins):
                                coins.append({"symbol": new_coin, "owner": owner})
                                save_coins(coins)
                                send_telegram_message(f"Coin {new_coin} added with owner {owner}.")
                            else:
                                send_telegram_message(f"Coin {new_coin} is already in the list.")
                        else:
                            send_telegram_message("Invalid command format. Use '/add_coin SYMBOL OWNER'.")

                    elif text.startswith("/delete_coin "):
                        coin_to_delete = text[len("/delete_coin "):].upper()
                        coin_found = next((coin for coin in coins if coin['symbol'] == coin_to_delete), None)
                        if coin_found:
                            coins.remove(coin_found)
                            save_coins(coins)
                            send_telegram_message(f"Coin {coin_to_delete} deleted.")
                        else:
                            send_telegram_message(f"Coin {coin_to_delete} not found.")

                    elif text == "/view_coins":
                        if coins:
                            coins_list = "\n".join([f"({coin['symbol']} / {coin['owner']})" for coin in coins])
                            send_telegram_message(f"Current coins list:\n{coins_list}")
                        else:
                            send_telegram_message("The coins list is currently empty.")

                    elif text == "/set_threshold":
                        buttons = [[
                            {"text": "Small Move 1%",   "callback_data": "threshold_0.01"},
                            {"text": "Standard 1.98%", "callback_data": "threshold_0.0198"},
                            {"text": "Big Move 3.4%",  "callback_data": "threshold_0.034"},
                        ]]
                        send_telegram_message("Choose a new threshold:", buttons=buttons)

                    elif text == "/view_settings":
                        followed_list = ", ".join(followed_owners) or "No Filter"
                        send_telegram_message(
                            f"Current settings:\nThreshold: {threshold * 100:.2f}%\nFollowing owners: {followed_list}"
                        )

                    last_update_id = message["update_id"]

                elif "callback_query" in message:
                    callback_id = message["callback_query"]["id"]
                    if callback_id in processed_callback_ids:
                        continue
                    processed_callback_ids.add(callback_id)

                    callback_data = message["callback_query"]["data"]
                    if callback_data.startswith("threshold_"):
                        try:
                            threshold = float(callback_data.split("_")[1])
                            send_telegram_message(f"Threshold set to {threshold * 100:.2f}%.")
                        except ValueError:
                            send_telegram_message("Invalid threshold value received.")

                    elif callback_data.startswith("follow_"):
                        owner = callback_data.split("_", 1)[1]
                        if owner == "all":
                            followed_owners.clear()
                            send_telegram_message("Now scanning all coins without filters.")
                        else:
                            if owner in followed_owners:
                                followed_owners.remove(owner)
                            else:
                                followed_owners.add(owner)
                            followed_list = ", ".join(followed_owners) or "None"
                            send_telegram_message(f"Now following: {followed_list}")

                    # Housekeeping: voorkom onbegrensde groei
                    if len(processed_callback_ids) > 5000:
                        processed_callback_ids.clear()
    else:
        print("Failed to receive commands: HTTP", response.status_code)


# 🔧 Init: sla oude Telegram-updates over bij opstart
try:
    response = http.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
        timeout=15
    )
    updates = response.json().get("result", [])
    if updates:
        last_update_id = updates[-1]["update_id"]
        print(f"Start vanaf update_id: {last_update_id}")
except Exception as e:
    print(f"Fout bij ophalen van updates bij opstart: {e}")


# ── Rate limiter ────────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, max_requests, period):
        self.max_requests = max_requests
        self.period = period
        self.timestamps = []

    def wait(self):
        now = time.time()
        while self.timestamps and now - self.timestamps[0] > self.period:
            self.timestamps.pop(0)
        if len(self.timestamps) >= self.max_requests:
            sleep_time = self.period - (now - self.timestamps[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
        self.timestamps.append(time.time())


# ── KuCoin fetch ────────────────────────────────────────────────────────────────
def fetch_prices(retry_count=3, timeout=10, rate_limiter=None):
    for attempt in range(retry_count):
        try:
            if rate_limiter:
                rate_limiter.wait()
            tickers = market.get_all_tickers()
            usdt_pairs = {}
            for ticker in tickers['ticker']:
                base = ticker['symbol'].split('-')[0]
                if ticker['symbol'].endswith('USDT') and not any(
                    excl in base for excl in ['UP', 'DOWN', '3L', '2L', '3S', '2S']
                ):
                    last_price = ticker.get('last')
                    if last_price is not None:
                        usdt_pairs[ticker['symbol']] = float(last_price)
            return usdt_pairs

        except requests.exceptions.Timeout:
            send_telegram_message(f"Attempt {attempt + 1} of {retry_count}: Request timed out. Retrying...")
            time.sleep(2)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                send_telegram_message("Rate limit exceeded. Pausing for 5 minutes.")
                time.sleep(300)
                continue
            else:
                send_telegram_message(f"HTTP error: {e}")
        except Exception as e:
            send_telegram_message(f"Error fetching prices: {e}")
            return {}

    send_telegram_message("Maximum retry attempts reached. Exiting.")
    return {}


# ── Price change check ─────────────────────────────────────────────────────────
def check_price_changes(initial_prices, current_prices):
    global threshold, followed_owners
    loaded_coins = load_coins()

    for symbol, initial_price in initial_prices.items():
        current_price = current_prices.get(symbol)
        if current_price is None:
            continue

        change = (current_price - initial_price) / initial_price
        base_token = symbol.split('-')[0]
        owner_info = next((c["owner"] for c in loaded_coins if c["symbol"] == base_token), None)

        if (not followed_owners or owner_info in followed_owners) and change >= threshold:
            msg = f"{symbol} is up {change*100:.2f}%"
            if owner_info:
                msg += f" owned by {owner_info} ✅"

            buttons = [[{"text": "KuCoin", "url": f"https://www.kucoin.com/price/{base_token}"}]]
            send_telegram_message(msg, buttons=buttons)


# ── Main loop ──────────────────────────────────────────────────────────────────
def main():
    print("Starting bot...")
    global reset_baseline_on_start

    # Rustiger limiet; we callen maar 1x per 5s
    rate_limiter = RateLimiter(max_requests=5, period=1)

    send_telegram_message("Fetching initial prices for USDT pairs...")
    initial_prices = fetch_prices(rate_limiter=rate_limiter)

    send_telegram_message(
        "\U0001F680 *Bot is now live!* ✅\n\n"
        "▶️ */start* to start receiving messages\n\n"
        "⏹️ */stop* to stop receiving messages\n\n"
        "🔍 */view_coins* to view coins\n\n"
        "➕ */add_coin* <COIN> <OWNER> to add a coin\n\n"
        "❌ */delete_coin* <COIN> to remove a coin\n\n"
        "⚙️ */set_threshold* to set a threshold\n\n"
        "👤 */follow_owners* to select owners to follow\n\n"
        "🔧 */view_settings* to view current settings"
    )

    try:
        while True:
            receive_telegram_commands()

            # reset baseline na /start
            if reset_baseline_on_start:
                initial_prices = fetch_prices(rate_limiter=rate_limiter)
                reset_baseline_on_start = False
                time.sleep(1)
                continue

            # alleen scannen als het aan staat
            if scan_enabled:
                current_prices = fetch_prices(rate_limiter=rate_limiter)
                check_price_changes(initial_prices, current_prices)
                initial_prices = current_prices

            time.sleep(5)  # voorkom hammering
    except KeyboardInterrupt:
        send_telegram_message("Stopped monitoring.")


# ── Runner ─────────────────────────────────────────────────────────────────────
running = True
while running:
    try:
        main()
    except KeyboardInterrupt:
        send_telegram_message("Stopped by user.")
        break
    except Exception as e:
        print(f"Er is een fout opgetreden: {e}. Herstarten...")
        time.sleep(10)
