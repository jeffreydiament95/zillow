#!/usr/bin/env python3
"""Zillow property lookup tool.

Takes a property address, finds the Zillow listing, and reports
listing status, list price, and sale price.
"""

import argparse
import json
import re
import sys

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

STATUS_MAP = {
    "FOR_SALE": "Active",
    "PENDING": "Pending",
    "RECENTLY_SOLD": "Sold",
    "SOLD": "Sold",
    "OFF_MARKET": "Off Market",
}


def normalize_address(address: str) -> str:
    """Convert a street address into Zillow's URL-friendly format."""
    normalized = address.strip()
    # Remove unit/apt designators for the URL (Zillow handles them differently)
    normalized = re.sub(r"[#,.]", "", normalized)
    normalized = re.sub(r"\s+", "-", normalized)
    return normalized


def build_zillow_url(address: str) -> str:
    """Build a Zillow search URL from a normalized address."""
    return f"https://www.zillow.com/homes/{normalize_address(address)}_rb/"


def fetch_page(url: str) -> tuple[str, str]:
    """Fetch a Zillow page and return (html, final_url)."""
    session = requests.Session()
    session.headers.update(HEADERS)

    response = session.get(url, timeout=15, allow_redirects=True)

    if response.status_code == 403:
        print("Error: Blocked by Zillow (403 Forbidden).", file=sys.stderr)
        print("Try again later or from a different network.", file=sys.stderr)
        sys.exit(2)

    if response.status_code == 404:
        print("Error: Property not found (404).", file=sys.stderr)
        sys.exit(1)

    response.raise_for_status()

    body = response.text.lower()
    if "captcha" in body or "pardon our interruption" in body:
        print("Error: Zillow is showing a captcha/block page.", file=sys.stderr)
        print("Try again later or from a different network.", file=sys.stderr)
        sys.exit(2)

    return response.text, response.url


def extract_from_next_data(soup: BeautifulSoup) -> dict | None:
    """Tier 1: Extract property data from __NEXT_DATA__ script tag."""
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return None

    try:
        next_data = json.loads(script.string)
    except json.JSONDecodeError:
        return None

    # Navigate the nested structure to find property data
    # Zillow's NEXT_DATA structure varies, so we try multiple paths
    try:
        # Path 1: gdpClientCache contains a JSON string with property data
        page_props = next_data.get("props", {}).get("pageProps", {})

        # Try componentProps path
        gdp_cache = (
            page_props.get("componentProps", {}).get("gdpClientCache", None)
        )
        if gdp_cache:
            if isinstance(gdp_cache, str):
                gdp_cache = json.loads(gdp_cache)
            # gdp_cache is keyed by something, get the first value
            for value in gdp_cache.values():
                if isinstance(value, str):
                    value = json.loads(value)
                prop = value.get("property", {})
                if prop:
                    return _extract_property_fields(prop)

        # Path 2: initialData path
        initial_data = page_props.get("initialData", {})
        if initial_data:
            # Try to find property in various nested locations
            for key in ("property", "homeInfo", "listing"):
                if key in initial_data:
                    return _extract_property_fields(initial_data[key])

        # Path 3: Direct property data in pageProps
        if "property" in page_props:
            return _extract_property_fields(page_props["property"])

    except (KeyError, TypeError, json.JSONDecodeError):
        pass

    return None


def extract_from_json_ld(soup: BeautifulSoup) -> dict | None:
    """Tier 2: Extract from JSON-LD schema.org data."""
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                data = data[0]

            if not isinstance(data, dict):
                continue

            schema_type = data.get("@type", "")
            if schema_type not in (
                "SingleFamilyResidence",
                "Product",
                "RealEstateListing",
                "Residence",
            ):
                continue

            result = {}

            # Try to get price from offers
            offers = data.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = offers.get("price")
            if price:
                result["list_price"] = int(float(str(price).replace(",", "")))

            return result if result else None

        except (json.JSONDecodeError, ValueError, TypeError):
            continue

    return None


