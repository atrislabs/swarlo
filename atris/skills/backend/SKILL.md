---
name: backend
description: Backend policy for Swarlo. Keep APIs, storage, and protocol changes simple and testable.
version: 1.0.0
---

# Backend Skill

Swarlo is a small protocol repo. Treat it that way.

## Default

- Reuse existing code paths.
- Prefer direct functions over new abstractions.
- Keep storage logic obvious.
- Keep HTTP routes boring and consistent.

## Before shipping

- Can you explain the change in one sentence?
- Is there a focused test for it?
- Did you keep the protocol shape stable?
- Did you avoid adding product-specific baggage to the core?
