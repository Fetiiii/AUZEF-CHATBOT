"""Dependency-free input limits shared by the chat route and the pilot preflight.

Kept out of ``core.deps`` on purpose: importing ``core.deps`` builds the search
providers, and the preflight must be able to read this value without that.
"""

# Widget chat message limit (characters). The widget's ``#w-input`` textarea
# carries the same value as ``maxlength``; a static test keeps the two equal.
#
# 500 is the internal-pilot input policy: a ~500-character message needs at
# most ~450-525 Intent Analyzer output tokens, which fits the pilot's
# max_tokens=600 budget without truncation
# (outputs/performance-readiness/PILOT_CANDIDATE_VALIDATION.md §2).
CHAT_MESSAGE_MAX_CHARS = 500
