"""Who publishes OpenBot, and where support/donations go. Shown in the app and web page."""

import os

NAME = "OpenBot"
PUBLISHER = "Lighthouse Consulting"
PUBLISHER_URL = "https://www.lighthouseconsult.com/case-studies/openbot/"
REPO_URL = "https://github.com/dalerks/openbot"
HOMEPAGE_URL = "https://www.josephrounds.dev/openbot/"
LICENSE = "GPL-3.0-or-later"

SUGGESTED_DONATION_USD = 20
# A Stripe Payment Link ("customer chooses what to pay", preset to $20), e.g.
# "https://donate.stripe.com/xxxxxxxx". Empty until Lighthouse Consulting creates it;
# the OPENBOT_DONATE_URL environment variable overrides it (handy for testing).
DONATE_URL = ""


def donate_url():
    return os.environ.get("OPENBOT_DONATE_URL") or DONATE_URL or None


def public_info():
    return {"name": NAME, "publisher": PUBLISHER, "publisher_url": PUBLISHER_URL,
            "repo_url": REPO_URL, "homepage_url": HOMEPAGE_URL, "license": LICENSE, "donate_url": donate_url(),
            "suggested_donation_usd": SUGGESTED_DONATION_USD}
