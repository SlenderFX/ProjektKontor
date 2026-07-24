# SEO-Vorbereitung für ProjektKontor

Stand: 16. Juli 2026 – Vorbereitung, noch kein Marktstart

## Positionierung

ProjektKontor soll nicht allgemein als weiteres Aufgaben- oder Kanban-Tool
positioniert werden. Der unterscheidende Such- und Inhaltsfokus liegt auf der
Verbindung von realitätsnahen beruflichen Handlungssituationen,
handlungsorientiertem Unterricht und dem Modell der vollständigen Handlung.

Zentrale Aussage:

> Berufliche Handlungssituationen vollständig durchlaufen.

## Suchintentionen und Themenfelder

Priorität A – präzise Produkt- und Problembegriffe:

- berufliche Handlungssituationen digital
- vollständige Handlung Software
- berufliche Simulation Unterricht
- digitale Arbeitsumgebung Berufsschule
- handlungsorientierter Unterricht Software
- Projektmanagement Schule
- Projektorganisation Unterricht
- Projektmanagement Berufsschule
- Software für handlungsorientierten Unterricht
- digitale Projektarbeit Schule

Priorität B – didaktische Informationssuchen:

- vollständige Handlung digital unterstützen
- vollständige Handlung Berufsschule
- handlungsorientierter Unterricht Projektarbeit
- Projektunterricht organisieren
- selbstreguliertes Lernen Projektunterricht

Priorität C – spätere vertiefende Inhalte:

- Kanban im Unterricht
- Rollen und Verantwortung in Schülerprojekten
- Projektbericht Schule
- Lernfeldunterricht Projektorganisation
- Projektarbeit im Wirtschaftsunterricht

Die Begriffe werden natürlich in Überschriften und Texten verwendet. Es werden
keine künstlichen Wiederholungen oder austauschbare SEO-Texte angelegt.

## Technische Trennung

- `/` ist die indexierbare Produktseite und kanonische Adresse.
- `/login` trägt `noindex` und bleibt außerhalb der Sitemap.
- `/api/`, Uploads und Berichte sind nicht öffentlich zugänglich und tragen
  zusätzlich einen `X-Robots-Tag` mit `noindex`.
- `robots.txt` sperrt `/api/`, lässt die öffentliche Produktseite erreichbar
  und verweist auf die XML-Sitemap.
- Die Sitemap enthält aktuell ausschließlich die öffentliche Startseite.
- Titel, Beschreibung, Open-Graph-Daten und strukturierte Angaben zu Website
  und Software sind vorbereitet.

## Inhaltliche Architektur zum Start

Der Onepager deckt zunächst eine eindeutige Suchintention ab. Vor einem
größeren Marktstart sollten eigenständige, indexierbare Seiten folgen:

1. `/vollstaendige-handlung` – fachliche Erläuterung und Produktbezug,
2. `/berufliche-handlungssituationen` – realitätsnahe Simulation und Lernfeldbezug,
3. `/projektmanagement-berufsschule` – Einsatzszenarien und Abgrenzung,
4. `/kanban-im-unterricht` – didaktisch begründete Anwendung,
5. `/datenschutz-und-betrieb` – Hosting, Rollen und Datenkontrolle,
6. `/funktionen` – überprüfbare Funktionsübersicht,
7. `/impressum` und `/datenschutz` – rechtlich freigegebene öffentliche Seiten.

Jede Seite benötigt eine eigene Suchintention und darf nicht lediglich Text
des Onepagers wiederholen.

## Vor dem Marktstart

- Betreiber, Impressum, Datenschutzkontakt und Rechtsgrundlage finalisieren.
- Kontaktformular nach Einrichtung von SMTP und Turnstile mit einer echten Anfrage prüfen.
- Google Search Console und Bing Webmaster Tools einrichten.
- Sitemap einreichen und kanonische Domain prüfen.
- Darstellung von Titel, Beschreibung und Social Preview kontrollieren.
- Core Web Vitals und mobile Bedienung messen.
- Strukturierte Daten mit Validatoren prüfen.
- Erst nach praktischer Erprobung belegbare Fallbeispiele veröffentlichen;
  keine erfundenen Bewertungen, Zahlen oder Wirkungsversprechen verwenden.

## Erfolgsmessung

Zum Start genügen datensparsame Kennzahlen:

- Sichtbarkeit und Klicks nach Suchanfrage,
- Zugriffe auf den Produkt-CTA,
- qualifizierte Kontaktanfragen,
- Indexierungsfehler und Seitenerfahrung.

Loginbereich, interne Nutzung, Klassen-, Personen- und Projektdaten werden
nicht für Marketingmessung verwendet.

## Referenzen zur technischen Umsetzung

- [Google SEO Starter Guide](https://developers.google.com/search/docs/fundamentals/seo-starter-guide)
- [Google: Canonical URLs](https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls)
- [Google: Sitemaps](https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap)
- [Google: SoftwareApplication-Daten](https://developers.google.com/search/docs/appearance/structured-data/software-app)
