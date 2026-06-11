from dataclasses import dataclass

from scrapers.alameda_legalnotices import AlamedaLegalNoticesScraper
from scrapers.barry_legalnotices import BarryLegalNoticesScraper
from scrapers.barry_taxforeclosure import BarryTaxForeclosureScraper
from scrapers.cheatham import CheathamScraper
from scrapers.harris_taxsale import HarrisTaxSaleScraper
from scrapers.maricopa_azcapitoltimes import MaricopaAzCapitolTimesScraper
from scrapers.maricopa_recordreporter import MaricopaRecordReporterScraper
from scrapers.maricopa_trusteesale import MaricopaTrusteeSaleScraper
from scrapers.contracosta_legalnotices import ContraCostaLegalNoticesScraper
from scrapers.davidson import DavidsonScraper
from scrapers.fresno_legalnotices import FresnoLegalNoticesScraper
from scrapers.jaxdailyrecord_legalnotices import (
    ClayJaxDailyRecordScraper,
    DuvalJaxDailyRecordScraper,
    NassauJaxDailyRecordScraper,
    StJohnsJaxDailyRecordScraper,
)
from scrapers.jaxdailyrecord_tax import DuvalJaxDailyRecordRealEstateTaxScraper
from scrapers.kern_legalnotices import KernLegalNoticesScraper
from scrapers.losangeles_legalnotices import LosAngelesLegalNoticesScraper
from scrapers.orange_legalnotices import OrangeLegalNoticesScraper
from scrapers.orange_taxsale import OrangeTaxSaleScraper
from scrapers.riverside_legalnotices import RiversideLegalNoticesScraper
from scrapers.robertson import RobertsonScraper
from scrapers.rutherford import RutherfordScraper
from scrapers.sacramento_legalnotices import SacramentoLegalNoticesScraper
from scrapers.sanbernardino_legalnotices import SanBernardinoLegalNoticesScraper
from scrapers.sandiego_legalnotices import SanDiegoLegalNoticesScraper
from scrapers.sandiego_taxsale import SanDiegoTaxSaleScraper
from scrapers.sanmateo_legalnotices import SanMateoLegalNoticesScraper
from scrapers.santaclara_legalnotices import SantaClaraLegalNoticesScraper
from scrapers.sumner import SumnerScraper
from scrapers.ventura_legalnotices import VenturaLegalNoticesScraper
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
    "sanbernardino_legalnotices": SourceDefinition("sanbernardino_legalnotices", "San Bernardino legal notices", "inland_empire", "Legal notices", SanBernardinoLegalNoticesScraper),
    "ventura_legalnotices": SourceDefinition("ventura_legalnotices", "Ventura legal notices", "ventura", "Legal notices", VenturaLegalNoticesScraper),
    "sacramento_legalnotices": SourceDefinition("sacramento_legalnotices", "Sacramento legal notices", "sacramento", "Legal notices", SacramentoLegalNoticesScraper),
    "alameda_legalnotices": SourceDefinition("alameda_legalnotices", "Alameda legal notices", "bayarea", "Legal notices", AlamedaLegalNoticesScraper),
    "santaclara_legalnotices": SourceDefinition("santaclara_legalnotices", "Santa Clara legal notices", "bayarea", "Legal notices", SantaClaraLegalNoticesScraper),
    "kern_legalnotices": SourceDefinition("kern_legalnotices", "Kern legal notices", "kern", "Legal notices", KernLegalNoticesScraper),
    "fresno_legalnotices": SourceDefinition("fresno_legalnotices", "Fresno legal notices", "fresno", "Legal notices", FresnoLegalNoticesScraper),
    "contracosta_legalnotices": SourceDefinition("contracosta_legalnotices", "Contra Costa legal notices", "bayarea", "Legal notices", ContraCostaLegalNoticesScraper),
    "sanmateo_legalnotices": SourceDefinition("sanmateo_legalnotices", "San Mateo legal notices", "bayarea", "Legal notices", SanMateoLegalNoticesScraper),
    "harris_taxsale": SourceDefinition(
        "harris_taxsale", "Harris County TX delinquent tax sale", "harris_tx", "Tax sale",
        HarrisTaxSaleScraper, "https://www.hctax.net/Property/listings/taxsalelisting"
    ),
    "maricopa_trusteesale": SourceDefinition(
        "maricopa_trusteesale", "Maricopa County AZ trustee sales (Tiffany & Bosco)", "maricopa_az", "Trustee sale",
        MaricopaTrusteeSaleScraper, "https://fs.tblaw.com/sales/PendingSalesAz.aspx"
    ),
    "maricopa_azcapitoltimes": SourceDefinition(
        "maricopa_azcapitoltimes", "Maricopa County AZ trustee notices (AZ Capitol Times)", "maricopa_az", "Legal notices",
        MaricopaAzCapitolTimesScraper, "https://azcapitoltimes.com/public-notice/search-results/"
    ),
    "maricopa_recordreporter": SourceDefinition(
        "maricopa_recordreporter", "Maricopa County AZ trustee notices (Record Reporter)", "maricopa_az", "Legal notices",
        MaricopaRecordReporterScraper, "https://recordreporter.com/LegalNotices/"
    ),
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
    "barry_taxforeclosure": SourceDefinition(
        "barry_taxforeclosure",
        "Barry County MI tax-foreclosure auction",
        "barry_mi",
        "Tax delinquent",
        BarryTaxForeclosureScraper,
        "https://www.tax-sale.info/auctions",
    ),
    "barry_legalnotices": SourceDefinition(
        "barry_legalnotices",
        "Barry County MI foreclosure notices (Hastings Banner)",
        "barry_mi",
        "Legal notices",
        BarryLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
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
    "sanbernardino": ["sanbernardino_legalnotices"],
    "ventura": ["ventura_legalnotices"],
    "sacramento": ["sacramento_legalnotices"],
    "alameda": ["alameda_legalnotices"],
    "santaclara": ["santaclara_legalnotices"],
    "kern": ["kern_legalnotices"],
    "fresno": ["fresno_legalnotices"],
    "contracosta": ["contracosta_legalnotices"],
    "sanmateo": ["sanmateo_legalnotices"],
    "harris-tx": ["harris_taxsale"],
    "maricopa-az": ["maricopa_trusteesale", "maricopa_azcapitoltimes"],
    # Backward-compatible UI keys from the original broad county list.
    "harris": ["harris_taxsale"],
    "maricopa": ["maricopa_trusteesale", "maricopa_azcapitoltimes"],
    "barry-mi": ["barry_taxforeclosure", "barry_legalnotices"],
    "duval-fl": ["duval_jaxdailyrecord", "duval_jaxdailyrecord_retax"],
    "stjohns-fl": ["stjohns_jaxdailyrecord"],
    "nassau-fl": ["nassau_jaxdailyrecord"],
    "clay-fl": ["clay_jaxdailyrecord"],
}


ALL_SCRAPERS = {key: source.cls for key, source in SOURCES.items()}
REGION_BY_KEY = {key: source.region for key, source in SOURCES.items()}
SOURCE_METADATA = {key: source.to_public_dict() for key, source in SOURCES.items()}
