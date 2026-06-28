from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import requests

from scanners.congress.models import ExecutiveRole, PoliticalFiler, PoliticalRole, PoliticalRoleResolution


logger = logging.getLogger(__name__)

LEGISLATORS_CURRENT_URL = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
LEGISLATORS_HISTORICAL_URL = "https://unitedstates.github.io/congress-legislators/legislators-historical.json"
COMMITTEES_CURRENT_URL = "https://unitedstates.github.io/congress-legislators/committees-current.json"
COMMITTEE_MEMBERSHIP_CURRENT_URL = "https://unitedstates.github.io/congress-legislators/committee-membership-current.json"

MAPPINGS_DIR = Path(__file__).with_name("mappings")

EXECUTIVE_SENIORITY = {
    "president": ("PRESIDENT", 1.00),
    "vice_president": ("VICE_PRESIDENT", 0.95),
    "cabinet_secretary": ("CABINET_SECRETARY", 1.00),
    "cabinet_level": ("CABINET_LEVEL", 1.00),
    "deputy_secretary": ("DEPUTY_SECRETARY", 0.85),
    "senior_white_house": ("SENIOR_WHITE_HOUSE", 0.85),
    "level_i": ("LEVEL_I", 1.00),
    "level_ii": ("LEVEL_II", 0.85),
    "level_iii": ("LEVEL_III", 0.70),
    "level_iv": ("LEVEL_IV", 0.55),
    "other": ("OTHER_EXECUTIVE", 0.40),
    "unknown": ("UNKNOWN", 0.30),
}


def _load_json_yaml(name: str) -> dict[str, Any]:
    return json.loads((MAPPINGS_DIR / name).read_text(encoding="utf-8"))


def _normalize_name(value: str) -> str:
    return " ".join(str(value or "").replace(",", " ").split()).strip().lower()


def _normalize_agency_key(value: str) -> str:
    text = _normalize_name(value)
    return text.replace("&", "and").replace("/", " ").replace("-", " ").replace("  ", " ").replace(" ", "_")


