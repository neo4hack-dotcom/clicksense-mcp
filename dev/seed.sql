-- Demo dataset: a European e-commerce shop. Deterministic (hash-based), ~23M rows, ~300 MB.
-- Values are deliberately "realistic-ambiguous": countries are ISO codes (users say "France",
-- data says 'FR'; 'CA' is Canada), some cities carry accents or hyphens ('Saint-Étienne',
-- 'Montréal'), statuses are lowercase codes while users say "cancelled orders" or "refunds".

DROP DATABASE IF EXISTS shop;
CREATE DATABASE shop COMMENT 'E-commerce demo: customers, products, orders, web events';

CREATE TABLE shop.customers
(
    customer_id UInt32 COMMENT 'Customer surrogate key',
    name        String COMMENT 'Full name',
    email       String COMMENT 'Email address (PII)',
    country     LowCardinality(String) COMMENT 'ISO 3166-1 alpha-2 country code',
    city        LowCardinality(String) COMMENT 'City',
    segment     LowCardinality(String) COMMENT 'B2C | B2B | VIP',
    signup_date Date COMMENT 'Account creation date'
)
ENGINE = MergeTree
ORDER BY customer_id
COMMENT 'One row per customer account';

CREATE TABLE shop.products
(
    product_id UInt32 COMMENT 'Product surrogate key',
    name       String COMMENT 'Product name',
    category   LowCardinality(String) COMMENT 'Product category',
    brand      LowCardinality(String) COMMENT 'Brand',
    unit_price Decimal(10, 2) COMMENT 'Current list price, EUR incl. VAT'
)
ENGINE = MergeTree
ORDER BY product_id
COMMENT 'Product catalogue';

