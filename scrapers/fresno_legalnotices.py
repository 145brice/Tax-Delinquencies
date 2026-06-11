"""Fresno County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class FresnoLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "Fresno"
    default_city = "Fresno"

    # Fresno County zips: 937xx-938xx (city + county)
    allowed_zip_patterns = [r"\b(937\d{2}|938\d{2})\b"]
    rejected_zip_patterns = [
        r"\b(932\d{2}|933\d{2})\b",  # Kern
        r"\b(935\d{2}|936\d{2})\b",  # Tulare / Kings
        r"\b(939\d{2})\b",           # Madera / Merced
        r"\b(933\d{2})\b",           # Kern
    ]

    allowed_cities = {
        "fresno", "clovis", "sanger", "selma", "fowler", "reedley", "kingsburg",
        "coalinga", "huron", "san joaquin", "mendota", "Kerman", "parlier",
        "orange cove", "del rey", "caruthers", "riverdale", "tranquillity",
        "biola", "five points", "firebaugh", "dos palos", "helm",
        "auberry", "prather", "shaver lake", "pine flat",
    }
    rejected_cities = {
        "bakersfield", "los angeles", "visalia", "tulare", "porterville",
        "modesto", "merced", "stockton", "madera", "chowchilla",
    }
