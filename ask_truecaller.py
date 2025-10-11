import os, asyncio
from typing import List
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PHONE = os.getenv("PHONE", "")
TARGET_BOT = os.getenv("TARGET_BOT", "@TrueCaller1Bot")
SESSION = os.getenv("SESSION_NAME", "tc_user_session")

client = TelegramClient(SESSION, API_ID, API_HASH)

async def ask_once(text: str, collect_window_sec: float = 6.0) -> List[str]:
    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(PHONE)
        code = input("הכנס קוד אימות מטלגרם: ").strip()
        await client.sign_in(PHONE, code)

    entity = await client.get_entity(TARGET_BOT)

    # שולח ומחכה לתגובה ראשונה
    async with client.conversation(entity, timeout=60) as conv:
        await conv.send_message(text)
        first = await conv.get_response()

    # מחכים חלון זמן כדי לאפשר עריכה/הודעות נוספות
    await asyncio.sleep(collect_window_sec)

    # --- רענון ההודעה הראשונה (יתכן ונערכה) ---
    # נסה לקבל אובייקט יחיד:
    refreshed_first = await client.get_messages(entity, ids=first.id)
    # אם משום מה קיבלנו רשימה, קח את הראשון
    if isinstance(refreshed_first, (list, tuple)):
        refreshed_first = refreshed_first[0] if refreshed_first else None

    replies: List[str] = []
    if refreshed_first is not None:
        replies.append((refreshed_first.text or "").strip())
    else:
        # fallback: אם מסיבה כלשהי לא התקבל, נכניס את המקורי
        replies.append((first.text or "").strip())

    # --- איסוף הודעות נוספות שמגיעות אחרי הראשונה ---
    more: List[str] = []
    async for msg in client.iter_messages(entity, min_id=first.id):
        # רק נכנסות (מהבוט) ורק טקסט לא ריק
        if not msg.out and (msg.text or "").strip():
            more.append(msg.text.strip())

    replies.extend(reversed(more))  # לשמור על סדר כרונולוגי
    # סינון כפילויות אם הבוט שולח/עורך כמה פעמים את אותו טקסט
    cleaned = []
    for r in replies:
        if r and (not cleaned or cleaned[-1] != r):
            cleaned.append(r)
    return cleaned

async def main():
    q = input("כתוב את רשימת המספרים אותם תרצה לאתר:\n").strip()
    # אם אתה תמיד רוצה להוסיף קידומת +972:
    if not q.startswith("+972"):
        q = "+972" + q
    res = await ask_once(q)
    print("=== תשובות שהתקבלו ===")
    for i, r in enumerate(res, 1):
        print(f"[{i}] {r}")

if __name__ == "__main__":
    asyncio.run(main())
