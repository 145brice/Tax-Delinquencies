"""Riverside County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class RiversideLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "Riverside"
    default_city = "Riverside"

    # Riverside zips: 925xx (most), parts of 922xx, 923xx (north), 92201-92254
    allowed_zip_patterns = [
        r"\b(925\d{2})\b",
        r"\b(9220[0-9]|9221[0-9]|9222[0-9]|9223[0-9]|9224[0-9]|9225[0-4])\b",
        r"\b(922\d{2})\b",  # broader Riverside Valley
    ]
    rejected_zip_patterns = [
        r"\b(900\d{2}|901\d{2}|902\d{2}|903\d{2}|904\d{2}|905\d{2}|906\d{2}|907\d{2}|908\d{2})\b",  # LA
        r"\b(910\d{2}|911\d{2}|912\d{2}|913\d{2}|914\d{2}|915\d{2}|916\d{2}|917\d{2})\b",          # LA
        r"\b(926\d{2}|927\d{2}|928\d{2})\b",                                                       # OC
        r"\b(919\d{2}|920\d{2}|921\d{2})\b",                                                       # SD
        # San Bernardino 923xx (much overlap with Riverside in some zips) — be lenient
    ]

    allowed_cities = {
        "riverside", "moreno valley", "corona", "temecula", "murrieta",
        "menifee", "hemet", "perris", "lake elsinore", "indio", "palm desert",
        "palm springs", "cathedral city", "la quinta", "rancho mirage",
        "coachella", "desert hot springs", "blythe", "banning", "beaumont",
        "wildomar", "san jacinto", "norco", "eastvale", "jurupa valley",
        "calimesa", "canyon lake", "indian wells", "yucaipa",
    }
    rejected_cities = {
        "los angeles", "long beach", "san diego", "chula vista", "escondido",
        "oceanside", "carlsbad", "irvine", "anaheim", "santa ana",
        "san bernardino", "fontana", "ontario", "rancho cucamonga",
        "victorville", "redlands", "highland", "loma linda",
    }
