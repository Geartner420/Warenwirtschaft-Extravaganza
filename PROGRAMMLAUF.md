# Programmlaufdiagramm

Dieses Dokument beschreibt den groben Ablauf der Warenwirtschaft-App. Die Diagramme
sind in Mermaid geschrieben und werden auf GitHub direkt gerendert.

## Serverstart

```mermaid
flowchart TD
    A["Start: python3 app.py"] --> B["Argumente lesen"]
    B --> C["Datenbank initialisieren"]
    C --> D["Backup bei Bedarf erstellen"]
    D --> E["Tabellen, Indizes und Trigger pruefen"]
    E --> F["Fehlende Material-, Nutzer- und Ort-Codes erzeugen"]
    F --> G{"HTTPS aktiv?"}
    G -- "ja" --> H["Lokales Zertifikat pruefen oder erzeugen"]
    H --> I["HTTP-Server mit TLS starten"]
    G -- "nein" --> J["HTTP-Server starten"]
    I --> K["Adressen im Terminal anzeigen"]
    J --> K
    K --> L["Anfragen bearbeiten bis Strg+C"]
```

## Anfrageverarbeitung

```mermaid
flowchart TD
    A["Browser sendet Anfrage"] --> B{"Methode"}
    B -- "GET" --> C{"Pfad"}
    C -- "/" --> D["Desktop-Seite rendern"]
    C -- "/mobile" --> E["Mobile Seite rendern"]
    C -- "/labels/..." --> F["Barcode-Labels rendern"]
    C -- "/export/material.csv" --> G["CSV exportieren"]
    C -- "sonst" --> H["404"]
    B -- "POST" --> I{"Pfad"}
    I -- "/materials/add" --> J["Material anlegen oder Bestand erhoehen"]
    I -- "/materials/remove" --> K["Material ausbuchen"]
    I -- "/scan/book" --> L["Einzelnen Materialcode buchen"]
    I -- "/scan/batch" --> M["Stapel-Scan verbuchen"]
    I -- "/users/add" --> N["Nutzer mit Code anlegen"]
    I -- "/locations/add" --> O["Ort mit Code anlegen"]
    I -- "/loans/add" --> P["Verleih oder Ausgabe buchen"]
    I -- "/loans/return" --> Q["Rueckgabe buchen"]
    I -- "/api/users" --> R["Nutzer im Wizard per JSON anlegen"]
    I -- "/api/locations" --> S["Ort im Wizard per JSON anlegen"]
    I -- "sonst" --> H
    J --> T["Redirect mit Meldung"]
    K --> T
    L --> T
    M --> T
    N --> T
    O --> T
    P --> T
    Q --> T
```

## Stapel-Scan fuer Verleih oder Ausgabe

```mermaid
flowchart TD
    A["Codes nacheinander scannen"] --> B{"Code-Typ"}
    B -- "WX..." --> C["Material im Stapel hochzaehlen"]
    B -- "WU..." --> D["Nutzer fuer Stapel setzen"]
    B -- "WO..." --> E["Ort fuer Stapel setzen"]
    B -- "unbekannt" --> F["Fehler anzeigen"]
    C --> G{"Buchen gedrueckt?"}
    D --> G
    E --> G
    G -- "nein" --> A
    G -- "ja" --> H{"Material, Nutzer und Ort vorhanden?"}
    H -- "nein" --> I["Buchung blockieren"]
    H -- "ja" --> J["Transaktion starten"]
    J --> K{"Genug Bestand frei?"}
    K -- "nein" --> L["Rollback und Fehlermeldung"]
    K -- "ja" --> M{"Verbrauchsmaterial?"}
    M -- "ja" --> N["Bestand reduzieren und Ausgabe protokollieren"]
    M -- "nein" --> O["Ausleihe anlegen und freien Bestand reduzieren"]
    N --> P["Transaktion speichern"]
    O --> P
    P --> Q["Erfolgsmeldung anzeigen"]
```

## Label-Erstellung

```mermaid
flowchart TD
    A["Material, Nutzer oder Ort wird erstellt"] --> B["Datensatz speichern"]
    B --> C["Code aus ID erzeugen"]
    C --> D["Code am Datensatz speichern"]
    D --> E["Liste neu rendern"]
    E --> F["Code und Link 'Label drucken' anzeigen"]
    F --> G["Label-Seite oeffnen"]
    G --> H["Code 128 Barcode als SVG rendern"]
    H --> I["Browser-Druckdialog nutzen"]
```
