from __future__ import annotations

import unittest

from scanners.insider.parser import find_ownership_xml_filename, parse_master_index, parse_ownership_xml


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
