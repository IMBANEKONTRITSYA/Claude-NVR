# The upstream image is scratch-based and cannot execute CMD-SHELL healthchecks.
# Keep the pinned server binary, with BusyBox wget for a real Control API probe.
FROM bluenviron/mediamtx:1.16.1 AS upstream
FROM alpine:3.22
COPY --from=upstream /mediamtx /mediamtx
ENTRYPOINT ["/mediamtx"]
