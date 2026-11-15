"""Four realistic sample pages, as the service's `POST /v1/samples` returns them.

Nothing is fetched. The three book pages follow the markup of
https://books.toscrape.com/ (the scraping sandbox the acceptance stand used; the
first 900 characters of the first page match the distilled HTML recorded on the
stand), rebuilt by hand. The fourth page is a WooCommerce single-product page:
WooCommerce's own element classes and its schema.org Product JSON-LD, with an
invented product. The HTML is in the service's distilled form
(`web-scraper-service/app/extraction/distill.py`: no script/style/meta, only
id/class/itemprop/href/src/alt/content/name/property and data-* attributes,
whitespace collapsed).
"""

from __future__ import annotations

import re
from typing import Any


def _squash(html: str) -> str:
    text = re.sub(r"\s+", " ", html).strip()
    return re.sub(r">\s+<", "><", text)


_BOOK_HEAD = """
<header class="header container-fluid"><div class="page_inner"><div class="row">
<div class="col-sm-8 h1"><a href="../../index.html">Books to Scrape</a><small> We love being scraped!</small></div>
</div></div></header>
<div class="container-fluid page"><div class="page_inner">
<ul class="breadcrumb"><li><a href="../../index.html">Home</a></li>
<li><a href="../category/books_1/index.html">Books</a></li>
<li><a href="../category/books/{cat_slug}/index.html">{category}</a></li><li class="active">{title}</li></ul>
<div id="messages"></div><div class="content"><div id="promotions"></div><div id="content_inner">
<article class="product_page"><div class="row"><div class="col-sm-6">
<div id="product_gallery" class="carousel"><div class="thumbnail"><div class="carousel-inner">
<div class="item active"><img src="../../media/cache/{img}.jpg" alt="{title}"></div></div></div></div></div>
<div class="col-sm-6 product_main"><h1>{title}</h1><p class="price_color">£{price}</p>
<p class="instock availability"><i class="icon-ok"></i> In stock ({stock} available) </p>
<p class="star-rating {stars}"><i class="icon-star"></i><i class="icon-star"></i><i class="icon-star"></i>
<i class="icon-star"></i><i class="icon-star"></i></p><hr>
<div class="alert alert-warning">
<strong>Warning!</strong> This is a demo website for web scraping purposes. Prices and ratings here were
randomly assigned and have no real meaning.</div></div></div>
<div id="product_description" class="sub-header"><h2>Product Description</h2></div><p>{description}</p>
<div class="sub-header"><h2>Product Information</h2></div>
<table class="table table-striped"><tr><th>UPC</th><td>{upc}</td></tr><tr><th>Product Type</th><td>Books</td></tr>
<tr><th>Price (excl. tax)</th><td>£{price}</td></tr><tr><th>Price (incl. tax)</th><td>£{price}</td></tr>
<tr><th>Tax</th><td>£0.00</td></tr><tr><th>Availability</th><td>In stock ({stock} available)</td></tr>
<tr><th>Number of reviews</th><td>0</td></tr></table>
<div id="reviews"></div></article></div></div></div></div>
<footer class="footer container-fluid"></footer>
"""

_BOOKS = [
    {
        "slug": "a-light-in-the-attic_1000",
        "title": "A Light in the Attic",
        "category": "Poetry",
        "cat_slug": "poetry_23",
        "price": "51.77",
        "stock": 22,
        "stars": "Three",
        "upc": "a897fe39b1053632",
        "img": "fe/72/fe72f0532301ec28892ae79a629a293c",
        "description": (
            "It's hard to imagine a world without A Light in the Attic. This now-classic "
            "collection of poetry and drawings from Shel Silverstein celebrates its 20th "
            "anniversary with this special edition. Silverstein's humorous and creative verse "
            "can amuse the dowdiest of readers. Lemon-faced adults and fidgety kids sit still "
            "and read these rhythmic words and laugh and smile and love that Silverstein. "
        )
        * 3,
    },
    {
        "slug": "tipping-the-velvet_999",
        "title": "Tipping the Velvet",
        "category": "Historical Fiction",
        "cat_slug": "historical-fiction_4",
        "price": "53.74",
        "stock": 20,
        "stars": "One",
        "upc": "90fa61229261140a",
        "img": "08/e9/08e94f3731d7d6b760dfbfbc02ca5c62",
        "description": (
            "Erotic and absorbing...Written with starling power. A novel of the music halls "
            "of late nineteenth-century London, of a young woman from Whitstable and of the "
            "theatre that changes her life. "
        )
        * 4,
    },
    {
        "slug": "soumission_998",
        "title": "Soumission",
        "category": "Fiction",
        "cat_slug": "fiction_10",
        "price": "50.10",
        "stock": 20,
        "stars": "One",
        "upc": "6957f44c3847a760",
        "img": "ee/cf/eecfe998905e455df12064dba399c075",
        "description": (
            "Dans une France assez proche de la nôtre, un homme s'engage dans la carrière "
            "universitaire. Peu motivé par l'enseignement, il s'attend à une vie ennuyeuse. "
        )
        * 4,
    },
]