CREATE TABLE shop.orders
(
    order_id    UInt64 COMMENT 'Order line id',
    order_date  Date COMMENT 'Order date (partition key)',
    order_ts    DateTime('Europe/Paris') COMMENT 'Order timestamp',
    customer_id UInt32 COMMENT 'FK customers.customer_id',
    product_id  UInt32 COMMENT 'FK products.product_id',
    quantity    UInt16 COMMENT 'Units ordered',
    amount_eur  Decimal(12, 2) COMMENT 'Line amount EUR incl. VAT',
    status      LowCardinality(String) COMMENT 'completed | cancelled | refunded | pending',
    channel     LowCardinality(String) COMMENT 'web | app | store | marketplace',
    country     LowCardinality(String) COMMENT 'Delivery country, ISO alpha-2'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(order_date)
ORDER BY (order_date, customer_id)
COMMENT 'Order lines, one row per product per order';

CREATE TABLE shop.web_events
(
    event_ts    DateTime('Europe/Paris') COMMENT 'Event timestamp',
    event_date  Date COMMENT 'Event date (partition key)',
    session_id  UInt64 COMMENT 'Web session id',
    customer_id UInt32 COMMENT '0 = anonymous visitor',
    event_type  LowCardinality(String) COMMENT 'page_view | add_to_cart | checkout | purchase',
    page        String COMMENT 'URL path',
    device      LowCardinality(String) COMMENT 'desktop | mobile | tablet'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_date)
ORDER BY (event_date, event_type, intHash32(session_id))
SAMPLE BY intHash32(session_id)
COMMENT 'Clickstream, ~20M events';

INSERT INTO shop.customers
SELECT
    toUInt32(number + 1) AS customer_id,
    concat(
        ['Jean','Marie','Camille','Lucas','Léa','Hugo','Chloé','Louis','Emma','Nathan','Inès','Jules','Manon','Arthur','Zoé','Paul'][cityHash64(number, 'f') % 16 + 1],
        ' ',
        ['Martin','Bernard','Dubois','Thomas','Robert','Richard','Petit','Durand','Leroy','Moreau','Simon','Laurent','Lefebvre','Michel','Garcia','Roux'][cityHash64(number, 'l') % 16 + 1]
    ) AS name,
    concat('user', toString(number + 1), '@example.com') AS email,
    ['FR','FR','FR','FR','FR','BE','BE','CH','DE','ES','IT','CA','US','GB'][cityHash64(number, 'c') % 14 + 1] AS country,
    multiIf(
        country = 'FR', ['Paris','Lyon','Marseille','Toulouse','Bordeaux','Lille','Nantes','Saint-Étienne'][cityHash64(number, 'v') % 8 + 1],
        country = 'BE', ['Brussels','Liège','Ghent'][cityHash64(number, 'v') % 3 + 1],
        country = 'CH', ['Geneva','Lausanne','Zurich'][cityHash64(number, 'v') % 3 + 1],
        country = 'DE', ['Berlin','Munich'][cityHash64(number, 'v') % 2 + 1],
        country = 'ES', ['Madrid','Barcelona'][cityHash64(number, 'v') % 2 + 1],
        country = 'IT', ['Rome','Milan'][cityHash64(number, 'v') % 2 + 1],
        country = 'CA', ['Montréal','Quebec City','Toronto'][cityHash64(number, 'v') % 3 + 1],
        country = 'US', ['New York','San Francisco'][cityHash64(number, 'v') % 2 + 1],
        ['London','Manchester'][cityHash64(number, 'v') % 2 + 1]
    ) AS city,
    ['B2C','B2C','B2C','B2C','B2C','B2C','B2B','B2B','VIP'][cityHash64(number, 's') % 9 + 1] AS segment,
    toDate('2020-01-01') + toIntervalDay(cityHash64(number, 'd') % 2400) AS signup_date
FROM numbers(200000);

INSERT INTO shop.products
SELECT
    toUInt32(number + 1) AS product_id,
    concat(category, ' ', brand, ' #', toString(number + 1)) AS name,
    ['Home Appliances','Computers','Garden','Home & Living','Sports','Phones','Video Games','Books'][cityHash64(number, 'cat') % 8 + 1] AS category,
    ['Zenith','Atlas','Nova','Orion','Lumen','Vega','Kappa','Sirius','Delta','Helix'][cityHash64(number, 'br') % 10 + 1] AS brand,
    toDecimal64(5 + (cityHash64(number + 1, 'p') % 99500) / 100, 2) AS unit_price
FROM numbers(5000);

INSERT INTO shop.orders
SELECT
    number + 1 AS order_id,
    toDate('2024-01-01') + toIntervalDay(cityHash64(number, 'od') % 1034) AS order_date,
    toDateTime(order_date, 'Europe/Paris') + toIntervalSecond(cityHash64(number, 'ot') % 86400) AS order_ts,
    toUInt32(cityHash64(number, 'oc') % 200000 + 1) AS customer_id,
    toUInt32(cityHash64(number, 'op') % 5000 + 1) AS pid,
    toUInt16(cityHash64(number, 'oq') % 4 + 1) AS quantity,
    toDecimal128(quantity * (5 + (cityHash64(pid, 'p') % 99500) / 100), 2) AS amount_eur,
    ['completed','completed','completed','completed','completed','completed','completed','completed','completed','completed','completed','completed','completed','completed','completed','completed','completed','cancelled','refunded','pending'][cityHash64(number, 'os') % 20 + 1] AS status,
    ['web','web','web','app','app','store','marketplace'][cityHash64(number, 'ch') % 7 + 1] AS channel,
    ['FR','FR','FR','FR','FR','BE','BE','CH','DE','ES','IT','CA','US','GB'][cityHash64(customer_id, 'c') % 14 + 1] AS country
FROM numbers(3000000);

INSERT INTO shop.web_events
SELECT
    toDateTime(toDate('2024-01-01') + toIntervalDay(cityHash64(intDiv(number, 8), 'ed') % 1034), 'Europe/Paris')
        + toIntervalSecond(cityHash64(intDiv(number, 8), 'et') % 86000 + number % 8 * 30) AS event_ts,
    toDate(event_ts) AS event_date,
    intDiv(number, 8) + 1 AS session_id,
    if(cityHash64(intDiv(number, 8), 'anon') % 3 = 0, toUInt32(0), toUInt32(cityHash64(intDiv(number, 8), 'ec') % 200000 + 1)) AS customer_id,
    multiIf(number % 8 < 5, 'page_view', number % 8 < 7, 'add_to_cart', cityHash64(intDiv(number, 8), 'buy') % 3 = 0, 'purchase', 'checkout') AS event_type,
    ['/','/search','/product','/cart','/checkout','/account','/promo'][cityHash64(number, 'pg') % 7 + 1] AS page,
    ['desktop','mobile','mobile','tablet'][cityHash64(intDiv(number, 8), 'dv') % 4 + 1] AS device
FROM numbers(20000000);

OPTIMIZE TABLE shop.customers FINAL;
OPTIMIZE TABLE shop.products FINAL;
