"""Ventura County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class VenturaLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "Ventura"
    default_city = "Ventura"

    # Ventura County zips: 930xx-931xx
    allowed_zip_patterns = [r"\b(930\d{2}|931\d{2})\b"]
    rejected_zip_patterns = [
        r"\b(900\d{2}|901\d{2}|902\d{2}|903\d{2}|904\d{2}|905\d{2}|906\d{2}|907\d{2}|908\d{2})\b",  # LA
        r"\b(910\d{2}|911\d{2}|912\d{2}|913\d{2}|914\d{2}|915\d{2}|916\d{2}|917\d{2})\b",  # LA
        r"\b(932\d{2}|933\d{2})\b",  # SB/Kern
    ]

    allowed_cities = {
        "ventura", "oxnard", "thousand oaks", "simi valley", "camarillo",
        "moorpark", "santa paula", "port hueneme", "fillmore", "ojai",
        "westlake village", "newbury park", "oak park", "agoura",
        "somis", "piru", "casitas springs", "oak view",
    }
    rejected_cities = {
        "los angeles", "santa monica", "malibu", "calabasas", "agoura hills",
        "burbank", "glendale", "pasadena", "san bernardino", "fontana",
        "bakersfield", "santa barbara",
    }
