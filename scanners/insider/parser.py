from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


try:
    # lxml is already a workflow dependency (pip install ... lxml). We prefer it
    # over ``xml.etree.ElementTree`` because stdlib ET is strict and rejects SEC
    # SGML constructs that the Form 4 filings regularly carry: bare
    # ampersands (``&FOO`` not ``&amp;`` or ``&FOO;``), undeclared entities,
    # control characters in element text, etc. With ``recover=True``, lxml
    # tolerates those issues and still extracts the well-formed subtrees so
    # the calls below (``findall``, ``attrib.get``, ``text``) work unchanged.
    from lxml import etree as _LXML_ETREE  # noqa: F401  (presence-tested below)

    _HAS_LXML = True
except ImportError:  # pragma: no cover - kept for dev environments without lxml
    _LXML_ETREE = None  # type: ignore[assignment]
    _HAS_LXML = False


MASTER_INDEX_SEPARATOR = "--------------------------------------------------------------------------------"


@dataclass(frozen=True)
class MasterIndexEntry:
    cik: str
    company_name: str
    form_type: str
    date_filed: str
    archive_path: str


@dataclass(frozen=True)
class ReportingOwner:
    cik: str
    name: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    officer_title: str


@dataclass(frozen=True)
class NonDerivativeTransaction:
    security_title: str
    transaction_date: str
    transaction_code: str
    acquired_disposed: str
    shares: float
    price_per_share: float
    shares_owned_after: float | None
    direct_or_indirect: str
    footnotes: list[str]


@dataclass(frozen=True)
class ParsedOwnershipFiling:
    accession: str
    issuer_cik: str
    issuer_ticker: str
    acceptance_datetime: str
    reporting_owners: list[ReportingOwner]
    transactions: list[NonDerivativeTransaction]
    raw_footnotes: dict[str, str] = field(default_factory=dict)


def parse_master_index(text: str) -> list[MasterIndexEntry]:
    if MASTER_INDEX_SEPARATOR not in text:
        return []
    lines = text.splitlines()
    data_started = False
    entries: list[MasterIndexEntry] = []
    for line in lines:
        if not data_started:
            if line.strip() == MASTER_INDEX_SEPARATOR:
                data_started = True
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 5:
            continue
        entries.append(
            MasterIndexEntry(
                cik=parts[0],
                company_name=parts[1],
                form_type=parts[2],
                date_filed=parts[3],
                archive_path=parts[4],
            )
        )
    return entries


def find_ownership_xml_filename(filing_text: str) -> str | None:
    if "<ownershipDocument" in filing_text:
        return None

    pattern = re.compile(
        r"<DOCUMENT>.*?<TYPE>(?:4|4/A|XML).*?<FILENAME>([^<]+\.xml)</FILENAME>.*?</DOCUMENT>",
        re.IGNORECASE | re.DOTALL,
    )
    matches = pattern.findall(filing_text)
    for match in matches:
        if "ownership" in match.lower():
            return match
    return matches[0] if matches else None


def _text(node: Any) -> str:
    # Accepts both xml.etree.ElementTree.Element and lxml.etree._Element - the
    # public surface used here (.text) is identical across both libraries.
    return (node.text or "").strip() if node is not None and node.text else ""


def _float_text(node: ET.Element | None) -> float | None:
    text = _text(node)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_root(xml_text: str) -> Any:
    """Return an Element compatible with both ``xml.etree`` and ``lxml.etree``.

    Prefers ``lxml.etree.XMLParser(recover=True)`` which tolerates the SEC SGML
    patterns that stdlib ``xml.etree.ElementTree.fromstring`` rejects (bare
    ampersands, undeclared entity references, embedded control characters,
    etc.). On a fully unrecoverable doc, ``lxml.etree.XMLSyntaxError`` is
    re-raised as ``xml.etree.ElementTree.ParseError`` so the caller's
    ``except ET.ParseError`` keeps working unchanged.

    Falls back to stdlib only when ``lxml`` is missing (e.g. a partial dev
    environment); lxml is in the workflow dependency list already.
    """
    if _HAS_LXML:
        parser = _LXML_ETREE.XMLParser(recover=True, resolve_entities=False)
        try:
            root = _LXML_ETREE.fromstring(xml_text.encode("utf-8"), parser=parser)
        except _LXML_ETREE.XMLSyntaxError as exc:
            # Recovery exhausted - surface as stdlib ParseError so the
            # engine-level catch in ``scanners/insider/engine.py`` continues
            # to emit a single ``ParseError`` warning instead of two distinct
            # exception types.
            raise ET.ParseError(str(exc)) from exc
        if root is None:
            # lxml's ``recover=True`` returns ``None`` instead of raising
            # when no root element can be salvaged (empty body, plain-text
            # 404 page from a misrouted SEC mirror, or any other input with
            # no recoverable root). Without this guard, ``parse_ownership_xml``
            # would crash on the very first ``root.findall(...)`` with a
            # confusing ``AttributeError: 'NoneType' object has no
            # attribute 'findall'`` instead of the expected ``ParseError``.
            # Surface as ``ParseError`` so the engine-level catch keeps
            # treating this uniformly with the syntax-error case.
            raise ET.ParseError(
                "lxml XMLParser(recover=True) returned no root element "
                f"from input (first 80 chars): {_one_line(xml_text[:80])!r}"
            )
        # Defence-in-depth: lxml's recover=True can return a valid Element
        # whose root tag is anything other than ``<ownershipDocument>``
        # (an HTML-style body from a misrouted SEC mirror, a ``<error>``
        # XML envelope, a partial subtree the parser recovered enough to
        # anchor but not enough to identify as the filing format). Without
        # this check, ``parse_ownership_xml`` would produce silently empty
        # filings (issuer_cik="", no transactions, no owners). The
        # ``split("}", 1)[-1]`` (a.k.a. Clark notation local name) pattern
        # handles both the bare tag and any XBRL namespace prefix uniformly.
        local_name = root.tag.split("}", 1)[-1]
        if local_name != "ownershipDocument":
            raise ET.ParseError(
                f"unexpected root tag {root.tag!r} (expected <ownershipDocument>); "
                f"input first 80 chars: {_one_line(xml_text[:80])!r}"
            )
        return root
    return ET.fromstring(xml_text)


