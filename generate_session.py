"""One-shot helper to generate a Telethon StringSession for your user account.

Run locally (NOT on Railway). It will ask for your API id/hash and phone
number, send a login code to your Telegram, and print a session string.
Copy that string into Railway as TELEGRAM_SESSION_STRING.

Usage:
    python generate_session.py
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession


def main() -> None:
    api_id = int(input("TELEGRAM_API_ID (from https://my.telegram.org/apps): ").strip())
    api_hash = input("TELEGRAM_API_HASH: ").strip()
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        print()
        print("=" * 60)
        print("TELEGRAM_SESSION_STRING (paste this as a Railway secret):")
        print(client.session.save())
        print("=" * 60)


if __name__ == "__main__":
    main()
