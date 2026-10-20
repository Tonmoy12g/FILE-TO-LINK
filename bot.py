"""Telegram File-to-Link Bot.

Flow: user sends any file in a private chat -> the bot downloads it to a
temp file on disk (streamed, never fully in memory) -> uploads it through
uploader.py (default provider: GoFile, live-verified) -> verifies the link
-> replies with file name, size and the download link.

Run:  python bot.py
Required env: TELEGRAM_BOT_TOKEN
Optional env: ADMIN_CHAT_ID, SUPPORT_USERNAME, CATBOX_USERHASH,
              UPLOAD_PROVIDER (gofile|catbox|litterbox), MAX_FILE_MB (20),
              PORT (Flask keep-alive, set automatically on Render)
"""

import asyncio
import logging
import os
import tempfile
from threading import Thread

from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import uploader

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("file-to-link-bot")

# ------------------------------------------------------------------ config

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
_admin_raw = os.environ.get("ADMIN_CHAT_ID", "").strip()
ADMIN_CHAT_ID = (
    int(_admin_raw) if _admin_raw.lstrip("-").isdigit() else None
)
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "").strip().lstrip("@")
try:
    MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "20"))
except ValueError:
    MAX_FILE_MB = 20
# The Telegram cloud Bot API cannot download files larger than 20 MB at all,
# so the effective cap is never above 20 MB on this setup.
MAX_FILE_BYTES = min(MAX_FILE_MB, 20) * 1024 * 1024

# ------------------------------------------------------------------ texts (Bangla)

DEFAULT_WELCOME = (
    "👋 স্বাগতম File-to-Link Bot-এ!\n\n"
    "আমাকে যেকোনো ফাইল পাঠালেই আমি সেটির একটি ডাউনলোড লিংক বানিয়ে দেবো।\n\n"
    "📤 ফাইল পাঠাতে শুধু এই চ্যাটে ফাইলটি পাঠিয়ে দিন।\n"
    f"📏 সর্বোচ্চ ফাইল সাইজ: {MAX_FILE_MB} MB (Telegram Bot API-এর সীমা)\n\n"
    "নিচের বাটনগুলো ব্যবহার করুন 👇"
)

HELP_TEXT = (
    "📚 সাহায্য ও নির্দেশনা\n\n"
    "১. যেকোনো ফাইল (ছবি, ভিডিও, গান, PDF, ZIP, APK ইত্যাদি) এই চ্যাটে পাঠান।\n"
    "২. কয়েক সেকেন্ড অপেক্ষা করুন — বট ফাইলটি আপলোড করে দেবে।\n"
    "৩. বট আপনাকে ফাইলের নাম, সাইজ ও ডাউনলোড লিংক দেবে। লিংকটি যেকোনো জায়গায় শেয়ার করতে পারবেন।\n\n"
    f"📏 সর্বোচ্চ সাইজ: একবারে {MAX_FILE_MB} MB পর্যন্ত। এর বড় ফাইল Telegram Bot API ডাউনলোডই করতে পারে না।\n"
    "♻️ একই ফাইল আবার পাঠালে বট আগের লিংকটিই ফেরত দেবে, নতুন করে আপলোড করবে না।\n"
    "ℹ️ GoFile লিংক হলো ডাউনলোড পেজের লিংক — পেজ খুলে Download বাটনে চাপ দিলেই ফাইল নামবে।"
)

MAINTENANCE_TEXT = (
    "🔧 বটটি এখন রক্ষণাবেক্ষণের (maintenance) কাজে সাময়িকভাবে বন্ধ আছে।\n"
    "কিছুক্ষণ পরে আবার চেষ্টা করুন। দুঃখিত! 🙏"
)

