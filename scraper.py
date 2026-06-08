import os
import json
import re
import ast
import time
from copy import deepcopy
import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
import soupsieve
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Any, Optional, Tuple

# --- Utility Functions ---
def safe_filename(name: str) -> str:
    """Normalize string for use as a filename or folder name."""
    if not name:
        return 'unnamed'
    s = name.strip().lower()
    s = re.sub(r'\s+', '_', s)
    s = re.sub(r'[<>:"/\\|?*]+', '_', s)
    s = re.sub(r'[^A-Za-z0-9_.-]', '_', s)
    s = re.sub(r'_+', '_', s)
    s = s.strip('._')
    return s[:200] or 'unnamed'


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def download_image(url: str, dest_path: str, timeout: int = 10) -> bool:
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return True
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()
        with open(dest_path, 'wb') as f:
            for chunk in resp.iter_content(1024):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"[WARN] Failed to download {url}: {e}")
        return False


def get_file_extension(url: str) -> str:
    parsed = urlparse(url)
    ext = os.path.splitext(parsed.path)[1].lower().lstrip('.')
    return ext


# --- Scraper Core ---
class Scraper:
    def __init__(self, config_path: str):
        with open(config_path, encoding='utf-8') as f:
            self.config = json.load(f)
        self.brand = self.config['brand']
        output_cfg = self.config.get('output', {})
        self.output_root = output_cfg.get('root_dir') or 'output'
        self.data_root = output_cfg.get('data_root_dir') or self.output_root
        self.media_root = output_cfg.get('media_root_dir') or 'media'
        self.images_root = output_cfg.get('images_root_dir') or os.path.join(self.media_root, 'images')
        self.logs_root = output_cfg.get('logs_root_dir') or 'logs_execution'
        self.brand_root = self.get_brand_root(self.brand)
        self.data_dir = self.get_data_dir(self.brand)
        self.image_dir = self.get_images_dir(self.brand)
        ensure_dir(self.data_dir)
        ensure_dir(self.image_dir)
        ensure_dir(self.get_logs_dir(self.brand))
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; WebScraper/1.0)'})
        self._html_cache: Dict[str, str] = {}
        self.current_product_context: Dict[str, Any] = {}
        self._asset_log_seen = set()
        self.run_log: Dict[str, Any] = {
            'brand': self.brand,
            'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'config': {'brand': self.brand},
            'pages': [],
            'assets': {
                'images': [],
                'documents': [],
                'video': [],
                'links': [],
            },
            'warnings': [],
        }

    def scrape(self, limit_products: Optional[int] = None):
        pages: List[Dict[str, Any]] = []
        all_products: List[Dict[str, Any]] = []
        save_per_link_files = self.normalize_truthy_flag(
            self.config.get('output', {}).get('save_per_link_files', True)
        )
        save_combined_file = self.normalize_truthy_flag(
            self.config.get('output', {}).get('save_combined_file', True)
        )

        for idx, page in enumerate(self.resolve_links(), start=1):
            page_url = page.get('url')
            print(f"[INFO] Scraping: {page_url}")
            soup = self.fetch_soup(page_url)
            if not soup:
                continue
            structured_sources = self.discover_structured_sources(soup, page_url)

            page_title = self.extract_page_title(soup, page.get('page_title_selector'))
            if not page_title:
                parsed = urlparse(page_url or '')
                fallback = (parsed.netloc + parsed.path) if parsed.netloc or parsed.path else page_url
                page_title = fallback or 'unnamed'
                print(f"[WARN] Page title not found for {page_url}; using fallback '{page_title}'")

            print(f"[INFO] Page: {page_title}")
            page_category = self.resolve_category(page.get('category'), page_title)
            page_result: Dict[str, Any] = {
                'source_url': page_url,
                'page_title': page_title,
                'category': page_category,
            }

            if self.is_listing_page(page):
                products = self.extract_listing_products_from_structured_sources(
                    structured_sources,
                    page,
                    page_category,
                    page_url,
                )
                if products:
                    print(f"[INFO] Products found from structured/API data: {len(products)}")
                else:
                    products = self.extract_listing_products(soup, page, page_category, page_url)

                if not products and self.requires_rendered_dom(page):
                    rendered_soup = self.fetch_rendered_soup(page_url)
                    if rendered_soup:
                        rendered_sources = self.discover_structured_sources(rendered_soup, page_url)
                        products = self.extract_listing_products_from_structured_sources(
                            rendered_sources,
                            page,
                            page_category,
                            page_url,
                        )
                        if not products:
                            products = self.extract_listing_products(rendered_soup, page, page_category, page_url)

                products = self.deduplicate_products(products)
                if limit_products is not None:
                    products = products[:max(0, int(limit_products))]
                print(f"[INFO] Products found: {len(products)}")

                if self.config.get('details_page'):
                    self.enrich_products_with_details(products)

                products = self.normalize_products_for_output(products, page_category, page_url)
                page_result['products'] = products
                all_products.extend(products)
                self.run_log['pages'].append({
                    'source_url': page_url,
                    'page_title': page_title,
                    'category': page_category,
                    'products_found': len(products),
                    'products': [
                        {
                            'name': product.get('name'),
                            'url': product.get('url'),
                            'manufacturer_category': product.get('manufacturer_category') or product.get('category'),
                        }
                        for product in products
                    ],
                })
            else:
                specifications = self.process_extract_rules(soup, self.config.get('extract', []))
                image_blocks = self.config.get('images', [])
                if isinstance(image_blocks, dict):
                    image_blocks = image_blocks.get('sources', [])
                images = self.process_image_blocks(soup, page_url, image_blocks)
                page_result['specifications'] = specifications
                page_result['images'] = images
                self.run_log['pages'].append(page_result)

            pages.append(page_result)
            if save_per_link_files:
                page_products = page_result.get('products')
                self.save_single_link_output(page, page_result, page_products, idx)

        all_products = self.deduplicate_products(all_products)
        if save_combined_file:
            self.save_output(pages, all_products)
        self.save_execution_log()

    def fetch_html(self, url: str, timeout: int = 20, retries: int = 3) -> Optional[str]:
        if not url:
            return None
        if url in self._html_cache:
            return self._html_cache[url]
        last_error = None
        for attempt in range(retries):
            try:
                resp = self.session.get(url, timeout=timeout)
                resp.raise_for_status()
                if resp.apparent_encoding:
                    resp.encoding = resp.apparent_encoding
                self._html_cache[url] = resp.text
                return resp.text
            except Exception as e:
                last_error = e
                if attempt < retries - 1:
                    time.sleep(0.5 + attempt * 0.5)
                    continue
                break
        print(f"[ERROR] Could not fetch {url}: {last_error}")
        return None

    def fetch_json(self, url: str, timeout: int = 20) -> Optional[Any]:
        try:
            resp = self.session.get(url, timeout=timeout, headers={'Accept': 'application/json, text/plain, */*'})
            resp.raise_for_status()
            text = resp.text.strip()
            content_type = (resp.headers.get('content-type') or '').lower()
            if 'json' not in content_type and not text.startswith(('{', '[')):
                return None
            return resp.json()
        except Exception as e:
            print(f"[WARN] Could not fetch JSON {url}: {e}")
            return None

    def fetch_soup(self, url: str) -> Optional[BeautifulSoup]:
        html = self.fetch_html(url)
        if html is None:
            return None
        return BeautifulSoup(html, 'html.parser')

    def fetch_rendered_soup(self, url: str) -> Optional[BeautifulSoup]:
        local_playwright_browsers = os.path.join(os.getcwd(), '.venv', 'ms-playwright')
        if not os.environ.get('PLAYWRIGHT_BROWSERS_PATH') and os.path.isdir(local_playwright_browsers):
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = local_playwright_browsers

        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            print(f"[WARN] Playwright fallback unavailable for {url}: {e}")
            return None

        timeout_ms = int(self.config.get('playwright', {}).get('timeout_ms', 30000))
        wait_until = self.config.get('playwright', {}).get('wait_until', 'networkidle')
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                html = page.content()
                browser.close()
            print(f"[INFO] Rendered DOM fetched with Playwright: {url}")
            return BeautifulSoup(html, 'html.parser')
        except Exception as e:
            print(f"[WARN] Playwright fallback failed for {url}: {e}")
            return None

    def requires_rendered_dom(self, cfg: Dict[str, Any]) -> bool:
        return self.normalize_truthy_flag(cfg.get('requires_rendered_dom', False))

    def discover_structured_sources(self, soup: BeautifulSoup, base_url: str) -> Dict[str, Any]:
        sources: Dict[str, Any] = {
            'json_ld': [],
            'next_data': None,
            'api_json': [],
            'api_candidates': [],
            'script_srcs': [],
        }

        for script in soup.find_all('script'):
            script_type = (script.get('type') or '').lower()
            script_id = script.get('id')
            text = script.string or script.get_text() or ''

            if script.get('src'):
                script_url = urljoin(base_url, script.get('src'))
                sources['script_srcs'].append(script_url)

            if script_type == 'application/ld+json':
                parsed = self.parse_json_text(text)
                if parsed is not None:
                    if isinstance(parsed, list):
                        sources['json_ld'].extend(parsed)
                    else:
                        sources['json_ld'].append(parsed)

            if script_id == '__NEXT_DATA__':
                sources['next_data'] = self.parse_json_text(text)

            for candidate in self.extract_api_candidates_from_text(text, base_url):
                if candidate not in sources['api_candidates']:
                    sources['api_candidates'].append(candidate)

        for candidate in self.next_data_api_candidates(sources.get('next_data'), base_url):
            if candidate not in sources['api_candidates']:
                sources['api_candidates'].append(candidate)

        for candidate in sources['api_candidates'][:8]:
            data = self.fetch_json(candidate)
            if data is not None:
                sources['api_json'].append({'url': candidate, 'data': data})

        if sources['json_ld'] or sources['next_data'] or sources['api_json']:
            print(
                "[INFO] Structured sources: "
                f"json_ld={len(sources['json_ld'])}, "
                f"next_data={bool(sources['next_data'])}, "
                f"api_json={len(sources['api_json'])}"
            )

        return sources

    def parse_json_text(self, text: str) -> Optional[Any]:
        text = (text or '').strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    def extract_api_candidates_from_text(self, text: str, base_url: str) -> List[str]:
        if not text:
            return []

        candidates: List[str] = []
        pattern = re.compile(r"""['"]((?:https?:)?//[^'"]+|/[^'"]*(?:api|\.json)[^'"]*)['"]""", re.IGNORECASE)
        base_host = urlparse(base_url).netloc

        for match in pattern.finditer(text):
            raw = match.group(1)
            if raw.startswith('//'):
                raw = f"{urlparse(base_url).scheme or 'https'}:{raw}"
            url = urljoin(base_url, raw)
            parsed = urlparse(url)
            if parsed.netloc and parsed.netloc != base_host:
                continue
            if not (parsed.path.endswith('.json') or '/api/' in parsed.path.lower() or parsed.path.lower().endswith('/api')):
                continue
            if url not in candidates:
                candidates.append(url)

        return candidates

    def next_data_api_candidates(self, next_data: Any, base_url: str) -> List[str]:
        if not isinstance(next_data, dict):
            return []

        build_id = next_data.get('buildId')
        as_path = next_data.get('asPath') or next_data.get('page')
        if not build_id or not as_path:
            return []

        parsed_as_path = urlparse(str(as_path))
        path = parsed_as_path.path or '/'
        if path == '/':
            path = '/index'
        paths = [path]
        base_parts = [part for part in urlparse(base_url).path.split('/') if part]
        if base_parts:
            locale_prefix = base_parts[0]
            if len(locale_prefix) == 2 and not path.startswith(f'/{locale_prefix}/'):
                paths.insert(0, f'/{locale_prefix}{path}')

        return [urljoin(base_url, f"/_next/data/{build_id}{candidate}.json") for candidate in paths]

    def extract_listing_products_from_structured_sources(
        self,
        sources: Dict[str, Any],
        page_cfg: Dict[str, Any],
        page_category: str,
        page_url: str,
    ) -> List[Dict[str, Any]]:
        products: List[Dict[str, Any]] = []

        for item in sources.get('json_ld', []):
            products.extend(self.extract_products_from_structured_value(item, page_category, page_url))

        next_data = sources.get('next_data')
        if next_data:
            products.extend(self.extract_products_from_structured_value(next_data, page_category, page_url))

        for api_source in sources.get('api_json', []):
            api_products = self.extract_products_from_structured_value(
                api_source.get('data'),
                page_category,
                page_url,
            )
            for product in api_products:
                product['source_url'] = api_source.get('url') or page_url
            products.extend(api_products)

        return self.deduplicate_products(products)

    def extract_products_from_structured_value(self, value: Any, page_category: str, base_url: str) -> List[Dict[str, Any]]:
        products: List[Dict[str, Any]] = []
        for node in self.walk_dicts(value):
            product = self.product_from_structured_dict(node, page_category, base_url)
            if product:
                products.append(product)
        return products

    def walk_dicts(self, value: Any) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        if isinstance(value, dict):
            found.append(value)
            for child in value.values():
                found.extend(self.walk_dicts(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(self.walk_dicts(child))
        return found

    def product_from_structured_dict(self, data: Dict[str, Any], page_category: str, base_url: str) -> Optional[Dict[str, Any]]:
        if not isinstance(data, dict):
            return None

        data_type = data.get('@type') or data.get('type')
        type_values = data_type if isinstance(data_type, list) else [data_type]
        is_schema_product = any(str(value).lower() == 'product' for value in type_values if value)
        productish_keys = {'productName', 'product_name', 'model', 'sku', 'slug', 'images', 'image'}
        looks_productish = bool(productish_keys.intersection(data.keys()))

        name = (
            data.get('name')
            or data.get('productName')
            or data.get('product_name')
            or data.get('title')
        )
        name = self.normalize_text(name)
        if not name:
            return None
        if self.looks_like_error_page_text(name):
            return None

        raw_url = data.get('url') or data.get('href') or data.get('link') or data.get('path') or data.get('slug')
        detail_url = self.normalize_structured_url(raw_url, base_url)
        image_url = self.first_image_url(
            data.get('image') or data.get('images') or data.get('thumbnail') or data.get('thumbnailUrl'),
            base_url,
        )

        if not is_schema_product and not (looks_productish and (detail_url or image_url)):
            return None

        product: Dict[str, Any] = {
            'category': page_category,
            'name': name,
            'product_name': name,
            'description': self.normalize_text(data.get('description') or data.get('summary')),
            'model': self.normalize_text(data.get('model') or data.get('sku') or data.get('mpn')) or None,
            'detail_url': detail_url,
            'listing_image_url': image_url,
            'raw_structured_data': data,
        }

        return {key: value for key, value in product.items() if value not in (None, '', [])}

    def normalize_structured_url(self, value: Any, base_url: str) -> Optional[str]:
        if isinstance(value, dict):
            value = value.get('url') or value.get('@id')
        if isinstance(value, list):
            value = next((item for item in value if isinstance(item, (str, dict))), None)
            return self.normalize_structured_url(value, base_url)
        if not isinstance(value, str) or not value.strip():
            return None
        return urljoin(base_url, value.strip())

    def first_image_url(self, value: Any, base_url: str) -> Optional[str]:
        urls = self.collect_image_urls(value, base_url)
        return urls[0] if urls else None

    def collect_image_urls(self, value: Any, base_url: str = '') -> List[str]:
        urls: List[str] = []

        def add(candidate: Any):
            if not isinstance(candidate, str) or not candidate.strip():
                return
            candidate = candidate.strip()
            if candidate.startswith('data:'):
                return
            if re.search(r'\.(?:png|jpe?g|webp|avif)(?:[?#].*)?$', candidate, re.IGNORECASE) or candidate.startswith(('http', '/', '//')):
                normalized = urljoin(base_url, candidate)
                if normalized not in urls:
                    urls.append(normalized)

        def walk(node: Any):
            if isinstance(node, str):
                add(node)
            elif isinstance(node, dict):
                for key in ('url', 'src', 'href', 'image', 'images', 'thumbnail', 'thumbnailUrl'):
                    if key in node:
                        walk(node[key])
                for child in node.values():
                    if isinstance(child, (dict, list)):
                        walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(value)
        return urls

    def extract_detail_from_structured_sources(
        self,
        sources: Dict[str, Any],
        product: Dict[str, Any],
        base_url: str,
    ) -> Dict[str, Any]:
        candidates: List[Dict[str, Any]] = []
        for item in sources.get('json_ld', []):
            candidates.extend(self.walk_dicts(item))
        if sources.get('next_data'):
            candidates.extend(self.walk_dicts(sources['next_data']))
        for api_source in sources.get('api_json', []):
            candidates.extend(self.walk_dicts(api_source.get('data')))

        product_name = self.normalize_text(product.get('product_name') or product.get('name')).casefold()
        product_url = self.normalize_text(product.get('detail_url')).casefold()
        best: Optional[Dict[str, Any]] = None
        for candidate in candidates:
            mapped = self.product_from_structured_dict(candidate, product.get('category', ''), base_url)
            if not mapped:
                continue
            candidate_name = self.normalize_text(mapped.get('product_name') or mapped.get('name')).casefold()
            candidate_url = self.normalize_text(mapped.get('detail_url')).casefold()
            if product_url and candidate_url and product_url == candidate_url:
                best = candidate
                break
            if product_name and candidate_name and (product_name == candidate_name or product_name in candidate_name or candidate_name in product_name):
                best = candidate
                break
            if best is None:
                best = candidate

        if not best:
            return {}

        detail: Dict[str, Any] = {}
        mapped = self.product_from_structured_dict(best, product.get('category', ''), base_url) or {}
        for key in ('product_name', 'name', 'description', 'model'):
            if mapped.get(key):
                detail[key] = mapped[key]

        image_urls = self.collect_image_urls(
            best.get('image') or best.get('images') or best.get('thumbnail') or best.get('thumbnailUrl'),
            base_url,
        )
        if image_urls:
            detail['images'] = self.materialize_image_urls(image_urls, detail.get('product_name') or product.get('product_name'), 'primary_image')

        additional_props = best.get('additionalProperty') or best.get('additionalProperties')
        specs = self.extract_specs_from_additional_properties(additional_props)
        if specs:
            detail['specifications'] = specs
        return detail

    def extract_specs_from_additional_properties(self, value: Any) -> List[Dict[str, Any]]:
        if not value:
            return []
        items = value if isinstance(value, list) else [value]
        specs: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = self.normalize_text(item.get('name') or item.get('propertyID'))
            val = self.normalize_text(item.get('value') or item.get('description'))
            if name or val:
                specs.append({'title': name, 'values': [val] if val else []})
        return specs

    def is_listing_page(self, page_cfg: Dict[str, Any]) -> bool:
        selectors = page_cfg.get('selectors', {})
        return page_cfg.get('type') == 'listing' or bool(selectors.get('product_container') or selectors.get('product_containers'))

    def extract_listing_products(
        self,
        soup: BeautifulSoup,
        page_cfg: Dict[str, Any],
        page_category: str,
        page_url: str,
    ) -> List[Dict[str, Any]]:
        selectors = page_cfg.get('selectors', {})
        product_container_sel = selectors.get('product_container') or selectors.get('product_containers')
        fields = selectors.get('fields', [])

        if not product_container_sel:
            return []

        products: List[Dict[str, Any]] = []
        base_url = page_cfg.get('url', '')
        containers = self.query_all_first_match(
            soup,
            product_container_sel,
            context=f"{page_url} product containers",
        )
        if not containers:
            print(f"[WARN] Listing page without product containers: {page_url}")
            return []

        for container in containers:
            item: Dict[str, Any] = {'category': page_category}
            for field in fields:
                key = field.get('key')
                if not key:
                    continue
                item[key] = self.extract_field_value(container, field, base_url)

            if not item.get('product_name') and item.get('name'):
                item['product_name'] = item.get('name')

            detail_url = item.get('detail_url')
            if isinstance(detail_url, str) and detail_url:
                item['detail_url'] = urljoin(base_url, detail_url)

            products.append(item)

        return products

    def product_dedup_key(self, product: Dict[str, Any]) -> str:
        detail_url = self.normalize_text(product.get('detail_url') or product.get('url'))
        if detail_url:
            return f"url:{detail_url.casefold()}"

        product_name = self.normalize_text(product.get('product_name') or product.get('name'))
        if product_name:
            return f"name:{product_name.casefold()}"

        listing_image_url = self.normalize_text(product.get('listing_image_url'))
        if listing_image_url:
            return f"img:{listing_image_url.casefold()}"

        return f"raw:{json.dumps(product, sort_keys=True, ensure_ascii=False)}"

    def deduplicate_products(self, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        deduped: List[Dict[str, Any]] = []
        for product in products:
            key = self.product_dedup_key(product)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(product)
        return deduped

    def normalize_products_for_output(
        self,
        products: List[Dict[str, Any]],
        page_category: str,
        page_url: str,
    ) -> List[Dict[str, Any]]:
        return [self.normalize_product_for_output(product, page_category, page_url) for product in products]

    def normalize_product_for_output(
        self,
        product: Dict[str, Any],
        page_category: str,
        page_url: str,
    ) -> Dict[str, Any]:
        name = self.normalize_text(product.get('product_name') or product.get('name'))
        detail_url = product.get('detail_url') or product.get('url')
        category = (
            product.get('manufacturer_category')
            or product.get('category')
            or page_category
        )
        if self.normalize_text(category).casefold() == 'products':
            category = self.infer_manufacturer_category(product)
        product_type = product.get('product_type') or product.get('type') or category

        features = self.clean_features(product)

        variants = (
            product.get('variants')
            or product.get('static_variant_candidates')
            or product.get('product_variants')
            or []
        )
        variants = self.clean_list_of_dicts(variants, required_any=('name', 'description'))
        related_products = self.clean_list_of_dicts(product.get('related_products') or product.get('related') or [], required_any=('name', 'url'))
        specs = self.clean_specs(product.get('specs') or product.get('specifications') or [])
        images = self.flatten_images(product.get('images'))
        max_images = self.config.get('images', {}).get('max_output_per_product') if isinstance(self.config.get('images'), dict) else None
        if isinstance(max_images, int) and max_images > 0:
            images = images[:max_images]
        documents = self.normalize_link_items(
            product.get('documents'),
            product.get('document_links'),
            product.get('datasheet_links'),
            product.get('asset_links'),
        )
        video = self.normalize_link_items(
            product.get('media'),
            product.get('media_links'),
            product.get('video_links'),
            product.get('detail_media_urls'),
        )
        for document in documents:
            self.record_asset('documents', document.get('url'), product_name=name, product_url=detail_url, source_page_url=detail_url, label=document.get('text'))
        for video_item in video:
            self.record_asset('video', video_item.get('url'), product_name=name, product_url=detail_url, source_page_url=detail_url, label=video_item.get('text'))

        item = {
            'brand': self.brand,
            'manufacturer_category': category,
            'type': None if self.normalize_text(product_type).casefold() == 'products' else product_type,
            'name': name or None,
            'model': product.get('model') or name or None,
            'url': detail_url,
            'images': images,
            'documents': documents,
            'video': video,
            'features': features,
            'specs': specs,
            'variants': variants,
            'related_products': related_products,
        }
        return {key: value for key, value in item.items() if value not in (None, '', [], {})}

    def infer_manufacturer_category(self, product: Dict[str, Any]) -> str:
        text = self.normalize_text(
            f"{product.get('manufacturer_category', '')} {product.get('name', '')} {product.get('product_name', '')} {product.get('detail_url', '')}"
        ).casefold()
        if self.brand.casefold() in {'verifone', 'verifon'}:
            if 'victa' in text:
                return 'Verifone Victa'
            if any(token in text for token in ('ux400', 'ux401', 'ux300', 'ux301', 'ux100', 'ux410', 'ux700')):
                return 'Unattended'
            if any(token in text for token in ('m450', 'm425', 'm400', 'm424', 'm440')):
                return 'Multilane'
            if any(token in text for token in ('p200', 'p400', 'p630')):
                return 'Countertop PINpad'
            if any(token in text for token in ('v200c', 'v400c', 't650c', 'v200t')):
                return 'Countertop'
            if any(token in text for token in ('v660p', 't650p', 't650m', 'e235', 'e280', 'e285', 'v400m')):
                return 'mPOS'
            if 'x990' in text:
                return 'Integrated POS'
        return self.resolve_category(product.get('category'), product.get('product_name') or product.get('name'))

    def clean_features(self, product: Dict[str, Any]) -> Dict[str, Any]:
        features: Dict[str, Any] = {}
        items = self.clean_list_of_dicts(product.get('features') or [], required_any=('title', 'text', 'name'))
        cards = self.clean_list_of_dicts(product.get('feature_cards') or [], required_any=('title', 'text'))
        paragraphs = self.clean_list_of_dicts(product.get('feature_paragraphs') or [], required_any=('title', 'text'))
        if items:
            features['items'] = items
        if cards:
            features['cards'] = cards
        if paragraphs:
            features['paragraphs'] = paragraphs
        return features

    def clean_list_of_dicts(self, values: Any, required_any: Tuple[str, ...]) -> List[Dict[str, Any]]:
        if not isinstance(values, list):
            return []
        out: List[Dict[str, Any]] = []
        seen = set()
        for value in values:
            if not isinstance(value, dict):
                continue
            cleaned = {
                key: self.normalize_text(val) if isinstance(val, str) else val
                for key, val in value.items()
                if val not in (None, '', [], {})
            }
            title = self.normalize_text(cleaned.get('title') or cleaned.get('name'))
            text = self.normalize_text(cleaned.get('text') or cleaned.get('description'))
            if re.fullmatch(r'feature\s*\d+', title, flags=re.IGNORECASE) and not text:
                continue
            if not any(cleaned.get(key) not in (None, '', [], {}) for key in required_any):
                continue
            sig = json.dumps(cleaned, sort_keys=True, ensure_ascii=False)
            if sig in seen:
                continue
            seen.add(sig)
            out.append(cleaned)
        return out

    def clean_specs(self, values: Any) -> List[Dict[str, Any]]:
        if not isinstance(values, list):
            return []
        out: List[Dict[str, Any]] = []
        seen = set()
        for value in values:
            if not isinstance(value, dict):
                continue
            title = self.normalize_text(value.get('title') or value.get('name') or value.get('variant'))
            raw_values = value.get('values') if isinstance(value.get('values'), list) else value.get('items')
            if raw_values is None and value.get('text'):
                raw_values = [value.get('text')]
            cleaned_values = [self.normalize_text(v) for v in (raw_values or []) if self.normalize_text(v)]
            nested_specs = self.clean_specs(value.get('specs') or [])
            if not title and not cleaned_values and not nested_specs:
                continue
            item: Dict[str, Any] = {}
            if title:
                item['title'] = title
            if cleaned_values:
                item['values'] = cleaned_values
            if nested_specs:
                item['specs'] = nested_specs
            sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if sig in seen:
                continue
            seen.add(sig)
            out.append(item)
        return out

    def flatten_images(self, images: Any) -> List[str]:
        flattened: List[str] = []

        def add(value: Any):
            if isinstance(value, str) and value and value not in flattened:
                flattened.append(value)
            elif isinstance(value, list):
                for item in value:
                    add(item)
            elif isinstance(value, dict):
                for item in value.values():
                    add(item)

        add(images)
        return flattened

    def normalize_link_items(self, *values: Any) -> List[Dict[str, str]]:
        links: List[Dict[str, str]] = []
        seen = set()

        def add(text: Any, url: Any):
            if not isinstance(url, str) or not url.strip():
                return
            url = url.strip()
            key = url.casefold()
            if key in seen:
                return
            seen.add(key)
            links.append({
                'text': self.normalize_text(text) or os.path.basename(urlparse(url).path) or url,
                'url': url,
            })

        def walk(value: Any):
            if not value:
                return
            if isinstance(value, str):
                add('', value)
            elif isinstance(value, dict):
                add(value.get('text') or value.get('label') or value.get('name') or value.get('title'), value.get('url') or value.get('href') or value.get('src'))
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        for value in values:
            walk(value)
        return links

    def extract_field_value(self, container: BeautifulSoup, field_cfg: Dict[str, Any], base_url: str) -> Any:
        selector = field_cfg.get('selector') or field_cfg.get('selectors')
        mode = field_cfg.get('mode', 'text')
        multiple = bool(field_cfg.get('multiple', False))
        normalize_url = bool(field_cfg.get('normalize_url', False))
        value_map = field_cfg.get('value_map', {})

        if mode == 'static':
            return field_cfg.get('value')
        if mode == 'bool':
            return bool(field_cfg.get('value'))

        search_root = container
        ancestor_selector = field_cfg.get('ancestor_selector')
        if ancestor_selector:
            ancestor = self.closest_ancestor(container, ancestor_selector)
            if ancestor:
                search_root = ancestor

        if not selector:
            return [] if multiple else None

        nodes = self.query_all_first_match(search_root, selector)
        if not nodes:
            return [] if multiple else None

        def read(node):
            if mode == 'text':
                return self.normalize_text(node.get_text(' ', strip=True))
            if mode == 'attr':
                attr = field_cfg.get('attr', 'href')
                value = node.get(attr)
                if value and normalize_url:
                    value = urljoin(base_url, value)
                return value
            if mode == 'static':
                return field_cfg.get('value')
            if mode == 'bool':
                return bool(field_cfg.get('value'))
            return node.get_text(strip=True)

        if multiple:
            return [v for v in (read(n) for n in nodes) if v not in (None, '')]

        value = read(nodes[0])
        if isinstance(value_map, dict) and value in value_map:
            value = value_map[value]
        elif 'value_map_default' in field_cfg:
            value = field_cfg.get('value_map_default')
        return value

    def closest_ancestor(self, node: BeautifulSoup, selector: Any) -> Optional[BeautifulSoup]:
        selectors = self.selector_candidates(selector)
        current = node
        while current and getattr(current, 'name', None):
            for candidate in selectors:
                try:
                    if soupsieve.match(candidate, current):
                        return current
                except Exception:
                    continue
            current = current.parent
        return None

    def enrich_products_with_details(self, products: List[Dict[str, Any]]):
        for product in products:
            self.enrich_product_with_detail(product)

    def enrich_product_with_detail(self, product: Dict[str, Any]):
        detail_page_url = product.get('detail_url')
        if not detail_page_url:
            return

        print(f"[INFO] Detail: {detail_page_url}")
        self.current_product_context = {
            'name': product.get('product_name') or product.get('name'),
            'url': detail_page_url,
        }
        soup = self.fetch_soup(detail_page_url)
        if not soup:
            self.apply_listing_image_fallback(product, detail_page_url)
            self.current_product_context = {}
            return

        self.apply_detail_soup(product, soup, detail_page_url)

        detail_cfg = self.config.get('details_page', {})
        if self.requires_rendered_dom(detail_cfg) and not self.product_has_detail_content(product):
            rendered_soup = self.fetch_rendered_soup(detail_page_url)
            if rendered_soup:
                self.apply_detail_soup(product, rendered_soup, detail_page_url)

        if not self.has_any_images(product.get('images', {})):
            self.apply_listing_image_fallback(product, detail_page_url)
        elif 'images' in product and not self.has_any_images(product.get('images', {})):
            product.pop('images', None)
        self.current_product_context = {}

    def apply_detail_soup(self, product: Dict[str, Any], soup: BeautifulSoup, detail_page_url: str):
        structured_sources = self.discover_structured_sources(soup, detail_page_url)
        structured_detail = self.extract_detail_from_structured_sources(structured_sources, product, detail_page_url)
        self.merge_nonempty_product_data(product, structured_detail)

        current_name = product.get('product_name') or product.get('name')
        detail_name = self.extract_detail_product_name(soup)
        if self.should_replace_product_name(current_name, detail_name):
            product['product_name'] = detail_name

        detail_data = self.extract_detail_data(soup, product.get('product_name'), detail_page_url)
        self.merge_nonempty_product_data(product, detail_data)

        images = self.extract_images_from_config(soup, detail_page_url, product.get('product_name'))
        if self.has_any_images(images):
            product['images'] = self.merge_image_dicts(product.get('images', {}), images)

    def merge_nonempty_product_data(self, product: Dict[str, Any], data: Dict[str, Any]):
        for key, value in (data or {}).items():
            if value in (None, '', []):
                continue
            if key == 'images' and isinstance(value, dict):
                product['images'] = self.merge_image_dicts(product.get('images', {}), value)
            elif key in {'product_name', 'name'}:
                current_name = product.get('product_name') or product.get('name')
                if self.should_replace_product_name(current_name, value):
                    product[key] = value
            else:
                product[key] = value

    def product_has_detail_content(self, product: Dict[str, Any]) -> bool:
        detail_keys = (
            'specifications',
            'specs',
            'feature_cards',
            'feature_paragraphs',
            'features',
            'datasheet_links',
            'detail_image_urls',
            'static_variant_candidates',
            'variants',
        )
        if any(product.get(key) not in (None, '', []) for key in detail_keys):
            return True
        return self.has_any_images(product.get('images', {}))

    def apply_listing_image_fallback(self, product: Dict[str, Any], base_url: str):
        listing_img = product.get('listing_image_url')
        if not listing_img:
            return

        listing_img_url = urljoin(base_url, listing_img)
        image_cfg = self.config.get('images', {})
        download_flag = bool(image_cfg.get('download', False)) if isinstance(image_cfg, dict) else False

        if not download_flag:
            product['images'] = {'primary_image': [listing_img_url]}
            return

        allowed_exts = set(ext.lower() for ext in image_cfg.get('allowed_extensions', []))
        naming = image_cfg.get('naming', 'image_{index}')
        out_dir, token_values = self.resolve_image_output_dir(product.get('product_name'))

        ext = get_file_extension(listing_img_url) or 'jpg'
        if allowed_exts and ext.lower() not in allowed_exts:
            product['images'] = {'primary_image': [listing_img_url]}
            return

        ensure_dir(out_dir)
        base_name = self.render_template(naming, {**token_values, 'index': 1, 'key': 'primary_image', 'ext': ext}) or 'image_1'
        file_name = f"{safe_filename(base_name)}.{ext}"
        image_path = os.path.join(out_dir, file_name)
        if download_image(listing_img_url, image_path):
            rel = os.path.relpath(image_path, '.').replace('\\', '/')
            product['images'] = {'primary_image': [rel]}
            self.record_asset(
                'images',
                listing_img_url,
                local_path=rel,
                product_name=product.get('product_name'),
                product_url=base_url,
                source_page_url=base_url,
                key='primary_image',
                downloaded=True,
            )
        else:
            product['images'] = {'primary_image': [listing_img_url]}

    def has_any_images(self, images: Dict[str, Any]) -> bool:
        if not isinstance(images, dict):
            return False
        for value in images.values():
            if isinstance(value, list) and value:
                return True
        return False

    def merge_image_dicts(self, current: Any, new_images: Any) -> Dict[str, List[str]]:
        merged: Dict[str, List[str]] = {}
        for source in (current, new_images):
            if not isinstance(source, dict):
                continue
            for key, values in source.items():
                if not isinstance(values, list):
                    values = [values] if values else []
                bucket = merged.setdefault(key, [])
                for value in values:
                    if value and value not in bucket:
                        bucket.append(value)
        return {key: values for key, values in merged.items() if values}

    def materialize_image_urls(
        self,
        image_urls: List[str],
        product_name: Optional[str],
        key: str = 'images',
        start_index: int = 1,
    ) -> Dict[str, List[str]]:
        image_cfg = self.config.get('images', {})
        download_flag = bool(image_cfg.get('download', False)) if isinstance(image_cfg, dict) else False
        allowed_exts = set(ext.lower() for ext in image_cfg.get('allowed_extensions', [])) if isinstance(image_cfg, dict) else set()
        naming = image_cfg.get('naming', 'image_{index}') if isinstance(image_cfg, dict) else 'image_{index}'
        out_dir, token_values = self.resolve_image_output_dir(product_name)

        collected: List[str] = []
        image_index = start_index
        for image_url in image_urls:
            if not image_url or image_url in collected:
                continue

            ext = get_file_extension(image_url) or 'jpg'
            if allowed_exts and ext.lower() not in allowed_exts:
                continue

            if download_flag:
                ensure_dir(out_dir)
                naming_tokens = {
                    **token_values,
                    'index': image_index,
                    'key': key,
                    'ext': ext,
                }
                base_name = self.render_template(naming, naming_tokens) or f"image_{image_index}"
                file_name = f"{safe_filename(base_name)}.{ext}"
                image_path = os.path.join(out_dir, file_name)
                if download_image(image_url, image_path):
                    rel_path = os.path.relpath(image_path, '.').replace('\\', '/')
                    collected.append(rel_path)
                    self.record_asset(
                        'images',
                        image_url,
                        local_path=rel_path,
                        product_name=product_name,
                        product_url=self.current_product_context.get('url'),
                        source_page_url=self.current_product_context.get('url'),
                        key=key,
                        downloaded=True,
                    )
                    image_index += 1
            else:
                collected.append(image_url)
                self.record_asset(
                    'images',
                    image_url,
                    product_name=product_name,
                    product_url=self.current_product_context.get('url'),
                    source_page_url=self.current_product_context.get('url'),
                    key=key,
                    downloaded=False,
                )
                image_index += 1

        return {key: collected} if collected else {}

    def record_asset(
        self,
        kind: str,
        source_url: Optional[str],
        local_path: Optional[str] = None,
        product_name: Optional[str] = None,
        product_url: Optional[str] = None,
        source_page_url: Optional[str] = None,
        key: Optional[str] = None,
        label: Optional[str] = None,
        downloaded: bool = False,
    ):
        if not source_url:
            return
        if kind == 'media':
            kind = 'video'
        bucket = kind if kind in self.run_log['assets'] else 'links'
        entry = {
            'product_name': self.normalize_text(product_name or self.current_product_context.get('name')),
            'product_url': product_url or self.current_product_context.get('url'),
            'source_page_url': source_page_url or self.current_product_context.get('url'),
            'source_url': source_url,
            'local_path': local_path,
            'key': key,
            'label': label,
            'downloaded': downloaded,
        }
        entry = {k: v for k, v in entry.items() if v not in (None, '', [], {})}
        sig = (bucket, entry.get('product_url'), entry.get('source_url'), entry.get('local_path'))
        if sig in self._asset_log_seen:
            return
        self._asset_log_seen.add(sig)
        self.run_log['assets'][bucket].append(entry)

    def normalize_truthy_flag(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value == 1
        if isinstance(value, str):
            return value.strip().lower() in {'1', 'true', 'yes', 'on'}
        return False

    def normalize_text(self, value: Optional[str]) -> str:
        if value is None:
            return ''
        return re.sub(r'\s+', ' ', str(value)).strip()

    def render_template(self, template: Any, token_values: Dict[str, Any]) -> str:
        """Render {tokens} safely; unknown tokens are replaced with empty string."""
        if template is None:
            return ''
        text = str(template)
        return re.sub(r'\{([^{}]+)\}', lambda m: str(token_values.get(m.group(1), '')), text)

    def looks_like_product_description(self, value: Optional[str]) -> bool:
        text = self.normalize_text(value)
        if not text:
            return False

        words = text.split()
        if len(words) >= 12:
            return True

        if len(words) >= 8 and any(ch in text for ch in '.!?'):
            return True

        return False

    def looks_like_error_page_text(self, value: Optional[str]) -> bool:
        text = self.normalize_text(value).casefold()
        if not text:
            return False
        return any(
            marker in text
            for marker in (
                "page you're looking",
                "can't be found",
                '404',
                'not found',
            )
        )

    def should_replace_product_name(self, current_name: Optional[str], candidate_name: Optional[str]) -> bool:
        current = self.normalize_text(current_name)
        candidate = self.normalize_text(candidate_name)

        if not candidate:
            return False
        if self.looks_like_error_page_text(candidate):
            return False

        if not current:
            return True

        if candidate.casefold() == current.casefold():
            return False

        current_is_description = self.looks_like_product_description(current)
        candidate_is_description = self.looks_like_product_description(candidate)

        # Keep short "title-like" names over long marketing descriptions.
        if candidate_is_description and not current_is_description:
            return False

        if current_is_description and not candidate_is_description:
            return True

        return len(candidate) < len(current)

    def extract_detail_product_name(self, soup: BeautifulSoup) -> Optional[str]:
        detail_cfg = self.config.get('details_page', {})
        selectors_cfg = detail_cfg.get('selectors', {})
        name_rules = selectors_cfg.get('product_name', [])

        for rule in name_rules:
            value = self.extract_field_value(soup, rule, '')
            if isinstance(value, list):
                value = value[0] if value else None
            if value:
                return value

        h1 = soup.find('h1')
        if h1 and h1.get_text(strip=True):
            return h1.get_text(strip=True)

        return None

    def extract_detail_data(self, soup: BeautifulSoup, product_name: Optional[str], base_url: str = '') -> Dict[str, Any]:
        detail_cfg = self.config.get('details_page', {})
        rules = detail_cfg.get('extract', [])
        result: Dict[str, Any] = {}

        for rule in rules:
            key = rule.get('key')
            if not key:
                continue

            mode = rule.get('mode', 'container')
            if mode == 'grouped_sections':
                result[key] = self.extract_grouped_sections(soup, rule, product_name)
            elif mode == 'keyed_sections':
                result[key] = self.extract_keyed_sections(soup, rule)
            elif mode == 'repeat':
                result[key] = self.extract_repeat_items(soup, rule)
            elif mode == 'pairs':
                result[key] = self.extract_pairs(soup, rule)
            elif mode == 'paired_headings_paragraphs':
                result[key] = self.extract_paired_headings_paragraphs(soup, rule)
            elif mode == 'links':
                result[key] = self.extract_links(soup, rule, base_url)
            else:
                result[key] = self.process_extract_rules(soup, [rule]).get(key, [])

        return result

    def extract_links(self, soup: BeautifulSoup, rule: Dict[str, Any], base_url: str) -> List[Dict[str, str]]:
        selector = rule.get('selector') or rule.get('selectors') or 'a[href]'
        attr = rule.get('attr', 'href')
        attrs = rule.get('attrs')
        if isinstance(attrs, str):
            attr_candidates = [attrs]
        elif isinstance(attrs, list):
            attr_candidates = [value for value in attrs if isinstance(value, str) and value]
        else:
            attr_candidates = [attr, 'href', 'src', 'data-src', 'data-href']
        text_selector = rule.get('text_selector')
        include_patterns = [re.compile(p, re.IGNORECASE) for p in rule.get('include_if_matches', [])]
        exclude_patterns = [re.compile(p, re.IGNORECASE) for p in rule.get('exclude_if_matches', [])]
        normalize_url = bool(rule.get('normalize_url', True))

        links: List[Dict[str, str]] = []
        seen = set()
        for node in self.query_all_first_match(soup, selector):
            href = None
            if hasattr(node, 'get'):
                href = next((node.get(candidate) for candidate in attr_candidates if node.get(candidate)), None)
            if not href:
                continue

            url = urljoin(base_url, href) if normalize_url else href
            if normalize_url and url.split('#', 1)[0].rstrip('/') == base_url.split('#', 1)[0].rstrip('/'):
                continue
            label_node = self.query_first(node, text_selector) if text_selector else None
            label = label_node.get_text(' ', strip=True) if label_node else node.get_text(' ', strip=True)
            label = self.normalize_text(label)
            haystack = f"{label} {url}"

            if include_patterns and not any(pattern.search(haystack) for pattern in include_patterns):
                continue
            if exclude_patterns and any(pattern.search(haystack) for pattern in exclude_patterns):
                continue

            key = url.casefold()
            if key in seen:
                continue
            seen.add(key)
            links.append({'text': label, 'url': url})

        return links

    def extract_keyed_sections(self, soup: BeautifulSoup, rule: Dict[str, Any]) -> List[Dict[str, Any]]:
        container_sel = rule.get('container_selector')
        section_sel = rule.get('section_selector')
        title_sel = rule.get('title_selector')
        value_selectors = rule.get('value_selectors', [])
        cfg_rules = rule.get('rules', {}) if isinstance(rule.get('rules', {}), dict) else {}

        skip_if_title_empty = bool(cfg_rules.get('skip_if_title_empty', False))
        skip_if_no_values = bool(cfg_rules.get('skip_if_no_values', False))

        containers = self.query_all_first_match(soup, container_sel) if container_sel else [soup]
        out: List[Dict[str, Any]] = []

        for container in containers:
            sections = self.query_all_first_match(container, section_sel) if section_sel else []

            # Fallback: some pages don't use section containers, but do have h4 + list/table blocks.
            if not sections and title_sel:
                for heading in self.query_all_first_match(container, title_sel):
                    title = heading.get_text(strip=True)
                    if skip_if_title_empty and not title:
                        continue

                    values: List[str] = []
                    for sibling in heading.next_siblings:
                        if not getattr(sibling, 'name', None):
                            continue
                        if re.match(r'^h[1-6]$', sibling.name or ''):
                            break
                        values.extend(self.collect_section_values(sibling, value_selectors))

                    if skip_if_no_values and not values:
                        continue
                    out.append({'title': title, 'values': values})
                continue

            for section in sections:
                title = ''
                if title_sel:
                    title_el = self.query_first(section, title_sel)
                    if title_el:
                        title = title_el.get_text(strip=True)

                if skip_if_title_empty and not title:
                    continue

                values = self.collect_section_values(section, value_selectors)
                if skip_if_no_values and not values:
                    continue

                out.append({'title': title, 'values': values})

        return out

    def collect_section_values(self, section: BeautifulSoup, value_selectors: List[Dict[str, Any]]) -> List[str]:
        values: List[str] = []

        for value_rule in value_selectors:
            v_mode = value_rule.get('mode', 'text')
            v_sel = value_rule.get('selector')
            if not v_sel:
                continue

            if v_mode == 'list':
                for el in self.query_all_first_match(section, v_sel):
                    txt = el.get_text(strip=True)
                    if txt:
                        values.append(txt)
            elif v_mode == 'table':
                for table in self.query_all_first_match(section, v_sel):
                    rows = table.find_all('tr')
                    if rows:
                        for row in rows:
                            row_text = ' '.join(td.get_text(strip=True) for td in row.find_all(['td', 'th']))
                            if row_text:
                                values.append(row_text)
                    else:
                        txt = table.get_text(strip=True)
                        if txt:
                            values.append(txt)
            else:
                for el in self.query_all_first_match(section, v_sel):
                    txt = el.get_text(strip=True)
                    if txt:
                        values.append(txt)

        return values

    def extract_repeat_items(self, soup: BeautifulSoup, rule: Dict[str, Any]) -> List[Dict[str, Any]]:
        container_sel = rule.get('container_selector')
        item_sel = rule.get('item_selector')
        fields = rule.get('fields', [])
        cfg_rules = rule.get('rules', {}) if isinstance(rule.get('rules', {}), dict) else {}
        drop_items_if_title_empty = bool(cfg_rules.get('drop_items_if_title_empty', False))

        containers = self.query_all_first_match(soup, container_sel) if container_sel else [soup]
        out: List[Dict[str, Any]] = []

        for container in containers:
            items = self.query_all_first_match(container, item_sel) if item_sel else [container]
            for node in items:
                item: Dict[str, Any] = {}
                for field in fields:
                    key = field.get('key')
                    if not key:
                        continue
                    item[key] = self.extract_field_value(node, field, '')

                if drop_items_if_title_empty and not self.normalize_text(item.get('title')):
                    continue

                if any(v not in (None, '', []) for v in item.values()):
                    out.append(item)

        return out

    def extract_pairs(self, soup: BeautifulSoup, rule: Dict[str, Any]) -> List[Dict[str, str]]:
        container_sel = rule.get('container_selector')
        title_sel = rule.get('pair_title_selector')
        text_sel = rule.get('pair_text_selector')

        containers = self.query_all_first_match(soup, container_sel) if container_sel else [soup]
        out: List[Dict[str, str]] = []

        for container in containers:
            titles = [el.get_text(strip=True) for el in self.query_all_first_match(container, title_sel)] if title_sel else []
            texts = [el.get_text(strip=True) for el in self.query_all_first_match(container, text_sel)] if text_sel else []
            max_len = max(len(titles), len(texts), 0)

            for i in range(max_len):
                title = titles[i] if i < len(titles) else ''
                text = texts[i] if i < len(texts) else ''
                if title or text:
                    out.append({'title': title, 'text': text})

        return out

    def extract_grouped_sections(self, soup: BeautifulSoup, rule: Dict[str, Any], product_name: Optional[str]) -> List[Dict[str, Any]]:
        container_sel = rule.get('container_selector')
        section_container_sel = rule.get('section_container_selector')
        title_sel = rule.get('section_title_selector')
        content_rules = rule.get('section_content_rules', [])
        ignore_patterns = [re.compile(p) for p in rule.get('ignore_if_title_matches', [])]
        skip_only_title = bool(rule.get('skip_sections_where_only_title_is_product_name', False))

        containers = self.query_all_first_match(soup, container_sel) if container_sel else [soup]
        sections_out: List[Dict[str, Any]] = []

        for container in containers:
            sections = self.query_all_first_match(container, section_container_sel) if section_container_sel else [container]
            for section in sections:
                title = None
                if title_sel:
                    title_el = self.query_first(section, title_sel)
                    if title_el:
                        title = title_el.get_text(strip=True)

                if title and any(p.search(title) for p in ignore_patterns):
                    continue

                items: List[str] = []
                for content_rule in content_rules:
                    c_mode = content_rule.get('mode', 'text')
                    c_sel = content_rule.get('selector')
                    if not c_sel:
                        continue

                    if c_mode == 'list':
                        for el in self.query_all_first_match(section, c_sel):
                            txt = el.get_text(strip=True)
                            if txt:
                                items.append(txt)
                    elif c_mode == 'text':
                        for el in self.query_all_first_match(section, c_sel):
                            txt = el.get_text(strip=True)
                            if txt:
                                items.append(txt)
                    elif c_mode == 'table':
                        for table in self.query_all_first_match(section, c_sel):
                            rows = table.find_all('tr')
                            if rows:
                                for row in rows:
                                    row_text = ' '.join(td.get_text(strip=True) for td in row.find_all(['td', 'th']))
                                    if row_text:
                                        items.append(row_text)
                            else:
                                txt = table.get_text(strip=True)
                                if txt:
                                    items.append(txt)

                if skip_only_title and title and product_name and title.strip() == str(product_name).strip() and not items:
                    continue

                if title or items:
                    sections_out.append({
                        'title': title,
                        'items': items,
                    })

        return sections_out

    def extract_paired_headings_paragraphs(self, soup: BeautifulSoup, rule: Dict[str, Any]) -> List[Dict[str, str]]:
        container_sel = rule.get('container_selector')
        title_sel = rule.get('title_selector')
        text_sel = rule.get('text_selector')

        containers = self.query_all_first_match(soup, container_sel) if container_sel else [soup]
        pairs: List[Dict[str, str]] = []

        for container in containers:
            titles = [el.get_text(strip=True) for el in self.query_all_first_match(container, title_sel)] if title_sel else []
            texts = [el.get_text(strip=True) for el in self.query_all_first_match(container, text_sel)] if text_sel else []
            max_len = max(len(titles), len(texts), 0)

            for i in range(max_len):
                title = titles[i] if i < len(titles) else ''
                text = texts[i] if i < len(texts) else ''
                if title or text:
                    pairs.append({'title': title, 'text': text})

        return pairs

    def process_extract_rules(self, soup: BeautifulSoup, rules: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Process `extract` rules from config and return a dict keyed by rule `key`."""
        result: Dict[str, Any] = {}
        for rule in rules:
            key = rule.get('key')
            container_sel = rule.get('container_selector')
            extract_mode = rule.get('extract_mode', 'container')
            children = rule.get('children', [])
            collected = []

            if not container_sel:
                containers = [soup]
            else:
                containers = self.query_all_first_match(soup, container_sel)

            for c in containers:
                if extract_mode == 'container':
                    item = {}
                    for child in children:
                        ckey = child.get('key')
                        sel = child.get('selector')
                        mode = child.get('extract_mode', 'text')
                        if not sel:
                            item[ckey] = None
                            continue
                        el = self.query_first(c, sel)
                        if not el:
                            item[ckey] = None
                            continue
                        if mode == 'text':
                            item[ckey] = el.get_text(strip=True)
                        elif mode == 'recursive':
                            values: List[str] = []
                            for li in el.find_all('li'):
                                txt = li.get_text(strip=True)
                                if txt:
                                    values.append(txt)
                            if not values:
                                for row in el.find_all('tr'):
                                    txt = ' '.join(td.get_text(strip=True) for td in row.find_all(['td', 'th']))
                                    if txt:
                                        values.append(txt)
                            if not values:
                                txt = el.get_text(strip=True)
                                values = [txt] if txt else []
                            item[ckey] = values
                        else:
                            item[ckey] = el.get_text(strip=True)
                    collected.append(item)
                else:
                    collected.append(c.get_text(strip=True))

            result[key] = collected
        return result

    def extract_images_from_config(self, soup: BeautifulSoup, base_url: str, product_name: Optional[str]) -> Dict[str, List[str]]:
        image_cfg = self.config.get('images', {})
        if not isinstance(image_cfg, dict):
            return {}

        sources = image_cfg.get('sources', [])
        if not sources:
            return {}

        download_flag = bool(image_cfg.get('download', False))
        allowed_exts = set(ext.lower() for ext in image_cfg.get('allowed_extensions', []))
        naming = image_cfg.get('naming', 'image_{index}')
        out_dir, token_values = self.resolve_image_output_dir(product_name)

        result: Dict[str, List[str]] = {}
        image_index = 1

        for source in sources:
            key = source.get('key', 'images')
            container_sel = source.get('container_selector') or source.get('container_selectors')
            image_sel = source.get('image_selector') or source.get('image_selectors') or 'img'
            mode = source.get('mode', 'attr')
            attr = source.get('attr', 'src')

            containers = self.query_all_first_match(
                soup,
                container_sel,
                context=f"{base_url} image containers ({key})",
            ) if container_sel else [soup]
            collected: List[str] = []

            for container in containers:
                for node in self.query_all_first_match(container, image_sel):
                    if mode == 'attr':
                        candidates = [
                            node.get(attr),
                            node.get('data-src'),
                            node.get('data-original'),
                            node.get('data-lazy-src'),
                            node.get('data-lazy'),
                            node.get('src'),
                        ]
                        value = next((candidate for candidate in candidates if candidate and not str(candidate).startswith('data:')), None)
                    else:
                        value = node.get_text(strip=True)

                    if not value:
                        continue

                    image_url = urljoin(base_url, value)
                    ext = get_file_extension(image_url) or 'jpg'
                    if allowed_exts and ext.lower() not in allowed_exts:
                        continue

                    if download_flag:
                        ensure_dir(out_dir)
                        naming_tokens = {
                            **token_values,
                            'index': image_index,
                            'key': key,
                            'ext': ext,
                        }
                        base_name = self.render_template(naming, naming_tokens) or f"image_{image_index}"
                        file_name = f"{safe_filename(base_name)}.{ext}"
                        image_path = os.path.join(out_dir, file_name)
                        if download_image(image_url, image_path):
                            rel = os.path.relpath(image_path, '.').replace('\\', '/')
                            collected.append(rel)
                            self.record_asset(
                                'images',
                                image_url,
                                local_path=rel,
                                product_name=product_name,
                                product_url=self.current_product_context.get('url'),
                                source_page_url=base_url,
                                key=key,
                                downloaded=True,
                            )
                            image_index += 1
                    else:
                        collected.append(image_url)
                        self.record_asset(
                            'images',
                            image_url,
                            product_name=product_name,
                            product_url=self.current_product_context.get('url'),
                            source_page_url=base_url,
                            key=key,
                            downloaded=False,
                        )
                        image_index += 1

            if collected:
                result[key] = collected

        return result

    def process_image_blocks(self, soup: BeautifulSoup, base_url: str, image_blocks: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        """Legacy support: process image blocks list and optionally download images."""
        images_result: Dict[str, List[str]] = {}
        out_dir, _ = self.resolve_image_output_dir(None)

        for block in image_blocks:
            key = block.get('key', 'images')
            container_sel = block.get('container_selector') or block.get('container_selectors')
            img_sel = block.get('image_selector') or block.get('image_selectors') or 'img'
            download_flag = block.get('download', False)
            collected: List[str] = []

            if container_sel:
                containers = self.query_all_first_match(
                    soup,
                    container_sel,
                    context=f"{base_url} image block containers ({key})",
                )
            else:
                containers = [soup]

            img_count = 1
            for c in containers:
                for img in self.query_all_first_match(c, img_sel):
                    src = img.get('src') or img.get('data-src') or img.get('data-original')
                    if not src:
                        continue
                    img_url = urljoin(base_url, src)
                    if download_flag:
                        ext = get_file_extension(img_url) or 'jpg'
                        ensure_dir(out_dir)
                        img_filename = f"{key}_{img_count}.{ext}"
                        img_path = os.path.join(out_dir, img_filename)
                        if download_image(img_url, img_path):
                            rel = os.path.relpath(img_path, '.').replace('\\', '/')
                            collected.append(rel)
                            img_count += 1
                    else:
                        collected.append(img_url)

            images_result[key] = collected

        return images_result

    def get_brand_root(self, brand: str) -> str:
        return os.path.join(self.output_root, safe_filename(brand))

    def get_data_dir(self, brand: str) -> str:
        output_cfg = self.config.get('output', {})
        data_folder = output_cfg.get('data_folder')
        if data_folder is not None:
            brand_root = self.get_brand_root(brand)
            if data_folder in (None, ''):
                return brand_root
            return os.path.join(brand_root, str(data_folder))
        return os.path.join(self.data_root, safe_filename(brand))

    def get_images_dir(self, brand: str) -> str:
        output_cfg = self.config.get('output', {})
        images_folder = output_cfg.get('images_folder')
        if images_folder:
            return os.path.join(self.get_brand_root(brand), str(images_folder))
        return os.path.join(self.images_root, safe_filename(brand))

    def get_logs_dir(self, brand: str) -> str:
        return self.logs_root

    # Aliases kept for parity with external naming in task notes.
    def getBrandRoot(self, brand: str) -> str:
        return self.get_brand_root(brand)

    def getDataDir(self, brand: str) -> str:
        return self.get_data_dir(brand)

    def getImagesDir(self, brand: str) -> str:
        return self.get_images_dir(brand)

    def getLogsDir(self, brand: str) -> str:
        return self.get_logs_dir(brand)

    def selector_candidates(self, selector_or_selectors: Any) -> List[str]:
        if isinstance(selector_or_selectors, str):
            value = selector_or_selectors.strip()
            return [value] if value else []
        if isinstance(selector_or_selectors, (list, tuple)):
            out: List[str] = []
            for value in selector_or_selectors:
                if isinstance(value, str) and value.strip():
                    out.append(value.strip())
            return out
        return []

    def query_first(self, root: BeautifulSoup, selector_or_selectors: Any) -> Optional[BeautifulSoup]:
        for selector in self.selector_candidates(selector_or_selectors):
            if isinstance(root, Tag):
                try:
                    if soupsieve.match(selector, root):
                        return root
                except Exception:
                    pass
            found = root.select_one(selector)
            if found:
                return found
        return None

    def query_all_first_match(
        self,
        root: BeautifulSoup,
        selector_or_selectors: Any,
        context: Optional[str] = None,
    ) -> List[BeautifulSoup]:
        selectors = self.selector_candidates(selector_or_selectors)
        for selector in selectors:
            found = []
            if isinstance(root, Tag):
                try:
                    if soupsieve.match(selector, root):
                        found.append(root)
                except Exception:
                    pass
            found.extend(root.select(selector))
            if found:
                return found

        if context and selectors:
            joined = ' | '.join(selectors)
            print(f"[WARN] No matches for any selector ({context}): {joined}")
        return []

    # Aliases kept for parity with external naming in task notes.
    def queryFirst(self, root: BeautifulSoup, selectors: Any) -> Optional[BeautifulSoup]:
        return self.query_first(root, selectors)

    def queryAllFirstMatch(self, root: BeautifulSoup, selectors: Any, context: Optional[str] = None) -> List[BeautifulSoup]:
        return self.query_all_first_match(root, selectors, context=context)

    def resolve_category(self, category: Optional[str], page_title: Optional[str]) -> str:
        category_text = self.normalize_text(category)
        title_text = self.normalize_text(page_title)

        source = category_text or title_text
        lowered = source.casefold()
        if 'mpos' in lowered:
            return 'MPOS'
        if 'victa' in lowered:
            return 'Victa'
        if not source:
            return 'Unknown'
        return source

    def resolve_image_output_dir(self, product_name: Optional[str]) -> Tuple[str, Dict[str, str]]:
        image_cfg = self.config.get('images', {})
        folders_cfg = image_cfg.get('folders', {}) if isinstance(image_cfg, dict) else {}
        path_folder_tpl = folders_cfg.get('path_folder')
        product_folder_tpl = folders_cfg.get('product_folder')

        brand_safe = safe_filename(self.brand)
        product_safe = safe_filename(str(product_name or 'unnamed'))
        product_without_brand = re.sub(rf'^{re.escape(brand_safe)}[_-]*', '', product_safe)
        if not product_without_brand:
            product_without_brand = product_safe

        token_values = {
            'brand': brand_safe,
            'product_name_sanitized': product_without_brand,
        }

        out_dir = self.image_dir
        if path_folder_tpl:
            rendered = self.render_template(path_folder_tpl, token_values)
            parts = [safe_filename(p) for p in rendered.split('/') if p]
            if parts:
                out_dir = os.path.join(self.image_dir, *parts)
        elif product_folder_tpl:
            rendered = safe_filename(self.render_template(product_folder_tpl, token_values))
            if rendered:
                out_dir = os.path.join(self.image_dir, rendered)

        return out_dir, token_values

    def extract_page_title(self, soup: BeautifulSoup, selector: Any) -> Optional[str]:
        if selector:
            el = self.query_first(soup, selector)
            if el:
                return el.get_text(strip=True)

        h1 = soup.find('h1')
        if h1 and h1.get_text(strip=True):
            return h1.get_text(strip=True)

        title_tag = soup.title
        if title_tag and title_tag.get_text(strip=True):
            return title_tag.get_text(strip=True)

        return None

    def deep_merge_dicts(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base)
        for key, value in override.items():
            if isinstance(merged.get(key), dict) and isinstance(value, dict):
                merged[key] = self.deep_merge_dicts(merged[key], value)
            else:
                merged[key] = value
        return merged

    def default_link_template(self) -> Dict[str, Any]:
        defaults = self.config.get('link_defaults')
        if isinstance(defaults, dict):
            return deepcopy(defaults)

        defaults = self.config.get('master_page')
        if isinstance(defaults, dict):
            return deepcopy(defaults)

        # Compatibility shortcut: allow top-level `listing` as shared page template.
        listing = self.config.get('listing')
        if isinstance(listing, dict):
            fallback = deepcopy(listing)
            if 'type' not in fallback:
                fallback['type'] = 'listing'
            return fallback

        return {}

    def infer_category_from_url(self, url: str, index: int) -> str:
        parsed = urlparse(url or '')
        path_parts = [p for p in parsed.path.split('/') if p]
        if path_parts:
            return safe_filename(path_parts[-1])
        if parsed.netloc:
            return safe_filename(parsed.netloc)
        return f"link_{index}"

    def resolve_links(self) -> List[Dict[str, Any]]:
        resolved: List[Dict[str, Any]] = []
        shared_template = self.default_link_template()

        for idx, raw_link in enumerate(self.config.get('links', []), start=1):
            if isinstance(raw_link, str):
                link_cfg = {'url': raw_link}
            elif isinstance(raw_link, dict):
                link_cfg = raw_link
            else:
                print(f"[WARN] Invalid link entry at index {idx}: {raw_link}")
                continue

            page_cfg = self.deep_merge_dicts(deepcopy(shared_template), link_cfg)
            page_url = page_cfg.get('url')
            if not page_url:
                print(f"[WARN] Skipping link entry without URL at index {idx}")
                continue

            if not self.normalize_text(page_cfg.get('category')):
                page_cfg['category'] = self.infer_category_from_url(page_url, idx)

            resolved.append(page_cfg)

        return resolved

    def save_single_link_output(
        self,
        page_cfg: Dict[str, Any],
        page_result: Dict[str, Any],
        products: Optional[List[Dict[str, Any]]],
        page_index: int,
    ):
        category = self.resolve_category(
            page_cfg.get('category') or page_cfg.get('slug') or page_cfg.get('name')
            ,
            page_result.get('page_title'),
        )
        if category == 'Unknown':
            category = self.infer_category_from_url(page_cfg.get('url', ''), page_index)

        include_flat_products = self.normalize_truthy_flag(
            self.config.get('output', {}).get('include_flat_products', False)
        )

        payload: Dict[str, Any] = {
            'brand': self.brand,
            'category': category,
            'url': page_result.get('url'),
            'page': page_result,
        }
        if include_flat_products and products:
            payload['products'] = products

        base_name = f"{safe_filename(category)}.json"
        out_path = os.path.join(self.data_dir, base_name)
        ensure_dir(os.path.dirname(out_path))
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"[INFO] Output saved to {out_path}")

    def save_output(self, pages: List[Dict[str, Any]], products: Optional[List[Dict[str, Any]]] = None):
        out_path = os.path.join(self.data_dir, f"{safe_filename(self.brand)}.json")
        ensure_dir(os.path.dirname(out_path))
        data: Dict[str, Any] = {
            'brand': self.brand,
            'products': products or [],
        }

        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"[INFO] Output saved to {out_path}")

    def save_execution_log(self):
        self.run_log['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        log_dir = self.get_logs_dir(self.brand)
        ensure_dir(log_dir)
        log_slug = self.config.get('log_file_slug') or self.brand
        out_path = os.path.join(log_dir, f"{safe_filename(log_slug)}-log-execution.json")
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(self.run_log, f, indent=2, ensure_ascii=False)
        print(f"[INFO] Execution log saved to {out_path}")


class VerifoneScraper(Scraper):
    """Brand-specific class kept explicit for Verifone config compatibility."""


class SunmiScraper(Scraper):
    def enrich_product_with_detail(self, product: Dict[str, Any]):
        detail_page_url = product.get('detail_url')
        if not detail_page_url:
            return

        print(f"[INFO] Detail: {detail_page_url}")
        self.current_product_context = {
            'name': product.get('product_name') or product.get('name'),
            'url': detail_page_url,
        }
        soup = self.fetch_soup(detail_page_url)
        if not soup:
            self.apply_listing_image_fallback(product, detail_page_url)
            self.current_product_context = {}
            return

        static_detail = self.extract_sunmi_static_detail(soup, detail_page_url, product)
        self.merge_nonempty_product_data(product, static_detail)
        self.apply_detail_soup(product, soup, detail_page_url)

        if not product.get('specifications') and not product.get('specs'):
            rendered_specs = self.extract_sunmi_rendered_specs(detail_page_url)
            if rendered_specs:
                product['specifications'] = rendered_specs

        detail_cfg = self.config.get('details_page', {})
        if self.requires_rendered_dom(detail_cfg) and not self.product_has_detail_content(product):
            rendered_soup = self.fetch_rendered_soup(detail_page_url)
            if rendered_soup:
                self.apply_detail_soup(product, rendered_soup, detail_page_url)

        if not self.has_any_images(product.get('images', {})):
            self.apply_listing_image_fallback(product, detail_page_url)
        self.current_product_context = {}

    def extract_sunmi_rendered_specs(self, detail_page_url: str) -> List[Dict[str, Any]]:
        local_playwright_browsers = os.path.join(os.getcwd(), '.venv', 'ms-playwright')
        if not os.environ.get('PLAYWRIGHT_BROWSERS_PATH') and os.path.isdir(local_playwright_browsers):
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = local_playwright_browsers

        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            print(f"[WARN] Playwright unavailable for SUNMI specs {detail_page_url}: {e}")
            return []

        specs: List[Dict[str, Any]] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(detail_page_url, wait_until='networkidle', timeout=60000)
                section = page.locator("section[class*='SpecScreen_spec-screen']").first
                if section.count() == 0:
                    browser.close()
                    return []

                tabs = [
                    self.normalize_text(text)
                    for text in section.locator('.ant-tabs-tab-btn').all_inner_texts()
                    if self.normalize_text(text)
                ]
                if not tabs:
                    tabs = ['Default']

                for tab in tabs:
                    if tab != 'Default':
                        try:
                            section.get_by_text(tab, exact=True).first.click(timeout=5000)
                            page.wait_for_timeout(500)
                        except Exception:
                            pass

                    cards = section.locator("div[class*='SpecScreen_property-card']").evaluate_all(
                        """nodes => nodes.map(node => {
                            const title = node.querySelector('[class*="SpecScreen_title"]')?.innerText?.trim() || '';
                            const desc = node.querySelector('[class*="SpecScreen_desc"]')?.innerText?.trim() || '';
                            return {title, desc};
                        })"""
                    )
                    card_specs = []
                    for card in cards:
                        title = self.normalize_text(card.get('title'))
                        values = [
                            self.normalize_text(value)
                            for value in str(card.get('desc') or '').split('\n')
                            if self.normalize_text(value)
                        ]
                        if title and values:
                            card_specs.append({'title': title, 'values': values})

                    if card_specs:
                        specs.append({'title': tab, 'specs': card_specs})

                browser.close()
        except Exception as e:
            print(f"[WARN] Could not extract SUNMI rendered specs {detail_page_url}: {e}")
            return []

        return specs

    def extract_sunmi_static_detail(
        self,
        soup: BeautifulSoup,
        detail_page_url: str,
        product: Dict[str, Any],
    ) -> Dict[str, Any]:
        script_urls = []
        for script in soup.find_all('script', src=True):
            src = script.get('src')
            if src and '.js' in src:
                script_url = urljoin(detail_page_url, src)
                if script_url not in script_urls:
                    script_urls.append(script_url)

        chunks: List[str] = []
        for script_url in script_urls:
            chunk = self.fetch_html(script_url, timeout=30)
            if chunk:
                chunks.append(chunk)

        locales: List[Dict[str, Any]] = []
        for chunk in chunks:
            locales.extend(self.extract_sunmi_json_blobs(chunk))

        locale = self.choose_sunmi_locale(locales, product, detail_page_url)
        if not locale:
            return {}

        features = self.extract_sunmi_features(locale)
        variants = self.extract_sunmi_variant_candidates(locale)
        slug = self.sunmi_product_slug(detail_page_url)

        locale_image_urls = self.collect_sunmi_image_urls(locale, detail_page_url, slug)
        chunk_image_urls: List[str] = []
        document_urls: List[str] = []
        media_urls: List[str] = []
        for text in [self._html_cache.get(detail_page_url, ''), *chunks]:
            for image_url in self.collect_sunmi_image_urls_from_text(text, detail_page_url, slug):
                if image_url not in chunk_image_urls:
                    chunk_image_urls.append(image_url)
            for document_url in self.collect_sunmi_asset_urls_from_text(text, detail_page_url, {'pdf', 'doc', 'docx'}, slug):
                if document_url not in document_urls:
                    document_urls.append(document_url)
            for media_url in self.collect_sunmi_asset_urls_from_text(text, detail_page_url, {'mp4', 'webm', 'mov', 'm4v'}, slug):
                if media_url not in media_urls:
                    media_urls.append(media_url)

        for document_url in self.collect_sunmi_asset_urls(locale, detail_page_url, {'pdf', 'doc', 'docx'}, slug):
            if document_url not in document_urls:
                document_urls.append(document_url)
        for media_url in self.collect_sunmi_asset_urls(locale, detail_page_url, {'mp4', 'webm', 'mov', 'm4v'}, slug):
            if media_url not in media_urls:
                media_urls.append(media_url)

        image_urls = locale_image_urls or chunk_image_urls
        product_name = (
            locale.get('page.title')
            or locale.get('title')
            or product.get('product_name')
            or product.get('name')
        )

        detail: Dict[str, Any] = {
            'product_name': product_name,
            'feature_paragraphs': features.get('feature_paragraphs', []),
            'feature_cards': features.get('feature_cards', []),
            'static_variant_candidates': variants,
            'detail_image_urls': [{'url': image_url} for image_url in image_urls],
            'document_links': self.urls_to_link_items(document_urls, 'SUNMI document'),
            'media_links': self.urls_to_link_items(media_urls, 'SUNMI media'),
            'sunmi_static_source': 'SUNMI product page JavaScript translation chunks',
        }

        materialized = self.materialize_image_urls(image_urls[:28], product_name, 'detail_images')
        if materialized:
            detail['images'] = materialized

        return {key: value for key, value in detail.items() if value not in (None, '', [])}

    def sunmi_product_slug(self, detail_page_url: str) -> str:
        path_parts = [part for part in urlparse(detail_page_url or '').path.split('/') if part]
        return path_parts[-1].casefold() if path_parts else ''

    def urls_to_link_items(self, urls: List[str], label: str) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        seen = set()
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({'text': label, 'url': url})
        return out

    def extract_sunmi_json_blobs(self, chunk: str) -> List[Dict[str, Any]]:
        blobs: List[Dict[str, Any]] = []
        pattern = re.compile(r"JSON\.parse\((['\"])((?:\\.|(?!\1).)*)\1\)", re.DOTALL)
        for match in pattern.finditer(chunk or ''):
            quote = match.group(1)
            body = match.group(2)
            try:
                decoded = ast.literal_eval(f"{quote}{body}{quote}")
                parsed = json.loads(decoded)
            except Exception:
                continue
            if isinstance(parsed, dict) and parsed.get('__desc__') == '英文':
                blobs.append(parsed)
        return blobs

    def choose_sunmi_locale(
        self,
        locales: List[Dict[str, Any]],
        product: Dict[str, Any],
        detail_page_url: str,
    ) -> Optional[Dict[str, Any]]:
        if not locales:
            return None

        needles = [
            product.get('product_name'),
            product.get('name'),
            str(detail_page_url or '').split('/').pop(),
        ]
        needles = [self.normalize_text(value).casefold() for value in needles if self.normalize_text(value)]

        best_locale = None
        best_score = 0
        for locale in locales:
            haystack = json.dumps(locale, ensure_ascii=False).casefold()
            score = sum(1 for needle in needles if needle and needle in haystack)
            if score > best_score:
                best_score = score
                best_locale = locale

        if best_locale:
            return best_locale

        return next((locale for locale in locales if locale.get('page.title') or locale.get('title')), locales[0])

    def flatten_sunmi_text(self, value: Any) -> str:
        if value is None:
            return ''
        if isinstance(value, (str, int, float)):
            return str(value)
        if isinstance(value, list):
            return ' '.join(self.flatten_sunmi_text(item) for item in value if self.flatten_sunmi_text(item))
        if isinstance(value, dict):
            if 'text' in value:
                return self.flatten_sunmi_text(value.get('text'))
            return ''
        return ''

    def walk_sunmi_entries(self, value: Any, prefix: str = '') -> List[Tuple[str, Any]]:
        entries: List[Tuple[str, Any]] = []
        if isinstance(value, dict):
            for key, child in value.items():
                next_key = f"{prefix}.{key}" if prefix else str(key)
                entries.append((next_key, child))
                entries.extend(self.walk_sunmi_entries(child, next_key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                entries.extend(self.walk_sunmi_entries(child, f"{prefix}[{index}]"))
        return entries

    def extract_sunmi_features(self, locale: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        entries = self.walk_sunmi_entries(locale)
        lookup = {key: value for key, value in entries}
        paragraphs: List[Dict[str, str]] = []
        cards: List[Dict[str, Any]] = []

        for key, value in entries:
            if not key.endswith('.title'):
                continue
            title = self.flatten_sunmi_text(value).strip()
            if not title or len(title) < 4:
                continue
            base = key[:-len('.title')]
            desc = (
                self.flatten_sunmi_text(lookup.get(f'{base}.desc')).strip()
                or self.flatten_sunmi_text(lookup.get(f'{base}.subtitle')).strip()
                or self.flatten_sunmi_text(lookup.get(f'{base}.tips')).strip()
            )
            if desc or re.match(r'^screen\d+', base):
                paragraphs.append({'title': title, 'text': desc})

        for key, value in entries:
            if not key.endswith('.feat.list') or not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                title = self.flatten_sunmi_text(item.get('title') or item.get('text')).strip()
                text = self.flatten_sunmi_text(item.get('desc') or item.get('tips')).strip()
                image_url = next(iter(self.collect_sunmi_image_urls(item, '')), None)
                if title or text:
                    cards.append({'title': title, 'text': text, 'image_url': image_url})

        return {
            'feature_paragraphs': self.dedup_sunmi_feature_items(paragraphs, exclude_sunmi_platform=True)[:30],
            'feature_cards': self.dedup_sunmi_feature_items(cards)[:60],
        }

    def dedup_sunmi_feature_items(
        self,
        items: List[Dict[str, Any]],
        exclude_sunmi_platform: bool = False,
    ) -> List[Dict[str, Any]]:
        seen = set()
        deduped: List[Dict[str, Any]] = []
        for item in items:
            sig = f"{item.get('title', '')}|{item.get('text', '')}"
            if sig in seen:
                continue
            seen.add(sig)
            if exclude_sunmi_platform and re.search(r'SUNMI OS|SUNMI DMP|SUNMI Home', sig, re.IGNORECASE):
                continue
            deduped.append(item)
        return deduped

    def extract_sunmi_variant_candidates(self, locale: Dict[str, Any]) -> List[Dict[str, str]]:
        candidates: List[Dict[str, str]] = []
        for key, value in self.walk_sunmi_entries(locale):
            if not key.endswith('.feat.list') or not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                title = self.flatten_sunmi_text(item.get('title')).strip()
                if not title:
                    continue
                if re.search(r'version|family|\bV\d|\bT\d|\bB\d|\bP\d|\bD\d|inch|"', title, re.IGNORECASE):
                    candidates.append({
                        'name': title,
                        'description': self.flatten_sunmi_text(item.get('desc') or item.get('tips')).strip(),
                        'source_key': key,
                    })

        seen = set()
        deduped: List[Dict[str, str]] = []
        for item in candidates:
            if item['name'] in seen:
                continue
            seen.add(item['name'])
            deduped.append(item)
        return deduped

    def collect_sunmi_image_urls(self, value: Any, base_url: str, slug: str = '') -> List[str]:
        return self.collect_sunmi_asset_urls(value, base_url, {'png', 'jpg', 'jpeg', 'webp', 'avif'}, slug)

    def collect_sunmi_image_urls_from_text(self, text: str, base_url: str, slug: str = '') -> List[str]:
        return self.collect_sunmi_asset_urls_from_text(text, base_url, {'png', 'jpg', 'jpeg', 'webp', 'avif'}, slug)

    def collect_sunmi_asset_urls(
        self,
        value: Any,
        base_url: str,
        extensions: set,
        slug: str = '',
    ) -> List[str]:
        urls: List[str] = []
        if isinstance(value, str):
            urls.extend(self.collect_sunmi_asset_urls_from_text(value, base_url, extensions, slug))
        elif isinstance(value, dict):
            for child in value.values():
                for asset_url in self.collect_sunmi_asset_urls(child, base_url, extensions, slug):
                    if asset_url not in urls:
                        urls.append(asset_url)
        elif isinstance(value, list):
            for child in value:
                for asset_url in self.collect_sunmi_asset_urls(child, base_url, extensions, slug):
                    if asset_url not in urls:
                        urls.append(asset_url)
        return urls

    def collect_sunmi_asset_urls_from_text(
        self,
        text: str,
        base_url: str,
        extensions: set,
        slug: str = '',
    ) -> List[str]:
        urls: List[str] = []
        pattern = re.compile(
            r"(?:https?:)?//[^'\"\\\s<>]+|/[^'\"\\\s<>]+",
            re.IGNORECASE,
        )
        slug = (slug or '').casefold()
        for match in pattern.finditer(text or ''):
            raw = match.group(0)
            if raw.startswith('//'):
                raw = f"{urlparse(base_url).scheme or 'https'}:{raw}"
            asset_url = urljoin(base_url, raw)
            ext = get_file_extension(asset_url)
            if ext.casefold() not in extensions:
                continue
            lowered = asset_url.casefold()
            if slug and '/products/' in lowered and slug not in lowered:
                continue
            if asset_url not in urls:
                urls.append(asset_url)
        return urls


def create_scraper(config_path: str) -> Scraper:
    with open(config_path, encoding='utf-8') as f:
        config = json.load(f)

    brand = str(config.get('brand', '')).strip().casefold()
    scraper_cls = {
        'verifone': VerifoneScraper,
        'verifon': VerifoneScraper,
        'sunmi': SunmiScraper,
        'sumni': SunmiScraper,
    }.get(brand, Scraper)
    return scraper_cls(config_path)
