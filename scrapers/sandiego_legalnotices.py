"""San Diego County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class SanDiegoLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "San Diego"
    default_city = "San Diego"

    # SD County zips: 919xx and 92003-92199
    allowed_zip_patterns = [r"\b(919\d{2}|920\d{2}|921\d{2})\b"]
    # Nearby non-SD: Riverside/SB/Imperial 922xx-928xx
    rejected_zip_patterns = [r"\b(922\d{2}|923\d{2}|924\d{2}|925\d{2}|926\d{2}|927\d{2}|928\d{2})\b"]

    allowed_cities = {
        "san diego", "chula vista", "el cajon", "escondido", "oceanside",
        "carlsbad", "vista", "san marcos", "santee", "la mesa",
        "spring valley", "poway", "national city", "lemon grove", "encinitas",
        "solana beach", "del mar", "la jolla", "rancho bernardo",
        "coronado", "imperial beach", "bonita", "lakeside", "alpine", "ramona",
        "valley center", "fallbrook", "bonsall", "rainbow", "julian",
        "borrego springs", "campo", "tecate", "pine valley", "rancho santa fe",
    }
    rejected_cities = {
        "temecula", "murrieta", "menifee", "hemet", "perris", "riverside",
        "moreno valley", "corona", "lake elsinore", "san bernardino",
        "fontana", "ontario", "rancho cucamonga", "victorville",
        "palm springs", "palm desert", "indio", "los angeles", "long beach",
    }