ERR_TOO_BIG = (
    "❌ ফাইলটি খুব বড়!\n\n"
    f"এই বট বর্তমানে সর্বোচ্চ {MAX_FILE_MB} MB পর্যন্ত ফাইল নিতে পারে। "
    "এটি Telegram Bot API-এর সীমা — এর বড় ফাইল বট ডাউনলোডই করতে পারে না।\n\n"
    "💡 বড় ফাইলের জন্য ফাইলটি ভাগ করে পাঠান, অথবা সরাসরি gofile.io ওয়েবসাইটে আপলোড করুন।"
)
ERR_DOWNLOAD = "❌ দুঃখিত, ফাইলটি Telegram থেকে ডাউনলোড করা যায়নি। আবার পাঠিয়ে চেষ্টা করুন।"
ERR_UPLOAD = "❌ দুঃখিত, ফাইলটি আপলোড করা যায়নি (সার্ভার সমস্যা)। কিছুক্ষণ পরে আবার চেষ্টা করুন।"
ERR_VERIFY = "❌ ফাইল আপলোড হলেও লিংকটি যাচাই করা যায়নি। আবার চেষ্টা করুন।"
ERR_GENERIC = "❌ দুঃখিত, একটি সমস্যা হয়েছে। কিছুক্ষণ পরে আবার চেষ্টা করুন।"
ERR_UNSUPPORTED = "❌ এই ধরনের মেসেজ থেকে ফাইল পাওয়া যায়নি। অনুগ্রহ করে একটি ফাইল/ছবি/ভিডিও পাঠান।"


# ------------------------------------------------------------------ helpers

def format_size(num_bytes):
    """1024 -> '1.00 KB', 0 -> '0 B', None -> 'অজানা'."""
    if num_bytes is None:
        return "অজানা"
    size = float(num_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    if i == 0:
        return f"{int(size)} B"
    return f"{size:.2f} {units[i]}"


def check_limit(size_bytes, max_bytes=None):
    """True if the file may be accepted. Unknown size (None) is allowed;
    the real size is checked again after download."""
    if size_bytes is None:
        return True
    return size_bytes <= (max_bytes if max_bytes is not None else MAX_FILE_BYTES)


def is_admin(user_id):
    return ADMIN_CHAT_ID is not None and user_id == ADMIN_CHAT_ID


def maintenance_on():
    return db.get_setting("maintenance", "0") == "1"


def main_menu_keyboard():
    rows = [
        [InlineKeyboardButton("📤 ফাইল আপলোড", callback_data="m:upload")],
        [InlineKeyboardButton("🔗 আমার শেষ লিংক", callback_data="m:last")],
        [InlineKeyboardButton("📊 আমার পরিসংখ্যান", callback_data="m:stats")],
        [InlineKeyboardButton("📚 সাহায্য", callback_data="m:help")],
    ]
    if SUPPORT_USERNAME:
        rows.append(
            [InlineKeyboardButton("💬 সাপোর্ট", url=f"https://t.me/{SUPPORT_USERNAME}")]
        )
    else:
        rows.append([InlineKeyboardButton("💬 সাপোর্ট", callback_data="m:support")])
    return InlineKeyboardMarkup(rows)


def back_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 মেইন মেনু", callback_data="m:home")]]
    )


def admin_keyboard():
    state = "ON 🟢" if maintenance_on() else "OFF 🔴"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 তথ্য আপডেট", callback_data="a:panel")],
            [InlineKeyboardButton(f"🔧 মেইনটেন্যান্স: {state}", callback_data="a:maint")],
            [InlineKeyboardButton("📋 এরর লগ", callback_data="a:errors")],
            [InlineKeyboardButton("📢 ব্রডকাস্ট", callback_data="a:broadcast")],
            [InlineKeyboardButton("✏️ ওয়েলকাম মেসেজ", callback_data="a:welcome")],
            [InlineKeyboardButton("🏠 মেইন মেনু", callback_data="m:home")],
        ]
    )


