from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from scanners.insider.parser import (
    _HAS_LXML,
    find_ownership_xml_filename,
    parse_master_index,
    parse_ownership_xml,
)


@unittest.skipUnless(_HAS_LXML, "lxml is required for SGML-tolerant parsing tests")
class LxmlRecoveryTests(unittest.TestCase):
    """Regression tests for the lxml-with-recover parser path.

    SEC Form 4 filings routinely carry SGML constructs that strict
    ``xml.etree.ElementTree.fromstring`` rejects: bare ampersands inside
    element text (``Brown & Co``), entity references that aren't part of
    the standard XML set, and stray control characters. ``lxml`` with
    ``recover=True`` tolerates these and surfaces only fatally broken
    XML as ``xml.etree.ElementTree.ParseError``.

    Without the lxml-with-recover swap, the production funnel logs
    ~15 ``ParseError`` warnings per refresh and silently skips the
    affected filings (buys, sells, and grants all lost in the noise).
    """

    def _build_ownership_xml(self, security_title_value: str) -> str:
        return (
            '<?xml version="1.0"?>'
            '<ownershipDocument>'
            '  <issuer>'
            '    <issuerCik>1001</issuerCik>'
            '    <issuerTradingSymbol>SGML</issuerTradingSymbol>'
            '  </issuer>'
            '  <reportingOwner>'
            '    <reportingOwnerId>'
            '      <rptOwnerCik>2002</rptOwnerCik>'
            '      <rptOwnerName>John Smith</rptOwnerName>'
            '    </reportingOwnerId>'
            '    <reportingOwnerRelationship>'
            '      <isDirector>0</isDirector>'
            '      <isOfficer>1</isOfficer>'
            '      <isTenPercentOwner>0</isTenPercentOwner>'
            '      <officerTitle>Chief Financial Officer</officerTitle>'
            '    </reportingOwnerRelationship>'
            '  </reportingOwner>'
            '  <periodOfReport>2026-06-20</periodOfReport>'
            '  <nonDerivativeTable>'
            '    <nonDerivativeTransaction>'
            f'      <securityTitle><value>{security_title_value}</value></securityTitle>'
            '      <transactionDate><value>2026-06-20</value></transactionDate>'
            '      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>'
            '      <transactionAmounts>'
            '        <transactionShares><value>500</value></transactionShares>'
            '        <transactionPricePerShare><value>10.0</value></transactionPricePerShare>'
            '        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>'
            '      </transactionAmounts>'
            '      <postTransactionAmounts>'
            '        <sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction>'
            '      </postTransactionAmounts>'
            '      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>'
            '    </nonDerivativeTransaction>'
            '  </nonDerivativeTable>'
            '</ownershipDocument>'
        )

    def test_bare_ampersand_in_element_text_recovers(self) -> None:
        # ``Brown & Co`` is a textbook SEC SGML quirk: a literal ampersand
        # in element text where strict XML requires ``Brown &amp; Co``.
        # stdlib ET.fromstring raises ``ParseError`` here; with
        # ``recover=True`` lxml keeps the surrounding elements intact and
        # we can still extract owner + transaction metadata. The bare
        # ``&`` is replaced with a single whitespace character during
        # recovery (canonical lxml behaviour with
        # ``resolve_entities=False``). The exact whitespace substitution
        # can drift between patch versions of the same lxml major, so we
        # assert the structural contract (start, end, no ``&``, and the
        # four tokens) rather than the exact recovered string.
        xml = self._build_ownership_xml("Brown & Co Common Stock")

        # Explicit no-raise contract: the whole point of the lxml swap
        # is that malformed SEC Form 4 filings do not crash the parser.
        # A future regression that swallows ``ParseError`` would still
        # leave downstream fields empty, so we surface the failure mode
        # explicitly with a clear diagnostic.
        try:
            filing = parse_ownership_xml(xml, accession="0001")
        except ET.ParseError as exc:
            self.fail(
                "lxml recover=True should have absorbed the bare "
                f"ampersand without raising, but got: {exc!r}"
            )

        title = filing.transactions[0].security_title
        # The structural contract alone is sufficient: any correct
        # recovery of the four tokens (whether via whitespace
        # substitution, escaping, or splitting) yields this exact list.
        self.assertEqual(title.split(), ["Brown", "Co", "Common", "Stock"])
        self.assertEqual(filing.reporting_owners[0].name, "John Smith")
        self.assertEqual(filing.transactions[0].transaction_code, "P")
        self.assertEqual(filing.transactions[0].shares, 500.0)

    def test_unrecoverable_xml_raises_parse_error(self) -> None:
        # Defence-in-depth: if the filing body is genuinely unparseable
        # (e.g. blank body returned by an unloadable SEC mirror, or a
        # plain-text 404 page from a misrouted URL), ``_parse_root`` must
        # surface it as ``xml.etree.ElementTree.ParseError`` rather than
        # silently returning an empty tree that produces empty filings
        # downstream. The body deliberately has no XML markup at all so
        # it is unambiguously unrecoverable across every parser
        # implementation: lxml's ``recover=True`` cannot rescue a body
        # that contains no start tag.
        body = "not-xml-at-all"

        with self.assertRaises(ET.ParseError):
            parse_ownership_xml(body, accession="0004")

    def test_well_formed_xml_still_parses_with_lxml_path(self) -> None:
        # Belt-and-braces: confirm the lxml fallback did not regress the
        # happy path. Stdlib ET handles this fine; lxml must produce the
        # same observable field values.
        xml = self._build_ownership_xml("Common Stock")

        filing = parse_ownership_xml(xml, accession="0002")

        self.assertEqual(filing.transactions[0].security_title, "Common Stock")
        self.assertEqual(filing.transactions[0].price_per_share, 10.0)
        # ``parser.py`` runs ``<periodOfReport>`` through
        # ``datetime.fromisoformat(...).isoformat()``, which expands a
        # date-only ISO string to ``2026-06-20T00:00:00``. We assert
        # the post-normalisation form so the contract with the parser
        # is explicit.
        self.assertEqual(filing.acceptance_datetime, "2026-06-20T00:00:00")


