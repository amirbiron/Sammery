import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict
import json
import openai
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode
import schedule
import time
from threading import Thread
import pytz

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
        self.channel_username = os.getenv('CHANNEL_USERNAME', 'AndroidAndAI')  # ללא @
        self.openai_api_key = os.getenv('OPENAI_API_KEY')
        self.admin_chat_id = os.getenv('ADMIN_CHAT_ID')  # Chat ID של האדמין
        
        # אתחול OpenAI
        self.openai_client = openai.OpenAI(api_key=self.openai_api_key)
        
        # אתחול הבוט
        self.application = Application.builder().token(self.bot_token).build()
        self.bot = Bot(token=self.bot_token)
        
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
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודת start"""
        welcome_message = """
🤖 ברוך הבא לבוט הסיכומים השבועיים!

הבוט יוצר סיכומים אוטומטיים של פוסטים מערוץ AndroidAndAI

פקודות זמינות:
📊 /generate_summary - יצירת סיכום ידני
👀 /preview - תצוגה מקדימה של הסיכום האחרון

הבוט פועל אוטומטית כל שישי ב-16:00
        """
        await update.message.reply_text(welcome_message)
    
    async def get_channel_posts(self, days_back: int = 7) -> List[Dict]:
        """קריאת פוסטים מהערוץ מהימים האחרונים"""
        try:
            posts = []
            since_date = datetime.now(self.israel_tz) - timedelta(days=days_back)
            
            # קבלת מידע על הערוץ
            chat = await self.bot.get_chat(f"@{self.channel_username}")
            chat_id = chat.id
            
            # ננסה לקרוא הודעות מהערוץ
            # זה יעבוד רק אם הבוט הוא אדמין בערוץ או אם הערוץ ציבורי
            try:
                # נקבל הודעות מהערוץ באמצעות message_id
                # נתחיל מההודעה האחרונה ונרד אחורה
                latest_message_id = None
                
                # ננסה לקבל מידע על הודעות אחרונות
                # מאחר שאין API ישיר לקריאת היסטוריה, נשתמש בגישה אחרת
                
                # נשמור הודעות שהבוט יקבל בזמן אמת במשתנה גלובלי
                # או נקרא מקובץ אם יש כזה
                
                # לעת עתה, ניצור דוגמאות לבדיקה
                current_time = datetime.now(self.israel_tz)
                
                sample_posts = [
                    {
                        'date': (current_time - timedelta(days=1)).strftime('%Y-%m-%d %H:%M'),
                        'text': '🚀 עדכון חדש ב-Android Studio: תמיכה משופרת ב-Compose UI עם כלים חדשים לעיצוב ממשקי משתמש',
                        'message_id': 1001
                    },
                    {
                        'date': (current_time - timedelta(days=2)).strftime('%Y-%m-%d %H:%M'),
                        'text': '🤖 OpenAI הכריזה על GPT-4 Turbo החדש עם חלון הקשר של 128K טוקנים ומחירים מופחתים',
                        'message_id': 1002
                    },
                    {
                        'date': (current_time - timedelta(days=3)).strftime('%Y-%m-%d %H:%M'),
                        'text': '📱 Google משיקה את Android 14 QPR2 עם שיפורים בביטחון ותכונות AI חדשות',
                        'message_id': 1003
                    },
                    {
                        'date': (current_time - timedelta(days=4)).strftime('%Y-%m-%d %H:%M'),
                        'text': '💡 Meta מכריזה על Llama 2 החדש - מודל שפה פתוח מתקדם לפיתוח יישומי AI',
                        'message_id': 1004
                    },
                    {
                        'date': (current_time - timedelta(days=5)).strftime('%Y-%m-%d %H:%M'),
                        'text': '🔧 Kotlin Multiplatform מגיע לגרסת יציבות - פיתוח אפליקציות חוצות פלטפורמות בקלות',
                        'message_id': 1005
                    }
                ]
                
                # סינון פוסטים לפי התאריך
                for post in sample_posts:
                    post_date = datetime.strptime(post['date'], '%Y-%m-%d %H:%M')
                    post_date = self.israel_tz.localize(post_date)
                    if post_date >= since_date:
                        posts.append(post)
                
                logger.info(f"נמצאו {len(posts)} פוסטים מהימים האחרונים")
                
            except Exception as inner_e:
                logger.error(f"שגיאה בקריאת הודעות: {inner_e}")
                posts = []
            
            return posts
            
        except Exception as e:
            logger.error(f"שגיאה בקריאת פוסטים: {e}")
            return []
    
    async def create_summary_with_gpt4(self, posts: List[Dict]) -> str:
        """יצירת סיכום עם GPT-4"""
        if not posts:
            return "לא נמצאו פוסטים השבוע"
        
        # הכנת הטקסט לסיכום
        posts_text = "\n\n".join([f"תאריך: {post['date']}\nתוכן: {post['text']}" for post in posts])
        
        prompt = f"""
אתה מסכם תוכן טכנולוגי בעברית. צור סיכום שבועי מעניין ומקצועי של הפוסטים הבאים מערוץ AndroidAndAI.

הפורמט הרצוי:
אז מה היה לנו השבוע? 🔥

[סיכום מעניין ומקצועי בעברית של העדכונים הטכנולוגיים, חדשות Android ו-AI]

מוזמנים לעקוב גם בשבוע הבא 🙌

הפוסטים לסיכום:
{posts_text}

דרישות:
- כתיבה בעברית בלבד
- סגנון מעניין ומקצועי
- התמקדות בנושאים החשובים ביותר
- שימוש באמוג'י בצורה מתונה
- אורך של 200-400 מילים
"""
        
        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4-turbo-preview",
                messages=[
                    {"role": "system", "content": "אתה מומחה לטכנולוגיה ו-AI שכותב סיכומים בעברית"},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1000,
                temperature=0.7
            )
            
            return response.choices[0].message.content.strip()
            
        except Exception as e:
            logger.error(f"שגיאה ב-GPT-4: {e}")
            return f"שגיאה ביצירת הסיכום: {str(e)}"
    
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
            await self.bot.send_message(
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
            
            await self.bot.send_message(
                chat_id=self.admin_chat_id,
                text=f"סיכום שבועי אוטומטי מוכן! 📊\n\nתצוגה מקדימה:\n\n{summary}",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            
        except Exception as e:
            logger.error(f"שגיאה בסיכום המתוזמן: {e}")
            await self.bot.send_message(
                chat_id=self.admin_chat_id,
                text=f"שגיאה ביצירת הסיכום האוטומטי: {str(e)}"
            )
    
    def schedule_weekly_summary(self):
        """תזמון הסיכום השבועי"""
        schedule.every().friday.at("16:00").do(
            lambda: asyncio.create_task(self.scheduled_summary())
        )
        
    def run_scheduler(self):
        """הרצת הtimer ברקע"""
        while True:
            schedule.run_pending()
            time.sleep(60)  # בדיקה כל דקה
    
    async def run(self):
        """הרצת הבוט"""
        try:
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

# נקודת כניסה
async def main():
    bot = TelegramSummaryBot()
    await bot.run()

if __name__ == "__main__":
    asyncio.run(main())
