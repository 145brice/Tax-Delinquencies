"""Kern County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class KernLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "Kern"
    default_city = "Bakersfield"

    # Kern County zips: 932xx-933xx (Bakersfield core + county), parts of 934xx
    allowed_zip_patterns = [
        r"\b(932\d{2}|933\d{2})\b",
        r"\b(9340[0-9]|9341[0-9]|9342[0-9])\b",  # Tehachapi area
    ]
    rejected_zip_patterns = [
        r"\b(900\d{2}|901\d{2}|902\d{2}|903\d{2}|904\d{2}|905\d{2}|906\d{2}|907\d{2}|908\d{2})\b",  # LA
        r"\b(910\d{2}|911\d{2}|912\d{2}|913\d{2}|914\d{2}|915\d{2}|916\d{2}|917\d{2})\b",  # LA
        r"\b(930\d{2}|931\d{2})\b",  # Ventura
        r"\b(935\d{2}|936\d{2}|937\d{2}|938\d{2})\b",  # Fresno / Tulare / Kings
    ]

    allowed_cities = {
        "bakersfield", "delano", "ridgecrest", "tehachapi", "wasco", "shafter",
        "arvin", "taft", "maricopa", "mcfarland", "california city", "mojave",
        "lamont", "rosamond", "lake isabella", "boron", "edwards",
        "buttonwillow", "lost hills", "mettler", "lebec", "gorman",
        "frazier park", "stallion springs", "bear valley springs", "pine mountain club",
    }
    rejected_cities = {
        "los angeles", "ventura", "oxnard", "santa barbara", "fresno",
        "visalia", "tulare", "porterville", "lancaster", "palmdale",
        "san bernardino", "victorville",
    }
