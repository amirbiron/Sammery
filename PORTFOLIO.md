```yaml
name: "Sammery - בוט סיכומים שבועיים לטלגרם"
repo: "https://github.com/amirbiron/Sammery"
status: "פעיל (בייצור)"

one_liner: "בוט טלגרם שמאזין לפוסטים בערוץ, שומר אותם ב-MongoDB, ויוצר סיכומים שבועיים אוטומטיים באמצעות GPT-4."

stack:
  - Python 3.x
  - python-telegram-bot 21.x (עם job-queue)
  - OpenAI API (GPT-4 Turbo)
  - MongoDB (pymongo)
  - Flask + Gunicorn
  - schedule
  - pytz
  - Docker

key_features:
  - "יצירת סיכומים שבועיים חכמים באמצעות GPT-4 Turbo"
  - "אחסון יציב של פוסטים ב-MongoDB Atlas"
  - "תזמון גמיש - בחירת שעת שליחה ביום שישי (14:00-19:00)"
  - "תצוגה מקדימה לפני פרסום עם אפשרות ליצור מחדש"
  - "אכלוס נתונים ידני - Forward פוסטים ישנים לבוט"
  - "תמיכה בתמונת כותרת לסיכום (file_id)"
  - "פרסום אוטומטי עם AUTO_PUBLISH_ON_START"
  - "שרת Flask עם נתיב בריאות לפריסה על Render"

architecture:
  summary: |
    בוט טלגרם מבוסס python-telegram-bot עם OpenAI GPT-4 ליצירת סיכומים.
    MongoDB Atlas לאחסון פוסטים. Flask + Gunicorn כשרת ווב לשמירה על
    חיות השירות ב-Render. תמיכה ב-Docker לפריסה. תזמון אוטומטי עם schedule.
  entry_points:
    - "main.py - כל הלוגיקה: בוט, GPT-4 סיכומים, תזמון, Flask"
    - "Dockerfile - הגדרת קונטיינר לפריסה"
    - "render.yaml - קונפיגורציית Render"
    - "activity_reporter.py - דיווח פעילות למערכת Suspended"

demo:
  live_url: "" # TODO: בדוק ידנית
  video_url: "" # TODO: בדוק ידנית

setup:
  quickstart: |
    1. git clone <repository-url> && cd Sammery
    2. pip install -r requirements.txt
    3. הגדר משתני סביבה: TELEGRAM_BOT_TOKEN, CHANNEL_USERNAME, OPENAI_API_KEY, ADMIN_CHAT_ID, MONGODB_URI
    4. python main.py
    (או: docker build -t sammery . && docker run sammery)

your_role: "פיתוח מלא - ארכיטקטורה, אינטגרציה עם GPT-4 ו-Telegram, תזמון, פריסה"

tradeoffs:
  - "GPT-4 Turbo במקום מודל זול יותר - איכות סיכום גבוהה על חשבון עלות"
  - "כל הלוגיקה בקובץ אחד - פשטות על חשבון מודולריות"
  - "Flask + Gunicorn לשמירה על חיות - workaround לפלטפורמות שלא תומכות בתהליכים ארוכים"
  - "MongoDB Atlas - אמינות ענן על חשבון עלות פוטנציאלית"

metrics: {} # TODO: בדוק ידנית

faq:
  - q: "איך הבוט יודע מה לסכם?"
    a: "הבוט מאזין לפוסטים בערוץ ושומר אותם ב-MongoDB. בזמן יצירת סיכום הוא שולף את הפוסטים מה-7 ימים האחרונים"
  - q: "אפשר לשנות את סגנון הסיכום?"
    a: "כן, על ידי עריכת הפרומפט בפונקציה create_summary_with_gpt4 ב-main.py"
  - q: "מה קורה אם Render מפעיל מחדש?"
    a: "DEFAULT_SCHEDULE_TIME משחזר את התזמון אוטומטית בכל עלייה"
```