_WOO_HTML = """
<div id="page" class="hfeed site"><header id="masthead" class="site-header" role="banner">
<div class="site-branding"><p class="site-title"><a href="https://shop.example/">Lumen Lighting</a></p></div>
<nav id="site-navigation" class="main-navigation"><ul id="menu-primary" class="menu">
<li class="menu-item"><a href="https://shop.example/shop/">Shop</a></li>
<li class="menu-item"><a href="https://shop.example/product-category/pendants/">Pendants</a></li>
<li class="menu-item"><a href="https://shop.example/cart/">Cart</a></li></ul></nav></header>
<div id="content" class="site-content"><div class="col-full"><div class="woocommerce"></div>
<nav class="woocommerce-breadcrumb"><a href="https://shop.example">Home</a> / <a
 href="https://shop.example/product-category/pendants/">Pendants</a> / Aurora Opal Pendant 60</nav>
<div id="primary" class="content-area"><main id="main" class="site-main">
<div id="product-412" class="product type-product post-412 status-publish first instock product_cat-pendants
 has-post-thumbnail shipping-taxable purchasable product-type-simple">
<div class="woocommerce-product-gallery woocommerce-product-gallery--with-images images" data-columns="4">
<div class="woocommerce-product-gallery__wrapper">
<div data-thumb="https://shop.example/wp-content/uploads/aurora-60-100x100.jpg" class="woocommerce-product-gallery__image">
<a href="https://shop.example/wp-content/uploads/aurora-60.jpg"><img
 src="https://shop.example/wp-content/uploads/aurora-60-600x600.jpg" class="wp-post-image" alt="Aurora Opal Pendant 60"
 data-large_image="https://shop.example/wp-content/uploads/aurora-60.jpg"></a></div>
<div data-thumb="https://shop.example/wp-content/uploads/aurora-60-detail-100x100.jpg" class="woocommerce-product-gallery__image">
<a href="https://shop.example/wp-content/uploads/aurora-60-detail.jpg"><img
 src="https://shop.example/wp-content/uploads/aurora-60-detail-600x600.jpg" alt="Aurora detail"></a></div></div></div>
<div class="summary entry-summary"><h1 class="product_title entry-title">Aurora Opal Pendant 60</h1>
<p class="price"><span class="woocommerce-Price-amount amount"><bdi>249,00&nbsp;<span
 class="woocommerce-Price-currencySymbol">€</span></bdi></span></p>
<div class="woocommerce-product-details__short-description"><p>Mouth-blown opal glass shade, 60 cm,
 E27 socket, textile cable 150 cm.</p></div>
<p class="stock in-stock">12 in stock</p>
<form class="cart" action="https://shop.example/product/aurora-opal-pendant-60/" enctype="multipart/form-data">
<div class="quantity"><label class="screen-reader-text">Aurora Opal Pendant 60 quantity</label><input
 class="input-text qty text" name="quantity"></div><button name="add-to-cart" class="single_add_to_cart_button button alt">Add to cart</button></form>
<div class="product_meta"><span class="sku_wrapper">SKU: <span class="sku">AUR-60-OP</span></span>
<span class="posted_in">Category: <a href="https://shop.example/product-category/pendants/">Pendants</a></span></div></div>
<div class="woocommerce-tabs wc-tabs-wrapper"><ul class="tabs wc-tabs">
<li class="description_tab" id="tab-title-description"><a href="#tab-description">Description</a></li>
<li class="additional_information_tab" id="tab-title-additional_information"><a href="#tab-additional_information">Additional information</a></li></ul>
<div class="woocommerce-Tabs-panel woocommerce-Tabs-panel--description panel entry-content wc-tab" id="tab-description">
<h2>Description</h2>{description}</div>
<div class="woocommerce-Tabs-panel woocommerce-Tabs-panel--additional_information panel entry-content wc-tab" id="tab-additional_information">
<h2>Additional information</h2><table class="woocommerce-product-attributes shop_attributes">
<tr class="woocommerce-product-attributes-item woocommerce-product-attributes-item--weight"><th class="woocommerce-product-attributes-item__label">Weight</th>
<td class="woocommerce-product-attributes-item__value">3.2 kg</td></tr>
<tr class="woocommerce-product-attributes-item woocommerce-product-attributes-item--dimensions"><th class="woocommerce-product-attributes-item__label">Dimensions</th>
<td class="woocommerce-product-attributes-item__value">60 × 60 × 32 cm</td></tr>
<tr class="woocommerce-product-attributes-item woocommerce-product-attributes-item--attribute_pa_socket"><th class="woocommerce-product-attributes-item__label">Socket</th>
<td class="woocommerce-product-attributes-item__value"><p>E27</p></td></tr></table></div></div>
<section class="related products"><h2>Related products</h2><ul class="products columns-4">{related}</ul></section>
</div></main></div></div></div>
<footer id="colophon" class="site-footer"><div class="site-info">© Lumen Lighting</div></footer></div>
"""

