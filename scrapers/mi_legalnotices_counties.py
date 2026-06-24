"""
Michigan county legal-notice scrapers — mipublicnotices.com.

Each class is a thin subclass of MILegalNoticesScraper. The only difference
between counties is:
  area_alias  — numeric county alias in the mipublicnotices API, equal to the
                county's 1-indexed alphabetical position among Michigan's 83
                counties (Barry=8 confirmed; others derived from same ordering)
  county_name — display label
  default_city — county seat / largest city, used when address cannot be parsed

Lead volume (90-day window, 2026-06):
  Wayne: ~971  Macomb: ~467  Oakland: ~444  Genesee: ~243  Ingham: ~168
  Kent: ~140   Jackson: ~109  Muskegon: ~95  Calhoun: ~74   Kalamazoo: ~69
  Monroe: ~68   Lenawee: ~65  Berrien: ~62  Hillsdale: ~60  Washtenaw: ~59
  Lapeer: ~45   Eaton: ~43  Livingston: ~42  Bay: ~34  Montcalm: ~34
  Tuscola: ~33  Ottawa: ~32  Allegan: ~26  Cass: ~23  Barry: ~23
  Saginaw: ~3
"""
from .mi_legalnotices_base import MILegalNoticesScraper


# ── Southeast Michigan ──────────────────────────────────────────────────────

class WayneLegalNoticesScraper(MILegalNoticesScraper):
    """Wayne County (Detroit metro) — alias 82."""
    county_name = "Wayne"
    area_alias = "82"
    default_city = "Detroit"


class MacombLegalNoticesScraper(MILegalNoticesScraper):
    """Macomb County (Warren / Sterling Heights) — alias 50."""
    county_name = "Macomb"
    area_alias = "50"
    default_city = "Mount Clemens"


class OaklandLegalNoticesScraper(MILegalNoticesScraper):
    """Oakland County (Pontiac / Troy) — alias 63."""
    county_name = "Oakland"
    area_alias = "63"
    default_city = "Pontiac"


class LivingstonLegalNoticesScraper(MILegalNoticesScraper):
    """Livingston County (Howell) — alias 47."""
    county_name = "Livingston"
    area_alias = "47"
    default_city = "Howell"


class WashtenawLegalNoticesScraper(MILegalNoticesScraper):
    """Washtenaw County (Ann Arbor) — alias 81."""
    county_name = "Washtenaw"
    area_alias = "81"
    default_city = "Ann Arbor"


# ── Mid-Michigan ────────────────────────────────────────────────────────────

class EatonLegalNoticesScraper(MILegalNoticesScraper):
    """Eaton County (Charlotte) — alias 23."""
    county_name = "Eaton"
    area_alias = "23"
    default_city = "Charlotte"


class GenesseeLegalNoticesScraper(MILegalNoticesScraper):
    """Genesee County (Flint) — alias 25."""
    county_name = "Genesee"
    area_alias = "25"
    default_city = "Flint"


class InghamLegalNoticesScraper(MILegalNoticesScraper):
    """Ingham County (Lansing) — alias 33."""
    county_name = "Ingham"
    area_alias = "33"
    default_city = "Lansing"


class SaginawLegalNoticesScraper(MILegalNoticesScraper):
    """Saginaw County (Saginaw) — alias 73."""
    county_name = "Saginaw"
    area_alias = "73"
    default_city = "Saginaw"


class JacksonLegalNoticesScraper(MILegalNoticesScraper):
    """Jackson County (Jackson) — alias 38."""
    county_name = "Jackson"
    area_alias = "38"
    default_city = "Jackson"


# ── West Michigan ───────────────────────────────────────────────────────────

class AlleganLegalNoticesScraper(MILegalNoticesScraper):
    """Allegan County (Allegan) — alias 3."""
    county_name = "Allegan"
    area_alias = "3"
    default_city = "Allegan"


class BayLegalNoticesScraper(MILegalNoticesScraper):
    """Bay County (Bay City) — alias 9."""
    county_name = "Bay"
    area_alias = "9"
    default_city = "Bay City"


class KentLegalNoticesScraper(MILegalNoticesScraper):
    """Kent County (Grand Rapids) — alias 41."""
    county_name = "Kent"
    area_alias = "41"
    default_city = "Grand Rapids"


class OttawaLegalNoticesScraper(MILegalNoticesScraper):
    """Ottawa County (Holland / Grand Haven) — alias 70."""
    county_name = "Ottawa"
    area_alias = "70"
    default_city = "Grand Haven"


class MuskegonLegalNoticesScraper(MILegalNoticesScraper):
    """Muskegon County (Muskegon) — alias 61."""
    county_name = "Muskegon"
    area_alias = "61"
    default_city = "Muskegon"


class KalamazooLegalNoticesScraper(MILegalNoticesScraper):
    """Kalamazoo County (Kalamazoo) — alias 39."""
    county_name = "Kalamazoo"
    area_alias = "39"
    default_city = "Kalamazoo"


class BerienLegalNoticesScraper(MILegalNoticesScraper):
    """Berrien County (Benton Harbor / St. Joseph) — alias 11."""
    county_name = "Berrien"
    area_alias = "11"
    default_city = "St. Joseph"


class CalhounLegalNoticesScraper(MILegalNoticesScraper):
    """Calhoun County (Battle Creek) — alias 13."""
    county_name = "Calhoun"
    area_alias = "13"
    default_city = "Battle Creek"


class CassLegalNoticesScraper(MILegalNoticesScraper):
    """Cass County (Cassopolis) — alias 14."""
    county_name = "Cass"
    area_alias = "14"
    default_city = "Cassopolis"


class HillsdaleLegalNoticesScraper(MILegalNoticesScraper):
    """Hillsdale County (Hillsdale) — alias 30."""
    county_name = "Hillsdale"
    area_alias = "30"
    default_city = "Hillsdale"


class LapeerLegalNoticesScraper(MILegalNoticesScraper):
    """Lapeer County (Lapeer) — alias 44."""
    county_name = "Lapeer"
    area_alias = "44"
    default_city = "Lapeer"


class LenaweeLegalNoticesScraper(MILegalNoticesScraper):
    """Lenawee County (Adrian) — alias 46."""
    county_name = "Lenawee"
    area_alias = "46"
    default_city = "Adrian"


class MonroeLegalNoticesScraper(MILegalNoticesScraper):
    """Monroe County (Monroe) — alias 58."""
    county_name = "Monroe"
    area_alias = "58"
    default_city = "Monroe"


class MontcalmLegalNoticesScraper(MILegalNoticesScraper):
    """Montcalm County (Stanton) — alias 59."""
    county_name = "Montcalm"
    area_alias = "59"
    default_city = "Stanton"


class TuscolaLegalNoticesScraper(MILegalNoticesScraper):
    """Tuscola County (Caro) — alias 79."""
    county_name = "Tuscola"
    area_alias = "79"
    default_city = "Caro"
