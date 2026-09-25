"""Je ein Beispiel pro Katalogtyp – für die Galerie, die Tests und als Orientierung für den Agenten."""

EXAMPLES: dict[str, dict] = {
    "fraction_bar": {
        "type": "fraction_bar", "title": "1/2 und 1/3 in Sechstel verwandeln",
        "alt": "Zwei Bruchstreifen: 3 von 6 Teilen und 2 von 6 Teilen gefärbt.",
        "bars": [{"numerator": 3, "denominator": 6, "label": "1/2 = 3/6"},
                 {"numerator": 2, "denominator": 6, "label": "1/3 = 2/6"}]},
    "fraction_circle": {
        "type": "fraction_circle", "title": "Gleich viel – anders geteilt",
        "alt": "Drei Kreise: 1/2, 2/4 und 4/8 sind gleich groß gefärbt.",
        "circles": [{"numerator": 1, "denominator": 2, "label": "Hälfte"},
                    {"numerator": 2, "denominator": 4, "label": "zwei Viertel"},
                    {"numerator": 4, "denominator": 8, "label": "vier Achtel"}]},
    "number_line": {
        "type": "number_line", "title": "Wo liegt 3/4?",
        "alt": "Zahlenstrahl von 0 bis 2 in Vierteln. Drei Sprünge von je 1/4 führen von 0 zu einem Fragezeichen bei 3/4.",
        "start": 0, "end": 2, "major_step": 1, "minor_divisions": 4, "label_format": "fraction",
        "marks": [{"value": "3/4", "question": True}],
        "jumps": [{"start": 0, "end": "1/4", "label": "+1/4"}, {"start": "1/4", "end": "1/2", "label": "+1/4"},
                  {"start": "1/2", "end": "3/4", "label": "+1/4"}]},
    "place_value_chart": {
        "type": "place_value_chart", "title": "Die Zahl 3 407,25",
        "alt": "Stellenwerttafel mit Tausender, Hunderter, Zehner, Einer, Zehntel, Hundertstel: 3 4 0 7 , 2 5.",
        "columns": ["T", "H", "Z", "E", "z", "h"], "rows": [["3", "4", "0", "7", "2", "5"]], "decimal_after": 3,
        "highlight_columns": [2]},
    "area_model": {
        "type": "area_model", "title": "2/3 von 3/4",
        "alt": "Raster aus 3 mal 4 Kästchen. 3/4 der Spalten blau, 2/3 der Zeilen orange; die Überlappung zeigt 6/12.",
        "rows": 3, "cols": 4, "row_label": "2/3", "col_label": "3/4",
        "regions": [{"row_start": 0, "row_end": 3, "col_start": 0, "col_end": 3, "color": 0, "label": "3/4 des Ganzen"},
                    {"row_start": 0, "row_end": 2, "col_start": 0, "col_end": 4, "color": 1, "label": "2/3 des Ganzen"}]},
    "coordinate_plane": {
        "type": "coordinate_plane", "title": "Gerade y = 2x − 1",
        "alt": "Koordinatensystem mit der Geraden y = 2x − 1 und den Punkten A(0|−1) und B(2|3).",
        "x_range": [-4, 4], "y_range": [-4, 5],
        "functions": [{"expression": "2*x-1", "label": "y = 2x − 1"}],
        "points": [{"x": 0, "y": -1, "label": "A"}, {"x": 2, "y": 3, "label": "B"}]},
    "geometry": {
        "type": "geometry", "title": "Rechtwinkliges Dreieck",
        "alt": "Dreieck ABC mit rechtem Winkel bei C, Seiten a = 3 cm, b = 4 cm, c = 5 cm.",
        "shapes": [{"kind": "polygon", "points": [[1, 1], [9, 1], [1, 7]], "vertex_labels": ["C", "A", "B"],
                    "side_labels": ["b = 4 cm", "c = 5 cm", "a = 3 cm"], "fill": 5},
                   {"kind": "angle", "points": [[9, 1], [1, 1], [1, 7]]}]},
    "balance_scale": {
        "type": "balance_scale", "title": "x + x + 3 = 7",
        "alt": "Waage im Gleichgewicht: links zwei x-Päckchen und ein Gewicht 3, rechts ein Gewicht 7.",
        "left": ["x", "x", "3"], "right": ["7"], "balanced": True},
    "bar_chart": {
        "type": "bar_chart", "title": "Lieblingsobst der Klasse 4b",
        "alt": "Säulendiagramm: Apfel 8, Banane 5, Erdbeere 11, Traube 4 Stimmen. Erdbeere ist hervorgehoben.",
        "categories": ["Apfel", "Banane", "Erdbeere", "Traube"], "values": [8, 5, 11, 4], "y_label": "Stimmen",
        "highlight": [2]},
    "sentence_parts": {
        "type": "sentence_parts", "title": "Satzglieder bestimmen",
        "alt": "Satz: Die Katze fängt am Morgen eine Maus. Subjekt: Die Katze. Prädikat: fängt. "
               "Zeitangabe: am Morgen. Akkusativobjekt: eine Maus.",
        "tokens": [{"text": "Die Katze", "role": "Subjekt"}, {"text": "fängt", "role": "Prädikat"},
                   {"text": "am Morgen", "role": "Zeit"}, {"text": "eine Maus", "role": "Akkusativobjekt"},
                   {"text": "."}]},
    "word_parts": {
        "type": "word_parts", "title": "Wortbausteine",
        "alt": "Wörter in Bausteine zerlegt: ver-kauf-en, Freund-lich-keit.",
        "words": [{"parts": [{"text": "ver", "kind": "praefix"}, {"text": "kauf", "kind": "stamm"},
                             {"text": "en", "kind": "endung"}]},
                  {"parts": [{"text": "Freund", "kind": "stamm"}, {"text": "lich", "kind": "suffix"},
                             {"text": "keit", "kind": "suffix"}]}]},
    "syllables": {
        "type": "syllables", "title": "Silben schwingen",
        "alt": "Wörter mit Silbenbögen: Ba-na-ne, Scho-ko-la-de.",
        "words": [{"syllables": ["Ba", "na", "ne"]}, {"syllables": ["Scho", "ko", "la", "de"]}]},
    "cycle": {
        "type": "cycle", "title": "Der Wasserkreislauf",
        "alt": "Kreislauf: Verdunstung, Wolkenbildung, Niederschlag, Versickern und Abfließen, zurück zum Meer.",
        "nodes": ["Wasser verdunstet", "Wolken entstehen", "Regen fällt", "Wasser fließt ins Meer"],
        "center_label": "Sonne liefert Energie"},
    "flow_diagram": {
        "type": "flow_diagram", "title": "Wie entsteht Strom im Wasserkraftwerk?",
        "alt": "Ablauf: Stausee, Wasser fällt durch Rohr, Turbine dreht sich, Generator erzeugt Strom, Stromnetz.",
        "direction": "vertical",
        "nodes": [{"id": "a", "label": "Wasser im Stausee"}, {"id": "b", "label": "Wasser strömt durch das Rohr"},
                  {"id": "c", "label": "Turbine dreht sich"}, {"id": "d", "label": "Generator erzeugt Strom"}],
        "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "c"},
                  {"source": "c", "target": "d", "label": "Bewegung"}]},
    "timeline": {
        "type": "timeline", "title": "Vom Mittelalter zur Neuzeit",
        "alt": "Zeitstrahl von 800 bis 1600 mit Mittelalter-Epoche und Ereignissen 800, 1150, 1450, 1492.",
        "start": 800, "end": 1600,
        "periods": [{"start": 800, "end": 1500, "label": "Mittelalter"}, {"start": 1500, "end": 1600, "label": "Neuzeit"}],
        "events": [{"year": 800, "label": "Karl der Große wird Kaiser"}, {"year": 1150, "label": "Viele Städte entstehen"},
                   {"year": 1450, "label": "Buchdruck"}, {"year": 1492, "label": "Kolumbus erreicht Amerika"}]},
    "table": {
        "type": "table", "title": "Wertetabelle y = 2x − 1",
        "alt": "Wertetabelle mit x von 0 bis 3 und y-Werten −1, 1, 3, 5.",
        "headers": ["x", "0", "1", "2", "3"], "rows": [["y", "−1", "1", "3", "5"]], "highlight_cells": [[0, 3]]},
    "labeled_diagram": {
        "type": "labeled_diagram", "title": "Aufbau einer Pflanzenzelle",
        "alt": "Schema einer Pflanzenzelle: Zellwand außen, Zellmembran, Zellplasma, Zellkern, Vakuole, Chloroplasten.",
        "aspect": "square",
        "parts": [
            {"id": "zellwand", "shape": "rect", "x": 2, "y": 2, "w": 96, "h": 96, "fill": 2, "label": "Zellwand",
             "anchor": [97, 8]},
            {"id": "membran", "shape": "rect", "x": 7, "y": 7, "w": 86, "h": 86, "fill": 5, "label": "Zellmembran",
             "anchor": [93, 30]},
            {"id": "plasma", "shape": "label", "x": 16, "y": 52, "label": "Zellplasma"},
            {"id": "vakuole", "shape": "ellipse", "x": 58, "y": 55, "w": 50, "h": 44, "fill": 0, "label": "Vakuole"},
            {"id": "kern", "shape": "circle", "x": 26, "y": 30, "r": 10, "fill": 3, "label": "Zellkern"},
            {"id": "chloroplast1", "shape": "ellipse", "x": 22, "y": 72, "w": 14, "h": 8, "fill": 2, "label": "Chloroplast"},
            {"id": "chloroplast2", "shape": "ellipse", "x": 78, "y": 20, "w": 14, "h": 8, "fill": 2}]},
    "freeform_svg": {
        "type": "freeform_svg", "title": "Aufbau einer Pflanze",
        "alt": "Einfache Pflanze mit beschrifteter Blüte, Blatt, Stängel und Wurzel.",
        "svg": ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 520 300">'
                '<rect x="0" y="0" width="520" height="300" rx="12" fill="#FFFFFF"/>'
                '<line x1="260" y1="90" x2="260" y2="230" stroke="#009E73" stroke-width="6"/>'
                '<circle cx="260" cy="70" r="26" fill="#E69F00"/><circle cx="260" cy="70" r="10" fill="#D55E00"/>'
                '<ellipse cx="300" cy="160" rx="34" ry="14" fill="#009E73"/>'
                '<path d="M260,230 L235,275 M260,230 L260,285 M260,230 L285,275" stroke="#5B6472" stroke-width="3"/>'
                '<text x="330" y="70" font-size="15" font-family="Helvetica, Arial, sans-serif" fill="#1F2933">Blüte</text>'
                '<text x="345" y="165" font-size="15" font-family="Helvetica, Arial, sans-serif" fill="#1F2933">Blatt</text>'
                '<text x="180" y="190" font-size="15" font-family="Helvetica, Arial, sans-serif" fill="#1F2933">Stängel</text>'
                '<text x="300" y="285" font-size="15" font-family="Helvetica, Arial, sans-serif" fill="#1F2933">Wurzel</text>'
                '</svg>')},
}