_WOO_DESCRIPTION = (
    "<p>The Aurora pendant is blown by hand in a small glassworks; every shade differs a "
    "little in the thickness of its opal layer. Lit, it gives an even, glare-free light "
    "over a dining table.</p><ul><li>Opal glass, mouth-blown</li><li>Brushed brass canopy</li>"
    "<li>Dimmable with a phase-cut dimmer</li></ul>"
) * 3
_WOO_RELATED = "".join(
    f'<li class="product type-product post-{400 + n} instock product_cat-pendants">'
    f'<a href="https://shop.example/product/related-{n}/" class="woocommerce-LoopProduct-link">'
    f'<img src="https://shop.example/wp-content/uploads/related-{n}-300x300.jpg" alt="Related {n}">'
    f'<h2 class="woocommerce-loop-product__title">Related pendant {n}</h2>'
    f'<span class="price"><span class="woocommerce-Price-amount amount"><bdi>{99 + n * 10},00 €</bdi>'
    f"</span></span></a></li>"
    for n in range(1, 5)
)


def samples() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, book in enumerate(_BOOKS):
        html = _squash(_BOOK_HEAD.format(**book))
        rows.append(
            {
                "sample_id": f"smp_01M795V203DSZR7FRG1Y0MSN2{index}",
                "url": f"https://books.toscrape.com/catalogue/{book['slug']}/index.html",
                "title": f"{book['title']} | Books to Scrape - Sandbox",
                "template_hint": {
                    "matched_template_id": None,
                    "suggested_template_id": "book_page",
                    "signature": "3c1d9f0e2b7a4c55",
                },
                "jsonld": [],
                "meta": {"description": book["description"][:160].strip()},
                "distilled_html": html,
                "distilled_html_truncated": False,
                "extracted": None,
            }
        )
    woo = _squash(_WOO_HTML.format(description=_WOO_DESCRIPTION, related=_WOO_RELATED))
    rows.append(
        {
            "sample_id": "smp_01M795V3YNZPNXNTP81DA328YQ",
            "url": "https://shop.example/product/aurora-opal-pendant-60/",
            "title": "Aurora Opal Pendant 60 – Lumen Lighting",
            "template_hint": {
                "matched_template_id": None,
                "suggested_template_id": "product",
                "signature": "9f2c4e1a7b3d5f60",
            },
            "jsonld": [
                {
                    "@context": "https://schema.org/",
                    "@type": "Product",
                    "@id": "https://shop.example/product/aurora-opal-pendant-60/#product",
                    "name": "Aurora Opal Pendant 60",
                    "url": "https://shop.example/product/aurora-opal-pendant-60/",
                    "description": "Mouth-blown opal glass shade, 60 cm, E27 socket.",
                    "image": "https://shop.example/wp-content/uploads/aurora-60.jpg",
                    "sku": "AUR-60-OP",
                    "offers": [
                        {
                            "@type": "Offer",
                            "price": "249.00",
                            "priceCurrency": "EUR",
                            "availability": "http://schema.org/InStock",
                            "url": "https://shop.example/product/aurora-opal-pendant-60/",
                            "seller": {"@type": "Organization", "name": "Lumen Lighting"},
                        }
                    ],
                },
                {
                    "@context": "https://schema.org/",
                    "@type": "BreadcrumbList",
                    "itemListElement": [
                        {"@type": "ListItem", "position": 1, "item": {"name": "Home"}},
                        {"@type": "ListItem", "position": 2, "item": {"name": "Pendants"}},
                    ],
                },
            ],
            "meta": {
                "og:title": "Aurora Opal Pendant 60",
                "og:type": "product",
                "og:image": "https://shop.example/wp-content/uploads/aurora-60.jpg",
                "product:price:amount": "249.00",
                "product:price:currency": "EUR",
            },
            "distilled_html": woo,
            "distilled_html_truncated": False,
            "extracted": None,
        }
    )
    return rows
