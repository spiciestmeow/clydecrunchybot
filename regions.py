# regions.py
# ISO 3166-1 alpha-2 region codes → flag emojis

REGION_HINTS: dict[str, str] = {
    # ── Asia Pacific ──
    "PH": "🇵🇭",  # Philippines
    "JP": "🇯🇵",  # Japan
    "KR": "🇰🇷",  # South Korea
    "VN": "🇻🇳",  # Vietnam
    "TH": "🇹🇭",  # Thailand
    "SG": "🇸🇬",  # Singapore
    "MY": "🇲🇾",  # Malaysia
    "ID": "🇮🇩",  # Indonesia
    "TW": "🇹🇼",  # Taiwan
    "HK": "🇭🇰",  # Hong Kong
    "IN": "🇮🇳",  # India
    "AU": "🇦🇺",  # Australia
    "NZ": "🇳🇿",  # New Zealand
    "CN": "🇨🇳",  # China
    "MO": "🇲🇴",  # Macau
    "MM": "🇲🇲",  # Myanmar
    "KH": "🇰🇭",  # Cambodia
    "LA": "🇱🇦",  # Laos
    "BD": "🇧🇩",  # Bangladesh
    "PK": "🇵🇰",  # Pakistan
    "LK": "🇱🇰",  # Sri Lanka
    "NP": "🇳🇵",  # Nepal
    "MN": "🇲🇳",  # Mongolia
    "KZ": "🇰🇿",  # Kazakhstan
    "UZ": "🇺🇿",  # Uzbekistan
    "AZ": "🇦🇿",  # Azerbaijan
    "GE": "🇬🇪",  # Georgia
    "AM": "🇦🇲",  # Armenia
    # ── Europe ──
    "GB": "🇬🇧",  # United Kingdom
    "FR": "🇫🇷",  # France
    "DE": "🇩🇪",  # Germany
    "PL": "🇵🇱",  # Poland
    "IT": "🇮🇹",  # Italy
    "ES": "🇪🇸",  # Spain
    "NL": "🇳🇱",  # Netherlands
    "PT": "🇵🇹",  # Portugal
    "RO": "🇷🇴",  # Romania
    "TR": "🇹🇷",  # Turkey
    "UA": "🇺🇦",  # Ukraine
    "RU": "🇷🇺",  # Russia
    "SE": "🇸🇪",  # Sweden
    "NO": "🇳🇴",  # Norway
    "DK": "🇩🇰",  # Denmark
    "FI": "🇫🇮",  # Finland
    "CZ": "🇨🇿",  # Czech Republic
    "HU": "🇭🇺",  # Hungary
    "SK": "🇸🇰",  # Slovakia
    "GR": "🇬🇷",  # Greece
    "BE": "🇧🇪",  # Belgium
    "AT": "🇦🇹",  # Austria
    "CH": "🇨🇭",  # Switzerland
    "BG": "🇧🇬",  # Bulgaria
    "HR": "🇭🇷",  # Croatia
    "RS": "🇷🇸",  # Serbia
    "SI": "🇸🇮",  # Slovenia
    "EE": "🇪🇪",  # Estonia
    "LV": "🇱🇻",  # Latvia
    "LT": "🇱🇹",  # Lithuania
    "IE": "🇮🇪",  # Ireland
    "IS": "🇮🇸",  # Iceland
    "LU": "🇱🇺",  # Luxembourg
    "MT": "🇲🇹",  # Malta
    "CY": "🇨🇾",  # Cyprus
    "AL": "🇦🇱",  # Albania
    "MK": "🇲🇰",  # North Macedonia
    "BA": "🇧🇦",  # Bosnia
    "ME": "🇲🇪",  # Montenegro
    "MD": "🇲🇩",  # Moldova
    "BY": "🇧🇾",  # Belarus
    # ── Americas ──
    "US": "🇺🇸",  # United States
    "CA": "🇨🇦",  # Canada
    "BR": "🇧🇷",  # Brazil
    "MX": "🇲🇽",  # Mexico
    "AR": "🇦🇷",  # Argentina
    "CO": "🇨🇴",  # Colombia
    "CL": "🇨🇱",  # Chile
    "PE": "🇵🇪",  # Peru
    "VE": "🇻🇪",  # Venezuela
    "EC": "🇪🇨",  # Ecuador
    "BO": "🇧🇴",  # Bolivia
    "PY": "🇵🇾",  # Paraguay
    "UY": "🇺🇾",  # Uruguay
    "CR": "🇨🇷",  # Costa Rica
    "PA": "🇵🇦",  # Panama
    "GT": "🇬🇹",  # Guatemala
    "HN": "🇭🇳",  # Honduras
    "SV": "🇸🇻",  # El Salvador
    "NI": "🇳🇮",  # Nicaragua
    "DO": "🇩🇴",  # Dominican Republic
    "CU": "🇨🇺",  # Cuba
    "JM": "🇯🇲",  # Jamaica
    "TT": "🇹🇹",  # Trinidad and Tobago
    # ── Middle East ──
    "AE": "🇦🇪",  # UAE
    "SA": "🇸🇦",  # Saudi Arabia
    "QA": "🇶🇦",  # Qatar
    "KW": "🇰🇼",  # Kuwait
    "BH": "🇧🇭",  # Bahrain
    "OM": "🇴🇲",  # Oman
    "JO": "🇯🇴",  # Jordan
    "LB": "🇱🇧",  # Lebanon
    "IQ": "🇮🇶",  # Iraq
    "IR": "🇮🇷",  # Iran
    "IL": "🇮🇱",  # Israel
    "PS": "🇵🇸",  # Palestine
    "SY": "🇸🇾",  # Syria
    "YE": "🇾🇪",  # Yemen
    # ── Africa ──
    "ZA": "🇿🇦",  # South Africa
    "EG": "🇪🇬",  # Egypt
    "NG": "🇳🇬",  # Nigeria
    "KE": "🇰🇪",  # Kenya
    "GH": "🇬🇭",  # Ghana
    "TZ": "🇹🇿",  # Tanzania
    "ET": "🇪🇹",  # Ethiopia
    "UG": "🇺🇬",  # Uganda
    "DZ": "🇩🇿",  # Algeria
    "MA": "🇲🇦",  # Morocco
    "TN": "🇹🇳",  # Tunisia
    "LY": "🇱🇾",  # Libya
    "SD": "🇸🇩",  # Sudan
    "CM": "🇨🇲",  # Cameroon
    "CI": "🇨🇮",  # Ivory Coast
    "SN": "🇸🇳",  # Senegal
    "ZW": "🇿🇼",  # Zimbabwe
    "ZM": "🇿🇲",  # Zambia
    "AO": "🇦🇴",  # Angola
    "MZ": "🇲🇿",  # Mozambique
}


def get_region_flag(service_type: str) -> str:
    """Extract region flag from service_type e.g. 'Netflix Premium GB' → ' 🇬🇧'"""
    svc_upper = service_type.upper().strip()
    for code, flag in REGION_HINTS.items():
        if svc_upper.endswith(f" {code}"):
            return f" {flag}"
    return ""