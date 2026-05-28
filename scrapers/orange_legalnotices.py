"""Orange County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class OrangeLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "Orange"
    default_city = "Santa Ana"

    # OC zips: 926xx, 927xx, 928xx (a portion)
    allowed_zip_patterns = [r"\b(926\d{2}|927\d{2}|92805|92806|92807|92808)\b"]
    # Reject neighbors
    rejected_zip_patterns = [
        r"\b(900\d{2}|901\d{2}|902\d{2}|903\d{2}|904\d{2}|905\d{2}|906\d{2}|907\d{2}|908\d{2})\b",  # LA
        r"\b(910\d{2}|911\d{2}|912\d{2}|913\d{2}|914\d{2}|915\d{2}|916\d{2}|917\d{2})\b",          # LA
        r"\b(919\d{2}|920\d{2}|921\d{2})\b",                                                       # SD
        r"\b(922\d{2}|923\d{2}|924\d{2}|925\d{2})\b",                                              # Riverside / SB
    ]

    allowed_cities = {
        "anaheim", "santa ana", "irvine", "huntington beach", "garden grove",
        "fullerton", "orange", "costa mesa", "mission viejo", "westminster",
        "newport beach", "buena park", "lake forest", "tustin", "yorba linda",
        "san clemente", "laguna niguel", "la habra", "fountain valley",
        "placentia", "rancho santa margarita", "aliso viejo", "cypress",
        "brea", "stanton", "san juan capistrano", "laguna hills", "dana point",
        "laguna beach", "la palma", "los alamitos", "seal beach", "villa park",
        "fountain valley", "ladera ranch", "coto de caza", "trabuco canyon",
    }
    rejected_cities = {
        "los angeles", "long beach", "san diego", "chula vista", "escondido",
        "temecula", "murrieta", "riverside", "corona", "moreno valley",
        "san bernardino", "fontana", "ontario", "rancho cucamonga",
    }
