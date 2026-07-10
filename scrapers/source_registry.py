from dataclasses import dataclass

from scrapers.alameda_legalnotices import AlamedaLegalNoticesScraper
from scrapers.barry_legalnotices import BarryLegalNoticesScraper
from scrapers.barry_taxforeclosure import BarryTaxForeclosureScraper
from scrapers.mi_legalnotices_counties import (
    AlleganLegalNoticesScraper,
    BayLegalNoticesScraper,
    BerienLegalNoticesScraper,
    CalhounLegalNoticesScraper,
    CassLegalNoticesScraper,
    EatonLegalNoticesScraper,
    GenesseeLegalNoticesScraper,
    HillsdaleLegalNoticesScraper,
    InghamLegalNoticesScraper,
    JacksonLegalNoticesScraper,
    KalamazooLegalNoticesScraper,
    KentLegalNoticesScraper,
    LapeerLegalNoticesScraper,
    LenaweeLegalNoticesScraper,
    LivingstonLegalNoticesScraper,
    MacombLegalNoticesScraper,
    MonroeLegalNoticesScraper,
    MontcalmLegalNoticesScraper,
    MuskegonLegalNoticesScraper,
    OaklandLegalNoticesScraper,
    OttawaLegalNoticesScraper,
    SaginawLegalNoticesScraper,
    TuscolaLegalNoticesScraper,
    WashtenawLegalNoticesScraper,
    WayneLegalNoticesScraper,
)
from scrapers.cheatham import CheathamScraper
from scrapers.clark_sheriff_sales import ClarkSheriffSalesScraper
from scrapers.collin_foreclosures import CollinForeclosuresScraper
from scrapers.bexar_foreclosures import BexarForeclosuresScraper
from scrapers.fl_publicnotices import (
    BrowardPublicNoticesScraper,
    HillsboroughPublicNoticesScraper,
    MiamiDadePublicNoticesScraper,
    OrangeFLPublicNoticesScraper,
    PalmBeachPublicNoticesScraper,
)
from scrapers.harris_taxsale import HarrisTaxSaleScraper
from scrapers.maricopa_azcapitoltimes import MaricopaAzCapitolTimesScraper
from scrapers.maricopa_probate import MaricopaProbateScraper
from scrapers.maricopa_recordreporter import MaricopaRecordReporterScraper
from scrapers.maricopa_trusteesale import MaricopaTrusteeSaleScraper
from scrapers.contracosta_legalnotices import ContraCostaLegalNoticesScraper
from scrapers.davidson import DavidsonScraper
from scrapers.fl_probate_divorce import (
    BrowardDivorceScraper,
    BrowardProbateScraper,
    HillsboroughDivorceScraper,
    HillsboroughProbateScraper,
    MiamiDadeDivorceScraper,
    MiamiDadeProbateScraper,
    OrangeFLDivorceScraper,
    OrangeFLProbateScraper,
    PalmBeachDivorceScraper,
    PalmBeachProbateScraper,
)
from scrapers.fl_taxdeed import (
    BrowardTaxDeedScraper,
    HillsboroughTaxDeedScraper,
    MiamiDadeTaxDeedScraper,
    OrangeFLTaxDeedScraper,
    PalmBeachTaxDeedScraper,
)
from scrapers.fresno_legalnotices import FresnoLegalNoticesScraper
from scrapers.jaxdailyrecord_legalnotices import (
    ClayJaxDailyRecordScraper,
    DuvalJaxDailyRecordScraper,
    NassauJaxDailyRecordScraper,
    StJohnsJaxDailyRecordScraper,
)
from scrapers.jaxdailyrecord_probate import (
    ClayJaxProbateScraper,
    DuvalJaxDissolutionScraper,
    DuvalJaxProbateScraper,
    NassauJaxProbateScraper,
    StJohnsJaxProbateScraper,
)
from scrapers.jaxdailyrecord_tax import DuvalJaxDailyRecordRealEstateTaxScraper
from scrapers.kern_legalnotices import KernLegalNoticesScraper
from scrapers.mi_probate import MI_PROBATE_SCRAPERS
from scrapers.ontario_taxsale import OntarioTaxSaleScraper
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
    "collin_foreclosures": SourceDefinition(
        "collin_foreclosures", "Collin County TX foreclosure notices",
        "collin_tx", "Legal notices", CollinForeclosuresScraper,
        "https://apps2.collincountytx.gov/ForeclosureNotices",
    ),
    "bexar_foreclosures": SourceDefinition(
        "bexar_foreclosures", "Bexar County TX mortgage + tax foreclosures",
        "bexar_tx", "Foreclosure map", BexarForeclosuresScraper,
        "https://maps.bexar.org/foreclosures/",
    ),
    "clark_sheriff_sales": SourceDefinition(
        "clark_sheriff_sales", "Clark County NV sheriff sales",
        "clark_nv", "Sheriff sale", ClarkSheriffSalesScraper,
        "https://www.clarkcountynv.gov/government/departments/sheriff_civil/sheriff_s_sales/",
    ),
    "broward_publicnotices": SourceDefinition(
        "broward_publicnotices", "Broward County FL foreclosure notices",
        "south_fl", "Legal notices", BrowardPublicNoticesScraper,
        "https://floridapublicnotices.com/",
    ),
    "miamidade_publicnotices": SourceDefinition(
        "miamidade_publicnotices", "Miami-Dade County FL foreclosure notices",
        "south_fl", "Legal notices", MiamiDadePublicNoticesScraper,
        "https://floridapublicnotices.com/",
    ),
    "palmbeach_publicnotices": SourceDefinition(
        "palmbeach_publicnotices", "Palm Beach County FL foreclosure notices",
        "south_fl", "Legal notices", PalmBeachPublicNoticesScraper,
        "https://floridapublicnotices.com/",
    ),
    "orangefl_publicnotices": SourceDefinition(
        "orangefl_publicnotices", "Orange County FL foreclosure notices",
        "central_fl", "Legal notices", OrangeFLPublicNoticesScraper,
        "https://floridapublicnotices.com/",
    ),
    "hillsborough_publicnotices": SourceDefinition(
        "hillsborough_publicnotices", "Hillsborough County FL foreclosure notices",
        "tampa_bay", "Legal notices", HillsboroughPublicNoticesScraper,
        "https://floridapublicnotices.com/",
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
    "maricopa_probate": SourceDefinition(
        "maricopa_probate", "Maricopa County AZ probate notices (Record Reporter)", "maricopa_az", "Probate",
        MaricopaProbateScraper, "https://recordreporter.com/LegalNotices/"
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
    # ── Michigan statewide — mipublicnotices.com ──────────────────────────
    "wayne_legalnotices": SourceDefinition(
        "wayne_legalnotices", "Wayne County MI foreclosure notices",
        "wayne_mi", "Legal notices", WayneLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "macomb_legalnotices": SourceDefinition(
        "macomb_legalnotices", "Macomb County MI foreclosure notices",
        "macomb_mi", "Legal notices", MacombLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "oakland_legalnotices": SourceDefinition(
        "oakland_legalnotices", "Oakland County MI foreclosure notices",
        "oakland_mi", "Legal notices", OaklandLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "genesee_legalnotices": SourceDefinition(
        "genesee_legalnotices", "Genesee County MI foreclosure notices",
        "genesee_mi", "Legal notices", GenesseeLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "ingham_legalnotices": SourceDefinition(
        "ingham_legalnotices", "Ingham County MI foreclosure notices",
        "ingham_mi", "Legal notices", InghamLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "kent_legalnotices": SourceDefinition(
        "kent_legalnotices", "Kent County MI foreclosure notices",
        "kent_mi", "Legal notices", KentLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "jackson_legalnotices": SourceDefinition(
        "jackson_legalnotices", "Jackson County MI foreclosure notices",
        "jackson_mi", "Legal notices", JacksonLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "muskegon_legalnotices": SourceDefinition(
        "muskegon_legalnotices", "Muskegon County MI foreclosure notices",
        "muskegon_mi", "Legal notices", MuskegonLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "kalamazoo_legalnotices": SourceDefinition(
        "kalamazoo_legalnotices", "Kalamazoo County MI foreclosure notices",
        "kalamazoo_mi", "Legal notices", KalamazooLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "calhoun_legalnotices": SourceDefinition(
        "calhoun_legalnotices", "Calhoun County MI foreclosure notices",
        "calhoun_mi", "Legal notices", CalhounLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "berrien_legalnotices": SourceDefinition(
        "berrien_legalnotices", "Berrien County MI foreclosure notices",
        "berrien_mi", "Legal notices", BerienLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "washtenaw_legalnotices": SourceDefinition(
        "washtenaw_legalnotices", "Washtenaw County MI foreclosure notices",
        "washtenaw_mi", "Legal notices", WashtenawLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "livingston_legalnotices": SourceDefinition(
        "livingston_legalnotices", "Livingston County MI foreclosure notices",
        "livingston_mi", "Legal notices", LivingstonLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "ottawa_legalnotices": SourceDefinition(
        "ottawa_legalnotices", "Ottawa County MI foreclosure notices",
        "ottawa_mi", "Legal notices", OttawaLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "saginaw_legalnotices": SourceDefinition(
        "saginaw_legalnotices", "Saginaw County MI foreclosure notices",
        "saginaw_mi", "Legal notices", SaginawLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "monroe_legalnotices": SourceDefinition(
        "monroe_legalnotices", "Monroe County MI foreclosure notices",
        "monroe_mi", "Legal notices", MonroeLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "lenawee_legalnotices": SourceDefinition(
        "lenawee_legalnotices", "Lenawee County MI foreclosure notices",
        "lenawee_mi", "Legal notices", LenaweeLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "hillsdale_legalnotices": SourceDefinition(
        "hillsdale_legalnotices", "Hillsdale County MI foreclosure notices",
        "hillsdale_mi", "Legal notices", HillsdaleLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "lapeer_legalnotices": SourceDefinition(
        "lapeer_legalnotices", "Lapeer County MI foreclosure notices",
        "lapeer_mi", "Legal notices", LapeerLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "eaton_legalnotices": SourceDefinition(
        "eaton_legalnotices", "Eaton County MI foreclosure notices",
        "eaton_mi", "Legal notices", EatonLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "bay_legalnotices": SourceDefinition(
        "bay_legalnotices", "Bay County MI foreclosure notices",
        "bay_mi", "Legal notices", BayLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "montcalm_legalnotices": SourceDefinition(
        "montcalm_legalnotices", "Montcalm County MI foreclosure notices",
        "montcalm_mi", "Legal notices", MontcalmLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "tuscola_legalnotices": SourceDefinition(
        "tuscola_legalnotices", "Tuscola County MI foreclosure notices",
        "tuscola_mi", "Legal notices", TuscolaLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "allegan_legalnotices": SourceDefinition(
        "allegan_legalnotices", "Allegan County MI foreclosure notices",
        "allegan_mi", "Legal notices", AlleganLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    "cass_legalnotices": SourceDefinition(
        "cass_legalnotices", "Cass County MI foreclosure notices",
        "cass_mi", "Legal notices", CassLegalNoticesScraper,
        "https://www.mipublicnotices.com/",
    ),
    # ── Ontario, Canada — municipal tax sales (OntarioTaxSales.ca) ─────────
    "ontario_taxsale": SourceDefinition(
        "ontario_taxsale", "Ontario CA municipal tax sales",
        "ontario_ca", "Tax sale", OntarioTaxSaleScraper,
        "https://www.ontariotaxsales.ca/tax-sale-properties",
    ),
    # ── Florida probate (Notice to Creditors) — floridapublicnotices.com ───
    "broward_probate": SourceDefinition(
        "broward_probate", "Broward County FL probate notices",
        "south_fl", "Probate", BrowardProbateScraper,
        "https://floridapublicnotices.com/",
    ),
    "miamidade_probate": SourceDefinition(
        "miamidade_probate", "Miami-Dade County FL probate notices",
        "south_fl", "Probate", MiamiDadeProbateScraper,
        "https://floridapublicnotices.com/",
    ),
    "palmbeach_probate": SourceDefinition(
        "palmbeach_probate", "Palm Beach County FL probate notices",
        "south_fl", "Probate", PalmBeachProbateScraper,
        "https://floridapublicnotices.com/",
    ),
    "orangefl_probate": SourceDefinition(
        "orangefl_probate", "Orange County FL probate notices",
        "central_fl", "Probate", OrangeFLProbateScraper,
        "https://floridapublicnotices.com/",
    ),
    "hillsborough_probate": SourceDefinition(
        "hillsborough_probate", "Hillsborough County FL probate notices",
        "tampa_bay", "Probate", HillsboroughProbateScraper,
        "https://floridapublicnotices.com/",
    ),
    # ── Florida tax deed applications (FS 197.512) — floridapublicnotices.com
    "broward_taxdeed": SourceDefinition(
        "broward_taxdeed", "Broward County FL tax deed notices",
        "south_fl", "Tax Sale", BrowardTaxDeedScraper,
        "https://floridapublicnotices.com/",
    ),
    "miamidade_taxdeed": SourceDefinition(
        "miamidade_taxdeed", "Miami-Dade County FL tax deed notices",
        "south_fl", "Tax Sale", MiamiDadeTaxDeedScraper,
        "https://floridapublicnotices.com/",
    ),
    "palmbeach_taxdeed": SourceDefinition(
        "palmbeach_taxdeed", "Palm Beach County FL tax deed notices",
        "south_fl", "Tax Sale", PalmBeachTaxDeedScraper,
        "https://floridapublicnotices.com/",
    ),
    "orangefl_taxdeed": SourceDefinition(
        "orangefl_taxdeed", "Orange County FL tax deed notices",
        "central_fl", "Tax Sale", OrangeFLTaxDeedScraper,
        "https://floridapublicnotices.com/",
    ),
    "hillsborough_taxdeed": SourceDefinition(
        "hillsborough_taxdeed", "Hillsborough County FL tax deed notices",
        "tampa_bay", "Tax Sale", HillsboroughTaxDeedScraper,
        "https://floridapublicnotices.com/",
    ),
    # ── Florida divorce (Dissolution of Marriage notices of action) ────────
    "broward_divorce": SourceDefinition(
        "broward_divorce", "Broward County FL divorce notices",
        "south_fl", "Divorce", BrowardDivorceScraper,
        "https://floridapublicnotices.com/",
    ),
    "miamidade_divorce": SourceDefinition(
        "miamidade_divorce", "Miami-Dade County FL divorce notices",
        "south_fl", "Divorce", MiamiDadeDivorceScraper,
        "https://floridapublicnotices.com/",
    ),
    "palmbeach_divorce": SourceDefinition(
        "palmbeach_divorce", "Palm Beach County FL divorce notices",
        "south_fl", "Divorce", PalmBeachDivorceScraper,
        "https://floridapublicnotices.com/",
    ),
    "orangefl_divorce": SourceDefinition(
        "orangefl_divorce", "Orange County FL divorce notices",
        "central_fl", "Divorce", OrangeFLDivorceScraper,
        "https://floridapublicnotices.com/",
    ),
    "hillsborough_divorce": SourceDefinition(
        "hillsborough_divorce", "Hillsborough County FL divorce notices",
        "tampa_bay", "Divorce", HillsboroughDivorceScraper,
        "https://floridapublicnotices.com/",
    ),
    # ── Northeast FL probate + divorce — Jax Daily Record ──────────────────
    "duval_jax_probate": SourceDefinition(
        "duval_jax_probate", "Duval Jax Daily Record probate notices",
        "northeast_fl", "Probate", DuvalJaxProbateScraper,
        "https://legals.jaxdailyrecord.com/public_notices/publicnotices.php?Category=Probate&mode=daily",
    ),
    "stjohns_jax_probate": SourceDefinition(
        "stjohns_jax_probate", "St. Johns Jax Daily Record probate notices",
        "northeast_fl", "Probate", StJohnsJaxProbateScraper,
        "https://legals.jaxdailyrecord.com/public_notices/publicnotices.php?Category=Probate&mode=daily",
    ),
    "clay_jax_probate": SourceDefinition(
        "clay_jax_probate", "Clay Jax Daily Record probate notices",
        "northeast_fl", "Probate", ClayJaxProbateScraper,
        "https://legals.jaxdailyrecord.com/public_notices/publicnotices.php?Category=Probate&mode=daily",
    ),
    "nassau_jax_probate": SourceDefinition(
        "nassau_jax_probate", "Nassau Jax Daily Record probate notices",
        "northeast_fl", "Probate", NassauJaxProbateScraper,
        "https://legals.jaxdailyrecord.com/public_notices/publicnotices.php?Category=Probate&mode=daily",
    ),
    "duval_jax_dissolution": SourceDefinition(
        "duval_jax_dissolution", "Duval Jax Daily Record divorce notices",
        "northeast_fl", "Divorce", DuvalJaxDissolutionScraper,
        "https://legals.jaxdailyrecord.com/public_notices/publicnotices.php?Category=Notice+of+Action+-+Dissolution+of+Marriage&mode=daily",
    ),
}

# ── Michigan probate — one source per county, mirroring foreclosure coverage ─
for _key, _cls in MI_PROBATE_SCRAPERS.items():
    SOURCES[_key] = SourceDefinition(
        _key,
        f"{_cls.county_name} County MI probate notices",
        f"{_cls.county_name.lower().replace(' ', '').replace('.', '')}_mi",
        "Probate",
        _cls,
        "https://www.mipublicnotices.com/",
    )


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
    "collin-tx": ["collin_foreclosures"],
    "bexar-tx": ["bexar_foreclosures"],
    "clark-nv": ["clark_sheriff_sales"],
    "broward-fl": ["broward_publicnotices"],
    "miamidade-fl": ["miamidade_publicnotices"],
    "palmbeach-fl": ["palmbeach_publicnotices"],
    "orange-fl": ["orangefl_publicnotices"],
    "hillsborough-fl": ["hillsborough_publicnotices"],
    "maricopa-az": ["maricopa_trusteesale", "maricopa_azcapitoltimes", "maricopa_probate"],
    # Backward-compatible UI keys from the original broad county list.
    "harris": ["harris_taxsale"],
    "maricopa": ["maricopa_trusteesale", "maricopa_azcapitoltimes", "maricopa_probate"],
    "miami-dade": ["miamidade_publicnotices"],
    "barry-mi": ["barry_taxforeclosure", "barry_legalnotices"],
    "wayne-mi": ["wayne_legalnotices"],
    "macomb-mi": ["macomb_legalnotices"],
    "oakland-mi": ["oakland_legalnotices"],
    "genesee-mi": ["genesee_legalnotices"],
    "ingham-mi": ["ingham_legalnotices"],
    "kent-mi": ["kent_legalnotices"],
    "jackson-mi": ["jackson_legalnotices"],
    "muskegon-mi": ["muskegon_legalnotices"],
    "kalamazoo-mi": ["kalamazoo_legalnotices"],
    "calhoun-mi": ["calhoun_legalnotices"],
    "berrien-mi": ["berrien_legalnotices"],
    "washtenaw-mi": ["washtenaw_legalnotices"],
    "livingston-mi": ["livingston_legalnotices"],
    "ottawa-mi": ["ottawa_legalnotices"],
    "saginaw-mi": ["saginaw_legalnotices"],
    "monroe-mi": ["monroe_legalnotices"],
    "lenawee-mi": ["lenawee_legalnotices"],
    "hillsdale-mi": ["hillsdale_legalnotices"],
    "lapeer-mi": ["lapeer_legalnotices"],
    "eaton-mi": ["eaton_legalnotices"],
    "bay-mi": ["bay_legalnotices"],
    "montcalm-mi": ["montcalm_legalnotices"],
    "tuscola-mi": ["tuscola_legalnotices"],
    "allegan-mi": ["allegan_legalnotices"],
    "cass-mi": ["cass_legalnotices"],
    "duval-fl": ["duval_jaxdailyrecord", "duval_jaxdailyrecord_retax",
                 "duval_jax_probate", "duval_jax_dissolution"],
    "stjohns-fl": ["stjohns_jaxdailyrecord", "stjohns_jax_probate"],
    "nassau-fl": ["nassau_jaxdailyrecord", "nassau_jax_probate"],
    "clay-fl": ["clay_jaxdailyrecord", "clay_jax_probate"],
    "ontario-ca": ["ontario_taxsale"],
}

# Attach FL probate + divorce + tax deed sources to their county UI keys.
for _ui_key, _extra in {
    "broward-fl": ["broward_probate", "broward_divorce", "broward_taxdeed"],
    "miamidade-fl": ["miamidade_probate", "miamidade_divorce", "miamidade_taxdeed"],
    "palmbeach-fl": ["palmbeach_probate", "palmbeach_divorce", "palmbeach_taxdeed"],
    "orange-fl": ["orangefl_probate", "orangefl_divorce", "orangefl_taxdeed"],
    "hillsborough-fl": ["hillsborough_probate", "hillsborough_divorce", "hillsborough_taxdeed"],
}.items():
    UI_COUNTY_SOURCES[_ui_key] = UI_COUNTY_SOURCES[_ui_key] + _extra

# Attach each Michigan county's probate source to its UI key
# (probate source keys are "<county>_probate"; UI keys are "<county>-mi").
for _probate_key in MI_PROBATE_SCRAPERS:
    _ui_key = _probate_key.replace("_probate", "-mi")
    if _ui_key in UI_COUNTY_SOURCES:
        UI_COUNTY_SOURCES[_ui_key] = UI_COUNTY_SOURCES[_ui_key] + [_probate_key]


ALL_SCRAPERS = {key: source.cls for key, source in SOURCES.items()}
REGION_BY_KEY = {key: source.region for key, source in SOURCES.items()}
SOURCE_METADATA = {key: source.to_public_dict() for key, source in SOURCES.items()}