def admin_panel_text():
    users, files_count, total_size = db.global_stats()
    return (
        "🛠️ অ্যাডমিন প্যানেল\n\n"
        f"👥 মোট ইউজার: {users}\n"
        f"📁 মোট আপলোড ফাইল: {files_count}\n"
        f"💾 মোট স্টোরেজ ব্যবহার: {format_size(total_size)}\n"
        f"🔧 মেইনটেন্যান্স মোড: {'ON' if maintenance_on() else 'OFF'}\n"
        f"☁️ আপলোড প্রোভাইডার: {uploader.PROVIDER_LABELS.get(uploader.PROVIDER, uploader.PROVIDER)}"
    )


def extract_file(message):
    """Return dict(file_id, name, size, unique_id) from any file message."""
    f = message.document
    if f is not None:
        return f, (f.file_name or f"file_{f.file_unique_id}")
    if message.photo:
        f = message.photo[-1]  # largest size
        return f, f"photo_{f.file_unique_id}.jpg"
    f = message.video
    if f is not None:
        return f, (f.file_name or f"video_{f.file_unique_id}.mp4")
    f = message.audio
    if f is not None:
        name = f.file_name or f.title or f"audio_{f.file_unique_id}.mp3"
        return f, name
    f = message.voice
    if f is not None:
        return f, f"voice_{f.file_unique_id}.ogg"
    f = message.animation
    if f is not None:
        return f, (f.file_name or f"animation_{f.file_unique_id}.mp4")
    f = message.video_note
    if f is not None:
        return f, f"video_note_{f.file_unique_id}.mp4"
    f = message.sticker
    if f is not None:
        return f, f"sticker_{f.file_unique_id}.webp"
    return None, None


def register_user(update):
    user = update.effective_user
    if user:
        db.upsert_user(user.id, user.username, user.first_name)


# ------------------------------------------------------------------ commands

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)
    user = update.effective_user
    if maintenance_on() and not is_admin(user.id):
        await update.message.reply_text(MAINTENANCE_TEXT)
        return
    welcome = db.get_setting("welcome", DEFAULT_WELCOME)
    await update.message.reply_text(welcome, reply_markup=main_menu_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)
    await update.message.reply_text(HELP_TEXT, reply_markup=back_keyboard())


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ এই কমান্ড শুধুমাত্র বটের অ্যাডমিনের জন্য।")
        return
    await update.message.reply_text(admin_panel_text(), reply_markup=admin_keyboard())


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ এই কমান্ড শুধুমাত্র বটের অ্যাডমিনের জন্য।")
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "ব্যবহার: /broadcast আপনার মেসেজ\nউদাহরণ: /broadcast আগামীকাল বট আপডেট হবে।"
        )
        return
    user_ids = db.all_user_ids()
    ok, fail = 0, 0
    for uid in user_ids:
        try:
            await context.bot.send_message(uid, f"📢 অ্যাডমিনের বার্তা:\n\n{text}")
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # stay gentle with Telegram rate limits
    await update.message.reply_text(
        f"📢 ব্রডকাস্ট শেষ।\n✅ পাঠানো হয়েছে: {ok} জন\n❌ ব্যর্থ: {fail} জন"
    )


async def setwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ এই কমান্ড শুধুমাত্র বটের অ্যাডমিনের জন্য।")
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "ব্যবহার: /setwelcome আপনার নতুন ওয়েলকাম মেসেজ"
        )
        return
    db.set_setting("welcome", text)
    await update.message.reply_text("✅ ওয়েলকাম মেসেজ আপডেট হয়েছে। /start দিয়ে দেখুন।")


