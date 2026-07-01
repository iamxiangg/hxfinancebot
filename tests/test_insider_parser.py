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


if __name__ == "__main__":
    unittest.main()
