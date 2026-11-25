// Waarnemingen - frontend
// Gebruik relatieve URL's ("./api/..."), anders werkt het niet via Ingress.

// TODO: kaart aanmaken
//   const map = L.map("map");
//   Achtergrond: OpenStreetMap of PDOK (BRT-achtergrondkaart / luchtfoto)

// TODO: config ophalen
//   fetch("./api/config") -> centreer de kaart op je thuislocatie
//   Optioneel: cirkel tekenen met de zoekstraal (L.circle)

// TODO: waarnemingen ophalen
//   fetch("./api/observations")
//   Per waarneming een marker. Let op: point.coordinates is [lng, lat],
//   Leaflet wil [lat, lng].

// TODO: popup per marker
//   species_detail.name, species_detail.scientific_name, date, time, number,
//   photos[0] (als die er is) en een link naar permalink
