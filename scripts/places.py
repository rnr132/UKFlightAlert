"""IATA airport code -> human place name, for the weekly digest.

Offline lookup only — `airportsdata` and `pycountry_convert` both bundle
static data, no network, so this holds to the brief's no-external-calls
rule even though notify.py isn't in the sweep path. A tiny manual override
covers metro/island codes airportsdata doesn't carry; it can grow if more
gaps turn up (they show as the raw code under "Other", never a crash).
"""
import airportsdata
import pycountry_convert as pcc

_AIRPORTS = airportsdata.load("IATA")

# Metro / island codes airportsdata (airport-level) doesn't resolve.
_OVERRIDES = {
    "BUH": ("Bucharest", "Romania", "Europe"),
    "TCI": ("Tenerife", "Spain", "Europe"),
    "BAK": ("Baku", "Azerbaijan", "Asia"),
}

_CONTINENT_NAMES = {
    "AF": "Africa",
    "AS": "Asia",
    "EU": "Europe",
    "NA": "North America",
    "SA": "South America",
    "OC": "Oceania",
    "AN": "Antarctica",
}


def resolve(iata):
    """Return (city, country, continent) in English. Unknown codes fall
    back to (code, "", "Other") — visible in the digest, never fatal."""
    if iata in _OVERRIDES:
        return _OVERRIDES[iata]

    rec = _AIRPORTS.get(iata)
    if not rec:
        return (iata, "", "Other")

    # airportsdata city fields sometimes carry a location descriptor after
    # a slash ("Bergerac/Roumaniere") — keep the city, drop the rest.
    city = rec["city"].split("/")[0].strip() or iata

    try:
        country = pcc.country_alpha2_to_country_name(rec["country"])
    except KeyError:
        country = rec["country"]

    try:
        continent = _CONTINENT_NAMES.get(
            pcc.country_alpha2_to_continent_code(rec["country"]), "Other"
        )
    except KeyError:
        continent = "Other"

    return (city, country, continent)
