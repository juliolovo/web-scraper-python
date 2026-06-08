import argparse
from scraper import create_scraper
from scraper_output_rules import apply_output_rules


def main():
    parser = argparse.ArgumentParser(description="Generic Scraper - runner")
    parser.add_argument('--config', type=str, default='config/verifone.json')
    parser.add_argument('--limit-products', type=int, default=None, help='Limit products per listing page for validation runs')
    args = parser.parse_args()
    scraper = apply_output_rules(create_scraper(args.config))
    scraper.scrape(limit_products=args.limit_products)


if __name__ == '__main__':
    main()
