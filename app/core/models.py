"""Contract models shared by store, trace, tools, vlm and the API (W0; mirrors schemas/*.json).

Change only through the contract: a card that needs a field writes it into its report.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCENE_KEY_RE = re.compile(r"^[a-z0-9_-]+$")
ENTITY_ID_RE = re.compile(r"^[PFV][0-9]+$")

Workspace = Literal["warehouse", "fleet", "street"]
Level = Literal["L1", "L2", "L3"]
Scope = Literal["scene", "corpus"]
Tag = Literal["warehouse", "fleet", "fixed"]
EntityKind = Literal["person", "forklift", "vehicle"]
EvidenceKind = Literal["measure", "map", "model", "human"]
ExecState = Literal["cached", "live", "precomputed", "waiting", "failed"]
AgentState = Literal["parse", "plan", "retrieve", "inspect", "measure", "assess", "answer"]
Aggregate = Literal["SUPPORTED", "CONTRADICTED", "UNRESOLVED"]
Final = Literal["SUPPORTED", "CONTRADICTED", "UNRESOLVED", "UNSUPPORTED", "NO_MATCH", "OUT_OF_RANGE"]
StopReason = Literal["enough_evidence", "no_new_info", "budget_exhausted", "abstained", "error"]
Template = Literal["find", "where_is", "what_happened", "link", "unsupported"]
Backend = Literal["duckdb", "vastdb", "vss"]
CosmosTask = Literal["C1", "C2", "C3", "C4", "C5"]


def scene_key_of(scene_id: str) -> str:
    """`wh/sdg/run_003` -> `wh-sdg-run_003`; manifests store both and schema_check enforces this mapping."""
    return scene_id.replace("/", "-").lower()


ABSTAIN_TEXT: dict[str, str] = {
    "no_geometry": "This camera has no geometry, so distances, speeds and positions are not judged.",
    "scene_not_metric": "This scene's calibration is not metric, so meters are not given.",
    "no_track_ids": "Detections on this camera have no track ids, so one person cannot be followed across frames.",
    "no_gps": "This camera has no GPS or speed data.",
    "no_visibility": "This scene has no camera visibility grid, so blind spots are not checked.",
}
CAPABILITIES = ("distance", "ground_position", "map", "blind_spot", "tracks", "speed", "boxes_2d", "caption", "cosmos")


def level_of(manifest: dict) -> Level:
    """Evidence level: manifest evidence_level if set; street -> L1; metric warehouse -> L3; else L2."""
    if manifest.get("evidence_level"):
        return manifest["evidence_level"]
    if manifest["workspace"] == "street":
        return "L1"
    if manifest["workspace"] == "warehouse" and manifest["metric"]:
        return "L3"
    return "L2"


def tag_of(manifest: dict) -> Tag:
    """Source tag: warehouse and fleet by workspace; a street scene is always fixed."""
    if manifest["workspace"] != "street":
        return manifest["workspace"]
    return "fixed"


def capabilities(manifest: dict) -> dict[str, dict]:
    """{cap: {ok, reason, text}}; the one source of the agent's abstain reasons and the UI's "cannot prove" lines."""
    street, a = manifest["workspace"] == "street", manifest["assets"]
    spatial = "no_geometry" if street else None if manifest["metric"] else "scene_not_metric"
    reasons = {
        "distance": spatial,
        "ground_position": "no_geometry" if street else None,
        "map": "no_geometry" if street else None,
        "blind_spot": "no_geometry" if street else None if "visibility" in a else "no_visibility",
        "tracks": "no_track_ids" if street else None,
        "speed": None if manifest["workspace"] == "fleet" and "trip" in a else "no_gps",
    }
    out = {}
    for cap in CAPABILITIES:
        if cap in reasons:
            r = reasons[cap]
            out[cap] = {"ok": r is None, "reason": r, "text": ABSTAIN_TEXT.get(r)}
        else:  # image-only capabilities need nothing but camera video
            out[cap] = {"ok": bool(manifest["cams"]), "reason": None, "text": None}
    return out


def _digest(parts: Any, n: int) -> str:
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:n]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityRef(_M):
    scene_key: str
    entity_id: str
    kind: EntityKind


class ObservationRef(_M):
    scene_key: str
    cam_id: str
    local_track_id: int
    t_ms: int


def make_eid(scene_key: str, cam_id: str | None, t0_ms: int, t1_ms: int, observation_ids: list[ObservationRef],
             analysis_rev: str, method: str, params: dict | None = None) -> str:
    """Deterministic evidence id. method + params are part of the key so two measurements over the same observations differ."""
    obs = sorted([o.scene_key, o.cam_id, o.local_track_id, o.t_ms] for o in observation_ids)
    return "e_" + _digest([scene_key, cam_id, t0_ms, t1_ms, obs, analysis_rev, method, params or {}], 16)


class Evidence(_M):
    model_config = ConfigDict(extra="forbid", frozen=True)
    eid: str
    scene_key: str
    cam_id: str | None
    t0_ms: int
    t1_ms: int
    observation_ids: list[ObservationRef]
    analysis_rev: str
    kind: EvidenceKind
    method: str
    value: Any = None
    unit: str | None = None
    sigma: float | None = None
    artifact_ref: str | None = None
    evidence_level: Level | None = None  # not part of the eid

    @classmethod
    def create(cls, *, scene_key: str, cam_id: str | None, t0_ms: int, t1_ms: int, observation_ids: list[ObservationRef],
               analysis_rev: str, kind: EvidenceKind, method: str, params: dict | None = None, **rest: Any) -> "Evidence":
        eid = make_eid(scene_key, cam_id, t0_ms, t1_ms, observation_ids, analysis_rev, method, params)
        return cls(eid=eid, scene_key=scene_key, cam_id=cam_id, t0_ms=t0_ms, t1_ms=t1_ms, observation_ids=observation_ids,
                   analysis_rev=analysis_rev, kind=kind, method=method, **rest)


class InputRange(_M):
    scene_key: str
    cam_id: str | None = None
    t0_ms: int
    t1_ms: int


class Envelope(_M):
    """Every tool returns this (§2.6 item 4). pts_read = frames or samples actually read."""
    input_range: InputRange
    pts_read: int
    model_rev: str | None
    output: Any
    coverage: float | None
    gaps: list[tuple[int, int, str]]
    quality: str | None
    elapsed_ms: int
    exec_state: ExecState
    error: str | None = None


class ToolResult(_M):
    envelope: Envelope
    evidence: list[Evidence]


class MomentCard(_M):
    moment_id: str
    scene_key: str
    t0_ms: int
    t1_ms: int
    cams: list[str]
    primary_cam: str
    actor_refs: list[EntityRef]
    evidence_ids: list[str]
    score: float
    why: list[str]
    thumb: str | None
    exec_state: ExecState | None = None
    backend: Backend | None = None
    clip_id: str | None = None
    vss_rank: int | None = None
    time_filter: dict | None = None
    workspace: Workspace | None = None
    evidence_level: Level | None = None
    tag: Tag | None = None
    title: str | None = None


class Predicate(_M):
    predicate_id: str
    subject_refs: list[EntityRef]
    operator: str
    value: Any
    unit: str | None
    status: Literal["supported", "contradicted", "unknown"]
    eids: list[str]


def aggregate_of(predicates: list[Predicate]) -> Aggregate:
    """§8.3: any contradicted -> CONTRADICTED; all supported -> SUPPORTED; else UNRESOLVED."""
    if any(p.status == "contradicted" for p in predicates):
        return "CONTRADICTED"
    if predicates and all(p.status == "supported" for p in predicates):
        return "SUPPORTED"
    return "UNRESOLVED"


class Claim(_M):
    run_id: str
    claim_id: str
    text: str
    predicates: list[Predicate]
    aggregate: Aggregate
    before: Aggregate | None
    after: Aggregate
    examined_eids: list[str]
    available_eids: list[str]
    explain: dict | None = None  # may hold abstain = {reason, text}; such a claim does not count toward status.final
    scene_key: str | None = None
    moment_id: str | None = None
    evidence_level: Level | None = None


class CosmosVerification(_M):
    status: Literal["verified", "unverified", "conflict"]
    method: str
    ref: str | None


class FrameRef(_M):
    cam_id: str
    t_ms: int


def make_record_id(scene_key: str, task: str, t_ms: int, cam_id: str | None, actor_ids: list[str], prompt_rev: str) -> str:
    return "c_" + _digest([scene_key, task, t_ms, cam_id, sorted(actor_ids), prompt_rev], 12)


class CosmosRecord(_M):
    record_id: str
    task: CosmosTask
    t_ms: int
    cam_id: str | None
    actor_refs: list[EntityRef]
    input_frame_refs: list[FrameRef]
    prefix_only: bool
    model_rev: str
    prompt_rev: str
    payload: dict
    raw: dict | None = None
    verification: CosmosVerification


class Query(_M):
    """Parser output (§8.1 parse)."""
    template: Template
    entities: list[str]
    t_range: tuple[int, int] | None
    place: str | None
    zone: list[tuple[float, float]] | None
    scene_key: str | None
    workspace: Workspace
    abstain_reason: str | None = None
    scope: Scope = "scene"
    sources: list[Tag] | None = None


class SearchFilters(_M):
    workspace: Workspace
    scene_keys: list[str] | None = None
    t_range: tuple[int, int] | None = None
    place_id: str | None = None
    zone_id: str | None = None
    cam_ids: list[str] | None = None
    k: int = 50


class Clip(_M):
    """A row of index `clips` plus per-query ranks; similarities are raw cosine values."""
    clip_id: str
    scene_id: str
    scene_key: str
    workspace: Workspace
    cam_id: str
    t0_ms: int
    t1_ms: int
    capture_ms: int | None = None
    upload_ms: int | None = None
    analyzed_ms: int | None = None
    vss_source: str | None = None
    caption: str | None = None
    n_person: int | None = None
    n_vehicle: int | None = None
    lat: float | None = None
    lon: float | None = None
    heading: float | None = None
    speed: float | None = None
    place_id: str | None = None
    zone_ids: list[str] = Field(default_factory=list)
    video_uri: str | None = None
    sim_text: float | None = None
    sim_visual: float | None = None
    rank_text: int | None = None
    rank_visual: int | None = None
    # ranks among the live vss-collection rows mapped to this clip
    sim_vss_text: float | None = None
    sim_vss_visual: float | None = None
    rank_vss_text: int | None = None
    rank_vss_visual: int | None = None


class Event(_M):
    event_id: str  # "<scene_key>:<events.json id>"
    scene_key: str
    type: str
    t0_ms: int
    t1_ms: int
    actor_ids: list[str]
    place_id: str | None = None
    severity: int
    source: Literal["rule", "gt", "model"]
    params: dict = Field(default_factory=dict)
    clip_ids: list[str] = Field(default_factory=list)
    eids: list[str] = Field(default_factory=list)


class PersonAtMoment(_M):
    entity_id: str
    root_world: tuple[float, float]
    root_method: Literal["tri", "ankle", "boxbottom"]
    sigma_m: float
    xyz17: list[list[float] | None] | None = None
    seen_by: list[str]      # cameras whose floor cell under the root is visible (geometry)
    observed_in: list[str]  # cameras with a 2D observation at this time


class ForkliftAtMoment(_M):
    entity_id: str
    x: float
    y: float
    heading: float
    speed: float
    footprint: list[tuple[float, float]]
    sigma_m: float
    source: Literal["yolo_wedge", "gt"]


class CamFrame(_M):
    id: str
    frame_url: str | None


class Moment(_M):
    """GET /api/scene/{scene_key}/moment?t= response."""
    t_ms: int
    persons: list[PersonAtMoment]
    forklift: list[ForkliftAtMoment] = Field(default_factory=list)
    floor_visibility_ref: str | None
    cams: list[CamFrame]


class Entity(_M):
    entity_id: str
    kind: EntityKind
    observations: list[dict]
    association: dict


class BlobRef(_M):
    uri: str
    sha256: str
    size: int


class RunStep(_M):
    step_id: str
    parent_step_id: str | None
    i: int
    state: AgentState
    name: str
    started_ms: int
    elapsed_ms: int
    input: Any
    output: Any
    evidence_ids: list[str]
    note: str | None = None
    exec_state: ExecState
    weave_call_id: str | None = None
    trace_delivery_state: Literal["local", "weave_pending", "weave_ok", "weave_failed", "disabled"]
    blob_ref: BlobRef | None = None


class AgentRun(_M):
    """Mirrors schemas/agent_run.json (GET /api/agent/runs/{run_id})."""
    run_id: str
    kind: Literal["ask", "watch"]
    q: str
    context: dict
    status: Literal["running", "done", "error", "aborted"]
    final: Final | None
    created: str
    elapsed_ms: int | None
    source_run_id: str | None
    weave_url: str | None
    model_revs: dict
    steps: list[RunStep]
    claims: list[Claim]
    evidence: list[Evidence]
    hits: list[MomentCard] = Field(default_factory=list)


class Action(_M):
    action_id: str
    scene_key: str
    event_id: str | None
    run_id: str | None
    claim_id: str | None
    auto: bool
    actions: list[Literal["create_review_task", "hazard_zone", "webhook", "wandb_alert"]]
    review_task_id: str | None
    zone_id: str | None
    delivered: bool
    created: str


class Zone(_M):
    zone_id: str
    scene_key: str
    kind: Literal["hazard", "user"]
    polygon_world: list[tuple[float, float]]
    source_event_id: str | None
    action_id: str | None
    t_range: tuple[int, int] | None
    created: str


class TriggerRequest(_M):
    """POST /api/actions/trigger body (§7.2)."""
    scene_key: str
    event_id: str | None = None
    run_id: str | None = None
    claim_id: str | None = None
    auto: bool
    actions: list[Literal["create_review_task", "hazard_zone", "webhook", "wandb_alert"]]


class TriggerResult(_M):
    action_id: str
    delivered: bool
    review_task_id: str | None
    zone_id: str | None
    existing: bool  # true when (scene_key, event_id) was already triggered and the stored action is returned
