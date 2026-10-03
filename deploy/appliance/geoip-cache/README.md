# Optional checked build input

`uv run python scripts/prepare_geoip_cache.py` copies the installed compiled
Geo-IP/ASN tables here, recording a checksum for every file. This generated
directory is ignored by Git. Docker verifies the checksums and includes the
existing source-edition/licence manifest. Without the cache, the usual build
downloads and compiles the public databases. The copied release bundle needs
no runtime download.
