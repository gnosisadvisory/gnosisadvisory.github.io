#!/usr/bin/env python3
"""
market_sizing.py — Market Sizing Analysis Tool
Gnosis Advisory Ltd

Usage:
    python market_sizing.py --company "Wagestream" --country "UK" \
        --description "financial wellbeing platform that lets employees access earned wages early" \
        --sic "62012"
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from urllib.parse import urlencode, urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4 lxml")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Global configuration
# ---------------------------------------------------------------------------
REQUEST_DELAY = 2.5
_last_request_time: float = 0.0

# Bing block-avoidance: pause for ~15 s (with jitter) after every N searches.
_BING_SEARCH_COUNT = 0
_BING_PAUSE_EVERY = 5       # searches between long pauses
_BING_PAUSE_BASE = 15.0     # seconds (jitter ±3 s added below)

# Rotate through realistic browser user-agents to reduce block rate.
# A new agent is picked randomly for each search session.
import random as _random
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

HEADERS = {
    "User-Agent": _random.choice(_USER_AGENTS)
}

def _fresh_headers() -> dict:
    """Return headers with a freshly picked user-agent."""
    return {**HEADERS, "User-Agent": _random.choice(_USER_AGENTS)}

COMPANIES_HOUSE_API_KEY = os.environ.get("COMPANIES_HOUSE_API_KEY", "")
PAPPERS_API_KEY = os.environ.get("PAPPERS_API_KEY", "")

CH_BASE = "https://api.company-information.service.gov.uk"
OC_BASE = "https://api.opencorporates.com/v0.4"
PAPPERS_BASE = "https://api.pappers.fr/v2"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg: str):
    print(msg, flush=True)

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

# ---------------------------------------------------------------------------
# MODULE 1 — HTTP utilities
# ---------------------------------------------------------------------------
def _rate_limit():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    delay = REQUEST_DELAY + _random.uniform(-0.5, 1.0)  # jitter ±~0.75 s
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_request_time = time.time()

def fetch_url(url: str, params=None, headers=None, method="GET",
              data=None, retries=3, auth=None) -> "requests.Response | None":
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    for attempt in range(retries):
        try:
            _rate_limit()
            if method == "POST":
                resp = requests.post(url, params=params, headers=h,
                                     data=data, auth=auth, timeout=20)
            else:
                resp = requests.get(url, params=params, headers=h,
                                    auth=auth, timeout=20)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** (attempt + 1)
                log(f"  [{ts()}] HTTP {resp.status_code} from {url} — retrying in {wait}s")
                time.sleep(wait)
                continue
            return resp
        except Exception as e:
            wait = 2 ** (attempt + 1)
            log(f"  [{ts()}] Request error ({e}) for {url} — retrying in {wait}s")
            time.sleep(wait)
    log(f"  [{ts()}] FAILED after {retries} attempts: {url}")
    return None

def search_web(query: str, num_results: int = 10) -> list:
    """Search via Bing (primary). DDG consistently returns HTTP 202 CAPTCHA block."""
    results = _search_bing(query, num_results)
    if not results:
        results = _search_duckduckgo(query, num_results)
    return results

def _search_duckduckgo(query: str, num_results: int = 10) -> list:
    try:
        _rate_limit()
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={
                **_fresh_headers(),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-GB,en;q=0.9",
            },
            timeout=20,
            allow_redirects=True,
        )
        if not resp or resp.status_code != 200:
            log(f"    DDG status: {resp.status_code if resp else 'no response'}")
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        results = []
        # Try multiple selector patterns (DDG occasionally changes their HTML)
        selectors = [
            (".result__title a", ".result__snippet"),
            (".result__a", ".result__snippet"),
            ("h2.result__title a", ".result__snippet"),
            ("a.result__url", None),
        ]
        for title_sel, snippet_sel in selectors:
            items = soup.select(title_sel)
            if items:
                for item in items[:num_results]:
                    title = item.get_text(strip=True)
                    url = item.get("href", "")
                    snippet = ""
                    if snippet_sel:
                        # Walk up to containing result div and find snippet
                        parent = item.find_parent(class_=re.compile(r"result"))
                        if parent:
                            sn = parent.select_one(snippet_sel)
                            if sn:
                                snippet = sn.get_text(strip=True)
                    if url and title and not title.startswith("http"):
                        results.append({"title": title, "url": url, "snippet": snippet})
                break
        return results
    except Exception as e:
        log(f"    DDG error: {e}")
        return []

def _search_bing(query: str, num_results: int = 10) -> list:
    """Bing web search (HTML scrape, no API key required)."""
    global _BING_SEARCH_COUNT
    # Pause every N searches to avoid Bing rate-limiting / CAPTCHA blocks.
    _BING_SEARCH_COUNT += 1
    if _BING_SEARCH_COUNT % _BING_PAUSE_EVERY == 0:
        pause = _BING_PAUSE_BASE + _random.uniform(-3.0, 3.0)
        log(f"  [{ts()}] Bing cooldown after {_BING_SEARCH_COUNT} searches — sleeping {pause:.1f}s")
        time.sleep(pause)
    try:
        _rate_limit()
        params = {"q": query, "count": num_results, "setlang": "en-GB"}
        resp = requests.get(
            "https://www.bing.com/search",
            params=params,
            headers={
                **_fresh_headers(),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-GB,en;q=0.9",
            },
            timeout=20,
        )
        if not resp or resp.status_code != 200:
            return []
        # Detect Bing CAPTCHA / block page (returns 200 but no real results).
        if re.search(r'id=["\']captcha|recaptcha|g-recaptcha|challenge|'
                     r'automated.{0,30}request|unusual.{0,30}traffic',
                     resp.text[:4000], re.I):
            log(f"  [{ts()}] Bing CAPTCHA detected — falling back to DDG")
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        # Decompose ALL known ad containers from the DOM before selecting results.
        # Bing changes ad class names periodically; we cast a wide net.
        for ad_el in soup.select(
            "#b_tpad, .b_adTop, .b_adBottom, .b_ad, .b_adLastChild, "
            "#b_ad, .b_adcenterb, .b_adcenter, .b_adSlug, "
            "[data-tag='RelatedSearchesBelow'], [data-bm='ads'], "
            "[aria-label='Ads'], [aria-label='Advertisement']"
        ):
            ad_el.decompose()
        # Scope to #b_results to avoid any remaining stray elements
        container = soup.select_one("#b_results") or soup
        results = []
        for item in container.select("li.b_algo")[:num_results]:
            # Secondary ad check: some sponsored results survive container removal.
            # Reject items that have an explicit "Ad" or "Sponsored" label.
            item_text = item.get_text(" ", strip=True)
            if re.search(r'\bSponsored\b|\bAdvertisement\b', item_text):
                continue
            # Also reject if the visible label element contains just "Ad"
            ad_label = item.select_one(".b_adSlug, .b_tpcn, [data-bm]")
            if ad_label and re.fullmatch(r'Ad', ad_label.get_text(strip=True)):
                continue

            title_el = item.select_one("h2 a")
            snippet_el = item.select_one(".b_caption p, .b_algoSlug")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            # Bing h2 a href is a /ck/a? redirect — use cite for the real URL
            cite_el = item.select_one("cite, .b_displayUrl, .b_attribution cite")
            if cite_el:
                cite_text = cite_el.get_text(strip=True)
                # Bing shows breadcrumbs like "domain › path › sub" — keep only domain
                if '›' in cite_text:
                    cite_text = cite_text.split('›')[0].strip()
                url = cite_text if "://" in cite_text else "https://" + cite_text
            else:
                href = title_el.get("href", "")
                # Reject Bing ad-click URLs (aclick, adclick, etc.)
                if re.search(r'bing\.com/a(?:d)?click', href, re.I):
                    continue
                u_match = re.search(r'[?&]u=a1([A-Za-z0-9_\-]+)', href)
                if u_match:
                    try:
                        import base64
                        url = base64.urlsafe_b64decode(
                            u_match.group(1) + "==").decode("utf-8", errors="ignore")
                    except Exception:
                        url = href
                else:
                    url = href
            # Final URL-level ad guard: skip tracking/ad-network domains
            if re.search(r'doubleclick\.net|googleadservices|bing\.com/aclick', url, re.I):
                continue
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            if url and title:
                results.append({"title": title, "url": url, "snippet": snippet})
        return results
    except Exception as e:
        log(f"    Bing error: {e}")
        return []

# ---------------------------------------------------------------------------
# MODULE 2 — Country detection & registry routing
# ---------------------------------------------------------------------------
UK_VARIANTS = {"uk", "united kingdom", "england", "scotland", "wales",
               "great britain", "gb", "northern ireland"}
DE_VARIANTS = {"germany", "deutschland", "de", "german"}
FR_VARIANTS = {"france", "fr", "french"}

def detect_registry_mode(country: str) -> dict:
    c = country.lower().strip()
    if c in UK_VARIANTS:
        return {"mode": "A", "country_code": "GB", "language": "English",
                "description": "UK — Companies House API"}
    if c in DE_VARIANTS:
        return {"mode": "B", "country_code": "DE", "language": "German",
                "description": "Germany — Unternehmensregister / North Data"}
    if c in FR_VARIANTS:
        return {"mode": "C", "country_code": "FR", "language": "French",
                "description": "France — Pappers API (NAF codes)"}
    # Try OpenCorporates ping for mode D
    iso = _country_to_iso(c)
    if iso and _oc_has_coverage(iso):
        return {"mode": "D", "country_code": iso, "language": _country_language(c),
                "description": f"{country} — OpenCorporates registry"}
    return {"mode": "E", "country_code": iso or country.upper()[:2],
            "language": _country_language(c),
            "description": f"{country} — Web-only mode (no accessible public registry)"}

def _country_to_iso(country: str) -> str:
    mapping = {
        "usa": "US", "united states": "US", "us": "US", "america": "US",
        "australia": "AU", "au": "AU", "canada": "CA", "ca": "CA",
        "ireland": "IE", "ie": "IE", "netherlands": "NL", "nl": "NL",
        "sweden": "SE", "se": "SE", "norway": "NO", "no": "NO",
        "denmark": "DK", "dk": "DK", "spain": "ES", "es": "ES",
        "italy": "IT", "it": "IT", "poland": "PL", "pl": "PL",
        "switzerland": "CH", "ch": "CH", "belgium": "BE", "be": "BE",
        "austria": "AT", "at": "AT", "portugal": "PT", "pt": "PT",
        "brazil": "BR", "br": "BR", "india": "IN", "in": "IN",
        "singapore": "SG", "sg": "SG", "japan": "JP", "jp": "JP",
        "south africa": "ZA", "za": "ZA", "nigeria": "NG", "ng": "NG",
        "venezuela": "VE", "ve": "VE",
    }
    return mapping.get(country.lower(), "")

def _country_language(country: str) -> str:
    mapping = {
        "germany": "German", "deutschland": "German", "de": "German",
        "france": "French", "fr": "French",
        "spain": "Spanish", "es": "Spanish",
        "italy": "Italian", "it": "Italian",
        "portugal": "Portuguese", "pt": "Portuguese",
        "brazil": "Portuguese", "br": "Portuguese",
        "netherlands": "Dutch", "nl": "Dutch",
        "sweden": "Swedish", "se": "Swedish",
        "norway": "Norwegian", "no": "Norwegian",
        "denmark": "Danish", "dk": "Danish",
        "poland": "Polish", "pl": "Polish",
        "venezuela": "Spanish", "ve": "Spanish",
    }
    return mapping.get(country.lower(), "English")

def _oc_has_coverage(iso: str) -> bool:
    try:
        _rate_limit()
        resp = requests.get(f"{OC_BASE}/jurisdictions",
                            headers=HEADERS, timeout=10)
        if resp and resp.status_code == 200:
            data = resp.json()
            juris = data.get("results", {}).get("jurisdictions", [])
            codes = [j.get("jurisdiction_code", "").split("_")[0].upper() for j in juris]
            return iso.upper() in codes
    except Exception:
        pass
    return False

# ---------------------------------------------------------------------------
# MODULE 3A — Companies House
# ---------------------------------------------------------------------------
def _ch_auth():
    if COMPANIES_HOUSE_API_KEY:
        return (COMPANIES_HOUSE_API_KEY, "")
    return None

def ch_search_by_sic_public(sic_code: str, max_results: int = 100) -> list:
    """
    Scrape the public Companies House advanced-search website for a SIC code.
    No API key required. Paginates until max_results reached or no more pages.
    """
    base_url = ("https://find-and-update.company-information.service.gov.uk"
                "/advanced-search/results")
    companies: list = []
    start_index = 0
    page_size = 20  # CH renders 20 results per page
    while len(companies) < max_results:
        params = {"sicCodes": sic_code, "status": "active",
                  "startIndex": start_index}
        resp = fetch_url(base_url, params=params, headers={
            **HEADERS,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-GB,en;q=0.9",
        })
        if not resp or resp.status_code != 200:
            log(f"  [{ts()}] CH public search HTTP "
                f"{resp.status_code if resp else 'error'}")
            break
        soup = BeautifulSoup(resp.text, "lxml")
        # CH uses GOV.UK design — every company link looks like /company/XXXXXXXX
        seen: set = set()
        page_companies: list = []
        for link in soup.select("a[href*='/company/']"):
            href = link.get("href", "")
            m = re.search(r'/company/([A-Z0-9]{6,8})(?:/|$)', href)
            if not m:
                continue
            number = m.group(1)
            if number in seen:
                continue
            seen.add(number)
            name = link.get_text(strip=True)
            if name:
                page_companies.append({
                    "name": name,
                    "company_number": number,
                    "sic_codes": [sic_code],
                    "status": "active",
                })
        if not page_companies:
            break
        companies.extend(page_companies)
        log(f"  [{ts()}] CH public: startIndex={start_index} "
            f"→ {len(page_companies)} companies")
        if len(page_companies) < page_size:
            break  # last page
        start_index += page_size
    log(f"  [{ts()}] CH public scraper total: {len(companies)} companies")
    return companies[:max_results]


def ch_search_by_sic(sic_code: str, max_results: int = 100) -> list:
    log(f"  [{ts()}] Companies House: searching SIC {sic_code}")
    # ── Primary: authenticated REST API (requires COMPANIES_HOUSE_API_KEY) ──
    if COMPANIES_HOUSE_API_KEY:
        resp = fetch_url(f"{CH_BASE}/advanced-search/companies",
                         params={"sic_codes": sic_code,
                                 "company_status": "active",
                                 "size": min(max_results, 100)},
                         auth=_ch_auth())
        if resp and resp.status_code == 200:
            try:
                items = resp.json().get("items", [])
                results = []
                for item in items:
                    results.append({
                        "name": item.get("company_name", ""),
                        "company_number": item.get("company_number", ""),
                        "sic_codes": item.get("sic_codes", []),
                        "date_of_creation": item.get("date_of_creation", ""),
                        "address": item.get("registered_office_address", {}),
                        "status": item.get("company_status", ""),
                    })
                log(f"  [{ts()}] CH API: {len(results)} companies for SIC {sic_code}")
                return results
            except Exception as e:
                log(f"  [{ts()}] CH API parse error: {e}")
        else:
            log(f"  [{ts()}] CH API HTTP {resp.status_code if resp else 'error'}"
                f" — falling back to public website scraper")
    else:
        log(f"  [{ts()}] No COMPANIES_HOUSE_API_KEY — using public website scraper"
            f" (set env var for faster/fuller results)")
    # ── Fallback: public website (no key needed) ──
    return ch_search_by_sic_public(sic_code, max_results)

def ch_get_company(company_number: str) -> dict:
    resp = fetch_url(f"{CH_BASE}/company/{company_number}", auth=_ch_auth())
    if resp and resp.status_code == 200:
        try:
            return resp.json()
        except Exception:
            pass
    return {}

def ch_search_company_name(name: str) -> list:
    resp = fetch_url(f"{CH_BASE}/search/companies",
                     params={"q": name, "items_per_page": 5},
                     auth=_ch_auth())
    if resp and resp.status_code == 200:
        try:
            return resp.json().get("items", [])
        except Exception:
            pass
    return []

def _scrape_company_number_from_website(website: str) -> str:
    """
    Scrape a company's website to find their Companies House registration number.
    UK companies almost always include it in footer, About, or T&Cs:
      "Company number: 01234567"  /  "Registered in England No. 01234567"
    Returns the 8-digit (zero-padded) number string, or "" if not found.
    """
    # Bail out immediately on non-parseable "URLs" (e.g. Bing breadcrumbs with ›)
    if not website or '›' in website or ' ' in website.split('/')[0]:
        return ""

    pages_to_try = [
        f"https://{website}",
        f"https://{website}/about",
        f"https://{website}/about-us",
        f"https://{website}/contact",
        f"https://{website}/terms",
        f"https://{website}/terms-and-conditions",
        f"https://{website}/legal",
        f"https://{website}/privacy-policy",
    ]
    # Pattern for UK company numbers: exactly 8 digits, or 2 letters + 6 digits (e.g. SC123456)
    ch_pattern = re.compile(
        r'(?:company\s+(?:number|no\.?|registration\s+(?:number|no\.?))'
        r'|registered\s+(?:number|no\.?|in\s+england[^0-9]{0,30}no\.?)'
        r'|co\.?\s*(?:number|no\.?)'
        r'|companies\s+house\s+(?:number|no\.?))'
        r'\s*[:\-]?\s*([A-Z]{0,2}\d{6,8})',
        re.IGNORECASE
    )
    for url in pages_to_try:
        resp = fetch_url(url)
        if not resp or resp.status_code != 200:
            continue
        try:
            soup = BeautifulSoup(resp.text, "lxml")
            text = soup.get_text(separator=" ", strip=True)
            m = ch_pattern.search(text)
            if m:
                num = m.group(1).strip().zfill(8)
                log(f"    Found company number {num} on {url}")
                return num
        except Exception:
            continue
    return ""

def ch_get_financials(company_number: str) -> list:
    """Get financial data from Companies House filing history."""
    resp = fetch_url(f"{CH_BASE}/company/{company_number}/filing-history",
                     params={"category": "accounts", "items_per_page": 10},
                     auth=_ch_auth())
    if not resp or resp.status_code != 200:
        return []
    try:
        filings = resp.json().get("items", [])
    except Exception:
        return []

    snapshots = []
    current_year = datetime.now().year
    for filing in filings[:5]:
        date_str = filing.get("date", "")
        try:
            filing_year = int(date_str[:4]) if date_str else None
        except Exception:
            filing_year = None
        if filing_year and filing_year < current_year - 5:
            continue

        desc = filing.get("description", "").lower()
        abbreviated = any(w in desc for w in ["abbreviated", "micro", "small company"])

        snap = {
            "year": filing_year,
            "turnover": None, "operating_profit": None, "net_profit": None,
            "total_assets": None, "net_assets": None, "employees": None,
            "trade_debtors": None, "trade_creditors": None, "staff_costs": None,
            "deferred_income": None, "fixed_assets": None, "director_emoluments": None,
            "abbreviated": abbreviated,
            "filing_date": date_str,
            "source_url": f"https://find-and-update.company-information.service.gov.uk/company/{company_number}/filing-history",
        }

        # Try to get document links and fetch XBRL/iXBRL data
        links = filing.get("links", {})
        doc_url = links.get("document_metadata", "")
        if doc_url:
            doc_resp = fetch_url(doc_url, auth=_ch_auth())
            if doc_resp and doc_resp.status_code == 200:
                try:
                    doc_data = doc_resp.json()
                    resources = doc_data.get("resources", {})
                    for mime, meta in resources.items():
                        if "xhtml" in mime or "xml" in mime:
                            doc_dl = meta.get("links", {}).get("download", "")
                            if doc_dl:
                                xbrl_resp = fetch_url(doc_dl, auth=_ch_auth())
                                if xbrl_resp and xbrl_resp.status_code == 200:
                                    _parse_xbrl_into_snap(xbrl_resp.text, snap)
                except Exception as xe:
                    log(f"    XBRL parse error: {xe}")

        snapshots.append(snap)

    return snapshots

def _parse_xbrl_into_snap(xbrl_text: str, snap: dict):
    """Parse iXBRL/XBRL content to extract financial values."""
    try:
        soup = BeautifulSoup(xbrl_text, "lxml")
        tag_map = {
            "turnover": ["turnover", "revenue", "Turnover", "Revenue",
                         "ix:nonfraction[name*='Turnover']",
                         "ix:nonfraction[name*='Revenue']"],
        }
        # Generic numeric tag extraction
        for tag in soup.find_all(["ix:nonfraction", "xbrli:measure"]):
            name = (tag.get("name", "") or "").lower()
            val_str = tag.get_text(strip=True).replace(",", "").replace("£", "")
            try:
                val = float(val_str)
            except Exception:
                continue
            sign_attr = tag.get("sign", "")
            if sign_attr == "-":
                val = -val
            scale = tag.get("scale", "0")
            try:
                val = val * (10 ** int(scale))
            except Exception:
                pass

            if any(k in name for k in ["turnover", "revenue", "sales"]):
                if snap["turnover"] is None:
                    snap["turnover"] = val
            elif any(k in name for k in ["operatingprofit", "operatingloss"]):
                if snap["operating_profit"] is None:
                    snap["operating_profit"] = val
            elif any(k in name for k in ["profitloss", "netprofit"]):
                if snap["net_profit"] is None:
                    snap["net_profit"] = val
            elif any(k in name for k in ["totalassets"]):
                if snap["total_assets"] is None:
                    snap["total_assets"] = val
            elif any(k in name for k in ["netassets", "equity", "shareholdersfunds"]):
                if snap["net_assets"] is None:
                    snap["net_assets"] = val
            elif any(k in name for k in ["employees", "averagenumberofemployees"]):
                if snap["employees"] is None:
                    snap["employees"] = int(val)
            elif any(k in name for k in ["tradedebtors", "tradereceivables"]):
                if snap["trade_debtors"] is None:
                    snap["trade_debtors"] = val
            elif any(k in name for k in ["tradecreditors", "tradepayables"]):
                if snap["trade_creditors"] is None:
                    snap["trade_creditors"] = val
            elif any(k in name for k in ["staffcosts", "wagessalaries"]):
                if snap["staff_costs"] is None:
                    snap["staff_costs"] = val
            elif any(k in name for k in ["deferredincome", "contractliabilities"]):
                if snap["deferred_income"] is None:
                    snap["deferred_income"] = val
            elif any(k in name for k in ["tangibleassets", "fixedassets", "propertyplant"]):
                if snap["fixed_assets"] is None:
                    snap["fixed_assets"] = val
            elif any(k in name for k in ["directoremoluments", "directorremuneration"]):
                if snap["director_emoluments"] is None:
                    snap["director_emoluments"] = val
    except Exception as e:
        pass

# ---------------------------------------------------------------------------
# MODULE 3B — OpenCorporates
# ---------------------------------------------------------------------------
def oc_search_companies(name: str, jurisdiction: str = None) -> list:
    params = {"q": name, "per_page": 10}
    if jurisdiction:
        params["jurisdiction_code"] = jurisdiction.lower()
    resp = fetch_url(f"{OC_BASE}/companies/search", params=params)
    if resp and resp.status_code == 200:
        try:
            companies = resp.json().get("results", {}).get("companies", [])
            return [c.get("company", {}) for c in companies]
        except Exception:
            pass
    return []

def oc_get_company(jurisdiction_code: str, company_number: str) -> dict:
    resp = fetch_url(f"{OC_BASE}/companies/{jurisdiction_code}/{company_number}")
    if resp and resp.status_code == 200:
        try:
            return resp.json().get("results", {}).get("company", {})
        except Exception:
            pass
    return {}

# ---------------------------------------------------------------------------
# MODULE 3C — Pappers (France)
# ---------------------------------------------------------------------------
def pappers_search_by_naf(naf_code: str, max_results: int = 50) -> list:
    params = {"code_naf": naf_code, "statut": "A", "par_page": max_results}
    if PAPPERS_API_KEY:
        params["api_token"] = PAPPERS_API_KEY
    resp = fetch_url(f"{PAPPERS_BASE}/recherche-entreprises", params=params)
    if resp and resp.status_code == 200:
        try:
            return resp.json().get("resultats", [])
        except Exception:
            pass
    return []

def pappers_get_company(siren: str) -> dict:
    params = {"siren": siren}
    if PAPPERS_API_KEY:
        params["api_token"] = PAPPERS_API_KEY
    resp = fetch_url(f"{PAPPERS_BASE}/entreprise", params=params)
    if resp and resp.status_code == 200:
        try:
            return resp.json()
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# MODULE 4 — Keyword / sector extraction helpers
# ---------------------------------------------------------------------------
COMPANY_SUFFIXES = re.compile(
    r'\b(Ltd|Limited|Inc|Incorporated|Corp|Corporation|PLC|LLP|GmbH|'
    r'SAS|SA|AG|BV|NV|Oy|AB|AS|SRL|SARL|SpA|Sdn\s*Bhd|Pte\s*Ltd|'
    r'LLC|LP|Co\b|Company)\b', re.IGNORECASE
)
STOP_WORDS = {"the", "and", "for", "with", "from", "that", "this", "have",
              "will", "are", "not", "but", "can", "all", "any", "been",
              "its", "also", "such", "into", "than", "then", "more", "over"}

def extract_sector(description: str) -> str:
    """Extract 2-3 key sector nouns from a company description."""
    words = re.findall(r'\b[a-zA-Z]{4,}\b', description)
    keywords = [w.lower() for w in words if w.lower() not in STOP_WORDS]
    # Prefer nouns that appear to be domain-specific
    sector_words = [w for w in keywords if w not in
                    {"company", "provides", "offers", "platform", "service",
                     "services", "solution", "solutions", "software", "helps",
                     "enables", "allows", "businesses", "customers", "users"}]
    return " ".join(sector_words[:3]) if sector_words else " ".join(keywords[:3])

def extract_company_names_from_text(text: str) -> list:
    """Extract potential company names from text — suffixed names AND proper nouns."""
    # --- Pre-clean ---
    # Remove breadcrumb separators (Bing/DDG cite format: "Site.com › Page › Sub")
    text = re.sub(r'[›»]\s*\S+', ' ', text)
    # Remove raw URLs and domain-like tokens to stop "baidu.com" → "Baidu"
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'\b\S+\.(com|co\.uk|org|net|io|biz|info)\b', ' ', text, flags=re.I)

    names = []

    # Pattern 1: Names with explicit company suffixes (most reliable)
    suffix_pattern = re.compile(
        r'([A-Z][a-zA-Z0-9&\-\'\s]{1,40}?\s+'
        r'(?:Ltd|Limited|Inc|Incorporated|Corp|Corporation|PLC|LLP|'
        r'GmbH|SAS|SA|AG|BV|LLC|LP|Co\b|Group\b))\b'
    )
    for m in suffix_pattern.findall(text):
        name = m.strip()
        if 3 < len(name) < 80:
            names.append(name)

    # Words that commonly start sentences or appear capitalised for grammar reasons,
    # NOT because they are company names.  Checked per-word (not full-name) so that
    # "United Kingdom" is rejected because "United" appears here.
    _SENTENCE_STARTERS = {
        "the", "this", "these", "that", "there", "they", "their", "them",
        "our", "your", "its", "and", "but", "for", "with", "from", "into",
        "since", "among", "small", "large", "many", "most", "some", "each",
        "both", "other", "another", "such", "more", "also", "when", "where",
        "which", "what", "how", "why", "who", "whom", "whose",
        "united", "british", "english", "scottish", "welsh", "irish",
        "international", "national", "global", "local", "regional",
        "new", "old", "top", "key", "main", "major", "leading", "based",
        "market", "sector", "industry", "business", "company", "companies",
        "product", "service", "customer", "consumer", "employee", "staff",
        "brand", "store", "online", "digital", "financial", "retail",
        "monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday", "sunday",
        "january", "february", "march", "april", "june", "july",
        "august", "september", "october", "november", "december",
        "london", "manchester", "birmingham", "edinburgh", "glasgow",
        "kingdom", "england", "scotland", "wales", "ireland",
    }

    # If a name contains ANY of these words it is a descriptor, not a company
    _NOISE_WORDS_IN_NAME = {
        "competitors", "competitor", "alternatives", "alternative",
        "directory", "guide", "list", "review", "ranking", "comparison",
        "analysis", "report", "overview", "landscape", "market",
        "franchise", "since", "among", "investopedia", "wikipedia",
        "crunchbase", "statista", "similar", "versus",
    }

    # Pattern 2: Sequences of 1-4 capitalised words
    proper_noun_pattern = re.compile(
        r'\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,3})\b'
    )
    for m in proper_noun_pattern.finditer(text):
        name = m.group(1).strip()
        name_words = name.lower().split()
        # Reject if the first word is a common sentence starter / non-name word
        if name_words[0] in _SENTENCE_STARTERS:
            continue
        # Reject if ANY word in the name is a noise descriptor
        if any(w in _NOISE_WORDS_IN_NAME for w in name_words):
            continue
        if 4 < len(name) < 60 and not name.isupper():
            names.append(name)

    return list(set(names))

# Domains that can never produce real B2B competitors — skip entirely
_NOISE_DOMAINS = {
    "microsoft.com", "apple.com", "google.com", "bing.com", "yahoo.com",
    "amazon.com", "amazon.co.uk", "ebay.com", "ebay.co.uk",
    "bbc.co.uk", "bbc.com", "theguardian.com", "ft.com", "telegraph.co.uk",
    "dailymail.co.uk", "independent.co.uk", "mirror.co.uk", "sky.com",
    "wikipedia.org", "wikihow.com",
    "linkedin.com", "indeed.com", "glassdoor.com", "reed.co.uk",
    "totaljobs.com", "cv-library.co.uk", "monster.com",
    "companies-house.gov.uk", "gov.uk", "hmrc.gov.uk", "nhs.uk",
    "crunchbase.com", "bloomberg.com", "reuters.com",
    "youtube.com", "twitter.com", "facebook.com", "instagram.com",
    "reddit.com", "quora.com", "stackoverflow.com",
    "investopedia.com", "statista.com", "ibisworld.com", "mordorintelligence.com",
    "businesswire.com", "prnewswire.com", "techcrunch.com", "forbes.com",
    "wsj.com", "nytimes.com", "theverge.com", "wired.com",
    "hinative.com", "zhihu.com", "zhidao.baidu.com", "baidu.com", "weibo.com",
    "similarweb.com", "semrush.com", "owler.com", "dnb.com",
    "comparably.com", "g2.com", "trustpilot.com", "capterra.com",
    "latterly.org", "comparably.com",
    # Gaming / entertainment platforms that pollute competitor searches
    "elderscrollsonline.com", "steampowered.com", "store.steampowered.com",
    "ign.com", "gamespot.com", "pcgamer.com", "eurogamer.net", "rockpapershotgun.com",
    "nexusmods.com", "curseforge.com", "modrinth.com",
    "twitch.tv", "discord.com", "discordapp.com",
    "bandits-clan.ru",
}

# Domain prefixes that are never legitimate competitors (checked via startswith)
_NOISE_DOMAIN_PREFIXES = ("forums.", "forum.", "community.", "discuss.", "answers.",
                          "support.", "help.", "wiki.", "docs.", "dev.")

# ---------------------------------------------------------------------------
# Sector seed lists — known major players guaranteed in every analysis.
# Keyed by (country_lower, sic_3digit_prefix).  Company numbers are left None
# so the existing CH name-search in collect_financials resolves them at run
# time (avoids hardcoding numbers that can go stale if a company restructures).
# ---------------------------------------------------------------------------
SECTOR_SEEDS: dict = {
    # UK grocery / food retail  (SIC 471xx)
    ("uk", "471"): [
        "J SAINSBURY PLC",
        "ASDA STORES LIMITED",
        "WM MORRISON SUPERMARKETS PLC",
        "LIDL GREAT BRITAIN LIMITED",
        "ALDI STORES LIMITED",
        "MARKS AND SPENCER PLC",
        "WAITROSE LIMITED",
        "ICELAND FOODS LIMITED",
        "OCADO GROUP PLC",
        "CO-OPERATIVE GROUP LIMITED",
        "BOOKER LIMITED",
    ],
    # UK food & drink manufacturing (SIC 107xx / 108xx / 110xx)
    ("uk", "107"): [
        "ASSOCIATED BRITISH FOODS PLC",
        "PREMIER FOODS PLC",
        "GREENCORE GROUP PLC",
        "BAKKAVOR GROUP PLC",
        "CRANSWICK PLC",
    ],
    # UK pubs / bars / restaurants (SIC 561xx)
    ("uk", "561"): [
        "MITCHELLS & BUTLERS PLC",
        "WETHERSPOON (J D) PLC",
        "WHITBREAD PLC",
        "RESTAURANT GROUP PLC",
        "WAGAMAMA LIMITED",
    ],
    # UK software / SaaS (SIC 620xx)
    ("uk", "620"): [
        "SAGE GROUP PLC",
        "MICRO FOCUS INTERNATIONAL PLC",
        "AVEVA GROUP PLC",
        "KAINOS GROUP PLC",
    ],
    # UK financial services / fintech (SIC 641xx / 649xx)
    ("uk", "641"): [
        "LLOYDS BANKING GROUP PLC",
        "NATWEST GROUP PLC",
        "BARCLAYS PLC",
        "HSBC UK BANK PLC",
        "MONZO BANK LIMITED",
        "REVOLUT LIMITED",
        "STARLING BANK LIMITED",
        "TRANSFERWISE LIMITED",
    ],
    # France grocery (NAF 4711)
    ("france", "471"): [
        "CARREFOUR SA",
        "AUCHAN RETAIL FRANCE SAS",
        "LECLERC",
        "INTERMARCHE",
        "LIDL SAS",
        "ALDI MARCHE SAS",
    ],
}

# Words that look like proper nouns but are not company names
_NOT_COMPANY_NAMES = {
    # Tech giants / Big Tech — single-word forms that contaminate snippets
    "microsoft", "apple", "google", "bing", "amazon", "facebook", "twitter",
    "youtube", "linkedin", "windows", "android", "iphone", "ipad",
    "alibaba", "tencent", "samsung", "huawei", "xiaomi", "baidu",
    "netflix", "spotify", "adobe", "oracle", "salesforce", "sap",
    "paypal", "stripe", "shopify", "ebay", "etsy",
    "uber", "airbnb", "booking", "tripadvisor",
    "volkswagen", "toyota", "tesla", "bmw", "mercedes", "honda", "ford",
    # Generic web UI / navigation
    "login", "sign", "register", "search", "menu", "home", "contact",
    "about", "services", "products", "news", "blog", "careers", "jobs",
    "privacy", "cookies", "terms", "sitemap", "help", "support",
    # Common English words mistaken for names
    "cheadle", "gatley", "cheshire", "stockport",
    "curriculum", "exams", "calendar", "timetable", "pastoral", "ethos",
    "values", "vision", "mission", "registered", "incorporated",
    "foundation", "academy", "college", "school", "university",
    "japanese", "french", "english", "spanish", "italian", "german",
    "progression", "assessment", "achievement", "behaviour",
    "sixth", "stage", "year", "form", "class",
    # Generic adjectives / prepositions that appear in longer phrases
    "easy", "anti", "cheat", "sign", "next", "free", "open", "fast",
    "smart", "just", "best", "good", "true", "real", "pure", "core",
    # Component/IT system strings from device info pages
    "identification", "manufacturer", "system", "model", "version",
    "serial", "number", "part", "code", "type", "product",
}

def _is_plausible_company_name(name: str) -> bool:
    """Return True only if name looks like an actual company, not garbage."""
    if not name or len(name) < 4:
        return False
    words = name.lower().split()
    # ALWAYS check noise list first — catches "Apple Pay", "Alibaba Group" etc.
    # This must run before the cap_words short-circuit.
    for w in words:
        if w in _NOT_COMPANY_NAMES:
            return False
    # Reject names that consist entirely of non-business words
    business_word = re.search(
        r'\b(ltd|limited|plc|llp|inc|group|systems|solutions|products|'
        r'manufacturing|industries|services|technologies|supplies|'
        r'hardware|furniture|doors|ironmongery|security|design|fire|'
        r'health|care|safe|secure|ligature|anti)\b',
        name.lower()
    )
    # Multi-word names with at least one capitalised word are usually fine
    cap_words = [w for w in name.split() if w and w[0].isupper()]
    if len(cap_words) >= 2:
        return True
    # Single capitalised word — only keep if it has a business suffix
    if business_word:
        return True
    # Single word, no business suffix, not in noise list — borderline
    # Keep only if it's 5+ chars (e.g. "Allgood", "Dorma", "Hafele")
    return len(name.strip()) >= 5

def _domain_to_company_name(domain: str) -> str:
    """Convert a domain like 'safehingeprimera.com' → 'Safe Hinge Primera'."""
    if not domain:
        return ""
    # Strip TLD and www
    base = re.sub(r'\.(com|co\.uk|uk|net|org|io|biz|info)$', '', domain, flags=re.I)
    base = re.sub(r'^www\.', '', base)
    # Split on hyphens or detect camelCase/wordboundaries in concatenated words
    if '-' in base:
        words = base.split('-')
    else:
        # Try to split concatenated words using a simple capitalisation heuristic
        # e.g. "safehingeprimera" → try common splits (no perfect solution without dict)
        words = [base]
    return ' '.join(w.capitalize() for w in words if w)

def normalise_company_name(name: str) -> str:
    """Strip suffixes and normalise for deduplication."""
    n = COMPANY_SUFFIXES.sub("", name).strip(" .,")
    n = re.sub(r'\s+', ' ', n).lower().strip()
    return n

def deduplicate_companies(companies: list) -> list:
    """Deduplicate by normalised name, keeping richest entry."""
    seen: dict = {}
    for c in companies:
        key = normalise_company_name(c.get("name", ""))
        if not key or len(key) < 2:
            continue
        if key not in seen:
            seen[key] = c
        else:
            # Keep the one with more data
            existing = seen[key]
            if len(str(c)) > len(str(existing)):
                seen[key] = {**existing, **{k: v for k, v in c.items() if v}}
    return list(seen.values())


def _ch_prescreen_sic_results(raw: list, max_fetches: int = 100,
                               quality_cap: int = 4) -> list:
    """
    Fetch the CH company profile for each SIC search result and HARD-FILTER to:
      - company_status == "active" (not dormant / dissolved)
      - accounts.last_accounts.type NOT micro-entity or small-company
      - accounts.last_accounts.made_up_to within the last 2 years

    Account types ranked by desirability:
      TIER_1 (full / group / plc)  — large companies above audit threshold
      TIER_2 (small / total-exemption-full) — small but with real accounts
      DROPPED — micro-entity, dormant, dissolved

    Within quality, TIER_1 companies are returned first.
    Micro-entity and dormant companies are completely discarded.
    Small-company filers are only included to pad if fewer than 5 TIER_1 found.
    Returns at most `quality_cap` companies.
    """
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=730)   # 2-year window

    micro_types  = {"micro-entity", "micro", "micro entity", "no accounts filed"}
    dormant_statuses = {"dormant", "converted-closed", "dissolved"}
    full_types   = {"full", "group", "audit-exemption-subsidiary",
                    "filing-exemption-subsidiary"}

    tier1, tier2, border = [], [], []
    n_micro = n_dormant = fetches = 0

    for comp in raw:
        if len(tier1) >= quality_cap:
            break  # enough large companies found

        reg_num = comp.get("company_number", "") or comp.get("registry_number", "")
        if not reg_num or fetches >= max_fetches:
            border.append(comp)
            continue

        profile = ch_get_company(reg_num)
        fetches += 1

        if not profile:
            border.append(comp)
            continue

        status = profile.get("company_status", "active").lower()
        if any(s in status for s in dormant_statuses):
            n_dormant += 1
            continue   # hard drop

        accts    = profile.get("accounts", {})
        last     = accts.get("last_accounts", {})
        acct_type = last.get("type", "").lower().replace("-", " ").strip()
        made_up_to = last.get("made_up_to", "")

        # Hard drop micro / no-accounts
        if any(mt in acct_type for mt in micro_types) or acct_type == "":
            n_micro += 1
            continue

        is_recent = True
        if made_up_to:
            try:
                is_recent = datetime.strptime(made_up_to[:10], "%Y-%m-%d") >= cutoff
            except ValueError:
                pass

        if not is_recent:
            border.append(comp)   # stale but non-micro — keep only as fallback
            continue

        # Bucket by account tier
        normalised = acct_type.replace(" ", "-")
        if normalised in full_types or "full" in normalised:
            tier1.append(comp)
        else:
            tier2.append(comp)   # small / total-exemption etc.

    # Build result: large-company full filers first, then small filers as padding
    # if we found fewer than 2 tier1. Keep total ≤ quality_cap.
    result = tier1
    if len(result) < 2:
        needed = min(quality_cap - len(result), len(tier2))
        result = tier1 + tier2[:needed]

    log(f"  [{ts()}] CH pre-screen ({fetches} profiles fetched): "
        f"{len(tier1)} full-account (large co.), "
        f"{len(tier2)} small-account, "
        f"{n_micro} micro-entity dropped, "
        f"{n_dormant} dormant dropped — "
        f"returning {len(result)} companies")

    return result


# ---------------------------------------------------------------------------
# MODULE 5 — Competitor Discovery
# ---------------------------------------------------------------------------
def discover_competitors(company: str, country: str, description: str,
                         sic_code: str, registry_mode: dict) -> tuple:
    """
    Run all discovery methods and return (competitors_list, discovery_meta).
    """
    all_companies = []
    discovery_meta = {
        "methods_run": [],
        "counts_by_method": {},
        "warning": None,
    }

    sector = extract_sector(description)

    # ---- Method A: Registry SIC search ----
    log(f"  [{ts()}] Method A: Registry SIC/NAF search")
    method_a_results = []
    if registry_mode["mode"] == "A" and sic_code:
        raw = ch_search_by_sic(sic_code)
        # Pre-screen: sort raw results so quality companies (active, non-micro,
        # accounts filed within 2 years) are processed first. Micro-entity and
        # dormant companies are moved to the end so they don't consume financial-
        # collection budget before the interesting players are processed.
        log(f"  [{ts()}] Pre-screening {len(raw)} SIC results for quality...")
        raw = _ch_prescreen_sic_results(raw)
        for r in raw:
            method_a_results.append({
                "name": r["name"],
                "website": None,
                "registry_number": r.get("company_number", ""),
                "discovery_method": "A_companies_house",
                "description": f"SIC {sic_code} registered company",
                "country": country,
                "raw_data": r,
            })
        # Remove the target company itself
        method_a_results = [c for c in method_a_results
                            if normalise_company_name(c["name"]) != normalise_company_name(company)]
    elif registry_mode["mode"] == "C" and sic_code:
        raw = pappers_search_by_naf(sic_code)
        for r in raw:
            method_a_results.append({
                "name": r.get("nom_entreprise", r.get("denomination", "")),
                "website": None,
                "registry_number": r.get("siren", ""),
                "discovery_method": "A_pappers",
                "description": f"NAF {sic_code} registered company",
                "country": country,
                "raw_data": r,
            })

    all_companies.extend(method_a_results)
    discovery_meta["methods_run"].append("A_registry")
    discovery_meta["counts_by_method"]["A_registry"] = len(method_a_results)
    log(f"  [{ts()}] Method A found {len(method_a_results)} companies")

    # ---- Seed: inject known major players for this sector (priority) ----
    # The SIC search returns an arbitrary 100 companies and often misses major
    # players entirely.  We prepend a curated list so they are always present
    # and are processed first (financial collection works top-to-bottom).
    if sic_code and registry_mode["mode"] in ("A", "C"):
        sic_prefix = str(sic_code)[:3]
        seed_names = SECTOR_SEEDS.get((country.lower(), sic_prefix), [])
        seed_results = []
        for seed_name in seed_names:
            if normalise_company_name(seed_name) == normalise_company_name(company):
                continue
            if any(normalise_company_name(c["name"]) == normalise_company_name(seed_name)
                   for c in all_companies):
                continue  # already present from registry search
            seed_results.append({
                "name": seed_name,
                "website": None,
                "registry_number": None,  # resolved during financial collection
                "discovery_method": "A_seed",
                "description": "Known major sector player (seeded)",
                "country": country,
                "raw_data": {},
            })
        # Prepend seeds so they are always included and processed first
        all_companies = seed_results + all_companies
        if seed_results:
            log(f"  [{ts()}] Seeded {len(seed_results)} known major players "
                f"(SIC prefix {sic_prefix})")
        discovery_meta["counts_by_method"]["A_seed"] = len(seed_results)

    # ---- Method B: Web search ----
    log(f"  [{ts()}] Method B: Web search queries")
    method_b_results = []

    # Build niche-specific queries: use full description keywords, not just the
    # 3-word sector extract — this matters for rare B2B niches
    # Extract key noun phrases directly from description
    desc_words = [w for w in re.findall(r'\b[a-zA-Z\-]{4,}\b', description.lower())
                  if w not in STOP_WORDS]
    # Take the most distinctive 4 words (skip generic ones)
    generic = {"manufacturer", "provider", "supplier", "company", "business",
               "service", "product", "solution", "platform", "system"}
    niche_words = [w for w in desc_words if w not in generic][:4]
    niche_phrase = " ".join(niche_words)

    # Keep to 5 focused queries — more than this triggers rate-limiting on
    # Bing/DDG before the session completes.  Prioritise the most specific ones.
    b_queries = [
        f"{niche_phrase} companies {country}",
        f"{company} competitors {country}",
        f"{niche_phrase} supplier {country}",
        f"{sector} association {country} members",
        f"{niche_phrase} {country} directory",
    ]
    for q in b_queries:
        log(f"    Searching: {q[:70]}...")
        results = search_web(q, num_results=8)
        log(f"      → {len(results)} results")
        for r in results:
            # Skip results from domains that can never be competitors
            domain = _extract_domain(r.get("url", ""))
            if any(nd in domain for nd in _NOISE_DOMAINS):
                continue
            if domain.startswith(_NOISE_DOMAIN_PREFIXES):
                continue

            # Use only the snippet for proper-noun extraction.
            # Page titles like "Top 10 Tesco Competitors 2024 | Latterly.org"
            # produce garbage names ("Tesco Competitors") when included.
            snippet_text = r.get("snippet", "")
            names = extract_company_names_from_text(snippet_text)

            # Also extract company name directly from the result URL domain
            domain_name = _domain_to_company_name(domain)
            if domain_name and _is_plausible_company_name(domain_name):
                names.append(domain_name)

            for name in names:
                if not _is_plausible_company_name(name):
                    continue
                if normalise_company_name(name) == normalise_company_name(company):
                    continue
                method_b_results.append({
                    "name": name,
                    "website": domain,
                    "registry_number": None,
                    "discovery_method": "B_web_search",
                    "description": r.get("snippet", "")[:200],
                    "country": country,
                    "raw_data": r,
                })

    all_companies.extend(method_b_results)
    discovery_meta["methods_run"].append("B_web_search")
    discovery_meta["counts_by_method"]["B_web_search"] = len(method_b_results)
    log(f"  [{ts()}] Method B found {len(method_b_results)} raw entries")

    # ---- Method E: International registries ----
    if registry_mode["mode"] in ("D", "E"):
        log(f"  [{ts()}] Method E: International registry / web-only mode")
        method_e_results = []
        lang = registry_mode.get("language", "English")
        e_queries = [
            f"{sector} companies {country}",
            f"{country} business directory {sector}",
        ]
        if lang != "English":
            e_queries.extend([
                f"{sector} empresas {country}",
                f"{sector} unternehmen {country}",
                f"{sector} entreprises {country}",
            ])
        if registry_mode["mode"] == "D":
            iso = registry_mode.get("country_code", "").lower()
            oc_results = oc_search_companies(sector, iso)
            for r in oc_results:
                name = r.get("name", "")
                if name and normalise_company_name(name) != normalise_company_name(company):
                    method_e_results.append({
                        "name": name,
                        "website": r.get("registered_address", {}).get("street", ""),
                        "registry_number": r.get("company_number", ""),
                        "discovery_method": "E_opencorporates",
                        "description": f"OpenCorporates: {r.get('jurisdiction_code', '')}",
                        "country": country,
                        "raw_data": r,
                    })
        for q in e_queries[:3]:
            results = search_web(q, num_results=6)
            for r in results:
                names = extract_company_names_from_text(r.get("snippet", ""))
                for name in names:
                    if normalise_company_name(name) == normalise_company_name(company):
                        continue
                    method_e_results.append({
                        "name": name,
                        "website": _extract_domain(r.get("url", "")),
                        "registry_number": None,
                        "discovery_method": "E_web_registry",
                        "description": r.get("snippet", "")[:200],
                        "country": country,
                        "raw_data": r,
                    })
        all_companies.extend(method_e_results)
        discovery_meta["methods_run"].append("E_international")
        discovery_meta["counts_by_method"]["E_international"] = len(method_e_results)
        log(f"  [{ts()}] Method E found {len(method_e_results)} additional companies")

    # Final deduplication
    all_companies = deduplicate_companies(all_companies)

    # Remove empty names and very short names
    all_companies = [c for c in all_companies if len(c.get("name", "")) > 3]

    # Filter out obviously irrelevant results (job boards, news sites, etc.)
    noise_domains = {"indeed.com", "glassdoor.com", "linkedin.com", "reed.co.uk",
                     "totaljobs.com", "guardian.com", "bbc.co.uk", "ft.com",
                     "crunchbase.com", "wikipedia.org", "companies-house.gov.uk",
                     "zhihu.com"}
    filtered = []
    for c in all_companies:
        site = c.get("website", "") or ""
        if not any(nd in site for nd in noise_domains):
            filtered.append(c)
    all_companies = filtered

    # Hard cap: seeds + up to 4 quality CH companies.  Anything beyond 15 is
    # noise that slows the run down without improving the market sizing model.
    MAX_COMPANIES = 15
    if len(all_companies) > MAX_COMPANIES:
        log(f"  [{ts()}] Capping list at {MAX_COMPANIES} companies "
            f"(was {len(all_companies)} — trailing entries are lower-quality web signals)")
        all_companies = all_companies[:MAX_COMPANIES]

    log(f"  [{ts()}] Final deduplicated competitor list: {len(all_companies)} companies")

    if len(all_companies) < 8:
        discovery_meta["warning"] = (
            f"Only {len(all_companies)} companies found. This may indicate a very fragmented "
            f"market, limited web presence of players, or discovery limitations. "
            f"Manual research recommended."
        )
        log(f"  [{ts()}] WARNING: Fewer than 8 companies found")

    return all_companies, discovery_meta

def _extract_domain(url: str) -> str:
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        domain = parsed.netloc.replace("www.", "")
        return domain if domain else ""
    except Exception:
        return ""

def _extract_linkedin_company(url: str) -> str:
    m = re.search(r'linkedin\.com/company/([^/?&]+)', url)
    if m:
        name = m.group(1).replace("-", " ").title()
        return name
    return ""

def _fetch_page_text(url: str) -> str:
    resp = fetch_url(url)
    if resp and resp.status_code == 200:
        try:
            soup = BeautifulSoup(resp.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return soup.get_text(separator=" ", strip=True)
        except Exception:
            pass
    return ""


# ---------------------------------------------------------------------------
# MODULE 6 — Competitor Classification
# ---------------------------------------------------------------------------
def classify_competitors(competitors: list, target_company: str,
                          target_description: str, target_country: str) -> list:
    log(f"  [{ts()}] Classifying {len(competitors)} competitors")
    target_keywords = set(re.findall(r'\b[a-zA-Z]{4,}\b',
                                     target_description.lower())) - STOP_WORDS
    target_country_lower = target_country.lower()

    for comp in competitors:
        name = comp.get("name", "")
        desc = comp.get("description", "")
        website = comp.get("website", "")
        country_match = None

        # Fetch website description if we have a website
        if website and not desc:
            page = _fetch_page_text(f"https://{website}")
            if not page:
                page = _fetch_page_text(f"http://{website}")
            if page:
                # Extract meta description or first 400 chars
                desc = page[:400]
                comp["description"] = desc[:200]

        # Keyword overlap for same_product
        comp_keywords = set(re.findall(r'\b[a-zA-Z]{4,}\b',
                                       desc.lower())) - STOP_WORDS
        overlap = target_keywords & comp_keywords
        overlap_ratio = len(overlap) / max(len(target_keywords), 1)
        same_product = overlap_ratio >= 0.25

        # Geography check
        country_signals = [target_country_lower, target_country_lower[:2]]
        country_text = (desc + " " + str(comp.get("raw_data", {}))).lower()
        same_geography = any(sig in country_text for sig in country_signals)
        if not same_geography and comp.get("country", "").lower() == target_country_lower:
            same_geography = True

        # Segment check (B2B/B2C, size signals)
        b2b_signals = {"enterprise", "business", "corporate", "b2b", "professional",
                       "commercial", "organisation", "organization", "employer",
                       "client", "company", "companies"}
        target_is_b2b = bool(target_keywords & b2b_signals)
        comp_is_b2b = bool(comp_keywords & b2b_signals)
        same_segment = (target_is_b2b == comp_is_b2b) if (comp_keywords) else None

        # Classification
        criteria_met = sum([
            same_product is True,
            same_segment is True,
            same_geography is True,
        ])

        if same_product and same_segment is not False and same_geography:
            tier = "DIRECT"
            rationale = (f"Classified as direct: sells overlapping product/service "
                         f"({', '.join(list(overlap)[:3])}), "
                         f"targets similar segment, operates in {target_country}.")
        elif criteria_met >= 1:
            tier = "SECOND_TIER"
            reasons = []
            if same_product:
                reasons.append(f"product overlap ({', '.join(list(overlap)[:2])})")
            else:
                reasons.append("adjacent/partial product overlap")
            if not same_geography:
                reasons.append("different primary geography")
            if same_segment is False:
                reasons.append("different customer segment")
            rationale = f"Second-tier: {'; '.join(reasons)}."
        else:
            tier = "UNKNOWN"
            rationale = (f"Insufficient information to classify confidently "
                         f"(keyword overlap: {overlap_ratio:.0%}, "
                         f"desc length: {len(desc)} chars).")

        comp["tier"] = tier
        comp["classification_rationale"] = rationale
        comp["same_product"] = same_product
        comp["same_segment"] = same_segment
        comp["same_geography"] = same_geography

    direct = sum(1 for c in competitors if c.get("tier") == "DIRECT")
    second = sum(1 for c in competitors if c.get("tier") == "SECOND_TIER")
    unknown = sum(1 for c in competitors if c.get("tier") == "UNKNOWN")
    log(f"  [{ts()}] Classification: {direct} direct, {second} second-tier, {unknown} unknown")
    return competitors

# ---------------------------------------------------------------------------
# MODULE 7 — Financial Data Collection
# ---------------------------------------------------------------------------
def collect_financials(competitors: list, registry_mode: dict) -> list:
    log(f"  [{ts()}] Collecting financial data for {len(competitors)} companies")
    current_year = datetime.now().year

    for i, comp in enumerate(competitors):
        name = comp.get("name", "")
        reg_num = comp.get("registry_number", "")
        log(f"  [{ts()}] [{i+1}/{len(competitors)}] {name}")

        financials = []
        data_quality = "UNKNOWN"
        data_source = "None"
        data_source_url = None

        # Mode A — Companies House
        if registry_mode["mode"] == "A":
            is_seed = comp.get("discovery_method") == "A_seed"
            # Step 1: try to scrape the company's own website for their reg number
            # (most reliable — avoids name-search ambiguity).
            # Skip for seeds — their names are exact CH names, and major retail
            # websites (waitrose.com, asda.com) block scraping with CDN/bot protection,
            # causing 3-retry × 20s timeouts per company.
            if not reg_num and comp.get("website") and not is_seed:
                scraped = _scrape_company_number_from_website(comp["website"])
                if scraped:
                    reg_num = scraped
                    comp["registry_number"] = reg_num
            # Step 2: fall back to Companies House name search
            if not reg_num:
                matches = ch_search_company_name(name)
                if matches:
                    reg_num = matches[0].get("company_number", "")
                    comp["registry_number"] = reg_num
                    comp["website"] = comp.get("website") or (
                        f"https://find-and-update.company-information.service.gov.uk"
                        f"/company/{reg_num}"
                    )
            if reg_num:
                financials = ch_get_financials(reg_num)
                if financials:
                    # Check if any year has turnover
                    has_pl = any(f.get("turnover") is not None for f in financials)
                    data_quality = "VERIFIED" if has_pl else "INFERRED"
                    data_source = "Companies House"
                    data_source_url = (
                        f"https://find-and-update.company-information.service.gov.uk"
                        f"/company/{reg_num}/filing-history"
                    )

        # Mode C — Pappers
        elif registry_mode["mode"] == "C":
            if not reg_num:
                # Try Pappers search
                params = {"q": name}
                if PAPPERS_API_KEY:
                    params["api_token"] = PAPPERS_API_KEY
                resp = fetch_url(f"{PAPPERS_BASE}/recherche-entreprises", params=params)
                if resp and resp.status_code == 200:
                    try:
                        results = resp.json().get("resultats", [])
                        if results:
                            reg_num = results[0].get("siren", "")
                            comp["registry_number"] = reg_num
                    except Exception:
                        pass
            if reg_num:
                company_data = pappers_get_company(reg_num)
                if company_data:
                    financials = _parse_pappers_financials(company_data)
                    if financials:
                        data_quality = "VERIFIED"
                        data_source = "Pappers"
                        data_source_url = f"https://pappers.fr/entreprise/{reg_num}"

        # Mode D — OpenCorporates
        elif registry_mode["mode"] == "D":
            iso = registry_mode.get("country_code", "").lower()
            if reg_num:
                oc_data = oc_get_company(iso, reg_num)
                if oc_data:
                    financials = _parse_oc_financials(oc_data)
                    if financials:
                        data_quality = "VERIFIED"
                        data_source = "OpenCorporates"

        # Web-based financial discovery (all modes as fallback).
        # Also fires for INFERRED companies: abbreviated/micro-entity accounts
        # contain balance-sheet data but no P&L turnover.  For well-known
        # companies the web search can supply the missing revenue figure.
        #
        # Skip web search for companies discovered via the SIC registry that
        # only have micro-entity / abbreviated accounts AND no balance-sheet
        # data worth inferring from — these are typically tiny corner shops
        # with no web presence, so web searches would just waste rate-limit
        # budget without finding anything useful.
        is_registry_micro = (
            comp.get("discovery_method") == "A_companies_house"
            and data_quality == "INFERRED"
            and financials
            and not any(
                financials[-1].get(k)
                for k in ["trade_debtors", "trade_creditors", "staff_costs",
                          "deferred_income", "director_emoluments"]
            )
        )
        if not financials or (data_quality in ("UNKNOWN", "INFERRED") and not is_registry_micro):
            web_financials = _search_web_financials(name, current_year)
            if web_financials:
                if data_quality == "INFERRED" and financials:
                    # Merge: keep the richer balance-sheet snapshot from CH,
                    # inject the web-sourced turnover into it.
                    web_turnover = web_financials[0].get("turnover")
                    if web_turnover:
                        for fin in financials:
                            if fin.get("turnover") is None:
                                fin["turnover"] = web_turnover
                        has_pl = any(f.get("turnover") is not None for f in financials)
                        if has_pl:
                            data_quality = "VERIFIED"
                            data_source = "Companies House (balance sheet) + Web (turnover)"
                            data_source_url = web_financials[0].get("source_url")
                else:
                    financials = web_financials
                    data_quality = "ESTIMATED"
                    data_source = "Web sources (annual reports / news)"
                    data_source_url = web_financials[0].get("source_url") if web_financials else None

        comp["financials"] = financials
        comp["data_quality"] = data_quality
        comp["data_source"] = data_source
        comp["data_source_url"] = data_source_url
        comp["last_updated"] = datetime.now().strftime("%Y-%m-%d")

    verified = sum(1 for c in competitors if c.get("data_quality") == "VERIFIED")
    estimated = sum(1 for c in competitors if c.get("data_quality") == "ESTIMATED")
    inferred = sum(1 for c in competitors if c.get("data_quality") == "INFERRED")
    unknown = sum(1 for c in competitors if c.get("data_quality") == "UNKNOWN")
    log(f"  [{ts()}] Financials: {verified} verified, {estimated} estimated, "
        f"{inferred} inferred (balance-sheet only), {unknown} unknown")
    return competitors

def _parse_pappers_financials(data: dict) -> list:
    results = []
    for year_data in data.get("finances", []):
        snap = {
            "year": year_data.get("annee"),
            "turnover": year_data.get("chiffre_affaires"),
            "net_profit": year_data.get("resultat"),
            "total_assets": year_data.get("total_bilan"),
            "net_assets": year_data.get("capitaux_propres"),
            "employees": year_data.get("effectif"),
            "operating_profit": None,
            "trade_debtors": None, "trade_creditors": None,
            "staff_costs": None, "deferred_income": None,
            "fixed_assets": None, "director_emoluments": None,
            "abbreviated": False,
            "filing_date": str(year_data.get("annee", "")),
            "source_url": "https://pappers.fr",
        }
        results.append(snap)
    return results

def _parse_oc_financials(data: dict) -> list:
    # OpenCorporates rarely has financials, but try
    results = []
    for filing in data.get("filings", []):
        if "annual" in filing.get("filing_type_name", "").lower():
            snap = {
                "year": None, "turnover": None, "operating_profit": None,
                "net_profit": None, "total_assets": None, "net_assets": None,
                "employees": None, "trade_debtors": None, "trade_creditors": None,
                "staff_costs": None, "deferred_income": None, "fixed_assets": None,
                "director_emoluments": None, "abbreviated": True,
                "filing_date": filing.get("date", ""),
                "source_url": filing.get("url", ""),
            }
            results.append(snap)
    return results

def _extract_revenue_value(text: str) -> float | None:
    """
    Return the first plausible annual revenue/turnover figure found in text.
    Handles patterns like: £68.2bn, £28,000m, £1.2 billion, $4.5 million, 68,215 million
    """
    patterns = [
        # £/$/€ followed by number + optional unit
        re.compile(
            r'(?:turnover|revenue|sales)[^£$€\d]{0,40}[\$£€]\s*([\d,]+(?:\.\d+)?)\s*'
            r'(billion|bn|million|m\b)',
            re.IGNORECASE),
        re.compile(
            r'[\$£€]\s*([\d,]+(?:\.\d+)?)\s*(billion|bn|million|m\b)',
            re.IGNORECASE),
        # bare number + billion/million (e.g. "turnover of 28,000 million")
        re.compile(
            r'(?:turnover|revenue|sales)[^0-9]{0,30}([\d,]+(?:\.\d+)?)\s*(billion|bn|million|m\b)',
            re.IGNORECASE),
    ]
    for pat in patterns:
        for m in pat.finditer(text):
            groups = m.groups()
            val_str = groups[-2]
            unit = (groups[-1] or "").lower()
            try:
                val = float(val_str.replace(",", ""))
                if "billion" in unit or unit == "bn":
                    val *= 1_000_000_000
                elif "million" in unit or unit == "m":
                    val *= 1_000_000
                if val > 1_000_000:  # At least £1m — filters out years/page numbers
                    return val
            except Exception:
                continue
    return None


def _search_web_financials(company_name: str, current_year: int) -> list:
    """Search for revenue figures, reading both snippets and actual pages."""
    queries = [
        f'"{company_name}" revenue OR turnover {current_year - 1}',
        f'"{company_name}" annual results {current_year - 1}',
        f'"{company_name}" annual report {current_year - 2}',
    ]

    def _make_snap(val: float, url: str) -> dict:
        return {
            "year": current_year - 1,
            "turnover": val,
            "operating_profit": None, "net_profit": None,
            "total_assets": None, "net_assets": None,
            "employees": None, "trade_debtors": None,
            "trade_creditors": None, "staff_costs": None,
            "deferred_income": None, "fixed_assets": None,
            "director_emoluments": None, "abbreviated": False,
            "filing_date": str(current_year - 1),
            "source_url": url,
        }

    for q in queries[:2]:  # 2 queries per company max
        web_results = search_web(q, num_results=5)
        for r in web_results:
            url = r.get("url", "")
            if '›' in url or not url:
                continue

            # --- Pass 1: snippet (fast, no extra request) ---
            text = r.get("snippet", "") + " " + r.get("title", "")
            val = _extract_revenue_value(text)
            if val:
                return [_make_snap(val, url)]

            # --- Pass 2: fetch the actual page for richer text ---
            # Only fetch HTML pages that look like investor/results pages
            is_report_page = any(kw in url.lower() for kw in
                                 ("annual", "result", "investor", "finance",
                                  "report", "turnover", "revenue", "accounts"))
            is_pdf = url.lower().endswith(".pdf")
            if not is_report_page and not is_pdf:
                continue

            page_resp = fetch_url(url)
            if not page_resp or page_resp.status_code != 200:
                continue

            if is_pdf:
                # PDFs: scan raw bytes for embedded text strings
                raw = page_resp.content[:150_000].decode("latin-1", errors="ignore")
                val = _extract_revenue_value(raw)
            else:
                soup = BeautifulSoup(page_resp.text, "lxml")
                page_text = soup.get_text(" ", strip=True)
                val = _extract_revenue_value(page_text[:60_000])

            if val:
                return [_make_snap(val, url)]

    return []


# ---------------------------------------------------------------------------
# MODULE 8 — Revenue Inference Engine
# ---------------------------------------------------------------------------
DSO_BY_SIC_PREFIX = {
    "62": ("Software/SaaS", 30, 12.2),
    "63": ("Software/SaaS", 30, 12.2),
    "58": ("Software/SaaS", 30, 12.2),
    "64": ("Financial services", 30, 12.0),
    "65": ("Financial services", 30, 12.0),
    "66": ("Financial services", 30, 12.0),
    "69": ("Professional services", 45, 8.1),
    "70": ("Professional services", 45, 8.1),
    "71": ("Professional services", 45, 8.1),
    "72": ("Professional services", 45, 8.1),
    "73": ("Professional services", 45, 8.1),
    "74": ("Professional services", 45, 8.1),
    "78": ("Professional services", 45, 8.1),
    "41": ("Construction", 60, 6.1),
    "42": ("Construction", 60, 6.1),
    "43": ("Construction", 60, 6.1),
    "45": ("Retail/distribution", 21, 17.4),
    "46": ("Retail/distribution", 21, 17.4),
    "47": ("Retail/distribution", 21, 17.4),
    "10": ("Manufacturing", 35, 10.4),
    "20": ("Manufacturing", 35, 10.4),
    "25": ("Manufacturing", 35, 10.4),
}
DEFAULT_SECTOR = ("Professional services", 45, 8.1)

GROSS_MARGINS = {
    "Software/SaaS": 0.70,
    "Financial services": 0.65,
    "Professional services": 0.40,
    "Construction": 0.25,
    "Retail/distribution": 0.30,
    "Manufacturing": 0.35,
}
ONS_MEDIAN_WAGES = {
    "Software/SaaS": 48000,
    "Financial services": 45000,
    "Professional services": 38000,
    "Construction": 33000,
    "Retail/distribution": 24000,
    "Manufacturing": 30000,
}
REV_PER_EMPLOYEE = {
    "Software/SaaS": 130000,
    "Financial services": 150000,
    "Professional services": 95000,
    "Construction": 85000,
    "Retail/distribution": 180000,
    "Manufacturing": 120000,
}
NET_MARGINS = {
    "Software/SaaS": 0.15,
    "Financial services": 0.20,
    "Professional services": 0.12,
    "Construction": 0.05,
    "Retail/distribution": 0.04,
    "Manufacturing": 0.07,
}

def _get_sector_params(sic_code: str) -> tuple:
    if sic_code:
        prefix = str(sic_code)[:2]
        return DSO_BY_SIC_PREFIX.get(prefix, DEFAULT_SECTOR)
    return DEFAULT_SECTOR

def infer_revenue(financials: list, sic_code: str) -> dict:
    if not financials:
        return None
    latest = financials[-1]
    sector_name, assumed_dso, multiplier = _get_sector_params(sic_code)
    gross_margin = GROSS_MARGINS.get(sector_name, 0.35)
    median_wage = ONS_MEDIAN_WAGES.get(sector_name, 32000)
    rev_per_emp = REV_PER_EMPLOYEE.get(sector_name, 90000)
    net_margin = NET_MARGINS.get(sector_name, 0.08)

    inferences = []

    # Method 1 — Trade debtors
    if latest.get("trade_debtors"):
        td = latest["trade_debtors"]
        rev_low = td * (365.0 / 60)
        rev_high = td * (365.0 / 30)
        rev_mid = td * (365.0 / assumed_dso)
        inferences.append({
            "method": "Method 1 — Trade Debtors",
            "balance_sheet_line": f"Trade debtors: £{td:,.0f}",
            "assumption": f"DSO range 30–60 days (sector assumption: {assumed_dso} days for {sector_name})",
            "revenue_low": rev_low,
            "revenue_high": rev_high,
            "confidence": "MEDIUM",
            "workings": (f"Trade debtors £{td:,.0f} × (365 ÷ {assumed_dso} days) = "
                         f"£{rev_mid:,.0f} midpoint; "
                         f"range £{rev_low:,.0f}–£{rev_high:,.0f} (30–60 day DSO bounds)")
        })

    # Method 2 — Trade creditors
    if latest.get("trade_creditors"):
        tc = latest["trade_creditors"]
        cogs = tc * (365.0 / 45)
        rev_low = cogs / (1 - gross_margin + 0.05)
        rev_high = cogs / max(1 - gross_margin - 0.05, 0.05)
        inferences.append({
            "method": "Method 2 — Trade Creditors",
            "balance_sheet_line": f"Trade creditors: £{tc:,.0f}",
            "assumption": (f"45-day payables assumed; {sector_name} gross margin "
                           f"{gross_margin:.0%} ± 5pp"),
            "revenue_low": rev_low,
            "revenue_high": rev_high,
            "confidence": "LOW",
            "workings": (f"Trade creditors £{tc:,.0f} × (365 ÷ 45) = estimated COGS £{cogs:,.0f}; "
                         f"÷ (1 − {gross_margin:.0%} gross margin) = revenue £{(cogs/(1-gross_margin)):,.0f}; "
                         f"range £{rev_low:,.0f}–£{rev_high:,.0f}")
        })

    # Method 3 — Staff costs
    if latest.get("staff_costs"):
        sc = latest["staff_costs"]
        headcount = sc / median_wage
        rev_low = headcount * rev_per_emp * 0.8
        rev_high = headcount * rev_per_emp * 1.2
        inferences.append({
            "method": "Method 3 — Staff Costs",
            "balance_sheet_line": f"Staff costs: £{sc:,.0f}",
            "assumption": (f"ONS median wage for {sector_name}: £{median_wage:,}; "
                           f"revenue per employee benchmark: £{rev_per_emp:,}"),
            "revenue_low": rev_low,
            "revenue_high": rev_high,
            "confidence": "LOW",
            "workings": (f"Staff costs £{sc:,.0f} ÷ £{median_wage:,} median wage = "
                         f"~{headcount:.0f} employees; "
                         f"× £{rev_per_emp:,} revenue/employee = £{headcount*rev_per_emp:,.0f}; "
                         f"range ±20%: £{rev_low:,.0f}–£{rev_high:,.0f}")
        })

    # Method 4 — Net asset growth (needs 2 years)
    if len(financials) >= 2:
        prev = financials[-2]
        if (latest.get("net_assets") is not None and
                prev.get("net_assets") is not None and
                latest["net_assets"] > prev["net_assets"]):
            retained = latest["net_assets"] - prev["net_assets"]
            if net_margin > 0.02:
                rev_mid = retained / net_margin
                rev_low = retained / (net_margin + 0.03)
                rev_high = retained / max(net_margin - 0.02, 0.01)
                inferences.append({
                    "method": "Method 4 — Net Asset Growth",
                    "balance_sheet_line": (f"Net assets: £{latest['net_assets']:,.0f} "
                                           f"(prev: £{prev['net_assets']:,.0f})"),
                    "assumption": (f"{sector_name} typical net margin "
                                   f"{net_margin:.0%} ± 2–3pp"),
                    "revenue_low": rev_low,
                    "revenue_high": rev_high,
                    "confidence": "LOW",
                    "workings": (f"Net assets grew by £{retained:,.0f}; "
                                 f"÷ {net_margin:.0%} net margin = £{rev_mid:,.0f}; "
                                 f"range £{rev_low:,.0f}–£{rev_high:,.0f}")
                })

    # Method 5 — Deferred income
    if latest.get("deferred_income") and latest["deferred_income"] > 0:
        di = latest["deferred_income"]
        rev_low = di * 1.0    # annual contracts
        rev_high = di * 12.0  # monthly billing
        inferences.append({
            "method": "Method 5 — Deferred Income",
            "balance_sheet_line": f"Deferred income: £{di:,.0f}",
            "assumption": "Range assumes annual (×1) to monthly billing cycle (×12)",
            "revenue_low": rev_low,
            "revenue_high": rev_high,
            "confidence": "LOW",
            "workings": (f"Deferred income £{di:,.0f}: annual billing = £{rev_low:,.0f}; "
                         f"monthly billing = £{rev_high:,.0f}")
        })

    # Method 7 — Director emoluments (scale signal only)
    if latest.get("director_emoluments") and latest["director_emoluments"] > 0:
        de = latest["director_emoluments"]
        inferences.append({
            "method": "Method 7 — Director Emoluments",
            "balance_sheet_line": f"Director emoluments: £{de:,.0f}",
            "assumption": "Provides minimum profit floor; directional only",
            "revenue_low": de * 3,
            "revenue_high": de * 20,
            "confidence": "LOW",
            "workings": (f"Director emoluments £{de:,.0f} signals minimum profit floor; "
                         f"directional range £{de*3:,.0f}–£{de*20:,.0f} based on typical "
                         f"director pay as % of profit (5–33%)")
        })

    if not inferences:
        return None

    all_lows = [i["revenue_low"] for i in inferences]
    all_highs = [i["revenue_high"] for i in inferences]
    all_mids = [(i["revenue_low"] + i["revenue_high"]) / 2 for i in inferences]

    # Weight Method 1 (trade debtors) higher if present
    medium_conf = [i for i in inferences if i["confidence"] == "MEDIUM"]
    if medium_conf:
        consolidated_mid = (medium_conf[0]["revenue_low"] + medium_conf[0]["revenue_high"]) / 2
    else:
        consolidated_mid = sum(all_mids) / len(all_mids)

    return {
        "inferences": inferences,
        "consolidated_low": min(all_lows),
        "consolidated_high": max(all_highs),
        "consolidated_mid": consolidated_mid,
        "methods_used": [i["method"] for i in inferences],
        "sector": sector_name,
        "label": "INFERRED ESTIMATE — NOT FROM ACCOUNTS",
    }

# ---------------------------------------------------------------------------
# MODULE 9 — CAGR and period changes
# ---------------------------------------------------------------------------
def calculate_cagr(start: float, end: float, years: int) -> "float | None":
    if not start or not end or years <= 0 or start <= 0:
        return None
    try:
        return (end / start) ** (1.0 / years) - 1
    except Exception:
        return None

def calculate_financial_trends(competitors: list) -> list:
    for comp in competitors:
        financials = comp.get("financials", [])
        if not financials or len(financials) < 2:
            comp["trends"] = None
            continue

        # Sort by year
        fin_sorted = sorted(
            [f for f in financials if f.get("year")],
            key=lambda x: x["year"]
        )
        if len(fin_sorted) < 2:
            comp["trends"] = None
            continue

        first = fin_sorted[0]
        last = fin_sorted[-1]
        n_years = (last["year"] or 0) - (first["year"] or 0)

        rev_cagr = calculate_cagr(first.get("turnover"), last.get("turnover"), n_years)
        prof_cagr = calculate_cagr(first.get("operating_profit") or first.get("net_profit"),
                                   last.get("operating_profit") or last.get("net_profit"),
                                   n_years)
        emp_cagr = calculate_cagr(first.get("employees"), last.get("employees"), n_years)

        yoy = []
        for i in range(1, len(fin_sorted)):
            prev_f = fin_sorted[i - 1]
            curr_f = fin_sorted[i]
            period = f"{prev_f['year']}→{curr_f['year']}"
            rev_abs = None
            rev_pct = None
            prof_pct = None
            if curr_f.get("turnover") and prev_f.get("turnover") and prev_f["turnover"] > 0:
                rev_abs = curr_f["turnover"] - prev_f["turnover"]
                rev_pct = rev_abs / prev_f["turnover"]
            prev_prof = prev_f.get("operating_profit") or prev_f.get("net_profit")
            curr_prof = curr_f.get("operating_profit") or curr_f.get("net_profit")
            if curr_prof and prev_prof and prev_prof != 0:
                prof_pct = (curr_prof - prev_prof) / abs(prev_prof)
            yoy.append({
                "period": period,
                "revenue_change_abs": rev_abs,
                "revenue_change_pct": rev_pct,
                "profit_change_pct": prof_pct,
            })

        comp["trends"] = {
            "revenue_cagr": rev_cagr,
            "revenue_cagr_period": (f"{first['year']}–{last['year']} "
                                    f"({n_years}-year CAGR)" if n_years else ""),
            "profit_cagr": prof_cagr,
            "headcount_cagr": emp_cagr,
            "yoy_changes": yoy,
            "data_years": [f["year"] for f in fin_sorted],
        }

    return competitors


# ---------------------------------------------------------------------------
# MODULE 10 — Market Sizing
# ---------------------------------------------------------------------------
def _get_best_revenue(comp: dict) -> "tuple[float | None, str]":
    """Return (revenue_value, source_label) for the most recent year."""
    financials = comp.get("financials", [])
    if financials:
        sorted_fin = sorted(
            [f for f in financials if f.get("year")],
            key=lambda x: x["year"], reverse=True
        )
        for f in sorted_fin:
            if f.get("turnover"):
                return f["turnover"], f"Verified ({f['year']})"

    # Try inference
    inference = comp.get("inference")
    if inference:
        return inference["consolidated_mid"], "Inferred (mid estimate)"

    return None, "Not found"

def calculate_market_sizing(competitors: list, description: str,
                             country: str, sic_code: str) -> dict:
    # --- Top-down ---
    log(f"  [{ts()}] Top-down: searching for market size data")
    sector = extract_sector(description)
    year = datetime.now().year
    td_queries = [
        f"{description} market size {country} {year}",
        f"{description} market value {country}",
        f"{sector} industry revenue {country} {year}",
        f"{sector} market size {country} billion million",
    ]

    currency_pattern = re.compile(
        r'[\$£€]\s*([\d,]+(?:\.\d+)?)\s*(trillion|billion|billion|bn|million|m\b)',
        re.IGNORECASE
    )
    alt_pattern = re.compile(
        r'([\d,]+(?:\.\d+)?)\s*(trillion|billion|bn|million)\s*(?:pounds|dollars|euros|GBP|USD|EUR)?',
        re.IGNORECASE
    )

    top_down_estimate = None
    top_down_source = None
    top_down_url = None
    top_down_year = None
    top_down_confidence = "LOW"
    top_down_notes = "No reliable market size figure found; constructed proxy estimate used."

    for q in td_queries:
        results = search_web(q, num_results=6)
        for r in results:
            text = r.get("snippet", "") + " " + r.get("title", "")
            for pattern in [currency_pattern, alt_pattern]:
                matches = pattern.findall(text)
                for match in matches:
                    try:
                        val_str = match[0].replace(",", "")
                        unit = match[1].lower()
                        val = float(val_str)
                        if "trillion" in unit:
                            val *= 1_000_000_000_000
                        elif "billion" in unit or "bn" in unit:
                            val *= 1_000_000_000
                        elif "million" in unit or unit == "m":
                            val *= 1_000_000
                        if val > 1_000_000:  # At least £1m to be plausible
                            top_down_estimate = val
                            top_down_source = r.get("title", "Web source")[:100]
                            top_down_url = r.get("url", "")
                            top_down_confidence = "MEDIUM"
                            top_down_notes = (
                                f"Figure found in web source: '{r.get('snippet','')[:150]}'. "
                                f"Verify against primary source."
                            )
                            break
                    except Exception:
                        pass
                if top_down_estimate:
                    break
            if top_down_estimate:
                break
        if top_down_estimate:
            break

    if not top_down_estimate:
        log(f"  [{ts()}] No market size figure found — constructing proxy estimate")
        # Proxy: sum known revenues × market coverage assumption
        all_revenues = [_get_best_revenue(c)[0] for c in competitors
                        if _get_best_revenue(c)[0]]
        if all_revenues:
            sum_rev = sum(all_revenues)
            top_down_estimate = sum_rev / 0.6  # assume 60% coverage
            top_down_source = "Constructed proxy: sum of identified players ÷ 60% coverage assumption"
            top_down_confidence = "LOW"
            top_down_notes = (
                f"No published market size found. Proxy constructed from "
                f"{len(all_revenues)} companies with known revenue data "
                f"(total £{sum_rev/1e6:.1f}m ÷ 60% assumed coverage = "
                f"£{top_down_estimate/1e6:.1f}m). Treat as directional only."
            )
        else:
            top_down_notes = (
                "No market size figure found and insufficient company revenue "
                "data to construct a proxy estimate. Primary research required."
            )

    # --- Bottom-up ---
    log(f"  [{ts()}] Bottom-up: building 3×3 matrix")
    coverage_scenarios = {
        "conservative": 0.80,
        "base": 0.60,
        "aggressive": 0.40,
    }
    tier_filters = {
        "direct_only": ["DIRECT"],
        "direct_and_second_tier": ["DIRECT", "SECOND_TIER"],
        "all": ["DIRECT", "SECOND_TIER", "UNKNOWN"],
    }

    matrix = {}
    for tier_key, tier_list in tier_filters.items():
        tier_comps = [c for c in competitors if c.get("tier") in tier_list]
        revenues = []
        for c in tier_comps:
            rev, source = _get_best_revenue(c)
            if rev:
                revenues.append({"company": c["name"], "revenue": rev,
                                  "source": source, "tier": c.get("tier")})

        matrix[tier_key] = {}
        for scenario, coverage in coverage_scenarios.items():
            total_rev = sum(r["revenue"] for r in revenues)
            if total_rev > 0:
                market_est = total_rev / coverage
                matrix[tier_key][scenario] = {
                    "companies_included": len(tier_comps),
                    "companies_with_revenue": len(revenues),
                    "total_identified_revenue": total_rev,
                    "coverage_assumption": coverage,
                    "market_estimate": market_est,
                    "revenue_breakdown": revenues,
                    "maths": (f"£{total_rev/1e6:.1f}m identified revenue "
                              f"÷ {coverage:.0%} coverage = "
                              f"£{market_est/1e6:.1f}m"),
                    "footnote": (f"Sum of {len(revenues)} companies with revenue data "
                                 f"from {len(tier_comps)} {tier_key.replace('_',' ')} "
                                 f"competitors, assuming they represent "
                                 f"{coverage:.0%} of the market."),
                }
            else:
                matrix[tier_key][scenario] = {
                    "companies_included": len(tier_comps),
                    "companies_with_revenue": 0,
                    "total_identified_revenue": 0,
                    "coverage_assumption": coverage,
                    "market_estimate": None,
                    "revenue_breakdown": [],
                    "maths": "Insufficient revenue data",
                    "footnote": f"No revenue data available for {tier_key.replace('_',' ')} companies.",
                }

    # Aggregate CAGR
    agg_cagr = None
    cagr_period = ""
    all_cagrs = [c["trends"]["revenue_cagr"] for c in competitors
                 if c.get("trends") and c["trends"].get("revenue_cagr") is not None]
    if all_cagrs:
        agg_cagr = sum(all_cagrs) / len(all_cagrs)
        periods = [c["trends"].get("revenue_cagr_period", "") for c in competitors
                   if c.get("trends") and c["trends"].get("revenue_cagr_period")]
        cagr_period = periods[0] if periods else ""

    # Reliability warning
    total_with_data = sum(
        1 for c in competitors if _get_best_revenue(c)[0] is not None
    )
    reliability_warning = None
    if total_with_data < 4:
        reliability_warning = (
            f"Only {total_with_data} companies have revenue data. "
            f"Bottom-up estimate is unreliable. Additional data needed: "
            f"Companies House filings for identified competitors, or "
            f"trade body statistics for sector revenue benchmarks."
        )

    # Cross-check commentary
    base_bu = matrix.get("direct_and_second_tier", {}).get("base", {}).get("market_estimate")
    if top_down_estimate and base_bu:
        ratio = top_down_estimate / base_bu
        if 0.5 <= ratio <= 2.0:
            cross_check = (
                f"Top-down (£{top_down_estimate/1e6:.1f}m) and bottom-up base case "
                f"(£{base_bu/1e6:.1f}m) are directionally consistent "
                f"(ratio {ratio:.1f}×). This gives reasonable confidence in the "
                f"£{min(top_down_estimate,base_bu)/1e6:.0f}m–"
                f"£{max(top_down_estimate,base_bu)/1e6:.0f}m range."
            )
        elif ratio > 2.0:
            cross_check = (
                f"Top-down estimate (£{top_down_estimate/1e6:.1f}m) is "
                f"{ratio:.1f}× the bottom-up base case (£{base_bu/1e6:.1f}m). "
                f"This divergence suggests either: (a) the identified competitor "
                f"set covers significantly less than 60% of the market, or "
                f"(b) the top-down source includes adjacent categories. "
                f"Recommend treating the bottom-up as the conservative anchor."
            )
        else:
            cross_check = (
                f"Bottom-up base case (£{base_bu/1e6:.1f}m) is {1/ratio:.1f}× "
                f"the top-down estimate (£{top_down_estimate/1e6:.1f}m). "
                f"This may indicate the market is more concentrated than assumed, "
                f"or the top-down source is understating market size. "
                f"Recommend validating the top-down figure against primary sources."
            )
    elif top_down_estimate:
        cross_check = (
            "Insufficient bottom-up data to cross-check against top-down estimate. "
            "See Gaps & Next Steps for recommended research actions."
        )
    else:
        cross_check = (
            "Neither top-down nor bottom-up estimates are well-supported by data. "
            "This analysis should be treated as preliminary. "
            "Primary research is strongly recommended."
        )

    return {
        "top_down": {
            "estimate": top_down_estimate,
            "unit": "GBP",
            "source": top_down_source,
            "source_url": top_down_url,
            "year": top_down_year or year - 1,
            "confidence": top_down_confidence,
            "notes": top_down_notes,
        },
        "bottom_up": {
            "matrix": matrix,
            "aggregate_cagr": agg_cagr,
            "cagr_period": cagr_period,
            "companies_with_revenue_data": total_with_data,
            "total_companies": len(competitors),
            "reliability_warning": reliability_warning,
        },
        "cross_check": cross_check,
    }

# ---------------------------------------------------------------------------
# MODULE 11 — Known Unknowns
# ---------------------------------------------------------------------------
def generate_known_unknowns(competitors: list, market_sizing: dict,
                             discovery_meta: dict) -> dict:
    cat1, cat2, cat3, cat4 = [], [], [], []

    # Category 1 — no financials
    for c in competitors:
        if c.get("data_quality") in ("UNKNOWN", None):
            reg_num = c.get("registry_number", "")
            if reg_num:
                action = (f"Retrieve Companies House filing history for "
                          f"{c['name']} (#{reg_num}) at "
                          f"find-and-update.company-information.service.gov.uk")
                effort = "Quick Win (< 1 hour)"
            else:
                action = (f"Search '{c['name']} annual report filetype:pdf' and "
                          f"'{c['name']} revenue' to find published figures")
                effort = "Quick Win (< 1 hour)"
            cat1.append({
                "company": c["name"],
                "action": action,
                "effort": effort,
                "potential_value": "Would add data point to bottom-up estimate",
            })

    # Category 2 — data quality issues
    for c in competitors:
        if c.get("data_quality") == "INFERRED":
            cat2.append({
                "issue": "Abbreviated accounts — revenue inferred from balance sheet",
                "company": c["name"],
                "detail": (f"Full P&L not filed. Inference used: "
                           f"{', '.join(c.get('inference', {}).get('methods_used', []))}."),
                "effort": "Medium (half day)",
                "action": (f"File a request to Companies House for the full accounts PDF, "
                           f"or contact {c['name']} directly to request revenue data."),
            })
        financials = c.get("financials", [])
        if financials:
            most_recent_year = max((f.get("year") or 0) for f in financials)
            if most_recent_year and most_recent_year < datetime.now().year - 2:
                cat2.append({
                    "issue": f"Stale data — most recent filing is {most_recent_year}",
                    "company": c["name"],
                    "detail": f"Last available accounts are {datetime.now().year - most_recent_year} years old.",
                    "effort": "Quick Win (< 1 hour)",
                    "action": "Check if more recent accounts have been filed; company may have been acquired or dissolved.",
                })

    # Category 3 — discovery gaps
    if not discovery_meta.get("counts_by_method", {}).get("A_registry"):
        cat3.append({
            "gap": "Registry SIC/NAF code search not performed",
            "reason": "No SIC code provided or registry not available for this country",
            "action": "Identify the correct SIC/SIC equivalent code and re-run Method A",
            "effort": "Quick Win (< 1 hour)",
        })
    cat3.append({
        "gap": "Foreign-owned subsidiaries may be under-represented",
        "reason": ("UK subsidiaries of foreign parents may file consolidated accounts "
                   "that obscure local revenue"),
        "action": ("Review Companies House filings for parent company disclosures; "
                   "search for UK-specific revenue in group annual reports"),
        "effort": "Medium (half day)",
    })
    cat3.append({
        "gap": "Private companies with dormant/micro filings may be excluded",
        "reason": "Very small companies below micro-entity threshold may have no useful financial data",
        "action": "LinkedIn headcount check for all identified companies to confirm active status",
        "effort": "Quick Win (< 1 hour)",
    })
    if discovery_meta.get("warning"):
        cat3.append({
            "gap": discovery_meta["warning"],
            "reason": "Fewer than 8 competitors identified",
            "action": ("Expand search with alternative SIC codes, trade directory manual review, "
                       "and sector-specific association membership lists"),
            "effort": "Medium (half day)",
        })

    # Category 4 — market sizing limitations
    cat4.append({
        "limitation": "Coverage assumption (40–80%) is the most sensitive variable in the model",
        "action": ("Validate by checking ONS Annual Business Survey for total sector revenue, "
                   "or by obtaining trade association aggregate statistics"),
        "effort": "Quick Win (< 1 hour)",
        "value": "Would allow pinning the bottom-up coverage assumption to a verifiable figure",
    })
    if market_sizing["top_down"]["confidence"] in ("LOW", "MEDIUM"):
        cat4.append({
            "limitation": "Top-down market size figure not from a primary source",
            "action": ("Search IBISWorld, Mintel, or Statista free previews; "
                       "contact relevant trade body; check ONS PRODCOM or ABI data"),
            "effort": "Medium (half day)",
            "value": "High — a credible published market size would anchor the entire analysis",
        })
    cat4.append({
        "limitation": "No primary research (customer interviews) conducted",
        "action": ("Conduct 5–10 expert interviews via industry contacts or "
                   "expert network (e.g. GLG, AlphaSights) to validate size and growth rate"),
        "effort": "Hard (specialist access needed)",
        "value": "Would provide qualitative validation of market dynamics and growth trajectory",
    })
    cat4.append({
        "limitation": "Revenue CAGR based on limited sample of companies with multi-year data",
        "action": ("Obtain 3–5 year revenue series for at least 4 direct competitors "
                   "before relying on the aggregate CAGR figure"),
        "effort": "Medium (half day)",
        "value": "Medium — market growth rate affects valuation multiples and projections",
    })

    return {
        "category_1_no_financials": cat1,
        "category_2_data_quality": cat2,
        "category_3_discovery_gaps": cat3,
        "category_4_market_sizing": cat4,
    }

# ---------------------------------------------------------------------------
# MODULE 12 — Data persistence
# ---------------------------------------------------------------------------
def save_data(data: dict, company: str) -> str:
    filename = f"market_data_{company.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    return filename

def load_data(company: str) -> "dict | None":
    import glob as _glob
    pattern = f"market_data_{company.replace(' ', '_')}_*.json"
    files = sorted(_glob.glob(pattern), reverse=True)
    if files:
        with open(files[0], encoding="utf-8") as f:
            return json.load(f)
    return None


# ---------------------------------------------------------------------------
# MODULE 13 — HTML Dashboard Generator
# ---------------------------------------------------------------------------
def _fmt_currency(val, suffix="m") -> str:
    if val is None:
        return "—"
    if suffix == "m":
        return f"£{val/1_000_000:.1f}m"
    if suffix == "k":
        return f"£{val/1_000:.0f}k"
    return f"£{val:,.0f}"

def _fmt_pct(val) -> str:
    if val is None:
        return "—"
    return f"{val*100:+.1f}%"

def _quality_badge(quality: str) -> str:
    colours = {
        "VERIFIED": "#27ae60",
        "ESTIMATED": "#f39c12",
        "INFERRED": "#e67e22",
        "UNKNOWN": "#95a5a6",
    }
    colour = colours.get(quality, "#95a5a6")
    return (f'<span style="background:{colour};color:white;padding:2px 8px;'
            f'border-radius:10px;font-size:11px;font-weight:600;">'
            f'{quality}</span>')

def _tier_badge(tier: str) -> str:
    styles = {
        "DIRECT": "background:#1B2B4A;color:white",
        "SECOND_TIER": "background:#C9A961;color:white",
        "UNKNOWN": "background:#bdc3c7;color:#333",
    }
    labels = {"DIRECT": "DIRECT", "SECOND_TIER": "2ND TIER", "UNKNOWN": "UNKNOWN"}
    style = styles.get(tier, styles["UNKNOWN"])
    label = labels.get(tier, tier)
    return (f'<span style="{style};padding:2px 8px;border-radius:10px;'
            f'font-size:11px;font-weight:600;">{label}</span>')

def _effort_badge(effort: str) -> str:
    if "Quick" in effort:
        return (f'<span style="background:#27ae60;color:white;padding:2px 8px;'
                f'border-radius:10px;font-size:11px;">{effort}</span>')
    elif "Medium" in effort:
        return (f'<span style="background:#f39c12;color:white;padding:2px 8px;'
                f'border-radius:10px;font-size:11px;">{effort}</span>')
    else:
        return (f'<span style="background:#c0392b;color:white;padding:2px 8px;'
                f'border-radius:10px;font-size:11px;">{effort}</span>')

def generate_dashboard(data: dict) -> str:
    company = data.get("company", "")
    country = data.get("country", "")
    description = data.get("description", "")
    analysis_date = data.get("analysis_date", datetime.now().isoformat())[:10]
    registry_mode = data.get("registry_mode", {})
    competitors = data.get("competitors", [])
    market_sizing = data.get("market_sizing", {})
    known_unknowns = data.get("known_unknowns", {})
    discovery_meta = data.get("discovery_meta", {})

    # Compute summary stats
    total = len(competitors)
    n_verified = sum(1 for c in competitors if c.get("data_quality") == "VERIFIED")
    n_inferred = sum(1 for c in competitors if c.get("data_quality") == "INFERRED")
    n_estimated = sum(1 for c in competitors if c.get("data_quality") == "ESTIMATED")
    n_no_data = total - n_verified - n_inferred - n_estimated
    completeness_pct = int((n_verified + n_inferred + n_estimated) / max(total, 1) * 100)

    is_web_only = registry_mode.get("mode") == "E"

    if completeness_pct >= 70:
        confidence = "MEDIUM"
        conf_reason = f"{completeness_pct}% of companies have financial data (verified or inferred)"
    elif completeness_pct >= 40:
        confidence = "LOW"
        conf_reason = f"Only {completeness_pct}% of companies have financial data"
    else:
        confidence = "LOW"
        conf_reason = f"Very limited financial data ({completeness_pct}% coverage)"
    if n_verified >= 3 and completeness_pct >= 60:
        confidence = "MEDIUM"

    conf_colours = {"HIGH": "#27ae60", "MEDIUM": "#f39c12", "LOW": "#c0392b"}
    conf_colour = conf_colours.get(confidence, "#95a5a6")

    sector = extract_sector(description)

    # ---- Build competitor table rows ----
    comp_rows = []
    # Sort: verified first, then by revenue desc
    def sort_key(c):
        rev, _ = _get_best_revenue(c)
        dq_order = {"VERIFIED": 0, "ESTIMATED": 1, "INFERRED": 2, "UNKNOWN": 3}
        return (dq_order.get(c.get("data_quality", "UNKNOWN"), 3), -(rev or 0))

    sorted_comps = sorted(competitors, key=sort_key)

    for c in sorted_comps:
        name = c.get("name", "")
        tier = c.get("tier", "UNKNOWN")
        dq = c.get("data_quality", "UNKNOWN")
        rev, rev_src = _get_best_revenue(c)
        inference = c.get("inference")

        if rev and dq == "VERIFIED":
            rev_display = _fmt_currency(rev)
        elif inference:
            rev_display = (f'<em style="color:#e67e22">Inferred: '
                           f'{_fmt_currency(inference["consolidated_low"])}–'
                           f'{_fmt_currency(inference["consolidated_high"])}</em>')
        elif rev:
            rev_display = f'<em>{_fmt_currency(rev)} (est.)</em>'
        else:
            rev_display = '<em style="color:#95a5a6">Not found</em>'

        emp = None
        for f in sorted(c.get("financials", []), key=lambda x: x.get("year") or 0, reverse=True):
            if f.get("employees"):
                emp = f["employees"]
                break

        reg_num = c.get("registry_number", "")
        reg_link = ""
        if reg_num and registry_mode.get("mode") == "A":
            reg_link = (f'<a href="https://find-and-update.company-information.service.gov.uk'
                        f'/company/{reg_num}" target="_blank" '
                        f'style="color:#1B2B4A;font-size:11px;">CH ↗</a>')

        website = c.get("website", "")
        name_cell = name
        if website:
            url = website if "://" in website else f"https://{website}"
            name_cell = f'<a href="{url}" target="_blank" style="color:#1B2B4A">{name}</a>'

        rationale = c.get("classification_rationale", "")

        # Inference workings
        inference_html = ""
        if inference:
            rows_inf = ""
            for inf in inference.get("inferences", []):
                conf_col = "🟡" if inf["confidence"] == "MEDIUM" else "🔴"
                rows_inf += (f"<tr><td>{inf['method']}</td>"
                             f"<td>{inf['balance_sheet_line']}</td>"
                             f"<td>{_fmt_currency(inf['revenue_low'])}–"
                             f"{_fmt_currency(inf['revenue_high'])}</td>"
                             f"<td>{conf_col} {inf['confidence']}</td>"
                             f"<td style='font-size:11px;color:#555'>{inf['workings']}</td></tr>")
            inference_html = f"""
            <details style="margin-top:6px">
              <summary style="cursor:pointer;color:#C9A961;font-size:12px;font-weight:600">
                ▶ Show inference workings — {inference['label']}
              </summary>
              <div style="background:#f8f9fa;padding:10px;border-radius:6px;margin-top:6px">
                <p style="font-size:12px;color:#666;margin:0 0 6px">
                  Sector: {inference.get('sector','')} |
                  Methods used: {len(inference.get('inferences',[]))} |
                  Consolidated range: {_fmt_currency(inference['consolidated_low'])}–{_fmt_currency(inference['consolidated_high'])}
                </p>
                <table style="width:100%;font-size:11px;border-collapse:collapse">
                  <thead><tr style="background:#e8e8e8">
                    <th style="text-align:left;padding:4px">Method</th>
                    <th>Balance sheet line</th>
                    <th>Revenue range</th>
                    <th>Confidence</th>
                    <th>Workings</th>
                  </tr></thead>
                  <tbody>{rows_inf}</tbody>
                </table>
              </div>
            </details>"""

        comp_rows.append({
            "name": name_cell,
            "raw_name": name,
            "tier": tier,
            "rev_display": rev_display,
            "rev_val": rev or 0,
            "emp": f"{emp:,}" if emp else "—",
            "source": c.get("data_source", "—"),
            "quality": _quality_badge(dq),
            "reg_link": reg_link,
            "inference_html": inference_html,
            "dq": dq,
        })

    comp_rows_html = ""
    for r in comp_rows:
        comp_rows_html += f"""
        <tr class="comp-row">
          <td style="padding:8px 10px">
            {r['name']}
            {r['inference_html']}
          </td>
          <td style="padding:8px 10px">{r['rev_display']}</td>
          <td style="padding:8px 10px;text-align:center">{r['emp']}</td>
          <td style="padding:8px 10px;font-size:12px">{r['source']}</td>
          <td style="padding:8px 10px;text-align:center">{r['quality']}</td>
          <td style="padding:8px 10px;text-align:center">{r['reg_link']}</td>
        </tr>"""

    # ---- Chart data ----
    chart_labels = json.dumps([r["raw_name"][:25] for r in comp_rows if r["rev_val"] > 0])
    chart_values = json.dumps([r["rev_val"] / 1_000_000 for r in comp_rows if r["rev_val"] > 0])
    chart_colours = json.dumps(["#1B2B4A"] * len([r for r in comp_rows if r["rev_val"] > 0]))

    # Discovery donut data
    method_counts = discovery_meta.get("counts_by_method", {})
    method_labels = json.dumps(list(method_counts.keys()))
    method_values = json.dumps(list(method_counts.values()))
    donut_colours = json.dumps(["#1B2B4A", "#C9A961", "#2980b9", "#27ae60", "#e74c3c"])

    # ---- Trends chart data ----
    # Collect multi-year data
    trend_datasets = []
    trend_all_years = set()
    for c in competitors:
        fins = sorted(
            [f for f in c.get("financials", []) if f.get("year") and f.get("turnover")],
            key=lambda x: x["year"]
        )
        if len(fins) >= 2:
            trend_all_years.update(f["year"] for f in fins)
    trend_years = sorted(trend_all_years)

    line_colours = ["#1B2B4A", "#2c4a7c", "#1a5276", "#0e6655", "#145a32",
                    "#C9A961", "#d4ac0d", "#e67e22", "#784212", "#935116"]
    ci = 0
    for c in competitors:
        fins = sorted(
            [f for f in c.get("financials", []) if f.get("year") and f.get("turnover")],
            key=lambda x: x["year"]
        )
        if not fins:
            continue
        fin_by_year = {f["year"]: f["turnover"] for f in fins}
        values = [fin_by_year.get(y) for y in trend_years]
        if not any(v for v in values):
            continue
        colour = line_colours[ci % len(line_colours)]
        ci += 1

        js_values = [v / 1_000_000 if v else "null" for v in values]
        trend_datasets.append({
            "label": c["name"][:20],
            "data": js_values,
            "borderColor": colour,
            "borderDash": [],
            "fill": False,
            "tension": 0.1,
            "pointStyle": "circle",
        })

    trend_labels_js = json.dumps([str(y) for y in trend_years])
    trend_datasets_js = json.dumps(trend_datasets).replace('"null"', 'null')

    # ---- YoY table ----
    yoy_html = ""
    for c in competitors:
        trends = c.get("trends")
        if not trends or not trends.get("yoy_changes"):
            continue
        row_cells = f"<td style='padding:6px 10px'>{c['name'][:25]}</td>"
        for yc in trends["yoy_changes"]:
            pct = yc.get("revenue_change_pct")
            if pct is None:
                cell = "<td style='text-align:center;color:#aaa'>—</td>"
            elif pct >= 0:
                cell = (f"<td style='text-align:center;background:#d5f5e3;"
                        f"color:#1e8449'>{_fmt_pct(pct)}</td>")
            else:
                cell = (f"<td style='text-align:center;background:#fadbd8;"
                        f"color:#c0392b'>{_fmt_pct(pct)}</td>")
            row_cells += cell
        yoy_html += f"<tr>{row_cells}</tr>"

    # ---- CAGR table ----
    cagr_rows_html = ""
    comps_with_cagr = [(c, c.get("trends", {})) for c in competitors
                       if c.get("trends") and c["trends"].get("revenue_cagr") is not None]
    if comps_with_cagr:
        max_cagr = max(c[1]["revenue_cagr"] for c in comps_with_cagr)
        min_cagr = min(c[1]["revenue_cagr"] for c in comps_with_cagr)
        for c, t in comps_with_cagr:
            is_fastest = t["revenue_cagr"] == max_cagr
            is_slowest = t["revenue_cagr"] == min_cagr
            row_style = ("background:#FFF9E6" if is_fastest else
                         "background:#FFF0EE" if is_slowest else "")
            fastest_label = " ⭐" if is_fastest else ""
            cagr_rows_html += f"""<tr style="{row_style}">
              <td style="padding:6px 10px">{c['name'][:25]}{fastest_label}</td>
              <td style="text-align:center;font-size:11px">{t.get('revenue_cagr_period','')}</td>
              <td style="text-align:center;font-weight:600">{_fmt_pct(t.get('revenue_cagr'))}</td>
              <td style="text-align:center">{_fmt_pct(t.get('profit_cagr'))}</td>
              <td style="text-align:center">{_fmt_pct(t.get('headcount_cagr'))}</td>
            </tr>"""

    # ---- Market sizing tab ----
    td = market_sizing.get("top_down", {})
    bu = market_sizing.get("bottom_up", {})
    td_est = td.get("estimate")
    td_display = _fmt_currency(td_est) if td_est else "Not found"
    td_conf = td.get("confidence", "LOW")
    td_conf_colour = conf_colours.get(td_conf, "#95a5a6")

    matrix = bu.get("matrix", {})

    def matrix_cell(tier_key, scenario):
        cell = matrix.get(tier_key, {}).get(scenario, {})
        est = cell.get("market_estimate")
        if est:
            return (f'<div style="font-size:18px;font-weight:700;color:#1B2B4A">'
                    f'{_fmt_currency(est)}</div>'
                    f'<div style="font-size:10px;color:#888;margin-top:4px">'
                    f'{cell.get("maths","")}</div>')
        return '<div style="color:#aaa;font-size:13px">Insufficient data</div>'

    agg_cagr = bu.get("aggregate_cagr")
    agg_cagr_display = _fmt_pct(agg_cagr) if agg_cagr is not None else "—"
    rel_warning = bu.get("reliability_warning", "")

    # ---- Gaps tab ----
    def gap_rows(items, cols):
        if not items:
            return '<tr><td colspan="4" style="color:#aaa;padding:12px">No items identified.</td></tr>'
        html = ""
        for item in items:
            cells = "".join(f"<td style='padding:8px 10px;vertical-align:top'>{item.get(c,'')}</td>"
                            for c in cols[:3])
            effort = item.get("effort", "")
            cells += f"<td style='padding:8px 10px;text-align:center'>{_effort_badge(effort)}</td>"
            cells += (f"<td style='padding:8px 10px;font-size:11px;color:#666'>"
                      f"{item.get('potential_value', item.get('value', ''))}</td>")
            html += f"<tr style='border-bottom:1px solid #eee'>{cells}</tr>"
        return html

    web_only_banner = ""
    if is_web_only:
        web_only_banner = f"""
        <div style="background:#f39c12;color:white;padding:12px 20px;
                    border-radius:6px;margin-bottom:16px;font-weight:600">
          ⚠ Web-only mode: {country} has no accessible public company registry.
          All financial data is sourced from public web sources and unverified.
        </div>"""

    discovery_warning_html = ""
    if discovery_meta.get("warning"):
        discovery_warning_html = f"""
        <div style="background:#fef9e7;border:1px solid #f39c12;padding:10px 16px;
                    border-radius:6px;margin-bottom:12px;font-size:13px;color:#7d6608">
          ⚠ {discovery_meta['warning']}
        </div>"""

    # ---- Assemble HTML ----
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Market Sizing — {company}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; background: #f5f7fa;
          color: #222; font-size: 14px; }}
  .header {{ background: #1B2B4A; color: white; padding: 20px 30px; }}
  .header h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
  .header .meta {{ font-size: 13px; opacity: 0.8; margin-bottom: 10px; }}
  .completeness-bar {{
    display: inline-flex; align-items: center; gap: 12px;
    background: rgba(255,255,255,0.1); padding: 6px 14px;
    border-radius: 20px; font-size: 12px;
  }}
  .conf-dot {{ width: 10px; height: 10px; border-radius: 50%;
               background: {conf_colour}; display: inline-block; }}
  .tabs {{ background: white; border-bottom: 2px solid #e0e0e0;
           padding: 0 30px; display: flex; gap: 0; }}
  .tab-btn {{ padding: 14px 22px; cursor: pointer; font-size: 13px;
              font-weight: 600; color: #666; border: none; background: none;
              border-bottom: 3px solid transparent; margin-bottom: -2px;
              transition: all 0.2s; }}
  .tab-btn.active {{ color: #1B2B4A; border-bottom-color: #C9A961; }}
  .tab-btn:hover {{ color: #1B2B4A; }}
  .tab-content {{ display: none; padding: 24px 30px; }}
  .tab-content.active {{ display: block; }}
  .card {{ background: white; border-radius: 8px; padding: 20px;
           box-shadow: 0 1px 4px rgba(0,0,0,0.08); margin-bottom: 16px; }}
  .card h3 {{ font-size: 15px; font-weight: 700; color: #1B2B4A;
              margin-bottom: 14px; border-bottom: 1px solid #f0f0f0;
              padding-bottom: 8px; }}
  .summary-cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }}
  .summary-card {{ background: white; border-radius: 8px; padding: 16px 20px;
                   box-shadow: 0 1px 4px rgba(0,0,0,0.08); min-width: 150px;
                   text-align: center; flex: 1; }}
  .summary-card .num {{ font-size: 28px; font-weight: 700; color: #1B2B4A; }}
  .summary-card .lbl {{ font-size: 11px; color: #888; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #f8f9fa; padding: 10px; text-align: left;
        font-size: 12px; color: #555; border-bottom: 2px solid #e0e0e0; }}
  td {{ border-bottom: 1px solid #f0f0f0; vertical-align: middle; }}
  tr:hover td {{ background: #fafbfc; }}
  .charts-row {{ display: grid; grid-template-columns: 2fr 1fr; gap: 16px;
                 margin-bottom: 16px; }}
  .matrix-table td {{ text-align: center; padding: 14px; min-width: 160px; }}
  .matrix-table th {{ text-align: center; }}
  .matrix-table .row-header {{ background: #f8f9fa; font-weight: 600;
                                font-size: 12px; color: #1B2B4A; }}
  .big-number {{ font-size: 36px; font-weight: 700; color: #1B2B4A;
                 margin: 8px 0; }}
  .gap-section h4 {{ color: #1B2B4A; font-size: 13px; font-weight: 700;
                     margin-bottom: 10px; text-transform: uppercase;
                     letter-spacing: 0.5px; }}
  a {{ color: #1B2B4A; }}
  .footnotes {{ font-size: 11px; color: #888; margin-top: 16px;
                border-top: 1px solid #eee; padding-top: 10px; }}
  .hidden {{ display: none; }}
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <h1>Market Sizing Analysis — {company}</h1>
  <div class="meta">{country} &nbsp;·&nbsp; {sector.title()} &nbsp;·&nbsp; Analysis date: {analysis_date} &nbsp;·&nbsp; Mode: {registry_mode.get('description','')}</div>
  {web_only_banner}
  <div class="completeness-bar">
    <span class="conf-dot"></span>
    <span>Confidence: <strong>{confidence}</strong> — {conf_reason}</span>
    &nbsp;|&nbsp;
    <span>Data completeness: <strong>{completeness_pct}%</strong></span>
  </div>
</div>

<!-- TABS -->
<div class="tabs">
  <button class="tab-btn active" onclick="showTab('landscape',this)">Competitor Landscape</button>
  <button class="tab-btn" onclick="showTab('trends',this)">Financial Trends</button>
  <button class="tab-btn" onclick="showTab('sizing',this)">Market Sizing</button>
  <button class="tab-btn" onclick="showTab('gaps',this)">Gaps &amp; Next Steps</button>
</div>

<!-- ================================================================ -->
<!-- TAB 1 — COMPETITOR LANDSCAPE -->
<!-- ================================================================ -->
<div id="tab-landscape" class="tab-content active">

  {discovery_warning_html}

  <div class="summary-cards">
    <div class="summary-card"><div class="num">{total}</div><div class="lbl">Total companies found</div></div>
    <div class="summary-card"><div class="num" style="color:#27ae60">{n_verified}</div><div class="lbl">Verified financials</div></div>
    <div class="summary-card"><div class="num" style="color:#e67e22">{n_inferred}</div><div class="lbl">Inferred financials</div></div>
    <div class="summary-card"><div class="num" style="color:#95a5a6">{n_no_data}</div><div class="lbl">No financial data</div></div>
  </div>

  <div class="card">
    <h3>Competitor Table</h3>
    <table id="comp-table">
      <thead>
        <tr>
          <th>Company</th>
          <th>Revenue</th>
          <th style="text-align:center">Employees</th>
          <th>Data Source</th>
          <th style="text-align:center">Quality</th>
          <th style="text-align:center">Registry</th>
        </tr>
      </thead>
      <tbody id="comp-tbody">
        {comp_rows_html}
      </tbody>
    </table>
  </div>

  <div class="charts-row">
    <div class="card">
      <h3>Revenue Comparison</h3>
      <canvas id="revenueChart" height="300"></canvas>
    </div>
    <div class="card">
      <h3>Discovery Methods</h3>
      <canvas id="discoveryDonut"></canvas>
    </div>
  </div>

</div>

<!-- ================================================================ -->
<!-- TAB 2 — FINANCIAL TRENDS -->
<!-- ================================================================ -->
<div id="tab-trends" class="tab-content">

  <div class="card">
    <h3>Revenue Trends (£m)</h3>
    <canvas id="trendsChart" height="200"></canvas>
    <p style="font-size:12px;color:#888;margin-top:8px">
      Solid lines = direct competitors. Dashed lines = second-tier.
      {sum(1 for c in competitors if not any(f.get('turnover') for f in c.get('financials',[])))} companies excluded due to missing data.
    </p>
  </div>

  <div class="card">
    <h3>Year-on-Year Revenue Growth</h3>
    <div style="overflow-x:auto">
      <table>
        <thead><tr><th>Company</th><th>Tier</th>
          {" ".join(f"<th style='text-align:center'>{c.get('trends',{}).get('yoy_changes',[{}])[i].get('period','') if c.get('trends') and i < len(c['trends'].get('yoy_changes',[])) else '' }</th>" for i in range(4) for c in competitors[:1])}
        </tr></thead>
        <tbody>{yoy_html if yoy_html else '<tr><td colspan="6" style="color:#aaa;padding:12px">Insufficient multi-year data available.</td></tr>'}</tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h3>CAGR Summary</h3>
    <table>
      <thead><tr>
        <th>Company</th>
        <th style="text-align:center">Data Period</th>
        <th style="text-align:center">Revenue CAGR</th>
        <th style="text-align:center">Profit CAGR</th>
        <th style="text-align:center">Headcount CAGR</th>
      </tr></thead>
      <tbody>
        {cagr_rows_html if cagr_rows_html else '<tr><td colspan="5" style="color:#aaa;padding:12px">Insufficient multi-year data for CAGR calculation.</td></tr>'}
      </tbody>
    </table>
    {'<p style="font-size:12px;color:#888;margin-top:8px">⭐ Fastest revenue grower highlighted</p>' if cagr_rows_html else ''}
  </div>

</div>

<!-- ================================================================ -->
<!-- TAB 3 — MARKET SIZING -->
<!-- ================================================================ -->
<div id="tab-sizing" class="tab-content">

  <div class="card">
    <h3>Top-Down Market Estimate</h3>
    <div style="display:flex;gap:20px;align-items:flex-start">
      <div>
        <div class="big-number">{td_display}</div>
        <div style="font-size:12px;color:#888">Total addressable market — {country}</div>
      </div>
      <div style="flex:1">
        <span style="background:{td_conf_colour};color:white;padding:3px 10px;
                     border-radius:12px;font-size:11px;font-weight:600">{td_conf}</span>
        <p style="margin-top:8px;font-size:13px;color:#555">{td.get('notes','')}</p>
        {f'<p style="font-size:12px;color:#888;margin-top:6px">Source: <a href="{td.get("source_url","")}" target="_blank">{td.get("source","")}</a></p>' if td.get('source') else ''}
      </div>
    </div>
  </div>

  <div class="card">
    <h3>Bottom-Up Market Estimate — 3×3 Scenario Matrix</h3>
    {f'<div style="background:#fef9e7;border:1px solid #f39c12;padding:8px 14px;border-radius:6px;margin-bottom:12px;font-size:12px">⚠ {rel_warning}</div>' if rel_warning else ''}
    <div style="overflow-x:auto">
    <table class="matrix-table" style="border:1px solid #e0e0e0">
      <thead>
        <tr style="background:#1B2B4A;color:white">
          <th style="padding:12px;min-width:140px">Coverage scenario</th>
          <th>All identified players<br><small style="font-weight:normal;opacity:0.8">({total} companies)</small></th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td class="row-header">Conservative<br><small style="font-weight:400">Identified = 80% of market</small></td>
          <td>{matrix_cell('all','conservative')}</td>
        </tr>
        <tr style="background:#f8f9fa">
          <td class="row-header" style="background:#EBF0F7;color:#1B2B4A">
            <strong>Base case</strong><br><small style="font-weight:400">Identified = 60% of market</small>
          </td>
          <td style="background:#EBF0F7"><strong>{matrix_cell('all','base')}</strong></td>
        </tr>
        <tr>
          <td class="row-header">Aggressive<br><small style="font-weight:400">Identified = 40% of market</small></td>
          <td>{matrix_cell('all','aggressive')}</td>
        </tr>
      </tbody>
    </table>
    </div>
    <p style="font-size:11px;color:#888;margin-top:8px">
      Market size = sum of identified company revenues ÷ assumed coverage fraction.
      Revenue figures from Companies House filings (verified) or balance sheet inference (inferred).
    </p>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
    <div class="card">
      <h3>Aggregate Market CAGR</h3>
      <div class="big-number">{agg_cagr_display}</div>
      <div style="font-size:12px;color:#888">{bu.get('cagr_period','')}</div>
      <p style="font-size:12px;color:#666;margin-top:8px">
        Based on {len([c for c in competitors if c.get('trends') and c['trends'].get('revenue_cagr') is not None])} companies with multi-year data.
        Treat as indicative — confidence increases with more data points.
      </p>
    </div>
    <div class="card">
      <h3>Cross-Check Commentary</h3>
      <p style="font-size:13px;line-height:1.6;color:#444">{market_sizing.get('cross_check','')}</p>
    </div>
  </div>

</div>

<!-- ================================================================ -->
<!-- TAB 4 — GAPS & NEXT STEPS -->
<!-- ================================================================ -->
<div id="tab-gaps" class="tab-content">

  <div class="card gap-section">
    <h3>Category 1 — Companies Found, No Financials</h3>
    <h4 style="color:#888;font-size:11px;margin-bottom:8px">{len(known_unknowns.get('category_1_no_financials',[]))} items</h4>
    <table>
      <thead><tr><th>Company</th><th>Recommended Action</th><th style="text-align:center">Effort</th><th>Potential Value</th></tr></thead>
      <tbody>{gap_rows(known_unknowns.get('category_1_no_financials',[]), ['company','action','effort','potential_value'])}</tbody>
    </table>
  </div>

  <div class="card gap-section">
    <h3>Category 2 — Data Quality Issues</h3>
    <h4 style="color:#888;font-size:11px;margin-bottom:8px">{len(known_unknowns.get('category_2_data_quality',[]))} items</h4>
    <table>
      <thead><tr><th>Issue</th><th>Company</th><th>Detail</th><th style="text-align:center">Effort</th><th>Action</th></tr></thead>
      <tbody>{gap_rows(known_unknowns.get('category_2_data_quality',[]), ['issue','company','detail','effort','action'])}</tbody>
    </table>
  </div>

  <div class="card gap-section">
    <h3>Category 3 — Discovery Gaps</h3>
    <h4 style="color:#888;font-size:11px;margin-bottom:8px">{len(known_unknowns.get('category_3_discovery_gaps',[]))} items</h4>
    <table>
      <thead><tr><th>Gap</th><th>Reason</th><th>Action</th><th style="text-align:center">Effort</th></tr></thead>
      <tbody>{gap_rows(known_unknowns.get('category_3_discovery_gaps',[]), ['gap','reason','action','effort'])}</tbody>
    </table>
  </div>

  <div class="card gap-section">
    <h3>Category 4 — Market Sizing Limitations</h3>
    <h4 style="color:#888;font-size:11px;margin-bottom:8px">{len(known_unknowns.get('category_4_market_sizing',[]))} items</h4>
    <table>
      <thead><tr><th>Limitation</th><th>Recommended Action</th><th style="text-align:center">Effort</th><th>Value</th></tr></thead>
      <tbody>{gap_rows(known_unknowns.get('category_4_market_sizing',[]), ['limitation','action','effort','value'])}</tbody>
    </table>
  </div>

</div>

<!-- ================================================================ -->
<!-- JAVASCRIPT -->
<!-- ================================================================ -->
<script>
// Tab switching
function showTab(id, btn) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  btn.classList.add('active');
}}

// Revenue bar chart
const revCtx = document.getElementById('revenueChart').getContext('2d');
new Chart(revCtx, {{
  type: 'bar',
  data: {{
    labels: {chart_labels},
    datasets: [{{
      data: {chart_values},
      backgroundColor: {chart_colours},
      borderWidth: 0,
      borderRadius: 3,
    }}]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        callbacks: {{
          label: ctx => '£' + ctx.raw.toFixed(1) + 'm'
        }}
      }}
    }},
    scales: {{
      x: {{
        title: {{ display: true, text: 'Revenue (£m)', font: {{ size: 11 }} }},
        ticks: {{ callback: v => '£' + v + 'm' }}
      }}
    }}
  }}
}});

// Discovery donut
const dCtx = document.getElementById('discoveryDonut').getContext('2d');
new Chart(dCtx, {{
  type: 'doughnut',
  data: {{
    labels: {method_labels},
    datasets: [{{
      data: {method_values},
      backgroundColor: {donut_colours},
      borderWidth: 2,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ position: 'bottom', labels: {{ font: {{ size: 11 }} }} }}
    }}
  }}
}});

// Trends line chart
const tCtx = document.getElementById('trendsChart').getContext('2d');
const trendsData = {trend_datasets_js};
trendsData.forEach(ds => {{
  ds.borderDash = ds.borderDash || [];
  ds.pointRadius = 4;
  ds.pointHoverRadius = 6;
}});
new Chart(tCtx, {{
  type: 'line',
  data: {{
    labels: {trend_labels_js},
    datasets: trendsData
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{
        position: 'bottom',
        labels: {{ font: {{ size: 11 }}, usePointStyle: true }}
      }},
      tooltip: {{
        callbacks: {{
          label: ctx => ctx.dataset.label + ': £' + (ctx.raw || 0).toFixed(1) + 'm'
        }}
      }}
    }},
    scales: {{
      y: {{
        title: {{ display: true, text: 'Revenue (£m)' }},
        ticks: {{ callback: v => '£' + v + 'm' }}
      }}
    }}
  }}
}});
</script>

</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# MODULE 14 — Terminal Summary
# ---------------------------------------------------------------------------
def print_terminal_summary(data: dict):
    company = data.get("company", "")
    country = data.get("country", "")
    analysis_date = data.get("analysis_date", "")[:10]
    registry_mode = data.get("registry_mode", {})
    competitors = data.get("competitors", [])
    market_sizing = data.get("market_sizing", {})
    known_unknowns = data.get("known_unknowns", {})
    discovery_meta = data.get("discovery_meta", {})

    n_total = len(competitors)
    counts = discovery_meta.get("counts_by_method", {})
    n_verified = sum(1 for c in competitors if c.get("data_quality") == "VERIFIED")
    n_inferred = sum(1 for c in competitors if c.get("data_quality") == "INFERRED")
    n_no_data = n_total - n_verified - n_inferred

    td = market_sizing.get("top_down", {})
    bu = market_sizing.get("bottom_up", {})
    td_est = td.get("estimate")
    td_conf = td.get("confidence", "—")
    td_display = f"£{td_est/1e6:.1f}m" if td_est else "Not found"

    all_base = bu.get("matrix", {}).get("all", {}).get("base", {})
    all_est = all_base.get("market_estimate")
    all_display = f"£{all_est/1e6:.1f}m" if all_est else "Insufficient data"

    ku = known_unknowns
    n_ku1 = len(ku.get("category_1_no_financials", []))
    n_ku2 = len(ku.get("category_2_data_quality", []))
    n_ku3 = len(ku.get("category_3_discovery_gaps", []))
    n_ku4 = len(ku.get("category_4_market_sizing", []))

    date_str = datetime.now().strftime("%Y%m%d")
    data_file = f"market_data_{company.replace(' ','_')}_{date_str}.json"
    html_file = f"market_sizing_{company.replace(' ','_')}_{date_str}.html"

    print("\n" + "=" * 62)
    print("  MARKET SIZING ANALYSIS COMPLETE")
    print("=" * 62)
    print(f"  Target:  {company} | {country} | {analysis_date}")
    print(f"  Mode:    {registry_mode.get('description', '')}")
    print()
    print("  DISCOVERY")
    print(f"    Total companies found:             {n_total}")
    for method, count in counts.items():
        print(f"      {method:<30} {count}")
    if discovery_meta.get("warning"):
        print(f"    ⚠  {discovery_meta['warning'][:60]}...")
    print()
    print("  FINANCIALS")
    print(f"    Verified financials:               {n_verified} companies")
    print(f"    Inferred financials (abbrev. accts): {n_inferred} companies")
    print(f"    No financial data:                 {n_no_data} companies")
    print()
    print("  MARKET SIZING")
    print(f"    Top-down estimate:                 {td_display} (confidence: {td_conf})")
    print(f"    Bottom-up (all players, base 60%): {all_display}")
    print()
    print("  KNOWN UNKNOWNS")
    print(f"    Category 1 — no financials:        {n_ku1} items")
    print(f"    Category 2 — data quality:         {n_ku2} items")
    print(f"    Category 3 — discovery gaps:       {n_ku3} items")
    print(f"    Category 4 — market sizing limits: {n_ku4} items")
    print()
    print("  Output files:")
    print(f"    Data:      {data_file}")
    print(f"    Dashboard: {html_file}")
    print("=" * 62 + "\n")

# ---------------------------------------------------------------------------
# MODULE 15 — Argument parsing & main orchestrator
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Market Sizing Analysis Tool — Gnosis Advisory Ltd",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python market_sizing.py --company "Wagestream" --country "UK" \\
    --description "financial wellbeing platform that lets employees access earned wages early" \\
    --sic "62012"

  python market_sizing.py --company "Acme Corp" --country "France" \\
    --description "B2B payroll software for SMEs" --sic "6201"

  python market_sizing.py --company "Acme Corp" --country "UK" \\
    --description "employee benefits platform" --skip-scrape
        """
    )
    parser.add_argument("--company", required=True,
                        help="Target/client company name")
    parser.add_argument("--country", required=True,
                        help="Country of operation (e.g. UK, France, Germany)")
    parser.add_argument("--description", required=True,
                        help="Brief description of what the company sells (1-2 sentences)")
    parser.add_argument("--sic", default=None,
                        help="SIC code (UK) or NAF code (France) if known")
    parser.add_argument("--skip-scrape", action="store_true",
                        help="Skip data collection and load cached JSON file instead")
    return parser.parse_args()

