import os
import json
import secrets
import string
import asyncio
import re
from datetime import datetime, time
from http.server import BaseHTTPRequestHandler
import aiohttp
from pymongo import MongoClient
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# --- CONFIG ---
BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
DB_URI            = os.environ.get("DB_URI", "")
CHANNEL_ID        = int(os.environ.get("CHANNEL_ID", "0"))
START_IMAGE       = os.environ.get("START_IMAGE", "")
LOG_GROUP_ID      = int(os.environ.get("LOG_GROUP_ID", "0"))
ADMIN_USERNAME    = os.environ.get("ADMIN_USERNAME", "@admin")
OWNER_ID          = int(os.environ.get("OWNER_ID", "0"))
ADMIN_EARNING_API = os.environ.get("ADMIN_EARNING_API", "")

# --- SYNC DB (pymongo - Vercel safe) ---
_mongo_client = None

def get_cols():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(DB_URI, serverSelectionTimeoutMS=5000)
    db = _mongo_client["FileStoreDB"]
    return db["users"], db["files"]

user_states = {}

# ── HELPERS ──────────────────────────────────────────────────────────────────

def generate_code():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))

def get_tonight_expiry():
    now = datetime.now()
    return datetime.combine(now.date(), time(23, 59, 59))

async def get_shortened_url(api_url, long_url):
    try:
        clean = re.split(r'[&?]url=',   api_url.strip(), flags=re.IGNORECASE)[0]
        clean = re.split(r'[&?]alias=', clean,            flags=re.IGNORECASE)[0]
        sep   = "&" if "?" in clean else "?"
        call  = f"{clean}{sep}url={long_url}"
        async with aiohttp.ClientSession() as s:
            async with s.get(call, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    try:
                        j = await r.json(content_type=None)
                        short = (j.get("shortenedUrl") or j.get("shortlink") or
                                 j.get("link") or j.get("url") or
                                 (j.get("data") or {}).get("shortitem"))
                        if short and short.startswith("http"):
                            return short
                    except Exception:
                        t = (await r.text()).strip()
                        if t.startswith("http"):
                            return t
    except Exception as e:
        print(f"Shortener error: {e}")
    return long_url

async def main_menu(update: Update, is_cb=False):
    text = "📂 **Main Menu**\n\nNiche diye buttons use karein:"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Your Links",         callback_data="your_links"),
         InlineKeyboardButton("⚙️ Enter Shortener",    callback_data="enter_shortener")],
        [InlineKeyboardButton("📁 Upload Single File", callback_data="upload_single"),
         InlineKeyboardButton("📦 Upload Bulk Files",  callback_data="upload_bulk")],
        [InlineKeyboardButton("❌ Delete Account",      callback_data="delete_confirm")]
    ])
    if is_cb:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")

# ── HANDLERS ─────────────────────────────────────────────────────────────────

