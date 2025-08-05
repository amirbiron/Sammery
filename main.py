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
from activity_reporter import create_reporter

# הגדרה חד-פעמית של ה-Reporter
reporter = create_reporter(
    mongodb_uri="mongodb+srv://mumin:M43M2TFgLfGvhBwY@muminai.tm6x81b.mongodb.net/?retryWrites=true&w=majority&appName=muminAI",
    service_id="srv-d26079be5dus73ctnegg",
    service_name="Sammery"
)

# ===============================================
# עדכון: תמיכה בשליחת תמונות באמצעות file_id
# ===============================================
# 1. נוספה פונקציה get_file_id שמחזירה file_id של תמונות וקבצים
# 2. פונקציית publish_summary עודכנה לשלוח תמונה באמצעות משתנה הסביבה SUMMARY_IMAGE_FILE_ID
# 3. כל התצוגות המקדימות כוללות כעת תמונה אם זמינה
# 4. הוספת handler ל-PHOTO ו-Document.ALL בפונקציית _setup_handlers

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
    level=logging.INFO  # הרמה הכללית נשארת INFO כדי לראות את הלוגים שלנו
)

# --- השתקת ספריות חיצוניות רועשות ---
# מעלים את רמת הלוג עבור הספריות הספציפיות האלה ל-WARNING.
# כך, נראה מהן רק אזהרות ושגיאות, ולא את הודעות ה-INFO של ה-polling.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

