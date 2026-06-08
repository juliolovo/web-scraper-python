"""Output rules and normalisation helpers applied to scraper instances."""
import json
import re
from pathlib import Path
from types import MethodType


def apply_output_rules(scraper):
    """Apply the current output/log contract to a scraper instance."""
    configured_data_root = _clean_path_value(scraper.config.get("data_root_dir"))
    if not configured_data_root or configured_data_root == "output":
        scraper.data_root = Path("output/data")

    configured_logs_root = _clean_path_value(scraper.config.get("logs_root_dir"))
    if not configured_logs_root or configured_logs_root == "logs":
        scraper.logs_root = Path("logs_execution")

    configured_log_slug = scraper.config.get("log_file_slug")
    if configured_log_slug:
        scraper.log_file_slug = _slugify(configured_log_slug)
    elif getattr(scraper, "brand_slug", "") == "sunmi":
        scraper.log_file_slug = "sumni"
    else:
        brand_slug = getattr(scraper, "brand_slug", "")
        scraper.log_file_slug = brand_slug or _slugify(
            getattr(scraper, "brand", "brand")
        )

    _normalize_asset_buckets(scraper.run_log.setdefault("assets", {}))

    original_record_asset = scraper.record_asset
    original_normalize_product = scraper.normalize_product_for_output
    original_save_output = scraper.save_output

    def record_asset(_self, kind, source_url, *args, **kwargs):
        return original_record_asset(_normalize_asset_kind(kind), source_url, *args, **kwargs)

    def save_execution_log(self):
        assets = self.run_log.setdefault("assets", {})
        _normalize_asset_buckets(assets)
        for bucket in ("images", "documents", "video", "links"):
            assets[bucket] = _clean_asset_list(assets.get(bucket, []))

        self.logs_root.mkdir(parents=True, exist_ok=True)
        output_path = self.logs_root / f"{self.log_file_slug}-log-execution.json"
        self.run_log = _rename_media_keys(self.run_log)
        with open(output_path, "w", encoding="utf-8") as file:
            json.dump(self.run_log, file, ensure_ascii=False, indent=2)
        print(f"Execution log saved to {output_path}")

    def save_output(_self, pages, all_products):
        return original_save_output(
            _rename_media_keys(pages),
            _rename_media_keys(all_products),
        )

    def normalize_product_for_output(self, product, *args, **kwargs):
        normalized = original_normalize_product(product, *args, **kwargs)

        if not normalized.get("specs"):
            normalized["specs"] = (
                normalized.get("specifications")
                or product.get("specifications")
                or product.get("specs")
            )

        legacy_images = []
        for key in (
            "detail_images", "details_images", "image_details",
            "primary_image", "spec_figure_image",
        ):
            legacy_images.extend(_flatten_asset_values(normalized.get(key)))
        legacy_documents = []
        for key in ("document_links", "asset_links"):
            legacy_documents.extend(_flatten_asset_values(normalized.get(key)))
        legacy_video = []
        for key in ("media", "media_links", "videos"):
            legacy_video.extend(_flatten_asset_values(normalized.get(key)))
        legacy_links = _flatten_asset_values(normalized.get("external_links"))

        for legacy_key in (
            "raw_brand_data",
            "source_url",
            "detail_images",
            "details_images",
            "image_details",
            "primary_image",
            "spec_figure_image",
            "specifications",
            "media",
            "media_links",
            "videos",
        ):
            normalized.pop(legacy_key, None)

        if "category" in normalized:
            category = normalized.pop("category")
            normalized.setdefault("manufacturer_category", category)

        category = normalized.get("manufacturer_category")
        if isinstance(category, str) and category.strip().lower() == "products":
            inferred = _infer_category(self, product)
            if inferred and inferred.strip().lower() != "products":
                normalized["manufacturer_category"] = inferred
            else:
                normalized.pop("manufacturer_category", None)

        product_type = normalized.get("type")
        if isinstance(product_type, str) and product_type.strip().lower() == "products":
            if normalized.get("manufacturer_category"):
                normalized["type"] = normalized["manufacturer_category"]
            else:
                normalized.pop("type", None)

        if not normalized.get("model") and normalized.get("name"):
            normalized["model"] = normalized["name"]

        images = []
        images.extend(legacy_images)
        for key in (
            "images", "detail_images", "details_images",
            "image_details", "primary_image", "spec_figure_image",
        ):
            images.extend(_flatten_asset_values(product.get(key)))
        images.extend(_flatten_asset_values(normalized.get("images")))
        _set_clean_list(normalized, "images", images)

        documents = []
        documents.extend(legacy_documents)
        for key in ("documents", "document_links", "asset_links"):
            documents.extend(_flatten_asset_values(product.get(key)))
        _set_clean_list(normalized, "documents", documents)
        for document_url in normalized.get("documents", []):
            self.record_asset(
                "documents",
                document_url,
                product_name=product.get("name") or product.get("product_name"),
                product_url=product.get("url"),
                source_page_url=product.get("url"),
                downloaded=False,
            )

        video = []
        video.extend(legacy_video)
        for key in ("video", "videos", "media", "media_links"):
            video.extend(_flatten_asset_values(product.get(key)))
        _set_clean_list(normalized, "video", video)
        for video_url in normalized.get("video", []):
            self.record_asset(
                "video",
                video_url,
                product_name=product.get("name") or product.get("product_name"),
                product_url=product.get("url"),
                source_page_url=product.get("url"),
                downloaded=False,
            )

        links = []
        links.extend(legacy_links)
        for key in ("links", "external_links"):
            links.extend(_flatten_asset_values(product.get(key)))
        _set_clean_list(normalized, "links", links)
        for link_url in normalized.get("links", []):
            self.record_asset(
                "links",
                link_url,
                product_name=product.get("name") or product.get("product_name"),
                product_url=product.get("url"),
                source_page_url=product.get("url"),
                downloaded=False,
            )

        features = normalized.get("features")
        if isinstance(features, dict):
            cleaned_features = {}
            cards = _clean_dict_list(features.get("cards"))
            paragraphs = _clean_dict_list(features.get("paragraphs"))
            if cards:
                cleaned_features["cards"] = cards
            if paragraphs:
                cleaned_features["paragraphs"] = paragraphs
            if cleaned_features:
                normalized["features"] = cleaned_features
            else:
                normalized.pop("features", None)

        _set_clean_dict_list(normalized, "specs")
        _set_clean_dict_list(normalized, "variants")
        _set_clean_dict_list(normalized, "related_products")

        normalized = {
            key: value
            for key, value in normalized.items()
            if value not in (None, "", [], {})
        }
        return _rename_media_keys(normalized)

    scraper.record_asset = MethodType(record_asset, scraper)
    scraper.save_execution_log = MethodType(save_execution_log, scraper)
    scraper.save_output = MethodType(save_output, scraper)
    scraper.normalize_product_for_output = MethodType(normalize_product_for_output, scraper)
    return scraper


