# data

- `cities15000.tsv.gz`: GeoNames cities15000 (https://download.geonames.org/export/dump/, CC BY 4.0,
  attribution "Places: GeoNames"), trimmed to: geonameid, name, asciiname, country code, admin1 name
  (from admin1CodesASCII.txt), lat, lon (2 decimals), population; tab-separated, gzipped. Used by
  places.py. 34,208 cities, 2.1 MB raw, 750 KB gzipped (cities5000 would be 4.3 MB / 1.5 MB,
  cities1000 10.6 MB / 3.4 MB).
