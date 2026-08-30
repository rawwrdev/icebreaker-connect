"""Icebreaker Connect.

A standalone desktop onboarding application. It helps an account owner capture the
minimal Tinder session bundle from their own logged-in session (running in a local
rootable Android emulator) and deliver it to Icebreaker either over a secure HTTPS
pairing flow or as an explicit local JSON export.

Design rules enforced across this package:
  * The captured bundle stays in memory by default; JSON is written only on an
    explicit Save action, with owner-only permissions where supported.
  * Only the allowlisted session-identity fields are captured. Token *values* are
    never printed, logged, or included in progress events.
  * Every exit path clears the Android proxy and terminates capture processes.
  * No telemetry, no crash reporting, no traffic dumps.
"""

__version__ = "0.1.8"