class InsiderParserTests(unittest.TestCase):
    def test_parse_master_index_retains_form4_rows(self) -> None:
        text = "\n".join(
            [
                "Description:",
                "--------------------------------------------------------------------------------",
                "0000001|Example Co|4|2026-06-25|edgar/data/1/0000001-26-000001.txt",
            ]
        )

        rows = parse_master_index(text)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].form_type, "4")

    def test_find_ownership_xml_filename_prefers_ownership_document(self) -> None:
        filing_text = """
<DOCUMENT>
<TYPE>XML</TYPE>
<FILENAME>ownership.xml</FILENAME>
</DOCUMENT>
"""

        self.assertEqual(find_ownership_xml_filename(filing_text), "ownership.xml")

    def test_parse_ownership_xml_extracts_owner_and_purchase(self) -> None:
        xml = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerCik>1001</issuerCik>
    <issuerTradingSymbol>TEAM</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>2002</rptOwnerCik>
      <rptOwnerName>Jane Doe</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-06-20</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>42.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>11000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

        filing = parse_ownership_xml(xml, accession="0001")

        self.assertEqual(filing.issuer_ticker, "TEAM")
        self.assertEqual(filing.reporting_owners[0].name, "Jane Doe")
        self.assertEqual(filing.transactions[0].transaction_code, "P")


