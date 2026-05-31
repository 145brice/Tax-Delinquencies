from dataclasses import dataclass

from scrapers.cheatham import CheathamScraper
from scrapers.davidson import DavidsonScraper
from scrapers.jaxdailyrecord_legalnotices import (
    ClayJaxDailyRecordScraper,
    DuvalJaxDailyRecordScraper,
    NassauJaxDailyRecordScraper,
    StJohnsJaxDailyRecordScraper,
)
from scrapers.jaxdailyrecord_tax import DuvalJaxDailyRecordRealEstateTaxScraper
from scrapers.losangeles_legalnotices import LosAngelesLegalNoticesScraper
from scrapers.orange_legalnotices import OrangeLegalNoticesScraper
from scrapers.orange_taxsale import OrangeTaxSaleScraper
from scrapers.riverside_legalnotices import RiversideLegalNoticesScraper
from scrapers.robertson import RobertsonScraper
from scrapers.rutherford import RutherfordScraper
from scrapers.sandiego_legalnotices import SanDiegoLegalNoticesScraper
from scrapers.sandiego_taxsale import SanDiegoTaxSaleScraper
from scrapers.sumner import SumnerScraper
from scrapers.williamson import WilliamsonScraper
from scrapers.wilson import WilsonScraper


@dataclass(frozen=True)
class SourceDefinition:
    key: str
    label: str
    region: str
    source_type: str
    cls: type
    source_url: str = ""

    def to_public_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "region": self.region,
            "source_type": self.source_type,
            "source_url": self.source_url,
        }


SOURCES = {
    "davidson": SourceDefinition("davidson", "Davidson tax + notices", "nashville", "County public records", DavidsonScraper),
    "williamson": SourceDefinition("williamson", "Williamson county records", "nashville", "County public records", WilliamsonScraper),
    "rutherford": SourceDefinition("rutherford", "Rutherford county records", "nashville", "County public records", RutherfordScraper),
    "wilson": SourceDefinition("wilson", "Wilson county records", "nashville", "County public records", WilsonScraper),
    "sumner": SourceDefinition("sumner", "Sumner tax sale PDFs", "nashville", "County public records", SumnerScraper),
    "robertson": SourceDefinition("robertson", "Robertson court auctions", "nashville", "County public records", RobertsonScraper),
    "cheatham": SourceDefinition("cheatham", "Cheatham tax sales", "nashville", "County public records", CheathamScraper),
    "sandiego_taxsale": SourceDefinition("sandiego_taxsale", "San Diego tax sale", "sandiego", "Tax sale", SanDiegoTaxSaleScraper),
    "sandiego_legalnotices": SourceDefinition("sandiego_legalnotices", "San Diego legal notices", "sandiego", "Legal notices", SanDiegoLegalNoticesScraper),
    "losangeles_legalnotices": SourceDefinition("losangeles_legalnotices", "Los Angeles legal notices", "losangeles", "Legal notices", LosAngelesLegalNoticesScraper),
    "orange_taxsale": SourceDefinition("orange_taxsale", "Orange tax sale", "orange", "Tax sale", OrangeTaxSaleScraper),
    "orange_legalnotices": SourceDefinition("orange_legalnotices", "Orange legal notices", "orange", "Legal notices", OrangeLegalNoticesScraper),
    "riverside_legalnotices": SourceDefinition("riverside_legalnotices", "Riverside legal notices", "riverside", "Legal notices", RiversideLegalNoticesScraper),
    "duval_jaxdailyrecord": SourceDefinition(
        "duval_jaxdailyrecord",
        "Duval Jax Daily Record notices",
        "northeast_fl",
        "Legal notices",
        DuvalJaxDailyRecordScraper,
        "https://legals.jaxdailyrecord.com/public_notices/publicnotices.php?Category=Notice+of+Sale+-+Foreclosure&mode=daily",
    ),
    "duval_jaxdailyrecord_retax": SourceDefinition(
        "duval_jaxdailyrecord_retax",
        "Duval Jax Daily Record delinquent RE tax",
        "northeast_fl",
        "Tax delinquent",
        DuvalJaxDailyRecordRealEstateTaxScraper,
        "https://legals.jaxdailyrecord.com/re_tax/retax_search.php",
    ),
    "stjohns_jaxdailyrecord": SourceDefinition(
        "stjohns_jaxdailyrecord",
        "St. Johns Jax Daily Record notices",
        "northeast_fl",
        "Legal notices",
        StJohnsJaxDailyRecordScraper,
        "https://legals.jaxdailyrecord.com/public_notices/publicnotices.php?Category=Notice+of+Sale+-+Foreclosure&mode=daily",
    ),
    "nassau_jaxdailyrecord": SourceDefinition(
        "nassau_jaxdailyrecord",
        "Nassau Jax Daily Record notices",
        "northeast_fl",
        "Legal notices",
        NassauJaxDailyRecordScraper,
        "https://legals.jaxdailyrecord.com/public_notices/publicnotices.php?Category=Notice+of+Sale+-+Foreclosure&mode=daily",
    ),
    "clay_jaxdailyrecord": SourceDefinition(
        "clay_jaxdailyrecord",
        "Clay Jax Daily Record notices",
        "northeast_fl",
        "Legal notices",
        ClayJaxDailyRecordScraper,
        "https://legals.jaxdailyrecord.com/public_notices/publicnotices.php?Category=Notice+of+Sale+-+Foreclosure&mode=daily",
    ),
}


UI_COUNTY_SOURCES = {
    "davidson": ["davidson"],
    "wilson-tn": ["wilson"],
    "williamson": ["williamson"],
    "rutherford": ["rutherford"],
    "sumner": ["sumner"],
    "robertson": ["robertson"],
    "cheatham": ["cheatham"],
    "sandiego": ["sandiego_taxsale", "sandiego_legalnotices"],
    "losangeles": ["losangeles_legalnotices"],
    "orange": ["orange_taxsale", "orange_legalnotices"],
    "riverside": ["riverside_legalnotices"],
    "duval-fl": ["duval_jaxdailyrecord", "duval_jaxdailyrecord_retax"],
    "stjohns-fl": ["stjohns_jaxdailyrecord"],
    "nassau-fl": ["nassau_jaxdailyrecord"],
    "clay-fl": ["clay_jaxdailyrecord"],
}


ALL_SCRAPERS = {key: source.cls for key, source in SOURCES.items()}
REGION_BY_KEY = {key: source.region for key, source in SOURCES.items()}
SOURCE_METADATA = {key: source.to_public_dict() for key, source in SOURCES.items()}
