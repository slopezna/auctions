"""
Unified Scrapy crawler for real estate auctions in the Valparaíso Region (Chile).

Sources:
  1. Buscaturemate (https://www.buscaturemate.cl/remates/region/valparaiso)
  2. Macal (https://macal.cl/venta)
  3. Tesorería General de la República - TGR (https://remates.tgr.cl/)

Usage:
    pip install scrapy
    python remates_valparaiso_scrapy.py

Output:
    valparaiso_auctions.json (in the current working directory)

Note on dynamic (SPA) sites:
    - Buscaturemate serves server-rendered HTML.
    - Macal may expose embedded JSON (__NEXT_DATA__) or HTML; the spider handles both.
    - TGR (remates.tgr.cl) is a dynamic web app; if the initial HTML returns no rows,
      inspect its Network (XHR/Fetch) tab or integrate scrapy-playwright in TgrValparaisoSpider.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.http import Response

OUTPUT_FILE = Path("valparaiso_auctions.json")

# All 38 municipalities (comunas) of the Valparaíso Region, normalized lowercase ASCII
VALPARAISO_MUNICIPALITIES = frozenset(
    {
        "valparaiso",
        "vina del mar",
        "concon",
        "quilpue",
        "villa alemana",
        "casablanca",
        "quintero",
        "puchuncavi",
        "juan fernandez",
        "san antonio",
        "algarrobo",
        "cartagena",
        "el quisco",
        "el tabo",
        "santo domingo",
        "quillota",
        "la calera",
        "la cruz",
        "hijuelas",
        "nogales",
        "limache",
        "olmue",
        "los andes",
        "calle larga",
        "rinconada",
        "san esteban",
        "san felipe",
        "catemu",
        "llay-llay",
        "llay llay",
        "panquehue",
        "putaendo",
        "santa maria",
        "la ligua",
        "cabildo",
        "papudo",
        "petorca",
        "zapallar",
        "isla de pascua",
        "rapa nui",
    }
)

ACCENT_TRANSLATION = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
WHITESPACE_RE = re.compile(r"\s+")
CASE_ID_RE = re.compile(r"Rol:\s*([A-Za-z]-\d+-\d{4})")
JUDICIAL_ROLE_RE = re.compile(r"Rol Judicial:\s*([A-Za-z]-\d+-\d{4})")
TAX_ROLE_RE = re.compile(r"Rol(?:\s+de\s+aval[úu]o)?:\s*(\d+-\d+)", re.IGNORECASE)
CASE_TITLE_RE = re.compile(r"Caratulado:\s*([^\n]+)")
COURT_RE = re.compile(r"(\d+[°º]?\s*JUZGADO\s+(?:CIVIL|DE\s+LETRAS)[^\n]*)", re.IGNORECASE)
TGR_COURT_RE = re.compile(r"(JUZGADO[^\n.;]+)", re.IGNORECASE)
PRICE_RE = re.compile(r"(UF\s*[\d.,]+|\$\s*[\d.,]+)")
CLP_AMOUNT_RE = re.compile(r"(\$\s*[\d.,]+)")
MACAL_DATE_RE = re.compile(r"Fecha de subasta[:\s]*(\d{1,2}\s+\w+(?:\s+\d{4})?)", re.IGNORECASE)
MACAL_URL_RE = re.compile(r"/venta/([^/]+)/([^/]+)/")
SKIP_SLUG_PREFIXES = ("/remate-vehiculo-", "/remate-auto-", "/remate-bienes-")


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    cleaned = WHITESPACE_RE.sub(" ", text).strip()
    return cleaned.translate(ACCENT_TRANSLATION).lower()


def clean_whitespace(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = WHITESPACE_RE.sub(" ", text).strip()
    return cleaned or None


def is_valparaiso_location(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if "valparaiso" in normalized:
        return True
    return any(municipality in normalized for municipality in VALPARAISO_MUNICIPALITIES)


@dataclass(slots=True)
class AuctionRecord:
    source_portal: str
    listing_url: str
    title: str | None = None
    property_type: str | None = None
    case_type: str | None = None
    address: str | None = None
    municipality: str | None = None
    region: str = "Valparaíso"
    auction_date: str | None = None
    minimum_bid: str | None = None
    minimum_deposit: str | None = None
    court: str | None = None
    case_number: str | None = None
    tax_id: str | None = None
    case_title: str | None = None
    original_source: str | None = None
    auction_link: str | None = None
    notice_text: str | None = None
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BaseRealEstateSpider(scrapy.Spider):
    """Shared extraction helpers for Chilean real estate auction spiders."""

    @staticmethod
    def extract_by_label(response: Response, label: str) -> str | None:
        # Target Spanish DOM labels on the source portals
        xpaths = (
            f'//dt[normalize-space(.)="{label}"]/following-sibling::dd[1]//text()',
            f'//th[normalize-space(.)="{label}"]/following-sibling::td[1]//text()',
            f'//*[normalize-space(text())="{label}"]/following-sibling::*[1]//text()',
        )
        for xp in xpaths:
            value = clean_whitespace(" ".join(response.xpath(xp).getall()))
            if value:
                return value
        return None

    @staticmethod
    def body_text(response: Response) -> str:
        return " ".join(response.xpath("//body//text()").getall())


class BuscaturemateValparaisoSpider(BaseRealEstateSpider):
    name = "buscaturemate_valparaiso"
    allowed_domains = ["buscaturemate.cl", "www.buscaturemate.cl"]
    start_urls = ["https://www.buscaturemate.cl/remates/region/valparaiso"]

    def parse(self, response: Response) -> Generator[Any, None, None]:
        seen_urls: set[str] = set()

        for href in response.css('a[href*="/remates/remate-"]::attr(href)').getall():
            absolute_url = response.urljoin(href)
            if absolute_url in seen_urls:
                continue
            seen_urls.add(absolute_url)

            if any(prefix in absolute_url.lower() for prefix in SKIP_SLUG_PREFIXES):
                continue

            yield scrapy.Request(url=absolute_url, callback=self.parse_detail)

        next_page = (
                response.xpath('//a[contains(normalize-space(.), "Siguiente")]/@href').get()
                or response.css('a[rel="next"]::attr(href)').get()
        )
        if next_page:
            yield response.follow(next_page, callback=self.parse)

    def parse_detail(self, response: Response) -> Generator[dict[str, Any], None, None]:
        category = self.extract_by_label(response, "Categoría") or ""
        property_type = self.extract_by_label(response, "Tipo inmueble")

        if category and "inmueble" not in normalize_text(category) and not property_type:
            return

        full_text = self.body_text(response)
        case_match = CASE_ID_RE.search(full_text)
        case_title_match = CASE_TITLE_RE.search(full_text)
        court_match = COURT_RE.search(full_text)
        notice_nodes = response.xpath(
            '//*[contains(normalize-space(.), "Texto del aviso")]/following-sibling::*[1]//text()'
        ).getall()

        record = AuctionRecord(
            source_portal="Buscaturemate",
            listing_url=response.url,
            title=clean_whitespace(response.css("h1::text").get())
                  or self.extract_by_label(response, "Título"),
            property_type=property_type,
            case_type=self.extract_by_label(response, "Tipo de caso"),
            address=self.extract_by_label(response, "Dirección inmueble"),
            municipality=self.extract_by_label(response, "Comuna inmueble"),
            region=self.extract_by_label(response, "Región inmueble") or "Valparaíso",
            auction_date=self.extract_by_label(response, "Fecha"),
            minimum_bid=self.extract_by_label(response, "Precio mínimo"),
            minimum_deposit=self.extract_by_label(response, "Garantía mínima"),
            court=clean_whitespace(court_match.group(1)) if court_match else None,
            case_number=case_match.group(1) if case_match else None,
            case_title=clean_whitespace(case_title_match.group(1)) if case_title_match else None,
            original_source=self.extract_by_label(response, "Fuente"),
            auction_link=self.extract_by_label(response, "Link"),
            notice_text=clean_whitespace(" ".join(notice_nodes)),
        )
        yield record.to_dict()


class MacalValparaisoSpider(BaseRealEstateSpider):
    name = "macal_valparaiso"
    allowed_domains = ["macal.cl", "www.macal.cl"]
    start_urls = [
        "https://macal.cl/venta",
        "https://macal.cl/venta/casa",
        "https://macal.cl/venta/departamento",
        "https://macal.cl/venta/terreno",
    ]

    def parse(self, response: Response) -> Generator[Any, None, None]:
        property_urls = self._extract_next_data_urls(response) | self._extract_html_urls(response)
        for property_url in sorted(property_urls):
            yield scrapy.Request(url=property_url, callback=self.parse_property)

    def _extract_next_data_urls(self, response: Response) -> set[str]:
        urls: set[str] = set()
        raw_next_data = response.css("script#__NEXT_DATA__::text").get()
        if not raw_next_data:
            return urls

        try:
            payload = json.loads(raw_next_data)
        except json.JSONDecodeError:
            return urls

        for item in self._iter_property_dicts(payload):
            raw_url = item.get("url") or item.get("slug")
            if not raw_url:
                continue
            absolute_url = raw_url if raw_url.startswith("http") else response.urljoin(raw_url)
            municipality = item.get("comuna") or item.get("commune") or ""
            region = item.get("region") or ""
            if (
                    is_valparaiso_location(municipality)
                    or is_valparaiso_location(region)
                    or is_valparaiso_location(absolute_url)
            ):
                urls.add(absolute_url)
        return urls

    @staticmethod
    def _extract_html_urls(response: Response) -> set[str]:
        urls: set[str] = set()
        for href in response.css('a[href*="/venta/"]::attr(href)').getall():
            absolute_url = response.urljoin(href)
            segments = [segment for segment in absolute_url.split("/") if segment]
            if len(segments) >= 6 and is_valparaiso_location(absolute_url):
                urls.add(absolute_url)
        return urls

    def _iter_property_dicts(self, node: Any) -> Generator[dict[str, Any], None, None]:
        if isinstance(node, dict):
            if any(key in node for key in ("comuna", "commune", "precioMinimo", "minimumPrice")):
                yield node
            for value in node.values():
                yield from self._iter_property_dicts(value)
        elif isinstance(node, list):
            for element in node:
                yield from self._iter_property_dicts(element)

    def parse_property(self, response: Response) -> Generator[dict[str, Any], None, None]:
        title = clean_whitespace(response.css("h1::text").get())
        description = clean_whitespace(
            " ".join(response.xpath('//*[contains(text(),"Descripción")]/following::p[1]//text()').getall())
        )
        full_text = self.body_text(response)

        url_match = MACAL_URL_RE.search(response.url)
        property_type = url_match.group(1).capitalize() if url_match else None
        municipality = url_match.group(2).replace("-", " ").title() if url_match else None

        price_match = PRICE_RE.search(full_text)
        date_match = MACAL_DATE_RE.search(full_text)

        record = AuctionRecord(
            source_portal="Macal",
            listing_url=response.url,
            title=title,
            property_type=property_type,
            case_type="Private / Bank / Insolvency Auction",
            address=title,
            municipality=municipality,
            auction_date=clean_whitespace(date_match.group(1)) if date_match else None,
            minimum_bid=price_match.group(1) if price_match else None,
            original_source="Macal",
            auction_link=response.url,
            notice_text=description,
        )
        yield record.to_dict()


class TgrValparaisoSpider(BaseRealEstateSpider):
    name = "tgr_valparaiso"
    allowed_domains = ["remates.tgr.cl"]
    start_urls = ["https://remates.tgr.cl/"]

    def parse(self, response: Response) -> Generator[dict[str, Any], None, None]:
        blocks = response.css("table tbody tr, .card, .remate-item")
        for block in blocks:
            block_text = clean_whitespace(" ".join(block.css("::text").getall())) or ""
            if not is_valparaiso_location(block_text):
                continue

            judicial_match = JUDICIAL_ROLE_RE.search(block_text)
            tax_match = TAX_ROLE_RE.search(block_text)
            amount_match = CLP_AMOUNT_RE.search(block_text)
            court_match = TGR_COURT_RE.search(block_text)

            record = AuctionRecord(
                source_portal="TGR (remates.tgr.cl)",
                listing_url=response.url,
                title="TGR Real Estate Tax Auction",
                property_type="Real Estate",
                case_type="Treasury Tax Collection (TGR)",
                minimum_bid=amount_match.group(1) if amount_match else None,
                court=clean_whitespace(court_match.group(1)) if court_match else None,
                case_number=judicial_match.group(1) if judicial_match else None,
                tax_id=tax_match.group(1) if tax_match else None,
                original_source="Tesorería General de la República",
                auction_link=response.url,
                notice_text=block_text,
            )
            yield record.to_dict()


def build_scrapy_settings(output_file: Path) -> dict[str, Any]:
    return {
        "USER_AGENT": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "ROBOTSTXT_OBEY": False,
        "DOWNLOAD_DELAY": 1.0,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 4,
        "FEED_EXPORT_ENCODING": "utf-8",
        "FEEDS": {
            str(output_file): {
                "format": "json",
                "encoding": "utf8",
                "indent": 2,
                "overwrite": True,
            }
        },
        "LOG_LEVEL": "INFO",
    }


def main() -> None:
    if OUTPUT_FILE.exists():
        OUTPUT_FILE.unlink()

    process = CrawlerProcess(settings=build_scrapy_settings(OUTPUT_FILE))
    for spider_cls in (
            BuscaturemateValparaisoSpider,
            MacalValparaisoSpider,
            TgrValparaisoSpider,
    ):
        process.crawl(spider_cls)

    process.start()
    print(f"Export completed: {OUTPUT_FILE.resolve()}")


if __name__ == "__main__":
    main()