def _one_line(text: str) -> str:
    """Collapse newlines/CRs to escaped ``\\n``/``\\r`` so log lines stay single-line."""
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def parse_ownership_xml(xml_text: str, *, accession: str) -> ParsedOwnershipFiling:
    root = _parse_root(xml_text)
    footnotes = {
        footnote.attrib.get("id", ""): _text(footnote)
        for footnote in root.findall(".//footnote")
        if footnote.attrib.get("id")
    }

    owners: list[ReportingOwner] = []
    for owner in root.findall(".//reportingOwner"):
        relationship = owner.find("./reportingOwnerRelationship")
        owners.append(
            ReportingOwner(
                cik=_text(owner.find("./reportingOwnerId/rptOwnerCik")),
                name=_text(owner.find("./reportingOwnerId/rptOwnerName")),
                is_director=_text(relationship.find("./isDirector")).strip() == "1" if relationship is not None else False,
                is_officer=_text(relationship.find("./isOfficer")).strip() == "1" if relationship is not None else False,
                is_ten_percent_owner=_text(relationship.find("./isTenPercentOwner")).strip() == "1" if relationship is not None else False,
                officer_title=_text(relationship.find("./officerTitle")) if relationship is not None else "",
            )
        )

    transactions: list[NonDerivativeTransaction] = []
    for transaction in root.findall(".//nonDerivativeTransaction"):
        security_title = _text(transaction.find("./securityTitle/value"))
        transaction_date = _text(transaction.find("./transactionDate/value"))
        transaction_code = _text(transaction.find("./transactionCoding/transactionCode"))
        acquired_disposed = _text(transaction.find("./transactionAmounts/transactionAcquiredDisposedCode/value"))
        shares = _float_text(transaction.find("./transactionAmounts/transactionShares/value")) or 0.0
        price = _float_text(transaction.find("./transactionAmounts/transactionPricePerShare/value")) or 0.0
        shares_after = _float_text(transaction.find("./postTransactionAmounts/sharesOwnedFollowingTransaction/value"))
        direct_or_indirect = _text(transaction.find("./ownershipNature/directOrIndirectOwnership/value"))
        footnote_ids = [
            footnote_id.attrib.get("id", "")
            for footnote_id in transaction.findall(".//footnoteId")
            if footnote_id.attrib.get("id", "")
        ]
        transactions.append(
            NonDerivativeTransaction(
                security_title=security_title,
                transaction_date=transaction_date,
                transaction_code=transaction_code,
                acquired_disposed=acquired_disposed,
                shares=shares,
                price_per_share=price,
                shares_owned_after=shares_after,
                direct_or_indirect=direct_or_indirect,
                footnotes=[footnotes.get(footnote_id, "") for footnote_id in footnote_ids if footnotes.get(footnote_id, "")],
            )
        )

    acceptance = _text(root.find(".//periodOfReport"))
    acceptance_dt = acceptance
    period_text = _text(root.find(".//periodOfReport"))
    if period_text:
        try:
            acceptance_dt = datetime.fromisoformat(period_text).isoformat()
        except ValueError:
            acceptance_dt = period_text

    return ParsedOwnershipFiling(
        accession=accession,
        issuer_cik=_text(root.find(".//issuer/issuerCik")),
        issuer_ticker=_text(root.find(".//issuer/issuerTradingSymbol")),
        acceptance_datetime=acceptance_dt,
        reporting_owners=owners,
        transactions=transactions,
        raw_footnotes=footnotes,
    )
