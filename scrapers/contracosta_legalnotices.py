"""Contra Costa County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class ContraCostaLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "Contra Costa"
    default_city = "Concord"

    # Contra Costa zips: 945xx (East Bay — shared with Alameda), 947xx-948xx
    allowed_zip_patterns = [
        r"\b(947\d{2}|948\d{2})\b",
        r"\b(9451[0-9]|9452[0-9]|9453[0-9])\b",  # Antioch, Brentwood, Oakley end of 945xx
    ]
    rejected_zip_patterns = [
        r"\b(945[0-9][0-9])\b",  # default reject broad 945 (Alameda) — city list refines
        r"\b(941\d{2}|942\d{2}|943\d{2}|944\d{2})\b",  # SF / Marin / San Mateo
        r"\b(945[0][0-9]|9450[0-9])\b",  # Fremont/Newark (Alameda)
        r"\b(946\d{2})\b",  # Oakland (Alameda)
        r"\b(949\d{2})\b",  # Marin
    ]

    allowed_cities = {
        "concord", "richmond", "antioch", "san ramon", "pittsburg", "walnut creek",
        "brentwood", "hercules", "pleasant hill", "martinez", "el cerrito",
        "oakley", "danville", "pinole", "orinda", "lafayette", "moraga",
        "el sobrante", "san pablo", "kensington", "rodeo", "crockett",
        "port costa", "bay point", "discovery bay", "bethel island",
        "alamo", "diablo", "blackhawk", "clayton", "clyde",
    }
    rejected_cities = {
        "oakland", "berkeley", "fremont", "hayward", "san leandro", "alameda",
        "san francisco", "daly city", "san jose", "sunnyvale",
        "vallejo", "benicia", "fairfield",  # Solano County
        "stockton", "tracy", "livermore",   # Livermore is Alameda
    }
