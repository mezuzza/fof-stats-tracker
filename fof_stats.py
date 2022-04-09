#!/usr/bin/env python
"""
Loads data from match timeline and computes stats.
"""
import asyncio
import os
import sys
import typing

from collections import defaultdict
from dataclasses import dataclass, field

from pyot.conf.model import activate_model, ModelConf
from pyot.conf.pipeline import activate_pipeline, PipelineConf


@activate_model("lol")
# pylint: disable-next=too-few-public-methods
class LolModel(ModelConf):
    """configuration for lol model for pyot"""

    default_platform = "na1"
    default_region = "americas"
    default_version = "latest"
    default_locale = "en_us"


@activate_pipeline("lol")
# pylint: disable-next=too-few-public-methods
class LolPipeline(PipelineConf):
    """lol pipeline config with in memory cache."""

    name = "lol_main"
    default = True
    stores = [
        {
            "backend": "pyot.stores.omnistone.Omnistone",
            "expirations": {
                "summoner_v4_by_name": 100,
                "match_v4_match": 600,
                "match_v4_timeline": 600,
            },
        },
        {
            "backend": "pyot.stores.riotapi.RiotAPI",
            "api_key": os.environ["RIOT_API_KEY"],
        },
    ]


# pylint: disable-next=wrong-import-position
from pyot.models import lol


@dataclass
class LaneStats:
    """lane stats"""

    plates: int = 0
    towers: int = 0
    inhib_down: bool = False


@dataclass
class MidLaneStats(LaneStats):
    """mid lane also has nexus towers stats"""

    nexus_towers = 0


@dataclass
class TeamStats:
    """team stats"""

    dragons: int = 0
    heralds: int = 0
    top: LaneStats = field(default_factory=LaneStats)
    mid: MidLaneStats = field(default_factory=MidLaneStats)
    bot: LaneStats = field(default_factory=LaneStats)


@dataclass
# pylint: disable-next=too-many-instance-attributes
class ParticipantStats:
    """participant stats"""

    kills: int = 0
    deaths: int = 0
    assists: int = 0
    minions_killed: int = 0
    monsters_killed: int = 0
    gold: int = 0
    # pylint: disable-next=invalid-name
    xp: int = 0
    level: int = 0


@dataclass
class MatchStats:
    """match stats"""

    participant_stats: dict[int, ParticipantStats]
    participant_names: dict[int, str]
    first_blood_by: int = 0
    blue: TeamStats = field(default_factory=TeamStats)
    red: TeamStats = field(default_factory=TeamStats)


def get_lane(team_id: int, lane_type: str, match_stats: MatchStats) -> LaneStats:
    """Gets the lane that needs to be modified"""
    match (team_id, lane_type):
        case (100, "TOP_LANE"):
            return match_stats.blue.top
        case (100, "MID_LANE"):
            return match_stats.blue.mid
        case (100, "BOT_LANE"):
            return match_stats.blue.bot
        case (200, "TOP_LANE"):
            return match_stats.red.top
        case (200, "MID_LANE"):
            return match_stats.red.mid
        case (200, "BOT_LANE"):
            return match_stats.red.bot
        case _:
            raise ValueError(f"Team or lane assignment invalid: {team_id}, {lane_type}")


def handle_building_kill(event: lol.match.TimelineEventData, match_stats: MatchStats):
    """handle BUILDING_KILL event"""
    lane = get_lane(event.team_id, event.lane_type, match_stats)
    match event.building_type:
        case "TOWER_BUILDING":
            match event.tower_type:
                case "OUTER_TURRET":
                    lane.towers = 1
                case "INNER_TURRET":
                    lane.towers = 2
                case "BASE_TURRET":
                    lane.towers = 3
                case "NEXUS_TURRET":
                    midlane = typing.cast(MidLaneStats, lane)
                    midlane.nexus_towers += 1
        case "INHIBITOR_BUILDING":
            lane.inhib_down = True


def handle_champ_kill(event: lol.match.TimelineEventData, match_stats: MatchStats):
    """handle CHAMPION_KILL event"""
    try:
        for i in event.assisting_participant_ids:
            match_stats.participant_stats[i].assists += 1
    except AttributeError:
        pass

    match_stats.participant_stats[event.killer_id].kills += 1
    match_stats.participant_stats[event.victim_id].deaths += 1


def handle_champ_special_kill(
    event: lol.match.TimelineEventData, match_stats: MatchStats
):
    """handle CHAMPION_SPECIAL_KILL event"""
    if event.kill_type == "KILL_FIRST_BLOOD":
        match_stats.first_blood_by = event.killer_id


# pylint: disable-next=unused-argument
def handle_elite_monster_kill(
    event: lol.match.TimelineEventData, match_stats: MatchStats
):
    """handle ELITE_MONSTER_KILL event"""
    team_stats = match_stats.blue if event.killer_team_id == 100 else match_stats.red
    match event.monster_type:
        case "DRAGON":
            team_stats.dragons += 1
        case "RIFTHERALD":
            team_stats.heralds += 1


def handle_turret_plate_destroyed(
    event: lol.match.TimelineEventData, match_stats: MatchStats
):
    """handle TURRET_PLATE_DESTROYED event"""
    lane = get_lane(event.team_id, event.lane_type, match_stats)
    lane.plates += 1


# pylint: disable-next=unused-argument
def handle_default_event(event: lol.match.TimelineEventData, match_stats: MatchStats):
    """handles events when there is no special handler for them."""


EVENT_HANDLERS = defaultdict(
    lambda: handle_default_event,
    {
        "BUILDING_KILL": handle_building_kill,
        "CHAMPION_KILL": handle_champ_kill,
        "CHAMPION_SPECIAL_KILL": handle_champ_special_kill,
        "ELITE_MONSTER_KILL": handle_elite_monster_kill,
        "TURRET_PLATE_DESTROYED": handle_turret_plate_destroyed,
    },
)


async def get_participant_names(participants: list[lol.match.TimelineParticipantData]):
    """gathers participant names from Summoner api"""
    return {
        p.id: (await lol.summoner.Summoner(puuid=p.puuid).get()).name
        for p in participants
    }


async def compute_match_stats(
    timeline: lol.match.Timeline,
) -> MatchStats:
    """computes match stats given a timeline"""
    match_stats = MatchStats(
        participant_stats={
            p.id: ParticipantStats() for p in timeline.info.participants
        },
        participant_names=await get_participant_names(timeline.info.participants),
    )

    if not timeline.info.frames:
        return match_stats

    frames = (frame for frame in timeline.info.frames if frame.timestamp < 930000)

    for frame in frames:
        for event in frame.events:
            EVENT_HANDLERS[event.type](event, match_stats)

    # pylint: disable-next=undefined-loop-variable,invalid-name
    for p in frame.participant_frames:
        stats = match_stats.participant_stats[p.participant_id]
        stats.minions_killed = p.minions_killed
        stats.monsters_killed = p.jungle_minions_killed
        stats.gold = p.total_gold
        stats.xp = p.xp
        stats.level = p.level

    return match_stats


async def main():
    """main func"""
    for match_id in sys.argv[1:]:
        timeline = await lol.match.Timeline(match_id).get()
        stats = await compute_match_stats(timeline)
        print(stats)


if __name__ == "__main__":
    asyncio.run(main())
