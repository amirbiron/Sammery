import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict
import json
import openai
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode
import schedule
import time
from threading import Thread
import pytz
import threading
from flask import Flask
from pymongo import MongoClient, DESCENDING

# ===============================================
# שרת אינטרנט מינימלי עבור Render
# ===============================================
app = Flask(__name__)

@app.route('/')
def health_check():
    return "OK, Bot is running!", 200

# ... (שאר הקוד שלך, הגדרת הקלאס TelegramSummaryBot וכו') ...

# הגדרת לוגים
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class TelegramSummaryBot:
    def __init__(self):
        # משתני סביבה
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.channel_username = os.getenv('CHANNEL_USERNAME', 'AndroidAndAI')
        self.openai_api_key = os.getenv('OPENAI_API_KEY')
        self.admin_chat_id = os.getenv('ADMIN_CHAT_ID')
        
        if not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set!")
        # אתחול לקוח OpenAI החדש
        self.openai_client = openai.OpenAI(api_key=self.openai_api_key)
        
        # אתחול הבוט
        self.application = Application.builder().token(self.bot_token).build()
        
        # אתחול MongoDB
        mongo_uri = os.getenv('MONGODB_URI')
        if not mongo_uri:
            raise ValueError("MONGODB_URI environment variable not set!")
        self.mongo_client = MongoClient(mongo_uri)
        self.db = self.mongo_client.telegram_bot_db
        self.posts_collection = self.db.posts
        logger.info("Successfully connected to MongoDB.")
        
        # משתני מצב
        self.pending_summary = None
        self.israel_tz = pytz.timezone('Asia/Jerusalem')
        
        # הוספת handlers
        self._setup_handlers()
    
    def _setup_handlers(self):
        """הגדרת handlers לבוט"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("generate_summary", self.generate_summary_command))
        self.application.add_handler(CommandHandler("preview", self.preview_command))
        self.application.add_handler(CommandHandler("cancel_schedule", self.cancel_schedule_command))
        self.application.add_handler(CommandHandler("show_schedule", self.show_schedule_command))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        self.application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, self.handle_new_channel_post))
        self.application.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, self.handle_forwarded_post))
    
    async def handle_new_channel_post(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """תופס פוסטים חדשים מהערוץ ושומר אותם ל-MongoDB"""
        message = update.channel_post
        post_content = message.text or message.caption
        
        if not post_content:
            return

        logger.info(f"New post {message.message_id} detected in channel. Saving to MongoDB.")
        
        new_post = {
            'message_id': message.message_id,
            'date': message.date,  # שמירת התאריך כאובייקט Datetime של Python
            'text': post_content
        }
        
        try:
            self.posts_collection.insert_one(new_post)
            logger.info("Post saved successfully.")
        except Exception as e:
            logger.error(f"Error saving new post to MongoDB: {e}", exc_info=True)
    
    async def handle_forwarded_post(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        תופס הודעות המועברות לבוט, בודק אם הן מהערוץ הנכון, ושומר אותן ב-MongoDB.
        זה מאפשר "מילוי לאחור" (backfill) ידני של פוסטים ישנים.
        """
        message = update.message
        
        # ודא שההודעה הועברה מהערוץ שלך
        if not message.forward_origin:
            await message.reply_text("אני יכול לשמור רק הודעות מועברות.")
            return
            
        # בדיקה אם ההודעה הועברה מהערוץ הנכון
        origin_chat = getattr(message.forward_origin, 'chat', None)
        if not origin_chat or not hasattr(origin_chat, 'username') or origin_chat.username != self.channel_username:
            await message.reply_text("אני יכול לשמור רק הודעות שהועברו מהערוץ הראשי.")
            return

        post_content = message.text or message.caption
        
        if not post_content:
            await message.reply_text("לא ניתן לשמור הודעה ללא טקסט.")
            return

        # חילוץ פרטי ההודעה המקורית
        original_message_id = message.forward_origin.message_id
        original_date = message.forward_origin.date

        logger.info(f"Manual backfill: Received forwarded post {original_message_id}. Saving to MongoDB.")
        
        post_document = {
            'message_id': original_message_id,
            'date': original_date,
            'text': post_content
        }
        
        try:
            # שימוש ב-update_one עם upsert=True כדי למנוע כפילויות
            self.posts_collection.update_one(
                {'message_id': original_message_id},
                {'$setOnInsert': post_document},
                upsert=True
            )
            logger.info(f"Post {original_message_id} saved/updated successfully via forward.")
            await message.reply_text(f"✅ הפוסט נשמר/עודכן בהצלחה!")
            
        except Exception as e:
            logger.error(f"Error saving forwarded post to MongoDB: {e}", exc_info=True)
            await message.reply_text("❌ אירעה שגיאה בשמירת הפוסט.")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודת start"""
        welcome_message = """
