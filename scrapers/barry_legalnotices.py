"""Barry County (MI) — mortgage/judicial foreclosure notices via mipublicnotices.com.

Michigan foreclosure-by-advertisement notices (the pre-sheriff-sale step) are
published in The Hastings Banner and indexed by the Michigan Press Association's
public-notice site. These are mortgage foreclosures — a different property set
from the county's tax-foreclosure auction, so the two Barry sources do not overlap.

Barry County area alias: 8 (alphabetical position among Michigan's 83 counties).
"""
import os

from .mi_legalnotices_base import MILegalNoticesScraper

_BARRY_ALIAS = os.getenv("MI_BARRY_AREA_ALIAS", "8")


class BarryLegalNoticesScraper(MILegalNoticesScraper):
    county_name = "Barry"
    area_alias = _BARRY_ALIAS
    default_city = "Hastings"
