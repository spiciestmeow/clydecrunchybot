import requests
import uuid

def check_crunchyroll():
    print("--- Crunchyroll Account Checker ---")
    username = input("Enter Email/Username: ")
    password = input("Enter Password: ")

    session = requests.Session()
    device_id = str(uuid.uuid4())

    auth_url = "https://beta-api.crunchyroll.com/auth/v1/token"
    headers = {
        "User-Agent": "AppleCoreMedia/1.0.0.20L563 (Apple TV; U; CPU OS 16_5 like Mac OS X; en_us)",
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": "beta-api.crunchyroll.com"
    }
    
    payload = {
        "grant_type": "password",
        "username": username,
        "password": password,
        "scope": "offline_access",
        "client_id": "y2arvjb0h0rgvtizlovy",
        "client_secret": "JVLvwdIpXvxU-qIBvT1M8oQTr1qlQJX2",
        "device_type": "AppleTV",
        "device_id": device_id,
        "device_name": "AppleTV"
    }

    try:
        response = session.post(auth_url, headers=headers, data=payload)
        
        if response.status_code != 200:
            print(f"[-] Login Failed: {username}")
            return

        token = response.json().get("access_token")
        headers["Authorization"] = f"Bearer {token}"
        me_resp = session.get("https://beta-api.crunchyroll.com/accounts/v1/me", headers=headers)
        me_data = me_resp.json()
        
        external_id = me_data.get("external_id")
        email_verified = me_data.get("email_verified", "False")
        created_date = me_data.get("created", "N/A").split("T")[0]
        prod_url = f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{external_id}/products"
        prod_resp = session.get(prod_url, headers=headers)
        prod_data = prod_resp.json()
        
        plan = "None"
        currency = "N/A"
        subscribable = "False"
        free_trials = "False"

        if "items" in prod_data and prod_data["items"]:
            item = prod_data["items"][0]
            plan = item.get("product", {}).get("sku", "None")
            currency = item.get("currency_code", "N/A")
            subscribable = item.get("product", {}).get("is_subscribable", False)
            free_trials = item.get("active_free_trial", False)
        subs_url = f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{external_id}"
        subs_resp = session.get(subs_url, headers=headers)
        subs_data = subs_resp.json()

        expiry = subs_data.get("next_renewal_date", "N/A")
        duration = subs_data.get("cycle_duration", "N/A")
        active = subs_data.get("is_active", False)
        country = subs_data.get("country_code", "N/A")
        output = (
            f"{username}:{password} | "
            f"EmailVerified = {email_verified} | "
            f"AccountCreationDate = {created_date} | "
            f"Plan = {plan} | "
            f"Currency = {currency} | "
            f"Subscribable = {subscribable} | "
            f"FreeTrails = {free_trials} | "
            f"Expiry = {expiry} | "
            f"PlanDuration = {duration} | "
            f"Active = {active} | "
            f"Country = {country}"
        )        
        print("\n" + output)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_crunchyroll()