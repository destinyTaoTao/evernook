# Service-bound embedding configuration (#193)

- Parse a service's trusted embedding configuration once into a backend and canonical configuration identity. Preserve startup with missing/invalid optional config when no semantic maintenance or reflection is requested; never silently accept later repairs without restart.
- Revalidate the current configuration before foreground semantic, topic and context use and background semantic/topic processing; reject changed settings with a fixed error. Keep unrelated capture and scheduling working. Existing standalone SDK configuration behavior remains unchanged.
- Report restart-required separately from format validity without paths or secrets. Test actual service change/restart, invalid and repaired config, concurrent mutation, background maintenance, same-space compatibility and installed artifacts; do not admit a new model or alter frozen scoring.
