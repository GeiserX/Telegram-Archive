"""The viewer's location, venue, live location and contact cards.

The helpers are EXECUTED under node, lifted verbatim from the template with the
real vendored moment (docs/design/location-and-contact.md, "Viewer cards").
Demo coordinates, names and numbers only.
"""

import json
import re
import unittest

from test_deleted_messages_frontend import _MOMENT
from test_frontend_bootstrap import INDEX_HTML, NODE, _run_setup_program

from telegram_archive.message_utils import METADATA_ONLY_MEDIA_TYPES

_PRELUDE = (
    _MOMENT
    + """
const viewerTimezone = { value: 'UTC' }
const _senderInitialsCache = new Map()
"""
)

_DECLARATIONS = (
    "const PAYLOAD_CARD_KINDS = [",
    "const payloadCardKind = (msg) =>",
    "const METADATA_ONLY_TYPES = new Set([",
    "const isMetadataOnlyMedia = (msg) =>",
    "const cardPoint = (data) =>",
    "const openStreetMapUrl = (point) =>",
    "const formatCardStamp = (iso, nowMs = Date.now()) =>",
    "const LIVE_LOCATION_FOREVER = ",
    "const locationCard = (msg, nowMs = Date.now()) =>",
    "const contactCard = (msg) =>",
    "const getPeerIndex = (id) =>",
    "const MEDIA_TYPE_WORDS = {",
    "const pinnedKindWord = (msg) =>",
    "const replyMediaLabels = {",
    "const replyToSnippet = (msg) =>",
    "const EXTENDED_MEDIA_CHIP_META = {",
    "const currentPoll = (msg) =>",
    "const getExtendedMediaChip = (msg) =>",
)

SENT = "2026-03-01T12:00:00"


def _ms(iso: str) -> str:
    return f"Date.parse({json.dumps(iso + 'Z')})"