async def start_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    uname  = ctx.bot.username
    args   = ctx.args or []
    users_col, files_col = get_cols()

    # Verification deep link
    if args and args[0].startswith("verify_"):
        token     = args[0]
        owner_doc = users_col.find_one({"Users.verify_token": token})
        if not owner_doc:
            await update.message.reply_text("❌ Link invalid ya expire ho chuka hai.")
            return
        users_col.update_one(
            {"user_id": owner_doc["user_id"], "Users.verify_token": token},
            {"$set": {"Users.$.status": "verified", "Users.$.expiretime": get_tonight_expiry()}}
        )
        await update.message.reply_text(
            "✅ **Verify ho gaye!**\nAb file link par dubara click karein.", parse_mode="Markdown")
        return

    # File deep link
    if args:
        code      = args[0].strip()
        file_data = files_col.find_one({"code": code})
        if not file_data:
            await update.message.reply_text("❌ Link invalid ya file delete ho chuki hai.")
            return

        owner_id = file_data.get("owner_id")
        if owner_id and owner_id != uid:
            owner = users_col.find_one({"user_id": owner_id})
            if owner:
                arr         = owner.get("Users", [])
                target_user = next((u for u in arr if u["userid"] == uid), None)
                now         = datetime.now()
                verified    = False

                if target_user:
                    if target_user.get("status") == "verified":
                        exp = target_user.get("expiretime")
                        if exp and now < exp:
                            verified = True
                        else:
                            users_col.update_one(
                                {"user_id": owner_id, "Users.userid": uid},
                                {"$set": {"Users.$.status": "unverified", "Users.$.verify_token": None}}
                            )
                else:
                    users_col.update_one(
                        {"user_id": owner_id},
                        {"$push": {"Users": {"userid": uid, "status": "unverified",
                                             "expiretime": None, "verify_token": None}}}
                    )

                if not verified:
                    wait_msg = await update.message.reply_text("⏳ Link ready ho raha hai...")
                    vtoken   = f"verify_{generate_code().lower()}"
                    base_url = f"https://t.me/{uname}?start={vtoken}"

                    users_col.update_one(
                        {"user_id": owner_id, "Users.userid": uid},
                        {"$set": {"Users.$.verify_token": vtoken}}
                    )

                    final_url = base_url
                    for api in owner.get("shorteners", []):
                        final_url = await get_shortened_url(api, final_url)
                    if ADMIN_EARNING_API:
                        final_url = await get_shortened_url(ADMIN_EARNING_API, final_url)
                    if not final_url.startswith("http"):
                        final_url = base_url

                    await wait_msg.delete()
                    await update.message.reply_text(
                        "⚠️ **Access Denied!**\n\nFile access ke liye verify karein. Aaj raat 12 baje tak valid.",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("🔐 Verify Account", url=final_url)]]),
                        parse_mode="Markdown"
                    )
                    return

        # Deliver files
        file_ids = file_data["file_ids"]
        await update.message.reply_text(f"📦 Files bhej raha hoon... ({len(file_ids)} files)")
        for fid in file_ids:
            try:
                await ctx.bot.copy_message(chat_id=uid, from_chat_id=CHANNEL_ID, message_id=int(fid))
                await asyncio.sleep(0.6)
            except Exception as e:
                print(f"Copy error: {e}")

        next_code = file_data.get("next_part")
        if next_code:
            nlink = f"https://t.me/{uname}?start={next_code}"
            await update.message.reply_text(
                "✨ Is part ki files ho gayin. Agla part 👇",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏩ Next Part", url=nlink)]]))
        else:
            await update.message.reply_text("✅ Saari files deliver ho gayi!")
        return

    # Normal /start
    user = users_col.find_one({"user_id": uid})
    if not user:
        text = "👋 Welcome! Files store karne ke liye account banao."
        kb   = InlineKeyboardMarkup([[InlineKeyboardButton("📝 Create Account", callback_data="create_account")]])
        if START_IMAGE:
            try:
                await update.message.reply_photo(photo=START_IMAGE, caption=text, reply_markup=kb)
                return
            except Exception:
                pass
        await update.message.reply_text(text, reply_markup=kb)
    else:
        await main_menu(update)


async def admin_verify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Access Denied!")
        return
    if not ctx.args:
        await update.message.reply_text("Format: `/a user_id`", parse_mode="Markdown")
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        return
    users_col, _ = get_cols()
    if not users_col.find_one({"user_id": tid}):
        await update.message.reply_text("❌ User nahi mila.")
        return
    users_col.update_one({"user_id": tid}, {"$set": {"status": "verified"}})
    await update.message.reply_text(f"✅ User `{tid}` verified!", parse_mode="Markdown")


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    d   = q.data
    await q.answer()
    users_col, _ = get_cols()

    if d == "create_account":
        if not users_col.find_one({"user_id": uid}):
            users_col.insert_one(
                {"user_id": uid, "links": [], "shorteners": [], "status": "unverified", "Users": []})
            await q.message.reply_text("🎉 Account ban gaya!")
        await main_menu(update, is_cb=False)

    elif d == "your_links":
        user = users_col.find_one({"user_id": uid})
        back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]])
        if not user or not user.get("links"):
            await q.edit_message_text("⚠️ Koi link nahi abhi tak.", reply_markup=back)
            return
        txt = "🔗 **Aapke Links:**\n\n" + "\n".join(f"{i+1}. {l}" for i, l in enumerate(user["links"]))
        await q.edit_message_text(txt, reply_markup=back, parse_mode="Markdown", disable_web_page_preview=True)

    elif d == "enter_shortener":
        user_states[uid] = {"state": "waiting_api", "apis": []}
        await q.edit_message_text(
            "⚙️ **Shortener API Mode:**\nLinks ek-ek bhejein. Khatam: `/end`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_to_menu")]]),
            parse_mode="Markdown")

    elif d == "upload_single":
        user_states[uid] = {"state": "waiting_single"}
        await q.edit_message_text(
            "📥 **Single File Mode:**\nKoi bhi file bhejein.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_to_menu")]]),
            parse_mode="Markdown")

    elif d == "upload_bulk":
        user_states[uid] = {"state": "waiting_bulk", "bulk_files": []}
        await q.edit_message_text(
            "📦 **Bulk File Mode:**\nSari files bhejein, khatam: `/end`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_to_menu")]]),
            parse_mode="Markdown")

    elif d == "delete_confirm":
        await q.edit_message_text("⚠️ **Pakka delete karna hai?**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Haan", callback_data="delete_account_final")],
                [InlineKeyboardButton("❌ Nahi", callback_data="back_to_menu")]]),
            parse_mode="Markdown")

    elif d == "delete_account_final":
        users_col.delete_one({"user_id": uid})
        user_states.pop(uid, None)
        await q.edit_message_text("🗑️ Account delete ho gaya. Wapas: `/start`")

    elif d == "back_to_menu":
        user_states.pop(uid, None)
        await main_menu(update, is_cb=True)


