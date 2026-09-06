-- ═══════════════════════════════════════════════════════════════════════════
-- 003 — 'alert' as a note and bookmark target
-- ═══════════════════════════════════════════════════════════════════════════
-- `notes` and `bookmarks` are polymorphic: a target_type plus a bare target_id
-- with no foreign key, because one notes table serving seven kinds of object is
-- worth more to an operator than seven near-identical tables. The CHECK is
-- therefore the only thing standing between a typo'd target_type and a note that
-- is written successfully and can never be read back.
--
-- It was missing 'alert', while three call sites already assumed it was there:
-- schemas.TargetType lists it, routers/alerts.py:132 inserts a note against it
-- when an operator confirms an alert with a comment, and routers/alerts.py:174
-- reads notes back with exactly that filter. The result was a 500 on the single
-- most important action in the alert queue — confirming a watchlist hit and
-- saying why — and a read path that could only ever return an empty list.
--
-- Widened rather than narrowed, and the reason is in this router's own docstring:
-- rule-typed alerts are deliberately *not* mirrored into `events`, so for those
-- there is no event row to hang the note on instead. Sending alert notes to the
-- mirrored event would work for watchlist hits and silently drop the note for
-- every stopped-vehicle anomaly.
--
-- Same DROP/ADD form as the alerts_alert_type_check widening in 002, so this file
-- can be re-applied without erroring.

ALTER TABLE notes DROP CONSTRAINT IF EXISTS notes_target_type_check;
ALTER TABLE notes ADD CONSTRAINT notes_target_type_check CHECK (
    target_type IN ('detection','event','incident','camera','track','pursuit','alert')
);

-- Bookmarks share the vocabulary through schemas.TargetType, so leaving them
-- behind would just move the same 500 to "star this alert for the handover".
ALTER TABLE bookmarks DROP CONSTRAINT IF EXISTS bookmarks_target_type_check;
ALTER TABLE bookmarks ADD CONSTRAINT bookmarks_target_type_check CHECK (
    target_type IN ('detection','event','incident','camera','track','pursuit','alert')
);
