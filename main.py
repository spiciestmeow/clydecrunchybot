import os
import asyncio
import re
import json
import random
import zipfile
import io
import html
import httpx
import pytz
import redis.asyncio as aioredis
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from telegram import BotCommandScopeChat
from io import BytesIO
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application
from deep_translator import GoogleTranslator
from gifs import *
from telegram import InputMediaPhoto
from regions import REGION_HINTS, get_region_flag

# ──────────────────────────────────────────────
# AUTO TRANSLATION SYSTEM — English + Tagalog + Bisaya (using deep-translator)
# ──────────────────────────────────────────────
SUPPORTED_LANGUAGES = {
    "en":  ("🇬🇧", "English"),
    "tl":  ("🇵🇭", "Tagalog"),
    "ceb": ("🇵🇭", "Bisaya"),
}


LEVEL_TITLE_DESCRIPTIONS = {
    1: ("🌱 Young Sprout",        "Your first steps into the enchanted forest."),
    2: ("🌿 Forest Sprout",       "The trees are beginning to recognize you."),
    3: ("🍃 Gentle Wanderer",     "You move through the clearing with quiet curiosity."),
    4: ("🌳 Woodland Explorer",   "The hidden paths of the forest open before you."),
    5: ("🌲 Whispering Wanderer", "The ancient trees whisper your name in the wind."),
    6: ("🪵 Tree Guardian",       "You stand firm like the oldest oak in the clearing."),
    7: ("🌌 Mist Walker",         "You drift through the forest like morning fog."),
    8: ("✨ Enchanted Keeper",    "The forest spirits have chosen you as their keeper."),
    9: ("🌠 Ancient Soul",        "Your roots run deeper than the oldest tree."),
    10: ("🌟 Eternal Guardian",   "The entire enchanted clearing bows to your wisdom."),
}

# ──────────────────────────────────────────────
# NEW: STEAM SEARCH HELPERS (Multi-account + Bundle UX)
# ──────────────────────────────────────────────

def is_bundle_account(acc: dict) -> bool:
    """Returns True if this is a big bundle account (2+ games in games[])"""
    games = acc.get("games") or []
    valid_games = [str(g).strip() for g in games if str(g).strip()]
    return len(valid_games) >= 2

async def show_steam_account_selection(chat_id: int, group_key: str, game_name: str, query_obj=None):
    raw = await redis_client.get(f"steam_group:{chat_id}:{group_key}")
    if not raw:
        await tg_app.bot.send_message(chat_id, "⏳ Result expired. Please search again.", parse_mode="HTML")
        return
    
    data = json.loads(raw)
    emails = data.get("emails", [])
    game_name = data.get("game_name", game_name) 
    
    accounts = []
    for email in emails:
        acc_data = await _sb_get(
            "steamCredentials",
            **{
                "email": f"eq.{email}",
                "status": "eq.Available",
                "select": "email,game_name,image_url,password,steam_id,release_type,games"
            }
        ) or []
        if acc_data:
            accounts.extend(acc_data)

    if not accounts:
        await tg_app.bot.send_message(chat_id, "❌ No accounts available anymore.", parse_mode="HTML")
        return

    text = f"🎮 <b>{html.escape(game_name)}</b> — Choose an account\n\n"
    buttons = []

    for i, acc in enumerate(accounts, 1):
        email = acc.get("email", "")
        short_email = email[:15] + "..." if len(email) > 15 else email
        bundle_note = " ← <b>Big Bundle</b>" if is_bundle_account(acc) else ""

        text += f"{i}️⃣ <b>Account {i}</b>{bundle_note}\n"
        text += f"   📧 <code>{html.escape(short_email)}</code>\n\n"

        buttons.append([
            InlineKeyboardButton(
                f"✅ Claim Account {i}", 
                callback_data=f"claim_steam|{acc['email']}|{group_key}"
            )
        ])

    # FIXED: Proper back button
    buttons.append([InlineKeyboardButton("⬅️ Back to Results", callback_data=f"steam_back_to_results|{group_key}")])
    buttons.append([InlineKeyboardButton("🔄 Search Different Game", callback_data="search_different_game")])

    try:
        if query_obj and query_obj.message:
            await query_obj.message.edit_text(text=text.strip(), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await tg_app.bot.send_message(chat_id, text=text.strip(), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        await tg_app.bot.send_message(chat_id, text=text.strip(), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

# ──────────────────────────────────────────────
# NEW: Get the exact display name the user saw during search
# ──────────────────────────────────────────────
async def get_display_name_for_claim(chat_id: int, group_key: str = None, fallback_email: str = None) -> str:
    """Returns the nice display name shown in search (especially important for bundles)"""
    if group_key:
        raw = await redis_client.get(f"steam_group:{chat_id}:{group_key}")
        if raw:
            try:
                data = json.loads(raw)
                game_name = data.get("game_name")
                if game_name:
                    return game_name
            except Exception:
                pass

    # Fallback: fetch from database
    if fallback_email:
        acc_data = await _sb_get(
            "steamCredentials",
            **{"email": f"eq.{fallback_email}", "select": "game_name"}
        ) or []
        if acc_data:
            return acc_data[0].get("game_name", "Steam Account")

    return "Steam Account"

# ──────────────────────────────────────────────
# NEW STEAM SEARCH + COOLDOWN SYSTEM
# ──────────────────────────────────────────────
STEAM_COOLDOWN_HOURS = {
    1: 29, 2: 27, 3: 25, 4: 23, 5: 20,
    6: 18, 7: 15, 8: 12, 9:  8, 10:  4,
}

def get_steam_cooldown_hours(level: int) -> int:
    """Returns cooldown in hours based on user level"""
    level = min(max(int(level), 1), 10)
    return STEAM_COOLDOWN_HOURS.get(level, 4)

def get_colored_progress_bar(percentage: int, width: int = 10) -> str:
    """Beautiful color-coded progress bar: empty → red → yellow → green"""
    if percentage == 0:
        return "⬛" * width + f" <b>0%</b> ⚫"

    filled = int(width * percentage / 100)
    empty = width - filled

    if percentage < 40:
        bar_color = "🟥"      # Red - early stage
        indicator = "🔴"
    elif percentage < 75:
        bar_color = "🟨"      # Yellow - good progress
        indicator = "🟡"
    else:
        bar_color = "🟩"      # Green - almost done
        indicator = "🟢"

    bar = bar_color * filled + "⬜" * empty
    return f"{bar} <b>{percentage}%</b> {indicator}"

def clean_image_url(url: str) -> str:
    """Smart cleaner for any image URL (Steam + general)"""
    if not url or not url.startswith(('http://', 'https://')):
        return url.strip()

    original_url = url.strip()

    # 1. Remove query parameters (?...) and fragments (#...)
    if "?" in url:
        url = url.split("?", 1)[0]
    if "#" in url:
        url = url.split("#", 1)[0]

    # 2. Special handling for Steam URLs (very common case)
    if "steamstatic.com" in url or "steamusercontent.com" in url:
        # Ensure it ends with /header.jpg
        if "/header." in url and not url.lower().endswith(('.jpg', '.jpeg', '.png')):
            url = url.split("/header.")[0] + "/header.jpg"
        # Remove any extra parameters that might remain
        if "?" in url:
            url = url.split("?", 1)[0]

    # 3. Final safety: If it doesn't look like an image, try to find last image extension
    lower_url = url.lower()
    image_exts = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
    
    if not any(lower_url.endswith(ext) for ext in image_exts):
        for ext in image_exts:
            pos = lower_url.rfind(ext)
            if pos != -1:
                url = url[:pos + len(ext)]
                break

    # 4. Final cleanup
    cleaned = url.strip()
    
    # Log if we changed something (helpful for debugging)
    if cleaned != original_url:
        print(f"🧹 Cleaned URL: {original_url[:100]}... → {cleaned}")

    return cleaned

# ══════════════════════════════════════════════════════════════════════════════
# SUPER ADVANCED ACHIEVEMENT SYSTEM (54 achievements)
# ══════════════════════════════════════════════════════════════════════════════
BOT_READY = False  # global flag

ACHIEVEMENTS_CACHE: dict = {}

async def load_achievements_cache():
    global ACHIEVEMENTS_CACHE
    data = await _sb_get("achievements", select="*", order="id.asc") or []
   
    ACHIEVEMENTS_CACHE = {ach["code"]: ach for ach in data}
   
    try:
        await redis_client.setex("achievements_cache", 3600, json.dumps(ACHIEVEMENTS_CACHE))
    except Exception as e:
        print(f"⚠️ Redis cache set failed: {e}")
   
    print(f"✅ Loaded {len(ACHIEVEMENTS_CACHE)} achievements. Codes: {list(ACHIEVEMENTS_CACHE.keys())[:15]}...")
    return len(ACHIEVEMENTS_CACHE)

async def get_user_achievements(chat_id: int) -> list:
    """Get all achievements for a user (unlocked + in-progress)"""
    data = await _sb_get(
        "user_achievements",
        **{"chat_id": f"eq.{chat_id}", "select": "*"}
    ) or []
    return data

async def get_user_telegram_photo(chat_id: int) -> str | None:
    """Fetch user's Telegram profile photo file_id"""
    try:
        photos = await tg_app.bot.get_user_profile_photos(user_id=chat_id, limit=1)
        if not photos or photos.total_count == 0:
            return None
        return photos.photos[0][-1].file_id  # highest res, file_id doesn't expire
    except Exception as e:
        print(f"⚠️ Could not fetch profile photo for {chat_id}: {e}")
        return None

async def get_gif_enabled(chat_id: int) -> bool:
    val = await redis_client.get(f"setting:gifs:{chat_id}")
    return val != "0"  # default ON

async def toggle_gif_setting(chat_id: int) -> bool:
    current = await get_gif_enabled(chat_id)
    new_state = not current
    await redis_client.set(f"setting:gifs:{chat_id}", "1" if new_state else "0")
    return new_state

async def check_and_award_achievements(chat_id: int, first_name: str, action: str = None):
    if not ACHIEVEMENTS_CACHE:
        await load_achievements_cache()

    profile = await get_user_profile(chat_id)
    if not profile:
        return

    user_achs = await get_user_achievements(chat_id)
    unlocked_codes = {u["achievement_code"] for u in user_achs if u.get("unlocked_at")}

    awarded_count = 0
    newly_awarded = [] 

    pending_column_updates: dict = {}

    for code, ach in ACHIEVEMENTS_CACHE.items():
        if code in unlocked_codes:
            continue
        if ach.get("condition", {}).get("type") == "manual":
            continue

        condition = ach.get("condition", {})
        cond_type = condition.get("type")
        should_unlock = False

        try:
            if cond_type == "count":
                field = condition.get("field")
                required = condition.get("required", 0)

                if field == "windows_views":
                    current = (profile.get("windows_views", 0) or 0) + \
                              (profile.get("office_views", 0) or 0)
                else:
                    current = profile.get(field, 0) or 0

                if current >= required:
                    should_unlock = True

            elif cond_type == "streak" and action == "daily_bonus":
                streak = await calculate_streak(chat_id)
                if streak >= condition.get("days", 0):
                    should_unlock = True

            elif cond_type == "level":
                if profile.get("level", 1) >= condition.get("required_level", 1):
                    should_unlock = True

            elif cond_type == "level_streak":
                level_ok = profile.get("level", 1) >= condition.get("required_level", 1)
                streak = await calculate_streak(chat_id)
                streak_ok = streak >= condition.get("required_streak", 1)
                if level_ok and streak_ok:
                    should_unlock = True

            elif cond_type == "reveal_netflix":
                if profile.get("netflix_reveals", 0) >= condition.get("required", 0):
                    should_unlock = True

            elif cond_type == "reveal_prime":
                if profile.get("prime_reveals", 0) >= condition.get("required", 0):
                    should_unlock = True

            elif cond_type in ("view_windows", "view_office"):
                views = (profile.get("windows_views", 0) or 0) + \
                        (profile.get("office_views", 0) or 0)
                if views >= condition.get("required", 0):
                    should_unlock = True

            elif cond_type == "wheel_spin":
                if profile.get("total_wheel_spins", 0) >= condition.get("required", 0):
                    should_unlock = True

            elif cond_type == "legendary_spin":
                if profile.get("legendary_spins", 0) >= condition.get("required", 0):
                    should_unlock = True

            elif cond_type == "steam_claim":
                if profile.get("steam_claims_count", 0) >= condition.get("required", 0):
                    should_unlock = True

            elif cond_type == "referral_total":
                if profile.get("referral_count", 0) >= condition.get("required", 0):
                    should_unlock = True

            elif cond_type == "daily_xp":
                if profile.get("total_xp_earned", 0) >= condition.get("required", 0):
                    should_unlock = True

            if should_unlock:
                progress_value = 100 if cond_type in ("level", "level_streak") else \
                                profile.get(condition.get("field"), 0)

                await _sb_post("user_achievements", {
                    "chat_id": chat_id,
                    "achievement_code": code,
                    "progress": progress_value,
                    "unlocked_at": datetime.now(pytz.utc).isoformat(),
                    "tier": 4 if ach.get("rarity") in ["legendary", "mythic"] else 3
                })

                # ── BUG 2 FIX: accumulate instead of patching immediately ──
                reward = ach.get("reward", {})
                if reward.get("type") == "permanent_slot":
                    col = reward.get("column")
                    amount = reward.get("amount", 0)
                    if col and amount:
                        pending_column_updates[col] = \
                            pending_column_updates.get(col, 0) + amount

                newly_awarded.append(ach)
                awarded_count += 1

        except Exception as e:
            print(f"🔴 Error checking achievement {code} for {chat_id}: {e}")

    # ── BUG 2 FIX: apply all column bonuses in one pass after the loop ──
    for col, total_amount in pending_column_updates.items():
        current_val = profile.get(col) or 0
        await _sb_patch(f"user_profiles?chat_id=eq.{chat_id}", {
            col: current_val + total_amount
        })
        print(f"✅ Applied +{total_amount} to {col} for {chat_id} (was {current_val})")

    # ── Send unlock notifications after all DB writes are done ──
    for ach in newly_awarded:
        await send_achievement_unlock(chat_id, ach, first_name)

    if awarded_count > 0:
        print(f"🎉 Awarded {awarded_count} new achievements to {chat_id}")

async def handle_award_beta_guardian(chat_id: int, target_id: int):
    """Manually award the Beta Guardian achievement"""
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can award this.")
        return

    # Check if already unlocked
    user_achs = await get_user_achievements(target_id)
    if any(u["achievement_code"] == "beta_guardian" and u.get("unlocked_at") for u in user_achs):
        await tg_app.bot.send_message(chat_id, f"⚠️ User {target_id} already has <b>Beta Guardian</b>.")
        return

    # Award it
    success = await _sb_post("user_achievements", {
        "chat_id": target_id,
        "achievement_code": "beta_guardian",
        "progress": 100,
        "unlocked_at": datetime.now(pytz.utc).isoformat(),
        "tier": 4  # mythic tier
    })

    if success:
        profile = await get_user_profile(target_id)
        first_name = profile.get("first_name", "Wanderer") if profile else "Wanderer"

        # Apply the permanent_slot reward for beta_guardian
        ach_data = ACHIEVEMENTS_CACHE.get("beta_guardian", {})
        reward = ach_data.get("reward", {})
        if reward.get("type") == "permanent_slot":
            col = reward.get("column")
            amount = reward.get("amount", 0)
            if col and amount:
                await _sb_patch(f"user_profiles?chat_id=eq.{target_id}", {
                    col: (profile.get(col) or 0) + amount
                })

        await tg_app.bot.send_message(
            chat_id,
            f"✅ <b>Beta Guardian</b> successfully awarded to user `{target_id}` ({first_name})"
        )

        # Send epic unlock to the user
        ach = ACHIEVEMENTS_CACHE.get("beta_guardian")
        if ach:
            await send_achievement_unlock(target_id, ach, first_name)
    else:
        await tg_app.bot.send_message(chat_id, "❌ Failed to award. Check logs.")

async def send_achievement_unlock(chat_id: int, ach: dict, first_name: str):
    rarity_emoji = {"common": "🌿", "rare": "✨", "epic": "🌟", "legendary": "🌠", "mythic": "🪐"}
    emoji = rarity_emoji.get(ach.get("rarity", "epic"), "🌱")

    is_manual = ach.get("condition", {}).get("type") == "manual"
    manual_note = "\n\n🌟 <i>This is a special manual achievement awarded by the Forest Caretaker.</i>" if is_manual else ""

    # ── BUILD REWARD LINE ──
    reward = ach.get("reward", {})
    reward_line = ""
    perk_text = ""
    if reward.get("type") == "permanent_slot":
        amount = reward.get("amount", 0)
        column = reward.get("column", "")
        column_labels = {
            "all_slots_bonus":       "ALL daily slots",
            "netflix_reveals_bonus": "Netflix reveals/day",
            "prime_reveals_bonus":   "Prime reveals/day",
            "crunchyroll_reveals_bonus": "Crunchyroll reveals/day",
            "windows_views_bonus":   "Windows/Office views/day",
            "daily_reveals_bonus":   "cookie reveals/day",
        }
        label = column_labels.get(column, column)
        reward_line = f"\n\n🎁 <b>Perk Unlocked:</b> +{amount} {label} <b>permanently!</b>"
        perk_text = f"🎁 Perk Unlocked: +{amount} {label} permanently!"

    caption = (
        f"{emoji} <b>A HIDDEN ACHIEVEMENT WAS REVEALED!</b>\n\n"
        f"🏆 <b>{ach['name']}</b>\n"
        f"{ach['description']}"
        f"{reward_line}"
        f"{manual_note}\n\n"
        f"<i>The ancient forest spirits have recognized you, {html.escape(first_name)}!</i> 🌲✨"
    )

    msg = await send_animated_translated(
        chat_id=chat_id,
        animation_url=ach.get("gif_url") or "https://media2.giphy.com/media/v1.Y2lkPTc5MGI3NjExcXB3ZW45ZTRzdmdlMmhreTczOXVzNjd3MWM5cDFpOGtzMXo1YWZwcCZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/JwcakAq5WbVPdOap7F/giphy.gif",
        caption=caption,
    )

    await _remember(chat_id, msg.message_id)

    # ── TEMP PERK REMINDER (3 seconds delay so it appears after the animation) ──
    if perk_text:
        async def _delayed_perk():
            await asyncio.sleep(3)
            await send_temporary_message(chat_id, perk_text, duration=3)
        asyncio.create_task(_delayed_perk())

async def get_forest_patrons() -> list:
    """Fetch visible patrons from Supabase (cached 1 hour)"""
    cached = await redis_client.get("patrons_cache")
    if cached:
        return json.loads(cached)

    data = await _sb_get(
        "forest_patrons",
        **{
            "select": "display_name,title",
            "is_visible": "eq.true",
            "order": "donated_at.desc"
        }
    ) or []

    await redis_client.setex("patrons_cache", 3600, json.dumps(data))
    return data

async def add_patron(username: str, title: str = "Kind Wanderer") -> bool:
    """Add a new donor"""
    display = username if username.startswith("@") else f"@{username}"
    
    payload = {
        "username": username.strip(),
        "display_name": display,
        "title": title.strip()
    }
    
    success = await _sb_upsert("forest_patrons", payload, on_conflict="username")
    
    if success:
        await redis_client.delete("patrons_cache")   # clear cache so new patron shows instantly
    return success

async def get_user_language(chat_id: int) -> str:
    profile = await get_user_profile(chat_id)
    return profile.get("preferred_language", "en") if profile else "en"

async def set_user_profile_gif(chat_id: int, file_id: str) -> bool:
    """Save custom GIF as profile logo — now uses safe UPSERT"""
    payload = {
        "chat_id": chat_id,
        "profile_gif_id": file_id
    }
    
    success = await _sb_upsert(
        "user_profiles", 
        payload, 
        on_conflict="chat_id"
    )
    
    if success:
        print(f"✅ Profile GIF saved for user {chat_id} → {file_id[:30]}...")
        # Extra safety: verify it actually wrote to DB
        profile = await get_user_profile(chat_id)
        if profile and profile.get("profile_gif_id") == file_id:
            print(f"✅ Verified in Supabase: profile_gif_id = {file_id}")
            return True
        else:
            print(f"🔴 Save succeeded but verification failed for {chat_id}")
    else:
        print(f"🔴 Failed to save GIF for {chat_id}")
    
    return success

async def set_user_language(chat_id: int, lang_code: str):
    if lang_code not in SUPPORTED_LANGUAGES:
        lang_code = "en"
    await _sb_patch(f"user_profiles?chat_id=eq.{chat_id}", {"preferred_language": lang_code})

async def translate_text(text: str, target_lang: str) -> str:
    """Auto translate using Google Translate (safe & compatible)"""
    if not text or target_lang == "en" or not text.strip():
        return text
    
    try:
        # Run in background thread so it doesn't block your async bot
        translated = await asyncio.to_thread(
            lambda: GoogleTranslator(source='auto', target=target_lang).translate(text)
        )
        return translated
    except Exception:
        return text  # fallback to original text if anything fails

# ──────────────────────────────────────────────
# ADMIN COMMAND: Add Game Logo (BULK + DUPLICATE SAFE)
# ──────────────────────────────────────────────
async def handle_add_game_logo(chat_id: int, raw_text: str):
    """Admin command: /addgamelogo — bulk upload with duplicate protection + smart Steam URL cleaning"""
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can add game logos.")
        return

    body = raw_text[len("/addgamelogo"):].strip()

    if not body:
        await tg_app.bot.send_message(
            chat_id,
            "📌 <b>Game Logo Bulk Upload</b>\n\n"
            "Send in this format:\n\n"
            "<code>/addgamelogo\n"
            "Game Name 1\n"
            "https://example.com/logo1.jpg\n\n"
            "Game Name 2\n"
            "https://shared.fastly.steamstatic.com/.../header.jpg?t=123456</code>\n\n"
            "• Game name on its own line\n"
            "• URL on the next line\n"
            "• Blank lines between entries are ignored\n"
            "• Lines starting with # are comments",
            parse_mode="HTML"
        )
        return

    # Clean lines
    lines = [line.strip() for line in body.splitlines() 
             if line.strip() and not line.strip().startswith('#')]

    added = 0
    duplicates = 0
    duplicate_list = []
    i = 0

    while i < len(lines):
        game_name_raw = lines[i]
        game_name = game_name_raw.strip().strip('"\'')   # remove quotes if present

        # Find the next URL
        url = None
        j = i + 1
        while j < len(lines):
            if lines[j].startswith(('http://', 'https://')):
                url = lines[j]
                break
            j += 1

        if game_name and url:
            # ── SMART STEAM URL CLEANING ──
            cleaned_url = clean_image_url(url)

            # Check for duplicate game
            existing = await _sb_get(
                "game_logos",
                **{"game_name": f"eq.{game_name}", "select": "game_name", "limit": 1}
            )

            if existing:
                duplicates += 1
                duplicate_list.append(game_name)
                print(f"⚠️ Duplicate skipped: {game_name}")
            else:
                payload = {
                    "game_name": game_name,
                    "game_url": cleaned_url
                }
                success, _ = await _sb_upsert("game_logos", payload, on_conflict="game_name")
                
                if success:
                    await redis_client.delete(f"game_logo:{game_name.lower()}")
                    added += 1
                    print(f"✅ Added: {game_name} → {cleaned_url}")
                else:
                    duplicates += 1
                    duplicate_list.append(game_name)

            i = j + 1
        else:
            i += 1

    # Result message...
    if added > 0 or duplicates > 0:
        result = (
            f"✅ <b>Bulk Game Logo Import Complete!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"🌱 <b>Successfully added:</b> {added} new logo(s)\n"
            f"⚠️ <b>Duplicates (skipped):</b> {duplicates}\n\n"
        )

        if duplicate_list:
            result += "<b>📋 Duplicate games:</b>\n" + "\n".join(f"• <code>{html.escape(name)}</code>" for name in duplicate_list[:10]) + "\n\n"

        result += "🧹 All Redis caches cleared — new logos are now live! 🌲"

        await tg_app.bot.send_message(chat_id, result, parse_mode="HTML")
    else:
        await tg_app.bot.send_message(chat_id, "❌ No valid game logos found.", parse_mode="HTML")

async def handle_game_logo_txt_upload(chat_id: int, content: str, filename: str = "logos.txt"):
    """Process game logo upload from .txt file with beautiful color-coded progress"""
    lines = [line.strip() for line in content.splitlines() 
             if line.strip() and not line.strip().startswith('#')]

    total_entries = len(lines) // 2
    added = 0
    duplicates = 0
    errors = []
    duplicate_list = []
    i = 0

    # Initial loading message
    loading = await tg_app.bot.send_message(
        chat_id, 
        f"🌿 <b>Processing Game Logos</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📄 <code>{filename}</code>\n"
        f"🔍 Found <b>{total_entries}</b> potential entries...\n\n"
        f"⏳ Starting import into the forest...",
        parse_mode="HTML"
    )

    processed = 0

    while i < len(lines):
        game_name_raw = lines[i]
        game_name = game_name_raw.strip().strip('"\'')
        
        url = None
        j = i + 1
        while j < len(lines):
            if lines[j].startswith(('http://', 'https://')):
                url = clean_image_url(lines[j])
                break
            j += 1

        if game_name and url:
            processed += 1
            percentage = int((processed / max(total_entries, 1)) * 100)

            # Update progress every 8 entries (or at the end)
            if processed % 8 == 0 or processed == total_entries:
                progress_bar = get_colored_progress_bar(percentage)

                await loading.edit_text(
                    f"🌿 <b>Processing Game Logos</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📄 <code>{filename}</code>\n"
                    f"📊 <b>{processed}/{total_entries}</b> entries • <b>{percentage}%</b>\n\n"
                    f"{progress_bar}\n\n"
                    f"✅ <b>Added:</b> {added}\n"
                    f"⚠️ <b>Duplicates:</b> {duplicates}\n"
                    f"❌ <b>Errors:</b> {len(errors)}",
                    parse_mode="HTML"
                )

            existing = await _sb_get(
                "game_logos", 
                **{"game_name": f"eq.{game_name}", "select": "game_name", "limit": 1}
            )

            if existing:
                duplicates += 1
                duplicate_list.append(game_name)
            else:
                payload = {
                    "game_name": game_name,
                    "game_url": url
                }
                success, _ = await _sb_upsert("game_logos", payload, on_conflict="game_name")
                
                if success:
                    await redis_client.delete(f"game_logo:{game_name.lower()}")
                    added += 1
                else:
                    errors.append(game_name)
            
            i = j + 1
        else:
            i += 1

    # ── Final beautiful result ──
    result = (
        f"✅ <b>Game Logo Import Complete!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📄 <b>File:</b> <code>{filename}</code>\n"
        f"🌱 <b>Successfully added:</b> {added} new logos\n"
        f"⚠️ <b>Duplicates skipped:</b> {duplicates}\n"
    )

    if errors:
        result += f"❌ <b>Failed:</b> {len(errors)}\n"

    if duplicate_list:
        result += f"\n<b>📋 Duplicate games (skipped):</b>\n"
        result += "\n".join(f"• <code>{html.escape(name)}</code>" for name in duplicate_list[:15])

    result += (
        f"\n\n🧹 All Redis caches cleared — new logos are now live in the forest! 🌲\n"
        f"<i>The ancient trees have memorized the new paths.</i>"
    )

    await loading.edit_text(result, parse_mode="HTML")
    
# ──────────────────────────────────────────────
# GAME LOGOS INTEGRATION — Prioritizes games[] array (as requested)
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# GAME LOGOS INTEGRATION — Prioritizes games[] array + preferred display name
# ──────────────────────────────────────────────
async def get_game_logo_url(
    game_name: str = None, 
    games_list: list = None, 
    preferred_name: str = None   # ← NEW: highest priority (what user actually saw)
) -> str | None:
    """Returns the best logo URL from game_logos table.
    
    Priority order:
    1. preferred_name (the exact name shown to the user during search — most important for bundles)
    2. game_name column
    3. All entries in the games[] text[] array
    """
    candidates = []

    # 1. Highest priority: the name the user actually saw in search results
    if preferred_name and str(preferred_name).strip():
        candidates.append(str(preferred_name).strip())

    # 2. Original game_name column (fallback)
    if game_name and str(game_name).strip():
        candidates.append(str(game_name).strip())

    # 3. All games from the games[] array (for bundles)
    if games_list and isinstance(games_list, list):
        for g in games_list:
            if g and isinstance(g, str) and str(g).strip():
                candidates.append(str(g).strip())

    if not candidates:
        return None

    # Remove exact duplicates while preserving order
    seen = set()
    unique_candidates = []
    for name in candidates:
        normalized = name.strip().lower()
        if normalized not in seen:
            seen.add(normalized)
            unique_candidates.append(name.strip())

    # Try each candidate until we find a logo
    for name in unique_candidates:
        normalized = name.strip()
        cache_key = f"game_logo:{normalized.lower()}"

        # Check Redis cache
        cached = await redis_client.get(cache_key)
        if cached:
            if cached != "null":
                return cached
            continue

        # Query game_logos table (exact match)
        data = await _sb_get(
            "game_logos",
            **{
                "game_name": f"eq.{normalized}",
                "select": "game_url",
                "limit": 1
            }
        )

        logo_url = data[0].get("game_url") if data and len(data) > 0 else None

        # Cache result
        await redis_client.setex(
            cache_key,
            3600,
            logo_url if logo_url else "null"
        )

        if logo_url:
            return logo_url

    return None

# ══════════════════════════════════════════════════════════════════════════════
# STEAM AUTOMATED DISTRIBUTION SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

def safe_handler(context: str = ""):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                print(f"🔴 [{context or func.__name__}]: {e}")
                import traceback
                traceback.print_exc()

                chat_id = None
                query = None

                for arg in args:
                    if hasattr(arg, 'effective_chat') and arg.effective_chat:
                        chat_id = arg.effective_chat.id
                    if hasattr(arg, 'callback_query') and arg.callback_query:
                        query = arg.callback_query
                    if isinstance(arg, int):
                        chat_id = arg

                if chat_id:
                    try:
                        # Answer the query first to stop the loading spinner
                        if query:
                            try:
                                await query.answer()
                            except Exception:
                                pass

                        # Always send a FRESH message — never edit
                        await tg_app.bot.send_message(
                            chat_id,
                            "🌫️ <b>Something went wrong in the forest...</b>",
                            parse_mode="HTML"
                        )
                    except Exception as send_err:
                        print(f"🔴 Could not send error message to {chat_id}: {send_err}")
                else:
                    print(f"🔴 safe_handler: could not find chat_id in args")

        return wrapper
    return decorator

def get_early_access_hour(level: int) -> int | None:
    """Returns the hour (Manila time) when this level gets early access. None = no early access."""
    if level >= 10:
        return 0   # immediate — sees it as soon as uploaded
    elif level == 9:
        return 8  # 8 AM
    elif level == 8:
        return 12  # 12 PM
    elif level == 7:
        return 16  # 4 PM
    else:
        return None  # Lv1-6 = website only, no bot access

def is_early_access_time(level: int) -> bool:
    """Returns True if current Manila time has passed this level's early access hour."""
    hour = get_early_access_hour(level)
    if hour is None:
        return False
    manila = pytz.timezone("Asia/Manila")
    return datetime.now(manila).hour >= hour

def get_time_until_early_access(level: int) -> tuple[int, int]:
    """Returns (hours, minutes) until this level's early access opens."""
    manila = pytz.timezone("Asia/Manila")
    now = datetime.now(manila)
    access_hour = get_early_access_hour(level)
    
    if access_hour is None:
        return 0, 0

    target = now.replace(hour=access_hour, minute=0, second=0, microsecond=0)
    if now >= target:
        return 0, 0  # already unlocked

    diff = target - now
    hours = int(diff.total_seconds() // 3600)
    mins = int((diff.total_seconds() % 3600) // 60)
    return hours, mins

def get_steam_tier(level: int) -> str:
    if level >= 10:
        return "legend"
    elif level == 9:
        return "early_sunday"
    elif level in (7, 8):
        return "early"
    else:
        return "public"

def is_public_drop_time() -> bool:
    """Returns True if Manila time is past 8 PM"""
    manila = pytz.timezone("Asia/Manila")
    return datetime.now(manila).hour >= 20

def is_sunday_manila() -> bool:
    manila = pytz.timezone("Asia/Manila")
    return datetime.now(manila).weekday() == 6

def is_sunday_noon_manila() -> bool:
    """Returns True if Sunday and past 12 PM Manila"""
    manila = pytz.timezone("Asia/Manila")
    now = datetime.now(manila)
    return now.weekday() == 6 and now.hour >= 12

def get_time_until_drop() -> tuple[int, int]:
    """Returns (hours, minutes) until next drop"""
    manila = pytz.timezone("Asia/Manila")
    now = datetime.now(manila)

    # Sunday before noon — show time until noon drop
    if now.weekday() == 6 and now.hour < 12:
        target = now.replace(hour=12, minute=0, second=0, microsecond=0)
        diff = target - now
        hours = int(diff.total_seconds() // 3600)
        mins = int((diff.total_seconds() % 3600) // 60)
        return hours, mins

    # Default: next 8 PM drop
    target = now.replace(hour=20, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    diff = target - now
    hours = int(diff.total_seconds() // 3600)
    mins = int((diff.total_seconds() % 3600) // 60)
    return hours, mins

async def release_daily_steam_accounts():
    """Just returns count of accounts that are now past their release_at time"""
    manila = pytz.timezone("Asia/Manila")
    now_iso = datetime.now(manila).astimezone(pytz.utc).isoformat()

    newly_visible = await _sb_get(
        "steamCredentials",
        **{
            "select": "*",
            "status": "eq.Available",
            "release_at": f"lte.{now_iso}",
        }
    ) or []

    return len(newly_visible)

async def get_steam_claims_today(chat_id: int) -> int:
    """How many accounts this user claimed today"""
    manila = pytz.timezone("Asia/Manila")
    today_start = datetime.now(manila).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(pytz.utc).isoformat()

    data = await _sb_get(
        "steam_claims",
        **{
            "chat_id": f"eq.{chat_id}",
            "claimed_at": f"gte.{today_start}",
            "select": "id",
        }
    ) or []
    return len(data)

async def claim_steam_account(
    chat_id: int, first_name: str, account_email: str, game_name: str = None
) -> bool:
    # ── Atomic lock prevents concurrent claims ──
    lock_key = f"claiming:{chat_id}:{account_email}"
    acquired = await redis_client.set(lock_key, 1, ex=15, nx=True)
    if not acquired:
        return False
    
    try:
        existing_claim = await _sb_get(
            "steam_claims",
            **{
                "chat_id": f"eq.{chat_id}",
                "account_email": f"eq.{account_email}",
                "select": "id",
            }
        ) or []

        if existing_claim:
            return False

        success = await _sb_post("steam_claims", {
            "chat_id": chat_id,
            "first_name": first_name,
            "account_email": account_email,
            "game_name": game_name or "Steam Account",
        })

        if success:
            await redis_client.setex(
                f"steam_claimed:{chat_id}:{account_email}",
                90000, "1"
            )
            asyncio.create_task(
                check_and_award_achievements(chat_id, first_name, action="steam_claim")
            )

        return success
    finally:
        await redis_client.delete(lock_key)

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
TOKEN        = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
OWNER_ID = int(os.getenv("OWNER_ID"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

MAINTENANCE_MESSAGE = (
    "🌿 <b>The Enchanted Clearing is currently under maintenance</b>\n\n"
    "The ancient trees are resting and being prepared for new wonders...\n\n"
    "We will be back very soon with a smoother experience!\n\n"
    "<i>Thank you for your patience, kind wanderer.</i> 🍃✨"
)

CACHE_TTL            = 300
NETFLIX_ITEMS_PER_PAGE = 8

DISPLAY_NAME_MAP = {
    "netflix": "Netflix Cookie",
    "prime": "PrimeVideo Cookie",
    "crunchyroll": "Crunchyroll Cookie", 
    "office": "Office Key",
    "windows": "Win Key",
    "win": "Win Key",
}

# ──────────────────────────────────────────────
# NEW HELPER: Fix Windows vs "win" key mismatch
# ──────────────────────────────────────────────
def normalize_view_category(cat: str) -> str:
    """Make sure "win" and "windows" always use the same Redis key"""
    c = str(cat).lower().strip()
    if c in ("win", "windows"):
        return "windows"
    return c  # office stays "office"

# ──────────────────────────────────────────────
# REFERRAL CONFIG
# ──────────────────────────────────────────────
MAX_REFERRALS_PER_DAY = 8
REFERRAL_XP = 25
NEW_USER_WELCOME_BONUS_IF_REFERRED = 40

# ──────────────────────────────────────────────
# COOLDOWN CONFIG
# ──────────────────────────────────────────────
COOLDOWN_SECONDS = {
    "view_windows": 10,
    "view_office": 10,
    "view_netflix": 10,
    "view_prime": 10,
    "view_crunchyroll": 10,
    "reveal_netflix": 18,
    "reveal_prime": 18,
    "reveal_crunchyroll": 18,
    "profile": 12,
    "clear": 20,
    "guidance": 30,
    "lore": 30,
    "general": 5,
    "steam_claim": 86400,
}

EVENT_BONUS_TIERS = {
    "netflix_double": {1: 2, 2: 6, 3: 6, 4: 8,  5: 8,  6: 14, 7: 18, 8: 24, 9: 30},
    "netflix_max":    {1: 5, 2: 8, 3: 8, 4: 12, 5: 12, 6: 16, 7: 22, 8: 28, 9: 35},
}

# ══════════════════════════════════════════════════════════════════════════════
# ATOMIC REVEAL CAP (Lua script — prevents TOCTOU race)
# ══════════════════════════════════════════════════════════════════════════════
REVEAL_CAP_SCRIPT = """
local key = KEYS[1]
local max_reveals = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])

local current = redis.call("INCR", key)

-- First time this key is created today → set proper TTL
if current == 1 then
    redis.call("EXPIRE", key, ttl)
end

-- Cap exceeded → rollback and return 0
if current > max_reveals then
    redis.call("DECR", key)
    return 0
end

return current
"""

limiter = Limiter(key_func=get_remote_address)


# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL SINGLETONS  (initialised in lifespan, never re-created)
# ══════════════════════════════════════════════════════════════════════════════
tg_app: Application = None
http:   httpx.AsyncClient = None
redis_client: aioredis.Redis = None
db_sem  = asyncio.Semaphore(10)   # cap concurrent Supabase calls
forest_memory: dict[int, list[int]] = {}  # fallback only — Redis is primary
BOT_START_TIME = datetime.now(pytz.utc)
BOT_USERNAME: str | None = None

# ──────────────────────────────────────────────
# LIFESPAN  (replaces @app.on_event)
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app, http, redis_client, BOT_READY

    http = httpx.AsyncClient(
        timeout=12.0,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )

    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    tg_app = Application.builder().token(TOKEN).build()
    await tg_app.initialize()
    await tg_app.start()

    await load_achievements_cache()
    print("✅ Achievement system loaded")

    asyncio.create_task(low_stock_monitor())

    global BOT_USERNAME
    me = await tg_app.bot.get_me()
    BOT_USERNAME = me.username
    print(f"✅ Bot username cached: @{BOT_USERNAME}")

    # ✅ Set AFTER everything is ready — only ONE yield
    BOT_READY = True
    await redis_client.setex("bot_just_restarted", 180, "1")
    print("✅ Bot fully ready")

    yield  # ← only one yield here

    # teardown
    BOT_READY = False
    await tg_app.stop()
    await tg_app.shutdown()
    await http.aclose()
    await redis_client.aclose()
    print("🌿 Bot shut down cleanly")

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

async def steam_daily_release_scheduler():
    """Runs forever — triggers steam releases at 8 PM daily and 12 PM on Sundays"""
    manila = pytz.timezone("Asia/Manila")

    while True:
        now = datetime.now(manila)

        # Build list of today's upcoming drop times
        candidates = []

        # 8 PM daily drop
        drop_8pm = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now < drop_8pm:
            candidates.append(("8pm", drop_8pm))

        # 12 PM Sunday drop
        if now.weekday() == 6:
            drop_noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
            if now < drop_noon:
                candidates.append(("noon", drop_noon))

        if candidates:
            next_label, next_drop = min(candidates, key=lambda x: x[1])
        else:
            # Both passed today — sleep until 8 PM tomorrow
            next_label = "8pm"
            next_drop = (now + timedelta(days=1)).replace(
                hour=20, minute=0, second=0, microsecond=0
            )

        wait_seconds = (next_drop - now).total_seconds()
        print(f"⏰ Next steam drop ({next_label}) in {wait_seconds/3600:.1f}h")
        await asyncio.sleep(wait_seconds)
        await asyncio.sleep(5) 
        count = await release_daily_steam_accounts()

        label_text = (
            "🌟 Sunday Noon Drop!"
            if next_label == "noon"
            else "🎮 Daily 8PM Drop!"
        )

        try:
            await tg_app.bot.send_message(
                OWNER_ID,
                f"🎮 <b>{label_text}</b>\n\n"
                f"✅ <b>{count}</b> accounts are now publicly visible.\n"
                f"🕗 Released at "
                f"{'12:00 PM' if next_label == 'noon' else '8:00 PM'} Manila time.\n\n"
                f"<i>Level 1–6 users can now see today's accounts on the website.</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass

        await asyncio.sleep(60)

async def low_stock_monitor():
    """Runs every hour — notifies owner when any service drops below threshold"""
    LOW_STOCK_THRESHOLD = 5
    CHECK_INTERVAL = 3600  # 1 hour

    SERVICE_EMOJIS = {
        "netflix": "🍿",
        "prime":   "🎥",
        "windows": "🪟",
        "office":  "📑",
        "steam":   "🎮",
    }

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            data = await get_vamt_data() or []

            counts: dict[str, int] = {}
            for item in data:
                svc = str(item.get("service_type", "")).lower().strip()
                
                # ── FIX: same normalization ──
                if any(x in svc for x in ("win", "windows")):
                    svc = "windows"
                elif "office" in svc:
                    svc = "office"
                elif "netflix" in svc:
                    svc = "netflix"
                elif "prime" in svc:
                    svc = "prime"
                
                if (str(item.get("status", "")).lower() == "active"
                        and int(item.get("remaining", 0)) > 0):
                    counts[svc] = counts.get(svc, 0) + 1

            steam_data = await _sb_get(
                "steamCredentials",
                **{"select": "status", "status": "eq.Available"}
            ) or []
            counts["steam"] = len(steam_data)

            alerts = []
            for svc, count in counts.items():
                if count <= LOW_STOCK_THRESHOLD:
                    emoji = SERVICE_EMOJIS.get(svc, "📦")
                    alerts.append(
                        f"{emoji} <b>{svc.title()}</b>: only <b>{count}</b> left"
                    )

            if alerts:
                alert_key = "low_stock_alerted"
                already_alerted = await redis_client.get(alert_key)
                if not already_alerted:
                    await redis_client.setex(alert_key, 21600, "1")
                    await tg_app.bot.send_message(
                        OWNER_ID,
                        "⚠️ <b>Low Stock Alert!</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n\n"
                        + "\n".join(alerts)
                        + "\n\n<i>Time to restock the forest! 🌿</i>",
                        parse_mode="HTML"
                    )
                    print(f"⚠️ Low stock alert sent: {alerts}")

        except Exception as e:
            print(f"🔴 Low stock monitor error: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# FASTAPI ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/keepalive")
async def health():
    return PlainTextResponse("🌿 Clyde's Enchanted Clearing is awake.")

@app.post("/")
@limiter.limit("60/minute")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if WEBHOOK_SECRET and token != WEBHOOK_SECRET:
        return PlainTextResponse("Forbidden", status_code=403)

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 1_000_000:
        return PlainTextResponse("Payload too large", status_code=413)

    data = await request.json()
    if not data:
        return PlainTextResponse("No data", status_code=400)
    background_tasks.add_task(process_update, data)
    return PlainTextResponse("OK")

# ──────────────────────────────────────────────
# NOTION WEBHOOK FROM SUPABASE (Minimal version)
# ──────────────────────────────────────────────
@app.post("/webhook/steam-live")
async def steam_to_notion_webhook(request: Request):
    secret = request.headers.get("X-Supabase-Secret")
    if secret != WEBHOOK_SECRET:
        return PlainTextResponse("Unauthorized", status_code=401)

    data = await request.json()
    record = data.get("record") or {}

    if record.get("action") != "live":
        return PlainTextResponse("Ignored - action not 'live'", status_code=200)

    # Minimal payload - only what you asked for
    success = await send_to_notion(
        title=record.get("game_name") or record.get("email") or "Steam Account",
        properties={
            "Game Name": {
                "title": [{"text": {"content": record.get("game_name") or "Unknown"}}]
            },
            "Email": {
                "rich_text": [{"text": {"content": record.get("email", "")}}]
            },
            "Password": {
                "rich_text": [{"text": {"content": record.get("password", "")}}]
            }
        },
        emoji="🎮"
    )

    return PlainTextResponse("OK" if success else "Notion failed", status_code=200)

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def _supabase_headers(extra: dict | None = None) -> dict:
    h = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    if extra:
        h.update(extra)
    return h

def _guidance_tier_text(category: str) -> str:
    tiers = [get_max_items(category, lvl) for lvl in range(1, 10)]
    parts = " • ".join(f"Lv{i+1}: {v}" for i, v in enumerate(tiers))
    return parts + " • Lv10+: Unlimited"


async def _sb_get(path: str, **params) -> list | dict | None:
    """Safe Supabase GET with semaphore + timeout."""
    async with db_sem:
        try:
            r = await asyncio.wait_for(
                http.get(f"{SUPABASE_URL}/rest/v1/{path}",
                         headers=_supabase_headers(), params=params),
                timeout=10.0,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"🔴 SB GET {path}: {e}")
            return None


async def _sb_post(path: str, payload: dict | list) -> bool:
    async with db_sem:
        try:
            r = await asyncio.wait_for(
                http.post(
                    f"{SUPABASE_URL}/rest/v1/{path}",
                    headers=_supabase_headers({
                        "Content-Type": "application/json",
                        "Prefer":       "return=minimal",
                    }),
                    json=payload,
                ),
                timeout=10.0,
            )
            # ✅ FIX: log the actual status so you can see what's happening
            print(f"🟢 SB POST {path}: status={r.status_code}")
            return r.status_code in (200, 201)
        except Exception as e:
            print(f"🔴 SB POST {path}: {e}")
            return False
        
async def _send_cold_start_notice(chat_id: int):
    """Tells user bot just woke up, waits until ready, then auto-deletes"""
    try:
        msg = await tg_app.bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ <b>The forest is waking up...</b>\n\n"
                "The bot just restarted. Your request will be ready in a moment.\n"
                "<i>Please wait a few seconds before tapping buttons.</i> 🍃"
            ),
            parse_mode="HTML"
        )

        # Wait until bot is actually ready (max 15 seconds)
        for _ in range(15):
            if BOT_READY:
                break
            await asyncio.sleep(1)

        await asyncio.sleep(2)  # small buffer after ready

        try:
            await tg_app.bot.delete_message(
                chat_id=chat_id,
                message_id=msg.message_id
            )
        except Exception:
            pass

    except Exception:
        pass

# ──────────────────────────────────────────────
# NOTION INTEGRATION (Ready for Render)
# ──────────────────────────────────────────────
async def send_to_notion(title: str, properties: dict, emoji: str = "🌿"):
    """Send data to your Notion database"""
    notion_token = os.getenv("NOTION_TOKEN")
    database_id = os.getenv("NOTION_DATABASE_ID")
    
    if not notion_token or not database_id:
        print("⚠️ Notion not configured — check Render environment variables")
        return False

    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    payload = {
        "parent": {"database_id": database_id},
        "icon": {"type": "emoji", "emoji": emoji},
        "properties": properties
    }

    try:
        r = await http.post(url, headers=headers, json=payload, timeout=10.0)
        if r.status_code in (200, 201):
            print(f"✅ [Notion] Saved: {title}")
            return True
        else:
            print(f"❌ Notion error {r.status_code}: {r.text}")
            return False
    except Exception as e:
        print(f"❌ Notion exception: {e}")
        return False

async def _sb_upsert(path: str, payload: dict | list, on_conflict: str, ignore_duplicates: bool = False) -> tuple[bool, int]:
    async with db_sem:
        try:
            data = [payload] if isinstance(payload, dict) else payload
            resolution = "ignore-duplicates" if ignore_duplicates else "merge-duplicates"

            r = await asyncio.wait_for(
                http.post(
                    f"{SUPABASE_URL}/rest/v1/{path}",
                    headers=_supabase_headers({
                        "Content-Type": "application/json",
                        # ✅ Use return=representation to detect actual inserts
                        "Prefer": f"resolution={resolution},return=representation",
                    }),
                    params={"on_conflict": on_conflict},
                    json=data,
                ),
                timeout=10.0,
            )

            print(f"🟢 SB UPSERT {path}: status={r.status_code}")

            if r.status_code not in (200, 201):
                return False, r.status_code

            # ✅ Check if any rows were actually returned (inserted)
            try:
                body = r.json()
                rows_inserted = len(body) if isinstance(body, list) else (1 if body else 0)
                # If rows returned = actual insert; if empty = duplicate was ignored
                actual_status = 201 if rows_inserted > 0 else 200
                return True, actual_status
            except Exception:
                return True, r.status_code

        except Exception as e:
            print(f"🔴 SB UPSERT {path} EXCEPTION: {e}")
            return False, 0

async def _sb_patch(path: str, payload: dict) -> bool:
    async with db_sem:
        try:
            r = await asyncio.wait_for(
                http.patch(
                    f"{SUPABASE_URL}/rest/v1/{path}",
                    headers=_supabase_headers({
                        "Content-Type": "application/json",
                        "Prefer":       "return=minimal",
                    }),
                    json=payload,
                ),
                timeout=10.0,
            )
            # ✅ FIX: 204 is success — don't call .json() on empty body
            return r.status_code in (200, 204)
        except Exception as e:
            print(f"🔴 SB PATCH {path}: {e}")
            return False

async def _sb_patch_check(path: str, payload: dict) -> tuple[bool, bool]:
    """
    Like _sb_patch but also returns whether any rows were actually updated.
    Returns (http_ok, rows_updated)
    """
    async with db_sem:
        try:
            r = await asyncio.wait_for(
                http.patch(
                    f"{SUPABASE_URL}/rest/v1/{path}",
                    headers=_supabase_headers({
                        "Content-Type": "application/json",
                        "Prefer": "return=representation",  # ✅ returns updated rows
                    }),
                    json=payload,
                ),
                timeout=10.0,
            )
            if r.status_code in (200, 204):
                body = r.json() if r.status_code == 200 else []
                rows_updated = len(body) > 0 if isinstance(body, list) else True
                return True, rows_updated
            return False, False
        except Exception as e:
            print(f"🔴 SB PATCH CHECK {path}: {e}")
            return False, False

async def _sb_delete(path: str) -> bool:
    async with db_sem:
        try:
            r = await asyncio.wait_for(
                http.delete(
                    f"{SUPABASE_URL}/rest/v1/{path}",
                    headers=_supabase_headers(),
                ),
                timeout=10.0,
            )
            return r.status_code in (200, 204)
        except Exception as e:
            print(f"🔴 SB DELETE {path}: {e}")
            return False


# ──────────────────────────────────────────────
# VAMT CACHE  (Redis)
# ──────────────────────────────────────────────
async def get_vamt_data() -> list | None:
    cached = await redis_client.get("vamt_cache")
    if cached:
        print("⚡ [CACHE] VAMT from Redis")
        return json.loads(cached)
 
    # ── Stampede lock: only ONE coroutine fetches at a time ──
    lock_key = "vamt_cache_lock"
    acquired = await redis_client.set(lock_key, 1, ex=10, nx=True)
 
    if not acquired:
        # Another coroutine is already fetching — wait and retry
        for _ in range(10):
            await asyncio.sleep(0.5)
            cached = await redis_client.get("vamt_cache")
            if cached:
                print("⚡ [CACHE] VAMT from Redis (waited for lock)")
                return json.loads(cached)
        # Fallback: fetch anyway if lock holder failed
        print("⚠️ Lock wait timed out — fetching directly")
 
    try:
        print("📡 [SUPABASE] Fetching fresh VAMT data…")
        data = await _sb_get("vamt_keys", select="*", order="service_type.asc")
        if data is not None:
            await redis_client.setex("vamt_cache", CACHE_TTL, json.dumps(data))
            print(f"✅ VAMT cached — {len(data)} items")
        else:
            print("🔴 Supabase returned nothing for vamt_keys")
        return data
    finally:
        await redis_client.delete(lock_key)

async def send_supabase_error(chat_id: int, query=None):
    """
    Shows a friendly error to the user when Supabase is unreachable.
    Works for both regular messages and callback queries.
    """
    msg = (
        "🌫️ <b>The forest is currently unreachable...</b>\n\n"
        "The ancient trees seem to be resting deeply.\n\n"
        "⚠️ Our database is temporarily unavailable.\n"
        "Please try again in a few moments.\n\n"
        "<i>The clearing will be back soon.</i> 🍃"
    )
    try:
        if query:
            # inside a button callback
            await query.answer(
                "🌫️ The forest is unreachable right now. Please try again shortly.",
                show_alert=True,
            )
        else:
            # regular command
            await tg_app.bot.send_message(chat_id, msg, parse_mode="HTML")
    except Exception:
        pass

async def handle_flushcache(chat_id: int):
    """Flush ALL major caches (VAMT + achievements + patrons + events)"""
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can refresh the forest cache.")
        return

    keys_to_flush = [
        "vamt_cache",           # Inventory cookies & keys
        "achievements_cache",   # All achievement data + GIF URLs
        "patrons_cache",        # Forest Patrons list
        "active_event",         # Current event banner
    ]

    try:
        deleted = await redis_client.delete(*keys_to_flush)
        # Force immediate reload of achievements (critical for GIF updates)
        await load_achievements_cache()

        await tg_app.bot.send_message(
            chat_id,
            f"✅ <b>Forest Cache Fully Cleared!</b>\n\n"
            f"🌿 Flushed <b>{deleted}</b> cache key(s)\n"
            f"• VAMT inventory\n"
            f"• Achievements (including GIFs)\n"
            f"• Patrons & active events\n\n"
            f"<i>All systems will now load fresh data from Supabase.</i> 🍃",
            parse_mode="HTML",
        )
    except Exception as e:
        await tg_app.bot.send_message(
            chat_id,
            f"❌ <b>Cache flush failed</b>\n\n"
            f"Error: {html.escape(str(e))}\n\n"
            f"Please check server logs.",
            parse_mode="HTML",
        )

async def broadcast_new_resources(added_counts: dict):
    if not added_counts or not any(added_counts.values()):
        return
 
    service_emojis = {
        "netflix": "🍿", "prime": "🎥", "windows": "🪟",
        "win": "🪟", "office": "📑", "steam": "🎮",
    }
    service_names = DISPLAY_NAME_MAP.copy()
    service_names["steam"] = "Steam Account"
 
    # Build per-service lines
    service_lines = {}
    for svc, count in added_counts.items():
        if count > 0:
            emoji = service_emojis.get(svc.lower(), "✨")
            name  = service_names.get(svc.lower(), svc.title())
            service_lines[svc.lower()] = (
                f"🌱 {emoji} +{count} {name}{'s' if count > 1 else ''} just added!"
            )
 
    if not service_lines:
        return
 
    # Fetch all users with pagination (handles >1000 users correctly)
    all_users = []
    limit, offset = 500, 0
    while True:
        batch = await _sb_get(
            "user_profiles",
            select="chat_id,notif_netflix,notif_prime,notif_windows,notif_steam",
            limit=limit,
            offset=offset,
            order="chat_id.asc"
        ) or []
        all_users.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
 
    print(f"📣 Broadcast → {len(all_users)} users")
 
    # Notification column map
    notif_col = {
        "netflix": "notif_netflix",
        "prime":   "notif_prime",
        "windows": "notif_windows",
        "win":     "notif_windows",
        "office":  "notif_windows",
        "steam":   "notif_steam",
    }
 
    # ── Metrics ──
    success_count = 0
    blocked_count = 0
    failed_count  = 0
 
    # ── Rate limit: Telegram allows ~30 msg/sec globally ──
    # We use a semaphore of 25 + 0.05s sleep = ~20 msg/sec (safe margin)
    sem = asyncio.Semaphore(25)
 
    async def safe_notify(user: dict):
        nonlocal success_count, blocked_count, failed_count
 
        uid = int(user.get("chat_id"))
 
        # Build lines this user is subscribed to
        user_lines = []
        for svc, line in service_lines.items():
            col = notif_col.get(svc)
            subscribed = user.get(col, True)
            if subscribed is not False:
                user_lines.append(line)
 
        if not user_lines:
            return  # user opted out of all relevant services
 
        text = "\n".join(user_lines)
 
        async with sem:
            # Small delay to stay under Telegram rate limit
            await asyncio.sleep(0.05 + random.uniform(0, 0.02))
 
            for attempt in range(2):  # 1 retry on flood
                try:
                    msg = await tg_app.bot.send_message(
                        chat_id=uid,
                        text=text,
                        parse_mode="HTML",
                        disable_notification=False,
                        disable_web_page_preview=True,
                        protect_content=True
                    )
                    success_count += 1
 
                    # Auto-delete after 30 seconds (non-blocking)
                    asyncio.create_task(_delayed_delete(uid, msg.message_id, delay=30))
                    return
 
                except Exception as e:
                    err = str(e).lower()
 
                    if "bot was blocked" in err or "user is deactivated" in err or "chat not found" in err:
                        blocked_count += 1
                        return  # no retry for blocked users
 
                    if "flood" in err or "too many requests" in err:
                        # Extract retry_after if available
                        retry_after = 5
                        try:
                            retry_after = int(str(e).split("retry after")[1].strip().split()[0])
                        except Exception:
                            pass
                        print(f"⏳ Flood wait {retry_after}s for {uid}")
                        await asyncio.sleep(retry_after + 1)
                        continue  # retry once
 
                    failed_count += 1
                    print(f"⚠️ Broadcast failed for {uid}: {e}")
                    return
 
    # Run all notifications concurrently (semaphore controls actual concurrency)
    await asyncio.gather(
        *(safe_notify(u) for u in all_users),
        return_exceptions=True
    )
 
    # Summary to owner
    try:
        await tg_app.bot.send_message(
            OWNER_ID,
            f"📣 <b>Broadcast Complete</b>\n\n"
            + "\n".join(f"• {line}" for line in service_lines.values())
            + f"\n\n👥 Total users: <b>{len(all_users)}</b>\n"
            f"✅ Delivered: <b>{success_count}</b>\n"
            f"🚫 Blocked/Inactive: <b>{blocked_count}</b>\n"
            f"❌ Failed: <b>{failed_count}</b>\n\n"
            f"<i>Users who opted out were skipped automatically.</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN KEY UPLOAD (TXT → Supabase vamt_keys)
# ══════════════════════════════════════════════════════════════════════════════
async def handle_uploadkeys_command(chat_id: int):
    """Command /uploadkeys — tells admin how to format the file"""
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can upload keys.")
        return

    msg = (
        "📤 <b>Upload Keys via TXT</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Send me a <b>.txt</b> file now.\n\n"
        "<b>Supported formats:</b>\n"
        "1. Simple (one key per line):\n"
        "<code>ABCDE-FGHIJ-KLMNO-PQRST-UVWXY\n"
        "NETFLIX-COOKIE-1234567890</code>\n\n"
        "2. Advanced (with details):\n"
        "<code>KEY_HERE|50|windows|Windows 11 Pro\n"
        "cookie123|999|netflix|Netflix Premium</code>\n\n"
        "• Lines starting with # are ignored\n"
        "• remaining defaults to 999 if not specified\n"
        "• service_type examples: windows, office, netflix, prime, steam\n\n"
        "<i>Just send the .txt file now — I’ll import it instantly.</i> 🍃"
    )
    await tg_app.bot.send_message(chat_id, msg, parse_mode="HTML")

def _extract_crunchyroll_region(content: str) -> str:
    # Match Country/Region label
    for label in ("Country", "Region"):
        match = re.search(
            rf'(?:[-–•]\s*)?{label}\s*[:\-]\s*(.+)',
            content, re.IGNORECASE
        )
        if match:
            raw = match.group(1).strip()
            raw = re.split(r'[|\n\r(]', raw)[0].strip()
            if raw:
                if len(raw) == 2 and raw.isalpha():
                    return raw.upper()
                return raw.title()

    # Fallback: locale string like en-US
    locale_match = re.search(r'\b[a-z]{2}-([A-Z]{2})\b', content)
    if locale_match:
        return locale_match.group(1).upper()

    return ""

def _extract_netflix_plan(content: str) -> str:
    # Match any "Plan: <value>" regardless of what the value is
    match = re.search(
        r'(?:[-–•]\s*)?Plan\s*[:\-]\s*(.+)',
        content, re.IGNORECASE
    )
    if match:
        raw = match.group(1).strip()
        raw = re.split(r'[|\n\r(]', raw)[0].strip()
        if raw:
            return raw.title()
    return "Premium"

def _extract_netflix_region(content: str) -> str:
    # Match any "Country: <value>" or "Region: <value>" regardless of what the value is
    for label in ("Country", "Region"):
        match = re.search(
            rf'(?:[-–•]\s*)?{label}\s*[:\-]\s*(.+)',
            content, re.IGNORECASE
        )
        if match:
            raw = match.group(1).strip()
            raw = re.split(r'[|\n\r(]', raw)[0].strip()
            if raw:
                # If it's a 2-letter code, return as-is uppercased
                if len(raw) == 2 and raw.isalpha():
                    return raw.upper()
                # Otherwise return the full value cleaned up
                return raw.title()

    # Fallback: locale string like en-US, es-CL, pt-BR
    locale_match = re.search(r'\b[a-z]{2}-([A-Z]{2})\b', content)
    if locale_match:
        known = {
            "US","GB","BR","PH","IN","CA","AU","DE","FR","MX","ZA","NL",
            "ES","IT","JP","KR","SG","MY","ID","TH","TR","AR","CO","CL",
            "PE","PL","SE","NO","DK","FI","PT","BE","CH","AT","NZ","HK",
            "TW","NG","EG","SA","AE","IL","CZ","HU","RO","SK","UA","VN",
        }
        code = locale_match.group(1).upper()
        if code in known:
            return code

    # Fallback: preferredLocale cookie value
    locale_cookie = re.search(r'preferredLocale\s+([a-z]{2,3}[-_][A-Z]{2})', content)
    if locale_cookie:
        parts = re.split(r'[-_]', locale_cookie.group(1))
        if len(parts) == 2:
            return parts[1].upper()

    return ""

def _extract_prime_region(content: str) -> str:
    # Match any "Country: <value>" or "Region: <value>"
    for label in ("Country", "Region"):
        match = re.search(
            rf'(?:[-–•]\s*)?{label}\s*[:\-]\s*(.+)',
            content, re.IGNORECASE
        )
        if match:
            raw = match.group(1).strip()
            raw = re.split(r'[|\n\r(]', raw)[0].strip()
            if raw:
                if len(raw) == 2 and raw.isalpha():
                    return raw.upper()
                return raw.title()

    # Fallback: ubid cookie name
    ubid_match = re.search(r'ubid-([a-z]+)', content.lower())
    if ubid_match:
        suffix = ubid_match.group(1)
        suffix_map = {
            "main":   "US", "acbde":  "DE", "acbfr":  "FR",
            "acbuk":  "GB", "acbca":  "CA", "acbin":  "IN",
            "acbau":  "AU", "acbmx":  "MX", "acbbr":  "BR",
            "acbpl":  "PL", "acbnl":  "NL", "acbes":  "ES",
            "acbit":  "IT", "acbjp":  "JP", "acbsg":  "SG",
            "acbae":  "AE", "acbza":  "ZA", "acbtr":  "TR",
            "acbsa":  "SA", "acbeg":  "EG",
        }
        if suffix in suffix_map:
            return suffix_map[suffix]

    # Fallback: Amazon domain
    domain_match = re.search(
        r'amazon\.(co\.uk|co\.jp|com\.au|com\.br|com\.mx|com\.tr|de|fr|it|es|pl|nl|ca|in|sg|ae|sa)',
        content.lower()
    )
    if domain_match:
        domain_map = {
            "co.uk": "GB", "co.jp": "JP", "com.au": "AU",
            "com.br": "BR", "com.mx": "MX", "com.tr": "TR",
            "de": "DE", "fr": "FR", "it": "IT", "es": "ES",
            "pl": "PL", "nl": "NL", "ca": "CA", "in": "IN",
            "sg": "SG", "ae": "AE", "sa": "SA",
        }
        return domain_map.get(domain_match.group(1), "")

    # Fallback: locale string
    locale_match = re.search(r'\b[a-z]{2}-([A-Z]{2})\b', content)
    if locale_match:
        return locale_match.group(1).upper()

    return ""

def detect_service_type(content: str, filename: str) -> tuple[str, str]:
    content_lower = content.lower()
    filename_lower = filename.lower()

    if (
        "crunchyroll.com" in content_lower or
        "crunchyroll" in filename_lower
    ):
        region = _extract_crunchyroll_region(content)
        service_type = f"Crunchyroll {region}".strip() if region else "Crunchyroll"
        display_name = "Crunchyroll Cookie"
        return service_type, display_name

    if (
        "netflixid" in content_lower or
        "netflix.com" in content_lower or
        "netflix" in filename_lower
    ):
        plan   = _extract_netflix_plan(content)
        region = _extract_netflix_region(content)
        
        if not region:
            fname_region = re.search(r'[_\-]([A-Z]{2})[_\-]', filename)
            if fname_region:
                region = fname_region.group(1).upper()
        
        service_type = f"Netflix {plan} {region}".strip() if region else f"Netflix {plan}"
        display_name = "Netflix Cookie"  # ← keep this as the enum-safe value
        return service_type, display_name

    if (
        "primevideo.com" in content_lower or
        "prime" in filename_lower or
        "ubid-" in content_lower
    ):
        region = _extract_prime_region(content)
        service_type = f"Prime Video {region}".strip() if region else "Prime Video"
        display_name = "PrimeVideo Cookie"
        return service_type, display_name

    if "office" in filename_lower or "office" in content_lower:
        return "office", "Office Key"

    if "windows" in filename_lower or "win" in filename_lower or "windows" in content_lower:
        return "windows", "Win Key"

    return "unknown", "Netflix Cookie"

async def parse_and_import_keys(content: str, filename: str = "unknown.txt") -> tuple[int, int, int, list[str], dict]:
    imported = 0
    skipped = 0
    duplicates = 0
    errors = []
    added_counts = Counter()

    detected_service, detected_display = detect_service_type(content, filename)

    # ══════════════════════════════════════
    # FORMAT 1 — Cookie file with header block
    # (any stealer format, any service)
    # ══════════════════════════════════════
    is_cookie_file = (
        "NetflixId" in content or
        "SecureNetflixId" in content or
        "crunchyroll.com" in content.lower() or
        "hacked by" in content.lower() or
        "stealer" in content.lower() or
        ("# copy the cookies from here" in content.lower()) or
        # Amazon/Prime detection
        ("amazon.com" in content.lower() and "ubid-main" in content.lower()) or
        ("primevideo.com" in content.lower())
    )

    if is_cookie_file:
        # Try to extract only the cookie block
        if "# copy the cookies from here" in content.lower():
            # Find the marker regardless of exact spacing/casing
            match = re.split(r"#\s*copy the cookies from here\s*:?", content, flags=re.IGNORECASE)
            cookie_block = match[-1].strip() if len(match) > 1 else content.strip()
        else:
            # Try to extract only Netscape-format cookie lines
            # These always start with a domain like .netflix.com or .amazon.com
            COOKIE_DOMAINS = (
                ".netflix.com",
                "www.netflix.com",
                ".primevideo.com",
                "www.primevideo.com",
                ".www.primevideo.com",
                ".www.crunchyroll.com",
                "www.crunchyroll.com",
                ".sso.crunchyroll.com",
                "sso.crunchyroll.com",
                ".crunchyroll.com",
                "static.crunchyroll.com",
            )
            cookie_lines = [
                line for line in content.splitlines()
                if line.strip() and line.strip().lower().startswith(COOKIE_DOMAINS)
            ]
            if cookie_lines:
                cookie_block = "\n".join(cookie_lines)
            else:
                # No recognizable cookie lines found — fallback to full content
                cookie_block = content.strip()

        if not cookie_block:
            errors.append(f"❌ {filename}: Cookie block was empty after extraction")
            return 0, 1, errors, {}

        payload = {
            "key_id":       cookie_block,
            "remaining":    999,
            "service_type": detected_service,
            "status":       "active",
            "display_name": detected_display,
        }

        ok, status_code = await _sb_upsert("vamt_keys", payload, on_conflict="key_id", ignore_duplicates=True)

        if ok and status_code == 201:
            imported += 1
            added_counts[detected_service] += 1
        elif ok and status_code == 200:
            duplicates += 1
        else:
            skipped += 1
            errors.append(f"❌ Rejected by Supabase: {cookie_block[:30]}")

        return imported, skipped, duplicates, errors, dict(added_counts)

    # ══════════════════════════════════════
    # FORMAT 2 — JSON format
    # {"email": "x@x.com", "password": "pass"}
    # ══════════════════════════════════════
    stripped = content.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                data = [data]

            for i, item in enumerate(data):
                # Try to build a cookie-like string from key:value pairs
                key_id = "\n".join(f"{k}: {v}" for k, v in item.items())
                payload = {
                    "key_id":       key_id,
                    "remaining":    int(item.get("remaining", 999)),
                    "service_type": item.get("service_type", detected_service),
                    "status":       "active",
                    "display_name": detected_display,
                }

                success = await _sb_upsert("vamt_keys", payload, on_conflict="key_id", ignore_duplicates=True)

                if success:
                    imported += 1
                    added_counts[item.get("service_type", detected_service)] += 1
                else:
                    skipped += 1
                    errors.append(f"❌ JSON item {i+1} skipped: Supabase rejected")
        except Exception as e:
            errors.append(f"❌ JSON parse failed: {str(e)[:100]}")
            skipped += 1
        return imported, skipped, errors, dict(added_counts)

    # ══════════════════════════════════════
    # FORMAT 3 — Key:Value block format
    # Email: x@x.com
    # Password: pass123
    # Plan: Premium
    # ══════════════════════════════════════
    lines = [l.strip() for l in content.splitlines() if l.strip() and not l.startswith("#")]
    is_keyvalue = sum(1 for l in lines if ":" in l) > len(lines) * 0.6  # >60% lines have ":"

    if is_keyvalue and not any("|" in l for l in lines):
        # Group into blocks separated by blank lines
        blocks = []
        current = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                if current:
                    blocks.append(current)
                    current = []
            elif not line.startswith("#"):
                current.append(line)
        if current:
            blocks.append(current)

        if not blocks:
            blocks = [lines]  # treat whole file as one block

        for i, block in enumerate(blocks):
            key_id = "\n".join(block)
            if not key_id:
                continue
            payload = {
                "key_id":       key_id,
                "remaining":    1,
                "service_type": detected_service,
                "status":       "active",
                "display_name": detected_display,
            }

            success = await _sb_upsert("vamt_keys", payload, on_conflict="key_id", ignore_duplicates=True)

            if success:
                imported += 1
                added_counts[detected_service] += 1
            else:
                skipped += 1
                errors.append(f"❌ Block {i+1} skipped: Supabase rejected")
        return imported, skipped, errors, dict(added_counts)

    # ══════════════════════════════════════
    # FORMAT 4 — CSV format
    # key_id,service,remaining
    # ABCDE,windows,50
    # ══════════════════════════════════════
    if "," in content and "\n" in content:
        csv_lines = [l.strip() for l in content.splitlines() if l.strip() and not l.startswith("#")]
        first_line = csv_lines[0].lower() if csv_lines else ""
        has_header = "key" in first_line or "service" in first_line or "remaining" in first_line
        data_lines = csv_lines[1:] if has_header else csv_lines

        SERVICE_MAP = {
            "netflix":    ("netflix",  "Netflix Cookie"),
            "prime":      ("prime",    "PrimeVideo Cookie"),
            "primevideo": ("prime",    "PrimeVideo Cookie"),
            "office":     ("office",   "Office Key"),
            "windows":    ("windows",  "Win Key"),
            "win":        ("windows",  "Win Key"),
        }

        all_pipe = all("|" not in l and "," in l for l in data_lines[:5])
        if all_pipe or has_header:
            for line_num, line in enumerate(data_lines, 1):
                parts = [p.strip() for p in line.split(",")]
                if not parts[0]:
                    continue
                key_id    = parts[0]
                remaining = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 999
                raw_svc   = parts[2].lower() if len(parts) > 2 else detected_service
                svc, disp = SERVICE_MAP.get(raw_svc, (detected_service, detected_display))

                payload = {
                    "key_id":       key_id,
                    "remaining":    remaining,
                    "service_type": svc,
                    "status":       "active",
                    "display_name": disp,
                }

                success = await _sb_upsert("vamt_keys", payload, on_conflict="key_id", ignore_duplicates=True)

                if success:
                    imported += 1
                    added_counts[svc] += 1
                else:
                    skipped += 1
                    errors.append(f"❌ CSV line {line_num} skipped: {key_id[:30]}")
            return imported, skipped, errors, dict(added_counts)

    # ══════════════════════════════════════
    # FORMAT 5 — Pipe separated OR plain keys
    # KEY|remaining|service|display
    # or just: ABCDE-FGHIJ-KLMNO
    # ══════════════════════════════════════
    SERVICE_MAP = {
        "netflix":    ("netflix",  "Netflix Cookie"),
        "prime":      ("prime",    "PrimeVideo Cookie"),
        "primevideo": ("prime",    "PrimeVideo Cookie"),
        "office":     ("office",   "Office Key"),
        "windows":    ("windows",  "Win Key"),
        "win":        ("windows",  "Win Key"),
    }

    for line_num, raw_line in enumerate(content.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue

        try:
            if "|" in line:
                parts        = [p.strip() for p in line.split("|")]
                key_id       = parts[0]
                remaining    = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 999
                raw_svc      = parts[2].lower() if len(parts) > 2 else detected_service
                svc, disp    = SERVICE_MAP.get(raw_svc, (detected_service, detected_display))
            else:
                key_id    = line
                remaining = 999
                svc       = detected_service
                disp      = detected_display

            if not key_id:
                continue

            payload = {
                "key_id":       key_id,
                "remaining":    remaining,
                "service_type": svc,
                "status":       "active",
                "display_name": disp,
            }

            success = await _sb_upsert("vamt_keys", payload, on_conflict="key_id", ignore_duplicates=True)

            if success:
                imported += 1
                added_counts[svc] += 1
            else:
                skipped += 1
                errors.append(
                    f"❌ Line {line_num} skipped\n"
                    f"   Key: {key_id[:30]}...\n"
                    f"   Service: {svc}\n"
                    f"   Reason: Supabase upsert rejected"
                )

        except Exception as e:
            skipped += 1
            errors.append(
                f"❌ Line {line_num} skipped\n"
                f"   Raw: {line[:30]}...\n"
                f"   Error: {str(e)[:80]}"
            )

    return imported, skipped, errors, dict(added_counts)

# ──────────────────────────────────────────────
# VIEW LIBRARY (newest updated first + empty name safe)
# ──────────────────────────────────────────────
async def view_notion_steam_library(chat_id: int, page: int = 0, query=None, filter_mode: str = "all"):
    """Improved UX - Detailed View with inline edit buttons"""
    database_id = os.getenv("NOTION_DATABASE_ID")
    if not database_id:
        await tg_app.bot.send_message(chat_id, "❌ Notion database not configured.")
        return

    headers = {
        "Authorization": f"Bearer {os.getenv('NOTION_TOKEN')}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    try:
        r = await http.post(
            f"https://api.notion.com/v1/databases/{database_id}/query",
            headers=headers,
            json={"page_size": 50}
        )
        results = r.json().get("results", [])
    except Exception as e:
        await tg_app.bot.send_message(chat_id, f"❌ Could not load Notion data: {e}")
        return

    if not results:
        await tg_app.bot.send_message(chat_id, "🌫️ No games found.")
        return

    has_update = []
    no_update = []

    for item in results:
        props = item.get("properties", {})

        # Game Name
        title_list = props.get("Game Name", {}).get("title", [])
        game_name = title_list[0].get("text", {}).get("content", "Untitled Game") if title_list else "Untitled Game"

        # Availability
        avail_prop = props.get("Availability", {})
        if avail_prop.get("type") == "multi_select":
            availability = ", ".join([m.get("name", "") for m in avail_prop.get("multi_select", [])]) or "—"
        else:
            availability = avail_prop.get("select", {}).get("name", "—") or "—"

        # Display
        display = props.get("Display", {}).get("formula", {}).get("string", "—")

        # Updated time (emoji column)
        updated = "—"
        for prop_name, prop in props.items():
            if prop and prop.get("type") == "formula":
                formula_str = prop.get("formula", {}).get("string", "").strip()
                if formula_str and any(k in formula_str.lower() for k in ["ago", "just now", "minute", "hour", "day"]):
                    updated = formula_str
                    break

        item_data = {
            "page_id": item["id"],
            "game_name": game_name,
            "availability": availability,
            "display": display,
            "updated": updated,
            "sort_key": parse_updated_for_sort(updated)
        }

        if updated != "—":
            has_update.append(item_data)
        else:
            no_update.append(item_data)

    # Sorting: Newest → Oldest → No update at bottom
    has_update.sort(key=lambda x: x["sort_key"], reverse=True)
    no_update.sort(key=lambda x: x["game_name"].lower())

    all_items = has_update + no_update

    # Simple filter support
    if filter_mode == "available":
        all_items = [g for g in all_items if "Available" in g["availability"]]
    elif filter_mode == "expired":
        all_items = [g for g in all_items if "Expired" in g["availability"]]
    elif filter_mode == "recent":
        all_items = [g for g in all_items if g["updated"] != "—" and ("day" in g["updated"].lower() or "hour" in g["updated"].lower() or "just now" in g["updated"].lower())]

    start = page * 8
    page_items = all_items[start:start + 8]

    # Build message
    text = "📋 <b>Notion STEAM LIBRARY</b>\n━━━━━━━━━━━━━━━━━━\n\n"

    # Top filter row
    text += "🔎 <b>Filter:</b> All • ✅ Available • ❌ Expired • 🔥 Recently Updated\n\n"

    buttons = []

    for item in page_items:
        text += f"🎮 <b>{item['game_name']}</b>\n"
        text += f"   {'✅' if 'Available' in item['availability'] else '❌'} <b>{item['availability']}</b>\n"
        text += f"   Display: {item['display']}\n"
        text += f"   Updated: {item['updated']}\n\n"

        # Inline edit buttons
        buttons.append([
            InlineKeyboardButton("✅ Mark Available", callback_data=f"quick_set|{item['page_id']}|Available"),
            InlineKeyboardButton("❌ Mark Expired",  callback_data=f"quick_set|{item['page_id']}|Expired")
        ])

    # Navigation + extras
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("↼ Previous", callback_data=f"notion_page_{page-1}"))
    if start + 8 < len(all_items):
        nav.append(InlineKeyboardButton("Next ⇀", callback_data=f"notion_page_{page+1}"))

    buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔄 Refresh Library", callback_data="view_notion_steam_refresh")])
    buttons.append([InlineKeyboardButton("⬅️ Back to Caretaker Menu", callback_data="caretaker_menu")])

    final_text = text + f"━━━━━━━━━━━━━━━━━━\n📄 Page {page+1} of {((len(all_items)-1)//8)+1} • {len(all_items)} games • Last synced just now"

    # Edit existing message when possible (clean UX)
    if query and query.message:
        try:
            await query.message.edit_text(
                text=final_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            return
        except:
            pass

    await tg_app.bot.send_message(
        chat_id=chat_id,
        text=final_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_document(update: Update):
    """Handle document uploads (.txt or .zip) - Fast single file + Batch for multiple"""
    message = update.message
    chat_id = message.chat_id

    # ── USER CUSTOM PROFILE GIF UPLOAD (anyone can do this) ──
    waiting = await redis_client.get(f"waiting_for_logo:{chat_id}")
    if waiting:
        await redis_client.delete(f"waiting_for_logo:{chat_id}")

        can_change, hours_left = await can_change_profile_gif(chat_id)
        if not can_change:
            await message.reply_text(
                f"🌿 You can only change your profile logo once per week.\n"
                f"Come back in {hours_left} hours!"
            )
            return

        document = message.document
        animation = message.animation
        photo = message.photo  # ← ADD THIS

        ACCEPTED_MIME_TYPES = (
            "image/gif",
            "image/png",
            "image/jpeg",
            "image/jpg",
            "image/webp",
        )
        ACCEPTED_EXTENSIONS = (".gif", ".png", ".jpg", ".jpeg", ".webp")

        file_id = None
        is_animated = False

        if animation:
            # Telegram GIF/animation
            file_id = animation.file_id
            is_animated = True

        elif photo:
            # Compressed photo sent directly
            file_id = photo[-1].file_id  # highest resolution

        elif document:
            fname = (document.file_name or "").lower()
            mime = document.mime_type or ""

            is_accepted = (
                mime in ACCEPTED_MIME_TYPES or
                any(fname.endswith(ext) for ext in ACCEPTED_EXTENSIONS)
            )

            if not is_accepted:
                await redis_client.setex(f"waiting_for_logo:{chat_id}", 600, "1")
                msg = await send_animated_translated(
                    chat_id=chat_id,
                    animation_url=LOADING_GIF,
                    caption=(
                        "❌ <b>Unsupported file type, wanderer!</b>\n\n"
                        "🌿 Please send one of the following:\n"
                        "• <b>GIF</b> — animated logo\n"
                        "• <b>PNG / JPG / WEBP</b> — static logo\n\n"
                        "<i>The forest only accepts these formats... 🍃</i>"
                    ),
                )
                await asyncio.sleep(3)
                try:
                    await msg.delete()
                except Exception:
                    pass
                return

            if document.file_size and document.file_size > 10 * 1024 * 1024:
                await message.reply_text("❌ File is too big. Maximum 10 MB.")
                return

            file_id = document.file_id
            is_animated = mime == "image/gif" or fname.endswith(".gif")

        else:
            await redis_client.setex(f"waiting_for_logo:{chat_id}", 600, "1")
            msg = await send_animated_translated(
                chat_id=chat_id,
                animation_url=LOADING_GIF,
                caption=(
                    "❌ <b>Please send an image or GIF, wanderer!</b>\n\n"
                    "🌿 Supported: <b>GIF, PNG, JPG, WEBP</b>\n"
                    "<i>The forest is waiting... 🍃</i>"
                ),
            )
            await asyncio.sleep(3)
            try:
                await msg.delete()
            except Exception:
                pass
            return
        
        if file_id:
            success = await set_user_profile_gif(chat_id, file_id)
            if success:
                await redis_client.setex(f"profile_gif_cooldown:{chat_id}", 7*24*3600, "1")

                profile = await get_user_profile(chat_id)
                current_count = (profile.get("profile_gif_changes") or 0) + 1
                await _sb_patch(
                    f"user_profiles?chat_id=eq.{chat_id}",
                    {"profile_gif_changes": current_count}
                )

                logo_type = "animated GIF" if is_animated else "image"  # ← dynamic label

                # Use send_photo for static, send_animation for GIF
                if is_animated:
                    await send_animated_translated(
                        chat_id=chat_id,
                        animation_url=file_id,
                        caption=f"✨ <b>Your profile {logo_type} has been saved!</b>\n\n"
                                "It will now appear every time you view your profile 🌿\n\n"
                                "<i>Try /profile to see it live.</i>",
                    )
                else:
                    await tg_app.bot.send_photo(
                        chat_id=chat_id,
                        photo=file_id,
                        caption=f"✨ <b>Your profile {logo_type} has been saved!</b>\n\n"
                                "It will now appear every time you view your profile 🌿\n\n"
                                "<i>Try /profile to see it live.</i>",
                        parse_mode="HTML",
                    )
            else:
                await message.reply_text("❌ Failed to save your logo. Please try again.")
        return
    
    # ── GAME LOGO TXT UPLOAD ──
    if chat_id == OWNER_ID:
        waiting_gamelogo = await redis_client.get(f"waiting_for_gamelogo:{chat_id}")
        if waiting_gamelogo and chat_id == OWNER_ID:
            await redis_client.delete(f"waiting_for_gamelogo:{chat_id}")
            
            document = message.document
            if document and (document.file_name or "").lower().endswith(".txt"):
                try:
                    file = await document.get_file()
                    file_bytes = await file.download_as_bytearray()
                    content = file_bytes.decode("utf-8", errors="replace")
                    
                    await handle_game_logo_txt_upload(chat_id, content, document.file_name or "logos.txt")
                    return
                except Exception as e:
                    await message.reply_text(f"❌ Failed to process file: {e}")
                    return
            else:
                await message.reply_text("❌ Please send a **.txt** file for game logos.")
                return
    # ── KEY UPLOAD: Owner only ──
    if chat_id != OWNER_ID:
        return

    document = message.document
    if not document:
        return

    filename = document.file_name or "unknown.txt"
    mime = document.mime_type or ""

    # ── Accept only .txt and .zip ──
    is_txt = mime == "text/plain" or filename.lower().endswith(".txt")
    is_zip = mime in ("application/zip", "application/x-zip-compressed") or filename.lower().endswith(".zip")

    if not is_txt and not is_zip:
        await message.reply_text("❌ Only .txt or .zip files are allowed.")
        return

    # ── File size limit for stability ──
    if document.file_size and document.file_size > 50 * 1024 * 1024:  # 50 MB
        await message.reply_text("❌ File is too large. Maximum allowed is 50 MB.")
        return

    try:
        file = await document.get_file()
        file_bytes = await file.download_as_bytearray()
    except Exception as e:
        await message.reply_text(f"❌ Failed to download file: {e}")
        return

    loading = await send_animated_translated(
        chat_id=chat_id,
        animation_url=LOADING_GIF,
        caption="🌿 <i>Unpacking the ancient scrolls...</i>",
    )

    # ══════════════════════════════════════
    # ZIP HANDLING (unchanged - bulk)
    # ══════════════════════════════════════
    if is_zip:
        total_imported = 0
        total_skipped = 0
        total_duplicates = 0
        all_errors = []
        processed_files = []
        total_added = Counter()

        try:
            with zipfile.ZipFile(io.BytesIO(bytes(file_bytes))) as zf:
                txt_files = [
                    name for name in zf.namelist()
                    if name.endswith(".txt") and not name.startswith("__MACOSX")
                ]
                if not txt_files:
                    await safe_edit(loading,"❌ No .txt files found inside the ZIP.")
                    return

                await safe_edit(loading,f"🌿 <i>Found {len(txt_files)} scrolls inside the archive...</i>")

                for txt_name in txt_files:
                    try:
                        raw_bytes = zf.read(txt_name)
                        content = raw_bytes.decode("utf-8", errors="replace")
                        basename = txt_name.split("/")[-1]
                        imported, skipped, errors, file_added = await parse_and_import_keys(content, basename)
                        total_added.update(file_added)
                        total_imported += imported
                        total_skipped += skipped
                        total_duplicates += duplicates
                        all_errors.extend(errors)
                        icon = "✅" if imported > 0 else "⚠️"
                        processed_files.append(f"{icon} <code>{basename}</code> → +{imported} imported")
                    except Exception as e:
                        total_skipped += 1
                        all_errors.append(f"❌ {txt_name}: {str(e)[:80]}")
                        processed_files.append(f"❌ <code>{txt_name.split('/')[-1]}</code> → Failed")

        except zipfile.BadZipFile:
            await safe_edit(loading,"❌ Invalid or corrupted ZIP file.")
            return
        except Exception as e:
            await safe_edit(loading,f"❌ Failed to extract ZIP: {e}")
            return

        await redis_client.delete("vamt_cache")
        if sum(total_added.values()) > 0:
            asyncio.create_task(broadcast_new_resources(dict(total_added)))

        # Build result message
        files_summary = "\n".join(processed_files[:20])
        result = (
            f"✅ <b>ZIP Import Complete!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 <b>Files processed:</b> {len(txt_files)}\n"
            f"🌱 <b>Total Imported:</b> {total_imported}\n"
            f"⚠️ <b>Total Duplicates:</b> {total_duplicates}\n"
            f"📦 <b>Total Skipped:</b> {total_skipped}\n\n"
            + (f"🧹 Cache refreshed!" if cache_cleared else f"📌 Cache unchanged — nothing new was added.")
        )

        if len(processed_files) > 20:
            result += f"\n<i>...and {len(processed_files) - 20} more files</i>"

        if len(result) <= 1000:
            await safe_edit(loading, result)
        else:
            await safe_edit(loading,
                f"✅ <b>ZIP Import Complete!</b>\n\n"
                f"📦 {len(txt_files)} files · 🌱 {total_imported} imported · ⚠️ {total_skipped} skipped",
            )
            # Send detailed breakdown
            chunk = ""
            for line in processed_files:
                addition = line + "\n"
                if len(chunk) + len(addition) > 4000:
                    await tg_app.bot.send_message(OWNER_ID, chunk, parse_mode="HTML")
                    chunk = addition
                else:
                    chunk += addition
            if chunk:
                await tg_app.bot.send_message(OWNER_ID, chunk, parse_mode="HTML")

        if all_errors:
            await tg_app.bot.send_message(OWNER_ID, "🔴 <b>ZIP Upload Issues:</b>\n\n" + "\n".join(all_errors[:10]), parse_mode="HTML")
        return

    # ══════════════════════════════════════
    # SINGLE TXT FILE - FAST PATH (NEW)
    # ══════════════════════════════════════
    try:
        content = file_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        await safe_edit(loading, f"❌ Failed to read file: {e}")
        return

    detected_service, detected_display = detect_service_type(content, filename)
    imported, skipped, duplicates, errors, added_counts = await parse_and_import_keys(content, filename)

    cache_cleared = False
    if imported > 0:
        await redis_client.delete("vamt_cache")
        cache_cleared = True

    await redis_client.delete("vamt_cache")     

    if imported > 0:
        temp_key = f"pending_broadcast:{chat_id}"
        
        existing = await redis_client.get(temp_key)
        if existing:
            existing_counts = json.loads(existing)
            for k, v in added_counts.items():
                existing_counts[k] = existing_counts.get(k, 0) + v
            added_counts = existing_counts

        await redis_client.setex(temp_key, 15, json.dumps(added_counts))

        asyncio.create_task(_debounced_broadcast(chat_id))

    # Immediate result for single file (no 4-second wait)
    result = (
        f"✅ <b>Import Complete!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📄 <b>File:</b> <code>{filename}</code>\n"
        f"🔍 <b>Detected as:</b> {detected_display}\n"
        f"🏷️ <b>Name:</b> {detected_service}\n\n"
        f"🌱 <b>Imported:</b> {imported}\n"
        f"⚠️ <b>Duplicates:</b> {duplicates}\n"
        f"📦 <b>Skipped/Failed:</b> {skipped}\n\n"
        + (f"🧹 Cache refreshed!" if cache_cleared else f"📌 Cache unchanged — nothing new was added.")
    )

    if skipped > 0 and errors:
        result += f"\n\n⚠️ <b>Skipped Details:</b>\n" + "\n".join([f"• {err}" for err in errors[:6]])

    try:
        await safe_edit(loading, result)
    except Exception:
        # Fallback if edit fails
        await tg_app.bot.send_message(chat_id, result, parse_mode="HTML")

    # Optional: small success feedback
    if imported > 0:
        await asyncio.sleep(0.8)
        await tg_app.bot.send_message(
            chat_id,
            f"✨ Successfully imported <b>{imported}</b> key(s) from <code>{filename}</code>",
            parse_mode="HTML"
        )

# ──────────────────────────────────────────────
# MAINTENANCE_MODE CONFIG
# ──────────────────────────────────────────────
async def get_maintenance_mode() -> bool:
    val = await redis_client.get("maintenance_mode")
    return val == "1"

async def set_maintenance_mode(enabled: bool):
    await redis_client.set("maintenance_mode", "1" if enabled else "0")

async def has_seen_winoffice_guide(chat_id: int) -> bool:
    # Returns True if this user has already seen the guide
    return bool(await redis_client.get(f"seen_winoffice_guide:{chat_id}"))

async def mark_winoffice_guide_seen(chat_id: int):
    # Permanently marks guide as seen — no TTL, never expires
    await redis_client.set(f"seen_winoffice_guide:{chat_id}", 1)

# ──────────────────────────────────────────────
# ONBOARDING TUTORIAL (3-step flow for new users)
# ──────────────────────────────────────────────
async def can_change_profile_gif(chat_id: int) -> tuple[bool, int]:
    """
    Returns (can_change: bool, hours_remaining: int)
    Cooldown = 7 days (1 change per week)
    """
    key = f"profile_gif_cooldown:{chat_id}"
    ttl = await redis_client.ttl(key)
    
    if ttl > 0:
        hours_left = ttl // 3600
        return False, max(1, hours_left)
    
    return True, 0

async def has_completed_onboarding(chat_id: int) -> bool:
    if await redis_client.get(f"onboarding_done:{chat_id}"):
        return True
    profile = await get_user_profile(chat_id)
    if profile and profile.get("onboarding_completed"):
        await redis_client.set(f"onboarding_done:{chat_id}", 1)
        return True
    return False

async def mark_onboarding_complete(chat_id: int):
    await redis_client.set(f"onboarding_done:{chat_id}", 1)
    await _sb_patch(f"user_profiles?chat_id=eq.{chat_id}", {
        "onboarding_completed": True
    })

async def send_onboarding_step(chat_id: int, first_name: str, step: int):
    """Send the correct onboarding step. Steps 1–3."""

    await redis_client.setex(f"onboarding_step:{chat_id}", 3600, step)

    if step == 1:
        caption = (
            f"🌿 <b>Welcome to Clyde's Enchanted Clearing, {html.escape(first_name)}!</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🍃 This bot gives you access to:\n\n"
            "• 🪟 <b>Windows & Office</b> activation keys\n"
            "• 🍿 <b>Netflix</b> premium cookies\n"
            "• 🎥 <b>PrimeVideo</b> premium cookies\n"
            "• 🎮 <b>Steam</b> accounts\n\n"
            "Everything is free. No tricks. Just a peaceful forest. 🌲\n\n"
            "<i>Step 1 of 3 — Tap below to continue your journey.</i>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Next → How XP & Levels Work 🌱", callback_data="onboarding_step_2")],
            [InlineKeyboardButton("⏩ Skip Tour", callback_data="onboarding_skip")],
        ])

    elif step == 2:
        caption = (
            "✨ <b>The Forest Energy System</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Every action in the clearing earns you <b>XP</b>:\n\n"
            "• View any list → <b>+8 XP</b>\n"
            "• Reveal a Netflix cookie → <b>+14 XP</b>\n"
            "• Spin the Wheel of Whispers → <b>+13 to +75 XP</b>\n\n"
            "🌅 <b>Daily Login Streak Bonus:</b>\n\n"
            "• Day 1 → <b>+10 XP</b> 🌅\n"
            "• Day 2 → <b>+12 XP</b> 🌱\n"
            "• Day 3-6 → <b>+20 XP</b> 🔥\n"
            "• Day 7-13 → <b>+30 XP</b> ⚡\n"
            "• Day 14-29 → <b>+40 XP</b> 🌟\n"
            "• Day 30-59 → <b>+50 XP</b> 🏆\n"
            "• Day 60+ → <b>+60 XP</b> 🌠\n\n"
            "⭐ <b>Why level up?</b>\n"
            "Higher levels unlock more items per day!\n\n"
            "• Level 1 → 5 Netflix cookies/day\n"
            "• Level 5 → 14 Netflix cookies/day\n"
            "• Level 9 → 35 Netflix cookies/day\n\n"
            "<i>Step 2 of 3 — Almost there!</i>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("← Back", callback_data="onboarding_step_1"),
            InlineKeyboardButton("Next → Your First Action 🎯", callback_data="onboarding_step_3")],
            [InlineKeyboardButton("⏩ Skip Tour", callback_data="onboarding_skip")],
        ])

    elif step == 3:
        caption = (
            "🎯 <b>Ready to Begin, Wanderer?</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Here's what the forest caretaker recommends:\n\n"
            "1️⃣ Tap <b>Check Forest Inventory</b>\n"
            "   → Browse today's available keys & cookies\n\n"
            "2️⃣ <b>Reveal a cookie</b> you need\n"
            "   → +14 XP earned instantly!\n\n"
            "3️⃣ Come back <b>every day</b>\n"
            "   → Streak bonuses stack up to +30 XP/day\n\n"
            "🎁 <b>Bonus:</b> You'll earn <b>+15 XP</b> just for completing this tour!\n\n"
            "<i>Step 3 of 3 — The clearing awaits you.</i> 🍃✨"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("← Back", callback_data="onboarding_step_2")],
            [InlineKeyboardButton("🌿 Enter the Enchanted Clearing! (+15 XP)", callback_data="onboarding_complete")],
        ])

    else:
        return

    await send_animated_translated(
        chat_id=chat_id,
        animation_url=ONBOARDING_GIF,
        caption=caption,
        reply_markup=keyboard,
    )

# ──────────────────────────────────────────────
# BOT CONFIG
# ──────────────────────────────────────────────
async def get_bot_config() -> dict:
    data = await _sb_get("bot_info", select="*", limit=1)
    if data is None:
        return {"current_version": "1.3.2", "last_updated": "Not set yet"}
    return data[0] if data else {"current_version": "1.3.2", "last_updated": "Not set yet"}


async def set_bot_info(new_version: str, custom_datetime: str, chat_id: int):
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can change forest info.")
        return

    payload = {
        "id": 1,
        "current_version": new_version.strip(),
        "last_updated": custom_datetime.strip()
    }

    # Use _sb_upsert instead of _sb_post
    ok = await _sb_upsert("bot_info", payload, on_conflict="id")

    if ok:
        msg = (
            f"✅ <b>Forest info updated!</b>\n\n"
            f"📜 Version: <b>{new_version}</b>\n"
            f"🔄 Last Updated: <b>{custom_datetime}</b>"
        )
    else:
        msg = "❌ Failed to save. Check server logs for details."

    await tg_app.bot.send_message(chat_id, msg, parse_mode="HTML")

# ──────────────────────────────────────────────
# UPTIME
# ──────────────────────────────────────────────
async def get_bot_uptime() -> str:
    try:
        config = await get_bot_config()
        raw = config.get("last_updated", "").strip()
        if not raw or raw == "Not set yet":
            return "Not set yet 🌱"

        # Just grab the date part before any separator
        date_only = raw.split("·")[0].strip()
        dt = datetime.strptime(date_only, "%B %d, %Y")
        dt = pytz.timezone("Asia/Manila").localize(dt)

        delta = datetime.now(pytz.timezone("Asia/Manila")) - dt
        total_h = delta.days * 24 + delta.seconds // 3600
        mins = (delta.seconds % 3600) // 60
        return f"{total_h}h {mins}m"
    except Exception as e:
        return f"Unknown ({e})"

# ══════════════════════════════════════════════════════════════════════════════
# USER PROFILE
# ══════════════════════════════════════════════════════════════════════════════
async def get_user_profile(chat_id: int) -> dict | None:
    data = await _sb_get(
        "user_profiles",
        **{
            "chat_id": f"eq.{chat_id}",
            "select": (
                "*,has_seen_menu,created_at,total_xp_earned,"
                "windows_views,office_views,netflix_views,netflix_reveals,"
                "prime_views,prime_reveals,times_cleared,guidance_reads,"
                "lore_reads,profile_views,"
                "total_wheel_spins,wheel_xp_earned,legendary_spins,"
                "onboarding_completed,"
                "notif_netflix,notif_prime,notif_windows,notif_steam,"
                "profile_gif_id,profile_gif_changes,"
                "all_slots_bonus,windows_views_bonus,netflix_reveals_bonus,"
                "prime_reveals_bonus,daily_reveals_bonus,"
                "active_title,"
                "crunchyroll_views,crunchyroll_reveals,crunchyroll_reveals_bonus"
            ),
        },
    )
    if data is None:
        print(f"🔴 get_user_profile failed for {chat_id}")
    return data[0] if data else None


async def update_has_seen_menu(chat_id: int):
    await _sb_patch(f"user_profiles?chat_id=eq.{chat_id}", {"has_seen_menu": True})


async def update_last_active(chat_id: int, action: str = "browsing"):
    # In update_last_active, also store what they were doing:
    await _sb_patch(
        f"user_profiles?chat_id=eq.{chat_id}",
        {
            "last_active": datetime.now(pytz.utc).isoformat(),
        }
    )
    # Notify owner — only once per 15 min per user (avoid spam)
    notify_key = f"online_notif:{chat_id}"
    just_came_online = await redis_client.set(notify_key, 1, ex=900, nx=True)
    if just_came_online and chat_id != OWNER_ID:
        profile = await get_user_profile(chat_id)
        name = profile.get("first_name", "Someone") if profile else "Someone"
        level = profile.get("level", 1) if profile else 1
        title = get_level_title(level)
        manila = pytz.timezone("Asia/Manila")
        time_str = datetime.now(manila).strftime("%I:%M %p")
        try:
            await tg_app.bot.send_message(
                OWNER_ID,
                f"🟢 <b>{name}</b> is online right now!\n"
                f"🏷️ {title} • Level {level}\n"
                f"🕒 {time_str} Manila time\n"
                f"🆔 <code>{chat_id}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass

# Helper to turn "9 days ago" into a number for sorting
def parse_updated_for_sort(updated_str: str) -> int:
    """Higher number = more recent. Fixed so any hour beats any day."""
    if not updated_str or updated_str.strip() in ("—", "-", "", None):
        return -9999999

    s = updated_str.lower().strip()

    if "just now" in s or "minute" in s:
        return 1_000_000

    # Days ago
    match = re.search(r'(\d+)\s*day', s)
    if match:
        days = int(match.group(1))
        return 100_000 - (days * 1440)

    # Hours ago - boosted base so 13hrs > 1 day
    match = re.search(r'(\d+)\s*hour', s)
    if match:
        hours = int(match.group(1))
        return 200_000 - (hours * 60)   # ← this is the key change

    # Minutes fallback
    match = re.search(r'(\d+)\s*min', s)
    if match:
        return 300_000 - int(match.group(1))

    return 0

# ══════════════════════════════════════════════════════════════════════════════
# LEVELING SYSTEM
# ══════════════════════════════════════════════════════════════════════════════
def get_cumulative_xp_for_level(target_level: int) -> int:
    levels = [0, 0, 240, 560, 980, 1520, 2180, 2980, 3950, 5050, 6300]
    if target_level < len(levels):
        return levels[target_level]
    return levels[-1] + (target_level - 10) * 1300


def get_level_title(level: int) -> str:
    titles = {
        1: "🌱 Young Sprout",
        2: "🌿 Forest Sprout",
        3: "🍃 Gentle Wanderer",
        4: "🌳 Woodland Explorer",
        5: "🌲 Whispering Wanderer",
        6: "🪵 Tree Guardian",
        7: "🌌 Mist Walker",
        8: "✨ Enchanted Keeper",
        9: "🌠 Ancient Soul",
        10: "🌟 Eternal Guardian",
    }
    return titles.get(level, f"🌟 Legend {level}")


def get_max_items(category: str, level: int, event: dict | None = None) -> int:
    level = int(level)

    if category in ("win", "windows", "office"):
        tiers = {1: 4, 2: 5, 3: 6, 4: 7, 5: 9, 6: 11, 7: 13, 8: 16, 9: 20}
        return tiers.get(level, 999)

    if category == "prime":
        tiers = {1: 4, 2: 5, 3: 6, 4: 7, 5: 9, 6: 11, 7: 13, 8: 16, 9: 20}
        return tiers.get(level, 999)

    if category == "netflix":
        if event:
            bonus_type = event.get("bonus_type", "").strip()
            if bonus_type in EVENT_BONUS_TIERS:
                tiers = EVENT_BONUS_TIERS[bonus_type]
                return tiers.get(level, 999)
            
        # Normal tiers
        tiers = {1: 5, 2: 7, 3: 9, 4: 11, 5: 14, 6: 17, 7: 21, 8: 25, 9: 35}
        return tiers.get(level, 999)

    if category == "crunchyroll":
        tiers = {1: 4, 2: 5, 3: 6, 4: 7, 5: 9, 6: 11, 7: 13, 8: 16, 9: 20}
        return tiers.get(level, 999)

    return 0

# ══════════════════════════════════════════════════════════════════════════════
# DAILY CAP HELPERS (Level-based)
# ══════════════════════════════════════════════════════════════════════════════

def get_max_daily_reveals(level: int, service_type: str) -> int:
    """Daily REVEAL limit for Netflix and Prime cookies"""
    level = int(level)
    if service_type == "netflix":
        tiers = {1: 15, 2: 20, 3: 25, 4: 32, 5: 40, 6: 50, 7: 65, 8: 85, 9: 110, 10: 999}
        return tiers.get(level, 999)
    else:  # prime
        tiers = {1: 12, 2: 16, 3: 20, 4: 26, 5: 32, 6: 40, 7: 52, 8: 68, 9: 90, 10: 999}
        return tiers.get(level, 999)


def get_max_daily_views(level: int, category: str) -> int:
    """Daily VIEW limit for Windows and Office keys"""
    level = int(level)
    if category in ("win", "windows", "office"):
        tiers = {1: 12, 2: 16, 3: 20, 4: 25, 5: 32, 6: 40, 7: 50, 8: 65, 9: 90, 10: 999}
        return tiers.get(level, 999)
    return 999

# ──────────────────────────────────────────────
# EVENT COUNTDOWN HELPER (reused in menu + viewevent)
# ──────────────────────────────────────────────
async def _debounced_broadcast(chat_id: int):
    """Wait 3 seconds and send only ONE combined notification"""
    await asyncio.sleep(3.0)
    temp_key = f"pending_broadcast:{chat_id}"
    
    data = await redis_client.get(temp_key)
    if not data:
        return
        
    added_counts = json.loads(data)
    await redis_client.delete(temp_key)
    
    if any(added_counts.values()):
        await broadcast_new_resources(added_counts)

async def _delayed_delete(chat_id: int, message_id: int, delay: int = 30):
    """Delete a message after a delay without blocking the broadcast loop."""
    await asyncio.sleep(delay)
    try:
        await tg_app.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass
 

async def get_event_countdown(event: dict) -> str:
    """Returns a nice countdown string like '⏳ Ends in 14h 32m' or empty if expired."""
    if not event or not event.get("event_date"):
        return ""
    
    try:
        manila = pytz.timezone("Asia/Manila")
        event_dt = datetime.strptime(event.get("event_date", "").strip(), "%B %d, %Y")
        event_dt = manila.localize(event_dt)
        expires_at = event_dt + timedelta(days=1)
        diff = expires_at - datetime.now(manila)
        
        if diff.total_seconds() > 0:
            hours = int(diff.total_seconds() // 3600)
            mins = int((diff.total_seconds() % 3600) // 60)
            return f"\n⏳ <b>Ends in {hours}h {mins}m</b>"
        else:
            return "\n⏳ <b>Event has ended</b>"
    except Exception:
        return ""  # fallback if date format is broken
    
def make_attempts_bar(used: int, total: int = 3, length: int = 9) -> str:
    available = total - used
    blocks_per_slot = length // total
    bar = ""
    for i in range(total):
        if i < available:
            if available == 3:
                emoji = "🟩"
            elif available == 2:
                emoji = "🟨"
            else:
                emoji = "🟥"
            bar += emoji * blocks_per_slot
        else:
            bar += "⬜" * blocks_per_slot
    return bar

def create_progress_bar(current_xp: int, required_xp: int, length: int = 12) -> str:
    if required_xp <= 0:
        return "🟩" * length
    pct     = min(current_xp / required_xp, 1.0)
    filled  = int(pct * length)
    bar     = "🟩" * filled + "⬜" * (length - filled)
    return f"[{bar}] {int(pct * 100)}%"

def create_daily_progress_bar(used: int, max_allowed: int, length: int = 10) -> str:
    """Depleting bar: starts full green, drains to yellow then red as limit is hit."""
    if max_allowed <= 0:
        return "🟩" * length

    remaining = max(0, max_allowed - used)
    pct_remaining = remaining / max_allowed
    filled = round(pct_remaining * length)
    empty = length - filled

    if pct_remaining > 0.5:
        fill_emoji = "🟩"   # plenty left — green
    elif pct_remaining > 0.25:
        fill_emoji = "🟨"   # getting low — yellow
    else:
        fill_emoji = "🟥"   # almost empty — red

    return fill_emoji * filled + "⬜" * empty

# ══════════════════════════════════════════════════════════════════════════════
# ANTI-ABUSE  (Redis-backed)
# ══════════════════════════════════════════════════════════════════════════════
MAX_ACTIONS_PER_MINUTE = 20  # raised from 8 — normal browsing hits ~8-10

async def check_xp_cooldown(chat_id: int, action: str) -> bool:
    """Per-action XP cooldown — prevents farming same button repeatedly.
    Returns True if XP is allowed, False if still on cooldown."""
    seconds = COOLDOWN_SECONDS.get(action, 8)
    key = f"xpcd:{chat_id}:{action}"
    if await redis_client.exists(key):
        return False
    await redis_client.setex(key, seconds, 1)
    return True

async def check_rate_limit(chat_id: int) -> bool:
    key = f"rl:{chat_id}"
    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.expire(key, 60)
    results = await pipe.execute()
    return results[0] <= MAX_ACTIONS_PER_MINUTE

async def try_consume_reveal_cap(chat_id: int, service_type: str) -> tuple[bool, int]:
    """
    Atomic daily REVEAL cap with FULL debug logging + error handling
    """
    try:
        profile = await get_user_profile(chat_id)
        user_level = profile.get("level", 1) if profile else 1
        all_bonus = profile.get("all_slots_bonus", 0)
        daily_reveals_bonus = profile.get("daily_reveals_bonus", 0)
        if service_type == "netflix":
            service_bonus = profile.get("netflix_reveals_bonus", 0)
        elif service_type == "crunchyroll":
            service_bonus = profile.get("crunchyroll_reveals_bonus", 0)
        else:
            service_bonus = profile.get("prime_reveals_bonus", 0)
        max_reveals = get_max_daily_reveals(user_level, service_type) + service_bonus + all_bonus + daily_reveals_bonus

        key = f"daily_reveals:{chat_id}:{service_type}"

        manila = pytz.timezone("Asia/Manila")
        now = datetime.now(manila)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        ttl = int((midnight - now).total_seconds())

        print(f"[DEBUG REVEAL CAP] {chat_id} | {service_type} | Lv{user_level} | Max={max_reveals} | TTL={ttl}s")

        result = await redis_client.eval(
            REVEAL_CAP_SCRIPT,
            1, key, max_reveals, ttl
        )

        print(f"[DEBUG REVEAL RESULT] Raw Lua result = {result}")

        if result == 0:
            current = int(await redis_client.get(key) or 0)
            remaining = max(0, max_reveals - current)
            print(f"[DEBUG] CAP REACHED. Currently used = {current}")
            return False, remaining
        else:
            used_after = int(result)
            remaining = max(0, max_reveals - used_after)
            print(f"[DEBUG] REVEAL SUCCESS → Used now = {used_after} | Remaining = {remaining}")
            return True, remaining
    except Exception as e:
        print(f"🔴 CRITICAL Redis error in try_consume_reveal_cap({chat_id}, {service_type}): {e}")
        import traceback
        traceback.print_exc()
        return False, 0

async def get_remaining_reveals_and_views(chat_id: int) -> dict:
    try:
        profile = await get_user_profile(chat_id)
        if not profile:
            return {"netflix": 0, "prime": 0, "windows": 0, "office": 0, "crunchyroll": 0}
        
        level = profile.get("level", 1)
        remaining = {}

        all_bonus = profile.get("all_slots_bonus", 0)
        daily_reveals_bonus = profile.get("daily_reveals_bonus", 0)
        for svc in ["netflix", "prime", "crunchyroll"]:
            try:
                key = f"daily_reveals:{chat_id}:{svc}"
                used = int(await redis_client.get(key) or 0)
                service_bonus = profile.get(f"{svc}_reveals_bonus", 0)
                max_allowed = get_max_daily_reveals(level, svc) + service_bonus + all_bonus + daily_reveals_bonus
                left = max(0, max_allowed - used)
                remaining[svc] = left
                print(f"[DEBUG REMAINING] {svc} → used={used}/{max_allowed} → left={left}")
            except Exception as e:
                print(f"🔴 Redis error reading {svc} remaining: {e}")
                remaining[svc] = 0

        windows_bonus = profile.get("windows_views_bonus", 0)
        for cat in ["windows", "office"]:
            try:
                key = f"daily_views:{chat_id}:{cat}"
                used = int(await redis_client.get(key) or 0)
                max_allowed = get_max_daily_views(level, cat) + windows_bonus + all_bonus
                left = max(0, max_allowed - used)
                remaining[cat] = left
                print(f"[DEBUG REMAINING] {cat} → used={used}/{max_allowed} → left={left}")
            except Exception as e:
                print(f"🔴 Redis error reading {cat} remaining: {e}")
                remaining[cat] = 0

        return remaining

    except Exception as e:
        print(f"🔴 Critical error in get_remaining_reveals_and_views: {e}")
        return {"netflix": 0, "prime": 0, "windows": 0, "office": 0, "crunchyroll": 0}

async def try_consume_view_cap(chat_id: int, category: str) -> tuple[bool, int]:
    """
    Atomic daily VIEW cap with FULL debug logging + error handling
    """
    try:
        profile = await get_user_profile(chat_id)
        user_level = profile.get("level", 1) if profile else 1
        windows_bonus = profile.get("windows_views_bonus", 0)
        all_bonus = profile.get("all_slots_bonus", 0)
        # daily_reveals_bonus intentionally excluded — only applies to cookie reveals
        max_views = get_max_daily_views(user_level, category) + windows_bonus + all_bonus
        
        # ←←← FIXED: Use normalized key
        normalized = normalize_view_category(category)
        key = f"daily_views:{chat_id}:{normalized}"
        
        manila = pytz.timezone("Asia/Manila")
        now = datetime.now(manila)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        ttl = int((midnight - now).total_seconds())

        print(f"[DEBUG VIEW CAP] {chat_id} | {category} (→{normalized}) | Lv{user_level} | Max={max_views} | TTL={ttl}s")

        result = await redis_client.eval(
            REVEAL_CAP_SCRIPT,
            1, key, max_views, ttl
        )
        print(f"[DEBUG VIEW RESULT] Raw Lua result = {result}")

        if result == 0:
            current = int(await redis_client.get(key) or 0)
            remaining = max(0, max_views - current)
            print(f"[DEBUG] VIEW CAP REACHED for {category}. Currently used = {current}")
            return False, remaining
        else:
            used_after = int(result)
            remaining = max(0, max_views - used_after)
            print(f"[DEBUG] VIEW SUCCESS → Used now = {used_after} | Remaining = {remaining}")
            return True, remaining
    except Exception as e:
        print(f"🔴 CRITICAL Redis error in try_consume_view_cap({chat_id}, {category}): {e}")
        import traceback
        traceback.print_exc()
        return False, 0

# ══════════════════════════════════════════════════════════════════════════════
# DAILY BONUS HELPER  (called inside add_xp, not exposed separately)
# ══════════════════════════════════════════════════════════════════════════════
async def _check_daily_bonus(chat_id: int, first_name: str, profile: dict) -> tuple[int, str]:
    manila = pytz.timezone("Asia/Manila")
    now = datetime.now(manila)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    ttl = int((midnight - now).total_seconds())
    key = f"daily_bonus:{chat_id}"

    claimed = await redis_client.set(key, 1, ex=ttl, nx=True)
    if not claimed:
        return 0, ""

    streak = await calculate_streak(chat_id)

    if streak >= 60:
        bonus, label = 60, "🌠 Ancient Soul Bonus!"
    elif streak >= 30:
        bonus, label = 50, "🏆 Devoted Wanderer Bonus!"
    elif streak >= 14:
        bonus, label = 40, "🌟 Forest Regular Bonus!"
    elif streak >= 7:
        bonus, label = 30, "⚡ Week Warrior Bonus!"
    elif streak >= 3:
        bonus, label = 20, "🔥 3-Day Streak Bonus!"
    elif streak >= 2:
        bonus, label = 12, "🌱 Getting Started!"
    else:
        bonus, label = 10, "🌅 Daily Login Bonus!"

    return bonus, label
# ──────────────────────────────────────────────
# NOTIFICATION PREFERENCE HELPERS
# ──────────────────────────────────────────────
async def get_daily_bonus_notif(chat_id: int) -> bool:
    """Daily bonus pref lives in Redis. Default = ON (missing key = ON)"""
    val = await redis_client.get(f"notif:daily_bonus:{chat_id}")
    return val != "0"

async def toggle_daily_bonus_notif(chat_id: int) -> bool:
    """Toggle and return NEW state"""
    current = await get_daily_bonus_notif(chat_id)
    new_state = not current
    await redis_client.set(f"notif:daily_bonus:{chat_id}", "1" if new_state else "0")
    return new_state

async def toggle_service_notif(chat_id: int, service: str) -> bool:
    """
    Toggle notif_netflix / notif_prime / notif_windows / notif_steam
    directly in user_profiles. Returns NEW state.
    """
    profile = await get_user_profile(chat_id)
    if not profile:
        return True

    col_map = {
        "netflix": "notif_netflix",
        "prime":   "notif_prime",
        "windows": "notif_windows",
        "steam":   "notif_steam",
    }
    col = col_map.get(service)
    if not col:
        return True

    current = profile.get(col, True)
    new_state = not current
    await _sb_patch(
        f"user_profiles?chat_id=eq.{chat_id}",
        {col: new_state}
    )
    return new_state

# ══════════════════════════════════════════════════════════════════════════════
# REFERRAL SYSTEM (new dedicated table)
# ══════════════════════════════════════════════════════════════════════════════
async def get_referral_link(chat_id: int) -> str:
    """Returns clean referral link using cached bot username"""
    global BOT_USERNAME
    if not BOT_USERNAME:
        me = await tg_app.bot.get_me()
        BOT_USERNAME = me.username
        print(f"✅ Bot username auto-cached: @{BOT_USERNAME}")
    
    return f"https://t.me/{BOT_USERNAME}?start=ref_{chat_id}"

async def store_pending_referral(new_chat_id: int, referrer_id: int) -> bool:
    if referrer_id == new_chat_id:
        return False
    
    # Check if already referred before
    existing = await _sb_get(
        "referrals",
        **{"referred_id": f"eq.{new_chat_id}", "select": "id"}
    )
    if existing:
        return False  # already has a referrer, don't overwrite
    
    await redis_client.setex(f"pending_ref:{new_chat_id}", 604800, str(referrer_id))
    
    # Notify referrer their link was clicked
    try:
        await tg_app.bot.send_message(
            referrer_id,
            "🌲 <b>Someone clicked your invite link!</b>\n\n"
            "🌿 Waiting for them to complete onboarding...\n"
            "<i>You'll earn +25 XP once they join! 🍃</i>",
            parse_mode="HTML"
        )
    except Exception:
        pass
    
    return True

async def get_pending_referrer(chat_id: int) -> int | None:
    key = f"pending_ref:{chat_id}"
    val = await redis_client.get(key)
    if val:
        await redis_client.delete(key)
        return int(val)
    return None


async def award_referral_bonus(referrer_id: int, referred_id: int, new_user_name: str):
    # Daily limit
    key = f"referral_daily:{referrer_id}"
    manila = pytz.timezone("Asia/Manila")
    now = datetime.now(manila)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    ttl = int((midnight - now).total_seconds())

    count = await redis_client.get(key) or "0"
    if int(count) >= MAX_REFERRALS_PER_DAY:
        return

    # Record in referrals table + award XP
    profile = await get_user_profile(referrer_id)
    if not profile:
        return

    old_xp = profile.get("xp", 0)
    old_level = profile.get("level", 1)
    new_xp = old_xp + REFERRAL_XP
    new_count = (profile.get("referral_count") or 0) + 1

    # Level-up logic
    new_level = old_level
    while True:
        needed = get_cumulative_xp_for_level(new_level + 1)
        if new_xp < needed:
            break
        new_level += 1

    # Update user_profiles
    ok = await _sb_patch(f"user_profiles?chat_id=eq.{referrer_id}", {
        "xp": new_xp,
        "level": new_level,
        "referral_count": new_count,
        "total_xp_earned": (profile.get("total_xp_earned") or 0) + REFERRAL_XP,
        "last_active": datetime.now(pytz.utc).isoformat(),
    })

    if ok:
        await redis_client.setex(key, ttl, int(count) + 1)
        await _log_xp_history(
            referrer_id, "Forest Wanderer", "referral", REFERRAL_XP,
            old_xp, new_xp, old_level, new_level
        )

        # Beautiful notification
        try:
            await tg_app.bot.send_message(
                referrer_id,
                f"🌲 <b>A new wanderer has joined the clearing!</b>\n\n"
                f"✨ <b>+{REFERRAL_XP} Forest Energy</b>\n"
                f"🌿 Your referral count is now <b>{new_count}</b>\n\n"
                f"Welcome, {html.escape(new_user_name)}! 🍃",
                parse_mode="HTML"
            )
        except Exception:
            pass

async def _try_award_referral(chat_id: int):
    lock_key = f"ref_awarding:{chat_id}"
    lock = await redis_client.set(lock_key, 1, ex=300, nx=True)
    if not lock:
        return

    try:
        referral = await _sb_get(
            "referrals",
            **{"referred_id": f"eq.{chat_id}", "awarded": "eq.false", "select": "*"}
        )
        if not referral:
            return

        ref = referral[0]
        referrer_id = ref["referrer_id"]

        marked, rows_updated = await _sb_patch_check(
            f"referrals?referred_id=eq.{chat_id}&awarded=eq.false",
            {"awarded": True, "awarded_at": datetime.now(pytz.utc).isoformat()}
        )
        if not marked or not rows_updated:
            return
        
        new_name = (await get_user_profile(chat_id) or {}).get("first_name", "Wanderer")
        await award_referral_bonus(referrer_id, chat_id, new_name)
    finally:
        await redis_client.delete(lock_key)

# ──────────────────────────────────────────────
# MILESTONES VALUE
# ──────────────────────────────────────────────
MILESTONES = [100, 250, 500, 1000, 2500, 5000, 10000, 25000]

MILESTONE_LABELS = {
    100:   ("🌱", "Your roots are growing strong!"),
    250:   ("🌿", "The forest begins to notice you!"),
    500:   ("🍃", "You're becoming a true wanderer!"),
    1000:  ("🌟", "A thousand steps in the clearing!"),
    2500:  ("✨", "The ancient trees bow to you!"),
    5000:  ("🌠", "You are truly one with the forest!"),
    10000: ("🏆", "A legend walks among the trees!"),
    25000: ("🪐", "The forest spirits are in awe!"),
}

# ══════════════════════════════════════════════════════════════════════════════
# XP ENGINE
# ══════════════════════════════════════════════════════════════════════════════
_XP_TABLE = {
    "guidance": 10, 
    "lore": 10, 
    "view_windows": 8,
    "view_office": 8,
    "view_netflix": 8,
    "view_crunchyroll": 8,
    "view_prime": 8,
    "reveal_netflix": 14,
    "reveal_prime": 14,
    "reveal_crunchyroll": 14,
    "profile": 6,
    "clear": 6,
    "daily_bonus": 0,
    "wheel_spin": 0,
    "onboarding_complete": 15,
    "onboarding_skip": 0,
    "steam_claim": 0,
}

_STAT_FIELD = {
    "view_windows": "windows_views",
    "view_office": "office_views",
    "view_netflix": "netflix_views",
    "view_prime": "prime_views",
    "view_crunchyroll": "crunchyroll_views",
    "reveal_netflix": "netflix_reveals",
    "reveal_crunchyroll": "crunchyroll_reveals",
    "reveal_prime": "prime_reveals",
    "clear": "times_cleared",
    "guidance": "guidance_reads",
    "lore": "lore_reads",
    "profile": "profile_views",
    "steam_claim": "steam_claims_count",
}

async def add_xp(chat_id: int, first_name: str, action: str = "general", xp_override: int = None) -> tuple[int, int]:
    # ── Hard spam block ──
    if not await check_rate_limit(chat_id):
        asyncio.create_task(send_temporary_message(
            chat_id,
            "🌿 <b>Slow down, wanderer!</b>\n\n"
            "The forest spirits need a moment to breathe... 🍃",
            duration=2
        ))
        return 0, 0
    
    # ── Per-action XP cooldown ──
    xp_allowed = await check_xp_cooldown(chat_id, action)

    # ── Fetch profile ──
    profile = await get_user_profile(chat_id)

    # ── Determine XP amount ──
    if xp_override is not None:
        xp_amount = xp_override  # wheel spin passes exact amount
    else:
        xp_amount = _XP_TABLE.get(action, 0) if xp_allowed else 0

    # Guidance: 10 XP first time, 2 XP recurring
    if action == "guidance":
        if not xp_allowed:
            xp_amount = 0
        elif profile is None or profile.get("guidance_reads", 0) == 0:
            xp_amount = 10
        else:
            xp_amount = 2

    # Lore: same pattern
    elif action == "lore":
        if not xp_allowed:
            xp_amount = 0
        elif profile is None or profile.get("lore_reads", 0) == 0:
            xp_amount = 10
        else:
            xp_amount = 2

    # Save the pure action XP BEFORE we add any daily bonus
    action_xp = xp_amount

    # ── existing user ──
    if profile:
        stat_field = _STAT_FIELD.get(action)
        stats_update: dict = {}

        # ALWAYS increment stat counter regardless of cooldown
        if stat_field:
            stats_update[stat_field] = (profile.get(stat_field) or 0) + 1

        # ── Daily login bonus (injected here, once per day) ──
        daily_bonus, daily_label = await _check_daily_bonus(chat_id, first_name, profile)
        xp_amount += daily_bonus   # 0 if already claimed today

        # ✅ Set days_active BEFORE building payload
        if daily_bonus > 0:
            stats_update["days_active"] = (profile.get("days_active") or 0) + 1

        # Only add to total_xp_earned if XP was actually awarded
        if xp_amount > 0:
            stats_update["total_xp_earned"] = (
                profile.get("total_xp_earned") or 0) + xp_amount
            
        # ── Level-up calculation ──
        new_xp    = (profile.get("xp") or 0) + xp_amount
        old_level = profile.get("level") or 1
        new_level = old_level

        while True:
            needed = get_cumulative_xp_for_level(new_level + 1)
            if new_xp < needed:
                break
            new_level += 1

        payload = {
            "xp":          new_xp,
            "level":       new_level,
            "first_name":  first_name,
            "last_active": datetime.now(pytz.utc).isoformat(),
            **stats_update,
        }
        
        ok = await _sb_patch(f"user_profiles?chat_id=eq.{chat_id}", payload)
        print(f"🔵 PATCH result for {chat_id}: ok={ok}, daily_bonus={daily_bonus}")

        # ── SUPER ADVANCED ACHIEVEMENTS ──
        if ok:
            asyncio.create_task(check_and_award_achievements(chat_id, first_name, action))

        # ── Sequential XP values for clean history logging ──
        base_xp = profile.get("xp") or 0
        xp_after_action = base_xp + action_xp
        xp_after_all    = base_xp + action_xp + daily_bonus

        # Compute level at each step (action might level up before bonus is added)
        level_after_action = old_level
        while get_cumulative_xp_for_level(level_after_action + 1) <= xp_after_action:
            level_after_action += 1

        if daily_bonus > 0:
            if ok:
                await redis_client.delete(f"streak:{chat_id}")
                asyncio.create_task(_announce_daily_bonus(chat_id, daily_bonus, daily_label))
                asyncio.create_task(
                    _log_xp_history(
                        chat_id, first_name, "daily_bonus", daily_bonus,
                        xp_after_action, xp_after_all, level_after_action, new_level,
                    )
                )
                asyncio.create_task(_try_award_referral(chat_id))
            else:
                await redis_client.delete(f"daily_bonus:{chat_id}")

        # ✅ prev = base, new = after action only — not inflated by bonus
        if action_xp > 0:
            asyncio.create_task(
                _log_xp_history(
                    chat_id, first_name, action, action_xp,
                    base_xp, xp_after_action, old_level, level_after_action,
                )
            )

        if new_level > old_level:
            asyncio.create_task(
                send_level_up_message(chat_id, first_name, old_level, new_level)
            )

        # ── MILESTONE CHECK ──
        if ok:
            base_xp = profile.get("xp") or 0
            crossed = get_crossed_milestones(base_xp, new_xp)
            if crossed:
                asyncio.create_task(
                    send_milestone_message(chat_id, first_name, crossed[-1])
                )

    # ── new user — first tap stat is recorded ──
    else:
        stat_field = _STAT_FIELD.get(action)
        initial_stats = {f: 0 for f in _STAT_FIELD.values()}
        if stat_field:
            initial_stats[stat_field] = 1

        # New users DO get XP for their first action
        # (previously they got 0 — bad first impression)
        first_xp = xp_amount  # whatever action triggered registration
        action_xp = xp_amount  # ✅ save action_xp before adding bonus
        
        # ✅ New users get daily bonus on first visit too
        daily_bonus, daily_label = await _check_daily_bonus(chat_id, first_name, {})
        first_xp += daily_bonus  # first_xp = action + bonus (what goes in DB)

        # NEW: Check for pending referral
        pending_referrer = await get_pending_referrer(chat_id)
        extra_welcome = 0
        if pending_referrer:
            extra_welcome = NEW_USER_WELCOME_BONUS_IF_REFERRED
            first_xp += extra_welcome

        payload = {
            "chat_id":        chat_id,
            "first_name":     first_name,
            "xp":             first_xp,
            "level":          1,
            "onboarding_completed": False,
            "last_active":    datetime.now(pytz.utc).isoformat(),
            "has_seen_menu":  False,
            "created_at": datetime.now(pytz.utc).isoformat(),  # ✅ real timestamp
            "total_xp_earned": first_xp,
            "referral_count": 0,
            "days_active":     1 if daily_bonus > 0 else 0,
            **{f: 0 for f in _STAT_FIELD.values()},
            **initial_stats,
        }
        # Inside the new-user else: block, just before the upsert
        print("🔵 NEW USER PAYLOAD KEYS:", list(payload.keys()))
        ok = await _sb_upsert("user_profiles", payload, on_conflict="chat_id", ignore_duplicates=True)
        print(f"🔵 NEW USER UPSERT for {chat_id}: ok={ok}")

        # ── MILESTONE CHECK for new users ──
        if ok and first_xp > 0:
            crossed = get_crossed_milestones(0, first_xp)
            if crossed:
                asyncio.create_task(
                    send_milestone_message(chat_id, first_name, crossed[-1])
                )

        # Insert into referrals table
        if pending_referrer and ok:
            await _sb_post("referrals", {
                "referrer_id": pending_referrer,
                "referred_id": chat_id
            })

        # ── Sequential XP cursor — each log entry shows its own exact before/after ──
        xp_cursor = 0

        if daily_bonus > 0:
            if ok:
                asyncio.create_task(_announce_daily_bonus(chat_id, daily_bonus, daily_label))
                asyncio.create_task(
                    _log_xp_history(
                        chat_id, first_name, "daily_bonus", daily_bonus,
                        xp_cursor, xp_cursor + daily_bonus, 1, 1,
                    )
                )
                asyncio.create_task(_try_award_referral(chat_id))
                xp_cursor += daily_bonus
            else:
                await redis_client.delete(f"daily_bonus:{chat_id}")

        if action_xp > 0 and ok:
            asyncio.create_task(
                _log_xp_history(
                    chat_id, first_name, action, action_xp,
                    xp_cursor, xp_cursor + action_xp, 1, 1,
                )
            )
            xp_cursor += action_xp

        # Notify friend + log welcome bonus separately
        if extra_welcome > 0 and ok:
            asyncio.create_task(
                _log_xp_history(
                    chat_id, first_name, "welcome_bonus", extra_welcome,
                    xp_cursor, xp_cursor + extra_welcome, 1, 1,
                )
            )
            try:
                # Change duration from temporary to a proper message
                await tg_app.bot.send_message(
                    chat_id,
                    "🎁 <b>Welcome Bonus!</b>\n\n"
                    f"✨ <b>+{extra_welcome} Forest Energy</b> added!\n\n"
                    "A friend invited you to the clearing.\n"
                    "<i>The forest rewards bonds. 🌿</i>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
    return action_xp, xp_amount

async def _log_xp_history(
    chat_id: int, first_name: str, action: str,
    xp_earned: int, prev_xp: int, new_xp: int,
    prev_level: int, new_level: int,
):
    await _sb_post(
        "xp_history",
        {
            "chat_id":        chat_id,
            "first_name":     first_name,
            "action":         action,
            "xp_earned":      xp_earned,
            "previous_xp":    prev_xp,
            "new_xp":         new_xp,
            "previous_level": prev_level,
            "new_level":      new_level,
            "leveled_up":     new_level > prev_level,
        },
    )

async def show_gcash_qr(chat_id: int, query=None):
    caption = (
        "💰 <b>GCash Donation QR Code</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Scan this QR code with your GCash app to support the Enchanted Clearing.\n\n"
        "Any amount helps keep the forest magic alive! 🌳✨\n\n"
        "<i>Thank you, kind wanderer. The trees are grateful.</i>"
    )

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Donate Menu", callback_data="donate")],
        [InlineKeyboardButton("⬅️ Back to the Clearing", callback_data="main_menu")],
    ])

    if query and query.message:
        try:
            # This edits the exact same message
            await query.message.edit_media(
                media=InputMediaPhoto(
                    media=QR_IMAGE_URL,
                    caption=caption,
                    parse_mode="HTML"
                ),
                reply_markup=markup
            )
            return
        except Exception:
            pass  # fallback if edit fails

    # Fallback (very rare)
    await tg_app.bot.send_photo(
        chat_id=chat_id,
        photo=QR_IMAGE_URL,
        caption=caption,
        parse_mode="HTML",
        reply_markup=markup
    )

async def _log_wheel_spin(
    chat_id: int,
    first_name: str,
    rarity: str,
    xp_earned: int,
    got_bonus_slot: bool = False,
    got_fresh_cookie: bool = False,
    cookie_service: str | None = None,
):
    """Log every wheel spin + update user summary stats (reliable version)"""
    # 1. Save detailed record
    await _sb_post("wheel_spins", {
        "chat_id": chat_id,
        "first_name": first_name,
        "rarity": rarity,
        "xp_earned": xp_earned,
        "got_bonus_slot": got_bonus_slot,
        "got_fresh_cookie": got_fresh_cookie,
        "cookie_service": cookie_service,
    })

    # 2. Update summary stats reliably
    profile = await get_user_profile(chat_id)
    if profile:
        new_spins = (profile.get("total_wheel_spins") or 0) + 1
        new_xp = (profile.get("wheel_xp_earned") or 0) + xp_earned
        new_legendary = (profile.get("legendary_spins") or 0) + (1 if rarity == "Legendary" else 0)

        await _sb_patch(f"user_profiles?chat_id=eq.{chat_id}", {
            "total_wheel_spins": new_spins,
            "wheel_xp_earned": new_xp,
            "legendary_spins": new_legendary,
        })

# ══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════════════════
def kb_start():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🌿 Enter the Enchanted Clearing", callback_data="show_main_menu")
    ]])

def kb_resources():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪄 Spirit Treasures", url="https://clyderesourcehub.short.gy/steam-account"),
            InlineKeyboardButton("📜 Ancient Scrolls", url="https://clyderesourcehub.short.gy/learn-and-guides"),
        ],
        [InlineKeyboardButton("🌲 The Whispering Forest", url="https://clyderesourcehub.short.gy/")],
        [InlineKeyboardButton("🍜 Crunchy Checker", callback_data="show_crunchyroll_bot")],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="main_menu")],
    ])

def kb_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌟 Wheel of Whispers", callback_data="show_wheel_menu")],
        [InlineKeyboardButton("🌿 Check Forest Inventory", callback_data="check_vamt")],
        [
            InlineKeyboardButton("👤 My Profile", callback_data="show_profile_page"),
            InlineKeyboardButton("⚙️ Settings", callback_data="show_settings_page"),
        ],
        [
            InlineKeyboardButton("❓ Guidance", callback_data="help"),
            InlineKeyboardButton("ℹ️ Lore", callback_data="about"),
        ],
        [
            InlineKeyboardButton("📦 Resources", callback_data="show_resources"),
            InlineKeyboardButton("🕊️ Messenger", url="https://t.me/caydigitals")
        ],
        [InlineKeyboardButton("🟢 Who's Online Now", callback_data="show_online_users")],
        [InlineKeyboardButton("🌳 Support the Enchanted Clearing", callback_data="donate")],
        [InlineKeyboardButton("🌟 Forest Patrons", callback_data="patrons")],
    ])

def kb_first_time_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❓ Start Here → Guidance", callback_data="help")],
        [InlineKeyboardButton("🌿 Check Forest Inventory", callback_data="check_vamt")],
        [InlineKeyboardButton("🌟 Wheel of Whispers", callback_data="show_wheel_menu")],
        [
            InlineKeyboardButton("👤 My Profile", callback_data="show_profile_page"),
            InlineKeyboardButton("⚙️ Settings", callback_data="show_settings_page"),
        ],
        [InlineKeyboardButton("📦 Resources", callback_data="show_resources")],
        [InlineKeyboardButton("🕊️ Messenger", url="https://t.me/caydigitals")],
    ])

def kb_donate():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 GCash QR Code", callback_data="gcash_qr")],
        [InlineKeyboardButton("⬅️ Back to the Clearing", callback_data="main_menu")],
    ])

def kb_patrons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to the Clearing", callback_data="main_menu")],
    ])

def kb_inventory():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪟 Windows Keys", callback_data="vamt_filter_win"),
            InlineKeyboardButton("📑 Office Keys", callback_data="vamt_filter_office"),
        ],
        [
            InlineKeyboardButton("🍿 Netflix Cookies", callback_data="vamt_filter_netflix"),
            InlineKeyboardButton("🎥 PrimeVideo Cookies", callback_data="vamt_filter_prime"),
        ],
        [
            InlineKeyboardButton("🍜 Crunchyroll Cookies", callback_data="vamt_filter_crunchyroll"),
            InlineKeyboardButton("🎮 Steam Accounts", callback_data="vamt_filter_steam"),
        ],
        [InlineKeyboardButton("⬅️ Back to Clearing", callback_data="main_menu")],
    ])

def kb_back_inventory():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Scroll Selection", callback_data="check_vamt")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Return to the Clearing", callback_data="main_menu")
    ]])

def kb_winoffice_guide():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Got it, show me the keys →",
            callback_data="winoffice_got_it"
        )
    ]])


def kb_back_to_wheel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Wheel of Whispers", callback_data="show_wheel_menu")],
    ])

# ══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS (final version)
# ══════════════════════════════════════════════════════════════════════════════
async def show_patrons_page(chat_id: int, query=None):
    """Dynamic Forest Patrons page from Supabase"""
    patrons = await get_forest_patrons()

    if not patrons:
        text = (
            "🌟 <b>The Grove of Eternal Gratitude</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "No kind souls have planted a tree for the forest yet...\n\n"
            "<i>Be the first to support the clearing and have your name remembered forever.</i> 🍃"
        )
    else:
        lines = [f"• {p.get('display_name', '')} - {p.get('title', 'Kind Wanderer')}" for p in patrons]
        text = (
            "🌟 <b>The Grove of Eternal Gratitude</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "These kind wanderers have helped the ancient trees thrive.\n"
            "Every donation keeps the forest alive and full of magic.\n\n"
            + "\n".join(lines) +
            "\n\n<i>The forest remembers every generous heart. "
            "Thank you for helping the clearing grow. 🍃✨</i>"
        )

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to the Clearing", callback_data="main_menu")]
    ])

    if query and query.message:
        try:
            await query.message.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            return
        except:
            pass

    msg = await send_animated_translated(
        chat_id=chat_id,
        caption=text,
        animation_url=DONOR_GIF,
    )
    await _remember(chat_id, msg.message_id) 

async def kb_caretaker_dynamic() -> InlineKeyboardMarkup:
    event = await get_active_event()
    
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📜 Patch Notes", callback_data="cpage_addupdate"),
            InlineKeyboardButton("📬 Feedbacks", callback_data="cpage_viewfeedback"),
        ],
        [
            InlineKeyboardButton("🎉 Events", callback_data="cpage_events"),
            InlineKeyboardButton("🔄 Cache", callback_data="cpage_flushcache"),
        ],
        [
            InlineKeyboardButton("📊 System Health", callback_data="cpage_health"),
            InlineKeyboardButton("📦 Stock", callback_data="cpage_checkstock"),
        ],
        [
            InlineKeyboardButton("📤 Upload Keys", callback_data="cpage_uploadkeys"),
            InlineKeyboardButton("📋 Reports", callback_data="cpage_viewreports"),
        ],
        [
            InlineKeyboardButton("🛠️ Maintenance", callback_data="cpage_maintenance"),
            InlineKeyboardButton("📝 Forest Info", callback_data="cpage_setinfo"),
        ],
        [
            InlineKeyboardButton("⚠️ Full Reset", callback_data="cpage_resetfirst"),
            InlineKeyboardButton("🎮 Steam", callback_data="cpage_steam"),
        ],
        [
            InlineKeyboardButton("📋 Notion Library", callback_data="cpage_notion"),
        ],
        [
            InlineKeyboardButton("👤 User Tools", callback_data="cpage_usertools"),
        ],
        [
            InlineKeyboardButton("⬅️ Back to Clearing", callback_data="main_menu"),
        ],
    ])

# ══════════════════════════════════════════════════════════════════════════════
# UTILITY MESSAGES
# ══════════════════════════════════════════════════════════════════════════════
def _greeting(tz_str: str = "Asia/Manila", first_name: str = None) -> tuple[str, str, str]:
    """Returns (icon, greeting_text, gif_url) — fully personalized & immersive"""
    now = datetime.now(pytz.timezone(tz_str))
    hour = now.hour
    is_weekend = now.weekday() >= 5   # Saturday or Sunday

    # Personalize the name (safe fallback)
    name = html.escape(first_name.strip()) if first_name and first_name.strip() else "wanderer"

    # ── Midnight (most magical) ──
    if hour == 0:
        return "🌌", f"Midnight in the Enchanted Clearing, {name}", MIDNIGHT_GIF

    # ── Dawn (peaceful early hours) ──
    elif 1 <= hour <= 4:
        return "🌄", f"Dawn whispers through the trees, {name}", DAWN_GIF

    # ── Morning ──
    elif 5 <= hour < 12:
        text = f"Good morning, gentle {name}" if not is_weekend else f"Peaceful weekend morning, {name}"
        return "🌅", text, MORNING_GIF

    # ── Afternoon ──
    elif 12 <= hour < 18:
        text = f"Good afternoon, kind {name}" if not is_weekend else f"Relaxed weekend afternoon, {name}"
        return "🌤️", text, AFTERNOON_GIF

    # ── Late Evening (cozy & quiet) ──
    elif 22 <= hour <= 23:
        return "🌃", f"The forest grows quiet, {name}", LATENIGHT_GIF

    # ── Evening ──
    else:  # 6 PM – 9:59 PM
        text = f"Good evening, {name}" if not is_weekend else f"Cozy weekend evening, {name}"
        return "🌙", text, EVENING_GIF


def get_crossed_milestones(old_xp: int, new_xp: int) -> list[int]:
    """Returns milestones crossed between old and new XP"""
    return [m for m in MILESTONES if old_xp < m <= new_xp]


async def send_milestone_message(chat_id: int, first_name: str, milestone: int):
    """Sends an immersive milestone celebration message"""
    await asyncio.sleep(2)  # delay so it appears after XP feedback popup

    emoji, flavor = MILESTONE_LABELS.get(milestone, ("🌟", "Amazing progress!"))

    msg = await send_animated_translated(
        chat_id=chat_id,
        animation_url=LOADING_GIF,  # swap for a celebration GIF if you have one
        caption=(
            f"{emoji} <b>Milestone Reached!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"🎉 <b>{html.escape(first_name)}, you just hit "
            f"{milestone:,} XP!</b>\n\n"
            f"<i>{flavor}</i>\n\n"
            "Keep exploring the clearing! 🍃✨"
        )
    )
    await _remember(chat_id, msg.message_id)

async def send_xp_feedback(chat_id: int, xp_amount: int, duration: int = 2):
    if xp_amount <= 0:
        return
    try:
        msg = await tg_app.bot.send_message(
            chat_id, f"✨ <b>+{xp_amount} XP</b> earned!", parse_mode="HTML"
        )
        await asyncio.sleep(duration)
        await msg.delete()
    except Exception:
        pass

async def send_temporary_message(chat_id: int, text: str, duration: int = 2):
    try:
        msg = await tg_app.bot.send_message(chat_id, text, parse_mode="HTML")
        await asyncio.sleep(duration)
        await msg.delete()
    except Exception:
        pass

async def send_loading(
    chat_id: int,
    caption: str = "🌫️ <i>The ancient mist begins to stir...</i>",
    remember: bool = False,
):
    msg = await send_animated_translated(
        chat_id=chat_id,
        animation_url=LOADING_GIF,
        caption=caption,
    )
    if remember:
        await _remember(chat_id, msg.message_id)
    return msg

async def _announce_daily_bonus(chat_id: int, bonus: int, label: str):
    # ── Respect user preference (Redis) ──
    if not await get_daily_bonus_notif(chat_id):
        return

    await asyncio.sleep(2.5)
    await send_temporary_message(
        chat_id,
        f"✨ <b>{label}</b>\n\n"
        f"<b>+{bonus} XP</b> for visiting the clearing today 🍃",
        duration=5,
    )

async def _remember(chat_id: int, message_id: int):
    try:
        await redis_client.lpush(f"mem:{chat_id}", message_id)
        await redis_client.expire(f"mem:{chat_id}", 86400)  # 24 hours TTL
    except Exception as e:
        print(f"⚠️ Redis remember failed: {e}")
        forest_memory.setdefault(chat_id, []).append(message_id)  # fallback to RAM


# ══════════════════════════════════════════════════════════════════════════════
# STREAK
# ══════════════════════════════════════════════════════════════════════════════
async def calculate_streak(chat_id: int) -> int:
    # ── Check Redis cache first ──
    cached = await redis_client.get(f"streak:{chat_id}")
    if cached is not None:
        return int(cached)

    
    data = await _sb_get(
        "xp_history",
        **{"chat_id": f"eq.{chat_id}", "select": "created_at", "order": "created_at.desc", "limit": 100},
    )
    if data is None:
        return 0
    data = data or []

    manila  = pytz.timezone("Asia/Manila")
    today   = datetime.now(manila).date()
    streak  = 0
    seen: set = set()
    for row in data:
        try:
            dt   = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            date = dt.astimezone(manila).date()
            if date not in seen:
                seen.add(date)
                if (today - date).days == streak:
                    streak += 1
                else:
                    break
        except Exception:
            continue
            
    # ── Cache result for 5 minutes ──
    await redis_client.setex(f"streak:{chat_id}", 300, streak)
    return streak

async def show_caretaker_page(chat_id: int, page: str, query=None):
    """Routes to the correct caretaker sub-page"""

    pages = {

        # ── UPLOAD KEYS ──
        "uploadkeys": (
            "📤 <b>Key Uploader</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Upload activation keys or cookies by sending a "
            "<b>.txt</b> or <b>.zip</b> file to the bot.\n\n"
            "📋 <b>Supported formats:</b>\n"
            "• Plain keys (one per line)\n"
            "• Pipe separated: <code>KEY|remaining|service|name</code>\n"
            "• Cookie files (Netflix, Prime)\n"
            "• JSON format\n"
            "• Key:Value block format\n"
            "• CSV format\n\n"
            "✅ Service type is <b>auto-detected</b> from filename/content.\n"
            "✅ After upload, cache is cleared and users are notified automatically.\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🗝️ <b>Manual Key Entry</b>\n\n"
            "Type Windows or Office keys directly — no file needed.\n"
            "Supports custom display name and remaining count.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Upload Keys (send file after)", callback_data="caretaker_uploadkeys")],
                [InlineKeyboardButton("🗝️ Manual Win/Office Entry", callback_data="caretaker_manualkeys")],  # ← NEW
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── PATCH NOTES ──
        "addupdate": (
            "📜 <b>Patch Notes Manager</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "This lets you add a new patch note entry that all users can see "
            "when they tap <b>Updates</b> in the bot.\n\n"
            "📌 <b>Format:</b>\n"
            "<code>/addupdate\n"
            "Your Title Here\n"
            "Full description of changes...</code>\n\n"
            "• Line 1 after command = Title\n"
            "• Line 2+ = Description (can be multi-line)\n"
            "• Date is auto-set to today (Manila time)\n\n"
            "📋 <b>Shows last 5 patch notes to users</b>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📜 Add Patch Note Now", callback_data="caretaker_addupdate")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── FEEDBACKS ──
        "viewfeedback": (
            "📬 <b>User Feedback Viewer</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Shows the latest <b>15 feedback messages</b> submitted by users "
            "via the <code>/feedback</code> command.\n\n"
            "📌 <b>Each entry shows:</b>\n"
            "• User's name and chat ID\n"
            "• Date and time submitted\n"
            "• Full feedback message\n\n"
            "⚠️ Users are limited to <b>3 feedbacks per day</b> to prevent spam.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📬 View Feedbacks Now", callback_data="caretaker_viewfeedback")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── EVENTS ──
        "events": (
            "🎉 <b>Event Manager</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Create and manage forest events that appear as banners "
            "on the main menu for all users.\n\n"
            "📌 <b>Create Event Format:</b>\n"
            "<code>/addevent\n"
            "Event Title\n"
            "April 25, 2026\n"
            "Description here...\n"
            "bonus:netflix_double</code>\n\n"
            "🎁 <b>Bonus Types:</b>\n"
            "• <code>bonus:netflix_double</code> — Doubles Netflix slots\n"
            "• <code>bonus:netflix_max</code> — Maximizes Netflix slots\n"
            "• <i>Omit bonus line for normal event</i>\n\n"
            "⚠️ Creating a new event replaces the current one.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🎉 Create Event", callback_data="caretaker_addevent")],
                [InlineKeyboardButton("👁️ View Current Event", callback_data="caretaker_viewevent")],
                [InlineKeyboardButton("🔴 End Current Event", callback_data="confirm_end_event")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── FLUSH CACHE ──
        "flushcache": (
            "🔄 <b>Cache Manager</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Clears all Redis cached data so the bot fetches "
            "fresh data from Supabase on the next request.\n\n"
            "🗂 <b>Caches that will be cleared:</b>\n"
            "• VAMT inventory (keys & cookies)\n"
            "• Achievements list (including GIF URLs)\n"
            "• Forest Patrons list\n"
            "• Active event banner\n\n"
            "✅ Achievements are also <b>immediately reloaded</b> after flush.\n\n"
            "💡 <b>When to use:</b> After updating data in Supabase directly, "
            "or when users report seeing outdated info.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Flush All Caches Now", callback_data="caretaker_flushcache")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── SYSTEM HEALTH ──
        "health": (
            "📊 <b>System Health Monitor</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Runs a full live diagnostic on all core systems.\n\n"
            "🔍 <b>What gets checked:</b>\n"
            "• <b>Redis</b> — Memory usage, peak, and key count\n"
            "• <b>Supabase</b> — Connection and DB slot usage\n"
            "• <b>Telegram</b> — Bot API responsiveness\n"
            "• <b>Env Vars</b> — Required and optional variables\n"
            "• <b>Maintenance Mode</b> — Current on/off state\n"
            "• <b>Uptime</b> — How long the bot has been running\n\n"
            "👥 <b>User Stats</b> — Total, active now, new today/week, level spread\n"
            "📦 <b>Inventory</b> — Stock levels per service with low-stock warnings\n"
            "⚡ <b>Today's Activity</b> — XP, reveals, spins, claims, feedbacks\n"
            "🗂 <b>Redis Snapshot</b> — Active cooldowns, caps, and rate limits\n"
            "🎉 <b>Active Event</b> — Current event banner and countdown\n"
            "🏆 <b>Achievements</b> — Total loaded in cache\n\n"
            "💡 Use this when something feels slow or broken.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Run Health Check Now", callback_data="caretaker_health")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── STOCK CHECK ──
        "checkstock": (
            "📦 <b>Stock Level Monitor</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Shows how many active items are currently available "
            "for each service in the inventory.\n\n"
            "📋 <b>Services monitored:</b>\n"
            "• 🍿 Netflix cookies\n"
            "• 🎥 Prime Video cookies\n"
            "• 🪟 Windows keys\n"
            "• 📑 Office keys\n"
            "• 🎮 Steam accounts\n\n"
            "⚠️ Items at or below <b>5</b> will show a warning.\n\n"
            "🔔 The bot auto-alerts you every 6 hours if stock is low.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Check Stock Now", callback_data="caretaker_checkstock")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── VIEW REPORTS ──
        "viewreports": (
            "📋 <b>Key Feedback Reports</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Shows the latest <b>20 reports</b> submitted by users "
            "when they mark a key or cookie as working or not working.\n\n"
            "📌 <b>Each report shows:</b>\n"
            "• User name and ID\n"
            "• Service type (Netflix, Prime, Windows, etc.)\n"
            "• The key/cookie that was reported\n"
            "• Status (✅ Working / ❌ Not Working)\n"
            "• Timestamp\n\n"
            "💡 Use this to identify and remove broken keys from the inventory.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 View Reports Now", callback_data="caretaker_viewreports")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── MAINTENANCE ──
        "maintenance": (
            "🛠️ <b>Maintenance Mode</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Toggles maintenance mode on or off for all users.\n\n"
            "🔴 <b>When ON:</b>\n"
            "• All users (except you) see a maintenance message\n"
            "• All button taps show an alert popup\n"
            "• No features are accessible\n\n"
            "🟢 <b>When OFF:</b>\n"
            "• Everything works normally\n\n"
            "⚠️ <b>Always turn this OFF after you're done with maintenance!</b>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🛠️ Toggle Maintenance Mode", callback_data="confirm_toggle_maintenance")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── SET FOREST INFO ──
        "setinfo": (
            "📝 <b>Forest Info Editor</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Updates the bot version number and last-updated timestamp "
            "shown in the <b>/forest</b> info command.\n\n"
            "📌 <b>Format (single line):</b>\n"
            "<code>/setforestinfo 1.4.5 April 14, 2026 · 02:58 PM</code>\n\n"
            "📌 <b>Format (multi-line):</b>\n"
            "<code>/setforestinfo\n"
            "1.4.5\n"
            "April 14, 2026\n"
            "02:58 PM</code>\n\n"
            "• Field 1 = Version number\n"
            "• Field 2 = Date\n"
            "• Field 3 = Time (merged as Date · Time)",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Set Forest Info (use command)", callback_data="caretaker_setinfo")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        "online_tools": (
            "🟢 <b>Online Users Viewer</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Shows all users who have been active in the last "
            "<b>15 minutes</b>, sorted by most recent activity.\n\n"
            "📋 <b>Each user shows:</b>\n"
            "• Name and level title\n"
            "• Presence dot (🟢 Just now / 🟡 Recent / 🔵 Last 15min)\n"
            "• Minutes since last action\n"
            "• Total XP\n\n"
            "🔄 Results are cached for <b>60 seconds</b> to reduce "
            "database load. Tap Refresh inside to get the latest.\n\n"
            "💡 You also receive an automatic DM notification "
            "whenever a user comes online (once per 15 min per user).",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🟢 View Online Users Now", callback_data="show_online_users")],
                [InlineKeyboardButton("⬅️ Back to User Tools", callback_data="cpage_usertools")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        "patron_tools": (
            "👥 <b>Patron & Diagnostics</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🌟 <b>Add Patron:</b>\n"
            "Adds a donor to the public <b>Forest Patrons</b> wall "
            "visible to all users via the main menu.\n\n"
            "📌 <b>Format:</b>\n"
            "<code>/addpatron @username</code>\n"
            "<code>/addpatron @username Legendary Guardian</code>\n\n"
            "• If no title given, defaults to <b>Kind Wanderer</b>\n"
            "• Display name auto-formats with @ prefix\n"
            "• Patron wall cache clears immediately after adding\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🔬 <b>Achievement Diagnostics:</b>\n"
            "Shows a full report of all achievements loaded in the "
            "system — total count, types, and condition breakdown.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🔬 Run Achievement Diagnostics",
                    callback_data="run_test_achievements"
                )],
                [InlineKeyboardButton("⬅️ Back to User Tools", callback_data="cpage_usertools")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        "reset_tools": (
            "🔄 <b>Reset Tools</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "All reset tools require a <b>user chat ID</b>.\n"
            "You can find any user's ID in the online notification "
            "messages the bot sends you.\n\n"
            "📋 <b>Available Resets:</b>\n\n"
            "🌱 <b>Onboarding</b> — Makes user see the welcome "
            "tutorial again on their next /start\n"
            "Command: <code>/resetonboarding &lt;id&gt;</code>\n\n"
            "🖼️ <b>Profile GIF</b> — Removes the 7-day cooldown so "
            "user can change their profile logo again immediately\n"
            "Command: <code>/resetprofilegif &lt;id&gt;</code>\n\n"
            "📖 <b>Win/Office Guide</b> — Makes user see the "
            "Windows/Office first-time guide again\n"
            "Command: <code>/resetguide &lt;id&gt;</code>\n\n"
            "🎰 <b>Wheel Spin</b> — Removes daily cooldown so user "
            "can spin the Wheel of Whispers again today\n"
            "Command: <code>/resetwheel &lt;id&gt;</code>\n\n"
            "🎮 <b>Steam Claim</b> — Resets today's Steam claim "
            "count so user can claim again\n"
            "Command: <code>/resetsteamclaim &lt;id&gt;</code>\n\n"
            "☀️ <b>Daily Bonus</b> — Resets daily bonus, referral "
            "lock and pending referral for clean testing\n"
            "Command: <code>/testdaily &lt;id&gt;</code>\n\n"
            "🎮 <b>Steam Search</b> — Resets cooldown and all search attempts\n"
            "Command: <code>/resetsteamsearch &lt;id&gt;</code>\n\n"
            "📌 <b>To use:</b> Close this and type the command.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to User Tools", callback_data="cpage_usertools")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── FULL RESET ──
        "resetfirst": (
            "⚠️ <b>Full User Reset</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Permanently resets <b>your own</b> profile data.\n\n"
            "🗑 <b>What gets wiped:</b>\n"
            "• Level → 1\n"
            "• XP → 0\n"
            "• Total XP Earned → 0\n"
            "• All activity stats (views, reveals, etc.)\n"
            "• Full XP history\n"
            "• has_seen_menu flag\n\n"
            "✅ <b>What is kept:</b>\n"
            "• Your chat ID and account\n"
            "• Referral count\n"
            "• Achievements\n\n"
            "❌ <b>This cannot be undone!</b>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⚠️ Proceed to Reset", callback_data="caretaker_resetfirst")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── STEAM ──
        "steam": (
            "🎮 <b>Steam Manager</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Manage Steam accounts in the bot.\n\n"
            "🔍 <b>Search Steam:</b>\n"
            "Search by email to view account details, "
            "live Steam status, and full games list.\n\n"
            "📤 <b>Upload Steam:</b>\n"
            "Add one or multiple Steam accounts.\n\n"
            "🔄 <b>Reset Search:</b>\n"
            "Reset cooldown and attempts for any user.\n"
            "Command: <code>/resetsteamsearch &lt;user_id&gt;</code>\n"
            "No ID = resets yourself.\n\n"
            "📋 <b>Notion Library:</b>\n"
            "View, filter, and update Steam account availability.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search Steam", callback_data="caretaker_searchsteam")],
                [InlineKeyboardButton("📤 Upload Steam", callback_data="caretaker_uploadsteam")],
                [InlineKeyboardButton("📋 Notion Library", callback_data="view_notion_steam")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── NOTION ──
        "notion": (
            "📋 <b>Notion Steam Library</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "View and manage your Steam account library "
            "stored in Notion directly from the bot.\n\n"
            "✅ <b>Features:</b>\n"
            "• See all games with availability status\n"
            "• Filter by Available / Expired / Recently Updated\n"
            "• One-tap mark as Available or Expired\n"
            "• Sorted by most recently updated first\n"
            "• Paginated (8 per page)\n\n"
            "💡 Changes made here update your Notion database in real time.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Open Notion Library", callback_data="view_notion_steam")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        "ach_tools": (
            "🏆 <b>Achievement Tools</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Manage user achievements manually.\n\n"
            "🌟 <b>Award Beta Guardian:</b>\n"
            "Gives the special <b>Beta Guardian</b> mythic achievement "
            "to a user. This is a one-time manual award that also grants "
            "a permanent slot bonus.\n"
            "Command: <code>/award_beta &lt;user_id&gt;</code>\n\n"
            "🗑 <b>Remove Achievement:</b>\n"
            "Removes a specific achievement from a user by their "
            "chat ID and achievement code.\n"
            "Command: <code>/remove_achievement &lt;user_id&gt; &lt;code&gt;</code>\n\n"
            "🔬 <b>Test Achievements:</b>\n"
            "Runs a full diagnostic report on the achievement system — "
            "shows all 54 achievements, their types, and current status.\n"
            "Command: <code>/test_achievements</code>\n\n"
            "📌 <b>To use:</b> Close this and type the command directly.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🔬 Run Achievement Diagnostics",
                    callback_data="run_test_achievements"
                )],
                [InlineKeyboardButton("⬅️ Back to User Tools", callback_data="cpage_usertools")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),

        # ── USER TOOLS ──
        "usertools": (
            "👤 <b>User Management Tools</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Select a tool category below:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🏆 Achievement Tools", callback_data="cpage_ach_tools")],
                [InlineKeyboardButton("🔄 Reset Tools", callback_data="cpage_reset_tools")],
                [InlineKeyboardButton("👥 Patron & Diagnostics", callback_data="cpage_patron_tools")],
                [InlineKeyboardButton("🟢 Online Users", callback_data="cpage_online_tools")],
                [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
            ])
        ),
    }

    if page not in pages:
        return

    text, keyboard = pages[page]

    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    await send_animated_translated(
        chat_id=chat_id,
        animation_url=CARETAKER_GIF,
        caption=text,
        reply_markup=keyboard,
    )

async def show_streak_calendar(chat_id: int, first_name: str, query=None):
    """📅 30-day streak calendar with activity heatmap"""

    manila = pytz.timezone("Asia/Manila")
    now = datetime.now(manila)

    # ── Fetch enough history for longest streak (no arbitrary 500 limit) ──
    data = await _sb_get(
        "xp_history",
        **{
            "chat_id": f"eq.{chat_id}",
            "select": "created_at",
            "order": "created_at.desc",
            "limit": 1000,   # increased from 500 to catch longer history
        },
    ) or []

    # ── Build set of active dates (Manila time) ──
    active_dates: set = set()
    for row in data:
        try:
            dt = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            active_dates.add(dt.astimezone(manila).date())
        except Exception:
            continue

    # ── Current streak from cache ──
    current_streak = await calculate_streak(chat_id)

    # ── FIX: Longest streak — scan all active dates properly ──
    longest_streak = 0
    if active_dates:
        sorted_dates = sorted(active_dates)
        temp = 1
        longest_streak = 1
        for i in range(1, len(sorted_dates)):
            diff = (sorted_dates[i] - sorted_dates[i - 1]).days
            if diff == 1:
                temp += 1
                longest_streak = max(longest_streak, temp)
            elif diff > 1:
                temp = 1
        # Edge case: single active day
        longest_streak = max(longest_streak, current_streak)

    # ── Count active days this month ──
    month_start = now.date().replace(day=1)
    days_this_month = sum(
        1 for d in active_dates
        if month_start <= d <= now.date()
    )

    # ── Total active days this year ──
    year_start = now.date().replace(month=1, day=1)
    days_this_year = sum(1 for d in active_dates if d >= year_start)

    # ── Build 30-day calendar grid ──
    start_date = now.date() - timedelta(days=29)
    calendar_rows = []
    current_row = []

    # Pad first row to align Monday = col 0
    first_weekday = start_date.weekday()
    for _ in range(first_weekday):
        current_row.append("⬛")

    cursor = start_date
    while cursor <= now.date():
        if cursor in active_dates:
            current_row.append("🟡" if cursor == now.date() else "🟩")
        else:
            current_row.append("🔵" if cursor == now.date() else "⬜")

        if len(current_row) == 7:
            calendar_rows.append(" ".join(current_row))
            current_row = []

        cursor += timedelta(days=1)

    # Pad and append the last incomplete row
    if current_row:
        while len(current_row) < 7:
            current_row.append("⬛")
        calendar_rows.append(" ".join(current_row))

    calendar_grid = "\n".join(calendar_rows)

    # ── Streak status ──
    if current_streak >= 60:
        streak_emoji, streak_label = "🌠", "Ancient Soul!"
    elif current_streak >= 30:
        streak_emoji, streak_label = "🏆", "Devoted Wanderer!"
    elif current_streak >= 14:
        streak_emoji, streak_label = "🌟", "Forest Regular!"
    elif current_streak >= 7:
        streak_emoji, streak_label = "⚡", "Week Warrior!"
    elif current_streak >= 3:
        streak_emoji, streak_label = "🔥", "On Fire!"
    elif current_streak >= 2:
        streak_emoji, streak_label = "🌱", "Getting Started!"
    elif current_streak >= 1:
        streak_emoji, streak_label = "✨", "First Step!"
    else:
        streak_emoji, streak_label = "💤", "Start your streak today!"

    # ── Next milestone ──
    milestones = {
        2:  ("🌱", "+12 XP/day"),
        3:  ("🔥", "+20 XP/day"),
        7:  ("⚡", "+30 XP/day"),
        14: ("🌟", "+40 XP/day"),
        30: ("🏆", "+50 XP/day"),
        60: ("🌠", "+60 XP/day — Ancient Soul!"),
    }

    next_milestone_text = ""
    for days, (icon, reward) in milestones.items():
        if current_streak < days:
            days_away = days - current_streak
            next_milestone_text = (
                f"🎯 <b>Next milestone in {days_away} day{'s' if days_away != 1 else ''}:</b>\n"
                f"   {icon} {days}-day streak → {reward}\n\n"
            )
            break

    # ── FIX: Use consistent header label ──
    HEADER = "<b>Mo Tu We Th Fr Sa Su</b>"

    # ── Consistency bar for this month ──
    days_in_month = now.day  # days elapsed so far
    consistency_pct = round((days_this_month / days_in_month) * 100) if days_in_month > 0 else 0
    consistency_bar = create_daily_progress_bar(days_this_month, days_in_month, length=10)

    text = (
        f"📅 <b>{html.escape(first_name)}'s Streak Calendar</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{streak_emoji} <b>{current_streak}-day streak</b> — {streak_label}\n\n"
        f"{HEADER}\n"
        f"{calendar_grid}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🟩 Active  ⬜ Missed  🟡 Today  ⬛ Out of range\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>Your Stats</b>\n\n"
        f"🔥 Current Streak: <b>{current_streak} days</b>\n"
        f"🏆 Longest Streak: <b>{longest_streak} days</b>\n"
        f"📆 Active This Month: <b>{days_this_month}/{days_in_month} days</b>\n"
        f"   {consistency_bar} {consistency_pct}%\n\n"
        f"🌿 Active This Year: <b>{days_this_year} days</b>\n"
        f"✅ Total Active Days: <b>{len(active_dates)} days</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{next_milestone_text}"
        f"<i>Come back every day to keep your streak alive! 🍃</i>"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="show_streak_calendar"),
            InlineKeyboardButton("⬅️ Back to Profile", callback_data="show_profile_page"),
        ],
    ])

    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    msg = await send_animated_translated(
        chat_id=chat_id,
        caption=text,
        animation_url=MORNING_GIF,
        reply_markup=keyboard,
    )
    await _remember(chat_id, msg.message_id)

# ══════════════════════════════════════════════════════════════════════════════
# MESSAGES / SCREENS
# ══════════════════════════════════════════════════════════════════════════════
async def send_delayed_feedback_buttons(
    chat_id: int,
    account_email: str,
    game_name: str,
    delay: int = 7200
):
    await asyncio.sleep(delay)

    # Fast Redis checks first
    fb_key = f"steam_fb:{chat_id}:{account_email}"
    if await redis_client.get(fb_key):
        return

    reminded_key = f"steam_reminded:{chat_id}:{account_email}"
    if await redis_client.get(reminded_key):
        return

    # Confirm claim marker exists (set during claim_steam_account)
    claim_marker = f"steam_claimed:{chat_id}:{account_email}"
    if not await redis_client.get(claim_marker):
        # Fallback: double-check DB in case Redis expired
        existing_claim = await _sb_get(
            "steam_claims",
            **{
                "chat_id": f"eq.{chat_id}",
                "account_email": f"eq.{account_email}",
                "select": "id",
            }
        ) or []
        if not existing_claim:
            print(f"[FEEDBACK] No claim found — skipping reminder for {chat_id}")
            return

    # Set reminded flag before sending to prevent race conditions
    await redis_client.setex(reminded_key, 90000, "1")

    try:
        await tg_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🎮 <b>Did {html.escape(game_name)} work?</b>\n\n"
                f"You claimed this account 2 hours ago.\n"
                f"Let us know so we can keep the forest clean! 🍃"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Working",
                        callback_data=f"stfb_ok|{account_email}|{game_name[:30]}"
                    ),
                    InlineKeyboardButton(
                        "❌ Not Working",
                        callback_data=f"stfb_bad|{account_email}|{game_name[:30]}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "⏳ Remind me tomorrow",
                        callback_data=f"remind_later|{account_email}|{game_name[:30]}"
                    )
                ]
            ])
        )
    except Exception as e:
        print(f"🔴 Failed to send feedback prompt: {e}")

async def send_reminder_feedback(
    chat_id: int,
    account_email: str,
    game_name: str,
    delay: int = 86400,  # 24 hours
    is_final: bool = True
):
    await asyncio.sleep(delay)

    fb_key = f"steam_fb:{chat_id}:{account_email}"
    if await redis_client.get(fb_key):
        return

    try:
        await tg_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🌿 <b>Last reminder!</b>\n\n"
                f"Did <b>{html.escape(game_name)}</b> work?\n\n"
                f"We won't ask again after this. 🍃"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Working",
                        callback_data=f"stfb_ok|{account_email}|{game_name[:30]}"
                    ),
                    InlineKeyboardButton(
                        "❌ Not Working",
                        callback_data=f"stfb_bad|{account_email}|{game_name[:30]}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🚫 Skip",
                        callback_data=f"skip_feedback|{account_email}"
                    )
                ]
            ])
        )
    except Exception as e:
        print(f"🔴 Final reminder failed: {e}")

# ── IMPROVED: Send animation + auto-translated caption ──
async def send_animated_translated(
    chat_id: int,
    caption: str,
    animation_url: str = None,
    lang: str = None,
    reply_markup=None,
    **kwargs
):
    if lang is None:
        lang = await get_user_language(chat_id)
   
    translated_caption = await translate_text(caption, lang)

    # ── GIF toggle check ──
    gifs_on = await get_gif_enabled(chat_id)

    if animation_url and gifs_on:
        msg = await tg_app.bot.send_animation(
            chat_id=chat_id,
            animation=animation_url,
            caption=translated_caption or None,
            parse_mode="HTML",
            reply_markup=reply_markup,
            **kwargs
        )
    else:
        msg = await tg_app.bot.send_message(
            chat_id=chat_id,
            text=translated_caption or None,
            parse_mode="HTML",
            reply_markup=reply_markup,
            **kwargs
        )
    return msg

async def send_initial_welcome(chat_id: int, first_name: str):
# ✅ Now correctly passes the name to _greeting()
    icon, greeting, gif_url = _greeting(first_name=first_name)

    # ── NEW: New users go through onboarding first ──
    profile = await get_user_profile(chat_id)
    is_new_user = profile is None
    onboarding_done = await has_completed_onboarding(chat_id)

    if is_new_user and not onboarding_done:
        await hide_bot_commands(chat_id)
        await send_onboarding_step(chat_id, first_name, step=1)
        return
    
    # ── Existing users or those who already completed onboarding ──
    caption = (
        f"{icon} {greeting}!\n\n"
        "🌿 Welcome, dear wanderer, to Clyde's Enchanted Clearing.\n\n"
        "Beneath the whispering ancient trees, a world of gentle magic awaits.\n"
        "Hidden wonders and peaceful moments are ready to be discovered.\n\n"
        "Tap the button below to step into the heart of the forest. 🍃✨"
    )
    
    msg = await send_animated_translated(
        chat_id=chat_id,
        animation_url=gif_url,
        caption=caption,
        reply_markup=kb_start(),
    )
    await _remember(chat_id, msg.message_id)

async def send_full_menu(chat_id: int, first_name: str, is_first_time: bool = False):
    icon, greeting, gif_url = _greeting(first_name=first_name)

    profile = await get_user_profile(chat_id)
    level      = profile.get("level", 1) if profile else 1
    title = profile.get("active_title") or get_level_title(level) if profile else get_level_title(level)
    level_info = f"🏷️ {title} • ⭐ Level {level}"
    if profile:
        await redis_client.delete(f"streak:{chat_id}")
    streak = await calculate_streak(chat_id) if profile else 0
    if streak >= 2:
        streak_txt = f'<tg-emoji emoji-id="4956499161319998529">🔥</tg-emoji> <b>{streak}-day streak!</b> The forest fire burns bright!'
    else:
        streak_txt = "🌱 <b>First steps in the forest!</b>"

    # ── NEW: Referral hint (only shown to existing users) ──
    referral_hint = "\n🌲 Invite friends → +25 XP each!" if not is_first_time else ""

    # ── Check for active event ──
    event = await get_active_event()
    event_banner = ""
    if event:
        bonus_line = ""
        bonus_type = event.get("bonus_type", "").strip()

        if bonus_type == "netflix_double":
            bonus_line = "\n🍿 <b>Netflix slots are doubled for all levels today!</b>"
        elif bonus_type == "netflix_max":
            bonus_line = "\n🍿 <b>Netflix slots are maximized for all levels today!</b>"

        countdown = await get_event_countdown(event)
        
        event_banner = (
            f"\n✦ ─────────────────── ✦\n"
            f"🎪 <b>FOREST EVENT</b>\n"
            f"✦ ─────────────────── ✦\n\n"
            f"🌸 <b>{event.get('title', '')}</b>\n"
            f"🕰️ {event.get('event_date', '')}{countdown}\n\n"
            f"<i>{event.get('description', '')}</i>\n"
            f"{bonus_line}\n\n"
            f"✦ ─────────────────── ✦\n"
        )

    if is_first_time:
        caption = (
            f"{icon} {greeting}!\n\n"
            "🌿 <b>Welcome to the Enchanted Clearing</b>\n\n"
            f"{level_info} • {streak_txt}{referral_hint}\n"
            f"{event_banner}"
            "Beneath the whispering ancient trees, many paths lie before you...\n\n"
            "🌱 <b>New wanderer?</b> We recommend starting with <b>Guidance</b> first.\n\n"
            "<i>Every view gives +8 XP • Every reveal gives +14 XP</i>\n\n"
            "<i>May your steps be guided by gentle forest magic.</i> 🍃✨"
        )
        keyboard = kb_first_time_menu()
    else:
        caption = (
            f"{icon} {greeting}!\n\n"
            "🌿 <b>Welcome back to the Enchanted Clearing</b>\n\n"
            f"{level_info} • {streak_txt}\n"
            f"{event_banner}"
            "The clearing welcomes you back, wanderer.\n\n"
            "<i>Every view gives +8 XP • Every reveal gives +14 XP</i>\n\n"
            "<i>May the forest welcome you once more.</i> 🍃✨"
        )
        keyboard = kb_main_menu()

    msg = await send_animated_translated(
        chat_id=chat_id,
        animation_url=gif_url,
        caption=caption,
        reply_markup=keyboard,
    )
    await _remember(chat_id, msg.message_id)

async def send_level_up_message(chat_id: int, first_name: str, old_level: int, new_level: int):

    # Show what they unlocked
    netflix_old = get_max_items("netflix", old_level)
    netflix_new = get_max_items("netflix", new_level)
    prime_old   = get_max_items("prime", old_level)
    prime_new   = get_max_items("prime", new_level)
    win_old     = get_max_items("win", old_level)
    win_new     = get_max_items("win", new_level)

    unlocks = []
    if netflix_new > netflix_old:
        unlocks.append(f"🍿 Netflix: {netflix_old} → <b>{netflix_new} cookies</b>")
    if prime_new > prime_old:
        unlocks.append(f"🎥 Prime: {prime_old} → <b>{prime_new} cookies</b>")
    if win_new > win_old:
        unlocks.append(f"🪟 Windows Keys: {win_old} → <b>{win_new} keys</b>")
    if new_level == 6:
        unlocks.append("✨ <b>You now receive the FRESHEST cookies first!</b>")

    unlock_text = "\n".join(unlocks) if unlocks else "More wonders await..."

    caption = (
        f"🌟 <b>LEVEL UP! Congratulations, {html.escape(first_name)}!</b>\n\n"
        f"⭐ <b>{old_level} → {new_level}</b>\n"
        f"🏷️ {get_level_title(new_level)}\n\n"
        f"🔓 <b>Newly Unlocked:</b>\n{unlock_text}\n\n"
        "<i>The forest grows with you.</i> 🍃✨"
    )
    try:
        msg = await send_animated_translated(
            chat_id=chat_id,
            animation_url=LOADING_GIF,
            caption=caption,
        )
        await _remember(chat_id, msg.message_id)  # ← ADD
    except Exception:
        pass

async def show_my_steam_claims(chat_id: int, first_name: str, query=None, page: int = 0):
    ITEMS_PER_PAGE = 5

    claims = await _sb_get(
        "steam_claims",
        **{
            "chat_id": f"eq.{chat_id}",
            "select": "game_name,account_email,claimed_at",
            "order": "claimed_at.desc",
            "limit": 500,
        }
    ) or []

    manila = pytz.timezone("Asia/Manila")
    now = datetime.now(manila)

    if not claims:
        text = (
            "🎮 <b>My Steam Collection</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🌫️ <b>Your collection is empty.</b>\n\n"
            "You haven't claimed any Steam accounts yet.\n"
            "Head over to the Steam section and grab your first game! 🍃\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>The forest holds many games waiting for you...</i> 🌲"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Find a Game", callback_data="vamt_filter_steam")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
        ])
        if query and query.message:
            try:
                await query.message.edit_caption(text, parse_mode="HTML", reply_markup=keyboard)
            except Exception:
                await tg_app.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await tg_app.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
        return

    total = len(claims)
    claims_today = await get_steam_claims_today(chat_id)

    week_ago = now - timedelta(days=7)
    claims_this_week = sum(
        1 for c in claims
        if c.get("claimed_at") and
        datetime.fromisoformat(c["claimed_at"].replace("Z", "+00:00")).astimezone(manila) >= week_ago
    )

    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    start = page * ITEMS_PER_PAGE
    page_claims = claims[start:start + ITEMS_PER_PAGE]

    # ── Check feedback status for page claims in parallel ──
    async def check_feedback(email: str) -> str:
        result = await _sb_get(
            "key_reports",
            **{
                "chat_id": f"eq.{chat_id}",
                "key_id": f"eq.{email}",
                "service_type": "eq.steam",
                "select": "status",
            }
        ) or []
        if not result:
            return ""
        return result[0].get("status", "")

    feedback_statuses = await asyncio.gather(*[
        check_feedback(c.get("account_email", "")) for c in page_claims
    ])

    # ── Store detail data in Redis for each claim ──
    for c in page_claims:
        email = c.get("account_email", "")
        short_key = f"{chat_id}_{abs(hash(email)) % 999999}"
        await redis_client.setex(f"cd:{short_key}", 3600, json.dumps({
            "email": email,
            "game_name": c.get("game_name", "Unknown Game"),
            "claimed_at": c.get("claimed_at", ""),
        }))

    # ── Tip rotation ──
    tips = [
        "Tap a game to view credentials & give feedback 🍃",
        "Feedback helps the Caretaker keep the forest clean 🌿",
        "Working accounts keep the clearing thriving ✨",
        "Report broken accounts so others aren't affected 🌲",
        "Your claims help shape the forest's future 🍃",
    ]
    tip = tips[page % len(tips)]

    # ── Build header ──
    text = (
        "🎮 <b>My Steam Collection</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 <b>{html.escape(first_name)}'s Game Library</b>\n\n"
        f"📦 {total} Total  •  📅 {claims_today} Today  •  🗓 {claims_this_week} This Week\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
    )

    buttons = []

    for i, (c, fb_status) in enumerate(zip(page_claims, feedback_statuses), start + 1):
        game = html.escape(c.get("game_name", "Unknown Game"))
        email = c.get("account_email", "")
        short_key = f"{chat_id}_{abs(hash(email)) % 999999}"

        # ── Feedback badge ──
        if fb_status == "working":
            fb_badge = "✅ Verified"
        elif fb_status == "not_working":
            fb_badge = "❌ Reported"
        else:
            fb_badge = "⏳ Pending"

        # ── Time ago ──
        try:
            dt = datetime.fromisoformat(c["claimed_at"].replace("Z", "+00:00"))
            dt_manila = dt.astimezone(manila)
            diff = now - dt_manila
            diff_hours = diff.total_seconds() / 3600

            if diff.days == 0 and diff_hours < 1:
                mins = int(diff.total_seconds() / 60)
                time_ago = f"🟢 {mins}m ago"
            elif diff.days == 0:
                time_ago = f"🟡 {int(diff_hours)}h ago"
            elif diff.days == 1:
                time_ago = "🔵 Yesterday"
            elif diff.days < 7:
                time_ago = f"🔵 {diff.days}d ago"
            else:
                time_ago = f"⚪ {dt_manila.strftime('%b %d, %Y')}"
        except Exception:
            time_ago = "⚪ Unknown"

        # ── Entry card ──
        text += (
            f"<b>{i}.</b> 🎮 <b>{game}</b>\n"
            f"     {time_ago}  ·  {fb_badge}\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        )

        # ── Button (keep original style — just the game name with fb icon) ──
        btn_icon = "✅" if fb_status == "working" else ("❌" if fb_status == "not_working" else "🎮")
        buttons.append([InlineKeyboardButton(
            f"{btn_icon} {game[:35]}",
            callback_data=f"steam_detail|{short_key}|{page}"
        )])

    # ── Footer ──
    text += (
        f"\n<i>{tip}</i>\n\n"
        f"📄 Page <b>{page + 1}</b> of <b>{total_pages}</b>"
    )

    # ── Navigation ──
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Previous", callback_data=f"myclaims_page_{page-1}"))
    # Center page indicator (disabled button)
    nav.append(InlineKeyboardButton(f"· {page + 1}/{total_pages} ·", callback_data="noop"))
    if start + ITEMS_PER_PAGE < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"myclaims_page_{page+1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🔍 Claim More Games", callback_data="vamt_filter_steam")])
    buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])

    markup = InlineKeyboardMarkup(buttons)

    if query and query.message:
        try:
            await query.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass

    await tg_app.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
    
async def show_steam_claim_detail(
    chat_id: int, 
    first_name: str, 
    short_key: str, 
    back_page: int, 
    query=None,
    show_full_stats: bool = False
):
    
    raw = await redis_client.get(f"cd:{short_key}")
    if not raw:
        await query.answer("⏳ Session expired. Please go back and try again.", show_alert=True)
        return

    data = json.loads(raw)
    email = data.get("email", "")
    game_name = data.get("game_name", "Unknown Game")
    claimed_at_raw = data.get("claimed_at", "")

    acc_data = await _sb_get(
        "steamCredentials",
        **{"email": f"eq.{email}", "select": "password,steam_id,games,image_url"},
    ) or []

    password = "—"
    steam_id = ""
    extra_games = []
    image_url = None
    
    if acc_data:
        acc = acc_data[0]
        password = acc.get("password", "—")
        steam_id = acc.get("steam_id", "") or ""
        extra_games = acc.get("games") or []
        image_url = acc.get("image_url")

    # ── Try to get a better logo from game_logos table ──
    logo_url = await get_game_logo_url(game_name, extra_games)
    final_image = logo_url or image_url

# ── Format claimed time ──
    try:
        manila = pytz.timezone("Asia/Manila")
        dt = datetime.fromisoformat(claimed_at_raw.replace("Z", "+00:00")).astimezone(manila)
        claimed_str = dt.strftime("%b %d, %Y • %I:%M %p")
        now = datetime.now(manila)
        diff = now - dt
        diff_h = diff.total_seconds() / 3600
        if diff_h < 1:
            ago = f"🟢 {int(diff.total_seconds()/60)}m ago"
        elif diff_h < 24:
            ago = f"🟡 {int(diff_h)}h ago"
        elif diff.days == 1:
            ago = "🔵 Yesterday"
        else:
            ago = f"🔵 {diff.days}d ago"
    except Exception:
        claimed_str = "—"
        ago = "—"

    # ── Check existing feedback ──
    fb_data = await _sb_get(
        "key_reports",
        **{"chat_id": f"eq.{chat_id}", "key_id": f"eq.{email}",
           "service_type": "eq.steam", "select": "status"}
    ) or []
    has_feedback = len(fb_data) > 0
    feedback_status = fb_data[0].get("status", "") if has_feedback else ""

    # ── Steam ID: Only show when it has value (no empty line) ──
    steam_id_line = ""
    if steam_id:
        steam_id_line = (
            f"🆔 <b>Steam ID:</b>\n"
            f"<code><tg-spoiler>{steam_id}</tg-spoiler></code>\n\n"
        )

    # ── Extra Games (Bundle): Only show when it's a bundle account (same clean style as Steam ID) ──
    extra_line = ""
    if extra_games and len(extra_games) > 0:
        preview = ", ".join(html.escape(g) for g in extra_games[:3])
        more = f" +{len(extra_games)-3} more" if len(extra_games) > 3 else ""
        extra_line = f"<b>🎮 Also includes:</b> <i>{preview}{more}</i>\n\n"

    # ── Fetch claim stats for this account ──
    claim_stats = await _sb_get(
        "steam_claims",
        **{"account_email": f"eq.{email}", "select": "chat_id"}
    ) or []
    total_claimers = len(claim_stats)

    # ── Fetch feedback stats ──
    fb_stats = await _sb_get(
        "key_reports",
        **{"key_id": f"eq.{email}", "service_type": "eq.steam", "select": "status"}
    ) or []
    working_count = sum(1 for f in fb_stats if f.get("status") == "working")
    bad_count = sum(1 for f in fb_stats if f.get("status") == "not_working")
    total_fb = working_count + bad_count
    rating_pct = round((working_count / total_fb) * 100) if total_fb > 0 else None

    # ── Build rating badge ──
    if rating_pct is None:
        rating_line = "⬜ No feedback yet"
    elif rating_pct >= 80:
        rating_line = f"🟢 {rating_pct}% working ({working_count}✅ {bad_count}❌)"
    elif rating_pct >= 50:
        rating_line = f"🟡 {rating_pct}% working ({working_count}✅ {bad_count}❌)"
    else:
        rating_line = f"🔴 {rating_pct}% working ({working_count}✅ {bad_count}❌)"

    # ── Top 3 games preview ──
    preview_games = [g for g in extra_games[:3] if g.strip()]
    games_preview = ""
    if preview_games:
        icons = ["🎯", "🎮", "🕹️"]
        games_preview = "\n" + "\n".join(
            f"   {icons[i]} {html.escape(g)}"
            for i, g in enumerate(preview_games)
        )
        if len(extra_games) > 3:
            games_preview += f"\n   ···  +{len(extra_games) - 3} more"

    # ── Feedback section ──
    if has_feedback:
        fb_emoji = "✅" if feedback_status == "working" else "❌"
        fb_label = "Working" if feedback_status == "working" else "Not Working"
        feedback_section = (
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"{fb_emoji} <b>Your feedback: {fb_label}</b>\n"
            f"<i>Changed your mind? Tap below to update.</i>"
        )
    else:
        feedback_section = (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Did this account work?</b>\n"
            f"<i>Tap below to let the Caretaker know 🍃</i>"
        )

    text = (
        f"🎮 <b>{html.escape(game_name)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📧 Email:\n"
        f"<tg-spoiler>{html.escape(email)}</tg-spoiler>\n\n"
        f"🔑 Password:\n"
        f"<tg-spoiler>{html.escape(password)}</tg-spoiler>\n\n"
        f"{steam_id_line}"
        f"{extra_line}"
        f"🕒 Claimed: <b>{claimed_str}</b>\n"
        f"     {ago}\n\n"
    )

    # ── Account Stats (hidden by default) ──
    if show_full_stats:
        text += (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Account Stats</b>\n"
            f"👥 Claimed by: <b>{total_claimers}</b> user{'s' if total_claimers != 1 else ''}\n"
            f"⭐ Rating: {rating_line}\n"
            f"{games_preview}\n"
            f"{feedback_section}"
        )
    else:
        text += "📊 <i>Tap the button below to see full account stats</i>\n\n"

    # ── Buttons ──
    buttons = []

    if not show_full_stats:
        buttons.append([InlineKeyboardButton("📊 Show Account Stats", callback_data=f"show_stats|{short_key}")])

    buttons.append([
        InlineKeyboardButton("✅ Working", callback_data=f"stfb_ok|{email}|{game_name[:30]}"),
        InlineKeyboardButton("❌ Not Working", callback_data=f"stfb_bad|{email}|{game_name[:30]}")
    ])


    if len(extra_games) > 3:
        buttons.append([InlineKeyboardButton(
            f"📋 See All {len(extra_games) + 1} Games",
            callback_data=f"show_all_games|{short_key}"
        )])

    buttons.append([InlineKeyboardButton(
        "↼ Back to My Claims",
        callback_data=f"myclaims_page_{back_page}"
    )])

    markup = InlineKeyboardMarkup(buttons)

    # ── Send / Edit message ──
    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    if final_image:
        try:
            await tg_app.bot.send_photo(
                chat_id=chat_id,
                photo=final_image,
                caption=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            return
        except Exception:
            pass

    # Fallback
    await tg_app.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

# ══════════════════════════════════════════════════════════════════════════════
# INVENTORY — PAGINATED COOKIES
# ══════════════════════════════════════════════════════════════════════════════
async def show_paginated_cookie_list(
    service_type: str, chat_id: int, query, page: int = 0
):
    if service_type == "netflix":
        title, emoji, accent = "Netflix", "🎬", "🔴"
    elif service_type == "crunchyroll":
        title, emoji, accent = "Crunchyroll", "🍜", "🟠"
    else:
        title, emoji, accent = "Prime Video", "📦", "🔵"

    # Loading states
    await query.message.edit_caption(
        caption=f"<i>Loading {title} cookies...</i>",
        parse_mode="HTML",
    )
    await asyncio.sleep(1.0)

    profile    = await get_user_profile(chat_id)
    user_level = profile.get("level", 1) if profile else 1
    event      = await get_active_event()
    max_items  = get_max_items(service_type, user_level, event)

    rem          = await get_remaining_reveals_and_views(chat_id)
    reveals_left = rem.get(service_type, 0)

    data = await get_vamt_data()
    if not data:
        await send_supabase_error(chat_id)
        try:
            await query.message.edit_caption(
                "🌫️ <b>Service temporarily unavailable.</b>\n\nPlease try again shortly.",
                parse_mode="HTML",
                reply_markup=kb_back_inventory(),
            )
        except Exception:
            pass
        return

    filtered = [
        item for item in data
        if service_type in str(item.get("service_type", "")).lower()
        and str(item.get("status", "")).lower() == "active"
        and int(item.get("remaining", 0)) > 0
    ]

    if not filtered:
        await query.message.edit_caption(
            caption=(
                f"{emoji} <b>No {title} cookies available</b>\n\n"
                "New cookies are added regularly.\n"
                "Check back soon! 🍃"
            ),
            parse_mode="HTML",
            reply_markup=kb_back_inventory(),
        )
        return

    def _freshness_sort_key(item):
        raw = item.get("last_updated")
        if not raw:
            return 0
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return 0

    filtered.sort(key=_freshness_sort_key)

    if user_level >= 6:
        filtered = filtered[-max_items:][::-1]
    else:
        filtered = filtered[:max_items]

    ITEMS_PER_PAGE = 5
    start       = page * ITEMS_PER_PAGE
    end         = start + ITEMS_PER_PAGE
    page_items  = filtered[start:end]
    total_pages = max(1, (len(filtered) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

    # ── Event banner (compact) ──
    event_line = ""
    if event:
        bonus_type = event.get("bonus_type", "").strip()
        if bonus_type == "netflix_double":
            event_line = "🎉 <b>Event active</b> — slots doubled!\n\n"
        elif bonus_type == "netflix_max":
            event_line = "🎉 <b>Event active</b> — slots maximized!\n\n"

    # ── Reveal bar ──
    all_bonus         = profile.get("all_slots_bonus", 0) if profile else 0
    daily_reveals_b   = profile.get("daily_reveals_bonus", 0) if profile else 0
    svc_bonus         = profile.get(f"{service_type}_reveals_bonus", 0) if profile else 0
    max_reveals       = get_max_daily_reveals(user_level, service_type) + svc_bonus + all_bonus + daily_reveals_b
    used_reveals      = max_reveals - reveals_left
    reveal_bar        = create_daily_progress_bar(used_reveals, max_reveals, length=8)

    # ── Header ──
    header = (
        f"{accent} <b>{title} Premium</b>  •  Lv{user_level}\n"
        f"{reveal_bar}  <b>{reveals_left}</b> reveals left\n\n"
        f"{event_line}"
        f"<b>{len(filtered)}</b> cookies available  •  Page {page + 1}/{total_pages}\n"
        f"──────────────────────\n\n"
    )

    # ── Cookie list ──
    body = ""
    buttons = []

    if user_level >= 6:
        tier_header = (
            "✨ <b>Freshest Cookies First</b>\n"
            "   Higher level = freshest stock, served first 🌟\n\n"
        )
    else:
        tier_header = (
            "📦 <b>Stable & Verified Cookies</b>\n"
            "   These are older but confirmed working 🍃\n\n"
        )

    body = tier_header

    for idx, item in enumerate(page_items, start=start + 1):
        raw_svc    = str(item.get("service_type") or service_type).strip()

        item_label = raw_svc if raw_svc != raw_svc.lower() else raw_svc.title()

        badge     = get_freshness_badge(item.get("last_updated"))
        dot       = badge[0]

        # ── CHANGE THIS: use full badge instead of compact map ──
        age_label = badge[2:].strip() if len(badge) > 1 else "Unknown age"
        remaining = item.get("remaining", 0)

       # REMOVE the old dict + for loop, replace with:
        region_flag = get_region_flag(raw_svc)

        body += f"{dot} <b>{item_label}</b>{region_flag}\n"
        body += f"   {age_label}  ·  {remaining} uses left\n\n"
        
        buttons.append([
            InlineKeyboardButton(
                f"Reveal  {dot}  {item_label}{region_flag}",
                callback_data=f"reveal_{service_type}|{idx}|{page}"
            )
        ])

    # ── Footer ──
    if user_level >= 6:
        priority_note = f"✨ Freshest first  •  Lv{user_level} → {max_items} slots"
    else:
        priority_note = f"📦 Stable & verified  •  Lv{user_level} → {max_items} slots"

    footer = (
        f"──────────────────────\n"
        f"{priority_note}"
    )

    # ── Navigation ──
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Prev", callback_data=f"{service_type}_page_{page - 1}"))
    if end < len(filtered):
        nav.append(InlineKeyboardButton("Next →", callback_data=f"{service_type}_page_{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton("❓ How to use cookies", callback_data=f"cookie_tutorial_{service_type}_1")
    ])
    buttons.append([
        InlineKeyboardButton("← Back", callback_data="check_vamt")
    ])

    caption = header + body + footer

    # Safety truncation
    if len(caption) > 1000:
        caption = caption[:950] + "\n<i>See next page for more</i>"

    await query.message.edit_caption(
        caption=caption,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    profile    = await get_user_profile(chat_id)
    first_name = profile.get("first_name", "Wanderer") if profile else "Wanderer"

    asyncio.create_task(
        check_and_award_achievements(chat_id, first_name, action=f"view_{service_type}")
    )

@safe_handler("reveal_cookie")
async def reveal_cookie(service_type: str, chat_id: int, first_name: str, query, idx: int, page: int):
    emoji = {"netflix": "🍿", "prime": "🎥", "crunchyroll": "🍜"}.get(service_type, "🎥")
    loading = None

    try:
        # ── Spam guard ──
        spam_key = f"reveal_spam:{chat_id}:{service_type}"
        if await redis_client.exists(spam_key):
            await query.answer("🌿 Slow down, wanderer! One reveal at a time 🍃", show_alert=True)
            return

        # Set immediately — use SET NX so concurrent requests can't both win
        acquired = await redis_client.set(spam_key, 1, ex=8, nx=True)
        if not acquired:
            await query.answer("🌿 Slow down, wanderer! One reveal at a time 🍃", show_alert=True)
            return

        # Now safe to validate data and consume cap
        profile = await get_user_profile(chat_id)
        user_level = profile.get("level", 1) if profile else 1
        event = await get_active_event()
        max_items = get_max_items(service_type, user_level, event)

        data = await get_vamt_data()
        if not data:
            await query.answer("🌫️ The forest is unreachable right now. Please try again shortly.", show_alert=True)
            return

        filtered = [
            item for item in data
            if service_type in str(item.get("service_type", "")).lower()
            and str(item.get("status", "")).lower() == "active"
            and int(item.get("remaining", 0)) > 0
        ]
        def _reveal_freshness_sort(item):
            raw = item.get("last_updated")
            if not raw:
                return 0
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                return 0

        filtered.sort(key=_reveal_freshness_sort)
        if user_level >= 6:
            filtered = filtered[-max_items:]
            filtered = filtered[::-1]
        else:
            filtered = filtered[:max_items]

        if idx < 1 or idx > len(filtered):
            await query.answer("❌ Cookie no longer available", show_alert=True)
            await show_paginated_cookie_list(service_type, chat_id, query, page)
            return

        item = filtered[idx - 1]

        if str(item.get("status", "")).lower() != "active" or int(item.get("remaining", 0)) <= 0:
            await query.answer("⚠️ This cookie has expired.", show_alert=True)
            await show_paginated_cookie_list(service_type, chat_id, query, page)
            return

        # ── Check bonus slots first ──
        bonus_key = f"daily_reveals_bonus:{chat_id}"
        bonus_slots = int(await redis_client.get(bonus_key) or 0)
        if bonus_slots > 0:
            await redis_client.decr(bonus_key)
        else:
            allowed, remaining = await try_consume_reveal_cap(chat_id, service_type)
            if not allowed:
                await query.message.edit_caption(
                    caption=(
                        f"{emoji} <b>Daily Reveal Limit Reached</b>\n\n"
                        f"🌿 You have already revealed your maximum cookies today.\n\n"
                        f"Come back tomorrow after midnight (Manila time) 🍃\n\n"
                        f"<i>Remaining reveals today: <b>{remaining}</b></i>"
                    ),
                    parse_mode="HTML",
                    reply_markup=kb_back_inventory(),
                )
                await query.answer("Daily limit reached", show_alert=False)
                return

        # ── Show loading (cap is now committed) ──
        loading = await send_loading(
            chat_id,
            f"{emoji} <i>Searching deep within the glowing glade for your cookie...</i>"
        )

        await asyncio.sleep(1.3)
        await safe_edit(loading,
            f"🌟 <i>The hidden {service_type} cookie spirit is slowly awakening...</i>\n\nPlease wait...")
        await asyncio.sleep(1.5)

        # ── Deliver the cookie ──
        cookie = str(item.get("key_id", "")).strip()
        display_name = str(item.get("display_name") or "").strip() or f"{service_type.title()} Cookie"

        # ── USE service_type as the title (it has the plan detail e.g. "Netflix Premium GB") ──
        raw_service_type = str(item.get("service_type") or "").strip()
        title_name = raw_service_type if raw_service_type else display_name

        action_map = {
            "netflix": "reveal_netflix",
            "prime": "reveal_prime",
            "crunchyroll": "reveal_crunchyroll",
        }
        action_name = action_map.get(service_type, "reveal_netflix")

        # Plan detail from service_type now, not display_name
        plan_detail = raw_service_type.replace("Netflix", "").replace("PrimeVideo", "").strip()
        plan_line = f"📋  Plan: <b>{plan_detail}</b>\n" if plan_detail and plan_detail.lower() != "cookie" else ""

        freshness    = get_freshness_badge(item.get("last_updated"))
        dot          = freshness[0]
        age_text     = freshness[2:].strip() if len(freshness) > 1 else "Unknown"
        service_name = {"netflix": "Netflix", "prime": "Prime Video", "crunchyroll": "Crunchyroll"}.get(service_type, "Prime Video")
        accent       = {"netflix": "🔴", "prime": "🔵", "crunchyroll": "🟠"}.get(service_type, "🔵")

        region_flag = get_region_flag(raw_service_type)

        caption = (
            f"{accent} <b>{title_name}</b>{region_flag}\n"
            f"──────────────────────\n\n"
            f"{dot}  Freshness: <b>{age_text}</b>\n"
            f"{plan_line}"
            f"📦  Uses remaining: <b>{item.get('remaining', 0)}</b>\n"
            f"🌐  Service: <b>{service_name}</b>\n"
            f"📄  Delivered as <code>.txt</code> file below\n\n"
            f"<i>Import the file using a cookie editor extension.</i>"
        )

        manila = pytz.timezone("Asia/Manila")
        revealed_at = datetime.now(manila).strftime('%Y-%m-%d at %H:%M:%S')

        file_content = (
            f"🌿🍃 Clyde's Enchanted Clearing — Secret {title_name} Cookie 🌿🍃\n"
            "══════════════════════════════════════════════════════════════\n"
            f"🌳 Cookie Spirit Awakened\n"
            f"Name : {title_name}\n"
            f"Plan : {plan_detail if plan_detail and plan_detail.lower() != 'cookie' else service_name}\n"
            f"Status : ✅ Working\n"
            f"Remaining: {item.get('remaining', 0)} uses\n"
            f"Revealed on : {revealed_at} (PH Time)\n"
            "🌲 Guard it well, wanderer.\n"
            "══════════════════════════════════════════════════════════════\n"
            f"{cookie}\n"
            "══════════════════════════════════════════════════════════════\n"
            "🍃 May this cookie bring you peaceful streams.\n"
            "— The Caretaker of the Enchanted Clearing 🌿\n"
        )

        file_bytes = BytesIO(file_content.encode("utf-8"))
        file_bytes.name = f"{display_name.replace(' ', '_')}.txt"

        await redis_client.setex(f"reveal_key:{chat_id}:{service_type}:{idx}", 3600, cookie)

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Working", callback_data=f"kfb_ok|{service_type}|{idx}"),
                InlineKeyboardButton("❌ Not Working", callback_data=f"kfb_bad|{service_type}|{idx}"),
            ],
            [InlineKeyboardButton("❓ How to use this?", callback_data=f"cookie_tutorial_{service_type}_1")],
            [InlineKeyboardButton(
                f"← Back to {service_name}",
                callback_data=f"back_to_{service_type}_list|{page}"
            )],
        ])

        doc_msg = await tg_app.bot.send_document(
            chat_id=chat_id,
            document=file_bytes,
            caption=caption,
            parse_mode="HTML",
            reply_markup=kb,
            filename=file_bytes.name
        )

        try:
            await loading.delete()
        except Exception:
            pass

        # Success only
        action_xp, _ = await add_xp(chat_id, first_name, action_name)
        if action_xp:
            asyncio.create_task(send_xp_feedback(chat_id, action_xp))

        # ── Check achievements after revealing a cookie ──
        asyncio.create_task(
            check_and_award_achievements(chat_id, first_name, action=action_name)
        )

        await redis_client.setex(
            f"reveal_msg:{chat_id}:{service_type}",
            3600,
            str(doc_msg.message_id)
        )

        asyncio.create_task(send_temporary_message(
            chat_id, f"✨ <i>{display_name} successfully delivered!</i>", duration=3
        ))

        try:
            await query.message.delete()
        except Exception:
            pass

    except Exception as e:
        print(f"🔴 CRITICAL error in reveal_cookie for {chat_id} ({service_type}): {e}")
        await query.answer("🌿 Something went wrong in the forest. Please try again.", show_alert=True)
        if loading:
            try:
                await loading.delete()
            except:
                pass

# ══════════════════════════════════════════════════════════════════════════════
# PROFILE / STATS / LEADERBOARD / HISTORY
# ══════════════════════════════════════════════════════════════════════════════
async def handle_referral_history(chat_id: int):
    data = await _sb_get(
        "referrals",
        **{
            "referrer_id": f"eq.{chat_id}",
            "select": "referred_id,created_at,awarded",
            "order": "created_at.desc",
            "limit": 20,
        }
    ) or []

    profile = await get_user_profile(chat_id)
    total_count = profile.get("referral_count", 0) if profile else 0
    total_xp = total_count * REFERRAL_XP

    if not data:
        text = (
            "🌲 <b>Your Referral History</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🌫️ You haven't referred anyone yet.\n\n"
            "Share your invite link and earn "
            f"<b>+{REFERRAL_XP} XP</b> per friend! 🍃"
        )
    else:
        manila = pytz.timezone("Asia/Manila")
        lines = []
        for i, r in enumerate(data, 1):
            try:
                dt = datetime.fromisoformat(
                    r["created_at"].replace("Z", "+00:00")
                ).astimezone(manila)
                date_str = dt.strftime("%b %d, %Y")
            except Exception:
                date_str = "—"

            status = "✅ Awarded" if r.get("awarded") else "⏳ Pending"
            lines.append(f"{i}. {status} — {date_str}")

        text = (
            "🌲 <b>Your Referral History</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Total Referred: <b>{total_count}</b>\n"
            f"✨ Total XP Earned: <b>{total_xp}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            + "\n".join(lines) +
            "\n\n<i>Keep inviting wanderers to grow the forest! 🍃</i>"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔗 Share My Link", 
            callback_data="invite_friends"
        )],
        [InlineKeyboardButton("⬅️ Back", callback_data="invite_friends")],
    ])

    msg = await send_animated_translated(
        chat_id=chat_id,
        caption=text,
        animation_url=INVITE_GIF,
        reply_markup=keyboard,
    )
    await _remember(chat_id, msg.message_id)

async def handle_online_users(chat_id: int, query=None):
    """🟢 Public online users page — active in last 15 minutes"""
    manila = pytz.timezone("Asia/Manila")
    now = datetime.now(manila)

    # ── 7. Redis cache (60 seconds) ──
    cache_key = "online_users_cache"
    cached = await redis_client.get(cache_key)
    if cached:
        active_users = json.loads(cached)
        print("⚡ [CACHE] Online users from Redis")
    else:
        ago_15min = (
            now - timedelta(minutes=15)
        ).astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

        active_users = await _sb_get(
            "user_profiles",
            **{
                "select": "first_name,level,last_active,chat_id,xp",
                "last_active": f"gte.{ago_15min}",
                "order": "last_active.desc",
                "limit": 50,
            }
        ) or []

        for u in active_users:
            if str(u.get("chat_id")) == str(OWNER_ID):
                u["first_name"] = "🌲 Forest Warden"

        await redis_client.setex(cache_key, 60, json.dumps(active_users))
        print(f"📡 [SUPABASE] Online users fetched — {len(active_users)} active")

    # ── 5. Timestamp for last refresh ──
    refresh_time = now.strftime("%I:%M %p")

    if not active_users:
        text = (
            "🌿 <b>Who's in the Clearing Right Now?</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🌫️ The forest is quiet...\n\n"
            "<i>No wanderers have been active in the last 15 minutes.</i>\n\n"
            "Be the first to explore! 🍃\n\n"
            f"🕒 <i>Last checked: {refresh_time}</i>"
        )
    else:
        lines = []
        for i, u in enumerate(active_users, 1):
            name = html.escape(u.get("first_name", "Wanderer"))
            level = u.get("level", 1)
            title = get_level_title(level)
            xp = u.get("xp", 0)

            # ── Time ago + presence dot ──
            try:
                dt = datetime.fromisoformat(
                    u["last_active"].replace("Z", "+00:00")
                ).astimezone(manila)
                diff_m = int((now - dt).total_seconds() / 60)

                # ── 3. Presence tiers ──
                if diff_m < 2:
                    dot = "🟢"
                    ago = "just now"
                elif diff_m < 8:
                    dot = "🟡"
                    ago = f"{diff_m} mins ago"
                else:
                    dot = "🔵"
                    ago = f"{diff_m} mins ago"

            except Exception:
                dot = "🔵"
                ago = "recently"

            lines.append(
                f"{i}. {dot} <b>{name}</b>\n"
                f"   {title} • Lv{level}\n"
                f"   🕒 {ago} • ✨ {xp:,} XP"
            )

        text = (
            f"🌿 <b>Who's in the Clearing Right Now?</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"✨ <b>{len(active_users)}</b> wanderer(s) active in the last 15 minutes\n\n"
            # ── 3. Legend ──
            f"🟢 Just now  🟡 Recent  🔵 Last 15 min\n\n"
            + "\n\n".join(lines)
            + "\n\n━━━━━━━━━━━━━━━━━━\n"
            # ── 5. Refresh timestamp ──
            f"🕒 <i>Last updated: {refresh_time} • Tap refresh for latest</i> 🍃"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="show_online_users")],
        [InlineKeyboardButton("⬅️ Back to Clearing", callback_data="main_menu")],
    ])

    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    msg = await send_animated_translated(
        chat_id=chat_id,
        animation_url=MORNING_GIF,
        caption=text,
        reply_markup=keyboard,
    )
    await _remember(chat_id, msg.message_id) 
    
async def handle_profile_page(chat_id: int, first_name: str, query=None, page: int = 0):
    """Fixed version — shows achievement summary directly in profile"""
    profile = await get_user_profile(chat_id)
    if not profile:
        await tg_app.bot.send_message(chat_id, "🌿 Please use /start first.")
        return
    
    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    loading = await send_loading(chat_id, "🌿 <i>Reading your forest soul...</i>")

    # ✅ Award XP FIRST before fetching profile for display
    action_xp, _ = await add_xp(chat_id, first_name, "profile")
    if action_xp:
        asyncio.create_task(send_xp_feedback(chat_id, action_xp))

    # ✅ NOW fetch fresh profile with updated XP
    profile = await get_user_profile(chat_id)
    if not profile:
        return

    level = profile.get("level", 1)
    xp = profile.get("xp", 0)
    xp_required_next = get_cumulative_xp_for_level(level + 1)
    streak = await calculate_streak(chat_id)

    # === ACHIEVEMENT SUMMARY ===
    user_achs = await get_user_achievements(chat_id)
    unlocked = [a for a in user_achs if a.get("unlocked_at")]
    unlocked_codes_set = {u["achievement_code"] for u in user_achs if u.get("unlocked_at")}
    visible_total = len([
        code for code, a in ACHIEVEMENTS_CACHE.items()
        if not a.get("is_secret", False) or code in unlocked_codes_set
    ])
    unlocked_count = len(unlocked)

    ach_summary = (
        f"🏆 <b>Achievements</b>: {unlocked_count}/{visible_total} visible "
        f"({round((unlocked_count / visible_total) * 100) if visible_total > 0 else 0}%)\n"
        f"{create_progress_bar(unlocked_count, visible_total, length=10)}\n\n"
    )

    # ── Daily limits progress
    rem = await get_remaining_reveals_and_views(chat_id)
    level_for_calc = profile.get("level", 1)

    _all_b = profile.get("all_slots_bonus", 0)
    _dr_b = profile.get("daily_reveals_bonus", 0)
    _nf_max = get_max_daily_reveals(level_for_calc, "netflix") + profile.get("netflix_reveals_bonus", 0) + _all_b + _dr_b
    _pr_max = get_max_daily_reveals(level_for_calc, "prime") + profile.get("prime_reveals_bonus", 0) + _all_b + _dr_b
    _cr_max = get_max_daily_reveals(level_for_calc, "crunchyroll") + profile.get("crunchyroll_reveals_bonus", 0) + _all_b + _dr_b
    _win_max = get_max_daily_views(level_for_calc, "windows") + profile.get("windows_views_bonus", 0) + _all_b
    _off_max = get_max_daily_views(level_for_calc, "office") + profile.get("windows_views_bonus", 0) + _all_b

    daily_section = (
        "📊 <b>Today's Forest Limits</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"╭🍿 Netflix Reveals: <b>{rem['netflix']}</b> left\n"
        f"╰{create_daily_progress_bar(_nf_max - rem['netflix'], _nf_max)}\n\n"
        f"╭🎥 Prime Reveals: <b>{rem['prime']}</b> left\n"
        f"╰{create_daily_progress_bar(_pr_max - rem['prime'], _pr_max)}\n\n"
        f"╭🍜 Crunchyroll Reveals: <b>{rem['crunchyroll']}</b> left\n"
        f"╰{create_daily_progress_bar(_cr_max - rem['crunchyroll'], _cr_max)}\n\n"
        f"╭🪟 Windows Keys: <b>{rem['windows']}</b> left\n"
        f"╰{create_daily_progress_bar(_win_max - rem['windows'], _win_max)}\n\n"
        f"╭📑 Office Keys: <b>{rem['office']}</b> left\n"
        f"╰{create_daily_progress_bar(_off_max - rem['office'], _off_max)}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
    )

    joined_date = "Unknown"
    if profile.get("created_at"):
        try:
            dt = datetime.fromisoformat(profile["created_at"].replace("Z", "+00:00"))
            joined_date = dt.strftime("%B %d, %Y")
        except Exception:
            pass

    last_active = "Just now"
    if profile.get("last_active"):
        try:
            dt = datetime.fromisoformat(profile["last_active"].replace("Z", "+00:00"))
            dt = dt.astimezone(pytz.timezone("Asia/Manila"))
            diff_m = int((datetime.now(pytz.timezone("Asia/Manila")) - dt).total_seconds() / 60)
            if diff_m < 2:
                last_active = "Just now"
            elif diff_m < 60:
                last_active = f"{diff_m} minutes ago"
            else:
                last_active = dt.strftime("%B %d, %Y • %I:%M %p")
        except Exception:
            pass

    streak_txt = f"🔥 {streak}-day streak!" if streak >= 2 else "🌱 Just getting started!"
    title = profile.get("active_title") or get_level_title(level)

    pages = {
        0: (  # Page 1: XP + Achievements + Daily Limits
            f"👤 <b>{html.escape(first_name)}'s Forest Profile</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"🏷️ <b>{title}</b>\n"
            f"⭐ Level <b>{level}</b> • {streak_txt}\n\n"
            f"✨ <b>XP:</b> {xp:,} / {xp_required_next:,}\n"
            f"{create_progress_bar(xp, xp_required_next, 12)}\n"
            f"📈 To next level: <b>{max(0, xp_required_next - xp):,} XP</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{ach_summary}"
            f"{daily_section}"
            f"🌱 Joined: <b>{joined_date}</b>\n"
            f"🌲 Last Active: <b>{last_active}</b>"
        ),
        1: (  # Page 2: Activity + Resource Usage + Wheel
            f"👤 <b>{html.escape(first_name)}'s Forest Profile</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "📊 <b>Activity</b>\n\n"
            f"• Total XP Earned: <b>{profile.get('total_xp_earned', xp):,}</b>\n"
            f"• Profile Logo Changes: <b>{profile.get('profile_gif_changes', 0)}</b>\n"
            f"• Days Active: <b>{profile.get('days_active', 0)}</b>\n"
            f"• Friends Referred: <b>{profile.get('referral_count', 0)}</b>\n"
            f"• Profile Views: <b>{profile.get('profile_views', 0)}</b>\n"
            f"• Guidance Read: <b>{profile.get('guidance_reads', 0)}</b>\n"
            f"• Lore Read: <b>{profile.get('lore_reads', 0)}</b>\n"
            f"• Times Cleared: <b>{profile.get('times_cleared', 0)}</b>\n\n"
            "🎮 <b>Resource Usage</b>\n\n"
            f"• Windows Keys Viewed: <b>{profile.get('windows_views', 0)}</b>\n"
            f"• Office Keys Viewed: <b>{profile.get('office_views', 0)}</b>\n"
            f"• Netflix Viewed: <b>{profile.get('netflix_views', 0)}</b>\n"
            f"• Netflix Revealed: <b>{profile.get('netflix_reveals', 0)}</b>\n"
            f"• PrimeVideo Viewed: <b>{profile.get('prime_views', 0)}</b>\n"
            f"• PrimeVideo Revealed: <b>{profile.get('prime_reveals', 0)}</b>\n"
            f"• Crunchyroll Viewed: <b>{profile.get('crunchyroll_views', 0)}</b>\n"
            f"• Crunchyroll Revealed: <b>{profile.get('crunchyroll_reveals', 0)}</b>\n"
            f"• Steam Claimed: <b>{profile.get('steam_claims_count', 0)}</b>\n\n"
            "🎰 <b>Wheel of Whispers</b>\n\n"
            f"• Total Spins: <b>{profile.get('total_wheel_spins', 0)}</b>\n"
            f"• Legendary Spins: <b>{profile.get('legendary_spins', 0)}</b>\n"
            f"• Wheel XP Earned: <b>{profile.get('wheel_xp_earned', 0):,}</b>\n\n"
            "<i>The trees remember every step of your journey.</i> 🍃"
        ),
    }

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("↼ Prev", callback_data=f"profile_page_{page-1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page+1}/2", callback_data="noop"))
    if page < 1:
        nav_row.append(InlineKeyboardButton("Next ⇀", callback_data=f"profile_page_{page+1}"))

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🖼️ Change Profile", callback_data="change_profile_logo"),
            InlineKeyboardButton("🏷️ Set Title", callback_data="show_set_title"),
        ],
        [
            InlineKeyboardButton("📜 XP History", callback_data="history_page_0"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard_from_profile"),
        ],
        [
            InlineKeyboardButton("🏆 Achievements", callback_data="show_achievements"),
            InlineKeyboardButton("📅 Streak Calendar", callback_data="show_streak_calendar"),
        ],
        nav_row,
        [InlineKeyboardButton("⬅️ Back to Clearing", callback_data="main_menu")],
    ])

    caption = pages.get(page, pages[0]) 
    gif_id = profile.get("profile_gif_id") if profile else None

    if gif_id:
        try:
            msg = await tg_app.bot.send_animation(
                chat_id=chat_id,
                animation=gif_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            try:
                msg = await tg_app.bot.send_photo(
                    chat_id=chat_id,
                    photo=gif_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            except Exception:
                tg_photo = await get_user_telegram_photo(chat_id)
                if tg_photo:
                    msg = await tg_app.bot.send_photo(
                        chat_id=chat_id,
                        photo=tg_photo,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=keyboard
                    )
                else:
                    msg = await send_animated_translated(
                        chat_id=chat_id,
                        animation_url=MYID_GIF,
                        caption=caption,
                        reply_markup=keyboard,
                    )
    else:
        tg_photo = await get_user_telegram_photo(chat_id)
        if tg_photo:
            msg = await tg_app.bot.send_photo(
                chat_id=chat_id,
                photo=tg_photo,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            msg = await send_animated_translated(
                chat_id=chat_id,
                animation_url=MYID_GIF,
                caption=caption,
                reply_markup=keyboard,
            )

    try:
        await loading.delete()
    except Exception:
        pass

    await _remember(chat_id, msg.message_id)

async def show_achievements_page(chat_id: int, query=None, page: int = 0):
    """Paginated achievements page — hides secret ones until unlocked"""
    ITEMS_PER_PAGE = 8   # You can change this (recommended 6–10)

    user_achs = await get_user_achievements(chat_id)
    unlocked_codes = {u["achievement_code"] for u in user_achs if u.get("unlocked_at")}

    # Filter out hidden secret achievements
    visible_achs = []
    for code, ach in ACHIEVEMENTS_CACHE.items():
        if ach.get("is_secret", False) and code not in unlocked_codes:
            continue
        visible_achs.append((code, ach))

    # ── Sort: locked first, unlocked last ──
    visible_achs.sort(key=lambda x: x[0] in unlocked_codes)

    total_achs = len(visible_achs)
    total_pages = (total_achs + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

    if total_achs == 0:
        text = "🌿 No achievements found yet."
    else:
        start = page * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_achs = visible_achs[start:end]

        text = f"🏆 <b>Forest Achievements</b>  ({page+1}/{total_pages})\n"
        text += "━━━━━━━━━━━━━━━━━━\n\n"

        hidden_count = 0
        for code, ach in page_achs:
            is_unlocked = code in unlocked_codes
            is_secret = ach.get("is_secret", False)

            if is_secret and not is_unlocked:
                hidden_count += 1
                continue

            # Use database icon first, fallback to rarity-based emoji
            icon = ach.get("icon") or ""
            if not icon:
                rarity_map = {
                    "common": "🌿", 
                    "rare": "✨", 
                    "epic": "🌟", 
                    "legendary": "🌠", 
                    "mythic": "🪐"
                }
                icon = rarity_map.get(ach.get("rarity", "epic"), "🌱")

            status = "✅" if is_unlocked else "🔒"

            text += f"{status} {icon} <b>{ach['name']}</b>\n"
            text += f"   {ach['description']}\n"

            if not is_unlocked and ach.get("condition", {}).get("type") == "count":
                field = ach["condition"].get("field")
                required = ach["condition"].get("required", 0)
                current = (await get_user_profile(chat_id) or {}).get(field, 0)
                text += f"   Progress: {current}/{required} {create_progress_bar(current, required, length=8)}\n"

            text += "\n"

        text += f"━━━━━━━━━━━━━━━━━━\n📄 Page {page+1} of {total_pages} • {total_achs} visible achievements"

    # Navigation buttons
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("↼ Previous", callback_data=f"ach_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ⇀", callback_data=f"ach_page_{page+1}"))

    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("⬅️ Back to Profile", callback_data="show_profile_page")])

    markup = InlineKeyboardMarkup(buttons)

    if query and query.message:
        try:
            await query.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass  # fallback if edit fails

    msg = await send_animated_translated(
        chat_id=chat_id,
        caption=text,
        animation_url=MIDNIGHT_GIF,
        reply_markup=markup
    )
    await _remember(chat_id, msg.message_id)

async def handle_cookie_tutorial(chat_id: int, service: str = "netflix", page: int = 1, query=None):

    emoji_map = {"netflix": "🍿", "prime": "🎥", "crunchyroll": "🍜"}
    name_map  = {"netflix": "Netflix", "prime": "Prime Video", "crunchyroll": "Crunchyroll"}
    domain_map = {"netflix": "netflix.com", "prime": "primevideo.com", "crunchyroll": "crunchyroll.com"}

    emoji  = emoji_map.get(service, "🍜")
    name   = name_map.get(service, service.title())
    domain = domain_map.get(service, "crunchyroll.com")
    btn_label = f"{emoji} Get a {name} Cookie Now"

    pages = {
        1: (
            f"{emoji} <b>How to Use a {name} Cookie — Page 1/3</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "📋 <b>What you need:</b>\n"
            "• A PC or laptop (Chrome/Firefox)\n"
            f"• The {name} cookie file from the bot\n"
            "• A cookie editor extension\n\n"
            "🔧 <b>Step 1 — Install Extension</b>\n\n"
            "Chrome:\n"
            "→ Install <b>EditThisCookie</b> or <b>Cookie-Editor</b>\n"
            "→ Search it on Chrome Web Store\n\n"
            "Firefox:\n"
            "→ Install <b>Cookie-Editor</b> from Firefox Add-ons\n\n"
            "<i>Tap Next → to continue</i>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Next ⇀", callback_data=f"cookie_tutorial_{service}_2")],
                [InlineKeyboardButton("⬅️ Back", callback_data="check_vamt")],
            ])
        ),
        2: (
            f"{emoji} <b>How to Use a {name} Cookie — Page 2/3</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🔧 <b>Step 2 — Prepare the Cookie</b>\n\n"
            "1. Open the <b>.txt file</b> the bot sent you\n"
            "2. Copy <b>everything</b> inside it\n\n"
            "🔧 <b>Step 3 — Import the Cookie</b>\n\n"
            "1. Open your browser\n"
            f"2. Go to <b>{domain}</b>\n"
            "   (don't log in — just open the site)\n"
            "3. Click your cookie extension icon\n"
            "4. Click <b>Import</b> or paste into the text box\n"
            "5. Paste the cookie content\n"
            "6. Click <b>Save</b> or <b>Import</b>\n\n"
            "<i>Tap Next → for the final step</i>",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("↼ Previous", callback_data=f"cookie_tutorial_{service}_1"),
                    InlineKeyboardButton("Next ⇀", callback_data=f"cookie_tutorial_{service}_3"),
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data="check_vamt")],
            ])
        ),
        3: (
            f"{emoji} <b>How to Use a {name} Cookie — Page 3/3</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🔧 <b>Step 4 — Access the Account</b>\n\n"
            f"1. After importing, <b>refresh</b> {domain}\n"
            "2. You should now be logged in ✅\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "⚠️ <b>Important Rules</b>\n\n"
            "• Do <b>NOT</b> change the password\n"
            "• Do <b>NOT</b> change email or account info\n"
            "• Do <b>NOT</b> sign out other sessions\n"
            "• Use in <b>private/incognito</b> mode when possible\n"
            f"• {name} cookies expire — if it stops working, get a new one\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "❌ <b>Cookie not working?</b>\n"
            f"→ Tap the <b>❌ Not Working</b> button on your {name} cookie file\n"
            "   to report it to the Caretaker 🍃",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("↼ Previous", callback_data=f"cookie_tutorial_{service}_2")],
                [InlineKeyboardButton(btn_label, callback_data=f"vamt_filter_{service}"
                )],
                [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")],
            ])
        ),
    }

    text, keyboard = pages.get(page, pages[1])

    if query and query.message:
        try:
            await query.message.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return
        except Exception:
            pass

    msg = await send_animated_translated(
        chat_id=chat_id,
        caption=text,
        animation_url=COOKIE_TUTORIAL_GIF,
        reply_markup=keyboard
    )
    await _remember(chat_id, msg.message_id)

async def handle_set_title(chat_id: int, first_name: str, query=None):
    profile = await get_user_profile(chat_id)
    if not profile:
        return

    current_level = profile.get("level", 1)
    active_title  = profile.get("active_title")

    # Build list of unlocked titles (all levels UP TO current level)
    unlocked = {
        lvl: data
        for lvl, data in LEVEL_TITLE_DESCRIPTIONS.items()
        if lvl <= current_level
    }

    current_display = active_title or LEVEL_TITLE_DESCRIPTIONS.get(current_level, ("", ""))[0]

    text = (
        "🏷️ <b>Choose Your Forest Title</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 <b>Active:</b> {current_display}\n"
        f"⭐ You are Level <b>{current_level}</b> — "
        f"<b>{len(unlocked)}</b> title(s) unlocked\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
    )

    # Show each unlocked title with description
    for lvl in sorted(unlocked.keys(), reverse=True):  # newest first
        title_name, description = unlocked[lvl]
        is_active = (active_title == title_name) or \
                    (not active_title and lvl == current_level)
        lock_icon = "✅" if is_active else f"Lv{lvl}"
        text += f"{lock_icon} <b>{title_name}</b>\n"
        text += f"   <i>{description}</i>\n\n"

    text += "━━━━━━━━━━━━━━━━━━\n"
    text += "<i>Tap a title below to equip it. 🍃</i>"

    # Build buttons
    buttons = []
    for lvl in sorted(unlocked.keys(), reverse=True):
        title_name, _ = unlocked[lvl]
        is_active = (active_title == title_name) or \
                    (not active_title and lvl == current_level)
        label = f"✅ {title_name}" if is_active else f"🏷️ {title_name}"
        buttons.append([
            InlineKeyboardButton(label, callback_data=f"set_title|{title_name[:50]}")
        ])

    # Reset only if using a non-default title
    default_title = LEVEL_TITLE_DESCRIPTIONS.get(current_level, ("", ""))[0]
    if active_title and active_title != default_title:
        buttons.append([
            InlineKeyboardButton("🔄 Reset to Current Level Title", callback_data="reset_title")
        ])

    buttons.append([InlineKeyboardButton("⬅️ Back to Profile", callback_data="show_profile_page")])

    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    await send_animated_translated(
        chat_id=chat_id,
        animation_url=MYID_GIF,
        caption=text,
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_history(chat_id: int, first_name: str, page: int = 0):
    limit  = 8
    offset = page * limit

    profile     = await get_user_profile(chat_id)
    level       = profile.get("level", 1) if profile else 1
    total_xp    = profile.get("total_xp_earned", 0) if profile else 0
    title_str   = get_level_title(level)

    manila = pytz.timezone("Asia/Manila")
    today_utc = (
        datetime.now(manila).replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )

    # ── single Supabase call, everything filtered in Python ──
    all_logs = await _sb_get(
        "xp_history",
        **{
            "chat_id": f"eq.{chat_id}",
            "select":  "*",
            "order":   "created_at.desc",
            "limit":   1000,
        },
    )

    if all_logs is None:
        await send_supabase_error(chat_id)
        return
    all_logs = all_logs or []  # empty list is valid
    # ── NEW PATTERN END ──
    
    # total count
    total_entries = len(all_logs)

    # paged logs
    logs = all_logs[offset: offset + limit]

    # xp earned today (filter in Python)
    xp_today = sum(
        r.get("xp_earned", 0) for r in all_logs
        if r.get("created_at", "") >= today_utc
    )

    streak = await calculate_streak(chat_id)
    streak_txt = f"You're on a {streak}-day XP streak! 🔥" if streak >= 2 else "Welcome to your journey! 🌱"

    action_count = Counter(r.get("action") for r in all_logs if r.get("action"))
    top_action_name  = "None yet"
    top_action_count = 0
    if action_count:
        top, cnt = action_count.most_common(1)[0]
        top_action_name  = top.replace("_", " ").title()
        top_action_count = cnt

    if not logs and page == 0:
        await tg_app.bot.send_message(
            chat_id, "🌱 No steps recorded yet.\nStart exploring the clearing to grow!"
        )
        return

    lines = [
        f"🌟 <b>{html.escape(first_name)}'s XP Journey</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🏷️ <b>Current Title:</b> {title_str} • Level {level}",
        f"✨ <b>Total XP Earned:</b> {total_xp:,}",
        f"🌞 <b>XP Today:</b> {xp_today:,}",
        f"🔥 <b>Streak:</b> {streak_txt}",
        f"🏆 <b>Most Used:</b> {top_action_name} ({top_action_count} times)",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for log in logs:
        action_name = log.get("action", "Unknown").replace("_", " ").title()
        try:
            dt       = datetime.fromisoformat(log["created_at"].replace("Z", "+00:00"))
            time_str = dt.astimezone(manila).strftime("%Y-%m-%d %H:%M")
        except Exception:
            time_str = str(log.get("created_at", ""))[:16]

        main_line = f" {action_name} → +{log.get('xp_earned', 0)} XP"
        if log.get("leveled_up") and log.get("new_level", 1) > 1:
            main_line += f" → <b>Level {log.get('new_level')} 🎉</b>"

        lines += [
            f"🕒 {time_str}",
            main_line,
            f" {log.get('previous_xp', 0)} → {log.get('new_xp', 0)} XP",
            "",
        ]

    total_pages = max(1, (total_entries + limit - 1) // limit)
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        f"🌱 Page {page + 1} of {total_pages} • The trees remember every step of your growth... 🍃",
    ]

    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("↼ Previous", callback_data=f"history_page_{page - 1}"))
    if (page + 1) * limit < total_entries:
        buttons.append(InlineKeyboardButton("Next ⇀", callback_data=f"history_page_{page + 1}"))

    await tg_app.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([buttons]) if buttons else None,
    )


async def handle_leaderboard(chat_id: int):
    MIN_LEVEL = 3

    profile = await get_user_profile(chat_id)
    if chat_id != OWNER_ID:
        if not profile or profile.get("level", 1) < MIN_LEVEL:
            current = profile.get("level", 1) if profile else 1
            await tg_app.bot.send_message(
                chat_id,
                f"🏆 <b>Guardians of the Enchanted Clearing</b>\n\n"
                f"🌲 The ancient trees guard the leaderboard.\n\n"
                f"Reach <b>Level {MIN_LEVEL}</b> to unlock it.\n\n"
                f"You are currently <b>Level {current}</b>.\n\n"
                "Keep exploring, gaining XP, and the trees will reveal the rankings. 🌱",
                parse_mode="HTML",
            )
            return

    top_data = await _sb_get(
        "user_profiles",
        **{"select": "first_name,xp,level,chat_id", "xp": "gt.0", "order": "xp.desc", "limit": 10},
    )

    if top_data is None:
        await tg_app.bot.send_message(
            chat_id,
            "🏆 <b>Guardians of the Enchanted Clearing</b>\n\n"
            "🌲 No one has earned any XP yet.\nBe the first to explore! 🌱✨",
            parse_mode="HTML",
        )
        return
    
    top_data = top_data or []
    if not top_data:
        await tg_app.bot.send_message(chat_id, "🌲 No one has earned any XP yet...")
        return

    text = "🏆 <b>Guardians of the Enchanted Clearing</b>\n━━━━━━━━━━━━━━━━━━\n\n"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for rank, u in enumerate(top_data, 1):
        name  = "The Forest Warden" if str(u.get("chat_id")) == str(OWNER_ID) else html.escape(u.get("first_name", "Unknown"))
        medal = medals.get(rank, f"{rank}.")
        text += f"{medal} <b>{name}</b>\n   {get_level_title(u.get('level', 1))} • Level {u.get('level', 1)}\n   ✨ {u.get('xp', 0):,} XP\n\n"

    if profile and profile.get("xp", 0) > 0:
        rank_data = await _sb_get("user_leaderboard", **{"chat_id": f"eq.{chat_id}", "select": "rank"}) or []
        real_rank = rank_data[0].get("rank", "?") if rank_data else "?"
        text += "━━━━━━━━━━━━━━━━━━\n"
        display = "The Forest Warden" if chat_id == OWNER_ID else f"ranked #{real_rank}"
        text += f"📍 <b>You are currently {display}</b>\n   {get_level_title(profile.get('level', 1))} • Level {profile.get('level', 1)}\n   ✨ {profile.get('xp', 0):,} XP\n"

    text += "\n<i>May your roots grow deep and your light shine through the canopy.</i> 🍃✨"
    await tg_app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

async def handle_wheel_leaderboard(chat_id: int):
    """🏆 Wheel of Whispers Leaderboard - Top Spinners"""
    top_data = await _sb_get(
        "user_profiles",
        **{
            "select": "chat_id,first_name,total_wheel_spins,wheel_xp_earned,legendary_spins,level",
            "total_wheel_spins": "gt.0",
            "order": "wheel_xp_earned.desc",
            "limit": 10
        }
    ) or []

    if not top_data:
        await tg_app.bot.send_message(
            chat_id,
            "🌿 <b>No one has spun the wheel yet...</b>\n\n"
            "Be the first to test your luck! ✨",
            parse_mode="HTML"
        )
        return

    text = (
        "🌟 <b>Wheel of Whispers Leaderboard</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🏆 <b>Top Spinners of the Forest</b>\n\n"
    )

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for rank, user in enumerate(top_data, 1):
        # ✅ FIX 2: chat_id is now in select so this comparison works correctly
        name = "The Forest Warden" if str(user.get("chat_id", "")) == str(OWNER_ID) else html.escape(user.get("first_name", "Unknown"))
        medal = medals.get(rank, f"{rank}.")
        spins = user.get("total_wheel_spins", 0)
        legendaries = user.get("legendary_spins", 0)
        wheel_xp = user.get("wheel_xp_earned", 0)

        text += (
            f"{medal} <b>{name}</b>\n"
            f"   🌿 {spins} spins • 🔥 {legendaries} Legendaries\n"
            f"   ✨ {wheel_xp:,} Wheel XP\n\n"
        )

    # ✅ FIX 3: Accurate rank using count of users with MORE spins
    profile = await get_user_profile(chat_id)
    if profile and profile.get("total_wheel_spins", 0) > 0:
        user_spins = profile.get("total_wheel_spins", 0)

        rank_data = await _sb_get(
            "user_profiles",
            **{
                "select": "chat_id",
                "wheel_xp_earned": f"gt.{profile.get('wheel_xp_earned', 0)}",
            }
        ) or []
        user_rank = len(rank_data) + 1

        # Check if the current user is already visible in top 10
        top_ids = [str(u.get("chat_id", "")) for u in top_data]
        already_shown = str(chat_id) in top_ids

        text += "━━━━━━━━━━━━━━━━━━\n"

        if not already_shown:
            # Only show the "Your rank" footer if user isn't already in the top 10 list
            display_name = "The Forest Warden" if chat_id == OWNER_ID else "You"
            text += (
                f"📍 <b>{display_name} — Rank #{user_rank}</b>\n"
                f"   ✨ {profile.get('wheel_xp_earned', 0):,} Wheel XP • "
                f"🌿 {user_spins} spins • "
                f"🔥 {profile.get('legendary_spins', 0)} Legendaries\n"
            )
        else:
            text += f"📍 <b>You are ranked #{user_rank} in the forest!</b>\n"

    elif profile:
        # User exists but has never spun
        text += (
            "━━━━━━━━━━━━━━━━━━\n"
            "📍 <b>You haven't spun the wheel yet!</b>\n"
            "   Try your luck to join the leaderboard 🌿\n"
        )

    text += "\n<i>May your spins bring you great fortune, wanderer...</i> 🍃✨"

    msg = await send_animated_translated(
        chat_id=chat_id,
        caption=text,
        animation_url=WHEEL_BOARD_GIF,
    )
    await _remember(chat_id, msg.message_id)

# ══════════════════════════════════════════════════════════════════════════════
# FEEDBACK
# ══════════════════════════════════════════════════════════════════════════════
async def handle_feedback(chat_id: int, first_name: str, feedback_text: str):
    # ── Rate limit: 3 per day ──
    key = f"feedback_limit:{chat_id}"
    count = await redis_client.incr(key)
    await redis_client.expire(key, 86400)  # 24 hours

    if count > 3:
        await tg_app.bot.send_message(
            chat_id,
            "🍃 <b>You've already used all your feedback today.</b>\n\nThe forest appreciates your voice — please return tomorrow. 🌿",
            parse_mode="HTML",
        )
        return

    remaining = 3 - count    
    
    manila    = pytz.timezone("Asia/Manila")
    timestamp = datetime.now(manila).strftime("%B %d, %Y • %I:%M %p")

    saved = await _sb_post(
        "feedback",
        {"chat_id": int(chat_id), "first_name": str(first_name), "feedback_text": feedback_text.strip()},
    )

    caption = (
        "🕊️ <b>A Message Carried by the Wind</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"Dear <b>{html.escape(str(first_name))}</b>,\n\n"
        "The gentle breeze has carried your words through the ancient trees...\n"
        "They have reached the caretaker of the Enchanted Clearing.\n\n"
        "Thank you for sharing your thoughts with this small, magical corner of the forest.\n\n"
        "<i>May your voice help the clearing bloom even brighter.</i> 🍃✨\n\n"
        f"🕒 <b>Sent:</b> {timestamp}\n"
        f"📬 <b>Remaining feedback today:</b> {remaining}"
    )
    await send_animated_translated(
        chat_id=chat_id,
        animation_url=HELP_GIF,
        caption=caption,
    )

    status = "✅ Saved to database" if saved else "⚠️ Failed to save"
    try:
        await tg_app.bot.send_message(
            OWNER_ID,
            f"🌿 <b>New Feedback</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 From: <b>{first_name}</b>\n🆔 <code>{chat_id}</code>\n\n"
            f"💬 {feedback_text}\n\n━━━━━━━━━━━━━━━━━━\n"
            f"🕒 {timestamp}\n💾 {status}",
            parse_mode="HTML",
        )
    except Exception as e:
        print(f"Owner notify failed: {e}")

async def handle_test_achievements(chat_id: int):
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can use this.")
        return

    await tg_app.bot.send_message(chat_id, "🔍 Running full achievement diagnostics...\n\n")

    total = len(ACHIEVEMENTS_CACHE)
    count = sum(1 for a in ACHIEVEMENTS_CACHE.values() if a.get("condition", {}).get("type") == "count")
    streak = sum(1 for a in ACHIEVEMENTS_CACHE.values() if a.get("condition", {}).get("type") == "streak")
    manual = sum(1 for a in ACHIEVEMENTS_CACHE.values() if a.get("condition", {}).get("type") == "manual")
    others = total - count - streak - manual

    text = (
        f"📊 <b>Achievement System Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Total Achievements: <b>{total}</b>\n"
        f"• Count-based: <b>{count}</b>\n"
        f"• Streak-based: <b>{streak}</b>\n"
        f"• Manual: <b>{manual}</b>\n"
        f"• Other types: <b>{others}</b>\n\n"
        f"✅ Currently supported: count + streak (daily_bonus)\n\n"
        f"📋 All Achievements & Their Condition:\n"
    )

    for code, ach in ACHIEVEMENTS_CACHE.items():
        cond = ach.get("condition", {})
        cond_type = cond.get("type", "unknown")
        icon = ach.get("icon", "❓")
        rarity = ach.get("rarity", "common")
        name = ach.get("name", "Unnamed")

        text += f"{icon} <code>{code}</code> → {name} [{cond_type}]\n"

    text += "\n<i>Copy this output and send it to me if you want me to expand specific ones.</i>"

    await tg_app.bot.send_message(chat_id, text, parse_mode="HTML")

async def handle_view_feedback(chat_id: int):
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the caretaker can view the feedback scrolls.")
        return

    data = await _sb_get("feedback", select="*", order="created_at.desc", limit=15)
    if data is None:
        await tg_app.bot.send_message(chat_id, "🌿 The feedback scroll is empty.")
        return
    data = data or []
    if not data:
        await tg_app.bot.send_message(chat_id, "🌿 The feedback scroll is empty.")
        return

    manila = pytz.timezone("Asia/Manila")
    msg    = "🌿 <b>Recent Feedback from the Forest</b>\n━━━━━━━━━━━━━━━━━━\n\n"

    for idx, item in enumerate(data, 1):
        try:
            dt       = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            time_str = dt.astimezone(manila).strftime("%b %d, %Y • %I:%M %p")
        except Exception:
            time_str = str(item.get("created_at", ""))[:16]

        msg += (
            f"✨ <b>{idx}.</b> From <b>{html.escape(str(item.get('first_name', 'Unknown')))}</b>\n"
            f"🆔 <code>{item.get('chat_id')}</code>\n"
            f"🕒 {time_str}\n\n"
            f"💬 {html.escape(str(item.get('feedback_text', '')).strip())}\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
        )

    await tg_app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
# PATCH NOTES
# ══════════════════════════════════════════════════════════════════════════════
async def handle_updates(chat_id: int):
    data = await _sb_get("patch_notes", order="created_at.desc", limit=5)
    if data is None:
        await tg_app.bot.send_message(chat_id, "🌱 No patch notes yet.")
        return
    data = data or []
    if not data:
        await tg_app.bot.send_message(chat_id, "🌱 No patch notes yet.")
        return

    text = "🌿 <b>Patch Notes — Recent Updates</b>\n"
    text += "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"

    for i, u in enumerate(data, 1):
        # ── Entry header ──
        if len(data) > 1:
            text += f"✨ <b>{i}. {u.get('date', '')}</b>\n"
        else:
            text += f"✨ <b>{u.get('date', '')}</b>\n"

        text += f"<b>📌 {u.get('title', '')}</b>\n"
        text += "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"

        # ── Content: render each line individually ──
        content_lines = u.get('content', '').strip().split('\n')
        for line in content_lines:
            line = line.strip()
            if not line:
                text += "\n"
            elif line.startswith("━"):
                # Dividers inside content → keep as-is
                text += f"{line}\n"
            elif line.startswith("🔹") or line.startswith("•") or line.startswith("-"):
                # Bullet lines → indent slightly
                text += f"  {line}\n"
            elif line.startswith("⚠️") or line.startswith("Note:"):
                # Warnings → italic
                text += f"<i>  {line}</i>\n"
            elif any(line.startswith(e) for e in ["🌍", "🎮", "🌿", "✨", "🎉", "🔧", "🌟"]):
                # Section headers inside content → bold
                text += f"\n<b>{line}</b>\n"
            else:
                text += f"{line}\n"

        text += "\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"

    text += "<i>🍃 May these updates bring more magic to your journey.</i> ✨"

    await tg_app.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True
    )

# ══════════════════════════════════════════════════════════════════════════════
# EVENTS
# ══════════════════════════════════════════════════════════════════════════════
def get_freshness_badge(last_updated: str | None) -> str:
    """
    Returns a freshness badge based on how recently the cookie was added/updated.
    🟢 Fresh (under 6 hours)
    🟡 Recent (6–24 hours)
    🟠 Aging (1–3 days)
    🔴 Old (3+ days)
    """
    if not last_updated:
        return "⚪ Unknown age"

    try:
        dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
        manila = pytz.timezone("Asia/Manila")
        now = datetime.now(manila)
        diff = now - dt.astimezone(manila)
        total_hours = diff.total_seconds() / 3600

        if total_hours < 6:
            mins = int(diff.total_seconds() / 60)
            if mins < 60:
                return f"🟢 Fresh ({mins}m ago)"
            return f"🟢 Fresh ({int(total_hours)}h ago)"
        elif total_hours < 24:
            return f"🟡 Recent ({int(total_hours)}h ago)"
        elif total_hours < 72:
            days = int(total_hours / 24)
            return f"🟠 Aging ({days}d ago)"
        else:
            days = int(total_hours / 24)
            return f"🔴 Old ({days}d ago)"

    except Exception:
        return "⚪ Unknown age"

async def get_wheel_history(chat_id: int) -> list:
    """Fetch last 5 wheel spins for a user"""
    data = await _sb_get(
        "wheel_spins",
        **{
            "chat_id": f"eq.{chat_id}",
            "select": "rarity,xp_earned,got_bonus_slot,got_fresh_cookie,created_at",
            "order": "created_at.desc",
            "limit": 5,
        }
    ) or []
    return data

async def get_active_event() -> dict | None:
    cached = await redis_client.get("active_event")
    if cached:
        result = json.loads(cached)
    else:
        data = await _sb_get("events", **{"is_active": "eq.true", "order": "created_at.desc", "limit": 1})
        result = data[0] if data else None
        if result:
            await redis_client.setex("active_event", 300, json.dumps(result))

    if result:
        # ── Auto-expire check ──
        try:
            manila = pytz.timezone("Asia/Manila")
            event_dt = datetime.strptime(result.get("event_date", "").strip(), "%B %d, %Y")
            event_dt = manila.localize(event_dt)
            expires_at = event_dt + timedelta(days=1)
            if datetime.now(manila) >= expires_at:
                await _sb_patch("events?is_active=eq.true", {"is_active": False})
                await redis_client.delete("active_event")
                return None
        except Exception:
            pass  # unparseable date — just show event normally

    return result

async def handle_remove_achievement(chat_id: int, target_id: int, achievement_code: str):
    """Remove an achievement from a user (Owner only)"""
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can remove achievements.")
        return

    # Delete the achievement record
    deleted = await _sb_delete(
        f"user_achievements?chat_id=eq.{target_id}&achievement_code=eq.{achievement_code}"
    )

    if deleted:
        ach = ACHIEVEMENTS_CACHE.get(achievement_code)
        ach_name = ach.get("name", achievement_code) if ach else achievement_code

        await tg_app.bot.send_message(
            chat_id,
            f"✅ Successfully <b>removed</b> achievement:\n"
            f"<b>{ach_name}</b> from user `{target_id}`"
        )

        # Optional: Notify the user
        try:
            await tg_app.bot.send_message(
                target_id,
                f"🌿 <b>An achievement was removed by the Caretaker.</b>\n\n"
                f"🏆 <b>{ach_name}</b> has been taken back.\n\n"
                f"<i>The forest sometimes adjusts its gifts...</i> 🍃",
                parse_mode="HTML"
            )
        except:
            pass  # user might have blocked the bot
    else:
        await tg_app.bot.send_message(
            chat_id,
            f"⚠️ No achievement found with code `{achievement_code}` for user `{target_id}`."
        )

async def handle_add_event(chat_id: int, title: str, description: str, event_date: str, bonus_type: str = ""):
    if chat_id != OWNER_ID:
        return

    # Deactivate previous — use checked version so we know if anything was active
    await _sb_patch_check("events?is_active=eq.true", {"is_active": False})

    ok = await _sb_post("events", {
        "title":       title.strip(),
        "description": description.strip(),
        "event_date":  event_date.strip(),
        "is_active":   True,
        "bonus_type":  bonus_type.strip(),
    })

    if ok:
        await tg_app.bot.send_message(
            chat_id,
            f"✅ <b>Event created!</b>\n\n"
            f"🎉 <b>{title}</b>\n"
            f"📅 {event_date}\n\n"
            f"{description}\n\n"
            f"<i>Event is now live and visible to all wanderers.</i> 🌿",
            parse_mode="HTML",
        )
        await redis_client.delete("active_event")
    else:
        await tg_app.bot.send_message(chat_id, "❌ Failed to save event.")
    
async def handle_end_event(chat_id: int):
    if chat_id != OWNER_ID:
        return

    event = await get_active_event()
    if not event:
        await tg_app.bot.send_message(
            chat_id,
            "🌿 <b>No active event to end.</b>\n\nThe forest is already peaceful. 🍃",
            parse_mode="HTML",
        )
        return

    ok, rows_updated = await _sb_patch_check("events?is_active=eq.true", {"is_active": False})
    if ok and rows_updated:
        await tg_app.bot.send_message(
            chat_id,
            "✅ <b>Event ended successfully!</b>\n\n"
            "🌿 The forest has returned to its peaceful state.\n"
            "<i>No active events running.</i> 🍃",
            parse_mode="HTML",
        )
        await redis_client.delete("active_event")
    elif ok and not rows_updated:
        await tg_app.bot.send_message(
            chat_id,
            "⚠️ <b>No active event found to end.</b>\n\n"
            "It may have already expired or been ended. 🍃",
            parse_mode="HTML",
        )
    else:
        await tg_app.bot.send_message(chat_id, "❌ Failed to end event.")

async def handle_view_event(chat_id: int):
    event = await get_active_event()
    if not event:
        await tg_app.bot.send_message(
            chat_id,
            "🌿 <b>No active event right now.</b>\n\n"
            "<i>Use the caretaker menu to create one.</i>",
            parse_mode="HTML",
        )
        return
    
    countdown = await get_event_countdown(event)

    text = (
            f"🎉 <b>Current Active Event</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 <b>{event.get('title', '')}</b>\n"
            f"📅 {event.get('event_date', '')}{countdown}\n\n"
            f"{event.get('description', '')}\n\n"
            f"🎁 <b>Bonus:</b> {event.get('bonus_type') or 'None'}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>Event is currently live and visible to all wanderers.</i> 🌿"
    )

    await tg_app.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML"
    )

async def add_new_update(title: str, content: str, owner_chat_id: int):
    date = datetime.now(pytz.timezone("Asia/Manila")).strftime("%B %d, %Y")
    ok   = await _sb_post("patch_notes", {"date": date, "title": title, "content": content})
    msg  = f"✅ Patch note added!\n\n📅 {date}\n📌 {title}" if ok else "❌ Failed to save to database."
    await tg_app.bot.send_message(owner_chat_id, msg)


# ══════════════════════════════════════════════════════════════════════════════
# MISC COMMANDS
# ══════════════════════════════════════════════════════════════════════════════
async def send_myid(chat_id: int):
    caption = (
        "🌿 <b>Your Eternal Forest ID</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        f"🪪 <code>{chat_id}</code>\n\n"
        "Keep this ID safe — the caretaker may ask for it if you ever need help.\n\n"
        "<i>May your roots stay strong in the Enchanted Clearing.</i> 🍃"
    )
    msg = await send_animated_translated(
        chat_id=chat_id,
        animation_url=MYID_GIF,
        caption=caption,
    )
    await _remember(chat_id, msg.message_id)


async def handle_info(chat_id: int):
    config = await get_bot_config()
    if not config:
        await send_supabase_error(chat_id)
        return

    version = config.get("current_version", "?")
    updated = config.get("last_updated", "?")

    # Total users
    total_data = await _sb_get("user_profiles", select="chat_id")
    total_users = len(total_data) if total_data else 0

    # ── Active Now (last 15 minutes) ──
    manila = pytz.timezone("Asia/Manila")
    ago_15min = (datetime.now(manila) - timedelta(minutes=15)).astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    active_data = await _sb_get(
        "user_profiles",
        **{"select": "chat_id", "last_active": f"gte.{ago_15min}"}
    )
    active_count = len(active_data) if active_data else 0

    text = (
        "🌿 <b>Enchanted Clearing Status</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        "🌳 The forest is thriving peacefully.\n\n"
        f"🕒 <b>Uptime:</b> {await get_bot_uptime()}\n"
        f"🌱 <b>Total Wanderers:</b> {total_users:,}\n"
        f"✨ <b>Active Now:</b> {active_count:,}\n\n"
        f"📜 <b>Current Version:</b> {version}\n"
        f"🔄 <b>Last Updated:</b> {updated}\n\n"
        "⚠️ <i>For personal and educational use only.</i>\n\n"
        "Made with care by the Forest Caretaker 🍃"
    )

    await tg_app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


async def handle_caretaker(chat_id: int, first_name: str):
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker may enter this sacred glade.")
        return
    text = (
        "🌲 <b>Welcome back, Forest Caretaker</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        f"👋 Hello, {html.escape(first_name)}!\n\n"
        "All owner commands are now available as buttons below.\n"
        "Choose your action:"
    )
    await send_animated_translated(
        chat_id=chat_id,
        animation_url=CARETAKER_GIF,
        caption=text,
        reply_markup=await kb_caretaker_dynamic(),
    )
# ──────────────────────────────────────────────
# INVITE HANDLER (used by both /invite and menu button)
# ──────────────────────────────────────────────
async def handle_invite(chat_id: int, first_name: str):
    """🌲 Full Referral System — now available to everyone"""
    profile = await get_user_profile(chat_id)
    if not profile:
        await tg_app.bot.send_message(chat_id, "🌿 Start your journey first with /start!")
        return

    link = await get_referral_link(chat_id)
    count = profile.get("referral_count", 0)

    caption = (
        f"🌲 <b>Your Personal Invite Link</b>\n\n"
        f"✨ Invite a friend → both get rewards!\n\n"
        f"🌿 You earn <b>+{REFERRAL_XP} XP</b> per successful referral\n"
        f"🌱 Your friend gets <b>+{NEW_USER_WELCOME_BONUS_IF_REFERRED} XP</b> welcome bonus\n\n"
        f"🔗 <code>{link}</code>\n\n"
        f"📊 You have invited <b>{count}</b> wanderers so far\n\n"
        f"Share the magic of the Enchanted Clearing, {html.escape(first_name)}! 🍃"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔗 Share Invite Link 🌲",
                url=f"https://t.me/share/url?url={link}&text=🌲%20Join%20me%20in%20Clyde%27s%20Enchanted%20Clearing!%0A%0AGet%20premium%20resources%20%2B%20XP%20bonuses%20%F0%9F%8C%BF"
            ),
            InlineKeyboardButton("📋 Copy Link", callback_data=f"copy_ref_link|{chat_id}"),
        ],
        [InlineKeyboardButton("📜 My Referral History", callback_data="show_referral_history")],
    ])

    msg = await send_animated_translated(
        chat_id=chat_id,
        caption=caption,
        animation_url=INVITE_GIF,
        reply_markup=keyboard,
    )
    await _remember(chat_id, msg.message_id)
    return

async def handle_toggle_maintenance(chat_id: int):
    if chat_id != OWNER_ID:
        return

    current = await get_maintenance_mode()
    new_state = not current
    await set_maintenance_mode(new_state)

    status = "🔴 ON" if new_state else "🟢 OFF"
    label  = "active" if new_state else "lifted"

    await tg_app.bot.send_message(
        chat_id,
        f"🌿 <b>Maintenance Mode {status}</b>\n\n"
        f"The Enchanted Clearing is now <b>{label}</b>.\n\n"
        f"{'Wanderers will see the maintenance message.' if new_state else 'Wanderers can access the clearing again.'} 🍃",
        parse_mode="HTML",
    )

async def handle_clear(chat_id: int, user_msg_id: int, first_name: str):
    try:
        await tg_app.bot.delete_message(chat_id, user_msg_id)
    except Exception:
        pass

    # ── 1. Show animation IMMEDIATELY ──
    loading = await send_animated_translated(
        chat_id=chat_id,
        animation_url=CLEAN_GIF,
        caption="🌫️ <b>The ancient mist begins to thicken...</b>",
    )

    # ── 2. Do ALL cleanup in background while animation plays ──
    async def _do_cleanup():
        # Collect Redis mem tracked messages
        try:
            message_ids = await redis_client.lrange(f"mem:{chat_id}", 0, -1)
            await redis_client.delete(f"mem:{chat_id}")
        except Exception as e:
            print(f"⚠️ Redis clear failed: {e}")
            message_ids = forest_memory.get(chat_id, [])
            forest_memory[chat_id] = []

        # Collect reveal doc messages
        for svc in ("netflix", "prime"):
            reveal_msg_id = await redis_client.get(f"reveal_msg:{chat_id}:{svc}")
            if reveal_msg_id:
                message_ids.append(reveal_msg_id)
                await redis_client.delete(f"reveal_msg:{chat_id}:{svc}")

        # Collect stored loading/temp message IDs
        for key_suffix in (
            "loading_msg",
            "winoffice_loading",
            "steam_loading",
            "inventory_loading",
        ):
            stored_id = await redis_client.get(f"{key_suffix}:{chat_id}")
            if stored_id:
                message_ids.append(stored_id)
                await redis_client.delete(f"{key_suffix}:{chat_id}")

        # Delete all collected messages
        for mid in message_ids:
            try:
                await tg_app.bot.delete_message(chat_id, int(mid))
            except Exception:
                pass

        # Cancel all pending waiting/ghost states
        ghost_keys = [
            f"waiting_for_logo:{chat_id}",
            f"winoffice_pending_cat:{chat_id}",
            f"onboarding_step:{chat_id}",
            f"reveal_spam:{chat_id}:netflix",
            f"reveal_spam:{chat_id}:prime",
            f"steam_claim_spam:{chat_id}",
            f"pending_broadcast:{chat_id}",
        ]
        for key in ghost_keys:
            await redis_client.delete(key)

        # Clear wildcard keys
        try:
            for pattern in (
                f"steam_reminded:{chat_id}:*",
                f"steam_fb:{chat_id}:*",
                f"steam_claim_data:{chat_id}:*",
                f"reveal_key:{chat_id}:*",
                f"winkey:{chat_id}:*",
            ):
                keys = await redis_client.keys(pattern)
                if keys:
                    await redis_client.delete(*keys)
        except Exception as e:
            print(f"⚠️ Wildcard key cleanup failed: {e}")

    # Run cleanup in background — don't await it
    asyncio.create_task(_do_cleanup())

    # ── 3. Animation sequence plays smoothly ──
    await asyncio.sleep(1.8)
    await safe_edit(loading, "🍃 <b>The wind spirit awakens...</b>")
    await asyncio.sleep(2.0)
    await safe_edit(loading, "✨ <b>The forest is resetting...</b>")
    await asyncio.sleep(1.2)

    try:
        await tg_app.bot.delete_message(chat_id, loading.message_id)
    except Exception:
        pass

    # ── 4. Award XP ──
    action_xp, _ = await add_xp(chat_id, first_name, "clear")
    if action_xp:
        await send_xp_feedback(chat_id, action_xp, duration=1)

    # ── 5. Show fresh menu ──
    await send_full_menu(chat_id, first_name, is_first_time=False)

async def handle_status(chat_id: int):
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can check system status.")
        return

    await tg_app.bot.send_message(chat_id, "🔍 <i>Running full diagnostics...</i>", parse_mode="HTML")

    manila = pytz.timezone("Asia/Manila")
    now = datetime.now(manila)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    ago_15min   = (now - timedelta(minutes=15)).astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    ago_1hr     = (now - timedelta(hours=1)).astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    ago_24hr    = (now - timedelta(hours=24)).astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    week_start  = (now - timedelta(days=7)).astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    # ── Run all checks in parallel ──
    async def check_redis():
        try:
            info     = await asyncio.wait_for(redis_client.info("memory"), timeout=3.0)
            used_mb  = round(info["used_memory"] / 1024 / 1024, 2)
            peak_mb  = round(info.get("used_memory_peak", 0) / 1024 / 1024, 2)
            key_count = await asyncio.wait_for(redis_client.dbsize(), timeout=3.0)
            flag = "⚠️" if used_mb > 50 else "✅"
            return flag, used_mb, peak_mb, key_count
        except Exception as e:
            return "❌", 0, 0, 0

    async def check_supabase():
        try:
            result = await _sb_get("user_profiles", select="chat_id", limit=1)
            slots_used = 10 - db_sem._value
            flag = "⚠️" if slots_used >= 8 else "✅"
            ok = result is not None
            return flag, slots_used, ok
        except Exception as e:
            return "❌", 0, False

    async def check_telegram():
        try:
            me = await asyncio.wait_for(tg_app.bot.get_me(), timeout=5.0)
            return "✅", me.username
        except Exception as e:
            return "❌", str(e)[:30]

    async def check_env():
        required = ["BOT_TOKEN", "SUPABASE_URL", "SUPABASE_KEY", "REDIS_URL", "OWNER_ID", "WEBHOOK_SECRET"]
        optional = ["NOTION_TOKEN", "NOTION_DATABASE_ID", "STEAM_API_KEY"]
        missing_req = [v for v in required if not os.getenv(v)]
        missing_opt = [v for v in optional if not os.getenv(v)]
        return missing_req, missing_opt

    # ── User stats ──
    async def get_user_stats():
        try:
            total     = await _sb_get("user_profiles", select="chat_id", limit=5000)
            active_15 = await _sb_get("user_profiles", **{"select": "chat_id", "last_active": f"gte.{ago_15min}"})
            active_1h = await _sb_get("user_profiles", **{"select": "chat_id", "last_active": f"gte.{ago_1hr}"})
            active_24 = await _sb_get("user_profiles", **{"select": "chat_id", "last_active": f"gte.{ago_24hr}"})
            new_today = await _sb_get("user_profiles", **{"select": "chat_id", "created_at": f"gte.{today_start}"})
            new_week  = await _sb_get("user_profiles", **{"select": "chat_id", "created_at": f"gte.{week_start}"})
            # Level distribution
            levels    = await _sb_get("user_profiles", select="level", limit=5000)
            lv_dist = {}
            if levels:
                for u in levels:
                    lv = u.get("level", 1)
                    bucket = f"Lv{lv}" if lv <= 9 else "Lv10+"
                    lv_dist[bucket] = lv_dist.get(bucket, 0) + 1
            return {
                "total":     len(total or []),
                "active_15": len(active_15 or []),
                "active_1h": len(active_1h or []),
                "active_24": len(active_24 or []),
                "new_today": len(new_today or []),
                "new_week":  len(new_week or []),
                "lv_dist":   lv_dist,
            }
        except Exception as e:
            print(f"User stats error: {e}")
            return {}

    # ── Inventory stats ──
    async def get_inventory_stats():
        try:
            SERVICE_EMOJIS = {"netflix": "🍿", "prime": "🎥", "windows": "🪟", "office": "📑"}
            vamt = await get_vamt_data() or []
            counts = {}
            for item in vamt:
                svc = str(item.get("service_type", "")).lower().strip()

                if any(x in svc for x in ("win", "windows")):
                    svc = "windows"
                elif "office" in svc:
                    svc = "office"
                elif "netflix" in svc:
                    svc = "netflix"
                elif "prime" in svc:
                    svc = "prime"

                if str(item.get("status", "")).lower() == "active" and int(item.get("remaining", 0)) > 0:
                    counts[svc] = counts.get(svc, 0) + 1

            steam_avail  = await _sb_get("steamCredentials", **{"select": "status", "status": "eq.Available"}) or []
            steam_all    = await _sb_get("steamCredentials", **{"select": "status"}) or []
            return counts, len(steam_avail), len(steam_all), SERVICE_EMOJIS
        except Exception as e:
            print(f"Inventory stats error: {e}")
            return {}, 0, 0, {}

    # ── Activity stats ──
    async def get_activity_stats():
        try:
            xp_today   = await _sb_get("xp_history", **{"select": "xp_earned", "created_at": f"gte.{today_start}"}) or []
            xp_1hr     = await _sb_get("xp_history", **{"select": "xp_earned", "created_at": f"gte.{ago_1hr}"}) or []
            reveals_nf = await _sb_get("xp_history", **{"select": "chat_id", "action": "eq.reveal_netflix", "created_at": f"gte.{today_start}"}) or []
            reveals_pr = await _sb_get("xp_history", **{"select": "chat_id", "action": "eq.reveal_prime",   "created_at": f"gte.{today_start}"}) or []
            spins      = await _sb_get("wheel_spins", **{"select": "chat_id", "created_at": f"gte.{today_start}"}) or []
            claims     = await _sb_get("steam_claims", **{"select": "chat_id", "claimed_at": f"gte.{today_start}"}) or []
            feedbacks  = await _sb_get("feedback", **{"select": "chat_id", "created_at": f"gte.{today_start}"}) or []
            total_xp   = sum(r.get("xp_earned", 0) for r in xp_today)
            hr_xp      = sum(r.get("xp_earned", 0) for r in xp_1hr)
            return {
                "xp_today":     total_xp,
                "xp_1hr":       hr_xp,
                "nf_reveals":   len(reveals_nf),
                "pr_reveals":   len(reveals_pr),
                "wheel_spins":  len(spins),
                "steam_claims": len(claims),
                "feedbacks":    len(feedbacks),
            }
        except Exception as e:
            print(f"Activity stats error: {e}")
            return {}

    # ── Redis key breakdown ──
    async def get_redis_snapshot():
        try:
            # Count key patterns
            cd_keys   = len(await redis_client.keys("xpcd:*"))
            daily_bon = len(await redis_client.keys("daily_bonus:*"))
            wheel_cd  = len(await redis_client.keys("wheel_spin:*"))
            reveals   = len(await redis_client.keys("daily_reveals:*"))
            views     = len(await redis_client.keys("daily_views:*"))
            spam_keys = len(await redis_client.keys("rl:*"))
            online_n  = len(await redis_client.keys("online_notif:*"))
            return {
                "cooldowns":   cd_keys,
                "daily_bonus": daily_bon,
                "wheel_cd":    wheel_cd,
                "reveals":     reveals,
                "views":       views,
                "spam":        spam_keys,
                "online":      online_n,
            }
        except Exception as e:
            print(f"Redis snapshot error: {e}")
            return {}

    # ── Active event ──
    async def get_event_info():
        try:
            event = await get_active_event()
            if not event:
                return None
            return event
        except:
            return None

    # ── Run everything in parallel ──
    (
        redis_result,
        supabase_result,
        telegram_result,
        env_result,
        user_stats,
        (inv_counts, steam_avail, steam_total, svc_emojis),
        activity,
        redis_snap,
        active_event,
        maintenance,
    ) = await asyncio.gather(
        check_redis(),
        check_supabase(),
        check_telegram(),
        check_env(),
        get_user_stats(),
        get_inventory_stats(),
        get_activity_stats(),
        get_redis_snapshot(),
        get_event_info(),
        get_maintenance_mode(),
    )

    redis_flag, redis_mb, redis_peak, redis_keys = redis_result
    sb_flag, sb_slots, sb_ok = supabase_result
    tg_flag, tg_name = telegram_result
    missing_req, missing_opt = env_result

    env_flag = "✅" if not missing_req else "❌"
    maintenance_status = "🔴 ON — users are blocked!" if maintenance else "🟢 OFF"

    uptime = datetime.now(pytz.utc) - BOT_START_TIME
    h, r   = divmod(int(uptime.total_seconds()), 3600)
    m, s   = divmod(r, 60)
    uptime_str = f"{h}h {m}m {s}s"

    ach_count = len(ACHIEVEMENTS_CACHE)

    # ── Build level distribution string ──
    lv_dist = user_stats.get("lv_dist", {})
    lv_line = "  " + "  ".join(
        f"{k}:{v}" for k, v in sorted(lv_dist.items(), key=lambda x: x[0])
    ) if lv_dist else "  N/A"

    # ── Inventory lines ──
    inv_lines = ""
    for svc in ("netflix", "prime", "windows", "office"):
        count = inv_counts.get(svc, 0)
        emoji = svc_emojis.get(svc, "📦")
        warn  = " ⚠️" if count <= 5 else ""
        inv_lines += f"  {emoji} {svc.title()}: <b>{count}</b>{warn}\n"
    inv_lines += f"  🎮 Steam: <b>{steam_avail}</b> available / {steam_total} total"

    # ── Optional env ──
    opt_line = ""
    if missing_opt:
        opt_line = f"\n⚠️ Optional missing: {', '.join(missing_opt)}"
    req_line = f"\n❌ Required missing: {', '.join(missing_req)}" if missing_req else ""

    # ── Event line ──
    event_line = "🌿 No active event" 
    if active_event:
        countdown = await get_event_countdown(active_event)
        bonus = f" [{active_event.get('bonus_type', '')}]" if active_event.get('bonus_type') else ""
        event_line = f"🎉 <b>{active_event.get('title', 'Unnamed')}</b>{bonus}{countdown}"

    text = (
        f"📊 <b>System Health — Full Report</b>\n"
        f"🕒 {now.strftime('%B %d, %Y • %I:%M %p')} Manila\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"

        f"⚙️ <b>CORE SYSTEMS</b>\n"
        f"{redis_flag} Redis: <b>{redis_mb} MB</b> used (peak {redis_peak} MB) • {redis_keys} keys\n"
        f"{sb_flag} Supabase: {'Connected' if sb_ok else 'ERROR'} • {sb_slots}/10 slots\n"
        f"{tg_flag} Telegram: @{tg_name}\n"
        f"{env_flag} Env Vars: {'All set' if not missing_req else 'MISSING required'}"
        f"{req_line}{opt_line}\n"
        f"🛠️ Maintenance: {maintenance_status}\n"
        f"⏱️ Uptime: <b>{uptime_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"

        f"👥 <b>USER STATS</b>\n"
        f"  Total Wanderers: <b>{user_stats.get('total', 0):,}</b>\n"
        f"  🟢 Online now (15m): <b>{user_stats.get('active_15', 0)}</b>\n"
        f"  🟡 Last hour: <b>{user_stats.get('active_1h', 0)}</b>\n"
        f"  🔵 Last 24h: <b>{user_stats.get('active_24', 0)}</b>\n"
        f"  🌱 New today: <b>{user_stats.get('new_today', 0)}</b>\n"
        f"  📅 New this week: <b>{user_stats.get('new_week', 0)}</b>\n"
        f"  Level spread:\n{lv_line}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"

        f"📦 <b>INVENTORY</b>\n"
        f"{inv_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"

        f"⚡ <b>TODAY'S ACTIVITY</b>\n"
        f"  ✨ XP awarded today: <b>{activity.get('xp_today', 0):,}</b>\n"
        f"  ⚡ XP last hour: <b>{activity.get('xp_1hr', 0):,}</b>\n"
        f"  🍿 Netflix reveals: <b>{activity.get('nf_reveals', 0)}</b>\n"
        f"  🎥 Prime reveals: <b>{activity.get('pr_reveals', 0)}</b>\n"
        f"  🌟 Wheel spins: <b>{activity.get('wheel_spins', 0)}</b>\n"
        f"  🎮 Steam claims: <b>{activity.get('steam_claims', 0)}</b>\n"
        f"  📬 Feedbacks: <b>{activity.get('feedbacks', 0)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"

        f"🗂 <b>REDIS KEY SNAPSHOT</b>\n"
        f"  XP cooldowns active: <b>{redis_snap.get('cooldowns', 0)}</b>\n"
        f"  Daily bonuses claimed: <b>{redis_snap.get('daily_bonus', 0)}</b>\n"
        f"  Wheel cooldowns: <b>{redis_snap.get('wheel_cd', 0)}</b>\n"
        f"  Reveal caps active: <b>{redis_snap.get('reveals', 0)}</b>\n"
        f"  View caps active: <b>{redis_snap.get('views', 0)}</b>\n"
        f"  Rate limit keys: <b>{redis_snap.get('spam', 0)}</b>\n"
        f"  Online notifs sent: <b>{redis_snap.get('online', 0)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"

        f"🎉 <b>ACTIVE EVENT</b>\n"
        f"  {event_line}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"

        f"🏆 <b>ACHIEVEMENT SYSTEM</b>\n"
        f"  Loaded in cache: <b>{ach_count}</b> achievements\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>All data live • Redis snapshot is current</i> 🍃"
    )

    await tg_app.bot.send_message(chat_id, text, parse_mode="HTML")

async def handle_key_feedback(chat_id: int, first_name: str, key_id: str, service_type: str, is_working: bool, query):
    status = "working" if is_working else "not_working"
    emoji  = "✅" if is_working else "🔴"
    label  = "✅ Working" if is_working else "❌ Not Working"

    # Save to Supabase
    await _sb_post("key_reports", {
        "chat_id":      chat_id,
        "first_name":   first_name,
        "key_id":       key_id,
        "service_type": service_type,
        "status":       status,
    })

    # Notify owner in DM
    await tg_app.bot.send_message(
        OWNER_ID,
        f"📋 <b>Key Feedback Report</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 <b>User:</b> {html.escape(str(first_name))} (<code>{chat_id}</code>)\n"
        f"🗂 <b>Service:</b> {service_type.title()}\n\n"
        f"🔑 <b>Key/Cookie:</b>\n<tg-spoiler>{html.escape(str(key_id))}</tg-spoiler>\n\n"
        f"Status: {emoji} <b>{label}</b>\n"
        f"🕐 <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode="HTML",
    )

    # Acknowledge user
    await query.answer(
        "✅ Thanks! Feedback sent to the Caretaker." if is_working else
        "❌ Reported! The Caretaker will look into it.",
        show_alert=True,
    )

async def handle_steam_feedback(
    chat_id: int,
    first_name: str,
    account_email: str,
    game_name: str,
    is_working: bool,
    query
):
    fb_key = f"steam_fb:{chat_id}:{account_email}"
    await redis_client.setex(fb_key, 90000, "1") 

    status = "working" if is_working else "not_working"
    emoji  = "✅" if is_working else "❌"
    label  = "Working" if is_working else "Not Working"

    # With this (upsert on chat_id + key_id + service_type combo):
    await _sb_upsert("key_reports", {
        "chat_id": chat_id,
        "first_name": first_name,
        "key_id": account_email,
        "service_type": "steam",
        "status": status,
        "updated_at": datetime.now(pytz.utc).isoformat(),
    }, on_conflict="chat_id,key_id,service_type")

    # ── Only notify owner — NO auto status change yet ──
    owner_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔁 Restore to Available",
                callback_data=f"owner_restore|{account_email}|{game_name[:25]}"
            ),
            InlineKeyboardButton(
                "🗑 Mark Unavailable",
                callback_data=f"owner_keep|{account_email}|{game_name[:25]}"
            ),
        ]
    ]) if not is_working else None

    await tg_app.bot.send_message(
        OWNER_ID,
        f"🎮 <b>Steam Account Feedback</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 <b>User:</b> {html.escape(str(first_name))} "
        f"(<code>{chat_id}</code>)\n"
        f"🎮 <b>Game:</b> {html.escape(game_name)}\n"
        f"📧 <b>Email:</b> <code>{html.escape(account_email)}</code>\n\n"
        f"Status: {emoji} <b>{label}</b>\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        + ("⚠️ <i>Account status unchanged — tap below to decide.</i>"
           if not is_working else
           "✅ <i>User confirmed this account is working.</i>"),
        parse_mode="HTML",
        reply_markup=owner_kb
    )

    # ── Show undo button to user (only for not working) ──
    undo_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "↩️ Undo — I made a mistake!",
                callback_data=f"stfb_undo|{account_email}|{game_name[:30]}"
            )
        ]
    ]) if not is_working else None

    await query.answer(
        "✅ Thanks! Feedback sent." if is_working else
        "❌ Reported! You have 30 seconds to undo.",
        show_alert=True,
    )

    # ── Edit claim message to show feedback result ──
    try:
        current_caption = query.message.caption or query.message.text or ""
        new_caption = (
            f"{current_caption}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} <b>You reported this as: {label}</b>\n"
            + ("<i>Made a mistake? Tap Undo within 30 seconds.</i>"
               if not is_working else
               "<i>Thank you for your feedback! 🍃</i>")
        )

        # Always keep the back button + optional undo
        back_button = [[InlineKeyboardButton(
            "◀ Back to My Claims",
            callback_data=f"my_steam_claims"   # goes to page 0, user can navigate
        )]]

        final_markup = InlineKeyboardMarkup(
            ([[InlineKeyboardButton(
                "↩️ Undo — I made a mistake!",
                callback_data=f"stfb_undo|{account_email}|{game_name[:30]}"
            )]] if not is_working else []) + back_button
        )
        
        await query.message.edit_caption(
            caption=new_caption,
            parse_mode="HTML",
            reply_markup=final_markup
        )
    except Exception:
        pass

    # ── Auto-remove undo button after 30 seconds ──
    if not is_working:
        async def remove_undo():
            await asyncio.sleep(30)
            try:
                await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀ Back to My Claims", callback_data="my_steam_claims")
                ]]))
            except Exception:
                pass
        asyncio.create_task(remove_undo())

async def handle_settings_page(chat_id: int, first_name: str, query=None):
    lang = await get_user_language(chat_id)
    flag, lang_name = SUPPORTED_LANGUAGES.get(lang, ("🇬🇧", "English"))

    # Fetch all prefs in parallel
    profile = await get_user_profile(chat_id)
    daily_on = await get_daily_bonus_notif(chat_id)
    gifs_on  = await get_gif_enabled(chat_id)    

    # Read service prefs from profile (default True if missing)
    netflix_on = profile.get("notif_netflix", True) if profile else True
    prime_on   = profile.get("notif_prime",   True) if profile else True
    windows_on = profile.get("notif_windows", True) if profile else True
    steam_on   = profile.get("notif_steam",  False) if profile else False

    def _icon(state: bool, on="🔔", off="🔕") -> str:
        return on if state else off

    caption = (
        f"⚙️ <b>Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🌍 <b>Language:</b> {flag} {lang_name}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔔 <b>Notifications</b>\n\n"
        f"{_icon(daily_on)} <b>Daily Bonus Alert:</b> {'ON' if daily_on else 'OFF'}\n"
        f"<i>Popup when your daily login XP is added.</i>\n\n"
        f"{_icon(netflix_on, '🍿', '🔇')} <b>Netflix Alerts:</b> {'ON' if netflix_on else 'OFF'}\n"
        f"{_icon(prime_on,   '🎥', '🔇')} <b>Prime Alerts:</b> {'ON' if prime_on else 'OFF'}\n"
        f"{_icon(windows_on, '🪟', '🔇')} <b>Windows/Office Alerts:</b> {'ON' if windows_on else 'OFF'}\n"
        f"{_icon(steam_on,   '🎮', '🔇')} <b>Steam Alerts:</b> {'ON' if steam_on else 'OFF'}\n\n"
        # f"{_icon(gifs_on, '🎞️', '🔇')} <b>Animated GIFs:</b> {'ON' if gifs_on else 'OFF'}\n"    
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        "<i>Tap any button to toggle it on or off.</i> 🍃"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🌍 Change Language ({flag} {lang_name})",
            callback_data="set_language"
        )],
        [InlineKeyboardButton(
            f"{_icon(daily_on)} Daily Bonus Alert: {'ON' if daily_on else 'OFF'}",
            callback_data="toggle_notif|daily_bonus"
        )],
        [
            InlineKeyboardButton(
                f"{_icon(netflix_on, '🍿', '🔇')} Netflix: {'ON' if netflix_on else 'OFF'}",
                callback_data="toggle_notif|netflix"
            ),
            InlineKeyboardButton(
                f"{_icon(prime_on, '🎥', '🔇')} Prime: {'ON' if prime_on else 'OFF'}",
                callback_data="toggle_notif|prime"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{_icon(windows_on, '🪟', '🔇')} Windows: {'ON' if windows_on else 'OFF'}",
                callback_data="toggle_notif|windows"
            ),
            InlineKeyboardButton(
                f"{_icon(steam_on, '🎮', '🔇')} Steam: {'ON' if steam_on else 'OFF'}",
                callback_data="toggle_notif|steam"
            ),
        ],
        # [InlineKeyboardButton(
        #     f"{'🎞️ GIFs: ON' if gifs_on else '🔇 GIFs: OFF'}",
        #     callback_data="toggle_notif|gifs"
        # )],
        [InlineKeyboardButton(
            "🌲 Invite Friends & Earn 25 XP",
            callback_data="invite_friends"
        )],
        [InlineKeyboardButton("⬅️ Back to Clearing", callback_data="main_menu")],
    ])

    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    await send_animated_translated(
        chat_id=chat_id,
        animation_url=MYID_GIF,
        caption=caption,
        reply_markup=keyboard,
    )

async def handle_view_reports(chat_id: int):
    reports = await _sb_get(
        "key_reports",
        select="*",
        order="reported_at.desc",
        limit=20,
    )

    if not reports:
        await tg_app.bot.send_message(chat_id, "🌿 No feedback reports yet.")
        return

    lines = []
    for r in reports:
        emoji = "✅" if r.get("status") == "working" else "❌"
        lines.append(
            f"{emoji} <b>{r.get('service_type', '').title()}</b>\n"
            f"└ 👤 {html.escape(str(r.get('first_name', 'Unknown')))}\n"
            f"└ 🔑 <tg-spoiler>{str(r.get('key_id', ''))[:40]}</tg-spoiler>\n"
            f"└ 🕐 {str(r.get('reported_at', ''))[:19]}\n"
        )

    text = "📋 <b>Latest Key Reports</b>\n━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines)
    await tg_app.bot.send_message(chat_id, text, parse_mode="HTML")

async def handle_reset_first_time(chat_id: int):
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the caretaker can reset the forest memory.")
        return
    confirm_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✔️ Yes, Reset Everything", callback_data="confirm_full_reset")],
        [InlineKeyboardButton("❌ No, Cancel",            callback_data="cancel_reset")],
    ])
    await tg_app.bot.send_message(
        chat_id,
        "⚠️ <b>Full Reset Confirmation</b>\n\n"
        "This will permanently reset Level, XP, all stats, and XP history.\n\n"
        "This action cannot be undone.",
        parse_mode="HTML",
        reply_markup=confirm_kb,
    )

# ──────────────────────────────────────────────
# CONFIRMATION: END EVENT
# ──────────────────────────────────────────────
async def handle_confirm_end_event(chat_id: int):
    if chat_id != OWNER_ID:
        return

    # ← NEW: Check if there's actually an active event first
    event = await get_active_event()

    if not event:
        await tg_app.bot.send_message(
            chat_id,
            "🌿 <b>No active event to end.</b>\n\n"
            "The forest is already in its peaceful state. 🍃",
            parse_mode="HTML",
        )
        return

    # Only show confirmation if there IS an active event
    confirm_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✔️ Yes, End Event Now", callback_data="yes_end_event")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_end_event")],
    ])

    await tg_app.bot.send_message(
        chat_id,
        "⚠️ <b>End Current Event?</b>\n\n"
        "This will immediately remove the event banner for <b>all users</b>.\n"
        "The forest will return to its peaceful state.\n\n"
        "Are you sure?",
        parse_mode="HTML",
        reply_markup=confirm_kb,
    )

# ──────────────────────────────────────────────
# CONFIRMATION: MAINTENANCE MODE
# ──────────────────────────────────────────────
async def handle_confirm_toggle_maintenance(chat_id: int):
    if chat_id != OWNER_ID:
        return
    current = await get_maintenance_mode()
    status = "🔴 CURRENTLY ON" if current else "🟢 CURRENTLY OFF"
    action = "ENABLE" if not current else "DISABLE"

    confirm_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✔️ Yes, {action} Maintenance Mode", callback_data="yes_toggle_maintenance")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_toggle_maintenance")],
    ])

    await tg_app.bot.send_message(
        chat_id,
        f"🛠️ <b>Maintenance Mode {status}</b>\n\n"
        f"This will {action.lower()} maintenance mode for <b>ALL users</b>.\n\n"
        "Are you sure you want to continue?",
        parse_mode="HTML",
        reply_markup=confirm_kb,
    )

async def show_wheel_history(chat_id: int, query=None):
    """Show last 5 wheel spins"""
    data = await get_wheel_history(chat_id)
    
    manila = pytz.timezone("Asia/Manila")
    
    RARITY_EMOJIS = {
        "Common":    "🌿",
        "Uncommon":  "🍃",
        "Rare":      "🌟",
        "Epic":      "✨",
        "Legendary": "🌠",
        "Secret":    "🪄",
    }

    if not data:
        text = (
            "🎰 <b>Your Wheel History</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🌫️ You haven't spun the wheel yet!\n\n"
            "<i>Try your luck today, wanderer.</i> 🍃"
        )
    else:
        lines = []
        for i, spin in enumerate(data, 1):
            rarity = spin.get("rarity", "Unknown")
            xp = spin.get("xp_earned", 0)
            emoji = RARITY_EMOJIS.get(rarity, "🌿")
            bonus_slot = "  +1 reveal slot" if spin.get("got_bonus_slot") else ""
            fresh_cookie = "  +fresh cookie!" if spin.get("got_fresh_cookie") else ""

            # Human-readable time ago
            try:
                dt = datetime.fromisoformat(
                    spin["created_at"].replace("Z", "+00:00")
                ).astimezone(manila)
                diff = datetime.now(manila) - dt
                total_mins = int(diff.total_seconds() / 60)

                if total_mins < 2:
                    time_str = "just now"
                elif total_mins < 60:
                    time_str = f"{total_mins} mins ago"
                elif total_mins < 1440:
                    time_str = f"{total_mins // 60}h ago"
                else:
                    time_str = f"{total_mins // 1440}d ago"
            except Exception:
                time_str = "unknown"

            lines.append(
                f"{i}. {emoji} <b>{rarity}</b> — +{xp} XP{bonus_slot}{fresh_cookie}\n"
                f"   🕒 {time_str}"
            )

        text = (
            "🎰 <b>Your Last 5 Spins</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            + "\n\n".join(lines) +
            "\n\n━━━━━━━━━━━━━━━━━━\n"
            "<i>Keep spinning for bigger rewards! 🍃</i>"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌟 Spin Now", callback_data="spin_now")],
        [InlineKeyboardButton("⬅️ Back to Wheel", callback_data="show_wheel_menu")],
    ])

    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    msg = await send_animated_translated(
        chat_id=chat_id,
        caption=text,
        animation_url=WHEEL_WHISPERS_GIF,
        reply_markup=keyboard,
    )
    await _remember(chat_id, msg.message_id)

async def show_winoffice_keys(chat_id: int, category: str, profile: dict, query, first_name: str):
    pending_cat = category
    cat_label = "Windows" if pending_cat in ("win", "windows") else "Office"
    cat_emoji = "🪟" if pending_cat in ("win", "windows") else "📑"
    cat_gif   = WINOS_GIF if pending_cat in ("win", "windows") else OFFICE_GIF

    try:
        if query and query.message:
            try:
                await query.message.delete()
            except Exception:
                pass

        # Send a FRESH animation (never edit — previous msg is deleted)
        loading = await send_animated_translated(
            chat_id=chat_id,
            animation_url=cat_gif,
            caption=f"{cat_emoji} <i>Opening the {cat_label} key scroll...</i>",
        )

        # New safety check
        if not loading or not hasattr(loading, "message_id"):
            await send_supabase_error(chat_id, query)
            return

        await asyncio.sleep(1.5)
        try:
            await safe_edit(loading,
                f"🌿 <i>The ancient {cat_label} scrolls are being unsealed...</i>",
            )
        except Exception:
            pass

        await asyncio.sleep(1.5)

        user_level = profile.get("level", 1)
        internal_cat = normalize_view_category(category)

        # Get remaining views (read only)
        rem = await get_remaining_reveals_and_views(chat_id)
        views_left = rem.get(internal_cat, 0)

        vamt = await get_vamt_data()
        if not vamt:
            await send_supabase_error(chat_id)
            try:
                await safe_edit(loading,
                    "🌫️ <b>The forest is unreachable right now...</b>\n\nPlease try again shortly. 🍃",
                    reply_markup=kb_back_inventory(),
                )
            except Exception:
                pass
            return

        if pending_cat in ("win", "windows"):
            filtered = [
                item for item in vamt
                if any(x in str(item.get("service_type", "")).lower() for x in ("windows", "win"))
                and int(item.get("remaining") or 0) > 0
            ]
        else:
            filtered = [
                item for item in vamt
                if "office" in str(item.get("service_type", "")).lower()
                and int(item.get("remaining") or 0) > 0
            ]

        if not filtered:
            await safe_edit(loading,
                "🌫️ <b>The forest is unreachable right now...</b>\n\nPlease try again shortly. 🍃",
                reply_markup=kb_back_inventory(),
            )
            return

        max_items = get_max_items(pending_cat, user_level)
        filtered.sort(key=lambda x: (str(x.get("service_type", "")), str(x.get("key_id", ""))))
        display_items = filtered[:max_items]

        windows_bonus = (profile.get("windows_views_bonus") or 0) if profile else 0
        all_bonus = (profile.get("all_slots_bonus") or 0) if profile else 0
        max_views = get_max_daily_views(user_level, internal_cat) + windows_bonus + all_bonus
        used_views = max_views - views_left
        view_bar = create_daily_progress_bar(used_views, max_views, length=8)

        report = f"{cat_emoji} <b>{cat_label} Activation Keys</b>  •  Lv{user_level}\n"
        report += f"{view_bar}  <b>{views_left}</b> views left\n\n"

        report += f"📋 <b>{len(display_items)} key(s) available for your level</b>\n\n"

        for item in display_items:
            stock = str(item.get("remaining", 0))
            report += (
                f"✨ <b>{item.get('service_type', 'Unknown')}</b>\n"
                f"└ 🔑 Key: <tg-spoiler>{item.get('key_id', 'HIDDEN')}</tg-spoiler>\n"
                f"└ 📦 Remaining: <b>{stock}</b>\n\n"
            )

        report += (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🌿 Level {user_level} → Up to <b>{max_items}</b> {cat_label} keys\n\n"
            "Tap ✅ if it worked, ❌ if it did not — Caretaker will be notified. 🍃"
        )

        buttons = []
        for item in display_items:
            raw_key = str(item.get("key_id", "")).strip()
            svc = str(item.get("service_type", pending_cat)).strip()
            short = raw_key[:20] + "…" if len(raw_key) > 20 else raw_key
            token = f"{chat_id}:{raw_key[:40]}"
            await redis_client.setex(f"winkey:{token}", 3600, f"{raw_key}||{svc}")
            buttons.append([
                InlineKeyboardButton(f"✅ {short}", callback_data=f"wkfb_ok|{token}"),
                InlineKeyboardButton(f"❌", callback_data=f"wkfb_bad|{token}"),
            ])

        buttons.append([InlineKeyboardButton(
            "❓ What is VAMT / Remaining?", callback_data=f"explain_vamt|{internal_cat}"
        )])
        buttons.append([InlineKeyboardButton(
            "⬅️ Back to Inventory", callback_data="check_vamt"
        )])

        await safe_edit(loading,
            report,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

        # ── Award XP + Check achievements after viewing keys ──
        action_name = "view_windows" if category in ("win", "windows") else "view_office"
        action_xp, _ = await add_xp(chat_id, first_name, action_name)
        if action_xp:
            asyncio.create_task(send_xp_feedback(chat_id, action_xp))

        # Check achievements
        asyncio.create_task(
            check_and_award_achievements(chat_id, first_name, action=action_name)
        )

    except Exception as e:
        print(f"🔴 Error in show_winoffice_keys for {chat_id}: {e}")
        await send_supabase_error(chat_id, query)

# ──────────────────────────────────────────────
# NEW USER STEAM SEARCH SYSTEM (regular users only)
# ──────────────────────────────────────────────
async def handle_steam_landing(chat_id: int, first_name: str, query=None):
    profile = await get_user_profile(chat_id)
    if not profile:
        return

    level = profile.get("level", 1)
    cooldown_hours = get_steam_cooldown_hours(level)
    total_cd_seconds = cooldown_hours * 3600

    claim_ttl = await redis_client.ttl(f"steam_claim_cd:{chat_id}")
    search_ttl = await redis_client.ttl(f"steam_search_cd:{chat_id}")
    active_cd = max(claim_ttl, search_ttl)
    active_cd = max(0, min(active_cd, total_cd_seconds))

    attempts_used = int(await redis_client.get(f"steam_search_attempts:{chat_id}") or 0)
    attempts_left = 3 - attempts_used

    def make_recharge_bar(remaining_seconds: int, total_seconds: int, length: int = 10) -> str:
        if total_seconds <= 0:
            return "🟩" * length
        remaining_seconds = max(0, min(remaining_seconds, total_seconds))
        # elapsed = how much time has PASSED = total - remaining
        elapsed = total_seconds - remaining_seconds
        pct = elapsed / total_seconds  # 0.0 = just started, 1.0 = done
        filled = round(pct * length)
        empty = length - filled
        if pct < 0.33:
            fill_emoji = "🟥"
        elif pct < 0.66:
            fill_emoji = "🟨"
        else:
            fill_emoji = "🟩"
        return fill_emoji * filled + "⬜" * empty



    if active_cd > 0:
        elapsed_seconds = total_cd_seconds - active_cd
        pct_done = round((elapsed_seconds / total_cd_seconds) * 100) if total_cd_seconds > 0 else 0

        hours = active_cd // 3600
        mins = (active_cd % 3600) // 60

        cd_bar = make_recharge_bar(active_cd, total_cd_seconds, length=10)

        status_text = (
            f"⏳ <b>Recharging...</b>\n"
            f"{cd_bar} {pct_done}%\n"
            f"⏰ Ready in: <b>{hours}h {mins}m</b>\n\n"
            f"🌿 Level {level} cooldown: <b>{cooldown_hours}h</b>"
        )
        search_buttons = []

    elif attempts_left <= 0:
        await redis_client.setex(f"steam_search_cd:{chat_id}", cooldown_hours * 3600, "1")
        await redis_client.delete(f"steam_search_attempts:{chat_id}")
        new_ttl = await redis_client.ttl(f"steam_search_cd:{chat_id}")
        hours = new_ttl // 3600
        mins = (new_ttl % 3600) // 60
        total_cd_seconds = cooldown_hours * 3600
        elapsed = total_cd_seconds - new_ttl
        pct_done = round((elapsed / total_cd_seconds) * 100) if total_cd_seconds > 0 else 0

        cd_bar = make_recharge_bar(elapsed, total_cd_seconds, length=10)
        attempts_bar = make_attempts_bar(3, 3)  # all used

        status_text = (
            f"🚫 <b>No Attempts Remaining</b>\n"
            f"Attempts: {attempts_bar} 0/3\n\n"
            f"⏳ <b>Recharging...</b>\n"
            f"{cd_bar} {pct_done}%\n"
            f"⏰ Ready in: <b>{hours}h {mins}m</b>\n\n"
            f"🌿 Level {level} cooldown: <b>{cooldown_hours}h</b>"
        )
        search_buttons = []

    else:
        attempts_bar = make_attempts_bar(attempts_used, 3)

        status_text = (
            f"🎯 <b>Search Attempts</b>\n"
            f"{attempts_bar} {attempts_left}/3\n\n"
            f"🌿 Level {level} cooldown after claim: <b>{cooldown_hours}h</b>"
        )
        search_buttons = [
            [InlineKeyboardButton("🔍 Search for a Game", callback_data="steam_do_search")],
        ]

    owner_buttons = []
    if chat_id == OWNER_ID:
        owner_buttons = [
            [InlineKeyboardButton("🛠️ Admin Steam Search", callback_data="owner_steam_search")],
        ]

    keyboard = InlineKeyboardMarkup(
        search_buttons +
        owner_buttons +
        [
            [InlineKeyboardButton("📜 My Claims", callback_data="my_steam_claims")],
            [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")],
        ]
    )

    caption = (
        "🎮 <b>Steam Accounts</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{status_text}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "• Search for any game you want\n"
        "• Found results expire in <b>10 minutes</b>\n"
        "• 3 failed/expired searches → cooldown starts\n\n"
        "<i>The forest holds many games, wanderer. 🍃</i>"
    )

    if query and query.message:
        try:
            await query.message.edit_caption(
                caption=caption, parse_mode="HTML", reply_markup=keyboard
            )
        except Exception:
            try:
                await query.message.edit_text(
                    text=caption, parse_mode="HTML", reply_markup=keyboard
                )
            except Exception:
                pass
    else:
        await tg_app.bot.send_message(
            chat_id, caption, parse_mode="HTML", reply_markup=keyboard
        )

async def handle_user_steam_search(chat_id: int, first_name: str, query=None):
    profile = await get_user_profile(chat_id)
    if not profile:
        return

    level = profile.get("level", 1)
    cooldown_hours = get_steam_cooldown_hours(level)

    # Check cooldown
    claim_ttl = await redis_client.ttl(f"steam_claim_cd:{chat_id}")
    search_ttl = await redis_client.ttl(f"steam_search_cd:{chat_id}")
    active_cd = max(claim_ttl, search_ttl)

    attempts_left = 3 - int(await redis_client.get(f"steam_search_attempts:{chat_id}") or 0)

    # Build the steam landing page
    if active_cd > 0:
        hours = active_cd // 3600
        mins = (active_cd % 3600) // 60
        status_text = (
            f"⏳ <b>You are on cooldown</b>\n"
            f"Time remaining: <b>{hours}h {mins}m</b>\n"
            f"Level {level} cooldown: {cooldown_hours} hours"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📜 My Claims", callback_data="my_steam_claims")],
            [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")],
        ])
    else:
        status_text = (
            f"🔍 <b>Search attempts remaining: {attempts_left}/3</b>\n"
            f"Level {level} cooldown after claim: <b>{cooldown_hours}h</b>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Search for a Game", callback_data="steam_do_search")],
            [InlineKeyboardButton("📜 My Claims", callback_data="my_steam_claims")],
            [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")],
        ])

    caption = (
        "🎮 <b>Steam Accounts</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{status_text}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "• Search for any game you want\n"
        "• Found results expire in <b>10 minutes</b>\n"
        "• 3 failed/expired searches → cooldown starts\n\n"
        "<i>The forest holds many games, wanderer. 🍃</i>"
    )

    if query and query.message:
        try:
            await query.message.edit_caption(caption=caption, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            try:
                await query.message.edit_text(text=caption, parse_mode="HTML", reply_markup=keyboard)
            except Exception:
                pass
    else:
        await tg_app.bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=keyboard)

async def handle_steam_game_search(chat_id: int, first_name: str, game_query: str):
    profile = await get_user_profile(chat_id)
    if not profile:
        return

    level = profile.get("level", 1)
    cooldown_hours = get_steam_cooldown_hours(level)
    cooldown_seconds = cooldown_hours * 3600

    attempts_key = f"steam_search_attempts:{chat_id}"
    current_attempts = int(await redis_client.get(attempts_key) or 0)

    if current_attempts >= 3:
        await redis_client.delete(f"steam_searching:{chat_id}")
        await redis_client.setex(f"steam_search_cd:{chat_id}", cooldown_hours * 3600, "1")
        await redis_client.delete(attempts_key)
        await tg_app.bot.send_message(
            chat_id,
            f"🚫 <b>No search attempts remaining.</b>\n\n"
            f"Please wait for your cooldown to expire before searching again. 🍃",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")],
            ])
        )
        return

    # ── Fetch ALL available accounts ──
    all_accounts = []
    offset = 0
    while True:
        batch = await _sb_get(
            "steamCredentials",
            **{"select": "*", "status": "eq.Available", "limit": 500, "offset": offset}
        ) or []
        all_accounts.extend(batch)
        if len(batch) < 500:
            break
        offset += 500

    query_lower = game_query.lower().strip()
    query_words = query_lower.split()

    def get_all_game_names(acc: dict) -> list[str]:
        """
        Returns all unique game names for this account.
        game_name = primary (never duplicated into games[])
        games[]   = everything else
        """
        names = []
        primary = (acc.get("game_name") or "").strip()
        if primary:
            names.append(primary)
        for g in (acc.get("games") or []):
            g = (g or "").strip()
            # Skip if empty or already added (handles accidental duplicates in DB)
            if g and g.lower() not in [n.lower() for n in names]:
                names.append(g)
        return names

    def match_score(acc: dict) -> tuple[int, str]:
        """
        Returns (best_score, matched_game_name).
        Searches game_name first (higher priority), then games[].
        matched_game_name = the specific game title that matched the query.
        """
        best_score = 0
        best_name = (acc.get("game_name") or "Steam Account").strip()

        all_names = get_all_game_names(acc)

        for i, name in enumerate(all_names):
            n = name.lower()

            if query_lower == n:
                score = 100
            elif n.startswith(query_lower):
                score = 90
            elif query_lower in n:
                score = 80
            elif all(w in n for w in query_words):
                score = 70
            else:
                matched_words = sum(1 for w in query_words if w in n)
                score = matched_words * 10

            # Slightly boost game_name matches over games[] matches
            if i == 0 and score > 0:
                score += 5

            if score > best_score:
                best_score = score
                best_name = name

        return best_score, best_name

    # ── Score and filter ──
    scored = []
    for acc in all_accounts:
        score, matched_name = match_score(acc)
        if score > 0:
            scored.append((acc, score, matched_name))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Add this right before building the result message (after unclaimed check)
    await redis_client.setex(f"steam_last_search:{chat_id}", 600, game_query)

    # ── Filter out already-claimed accounts by this user ──
    unclaimed = []
    for acc, score, matched_name in scored:
        account_email = acc.get("email", "")
        existing = await _sb_get(
            "steam_claims",
            **{
                "chat_id": f"eq.{chat_id}",
                "account_email": f"eq.{account_email}",
                "select": "id",
            }
        ) or []
        if not existing:
            unclaimed.append((acc, matched_name))

    await redis_client.delete(f"steam_searching:{chat_id}")

    # ── No results at all ──
    if not unclaimed and not scored:
        new_count = current_attempts + 1
        await redis_client.setex(attempts_key, cooldown_seconds, new_count)
        attempts_left = 3 - new_count

        if attempts_left <= 0:
            await redis_client.setex(f"steam_search_cd:{chat_id}", cooldown_seconds, "1")
            await redis_client.delete(attempts_key)
            await tg_app.bot.send_message(
                chat_id,
                f"❌ <b>Game not found: \"{html.escape(game_query)}\"</b>\n\n"
                f"🚫 <b>No search attempts remaining.</b>\n\n"
                f"Please wait for your cooldown to expire. 🍃",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")],
                ])
            )
        else:
            await tg_app.bot.send_message(
                chat_id,
                f"❌ <b>Game not found: \"{html.escape(game_query)}\"</b>\n\n"
                f"🔍 <b>{attempts_left} attempt(s) remaining.</b>\n\n"
                f"Try a different name or spelling. 🍃",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 Search Again", callback_data="steam_do_search")],
                    [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")],
                ])
            )
        return

    # ── All matched accounts already claimed ──
    if not unclaimed and scored:
        await tg_app.bot.send_message(
            chat_id,
            f"🌿 <b>You've already claimed all matching accounts for "
            f"\"{html.escape(game_query)}\"!</b>\n\n"
            f"Try searching for a different game. 🍃",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search Different Game", callback_data="steam_do_search")],
                [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")],
            ])
        )
        return

    # ── GROUP by matched game name ──
    from collections import defaultdict
    grouped: dict[str, list] = defaultdict(list)
    for acc, matched_name in unclaimed:
        grouped[matched_name].append(acc)

    # Store first result for claim flow + expiry tracking
    first_acc = unclaimed[0][0]
    first_email = first_acc.get("email", "")
    first_game = unclaimed[0][1]

    await redis_client.setex(
        f"steam_search_result:{chat_id}",
        600,
        json.dumps({"email": first_email, "game_name": first_game})
    )

    # ── Schedule result expiry ──
    async def _expire_result():
        await asyncio.sleep(610)
        still_there = await redis_client.get(f"steam_search_result:{chat_id}")
        if not still_there:
            return
        await redis_client.delete(f"steam_search_result:{chat_id}")
        new_count = int(await redis_client.get(attempts_key) or 0) + 1
        await redis_client.setex(attempts_key, cooldown_seconds, new_count)

        if new_count >= 3:
            await redis_client.setex(f"steam_search_cd:{chat_id}", cooldown_seconds, "1")
            await redis_client.delete(attempts_key)
            try:
                await tg_app.bot.send_message(
                    chat_id,
                    f"⏳ <b>Result expired and attempts used up.</b>\n\n"
                    f"Cooldown started: <b>{cooldown_hours} hours</b> 🍃",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            remaining = 3 - new_count
            try:
                await tg_app.bot.send_message(
                    chat_id,
                    f"⏳ <b>Claim window expired!</b>\n\n"
                    f"🔍 <b>{remaining} attempt(s) remaining.</b>\n\n"
                    f"<i>Tap below to search again. 🍃</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔍 Search Again", callback_data="steam_do_search")],
                        [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")],
                    ])
                )
            except Exception:
                pass

    asyncio.create_task(_expire_result())

    # ── Build result message ──
    total_accounts = len(unclaimed)
    total_games = len(grouped)

    text = (
        f"✅ <b>Found {total_accounts} account(s) across "
        f"{total_games} game title(s) for \"{html.escape(game_query)}\"!</b>\n\n"
        f"⏳ Results expire in <b>10 minutes</b>. Pick one:\n\n"
    )

    buttons = []
    for game_name, accounts in grouped.items():
        count = len(accounts)
        claim_email = accounts[0].get("email", "")

        # Store emails in Redis for show_steam_account_selection
        emails = [acc.get("email") for acc in accounts if acc.get("email")]
        safe_key = abs(hash(f"{chat_id}:{game_name}")) % 999999
        await redis_client.setex(
            f"steam_group:{chat_id}:{safe_key}",
            600,
            json.dumps({"emails": emails, "game_name": game_name})
        )

        other_games = get_all_game_names(accounts[0])
        other_games = [g for g in other_games if g.lower() != game_name.lower()]
        other_hint = ""
        if other_games:
            preview = ", ".join(other_games[:2])
            more = f" +{len(other_games) - 2} more" if len(other_games) > 2 else ""
            other_hint = f"\n   <i>Also includes: {html.escape(preview)}{more}</i>"

        if count > 1:
            text += f"🎮 <b>{html.escape(game_name)}</b> — {count} accounts available{other_hint}\n\n"
            label = f"✅ {game_name[:26]} ({count} avail.)"
        else:
            text += f"🎮 <b>{html.escape(game_name)}</b>{other_hint}\n\n"
            label = f"✅ {game_name[:35]}"

        if count > 1:
            buttons.append([InlineKeyboardButton(
                label,
                callback_data=f"steam_sel|{chat_id}|{safe_key}"
            )])
        else:
            buttons.append([InlineKeyboardButton(
                label,
                callback_data=f"claim_steam|{claim_email}"
            )])

    buttons.append([InlineKeyboardButton("🔍 Search Different Game", callback_data="search_different_game")])
    buttons.append([InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")])

    await tg_app.bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════════════════════════════════════════════════════
async def handle_searchsteam_command(chat_id: int, raw_text: str, page: int = 0, query=None):
    """Single search with pagination | Bulk search = Supabase only"""
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can use this.")
        return

    body = raw_text.replace("/searchsteam", "").strip()
    if not body:
        await send_animated_translated(
            chat_id,
            "🔍 <b>Steam Account Search</b>\n\n"
            "• Single: username or email\n"
            "• Bulk: multiple lines (Supabase only)\n\n"
            "Example:\n<code>/searchsteam clydeforest</code>",
            animation_url=STEAM_GIF,
        )
        return

    lines = [line.strip() for line in body.split("\n") if line.strip()]

    # ====================== BULK SEARCH ======================
    if len(lines) > 1:
        ITEMS_PER_PAGE = 8

        loading_msg = await tg_app.bot.send_message(
            chat_id,
            f"📋 <b>Processing {len(lines)} accounts...</b>\n\n<i>Searching Supabase...</i>",
            parse_mode="HTML"
        )

        all_accounts = await _sb_get(
            "steamCredentials",
            **{"select": "email,password,game_name,status", "limit": 2000}
        ) or []

        account_map = {str(acc.get("email", "")).lower().strip(): acc for acc in all_accounts if acc.get("email")}

        found_results = []
        not_found_results = []

        for term in lines:
            acc = account_map.get(term.lower().strip())
            if acc:
                password = html.escape(acc.get("password", "HIDDEN"))
                found_results.append(
                    f"✅ <code>{html.escape(term)}</code>\n"
                    f"🎮 {acc.get('game_name', '—')} | {acc.get('status', 'Available')}\n"
                    f"🔑 <tg-spoiler>{password}</tg-spoiler>"
                )
            else:
                not_found_results.append(f"❌ <code>{html.escape(term)}</code> — Not found")

        try:
            await loading_msg.delete()
        except Exception:
            pass

        total     = len(lines)
        succeeded = len(found_results)
        failed    = len(not_found_results)

        # Combine all result lines
        all_result_lines = []
        if found_results:
            all_result_lines.append("🟢 <b>Found Accounts</b>")
            all_result_lines.extend(found_results)
        if not_found_results:
            if found_results:
                all_result_lines.append("")
            all_result_lines.append("🔴 <b>Not Found</b>")
            all_result_lines.extend(not_found_results)

        # Paginate
        total_pages = max(1, (len(all_result_lines) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        start = page * ITEMS_PER_PAGE
        page_lines = all_result_lines[start:start + ITEMS_PER_PAGE]

        summary_header = (
            f"📊 <b>Bulk Search Results</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✅ Found: <b>{succeeded}</b>  |  ❌ Not Found: <b>{failed}</b>  |  📋 Total: <b>{total}</b>\n"
            f"📄 Page {page + 1} of {total_pages}\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
        )

        final_text = summary_header + "\n\n".join(page_lines)

        # Navigation buttons
        # Encode the search terms as a Redis key to avoid huge callback_data
        bulk_key = f"bulk_search:{chat_id}"
        await redis_client.setex(bulk_key, 600, json.dumps(lines))  # 10 min TTL

        buttons = []
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("↼ Previous", callback_data=f"bulk_page|{page-1}"))
        if start + ITEMS_PER_PAGE < len(all_result_lines):
            nav.append(InlineKeyboardButton("Next ⇀", callback_data=f"bulk_page|{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")])

        markup = InlineKeyboardMarkup(buttons) if buttons else None

        if query and query.message:
            try:
                await query.message.edit_caption(
                    caption=final_text,
                    parse_mode="HTML",
                    reply_markup=markup
                )
                return
            except Exception:
                try:
                    await query.message.edit_text(
                        text=final_text,
                        parse_mode="HTML",
                        reply_markup=markup
                    )
                    return
                except Exception:
                    pass

        await send_animated_translated(chat_id, final_text, animation_url=STEAM_RESULT_GIF, reply_markup=markup)
        return

    # ====================== SINGLE SEARCH ======================
    term = lines[0]

    STEAM_API_KEY = os.getenv("STEAM_API_KEY")
    if not STEAM_API_KEY:
        await tg_app.bot.send_message(chat_id, "❌ STEAM_API_KEY not set.")
        return

    # Fetch from Supabase
    all_accounts = await _sb_get(
        "steamCredentials",
        **{"select": "email,password,game_name,status,steam_id", "limit": 2000}
    ) or []

    supabase_acc = next((acc for acc in all_accounts 
                        if str(acc.get("email", "")).lower().strip() == term.lower().strip()), None)

    supabase_text = ""
    if supabase_acc:
        password = html.escape(supabase_acc.get("password", "HIDDEN"))
        supabase_text = (
            f"✅ <b>Found in Supabase</b>\n"
            f"🎮 Game: {supabase_acc.get('game_name', '—')}\n"
            f"Status: <b>{supabase_acc.get('status', 'Available')}</b>\n"
            f"🔑 Password: <tg-spoiler>{password}</tg-spoiler>\n\n"
        )
    else:
        supabase_text = "❌ Not found in Supabase.\n\n"

    # Get steam_id
    steamid = None
    if supabase_acc and supabase_acc.get("steam_id"):
        sid = str(supabase_acc.get("steam_id")).strip()
        if sid.isdigit() and len(sid) == 17:
            steamid = sid

    if not steamid:
        await send_animated_translated(
            chat_id,
            f"🔍 <b>Result for:</b> <code>{html.escape(term)}</code>\n\n"
            f"{supabase_text}"
            f"🌐 Live data unavailable — No steam_id stored.",
            animation_url=STEAM_RESULT_GIF,
        )
        return

    # Fetch live data
    live_status = "❌ Could not fetch status"
    games = []
    try:
        sum_url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={steamid}"
        sum_r = await http.get(sum_url, timeout=10.0)
        players = sum_r.json().get("response", {}).get("players", [])
        if players:
            p = players[0]
            state_map = {0:"Offline",1:"Online",2:"Busy",3:"Away",4:"Snooze",5:"Looking to Trade",6:"Looking to Play"}
            status = state_map.get(p.get("personastate", 0), "Unknown")
            game = p.get("gameextrainfo")
            live_status = f"🎮 <b>Currently playing</b>: {game}\nStatus: {status}" if game else f"Status: {status}"

        games_url = f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/?key={STEAM_API_KEY}&steamid={steamid}&include_appinfo=1&include_played_free_games=1"
        games_r = await http.get(games_url, timeout=15.0)
        games_data = games_r.json().get("response", {}).get("games", [])
        if games_data:
            games_data.sort(key=lambda x: x.get("playtime_forever", 0), reverse=True)
            games = games_data
    except Exception as e:
        print(f"Live fetch error: {e}")

    # Show the requested page (and edit if from callback)
    await show_games_page(chat_id, term, supabase_text, live_status, games, page=page, query=query)

async def show_steam_search_results(chat_id: int, results: list, first_name: str, query=None, game_query: str = ""):
    text = f"✅ <b>Found {len(results)} account(s)!</b>\n\n"
    buttons = []
    query_lower = game_query.lower()  # ← fix: define it here

    for acc in results[:8]:
        games_list = [acc.get("game_name") or ""] + (acc.get("games") or [])
        matched_game = next(
            (g for g in games_list if g and query_lower in g.lower()),
            acc.get("game_name") or "Steam Account"
        )
        text += f"🎮 <b>{html.escape(matched_game)}</b>\n"
        text += f"📦 <i>+{len(acc.get('games') or [])} other games in this account</i>\n\n"
        buttons.append([InlineKeyboardButton(
            f"✅ Claim — {matched_game[:30]}",
            callback_data=f"claim_steam|{acc.get('email')}"
        )])

    buttons.append([InlineKeyboardButton("🔎 Search Different Game", callback_data="vamt_filter_steam")])
    final_text = text + "⏳ Result expires in <b>5 minutes</b>."

    if query and query.message:
        try:
            await query.message.edit_caption(caption=final_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            await query.message.edit_text(text=final_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await tg_app.bot.send_message(chat_id, final_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

# ──────────────────────────────────────────────
# New Helper Function for Pagination
# ──────────────────────────────────────────────
async def show_games_page(chat_id: int, term: str, supabase_text: str, live_status: str, all_games: list, page: int = 0, query=None):
    """Show games with pagination - edits message if called from callback"""
    GAMES_PER_PAGE = 10
    total_games = len(all_games)
    total_pages = (total_games + GAMES_PER_PAGE - 1) // GAMES_PER_PAGE if total_games > 0 else 1

    start = page * GAMES_PER_PAGE
    end = min(start + GAMES_PER_PAGE, total_games)
    page_games = all_games[start:end]

    text = f"🔍 <b>Steam Search Result for:</b> <code>{html.escape(term)}</code>\n\n"
    text += supabase_text
    text += f"🌐 <b>Live Steam Data</b>\n{live_status}\n\n"

    if total_games == 0:
        text += "⚠️ No games visible (profile is likely private)."
    else:
        text += f"🎮 <b>{total_games:,} games owned</b> — Page {page+1} of {total_pages}\n\n"
        for g in page_games:
            name = g.get("name", "Unknown Game")
            hours = round(g.get("playtime_forever", 0) / 60, 1)
            text += f"• {name} — <b>{hours:,} hrs</b>\n"

    # Navigation buttons
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("↼ Previous", callback_data=f"games_page|{term}|{page-1}"))
    if end < total_games:
        nav.append(InlineKeyboardButton("Next ⇀", callback_data=f"games_page|{term}|{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ Back to Caretaker Menu", callback_data="caretaker_searchsteam")])

    markup = InlineKeyboardMarkup(buttons)

    # === KEY CHANGE: Edit if from callback, else send new message ===
    try:
        if query and query.message:
            await query.message.edit_text(
                text=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            return
    except Exception:
        pass  # fallback if edit fails

    # Fallback: send new message
    await send_animated_translated(
        chat_id=chat_id,
        caption=text,
        animation_url=STEAM_RESULT_GIF,
        reply_markup=markup
    )

# ──────────────────────────────────────────────
# LANGUAGE SELECTOR (now deletes old menu first)
# ──────────────────────────────────────────────
async def handle_set_language(chat_id: int, query=None):
    """Show language selector — deletes old animated main menu first"""
    lang = await get_user_language(chat_id)
    current_flag, current_name = SUPPORTED_LANGUAGES.get(lang, ("🇬🇧", "English"))
   
    text = f"🌍 <b>Current language:</b> {current_flag} {current_name}\n\nChoose your preferred language:"
    
    buttons = [
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_set|en")],
        [InlineKeyboardButton("🇵🇭 Tagalog", callback_data="lang_set|tl")],
        [InlineKeyboardButton("🇵🇭 Bisaya", callback_data="lang_set|ceb")],
        [InlineKeyboardButton("⬅️ Back to Clearing", callback_data="main_menu")]
    ]
    markup = InlineKeyboardMarkup(buttons)

    # 🔥 CRITICAL FIX: Delete the old animated menu
    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    # Send clean language selector
    await send_animated_translated(
        chat_id=chat_id,
        caption=text,
        animation_url=None,
        reply_markup=markup
    )

    # Beta notice (disappears automatically)
    asyncio.create_task(send_temporary_message(chat_id, "🌱 This language feature is still in beta.", duration=3))

# ── Auto-translate helpers (use these from now on) ──
async def send_translated(chat_id: int, text: str, lang: str = None, **kwargs):
    if lang is None:
        lang = await get_user_language(chat_id)
    translated = await translate_text(text, lang)
    return await tg_app.bot.send_message(chat_id=chat_id, text=translated, **kwargs)

async def edit_translated(query, text: str, lang: str = None, **kwargs):
    if lang is None:
        lang = await get_user_language(query.message.chat_id)
    translated = await translate_text(text, lang)
    return await query.message.edit_caption(caption=translated, **kwargs)

async def handle_uploadwinoffice_command(chat_id: int, raw_text: str):
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can upload keys.")
        return

    body = raw_text[len("/uploadwinoffice"):].strip()

    SERVICE_MAP = {
        "windows": ("windows", "Win Key", "🪟"),
        "win":     ("windows", "Win Key", "🪟"),
        "office":  ("office",  "Office Key", "📑"),
    }

    if not body:
        await send_animated_translated(
            chat_id,
            "🗝️ <b>Win / Office Key Uploader</b>\n\n"
            "<b>Basic (auto display name):</b>\n"
            "<code>/uploadkeys2\n"
            "windows\n"
            "XXXXX-XXXXX-XXXXX-XXXXX-XXXXX</code>\n\n"
            "<b>With custom display name:</b>\n"
            "<code>/uploadkeys2\n"
            "windows|Windows 11 Pro\n"
            "XXXXX-XXXXX-XXXXX-XXXXX-XXXXX</code>\n\n"
            "<b>With remaining count:</b>\n"
            "<code>/uploadkeys2\n"
            "office|Office 2021 Pro\n"
            "XXXXX-XXXXX-XXXXX-XXXXX|50\n"
            "YYYYY-YYYYY-YYYYY-YYYYY|30</code>\n\n"
            "<b>Bulk mixed blocks:</b>\n"
            "<code>/uploadkeys2\n"
            "windows|Windows 10 Home\n"
            "XXXXX-XXXXX-XXXXX-XXXXX\n"
            "YYYYY-YYYYY-YYYYY-YYYYY\n\n"
            "office|Office 2019\n"
            "ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ|25</code>\n\n"
            "• First line of each block = <code>service|Custom Name</code>\n"
            "• Custom name is optional — defaults to Win Key / Office Key\n"
            "• Key lines support <code>KEY|remaining</code> format\n"
            "• Separate blocks with a blank line\n\n"
            "<i>Tip: Use /checkstock to verify after uploading.</i>",
            animation_url=CARETAKER_GIF,
        )
        return

    imported = 0
    skipped = 0
    results = []
    added_counts = {}

    # Split into blocks by blank lines
    blocks = [b.strip() for b in body.split("\n\n") if b.strip()]

    for block in blocks:
        lines = [l.strip() for l in block.split("\n") if l.strip() and not l.startswith("#")]
        if not lines:
            continue

        # First line = service type (with optional custom display name)
        # Format: "windows" OR "windows|Windows 11 Pro"
        first = lines[0]
        first_parts = first.split("|", 1)
        service_raw = first_parts[0].strip().lower()
        custom_name = first_parts[1].strip() if len(first_parts) > 1 else None

        if service_raw not in SERVICE_MAP:
            skipped += len(lines) - 1
            results.append(
                f"❌ Unknown service: <code>{html.escape(first_parts[0])}</code> "
                f"— use <code>windows</code> or <code>office</code>"
            )
            continue

        service_type, default_name, emoji = SERVICE_MAP[service_raw]

        # Use custom name if provided, otherwise use default
        display_name = custom_name if custom_name else default_name

        key_lines = lines[1:]
        if not key_lines:
            results.append(f"⚠️ No keys found under <b>{display_name}</b> block")
            continue

        for line in key_lines:
            # Parse KEY|remaining or just KEY
            if "|" in line:
                parts = line.split("|")
                key_id = parts[0].strip()
                remaining = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 999
            else:
                key_id = line.strip()
                remaining = 999

            if not key_id:
                continue

            payload = {
                "key_id":       key_id,
                "remaining":    remaining,
                "service_type": display_name,
                "status":       "active",
                "display_name": default_name,
            }

            success = await _sb_upsert("vamt_keys", payload, on_conflict="key_id", ignore_duplicates=True)

            if success:
                imported += 1
                added_counts[service_type] = added_counts.get(service_type, 0) + 1
                short = key_id[:30] + "…" if len(key_id) > 30 else key_id
                results.append(
                    f"✅ {emoji} <b>{html.escape(display_name)}</b>\n"
                    f"   🔑 <code>{html.escape(short)}</code>\n"
                    f"   📦 Remaining: <b>{remaining}</b>"
                )
            else:
                skipped += 1
                results.append(
                    f"❌ {emoji} <b>{html.escape(display_name)}</b>\n"
                    f"   🔑 <code>{html.escape(key_id[:30])}</code>\n"
                    f"   ⚠️ Skipped — already exists or rejected by Supabase"
                )

    # Clear cache so keys show immediately
    await redis_client.delete("vamt_cache")

    # Build summary with per-service breakdown
    breakdown = ""
    if added_counts:
        breakdown = "\n".join(
            f"{'🪟' if svc == 'windows' else '📑'} {svc.title()}: <b>+{cnt}</b>"
            for svc, cnt in added_counts.items()
        )
        breakdown = f"\n<b>Breakdown:</b>\n{breakdown}\n"

    summary = (
        f"🗝️ <b>Key Upload Complete!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"✅ Imported: <b>{imported}</b>\n"
        f"⚠️ Skipped: <b>{skipped}</b>\n"
        f"{breakdown}\n"
        f"<b>Results:</b>\n"
        + "\n".join(results[:20])
    )

    if len(results) > 20:
        summary += f"\n<i>...and {len(results) - 20} more</i>"

    if imported > 0:
        asyncio.create_task(broadcast_new_resources(added_counts))

    await send_animated_translated(chat_id, summary, animation_url=CARETAKER_GIF)

async def handle_uploadsteam_command(chat_id: int, raw_text: str):
    if chat_id != OWNER_ID:
        await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can upload Steam accounts.")
        return

    body = raw_text.replace("/uploadsteam", "").strip()
    if not body:
        await send_animated_translated(
            chat_id,
            "🎮 <b>Steam Account Uploader</b>\n\n"
            "<b>Single account:</b>\n"
            "<code>/uploadsteam\n"
            "email\n"
            "password\n"
            "Game Name (optional)\n"
            "SteamID64 (optional)\n"
            "https://banner-url.jpg (optional)</code>\n\n"
            "<b>Bulk (separate accounts with a blank line):</b>\n"
            "<code>/uploadsteam\n"
            "email1\n"
            "password1\n"
            "Game Name\n"
            "76561198XXXXXXXXX\n"
            "https://banner.jpg\n\n"
            "email2\n"
            "password2\n\n"
            "email3\n"
            "password3\n"
            "Only Game Name</code>\n\n"
            "⚠️ <i>Minimum required: email + password</i>\n"
            "💡 <i>SteamID = 17 digits | Banner = must start with https://</i>\n"
            "<i>Tip: Use /searchsteam first to check for duplicates.</i>",
            animation_url=STEAM_GIF
        )
        return

    # Split blocks by blank lines
    blocks = [b.strip() for b in body.split("\n\n") if b.strip()]

    total_accounts = len(blocks)
    if total_accounts == 0:
        await tg_app.bot.send_message(chat_id, "❌ No valid account blocks found.")
        return
    
    # Initial nice loading message
    loading = await tg_app.bot.send_message(
        chat_id, 
        f"🎮 <b>Processing Steam Accounts</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📄 Processing pasted content\n"
        f"🔍 Found <b>{total_accounts}</b> accounts...\n\n"
        f"⏳ Starting import into the forest...",
        parse_mode="HTML"
    )

    imported = 0
    skipped = 0
    results = []
    processed = 0

    for i, block in enumerate(blocks, start=1):
        processed += 1
        percentage = int((processed / max(total_accounts, 1)) * 100)

        # Update progress every 5 accounts (smooth & safe for Telegram)
        if processed % 5 == 0 or processed == total_accounts:
            progress_bar = get_colored_progress_bar(percentage)

            await loading.edit_text(
                f"🎮 <b>Processing Steam Accounts</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📊 <b>{processed}/{total_accounts}</b> accounts • <b>{percentage}%</b>\n\n"
                f"{progress_bar}\n\n"
                f"✅ <b>Imported:</b> {imported}\n"
                f"⚠️ <b>Skipped:</b> {skipped}",
                parse_mode="HTML"
            )

        # === Your original smart parsing logic (kept 100% intact) ===
        lines = [l.strip() for l in block.split("\n") if l.strip()]

        if len(lines) < 2:
            skipped += 1
            results.append(f"❌ Block {i} — Too few fields (need at least email + password)")
            continue

        email     = lines[0]
        password  = lines[1]
        game_name = lines[2] if len(lines) >= 3 else None

        # Smart field detection
        steam_id  = None
        image_url = None
        extra_games = []

        for field in lines[3:]:
            field = field.strip()
            if not field:
                continue
            if field.isdigit() and len(field) == 17:
                steam_id = field
            elif field.startswith("http"):
                image_url = clean_image_url(field)
            else:
                extra_games.append(field)

        # Build payload (your original logic)
        manila = pytz.timezone("Asia/Manila")
        tonight_8pm = datetime.now(manila).replace(
            hour=20, minute=0, second=0, microsecond=0
        ).astimezone(pytz.utc).isoformat()

        payload = {
            "email":        email,
            "password":     password,
            "game_name":    game_name,
            "games":        extra_games,
            "steam_id":     steam_id,
            "image_url":    image_url,
            "status":       "Available",
            "release_type": "daily",
            "release_at":   tonight_8pm,
            "action":       None,
            "Posted":       None,
        }

        try:
            r = await asyncio.wait_for(
                http.post(
                    f"{SUPABASE_URL}/rest/v1/steamCredentials",
                    headers=_supabase_headers({
                        "Content-Type": "application/json",
                        "Prefer": "resolution=ignore-duplicates,return=minimal",
                    }),
                    json=payload,
                ),
                timeout=12.0,
            )
            if r.status_code in (200, 201):
                imported += 1
                label = f"{game_name or 'Unknown Game'}"
                results.append(f"✅ <code>{html.escape(email)}</code> — {label}")
            elif r.status_code == 409:
                skipped += 1
                results.append(f"⚠️ <code>{html.escape(email)}</code> — Duplicate")
            else:
                skipped += 1
                results.append(f"⚠️ <code>{html.escape(email)}</code> — Rejected (status {r.status_code})")
        except asyncio.TimeoutError:
            skipped += 1
            results.append(f"❌ <code>{html.escape(email)}</code> — Timed out")
        except Exception as e:
            skipped += 1
            results.append(f"❌ <code>{html.escape(email)}</code> — Error: {str(e)[:50]}")

    # Final beautiful result
    result = (
        f"✅ <b>Steam Accounts Import Complete!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎮 <b>Successfully imported:</b> {imported} accounts\n"
        f"⚠️ <b>Skipped / Duplicates:</b> {skipped}\n\n"
    )

    if results:
        result += "<b>Results:</b>\n" + "\n".join(results[:20]) + "\n"

    if len(results) > 20:
        result += f"<i>...and {len(results) - 20} more</i>"

    result += "\n\n🧹 All Redis caches cleared — new accounts are now live in the forest! 🌲"

    await loading.edit_text(result, parse_mode="HTML")

    if imported > 0:
        asyncio.create_task(broadcast_new_resources({"steam": imported}))

async def safe_edit(msg, text: str, reply_markup=None):
    """Edit caption or text depending on message type."""
    try:
        await msg.edit_caption(caption=text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        try:
            await msg.edit_text(text=text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass
    
async def hide_bot_commands(chat_id: int):
    try:
        await tg_app.bot.set_my_commands(
            commands=[],
            scope=BotCommandScopeChat(chat_id=chat_id)
        )
        print(f"✅ Commands hidden for {chat_id}")
    except Exception as e:
        print(f"🔴 Could not hide commands for {chat_id}: {e}")

async def restore_bot_commands(chat_id: int):
    """Remove chat-specific override → falls back to BotFather commands"""
    try:
        await tg_app.bot.delete_my_commands(
            scope=BotCommandScopeChat(chat_id=chat_id)  # ← just delete the override
        )
        print(f"✅ Commands restored to BotFather defaults for {chat_id}")
    except Exception as e:
        print(f"🔴 Could not restore commands for {chat_id}: {e}")

@safe_handler("handle_callback")
async def handle_callback(update: Update):
    query     = update.callback_query
    if not query or not query.data:
        return
    chat_id   = update.effective_chat.id
    first_name = update.effective_user.first_name if update.effective_user else "Wanderer"
    data      = query.data

    # ── Update presence on EVERY button tap ──
    asyncio.create_task(update_last_active(chat_id))

    FEEDBACK_PREFIXES = (
        "kfb_ok|",
        "kfb_bad|",
        "wkfb_ok|",
        "wkfb_bad|",
        "key_feedback_ok|",
        "key_feedback_bad|",
        "copy_ref_link|",
        "stfb_ok|", "stfb_bad|",
        "stfb_undo|",
        "owner_restore|",
        "owner_keep|",
    )
    if not data.startswith(FEEDBACK_PREFIXES):
        await query.answer()

    # ── ONBOARDING GUARD FOR CALLBACKS ──
    ONBOARDING_ALLOWED = (
        "onboarding_step_", "onboarding_skip", "onboarding_complete", "show_main_menu"
    )
    onboarding_done = await has_completed_onboarding(chat_id)
    profile_check = await get_user_profile(chat_id)

    if not onboarding_done and profile_check is None:
        if not any(data.startswith(p) for p in ONBOARDING_ALLOWED):
            await query.answer(
                "🌿 Please complete the welcome tour first!",
                show_alert=True
            )
            return

# ── ONBOARDING TUTORIAL ──
    if data.startswith("onboarding_step_"):
        try:
            step = int(data.split("_")[2])
        except Exception:
            step = 1
        try:
            await query.message.delete()
        except Exception:
            pass
        await send_onboarding_step(chat_id, first_name, step=step)
        return
    
    elif data.startswith("show_stats|"):
        short_key = data.split("|")[1]
        await show_steam_claim_detail(
            chat_id=chat_id,
            first_name=first_name,
            short_key=short_key,
            back_page=0,
            query=query,
            show_full_stats=True
        )
        return

    elif data.startswith("profile_page_"):
        try:
            page = int(data.split("_")[2])
            await handle_profile_page(chat_id, first_name, query, page=page)
        except Exception as e:
            print(f"Profile page error: {e}")
        return

    # ── NEW: MULTI-ACCOUNT SELECTION SCREEN ──
    elif data.startswith("steam_sel|"):
        parts = data.split("|")
        target_chat_id = int(parts[1])
        group_key = parts[2]
        await show_steam_account_selection(target_chat_id, group_key, "", query)
        return
    
    elif data.startswith("set_title|"):
        chosen_title = data.split("|", 1)[1].strip()

        # Fetch profile here since it may not be set yet at this point
        profile = await get_user_profile(chat_id)
        if not profile:
            await query.answer("🌿 Profile not found.", show_alert=True)
            return

        # Validate: title must exist in level titles AND be unlocked
        current_level = profile.get("level", 1)
        valid_titles = [
            title_name
            for lvl, (title_name, _) in LEVEL_TITLE_DESCRIPTIONS.items()
            if lvl <= current_level
        ]

        if chosen_title not in valid_titles:
            await query.answer("❌ You haven't unlocked this title!", show_alert=True)
            return

        await _sb_patch(
            f"user_profiles?chat_id=eq.{chat_id}",
            {"active_title": chosen_title}
        )

        await query.answer(f"✅ Title set!", show_alert=False)

        asyncio.create_task(send_temporary_message(
            chat_id,
            f"🏷️ <b>Title updated!</b>\n\n"
            f"You are now known as:\n<b>{chosen_title}</b> 🌿",
            duration=4
        ))

        await handle_profile_page(chat_id, first_name, query)

    elif data == "owner_steam_search":
            if chat_id != OWNER_ID:
                await query.answer("🌿 Only the Forest Caretaker.", show_alert=True)
                return
            await handle_searchsteam_command(chat_id, "/searchsteam")
            return

    elif data == "show_online_users":
            await redis_client.delete("online_users_cache")
            await handle_online_users(chat_id, query)
            return
    
    elif data == "show_referral_history":
        await handle_referral_history(chat_id)
        return

    elif data == "view_notion_steam_refresh":
        await view_notion_steam_library(chat_id, page=0, query=query)
        return
    
    elif data == "show_settings_page":
        await handle_settings_page(chat_id, first_name, query)
        return
    
    elif data == "show_achievements":
        await show_achievements_page(chat_id, query, page=0)

    elif data == "show_streak_calendar":
            await show_streak_calendar(chat_id, first_name, query)
            return
    
    # ── DONATE / SUPPORT THE FOREST ──
    elif data == "donate":
        await safe_edit(
            query.message,
            (
                "🌳 <b>Support the Enchanted Clearing</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "Every donation helps keep the ancient trees alive, "
                "the servers running smoothly, and new treasures appearing daily.\n\n"
                "Even the smallest act of kindness grows into something beautiful in the forest.\n\n"
                "<i>Thank you, kind wanderer. The trees remember you. 🍃✨</i>"
            ),
            reply_markup=kb_donate()
        )
        return
    
    # ── FOREST PATRONS / DONOR WALL ──
    elif data == "patrons":
        await show_patrons_page(chat_id, query)
        return
    
        # ── GCASH QR CODE SCREEN ──
    elif data == "gcash_qr":
        await show_gcash_qr(chat_id, query)
        return

    elif data == "onboarding_skip":
        await mark_onboarding_complete(chat_id)
        await redis_client.delete(f"onboarding_step:{chat_id}")
        await restore_bot_commands(chat_id)
        try:
            await query.message.delete()
        except Exception:
            pass
        # Give a small consolation XP for at least starting
        await add_xp(chat_id, first_name, "onboarding_skip")
        await send_initial_welcome(chat_id, first_name)
        return

    elif data == "onboarding_complete":
        await mark_onboarding_complete(chat_id)
        await redis_client.delete(f"onboarding_step:{chat_id}")
        await restore_bot_commands(chat_id)
        try:
            await query.message.delete()
        except Exception:
            pass
        await add_xp(chat_id, first_name, "onboarding_complete")
        asyncio.create_task(send_temporary_message(
            chat_id,
            "🎉 <b>Tour Complete!</b>\n\n✨ <b>+15 XP</b> added to your forest energy! 🌱",
            duration=5
        ))
        await asyncio.sleep(0.5)
        await send_initial_welcome(chat_id, first_name)
        return

    # ── MAIN MENU ──
    if data in ("show_main_menu", "main_menu"):
        try:
            await query.message.delete()
        except Exception:
            pass
        await asyncio.sleep(0.8)

        loading = await send_animated_translated(
            chat_id=chat_id,
            animation_url=LOADING_GIF,
            caption="🌫️ <i>The ancient mist begins to lift once more...</i>",
        )
        await asyncio.sleep(1.3)
        await safe_edit(loading,"🌿 <i>The whispering trees lean in to welcome you home...</i>")
        await asyncio.sleep(1.3)
        await safe_edit(loading,"✨ <i>You stand again in the heart of the Enchanted Clearing...</i>")
        await asyncio.sleep(1.0)

        profile = await get_user_profile(chat_id)
        if not profile:
            action_xp, _ = await add_xp(chat_id, first_name, "general")
            profile = await get_user_profile(chat_id)

        is_first = not bool(profile.get("has_seen_menu", False)) if profile else True

        if profile and not profile.get("has_seen_menu", False):
            asyncio.create_task(update_has_seen_menu(chat_id))
    
        try:
            await tg_app.bot.delete_message(chat_id, loading.message_id)
        except Exception:
            pass

        await send_full_menu(chat_id, first_name, is_first_time=is_first)
        return

    # ── REGISTRATION GUARD (all other callbacks) ──
    profile = await get_user_profile(chat_id)
    if not profile:
        await send_animated_translated(
            chat_id=chat_id,
            animation_url=HELLO_GIF,
            caption="🌿 <b>A gentle breeze rustles the leaves...</b>\n\n"
                    "To step into the Enchanted Clearing, please press the button below.",
            reply_markup=kb_start(),
        )
        return

    # ── INVENTORY ──
    if data == "check_vamt":
        try:
            await query.message.delete()
        except Exception:
            pass
        await send_animated_translated(
            chat_id=chat_id,
            animation_url=INVENTORY_GIF,
            caption=(
                "📜 <b>Ancient Library — Resource Scrolls</b>\n\n"
                "Choose the type of resource you need today:\n\n"
                "<i>Viewing items earns XP and helps you level up.</i>"
            ),
            reply_markup=kb_inventory(),
        )

    elif data == "show_resources":
            immersive_text = (
                "📦 <b>The Resource Grove</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "Deep within the Enchanted Clearing, where golden sunlight filters through ancient leaves, "
                "lies a hidden grove tended by the forest spirits themselves.\n\n"
                "Here they have gathered the rarest treasures and wisest knowledge from distant realms — "
                "all for kind wanderers like you.\n\n"
                "<i>Choose your path below, and may the trees guide you to what you seek...</i> 🍃✨"
            )
            try:
                await query.message.delete()
            except Exception:
                pass

            await send_animated_translated(
                chat_id=chat_id,
                caption=immersive_text,
                animation_url=RESOURCES_GIF,
                reply_markup=kb_resources()
            )
            return

    elif data == "show_crunchyroll_bot":
        try:
            await query.message.delete()
        except Exception:
            pass

        caption = (
            "🍜 <b>Clyde's Crunchy Checker</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "A companion bot nestled deeper in the enchanted forest, "
            "dedicated entirely to <b>Crunchyroll account validation</b>.\n\n"
            "🌿 <b>What it offers:</b>\n"
            "• 🔍 Check Crunchyroll account status\n"
            "• ✅ Verify premium subscription validity\n"
            "• 🌏 Detect regional restrictions\n"
            "• 🎌 Test streaming capability\n"
            "• 📊 Get detailed account info (plan, expiry, etc.)\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>The anime spirits await you in a quieter grove of the forest.</i> 🍃✨"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🍜 Open Crunchyroll Bot", url="https://t.me/clydecrunchybot")],
            [InlineKeyboardButton("⬅️ Back to Resources", callback_data="show_resources")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
        ])

        msg = await send_animated_translated(
            chat_id=chat_id,
            animation_url=CRUNCHY_GIF,
            caption=caption,
            reply_markup=keyboard,
        )
        await _remember(chat_id, msg.message_id)
        return

    elif data == "set_language":
        await handle_set_language(chat_id, query=query)
        return

    elif data.startswith("bulk_page|"):
        if chat_id != OWNER_ID:
            return
        try:
            page = int(data.split("|")[1])
            bulk_key = f"bulk_search:{chat_id}"
            stored = await redis_client.get(bulk_key)
            if not stored:
                await query.answer("Search expired. Please search again.", show_alert=True)
                return
            lines = json.loads(stored)
            # Pass query so it edits instead of sending new message
            fake_text = "/searchsteam\n" + "\n".join(lines)
            await handle_searchsteam_command(chat_id, fake_text, page=page, query=query)
        except Exception as e:
            print(f"Bulk page error: {e}")
            await query.answer("Error loading page", show_alert=True)
        return
    
    elif data.startswith("cpage_"):
        if chat_id != OWNER_ID:
            await query.answer("🌿 Only the Forest Caretaker may enter.", show_alert=True)
            return
        page_name = data.replace("cpage_", "")
        await show_caretaker_page(chat_id, page_name, query)
        return
    
    elif data == "reset_title":
        await _sb_patch(
            f"user_profiles?chat_id=eq.{chat_id}",
            {"active_title": None}
        )

        default = get_level_title(profile.get("level", 1))

        await query.answer("🔄 Title reset to default!", show_alert=False)

        asyncio.create_task(send_temporary_message(
            chat_id,
            f"🔄 <b>Title reset!</b>\n\n"
            f"Back to your default:\n<b>{default}</b> 🌿",
            duration=4
        ))

        await handle_profile_page(chat_id, first_name, query)
    
    elif data == "run_test_achievements":
        if chat_id != OWNER_ID:
            return
        await handle_test_achievements(chat_id)
        return

    elif data == "caretaker_home":
        if chat_id != OWNER_ID:
            return
        try:
            await query.message.delete()
        except Exception:
            pass
        await handle_caretaker(chat_id, first_name)
        return
    
    elif data.startswith("cookie_tutorial_"):
        parts = data.split("_")
        try:
            service = parts[2]
            page    = int(parts[3])
        except Exception:
            service, page = "netflix", 1

        # Dynamic labels
        emoji_map  = {"netflix": "🍿", "prime": "🎥", "crunchyroll": "🍜"}
        name_map   = {"netflix": "Netflix", "prime": "Prime Video", "crunchyroll": "Crunchyroll"}
        domain_map = {"netflix": "netflix.com", "prime": "primevideo.com", "crunchyroll": "crunchyroll.com"}

        emoji     = emoji_map.get(service, "🍜")
        name      = name_map.get(service, service.title())
        domain    = domain_map.get(service, "crunchyroll.com")
        btn_label = f"{emoji} Get a {name} Cookie Now"

        pages = {
            1: (
                f"{emoji} <b>How to Use a {name} Cookie — Page 1/3</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "📋 <b>What you need:</b>\n"
                "• A PC or laptop (Chrome/Firefox)\n"
                f"• The {name} cookie file from the bot\n"
                "• A cookie editor extension\n\n"
                "🔧 <b>Step 1 — Install Extension</b>\n\n"
                "Chrome:\n"
                "→ Install <b>EditThisCookie</b> or <b>Cookie-Editor</b>\n"
                "→ Search it on Chrome Web Store\n\n"
                "Firefox:\n"
                "→ Install <b>Cookie-Editor</b> from Firefox Add-ons\n\n"
                "<i>Tap Next → to continue</i>",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("Next ⇀", callback_data=f"cookie_tutorial_{service}_2")],
                    [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")],
                ])
            ),
            2: (
                f"{emoji} <b>How to Use a {name} Cookie — Page 2/3</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "🔧 <b>Step 2 — Prepare the Cookie</b>\n\n"
                "1. Open the <b>.txt file</b> the bot sent you\n"
                "2. Copy <b>everything</b> inside it\n\n"
                "🔧 <b>Step 3 — Import the Cookie</b>\n\n"
                "1. Open your browser\n"
                f"2. Go to <b>{domain}</b>\n"
                "   (don't log in — just open the site)\n"
                "3. Click your cookie extension icon\n"
                "4. Click <b>Import</b> or paste into text box\n"
                "5. Paste the cookie content\n"
                "6. Click <b>Save</b> or <b>Import</b>\n\n"
                "<i>Tap Next → for the final step</i>",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("↼ Previous", callback_data=f"cookie_tutorial_{service}_1"),
                        InlineKeyboardButton("Next ⇀",     callback_data=f"cookie_tutorial_{service}_3"),
                    ],
                    [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")],
                ])
            ),
            3: (
                f"{emoji} <b>How to Use a {name} Cookie — Page 3/3</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "🔧 <b>Step 4 — Access the Account</b>\n\n"
                f"1. After importing, <b>refresh</b> {domain}\n"
                "2. You should now be logged in ✅\n\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "⚠️ <b>Important Rules</b>\n\n"
                "• Do <b>NOT</b> change the password or email\n"
                "• Do <b>NOT</b> sign out other sessions\n"
                "• Use <b>incognito/private</b> mode when possible\n"
                f"• {name} cookies expire — get a new one if it stops working\n\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "❌ <b>Cookie not working?</b>\n"
                f"→ Tap <b>❌ Not Working</b> on your {name} cookie file\n"
                "   to report it to the Caretaker 🍃",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("↼ Previous", callback_data=f"cookie_tutorial_{service}_2")],
                    [InlineKeyboardButton(btn_label,    callback_data=f"vamt_filter_{service}")],
                    [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")],
                ])
            ),
        }

        text, keyboard = pages.get(page, pages[1])

        try:
            await query.message.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await send_animated_translated(
                chat_id=chat_id,
                caption=text,
                animation_url=COOKIE_TUTORIAL_GIF,
                reply_markup=keyboard
            )
        return
    
    elif data.startswith("toggle_notif|"):
        notif_type = data.split("|")[1]

        SUPABASE_SERVICES = ("netflix", "prime", "windows", "steam")
        REDIS_SERVICES    = ("daily_bonus","gifs")

        if notif_type not in (*SUPABASE_SERVICES, *REDIS_SERVICES):
            await query.answer("❌ Unknown setting.", show_alert=True)
            return

        if notif_type == "daily_bonus":
            new_state = await toggle_daily_bonus_notif(chat_id)
        elif notif_type == "gifs":          # ← add this
            new_state = await toggle_gif_setting(chat_id)
        else:
            new_state = await toggle_service_notif(chat_id, notif_type)

        label_map = {
            "daily_bonus": "Daily Bonus Alert",
            "netflix":     "Netflix Alerts",
            "prime":       "Prime Alerts",
            "windows":     "Windows/Office Alerts",
            "steam":       "Steam Alerts",
            "gifs": "GIF Animations",
        }

        context_map = {
            "daily_bonus": (
                "You'll see a popup when your daily XP is added.",
                "No popup when daily XP is added. XP still counts!"
            ),
            "netflix": (
                "You'll be notified when new Netflix cookies drop.",
                "You won't receive Netflix upload notifications."
            ),
            "prime": (
                "You'll be notified when new Prime cookies drop.",
                "You won't receive Prime upload notifications."
            ),
            "windows": (
                "You'll be notified when new Windows/Office keys drop.",
                "You won't receive Windows/Office upload notifications."
            ),
            "steam": (
                "You'll be notified when Steam accounts are uploaded.",
                "You won't receive Steam upload notifications."
            ),
            "gifs": (
                "Animations will play throughout the bot.",
                "Text-only mode active. No GIFs will play."
            ),
        }

        icon    = "🔔" if new_state else "🔕"
        status  = "ON"  if new_state else "OFF"
        label   = label_map.get(notif_type, notif_type)
        context_on, context_off = context_map.get(notif_type, ("", ""))
        context = context_on if new_state else context_off

        # ✅ Answer query first — stops spinner immediately
        await query.answer(f"{icon} {label} is now {status}", show_alert=False)

        # 3-second confirmation message
        asyncio.create_task(send_temporary_message(
            chat_id,
            f"{icon} <b>{label} is now {status}</b>\n\n"
            f"<i>{context}</i> 🍃",
            duration=3
        ))

        # ✅ Pass query so old settings page is deleted before new one appears
        await handle_settings_page(chat_id, first_name, query=query)
        return

    elif data.startswith("lang_set|"):
        lang_code = data.split("|")[1]
        await set_user_language(chat_id, lang_code)
        
        flag, name = SUPPORTED_LANGUAGES.get(lang_code, ("🇬🇧", "English"))
        
        await send_temporary_message(
            chat_id=chat_id,
            text=f"✅ Language successfully changed to {flag} {name}!",
            duration=3
        )
        
        await query.message.delete()
        
        await send_full_menu(chat_id, first_name)
        return
    
    elif data == "view_notion_steam":
        await view_notion_steam_library(chat_id)

    # ── Games Pagination ──
    elif data.startswith("games_page|"):
        try:
            _, term, page_str = data.split("|")
            page = int(page_str)

            # Pass the query so we can EDIT the current message instead of sending new one
            await handle_searchsteam_command(chat_id, f"/searchsteam {term}", page=page, query=query)
        except Exception as e:
            print(f"Pagination error: {e}")
            await query.answer("Error loading page", show_alert=True)

    elif data.startswith("notion_page_"):
        page = int(data.split("_")[2])
        await view_notion_steam_library(chat_id, page=page, query=query)

    # ── QUICK INLINE AVAILABILITY UPDATE
    elif data.startswith("quick_set|"):
        _, page_id, new_status = data.split("|")
        
        # Get game name for nice message (we'll extract it from query if possible)
        game_name = "Game"  # fallback
        try:
            # Try to read game name from current message text (best effort)
            if query and query.message and query.message.text:
                lines = query.message.text.split("\n")
                for line in lines:
                    if line.startswith("🎮"):
                        game_name = line.replace("🎮", "").strip()
                        break
        except:
            pass

        headers = {
            "Authorization": f"Bearer {os.getenv('NOTION_TOKEN')}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28"
        }
        payload = {
            "properties": {
                "Availability": {"multi_select": [{"name": new_status}]}
            }
        }
        
        try:
            r = await http.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=headers, json=payload)
            if r.status_code in (200, 204):
                # Beautiful disappearing message
                success_text = f"✅ {game_name} marked as {new_status} successfully!"
                asyncio.create_task(send_temporary_message(chat_id, success_text, duration=2))
                
                # Refresh library
                await view_notion_steam_library(chat_id, page=page if 'page' in locals() else 0, query=query)
            else:
                await query.answer("❌ Failed to update", show_alert=True)
        except Exception as e:
            await query.answer(f"❌ Error: {str(e)[:30]}", show_alert=True)

    # ── BACK TO CARETAKER MENU
    elif data == "caretaker_menu":
        await handle_caretaker(chat_id, first_name)

    # ── WHEEL OF WHISPERS ──
    elif data == "show_wheel_menu":
        await query.answer()
        try:
            await query.message.delete()
        except:
            pass

        caption = (
            "🌟 <b>Wheel of Whispers</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "The ancient wooden wheel, glowing with runes and floating leaves, awaits your touch.\n\n"
            "You may spin <b>once per day</b>.\n"
            "Each spin brings a blessing from the forest spirits.\n\n"
            "<i>Would you like to test your luck?</i> ✨"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌟 Spin Now", callback_data="spin_now")],
            [
                InlineKeyboardButton("🏆 Wheel Leaderboard", callback_data="wheel_leaderboard"),
                InlineKeyboardButton("📜 My Spin History", callback_data="show_wheel_history"),
            ],
            [InlineKeyboardButton("ℹ️ About the Wheel", callback_data="about_wheel")],
            [InlineKeyboardButton("⬅️ Back to Clearing", callback_data="main_menu")]
        ])
        await send_animated_translated(
            chat_id=chat_id,
            animation_url=WHEEL_WHISPERS_GIF,
            caption=caption,
            reply_markup=keyboard
        )
        return
        
    elif data == "show_wheel_history":
        await show_wheel_history(chat_id, query)
        return

    # ── ACTUAL SPIN ──
    elif data == "spin_now":
        await query.answer()

        # ── Manila midnight TTL ──
        manila = pytz.timezone("Asia/Manila")
        now = datetime.now(manila)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        ttl = int((midnight - now).total_seconds())

        # ── Check cooldown with time remaining ──
        if await redis_client.get(f"wheel_spin:{chat_id}"):
            ttl_remaining = await redis_client.ttl(f"wheel_spin:{chat_id}")
            hours = ttl_remaining // 3600
            mins = (ttl_remaining % 3600) // 60
            await tg_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🌿 <b>The Wheel of Whispers is resting...</b>\n\n"
                    f"You can spin again in <b>{hours}h {mins}m</b>\n"
                    "Resets at midnight Manila time ✨"
                ),
                parse_mode="HTML"
            )
            return

        reward_table = [
            {"rarity": "Common",    "xp": 13,  "text": "🌿 <b>Common Reward</b>\n\n+13 XP"},
            {"rarity": "Uncommon",  "xp": 30,  "text": "🍃 <b>Uncommon Reward</b>\n\n+30 XP"},
            {"rarity": "Rare",      "xp": 55,  "text": "🌟 <b>Rare Reward!</b>\n\n+55 XP\n+1 Extra Reveal Slot"},
            {"rarity": "Epic",      "xp": 75,  "text": "✨ <b>Epic Reward!</b>\n\n+75 XP"},
            {"rarity": "Legendary", "xp": 100, "text": "🌠 <b>Legendary Jackpot!</b>"},
            {"rarity": "Secret",    "xp": 45,  "text": "🪄 <b>Secret Reward!</b>\n\n+45 XP"},
        ]
        weights = [50, 28, 12, 6, 3, 1]

        selected = random.choices(reward_table, weights=weights, k=1)[0]
        rarity = selected["rarity"]
        reward_xp = selected["xp"]

        # ── Lock immediately after selection so exceptions can't allow re-spins ──
        await redis_client.setex(f"wheel_spin:{chat_id}", ttl, "1")

        # ── Rarity buildup messages ──
        RARITY_BUILDUP = {
            "Common":    "🌿 <b>The wheel slows gently...</b>\n\n<i>The forest offers a quiet blessing.</i>",
            "Uncommon":  "🍃 <b>The wheel hums with energy...</b>\n\n<i>Something stirs in the branches above.</i>",
            "Rare":      "🌟 <b>The runes flash brightly!</b>\n\n<i>The ancient spirits take notice...</i>",
            "Epic":      "✨ <b>The wheel blazes with golden light!</b>\n\n<i>The whole forest holds its breath...</i>",
            "Legendary": "🌠 <b>THE WHEEL ERUPTS IN STARLIGHT!</b>\n\n<i>Even the oldest trees have never seen this...</i>",
            "Secret":    "🪄 <b>Something unexpected flickers...</b>\n\n<i>A hidden spirit winks from the shadows...</i>",
        }

        # ── Rarity flavor text ──
        RARITY_FLAVOR = {
            "Common":    "The forest whispers a small blessing your way. 🌿",
            "Uncommon":  "The trees rustle with quiet approval. 🍃",
            "Rare":      "A golden leaf drifts down from the canopy above. 🌟",
            "Epic":      "The spirits dance around you in celebration! ✨",
            "Legendary": "The oldest tree in the clearing bows to you. 🌠",
            "Secret":    "A mischievous forest sprite vanishes with a grin. 🪄",
        }

        # ── Immersive spin animation ──
        loading = await send_animated_translated(
            chat_id=chat_id,
            animation_url=WHEEL_WHISPERS_GIF,
            caption="🌿 <b>You place your hand on the ancient wheel...</b>",
        )
        await asyncio.sleep(1.2)
        await safe_edit(loading,
            "🍃 <b>The runes begin to glow softly...</b>",
        )
        await asyncio.sleep(1.0)
        await safe_edit(loading,
            "✨ <b>Leaves and fireflies swirl around you...</b>",
        )
        await asyncio.sleep(1.0)
        await safe_edit(loading,
            "🌟 <b>The wheel spins faster and faster...</b>",
        )
        await asyncio.sleep(0.8)

        # ── Rarity buildup ──
        await safe_edit(loading, RARITY_BUILDUP[rarity])
        await asyncio.sleep(1.5)

        # ── Award XP via add_xp with override ──
        if reward_xp > 0:
            action_xp, _ = await add_xp(chat_id, first_name, "wheel_spin", xp_override=reward_xp)
            if action_xp:
                asyncio.create_task(send_xp_feedback(chat_id, action_xp))

        # ── Bonus reveal slot ──
        got_bonus_slot = rarity in ["Rare", "Epic", "Legendary"]
        if got_bonus_slot:
            await redis_client.incr(f"daily_reveals_bonus:{chat_id}")
            await redis_client.expire(f"daily_reveals_bonus:{chat_id}", ttl)

        # ── Legendary: deliver fresh cookie ──
        got_fresh_cookie = False
        cookie_service = None
        if rarity == "Legendary":
            vamt_data = await get_vamt_data()
            if vamt_data:
                fresh = [
                    item for item in vamt_data
                    if item.get("service_type") in ["netflix", "prime"]
                    and int(item.get("remaining", 0)) > 0
                ]
                if fresh:
                    item = fresh[-1]
                    service_type = item["service_type"]
                    cookie = str(item.get("key_id", "")).strip()
                    display = str(item.get("display_name", f"{service_type.title()} Cookie"))

                    file_content = (
                        f"🌠 Legendary Wheel Reward — Fresh {service_type.title()} Cookie 🌠\n"
                        "════════════════════════════════\n"
                        f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                        f"{cookie}\n"
                        "════════════════════════════════"
                    )
                    file_bytes = BytesIO(file_content.encode("utf-8"))
                    file_bytes.name = f"{display.replace(' ', '_')}.txt"

                    await tg_app.bot.send_document(
                        chat_id=chat_id,
                        document=file_bytes,
                        caption=(
                            f"🌠 <b>Legendary Jackpot!</b>\n\n"
                            f"Here is your fresh {service_type.title()} cookie!\n\n"
                            f"Enjoy, wanderer 🍃"
                        ),
                        parse_mode="HTML"
                    )
                    got_fresh_cookie = True
                    cookie_service = service_type
                else:
                    # Fallback — no cookies available, compensate with bonus XP
                    await tg_app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "🌠 <b>Legendary Jackpot!</b>\n\n"
                            "The forest searched but the cookie spirits are resting...\n"
                            "You have been blessed with <b>+50 bonus XP</b> instead! 🍃"
                        ),
                        parse_mode="HTML"
                    )
                    # Use a different action key so cooldown doesn't block it
                    action_xp, _ = await add_xp(chat_id, first_name, "legendary_fallback", xp_override=50)
                    if action_xp:
                        asyncio.create_task(send_xp_feedback(chat_id, action_xp))

        # ── Log to wheel_spins table ──
        asyncio.create_task(
            _log_wheel_spin(
                chat_id=chat_id,
                first_name=first_name,
                rarity=rarity,
                xp_earned=reward_xp,
                got_bonus_slot=got_bonus_slot,
                got_fresh_cookie=got_fresh_cookie,
                cookie_service=cookie_service,
            )
        )

        # Check achievements after wheel spin
        asyncio.create_task(
            check_and_award_achievements(chat_id, first_name, action="wheel_spin")
        )

        # ── Final result ──
        final_text = selected["text"] + f"\n\n{RARITY_FLAVOR[rarity]}"
        await safe_edit(loading, final_text)
        return

    # ── ABOUT WHEEL OF WHISPERS ──
    elif data == "about_wheel":
        await query.answer()

        try:
            await query.message.delete()
        except:
            pass

        text = (
            "🌟 <b>About the Wheel of Whispers</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Every wanderer gets <b>1 free spin per day</b>.\n\n"
            "🎁 <b>Rewards:</b>\n"
            "• Common (50%)   → +13 XP\n"
            "• Uncommon (28%) → +30 XP\n"
            "• Rare (12%)     → +55 XP + 1 Extra Reveal Slot\n"
            "• Epic (6%)      → +75 XP\n"
            "• Legendary (3%) → Fresh Netflix or Prime Cookie\n"
            "• Secret (1%)    → Special surprise +45 XP\n\n"
            "Extra Reveal Slots can be used when revealing Netflix or Prime cookies.\n\n"
            "The wheel resets every midnight (Manila time).\n\n"
            "<i>May the forest bless your spins, wanderer.</i> ✨"
        )
        await send_animated_translated(
            chat_id=chat_id,
            animation_url=NEW_UPLOAD_GIF,
            caption=text,
            reply_markup=kb_back_to_wheel()
            )
        return
    
    # ── WHEEL LEADERBOARD ──
    elif data == "wheel_leaderboard":
        await query.answer()
        await handle_wheel_leaderboard(chat_id)
        return

    # ── NOOP (disabled button)
    elif data == "noop":
        await query.answer("No active event right now 🌿", show_alert=True)
        return

    # ── FULL RESET ──
    elif data == "confirm_full_reset":
        reset_payload = {
            "has_seen_menu":   False, "level": 1, "xp": 0, "total_xp_earned": 0,
            "windows_views": 0, "office_views": 0, "netflix_views": 0, "netflix_reveals": 0,
            "prime_views": 0,   "prime_reveals": 0, "times_cleared": 0, "guidance_reads": 0,
            "lore_reads": 0,    "profile_views": 0,
            "last_active": datetime.now(pytz.utc).isoformat(),
        }
        ok1 = await _sb_patch(f"user_profiles?chat_id=eq.{chat_id}", reset_payload)
        ok2 = await _sb_delete(f"xp_history?chat_id=eq.{chat_id}")
        if ok1 and ok2:
            await query.message.edit_text(
                "✨ <b>Full Forest Reset Complete!</b>\n\nYou are now like a completely new wanderer. 🌱",
                parse_mode="HTML",
            )
        else:
            await query.message.edit_text("❌ Failed to perform full reset.")

    elif data == "cancel_reset":
        await query.message.edit_text("❌ Reset cancelled. Your data is safe.")

    # ── VAMT HELP POPUP (shortened - max 200 chars)
    elif data.startswith("explain_vamt") or data == "winoffice_help":
        parts = data.split("|")
        pending_cat = parts[1] if len(parts) > 1 else (await redis_client.get(f"winoffice_pending_cat:{chat_id}") or "win")
        cat_label = "Windows" if pending_cat in ("win", "windows") else "Office"
        cat_emoji = "🪟" if pending_cat in ("win", "windows") else "📑"

        help_text = (
            f"{cat_emoji} <b>What is VAMT / Remaining? ({cat_label} Keys)</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "✅ These are official <b>Microsoft Volume Activation</b> keys.\n"
            "One key can activate <b>multiple PCs</b> at once.\n\n"
            "📦 <b>Remaining</b> = How many more devices this key can still activate.\n\n"
            "• <b>Remaining: 10</b> → Works on up to 10 more computers\n"
            "• <b>Remaining: 0</b> → Key is fully used up\n\n"
            "⚡ <b>Tip:</b> Always use keys with the highest Remaining first — they disappear fast!\n\n"
            "<i>Test quickly after viewing. These are shared keys.</i> 🍃"
        )

        buttons = [
            [InlineKeyboardButton("⬅️ Back to Keys", callback_data=f"back_to_winoffice_keys|{pending_cat}")],
            [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")]
        ]

        await query.message.edit_caption(
            caption=help_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return
    
    elif data.startswith("back_to_winoffice_keys|"):
        try:
            _, category = data.split("|")
        except:
            category = "win"
        
        # Re-show the original key list
        await show_winoffice_keys(chat_id, category, profile, query, first_name)
        return

    # ── INVITE COPY LINK ──
    elif data.startswith("copy_ref_link|"):
        try:
            await query.answer("✅ Link copied to your clipboard!", show_alert=True)
        except:
            pass
        # Send clean link so user can long-press to copy
        link = await get_referral_link(chat_id)
        await tg_app.bot.send_message(
            chat_id=chat_id,
            text=f"🌲 <b>Here is your invite link:</b>\n\n<code>{link}</code>\n\n"
                 f"👉 Long-press the link above and tap <b>Copy</b>",
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    # ── INVITE FRIENDS BUTTON (from main menu)
    elif data == "invite_friends":
        await handle_invite(chat_id, first_name)
        return   # important
    
    elif data == "show_set_title":
        await handle_set_title(chat_id, first_name, query)
        return
    
    elif data == "show_profile_page":
        await handle_profile_page(chat_id, first_name, query)
        return
    
    elif data == "change_profile_logo":
        can_change, hours_left = await can_change_profile_gif(chat_id)
        if not can_change:
            days_left = hours_left // 24
            hours_remaining = hours_left % 24
            time_text = f"{days_left}d {hours_remaining}h" if days_left > 0 else f"{hours_left}h"

            await query.answer("🌿 You've already changed your logo this week!", show_alert=False)
            await tg_app.bot.send_message(
                chat_id,
                f"🌿 <b>Profile Logo Cooldown</b>\n\n"
                f"You can only change your profile logo <b>once per week</b>.\n\n"
                f"⏳ Come back in <b>{time_text}</b> to update it again.\n\n"
                f"<i>The forest remembers your emblem, wanderer. 🍃</i>",
                parse_mode="HTML"
            )
            return

        await redis_client.setex(f"waiting_for_logo:{chat_id}", 600, "1")
        await query.answer("✅ Send your image or GIF now!", show_alert=False)

        await tg_app.bot.send_message(
            chat_id,
            "🌿 <b>Upload Your New Profile Logo</b>\n\n"
            "Send me any of the following within <b>10 minutes</b>:\n\n"
            "• 🎞️ <b>GIF</b> — animated logo\n"
            "• 🖼️ <b>PNG / JPG / WEBP</b> — static image\n\n"
            "Maximum size: <b>10 MB</b>\n\n"
            "<i>This will become your personal emblem in the forest. ✨</i>",
            parse_mode="HTML"
        )
        
    elif data == "show_settings_page":
        await handle_settings_page(chat_id, first_name, query)
        return

    elif data == "leaderboard_from_profile":
        await handle_leaderboard(chat_id)
        return

    # ── MY STEAM CLAIMS ──
    elif data == "my_steam_claims":
        await show_my_steam_claims(chat_id, first_name, query, page=0)
        return

    elif data.startswith("myclaims_page_"):
        try:
            page = int(data.split("_")[2])
            await show_my_steam_claims(chat_id, first_name, query, page=page)
        except Exception as e:
            print(f"My claims page error: {e}")
            await query.answer("Error loading page", show_alert=True)
        return
    
    elif data.startswith("steam_detail|"):
            try:
                parts = data.split("|")
                short_key = parts[1]
                back_page = int(parts[2]) if len(parts) > 2 else 0
                await show_steam_claim_detail(chat_id, first_name, short_key, back_page, query)
            except Exception as e:
                print(f"Steam detail error: {e}")
                await query.answer("Error loading details", show_alert=True)
            return

    # ── STEAM FEEDBACK: Working ──
    elif data.startswith("stfb_ok|"):
        parts = data.split("|")
        if len(parts) == 3:
            _, account_email, game_name = parts

            fb_cooldown_key = f"steam_fb_cd:{chat_id}:{account_email}"
            if await redis_client.exists(fb_cooldown_key):
                ttl = await redis_client.ttl(fb_cooldown_key)
                await query.answer(
                    f"⏳ Please wait {ttl}s before changing your feedback again.",
                    show_alert=True
                )
                return
            await redis_client.setex(fb_cooldown_key, 300, "1")

            await handle_steam_feedback(
                chat_id, first_name,
                account_email, game_name,
                is_working=True,
                query=query
            )

    # ── STEAM FEEDBACK: Not Working ──
    elif data.startswith("stfb_bad|"):
        parts = data.split("|")
        if len(parts) == 3:
            _, account_email, game_name = parts

            fb_cooldown_key = f"steam_fb_cd:{chat_id}:{account_email}"
            if await redis_client.exists(fb_cooldown_key):
                ttl = await redis_client.ttl(fb_cooldown_key)
                await query.answer(
                    f"⏳ Please wait {ttl}s before changing your feedback again.",
                    show_alert=True
                )
                return
            await redis_client.setex(fb_cooldown_key, 300, "1")

            await handle_steam_feedback(
                chat_id, first_name,
                account_email, game_name,
                is_working=False,
                query=query
            )

    # ── STEAM FEEDBACK: User Undo ──
    elif data.startswith("stfb_undo|"):
        parts = data.split("|")
        if len(parts) == 3:
            _, account_email, game_name = parts

            # One undo only per account per session
            undo_key = f"steam_undo:{chat_id}:{account_email}"
            if not await redis_client.set(undo_key, 1, ex=60, nx=True):
                await query.answer(
                    "⚠️ You've already used your undo!",
                    show_alert=True
                )
                return

            # Reset feedback spam guard so they can resubmit
            await redis_client.delete(f"steam_fb:{chat_id}:{account_email}")

            # ── Fetch stored claim data from Redis ──
            stored = await redis_client.get(f"steam_claim_data:{chat_id}:{account_email}")
            claim_data = json.loads(stored) if stored else {}
            password = claim_data.get("password", "—")
            game_name_stored = claim_data.get("game_name", game_name)
            steam_id = claim_data.get("steam_id", "")
            release_type = claim_data.get("release_type", "daily")

            # Notify owner of undo
            await tg_app.bot.send_message(
                OWNER_ID,
                f"↩️ <b>Steam Feedback Undone</b>\n\n"
                f"👤 {html.escape(first_name)} (<code>{chat_id}</code>)\n"
                f"🎮 {html.escape(game_name_stored)}\n"
                f"📧 <code>{html.escape(account_email)}</code>\n\n"
                f"<i>User undid their ❌ report — account status unchanged.</i>",
                parse_mode="HTML"
            )

            await query.answer("↩️ Undone! No changes made.", show_alert=True)

            # ── Strip the "You reported this as" appended line from caption ──
            try:
                current = getattr(query.message, 'caption', None) or getattr(query.message, 'text', None) or ""
                clean_caption = current.split("\n\n━━━")[0]
                new_text = (
                    clean_caption +
                    "\n\n<i>↩️ Feedback cleared. New buttons coming shortly...</i>"
                )
                try:
                    await query.message.edit_caption(caption=new_text, parse_mode="HTML", reply_markup=None)
                except Exception:
                    try:
                        await query.message.edit_text(text=new_text, parse_mode="HTML", reply_markup=None)
                    except Exception:
                        pass  # message type doesn't support editing, ignore silently
            except Exception:
                pass

            # ── Reschedule feedback buttons after short delay ──
            asyncio.create_task(
                send_delayed_feedback_buttons(
                    chat_id=chat_id,
                    account_email=account_email,
                    game_name=game_name_stored,
                    delay=10
                )
            )

    elif data.startswith("remind_later|"):
        parts = data.split("|")
        if len(parts) == 3:
            _, account_email, game_name = parts

            reminded_key = f"steam_reminded:{chat_id}:{account_email}"
            await redis_client.setex(reminded_key, 90000, "1")  # slightly over 24h

            await query.answer("⏳ We'll remind you tomorrow!", show_alert=False)

            try:
                await query.message.edit_text(
                    f"⏳ <b>Got it!</b>\n\n"
                    f"We'll check back tomorrow about <b>{html.escape(game_name)}</b>.\n\n"
                    f"<i>Enjoy your game! 🍃</i>",
                    parse_mode="HTML",
                    reply_markup=None
                )
            except Exception:
                pass

            # Schedule 24 hour final reminder
            asyncio.create_task(
                send_reminder_feedback(
                    chat_id=chat_id,
                    account_email=account_email,
                    game_name=game_name,
                    delay=86400,  # 24 hours
                    is_final=True
                )
            )

    elif data.startswith("skip_feedback|"):
        parts = data.split("|")
        if len(parts) == 2:
            _, account_email = parts
            
            await query.answer(
                "🌿 No worries! Thanks anyway.",
                show_alert=False
            )
            
            try:
                await query.message.delete()
            except Exception:
                pass
                
    elif data.startswith("show_all_games|"):
        short_key = data.split("|")[1]
        raw = await redis_client.get(f"cd:{short_key}")
        if not raw:
            await query.answer("⏳ Session expired.", show_alert=True)
            return

        claim_data = json.loads(raw)
        email = claim_data.get("email", "")
        game_name = claim_data.get("game_name", "Unknown")   # display name user saw

        acc_data = await _sb_get(
            "steamCredentials",
            **{"email": f"eq.{email}", "select": "game_name,games"}
        ) or []

        if not acc_data:
            await query.answer("❌ Not found.", show_alert=True)
            return

        acc = acc_data[0]
        all_games = [acc.get("game_name", "")] + (acc.get("games") or [])
        all_games = [g.strip() for g in all_games if g and str(g).strip()]
        
        # Sort alphabetically for better UX
        all_games = sorted(set(all_games), key=str.lower)

        total_games = len(all_games)

        # Pagination
        parts = data.split("|")
        page = int(parts[2]) if len(parts) > 2 else 0
        games_per_page = 35
        start = page * games_per_page
        end = start + games_per_page
        page_games = all_games[start:end]

        # Build message
        text = (
            f"📋 <b>All Games in Bundle</b>\n"
            f"{html.escape(game_name)}\n"
            f"<b>{total_games}</b> games total\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
        )   

        # Highlight the main/searched game
        for g in page_games:
            prefix = "🎮 " if g.lower() == game_name.lower() else "• "
            text += f"{prefix}{html.escape(g)}\n"

        text += f"\n📄 Page <b>{page+1}</b> of <b>{(total_games-1)//games_per_page + 1}</b>"

        # Buttons
        buttons = []
        nav = []

        if page > 0:
            nav.append(InlineKeyboardButton("↼ Previous", callback_data=f"show_all_games|{short_key}|{page-1}"))
        if end < total_games:
            nav.append(InlineKeyboardButton("Next ⇀", callback_data=f"show_all_games|{short_key}|{page+1}"))

        if nav:
            buttons.append(nav)

        buttons.append([InlineKeyboardButton("⬅️ Back to Account", callback_data=f"steam_detail|{short_key}|0")])

        # === FIXED: Use edit_caption for photo messages ===
        try:
            await query.message.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception:
            # Fallback if it's a text-only message
            await query.message.edit_text(
                text=text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

    # ── OWNER: Restore account to Available ──
    elif data.startswith("owner_restore|"):
        if chat_id != OWNER_ID:
            await query.answer(
                "🌿 Only the Caretaker can do this.",
                show_alert=True
            )
            return

        parts = data.split("|")
        if len(parts) == 3:
            _, account_email, game_name = parts

            await _sb_patch(
                f"steamCredentials?email=eq.{account_email}",
                {"status": "Available"}
            )

            try:
                await query.message.edit_text(
                    query.message.text +
                    "\n\n✅ <b>Restored to Available by Caretaker.</b>",
                    parse_mode="HTML",
                    reply_markup=None
                )
            except Exception:
                pass

            await query.answer(
                "✅ Account restored to Available!",
                show_alert=True
            )

    # ── OWNER: Confirm mark as Unavailable ──
    elif data.startswith("owner_keep|"):
        if chat_id != OWNER_ID:
            await query.answer(
                "🌿 Only the Caretaker can do this.",
                show_alert=True
            )
            return

        parts = data.split("|")
        if len(parts) == 3:
            _, account_email, game_name = parts

            # ✅ Only NOW mark as unavailable
            await _sb_patch(
                f"steamCredentials?email=eq.{account_email}",
                {"status": "Unavailable"}
            )

            try:
                await query.message.edit_text(
                    query.message.text +
                    "\n\n🗑 <b>Marked Unavailable by Caretaker.</b>",
                    parse_mode="HTML",
                    reply_markup=None
                )
            except Exception:
                pass

            await query.answer(
                "🗑 Account marked Unavailable.",
                show_alert=False
            )

    # ── TEMPORARILY DISABLE STEAM FOR REGULAR USERS (OWNER still has full access) ──
    elif (data == "steam_do_search" or data.startswith("vamt_filter_steam")) and chat_id != OWNER_ID:
        asyncio.create_task(send_temporary_message(
            chat_id, "🌿 This feature is not available right now.", duration=4
        ))
        return
        
    elif data == "steam_do_search":
            attempts_left = 3 - int(await redis_client.get(f"steam_search_attempts:{chat_id}") or 0)
            
            await redis_client.setex(f"steam_searching:{chat_id}", 300, "1")

            guide = (
                "🔍 <b>Search for a Steam Game</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"🎯 <b>Attempts:</b> {attempts_left}/3 remaining\n"
                f"{make_attempts_bar(3 - attempts_left)}\n\n" 
                "📌 <b>Tips for better results:</b>\n"
                "• Use the exact game title\n"
                "• Short names work too (e.g. <code>batman</code>)\n"
                "• Partial names are supported\n\n"
                "⏰ <b>You have 5 minutes</b> to type the game name\n"
                "📌 Results expire in <b>10 minutes</b> after search\n"
                "⚠️ Expired without claiming = attempt used\n\n"
                "✏️ <b>Type the game name now:</b> 🍃"
            )

            if query and query.message:
                try:
                    await query.message.edit_caption(caption=guide, parse_mode="HTML")
                    await redis_client.setex(f"steam_search_prompt:{chat_id}", 300, str(query.message.message_id))
                except Exception:
                    try:
                        await query.message.edit_text(text=guide, parse_mode="HTML")
                        await redis_client.setex(f"steam_search_prompt:{chat_id}", 300, str(query.message.message_id))
                    except Exception:
                        pass
            else:
                await tg_app.bot.send_message(chat_id, guide, parse_mode="HTML")
            return
    
    # ── BACK TO RESULTS (after opening bundle)
    elif data.startswith("steam_back_to_results|"):
        try:
            _, group_key = data.split("|", 1)
            await show_steam_account_selection(chat_id, group_key, "", query)
        except:
            await query.answer("❌ Result expired", show_alert=True)
        return

    elif data == "steam_back_to_results":
        await query.answer("🔄 Returning to results...", show_alert=False)
        cached = await redis_client.get(f"steam_search_result:{chat_id}")
        if cached:
            await handle_searchsteam_command(chat_id, "", query=query)
        else:
            await query.answer("⏳ Search expired. Please search again.", show_alert=True)
        return

    elif data == "search_different_game":
            current_attempts = int(await redis_client.get(f"steam_search_attempts:{chat_id}") or 0)
            if current_attempts >= 3:
                await query.answer("🚫 No search attempts remaining!", show_alert=True)
                await handle_steam_landing(chat_id, first_name, query)
                return

            attempts_left = 3 - current_attempts

            # Delete old result — won't charge an attempt
            await redis_client.delete(f"steam_search_result:{chat_id}")

            # Set searching state so next text message is intercepted
            await redis_client.setex(f"steam_searching:{chat_id}", 300, "1")

            await query.answer("🔍 Ready for a new search", show_alert=False)

            guide = (
                "🔍 <b>Search for a Steam Game</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"🎯 <b>Attempts:</b> {attempts_left}/3 remaining\n"
                f"{make_attempts_bar(3 - attempts_left)}\n\n" 
                "📌 <b>Tips for better results:</b>\n"
                "• Use the exact game title\n"
                "• Short names work too (e.g. <code>batman</code>)\n"
                "• Partial names are supported\n\n"
                "⏰ <b>You have 5 minutes</b> to type the game name\n"
                "📌 Results expire in <b>10 minutes</b> after search\n"
                "⚠️ Expired without claiming = attempt used\n\n"
                "✏️ <b>Type the game name now:</b> 🍃"
            )

            if query and query.message:
                try:
                    await query.message.edit_caption(caption=guide, parse_mode="HTML")
                    await redis_client.setex(f"steam_search_prompt:{chat_id}", 300, str(query.message.message_id))
                except Exception:
                    try:
                        await query.message.edit_text(text=guide, parse_mode="HTML")
                        await redis_client.setex(f"steam_search_prompt:{chat_id}", 300, str(query.message.message_id))

                    except Exception:
                        await tg_app.bot.send_message(chat_id, guide, parse_mode="HTML")
            else:
                await tg_app.bot.send_message(chat_id, guide, parse_mode="HTML")
            return

    elif data.startswith("claim_steam|"):
        parts = data.split("|")
        account_email = parts[1]
        group_key = parts[2] if len(parts) > 2 else None

        # ── ATOMIC LOCK: prevent double-tap duplicates ──
        lock_key = f"claim_lock:{chat_id}:{account_email}"
        acquired = await redis_client.set(lock_key, 1, ex=30, nx=True)
        if not acquired:
            await query.answer("⏳ Already processing your claim...", show_alert=True)
            return

        # === NEW: Get the exact display name user saw in search ===
        display_name = await get_display_name_for_claim(
            chat_id=chat_id,
            group_key=group_key,
            fallback_email=account_email
        )

        # ── CHECK 10-MINUTE EXPIRATION ──
        cached_result = await redis_client.get(f"steam_search_result:{chat_id}")
        if not cached_result:
            current = int(await redis_client.get(f"steam_search_attempts:{chat_id}") or 0)
            await redis_client.setex(f"steam_search_attempts:{chat_id}", 86400, str(current + 1))
            remaining = 3 - (current + 1)
            expired_text = (
                f"⏳ <b>This search has expired.</b>\n\n"
                f"The results are no longer valid.\n"
                f"(10 minutes have passed without claiming)\n\n"
                f"🎯 <b>Search Attempts:</b> {remaining}/3 remaining\n"
                f"{make_attempts_bar(remaining)}\n\n"
                f"🌲 You can search again right now!"
            )
            buttons = [
                [InlineKeyboardButton("🔄 Search Again", callback_data="search_different_game")],
                [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")]
            ]
            await tg_app.bot.send_message(
                chat_id, expired_text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            return

        await query.answer("🔑 Claiming account...", show_alert=False)

        profile = await get_user_profile(chat_id)
        level = profile.get("level", 1)
        first_name = profile.get("first_name", "Wanderer")

        acc_data = await _sb_get(
            "steamCredentials", 
            **{"email": f"eq.{account_email}", "status": "eq.Available",
               "select": "game_name,image_url,password,steam_id,release_type,games"}
        ) or []
        
        if not acc_data:
            await query.answer("❌ Account no longer available!", show_alert=True)
            return

        acc = acc_data[0]
        password = acc.get("password", "")
        steam_id = acc.get("steam_id", "")
        account_image_url = acc.get("image_url")
        games_list = acc.get("games") or []

        logo_url = await get_game_logo_url(
            game_name=acc.get("game_name"),
            games_list=games_list,
            preferred_name=display_name
        )
        
        final_image_url = logo_url or account_image_url

        # Use the nice display name we got from search
        success = await claim_steam_account(chat_id, first_name, account_email, display_name)

        if success:
            current_attempts = int(await redis_client.get(f"steam_search_attempts:{chat_id}") or 0)
            await redis_client.setex(f"steam_search_attempts:{chat_id}", 86400, str(current_attempts + 1))

            cooldown_seconds = get_steam_cooldown_hours(level) * 3600
            await redis_client.setex(f"steam_claim_cd:{chat_id}", cooldown_seconds, "1")

            await redis_client.delete(f"steam_search_result:{chat_id}")
            await redis_client.delete(f"steam_searching:{chat_id}")

            hours_left = cooldown_seconds // 3600
            mins_left = (cooldown_seconds % 3600) // 60

            await tg_app.bot.send_message(
                chat_id,
                f"✅ <b>Claim successful!</b>\n\n"
                f"⏳ Next claim available in <b>{hours_left}h {mins_left}m</b>\n"
                f"(Level {level} cooldown)",
                parse_mode="HTML"
            )

            # === FINAL SUCCESS MESSAGE WITH CORRECT NAME ===
            caption = (
                f"🎮 <b>{html.escape(display_name)} — Successfully Claimed!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"📧 <b>Login Email:</b>\n"
                f"<tg-spoiler>{html.escape(account_email)}</tg-spoiler>\n\n"
                f"🔑 <b>Password:</b>\n"
                f"<tg-spoiler>{html.escape(password)}</tg-spoiler>\n\n"
            )
            if steam_id:
                caption += (
                    f"🆔 <b>Steam ID:</b>\n"
                    f"<tg-spoiler><code>{steam_id}</code></tg-spoiler>\n\n"
                )
            caption += (
                f"<blockquote expandable=\"true\">"
                f"⚠️ <b>Important Notice</b>\n"
                f"<i>Steam may show a warning on first login — this is completely normal.</i>\n\n"
                f"• Do not change the password or email\n"
                f"• Do not enable Steam Guard / 2FA on this account\n"
                f"• Use it within the next 48 hours for best results"
                f"</blockquote>\n\n"
                f"🕐 <b>We'll check back in 2 hours</b> to see if it worked. 🍃\n\n"
                f"🌲 <i>Enjoy your game, wanderer!</i>"
            )

            try:
                await query.message.delete()
            except Exception:
                pass

            if final_image_url:
                try:
                    await tg_app.bot.send_photo(chat_id=chat_id, photo=final_image_url, caption=caption, parse_mode="HTML")
                except:
                    await tg_app.bot.send_message(chat_id, caption, parse_mode="HTML")
            else:
                await tg_app.bot.send_message(chat_id, caption, parse_mode="HTML")

            asyncio.create_task(send_delayed_feedback_buttons(chat_id, account_email, display_name, delay=7200))

            action_xp, _ = await add_xp(chat_id, first_name, "steam_claim", xp_override=20)
            if action_xp:
                asyncio.create_task(send_xp_feedback(chat_id, action_xp))

        else:
            await query.answer("🌿 You already claimed this Steam account!", show_alert=False)

    # ── INVENTORY FILTERS ──
    elif data.startswith("vamt_filter_"):
        category = data.replace("vamt_filter_", "").lower()
        action_map = {
            "win": "view_windows", "windows": "view_windows",
            "office": "view_office", "netflix": "view_netflix", "prime": "view_prime",
            "crunchyroll": "view_crunchyroll",
        }

        # Only award XP immediately for netflix/prime/steam — win/office handles it later
        if category not in ("win", "windows", "office"):
            action = action_map.get(category)
            if action:
                action_xp, _ = await add_xp(chat_id, first_name, action)
                if action_xp:
                    asyncio.create_task(send_xp_feedback(chat_id, action_xp))

        await query.message.edit_caption(
            caption=f"✨ <i>Searching the glade for {category.upper()}...</i>", parse_mode="HTML"
        )

        # Steam - NEW SEARCH SYSTEM
        if category == "steam":
            await handle_steam_landing(chat_id, first_name, query)
            return

        # Cookie types
        if category in ("netflix", "prime", "crunchyroll"): 
            # Always delete current message first — it might be a document (.txt file)
            # which cannot have its caption edited, causing the GIF list to disappear
            try:
                await query.message.delete()
            except Exception:
                pass
            loading = await send_animated_translated(
                chat_id=chat_id,
                animation_url=INVENTORY_GIF,
                caption=f"{'🍿' if category == 'netflix' else '🎥'} <i>Loading {category.title()} Cookies...</i>",
            )
            class _FreshQuery:
                message = loading
            await show_paginated_cookie_list(category, chat_id, _FreshQuery(), page=0)
            return
        
        # ── NEW: First-time guide for Windows / Office ──────────────────
        if category in ("win", "windows", "office"):
                seen = await has_seen_winoffice_guide(chat_id)

                if not seen:
                    # First time → show guide ONLY (no cap deduction yet)
                    await redis_client.setex(f"winoffice_pending_cat:{chat_id}", 3600, category)
                    cat_label = "Windows" if category in ("win", "windows") else "Office"
                    cat_emoji = "🪟" if category in ("win", "windows") else "📑"
            
                    await query.message.edit_caption(
                        caption=(
                            f"{cat_emoji} <b>Before you open the {cat_label} scrolls...</b>\n\n"
                            "━━━━━━━━━━━━━━━━━━\n\n"
                            "🔵 <b>What is a VAMT Key?</b>\n"
                            "It is a <b>Volume Activation</b> key. These are official Microsoft keys designed to activate multiple PCs.\n\n"
                            "━━━━━━━━━━━━━━━━━━\n\n"
                            "📦 <b>What does \"Remaining\" mean?</b>\n"
                            "Because these keys are shared, they have a strict activation limit.\n\n"
                            "• <b>Remaining: 5</b> = Works on exactly 5 more devices.\n"
                            "• <b>Remaining: 0</b> = The key is fully exhausted.\n\n"
                            "<i>Apply them swiftly! The highest remaining keys vanish fast.</i> 🍃\n\n"
                            "━━━━━━━━━━━━━━━━━━"
                        ),
                        parse_mode="HTML",
                        reply_markup=kb_winoffice_guide(),
                    )
                    return
                
                # Normal flow (guide already seen) → now consume cap
                allowed, remaining = await try_consume_view_cap(chat_id, category)
                if not allowed:
                    cat_label = "Windows" if category in ("win", "windows") else "Office"
                    cat_emoji = "🪟" if category in ("win", "windows") else "📑"
                    max_daily = get_max_daily_views(profile.get("level", 1), category)
                    
                    await query.message.edit_caption(
                        caption=(
                            f"{cat_emoji} <b>{cat_label} Daily Limit Reached</b>\n\n"
                            f"🌿 You have already viewed your maximum <b>{max_daily}</b> keys today.\n\n"
                            f"Come back tomorrow after midnight (Manila time) for fresh keys 🍃\n\n"
                            f"<i>Remaining views today: <b>{remaining}</b></i>"
                        ),
                        parse_mode="HTML",
                        reply_markup=kb_back_inventory(),
                    )
                    await query.answer("Daily view limit reached", show_alert=False)
                    return
                
                # Award XP now that we're actually showing the keys
                action_xp, _ = await add_xp(chat_id, first_name,
                    "view_windows" if category in ("win","windows") else "view_office")
                if action_xp:
                    asyncio.create_task(send_xp_feedback(chat_id, action_xp))

                await show_winoffice_keys(chat_id, category, profile, query, first_name)
                return
        

    elif data.startswith("key_feedback_ok|") or data.startswith("key_feedback_bad|"):
        parts = data.split("|")
        if len(parts) == 3:
            _, key_id, service_type = parts
            is_working = data.startswith("key_feedback_ok|")
            await handle_key_feedback(chat_id, first_name, key_id, service_type, is_working, query)

    # Netflix/Prime reveal feedback (uses idx + Redis to avoid 64-byte limit)
    elif data.startswith("kfb_ok|") or data.startswith("kfb_bad|"):
        parts = data.split("|")
        if len(parts) == 3:
            _, service_type, idx_str = parts
            is_working = data.startswith("kfb_ok|")
            real_key = await redis_client.get(f"reveal_key:{chat_id}:{service_type}:{idx_str}")
            key_id = real_key if real_key else idx_str
            await handle_key_feedback(chat_id, first_name, key_id, service_type, is_working, query)

    # Windows/Office per-item feedback (uses token + Redis)
    elif data.startswith("wkfb_ok|") or data.startswith("wkfb_bad|"):
        parts = data.split("|", 1)
        if len(parts) == 2:
            _, token = parts
            is_working = data.startswith("wkfb_ok|")
            stored = await redis_client.get(f"winkey:{token}")
            if stored:
                real_key, svc = stored.split("||", 1)
            else:
                real_key, svc = token, "windows"
            await handle_key_feedback(chat_id, first_name, real_key, svc, is_working, query)

    # ── HISTORY PAGINATION ──
    elif data.startswith("history_page_"):
        try:
            page = int(data.split("_")[2])
            await query.message.delete()
        except Exception:
            page = 0
        await handle_history(chat_id, first_name, page=page)

    # ── GUIDANCE (3 pages) ──
    elif data == "help" or data.startswith("guidance_page_"):
        if data == "help":
            try:
                await query.message.delete()
            except Exception:
                pass
            action_xp, _ = await add_xp(chat_id, first_name, "guidance")
            if action_xp > 0:
                asyncio.create_task(send_xp_feedback(chat_id, action_xp))

        page = 1
        if data.startswith("guidance_page_"):
            try:
                page = int(data.split("_")[2])
            except Exception:
                page = 1

        level_req_text = "\n".join(
            f"• Level {lvl} → {get_cumulative_xp_for_level(lvl):,} XP"
            for lvl in range(2, 11)
        )

        pages = {
            1: (
                "<b>❓ Guidance - Page 1/3</b>\n\n"
                "🌿 <b>How to Navigate the Clearing</b>\n"
                "• Tap any button to explore the paths\n"
                "• Use /menu to return here anytime\n"
                "• Use /clear to renew your path\n\n"
                "📜 <b>Available Commands</b>\n"
                "• /start — Begin your journey\n"
                "• /menu — Return to the Clearing\n"
                "• /profile — View your Forest Profile\n"
                "• /mystats — Detailed statistics\n"
                "• /leaderboard — See Top Wanderers\n"
                "• /myid — Reveal your Forest ID\n"
                "• /clear — Cleanse the clearing\n"
                "• /feedback — Message the caretaker\n"
                "• /update — View patch notes\n\n"
                "🌲 <b>Treasures You Can Discover</b>\n"
                "• 🪄 Spirit Treasures — Steam accounts\n"
                "• 📜 Ancient Scrolls — Learning guides\n"
                "• 🌿 Forest Inventory — Keys & Cookies\n\n"
                "<i>Tap Next → to learn about the Leveling System</i>",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("Next ⇀", callback_data="guidance_page_2")],
                    [InlineKeyboardButton("⬅️ Back to Clearing", callback_data="main_menu")],
                ]),
            ),
            2: (
                "<b>❓ Guidance - Page 2/3</b>\n\n"
                "✨ <b>Forest Leveling System</b>\n\n"
                f"🪟 <b>Windows & Office Keys</b>\n"
                f"• {_guidance_tier_text('windows')}\n\n"
                f"🍿 <b>Netflix Premium Cookies</b>\n"
                f"• {_guidance_tier_text('netflix')}\n\n"
                f"🎥 <b>PrimeVideo Premium Cookies</b>\n"
                f"• {_guidance_tier_text('prime')}\n\n"
                "<i>Tap Next → for Steam & XP Rewards</i>",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("↼ Previous", callback_data="guidance_page_1"),
                        InlineKeyboardButton("Next ⇀",     callback_data="guidance_page_3"),
                    ],
                    [InlineKeyboardButton("⬅️ Back to Clearing", callback_data="main_menu")],
                ]),
            ),
            3: (
                f"<b>❓ Guidance - Page 3/3</b>\n\n"
                "🎮 <b>Steam Accounts</b>\n"
                "• Lv1-6: Public Drop Only (Website)\n"
                "• Lv7-8: Early Preview\n"
                "• Lv9: Early Preview + Sunday Double\n"
                "• Lv10+: 👑 Legend Tier\n\n"
                "<b>XP Rewards:</b>\n"
                "• Viewing any list → <b>+8 XP</b>\n"
                "• Revealing a cookie → <b>+14 XP</b>\n"
                "• /profile or /clear → <b>+6 XP</b>\n"
                "• First Guidance / Lore → <b>+10 XP</b>\n"
                "• Returning Guidance / Lore → <b>+2 XP</b>\n\n"
                "🌅 <b>Daily Login Streak Bonus:</b>\n"
                "• Day 1 → <b>+10 XP</b>\n"
                "• Day 2 → <b>+12 XP</b>\n"
                "• Day 3-6 → <b>+20 XP</b>\n"
                "• Day 7-13 → <b>+30 XP</b>\n"
                "• Day 14-29 → <b>+40 XP</b>\n"
                "• Day 30-59 → <b>+50 XP</b>\n"
                "• Day 60+ → <b>+60 XP</b>\n\n"
                f"<b>Cumulative XP Requirements:</b>\n\n{level_req_text}\n\n"
                "<i>The more you explore, the more the forest opens up to you.</i> 🍃✨",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("↼ Previous", callback_data="guidance_page_2")],
                    [InlineKeyboardButton("⬅️ Back to Clearing", callback_data="main_menu")],
                ]),
            ),
        }

        text, keyboard = pages.get(page, pages[1])

        if data == "help":
            msg = await send_animated_translated(
                chat_id=chat_id,
                animation_url=GUIDANCE_GIF,
                caption=text,
                reply_markup=keyboard,
            )
            await _remember(chat_id, msg.message_id)
            if not profile.get("has_seen_menu", False):
                asyncio.create_task(update_has_seen_menu(chat_id))
        else:
            try:
                await query.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=keyboard)
            except Exception:
                msg = await send_animated_translated(
                    chat_id=chat_id,
                    animation_url=GUIDANCE_GIF,
                    caption=text,
                    reply_markup=keyboard,
                )
                await _remember(chat_id, msg.message_id)

    # ── COOKIE PAGINATION ──
    elif "_page_" in data and data.split("_page_")[0] in ("netflix", "prime", "crunchyroll"):
        try:
            service_type = data.split("_page_")[0]
            new_page     = int(data.split("_page_")[1])
        except Exception:
            return
        loading = await send_animated_translated(
            chat_id=chat_id,
            animation_url=INVENTORY_GIF,
            caption=f"{'🍿' if service_type == 'netflix' else '🎥'} <i>Loading {service_type.title()}...</i>",
        )
        class _FQ:
            message = loading
        await show_paginated_cookie_list(service_type, chat_id, _FQ(), page=new_page)
        try:
            await query.message.delete()
        except Exception:
            pass

    elif data.startswith("ach_page_"):
        try:
            page = int(data.split("_")[2])
            await show_achievements_page(chat_id, query, page=page)
        except Exception:
            await show_achievements_page(chat_id, query, page=0)

    # ── REVEAL COOKIE ──
    elif data.startswith("reveal_netflix|") or data.startswith("reveal_prime|") or data.startswith("reveal_crunchyroll|"):
        try:
            parts = data.split("|")
            if parts[0] == "reveal_netflix":
                service_type = "netflix"
            elif parts[0] == "reveal_prime":
                service_type = "prime"
            else:
                service_type = "crunchyroll"
            idx  = int(parts[1])
            page = int(parts[2]) if len(parts) > 2 else 0
        except Exception:
            await query.answer("Invalid selection", show_alert=True)
            return
        await reveal_cookie(service_type, chat_id, first_name, query, idx, page)

    # ── BACK TO COOKIE LIST (with cleanup) ──
    elif (data.startswith("back_to_netflix_list") or 
          data.startswith("back_to_prime_list") or 
          data.startswith("back_to_crunchyroll_list")):
        
        if data.startswith("back_to_netflix_list"):
            service_type = "netflix"
        elif data.startswith("back_to_prime_list"):
            service_type = "prime"
        else:
            service_type = "crunchyroll"

        try:
            page = int(data.split("|")[1]) if "|" in data else 0
        except Exception:
            page = 0

        stored_msg_id = await redis_client.get(f"reveal_msg:{chat_id}:{service_type}")
        if stored_msg_id:
            try:
                await tg_app.bot.delete_message(chat_id, int(stored_msg_id))
            except Exception:
                pass
            await redis_client.delete(f"reveal_msg:{chat_id}:{service_type}")

        emoji_map = {"netflix": "🍿", "prime": "🎥", "crunchyroll": "🍜"}
        loading = await send_animated_translated(
            chat_id=chat_id,
            animation_url=INVENTORY_GIF,
            caption=f"{emoji_map[service_type]} <i>Loading {service_type.title()} Cookies...</i>",
        )
        class FakeQuery:
            message = loading
        await show_paginated_cookie_list(service_type, chat_id, FakeQuery(), page=page)

    # ── LORE / ABOUT ──
    elif data == "about":
        try:
            await query.message.delete()
        except Exception:
            pass
        action_xp, _ = await add_xp(chat_id, first_name, "lore")
        if action_xp > 0:
            asyncio.create_task(send_xp_feedback(chat_id, action_xp))

        loading = await send_animated_translated(
            chat_id=chat_id,
            animation_url=LOADING_GIF,
            caption="🌌 <i>The oldest spirits of the forest begin to stir...</i>",
        )
        await asyncio.sleep(1.2)
        await safe_edit(loading,"📜 <i>They gather beneath the ancient canopy...</i>")
        await asyncio.sleep(1.3)
        await safe_edit(loading,"✨ <i>The story of this sacred clearing gently unfolds...</i>")
        await asyncio.sleep(1.0)

        lore = (
            "<b>🌿 About Clyde's Resource Hub</b>\n\n"
            "This peaceful sanctuary was created to make useful digital resources easy and "
            "stress-free to access — all wrapped in a calm, Studio Ghibli-inspired forest theme.\n\n"
            "You can find:\n"
            "• Windows & Office activation keys\n"
            "• Netflix premium cookies\n"
            "• PrimeVideo premium cookies\n"
            "• Steam accounts\n"
            "• Learning guides\n\n"
            "<b>Current Rewards:</b> View lists = +8 XP | Reveal Netflix = +14 XP\n\n"
            "<i>May this small enchanted clearing bring you both practical resources and a moment of peace.</i> 🍃✨"
        )
        final = await send_animated_translated(
            chat_id=chat_id,
            animation_url=ABOUT_GIF,
            caption=lore,
            reply_markup=kb_back(),
        )
        try:
            await tg_app.bot.delete_message(loading.chat_id, loading.message_id)
        except Exception:
            pass
        await _remember(chat_id, final.message_id)

    # ── MAINTENANCE CONFIRMATION ──
    elif data in ("confirm_toggle_maintenance", "yes_toggle_maintenance", "cancel_toggle_maintenance"):
        if chat_id != OWNER_ID:
            await query.answer("🌿 Only the Forest Caretaker may do this.", show_alert=True)
            return
        if data == "confirm_toggle_maintenance":
            await handle_confirm_toggle_maintenance(chat_id)
        elif data == "yes_toggle_maintenance":
            await handle_toggle_maintenance(chat_id)
            await query.message.edit_text("✅ <b>Maintenance mode updated.</b> 🌿", parse_mode="HTML")
        elif data == "cancel_toggle_maintenance":
            await query.message.edit_text("❌ Maintenance mode change cancelled.")
    
    # ── END EVENT CONFIRMATION ──
    elif data in ("confirm_end_event", "yes_end_event", "cancel_end_event"):
        if chat_id != OWNER_ID:
            await query.answer("🌿 Only the Forest Caretaker may do this.", show_alert=True)
            return
        if data == "confirm_end_event":
            await handle_confirm_end_event(chat_id)
        elif data == "yes_end_event":
            await handle_end_event(chat_id)
            await query.message.edit_text("✅ <b>Event ended successfully.</b> 🌿", parse_mode="HTML")
        elif data == "cancel_end_event":
            await query.message.edit_text("❌ Event ending cancelled.")

    # ── CARETAKER ADMIN ──
    elif data.startswith("caretaker_"):
        if chat_id != OWNER_ID:
            await query.answer("🌿 Only the Forest Caretaker may enter this sacred glade.", show_alert=True)
            return

        if data == "caretaker_addupdate":
            await send_animated_translated(
                chat_id=chat_id,
                animation_url=ABOUT_GIF,
                caption=(
                    "📜 <b>Add New Patch Note</b>\n━━━━━━━━━━━━━━━━━━\n"
                    "Reply with:\n\n"
                    "<code>/addupdate\nYour Title Here\nYour full description here...</code>"
                ),
            )

        elif data == "caretaker_manualkeys":
            await send_animated_translated(
                chat_id=chat_id,
                animation_url=CARETAKER_GIF,
                caption=(
                    "🗝️ <b>Manual Win / Office Key Entry</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    "<b>Basic:</b>\n"
                    "<code>/uploadwinoffice\n"
                    "windows\n"
                    "XXXXX-XXXXX-XXXXX-XXXXX-XXXXX</code>\n\n"
                    "<b>With custom display name:</b>\n"
                    "<code>/uploadwinoffice\n"
                    "windows|Windows 11 Pro\n"
                    "XXXXX-XXXXX-XXXXX-XXXXX-XXXXX</code>\n\n"
                    "<b>With remaining count:</b>\n"
                    "<code>/uploadwinoffice\n"
                    "office|Office 2021 Pro\n"
                    "XXXXX-XXXXX-XXXXX-XXXXX|50\n"
                    "YYYYY-YYYYY-YYYYY-YYYYY|30</code>\n\n"
                    "<b>Mixed blocks (separate with blank line):</b>\n"
                    "<code>/uploadwinoffice\n"
                    "windows|Windows 10 LTSC\n"
                    "XXXXX-XXXXX-XXXXX-XXXXX|50\n\n"
                    "office|Office 2019\n"
                    "ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ|25</code>\n\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "• <code>windows</code> or <code>office</code> = service type\n"
                    "• <code>|Custom Name</code> = optional display name\n"
                    "• <code>KEY|50</code> = key with remaining count\n"
                    "<i>Close this and type the command above.</i> 🍃"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back to Upload Keys", callback_data="cpage_uploadkeys")],
                    [InlineKeyboardButton("⬅️ Back to Caretaker", callback_data="caretaker_home")],
                ])
            )

        elif data == "caretaker_addevent":
            await tg_app.bot.send_message(
                chat_id,
                "🎉 <b>Create New Event</b>\n━━━━━━━━━━━━━━━━━━\n\n"
                "Reply with:\n\n"
                "<code>/addevent\nEvent Title\nApril 12, 2026\nYour description here...\n"
                "Can span multiple lines!\nbonus:netflix_double</code>\n\n"
                "• Line 1 = Title\n"
                "• Line 2 = Date\n"
                "• Line 3+ = Description (any length)\n"
                "• Last line = <code>bonus:netflix_double</code> <i>(optional)</i>\n\n"
                "🎁 <b>Available bonus types:</b>\n"
                "• <code>bonus:netflix_double</code> — doubles Netflix slots\n"
                "• <code>bonus:netflix_max</code> — maximizes Netflix slots\n"
                "• <i>Omit the bonus line for a normal event</i>\n\n"
                "<i>This will replace any currently active event.</i>",
                parse_mode="HTML",
            )
        elif data == "caretaker_uploadkeys":
            await handle_uploadkeys_command(chat_id)
        elif data == "caretaker_health":
            await handle_status(chat_id)
        elif data == "caretaker_checkstock":
            await redis_client.delete("low_stock_alerted")
            await tg_app.bot.send_message(chat_id, "🔍 <i>Checking stock levels now...</i>", parse_mode="HTML")
            # reuse same logic — trigger via fake command
            data_items = await get_vamt_data() or []
            counts: dict[str, int] = {}
            SERVICE_EMOJIS = {"netflix":"🍿","prime":"🎥","windows":"🪟","office":"📑","steam":"🎮"}
            for item in data_items:
                svc = str(item.get("service_type","")).lower().strip()
                if str(item.get("status","")).lower() == "active" and int(item.get("remaining",0)) > 0:
                    counts[svc] = counts.get(svc, 0) + 1
            steam_data = await _sb_get("steamCredentials", **{"select":"status","status":"eq.Available"}) or []
            counts["steam"] = len(steam_data)
            lines = []
            for svc, count in sorted(counts.items()):
                emoji = SERVICE_EMOJIS.get(svc, "📦")
                status = "⚠️" if count <= 5 else "✅"
                lines.append(f"{status} {emoji} <b>{svc.title()}</b>: <b>{count}</b> available")
            await tg_app.bot.send_message(
                chat_id,
                "📊 <b>Current Stock Levels</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                + "\n".join(lines)
                + "\n\n<i>⚠️ = at or below threshold (5)</i>",
                parse_mode="HTML"
            )
        elif data == "caretaker_viewreports":
            await handle_view_reports(chat_id)
        elif data == "caretaker_setinfo":
            await tg_app.bot.send_message(
                chat_id,
                "📝 <b>Set Forest Info</b>\n\n"
                "Reply with the command in this format:\n\n"
                "<code>/setforestinfo 1.4.5 April 14, 2026 · 02:58 PM</code>",
                parse_mode="HTML",
            )
        elif data == "caretaker_viewevent":
            await handle_view_event(chat_id)
        elif data == "caretaker_viewfeedback":
            await handle_view_feedback(chat_id)
        elif data == "caretaker_flushcache":
            await handle_flushcache(chat_id)   
        elif data == "caretaker_resetfirst":
            await handle_reset_first_time(chat_id)
        elif data == "caretaker_uploadsteam":
            await send_animated_translated(
                chat_id,
                "🎮 <b>Steam Account Uploader</b>\n\n"
                "<b>Single account:</b>\n"
                "<code>/uploadsteam\n"
                "email\n"
                "password\n"
                "Game Name <b>(optional)</b>\n"
                "SteamID64 <b>(optional)</b>\n"
                "Banner (optional)</code>\n\n"
                "<b>Bulk (separate accounts with a blank line):</b>\n"
                "<code>/uploadsteam\n"
                "email1\n"
                "password1\n"
                "Game Name\n"
                "76561198XXXXXXXXX\n"
                "Banner.jpg\n\n"
                "email2\n"
                "password2\n\n"
                "email3\n"
                "password3\n"
                "Only Game Name</code>\n\n"
                "⚠️ <i>Minimum required: email + password</i>\n"
                "<i>Tip: Use /searchsteam first to check for duplicates.</i>",
                animation_url=STEAM_GIF,
            )
        elif data == "caretaker_searchsteam":
            await handle_searchsteam_command(chat_id, "/searchsteam")

    elif data == "winoffice_got_it":
        await mark_winoffice_guide_seen(chat_id)
        pending_cat = await redis_client.get(f"winoffice_pending_cat:{chat_id}") or "win"
        await redis_client.delete(f"winoffice_pending_cat:{chat_id}")
        await show_winoffice_keys(chat_id, pending_cat, profile, query, first_name)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN UPDATE PROCESSOR
# ══════════════════════════════════════════════════════════════════════════════
@safe_handler("process_update")
async def process_update(update_data: dict):
    update = Update.de_json(update_data, tg_app.bot)

    # ── Improved Cold Start + Auto-delete after 5 seconds
    if update.message:
        chat_id = update.effective_chat.id

        # Per-user cold start — each user gets notified once per restart
        cold_key = f"cold_start:{chat_id}"
        global_cold_key = "bot_just_restarted"

        # Only show if bot recently restarted (global flag in Redis)
        just_restarted = await redis_client.get(global_cold_key)

        if just_restarted and not await redis_client.get(cold_key):
            await redis_client.setex(cold_key, 300, "1")  # per-user, 5 min
            asyncio.create_task(_send_cold_start_notice(chat_id))

    # ── ONBOARDING GUARD ──
    if update.message and update.message.text:
        raw_check = update.message.text.strip()
        check_chat_id = update.effective_chat.id
        
        # Only /start is allowed during onboarding
        if not raw_check.lower().startswith("/start"):
            onboarding_done = await has_completed_onboarding(check_chat_id)
            if not onboarding_done:
                profile_check = await get_user_profile(check_chat_id)
                if not profile_check:  # truly new user
                    await tg_app.bot.send_message(
                        check_chat_id,
                        "🌿 <b>Please complete the welcome tour first!</b>\n\n"
                        "Use /start to begin your journey into the Enchanted Clearing. 🍃",
                        parse_mode="HTML"
                    )
                    return

    # ── Maintenance mode ──
    if await get_maintenance_mode():
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id and chat_id != OWNER_ID:
            try:
                if update.message:
                    await tg_app.bot.send_message(chat_id, MAINTENANCE_MESSAGE, parse_mode="HTML")
                elif update.callback_query:
                    await update.callback_query.answer(
                        "🌿 The Enchanted Clearing is under maintenance.\nPlease come back later!",
                        show_alert=True,
                    )
            except Exception:
                pass
            return

    # ── Callback queries ──
    if update.callback_query:
        await handle_callback(update)
        return

    # ── DOCUMENT HANDLER (Admin TXT key upload) ──
    if update.message and (
        update.message.document or
        update.message.photo or
        update.message.animation
    ):
        await handle_document(update)
        return

    # ── Text messages ──
    if not (update.message and update.message.text):
        return

    raw       = update.message.text.strip()
    text      = raw.lower()
    chat_id   = update.effective_chat.id
    msg_id    = update.message.message_id
    first_name = update.effective_user.first_name if update.effective_user else "Traveler"

    if not text.startswith("/clear"):
        await _remember(chat_id, msg_id)

    # Registration guard (except /start)
    if not text.startswith("/start"):
        profile = await get_user_profile(chat_id)
        if not profile:
            await send_animated_translated(
                chat_id=chat_id,
                animation_url=HELLO_GIF,
                caption=(
                    "<b>🌲 You stand at the edge of a mysterious forest.</b>\n\n"
                    "The ancient trees watch you with quiet curiosity.\n\n"
                    "To step into the Enchanted Clearing..."
                ),
                reply_markup=kb_start(),
            )
            return
        
# ── STEAM GAME SEARCH INTERCEPT ──
    is_real_command = any(text.startswith(cmd) for cmd in (
        "/start", "/menu", "/clear", "/profile", "/myid", "/feedback",
        "/leaderboard", "/history", "/streak", "/invite", "/spin", "/update",
        "/updates", "/lang", "/language", "/forest", "/health", "/caretaker",
        "/uploadkeys", "/uploadsteam", "/uploadwinoffice", "/searchsteam",
        "/addupdate", "/addevent", "/setforestinfo", "/flushcache", "/checkstock",
        "/viewreports", "/feedbacks", "/viewfeedback", "/resetfirst", "/reset",
        "/resetguide", "/resetwheel", "/resetonboarding", "/resetprofilegif",
        "/resetsteamclaim", "/testdaily", "/setlogo", "/addpatron", "/settitle",
        "/referrals", "/online", "/test_achievements", "/award_beta",
        "/remove_achievement",
    ))

    # ── FINAL SMART SEARCH RESULT LOGIC (Combined game_name + games[] + Clean Display) ──
    if await redis_client.get(f"steam_searching:{chat_id}"):
        if not is_real_command:
            await redis_client.delete(f"steam_searching:{chat_id}")

            # DELETE the user's typed message
            try:
                await tg_app.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass

            # DELETE the search prompt message (stored in Redis)
            search_prompt_id = await redis_client.get(f"steam_search_prompt:{chat_id}")
            if search_prompt_id:
                try:
                    await tg_app.bot.delete_message(chat_id, int(search_prompt_id))
                except Exception:
                    pass
                await redis_client.delete(f"steam_search_prompt:{chat_id}")

            term = raw.lower().strip()

            attempts = int(await redis_client.get(f"steam_search_attempts:{chat_id}") or 0)
            await redis_client.setex(f"steam_search_attempts:{chat_id}", 86400, str(attempts + 1))
            
            if attempts + 1 >= 3:
                await redis_client.delete(f"steam_searching:{chat_id}")
                await tg_app.bot.send_message(
                    chat_id,
                    "🚫 <b>You have used all 3 search attempts today.</b>\n\n"
                    "Come back tomorrow (Manila midnight) for fresh attempts 🍃",
                    parse_mode="HTML"
                )
                return

            all_accounts = await _sb_get(
                "steamCredentials",
                **{
                    "status": "eq.Available",
                    "select": "email,game_name,image_url,password,steam_id,release_type,games"
                }
            ) or []

            # Get emails this user has already claimed
            user_claimed_data = await _sb_get(
                "steam_claims",
                **{"chat_id": f"eq.{chat_id}", "select": "account_email"}
            ) or []
            user_claimed_emails = {row["account_email"] for row in user_claimed_data}

            # Filter out accounts this user already claimed
            accounts = [acc for acc in all_accounts if acc.get("email") not in user_claimed_emails]

            # Search in BOTH game_name AND games[] array
            matching_accounts = []
            for acc in accounts:
                game_name = str(acc.get("game_name", "")).lower()
                games_list = [str(g).lower() for g in (acc.get("games") or []) if str(g).strip()]
                
                if term in game_name or any(term in g for g in games_list):
                    matching_accounts.append(acc)

            if not matching_accounts:
                remaining_attempts = 3 - (attempts + 1)

                no_result_text = (
                    f"🌫️ <b>No accounts found for</b> \"<b>{html.escape(term)}</b>\"\n\n"
                    f"🎯 <b>Search Attempts:</b> {remaining_attempts}/3 remaining\n"
                    f"{make_attempts_bar(remaining_attempts)}\n\n"
                    f"💡 <b>Tips to get better results:</b>\n"
                    f"• Try the exact or shorter game title\n"
                    f"• Popular games usually have more accounts\n\n"
                    # f"• Partial names work well (e.g. <code>elden</code>, <code>cyber</code>, <code>gta</code>)\n\n"
                    f"🌲 <i>You still have <b>{remaining_attempts}</b> attempt{'' if remaining_attempts == 1 else 's'} left today!</i>"
                )

                buttons = [
                    [InlineKeyboardButton("🔄 Search Again", callback_data="search_different_game")],
                    [InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")]
                ]

                await tg_app.bot.send_message(
                    chat_id,
                    no_result_text,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return

            from collections import defaultdict
            grouped = defaultdict(list)
            for acc in matching_accounts:
                if is_bundle_account(acc):
                    # Smart display: Prefer the actual searched game name
                    games_list = [str(g).strip() for g in (acc.get("games") or []) if str(g).strip()]
                    best_match = next((g for g in games_list if term in g.lower()), None)
                    display_name = best_match or raw.title()
                else:
                    display_name = (acc.get("game_name") or "Steam Account").strip()
                grouped[display_name].append(acc)

            text = f"✅ Found <b>{len(matching_accounts)}</b> account(s) for \"<b>{html.escape(term)}</b>\"\n\n"
            buttons = []

            for display_name, acc_list in grouped.items():
                count = len(acc_list)
                has_bundle = any(is_bundle_account(acc) for acc in acc_list)

                if count == 1 and not has_bundle:
                    acc = acc_list[0]
                    text += f"🎮 <b>{html.escape(display_name)}</b> — 1 account available\n\n"
                    buttons.append([InlineKeyboardButton(
                        f"✅ Claim {html.escape(display_name)}",
                        callback_data=f"claim_steam|{acc['email']}"
                    )])
                else:
                    label = f"🎮 <b>{html.escape(display_name)}</b>"
                    if has_bundle:
                        total_games = len([g for g in (acc_list[0].get("games") or []) if str(g).strip()])
                        label += f" ← <b>Big Bundle ({total_games} games total)</b>"
                        
                        # Smart "Also includes"
                        games_list = acc_list[0].get("games") or []
                        example_games = [g for g in games_list if str(g).strip()][:4]
                        if example_games:
                            examples = ", ".join(example_games[:3])
                            text += label + "\n"
                            text += f"   Also includes: {examples} +{total_games-3} more\n\n"
                        else:
                            text += label + "\n\n"
                    else:
                        label += f" — {count} account{'s' if count > 1 else ''} available"
                        text += label + "\n\n"

                    # Store emails in Redis for show_steam_account_selection
                    emails = [acc.get("email") for acc in acc_list if acc.get("email")]
                    safe_key = abs(hash(f"{chat_id}:{display_name}")) % 999999
                    await redis_client.setex(
                        f"steam_group:{chat_id}:{safe_key}",
                        600,
                        json.dumps({"emails": emails, "game_name": display_name})
                    )
                    buttons.append([InlineKeyboardButton(
                        f"👉 View all {count} account{'s' if count > 1 else ''}",
                        callback_data=f"steam_sel|{chat_id}|{safe_key}"
                    )])

            # ✅ Set steam_search_result ONCE after the loop
            first_acc, first_name_game = next(
                ((a, n) for n, accs in grouped.items() for a in accs[:1]), (None, None)
            )
            if first_acc:
                await redis_client.setex(
                    f"steam_search_result:{chat_id}",
                    600,
                    json.dumps({"email": first_acc.get("email", ""), "game_name": first_name_game})
                )

            buttons.append([InlineKeyboardButton("🔄 Search Different Game", callback_data="search_different_game")])
            buttons.append([InlineKeyboardButton("⬅️ Back to Inventory", callback_data="check_vamt")])

            await tg_app.bot.send_message(
                chat_id=chat_id,
                text=text.strip(),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            return
        
    # ── Command dispatch ──
    if text.startswith("/start"):
        # ── Fetch profile first so we know if user is new or existing
        profile = await get_user_profile(chat_id)
        is_new_user = profile is None

        referral_stored = True  # default
        if " " in raw:
            payload = raw.split(maxsplit=1)[1]
            if payload.startswith("ref_"):
                try:
                    referrer_id = int(payload[4:])
                    referral_stored = await store_pending_referral(chat_id, referrer_id)

                    if not referral_stored:
                        # Self-referral
                        asyncio.create_task(
                            send_temporary_message(
                                chat_id,
                                "🌿 Nice try, wanderer!\n\n"
                                "You can't refer yourself in the forest 🍃\n"
                                "The trees are watching 👀",
                                duration=8
                            )
                        )
                    elif is_new_user:
                        # New user who was referred
                        asyncio.create_task(
                            send_temporary_message(
                                chat_id,
                                "✨ <b>Someone invited you!</b>\n\nYou'll receive <b>+40 XP</b> on your first daily bonus 🌱",
                                duration=8
                            )
                        )
                    else:
                        # ← NEW: Already claimed referral
                        asyncio.create_task(
                            send_temporary_message(
                                chat_id,
                                "🌿 <b>You've already claimed a referral!</b>\n\n"
                                "The forest remembers your previous invitation.\n"
                                "No extra bonus this time, but you're always welcome back! 🍃",
                                duration=6
                            )
                        )
                except:
                    pass

        await send_initial_welcome(chat_id, first_name)

    elif text.startswith("/start"):
        # ── SOFT RATE LIMIT ON /START
        start_key = f"start_cd:{chat_id}"
        if await redis_client.exists(start_key):
            await tg_app.bot.send_message(
                chat_id,
                "🌿 <b>Slow down, wanderer!</b>\n\n"
                "The ancient trees are still greeting you...\n"
                "Please wait a moment before trying again 🍃",
                parse_mode="HTML"
            )
            return
        await redis_client.setex(start_key, 10, 1)

    elif text.startswith("/resetsteamsearch"):
            if chat_id != OWNER_ID:
                await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can use this.")
                return
            
            parts = text.split()
            target_id = int(parts[1]) if len(parts) > 1 else chat_id
            
            keys = [
                f"steam_search_attempts:{target_id}",
                f"steam_searching:{target_id}",
                f"steam_search_result:{target_id}",
                f"steam_claim_cd:{target_id}",    # ← was missing
                f"steam_search_cd:{target_id}",   # ← was missing
            ]
            deleted = await redis_client.delete(*keys)
            
            await tg_app.bot.send_message(
                chat_id,
                f"✅ <b>Steam search fully reset for <code>{target_id}</code></b>\n\n"
                f"• Attempts restored to 3\n"
                f"• Search state cleared\n"
                f"• Claim cooldown cleared\n"       # ← new
                f"• Search cooldown cleared\n\n"    # ← new
                f"Deleted {deleted} key(s) 🍃",
                parse_mode="HTML"
            )

    elif text.startswith("/resetsteamclaim"):
            if chat_id != OWNER_ID:
                await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can use this.")
                return
            
            parts = text.split()
            if len(parts) < 3:
                await tg_app.bot.send_message(
                    chat_id,
                    "📌 Usage: <code>/resetsteamclaim &lt;user_id&gt; &lt;account_email&gt;</code>\n\n"
                    "Example:\n<code>/resetsteamclaim 123456789 user@gmail.com</code>",
                    parse_mode="HTML"
                )
                return
            
            target_id = parts[1]
            account_email = parts[2]
            
            manila = pytz.timezone("Asia/Manila")
            today_start = datetime.now(manila).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).astimezone(pytz.utc).isoformat()
            
            # Clear Redis markers
            deleted = await redis_client.delete(
                f"steam_claimed:{target_id}:{account_email}",
                f"steam_claim_cd:{target_id}",
                f"steam_search_cd:{target_id}",
                f"steam_search_attempts:{target_id}",
                f"steam_fb:{target_id}:{account_email}",
                f"steam_reminded:{target_id}:{account_email}",
            )
            
            # Clear DB claim record for today
            await _sb_delete(
                f"steam_claims?chat_id=eq.{target_id}&account_email=eq.{account_email}&claimed_at=gte.{today_start}"
            )
            
            await tg_app.bot.send_message(
                chat_id,
                f"✅ <b>Steam claim fully reset for <code>{target_id}</code></b>\n\n"
                f"📧 Email: <code>{account_email}</code>\n\n"
                f"• Claim record deleted from DB\n"
                f"• Redis claim marker cleared\n"
                f"• Cooldown cleared\n"
                f"• Feedback markers cleared\n\n"
                f"Deleted {deleted} Redis key(s) 🍃",
                parse_mode="HTML"
            )

    elif text.startswith("/settitle"):
        await handle_set_title(chat_id, first_name)

    elif text.startswith("/referrals"):
        await handle_referral_history(chat_id)

    elif text.startswith("/uploadwinoffice"):
        await handle_uploadwinoffice_command(chat_id, raw)

    elif text.startswith("/online"):
            await handle_online_users(chat_id)

    elif text.startswith("/award_beta"):
        try:
            parts = text.split()
            target_id = int(parts[1])
            await handle_award_beta_guardian(chat_id, target_id)
        except:
            await tg_app.bot.send_message(
                chat_id,
                "📌 Usage:\n`/award_beta <user_chat_id>`\nExample: `/award_beta 123456789`",
                parse_mode="HTML"
            )

    elif text.startswith("/test_achievements"):
        await handle_test_achievements(chat_id)

    elif text.startswith("/addpatron"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can add patrons.")
            return

        parts = raw.split(maxsplit=2)
        if len(parts) < 2:
            await tg_app.bot.send_message(
                chat_id,
                "📜 Usage:\n\n"
                "<code>/addpatron @username</code>\n"
                "<code>/addpatron @username Legendary Guardian</code>",
                parse_mode="HTML"
            )
            return

        username = parts[1]
        title = parts[2] if len(parts) > 2 else "Kind Wanderer"

        success = await add_patron(username, title)
        
        if success:
            await tg_app.bot.send_message(
                chat_id,
                f"✅ <b>Added to the Grove of Gratitude!</b>\n\n"
                f"🌟 {username} — {title}\n\n"
                f"The forest now remembers this generous heart. 🍃",
                parse_mode="HTML"
            )
        else:
            await tg_app.bot.send_message(chat_id, "❌ Failed to add patron. Check logs.")

    elif text.startswith("/setlogo"):
        can_change, hours_left = await can_change_profile_gif(chat_id)
        
        if not can_change:
            await tg_app.bot.send_message(
                chat_id,
                f"🌿 <b>You can only change your profile logo once per week.</b>\n\n"
                f"Come back in <b>{hours_left}</b> hours! ✨",
                parse_mode="HTML"
            )
            return
        
    elif text.startswith("/resetprofilegif"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can use this.")
            return

        parts = text.split()
        target_id = int(parts[1]) if len(parts) > 1 else chat_id

        deleted = await redis_client.delete(f"profile_gif_cooldown:{target_id}")

        await tg_app.bot.send_message(
            chat_id,
            f"✅ <b>Profile GIF cooldown reset for <code>{target_id}</code></b>\n\n"
            f"They can now change their profile logo again. 🌿\n"
            f"Deleted {deleted} cooldown key.",
            parse_mode="HTML"
        )

        await redis_client.setex(f"waiting_for_logo:{chat_id}", 600, "1")  # 10 min to upload
        await tg_app.bot.send_message(
            chat_id,
            "🌿 <b>Upload Your Profile Logo</b>\n\n"
            "Send me a <b>GIF</b> (as animation or document).\n"
            "Maximum 10 MB.\n\n"
            "<i>This will become your personal emblem in the forest.</i> ✨",
            parse_mode="HTML"
        )

    elif text.startswith("/uploadkeys"):
        await handle_uploadkeys_command(chat_id)

    elif text.startswith("/resetsteamclaim"):
        if chat_id != OWNER_ID:
            return
        parts = text.split()
        target_id = int(parts[1]) if len(parts) > 1 else chat_id

        manila = pytz.timezone("Asia/Manila")
        today_start = datetime.now(manila).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(pytz.utc).isoformat()

        ok = await _sb_delete(f"steam_claims?chat_id=eq.{target_id}&claimed_at=gte.{today_start}")
        await tg_app.bot.send_message(
            chat_id,
            f"✅ Steam claim limit reset for <code>{target_id}</code>",
            parse_mode="HTML"
        )

    elif text.startswith("/resetonboarding"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can use this.")
            return
        parts = text.split()
        target_id = int(parts[1]) if len(parts) > 1 else chat_id
        deleted1 = await redis_client.delete(f"onboarding_done:{target_id}")
        deleted2 = await redis_client.delete(f"onboarding_step:{target_id}")
        await _sb_patch(        # ← add this
            f"user_profiles?chat_id=eq.{target_id}",
            {"onboarding_completed": False}
        )
        await tg_app.bot.send_message(
            chat_id,
            f"✅ Onboarding reset for <code>{target_id}</code>\n"
            f"Redis: {deleted1 + deleted2} key(s) deleted\n"
            f"Supabase: onboarding_completed → false\n\n"
            f"They'll see the tutorial again on next /start.",
            parse_mode="HTML"
        )

    elif text.startswith("/remove_achievement"):
        try:
            parts = text.split()
            target_id = int(parts[1])
            achievement_code = parts[2].strip()
            await handle_remove_achievement(chat_id, target_id, achievement_code)
        except:
            await tg_app.bot.send_message(
                chat_id,
                "📌 Usage:\n"
                "`/remove_achievement <user_id> <achievement_code>`\n\n"
                "Example:\n"
                "`/remove_achievement 123456789 beta_guardian`",
                parse_mode="HTML"
            )

    elif text.startswith("/addevent"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the caretaker can create events.")
            return

        body = raw[9:].strip()
        if not body:
            await tg_app.bot.send_message(chat_id, "📌 Usage:\n`/addevent\nTitle\nDate\nDescription\nbonus:netflix_double`")
            return

        lines = body.split("\n")

        if len(lines) < 3:
            await tg_app.bot.send_message(
                chat_id, "❌ Need at least 3 lines: title, date, description."
            )
            return
        
        title      = lines[0].strip()
        event_date = lines[1].strip()

        # Extract bonus_type if last line starts with "bonus:"
        bonus_type = ""
        desc_lines = lines[2:]
        if desc_lines and desc_lines[-1].lower().startswith("bonus:"):
            bonus_type = desc_lines[-1].split(":", 1)[1].strip()
            desc_lines = desc_lines[:-1]

        description = "\n".join(desc_lines).strip()

        if not description:
            await tg_app.bot.send_message(chat_id, "❌ Description cannot be empty.")
            return

        await handle_add_event(chat_id, title, description, event_date, bonus_type)

    elif text.startswith("/forest"):
        await handle_info(chat_id)   
    elif text.startswith("/health"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can check the system status.")
            return
        await handle_status(chat_id)

    elif text.startswith("/checkstock"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can use this.")
            return
        # Force immediate stock check bypassing cooldown
        await redis_client.delete("low_stock_alerted")
        await tg_app.bot.send_message(chat_id, "🔍 <i>Checking stock levels now...</i>", parse_mode="HTML")
        data = await get_vamt_data() or []
        counts: dict[str, int] = {}
        SERVICE_EMOJIS = {"netflix":"🍿","prime":"🎥","windows":"🪟","office":"📑","steam":"🎮"}
        for item in data:
            svc = str(item.get("service_type","")).lower().strip()
            if str(item.get("status","")).lower() == "active" and int(item.get("remaining",0)) > 0:
                counts[svc] = counts.get(svc, 0) + 1
        steam_data = await _sb_get("steamCredentials", **{"select":"status","status":"eq.Available"}) or []
        counts["steam"] = len(steam_data)
        if not counts:
            await tg_app.bot.send_message(chat_id, "🌫️ No inventory data found.")
            return
        lines = []
        for svc, count in sorted(counts.items()):
            emoji = SERVICE_EMOJIS.get(svc, "📦")
            status = "⚠️" if count <= 5 else "✅"
            lines.append(f"{status} {emoji} <b>{svc.title()}</b>: <b>{count}</b> available")
        await tg_app.bot.send_message(
            chat_id,
            "📊 <b>Current Stock Levels</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            + "\n".join(lines)
            + "\n\n<i>⚠️ = at or below threshold (5)</i>",
            parse_mode="HTML"
        )
    
    elif text.startswith("/history"):
        await handle_history(chat_id, first_name)

    elif text.startswith("/streak"):
            await show_streak_calendar(chat_id, first_name)

    elif text.startswith("/leaderboard"):
        await handle_leaderboard(chat_id)

    elif text.startswith("/searchsteam"):
        await handle_searchsteam_command(chat_id, raw)

    elif text.startswith("/uploadsteam"):
        await handle_uploadsteam_command(chat_id, raw)

    elif text.startswith("/profile"):
        await handle_profile_page(chat_id, first_name)

    elif text.startswith("/viewreports"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can view reports.")
            return
        await handle_view_reports(chat_id)

    elif text.startswith("/caretaker"):
        await handle_caretaker(chat_id, first_name)

    elif text.startswith("/flushcache"):
        await handle_flushcache(chat_id)

    elif text.startswith("/menu"):
        profile = await get_user_profile(chat_id)
        is_first = not bool(profile.get("has_seen_menu", False)) if profile else True

        # === FIX FOR FIX 6 ===
        if profile and not profile.get("has_seen_menu", False):
            asyncio.create_task(update_has_seen_menu(chat_id))
        # ======================

        await send_full_menu(chat_id, first_name, is_first_time=is_first)

    elif text.startswith("/myid"):
        await send_myid(chat_id)

    elif text.startswith("/resetguide"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can use this.")
            return
        try:
            target_id = int(text.split()[1]) if len(text.split()) > 1 else chat_id
            deleted1 = await redis_client.delete(f"seen_winoffice_guide:{target_id}")
            deleted2 = await redis_client.delete(f"winoffice_pending_cat:{target_id}")
            await tg_app.bot.send_message(
                chat_id,
                f"✅ Guide reset for <code>{target_id}</code>\n\n"
                f"Deleted: {deleted1 + deleted2} key(s)",
                parse_mode="HTML"
            )
        except Exception as e:
            await tg_app.bot.send_message(chat_id, f"❌ Error: {e}")

    # ── RESET WHEEL (Safe - only removes daily cooldown) ──
    elif text.startswith("/resetwheel"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can reset the wheel.")
            return
        
        parts = text.split()
        target_id = int(parts[1]) if len(parts) > 1 else chat_id
        
        deleted = await redis_client.delete(f"wheel_spin:{target_id}")
        
        await tg_app.bot.send_message(
            chat_id,
            f"✅ <b>Wheel spin reset for user {target_id}</b>\n\n"
            f"The Wheel of Whispers is now ready again! ✨\n"
            f"Deleted {deleted} cooldown key.",
            parse_mode="HTML"
        )

    # ── TEST DAILY (Full test reset) ──
    elif text.startswith("/testdaily"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can use this.")
            return
        
        parts = text.split()
        if len(parts) < 2:
            await tg_app.bot.send_message(
                chat_id,
                "🔧 <b>Usage:</b> <code>/testdaily &lt;user_id&gt;</code>\n\n"
                "Example:\n<code>/testdaily 7399488750</code>",
                parse_mode="HTML"
            )
            return

        try:
            target_id = int(parts[1])

            # Reset everything needed for clean testing
            keys = [
                f"daily_bonus:{target_id}",
                f"pending_ref:{target_id}",
                f"ref_awarding:{target_id}",
            ]
            deleted = await redis_client.delete(*keys)

            await tg_app.bot.send_message(
                chat_id,
                f"✅ <b>Fully reset for user {target_id}</b>\n\n"
                f"• Daily bonus → now available again\n"
                f"• Pending referral → cleared\n"
                f"• Referral lock → cleared\n\n"
                f"Deleted {deleted} Redis key(s)\n\n"
                f"You can now test daily bonus or referral link cleanly.",
                parse_mode="HTML"
            )
        except ValueError:
            await tg_app.bot.send_message(chat_id, "❌ Invalid user ID. Must be a number.")
        except Exception as e:
            await tg_app.bot.send_message(chat_id, f"❌ Error: {e}")

    elif text.startswith("/clear"):
        await handle_clear(chat_id, msg_id, first_name)
        
    elif text.startswith("/addgamelogo"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can add game logos.")
            return

        body = raw[len("/addgamelogo"):].strip()

        # OLD WAY: User pasted content directly after the command
        if body:
            await handle_add_game_logo(chat_id, raw)   # ← your existing function
            return

        # NEW WAY: No content = ask for .txt file
        await redis_client.setex(f"waiting_for_gamelogo:{chat_id}", 600, "1")
        
        await tg_app.bot.send_message(
            chat_id,
            "📤 <b>Send Game Logos</b>\n\n"
            "Choose your preferred method:\n\n"
            "• <b>Paste directly</b> (old way) — just type again with content\n"
            "• <b>Send .txt file</b> (recommended for many games)\n\n"
            "Send your <b>.txt</b> file now or paste using the old format.",
            parse_mode="HTML"
        )
        return
        
    elif text.startswith("/feedback"):
        feedback_text = raw[9:].strip()   # preserve original casing
        if not feedback_text:
            await tg_app.bot.send_message(
                chat_id,
                "🌿 Please write your feedback after the command.\n\nExample: `/feedback I love the forest!`",
            )
        elif len(feedback_text) > 500:
            await tg_app.bot.send_message(
                chat_id,
                f"🍃 <b>Your message is too long!</b>\n\n"
                f"The forest can only carry <b>500 characters</b> at a time.\n"
                f"Your message is <b>{len(feedback_text)}</b> characters.\n\n"
                f"<i>Please shorten it and try again.</i> 🌿",
                parse_mode="HTML",
            )
        else:
            await handle_feedback(chat_id, first_name, feedback_text)

    elif text.startswith("/setforestinfo"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the Forest Caretaker can change forest info.")
            return

        # Support BOTH formats:
        # Format A (multi-line):
        #   /setforestinfo
        #   1.4.5
        #   April 15, 2026
        #   02:58 PM
        #
        # Format B (single-line):
        #   /setforestinfo 1.4.5 April 15, 2026 · 02:58 PM

        body = raw[len("/setforestinfo"):].strip()

        if "\n" in body:
            lines = [l.strip() for l in body.splitlines() if l.strip()]
            if len(lines) < 3:
                await tg_app.bot.send_message(
                    chat_id,
                    "📝 Multi-line usage:\n\n"
                    "<code>/setforestinfo\n1.4.5\nApril 15, 2026\n02:58 PM</code>",
                    parse_mode="HTML"
                )
                return
            version = lines[0]
            custom_datetime = f"{lines[1]} · {lines[2]}"
        elif body:
            # Single-line: version is first word, rest is datetime
            parts = body.split(" ", 1)
            if len(parts) < 2:
                await tg_app.bot.send_message(chat_id, "❌ Please include version and datetime.")
                return
            version = parts[0]
            custom_datetime = parts[1].strip()
        else:
            await tg_app.bot.send_message(
                chat_id,
                "📝 Usage:\n\n"
                "<code>/setforestinfo\n1.4.5\nApril 15, 2026\n02:58 PM</code>",
                parse_mode="HTML"
            )
            return

        await set_bot_info(version, custom_datetime, chat_id)

    elif text.startswith("/addupdate"):
        if chat_id != OWNER_ID:
            await tg_app.bot.send_message(chat_id, "🌿 Only the caretaker can add updates.")
            return

        body = raw[10:].strip()
        if not body:
            await tg_app.bot.send_message(
                chat_id,
                "📌 Usage:\n"
                "`/addupdate\nTitle Here\nYour full description...`\n\n"
                "or\n"
                "`/addupdate | Title | Your full description...`",
            )
            return

        title = ""
        content = ""

        if "\n" in body:
            lines = body.split("\n", 1)
            title = lines[0].strip()
            content = lines[1].strip() if len(lines) > 1 else ""
        elif "|" in body:
            parts = [p.strip() for p in body.split("|") if p.strip()]
            if len(parts) >= 2:
                title = parts[0]
                content = " | ".join(parts[1:])
            else:
                title = ""
                content = ""

        if not title or not content:
            await tg_app.bot.send_message(chat_id, "❌ Title and content cannot be empty.")
            return

        await add_new_update(title, content, chat_id)

    elif text.startswith("/invite"):
        await handle_invite(chat_id, first_name)

    elif text.startswith("/spin"):
        await handle_callback(Update.de_json({
            "callback_query": {
                "data": "spin_wheel",
                "from": update.effective_user.to_dict() if update.effective_user else {}
            }
        }, tg_app.bot))
        return
    
    elif text.startswith(("/updates", "/update")):
        await handle_updates(chat_id)

    elif text.startswith(("/lang", "/language")):
        await handle_set_language(chat_id)

    elif text.startswith(("/viewfeedback", "/feedbacks")):
        await handle_view_feedback(chat_id)

    elif text.startswith(("/resetfirst", "/reset")):
        if chat_id == OWNER_ID:
            await handle_reset_first_time(chat_id)
        else:
            await tg_app.bot.send_message(chat_id, "🌿 Only the caretaker can reset the forest memory.")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        log_level="info",
    )
