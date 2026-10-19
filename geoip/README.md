# GeoLite2 databases (optional)

Drop the two free MaxMind databases here to enable country/ASN enrichment in
the dashboard's top-offenders view:

- `GeoLite2-Country.mmdb` — country code + name per IP
- `GeoLite2-ASN.mmdb` — AS number + org (for DE residential IPs usually the ISP itself)

Download (free): official MaxMind GeoLite2 page (account required,
https://dev.maxmind.com/geoip/geolite2-free-geolocation-data), or without an
account via the P3TERX mirror — https://github.com/P3TERX/GeoLite.mmdb
(releases contain both files, refreshed monthly).

The files are read-only inputs for the `caddylog-alert` container (mounted at
`/var/lib/caddylog/geoip`). Without them everything still works — the CGNAT
flag and mobile-UA share need no data files, and country/ASN columns simply
stay empty until a later hourly run backfills them. MaxMind updates the free
databases roughly monthly; re-download and replace in place when you like.