# ------------------------------------------------------------------ callbacks

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    register_user(update)
    user = update.effective_user
    data = query.data or ""

    if data == "m:home":
        welcome = db.get_setting("welcome", DEFAULT_WELCOME)
        await query.edit_message_text(welcome, reply_markup=main_menu_keyboard())
    elif data == "m:upload":
        await query.edit_message_text(
            "📤 ফাইল আপলোড করতে শুধু এই চ্যাটে যেকোনো ফাইল পাঠিয়ে দিন — "
            "ব্যাস! বট বাকি কাজ করে লিংক দিয়ে দেবে।",
            reply_markup=back_keyboard(),
        )
    elif data == "m:last":
        row = db.last_upload(user.id)
        if row:
            text = (
                "🔗 আপনার সর্বশেষ লিংক:\n\n"
                f"📄 ফাইল: {row['file_name']}\n"
                f"📏 সাইজ: {format_size(row['size'])}\n"
                f"🔗 {row['link']}"
            )
        else:
            text = "🔗 আপনি এখনো কোনো ফাইল আপলোড করেননি। প্রথমে একটি ফাইল পাঠান!"
        await query.edit_message_text(text, reply_markup=back_keyboard())
    elif data == "m:stats":
        count, total = db.user_stats(user.id)
        await query.edit_message_text(
            "📊 আপনার পরিসংখ্যান\n\n"
            f"📁 মোট আপলোড: {count}টি ফাইল\n"
            f"💾 মোট সাইজ: {format_size(total)}",
            reply_markup=back_keyboard(),
        )
    elif data == "m:help":
        await query.edit_message_text(HELP_TEXT, reply_markup=back_keyboard())
    elif data == "m:support":
        await query.edit_message_text(
            "💬 সাপোর্ট\n\nদুঃখিত, এই বটে এখনো কোনো সাপোর্ট আইডি সেট করা হয়নি। "
            "বটের মালিক SUPPORT_USERNAME সেট করলেই এখানে সরাসরি সাপোর্ট বাটন আসবে।",
            reply_markup=back_keyboard(),
        )
    elif data.startswith("a:"):
        if not is_admin(user.id):
            await query.edit_message_text("⛔ এই প্যানেল শুধুমাত্র অ্যাডমিনের জন্য।")
            return
        if data == "a:panel":
            await query.edit_message_text(
                admin_panel_text(), reply_markup=admin_keyboard()
            )
        elif data == "a:maint":
            new_state = "0" if maintenance_on() else "1"
            db.set_setting("maintenance", new_state)
            await query.edit_message_text(
                admin_panel_text(), reply_markup=admin_keyboard()
            )
        elif data == "a:errors":
            rows = db.last_errors(10)
            if rows:
                lines = ["📋 সর্বশেষ এরর লগ:\n"]
                for r in rows:
                    lines.append(
                        f"• [{r['category']}] {r['message'][:120]}"
                    )
                text = "\n".join(lines)
            else:
                text = "✅ কোনো এরর লগ নেই। সব ঠিকঠাক চলছে!"
            await query.edit_message_text(text, reply_markup=admin_keyboard())
        elif data == "a:broadcast":
            await query.edit_message_text(
                "📢 ব্রডকাস্ট পাঠাতে কমান্ড ব্যবহার করুন:\n\n"
                "/broadcast আপনার মেসেজ\n\n"
                "মেসেজটি বটের সব ইউজারের কাছে পৌঁছে যাবে।",
                reply_markup=admin_keyboard(),
            )
        elif data == "a:welcome":
            await query.edit_message_text(
                "✏️ ওয়েলকাম মেসেজ বদলাতে কমান্ড ব্যবহার করুন:\n\n"
                "/setwelcome আপনার নতুন মেসেজ\n\n"
                "বর্তমান মেসেজ:\n" + db.get_setting("welcome", DEFAULT_WELCOME),
                reply_markup=admin_keyboard(),
            )


# ------------------------------------------------------------------ file handling