@unittest.skipUnless(NODE, "node is required to run the card helpers")
class TestCards(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, expression: str):
        return _run_setup_program(self.html, _DECLARATIONS, _PRELUDE, f"console.log(JSON.stringify({expression}))")

    def _location(self, msg: dict, now: str = "2026-03-01T12:10:00"):
        return self._run(f"locationCard({json.dumps(msg)}, {_ms(now)})")

    # --- location ---------------------------------------------------------

    def test_a_location_card_from_its_payload(self):
        card = self._location(
            {"date": SENT, "raw_data": {"geo": {"lat": 40.416775, "long": -3.70379, "accuracy_radius": 25}}}
        )
        self.assertEqual(card["title"], "Location")
        self.assertEqual([line["text"] for line in card["lines"]], ["40.416775, -3.703790", "± 25 m"])
        self.assertEqual(
            card["url"],
            "https://www.openstreetmap.org/?mlat=40.416775&mlon=-3.703790#map=16/40.416775/-3.703790",
        )
        self.assertEqual(card["coordinates"], "40.416775, -3.703790")
        self.assertEqual(card["label"], "Location. 40.416775, -3.703790. ± 25 m")

    def test_the_payload_wins_over_a_row_with_no_media(self):
        """The listener writes no media row: the card is keyed on raw_data first."""
        card = self._location({"date": SENT, "media": None, "raw_data": {"geo": {"lat": 1, "long": 2}}})
        self.assertEqual(card["kind"], "geo")
        self.assertTrue(card["url"])

    def test_a_row_with_only_the_type_says_details_not_archived(self):
        card = self._location({"date": SENT, "media": {"type": "geo", "file_path": "1/2.bin"}, "raw_data": {}})
        self.assertEqual(card["lines"], [{"text": "Details not archived"}])
        self.assertIsNone(card["url"])
        self.assertIsNone(card["point"])

    def test_bad_coordinates_give_location_unavailable_and_no_link(self):
        for data in (
            {},
            {"lat": "40.4", "long": "-3.7"},
            {"lat": 91, "long": 0},
            {"lat": 0, "long": -181},
            {"lat": None, "long": 2},
            {"lat": "javascript:alert(1)", "long": 2},
        ):
            with self.subTest(data=data):
                card = self._location({"date": SENT, "raw_data": {"geo": data}})
                self.assertIsNone(card["url"])
                self.assertEqual(card["lines"], [{"text": "Location unavailable"}])

    def test_the_url_is_built_from_numbers_only(self):
        url = self._run("openStreetMapUrl(cardPoint({ lat: -33.8688, long: 151.2093 }))")
        self.assertEqual(
            url, "https://www.openstreetmap.org/?mlat=-33.868800&mlon=151.209300#map=16/-33.868800/151.209300"
        )
        self.assertIsNone(self._run("openStreetMapUrl(cardPoint({ lat: NaN, long: 1 }))"))

    def test_a_venue_shows_its_title_and_address(self):
        card = self._location(
            {
                "date": SENT,
                "media": {"type": "venue"},
                "raw_data": {"venue": {"title": "Demo Cafe", "address": "1 Example Street", "lat": 1.0, "long": 2.0}},
            }
        )
        self.assertEqual(card["title"], "Demo Cafe")
        self.assertEqual(card["lines"], [{"text": "1 Example Street", "address": True}])
        self.assertTrue(card["url"].startswith("https://www.openstreetmap.org/?mlat=1.000000&mlon=2.000000"))
        self.assertEqual(card["label"], "Demo Cafe. 1 Example Street")

    def test_a_venue_with_no_address_shows_its_coordinates(self):
        card = self._location(
            {
                "date": SENT,
                "media": {"type": "venue"},
                "raw_data": {"venue": {"title": "Demo Cafe", "lat": 1, "long": 2}},
            }
        )
        self.assertEqual(card["title"], "Demo Cafe")
        self.assertEqual(card["lines"], [{"text": "1.000000, 2.000000"}])
        self.assertTrue(card["url"].startswith("https://www.openstreetmap.org/?mlat=1.000000&mlon=2.000000"))

    def test_a_venue_with_no_point_says_location_unavailable(self):
        card = self._location(
            {
                "date": SENT,
                "media": {"type": "venue"},
                "raw_data": {"venue": {"title": "Demo Cafe", "address": "1 Example Street"}},
            }
        )
        self.assertEqual(
            card["lines"], [{"text": "1 Example Street", "address": True}, {"text": "Location unavailable"}]
        )
        self.assertIsNone(card["url"])
        self.assertIsNone(card["point"])

    # --- live location ----------------------------------------------------

    def _live(self, now: str, **extra):
        payload = {"lat": 1.0, "long": 2.0, "period": 900, "at": "2026-03-01T12:14:00", **extra}
        card = self._location({"date": SENT, "raw_data": {"geo_live": payload}}, now)
        return [line["text"] for line in card["lines"]]

    def test_a_live_share_still_ahead_says_until_when(self):
        self.assertEqual(
            self._live("2026-03-01T12:10:00"),
            ["Last position, updated today at 12:14", "Sharing until today at 12:15"],
        )

    def test_an_ended_share_says_only_when_it_was_last_seen(self):
        self.assertEqual(self._live("2026-03-02T08:00:00"), ["Last position, updated yesterday at 12:14"])
        self.assertEqual(self._live("2026-04-20T08:00:00"), ["Last position, updated 1 Mar 2026 at 12:14"])

    def test_a_share_until_turned_off(self):
        self.assertEqual(
            self._live("2026-04-20T08:00:00", period=2147483647),
            ["Last position, updated 1 Mar 2026 at 12:14", "Sharing until turned off"],
        )

    def test_a_live_card_never_says_live_now(self):
        for now in ("2026-03-01T12:10:00", "2026-03-02T08:00:00"):
            for line in self._live(now):
                self.assertNotIn("live", line.lower())

    def test_a_stopped_share_with_no_point_says_location_unavailable(self):
        """A stopped share can come back as GeoPointEmpty: no lat, no long."""
        msg = {"date": SENT, "raw_data": {"geo_live": {"period": 900, "at": "2026-03-01T12:14:00"}}}
        for now, lines in (
            ("2026-03-01T12:10:00", ["Location unavailable", "Sharing until today at 12:15"]),
            ("2026-03-02T08:00:00", ["Location unavailable"]),
        ):
            with self.subTest(now=now):
                card = self._location(msg, now)
                self.assertEqual([line["text"] for line in card["lines"]], lines)
                self.assertIsNone(card["url"])
                # No point, so no copy button (v-if="card.point").
                self.assertIsNone(card["point"])

    def test_a_live_location_beats_the_plain_kinds(self):
        card = self._location(
            {"date": SENT, "raw_data": {"geo_live": {"lat": 1, "long": 2}, "geo": {"lat": 3, "long": 4}}}
        )
        self.assertEqual(card["kind"], "geo_live")
        self.assertEqual(card["title"], "Live location")

    # --- contact ----------------------------------------------------------

    def _contact(self, data):
        return self._run(f"contactCard({json.dumps({'raw_data': {'contact': data}})})")

    def test_a_contact_card_with_name_and_phone(self):
        card = self._contact({"first_name": "Alex", "last_name": "Demo", "phone_number": "15555550100", "user_id": 0})
        self.assertEqual(card["title"], "Alex Demo")
        self.assertEqual(card["line"], "+15555550100")
        self.assertEqual(card["phone"], "+15555550100")
        self.assertEqual(card["tel"], "tel:+15555550100")
        self.assertEqual(card["initials"], "AD")
        self.assertRegex(card["fill"], r"^var\(--tg-avatar-[0-6]\)$")

    def test_the_tel_link_keeps_only_plus_and_digits(self):
        card = self._contact({"first_name": "Alex", "phone_number": '+1 (555) 555-0100 ext"><script>'})
        self.assertEqual(card["tel"], "tel:+15555550100")
        self.assertEqual(card["phone"], '+1 (555) 555-0100 ext"><script>')

    def test_a_contact_falls_back_to_the_phone_then_to_contact(self):
        no_name = self._contact({"first_name": "", "last_name": "", "phone_number": "+1 555 0100"})
        self.assertEqual((no_name["title"], no_name["line"]), ("+1 555 0100", ""))
        nothing = self._contact({"first_name": "", "last_name": "", "phone_number": ""})
        self.assertEqual((nothing["title"], nothing["line"], nothing["tel"]), ("Contact", "Unknown number", None))
        self.assertEqual(nothing["phone"], "")

    def test_a_contact_row_with_only_the_type(self):
        card = self._run("contactCard({ media: { type: 'contact', file_path: '1/2.bin' }, raw_data: {} })")
        self.assertEqual((card["title"], card["line"], card["tel"]), ("Contact", "Details not archived", None))

    def test_other_messages_get_no_card(self):
        self.assertEqual(
            self._run(
                "[locationCard({ media: { type: 'photo' }, raw_data: {} }), contactCard({ raw_data: { poll: {} } }),"
                " locationCard({ raw_data: { contact: {} } }), contactCard({ raw_data: { geo: {} } })]"
            ),
            [None, None, None, None],
        )

    # --- words ------------------------------------------------------------

    def test_the_pinned_bar_names_the_card(self):
        out = self._run(
            "[{ raw_data: { contact: {} } }, { media: { type: 'venue' }, raw_data: {} }, { raw_data: { geo_live: {} } },"
            " { media: { type: 'photo' } }, { media: { type: 'dice' } }, {}].map(pinnedKindWord)"
        )
        self.assertEqual(out, ["Contact", "Location", "Live location", "Photo", "Media", "Message"])

    def test_a_reply_names_the_card(self):
        out = self._run(
            "[{ reply_to_media_type: 'geo' }, { reply_to_media_type: 'venue', reply_to_media_title: 'Demo Cafe' },"
            " { reply_to_media_type: 'venue' }, { reply_to_media_type: 'geo_live' }, { reply_to_media_type: 'contact' }]"
            ".map(replyToSnippet)"
        )
        self.assertEqual(out, ["Location", "Location, Demo Cafe", "Location", "Live location", "Contact"])

    def test_a_poll_with_no_details_says_so(self):
        """A poll row an earlier import left without raw_data.poll draws a chip, not an empty bubble."""
        out = self._run(
            "[getExtendedMediaChip({ media: { type: 'poll', file_path: '1/2.bin' }, raw_data: {} }),"
            " getExtendedMediaChip({ media: { type: 'poll' }, raw_data: { poll: { question: 'Where next?' } } }),"
            " pinnedKindWord({ media: { type: 'poll' }, raw_data: {} }),"
            " replyToSnippet({ reply_to_media_type: 'poll' })]"
        )
        self.assertEqual(out[0]["label"], "Poll")
        self.assertEqual(out[0]["detail"], "Details not archived")
        # A poll with its details draws the poll block instead.
        self.assertIsNone(out[1])
        self.assertEqual(out[2:], ["Poll", "Poll"])

    def test_the_metadata_only_list_mirrors_the_backend(self):
        js = self._run("[...METADATA_ONLY_TYPES].sort()")
        self.assertEqual(js, sorted(METADATA_ONLY_MEDIA_TYPES))


