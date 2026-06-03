const fs = require("fs/promises");
const path = require("path");

const JSON_PATH = path.resolve("output/data/sunmi/sunmi.json");
const IMAGES_DIR = path.resolve("output/images/sunmi");

process.on("uncaughtException", (error) => {
  console.error(`Recovered uncaught error: ${error.message || error}`);
});

function sanitize(value) {
  return String(value || "product")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 80) || "product";
}

function flattenText(value) {
  if (value == null) return "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(flattenText).filter(Boolean).join(" ");
  if (typeof value === "object") {
    if (typeof value.text !== "undefined") return flattenText(value.text);
    return "";
  }
  return "";
}

function collectUrls(value, urls = new Set()) {
  if (typeof value === "string") {
    if (/^https?:\/\/.+\.(png|jpe?g|webp|avif)(\?|#|$)/i.test(value)) urls.add(value);
    if (/^\/\/.+\.(png|jpe?g|webp|avif)(\?|#|$)/i.test(value)) urls.add(`https:${value}`);
    return urls;
  }
  if (Array.isArray(value)) {
    value.forEach((item) => collectUrls(item, urls));
    return urls;
  }
  if (value && typeof value === "object") {
    Object.values(value).forEach((item) => collectUrls(item, urls));
  }
  return urls;
}

function walkEntries(value, prefix = "", entries = []) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    Object.entries(value).forEach(([key, child]) => {
      const next = prefix ? `${prefix}.${key}` : key;
      entries.push([next, child]);
      walkEntries(child, next, entries);
    });
  } else if (Array.isArray(value)) {
    value.forEach((child, index) => walkEntries(child, `${prefix}[${index}]`, entries));
  }
  return entries;
}

function extractJsonBlobs(chunk) {
  const blobs = [];
  const pattern = /JSON\.parse\((['"])((?:\\.|(?!\1).)*)\1\)/g;
  let match;
  while ((match = pattern.exec(chunk))) {
    try {
      const decoded = Function(`return ${match[1]}${match[2]}${match[1]}`)();
      const parsed = JSON.parse(decoded);
      if (parsed && typeof parsed === "object" && parsed.__desc__ === "英文") blobs.push(parsed);
    } catch {
      // Ignore non-JSON translation fragments.
    }
  }
  return blobs;
}

async function fetchText(url) {
  global.fetchTextCache = global.fetchTextCache || new Map();
  if (global.fetchTextCache.has(url)) return global.fetchTextCache.get(url);
  let lastError;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const response = await fetch(url, { redirect: "follow" });
      if (!response.ok) throw new Error(`${response.status} ${url}`);
      const text = await response.text();
      global.fetchTextCache.set(url, text);
      return text;
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 600 + attempt * 800));
    }
  }
  throw lastError;
}

async function downloadImage(url, productName, index) {
  try {
    const response = await fetch(url, { redirect: "follow" });
    if (!response.ok) return null;
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.toLowerCase().startsWith("image/")) return null;
    const buffer = Buffer.from(await response.arrayBuffer());
    if (buffer.length < 2048) return null;
    const urlPath = new URL(url).pathname;
    const extFromUrl = path.extname(urlPath).replace(".", "").toLowerCase();
    const ext = extFromUrl || contentType.split("/")[1] || "jpg";
    const file = `img_sunmi_${sanitize(productName)}_detail_${index}.${ext.replace("jpeg", "jpg")}`;
    const target = path.join(IMAGES_DIR, file);
    try {
      await fs.access(target);
    } catch {
      await fs.writeFile(target, buffer);
    }
    return `output/images/sunmi/${file}`;
  } catch {
    return null;
  }
}

