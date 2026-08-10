# SEO-Vorbereitung für ProjektKontor

Stand: 10. August 2026 – organische Markteinführung ohne bezahlte Anzeigen

## Ziel: qualifizierte organische Nachfrage

ProjektKontor soll Interessierte über hilfreiche Suchergebnisse, fachliche
Empfehlungen und direkte Verweise erreichen. Bezahlte Such- oder Social-Anzeigen
sind nicht Bestandteil der Markteinführung. SEO wird dabei nicht als Ersatz für
ein gutes Produktversprechen verstanden, sondern macht fachlich hilfreiche und
glaubwürdige Inhalte auffindbar.

Eine Platzierung kann nicht garantiert werden. Die Strategie reduziert die
Abhängigkeit von Anzeigen durch einen klaren Themenschwerpunkt, eigene fachliche
Inhalte, saubere Indexierung und kontinuierliche Auswertung in der Search Console.

## Positionierung

ProjektKontor soll nicht allgemein als weiteres Aufgaben- oder Kanban-Tool
positioniert werden. Der unterscheidende Such- und Inhaltsfokus liegt auf der
Verbindung von realitätsnahen beruflichen Handlungssituationen,
handlungsorientiertem Unterricht und dem Modell der vollständigen Handlung.

Zentrale Aussage:

> Berufliche Handlungssituationen vollständig durchlaufen.

## Suchintentionen und Themenfelder

Die Ansprache folgt zwei unterschiedlichen Wegen: Lehrkräfte suchen nach einer
Lösung für ein konkretes Unterrichtsproblem; Fachbereichs- und
Bildungsgangleitungen, Schulleitungen sowie gegebenenfalls Schulträger benötigen
zusätzlich belastbare Informationen zu gemeinsamer Nutzung, Einführung,
Datenschutz und Kosten. Organische Inhalte müssen beide Wege abdecken, ohne
dieselbe Seite für jede Zielgruppe zu vervielfältigen.

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
- Die Sitemap enthält die öffentliche Startseite und das vollständige Impressum.
- Titel, Beschreibung, Open-Graph-Daten und strukturierte Angaben zu Website
  und Software sind auf die zentrale Suchintention abgestimmt.
- Die Startseite beantwortet sichtbare, konkrete Fragen von Lehrkräften. Diese
  Inhalte dienen Menschen und werden nicht als unsichtbarer SEO-Text angelegt.
- Das vollständige Impressum ist indexierbar und stärkt die überprüfbare
  Anbietertransparenz; Login und interne Bereiche bleiben ausgeschlossen.

## Inhaltliche Architektur zum Start

Der Onepager deckt zunächst eine eindeutige Suchintention ab. Vor einem
größeren Marktstart sollten eigenständige, indexierbare Seiten folgen:

1. `/vollstaendige-handlung` – fachliche Erläuterung und Produktbezug,
2. `/berufliche-handlungssituationen` – realitätsnahe Simulation und Lernfeldbezug,
3. `/projektmanagement-berufsschule` – Einsatzszenarien und Abgrenzung,
4. `/kanban-im-unterricht` – didaktisch begründete Anwendung,
5. `/datenschutz-und-betrieb` – Hosting, Rollen und Datenkontrolle,
6. `/funktionen` – überprüfbare Funktionsübersicht,
7. `/impressum` und `/datenschutz` – öffentliche Anbieter- und Datenschutzinformationen (umgesetzt; die schulbezogenen Angaben bleiben installationsabhängig).

Jede Seite benötigt eine eigene Suchintention und darf nicht lediglich Text
des Onepagers wiederholen.

## Vor dem Marktstart

- Die öffentliche Datenschutzerklärung bei Änderungen an Dienstleistern oder Funktionen aktualisieren; Datenschutzkontakt und Rechtsgrundlage der jeweiligen Schule vor deren Produktivbetrieb finalisieren.
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

## Organischer Veröffentlichungsrhythmus

Die nächsten Fachseiten werden nicht gleichzeitig als dünne Landingpages
veröffentlicht. Sinnvoll ist zunächst ein belastbarer Beitrag pro Monat, der
eine echte Frage aus Unterricht oder Einführung vollständig beantwortet.

1. Eine fachliche Hauptseite veröffentlichen und intern von der Startseite verlinken.
2. Indexierung und tatsächliche Suchanfragen in der Search Console beobachten.
3. Inhalte anhand echter Rückfragen von Lehrkräften ergänzen.
4. Erst nach eigenen Einsätzen Fallbeispiele mit nachvollziehbarem Kontext ergänzen.
5. Quartalsweise Titel, Klickrate, Indexierung und qualifizierte Anfragen prüfen.

Reichweite außerhalb von Suchmaschinen entsteht ergänzend durch fachliche
Beiträge, Fortbildungen, Fachbereichsnetzwerke und Empfehlungen. Der kostenfreie
Test senkt die Einstiegshürde für Lehrkräfte; Fachbereichs- und Schullizenzen
verlagern die spätere Kaufentscheidung dorthin, wo Budgets gebündelt werden
können. Alle Verweise führen auf dieselben hilfreichen Inhalte; es werden keine
separaten Werbe-Landingpages benötigt.

## Referenzen zur technischen Umsetzung

- [Google SEO Starter Guide](https://developers.google.com/search/docs/fundamentals/seo-starter-guide)
- [Google: Canonical URLs](https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls)
- [Google: Sitemaps](https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap)
- [Google: SoftwareApplication-Daten](https://developers.google.com/search/docs/appearance/structured-data/software-app)
