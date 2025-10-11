# make_string_session.py
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
import os

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PHONE = os.environ["PHONE"]

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    client.start(PHONE)
    print("STRING_SESSION:", client.session.save())