function collectImageUrlsFromText(text, slug) {
  const urls = new Set();
  const pattern = /https?:\/\/[^"'\\\s]+?\.(?:png|jpe?g|webp|avif)(?:\?[^"'\\\s]*)?|\/\/[^"'\\\s]+?\.(?:png|jpe?g|webp|avif)(?:\?[^"'\\\s]*)?/gi;
  let match;
  while ((match = pattern.exec(text))) {
    const url = match[0].startsWith("//") ? `https:${match[0]}` : match[0];
    if (!slug || url.toLowerCase().includes(`/products/${slug.toLowerCase()}/`)) urls.add(url);
  }
  return urls;
}

function extractFeatures(locale) {
  const entries = walkEntries(locale);
  const paragraphs = [];
  const cards = [];

  for (const [key, value] of entries) {
    if (!key.endsWith(".title")) continue;
    const title = flattenText(value).trim();
    if (!title || title.length < 4) continue;
    const base = key.slice(0, -".title".length);
    const desc =
      flattenText(locale[`${base}.desc`]).trim() ||
      flattenText(locale[`${base}.subtitle`]).trim() ||
      flattenText(locale[`${base}.tips`]).trim();
    if (desc || /^screen\d+/.test(base)) {
      paragraphs.push({ title, text: desc });
    }
  }

  for (const [key, value] of entries) {
    if (!key.endsWith(".feat.list") || !Array.isArray(value)) continue;
    value.forEach((item) => {
      const title = flattenText(item.title || item.text).trim();
      const text = flattenText(item.desc || item.tips).trim();
      const imageUrl = [...collectUrls(item)][0] || null;
      if (title || text) cards.push({ title, text, image_url: imageUrl });
    });
  }

  const seenParagraph = new Set();
  const dedupParagraphs = paragraphs.filter((item) => {
    const sig = `${item.title}|${item.text}`;
    if (seenParagraph.has(sig)) return false;
    seenParagraph.add(sig);
    return !/SUNMI OS|SUNMI DMP|SUNMI Home/i.test(sig);
  }).slice(0, 30);

  const seenCard = new Set();
  const dedupCards = cards.filter((item) => {
    const sig = `${item.title}|${item.text}`;
    if (seenCard.has(sig)) return false;
    seenCard.add(sig);
    return true;
  }).slice(0, 60);

  return { feature_paragraphs: dedupParagraphs, feature_cards: dedupCards };
}

function extractVariantCandidates(locale) {
  const candidates = [];
  Object.entries(locale).forEach(([key, value]) => {
    if (!key.endsWith(".feat.list") || !Array.isArray(value)) return;
    value.forEach((item) => {
      const title = flattenText(item.title).trim();
      if (!title) return;
      if (/version|family|\\bV\\d|\\bT\\d|\\bB\\d|\\bP\\d|\\bD\\d|inch|\"/i.test(title)) {
        candidates.push({
          name: title,
          description: flattenText(item.desc || item.tips).trim(),
          source_key: key,
        });
      }
    });
  });
  const seen = new Set();
  return candidates.filter((item) => {
    if (seen.has(item.name)) return false;
    seen.add(item.name);
    return true;
  });
}

async function enrich() {
  await fs.mkdir(IMAGES_DIR, { recursive: true });
  const data = JSON.parse(await fs.readFile(JSON_PATH, "utf8"));
  const products = data.pages?.[0]?.products || [];
  const summary = { products: products.length, enriched: 0, images: 0, errors: [] };

  for (const product of products) {
    try {
      if (!product.detail_url) continue;
      const html = await fetchText(product.detail_url);
      const scripts = [...html.matchAll(/src="([^"]+\.js)"/g)]
        .map((match) => match[1]);
      const chunks = [];
      for (const script of scripts) {
        try {
          chunks.push(await fetchText(script));
        } catch {
          // Some analytics/polyfill chunks can fail without affecting product content.
        }
      }
      const locales = chunks.flatMap(extractJsonBlobs);
      const productNeedles = [
        product.product_name,
        product.name,
        String(product.detail_url || "").split("/").filter(Boolean).pop(),
      ].filter(Boolean).map((item) => String(item).toLowerCase());
      const locale = locales.find((blob) => {
        const haystack = JSON.stringify(blob).toLowerCase();
        return productNeedles.some((needle) => haystack.includes(needle));
      }) || locales.find((blob) => blob["page.title"] || blob.title);
      if (!locale) {
        summary.errors.push({ name: product.name, url: product.detail_url, error: "No SUNMI locale JSON found in page scripts" });
        continue;
      }

      const features = extractFeatures(locale);
      product.product_name = product.product_name || locale["page.title"] || locale.title || product.name;
      product.feature_paragraphs = features.feature_paragraphs;
      product.feature_cards = features.feature_cards;
      product.static_variant_candidates = extractVariantCandidates(locale);
      const slug = String(product.detail_url || "").split("/").filter(Boolean).pop() || "";
      const chunkImageUrls = new Set();
      chunks.forEach((chunk) => collectImageUrlsFromText(chunk, slug).forEach((url) => chunkImageUrls.add(url)));
      collectImageUrlsFromText(html, slug).forEach((url) => chunkImageUrls.add(url));
      const localeImageUrls = collectUrls(locale);
      const imageUrls = localeImageUrls.size ? localeImageUrls : chunkImageUrls;
      product.detail_image_urls = [...imageUrls].map((url) => ({ url }));

      const saved = [];
      let index = 1;
      for (const item of product.detail_image_urls.slice(0, 28)) {
        const local = await downloadImage(item.url, product.product_name || product.name, index++);
        if (local) saved.push(local);
      }
      product.images = { ...(product.images || {}), detail_images: saved };
      summary.enriched += 1;
      summary.images += saved.length;
      console.log(`SUNMI static ${summary.enriched}/${products.length}: ${product.name} (${saved.length} images)`);
    } catch (error) {
      summary.errors.push({ name: product.name, url: product.detail_url, error: String(error.message || error) });
    }
    data.generated_at = new Date().toISOString();
    data.static_enrichment = {
      source: "SUNMI product page Next.js translation chunks",
      summary,
    };
    await fs.writeFile(JSON_PATH, JSON.stringify(data, null, 2), "utf8");
  }
  console.log(JSON.stringify(summary, null, 2));
}

enrich().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