def extract_from_regex(html: str) -> dict | None:
    """Tier 3: Regex fallback for extracting data from raw HTML."""
    result = {}

    # Try to find status
    status_match = re.search(
        r'"(?:homeStatus|listingStatus|statusType)"\s*:\s*"([A-Z_]+)"', html
    )
    if status_match:
        result["status_raw"] = status_match.group(1)

    # Try to find prices
    price_match = re.search(r'"price"\s*:\s*(\d+)', html)
    if price_match:
        result["list_price"] = int(price_match.group(1))

    list_price_match = re.search(r'"listPrice"\s*:\s*(\d+)', html)
    if list_price_match:
        result["list_price"] = int(list_price_match.group(1))

    sold_price_match = re.search(r'"lastSoldPrice"\s*:\s*(\d+)', html)
    if sold_price_match:
        result["sale_price"] = int(sold_price_match.group(1))

    return result if result else None


def _extract_property_fields(prop: dict) -> dict:
    """Pull standard fields out of a Zillow property object."""
    result = {}

    # Status
    for key in ("homeStatus", "listingStatus", "statusType", "homeType"):
        val = prop.get(key)
        if val and val in STATUS_MAP:
            result["status_raw"] = val
            break

    if "status_raw" not in result:
        status = prop.get("homeStatus") or prop.get("listingStatus") or ""
        if status:
            result["status_raw"] = status

    # List price
    price = prop.get("price")
    if price is not None:
        try:
            result["list_price"] = int(float(str(price).replace(",", "")))
        except (ValueError, TypeError):
            pass

    # Also check listPrice
    list_price = prop.get("listPrice")
    if list_price is not None and "list_price" not in result:
        try:
            result["list_price"] = int(float(str(list_price).replace(",", "")))
        except (ValueError, TypeError):
            pass

    # Sale price
    sold_price = prop.get("lastSoldPrice")
    if sold_price is not None:
        try:
            result["sale_price"] = int(float(str(sold_price).replace(",", "")))
        except (ValueError, TypeError):
            pass

    return result


def format_price(price: int | None) -> str:
    """Format a price as $X,XXX,XXX or N/A."""
    if price is None:
        return "N/A"
    return f"${price:,}"


def lookup_property(address: str) -> dict:
    """Look up a property on Zillow and return its details."""
    url = build_zillow_url(address)

    try:
        html, final_url = fetch_page(url)
    except requests.ConnectionError:
        print("Error: Could not connect to Zillow.", file=sys.stderr)
        sys.exit(4)
    except requests.Timeout:
        print("Error: Request to Zillow timed out.", file=sys.stderr)
        sys.exit(4)
    except requests.RequestException as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(4)

    # Check if we landed on a listing page vs search results
    if "/homedetails/" not in final_url:
        print(
            "Error: Could not find a specific listing for that address.",
            file=sys.stderr,
        )
        print(f"Zillow search URL: {final_url}", file=sys.stderr)
        sys.exit(1)

    soup = BeautifulSoup(html, "lxml")

    # Try each extraction tier
    data = extract_from_next_data(soup)
    if not data:
        data = extract_from_json_ld(soup)
    if not data:
        data = extract_from_regex(html)
    if not data:
        data = {}

    # Also try regex to fill in any missing fields
    regex_data = extract_from_regex(html)
    if regex_data:
        for key, value in regex_data.items():
            if key not in data:
                data[key] = value

    # Map status
    status_raw = data.get("status_raw", "")
    status = STATUS_MAP.get(status_raw, status_raw or "Unknown")

    return {
        "address": address,
        "zillow_url": final_url,
        "status": status,
        "list_price": data.get("list_price"),
        "sale_price": data.get("sale_price"),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Look up Zillow listing information for a property address."
    )
    parser.add_argument(
        "address",
        help='Full property address (e.g., "123 Main St, Austin, TX 78701")',
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Output raw JSON instead of formatted text",
    )

    args = parser.parse_args()
    result = lookup_property(args.address)

    if args.output_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Property:   {result['address']}")
        print(f"Zillow URL: {result['zillow_url']}")
        print(f"Status:     {result['status']}")
        print(f"List Price: {format_price(result['list_price'])}")
        print(f"Sale Price: {format_price(result['sale_price'])}")


if __name__ == "__main__":
    main()
