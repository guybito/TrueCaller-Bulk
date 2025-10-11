# בונים דימוי קל של Python
FROM python:3.11-slim

# מתקינים תלות בסיסית (נדרש ל-Telethon ול-FastAPI)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && \
    rm -rf /var/lib/apt/lists/*

# יוצרים תיקייה לאפליקציה
WORKDIR /app

# מעתיקים את דרישות הספריות ומתקינים
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# מעתיקים את שאר קבצי הפרויקט (כולל server.py)
COPY . .

# קובעים משתנה סביבה PORT כברירת מחדל
ENV PORT=8000

# מריצים את השרת
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
