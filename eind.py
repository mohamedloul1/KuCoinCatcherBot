from kucoin.client import Market
import requests
import time
import os
import json

# Telegram parameters
TELEGRAM_BOT_TOKEN = ''
TELEGRAM_CHAT_ID = ''

# Gebruiker berichten ontvangen status
user_wants_messages = True

COINS_FILE = "coins.json"
threshold = 0.0198  # Standaard drempelwaarde

processed_callback_ids = set()  # Set om verwerkte callbacks bij te houden
followed_owners = set()  # Set om gevolgde eigenaren bij te houden

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
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": buttons
        })

    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending message: {e}")

def receive_telegram_commands():
    global user_wants_messages, last_update_id, threshold, processed_callback_ids, followed_owners
    coins = load_coins()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    if last_update_id:
        url += f"?offset={last_update_id + 1}"
    response = requests.get(url)

    if response.status_code == 200:
        messages = response.json()["result"]

        if messages:
            for message in messages:
                if "message" in message and "text" in message["message"]:
                    text = message["message"]["text"].lower()

                    if text == "/stop":
                        send_telegram_message("You will no longer receive messages.")
                        user_wants_messages = False
                    elif text == "/start":
                        user_wants_messages = True
                        send_telegram_message("You will now receive messages again.")
                    elif text == "/follow_owners":
                        owners = {coin['owner'] for coin in coins}
                        buttons = [[{"text": owner, "callback_data": f"follow_{owner}"}] for owner in owners]
                        buttons.append([{ "text": "No Filter", "callback_data": "follow_all" }])
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
                        buttons = [
                            [
                                {"text": "Small Move 1%", "callback_data": "threshold_0.01"},
                                {"text": "Standard 1.90%", "callback_data": "threshold_0.019"},
                                {"text": "Big Move 3.4%", "callback_data": "threshold_0.034"}
                            ]
                        ]
                        send_telegram_message("Choose a new threshold:", buttons=buttons)
                    elif text == "/view_settings":
                        followed_list = ", ".join(followed_owners) or "No Filter"
                        send_telegram_message(f"Current settings:\nThreshold: {threshold * 100:.2f}%\nFollowing owners: {followed_list}")

                    last_update_id = messages[-1]["update_id"]
                elif "callback_query" in message:
                    callback_id = message["callback_query"]["id"]
                    if callback_id in processed_callback_ids:
                        continue  # Skip already processed callbacks
                    processed_callback_ids.add(callback_id)

                    callback_data = message["callback_query"]["data"]
                    if callback_data.startswith("threshold_"):
                        threshold = float(callback_data.split("_")[1])
                        send_telegram_message(f"Threshold set to {threshold * 100:.2f}%.")
                    elif callback_data.startswith("follow_"):
                        owner = callback_data.split("_")[1]
                        if owner == "all":
                            followed_owners.clear()  # Clear all specific owner selections
                            send_telegram_message("Now scanning all coins without filters.")
                        else:
                            if owner in followed_owners:
                                followed_owners.remove(owner)
                            else:
                                followed_owners.add(owner)
                            followed_list = ", ".join(followed_owners) or "None"
                            send_telegram_message(f"Now following: {followed_list}")

    else:
        print("Failed to receive commands")

last_update_id = None

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

def fetch_prices(retry_count=3, timeout=10, rate_limiter=None):
    market = Market(url='https://api.kucoin.com')
    for attempt in range(retry_count):
        try:
            if rate_limiter:
                rate_limiter.wait()
            tickers = market.get_all_tickers()
            usdt_pairs = {}
            for ticker in tickers['ticker']:
                if ticker['symbol'].endswith('USDT') and not any(exclusion in ticker['symbol'].split('-')[0] for exclusion in ['UP', 'DOWN', '3L', '2L', '3S', '2S']):
                    last_price = ticker.get('last')
                    if last_price is not None:
                        usdt_pairs[ticker['symbol']] = float(last_price)
            return usdt_pairs
        except requests.exceptions.Timeout:
            send_telegram_message(f"Attempt {attempt + 1} of {retry_count}: Request timed out. Retrying...")
            time.sleep(2)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
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

def check_price_changes(initial_prices, current_prices):
    global threshold, followed_owners
    loaded_coins = load_coins()
    for symbol, initial_price in initial_prices.items():
        if symbol in current_prices:
            current_price = current_prices[symbol]
            change = (current_price - initial_price) / initial_price
            owner_info = next((coin["owner"] for coin in loaded_coins if coin["symbol"] == symbol.split('-')[0]), None)
            if (not followed_owners or owner_info in followed_owners) and change >= threshold:
                message = f"{symbol} is up {change*100:.2f}%"
                if owner_info:
                    message += f" owned by {owner_info} ✅"

                base_token = symbol.split('-')[0]
                buttons = [
                    [
                        {"text": "KuCoin", "url": f"https://www.kucoin.com/price/{base_token}"},
                    ]
                ]
                send_telegram_message(message, buttons=buttons)

def main():
    global user_wants_messages
    rate_limiter = RateLimiter(max_requests=1800, period=60)
    send_telegram_message("Fetching initial prices for USDT pairs...")
    initial_prices = fetch_prices(rate_limiter=rate_limiter)
    send_telegram_message("\U0001F680 *Bot is now live!* ✅\n\n"
                          "▶️ */start* to start receiving messages\n\n"
                          "⏹️ */stop* to stop receiving messages\n\n"
                          "🔍 */view_coins* to view coins\n\n"
                          "➕ */add_coin* <COIN> <OWNER> to add a coin\n\n"
                          "❌ */delete_coin* <COIN> to remove a coin\n\n"
                          "⚙️ */set_threshold* to set a threshold\n\n"
                          "👤 */follow_owners* to select owners to follow\n\n"
                          "🔧 */view_settings* to view current settings")
    try:
        while True:
            receive_telegram_commands()
            current_prices = fetch_prices(rate_limiter=rate_limiter)
            check_price_changes(initial_prices, current_prices)
            initial_prices = current_prices
    except KeyboardInterrupt:
        send_telegram_message("Stopped monitoring.")

running = True
while running:
    try:
        main()
    except Exception as e:
        print(f"Er is een fout opgetreden: {e}. Herstarten...")
        time.sleep(10)
