"""Sacramento County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class SacramentoLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "Sacramento"
    default_city = "Sacramento"

    # Sacramento County zips: 956xx (city core + suburbs), parts of 957xx
    allowed_zip_patterns = [
        r"\b(9581[0-9]|9582[0-9]|9583[0-9]|9584[0-3]|95864)\b",  # Sacramento city
        r"\b(9562[0-9]|9563[0-9]|9566[0-9]|9567[0-9]|9568[0-9]|9569[0-9])\b",  # suburbs (Elk Grove, Citrus Heights, etc.)
        r"\b(9574[0-9]|9575[0-9]|9576[0-9])\b",  # Rancho Cordova, Folsom
    ]
    rejected_zip_patterns = [
        r"\b(956[0-9][0-9])\b",  # broad reject for out-of-county 956xx — city list handles it
        r"\b(950\d{2}|951\d{2})\b",  # Santa Clara
        r"\b(945\d{2}|946\d{2})\b",  # Alameda
        r"\b(947\d{2}|948\d{2})\b",  # Contra Costa / Marin
    ]

    allowed_cities = {
        "sacramento", "elk grove", "citrus heights", "rancho cordova", "folsom",
        "carmichael", "north highlands", "antelope", "arden arcade", "fair oaks",
        "orangevale", "gold river", "rosemont", "florin", "vineyard",
        "galt", "isleton", "walnut grove", "herald", "sloughhouse",
    }
    rejected_cities = {
        "roseville", "rocklin", "lincoln", "auburn",  # Placer County
        "davis", "woodland", "west sacramento",        # Yolo County
        "stockton", "lodi", "tracy",                   # San Joaquin
        "oakland", "san jose", "san francisco",
    }
