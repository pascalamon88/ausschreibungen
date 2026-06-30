-- BidOS Migration 008 — Quelle "bund" (oeffentlichevergabe.de / Datenservice Öffentlicher Einkauf)
-- Im Supabase SQL Editor ausführen. Fügt den Enum-Wert 'bund' hinzu, damit nationale
-- Bund-Vergabedaten als 2. Radar-Quelle neben TED gespeichert werden können.

alter type tender_source add value if not exists 'bund';

-- Prüfung:
-- select unnest(enum_range(null::tender_source));