def _clean_path_value(value):
    return str(value or "").replace("\\", "/").strip("/")


def _slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return slug or "brand"


def _normalize_asset_kind(kind):
    aliases = {
        "image": "images",
        "images": "images",
        "document": "documents",
        "documents": "documents",
        "media": "video",
        "videos": "video",
        "video": "video",
        "link": "links",
        "links": "links",
    }
    return aliases.get(kind, "links")


def _normalize_asset_buckets(assets):
    if "media" in assets:
        assets.setdefault("video", []).extend(assets.pop("media") or [])
    for bucket in ("images", "documents", "video", "links"):
        assets.setdefault(bucket, [])


def _rename_media_keys(value):
    if isinstance(value, list):
        return [_rename_media_keys(item) for item in value]
    if not isinstance(value, dict):
        return value

    renamed = {}
    for key, item in value.items():
        clean_key = "video" if key == "media" else key
        clean_item = _rename_media_keys(item)
        if clean_key in renamed and clean_key == "video":
            renamed[clean_key] = _merge_video_values(renamed[clean_key], clean_item)
        else:
            renamed[clean_key] = clean_item
    return renamed


def _merge_video_values(current, incoming):
    if isinstance(current, list) and isinstance(incoming, list):
        merged = current + incoming
        return [item for index, item in enumerate(merged) if item not in merged[:index]]
    if current in (None, "", [], {}):
        return incoming
    if incoming in (None, "", [], {}):
        return current
    return incoming


def _listify(value):
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return value
    return [value]


def _flatten_asset_values(value):
    flattened = []
    for item in _listify(value):
        if isinstance(item, str):
            if item.strip():
                flattened.append(item.strip())
            continue
        if isinstance(item, dict):
            for key in ("local_path", "url", "source_url", "href", "src", "image_url"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    flattened.append(candidate.strip())
                    break
    return list(dict.fromkeys(flattened))


def _clean_dict_list(items):
    cleaned = []
    seen = set()
    for item in _listify(items):
        if not isinstance(item, dict):
            continue
        compact = {}
        for key, value in item.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, str) and not value.strip():
                continue
            compact[key] = value
        title = str(compact.get("title") or compact.get("name") or "").strip()
        text = str(
            compact.get("text") or compact.get("value")
            or compact.get("description") or ""
        ).strip()
        if title.lower().startswith("feature") and not text:
            continue
        if compact:
            fingerprint = json.dumps(compact, ensure_ascii=False, sort_keys=True)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            cleaned.append(compact)
    return cleaned


def _clean_asset_list(items):
    cleaned = []
    seen = {}
    for item in _clean_dict_list(items):
        signature = (item.get("source_url"), item.get("local_path") or "")
        if signature in seen:
            existing = cleaned[seen[signature]]
            for key, value in item.items():
                if existing.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
                    existing[key] = value
            continue
        seen[signature] = len(cleaned)
        cleaned.append(item)
    return cleaned


def _set_clean_list(target, key, values):
    cleaned = [value for value in dict.fromkeys(values) if value]
    if cleaned:
        target[key] = cleaned
    else:
        target.pop(key, None)


def _set_clean_dict_list(target, key):
    cleaned = _clean_dict_list(target.get(key))
    if cleaned:
        target[key] = cleaned
    else:
        target.pop(key, None)


def _infer_category(scraper, product):
    infer = getattr(scraper, "infer_manufacturer_category", None)
    if not infer:
        return None
    try:
        return infer(product)
    except Exception:  # pylint: disable=broad-exception-caught
        return None
