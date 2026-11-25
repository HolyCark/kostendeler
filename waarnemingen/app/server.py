"""Waarnemingen - recente waarnemingen van waarneming.nl op een kaart.

Home Assistant-app (Ingress). Alleen Python-standaardbibliotheek.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8099"))
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
OPTIONS_PATH = "/data/options.json"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
API_BASE = "https://waarneming.nl/api/v1"

# Ingress-verkeer komt altijd van de Supervisor-proxy.
ALLOWED_IPS = ["172.30.32.2"]


# TODO: load_options()
#   Lees OPTIONS_PATH in en geef radius en days terug.


# TODO: get_home_location()
#   GET http://supervisor/core/api/config
#   Header: Authorization: Bearer <SUPERVISOR_TOKEN>
#   Geeft o.a. "latitude" en "longitude" terug.


# TODO: fetch_observations(lat, lng, days, radius)
#   GET {API_BASE}/observations/around-point/?coordinates=<lat>,<lng>&days=..&radius=..
#   - Antwoord: {"count", "next", "previous", "results"}; volg "next" voor meer pagina's
#   - Let op: point.coordinates is [lng, lat]
#   - Cache het resultaat een paar minuten, zodat je waarneming.nl niet bij elke klik belast


# TODO: fetch_groups()
#   GET {API_BASE}/species-groups/  (namen van soortgroepen, mag lang gecachet worden)


class Handler(BaseHTTPRequestHandler):
    # TODO: do_GET
    #   - Controleer self.client_address[0] tegen ALLOWED_IPS
    #   - /api/config        -> thuislocatie + radius + days als JSON
    #   - /api/observations  -> fetch_observations(...) als JSON
    #   - /api/groups        -> fetch_groups() als JSON
    #   - anders             -> bestand uit STATIC_DIR (index.html bij "/")
    pass


if __name__ == "__main__":
    # TODO: start de server
    #   httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    #   httpd.serve_forever()
    pass
