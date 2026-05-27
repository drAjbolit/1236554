AI Chatter Browser Extension 4.0.4 - event id dedupe for mock provider

Changes:
- Exact duplicate provider.job.run event.id is ignored: no second accepted/already_processing reply.
- Tracks processedEventIds in service worker memory, capped at 10000 IDs.
- Status popup reports processedEventIds count.

This version keeps the mock provider only. No real provider page worker is enabled yet.
