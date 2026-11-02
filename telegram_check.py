
import json
import os
import urllib.parse
import urllib.error
import urllib.request

token = os.environ["TELEGRAM_BOT_TOKEN"]
chat_id = "-1004343274047"

def check(method, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return json.loads(error.read().decode("utf-8"))

me = check("getMe")
if not me.get("ok"):
    print("BOT CHECK FAILED:", me)
    raise SystemExit(1)

print("GitHub is using bot: @" + me["result"]["username"])

chat = check("getChat", {"chat_id": chat_id})
if chat.get("ok"):
    print("CHANNEL FOUND:", chat["result"].get("title"))
else:
    print("CHANNEL CHECK FAILED:", chat.get("description"))
