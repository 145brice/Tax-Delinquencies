"""Alameda County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class AlamedaLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "Alameda"
    default_city = "Oakland"

    # Alameda County zips: 945xx (Fremont/Hayward/San Leandro), 946xx (Oakland/Berkeley)
    allowed_zip_patterns = [
        r"\b(945\d{2})\b",
        r"\b(946\d{2})\b",
        r"\b(94301|94302|94303)\b",  # Palo Alto border (East Palo Alto is San Mateo)
    ]
    rejected_zip_patterns = [
        r"\b(941\d{2}|942\d{2}|943\d{2}|944\d{2})\b",  # SF / Marin / San Mateo
        r"\b(947\d{2}|948\d{2})\b",  # Contra Costa
        r"\b(949\d{2})\b",           # Marin
        r"\b(950\d{2}|951\d{2})\b",  # Santa Clara
    ]

    allowed_cities = {
        "oakland", "fremont", "hayward", "berkeley", "livermore", "san leandro",
        "alameda", "albany", "castro valley", "dublin", "emeryville", "newark",
        "piedmont", "pleasanton", "san lorenzo", "union city", "ashland",
        "cherryland", "fairview", "sunol", "tesla",
    }
    rejected_cities = {
        "san francisco", "daly city", "san mateo", "redwood city", "palo alto",
        "san jose", "sunnyvale", "santa clara", "milpitas",
        "walnut creek", "concord", "richmond", "el cerrito", "martinez",
        "san ramon", "danville", "orinda", "lafayette",
    }
