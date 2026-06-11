"""San Mateo County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class SanMateoLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "San Mateo"
    default_city = "San Mateo"

    # San Mateo County zips: 940xx-941xx (Peninsula south of SF)
    allowed_zip_patterns = [
        r"\b(940\d{2})\b",
        r"\b(9410[0-9]|9411[0-9]|9412[0-9]|9413[0-9]|9414[0-9])\b",
    ]
    rejected_zip_patterns = [
        r"\b(941[5-9]\d|942\d{2}|943\d{2}|944\d{2})\b",  # SF / Marin
        r"\b(945\d{2}|946\d{2})\b",  # Alameda
        r"\b(950\d{2}|951\d{2})\b",  # Santa Clara
    ]

    allowed_cities = {
        "san mateo", "daly city", "redwood city", "south san francisco", "san bruno",
        "burlingame", "foster city", "san carlos", "menlo park", "belmont",
        "millbrae", "colma", "brisbane", "pacifica", "half moon bay",
        "atherton", "portola valley", "woodside", "east palo alto",
        "hillsborough", "san francisco", "north fair oaks", "ladera",
        "pescadero", "la honda",
    }
    rejected_cities = {
        "palo alto", "mountain view", "sunnyvale", "santa clara", "san jose",
        "oakland", "berkeley", "fremont", "hayward",
        "san francisco",  # SF is its own county
        "sausalito", "tiburon", "mill valley", "san rafael",  # Marin
    }