🤖 ברוך הבא לבוט הסיכומים השבועיים!

הבוט יוצר סיכומים אוטומטיים של פוסטים מערוץ AndroidAndAI

פקודות זמינות:
📊 /generate_summary - יצירת סיכום ידני
👀 /preview - תצוגה מקדימה של הסיכום האחרון
📋 /show_schedule - הצגת סטטוס התזמון האוטומטי
❌ /cancel_schedule - ביטול התזמון האוטומטי

הבוט פועל אוטומטית כל שישי ב-16:00 (שעון ישראל)
        """
        await update.message.reply_text(welcome_message)
    
    async def get_channel_posts(self, days_back: int = 7) -> List[Dict]:
        """קריאת פוסטים מהימים האחרונים מ-MongoDB"""
        logger.info("--- Starting get_channel_posts (Reading from MongoDB) ---")
        try:
            since_date = datetime.now(pytz.UTC) - timedelta(days=days_back)
            logger.info(f"Searching for posts since (UTC): {since_date.strftime('%Y-%m-%d %H:%M:%S')}")

            # בניית שאילתה ל-MongoDB
            query = {'date': {'$gte': since_date}}
            
            # שליפת הפוסטים ומיון מהישן לחדש
            posts_cursor = self.posts_collection.find(query).sort('date', 1)
            
            relevant_posts = list(posts_cursor)
            
            logger.info(f"Found {len(relevant_posts)} posts from the last {days_back} days in MongoDB.")
            return relevant_posts
            
        except Exception as e:
            logger.error(f"FATAL ERROR in get_channel_posts: {e}", exc_info=True)
            return []
    
    async def create_summary_with_gpt4(self, posts: List[Dict]) -> str:
        """יצירת סיכום עם GPT-4 באמצעות התחביר החדש של OpenAI"""
        if not posts:
            return "לא נמצאו פוסטים רלוונטיים לסיכום."
        
        # הכנת הטקסט לסיכום
        posts_text = "\n\n".join([f"תאריך: {post['date'].strftime('%Y-%m-%d')}\nתוכן: {post['text']}" for post in posts])
        
        prompt = f"""
אתה כותב סיכום שבועי לערוץ טלגרם שמתמקד באנדרואיד ובינה מלאכותית.

הפוסטים של השבוע מצורפים בהמשך. 
המטרה שלך היא לכתוב סיכום בסגנון קליל, סוחף ומעניין – לא רשמי מדי, אבל גם לא יבש. תשתמש באימוג'ים, משפטים זורמים, פתיחה חמה וסיום שמזמין לעקוב גם לשבוע הבא.

חשוב:
- כל פסקה צריכה להתחיל באימוג'י + נושא
- תן לכל נושא תיאור ברור ומעניין
- אל תכניס כותרות כמו "סטטיסטיקות" או "תובנות"
- אל תשתמש ב-tldr
- אל תכתוב כמו רובוט
- כל פוסט צריך לקבל סיכום נפרד - אל תחבר 2 פוסטים יחד באותה פסקה
- אל תכתוב שורה כללית על "חידושים טכנולוגיים מרהיבים" או דומה - היכנס ישר לעניין

פורמט הסיכום:
אז מה היה לנו השבוע? 🔥