async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user
    register_user(update)

    if maintenance_on() and not is_admin(user.id):
        await message.reply_text(MAINTENANCE_TEXT)
        return

    file_obj, file_name = extract_file(message)
    if file_obj is None:
        await message.reply_text(ERR_UNSUPPORTED)
        return

    file_size = getattr(file_obj, "file_size", None)
    unique_id = getattr(file_obj, "file_unique_id", None)

    # Hard size limit (Telegram Bot API itself cannot download > 20 MB).
    if not check_limit(file_size):
        db.log_error(user.id, "too_big", f"{file_name} size={file_size}")
        await message.reply_text(ERR_TOO_BIG)
        return

    # Duplicate: same file from the same user -> return the old link.
    old = db.find_by_unique_id(user.id, unique_id)
    if old:
        await message.reply_text(
            "♻️ এই ফাইলটি আপনি আগেও আপলোড করেছিলেন। আগের লিংকটিই দিলাম:\n\n"
            f"📄 ফাইল: {old['file_name']}\n"
            f"📏 সাইজ: {format_size(old['size'])}\n"
            f"🔗 {old['link']}"
        )
        return

    progress = await message.reply_text(
        f"⏳ ফাইল পাওয়া গেছে: {file_name}\nআপলোড হচ্ছে, একটু অপেক্ষা করুন..."
    )

    stage = "download"
    tmp_path = None
    try:
        suffix = os.path.splitext(file_name)[1] or ".bin"
        fd, tmp_path = tempfile.mkstemp(prefix="ftl_", suffix=suffix)
        os.close(fd)

        tg_file = await context.bot.get_file(file_obj.file_id)
        await tg_file.download_to_drive(tmp_path)  # streamed to disk

        real_size = os.path.getsize(tmp_path)
        if not check_limit(real_size):
            db.log_error(user.id, "too_big", f"{file_name} real_size={real_size}")
            await progress.edit_text(ERR_TOO_BIG)
            return

        stage = "upload"
        link, provider = await uploader.upload_file(tmp_path, file_name)

        stage = "verify"
        if not await uploader.verify_link(link):
            db.log_error(user.id, "verify_failed", f"{file_name} -> {link}")
            await progress.edit_text(ERR_VERIFY)
            return

        db.add_upload(user.id, unique_id, file_name, real_size, link)

        note = ""
        if provider == "gofile":
            note = "\nℹ️ এটি একটি ডাউনলোড পেজের লিংক — পেজটি খুলে Download বাটনে চাপ দিলেই ফাইলটি নামবে।"
        elif provider == "litterbox":
            note = "\n⏰ সতর্কতা: এই লিংকটি অস্থায়ী (প্রায় ৭২ ঘণ্টা পর্যন্ত থাকতে পারে)।"

        await progress.edit_text(
            "✅ আপলোড সম্পন্ন!\n\n"
            f"📄 ফাইল: {file_name}\n"
            f"📏 সাইজ: {format_size(real_size)}\n"
            f"🔗 ডাউনলোড লিংক:\n{link}"
            f"{note}"
        )
    except Exception as exc:
        logger.exception("file handling failed at stage=%s", stage)
        db.log_error(user.id, stage, f"{file_name}: {exc}")
        text = {
            "download": ERR_DOWNLOAD,
            "upload": ERR_UPLOAD,
            "verify": ERR_VERIFY,
        }.get(stage, ERR_GENERIC)
        try:
            await progress.edit_text(text)
        except Exception:
            await message.reply_text(text)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error", exc_info=context.error)
    user_id = None
    if isinstance(update, Update) and update.effective_user:
        user_id = update.effective_user.id
    db.log_error(user_id, "handler", str(context.error))


# ------------------------------------------------------------------ keep-alive + main

flask_app = Flask(__name__)


@flask_app.route("/")
def _home():
    return "File-to-Link bot is running."


def _run_flask():
    port = int(os.environ.get("PORT", "8080"))
    flask_app.run(host="0.0.0.0", port=port)


def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is required!")
    db.init_db()
    Thread(target=_run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("setwelcome", setwelcome_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ATTACHMENT, file_handler))
    app.add_error_handler(error_handler)

    logger.info("Bot started. Provider=%s, admin=%s", uploader.PROVIDER, ADMIN_CHAT_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
