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
  Berrien: ~62  Washtenaw: ~59  Livingston: ~42  Ottawa: ~32  Saginaw: ~3
  Barry: ~23
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
