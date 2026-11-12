#!/usr/bin/env python3
"""Refresh the public BRO roster from MightPulse without publishing game IDs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data.json"
API_URL = "https://api.mightpulse.com/v1/alliances/671/BRO"
MINIMUM_ROSTER_SIZE = 20


def api_get(api_key: str) -> dict:
    query = urllib.parse.urlencode(
        {
            "include": "info,members_full",
            "wait": "30",
            "max_age": "3600",
        }
    )
    request = urllib.request.Request(
        f"{API_URL}?{query}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "BRO-671-roster-refresh/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read(500).decode("utf-8", "replace")
        raise RuntimeError(f"MightPulse returned HTTP {error.code}: {detail}") from error


def pick(source: dict | None, fields: tuple[str, ...]) -> dict:
    source = source or {}
    return {field: source.get(field) for field in fields if field in source}


def sanitize_profile(raw: dict) -> dict:
    player = raw.get("player") or {}
    alliance = player.get("alliance") or {}
    ranks = raw.get("ranks") or {}
    heroes = raw.get("heroes") or []
    gear = raw.get("gov_gear") or {}

    public_player = pick(
        player,
        (
            "nick_name",
            "kid",
            "power",
            "town_center_level",
            "kills",
            "avatar_url",
        ),
    )
    public_player["alliance"] = pick(
        alliance, ("abbr", "name", "rank", "rank_label")
    )

    public_ranks = pick(
        ranks,
        (
            "power_rank",
            "kills_rank",
            "town_center_rank",
            "migrant_score",
            "migrant_rank",
            "mystic_trial",
            "mystic_rank",
        ),
    )
    public_ranks["leaderboards"] = [
        pick(
            board,
            (
                "name",
                "value",
                "value_label",
                "kingdom_rank",
                "kingdom_rank_label",
                "rank_type",
            ),
        )
        for board in ranks.get("leaderboards") or []
    ]

    public_heroes = [
        pick(
            hero,
            ("name", "level", "stars", "star_label", "icon", "position"),
        )
        for hero in heroes
    ]

    public_items = []
    for item in gear.get("items") or []:
        public_item = pick(
            item,
            (
                "slot",
                "name",
                "equipid",
                "quality",
                "tier",
                "star",
                "strength_level",
                "score",
                "combat",
                "icon",
            ),
        )
        public_item["gems"] = [pick(gem, ("slot", "id")) for gem in item.get("gems") or []]
        public_items.append(public_item)

    public_gear = pick(gear, ("hidden", "message"))
    public_gear["items"] = public_items
    return {
        "player": public_player,
        "ranks": public_ranks,
        "heroes": public_heroes,
        "gov_gear": public_gear,
    }


def unwrap_member(member: dict) -> tuple[str, dict]:
    raw = member.get("profile") if isinstance(member.get("profile"), dict) else member
    player = raw.get("player") or {}
    governor_id = (
        member.get("governor_id")
        or raw.get("governor_id")
        or player.get("governor_id")
        or player.get("fid")
    )
    if not governor_id or not player.get("nick_name"):
        raise ValueError("Alliance member is missing a governor ID or player name")
    return str(governor_id), raw


def identity_key(api_key: str, governor_id: str) -> str:
    digest = hmac.new(
        api_key.encode("utf-8"), governor_id.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"h1_{digest}"


def alliance_info(payload: dict) -> dict:
    alliance = payload.get("alliance") or {}
    return pick(
        alliance,
        ("name", "abbr", "kid", "power", "count", "leader_name", "power_rank"),
    )


def main() -> int:
    api_key = os.environ.get("MIGHTPULSE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("MIGHTPULSE_API_KEY is not configured")

    existing = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    payload = api_get(api_key)
    members = payload.get("members") or payload.get("players") or []
    if len(members) < MINIMUM_ROSTER_SIZE:
        raise RuntimeError(
            f"Refusing to replace the roster with only {len(members)} API members"
        )

    existing_rows = existing.get("players") or []
    by_identity = {
        row["identity_key"]: row
        for row in existing_rows
        if row.get("identity_key")
    }
    by_name: dict[str, list[dict]] = {}
    for row in existing_rows:
        name = (row.get("profile") or {}).get("player", {}).get("nick_name")
        if name:
            by_name.setdefault(name, []).append(row)

    next_public_id = max((int(row.get("public_id") or 0) for row in existing_rows), default=0) + 1
    refreshed: list[dict] = []
    added = 0

    for member in members:
        governor_id, raw_profile = unwrap_member(member)
        key = identity_key(api_key, governor_id)
        name = raw_profile["player"]["nick_name"]
        prior = by_identity.get(key)
        if prior is None and len(by_name.get(name, [])) == 1:
            prior = by_name[name][0]

        if prior is None:
            public_id = next_public_id
            next_public_id += 1
            prior = {}
            added += 1
        else:
            public_id = int(prior["public_id"])

        origin = "INK" if prior.get("origin") == "INK" else "BRO"
        refreshed.append(
            {
                "public_id": public_id,
                "identity_key": key,
                "origin": origin,
                "membership": "current",
                "profile": sanitize_profile(raw_profile),
                "swordland": bool(prior.get("swordland") or prior.get("swordland_role")),
                "swordland_role": prior.get("swordland_role") or "",
            }
        )

    current_ids = {row["public_id"] for row in refreshed}
    former_count = 0
    guest_count = 0
    for prior in existing_rows:
        public_id = int(prior["public_id"])
        if public_id in current_ids:
            continue
        row = dict(prior)
        if row.get("origin") == "INK":
            row["membership"] = "guest"
            guest_count += 1
        else:
            row["membership"] = "former"
            row["swordland"] = False
            row["swordland_role"] = ""
            former_count += 1
        refreshed.append(row)

    refreshed.sort(key=lambda row: int(row["public_id"]))
    alliance = alliance_info(payload)
    if not alliance:
        alliance = (existing.get("alliance_roster") or {}).get("alliance") or {}

    output = {
        "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "MightPulse authorized API — privacy-filtered public fields",
        "request": {
            "kingdom": 671,
            "alliance": "BRO",
            "include": ["info", "members_full"],
        },
        "alliance_roster": {"alliance": alliance},
        "refresh_meta": {
            "current_bro": len(members),
            "tracked_ink_guests": guest_count,
            "former_bro": former_count,
            "new_players": added,
        },
        "origin_tags": {
            "BRO": [
                row["public_id"]
                for row in refreshed
                if row.get("origin") == "BRO" and row.get("membership") != "former"
            ],
            "INK": [row["public_id"] for row in refreshed if row.get("origin") == "INK"],
            "EX_BRO": [
                row["public_id"]
                for row in refreshed
                if row.get("membership") == "former"
            ],
        },
        "players": refreshed,
    }
    DATA_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Refreshed {len(members)} current BRO; added {added}; "
        f"retained {guest_count} INK guests and {former_count} former BRO."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Roster refresh failed: {error}", file=sys.stderr)
        raise SystemExit(1)
