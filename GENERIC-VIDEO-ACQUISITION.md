# Generic video acquisition

The runtime supports the explicit `generic-video-transcript` job for public,
non-YouTube video pages. The current resolver supports Vidalytics landing pages
and settings payloads. It does not bypass login, CAPTCHA, paywalls, robots
controls, or provider terms; operators must have permission to acquire and
transcribe the media.

The workflow is deliberately staged:

1. Resolve the landing page to a typed MP4/HLS candidate.
2. Enforce HTTPS, redirect, host, byte, file-count, and duration limits while
   downloading/materializing media and extracting audio.
3. Invoke the configured local Whisper command with timeout and output limits.
4. Persist a JSON provenance artifact and a plain-text transcript.

Enable it only through the privileged command boundary by adding
`generic-video-transcript` to the explicit acquisition allowlist. Disabled mode
and an allowlist that omits the job leave the original message unchanged.

The checked-in fixtures under `channel_runtime/fixtures/generic_video/` are
replayable and do not require live Scalpius/Vidalytics availability. A live
rehearsal should be treated as optional and only run when the URL is reachable
and acquisition is authorized. Provider or media failures are returned as
typed, operator-safe error codes; they do not silently fall back to arbitrary
URL fetching.

For troubleshooting, check the helper's structured stderr error (`code` and
bounded `detail`), then verify the configured `ffmpeg` and `whisper` binaries,
the media host allowlist, and the resource limits. Do not put tokens or full
provider responses into logs.