class TelegramSummaryBot:
    def __init__(self):
        # משתני סביבה
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.channel_username = os.getenv('CHANNEL_USERNAME', 'AndroidAndAI')
        self.openai_api_key = os.getenv('OPENAI_API_KEY')
        self.admin_chat_id = os.getenv('ADMIN_CHAT_ID')
        self.admin_id = self.admin_chat_id  # הוספת משתנה נוסף עבור error_handler
        
        if not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set!")
        # אתחול לקוח OpenAI החדש
        self.openai_client = openai.OpenAI(api_key=self.openai_api_key)
        
        # אתחול הבוט
        self.application = Application.builder().token(self.bot_token).build()
        self.loop = asyncio.get_event_loop()
        
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
        self.auto_publish_enabled = False  # הוספת משתנה למצב פרסום אוטומטי (כבוי כברירת מחדל)
        
        # הוספת handlers
        self._setup_handlers()
    
    def _setup_handlers(self):
        """הגדרת handlers לבוט"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("generate_summary", self.generate_summary_command))
        self.application.add_handler(CommandHandler("preview", self.preview_command))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))

        # --- פקודות ניהול חדשות ---
        self.application.add_handler(CommandHandler("schedule_summary", self.schedule_summary_command))
        self.application.add_handler(CommandHandler("show_schedule", self.show_schedule_command))
        self.application.add_handler(CommandHandler("stats", self.show_stats))
        # שים לב: הפקודה cancel_schedule_command הוסרה כי היא מטופלת עכשיו בכפתור.

        # --- הוספת handler למפסק האוטומטי ---
        self.application.add_handler(CommandHandler("toggle_autopublish", self.toggle_autopublish_command))

        # --- Handlers לקליטת פוסטים ---
        self.application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, self.handle_new_channel_post))
        self.application.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, self.handle_forwarded_post))
        
        # --- Handler לקבלת file_id של תמונות וקבצים ---
        self.application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.get_file_id))
    
    async def get_file_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מדפיס את ה-file_id של כל תמונה או קובץ שנשלח לבוט."""
        reporter.report_activity(update.effective_user.id)
        if update.message.photo:
            # לוג עבור תמונה
            file_id = update.message.photo[-1].file_id  # לוקחים את הגרסה הגדולה ביותר
            logger.info(f"Received Photo. file_id: {file_id}")
            # נשתמש ב-HTML שהוא הרבה יותר נוח ובטוח למקרה הזה
            response_text = f"קיבלתי תמונה.\nה-file_id שלה הוא:\n<code>{file_id}</code>"
            await update.message.reply_text(response_text, parse_mode=ParseMode.HTML)
        elif update.message.document:
            # לוג עבור קובץ כללי
            file_id = update.message.document.file_id
            logger.info(f"Received Document. file_id: {file_id}")
            # נשתמש ב-HTML שהוא הרבה יותר נוח ובטוח למקרה הזה
            response_text = f"קיבלתי קובץ.\nה-file_id שלו הוא:\n<code>{file_id}</code>"
            await update.message.reply_text(response_text, parse_mode=ParseMode.HTML)
    
    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """רושם שגיאות ושולח הודעת טלגרם לאדמין כאשר מתרחשת שגיאה."""
        logger.error("Exception while handling an update:", exc_info=context.error)

        # הרכבת הודעת השגיאה לאדמין
        error_message = (
            f"🚨 התרחשה שגיאה בבוט 🚨\n\n"
            f"סוג השגיאה: {type(context.error).__name__}\n"
            f"הודעת השגיאה: {context.error}\n"
        )

        try:
            # שליחת ההודעה לאדמין
            if self.admin_id:
                await self.application.bot.send_message(
                    chat_id=self.admin_id,
                    text=error_message
                )
                logger.info(f"Error notification sent to admin ({self.admin_id}).")
            else:
                logger.warning("Admin ID not set, cannot send error notification.")
        except Exception as e:
            logger.error(f"Failed to send error notification to admin: {e}")
    
    async def handle_new_channel_post(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """תופס פוסטים חדשים מהערוץ ושומר אותם ל-MongoDB"""
        message = update.channel_post
        # בדוק אם יש תוכן טקסטואלי. אם לא, אין מה לשמור.
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
            logger.info(f"Post {message.message_id} saved successfully.")
        except Exception as e:
            logger.error(f"Error saving new post to MongoDB: {e}", exc_info=True)
    
    async def handle_forwarded_post(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        תופס הודעות המועברות לבוט, בודק אם הן מהערוץ הנכון, ושומר אותן ב-MongoDB.
        זה מאפשר "מילוי לאחור" (backfill) ידני של פוסטים ישנים.
        """
        reporter.report_activity(update.effective_user.id)
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
        reporter.report_activity(update.effective_user.id)
        welcome_message = """
🤖 ברוך הבא לבוט הסיכומים השבועיים!

הבוט יוצר סיכומים אוטומטיים של פוסטים מערוץ AndroidAndAI.

<b>פקודות זמינות:</b>
📊 /generate_summary - יצירת סיכום ידני מיידי.
👀 /preview - תצוגה מקדימה של הסיכום האחרון שנוצר.
⏰ /schedule_summary - הגדרת שעת שליחה אוטומטית ביום שישי.
📋 /show_schedule - הצגת סטטוס התזמון האוטומטי.
📈 /stats - הצגת סטטיסטיקות על כמות הפוסטים השמורים.

<b>פקודות מתקדמות:</b>
⚙️ /toggle_autopublish - הפעלה/כיבוי של מצב פרסום אוטומטי. במצב זה, הסיכום המתוזמן יפורסם ישירות לערוץ ללא צורך באישור ידני (שימושי כשאתה לא זמין).
"""
        await update.message.reply_text(welcome_message, parse_mode=ParseMode.HTML)
    
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
                max_tokens=4000,  # הגדלת מגבלת התווים ל-4000 עבור סיכומים מפורטים יותר
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
        reporter.report_activity(update.effective_user.id)
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
        reporter.report_activity(update.effective_user.id)
        if not self.pending_summary:
            await update.message.reply_text("אין סיכום מוכן לתצוגה מקדימה")
            return
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📢 פרסם", callback_data="publish"),
                InlineKeyboardButton("🔄 צור חדש", callback_data="regenerate")
            ]
        ])
        
        # בדיקה אם יש תמונה לשליחה יחד עם התצוגה המקדימה
        image_file_id = os.getenv("SUMMARY_IMAGE_FILE_ID")
        if image_file_id:
            try:
                await update.message.reply_photo(
                    photo=image_file_id,
                    caption=f"תצוגה מקדימה:\n\n{self.pending_summary}",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
            except Exception as img_error:
                logger.warning(f"Failed to send image with preview: {img_error}")
                # אם נכשלה שליחת התמונה, נשלח רק טקסט
                await update.message.reply_text(
                    f"תצוגה מקדימה:\n\n{self.pending_summary}",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
        else:
            await update.message.reply_text(
                f"תצוגה מקדימה:\n\n{self.pending_summary}",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """טיפול בלחיצות על כל הכפתורים"""
        reporter.report_activity(update.effective_user.id)
        query = update.callback_query
        await query.answer()
        
        data = query.data

        if data.startswith("schedule_set:"):
            hour = data.split(":")[1]
            time_str = f"{hour}:00"
            self.set_weekly_schedule(time_str)
            await query.edit_message_text(f"✅ הסיכום תזומן בהצלחה ליום שישי בשעה {time_str} (שעון ישראל).")
            return

        if data == "schedule_cancel_existing":
            schedule.clear('weekly-summary')
            logger.info("Weekly summary schedule has been cancelled by the admin via button.")
            await query.edit_message_text("✅ התזמון האוטומטי בוטל.")
            return

        # --- לוגיקה קיימת לסיכומים ---
        if query.data == "preview":
            if self.pending_summary:
                # בדיקה אם יש תמונה לשליחה יחד עם התצוגה המקדימה
                image_file_id = os.getenv("SUMMARY_IMAGE_FILE_ID")
                if image_file_id:
                    try:
                        await query.message.reply_photo(
                            photo=image_file_id,
                            caption=f"תצוגה מקדימה:\n\n{self.pending_summary}",
                            parse_mode=ParseMode.HTML
                        )
                    except Exception as img_error:
                        logger.warning(f"Failed to send image with preview: {img_error}")
                        # אם נכשלה שליחת התמונה, נשלח רק טקסט
                        await query.message.reply_text(
                            f"תצוגה מקדימה:\n\n{self.pending_summary}",
                            parse_mode=ParseMode.HTML
                        )
                else:
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
        """
        פרסום הסיכום לערוץ.
        שולח תמונת כותרת והודעת טקסט בנפרד, מנקה את המאגר,
        ומתזמן מחדש את הריצה הבאה כדי למנוע כפילויות.
        """
        if not self.pending_summary:
            logger.warning("publish_summary called but there is no pending summary.")
            await self.application.bot.send_message(
                chat_id=self.admin_chat_id,
                text="ניסית לפרסם, אבל לא היה סיכום בהמתנה."
            )
            return False

        try:
            # שלב 1: שליחת תמונת כותרת (הקוד הקיים שלך)
            image_file_id = os.getenv("SUMMARY_IMAGE_FILE_ID")
            if image_file_id:
                logger.info("Found SUMMARY_IMAGE_FILE_ID. Sending header image...")
                await self.application.bot.send_photo(
                    chat_id=f"@{self.channel_username}",
                    photo=image_file_id
                )

            # שלב 2: שליחת טקסט הסיכום (הקוד הקיים שלך)
            logger.info("Sending summary text to the channel...")
            await self.application.bot.send_message(
                chat_id=f"@{self.channel_username}",
                text=self.pending_summary,
                parse_mode=ParseMode.HTML
            )

            # שלב 3: ניקוי הפוסטים מהמאגר (הקוד הקיים שלך)
            logger.info("Summary published successfully. Clearing posts from the database...")
            delete_result = self.posts_collection.delete_many({})
            logger.info(f"Cleared {delete_result.deleted_count} posts from the collection.")

            # --- תוספת קריטית: איפוס ותזמון מחדש ---
            jobs = schedule.get_jobs('weekly-summary')
            if jobs:
                run_time_str = jobs[0].at_time.strftime('%H:%M')
                schedule.clear('weekly-summary')
                schedule.every().friday.at(run_time_str, self.israel_tz).do(
                    lambda: asyncio.run_coroutine_threadsafe(self.scheduled_summary(), self.application.loop)
                ).tag('weekly-summary')
                logger.info(f"Successfully published. Rescheduled next run for next Friday at {run_time_str}.")

            return True

        except Exception as e:
            logger.error(f"Failed to publish summary: {e}", exc_info=True)
            await self.application.bot.send_message(
                chat_id=self.admin_chat_id,
                text=f"❌ נכשלתי בפרסום הסיכום לערוץ.\n<b>שגיאה:</b>\n<pre>{e}</pre>",
                parse_mode=ParseMode.HTML
            )
            return False
    
    async def scheduled_summary(self):
        """
        יוצר סיכום מתוזמן.
        פועל במצב אוטומטי או ידני בהתאם למפסק auto_publish_enabled.
        """
        logger.info("--- Scheduled summary job started ---")
        logger.info(f"Current auto-publish mode: {'ON' if self.auto_publish_enabled else 'OFF'}")

        try:
            posts = await self.get_channel_posts()
            if not posts:
                logger.info("No new posts found for scheduled summary. Aborting.")
                await self.application.bot.send_message(
                    chat_id=self.admin_chat_id,
                    text="🤖 בוצע ניסיון סיכום אוטומטי, אך לא נמצאו פוסטים חדשים."
                )
                return

            summary = await self.create_summary_with_gpt4(posts)
            self.pending_summary = summary

            # --- לוגיקת המפסק ---
            if self.auto_publish_enabled:
                # מצב אוטומטי: פרסם ישירות
                logger.info("Auto-publish is ON. Proceeding with direct publishing.")
                success = await self.publish_summary()
                if success:
                    await self.application.bot.send_message(
                        chat_id=self.admin_chat_id,
                        text="✅ הסיכום השבועי פורסם אוטומטית בהצלחה!"
                    )
                    # כבה את המצב האוטומטי חזרה לברירת המחדל הבטוחה
                    self.auto_publish_enabled = False
                    logger.info("Auto-publish mode has been reset to OFF after successful run.")
            else:
                # מצב ידני: שלח לאישור האדמין (ההתנהגות המקורית)
                logger.info("Auto-publish is OFF. Sending for manual approval.")
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📢 פרסם", callback_data="publish")],
                    [InlineKeyboardButton("🔄 צור חדש", callback_data="regenerate")]
                ])
                
                # בדיקה אם יש תמונה לשליחה יחד עם הודעת התצוגה המקדימה
                image_file_id = os.getenv("SUMMARY_IMAGE_FILE_ID")
                if image_file_id:
                    try:
                        await self.application.bot.send_photo(
                            chat_id=self.admin_chat_id,
                            photo=image_file_id,
                            caption=f"סיכום שבועי אוטומטי מוכן! 📊\n\nתצוגה מקדימה:\n\n{summary}",
                            reply_markup=keyboard,
                            parse_mode=ParseMode.HTML
                        )
                    except Exception as img_error:
                        logger.warning(f"Failed to send image with scheduled summary preview: {img_error}")
                        # אם נכשלה שליחת התמונה, נשלח רק טקסט
                        await self.application.bot.send_message(
                            chat_id=self.admin_chat_id,
                            text=f"סיכום שבועי אוטומטי מוכן! 📊\n\nתצוגה מקדימה:\n\n{summary}",
                            reply_markup=keyboard,
                            parse_mode=ParseMode.HTML
                        )
                else:
                    await self.application.bot.send_message(
                        chat_id=self.admin_chat_id,
                        text=f"סיכום שבועי אוטומטי מוכן! 📊\n\nתצוגה מקדימה:\n\n{summary}",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML
                    )

        except Exception as e:
            logger.error(f"A critical error occurred in the scheduled_summary job: {e}", exc_info=True)
            await self.application.bot.send_message(
                chat_id=self.admin_chat_id,
                text=f"שגיאה קריטית בתהליך הסיכום האוטומטי: {str(e)}"
            )
        finally:
            # אל תנקה את pending_summary כאן, כי במצב ידני הוא נחוץ ללחיצת הכפתור
            if self.auto_publish_enabled: # נקה רק אם היינו במצב אוטומטי
                self.pending_summary = None
    
    async def schedule_summary_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מציג לאדמין כפתורים לבחירת שעת התזמון."""
        reporter.report_activity(update.effective_user.id)
        if str(update.effective_user.id) != self.admin_chat_id:
            await update.message.reply_text("אין לך הרשאה להשתמש בפקודה זו.")
            return

        keyboard = [
            [
                InlineKeyboardButton("14:00", callback_data="schedule_set:14"),
                InlineKeyboardButton("15:00", callback_data="schedule_set:15"),
                InlineKeyboardButton("16:00", callback_data="schedule_set:16"),
            ],
            [
                InlineKeyboardButton("17:00", callback_data="schedule_set:17"),
                InlineKeyboardButton("18:00", callback_data="schedule_set:18"),
                InlineKeyboardButton("19:00", callback_data="schedule_set:19"),
            ],
            [InlineKeyboardButton("❌ בטל תזמון קיים", callback_data="schedule_cancel_existing")],
        ]
        
        await update.message.reply_text(
            "אנא בחר שעת שליחה לסיכום האוטומטי ביום שישי:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def show_schedule_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודה להצגת סטטוס התזמון בצורה ידידותית ומאובטחת."""
        reporter.report_activity(update.effective_user.id)
        if str(update.effective_user.id) != self.admin_chat_id:
            await update.message.reply_text("אין לך הרשאה להשתמש בפקודה זו.")
            return
            
        jobs = schedule.get_jobs('weekly-summary')
        if jobs:
            job = jobs[0]
            
            # בניית הודעה ברורה יותר
            time_info = job.at_time if job.at_time else "לא צוינה שעה"
            day_info = "יום שישי"  # אנחנו יודעים מהלוגיקה שזה תמיד יום שישי
            
            friendly_text = f"📊 <b>קיים תזמון אוטומטי פעיל</b>\n\n"
            friendly_text += f"🔹 <b>תדירות:</b> כל שבוע\n"
            friendly_text += f"🔹 <b>יום:</b> {day_info}\n"
            friendly_text += f"🔹 <b>שעה (שעון ישראל):</b> {time_info}\n\n"
            
            # חישוב זמן מאובטח מפני שגיאות timezone
            try:
                now_israel = datetime.now(self.israel_tz)
                next_run_time = job.next_run
                
                # אם התזמון הוא "נאיבי" (ללא אזור זמן), נתייחס אליו כ-UTC ונמיר
                if next_run_time.tzinfo is None:
                    next_run_time = pytz.utc.localize(next_run_time).astimezone(self.israel_tz)

                time_until = next_run_time - now_israel
                
                # הסרת מיקרו-שניות מהתצוגה לפלט נקי
                time_until_str = str(timedelta(seconds=int(time_until.total_seconds())))
                
                friendly_text += f"⏳ <b>הרצה הבאה בעוד:</b> {time_until_str}"
            except Exception as e:
                logger.error(f"Could not calculate next run time in show_schedule: {e}")
                friendly_text += "לא ניתן היה לחשב את זמן הריצה הבאה."

            await update.message.reply_text(friendly_text, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("❌ לא קיים תזמון אוטומטי פעיל.")

    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """שולח לאדמין סטטיסטיקות על הבוט, כמו מספר הפוסטים השמורים."""
        reporter.report_activity(update.effective_user.id)
        logger.info("Stats command received by user %s.", update.effective_user.id)
        
        # בדיקה שהפקודה מופעלת רק על ידי האדמין
        if str(update.effective_user.id) != self.admin_chat_id:
            logger.warning("Unauthorized user tried to use /stats.")
            return

        try:
            # ביצוע שאילתת ספירה של כל המסמכים בקולקציה
            post_count = self.posts_collection.count_documents({})
            
            # הרכבת הודעת התשובה
            response_text = (
                f"📊 <b>סטטיסטיקות הבוט</b> 📊\n\n"
                f"נכון לעכשיו, שמורים במאגר הנתונים <b>{post_count}</b> פוסטים."
            )
            
            await update.message.reply_text(response_text, parse_mode=ParseMode.HTML)
            
        except Exception as e:
            logger.error(f"Failed to retrieve stats from database: {e}", exc_info=True)
            await update.message.reply_text("שגיאה בקבלת הסטטיסטיקות ממאגר הנתונים.")

    async def toggle_autopublish_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודה להפעלה/כיבוי של מצב פרסום אוטומטי."""
        reporter.report_activity(update.effective_user.id)
        if str(update.effective_user.id) != self.admin_chat_id:
            return

        # הופכים את המצב הקיים
        self.auto_publish_enabled = not self.auto_publish_enabled

        if self.auto_publish_enabled:
            status_text = "🟢 מופעל"
            message = (
                f"<b>מצב פרסום אוטומטי: {status_text}</b>\n\n"
                "הסיכום המתוזמן הבא יפורסם ישירות לערוץ ללא אישור ידני.\n"
                "המצב יתכבה אוטומטית לאחר הפרסום.\n\n"
                "כדי לבטל, פשוט שלח את הפקודה /toggle_autopublish שוב."
            )
        else:
            status_text = "🔴 כבוי"
            message = (
                f"<b>מצב פרסום אוטומטי: {status_text}</b>\n\n"
                "הסיכומים ימשיכו להגיע אליך לאישור ידני כרגיל."
            )

        logger.info(f"Auto-publish mode toggled by admin. New status: {status_text}")
        await update.message.reply_text(message, parse_mode=ParseMode.HTML)
    
    def set_weekly_schedule(self, time_str: str):
        """קובע תזמון שבועי לשעה ספציפית ומבטל תזמונים קודמים."""
        # ניקוי תזמונים קודמים עם אותו תג
        schedule.clear('weekly-summary')
        
        # קביעת התזמון החדש עם תג, שעה ואזור זמן
        schedule.every().friday.at(time_str, self.israel_tz).do(
            self.run_async_job, 
            self.scheduled_summary
        ).tag('weekly-summary')
        
        logger.info(f"Weekly summary has been set for Friday at {time_str} (Israel Time).")
    
    def run_async_job(self, async_func):
        """
        מריץ פונקציה אסינכרונית מה-thread של schedule
        באמצעות ה-event loop הראשי של הבוט.
        """
        logger.info(f"Scheduler is triggering async job: {async_func.__name__}")
        # זה הקוד הקריטי: הוא שולח את המשימה לביצוע בלולאה הנכונה
        asyncio.run_coroutine_threadsafe(async_func(), self.loop)
        
    def run_scheduler(self):
        """מריץ את לולאת התזמונים ב-thread נפרד."""
        logger.info("Scheduler thread started.")
        
        # הגדרת המשימה המתוזמנת
        # שים לב שאנחנו קוראים לפונקציית העזר החדשה
        schedule.every().friday.at("16:00", "Asia/Jerusalem").do(
            self.run_async_job, 
            self.scheduled_summary
        )

        while True:
            schedule.run_pending()
            time.sleep(1)

    def run_background_tasks(self):
        """
        מריץ את כל משימות הרקע ב-thread נפרד, עם לוגים מפורטים.
        """
        logger.info("Background tasks thread has started.")
        
        try:
            # --- הגדרת שרת ה-Flask ---
            flask_app = Flask('')
            
            @flask_app.route('/')
            def home():
                # לוג קצר כדי לראות ש-Render פונה לשרת שלנו
                logger.debug("Keep-alive endpoint was pinged.")
                return "I'm alive and checking schedules!"
                
            # הרצת שרת ה-Flask ב-thread נפרד כדי שלא יחסום את לולאת התזמונים
            flask_thread = Thread(target=lambda: flask_app.run(host='0.0.0.0', port=8080), name="FlaskThread")
            flask_thread.daemon = True
            flask_thread.start()
            
            logger.info("Keep-alive server (Flask) started in a sub-thread.")

            # --- לולאת התזמונים הראשית ---
            logger.info("Scheduler loop is starting now.")
            
            # נוסיף לוג "פעימת לב" כל 10 דקות כדי לוודא שהלולאה לא נתקעה
            heartbeat_interval = 600  # 10 דקות
            last_heartbeat = time.time()
            
            while True:
                schedule.run_pending()
                
                # בדיקת פעימת לב
                if time.time() - last_heartbeat > heartbeat_interval:
                    logger.info("Heartbeat: Background thread is still running and healthy.")
                    last_heartbeat = time.time()
                    
                time.sleep(1) # חשוב לא להעמיס על המעבד

        except Exception as e:
            logger.critical(f"A critical error occurred in the background tasks thread: {e}", exc_info=True)
        finally:
            # הלוג הכי חשוב: אם אי פעם נגיע לכאן, נדע שה-thread עומד להסתיים
            logger.warning("Background tasks thread is shutting down.")
    
    async def run(self):
        """הרצת הבוט"""
        try:
            # הוספת מנהל השגיאות הגלובלי
            self.application.add_error_handler(self.error_handler)
            
            # התחלת תזמון ברקע
            scheduler_thread = Thread(target=self.run_scheduler, name="SchedulerThread")
            scheduler_thread.daemon = True
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
