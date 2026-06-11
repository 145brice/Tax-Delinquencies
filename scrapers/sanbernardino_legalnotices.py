"""San Bernardino County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class SanBernardinoLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "San Bernardino"
    default_city = "San Bernardino"

    # SB County: 917xx (Chino/Ontario/Rancho Cucamonga), 923xx-924xx (Victor Valley, SB city, Redlands)
    allowed_zip_patterns = [
        r"\b(9173[0-9]|9174[0-9]|9175[0-9]|9176[0-4]|9178[4-6])\b",  # Chino, Ontario, Upland, Montclair
        r"\b(923\d{2}|924\d{2})\b",  # Victor Valley, San Bernardino, Redlands, Big Bear
    ]
    rejected_zip_patterns = [
        r"\b(900\d{2}|901\d{2}|902\d{2}|903\d{2}|904\d{2}|905\d{2}|906\d{2}|907\d{2}|908\d{2})\b",  # LA
        r"\b(910\d{2}|911\d{2}|912\d{2}|913\d{2}|914\d{2}|915\d{2}|916\d{2})\b",  # LA
        r"\b(919\d{2}|920\d{2}|921\d{2})\b",  # SD
        r"\b(925\d{2})\b",  # Riverside
        r"\b(926\d{2}|927\d{2}|928\d{2})\b",  # OC
    ]

    allowed_cities = {
        "san bernardino", "fontana", "rancho cucamonga", "ontario", "victorville",
        "rialto", "hesperia", "chino", "chino hills", "upland", "colton",
        "apple valley", "redlands", "yucaipa", "highland", "loma linda",
        "twentynine palms", "montclair", "yucca valley", "adelanto",
        "big bear lake", "big bear city", "barstow", "needles", "crestline",
        "lake arrowhead", "running springs", "wrightwood", "twin peaks",
        "grand terrace", "bloomington", "muscoy", "devore", "newberry springs",
        "lucerne valley", "joshua tree", "palm desert springs",
    }
    rejected_cities = {
        "los angeles", "long beach", "pasadena", "glendale", "burbank",
        "san diego", "chula vista", "escondido", "oceanside", "carlsbad",
        "riverside", "moreno valley", "corona", "temecula", "murrieta",
        "hemet", "perris", "palm springs", "palm desert", "indio",
        "anaheim", "santa ana", "irvine", "huntington beach",
        "ventura", "oxnard", "thousand oaks", "simi valley",
    }