def main():
    args = parse_args()

    log(f"\n{'='*62}")
    log(f"  Market Sizing Analysis Tool — Gnosis Advisory Ltd")
    log(f"{'='*62}")
    log(f"  [{ts()}] Starting analysis for: {args.company}")
    log(f"  [{ts()}] Country: {args.country}")
    log(f"  [{ts()}] Description: {args.description[:80]}...")
    if args.sic:
        log(f"  [{ts()}] SIC/NAF code: {args.sic}")
    log("")

    # ----------------------------------------------------------------
    # STEP 1 — Country detection and registry mode routing
    # ----------------------------------------------------------------
    log(f"[{ts()}] STEP 1 — Detecting registry mode")
    registry_mode = detect_registry_mode(args.country)
    log(f"[{ts()}] Registry mode: {registry_mode['mode']} — {registry_mode['description']}")

    if registry_mode["mode"] == "E":
        log(f"[{ts()}] ⚠  Web-only mode activated — no accessible registry for {args.country}")

    data = None

    # ----------------------------------------------------------------
    # Handle --skip-scrape
    # ----------------------------------------------------------------
    if args.skip_scrape:
        log(f"[{ts()}] Loading cached data (--skip-scrape)")
        data = load_data(args.company)
        if data:
            log(f"[{ts()}] Loaded cached data from file")
        else:
            log(f"[{ts()}] No cached data found — running full analysis")

    if data is None:
        # ------------------------------------------------------------
        # STEP 2 — Competitor discovery
        # ------------------------------------------------------------
        log(f"\n[{ts()}] STEP 2 — Competitor discovery")
        competitors, discovery_meta = discover_competitors(
            args.company, args.country, args.description,
            args.sic, registry_mode
        )
        log(f"[{ts()}] Discovery complete: {len(competitors)} unique companies found")

        # ------------------------------------------------------------
        # STEP 3 — (classification removed — all companies treated equally)
        # ------------------------------------------------------------
        for c in competitors:
            c["tier"] = "DIRECT"
            c["classification_rationale"] = "All companies included"

        # ------------------------------------------------------------
        # STEP 4 — Financial data collection
        # ------------------------------------------------------------
        log(f"\n[{ts()}] STEP 4 — Financial data collection")
        competitors = collect_financials(competitors, registry_mode)

        # ------------------------------------------------------------
        # STEP 5 — Revenue inference engine
        # ------------------------------------------------------------
        log(f"\n[{ts()}] STEP 5 — Revenue inference engine")
        n_inferred = 0
        for comp in competitors:
            financials = comp.get("financials", [])
            if not financials:
                continue
            # Check if latest year has abbreviated accounts with no turnover
            sorted_fins = sorted(
                [f for f in financials if f.get("year")],
                key=lambda x: x["year"], reverse=True
            )
            if not sorted_fins:
                continue
            latest = sorted_fins[0]
            if latest.get("abbreviated") or latest.get("turnover") is None:
                # Only try inference if we have any balance sheet data
                has_bs_data = any(
                    latest.get(k) for k in
                    ["trade_debtors", "trade_creditors", "staff_costs",
                     "net_assets", "deferred_income", "fixed_assets",
                     "director_emoluments"]
                )
                if has_bs_data:
                    result = infer_revenue(sorted_fins, args.sic)
                    if result:
                        comp["inference"] = result
                        comp["data_quality"] = "INFERRED"
                        n_inferred += 1
                        log(f"  [{ts()}] Inferred for {comp['name']}: "
                            f"£{result['consolidated_low']/1e6:.1f}m–"
                            f"£{result['consolidated_high']/1e6:.1f}m "
                            f"({len(result['inferences'])} methods)")
        log(f"[{ts()}] Inference complete: {n_inferred} companies with inferred revenue")

        # ------------------------------------------------------------
        # STEP 6 — CAGR and trend calculations
        # ------------------------------------------------------------
        log(f"\n[{ts()}] STEP 6 — CAGR and period change calculations")
        competitors = calculate_financial_trends(competitors)
        n_with_cagr = sum(1 for c in competitors
                          if c.get("trends") and c["trends"].get("revenue_cagr") is not None)
        log(f"[{ts()}] CAGR calculated for {n_with_cagr} companies")

        # ------------------------------------------------------------
        # STEP 7 — Market sizing
        # ------------------------------------------------------------
        log(f"\n[{ts()}] STEP 7 — Market sizing (top-down + bottom-up)")
        market_sizing = calculate_market_sizing(
            competitors, args.description, args.country, args.sic
        )
        td_est = market_sizing["top_down"].get("estimate")
        if td_est:
            log(f"[{ts()}] Top-down estimate: £{td_est/1e6:.1f}m "
                f"(confidence: {market_sizing['top_down']['confidence']})")
        else:
            log(f"[{ts()}] Top-down: No reliable market size figure found")

        # ------------------------------------------------------------
        # STEP 8 — Known unknowns
        # ------------------------------------------------------------
        log(f"\n[{ts()}] STEP 8 — Generating known unknowns research agenda")
        known_unknowns = generate_known_unknowns(
            competitors, market_sizing, discovery_meta
        )
        total_ku = sum(len(v) for v in known_unknowns.values())
        log(f"[{ts()}] Known unknowns: {total_ku} items across 4 categories")

        # ------------------------------------------------------------
        # Compile data object
        # ------------------------------------------------------------
        data = {
            "company": args.company,
            "country": args.country,
            "description": args.description,
            "sic_code": args.sic,
            "analysis_date": datetime.now().isoformat(),
            "registry_mode": registry_mode,
            "competitors": competitors,
            "discovery_meta": discovery_meta,
            "market_sizing": market_sizing,
            "known_unknowns": known_unknowns,
        }

        # Save raw data
        data_file = save_data(data, args.company)
        log(f"\n[{ts()}] Raw data saved to: {data_file}")

    # ----------------------------------------------------------------
    # STEP 9 — HTML dashboard generation
    # ----------------------------------------------------------------
    log(f"\n[{ts()}] STEP 9 — Generating HTML dashboard")
    try:
        html = generate_dashboard(data)
        html_filename = (f"market_sizing_{args.company.replace(' ', '_')}_"
                         f"{datetime.now().strftime('%Y%m%d')}.html")
        with open(html_filename, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"[{ts()}] Dashboard saved to: {html_filename}")
    except Exception as e:
        log(f"[{ts()}] ⚠  Dashboard generation error: {e}")
        traceback.print_exc()

    # ----------------------------------------------------------------
    # STEP 10 — Terminal summary
    # ----------------------------------------------------------------
    print_terminal_summary(data)

if __name__ == "__main__":
    main()