def _cache_dir() -> Path:
    path = Path(os.getenv("POLITICAL_ROLE_CACHE_DIR", "funnel_output/political_role_cache"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_file(name: str) -> Path:
    return _cache_dir() / f"{name}.json"


def _cache_read(name: str, *, ttl_hours: float) -> dict[str, Any] | None:
    path = _cache_file(name)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return None
    saved_at = datetime.fromisoformat(str(payload.get("saved_at", "")))
    if saved_at + timedelta(hours=ttl_hours) < datetime.now(UTC):
        return {
            "saved_at": payload.get("saved_at"),
            "hash": payload.get("hash", ""),
            "data": payload.get("data"),
            "stale": True,
        }
    return {
        "saved_at": payload.get("saved_at"),
        "hash": payload.get("hash", ""),
        "data": payload.get("data"),
        "stale": False,
    }


def _cache_write(name: str, data: Any) -> tuple[datetime, str]:
    encoded = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    saved_at = datetime.now(UTC)
    payload = {
        "saved_at": saved_at.isoformat(),
        "hash": digest,
        "data": data,
    }
    path = _cache_file(name)
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        temp_path = Path(handle.name)
    temp_path.replace(path)
    return saved_at, digest


class CongressionalRoleProvider:
    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float | None = None,
        cache_ttl_hours: float | None = None,
        enabled: bool | None = None,
        payload_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds or os.getenv("POLITICAL_ROLE_REQUEST_TIMEOUT", 30))
        self.cache_ttl_hours = float(cache_ttl_hours or os.getenv("POLITICAL_ROLE_CACHE_TTL_HOURS", 24))
        self.enabled = (
            str(os.getenv("POLITICAL_ROLE_DATA_ENABLE", "true")).strip().lower() not in {"0", "false", "no", "off"}
            if enabled is None
            else bool(enabled)
        )
        self.payload_overrides = payload_overrides or {}
        self.identity_overrides = _load_json_yaml("filer_identity_overrides.yaml")

    def current_roles(
        self,
        filer: PoliticalFiler,
        *,
        as_of: datetime,
    ) -> PoliticalRoleResolution:
        if filer.branch == "executive":
            executive_role = _resolve_executive_role(filer)
            return PoliticalRoleResolution(
                filer=filer,
                status="NOT_APPLICABLE_EXECUTIVE",
                executive_role=executive_role,
            )
        if not self.enabled:
            return PoliticalRoleResolution(filer=filer, status="ROLE_SOURCE_UNAVAILABLE", error="disabled")
        try:
            datasets = self._datasets()
        except Exception as exc:
            return PoliticalRoleResolution(filer=filer, status="ROLE_SOURCE_UNAVAILABLE", error=str(exc))

        resolution = _resolve_bioguide(
            filer,
            datasets["legislators"],
            self.identity_overrides,
        )
        resolved_filer = PoliticalFiler(**{**filer.__dict__, **resolution})
        status = str(resolved_filer.identity_resolution_status or "UNRESOLVED")
        retrieved_at = datasets["retrieved_at"]
        if status in {"UNRESOLVED", "AMBIGUOUS"}:
            return PoliticalRoleResolution(
                filer=resolved_filer,
                status=status,
                source_retrieved_at=retrieved_at,
                source_payload_hash=datasets["hash"],
                stale_cache=datasets["stale"],
            )
        if as_of.date() < (retrieved_at.date() - timedelta(days=30)):
            return PoliticalRoleResolution(
                filer=resolved_filer,
                status="HISTORICAL_ROLE_UNAVAILABLE",
                source_retrieved_at=retrieved_at,
                source_payload_hash=datasets["hash"],
                stale_cache=datasets["stale"],
            )

        roles = _current_committee_roles(
            str(resolved_filer.bioguide_id or ""),
            datasets["committees"],
            datasets["membership"],
            retrieved_at=retrieved_at,
            payload_hash=datasets["hash"],
        )
        return PoliticalRoleResolution(
            filer=resolved_filer,
            status="RESOLVED" if roles else "UNMAPPED_ROLE",
            roles=tuple(roles),
            source_retrieved_at=retrieved_at,
            source_payload_hash=datasets["hash"],
            stale_cache=datasets["stale"],
        )

    def _datasets(self) -> dict[str, Any]:
        legislators = self._load_dataset("legislators_current", LEGISLATORS_CURRENT_URL)
        historical = self._load_dataset("legislators_historical", LEGISLATORS_HISTORICAL_URL)
        committees = self._load_dataset("committees_current", COMMITTEES_CURRENT_URL)
        membership = self._load_dataset("committee_membership_current", COMMITTEE_MEMBERSHIP_CURRENT_URL)
        combined_hash = hashlib.sha256(
            "|".join(str(item["hash"]) for item in (legislators, historical, committees, membership)).encode("utf-8")
        ).hexdigest()
        retrieved_at = max(item["retrieved_at"] for item in (legislators, historical, committees, membership))
        return {
            "legislators": list(legislators["data"]) + list(historical["data"]),
            "committees": committees["data"],
            "membership": membership["data"],
            "hash": combined_hash,
            "retrieved_at": retrieved_at,
            "stale": any(item["stale"] for item in (legislators, historical, committees, membership)),
        }

    def _load_dataset(self, cache_name: str, url: str) -> dict[str, Any]:
        if cache := _cache_read(cache_name, ttl_hours=self.cache_ttl_hours):
            return {
                "data": cache["data"],
                "hash": str(cache["hash"]),
                "retrieved_at": datetime.fromisoformat(str(cache["saved_at"])),
                "stale": bool(cache["stale"]),
            }
        if cache_name in self.payload_overrides:
            retrieved_at, digest = _cache_write(cache_name, self.payload_overrides[cache_name])
            return {"data": self.payload_overrides[cache_name], "hash": digest, "retrieved_at": retrieved_at, "stale": False}
        data = self._request_json(url)
        retrieved_at, digest = _cache_write(cache_name, data)
        return {"data": data, "hash": digest, "retrieved_at": retrieved_at, "stale": False}

    def _request_json(self, url: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=self.timeout_seconds)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                if attempt >= 2:
                    raise
                time.sleep(min(2 ** attempt, 5))
        raise RuntimeError(str(last_error or "role request failed"))


def _resolve_bioguide(
    filer: PoliticalFiler,
    legislators: list[dict[str, Any]],
    overrides: dict[str, Any],
) -> dict[str, str]:
    normalized_name = _normalize_name(filer.filer_name)
    by_name: list[dict[str, Any]] = []
    by_name_state: list[dict[str, Any]] = []
    override_map = dict(overrides.get("kadoa_to_bioguide", {}))
    if filer.filer_id in override_map:
        return {
            "bioguide_id": str(override_map[filer.filer_id]),
            "identity_resolution_status": "EXPLICIT_OVERRIDE",
        }
    for item in legislators:
        identity = item.get("id", {})
        bioguide = str(identity.get("bioguide") or "").strip()
        if not bioguide:
            continue
        names = {str(item.get("name", {}).get("official_full") or "").strip()}
        names.add(" ".join(part for part in [item.get("name", {}).get("first"), item.get("name", {}).get("last")] if part))
        for alias in item.get("other_names", []):
            if isinstance(alias, dict):
                names.add(str(alias.get("official_full") or "").strip())
                names.add(" ".join(part for part in [alias.get("first"), alias.get("last")] if part))
        normalized_names = {_normalize_name(name) for name in names if name.strip()}
        if normalized_name in normalized_names:
            by_name.append(item)
            latest_term = item.get("terms", [])[-1] if item.get("terms") else {}
            state = str(latest_term.get("state") or "").strip().upper()
            chamber = "house" if latest_term.get("type") == "rep" else "senate" if latest_term.get("type") == "sen" else ""
            if (not filer.state or filer.state.upper() == state) and (not filer.chamber or filer.chamber.lower() == chamber):
                by_name_state.append(item)
    if len(by_name) == 1:
        return {
            "bioguide_id": str(by_name[0]["id"].get("bioguide") or ""),
            "identity_resolution_status": "EXACT_NAME",
        }
    if len(by_name_state) == 1:
        return {
            "bioguide_id": str(by_name_state[0]["id"].get("bioguide") or ""),
            "identity_resolution_status": "NAME_CHAMBER_STATE",
        }
    if len(by_name) > 1:
        return {"bioguide_id": "", "identity_resolution_status": "AMBIGUOUS"}
    return {"bioguide_id": "", "identity_resolution_status": "UNRESOLVED"}


def _current_committee_roles(
    bioguide_id: str,
    committees: list[dict[str, Any]],
    membership: dict[str, list[dict[str, Any]]],
    *,
    retrieved_at: datetime,
    payload_hash: str,
) -> list[PoliticalRole]:
    roles: list[PoliticalRole] = []
    committee_by_id = {str(item.get("thomas_id") or ""): item for item in committees}
    subcommittee_lookup: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for committee in committees:
        parent_id = str(committee.get("thomas_id") or "")
        for subcommittee in committee.get("subcommittees", []):
            child_id = f"{parent_id}{str(subcommittee.get('thomas_id') or '')}"
            subcommittee_lookup[child_id] = (committee, subcommittee)
    for organisation_id, members in membership.items():
        match = next((member for member in members if str(member.get("bioguide") or "") == bioguide_id), None)
        if match is None:
            continue
        title = str(match.get("title") or "").strip()
        rank = int(match["rank"]) if str(match.get("rank", "")).isdigit() else None
        seniority_class = _seniority_from_title(title, organisation_id, rank)
        if organisation_id in committee_by_id:
            committee = committee_by_id[organisation_id]
            roles.append(
                PoliticalRole(
                    role_type="COMMITTEE",
                    organisation_id=organisation_id,
                    organisation_name=str(committee.get("name") or ""),
                    parent_organisation_id=None,
                    parent_organisation_name=None,
                    title=title,
                    rank=rank,
                    seniority_class=seniority_class,
                    source="congress-legislators",
                    source_retrieved_at=retrieved_at,
                    source_payload_hash=payload_hash,
                )
            )
        elif organisation_id in subcommittee_lookup:
            parent, subcommittee = subcommittee_lookup[organisation_id]
            roles.append(
                PoliticalRole(
                    role_type="SUBCOMMITTEE",
                    organisation_id=organisation_id,
                    organisation_name=str(subcommittee.get("name") or ""),
                    parent_organisation_id=str(parent.get("thomas_id") or ""),
                    parent_organisation_name=str(parent.get("name") or ""),
                    title=title,
                    rank=rank,
                    seniority_class=seniority_class,
                    source="congress-legislators",
                    source_retrieved_at=retrieved_at,
                    source_payload_hash=payload_hash,
                )
            )
    return roles


def _seniority_from_title(title: str, organisation_id: str, rank: int | None) -> str:
    lower = title.lower()
    is_subcommittee = len(organisation_id) > 4
    if "chair" in lower and "vice" in lower:
        return "VICE_CHAIR"
    if "chair" in lower:
        return "SUBCOMMITTEE_CHAIR" if is_subcommittee else "CHAIR"
    if "ranking" in lower:
        return "SUBCOMMITTEE_RANKING_MEMBER" if is_subcommittee else "RANKING_MEMBER"
    if rank == 1 and is_subcommittee:
        return "SUBCOMMITTEE_MEMBER"
    if rank == 1:
        return "COMMITTEE_MEMBER"
    return "SUBCOMMITTEE_MEMBER" if is_subcommittee else "COMMITTEE_MEMBER"


def _resolve_executive_role(filer: PoliticalFiler) -> ExecutiveRole:
    office = _normalize_name(filer.office or filer.filer_name)
    agency_key = _normalize_agency_key(filer.agency or filer.office)
    level = str(filer.level or "").strip().upper()
    if "president" in office and "vice" not in office:
        seniority = EXECUTIVE_SENIORITY["president"][0]
    elif "vice president" in office:
        seniority = EXECUTIVE_SENIORITY["vice_president"][0]
    elif "white house" in _normalize_name(filer.agency):
        seniority = EXECUTIVE_SENIORITY["senior_white_house"][0]
    elif "secretary" in office and "deputy" not in office:
        seniority = EXECUTIVE_SENIORITY["cabinet_secretary"][0]
    elif "deputy" in office:
        seniority = EXECUTIVE_SENIORITY["deputy_secretary"][0]
    elif level in {"I", "LEVEL I", "1"}:
        seniority = EXECUTIVE_SENIORITY["level_i"][0]
    elif level in {"II", "LEVEL II", "2"}:
        seniority = EXECUTIVE_SENIORITY["level_ii"][0]
    elif level in {"III", "LEVEL III", "3"}:
        seniority = EXECUTIVE_SENIORITY["level_iii"][0]
    elif level in {"IV", "LEVEL IV", "4"}:
        seniority = EXECUTIVE_SENIORITY["level_iv"][0]
    elif filer.agency:
        seniority = EXECUTIVE_SENIORITY["other"][0]
    else:
        seniority = EXECUTIVE_SENIORITY["unknown"][0]
    return ExecutiveRole(
        agency=filer.agency,
        agency_key=agency_key,
        level=level,
        seniority_class=seniority,
        confidence="MEDIUM" if filer.agency or filer.level or filer.office else "LOW",
    )

