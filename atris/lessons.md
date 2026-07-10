# lessons.md — What We Learned

> Append-only. One line per lesson. Harvested by validator after every feature.

---
- [2026-07-10] SCHEMA indexes that reference optional columns must run after migrations, not in the same CREATE script — otherwise legacy DBs never open.
- [2026-07-10] Tower is read-only; it cannot migrate. It must SELECT-NULL missing columns so operators can still see the board while serve heals the schema.