class WrapperDrillingTests(unittest.TestCase):
    """Regression tests for ``_parse_root`` drilling past the ``<SEC-DOCUMENT>`` wrapper.

    Production funnel log surfaced the exact substring::

        ParseError("unexpected root tag 'SEC-DOCUMENT' (expected <ownershipDocument>);
                    input first 80 chars: '<SEC-DOCUMENT>0001207407-26-000010.txt ...'")

    The provider layer's ``find_ownership_xml_filename`` returns ``None`` for
    Case A (wrapper already contains ``<ownershipDocument>``); the
    ``scanners/insider/official.py`` path then falls back to passing the raw
    index text to the parser. Pre the round-10 drill-past-wrapper helper,
    ``_parse_root``'s root-tag check rejected the SGML envelope as
    ``unexpected root tag 'SEC-DOCUMENT'`` and this filing landed in the
    WARNING-noise bin instead of being parsed. The fix lives in parser.py,
    not in the provider, so this test suite covers all four edge cases
    locally.
    """

    _OWNERSHIP_BODY = (
        '<?xml version="1.0"?>'
        '<ownershipDocument>'
        '  <issuer>'
        '    <issuerCik>1001</issuerCik>'
        '    <issuerTradingSymbol>WRAP</issuerTradingSymbol>'
        '  </issuer>'
        '  <reportingOwner>'
        '    <reportingOwnerId>'
        '      <rptOwnerCik>2002</rptOwnerCik>'
        '      <rptOwnerName>Wrapper Tester</rptOwnerName>'
        '    </reportingOwnerId>'
        '    <reportingOwnerRelationship>'
        '      <isDirector>0</isDirector>'
        '      <isOfficer>1</isOfficer>'
        '      <isTenPercentOwner>0</isTenPercentOwner>'
        '      <officerTitle>Chief Executive Officer</officerTitle>'
        '    </reportingOwnerRelationship>'
        '  </reportingOwner>'
        '  <periodOfReport>2026-06-23</periodOfReport>'
        '  <nonDerivativeTable>'
        '    <nonDerivativeTransaction>'
        '      <securityTitle><value>Common Stock</value></securityTitle>'
        '      <transactionDate><value>2026-06-23</value></transactionDate>'
        '      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>'
        '      <transactionAmounts>'
        '        <transactionShares><value>1500</value></transactionShares>'
        '        <transactionPricePerShare><value>9.5</value></transactionPricePerShare>'
        '        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>'
        '      </transactionAmounts>'
        '      <postTransactionAmounts>'
        '        <sharesOwnedFollowingTransaction><value>15000</value></sharesOwnedFollowingTransaction>'
        '      </postTransactionAmounts>'
        '      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>'
        '    </nonDerivativeTransaction>'
        '  </nonDerivativeTable>'
        '</ownershipDocument>'
    )

    def _build_submission_wrapper(
        self,
        *,
        body: str | None = None,
        leading_whitespace: bool = False,
        trailing_after_close: str = "\n<post-filing-footer>cleanup</post-filing-footer>\n",
        truncate_closing_tag: bool = False,
    ) -> str:
        # Verbatim shape of an EDGAR submission index for accession
        # ``0001207407-26-000010`` -- the EXACT prefix the production log
        # showed (``<SEC-DOCUMENT>0001207407-26-000010.txt : 20260623\n
        # <SEC-HEADER>``). The helper must recognise this prefix and
        # drill past it to the inner ``<ownershipDocument>``.
        body_xml = body if body is not None else self._OWNERSHIP_BODY
        closing_tag = "</ownershipDocument>"
        if truncate_closing_tag:
            body_xml = body_xml[: body_xml.rfind(closing_tag)]
        prefix = (
            "<SEC-DOCUMENT>0001207407-26-000010.txt : 20260623\n"
            "<SEC-HEADER>0001207407-26-000010-index.htm\n"
            "<ACCEPTANCE-DATETIME>20260623120000\n"
            "<FILENAME>primary_doc.xml\n"
        )
        text = prefix + body_xml + (closing_tag if not truncate_closing_tag else "") + trailing_after_close
        return ("  \n" + text) if leading_whitespace else text

    def test_sec_submission_wrapper_is_drilled_past(self) -> None:
        # The exact production failure mode: provider hands the raw
        # index text (with ``<SEC-DOCUMENT>`` envelope intact) to the
        # parser. Pre-fix, ``_parse_root`` raised
        # ``unexpected root tag 'SEC-DOCUMENT' (expected <ownershipDocument>)``.
        # Post-fix, the drill helper isolates the inner XML and the
        # parser produces a fully-populated ``ParsedOwnershipFiling``.
        wrapped = self._build_submission_wrapper()

        filing = parse_ownership_xml(wrapped, accession="0001207407-26-000010")

        self.assertEqual(filing.issuer_ticker, "WRAP")
        self.assertEqual(filing.reporting_owners[0].name, "Wrapper Tester")
        self.assertEqual(filing.transactions[0].transaction_code, "P")
        self.assertEqual(filing.transactions[0].shares, 1500.0)
        self.assertEqual(filing.transactions[0].price_per_share, 9.5)
        # The is_officer branch on ``reportingOwnerRelationship`` reached
        # proves we descended *into* the inner XML, not just stopped at
        # the wrapper.
        self.assertTrue(filing.reporting_owners[0].is_officer)

    def test_inner_ownership_xml_unchanged_when_no_wrapper(self) -> None:
        # Belt-and-braces: drill must be a no-op for the happy path
        # where the parser already receives a bare ``<ownershipDocument>``
        # body (the ``HTTPSubmissionProvider`` case). Same observable
        # fields as the wrapper case proves the helper just stripped the
        # wrapper rather than accidentally mutating the inner XML.
        filing = parse_ownership_xml(self._OWNERSHIP_BODY, accession="0003")

        self.assertEqual(filing.issuer_ticker, "WRAP")
        self.assertEqual(filing.reporting_owners[0].name, "Wrapper Tester")
        self.assertEqual(filing.transactions[0].shares, 1500.0)

    def test_leading_whitespace_before_sec_document_still_triggers_drill(self) -> None:
        # Defensive edge case: SEC occasionally serves a leading BOM or
        # CRLF sequence before the SGML opener (Fed-filings ingestions
        # via FRED-style intermediaries have been observed doing this).
        # ``lstrip().startswith("<SEC-DOCUMENT")`` is the prefix check,
        # but the start-of-XML search uses the *original* ``xml_text``
        # to keep the index. We assert the wrapper is still drilled
        # past and the inner ownership XML survives.
        wrapped = self._build_submission_wrapper(leading_whitespace=True)

        filing = parse_ownership_xml(wrapped, accession="0004")

        self.assertEqual(filing.issuer_ticker, "WRAP")
        self.assertEqual(filing.reporting_owners[0].name, "Wrapper Tester")
        self.assertEqual(filing.transactions[0].shares, 1500.0)

    def test_truncated_wrapper_with_no_closing_tag_routes_to_parse_error(self) -> None:
        # Defence-in-depth: if the wrapper contains ``<SEC-DOCUMENT>``
        # *and* ``<ownershipDocument>`` but is truncated before
        # ``</ownershipDocument>`` (think: a 502 Bad Gateway mid-stream
        # that dropped the last 200 bytes of the body), the helper must
        # NOT silently emit a half-truncated ownership XML saying
        # ``transaction_shares = 0``. The contract is to surface the
        # existing ParseError so the engine-level catch records the
        # filing as a known failure rather than a successful parse that
        # produced an empty filing. Critical for not corrupting the
        # downstream ingest pipeline with zero-share rows.
        truncated = self._build_submission_wrapper(truncate_closing_tag=True)

        with self.assertRaises(ET.ParseError):
            parse_ownership_xml(truncated, accession="0005")


if __name__ == "__main__":
    unittest.main()