async def end_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    uname_tg = ctx.bot.username
    users_col, files_col = get_cols()
    user     = users_col.find_one({"user_id": uid})

    if user and user.get("status") == "unverified":
        tg_un = f"@{update.effective_user.username}" if update.effective_user.username else "No Username"
        try:
            await ctx.bot.send_message(LOG_GROUP_ID,
                f"key user ({tg_un})\nUser id({uid})\nUpload karne ki koshish ki")
        except Exception:
            pass
        await update.message.reply_text(f"❌ Admin se approval lo\n{ADMIN_USERNAME}")
        user_states.pop(uid, None)
        return

    if uid not in user_states:
        await update.message.reply_text("❌ Kisi active mode me nahi hain.")
        return

    state = user_states[uid]["state"]

    if state == "waiting_api":
        apis = user_states[uid]["apis"]
        if not apis:
            await update.message.reply_text("⚠️ Koi API link nahi bheja.")
            user_states.pop(uid, None)
            await main_menu(update)
            return
        users_col.update_one({"user_id": uid}, {"$set": {"shorteners": apis}})
        user_states.pop(uid, None)
        await update.message.reply_text(f"✅ **{len(apis)} API links save ho gaye!**", parse_mode="Markdown")
        await main_menu(update)

    elif state == "waiting_bulk":
        all_files = user_states[uid]["bulk_files"]
        if not all_files:
            await update.message.reply_text("⚠️ Koi file nahi bheji.")
            return
        chunks     = [all_files[i:i+50] for i in range(0, len(all_files), 50)]
        prev_code  = None
        first_link = ""
        for idx, chunk in enumerate(reversed(chunks)):
            code = generate_code()
            doc  = {"code": code, "file_ids": chunk, "owner_id": uid}
            if prev_code:
                doc["next_part"] = prev_code
            files_col.insert_one(doc)
            prev_code = code
            if idx == len(chunks) - 1:
                first_link = f"https://t.me/{uname_tg}?start={code}"
        users_col.update_one({"user_id": uid}, {"$push": {"links": first_link}})
        user_states.pop(uid, None)
        await update.message.reply_text(
            f"Link Ready:\n\n{first_link}",
            disable_web_page_preview=True)
        await main_menu(update)


async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_states:
        return
    if user_states[uid]["state"] == "waiting_api":
        api = update.message.text.strip()
        if not api.startswith("http"):
            await update.message.reply_text("❌ Valid http/https URL chahiye.")
            return
        user_states[uid]["apis"].append(api)
        n = len(user_states[uid]["apis"])
        await update.message.reply_text(f"📥 API #{n} saved! Agla bhejein ya `/end` karein.", parse_mode="Markdown")


async def file_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    uname_tg = ctx.bot.username
    users_col, files_col = get_cols()
    user     = users_col.find_one({"user_id": uid})
    if not user:
        return

    if user.get("status") == "unverified":
        tg_un = f"@{update.effective_user.username}" if update.effective_user.username else "No Username"
        try:
            await ctx.bot.send_message(LOG_GROUP_ID,
                f"key user ({tg_un})\nUser id({uid})\nFile upload karne ki koshish")
        except Exception:
            pass
        await update.message.reply_text(f"❌ Admin se approval lo\n{ADMIN_USERNAME}")
        return

    if uid not in user_states:
        return

    try:
        fwd = await update.message.forward(CHANNEL_ID)
        fid = fwd.message_id
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return

    state = user_states[uid]["state"]

    if state == "waiting_single":
        code = generate_code()
        link = f"https://t.me/{uname_tg}?start={code}"
        files_col.insert_one({"code": code, "file_ids": [fid], "owner_id": uid})
        users_col.update_one({"user_id": uid}, {"$push": {"links": link}})
        user_states.pop(uid, None)
        await update.message.reply_text(
            f"Link Ready:\n\n{first_link}",
            disable_web_page_preview=True)
        await main_menu(update)

    elif state == "waiting_bulk":
        user_states[uid]["bulk_files"].append(fid)
        n = len(user_states[uid]["bulk_files"])
        await update.message.reply_text(f"File received ({n}). /end se khatam karein.")


# ── APP BUILDER ───────────────────────────────────────────────────────────────

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("a",     admin_verify))
    app.add_handler(CommandHandler("end",   end_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (
            filters.Document.ALL | filters.VIDEO | filters.AUDIO |
            filters.PHOTO | filters.ANIMATION),
        file_handler))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        text_handler))
    return app


# ── VERCEL ENTRY POINT ────────────────────────────────────────────────────────

_app  = None
_loop = None

def get_loop():
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop

async def get_app():
    global _app
    if _app is None:
        _app = build_app()
        await _app.initialize()
    return _app

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length).decode())

            loop = get_loop()

            async def process():
                application = await get_app()
                upd = Update.de_json(body, application.bot)
                await application.process_update(upd)

            loop.run_until_complete(process())

        except Exception as e:
            print(f"Webhook error: {e}")
        finally:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is live!")
