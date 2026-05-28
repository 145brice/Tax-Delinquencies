"""Los Angeles County (CA) — Notice of Trustee Sale via capublicnotice.com."""
from .ca_legalnotices_base import CALegalNoticesScraper


class LosAngelesLegalNoticesScraper(CALegalNoticesScraper):
    county_name = "Los Angeles"
    default_city = "Los Angeles"

    # LA County zips: 900xx-908xx, 910xx-913xx, 914xx-917xx
    allowed_zip_patterns = [
        r"\b(900\d{2}|901\d{2}|902\d{2}|903\d{2}|904\d{2}|905\d{2}|906\d{2}|907\d{2}|908\d{2})\b",
        r"\b(910\d{2}|911\d{2}|912\d{2}|913\d{2})\b",
        r"\b(914\d{2}|915\d{2}|916\d{2}|917\d{2})\b",
    ]
    # Reject: OC 926xx-928xx, Ventura 930xx-931xx, San Bernardino 923xx-924xx
    rejected_zip_patterns = [
        r"\b(919\d{2}|920\d{2}|921\d{2}|922\d{2})\b",  # SD
        r"\b(923\d{2}|924\d{2}|925\d{2})\b",            # SB / Riverside
        r"\b(926\d{2}|927\d{2}|928\d{2})\b",            # OC
        r"\b(930\d{2}|931\d{2})\b",                     # Ventura
    ]

    allowed_cities = {
        "los angeles", "long beach", "santa clarita", "glendale", "lancaster",
        "palmdale", "pomona", "torrance", "pasadena", "el monte", "downey",
        "inglewood", "west covina", "norwalk", "burbank", "compton",
        "south gate", "carson", "santa monica", "hawthorne", "whittier",
        "alhambra", "lakewood", "bellflower", "baldwin park", "lynwood",
        "redondo beach", "pico rivera", "monterey park", "gardena",
        "huntington park", "arcadia", "diamond bar", "paramount", "rosemead",
        "san gabriel", "covina", "azusa", "monrovia", "temple city",
        "manhattan beach", "santa fe springs", "claremont", "la verne",
        "glendora", "san dimas", "duarte", "el segundo", "culver city",
        "beverly hills", "san fernando", "calabasas", "agoura hills",
        "malibu", "westlake village", "hidden hills", "rolling hills",
        "rancho palos verdes", "palos verdes estates", "marina del rey",
        "venice", "hollywood", "north hollywood", "studio city", "encino",
        "tarzana", "woodland hills", "canoga park", "chatsworth", "reseda",
        "van nuys", "sherman oaks", "valley village", "panorama city",
        "sylmar", "mission hills", "granada hills", "northridge",
        "porter ranch", "west hills", "winnetka", "pacoima", "sun valley",
    }
    rejected_cities = {
        "san diego", "chula vista", "escondido", "oceanside", "carlsbad",
        "temecula", "murrieta", "riverside", "moreno valley", "corona",
        "san bernardino", "fontana", "ontario", "rancho cucamonga",
        "victorville", "anaheim", "santa ana", "irvine", "huntington beach",
        "garden grove", "fullerton", "orange", "costa mesa", "newport beach",
        "ventura", "oxnard", "thousand oaks", "simi valley", "camarillo",
    }
