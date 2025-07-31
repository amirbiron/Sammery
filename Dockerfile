# שלב 1: שימוש בגרסת פייתון ספציפית ויציבה. 3.11 היא בחירה מצוינת.
FROM python:3.11-slim

# שלב 2: הגדרת תיקיית העבודה בתוך הקונטיינר
WORKDIR /app

# שלב 3: העתקת קובץ התלויות והתקנתן
# שלב זה מבוצע בנפרד כדי לנצל את מנגנון המטמון (cache) של דוקר
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# שלב 4: העתקת שאר קוד האפליקציה שלך
COPY . .

# שלב 5: חשיפת הפורט ששרת ה-"Keep-Alive" שלך ירוץ עליו
EXPOSE 8000

# שלב 6: הגדרת הפקודה להרצת האפליקציה
# פקודה זו מריצה את שרת ה-Flask שלך באמצעות Gunicorn, שהוא שרת ווב ברמת Production.
# הפקודה מניחה שאובייקט ה-Flask בקובץ main.py שלך נקרא 'app'.
# הלוגיקה המרכזית של הבוט (application.run_polling) תופעל ב-thread נפרד על ידי קוד הפייתון שלך.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "main:app"]
