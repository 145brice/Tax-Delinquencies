"""Santa Clara County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class SantaClaraLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "Santa Clara"
    default_city = "San Jose"

    # Santa Clara County zips: 950xx-951xx, 940xx (Los Altos/Mountain View/Palo Alto border)
    allowed_zip_patterns = [
        r"\b(950\d{2}|951\d{2})\b",
        r"\b(9400[0-9]|9401[0-9]|9402[0-9]|9403[0-9]|9404[0-9])\b",  # Mountain View, Los Altos, Palo Alto
    ]
    rejected_zip_patterns = [
        r"\b(945\d{2}|946\d{2})\b",  # Alameda
        r"\b(952\d{2}|953\d{2})\b",  # Santa Cruz / Monterey
        r"\b(941\d{2}|942\d{2}|943\d{2}|944\d{2})\b",  # SF / San Mateo
    ]

    allowed_cities = {
        "san jose", "sunnyvale", "santa clara", "mountain view", "gilroy",
        "milpitas", "palo alto", "campbell", "cupertino", "los altos",
        "los gatos", "morgan hill", "saratoga", "monte sereno", "los altos hills",
        "alviso", "coyote", "holly oak", "east san jose", "willow glen",
    }
    rejected_cities = {
        "oakland", "fremont", "hayward", "berkeley", "alameda", "san leandro",
        "san francisco", "daly city", "san mateo", "redwood city", "foster city",
        "santa cruz", "capitola", "scotts valley", "watsonville",
        "salinas", "monterey", "san luis obispo",
    }