[כאן יבוא הסיכום - עם אימוג'י בתחילת כל פסקה]

מוזמנים לעקוב גם בשבוע הבא 🙌

להלן הפוסטים שיש לסכם:
{posts_text}
"""
        
        try:
            logger.info("Sending request to OpenAI API...")
            # שימוש בתחביר החדש
            response = self.openai_client.chat.completions.create(
                model="gpt-4-turbo",  # שימוש במודל המעודכן
                messages=[
                    {"role": "system", "content": "אתה מומחה לטכנולוגיה ו-AI שכותב סיכומים שבועיים בעברית לערוץ טלגרם."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=2000,  # הגדלת מגבלת התווים מ-1000 ל-2000
                temperature=0.7
            )
            
            summary = response.choices[0].message.content.strip()
            logger.info("Successfully received summary from OpenAI.")
            return summary
            
        except Exception as e:
            logger.error(f"Error creating summary with OpenAI: {e}", exc_info=True)
            # החזרת הודעת השגיאה המקורית כדי שנדע מה קרה
            return f"שגיאה ביצירת הסיכום: \n\n{e}"
    
    async def generate_summary_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודה ליצירת סיכום ידני"""
        if str(update.effective_user.id) != self.admin_chat_id:
            await update.message.reply_text("אין לך הרשאה להשתמש בפקודה זו")
            return
        
        await update.message.reply_text("יוצר סיכום... ⏳")
        
        # קריאת פוסטים ויצירת סיכום
        posts = await self.get_channel_posts()
        summary = await self.create_summary_with_gpt4(posts)
        
        # שמירת הסיכום למשתנה
        self.pending_summary = summary
        
        # יצירת כפתורים לתצוגה מקדימה
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👀 תצוגה מקדימה", callback_data="preview"),
                InlineKeyboardButton("📢 פרסם עכשיו", callback_data="publish")
            ],
            [InlineKeyboardButton("🔄 צור סיכום חדש", callback_data="regenerate")]
        ])
        
        await update.message.reply_text(
            f"הסיכום נוצר בהצלחה! ✅\nנמצאו {len(posts)} פוסטים מהשבוע האחרון.\n\nמה תרצה לעשות?",
            reply_markup=keyboard
        )
    
    async def preview_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """תצוגה מקדימה של הסיכום"""
        if not self.pending_summary:
            await update.message.reply_text("אין סיכום מוכן לתצוגה מקדימה")
            return
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📢 פרסם", callback_data="publish"),
                InlineKeyboardButton("🔄 צור חדש", callback_data="regenerate")
            ]
        ])
        
        await update.message.reply_text(
            f"תצוגה מקדימה:\n\n{self.pending_summary}",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """טיפול בלחיצות על כפתורים"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "preview":
            if self.pending_summary:
                await query.message.reply_text(
                    f"תצוגה מקדימה:\n\n{self.pending_summary}",
                    parse_mode=ParseMode.HTML
                )
            else:
                await query.message.reply_text("אין סיכום זמין")
        
        elif query.data == "publish":
            if self.pending_summary:
                success = await self.publish_summary()
                if success:
                    await query.message.reply_text("הסיכום פורסם בהצלחה! ✅")
                    self.pending_summary = None
                else:
                    await query.message.reply_text("שגיאה בפרסום הסיכום ❌")
            else:
                await query.message.reply_text("אין סיכום לפרסום")
        
        elif query.data == "regenerate":
            await query.message.reply_text("יוצר סיכום חדש... ⏳")
            posts = await self.get_channel_posts()
            self.pending_summary = await self.create_summary_with_gpt4(posts)
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("👀 תצוגה מקדימה", callback_data="preview"),
                    InlineKeyboardButton("📢 פרסם עכשיו", callback_data="publish")
                ]
            ])
            
            await query.message.reply_text(
                "סיכום חדש נוצר! ✅",
                reply_markup=keyboard
            )
    
    async def publish_summary(self) -> bool:
        """פרסום הסיכום לערוץ"""
        try:
            await self.application.bot.send_message(
                chat_id=f"@{self.channel_username}",
                text=self.pending_summary,
                parse_mode=ParseMode.HTML
            )
            logger.info("הסיכום פורסם בהצלחה")
            return True
        except Exception as e:
            logger.error(f"שגיאה בפרסום: {e}")
            return False
    
    async def scheduled_summary(self):
        """סיכום מתוזמן"""
        try:
            logger.info("מתחיל יצירת סיכום מתוזמן")
            
            # יצירת הסיכום
            posts = await self.get_channel_posts()
            summary = await self.create_summary_with_gpt4(posts)
            
            # שליחת הסיכום לאדמין לאישור
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📢 פרסם", callback_data="publish"),
                    InlineKeyboardButton("🔄 צור חדש", callback_data="regenerate")
                ]
            ])
            
            self.pending_summary = summary
            
            await self.application.bot.send_message(
                chat_id=self.admin_chat_id,
                text=f"סיכום שבועי אוטומטי מוכן! 📊\n\nתצוגה מקדימה:\n\n{summary}",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            
        except Exception as e:
            logger.error(f"שגיאה בסיכום המתוזמן: {e}")
            await self.application.bot.send_message(
                chat_id=self.admin_chat_id,
                text=f"שגיאה ביצירת הסיכום האוטומטי: {str(e)}"
            )
    
    async def cancel_schedule_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודה לביטול התזמון האוטומטי"""
        if str(update.effective_user.id) != self.admin_chat_id:
            await update.message.reply_text("אין לך הרשאה להשתמש בפקודה זו.")
            return
            
        schedule.clear('weekly-summary')
        logger.info("Weekly summary schedule has been cancelled by the admin.")
        await update.message.reply_text("✅ התזמון האוטומטי לסיכום השבועי בוטל.")

    async def show_schedule_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודה להצגת סטטוס התזמון"""
        if str(update.effective_user.id) != self.admin_chat_id:
            await update.message.reply_text("אין לך הרשאה להשתמש בפקודה זו.")
            return
            
        jobs = schedule.get_jobs('weekly-summary')
        if jobs:
            await update.message.reply_text(f"📊 קיים תזמון אוטומטי פעיל.\nפרטים: {jobs[0]}")
        else:
            await update.message.reply_text("❌ לא קיים תזמון אוטומטי פעיל.")
    
    def schedule_weekly_summary(self):
        """תזמון הסיכום השבועי לשעה 16:00 שעון ישראל"""
        # ניקוי תזמונים קודמים כדי למנוע כפילויות
        schedule.clear('weekly-summary')
        
        # יצירת תזמון חדש עם תג ואזור זמן
        schedule.every().friday.at("16:00", self.israel_tz).do(
            lambda: asyncio.run_coroutine_threadsafe(self.scheduled_summary(), self.application.loop)
        ).tag('weekly-summary')
        
        logger.info("Weekly summary scheduled for Friday at 16:00 (Israel Time).")
        
    def run_scheduler(self):
        """הרצת ה-scheduler ברקע"""
        # שמירת לולאת האירועים של ה-thread הראשי
        self.application.loop = asyncio.get_event_loop()
        while True:
            schedule.run_pending()
            time.sleep(1) # בדיקה כל שנייה
    
    async def run(self):
        """הרצת הבוט"""
        try:
            # הגדרת התזמון השבועי
            self.schedule_weekly_summary()
            
            # התחלת תזמון ברקע
            scheduler_thread = Thread(target=self.run_scheduler, daemon=True)
            scheduler_thread.start()
            
            logger.info("הבוט מתחיל...")
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling()
            
            # שמירה על הבוט פעיל
            while True:
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"שגיאה בהרצת הבוט: {e}")
        finally:
            await self.application.stop()

def start_bot_logic():
    # נקודת כניסה
    async def main():
        bot = TelegramSummaryBot()
        await bot.run()
    
    asyncio.run(main())

# =================================================================
# הפעלת הבוט בתהליך רקע ברמה הגלובלית של המודול
# כך ש-Gunicorn יפעיל אותו בעת הייבוא.
# =================================================================
logging.info("Creating bot thread to run in the background...")
bot_thread = threading.Thread(target=start_bot_logic)
bot_thread.daemon = True
bot_thread.start()

logging.info("Background bot thread started. The main thread will now be managed by Gunicorn.")