@unittest.skipUnless(NODE, "node is required to run the placeholder helper")
class TestNoFileForAMetadataOnlyRow(unittest.TestCase):
    """An old row with a .bin path draws its card, never a file or a "Not downloaded yet"."""

    def test_the_placeholder_steps_aside(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        prelude = """
const getExtendedMediaChip = () => null
const isLockedAudio = () => false
const mediaMissingReason = (msg) => (msg.mediaLoadFailed ? 'missing' : 'pending')
const MISSING_REASONS = { missing: 'missing from the archive disk', pending: 'not downloaded yet' }
const formatBytes = () => ''
const getMediaDisplayName = (media) => media.file_name
"""
        out = _run_setup_program(
            html,
            (
                "const METADATA_ONLY_TYPES = new Set([",
                "const MEDIA_TYPE_WORDS = {",
                "const mediaPlaceholder = (msg) =>",
            ),
            prelude,
            f"const types = {json.dumps(sorted(METADATA_ONLY_MEDIA_TYPES))}\n"
            "console.log(JSON.stringify({"
            " leftovers: types.flatMap(type => [true, false].map(failed => ["
            "   type, failed, mediaPlaceholder({ media: { type, file_path: '1/2.bin' }, mediaLoadFailed: failed })])),"
            " bare: types.map(type => [type, mediaPlaceholder({ media: { type } })]),"
            " document: mediaPlaceholder({ media: { type: 'document', file_name: 'a.pdf' } }),"
            "}))",
        )
        self.assertEqual(len(out["leftovers"]), 2 * len(METADATA_ONLY_MEDIA_TYPES))
        for media_type, failed, placeholder in out["leftovers"]:
            with self.subTest(type=media_type, load_failed=failed):
                self.assertIsNone(placeholder)
        for media_type, placeholder in out["bare"]:
            with self.subTest(type=media_type):
                self.assertIsNone(placeholder)
        # Positive control: a real file still gets its placeholder.
        self.assertEqual(out["document"]["shape"], "file")

    def test_the_media_block_never_opens_for_one(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn("!mediaUnavailable(msg) && !isMetadataOnlyMedia(msg)) || isFirstInAlbum(msg, index)", html)


class TestCardMarkupAndColours(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")
        start = cls.html.index('<template v-for="card in [locationCard(msg)]"')
        end = cls.html.index("<!-- Extended media chip", start)
        cls.markup = cls.html[start:end]

    def _rule(self, selector: str) -> str:
        """The rule for ``selector`` alone (not a shared rule that lists it with others)."""
        start = self.html.index(f"\n        {selector} {{") + 1
        while self.html[self.html.rindex("\n", 0, start - 1) + 1 : start - 1].rstrip().endswith(","):
            start = self.html.index(f"\n        {selector} {{", start) + 1
        return self.html[start : self.html.index("}", start)]

    def test_the_cards_sit_on_the_quote_tint(self):
        """bg-tg-quote-bg, so a deleted bubble paints it over the plain fill (test_deleted_boxes_skip_the_wash)."""
        self.assertIn('class="geo-card bg-tg-quote-bg mb-1.5"', self.markup)
        self.assertIn('class="contact-card bg-tg-quote-bg mb-1.5"', self.markup)

    def test_text_uses_only_the_pairs_measured_at_4_5_to_1(self):
        """Titles keep the side's text colour, second lines the meta colour, controls the
        quote colour: text, meta and quote on the quote tint are the pairs
        test_bubble_text_pairs_read_on_both_sides measures in every palette."""
        self.assertIn("color: rgb(var(--tg-meta));", self._rule(".geo-card-meta"))
        self.assertIn("color: rgb(var(--tg-quote));", self._rule(".geo-card-action"))
        self.assertIn("color: inherit;", self._rule(".geo-card-main"))
        for selector in (".geo-card-title,\n        .contact-card-name", ".geo-card-address"):
            start = self.html.index(f"        {selector} {{")
            rule = self.html[start : self.html.index("}", start)]
            self.assertNotIn("color:", rule)
        # The initials are the avatar pair test_initials_clear_both_gradient_stops measures.
        self.assertIn("color: rgb(var(--tg-avatar-fg));", self._rule(".contact-card-disc"))

    def test_the_map_link_opens_a_new_tab_without_a_referrer(self):
        self.assertIn(":target=\"card.url ? '_blank' : null\"", self.markup)
        self.assertIn(":rel=\"card.url ? 'noopener noreferrer' : null\"", self.markup)

    def test_no_request_leaves_the_page_before_a_click(self):
        """No tiles and no third-party images: the only outside address is the link itself."""
        self.assertNotIn('<img src="http', self.markup)
        hosts = set(
            re.findall(
                r"https?://([^/'\"`]+)",
                self.html[self.html.index("const PAYLOAD_CARD_KINDS") : self.html.index("const pinnedKindWord")],
            )
        )
        self.assertEqual(hosts, {"www.openstreetmap.org"})
        self.assertNotIn("innerHTML", self.markup)

    def test_screen_readers_get_a_label_for_every_control(self):
        self.assertIn(':aria-label="card.url ? `${card.label}. Open on OpenStreetMap` : null"', self.markup)
        self.assertIn('aria-label="Copy coordinates"', self.markup)
        self.assertIn(':aria-label="`Call ${card.phone}`"', self.markup)
        self.assertIn('aria-label="Copy phone number"', self.markup)
        # The pin and the initials disc are decoration: the title and the name say it.
        self.assertIn('<span class="geo-card-pin" aria-hidden="true">', self.markup)
        self.assertIn(
            'class="contact-card-disc avatar-initials" :style="{ background: card.fill }" aria-hidden="true"',
            self.markup,
        )

    def test_copy_toasts_say_what_was_copied(self):
        self.assertIn("copyText(card.coordinates, 'Coordinates copied')", self.markup)
        self.assertIn("copyText(card.phone, 'Phone copied')", self.markup)
